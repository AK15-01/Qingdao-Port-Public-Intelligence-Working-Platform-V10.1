from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable, Mapping, Optional, Sequence

from backup_workspace import restore_database_snapshot
from platform_db import connect, initialize_database, new_id, now_iso, transaction
from review_provenance import HUMAN_REVIEWER_TYPES


ROOT = Path(__file__).resolve().parent
HUMAN_TYPES = tuple(sorted(HUMAN_REVIEWER_TYPES))
ACCEPTED_REVIEW_DECISIONS = {"accept", "accept_modified", "确认", "修改后接受"}
KEY_FIELD_MAP = {
    "标题": ("title",),
    "时间": ("event_date", "affected_period"),
    "地点": ("affected_area",),
    "主体": ("involved_entities",),
    "事件类型": ("category",),
    "风险等级": ("manual_risk_level", "risk_level"),
}
COMPLIANCE_VALUES = {
    "public_access_status": {"未复核", "公开可访问", "需要登录", "禁止访问"},
    "access_frequency_compliant": {"未复核", "是", "否"},
    "personal_information_status": {"未复核", "未发现", "可能涉及", "存在"},
    "important_data_status": {"未复核", "未发现", "可能涉及", "存在"},
}


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    checks: tuple[dict[str, object], ...]
    event_ids: tuple[str, ...]

    @property
    def blockers(self) -> tuple[dict[str, object], ...]:
        return tuple(
            item
            for item in self.checks
            if item["severity"] == "错误" and not bool(item["passed"])
        )

    @property
    def warnings(self) -> tuple[dict[str, object], ...]:
        return tuple(
            item
            for item in self.checks
            if item["severity"] == "警告" and not bool(item["passed"])
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": list(self.checks),
            "event_ids": list(self.event_ids),
            "blocker_count": len(self.blockers),
            "warning_count": len(self.warnings),
        }


def _json_load(value: object, fallback):
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _percentage(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _consecutive_run_days(values: Iterable[str]) -> int:
    days = sorted(
        {
            date.fromisoformat(str(value)[:10])
            for value in values
            if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)[:10])
        },
        reverse=True,
    )
    if not days:
        return 0
    total = 1
    cursor = days[0]
    for item in days[1:]:
        if item == cursor - timedelta(days=1):
            total += 1
            cursor = item
        elif item != cursor:
            break
    return total


def human_review_accuracy(workspace_id: str, db_path) -> dict[str, object]:
    """Calculate accuracy from the latest independent human review per event."""

    initialize_database(db_path)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT r.*,e.evidence_verified FROM event_reviews r
            JOIN events e ON e.event_id=r.event_id
            WHERE r.workspace_id=? AND r.reviewer_type IN ('human_user','industry_reviewer')
            ORDER BY r.reviewed_at DESC,r.created_at DESC""",
            (workspace_id,),
        ).fetchall()
    latest: dict[str, Mapping[str, object]] = {}
    for raw in rows:
        row = dict(raw)
        latest.setdefault(str(row["event_id"]), row)
    decisions = {
        "accept": 0,
        "accept_modified": 0,
        "reject": 0,
        "body_issue": 0,
        "needs_second_review": 0,
    }
    usable: list[Mapping[str, object]] = []
    for row in latest.values():
        decision = str(row.get("decision") or "")
        decision = {"确认": "accept", "修改后接受": "accept_modified"}.get(
            decision, decision
        )
        decisions[decision] = decisions.get(decision, 0) + 1
        if decision in ACCEPTED_REVIEW_DECISIONS:
            usable.append(row)
    field_rates: dict[str, Optional[float]] = {}
    for label, fields in KEY_FIELD_MAP.items():
        correct = 0
        for row in usable:
            changed = set(_json_load(row.get("changed_fields_json"), []))
            if not any(field in changed for field in fields):
                correct += 1
        field_rates[label] = _percentage(correct, len(usable))
    evidence_correct = sum(bool(row.get("evidence_verified")) for row in usable)
    field_rates["证据"] = _percentage(evidence_correct, len(usable))
    numeric_rates = [value for value in field_rates.values() if value is not None]
    return {
        "human_review_count": len(latest),
        "accepted_sample_count": len(usable),
        "sample_sufficient": len(latest) >= 50,
        "field_accuracy": field_rates,
        "key_field_accuracy": (
            sum(numeric_rates) / len(numeric_rates) if numeric_rates else None
        ),
        "decision_counts": decisions,
        "decision_rates": {
            key: _percentage(value, len(latest)) for key, value in decisions.items()
        },
    }


def _latest_backup(project_root: Path) -> tuple[str, str]:
    candidates = [
        path
        for path in (project_root / "backups").glob("*.zip")
        if path.is_file()
    ]
    if not candidates:
        return "", ""
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    modified = datetime.fromtimestamp(latest.stat().st_mtime).astimezone()
    return latest.name, modified.isoformat(timespec="seconds")


def commercial_readiness_metrics(
    workspace_id: str,
    db_path,
    *,
    project_root: Optional[Path] = None,
) -> dict[str, object]:
    initialize_database(db_path)
    project_root = Path(project_root or ROOT)
    with connect(db_path) as connection:
        runs = connection.execute(
            """SELECT started_at,status,fetched_count,qualified_document_count,
            new_event_count FROM crawl_source_runs
            WHERE workspace_id=? ORDER BY started_at""",
            (workspace_id,),
        ).fetchall()
        run_total = len(runs)
        successful_runs = sum(str(row["status"]) == "succeeded" for row in runs)
        fetched = sum(int(row["fetched_count"] or 0) for row in runs)
        qualified = sum(int(row["qualified_document_count"] or 0) for row in runs)
        produced_events = sum(int(row["new_event_count"] or 0) for row in runs)
        event_counts = connection.execute(
            """SELECT
            COUNT(*) AS active_events,
            SUM(CASE WHEN human_verified=1
                AND reviewer_type IN ('human_user','industry_reviewer') THEN 1 ELSE 0 END)
                AS human_events,
            SUM(CASE WHEN eligible_for_customer_report=1 AND report_eligible=1
                AND human_verified=1 AND reviewer_type IN ('human_user','industry_reviewer')
                AND evidence_verified=1 THEN 1 ELSE 0 END) AS customer_events,
            SUM(CASE WHEN duplicate_level IN ('可能重复','高度疑似重复') THEN 1 ELSE 0 END)
                AS duplicate_events,
            SUM(CASE WHEN business_value IN ('高','中') OR risk_level IN ('高','中')
                OR opportunity_level IN ('高','中') THEN 1 ELSE 0 END) AS core_events,
            SUM(CASE WHEN (business_value IN ('高','中') OR risk_level IN ('高','中')
                OR opportunity_level IN ('高','中')) AND human_verified=1
                AND reviewer_type IN ('human_user','industry_reviewer') THEN 1 ELSE 0 END)
                AS reviewed_core_events,
            SUM(CASE WHEN (business_value IN ('高','中') OR risk_level IN ('高','中')
                OR opportunity_level IN ('高','中')) AND evidence_verified=1 THEN 1 ELSE 0 END)
                AS evidence_core_events
            FROM events WHERE workspace_id=? AND record_state='active'""",
            (workspace_id,),
        ).fetchone()
        delivery = connection.execute(
            """SELECT
            SUM(CASE WHEN delivery_status='delivered' AND delivery_due_at!='' THEN 1 ELSE 0 END)
                AS due_deliveries,
            SUM(CASE WHEN delivery_status='delivered' AND delivery_due_at!=''
                AND delivered_at<=delivery_due_at THEN 1 ELSE 0 END) AS on_time_deliveries
            FROM report_delivery_snapshots WHERE workspace_id=?""",
            (workspace_id,),
        ).fetchone()
        restore = connection.execute(
            """SELECT finished_at,restore_drill_id,report_file FROM restore_drills
            WHERE workspace_id=? AND status='succeeded'
            ORDER BY finished_at DESC LIMIT 1""",
            (workspace_id,),
        ).fetchone()
        severe_errors = int(
            connection.execute(
                """SELECT COUNT(*) FROM technical_logs WHERE workspace_id=?
                AND lower(level) IN ('critical','fatal','severe') AND resolved_at=''""",
                (workspace_id,),
            ).fetchone()[0]
        )
    active_events = int(event_counts["active_events"] or 0)
    core_events = int(event_counts["core_events"] or 0)
    human_events = int(event_counts["human_events"] or 0)
    accuracy = human_review_accuracy(workspace_id, db_path)
    backup_name, backup_time = _latest_backup(project_root)
    due_deliveries = int(delivery["due_deliveries"] or 0)
    metrics: dict[str, object] = {
        "consecutive_run_days": _consecutive_run_days(
            str(row["started_at"]) for row in runs
        ),
        "source_run_count": run_total,
        "source_success_rate": _percentage(successful_runs, run_total),
        "qualified_body_rate": _percentage(qualified, fetched),
        "valid_event_yield_rate": _percentage(produced_events, qualified),
        "human_review_event_count": human_events,
        "customer_core_human_review_rate": _percentage(
            int(event_counts["reviewed_core_events"] or 0), core_events
        ),
        "core_evidence_verbatim_rate": _percentage(
            int(event_counts["evidence_core_events"] or 0), core_events
        ),
        "key_field_accuracy": accuracy["key_field_accuracy"],
        "accuracy_sample_sufficient": accuracy["sample_sufficient"],
        "accuracy_details": accuracy,
        "duplicate_event_rate": _percentage(
            int(event_counts["duplicate_events"] or 0), active_events
        ),
        "report_on_time_rate": _percentage(
            int(delivery["on_time_deliveries"] or 0), due_deliveries
        ),
        "last_backup_at": backup_time,
        "last_backup_file": backup_name,
        "last_restore_drill_at": str(restore["finished_at"]) if restore else "",
        "last_restore_drill_id": str(restore["restore_drill_id"]) if restore else "",
        "customer_report_eligible_event_count": int(
            event_counts["customer_events"] or 0
        ),
        "unresolved_severe_error_count": severe_errors,
        "active_event_count": active_events,
        "core_event_count": core_events,
        "fetched_document_count": fetched,
        "qualified_document_count": qualified,
        "produced_event_count": produced_events,
    }
    common_gates = [
        ("真人审核样本至少50条", human_events >= 50, human_events, "≥50"),
        (
            "客户报告核心事件真人复核率100%",
            metrics["customer_core_human_review_rate"] == 1.0,
            metrics["customer_core_human_review_rate"],
            "100%",
        ),
        (
            "核心证据逐字定位率100%",
            metrics["core_evidence_verbatim_rate"] == 1.0,
            metrics["core_evidence_verbatim_rate"],
            "100%",
        ),
        (
            "关键字段准确率至少95%且样本充分",
            bool(metrics["accuracy_sample_sufficient"])
            and isinstance(metrics["key_field_accuracy"], float)
            and float(metrics["key_field_accuracy"]) >= 0.95,
            metrics["key_field_accuracy"],
            "≥95%，样本≥50",
        ),
        (
            "重复事件率不超过3%",
            isinstance(metrics["duplicate_event_rate"], float)
            and float(metrics["duplicate_event_rate"]) <= 0.03,
            metrics["duplicate_event_rate"],
            "≤3%",
        ),
        (
            "来源运行成功率至少95%",
            isinstance(metrics["source_success_rate"], float)
            and float(metrics["source_success_rate"]) >= 0.95,
            metrics["source_success_rate"],
            "≥95%",
        ),
        ("严重未处理错误为0", severe_errors == 0, severe_errors, "0"),
        ("至少一次真实恢复演练", bool(restore), bool(restore), "已成功"),
        (
            "至少1条客户报告合格事件",
            int(metrics["customer_report_eligible_event_count"]) > 0,
            metrics["customer_report_eligible_event_count"],
            "≥1",
        ),
    ]
    pilot_gates = [
        (
            "连续真实运行至少14天",
            int(metrics["consecutive_run_days"]) >= 14,
            metrics["consecutive_run_days"],
            "≥14天",
        ),
        *common_gates,
    ]
    formal_gates = [
        (
            "连续真实运行至少28天",
            int(metrics["consecutive_run_days"]) >= 28,
            metrics["consecutive_run_days"],
            "≥28天",
        ),
        *common_gates,
        (
            "已记录按时交付表现",
            isinstance(metrics["report_on_time_rate"], float)
            and float(metrics["report_on_time_rate"]) >= 0.95,
            metrics["report_on_time_rate"],
            "≥95%且有真实交付",
        ),
    ]
    metrics["pilot_gates"] = [
        {"name": name, "passed": passed, "actual": actual, "target": target}
        for name, passed, actual, target in pilot_gates
    ]
    metrics["formal_gates"] = [
        {"name": name, "passed": passed, "actual": actual, "target": target}
        for name, passed, actual, target in formal_gates
    ]
    if all(item["passed"] for item in metrics["formal_gates"]):
        metrics["readiness_status"] = "正式交付可用"
    elif all(item["passed"] for item in metrics["pilot_gates"]):
        metrics["readiness_status"] = "试点可用"
    else:
        metrics["readiness_status"] = "未达到"
    metrics["missing_gates"] = [
        item["name"] for item in metrics["pilot_gates"] if not item["passed"]
    ]
    return metrics


def _preflight_check(
    code: str,
    label: str,
    severity: str,
    failures: Sequence[str],
) -> dict[str, object]:
    return {
        "code": code,
        "label": label,
        "severity": severity,
        "passed": not failures,
        "count": len(failures),
        "details": list(failures)[:20],
    }


def preflight_customer_report(
    workspace_id: str,
    event_ids: Sequence[str],
    db_path,
    *,
    reference_date: Optional[date] = None,
) -> PreflightResult:
    initialize_database(db_path)
    ordered_ids = tuple(dict.fromkeys(str(item) for item in event_ids if str(item)))
    if not ordered_ids:
        check = _preflight_check(
            "no_events", "至少包含一条客户报告核心事件", "错误", ["没有候选事件"]
        )
        return PreflightResult(False, (check,), ())
    placeholders = ",".join("?" for _ in ordered_ids)
    with connect(db_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""SELECT e.*,d.document_version,d.content_hash AS document_content_hash,
                d.quality_status,d.report_quality_eligible,d.record_state AS document_state,
                s.customer_summary_allowed,s.short_quote_allowed,s.terms_status,
                s.commercial_reuse_status,s.permission_basis,s.permission_note
                FROM events e JOIN documents d ON d.document_id=e.document_id
                JOIN sources s ON s.source_id=d.source_id
                WHERE e.workspace_id=? AND e.event_id IN ({placeholders}) AND d.is_current=1""",
                [workspace_id, *ordered_ids],
            ).fetchall()
        ]
        evidence_rows = [
            dict(row)
            for row in connection.execute(
                f"""SELECT x.*,d.document_version AS current_document_version,
                d.content_hash AS current_content_hash FROM event_evidence x
                JOIN documents d ON d.document_id=x.document_id AND d.is_current=1
                WHERE x.event_id IN ({placeholders})""",
                list(ordered_ids),
            ).fetchall()
        ]
    by_id = {str(row["event_id"]): row for row in rows}
    evidence_by_event: dict[str, list[dict[str, object]]] = {}
    for row in evidence_rows:
        evidence_by_event.setdefault(str(row["event_id"]), []).append(row)
    missing = [event_id for event_id in ordered_ids if event_id not in by_id]
    human_failures: list[str] = []
    evidence_failures: list[str] = []
    review_failures: list[str] = []
    stale_failures: list[str] = []
    duplicate_failures: list[str] = []
    duplicate_warnings: list[str] = []
    field_failures: list[str] = []
    internal_leaks: list[str] = []
    long_text: list[str] = []
    source_risks: list[str] = []
    source_warnings: list[str] = []
    inference_mixing: list[str] = []
    quality_failures: list[str] = []
    reference_date = reference_date or date.today()
    id_pattern = re.compile(r"\b(?:EVT|DOC|INTAKE|CRAWL|OP)-[A-Z0-9-]+\b", re.I)
    path_pattern = re.compile(r"(?:[A-Za-z]:\\|/Users/|/home/)")
    inference_pattern = re.compile(r"(?:我们认为|预计将|可能导致|建议客户|有望带来)")
    for event_id in ordered_ids:
        row = by_id.get(event_id)
        if not row:
            continue
        title = str(row.get("title") or "")
        if not bool(row.get("human_verified")) or str(
            row.get("reviewer_type") or ""
        ) not in HUMAN_TYPES:
            human_failures.append(title or event_id)
        evidence = evidence_by_event.get(event_id, [])
        exact = bool(evidence) and all(
            str(item.get("verification_status") or "") == "已验证"
            and int(item.get("start_offset") or -1) >= 0
            and int(item.get("end_offset") or -1) > int(item.get("start_offset") or -1)
            and int(item.get("document_version") or 0)
            == int(item.get("current_document_version") or -1)
            and str(item.get("content_hash") or "")
            == str(item.get("current_content_hash") or "")
            for item in evidence
        )
        if not exact or not bool(row.get("evidence_verified")):
            evidence_failures.append(title or event_id)
        if str(row.get("review_state") or "") in {
            "rejected",
            "needs_second_review",
            "body_issue",
        } or bool(row.get("requires_second_review")):
            review_failures.append(title or event_id)
        status = str(row.get("status") or "")
        category = str(row.get("category") or "")
        try:
            event_day = date.fromisoformat(str(row.get("event_date") or "")[:10])
        except ValueError:
            event_day = None
        if (
            event_day
            and (reference_date - event_day).days > 30
            and status in {"新增", "持续", "更新", "待核实"}
            and category in {"航行警告", "海上气象"}
        ):
            stale_failures.append(title or event_id)
        duplicate_level = str(row.get("duplicate_level") or "")
        if duplicate_level == "高度疑似重复":
            duplicate_failures.append(title or event_id)
        elif duplicate_level == "可能重复":
            duplicate_warnings.append(title or event_id)
        required = {
            "标题": title,
            "时间": str(row.get("event_date") or ""),
            "主体": str(row.get("involved_entities") or row.get("source_name") or ""),
            "来源": str(row.get("source_name") or ""),
            "原文URL": str(row.get("source_url") or ""),
        }
        missing_fields = [label for label, value in required.items() if not value.strip()]
        if required["原文URL"] and not required["原文URL"].startswith(("http://", "https://")):
            missing_fields.append("有效原文URL")
        if missing_fields:
            field_failures.append(f"{title or event_id}：{'、'.join(missing_fields)}")
        scan_text = "\n".join(
            str(row.get(field) or "")
            for field in ("title", "summary", "impact", "recommended_action")
        )
        if path_pattern.search(scan_text) or id_pattern.search(scan_text) or scan_text.lstrip().startswith(("{", "[")):
            internal_leaks.append(title or event_id)
        if len(str(row.get("summary") or "")) > 800 or any(
            len(str(item.get("quote_text") or "")) > 350 for item in evidence
        ):
            long_text.append(title or event_id)
        if not bool(row.get("customer_summary_allowed")) or not bool(
            row.get("short_quote_allowed")
        ):
            source_risks.append(title or event_id)
        if str(row.get("terms_status") or "") in {"禁止", "不允许"} or str(
            row.get("commercial_reuse_status") or ""
        ) in {"禁止", "不允许"}:
            source_risks.append(title or event_id)
        elif str(row.get("commercial_reuse_status") or "") in {
            "",
            "未明确",
            "需取得许可",
        }:
            source_warnings.append(title or event_id)
        if inference_pattern.search(str(row.get("summary") or "")):
            inference_mixing.append(title or event_id)
        if (
            str(row.get("record_state") or "") != "active"
            or str(row.get("document_state") or "") != "active"
            or str(row.get("quality_status") or "") != "合格"
            or not bool(row.get("report_quality_eligible"))
        ):
            quality_failures.append(title or event_id)
    checks = (
        _preflight_check("missing_events", "事件版本存在", "错误", missing),
        _preflight_check("human_review", "核心事件全部由真人审核", "错误", human_failures),
        _preflight_check("evidence", "核心证据全部逐字定位且绑定当前版本", "错误", evidence_failures),
        _preflight_check("review_state", "不存在驳回、正文问题或待二审事件", "错误", review_failures),
        _preflight_check("stale_warning", "不存在未复核的过期预警", "错误", stale_failures),
        _preflight_check("duplicate", "不存在高度疑似重复事件", "错误", duplicate_failures),
        _preflight_check("possible_duplicate", "可能重复事件已提示", "警告", duplicate_warnings),
        _preflight_check("required_fields", "标题、时间、主体、来源与URL完整", "错误", field_failures),
        _preflight_check("internal_fields", "不包含内部路径、数据库ID、调试字段或原始JSON", "错误", internal_leaks),
        _preflight_check("fulltext", "不包含大段第三方全文", "错误", long_text),
        _preflight_check("source_use", "来源允许客户摘要和必要短引用", "错误", source_risks),
        _preflight_check("source_warning", "来源使用风险已提示", "警告", source_warnings),
        _preflight_check("fact_analysis", "事实摘要未混入未标注推断", "错误", inference_mixing),
        _preflight_check("quality", "正文与记录状态通过质量门禁", "错误", quality_failures),
    )
    return PreflightResult(
        not any(item["severity"] == "错误" and not item["passed"] for item in checks),
        checks,
        ordered_ids,
    )


def candidate_event_ids_for_period(
    workspace_id: str,
    db_path,
    *,
    start_date: str,
    end_date: str,
) -> tuple[str, ...]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT e.event_id FROM events e JOIN documents d ON d.document_id=e.document_id
            WHERE e.workspace_id=? AND e.event_date>=? AND e.event_date<=?
            AND e.record_state='active' AND d.record_state='active' AND d.is_current=1
            AND d.quality_status='合格' ORDER BY e.event_date,e.event_id""",
            (workspace_id, start_date, end_date),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _snapshot_payload(
    workspace_id: str,
    report_id: str,
    version: int,
    db_path,
    file_paths: Sequence[Path],
    preflight: PreflightResult,
    *,
    pilot_id: str = "",
    delivery_due_at: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    with connect(db_path) as connection:
        report = connection.execute(
            """SELECT * FROM reports WHERE workspace_id=? AND report_id=? AND version=?""",
            (workspace_id, report_id, version),
        ).fetchone()
        if not report:
            raise KeyError("报告记录不存在")
        event_ids = _json_load(report["event_ids"], [])
        if tuple(event_ids) != tuple(preflight.event_ids):
            raise ValueError("报告事件清单与预检清单不一致")
        placeholders = ",".join("?" for _ in event_ids)
        events = [
            dict(row)
            for row in connection.execute(
                f"""SELECT e.event_id,e.document_id,e.updated_at,e.review_version,e.title,
                e.event_date,e.source_name,e.source_url,e.reviewer_type,e.reviewer_name,
                e.verified_at,d.document_version,d.content_hash
                FROM events e JOIN documents d ON d.document_id=e.document_id
                WHERE e.event_id IN ({placeholders}) ORDER BY e.event_id""",
                event_ids,
            ).fetchall()
        ] if event_ids else []
        evidence = [
            dict(row)
            for row in connection.execute(
                f"""SELECT evidence_id,event_id,quote_text,document_id,document_version,
                content_hash,start_offset,end_offset,verification_status,verified_at,
                normalization_method,quote_hash FROM event_evidence
                WHERE event_id IN ({placeholders}) ORDER BY event_id,start_offset""",
                event_ids,
            ).fetchall()
        ] if event_ids else []
    file_hashes = {
        path.name: {
            "sha256": _file_sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in file_paths
        if path.is_file()
    }
    report_dict = dict(report)
    event_versions = {
        item["event_id"]: {
            "event_updated_at": item["updated_at"],
            "review_version": item["review_version"],
            "document_id": item["document_id"],
            "document_version": item["document_version"],
            "content_hash": item["content_hash"],
        }
        for item in events
    }
    source_urls = [
        {
            "event_id": item["event_id"],
            "title": item["title"],
            "source_name": item["source_name"],
            "published_at": item["event_date"],
            "source_url": item["source_url"],
        }
        for item in events
    ]
    reviewers = [
        {
            "event_id": item["event_id"],
            "reviewer_type": item["reviewer_type"],
            "reviewer_name": item["reviewer_name"],
            "reviewed_at": item["verified_at"],
        }
        for item in events
    ]
    generated_at = str(report_dict.get("generated_at") or now_iso())
    payload = {
        "report_record_id": str(report_dict["report_record_id"]),
        "report_id": report_id,
        "workspace_id": workspace_id,
        "client_id": str(report_dict.get("client_id") or ""),
        "pilot_id": pilot_id,
        "report_version": version,
        "report_mode": str(report_dict.get("report_mode") or ""),
        "start_date": str(report_dict["start_date"]),
        "end_date": str(report_dict["end_date"]),
        "data_cutoff_at": generated_at,
        "generated_at": generated_at,
        "delivery_due_at": delivery_due_at,
        "event_ids": event_ids,
        "event_versions": event_versions,
        "source_urls": source_urls,
        "evidence": evidence,
        "reviewers": reviewers,
        "file_hashes": file_hashes,
        "preflight": preflight.as_dict(),
    }
    return report_dict, payload


def create_delivery_snapshot(
    workspace_id: str,
    report_id: str,
    version: int,
    db_path,
    *,
    file_paths: Sequence[Path],
    preflight: PreflightResult,
    pilot_id: str = "",
    delivery_due_at: str = "",
) -> dict[str, object]:
    if not preflight.passed:
        raise ValueError("客户报告预检未通过，不能创建正式交付快照")
    initialize_database(db_path)
    report, payload = _snapshot_payload(
        workspace_id,
        report_id,
        version,
        db_path,
        file_paths,
        preflight,
        pilot_id=pilot_id,
        delivery_due_at=delivery_due_at,
    )
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    timestamp = now_iso()
    snapshot_id = new_id("SNP")
    with transaction(db_path) as connection:
        existing = connection.execute(
            """SELECT * FROM report_delivery_snapshots
            WHERE workspace_id=? AND report_record_id=?""",
            (workspace_id, report["report_record_id"]),
        ).fetchone()
        if existing:
            if str(existing["payload_hash"]) != payload_hash:
                raise ValueError("交付快照已经存在且内容不同；请生成新的报告版本")
            return dict(existing)
        connection.execute(
            """INSERT INTO report_delivery_snapshots(
            snapshot_id,workspace_id,report_record_id,report_id,client_id,pilot_id,
            report_version,report_mode,start_date,end_date,data_cutoff_at,generated_at,
            delivery_due_at,delivery_status,event_ids_json,event_versions_json,
            source_urls_json,evidence_json,reviewer_json,file_hashes_json,preflight_json,
            immutable_payload_json,payload_hash,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'ready_for_delivery',?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                workspace_id,
                report["report_record_id"],
                report_id,
                str(report["client_id"] or ""),
                pilot_id,
                version,
                str(report["report_mode"] or ""),
                str(report["start_date"]),
                str(report["end_date"]),
                payload["data_cutoff_at"],
                payload["generated_at"],
                delivery_due_at,
                json.dumps(payload["event_ids"], ensure_ascii=False),
                json.dumps(payload["event_versions"], ensure_ascii=False),
                json.dumps(payload["source_urls"], ensure_ascii=False),
                json.dumps(payload["evidence"], ensure_ascii=False),
                json.dumps(payload["reviewers"], ensure_ascii=False),
                json.dumps(payload["file_hashes"], ensure_ascii=False),
                json.dumps(payload["preflight"], ensure_ascii=False),
                serialized,
                payload_hash,
                timestamp,
                timestamp,
            ),
        )
    return {"snapshot_id": snapshot_id, "payload_hash": payload_hash, **payload}


def mark_delivery_snapshot_delivered(
    snapshot_id: str,
    workspace_id: str,
    db_path,
    *,
    delivered_at: Optional[str] = None,
    changed_by: str = "human_user",
    note: str = "",
) -> None:
    if changed_by not in HUMAN_TYPES:
        raise ValueError("交付状态只能由真实用户或行业复核人更新")
    timestamp = delivered_at or now_iso()
    with transaction(db_path) as connection:
        row = connection.execute(
            """SELECT delivery_status FROM report_delivery_snapshots
            WHERE snapshot_id=? AND workspace_id=?""",
            (snapshot_id, workspace_id),
        ).fetchone()
        if not row:
            raise KeyError("交付快照不存在")
        previous = str(row["delivery_status"])
        if previous == "delivered":
            return
        connection.execute(
            """UPDATE report_delivery_snapshots SET delivery_status='delivered',
            delivered_at=?,updated_at=? WHERE snapshot_id=? AND workspace_id=?""",
            (timestamp, timestamp, snapshot_id, workspace_id),
        )
        connection.execute(
            """INSERT INTO report_delivery_amendments(
            amendment_id,snapshot_id,workspace_id,action_type,previous_status,new_status,
            reason,details_json,changed_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("AMD"),
                snapshot_id,
                workspace_id,
                "mark_delivered",
                previous,
                "delivered",
                note.strip(),
                "{}",
                changed_by,
                timestamp,
            ),
        )


def list_delivery_snapshots(workspace_id: str, db_path) -> list[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM report_delivery_snapshots WHERE workspace_id=?
                ORDER BY generated_at DESC""",
                (workspace_id,),
            ).fetchall()
        ]


def _database_counts(path: Path) -> dict[str, int]:
    required = (
        "documents",
        "events",
        "event_reviews",
        "event_evidence",
        "operation_runs",
    )
    connection = sqlite3.connect(str(path))
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if table in tables
            else -1
            for table in required
        }
    finally:
        connection.close()


def run_restore_drill(
    workspace_id: str,
    db_path,
    backup_zip: Path,
    *,
    project_root: Optional[Path] = None,
    created_by: str = "human_user",
) -> dict[str, object]:
    if created_by not in HUMAN_TYPES:
        raise ValueError("恢复演练必须由真实用户发起")
    initialize_database(db_path)
    project_root = Path(project_root or ROOT).resolve()
    production = Path(db_path).resolve()
    drill_id = new_id("DRL")
    drill_dir = project_root / "output" / "restore_drills" / drill_id
    target = drill_dir / "restored" / "portscope.db"
    if target == production or production in target.parents:
        raise ValueError("恢复演练目标必须与生产数据库隔离")
    drill_dir.mkdir(parents=True, exist_ok=False)
    started = now_iso()
    backup_zip = Path(backup_zip).resolve()
    try:
        production_counts = _database_counts(production)
        restored = restore_database_snapshot(backup_zip, target, confirmed=True)
        restored_counts = _database_counts(target)
        differences = {
            key: restored_counts[key] - production_counts[key]
            for key in production_counts
        }
        status = "succeeded" if restored["quick_check"] == "ok" else "failed"
        error = ""
    except Exception as exc:
        production_counts = _database_counts(production)
        restored_counts = {}
        differences = {}
        status = "failed"
        error = str(exc)[:1000]
    finished = now_iso()
    report_path = drill_dir / "restore_drill_report.md"
    lines = [
        "# PortScope 备份恢复演练报告",
        "",
        f"- 演练ID：{drill_id}",
        f"- 状态：{status}",
        f"- 开始时间：{started}",
        f"- 完成时间：{finished}",
        f"- 备份文件：{backup_zip.name}",
        f"- 恢复目标：output/restore_drills/{drill_id}/restored/portscope.db",
        f"- SQLite完整性：{'ok' if status == 'succeeded' else '未通过'}",
        "",
        "## 数量核对",
        "",
        "| 数据 | 生产演练前 | 恢复副本 | 差异 |",
        "|---|---:|---:|---:|",
    ]
    for key in production_counts:
        lines.append(
            f"| {key} | {production_counts[key]} | "
            f"{restored_counts.get(key, '未读取')} | {differences.get(key, '—')} |"
        )
    if error:
        lines.extend(["", "## 脱敏错误", "", error])
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(report_path)
    with transaction(db_path) as connection:
        connection.execute(
            """INSERT INTO restore_drills(
            restore_drill_id,workspace_id,backup_file,restore_target,started_at,finished_at,
            status,quick_check,production_counts_json,restored_counts_json,
            count_differences_json,report_file,error_summary,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                drill_id,
                workspace_id,
                backup_zip.name,
                str(target.relative_to(project_root)),
                started,
                finished,
                status,
                "ok" if status == "succeeded" else "",
                json.dumps(production_counts, ensure_ascii=False),
                json.dumps(restored_counts, ensure_ascii=False),
                json.dumps(differences, ensure_ascii=False),
                str(report_path.relative_to(project_root)),
                error,
                created_by,
                started,
            ),
        )
    return {
        "restore_drill_id": drill_id,
        "status": status,
        "quick_check": "ok" if status == "succeeded" else "",
        "production_counts": production_counts,
        "restored_counts": restored_counts,
        "count_differences": differences,
        "report_path": report_path,
        "error_summary": error,
    }


def add_pilot_feedback(
    workspace_id: str,
    db_path,
    *,
    customer_label: str,
    report_or_alert_id: str,
    viewed: str,
    already_known: str,
    action_taken: str,
    time_saved: str,
    false_positive: str,
    omission_reported: str,
    willing_to_continue: str,
    feedback_note: str = "",
    feedback_date: Optional[str] = None,
    created_by: str = "human_user",
) -> str:
    if created_by not in HUMAN_TYPES:
        raise ValueError("试点反馈只能由用户或行业复核人根据真实反馈手工录入")
    if not customer_label.strip():
        raise ValueError("必须填写客户或匿名试点编号")
    allowed = {"是", "否", "未知", "未记录"}
    values = {
        "viewed": viewed,
        "already_known": already_known,
        "action_taken": action_taken,
        "time_saved": time_saved,
        "false_positive": false_positive,
        "omission_reported": omission_reported,
        "willing_to_continue": willing_to_continue,
    }
    invalid = [key for key, value in values.items() if value not in allowed]
    if invalid:
        raise ValueError("反馈选项不合法：" + "、".join(invalid))
    feedback_id = new_id("FDB")
    summary = (
        f"查看={viewed}；原本已知={already_known}；采取行动={action_taken}；"
        f"节省时间={time_saved}；误报={false_positive}；遗漏={omission_reported}；"
        f"愿意继续={willing_to_continue}"
    )
    with transaction(db_path) as connection:
        connection.execute(
            """INSERT INTO customer_feedback(
            feedback_id,workspace_id,feedback_date,customer_label,feedback_text,
            created_by,created_at,report_or_alert_id,viewed,already_known,action_taken,
            time_saved,false_positive,omission_reported,willing_to_continue,feedback_note
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                feedback_id,
                workspace_id,
                feedback_date or date.today().isoformat(),
                customer_label.strip(),
                summary,
                created_by,
                now_iso(),
                report_or_alert_id.strip(),
                viewed,
                already_known,
                action_taken,
                time_saved,
                false_positive,
                omission_reported,
                willing_to_continue,
                feedback_note.strip(),
            ),
        )
    return feedback_id


def list_pilot_feedback(workspace_id: str, db_path) -> list[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM customer_feedback WHERE workspace_id=?
                ORDER BY feedback_date DESC,created_at DESC""",
                (workspace_id,),
            ).fetchall()
        ]


def save_source_compliance(
    workspace_id: str,
    source_id: str,
    db_path,
    values: Mapping[str, object],
) -> None:
    for field, allowed in COMPLIANCE_VALUES.items():
        if str(values.get(field) or "未复核") not in allowed:
            raise ValueError(f"{field}取值不合法")
    fulltext = bool(values.get("fulltext_redistribution_allowed"))
    resale = bool(values.get("raw_data_resale_allowed"))
    basis = str(values.get("permission_basis") or "").strip()
    reviewed_at = str(values.get("compliance_reviewed_at") or "").strip()
    if (fulltext or resale) and (not basis or not reviewed_at):
        raise ValueError("允许全文再分发或原始数据转售前必须填写明确依据和复核日期")
    timestamp = now_iso()
    with transaction(db_path) as connection:
        cursor = connection.execute(
            """UPDATE sources SET public_access_status=?,access_frequency_compliant=?,
            personal_information_status=?,important_data_status=?,
            internal_collection_allowed=?,internal_analysis_allowed=?,
            customer_summary_allowed=?,short_quote_allowed=?,
            fulltext_redistribution_allowed=?,raw_data_resale_allowed=?,
            permission_basis=?,permission_note=?,compliance_reviewed_at=?,
            permission_reviewed_at=?,updated_at=?
            WHERE workspace_id=? AND source_id=?""",
            (
                str(values.get("public_access_status") or "未复核"),
                str(values.get("access_frequency_compliant") or "未复核"),
                str(values.get("personal_information_status") or "未复核"),
                str(values.get("important_data_status") or "未复核"),
                int(bool(values.get("internal_collection_allowed", True))),
                int(bool(values.get("internal_analysis_allowed", True))),
                int(bool(values.get("customer_summary_allowed", False))),
                int(bool(values.get("short_quote_allowed", False))),
                int(fulltext),
                int(resale),
                basis,
                str(values.get("permission_note") or "").strip(),
                reviewed_at,
                reviewed_at,
                timestamp,
                workspace_id,
                source_id,
            ),
        )
        if not cursor.rowcount:
            raise KeyError("来源不存在")
