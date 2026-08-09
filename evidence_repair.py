from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional

from evidence_binding import locate_evidence_quote, suggest_evidence_candidates
from platform_db import connect, new_id, now_iso, transaction
from review_provenance import HUMAN_REVIEWER_TYPES


def _reason(quote: str, source: str, candidates: list[dict[str, object]]) -> tuple[str, bool]:
    compact_quote = "".join(quote.split())
    compact_source = "".join(source.split())
    if "黄海中南部" in compact_quote and "黄海中南部" not in compact_source:
        return "引用把原文“黄海中部”等地域改写为“黄海中南部”，涉及事实范围变化", True
    if candidates and float(candidates[0].get("similarity") or 0) >= 0.92:
        return "与原文候选高度接近，但存在标点、增删词或句界变化；旧引用不是连续原文", False
    if candidates and float(candidates[0].get("similarity") or 0) >= 0.55:
        return "模型对原文进行了概括、删节或把长句改写为短句", False
    if any(part and part in compact_source for part in compact_quote.replace("；", "。").split("。")):
        return "引用可能拼接了多个不连续片段", False
    return "未找到足够接近的连续原文，需回原文重新抽取", False


def list_evidence_issues(db_path, workspace_id: str = "") -> list[dict[str, object]]:
    where = ["x.verification_status!='已验证'"]
    params: list[object] = []
    if workspace_id:
        where.append("e.workspace_id=?")
        params.append(workspace_id)
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT x.*,d.title AS document_title,d.cleaned_text,d.document_version AS current_version,
            d.content_hash AS current_content_hash,d.canonical_url,d.publisher,
            e.title AS event_title,e.summary,e.impact,e.affected_area,e.event_date,
            e.category,e.status,e.workspace_id,e.human_verified,e.reviewer_type,e.record_state
            FROM event_evidence x JOIN documents d ON d.document_id=x.document_id
            JOIN events e ON e.event_id=x.event_id
            WHERE {' AND '.join(where)}
            ORDER BY e.event_id,x.evidence_id""",
            params,
        ).fetchall()
    issues: list[dict[str, object]] = []
    for raw in rows:
        row = dict(raw)
        source = str(row.get("cleaned_text") or "")
        candidates = suggest_evidence_candidates(str(row.get("quote_text") or ""), source, limit=3)
        reason, meaning_change = _reason(str(row.get("quote_text") or ""), source, candidates)
        issues.append(
            {
                **row,
                "reason": reason,
                "meaning_change": meaning_change,
                "suggested_action": (
                    "退回重新抽取或由真人纠正事件事实，不得只改偏移位置"
                    if meaning_change
                    else "由真人打开原文，从候选段落选择连续原文或重新填写引用"
                ),
                "candidates": candidates,
                "cleaned_text": source,
            }
        )
    return issues


def refresh_evidence_candidates(db_path, workspace_id: str = "") -> dict[str, int]:
    issues = list_evidence_issues(db_path, workspace_id)
    updated = 0
    with transaction(db_path) as connection:
        for issue in issues:
            candidate = (issue.get("candidates") or [{}])[0]
            connection.execute(
                """UPDATE event_evidence SET candidate_start_offset=?,candidate_end_offset=?,
                candidate_text=?,candidate_score=?,repair_status='needs_human_review',
                failure_reason=?,updated_at=? WHERE evidence_id=?""",
                (
                    int(candidate.get("start_offset") or -1),
                    int(candidate.get("end_offset") or -1),
                    str(candidate.get("candidate_text") or ""),
                    float(candidate.get("similarity") or 0),
                    str(issue["reason"]),
                    now_iso(),
                    issue["evidence_id"],
                ),
            )
            updated += 1
    return {"issues": len(issues), "updated": updated}


def apply_exact_evidence_repair(
    evidence_id: str,
    replacement_quote: str,
    db_path,
    *,
    reviewer_type: str,
    reviewer_name: str,
    reviewer_note: str = "",
    confirmed: bool = False,
    ui_session_id: str = "",
    event_edits: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    if not confirmed:
        raise PermissionError("保存证据修复前必须由用户确认")
    if reviewer_type not in HUMAN_REVIEWER_TYPES or not str(reviewer_name or "").strip():
        raise ValueError("证据修复必须记录 human_user 或 industry_reviewer")
    with connect(db_path) as connection:
        row = connection.execute(
            """SELECT x.*,d.cleaned_text,d.document_version AS current_version,
            d.content_hash AS current_content_hash,e.workspace_id
            FROM event_evidence x JOIN documents d ON d.document_id=x.document_id
            JOIN events e ON e.event_id=x.event_id WHERE x.evidence_id=?""",
            (evidence_id,),
        ).fetchone()
    if not row:
        raise KeyError("证据记录不存在")
    item = dict(row)
    candidates = suggest_evidence_candidates(
        str(item.get("quote_text") or ""),
        str(item.get("cleaned_text") or ""),
        limit=3,
    )
    reason, meaning_change = _reason(
        str(item.get("quote_text") or ""),
        str(item.get("cleaned_text") or ""),
        candidates,
    )
    edits = {
        key: str(value or "").strip()
        for key, value in dict(event_edits or {}).items()
        if key in {
            "title", "event_date", "summary", "impact", "affected_area",
            "affected_period", "category", "status", "related_event_id", "analyst_note",
        }
    }
    if meaning_change:
        if not edits:
            raise ValueError("该证据涉及地域事实变化，必须先同步修正事件内容，不能只替换引用")
        if not reviewer_note.strip():
            raise ValueError("涉及事实变化的修复必须填写修改原因")
        with connect(db_path) as connection:
            current_event = connection.execute(
                "SELECT * FROM events WHERE event_id=?",
                (item["event_id"],),
            ).fetchone()
        corrected = {**dict(current_event or {}), **edits}
        factual_text = " ".join(
            str(corrected.get(field) or "")
            for field in ("title", "summary", "affected_area", "analyst_note")
        )
        if "黄海中南部" in factual_text and "黄海中南部" not in str(item["cleaned_text"] or ""):
            raise ValueError("事件内容仍含原文不存在的“黄海中南部”，请先改为原文支持的地域表述")
    binding = locate_evidence_quote(
        replacement_quote,
        str(item["cleaned_text"] or ""),
        document_id=str(item["document_id"]),
        document_version=int(item["current_version"] or 1),
        content_hash=str(item["current_content_hash"] or ""),
    )
    if binding.verification_status != "已验证":
        raise ValueError("替换内容不是当前原文中的连续逐字引用，不能认证")
    before = {
        key: item.get(key)
        for key in (
            "quote_text",
            "document_version",
            "content_hash",
            "start_offset",
            "end_offset",
            "verification_status",
        )
    }
    after = binding.as_dict()
    timestamp = now_iso()
    with transaction(db_path) as connection:
        event_before = connection.execute(
            "SELECT * FROM events WHERE event_id=?",
            (item["event_id"],),
        ).fetchone()
        if edits:
            assignments = ",".join(f"{field}=?" for field in edits)
            connection.execute(
                f"UPDATE events SET {assignments},updated_at=? WHERE event_id=?",
                [*edits.values(), timestamp, item["event_id"]],
            )
        connection.execute(
            """UPDATE event_evidence SET quote_text=?,document_version=?,
            document_version_id=?,content_hash=?,quote_hash=?,start_offset=?,end_offset=?,
            verification_status='已验证',verified_at=?,normalization_method=?,
            failure_reason='',candidate_start_offset=-1,candidate_end_offset=-1,
            candidate_text='',candidate_score=0,repair_status='human_repaired',updated_at=?
            WHERE evidence_id=?""",
            (
                binding.quote_text,
                binding.document_version,
                binding.document_version_id,
                binding.content_hash,
                binding.quote_hash,
                binding.start_offset,
                binding.end_offset,
                timestamp,
                binding.normalization_method,
                timestamp,
                evidence_id,
            ),
        )
        failed = connection.execute(
            """SELECT COUNT(*) FROM event_evidence
            WHERE event_id=? AND verification_status!='已验证'""",
            (item["event_id"],),
        ).fetchone()[0]
        total = connection.execute(
            "SELECT COUNT(*) FROM event_evidence WHERE event_id=?",
            (item["event_id"],),
        ).fetchone()[0]
        if meaning_change:
            connection.execute(
                """UPDATE events SET evidence_verified=?,human_verified=0,verified_at='',
                report_eligible=0,active_for_internal_use=0,eligible_for_customer_report=0,
                review_state='needs_second_review',requires_second_review=1,updated_at=?
                WHERE event_id=?""",
                (int(bool(total) and not failed), timestamp, item["event_id"]),
            )
        else:
            connection.execute(
                """UPDATE events SET evidence_verified=?,
                active_for_internal_use=CASE WHEN human_verified=1 AND ?=1 THEN active_for_internal_use ELSE 0 END,
                eligible_for_customer_report=CASE WHEN human_verified=1 AND ?=1 THEN eligible_for_customer_report ELSE 0 END,
                report_eligible=CASE WHEN human_verified=1 AND ?=1 THEN report_eligible ELSE 0 END,
                updated_at=? WHERE event_id=?""",
                (
                    int(bool(total) and not failed),
                    int(bool(total) and not failed),
                    int(bool(total) and not failed),
                    int(bool(total) and not failed),
                    timestamp,
                    item["event_id"],
                ),
            )
        event_after = connection.execute(
            "SELECT * FROM events WHERE event_id=?",
            (item["event_id"],),
        ).fetchone()
        connection.execute(
            """INSERT INTO event_reviews(
            review_id,workspace_id,event_id,reviewer_type,reviewer_name,reviewed_at,
            review_method,review_version,reviewer_note,checklist_json,decision,created_at,
            before_json,after_json,changed_fields_json,requires_second_review,review_stage
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("REV"),
                item["workspace_id"],
                item["event_id"],
                reviewer_type,
                reviewer_name,
                timestamp,
                "界面逐字证据修复",
                "0.5.0-beta",
                f"{reason}。{reviewer_note}".strip("。"),
                "{}",
                "证据事实修正" if meaning_change else "证据修复",
                timestamp,
                json.dumps(
                    {"evidence": before, "event": dict(event_before or {})},
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps(
                    {"evidence": after, "event": dict(event_after or {})},
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps(["evidence_quote", *edits.keys()], ensure_ascii=False),
                int(meaning_change),
                "evidence_review",
            ),
        )
    if not meaning_change:
        from review_service import customer_summary_gate, get_review_record, internal_use_gate

        reviewed = get_review_record(
            str(item["event_id"]),
            str(item["workspace_id"]),
            db_path,
        )
        if reviewed:
            human = bool(
                reviewed.get("human_verified")
                and str(reviewed.get("reviewer_type") or "") in HUMAN_REVIEWER_TYPES
                and not reviewed.get("requires_second_review")
            )
            internal_allowed, _ = internal_use_gate(reviewed)
            customer_allowed, _ = customer_summary_gate(reviewed)
            with transaction(db_path) as connection:
                connection.execute(
                    """UPDATE events SET active_for_internal_use=?,
                    eligible_for_customer_report=?,report_eligible=?,updated_at=?
                    WHERE event_id=?""",
                    (
                        int(human and internal_allowed),
                        int(human and customer_allowed),
                        int(human and customer_allowed),
                        now_iso(),
                        item["event_id"],
                    ),
                )
    from operation_store import finish_operation, start_operation

    operation_run_id = start_operation(
        str(item["workspace_id"]),
        "evidence_repair",
        db_path,
        ui_session_id=ui_session_id,
        input_summary=f"修复事件 {item['event_id']} 的逐字证据",
        metadata={"event_id": item["event_id"], "evidence_id": evidence_id},
    )
    finish_operation(
        operation_run_id,
        db_path,
        result_summary="证据已绑定到当前文档版本",
        counts={"events_updated": 1},
        metadata={
            "event_ids": [item["event_id"]],
            "document_ids": [item["document_id"]],
            "evidence_id": evidence_id,
        },
    )
    return {
        "evidence_id": evidence_id,
        "event_id": item["event_id"],
        "verification_status": "已验证",
        "start_offset": binding.start_offset,
        "end_offset": binding.end_offset,
        "document_version_id": binding.document_version_id,
        "quote_hash": binding.quote_hash,
        "meaning_change": meaning_change,
        "requires_second_review": meaning_change,
        "post_audit_verified": not any(
            issue["evidence_id"] == evidence_id
            for issue in list_evidence_issues(db_path, str(item["workspace_id"]))
        ),
        "operation_run_id": operation_run_id,
    }


def write_evidence_repair_report(
    db_path,
    output_path: Path,
    *,
    workspace_id: str = "",
) -> dict[str, object]:
    issues = list_evidence_issues(db_path, workspace_id)
    payload = {
        "generated_at": now_iso(),
        "database": Path(db_path).name,
        "workspace_id": workspace_id,
        "issue_count": len(issues),
        "gate": "模糊候选只供人工查看；未逐字定位记录继续保留，但不能进入客户报告。",
        "issues": issues,
    }
    lines = [
        "# 证据逐字定位修复报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 问题数量：{len(issues)}",
        "- 规则：候选段落不等于认证；只有当前文档版本中的连续原文才能保存为已验证。",
        "",
    ]
    for index, issue in enumerate(issues, 1):
        candidate = (issue.get("candidates") or [{}])[0]
        lines.extend(
            [
                f"## {index}. {issue['event_id']} / {issue['document_id']}",
                "",
                f"- 文档标题：{issue.get('document_title') or '未提取标题'}",
                f"- 事件标题：{issue.get('event_title') or ''}",
                f"- 当前引用：{issue.get('quote_text') or ''}",
                f"- 最可能对应段落：{candidate.get('candidate_text') or '未找到可靠候选'}",
                f"- 候选相似度：{candidate.get('similarity') or 0}",
                f"- 无法定位原因：{issue['reason']}",
                f"- 建议处理：{issue['suggested_action']}",
                f"- 是否涉及事实含义变化：{'是' if issue['meaning_change'] else '未发现明确事实变化，但仍需真人核对'}",
                "",
            ]
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(output_path)
    json_path = output_path.with_suffix(".json")
    temporary_json = json_path.with_suffix(".json.tmp")
    temporary_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary_json.replace(json_path)
    return payload
