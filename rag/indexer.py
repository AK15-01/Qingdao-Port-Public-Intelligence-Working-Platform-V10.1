from __future__ import annotations

from pathlib import Path

from document_chunker import build_chunk_rows
from platform_db import connect, transaction

from .embeddings import LocalBGEEmbedding
from .vector_store import ChromaVectorStore


class KnowledgeIndexer:
    def __init__(self, db_path, chroma_path: Path, embedder=None, vector_store=None) -> None:
        self.db_path = db_path
        self.embedder = embedder or LocalBGEEmbedding()
        self.vector_store = vector_store or ChromaVectorStore(chroma_path)

    def delete_document(self, document_id: str) -> None:
        self.vector_store.delete_document(document_id)

    def rechunk_document(self, document_id: str) -> int:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """SELECT d.document_id,d.workspace_id,d.source_id,d.publisher,d.published_at,
                d.canonical_url,d.cleaned_text,e.event_id,e.category,e.status,e.human_verified,
                e.report_eligible,e.business_value,e.risk_level,e.opportunity_level,
                e.affected_area,e.evidence_verified
                FROM documents d LEFT JOIN events e ON e.document_id=d.document_id
                WHERE d.document_id=? AND d.is_current=1 AND d.record_state='active'
                AND (e.record_state IS NULL OR e.record_state='active')""",
                (document_id,),
            ).fetchone()
        if not row or not str(row["cleaned_text"] or "").strip():
            return 0
        metadata = {
            "workspace_id": str(row["workspace_id"] or ""),
            "document_id": document_id,
            "event_id": str(row["event_id"] or ""),
            "source_id": str(row["source_id"] or ""),
            "source_name": str(row["publisher"] or ""),
            "category": str(row["category"] or ""),
            "published_at": str(row["published_at"] or ""),
            "status": str(row["status"] or ""),
            "source_url": str(row["canonical_url"] or ""),
            "human_verified": bool(row["human_verified"]),
            "report_eligible": bool(row["report_eligible"]),
            "business_value": str(row["business_value"] or ""),
            "risk_level": str(row["risk_level"] or ""),
            "opportunity_level": str(row["opportunity_level"] or ""),
            "affected_area": str(row["affected_area"] or ""),
            "evidence_verified": bool(row["evidence_verified"]),
            "record_state": "active",
        }
        chunks = build_chunk_rows(str(row["cleaned_text"]), metadata)
        with transaction(self.db_path) as connection:
            connection.execute("DELETE FROM document_chunks_fts WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            for chunk in chunks:
                connection.execute(
                    """INSERT INTO document_chunks(
                    chunk_id,workspace_id,document_id,event_id,chunk_index,chunk_text,
                    token_or_character_count,metadata_json,embedding_status,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        chunk["chunk_id"], chunk["workspace_id"], document_id,
                        metadata["event_id"], chunk["chunk_index"], chunk["chunk_text"],
                        chunk["token_or_character_count"], chunk["metadata_json"],
                        "待向量化", chunk["created_at"],
                    ),
                )
                connection.execute(
                    """INSERT INTO document_chunks_fts(
                    chunk_id,workspace_id,document_id,event_id,chunk_text,metadata_json
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        chunk["chunk_id"], chunk["workspace_id"], document_id,
                        metadata["event_id"], chunk["chunk_text"], chunk["metadata_json"],
                    ),
                )
        return len(chunks)

    def index_document(self, document_id: str) -> int:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """SELECT c.*,d.source_id,d.publisher,d.published_at,d.canonical_url,d.is_current,
                e.category,e.status,e.human_verified,e.report_eligible,e.business_value,
                e.risk_level,e.opportunity_level,e.affected_area,e.evidence_verified
                FROM document_chunks c JOIN documents d ON d.document_id=c.document_id
                LEFT JOIN events e ON e.document_id=d.document_id
                WHERE c.document_id=? AND d.is_current=1 AND d.record_state='active'
                AND (e.record_state IS NULL OR e.record_state='active')
                ORDER BY c.chunk_index""",
                (document_id,),
            ).fetchall()
        if not rows:
            return 0
        texts = [str(row["chunk_text"]) for row in rows]
        vectors = self.embedder.encode(texts)
        metadatas = []
        for row in rows:
            metadatas.append({
                "workspace_id": str(row["workspace_id"] or ""),
                "document_id": document_id,
                "event_id": str(row["event_id"] or ""),
                "source_id": str(row["source_id"] or ""),
                "source_name": str(row["publisher"] or ""),
                "category": str(row["category"] or ""),
                "published_at": str(row["published_at"] or ""),
                "status": str(row["status"] or ""),
                "source_url": str(row["canonical_url"] or ""),
                "human_verified": bool(row["human_verified"]),
                "report_eligible": bool(row["report_eligible"]),
                "business_value": str(row["business_value"] or ""),
                "risk_level": str(row["risk_level"] or ""),
                "opportunity_level": str(row["opportunity_level"] or ""),
                "affected_area": str(row["affected_area"] or ""),
                "evidence_verified": bool(row["evidence_verified"]),
                "record_state": "active",
            })
        self.vector_store.upsert([str(row["chunk_id"]) for row in rows], texts, vectors, metadatas)
        with transaction(self.db_path) as connection:
            connection.execute("UPDATE document_chunks SET embedding_status='成功' WHERE document_id=?", (document_id,))
        return len(rows)

    def rebuild(self, workspace_id: str = "", *, rechunk: bool = True) -> dict[str, int]:
        self.vector_store.reset()
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """SELECT document_id FROM documents WHERE workspace_id=? AND is_current=1
                AND record_state='active' AND extraction_status IN ('成功','提取成功')""",
                (workspace_id,),
            ).fetchall()
        success = failed = chunks = 0
        for row in rows:
            try:
                if rechunk:
                    self.rechunk_document(str(row["document_id"]))
                chunks += self.index_document(str(row["document_id"]))
                success += 1
            except Exception:
                failed += 1
                with transaction(self.db_path) as connection:
                    connection.execute("UPDATE document_chunks SET embedding_status='失败' WHERE document_id=?", (row["document_id"],))
        return {"documents": len(rows), "success": success, "failed": failed, "chunks": chunks}
