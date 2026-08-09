from __future__ import annotations

import json
import re
from typing import Mapping

from platform_db import new_id, now_iso


_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])(?=[”’」』）)]|[^”’」』）)])")
_CLAUSE_END = re.compile(r"(?<=[，,:：、])")


def _split_long_unit(value: str, target_size: int) -> list[str]:
    """Split an unusually long sentence only at visible clause boundaries."""

    clauses = [item.strip() for item in _CLAUSE_END.split(value) if item.strip()]
    if len(clauses) <= 1:
        return [value[index:index + target_size] for index in range(0, len(value), target_size)]
    units: list[str] = []
    current = ""
    for clause in clauses:
        candidate = current + clause
        if current and len(candidate) > target_size:
            units.append(current)
            current = clause
        else:
            current = candidate
    if current:
        units.append(current)
    return units


def _sentence_units(text: str, target_size: int) -> list[str]:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [re.sub(r"[ \t\u3000]+", " ", item).strip() for item in re.split(r"\n\s*\n+", value)]
    units: list[str] = []
    for paragraph in (item for item in paragraphs if item):
        # A single line break in extracted HTML/PDF is layout, not a semantic boundary.
        paragraph = re.sub(r"\s*\n\s*", "", paragraph)
        sentences = [item.strip() for item in _SENTENCE_END.split(paragraph) if item.strip()]
        for sentence in sentences:
            units.extend(_split_long_unit(sentence, target_size) if len(sentence) > target_size else [sentence])
    return units


def _overlap_units(units: list[str], overlap: int) -> list[str]:
    if overlap <= 0:
        return []
    selected: list[str] = []
    used = 0
    for unit in reversed(units):
        if selected and used + len(unit) > overlap:
            break
        selected.insert(0, unit)
        used += len(unit)
        if used >= overlap:
            break
    return selected


def chunk_text(text: str, target_size: int = 650, overlap: int = 100, minimum: int = 120) -> list[str]:
    if target_size < 100 or overlap < 0 or overlap >= target_size:
        raise ValueError("分块参数无效")
    units = _sentence_units(text, target_size)
    chunks: list[str] = []
    current_units: list[str] = []
    for unit in units:
        candidate = "".join([*current_units, unit])
        if current_units and len(candidate) > target_size:
            completed = "".join(current_units).strip()
            if completed:
                chunks.append(completed)
            overlap_prefix = _overlap_units(current_units, overlap)
            current_units = [*overlap_prefix, unit]
            # A very long clause may exceed the nominal limit; keep the boundary intact.
            if len("".join(current_units)) > target_size + overlap:
                current_units = [unit]
        else:
            current_units.append(unit)
    current = "".join(current_units).strip()
    if current:
        if chunks and len(current) < minimum and len(chunks[-1]) + len(current) <= target_size + overlap:
            chunks[-1] = f"{chunks[-1]}{current}"
        else:
            chunks.append(current)
    return chunks


def complete_sentence_excerpt(text: str, maximum: int = 220) -> str:
    """Return a citation preview beginning and ending at a complete sentence boundary."""

    units = _sentence_units(text, max(100, maximum))
    selected: list[str] = []
    for unit in units:
        if selected and len("".join([*selected, unit])) > maximum:
            break
        selected.append(unit)
        if len("".join(selected)) >= maximum:
            break
    excerpt = "".join(selected).strip()
    if excerpt:
        return excerpt
    return str(text or "").strip()[:maximum]


def build_chunk_rows(text: str, metadata: Mapping[str, object]) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": new_id("CHK"),
            "document_id": str(metadata.get("document_id") or ""),
            "workspace_id": str(metadata.get("workspace_id") or ""),
            "event_id": str(metadata.get("event_id") or ""),
            "chunk_index": index,
            "chunk_text": chunk,
            "token_or_character_count": len(chunk),
            "metadata_json": json.dumps(dict(metadata), ensure_ascii=False, default=str),
            "embedding_status": "待向量化",
            "created_at": now_iso(),
        }
        for index, chunk in enumerate(chunk_text(text))
    ]
