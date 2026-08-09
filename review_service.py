from __future__ import annotations

import json
from typing import Mapping, Optional

import pandas as pd

from data_validator import validate_events
from platform_db import connect, new_id, now_iso, transaction
from review_provenance import HUMAN_REVIEWER_TYPES


REVIEW_DECISIONS = {
    "accept",
    "accept_modified",
    "reject",
    "body_issue",
    "needs_second_review",
}
EDITABLE_FIELDS = {
    "event_date",
    "title",
    "summary",
    "impact",
    "affected_area",
    "affected_period",
    "involved_entities",
    "category",
    "status",
    "manual_risk_level",
    "manual_keywords",
    "related_event_id",
    "analyst_note",
}


def _event_bundle(event_id: str, workspace_id: str, db_path) -> dict[str, object] | None:
    with connect(db_path) as connection:
        row = connection.execute(
            """SELECT e.*,d.title AS document_title,d.cleaned_text,d.quality_status,
            d.report_quality_eligible,d.record_state AS document_state,d.document_version,
            d.content_hash,d.canonical_url,d.publisher,d.published_at,d.fetched_at,
            s.internal_analysis_allowed,s.customer_summary_allowed,s.short_quote_allowed,s.terms_status,
            s.commercial_reuse_status,s.permission_basis,s.permission_note
            FROM events e JOIN documents d ON d.document_id=e.document_id
            JOIN sources s ON s.source_id=d.source_id
            WHERE e.event_id=? AND e.workspace_id=? AND d.is_current=1""",
            (event_id, workspace_id),
        ).fetchone()
    return dict(row) if row else None


def get_review_record(event_id: str, workspace_id: str, db_path) -> dict[str, object] | None:
    item = _event_bundle(event_id, workspace_id, db_path)
    if not item:
        return None
    with connect(db_path) as connection:
        evidence = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM event_evidence WHERE event_id=?
                ORDER BY CASE WHEN verification_status='已验证' THEN 0 ELSE 1 END,start_offset""",
                (event_id,),
            ).fetchall()
        ]
        reviews = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM event_reviews WHERE event_id=?
                ORDER BY reviewed_at DESC,created_at DESC""",
                (event_id,),
            ).fetchall()
        ]
    item["evidence"] = evidence
    item["reviews"] = reviews
    return item


def customer_summary_gate(item: Mapping[str, object]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if str(item.get("record_state") or "active") != "active":
        reasons.append("事件不在active状态")
    if str(item.get("document_state") or "active") != "active":
        reasons.append("文档不在active状态")
    if str(item.get("quality_status") or "") != "合格" or not bool(
        item.get("report_quality_eligible")
    ):
        reasons.append("正文质量门禁未通过")
    if not bool(item.get("evidence_verified")):
        reasons.append("核心证据未逐字定位")
    if not bool(item.get("customer_summary_allowed")):
        reasons.append("来源不允许生成客户摘要")
    if not bool(item.get("short_quote_allowed")):
        reasons.append("来源不允许必要短引用")
    if str(item.get("terms_status") or "") in {"禁止", "不允许"}:
        reasons.append("来源条款明确禁止")
    if str(item.get("duplicate_level") or "") == "高度疑似重复":
        reasons.append("高度疑似重复")
    if str(item.get("event_date") or "") == "":
        reasons.append("日期缺失")
    if not str(item.get("source_url") or "").startswith(("http://", "https://")):
        reasons.append("原文链接无效")
    return not reasons, reasons


def internal_use_gate(item: Mapping[str, object]) -> tuple[bool, list[str]]:
    """Return the formal-workspace gate, independent from customer export rights."""

    reasons: list[str] = []
    if str(item.get("record_state") or "active") != "active":
        reasons.append("事件不在active状态")
    if str(item.get("document_state") or "active") != "active":
        reasons.append("文档不在active状态")
    if str(item.get("quality_status") or "") != "合格":
        reasons.append("正文质量门禁未通过")
    if not bool(item.get("evidence_verified")):
        reasons.append("核心证据未逐字定位")
    if not bool(item.get("internal_analysis_allowed", True)):
        reasons.append("来源配置不允许内部分析")
    if str(item.get("duplicate_level") or "") == "高度疑似重复":
        reasons.append("高度疑似重复")
    if not str(item.get("source_url") or "").startswith(("http://", "https://")):
        reasons.append("原文链接无效")
    return not reasons, reasons


def save_event_review(
    event_id: str,
    workspace_id: str,
    db_path,
    *,
    decision: str,
    reviewer_type: str,
    reviewer_name: str,
    reviewer_note: str = "",
    edits: Optional[Mapping[str, object]] = None,
    checklist: Optional[Mapping[str, object]] = None,
    ui_session_id: str = "",
) -> dict[str, object]:
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"不支持的审核决定：{decision}")
    if reviewer_type not in HUMAN_REVIEWER_TYPES:
        raise ValueError("只有项目使用者或行业复核人可以保存真人审核")
    if not str(reviewer_name or "").strip():
        raise ValueError("必须填写核验人姓名或内部代号")
    before = _event_bundle(event_id, workspace_id, db_path)
    if not before:
        raise KeyError("事件不存在")
    updates = {
        key: ("" if value is None else str(value).strip())
        for key, value in dict(edits or {}).items()
        if key in EDITABLE_FIELDS
    }
    changed = [
        key for key, value in updates.items()
        if str(before.get(key) or "") != str(value or "")
    ]
    if decision == "accept_modified" and not changed:
        decision = "accept"

    after = dict(before)
    after.update(updates)
    if decision in {"accept", "accept_modified"}:
        report = validate_events(pd.DataFrame([after]))
        if report.error_count:
            raise ValueError(f"审核后的事件仍有 {report.error_count} 项必须修复的数据错误")
        if str(after.get("duplicate_level") or "") == "高度疑似重复":
            raise ValueError("高度疑似重复事件不能接受")
        if str(after.get("status") or "") == "解除" and not str(
            after.get("related_event_id") or ""
        ):
            raise ValueError("解除事件必须先关联原事件")
        if not bool((checklist or {}).get("source_opened")):
            raise ValueError("必须确认已打开并核对原始公开来源")
        if not bool((checklist or {}).get("facts_checked")):
            raise ValueError("必须确认事实字段已经与原文核对")

    timestamp = now_iso()
    review_state = {
        "accept": "accepted",
        "accept_modified": "modified_accepted",
        "reject": "rejected",
        "body_issue": "body_issue",
        "needs_second_review": "needs_second_review",
    }[decision]
    human_verified = int(decision in {"accept", "accept_modified"})
    requires_second_review = int(decision == "needs_second_review")
    record_state = "active"
    if decision == "reject":
        record_state = "rejected"
    elif decision == "body_issue":
        record_state = "quarantined"
    provisional = {**after, "record_state": record_state}
    summary_allowed, gate_reasons = customer_summary_gate(provisional)
    internal_allowed, internal_gate_reasons = internal_use_gate(provisional)
    active_for_internal_use = int(human_verified and internal_allowed)
    eligible_for_customer_report = int(human_verified and summary_allowed)
    report_eligible = eligible_for_customer_report

    with transaction(db_path) as connection:
        if updates:
            assignments = ",".join(f"{field}=?" for field in updates)
            connection.execute(
                f"UPDATE events SET {assignments},updated_at=? WHERE event_id=? AND workspace_id=?",
                [*updates.values(), timestamp, event_id, workspace_id],
            )
        connection.execute(
            """UPDATE events SET human_verified=?,verified_at=?,report_eligible=?,
            active_for_internal_use=?,eligible_for_customer_report=?,
            reviewer_type=?,reviewer_name=?,review_method='界面逐项人工审核',
            review_version='0.5.0-beta',reviewer_note=?,review_state=?,
            requires_second_review=?,record_state=?,
            quarantine_reason=CASE WHEN ? IN ('reject','body_issue') THEN ? ELSE quarantine_reason END,
            quarantined_at=CASE WHEN ?='body_issue' AND quarantined_at='' THEN ? ELSE quarantined_at END,
            updated_at=? WHERE event_id=? AND workspace_id=?""",
            (
                human_verified,
                timestamp if human_verified else "",
                report_eligible,
                active_for_internal_use,
                eligible_for_customer_report,
                reviewer_type,
                reviewer_name.strip(),
                reviewer_note.strip(),
                review_state,
                requires_second_review,
                record_state,
                decision,
                "人工驳回" if decision == "reject" else "人工标记正文有问题",
                decision,
                timestamp,
                timestamp,
                event_id,
                workspace_id,
            ),
        )
        if decision == "body_issue":
            document_id = str(before["document_id"])
            connection.execute(
                """UPDATE documents SET record_state='quarantined',
                quarantine_reason='人工审核标记正文有问题',
                quarantined_at=CASE WHEN quarantined_at='' THEN ? ELSE quarantined_at END,
                report_quality_eligible=0,review_status='已隔离',updated_at=?
                WHERE document_id=?""",
                (timestamp, timestamp, document_id),
            )
            connection.execute("DELETE FROM document_chunks_fts WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
        after_saved = connection.execute(
            "SELECT * FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        after_payload = dict(after_saved) if after_saved else after
        connection.execute(
            """INSERT INTO event_reviews(
            review_id,workspace_id,event_id,reviewer_type,reviewer_name,reviewed_at,
            review_method,review_version,reviewer_note,checklist_json,decision,created_at,
            before_json,after_json,changed_fields_json,requires_second_review,review_stage
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("REV"),
                workspace_id,
                event_id,
                reviewer_type,
                reviewer_name.strip(),
                timestamp,
                "界面逐项人工审核",
                "0.5.0-beta",
                reviewer_note.strip(),
                json.dumps(dict(checklist or {}), ensure_ascii=False, default=str),
                decision,
                timestamp,
                json.dumps(
                    {key: before.get(key) for key in EDITABLE_FIELDS | {
                        "human_verified", "report_eligible", "record_state", "review_state"
                    }},
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps(
                    {key: after_payload.get(key) for key in EDITABLE_FIELDS | {
                        "human_verified", "report_eligible", "record_state", "review_state"
                    }},
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps(changed, ensure_ascii=False),
                requires_second_review,
                "first_review",
            ),
        )
    from operation_store import finish_operation, start_operation

    operation_run_id = start_operation(
        workspace_id,
        "manual_review",
        db_path,
        ui_session_id=ui_session_id,
        input_summary=f"审核事件：{event_id}",
        metadata={"event_id": event_id, "decision": decision},
    )
    finish_operation(
        operation_run_id,
        db_path,
        status="succeeded",
        result_summary=f"事件审核完成：{decision}",
        counts={"events_updated": 1},
        metadata={
            "event_id": event_id,
            "decision": decision,
            "changed_fields": changed,
            "requires_second_review": bool(requires_second_review),
        },
    )
    return {
        "event_id": event_id,
        "decision": decision,
        "review_state": review_state,
        "human_verified": bool(human_verified),
        "report_eligible": bool(report_eligible),
        "active_for_internal_use": bool(active_for_internal_use),
        "eligible_for_customer_report": bool(eligible_for_customer_report),
        "changed_fields": changed,
        "internal_gate_reasons": internal_gate_reasons,
        "customer_gate_reasons": gate_reasons,
        "operation_run_id": operation_run_id,
    }


def restore_quarantined_record(
    *,
    document_id: str,
    workspace_id: str,
    db_path,
    reviewer_type: str,
    reviewer_name: str,
    confirmed: bool = False,
) -> dict[str, object]:
    if not confirmed:
        raise PermissionError("恢复隔离数据前必须确认")
    if reviewer_type not in HUMAN_REVIEWER_TYPES or not reviewer_name.strip():
        raise ValueError("恢复操作必须记录真人审核人")
    timestamp = now_iso()
    with transaction(db_path) as connection:
        row = connection.execute(
            """SELECT document_id,quality_status,extraction_status FROM documents
            WHERE document_id=? AND workspace_id=?""",
            (document_id, workspace_id),
        ).fetchone()
        if not row:
            raise KeyError("文档不存在")
        if str(row["quality_status"] or "") != "合格" or str(
            row["extraction_status"] or ""
        ) not in {"成功", "提取成功"}:
            raise ValueError("文档尚未重新通过正文质量检查，不能恢复为active")
        connection.execute(
            """UPDATE documents SET record_state='active',quarantine_reason='',
            restored_at=?,review_status='待审核',updated_at=? WHERE document_id=?""",
            (timestamp, timestamp, document_id),
        )
        connection.execute(
            """UPDATE events SET record_state='active',quarantine_reason='',
            restored_at=?,review_state='pending',human_verified=0,report_eligible=0,
            active_for_internal_use=0,eligible_for_customer_report=0,
            reviewer_type='unknown',reviewer_name='',verified_at='',updated_at=?
            WHERE document_id=?""",
            (timestamp, timestamp, document_id),
        )
    return {"document_id": document_id, "record_state": "active", "restored_at": timestamp}
