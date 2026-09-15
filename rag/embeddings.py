from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...
    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingUnavailable(RuntimeError):
    pass


@dataclass
class LocalBGEEmbedding:
    model: str = DEFAULT_MODEL
    device: str = "cpu"

    def __post_init__(self) -> None:
        self._encoder = None

    @property
    def model_name(self) -> str:
        return self.model

    def _load(self):
        if self._encoder is not None:
            return self._encoder
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "尚未安装 sentence-transformers（pip install -r requirements-rag.txt）；关键词检索仍可用。安装依赖后首次索引会提示下载 BAAI/bge-small-zh-v1.5。"
            ) from exc
        # Prefer an already downloaded model without performing remote HEAD
        # requests. If it is not cached, fall back to the normal first-run
        # download flow so the UI can still explain that network access is needed.
        try:
            self._encoder = SentenceTransformer(
                self.model,
                device=self.device,
                local_files_only=True,
            )
        except Exception:
            self._encoder = SentenceTransformer(self.model, device=self.device)
        return self._encoder

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._load().encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, vector)) for vector in vectors]
