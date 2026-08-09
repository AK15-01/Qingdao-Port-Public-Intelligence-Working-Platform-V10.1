from __future__ import annotations

from typing import Mapping, Sequence


def build_context(results: Sequence[Mapping[str, object]], max_characters: int = 12000) -> tuple[str, list[str]]:
    blocks: list[str] = []
    document_ids: list[str] = []
    used = 0
    for index, item in enumerate(results, 1):
        document_id = str(item.get("document_id") or (item.get("metadata") or {}).get("document_id") or "")
        if not document_id:
            continue
        text = str(item.get("chunk_text") or "").strip()
        block = (
            f"[来源{index} document_id={document_id} "
            f"status={item.get('status', '')} category={item.get('category', '')} "
            f"geography={item.get('geography_scope', '')} "
            f"qingdao_relation={item.get('qingdao_relation', '')}]\n{text}"
        )
        if used + len(block) > max_characters:
            break
        blocks.append(block)
        document_ids.append(document_id)
        used += len(block)
    return "\n\n".join(blocks), list(dict.fromkeys(document_ids))
