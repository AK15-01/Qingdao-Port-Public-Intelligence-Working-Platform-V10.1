from __future__ import annotations

import json
import re
from typing import Mapping, Optional

from platform_db import connect, initialize_database


def _terms(query: str) -> list[str]:
    values: list[str] = []
    for item in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", query or ""):
        values.append(item)
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", item):
            values.extend(item[index:index + 2] for index in range(len(item) - 1))
    return list(dict.fromkeys(value for value in values if value))


class KeywordSearcher:
    def __init__(self, db_path) -> None:
        self.db_path = db_path

    def search(self, query: str, workspace_id: str = "", limit: int = 10, filters: Optional[Mapping[str, object]] = None) -> list[dict[str, object]]:
        initialize_database(self.db_path)
        terms = _terms(query)
        filters = dict(filters or {})
        where = [
            "d.workspace_id=?",
            "d.is_current=1",
            "d.record_state='active'",
            "(e.record_state IS NULL OR e.record_state='active')",
        ]
        params: list[object] = [workspace_id]
        if filters.get("start_date"):
            where.append("d.published_at>=?")
            params.append(str(filters["start_date"]))
        if filters.get("end_date"):
            where.append("d.published_at<=?")
            params.append(str(filters["end_date"]) + "T23:59:59")
        if filters.get("source_id"):
            where.append("d.source_id=?")
            params.append(str(filters["source_id"]))
        if filters.get("category"):
            where.append("e.category=?")
            params.append(str(filters["category"]))
        if filters.get("status"):
            where.append("e.status=?")
            params.append(str(filters["status"]))
        if filters.get("statuses"):
            values = [str(value) for value in filters["statuses"]]
            where.append("e.status IN (%s)" % ",".join("?" for _ in values))
            params.extend(values)
        if filters.get("categories"):
            values = [str(value) for value in filters["categories"]]
            where.append("e.category IN (%s)" % ",".join("?" for _ in values))
            params.extend(values)
        if filters.get("risk_levels"):
            values = [str(value) for value in filters["risk_levels"]]
            where.append("e.risk_level IN (%s)" % ",".join("?" for _ in values))
            params.extend(values)
        if filters.get("opportunity_levels"):
            values = [str(value) for value in filters["opportunity_levels"]]
            where.append("e.opportunity_level IN (%s)" % ",".join("?" for _ in values))
            params.extend(values)
        if filters.get("exclude_resolved_by_relation"):
            where.append(
                """NOT EXISTS(
                SELECT 1 FROM events resolved
                WHERE resolved.workspace_id=e.workspace_id
                AND resolved.related_event_id=e.event_id
                AND resolved.status IN ('解除','已结束')
                )"""
            )
        if filters.get("human_verified") is not None:
            where.append("e.human_verified=?")
            params.append(int(bool(filters["human_verified"])))
        if filters.get("report_eligible") is not None:
            where.append("e.report_eligible=?")
            params.append(int(bool(filters["report_eligible"])))
        if filters.get("evidence_verified") is not None:
            where.append("e.evidence_verified=?")
            params.append(int(bool(filters["evidence_verified"])))
        business_values = filters.get("business_value")
        if isinstance(business_values, (list, tuple, set)) and business_values:
            values = [str(value) for value in business_values]
            where.append("e.business_value IN (%s)" % ",".join("?" for _ in values))
            params.extend(values)
        elif business_values:
            where.append("e.business_value=?")
            params.append(str(business_values))
        base_params = list(params)
        event_rows = []
        if terms:
            event_like = " OR ".join(
                "(e.title LIKE ? OR e.summary LIKE ? OR e.impact LIKE ? OR e.matched_terms LIKE ?)"
                for _ in terms
            )
            event_params: list[object] = []
            for term in terms:
                token = f"%{term}%"
                event_params.extend([token, token, token, token])
            with connect(self.db_path) as connection:
                event_rows = connection.execute(
                    f"""SELECT ('EVENT:' || e.event_id) AS chunk_id,d.document_id,e.event_id,
                    COALESCE(
                        (SELECT x.quote_text FROM event_evidence x
                         WHERE x.event_id=e.event_id AND x.verification_status='已验证'
                         ORDER BY x.start_offset LIMIT 1),
                        e.summary
                    ) AS chunk_text,'{{}}' AS metadata_json,
                    d.title,d.publisher,d.published_at,d.fetched_at,d.canonical_url,
                    e.category,e.status,e.human_verified,e.report_eligible,e.risk_level,
                    e.opportunity_level,e.business_value,e.summary,e.impact,e.affected_area,
                    e.recommended_action,e.related_event_id,e.evidence_verified,
                    e.reviewer_type,e.review_state,d.quality_status,
                    s.source_type,s.internal_analysis_allowed,
                    s.customer_summary_allowed,s.short_quote_allowed
                    FROM events e JOIN documents d ON d.document_id=e.document_id
                    LEFT JOIN sources s ON s.source_id=d.source_id
                    WHERE {' AND '.join(where)} AND ({event_like})
                    ORDER BY e.human_verified DESC,e.evidence_verified DESC,
                    CASE WHEN d.quality_status='合格' THEN 1 ELSE 0 END DESC,
                    d.published_at DESC LIMIT ?""",
                    [*base_params, *event_params, int(limit)],
                ).fetchall()
        fts_rows = []
        if terms:
            fts_query = " OR ".join(f'"{term}"' for term in terms)
            try:
                with connect(self.db_path) as connection:
                    fts_rows = connection.execute(
                        f"""SELECT c.chunk_id,c.document_id,c.event_id,c.chunk_text,c.metadata_json,
                        d.title,d.publisher,d.published_at,d.fetched_at,d.canonical_url,
                        e.category,e.status,e.human_verified,e.report_eligible,e.risk_level,
                        e.opportunity_level,e.business_value,e.summary,e.impact,e.affected_area,
                        e.recommended_action,e.related_event_id,e.evidence_verified,
                        e.reviewer_type,e.review_state,d.quality_status,
                        s.source_type,s.internal_analysis_allowed,
                        s.customer_summary_allowed,s.short_quote_allowed,
                        bm25(document_chunks_fts) AS fts_rank
                        FROM document_chunks_fts JOIN document_chunks c USING(chunk_id)
                        JOIN documents d ON d.document_id=c.document_id
                        LEFT JOIN events e ON e.document_id=d.document_id LEFT JOIN sources s ON s.source_id=d.source_id
                        WHERE {' AND '.join(where)} AND document_chunks_fts MATCH ?
                        ORDER BY e.human_verified DESC,e.evidence_verified DESC,
                        fts_rank,d.published_at DESC LIMIT ?""",
                        [*base_params, fts_query, int(limit)],
                    ).fetchall()
            except Exception:
                fts_rows = []
        like_clause = " OR ".join("c.chunk_text LIKE ?" for _ in terms) or "1=1"
        params = [*base_params, *[f"%{term}%" for term in terms], int(limit)]
        sql = f"""SELECT c.chunk_id,c.document_id,c.event_id,c.chunk_text,c.metadata_json,
            d.title,d.publisher,d.published_at,d.fetched_at,d.canonical_url,
            e.category,e.status,e.human_verified,e.report_eligible,e.risk_level,
            e.opportunity_level,e.business_value,e.summary,e.impact,e.affected_area,
            e.recommended_action,e.related_event_id,e.evidence_verified,
            e.reviewer_type,e.review_state,d.quality_status,
            s.source_type,s.internal_analysis_allowed,
            s.customer_summary_allowed,s.short_quote_allowed
            FROM document_chunks c JOIN documents d ON d.document_id=c.document_id
            LEFT JOIN events e ON e.document_id=d.document_id LEFT JOIN sources s ON s.source_id=d.source_id
            WHERE {' AND '.join(where)} AND ({like_clause})
            ORDER BY e.human_verified DESC,e.evidence_verified DESC,d.published_at DESC LIMIT ?"""
        if fts_rows:
            rows = fts_rows
        else:
            with connect(self.db_path) as connection:
                rows = connection.execute(sql, params).fetchall()
        results = []
        event_documents = {str(row["document_id"]) for row in event_rows}
        combined_rows = list(event_rows) + [
            row for row in rows if str(row["document_id"]) not in event_documents
        ]
        for rank, row in enumerate(combined_rows):
            item = dict(row)
            item["keyword_score"] = 1.0 / (rank + 1)
            item["retrieval_tier"] = (
                "human_verified_event"
                if str(item.get("chunk_id", "")).startswith("EVENT:")
                and bool(item.get("human_verified"))
                and bool(item.get("evidence_verified"))
                else (
                    "evidence_verified_event"
                    if str(item.get("chunk_id", "")).startswith("EVENT:")
                    and bool(item.get("evidence_verified"))
                    else "quality_document_chunk"
                )
            )
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                item["metadata"] = {}
            results.append(item)
        return results
