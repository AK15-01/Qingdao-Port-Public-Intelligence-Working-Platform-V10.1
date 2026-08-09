from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
import json
from typing import Mapping, Optional, Union

import pandas as pd

from commercial_report import (
    ReportArtifacts,
    ReportOptions,
    _build_xlsx,
    _core_events,
    filter_report_events,
    generate_commercial_report,
)
from data_store import STANDARD_FIELDS, normalize_events
from data_validator import validate_events
from platform_db import connect, initialize_database
from workspace_store import WorkspacePaths


class ReportEligibilityError(ValueError):
    def __init__(self, message: str, reasons: Optional[Mapping[str, int]] = None):
        super().__init__(message)
        self.reasons = dict(reasons or {})


VALID_REPORT_MODES = {"标准报告", "内部研究版", "客户交付版"}


def _validate_report_mode(report_mode: str) -> str:
    value = str(report_mode or "").strip()
    if value not in VALID_REPORT_MODES:
        raise ValueError(
            "不支持的报告模式。请选择“内部研究版”或“客户交付版”；"
            "旧版兼容流程仅允许“标准报告”。"
        )
    return value


def eligible_events_dataframe(
    workspace_id: str,
    db_path,
    *,
    human_verified_only: bool = True,
    report_mode: str = "标准报告",
) -> pd.DataFrame:
    initialize_database(db_path)
    report_mode = _validate_report_mode(report_mode)
    where = [
        "e.workspace_id=?", "d.is_current=1", "d.extraction_status IN ('成功','提取成功')",
        "d.report_quality_eligible=1", "e.duplicate_level!='高度疑似重复'",
        "d.record_state='active'", "e.record_state='active'",
    ]
    params: list[object] = [workspace_id]
    if report_mode == "客户交付版":
        where.extend([
            "e.human_verified=1", "e.reviewer_type IN ('human_user','industry_reviewer')",
            "e.evidence_verified=1", "e.report_eligible=1",
            "e.eligible_for_customer_report=1",
            "s.customer_summary_allowed=1", "s.short_quote_allowed=1",
        ])
        where.extend([
            "e.source_url LIKE 'http%'", "e.event_date!=''",
            "s.terms_status NOT IN ('禁止','不允许')",
            "s.commercial_reuse_status NOT IN ('禁止','不允许')",
        ])
    elif report_mode == "内部研究版":
        where.extend([
            "e.source_url LIKE 'http%'", "e.event_date!=''",
            "s.internal_analysis_allowed=1",
        ])
    elif human_verified_only:
        where.extend([
            "e.human_verified=1", "e.reviewer_type IN ('human_user','industry_reviewer')",
            "e.evidence_verified=1", "e.report_eligible=1",
            "s.customer_summary_allowed=1", "s.short_quote_allowed=1",
            "e.source_url LIKE 'http%'", "e.event_date!=''",
            "s.terms_status NOT IN ('禁止','不允许')",
            "s.commercial_reuse_status NOT IN ('禁止','不允许')",
        ])
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT e.*,d.source_id,d.fetched_at,d.quality_status,d.extraction_quality,
            d.document_format,d.source_file_url,
            d.quality_metrics_json,d.report_quality_eligible,s.commercial_reuse_status,s.report_use_allowed,
            s.terms_status,s.license_note,s.last_license_checked_at,
            s.internal_analysis_allowed,s.customer_summary_allowed,s.short_quote_allowed,
            s.fulltext_redistribution_allowed,s.raw_data_resale_allowed,
            s.permission_basis,s.permission_note,
            (SELECT GROUP_CONCAT(x.quote_text,'｜') FROM event_evidence x
             WHERE x.event_id=e.event_id AND x.verification_status='已验证') AS evidence_quotes,
            (SELECT GROUP_CONCAT(x.failure_reason,'；') FROM event_evidence x
             WHERE x.event_id=e.event_id AND x.verification_status!='已验证') AS evidence_failure_reasons
            FROM events e JOIN documents d ON d.document_id=e.document_id JOIN sources s ON s.source_id=d.source_id
            WHERE {' AND '.join(where)} ORDER BY e.event_date DESC,e.created_at DESC""",
            params,
        ).fetchall()
    if not rows:
        return normalize_events(pd.DataFrame(), assign_ids=False)
    data = pd.DataFrame([dict(row) for row in rows])
    extra_columns = [
        "extraction_confidence", "human_verified", "commercial_reuse_status",
        "quality_status", "extraction_quality", "report_quality_eligible",
        "business_value", "value_reason", "evidence_verified", "document_format",
        "source_file_url", "evidence_quotes", "evidence_failure_reasons",
        "internal_analysis_allowed", "customer_summary_allowed", "short_quote_allowed",
        "fulltext_redistribution_allowed", "raw_data_resale_allowed",
        "permission_basis", "permission_note",
    ]
    extras = data.set_index("event_id")[extra_columns].to_dict("index")
    rename = {"verified_at": "human_confirmed_at", "updated_at": "last_modified_at", "report_eligible": "report_included", "ai_generated": "ai_assisted"}
    data = data.rename(columns=rename)
    data["report_included"] = data["report_included"].map(lambda value: "是" if bool(value) else "否")
    if report_mode == "内部研究版":
        data["report_included"] = "是"
    data["ai_assisted"] = data["ai_assisted"].map(lambda value: "是" if bool(value) else "否")
    normalized = normalize_events(data, assign_ids=False)
    for column in extra_columns:
        normalized[column] = normalized["event_id"].map(lambda value: extras.get(value, {}).get(column, ""))
    return normalized


def customer_eligibility_reasons(workspace_id: str, db_path, start: date, end: date) -> dict[str, int]:
    conditions = {
        "未人工确认": "e.human_verified=0",
        "核验来源不是独立真人": "e.reviewer_type NOT IN ('human_user','industry_reviewer')",
        "证据引用不可逐字定位": "e.evidence_verified=0",
        "未标记可进入报告": "e.report_eligible=0 OR e.eligible_for_customer_report=0",
        "来源不允许客户摘要或短引用": "s.customer_summary_allowed=0 OR s.short_quote_allowed=0",
        "日期缺失": "e.event_date=''",
        "原文链接无效": "e.source_url NOT LIKE 'http%'",
        "高度疑似重复": "e.duplicate_level='高度疑似重复'",
        "AI低置信度未复核": "e.extraction_confidence<0.6 AND e.human_verified=0",
        "中高风险未确认": "e.risk_level IN ('中','高') AND e.human_verified=0",
        "来源条款明确禁止": "s.terms_status IN ('禁止','不允许') OR s.commercial_reuse_status IN ('禁止','不允许')",
    }
    result: dict[str, int] = {}
    with connect(db_path) as connection:
        for label, clause in conditions.items():
            result[label] = int(connection.execute(
                f"""SELECT COUNT(*) FROM events e JOIN documents d ON d.document_id=e.document_id
                JOIN sources s ON s.source_id=d.source_id WHERE e.workspace_id=? AND
                d.is_current=1 AND d.extraction_status IN ('成功','提取成功')
                AND d.report_quality_eligible=1 AND d.record_state='active'
                AND e.record_state='active' AND e.duplicate_level!='高度疑似重复'
                AND e.event_date>=? AND e.event_date<=? AND ({clause})""",
                (workspace_id, start.isoformat(), end.isoformat()),
            ).fetchone()[0])
    return {key: value for key, value in result.items() if value}


def _generate_platform_report_impl(
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    *,
    client: Optional[Mapping[str, object]],
    start_date: Union[str, date],
    end_date: Union[str, date],
    report_title: str,
    analyst: str,
    executive_summary: str = "",
    options: ReportOptions = ReportOptions(),
    db_path=None,
    human_verified_only: bool = True,
    report_mode: str = "标准报告",
) -> ReportArtifacts:
    report_mode = _validate_report_mode(report_mode)
    start = pd.to_datetime(start_date).date()
    end = pd.to_datetime(end_date).date()
    events = eligible_events_dataframe(
        str(workspace["workspace_id"]), db_path, human_verified_only=human_verified_only,
        report_mode=report_mode,
    )
    options = replace(options, report_mode=report_mode)
    selected_before = filter_report_events(events, start, end, client, options)
    customer_preflight = None
    if report_mode == "客户交付版" and not selected_before.empty:
        from commercial_readiness import preflight_customer_report

        selected_ids = [
            str(value)
            for value in selected_before.get("event_id", pd.Series(dtype=str))
            if str(value)
        ]
        customer_preflight = preflight_customer_report(
            str(workspace["workspace_id"]),
            selected_ids,
            db_path,
            reference_date=end,
        )
        if not customer_preflight.passed:
            reasons = {
                str(item["label"]): int(item["count"])
                for item in customer_preflight.blockers
            }
            raise ReportEligibilityError(
                "客户报告预检未通过，不能标记为正式交付版："
                + "；".join(
                    f"{item['label']}{item['count']}条"
                    for item in customer_preflight.blockers
                )
                + "。可以改为生成内部研究草稿。",
                reasons,
            )
    if (
        options.require_business_value
        and not selected_before.empty
        and "business_value" in selected_before.columns
        and not selected_before["business_value"].isin(["高", "中"]).any()
    ):
        raise ReportEligibilityError(
            "本期没有足够高价值内容，已阻止生成正式样刊。"
            "可以继续生成数据采集质检报告，但不会使用低价值宣传稿填充核心栏目。",
            {"中高业务价值事件": 0},
        )
    if options.require_business_value and not selected_before.empty and _core_events(selected_before).empty:
        raise ReportEligibilityError(
            "本期中高价值资料仍未通过低置信度或证据可追溯门禁，已阻止生成正式样刊。",
            {"可进入核心栏目的中高价值事件": 0},
        )
    if report_mode in {"内部研究版", "客户交付版"} and selected_before.empty and not options.allow_empty_template:
        reasons = (
            customer_eligibility_reasons(str(workspace["workspace_id"]), db_path, start, end)
            if report_mode == "客户交付版" else {}
        )
        reason_text = "；".join(f"{key}{value}条" for key, value in reasons.items()) or "所选周期没有通过正文质量与证据门禁的事件"
        raise ReportEligibilityError(
            "当前没有足够证据生成报告，已阻止生成完整DOCX/HTML/Excel。"
            + reason_text
            + ("。可以先生成内部研究版" if report_mode == "客户交付版" else "")
            + "。如只需要版式，请明确选择生成空白模板。",
            reasons,
        )
    if report_mode == "客户交付版" and selected_before.empty:
        reasons = customer_eligibility_reasons(str(workspace["workspace_id"]), db_path, start, end)
        reason_text = "；".join(f"{key}{value}条" for key, value in reasons.items()) or "所选周期没有事件"
        raise ReportEligibilityError(
            "客户交付版没有足够的合格数据，已阻止生成空洞报告。缺少条件：" + reason_text + "。可以先生成内部研究版。",
            reasons,
        )
    artifacts = generate_commercial_report(
        events,
        workspace,
        paths,
        client=client,
        start_date=start_date,
        end_date=end_date,
        report_title=report_title,
        analyst=analyst,
        executive_summary=executive_summary,
        options=options,
        db_path=db_path,
    )
    selected = filter_report_events(events, start, end, client, options)
    quality = validate_events(selected[STANDARD_FIELDS] if not selected.empty else selected)
    with connect(db_path) as connection:
        runs = pd.DataFrame([dict(row) for row in connection.execute(
            "SELECT * FROM crawl_runs WHERE workspace_id=? AND started_at>=? AND started_at<=? ORDER BY started_at DESC",
            (str(workspace["workspace_id"]), start.isoformat(), end.isoformat() + "T23:59:59"),
        ).fetchall()])
    _build_xlsx(
        artifacts.xlsx_path,
        selected,
        quality,
        extended=report_mode != "客户交付版",
        crawl_runs=runs,
        report_mode=report_mode,
    )
    document_ids: list[str] = []
    if artifacts.event_ids:
        placeholders = ",".join("?" for _ in artifacts.event_ids)
        with connect(db_path) as connection:
            document_ids = [str(row[0]) for row in connection.execute(
                f"SELECT document_id FROM events WHERE event_id IN ({placeholders}) ORDER BY event_date,event_id",
                list(artifacts.event_ids),
            ).fetchall() if row[0]]
            actual_models: list[str] = []
            if document_ids:
                document_placeholders = ",".join("?" for _ in document_ids)
                actual_models = [
                    str(row[0])
                    for row in connection.execute(
                        f"""SELECT DISTINCT model_name FROM ai_call_logs
                        WHERE workspace_id=? AND success=1 AND document_id IN ({document_placeholders})
                        ORDER BY model_name""",
                        [str(workspace["workspace_id"]), *document_ids],
                    ).fetchall()
                    if row[0]
                ]
            analysis_model = "deterministic-rules"
            if actual_models:
                analysis_model += "; extraction:" + ",".join(actual_models)
            connection.execute(
                """UPDATE reports SET document_ids=?,report_version=?,docx_path=?,html_path=?,excel_path=?,client_name=?,
                report_mode=?,analysis_model=?
                WHERE workspace_id=? AND report_id=? AND version=?""",
                (json.dumps(document_ids, ensure_ascii=False), artifacts.version, str(artifacts.docx_path),
                 str(artifacts.html_path), str(artifacts.xlsx_path), str((client or {}).get("client_name") or "通用报告"),
                 report_mode, analysis_model,
                 str(workspace["workspace_id"]), artifacts.report_id, artifacts.version),
            )
            connection.commit()
    if report_mode == "客户交付版":
        if customer_preflight is None:
            raise ReportEligibilityError("客户报告缺少可验证的预检记录，不能创建交付快照。")
        from commercial_readiness import create_delivery_snapshot

        create_delivery_snapshot(
            str(workspace["workspace_id"]),
            artifacts.report_id,
            artifacts.version,
            db_path,
            file_paths=[
                artifacts.docx_path,
                artifacts.html_path,
                artifacts.xlsx_path,
            ],
            preflight=customer_preflight,
            pilot_id=str((client or {}).get("client_id") or ""),
        )
    return artifacts


def generate_platform_report(
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    *,
    client: Optional[Mapping[str, object]],
    start_date: Union[str, date],
    end_date: Union[str, date],
    report_title: str,
    analyst: str,
    executive_summary: str = "",
    options: ReportOptions = ReportOptions(),
    db_path=None,
    human_verified_only: bool = True,
    report_mode: str = "标准报告",
    ui_session_id: str = "",
) -> ReportArtifacts:
    from operation_store import finish_operation, start_operation

    report_mode = _validate_report_mode(report_mode)
    workspace_id = str(workspace["workspace_id"])
    operation_run_id = start_operation(
        workspace_id,
        "report_generation",
        db_path,
        ui_session_id=ui_session_id,
        input_summary=f"{report_mode}｜{report_title}｜{start_date}至{end_date}",
        metadata={
            "report_mode": report_mode,
            "report_title": report_title,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "client_name": str((client or {}).get("client_name") or "通用报告"),
        },
    )
    try:
        artifacts = _generate_platform_report_impl(
            workspace,
            paths,
            client=client,
            start_date=start_date,
            end_date=end_date,
            report_title=report_title,
            analyst=analyst,
            executive_summary=executive_summary,
            options=options,
            db_path=db_path,
            human_verified_only=human_verified_only,
            report_mode=report_mode,
        )
    except Exception as exc:
        finish_operation(
            operation_run_id,
            db_path,
            status="failed",
            result_summary="报告未生成；门禁或生成过程返回问题",
            error_summary=str(exc),
            counts={"failed_count": 1},
            metadata={"exception_type": type(exc).__name__},
        )
        raise
    finish_operation(
        operation_run_id,
        db_path,
        status="succeeded",
        result_summary=(
            f"已生成 {report_mode}：{artifacts.report_id} v{artifacts.version}；"
            f"纳入事件 {len(artifacts.event_ids)} 条"
        ),
        metadata={
            "report_id": artifacts.report_id,
            "version": artifacts.version,
            "event_ids": list(artifacts.event_ids),
            "docx_file": artifacts.docx_path.name,
            "html_file": artifacts.html_path.name,
            "xlsx_file": artifacts.xlsx_path.name,
        },
    )
    return artifacts
