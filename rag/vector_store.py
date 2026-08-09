from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence


class VectorStoreUnavailable(RuntimeError):
    pass


class ChromaVectorStore:
    def __init__(self, persist_path: Path, collection_name: str = "port_documents") -> None:
        self.persist_path = Path(persist_path)
        self.collection_name = collection_name
        self._client = None
        self._collection = None

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb
        except ImportError as exc:
            raise VectorStoreUnavailable("尚未安装 chromadb；SQLite FTS5 关键词检索仍可用。") from exc
        self.persist_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_path))
        self._collection = self._client.get_or_create_collection(name=self.collection_name, metadata={"hnsw:space": "cosine"})
        return self._collection

    def upsert(self, ids: Sequence[str], documents: Sequence[str], embeddings: Sequence[Sequence[float]], metadatas: Sequence[Mapping[str, object]]) -> None:
        if not ids:
            return
        clean_meta = [{key: value if isinstance(value, (str, int, float, bool)) else str(value) for key, value in item.items()} for item in metadatas]
        self._get_collection().upsert(ids=list(ids), documents=list(documents), embeddings=[list(item) for item in embeddings], metadatas=clean_meta)

    def delete_document(self, document_id: str) -> None:
        self._get_collection().delete(where={"document_id": document_id})

    def query(self, embedding: Sequence[float], n_results: int = 10, where: Optional[dict[str, object]] = None) -> list[dict[str, object]]:
        payload = self._get_collection().query(query_embeddings=[list(embedding)], n_results=n_results, where=where or None,
                                                include=["documents", "metadatas", "distances"])
        results: list[dict[str, object]] = []
        ids = payload.get("ids", [[]])[0]
        documents = payload.get("documents", [[]])[0]
        metadatas = payload.get("metadatas", [[]])[0]
        distances = payload.get("distances", [[]])[0]
        for index, chunk_id in enumerate(ids):
            distance = float(distances[index]) if index < len(distances) else 1.0
            results.append({"chunk_id": chunk_id, "chunk_text": documents[index], "metadata": metadatas[index] or {}, "vector_score": max(0.0, 1.0 - distance)})
        return results

    def count(self) -> int:
        return int(self._get_collection().count())

    def reset(self) -> None:
        collection = self._get_collection()
        if self._client is not None:
            self._client.delete_collection(self.collection_name)
        self._collection = None
