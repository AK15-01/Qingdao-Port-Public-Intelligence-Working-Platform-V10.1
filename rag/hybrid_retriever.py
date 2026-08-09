from __future__ import annotations

from typing import Mapping, Optional

from platform_db import connect

from .keyword_search import KeywordSearcher


class HybridRetriever:
    def __init__(self, keyword_searcher: KeywordSearcher, embedder=None, vector_store=None) -> None:
        self.keyword_searcher = keyword_searcher
        self.embedder = embedder
        self.vector_store = vector_store

    def retrieve(self, query: str, workspace_id: str = "", limit: int = 8, filters: Optional[Mapping[str, object]] = None) -> list[dict[str, object]]:
        keyword = self.keyword_searcher.search(query, workspace_id, max(limit * 2, 10), filters)
        combined: dict[str, dict[str, object]] = {}
        for rank, item in enumerate(keyword):
            value = dict(item)
            tier_bonus = {
                "human_verified_event": 1.2,
                "evidence_verified_event": 0.75,
                "quality_document_chunk": 0.25,
            }.get(str(value.get("retrieval_tier") or ""), 0.0)
            value["hybrid_score"] = tier_bonus + 0.55 / (rank + 1)
            combined[str(value["chunk_id"])] = value
        if self.embedder is not None and self.vector_store is not None:
            try:
                vector = self.embedder.encode([query])[0]
                conditions: list[dict[str, object]] = []
                if workspace_id:
                    conditions.append({"workspace_id": workspace_id})
                for key in (
                    "source_id", "category", "status", "human_verified",
                    "report_eligible", "evidence_verified",
                ):
                    if filters and filters.get(key) is not None:
                        conditions.append({key: filters[key]})
                for key, metadata_key in (
                    ("statuses", "status"),
                    ("categories", "category"),
                    ("risk_levels", "risk_level"),
                    ("opportunity_levels", "opportunity_level"),
                ):
                    if filters and filters.get(key):
                        conditions.append({metadata_key: {"$in": list(filters[key])}})
                if filters and filters.get("business_value"):
                    values = filters["business_value"]
                    conditions.append(
                        {"business_value": {"$in": list(values)}}
                        if isinstance(values, (list, tuple, set))
                        else {"business_value": values}
                    )
                if filters and filters.get("start_date"):
                    conditions.append({"published_at": {"$gte": str(filters["start_date"])}})
                if filters and filters.get("end_date"):
                    conditions.append({"published_at": {"$lte": str(filters["end_date"]) + "T23:59:59"}})
                where = conditions[0] if len(conditions) == 1 else ({"$and": conditions} if conditions else None)
                semantic = self.vector_store.query(vector, max(limit * 2, 10), where=where)
                with connect(self.keyword_searcher.db_path) as connection:
                    active_document_ids = {
                        str(row["document_id"])
                        for row in connection.execute(
                            """SELECT document_id FROM documents WHERE workspace_id=?
                            AND is_current=1 AND record_state='active'""",
                            (workspace_id,),
                        ).fetchall()
                    }
                for rank, item in enumerate(semantic):
                    semantic_item = dict(item)
                    metadata = dict(semantic_item.get("metadata") or {})
                    for key, value in metadata.items():
                        semantic_item.setdefault(key, value)
                    if str(semantic_item.get("document_id") or "") not in active_document_ids:
                        continue
                    chunk_id = str(semantic_item["chunk_id"])
                    value = combined.get(chunk_id, semantic_item)
                    value["hybrid_score"] = float(value.get("hybrid_score", 0)) + 0.45 / (rank + 1)
                    combined[chunk_id] = value
            except Exception:
                pass
        values = list(combined.values())
        values.sort(
            key=lambda item: (
                float(item.get("hybrid_score", 0)),
                bool(item.get("human_verified", False)),
                bool(item.get("evidence_verified", False)),
                str(item.get("published_at", "")),
            ),
            reverse=True,
        )
        deduplicated: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in values:
            identity = (
                f"event:{item.get('event_id')}"
                if str(item.get("event_id") or "")
                else f"document:{item.get('document_id')}"
            )
            if identity in seen:
                continue
            seen.add(identity)
            deduplicated.append(item)
            if len(deduplicated) >= limit:
                break
        return deduplicated
