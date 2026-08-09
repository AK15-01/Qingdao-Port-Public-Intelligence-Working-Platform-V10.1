from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Iterable, Mapping

from platform_db import connect, initialize_database, new_id, now_iso, transaction
from review_provenance import HUMAN_REVIEWER_TYPES
from workspace_store import log_action


@dataclass(frozen=True)
class PromotionResult:
    promoted: tuple[str, ...]
    blocked: Mapping[str, str]


def _row(connection: sqlite3.Connection, query: str, params: tuple[object, ...]) -> dict[str, object] | None:
    value = connection.execute(query, params).fetchone()
    return dict(value) if value else None


def _insert_mapping(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, object],
    *,
    replace: bool = False,
) -> None:
    allowed = {
        str(item["name"])
        for item in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    payload = {key: value for key, value in values.items() if key in allowed}
    columns = ",".join(payload)
    placeholders = ",".join("?" for _ in payload)
    operation = "INSERT OR REPLACE" if replace else "INSERT"
    connection.execute(
        f"{operation} INTO {table}({columns}) VALUES({placeholders})",
        list(payload.values()),
    )


def _source_allowed(source: Mapping[str, object], report_mode: str) -> tuple[bool, str]:
    commercial = str(source.get("commercial_reuse_status") or "未明确")
    terms = str(source.get("terms_status") or "未检查")
    if commercial in {"禁止", "不允许"} or terms in {"禁止", "不允许"}:
        return False, "来源条款明确禁止当前用途"
    if report_mode == "客户交付版" and not (
        bool(source.get("customer_summary_allowed"))
        and bool(source.get("short_quote_allowed"))
    ):
        return False, "来源未允许客户摘要或必要短引用"
    if report_mode == "内部研究版" and not bool(source.get("internal_analysis_allowed", True)):
        return False, "来源配置不允许内部分析"
    return True, ""


def _promotion_gate(
    qa_connection: sqlite3.Connection,
    event: Mapping[str, object],
    document: Mapping[str, object],
    source: Mapping[str, object],
    report_mode: str,
) -> str:
    reasons = _promotion_gate_reasons(qa_connection, event, document, source, report_mode)
    return "；".join(reasons)


def _promotion_gate_reasons(
    qa_connection: sqlite3.Connection,
    event: Mapping[str, object],
    document: Mapping[str, object],
    source: Mapping[str, object],
    report_mode: str,
) -> list[str]:
    reasons: list[str] = []
    if str(event.get("reviewer_type") or "") not in HUMAN_REVIEWER_TYPES:
        reasons.append("核验来源不是独立真人")
    if not bool(event.get("human_verified")) or not str(event.get("reviewer_name") or "").strip():
        reasons.append("缺少可追溯的真人核验")
    if str(event.get("review_state") or "") not in {"accepted", "modified_accepted"}:
        reasons.append("真人审核尚未接受")
    if bool(event.get("requires_second_review")):
        reasons.append("仍需第二人复核")
    if str(event.get("record_state") or "active") != "active":
        reasons.append("事件已驳回或隔离")
    if str(document.get("record_state") or "active") != "active":
        reasons.append("原文处于隔离状态")
    if str(document.get("quality_status") or "") != "合格" or not bool(
        document.get("report_quality_eligible")
    ):
        reasons.append("原文未通过质量门禁")
    if not str(document.get("canonical_url") or "").startswith(("http://", "https://")):
        reasons.append("原文URL不完整")
    if not str(event.get("source_name") or "").strip():
        reasons.append("来源名称缺失")
    if str(event.get("duplicate_level") or "") == "高度疑似重复":
        reasons.append("事件为高度疑似重复")
    evidence = qa_connection.execute(
        """SELECT verification_status,document_version,content_hash
        FROM event_evidence WHERE event_id=?""",
        (event["event_id"],),
    ).fetchall()
    if not evidence or any(
        str(item["verification_status"]) != "已验证"
        or int(item["document_version"]) != int(document.get("document_version") or 1)
        or str(item["content_hash"]) != str(document.get("content_hash") or "")
        for item in evidence
    ):
        reasons.append("证据引用未在当前原文版本逐字定位")
    allowed, reason = _source_allowed(source, report_mode)
    if not allowed:
        reasons.append(reason)
    return list(dict.fromkeys(reason for reason in reasons if reason))


def list_qa_promotion_candidates(
    qa_db_path,
    *,
    source_workspace_id: str,
    report_mode: str = "内部研究版",
) -> list[dict[str, object]]:
    """Return transparent gate results without changing QA or formal data."""

    initialize_database(qa_db_path)
    with connect(qa_db_path) as qa:
        events = qa.execute(
            """SELECT * FROM events WHERE workspace_id=?
            ORDER BY event_date DESC,created_at DESC""",
            (source_workspace_id,),
        ).fetchall()
        result: list[dict[str, object]] = []
        for raw_event in events:
            event = dict(raw_event)
            document = _row(
                qa,
                "SELECT * FROM documents WHERE document_id=? AND workspace_id=? AND is_current=1",
                (event["document_id"], source_workspace_id),
            )
            source = (
                _row(qa, "SELECT * FROM sources WHERE source_id=?", (document["source_id"],))
                if document
                else None
            )
            if not document or not source:
                reasons = ["原始文档或来源不存在"]
            else:
                reasons = _promotion_gate_reasons(
                    qa, event, document, source, report_mode
                )
            customer_reasons = (
                _promotion_gate_reasons(qa, event, document, source, "客户交付版")
                if document and source
                else ["原始文档或来源不存在"]
            )
            relevance_text = " ".join(
                str(event.get(field) or "")
                for field in ("title", "summary", "impact", "affected_area", "category")
            )
            if any(term in relevance_text for term in ("青岛", "胶州湾", "前湾港", "董家口")):
                qingdao_relevance = "直接相关"
            elif any(term in relevance_text for term in ("黄海", "山东", "港口", "海事", "航运", "物流")):
                qingdao_relevance = "可能相关，需人工判断"
            else:
                qingdao_relevance = "未发现明确关联"
            result.append(
                {
                    "event_id": str(event["event_id"]),
                    "title": str(event.get("title") or ""),
                    "event_date": str(event.get("event_date") or ""),
                    "human_reviewed": bool(
                        event.get("human_verified")
                        and str(event.get("reviewer_type") or "") in HUMAN_REVIEWER_TYPES
                    ),
                    "evidence_verified": bool(event.get("evidence_verified")),
                    "quality_status": str((document or {}).get("quality_status") or "缺失"),
                    "source_complete": bool(
                        document
                        and source
                        and str(document.get("canonical_url") or "").startswith(("http://", "https://"))
                        and str(event.get("source_name") or "").strip()
                    ),
                    "duplicate_level": str(event.get("duplicate_level") or ""),
                    "record_state": str(event.get("record_state") or ""),
                    "internal_eligible": not reasons,
                    "customer_eligible": not customer_reasons,
                    "qingdao_relevance": qingdao_relevance,
                    "block_reasons": reasons,
                    "customer_block_reasons": customer_reasons,
                }
            )
    return result


def promote_qa_events(
    qa_db_path,
    formal_db_path,
    *,
    source_workspace_id: str,
    target_workspace_id: str,
    event_ids: Iterable[str],
    qa_run_id: str,
    report_mode: str = "内部研究版",
    ui_session_id: str = "",
) -> PromotionResult:
    """Promote only gated records; never copies or overwrites a whole QA database."""

    initialize_database(qa_db_path)
    initialize_database(formal_db_path)
    promoted: list[str] = []
    blocked: dict[str, str] = {}
    requested = list(dict.fromkeys(str(value) for value in event_ids if str(value)))
    plans: list[dict[str, object]] = []
    with connect(qa_db_path) as qa, connect(formal_db_path) as formal:
        for event_id in requested:
            event = _row(
                qa,
                "SELECT * FROM events WHERE event_id=? AND workspace_id=?",
                (event_id, source_workspace_id),
            )
            if not event:
                blocked[event_id] = "QA事件不存在"
                continue
            document = _row(
                qa,
                "SELECT * FROM documents WHERE document_id=? AND workspace_id=? AND is_current=1",
                (event["document_id"], source_workspace_id),
            )
            source = (
                _row(qa, "SELECT * FROM sources WHERE source_id=?", (document["source_id"],))
                if document else None
            )
            if not document or not source:
                blocked[event_id] = "原始文档或来源不存在"
                continue
            reason = _promotion_gate(qa, event, document, source, report_mode)
            if reason:
                blocked[event_id] = reason
                continue
            previous = formal.execute(
                """SELECT target_event_id FROM promotion_history
                WHERE qa_run_id=? AND source_workspace_id=? AND target_workspace_id=?
                AND source_event_id=? LIMIT 1""",
                (qa_run_id, source_workspace_id, target_workspace_id, event_id),
            ).fetchone()
            if previous:
                blocked[event_id] = f"该QA事件已经晋升：{previous['target_event_id']}"
                continue
            evidences = [
                dict(row)
                for row in qa.execute(
                    "SELECT * FROM event_evidence WHERE event_id=? ORDER BY start_offset",
                    (event_id,),
                ).fetchall()
            ]
            chunks = [
                dict(row)
                for row in qa.execute(
                    "SELECT * FROM document_chunks WHERE document_id=? ORDER BY chunk_index",
                    (document["document_id"],),
                ).fetchall()
            ]
            reviews = [
                dict(row)
                for row in qa.execute(
                    "SELECT * FROM event_reviews WHERE event_id=? ORDER BY reviewed_at",
                    (event_id,),
                ).fetchall()
            ]
            duplicate = formal.execute(
                """SELECT d.document_id FROM documents d
                WHERE d.workspace_id=? AND d.is_current=1
                AND (d.canonical_url=? OR d.content_hash=?) LIMIT 1""",
                (target_workspace_id, document["canonical_url"], document["content_hash"]),
            ).fetchone()
            id_collision = formal.execute(
                "SELECT workspace_id FROM documents WHERE document_id=?",
                (document["document_id"],),
            ).fetchone()
            if duplicate:
                blocked[event_id] = f"正式工作空间已有相同URL或正文：{duplicate['document_id']}"
                continue
            if id_collision:
                blocked[event_id] = "原document_id在正式数据库中已被其他记录占用"
                continue
            plans.append(
                {
                    "event_id": event_id,
                    "event": event,
                    "document": document,
                    "source": source,
                    "evidences": evidences,
                    "chunks": chunks,
                    "reviews": reviews,
                }
            )
    # Batch promotion is deliberately all-or-nothing. A single blocked item
    # prevents every selected item from being written.
    if blocked:
        return PromotionResult((), blocked)
    if not plans:
        return PromotionResult((), blocked)

    from operation_store import finish_operation, start_operation

    operation_run_id = start_operation(
        target_workspace_id,
        "qa_promotion",
        formal_db_path,
        ui_session_id=ui_session_id,
        input_summary=f"受控晋升 {len(plans)} 条QA事件",
        metadata={"qa_run_id": qa_run_id, "event_ids": requested},
    )
    timestamp = now_iso()
    source_id_map: dict[str, str] = {}
    try:
        with transaction(formal_db_path) as formal:
            for plan in plans:
                event_id = str(plan["event_id"])
                event = dict(plan["event"])
                document = dict(plan["document"])
                source = dict(plan["source"])
                original_source_id = str(source["source_id"])
                target_source_id = source_id_map.get(original_source_id, original_source_id)
                if original_source_id not in source_id_map:
                    existing_source = formal.execute(
                        "SELECT * FROM sources WHERE source_id=?",
                        (target_source_id,),
                    ).fetchone()
                    if existing_source and str(existing_source["workspace_id"]) != target_workspace_id:
                        target_source_id = new_id("SRC")
                    source_id_map[original_source_id] = target_source_id
                    source_values = {
                        **source,
                        "source_id": target_source_id,
                        "workspace_id": target_workspace_id,
                        "enabled": 0,
                        "updated_at": timestamp,
                    }
                    if not existing_source or target_source_id != original_source_id:
                        _insert_mapping(formal, "sources", source_values)

                document_values = {
                    **document,
                    "workspace_id": target_workspace_id,
                    "source_id": target_source_id,
                    "qa_run_id": qa_run_id,
                    "promotion_source_document_id": document["document_id"],
                    "updated_at": timestamp,
                }
                _insert_mapping(formal, "documents", document_values)
                effective_source = (
                    dict(existing_source)
                    if existing_source
                    and str(existing_source["workspace_id"]) == target_workspace_id
                    and target_source_id == original_source_id
                    else source
                )
                customer_allowed, _ = _source_allowed(effective_source, "客户交付版")
                event_values = {
                    **event,
                    "workspace_id": target_workspace_id,
                    "qa_run_id": qa_run_id,
                    "promotion_source_event_id": event_id,
                    "record_state": "active",
                    "active_for_internal_use": 1,
                    "eligible_for_customer_report": int(customer_allowed),
                    "report_eligible": int(customer_allowed),
                    "updated_at": timestamp,
                }
                _insert_mapping(formal, "events", event_values)
                for evidence in plan["evidences"]:
                    _insert_mapping(
                        formal,
                        "event_evidence",
                        {**evidence, "evidence_id": new_id("EVD"), "updated_at": timestamp},
                    )
                for review in plan["reviews"]:
                    _insert_mapping(
                        formal,
                        "event_reviews",
                        {
                            **review,
                            "review_id": new_id("REV"),
                            "workspace_id": target_workspace_id,
                        },
                    )
                for chunk in plan["chunks"]:
                    chunk_id = new_id("CHK")
                    try:
                        metadata = json.loads(str(chunk.get("metadata_json") or "{}"))
                    except json.JSONDecodeError:
                        metadata = {}
                    metadata.update(
                        {
                            "workspace_id": target_workspace_id,
                            "document_id": document["document_id"],
                            "event_id": event_id,
                        }
                    )
                    chunk_values = {
                        **chunk,
                        "chunk_id": chunk_id,
                        "workspace_id": target_workspace_id,
                        "event_id": event_id,
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                        "embedding_status": "待向量化",
                        "created_at": timestamp,
                    }
                    _insert_mapping(formal, "document_chunks", chunk_values)
                    _insert_mapping(formal, "document_chunks_fts", chunk_values)
                formal.execute(
                    """INSERT INTO promotion_history(
                    promotion_id,qa_run_id,source_workspace_id,target_workspace_id,
                    source_event_id,source_document_id,target_event_id,target_document_id,
                    reviewer_type,reviewer_name,reviewed_at,promoted_at,details_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("PRM"), qa_run_id, source_workspace_id, target_workspace_id,
                        event_id, document["document_id"], event_id, document["document_id"],
                        event["reviewer_type"], event["reviewer_name"], event["verified_at"],
                        timestamp,
                        json.dumps(
                            {
                                "report_mode": report_mode,
                                "canonical_url": document["canonical_url"],
                                "content_hash": document["content_hash"],
                                "review_method": event.get("review_method", ""),
                                "active_for_internal_use": True,
                                "eligible_for_customer_report": bool(customer_allowed),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                promoted.append(event_id)
    except Exception as exc:
        finish_operation(
            operation_run_id,
            formal_db_path,
            status="failed",
            result_summary="QA晋升失败，事务已全部回滚",
            counts={"failed_count": len(plans)},
            error_summary=f"{type(exc).__name__}: {exc}",
        )
        return PromotionResult((), {event_id: "晋升失败，事务已回滚" for event_id in requested})

    for plan in plans:
        event_id = str(plan["event_id"])
        event = dict(plan["event"])
        document = dict(plan["document"])
        log_action(
            target_workspace_id,
            "QA晋升",
            "event",
            event_id,
            {
                "qa_run_id": qa_run_id,
                "source_workspace_id": source_workspace_id,
                "source_document_id": document["document_id"],
                "reviewer_type": event["reviewer_type"],
                "reviewer_name": event["reviewer_name"],
            },
            formal_db_path,
        )
    finish_operation(
        operation_run_id,
        formal_db_path,
        status="succeeded",
        result_summary=f"已受控晋升 {len(promoted)} 条事件；QA原记录保留",
        counts={"documents_created": len(promoted), "events_created": len(promoted)},
        metadata={"qa_run_id": qa_run_id, "event_ids": promoted},
    )
    return PromotionResult(tuple(promoted), blocked)
