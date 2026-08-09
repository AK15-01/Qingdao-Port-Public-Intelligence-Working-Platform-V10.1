from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
import time
from typing import Mapping, Optional

from business_value import classify_business_value
from data_store import ALLOWED_CATEGORIES
from data_validator import validate_events
from intake_analyzer import suggest_category, suggest_status
from platform_db import connect, new_id, now_iso, transaction
from risk_engine import calculate_scores
from deepseek_service import DeepSeekSettings, ExtractionOutcome, extract_event
from evidence_binding import locate_evidence_quote, replace_event_evidence
from review_provenance import normalize_review_identity


def _source_confidence_type(source: Mapping[str, object]) -> str:
    return str(source.get("source_type") or "其他")


def _first_verbatim_sentence(text: str, maximum: int = 180) -> str:
    """Return a short, exact source sentence for deterministic evidence binding."""

    value = str(text or "").strip()
    if not value:
        return ""
    match = re.search(r".{4,%d}?[。！？!?](?:[”’」』])?" % maximum, value, re.S)
    if match:
        return match.group(0).strip()
    first_line = next((line.strip() for line in value.splitlines() if len(line.strip()) >= 4), "")
    return first_line[:maximum].strip()


def local_extract(document: Mapping[str, object], source: Mapping[str, object]) -> dict[str, object]:
    title = str(document.get("title") or "").strip()
    text = str(document.get("cleaned_text") or "").strip()
    category, category_terms = suggest_category(title, text, str(source.get("category_hint") or ""))
    status, status_terms = suggest_status(title, text)
    summary = text[:600].strip()
    if len(text) > 600:
        summary = summary.rsplit("。", 1)[0] + "。" if "。" in summary else summary
    quote = _first_verbatim_sentence(text)
    return {
        "title": title or "待人工补充标题",
        "event_date": str(document.get("published_at") or "")[:10],
        "category": category if category in ALLOWED_CATEGORIES else "企业动态",
        "summary": summary,
        "impact": "待人工评估公开信息对业务的潜在影响。",
        "affected_area": "",
        "affected_period": "",
        "status": status,
        "recommended_action": "核对原文、日期、影响范围和来源许可后再确认进入商业报告。",
        "extraction_method": "rules",
        "extraction_confidence": 0.35,
        "ai_generated": 0,
        "evidence_quotes": [quote] if quote else [],
        "evidence_verified": int(bool(quote)),
        "analyst_note": (
            "确定性规则初稿，需人工确认。证据片段：" + quote
            if quote else "确定性规则初稿，未找到可逐字定位的证据片段。"
        ),
    }


def _evidence_traceable(quotes: list[str], source_text: str) -> bool:
    return bool(quotes) and all(
        locate_evidence_quote(quote, source_text).verification_status == "已验证"
        for quote in quotes
    )


def event_from_outcome(
    outcome: ExtractionOutcome,
    fallback: dict[str, object],
    source_text: str = "",
) -> dict[str, object]:
    if not outcome.ok or outcome.data is None:
        return {
            **fallback,
            "evidence_verified": int(
                _evidence_traceable(list(fallback.get("evidence_quotes") or []), source_text)
            ),
        }
    item = outcome.data
    quotes = [str(value).strip() for value in item.evidence_quotes if str(value).strip()][:3]
    evidence_verified = _evidence_traceable(quotes, source_text)
    evidence_note = "｜".join(quotes)
    if not evidence_verified:
        evidence_note += "｜证据片段未通过原文定位，需人工复核"
    return {
        "title": item.title,
        "event_date": item.event_date or fallback.get("event_date", ""),
        "category": item.category,
        "summary": item.factual_summary,
        "impact": item.potential_impact,
        "affected_area": item.affected_area,
        "affected_period": item.affected_period,
        "status": item.status,
        "recommended_action": item.suggested_action,
        "extraction_method": "deepseek",
        "extraction_confidence": item.confidence,
        "ai_generated": 1,
        "evidence_verified": int(evidence_verified),
        "evidence_quotes": quotes,
        "analyst_note": "机器抽取结果，需人工确认。证据片段：" + evidence_note,
    }


def duplicate_level(title: str, source_url: str, workspace_id: str, db_path) -> tuple[str, list[str]]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT e.event_id,e.title,e.source_url FROM events e
            LEFT JOIN documents d ON d.document_id=e.document_id
            WHERE e.workspace_id=? AND (e.document_id='' OR d.is_current=1)""",
            (workspace_id,),
        ).fetchall()
    candidates: list[str] = []
    highest = "未发现明显重复"
    for row in rows:
        if source_url and source_url == row["source_url"]:
            return "高度疑似重复", [str(row["event_id"])]
        ratio = SequenceMatcher(None, title.casefold(), str(row["title"]).casefold()).ratio()
        if title and title == row["title"]:
            highest = "高度疑似重复"
            candidates.append(str(row["event_id"]))
        elif ratio >= 0.84:
            if highest != "高度疑似重复":
                highest = "可能重复"
            candidates.append(str(row["event_id"]))
    return highest, candidates[:5]


def create_event_for_document(
    document_id: str,
    document: Mapping[str, object],
    source: Mapping[str, object],
    workspace_id: str,
    db_path,
    ai_enabled: bool = True,
    requester=None,
    settings: Optional[DeepSeekSettings] = None,
) -> tuple[str, dict[str, object]]:
    fallback = local_extract(document, source)
    metadata = {
        "workspace_id": workspace_id,
        "document_id": document_id,
        "published_at": document.get("published_at", ""),
        "publisher": document.get("publisher", ""),
        "source_url": document.get("canonical_url", ""),
        "fetched_at": document.get("fetched_at", ""),
    }
    if ai_enabled:
        kwargs = {"db_path": db_path}
        if settings is not None:
            kwargs["settings"] = settings
        if requester is not None:
            kwargs["requester"] = requester
        outcome = extract_event(str(document.get("cleaned_text") or ""), metadata, **kwargs)
    else:
        outcome = ExtractionOutcome(False, None, "", "disabled", "AI辅助已关闭。")
    event = event_from_outcome(outcome, fallback, str(document.get("cleaned_text") or ""))
    if str(document.get("title") or "").strip():
        event["title"] = str(document["title"]).strip()
    trusted_published_at = str(document.get("published_at") or "")[:10]
    if trusted_published_at:
        event["event_date"] = trusted_published_at
    event_id = new_id("EVT")
    timestamp = now_iso()
    event.update({
        "event_id": event_id,
        "workspace_id": workspace_id,
        "document_id": document_id,
        "collected_at": str(document.get("fetched_at") or timestamp),
        "source_name": str(document.get("publisher") or source.get("source_name") or ""),
        "source_type": _source_confidence_type(source),
        "source_url": str(document.get("canonical_url") or document.get("original_url") or ""),
        "related_event_id": "",
    })
    level, duplicate_ids = duplicate_level(str(event["title"]), str(event["source_url"]), workspace_id, db_path)
    event["duplicate_level"] = level
    event["analyst_note"] = (str(event.get("analyst_note") or "") + (f" 重复候选：{','.join(duplicate_ids)}" if duplicate_ids else "")).strip()
    value = classify_business_value(event)
    event["business_value"] = value.level
    event["value_reason"] = value.reason
    scores = calculate_scores(event)
    # Automated events are intentionally not report eligible until a human confirms them.
    with transaction(db_path) as connection:
        connection.execute(
            """INSERT INTO events(event_id,workspace_id,document_id,event_date,collected_at,category,title,summary,impact,
            affected_area,affected_period,source_name,source_type,source_url,status,related_event_id,analyst_note,
            extraction_method,extraction_confidence,ai_generated,human_verified,verified_at,report_eligible,duplicate_level,
            raw_risk_score,historical_risk_score,current_priority_score,source_confidence,opportunity_score,risk_level,
            opportunity_level,matched_terms,score_explanation,recommended_action,business_value,value_reason,
            evidence_verified,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(event_id), str(workspace_id), str(document_id), str(event["event_date"]), str(event["collected_at"]), str(event["category"]), str(event["title"]),
             str(event["summary"]), str(event["impact"]), str(event["affected_area"]), str(event["affected_period"]), str(event["source_name"]),
             str(event["source_type"]), str(event["source_url"]), str(event["status"]), str(event["related_event_id"]), str(event.get("analyst_note", "")),
             str(event["extraction_method"]), float(event["extraction_confidence"]), int(event["ai_generated"]), 0, "", 0, str(level),
             scores["raw_risk_score"], scores["historical_risk_score"], scores["current_priority_score"], scores["source_confidence"],
             scores["opportunity_score"], str(scores["risk_level"]), str(scores["opportunity_level"]), str(scores["matched_terms"]),
             str(scores["score_explanation"]), str(scores["action"]), str(event["business_value"]),
             str(event["value_reason"]), int(event.get("evidence_verified", 0)), str(timestamp), str(timestamp)),
        )
        connection.execute(
            "UPDATE documents SET ai_status=?,updated_at=? WHERE document_id=?",
            ("已完成" if outcome.ok else "待AI处理", timestamp, document_id),
        )
        bindings = replace_event_evidence(
            connection,
            event_id,
            document_id,
            list(event.get("evidence_quotes") or []),
        )
        event["evidence_verified"] = int(
            bool(bindings) and all(item.verification_status == "已验证" for item in bindings)
        )
        connection.execute("UPDATE document_chunks SET event_id=? WHERE document_id=?", (event_id, document_id))
        connection.execute("UPDATE document_chunks_fts SET event_id=? WHERE document_id=?", (event_id, document_id))
    return event_id, {**event, **scores, "validation": validate_events(__import__("pandas").DataFrame([event]))}


def batch_confirm_events(
    event_ids: list[str],
    workspace_id: str,
    db_path,
    report_eligible: bool = True,
    *,
    reviewer_type: str = "unknown",
    reviewer_name: str = "",
    review_method: str = "",
    review_version: str = "0.5.0-beta",
    reviewer_note: str = "",
    checklist: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    confirmed: list[str] = []
    blocked: dict[str, str] = {}
    report_ineligible: dict[str, str] = {}
    timestamp = now_iso()
    identity = normalize_review_identity(
        reviewer_type,
        reviewer_name,
        review_method,
        review_version,
        reviewer_note,
    )
    if not identity.is_independent_human:
        return {
            "confirmed": [],
            "blocked": {event_id: "只有 human_user 或 industry_reviewer 才能完成人工核验" for event_id in event_ids},
            "report_ineligible": {},
        }
    if not identity.reviewer_name or not identity.review_method:
        return {
            "confirmed": [],
            "blocked": {event_id: "必须记录核验人和核验方法" for event_id in event_ids},
            "report_ineligible": {},
        }
    checklist_payload = json.dumps(dict(checklist or {}), ensure_ascii=False, default=str)
    with transaction(db_path) as connection:
        for event_id in event_ids:
            row = connection.execute("SELECT * FROM events WHERE event_id=? AND workspace_id=?", (event_id, workspace_id)).fetchone()
            if not row:
                blocked[event_id] = "事件不存在"
                continue
            item = dict(row)
            report = validate_events(__import__("pandas").DataFrame([item]))
            if report.error_count:
                blocked[event_id] = f"数据校验有 {report.error_count} 项错误"
                continue
            if item["duplicate_level"] == "高度疑似重复":
                blocked[event_id] = "高度疑似重复"
                continue
            if item["status"] == "解除" and not item["related_event_id"]:
                blocked[event_id] = "解除事件未关联原事件"
                continue
            document_quality = connection.execute(
                "SELECT quality_status,report_quality_eligible FROM documents WHERE document_id=?",
                (item["document_id"],),
            ).fetchone()
            if not document_quality or document_quality["quality_status"] != "合格" or not document_quality["report_quality_eligible"]:
                blocked[event_id] = "原始文档未通过正文质量或报告门禁"
                continue
            source = connection.execute(
                """SELECT internal_analysis_allowed,customer_summary_allowed,short_quote_allowed,
                commercial_reuse_status,terms_status FROM sources
                WHERE source_id=(SELECT source_id FROM documents WHERE document_id=?)""",
                (item["document_id"],),
            ).fetchone()
            source_eligible = bool(
                source
                and source["customer_summary_allowed"]
                and source["short_quote_allowed"]
                and str(source["commercial_reuse_status"] or "") not in {"禁止", "不允许"}
                and str(source["terms_status"] or "") not in {"禁止", "不允许"}
            )
            evidence_eligible = bool(item.get("evidence_verified"))
            eligible = bool(report_eligible and source_eligible and evidence_eligible)
            internal_eligible = bool(
                evidence_eligible
                and source
                and source["internal_analysis_allowed"]
            )
            failures: list[str] = []
            if report_eligible and not source_eligible:
                failures.append("来源不允许生成客户摘要/短引用，或条款明确禁止")
            if report_eligible and not evidence_eligible:
                failures.append("证据引用尚未在当前原文版本中逐字定位")
            if failures:
                report_ineligible[event_id] = "；".join(failures)
            connection.execute(
                """UPDATE events SET human_verified=1,verified_at=?,report_eligible=?,
                active_for_internal_use=?,eligible_for_customer_report=?,
                reviewer_type=?,reviewer_name=?,review_method=?,review_version=?,
                reviewer_note=?,review_state='accepted',requires_second_review=0,
                updated_at=? WHERE event_id=?""",
                (
                    timestamp,
                    int(eligible),
                    int(internal_eligible),
                    int(eligible),
                    identity.reviewer_type,
                    identity.reviewer_name,
                    identity.review_method,
                    identity.review_version,
                    identity.reviewer_note,
                    timestamp,
                    event_id,
                ),
            )
            connection.execute(
                """INSERT INTO event_reviews(
                review_id,workspace_id,event_id,reviewer_type,reviewer_name,reviewed_at,
                review_method,review_version,reviewer_note,checklist_json,decision,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("REV"),
                    workspace_id,
                    event_id,
                    identity.reviewer_type,
                    identity.reviewer_name,
                    timestamp,
                    identity.review_method,
                    identity.review_version,
                    identity.reviewer_note,
                    checklist_payload,
                    "确认",
                    timestamp,
                ),
            )
            connection.execute("UPDATE documents SET review_status='已确认',updated_at=? WHERE document_id=?", (timestamp, item["document_id"]))
            confirmed.append(event_id)
    return {"confirmed": confirmed, "blocked": blocked, "report_ineligible": report_ineligible}


def _reprocess_pending_ai_impl(
    workspace_id: str,
    db_path,
    *,
    limit: int = 100,
    settings: Optional[DeepSeekSettings] = None,
    requester=None,
    cancel_check=None,
    progress=None,
    time_budget_seconds: float = 120.0,
    operation_run_id: str = "",
) -> dict[str, object]:
    """Re-run extraction for stored documents only; never fetches the web again."""
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT d.*,s.source_name,s.source_type,s.category_hint,e.event_id
            FROM documents d JOIN sources s ON s.source_id=d.source_id
            LEFT JOIN events e ON e.document_id=d.document_id
            WHERE d.workspace_id=? AND d.is_current=1 AND d.ai_status='待AI处理'
            AND d.quality_status='合格' AND d.extraction_status IN ('成功','提取成功')
            ORDER BY d.fetched_at LIMIT ?""",
            (workspace_id, max(1, min(int(limit), 500))),
        ).fetchall()
    result: dict[str, object] = {
        "input": len(rows), "success": 0, "failed": 0, "skipped": 0,
        "event_ids": [], "errors": [], "models": [], "cancelled": False,
    }
    batch_started = time.monotonic()
    for index, raw in enumerate(rows, start=1):
        if cancel_check and cancel_check():
            result["cancelled"] = True
            break
        if time.monotonic() - batch_started >= max(1.0, float(time_budget_seconds)):
            result["errors"].append("AI补跑达到本批总时间预算，剩余文档留待下次处理。")
            break
        row = dict(raw)
        if progress:
            progress(index - 1, len(rows), row, result, "正在AI抽取")
        if not row.get("event_id"):
            # A document explicitly released from the quality quarantine has
            # no event yet. Create a rules-only, unverified placeholder here;
            # this AI task remains separate from the user's quality decision.
            try:
                event_id, _ = create_event_for_document(
                    str(row["document_id"]),
                    row,
                    row,
                    workspace_id,
                    db_path,
                    ai_enabled=False,
                )
                row["event_id"] = event_id
            except Exception as exc:
                result["failed"] = int(result["failed"]) + 1
                result["errors"].append(
                    f"{row['document_id']}：无法建立待AI事件：{type(exc).__name__}"
                )
                if progress:
                    progress(index, len(rows), row, result, "建立待AI事件失败")
                continue
        metadata = {
            "workspace_id": workspace_id, "document_id": row["document_id"],
            "published_at": row.get("published_at", ""), "publisher": row.get("publisher", ""),
            "source_url": row.get("canonical_url", ""), "fetched_at": row.get("fetched_at", ""),
        }
        kwargs = {"db_path": db_path}
        if settings is not None:
            kwargs["settings"] = settings
        if requester is not None:
            kwargs["requester"] = requester
        try:
            outcome = extract_event(
                str(row.get("cleaned_text") or ""),
                metadata,
                **kwargs,
            )
        except Exception as exc:
            result["failed"] = int(result["failed"]) + 1
            result["errors"].append(
                f"{row['document_id']}：{type(exc).__name__}: {str(exc)[:300]}"
            )
            if operation_run_id:
                from operation_store import add_technical_log

                add_technical_log(
                    workspace_id,
                    db_path,
                    f"AI文档处理异常：{type(exc).__name__}",
                    operation_run_id=operation_run_id,
                    level="error",
                    component="ai_reprocess",
                    details={
                        "document_id": str(row["document_id"]),
                        "error_type": type(exc).__name__,
                    },
                )
            if progress:
                progress(index, len(rows), row, result, "AI处理失败")
            continue
        if not outcome.ok or outcome.data is None:
            result["failed"] = int(result["failed"]) + 1
            result["errors"].append(f"{row['document_id']}：{outcome.note or outcome.error_type}")
            if operation_run_id:
                from operation_store import add_technical_log

                add_technical_log(
                    workspace_id,
                    db_path,
                    f"AI文档处理失败：{outcome.error_type or 'unknown'}",
                    operation_run_id=operation_run_id,
                    level=(
                        "warning"
                        if outcome.error_type in {"timeout", "network_error"}
                        else "error"
                    ),
                    component="ai_reprocess",
                    details={
                        "document_id": str(row["document_id"]),
                        "error_type": str(outcome.error_type or ""),
                    },
                )
            if progress:
                progress(index, len(rows), row, result, "AI处理失败")
            continue
        fallback = local_extract(row, row)
        event = event_from_outcome(outcome, fallback, str(row.get("cleaned_text") or ""))
        if str(row.get("title") or "").strip():
            event["title"] = str(row["title"]).strip()
        if row.get("published_at"):
            event["event_date"] = str(row["published_at"])[:10]
        existing = {
            **event,
            "source_name": str(row.get("publisher") or row.get("source_name") or ""),
            "source_type": str(row.get("source_type") or "其他"),
            "source_url": str(row.get("canonical_url") or row.get("original_url") or ""),
        }
        value = classify_business_value(existing)
        event["business_value"] = value.level
        event["value_reason"] = value.reason
        scores = calculate_scores(existing)
        timestamp = now_iso()
        with transaction(db_path) as connection:
            connection.execute(
                """UPDATE events SET event_date=?,category=?,title=?,summary=?,impact=?,affected_area=?,affected_period=?,
                status=?,analyst_note=?,extraction_method='deepseek',extraction_confidence=?,ai_generated=1,
                raw_risk_score=?,historical_risk_score=?,current_priority_score=?,source_confidence=?,opportunity_score=?,
                risk_level=?,opportunity_level=?,matched_terms=?,score_explanation=?,recommended_action=?,
                business_value=?,value_reason=?,evidence_verified=?,updated_at=?
                WHERE event_id=? AND workspace_id=?""",
                (event["event_date"], event["category"], event["title"], event["summary"], event["impact"],
                 event["affected_area"], event["affected_period"], event["status"], event.get("analyst_note", ""),
                 float(event["extraction_confidence"]), scores["raw_risk_score"], scores["historical_risk_score"],
                 scores["current_priority_score"], scores["source_confidence"], scores["opportunity_score"],
                 scores["risk_level"], scores["opportunity_level"], scores["matched_terms"],
                  scores["score_explanation"], scores["action"], event["business_value"], event["value_reason"],
                  int(event.get("evidence_verified", 0)), timestamp, row["event_id"], workspace_id),
            )
            bindings = replace_event_evidence(
                connection,
                str(row["event_id"]),
                str(row["document_id"]),
                list(event.get("evidence_quotes") or []),
            )
            event["evidence_verified"] = int(
                bool(bindings) and all(item.verification_status == "已验证" for item in bindings)
            )
            connection.execute(
                "UPDATE documents SET ai_status='已完成',updated_at=? WHERE document_id=?",
                (timestamp, row["document_id"]),
            )
        result["success"] = int(result["success"]) + 1
        result["event_ids"].append(str(row["event_id"]))
        if outcome.model_name and outcome.model_name not in result["models"]:
            result["models"].append(outcome.model_name)
        if progress:
            progress(index, len(rows), row, result, "AI处理完成")
    return result


def reprocess_pending_ai(
    workspace_id: str,
    db_path,
    *,
    limit: int = 100,
    settings: Optional[DeepSeekSettings] = None,
    requester=None,
    ui_session_id: str = "",
    operation_run_id: str = "",
    cancel_check=None,
    progress=None,
    time_budget_seconds: float = 120.0,
) -> dict[str, object]:
    """Persist the AI reprocessing lifecycle while reusing already archived documents."""

    from operation_store import add_technical_log, finish_operation, start_operation

    operation_run_id = str(operation_run_id or "")
    if not operation_run_id:
        operation_run_id = start_operation(
            workspace_id,
            "ai_reprocess",
            db_path,
            ui_session_id=ui_session_id,
            input_summary=f"补跑最多 {max(1, min(int(limit), 500))} 篇待AI文档；不重新抓取网页",
            metadata={"limit": limit, "network_refetch": False},
        )
    add_technical_log(
        workspace_id,
        db_path,
        "待AI补跑任务开始；只读取本地归档，不重新抓取网页。",
        operation_run_id=operation_run_id,
        level="info",
        component="ai_reprocess",
        details={"limit": max(1, min(int(limit), 5))},
    )
    try:
        result = _reprocess_pending_ai_impl(
            workspace_id,
            db_path,
            limit=limit,
            settings=settings,
            requester=requester,
            cancel_check=cancel_check,
            progress=progress,
            time_budget_seconds=time_budget_seconds,
            operation_run_id=operation_run_id,
        )
    except Exception as exc:
        finish_operation(
            operation_run_id,
            db_path,
            status="failed",
            result_summary="待AI补跑异常结束",
            error_summary=f"{type(exc).__name__}: {exc}",
            counts={"failed_count": 1},
        )
        raise
    status = (
        "cancelled"
        if bool(result.get("cancelled"))
        else (
            "succeeded"
            if int(result["failed"]) == 0 and not result.get("errors")
            else ("partially_succeeded" if int(result["success"]) else "failed")
        )
    )
    finish_operation(
        operation_run_id,
        db_path,
        status=status,
        result_summary=(
            f"输入{result['input']}；成功{result['success']}；"
            f"跳过{result['skipped']}；失败{result['failed']}"
        ),
        counts={
            "events_updated": int(result["success"]),
            "skipped_count": int(result["skipped"]),
            "failed_count": int(result["failed"]),
        },
        error_summary="\n".join(str(value) for value in result.get("errors", [])),
        metadata={"models": list(result.get("models", [])), "network_refetch": False},
    )
    result["operation_run_id"] = operation_run_id
    add_technical_log(
        workspace_id,
        db_path,
        f"待AI补跑任务结束：成功{result['success']}，失败{result['failed']}。",
        operation_run_id=operation_run_id,
        level=(
            "info"
            if status == "succeeded"
            else ("warning" if status in {"partially_succeeded", "cancelled"} else "error")
        ),
        component="ai_reprocess",
        details={
            "input": int(result["input"]),
            "success": int(result["success"]),
            "failed": int(result["failed"]),
            "cancelled": bool(result.get("cancelled")),
        },
    )
    return result
