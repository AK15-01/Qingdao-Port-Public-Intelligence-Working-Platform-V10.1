from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from operation_store import finish_operation, start_operation
from platform_db import connect, initialize_database, new_id, now_iso, transaction
from scripts.reevaluate_warning_quality import evaluate


DEFAULT_SOURCE_ID = "SRC-2FF28D3D7FC5"


def list_warning_transition_candidates(
    db_path,
    workspace_id: str,
    *,
    source_id: str = DEFAULT_SOURCE_ID,
) -> list[dict[str, object]]:
    """Return current live dry-run results; this function never mutates data."""

    initialize_database(db_path)
    reevaluation = evaluate(Path(db_path), source_id)
    approved = {
        str(item["document_id"]): dict(item)
        for item in reevaluation.get("documents", [])
        if bool(item.get("processing_allowed")) and str(item.get("new_status")) == "合格"
    }
    if not approved:
        return []
    placeholders = ",".join("?" for _ in approved)
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT document_id,title,publisher,published_at,canonical_url,
            record_state,quality_status,quarantine_reason,ai_status,review_status
            FROM documents WHERE workspace_id=? AND source_id=? AND is_current=1
            AND document_id IN ({placeholders}) ORDER BY published_at DESC""",
            (workspace_id, source_id, *approved),
        ).fetchall()
    result = []
    for row in rows:
        item = {**dict(row), **approved[str(row["document_id"])]}
        item["transition_allowed"] = (
            str(row["record_state"]) == "quarantined"
            and str(row["quality_status"]) != "合格"
        )
        result.append(item)
    return result


def confirm_warning_transition(
    db_path,
    workspace_id: str,
    document_ids: Iterable[str],
    *,
    source_id: str = DEFAULT_SOURCE_ID,
    confirmed: bool = False,
    confirmed_by: str = "local_user",
) -> dict[str, object]:
    """Move explicitly selected PDFs to the internal pending-review pipeline.

    This is a quality-state decision, not a human event review. It never sets
    human verification, evidence verification, internal event eligibility or
    customer-report eligibility.
    """

    if not confirmed:
        raise PermissionError("转入待人工审核前必须由用户明确确认")
    selected = list(dict.fromkeys(str(value) for value in document_ids if str(value)))
    if not selected:
        raise ValueError("至少选择一篇文档")
    live = {
        str(item["document_id"]): item
        for item in list_warning_transition_candidates(
            db_path,
            workspace_id,
            source_id=source_id,
        )
    }
    blocked = [value for value in selected if not live.get(value, {}).get("transition_allowed")]
    if blocked:
        raise ValueError("所选文档已变化或不再满足复评条件：" + "、".join(blocked))
    operation_id = start_operation(
        workspace_id,
        "quality_reevaluation",
        db_path,
        source_id=source_id,
        created_by="local_user",
        input_summary=f"确认{len(selected)}篇官方预警PDF转入内部待审核流程",
        metadata={"document_ids": selected, "automatic_human_review": False},
    )
    timestamp = now_iso()
    try:
        with transaction(db_path) as connection:
            for document_id in selected:
                row = connection.execute(
                    """SELECT source_id,record_state,quality_status,quarantine_reason,
                    quality_metrics_json FROM documents
                    WHERE workspace_id=? AND document_id=? AND is_current=1""",
                    (workspace_id, document_id),
                ).fetchone()
                if not row or str(row["record_state"]) != "quarantined":
                    raise ValueError(f"文档状态已变化：{document_id}")
                metrics = {}
                try:
                    metrics = json.loads(str(row["quality_metrics_json"] or "{}"))
                except json.JSONDecodeError:
                    metrics = {}
                if not isinstance(metrics, dict):
                    metrics = {}
                metrics["controlled_reevaluation"] = {
                    "confirmed_at": timestamp,
                    "confirmed_by": confirmed_by,
                    "result": live[document_id],
                    "human_review_created": False,
                    "customer_eligibility_created": False,
                }
                connection.execute(
                    """UPDATE documents SET quality_status='合格',
                    extraction_status='成功',quality_metrics_json=?,
                    report_quality_eligible=1,record_state='active',
                    review_status='待人工审核',ai_status='待AI处理',
                    restored_at=?,updated_at=?
                    WHERE document_id=? AND workspace_id=?""",
                    (
                        json.dumps(metrics, ensure_ascii=False, default=str),
                        timestamp,
                        timestamp,
                        document_id,
                        workspace_id,
                    ),
                )
                # Existing quarantined events are not restored. Any old evidence
                # is explicitly invalidated and must be rebuilt against the
                # current document before a later human review can pass.
                event_ids = [
                    str(item[0])
                    for item in connection.execute(
                        "SELECT event_id FROM events WHERE document_id=?",
                        (document_id,),
                    ).fetchall()
                ]
                if event_ids:
                    placeholders = ",".join("?" for _ in event_ids)
                    connection.execute(
                        f"""UPDATE event_evidence SET verification_status='待验证',
                        verified_at='',failure_reason='质量复评后必须按当前文档重新绑定'
                        WHERE event_id IN ({placeholders})""",
                        event_ids,
                    )
                    connection.execute(
                        f"""UPDATE events SET evidence_verified=0,human_verified=0,
                        report_eligible=0,active_for_internal_use=0,
                        eligible_for_customer_report=0,review_state='pending',
                        reviewer_type='unknown',reviewer_name='',verified_at='',
                        updated_at=? WHERE event_id IN ({placeholders})""",
                        (timestamp, *event_ids),
                    )
                connection.execute(
                    """INSERT INTO document_quality_reevaluations(
                    reevaluation_id,document_id,workspace_id,source_id,
                    previous_record_state,previous_quality_status,
                    previous_quarantine_reason,reevaluated_quality_status,
                    reevaluation_result_json,decision,confirmed_by,confirmed_at,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("QRE"),
                        document_id,
                        workspace_id,
                        source_id,
                        row["record_state"],
                        row["quality_status"],
                        row["quarantine_reason"],
                        "合格",
                        json.dumps(live[document_id], ensure_ascii=False, default=str),
                        "转入内部待审核",
                        confirmed_by,
                        timestamp,
                        timestamp,
                    ),
                )
        finish_operation(
            operation_id,
            db_path,
            status="succeeded",
            result_summary=f"{len(selected)}篇文档已转入内部待审核；未创建真人审核或客户报告资格",
            counts={"documents_updated": len(selected)},
            metadata={
                "document_ids": selected,
                "human_verified_count": 0,
                "customer_eligible_count": 0,
            },
        )
    except Exception as exc:
        finish_operation(
            operation_id,
            db_path,
            status="failed",
            result_summary="官方预警PDF状态转换失败，事务已回滚",
            error_summary=f"{type(exc).__name__}: {str(exc)[:1000]}",
            counts={"failed_count": len(selected)},
        )
        raise
    return {
        "operation_run_id": operation_id,
        "documents_updated": len(selected),
        "document_ids": selected,
        "human_verified_count": 0,
        "customer_eligible_count": 0,
        "next_step": "单独运行AI补跑，随后重新验证证据并由真人审核。",
    }
