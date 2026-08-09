from __future__ import annotations

from typing import Mapping, Sequence

from document_chunker import complete_sentence_excerpt
from platform_db import connect


def build_citations(document_ids: Sequence[str], snippets: Mapping[str, str], db_path) -> list[dict[str, object]]:
    citations: list[dict[str, object]] = []
    with connect(db_path) as connection:
        for document_id in dict.fromkeys(document_ids):
            row = connection.execute(
                """SELECT d.document_id,d.document_version,d.content_hash,d.title,d.publisher,
                d.published_at,d.fetched_at,d.canonical_url,e.event_id,e.human_verified,
                e.reviewer_type,e.review_state,e.evidence_verified
                FROM documents d LEFT JOIN events e ON e.document_id=d.document_id
                AND e.record_state='active'
                WHERE d.document_id=? AND d.is_current=1 AND d.record_state='active'
                ORDER BY e.human_verified DESC,e.evidence_verified DESC LIMIT 1""",
                (document_id,),
            ).fetchone()
            if not row:
                continue
            item = dict(row)
            evidence = connection.execute(
                """SELECT quote_text,start_offset,end_offset,verification_status,
                normalization_method,verified_at FROM event_evidence
                WHERE document_id=? AND document_version=? AND content_hash=?
                AND verification_status='已验证'
                ORDER BY start_offset LIMIT 1""",
                (document_id, item["document_version"], item["content_hash"]),
            ).fetchone()
            if evidence:
                item.update(dict(evidence))
                item["quote"] = str(evidence["quote_text"])
                item["evidence_verified"] = True
            else:
                item["quote"] = complete_sentence_excerpt(str(snippets.get(document_id, "")), 220)
                item["evidence_verified"] = False
                item["verification_status"] = "检索片段，未作为逐字证据绑定"
            if not str(item.get("title") or "").strip() or str(item.get("title")) == "未提取标题":
                item["title"] = (
                    f"{item.get('publisher') or '公开来源'}"
                    f" {str(item.get('published_at') or '')[:10]} 公开资料"
                ).strip()
            item["human_review_status"] = (
                "独立真人已核验"
                if bool(item.get("human_verified"))
                and str(item.get("reviewer_type")) in {"human_user", "industry_reviewer"}
                else "待真人核验"
            )
            citations.append(item)
    return citations
