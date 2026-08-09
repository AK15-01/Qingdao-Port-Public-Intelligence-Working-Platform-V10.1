from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import difflib
import re
import sqlite3
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class EvidenceBinding:
    quote_text: str
    document_id: str
    document_version: int
    document_version_id: str
    content_hash: str
    quote_hash: str
    start_offset: int
    end_offset: int
    verification_status: str
    verified_at: str
    normalization_method: str
    failure_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _compact_whitespace_with_offsets(value: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(str(value or "")):
        if character.isspace():
            continue
        compact.append(character)
        offsets.append(index)
    return "".join(compact), offsets


def locate_evidence_quote(
    quote_text: str,
    source_text: str,
    *,
    document_id: str = "",
    document_version: int = 1,
    content_hash: str = "",
) -> EvidenceBinding:
    """Locate a verbatim quote, allowing only whitespace/newline normalization."""

    quote = str(quote_text or "").strip()
    source = str(source_text or "")
    common = {
        "quote_text": quote,
        "document_id": str(document_id or ""),
        "document_version": max(1, int(document_version or 1)),
        "document_version_id": (
            f"{str(document_id or '')}:v{max(1, int(document_version or 1))}"
        ),
        "content_hash": str(content_hash or ""),
        "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
    }
    if len(re.sub(r"\s+", "", quote)) < 4:
        return EvidenceBinding(
            **common,
            start_offset=-1,
            end_offset=-1,
            verification_status="未定位",
            verified_at="",
            normalization_method="none",
            failure_reason="引用片段少于4个有效字符",
        )
    if not source:
        return EvidenceBinding(
            **common,
            start_offset=-1,
            end_offset=-1,
            verification_status="原文缺失",
            verified_at="",
            normalization_method="none",
            failure_reason="清洗原文为空",
        )
    exact_start = source.find(quote)
    if exact_start >= 0:
        return EvidenceBinding(
            **common,
            start_offset=exact_start,
            end_offset=exact_start + len(quote),
            verification_status="已验证",
            verified_at=_now_iso(),
            normalization_method="exact",
        )
    compact_source, source_offsets = _compact_whitespace_with_offsets(source)
    compact_quote, _ = _compact_whitespace_with_offsets(quote)
    normalized_start = compact_source.find(compact_quote)
    if normalized_start >= 0 and compact_quote:
        start = source_offsets[normalized_start]
        end = source_offsets[normalized_start + len(compact_quote) - 1] + 1
        return EvidenceBinding(
            **common,
            start_offset=start,
            end_offset=end,
            verification_status="已验证",
            verified_at=_now_iso(),
            normalization_method="whitespace_only",
        )
    return EvidenceBinding(
        **common,
        start_offset=-1,
        end_offset=-1,
        verification_status="未定位",
        verified_at="",
        normalization_method="none",
        failure_reason="原文中未找到逐字一致片段；仅空白和换行标准化也未匹配",
    )


def extract_legacy_evidence_quotes(note: object) -> list[str]:
    text = str(note or "").strip()
    match = re.search(
        r"证据片段[：:]\s*(.+?)(?:[｜|]证据片段未通过原文定位|\s+重复候选[：:]|"
        r"\s+人工QA复核[：:]|\s+历史文档经质量硬化|$)",
        text,
    )
    if not match:
        return []
    return [
        item.strip()
        for item in re.split(r"[｜|]", match.group(1))
        if len(re.sub(r"\s+", "", item)) >= 4
    ][:5]


def _document_row(connection: sqlite3.Connection, document_id: str) -> Mapping[str, object] | None:
    row = connection.execute(
        """SELECT document_id,document_version,content_hash,cleaned_text,is_current
        FROM documents WHERE document_id=?""",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def replace_event_evidence(
    connection: sqlite3.Connection,
    event_id: str,
    document_id: str,
    quotes: Sequence[str],
) -> list[EvidenceBinding]:
    from platform_db import new_id

    document = _document_row(connection, document_id)
    connection.execute("DELETE FROM event_evidence WHERE event_id=?", (event_id,))
    if not document:
        connection.execute("UPDATE events SET evidence_verified=0 WHERE event_id=?", (event_id,))
        return []
    bindings = [
        locate_evidence_quote(
            quote,
            str(document.get("cleaned_text") or ""),
            document_id=document_id,
            document_version=int(document.get("document_version") or 1),
            content_hash=str(document.get("content_hash") or ""),
        )
        for quote in quotes
        if str(quote or "").strip()
    ]
    timestamp = _now_iso()
    for binding in bindings:
        connection.execute(
            """INSERT INTO event_evidence(
            evidence_id,event_id,quote_text,document_id,document_version,content_hash,
            start_offset,end_offset,verification_status,verified_at,normalization_method,
            failure_reason,created_at,updated_at,document_version_id,quote_hash,
            repair_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("EVD"),
                event_id,
                binding.quote_text,
                binding.document_id,
                binding.document_version,
                binding.content_hash,
                binding.start_offset,
                binding.end_offset,
                binding.verification_status,
                binding.verified_at,
                binding.normalization_method,
                binding.failure_reason,
                timestamp,
                timestamp,
                binding.document_version_id,
                binding.quote_hash,
                "verified" if binding.verification_status == "已验证" else "needs_human_review",
            ),
        )
    verified = bool(bindings) and all(item.verification_status == "已验证" for item in bindings)
    connection.execute(
        "UPDATE events SET evidence_verified=?,updated_at=? WHERE event_id=?",
        (int(verified), timestamp, event_id),
    )
    return bindings


def invalidate_superseded_document_evidence(
    connection: sqlite3.Connection,
    document_id: str,
) -> int:
    timestamp = _now_iso()
    event_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT event_id FROM event_evidence WHERE document_id=?",
            (document_id,),
        ).fetchall()
    ]
    connection.execute(
        """UPDATE event_evidence SET verification_status='版本失配',verified_at='',
        failure_reason='文档已产生新版本，必须对新版本原文重新定位',
        repair_status='version_invalidated',updated_at=?
        WHERE document_id=?""",
        (timestamp, document_id),
    )
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        connection.execute(
            f"UPDATE events SET evidence_verified=0,report_eligible=0,updated_at=? "
            f"WHERE event_id IN ({placeholders})",
            [timestamp, *event_ids],
        )
    return len(event_ids)


def migrate_legacy_event_evidence(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """SELECT e.event_id,e.document_id,e.analyst_note
        FROM events e LEFT JOIN event_evidence x ON x.event_id=e.event_id
        WHERE e.document_id!='' GROUP BY e.event_id HAVING COUNT(x.evidence_id)=0"""
    ).fetchall()
    result = {"events": 0, "quotes": 0, "verified_events": 0, "failed_quotes": 0}
    for row in rows:
        quotes = extract_legacy_evidence_quotes(row["analyst_note"])
        if not quotes:
            if "证据片段" in str(row["analyst_note"] or ""):
                connection.execute(
                    "UPDATE events SET evidence_verified=0,report_eligible=0 WHERE event_id=?",
                    (row["event_id"],),
                )
            continue
        bindings = replace_event_evidence(
            connection,
            str(row["event_id"]),
            str(row["document_id"]),
            quotes,
        )
        result["events"] += 1
        result["quotes"] += len(bindings)
        result["verified_events"] += int(
            bool(bindings) and all(item.verification_status == "已验证" for item in bindings)
        )
        result["failed_quotes"] += sum(item.verification_status != "已验证" for item in bindings)
    return result


def suggest_evidence_candidates(
    quote_text: str,
    source_text: str,
    *,
    limit: int = 3,
) -> list[dict[str, object]]:
    """Return review suggestions only; callers must never mark them verified automatically."""

    quote = str(quote_text or "").strip()
    source = str(source_text or "")
    if not quote or not source:
        return []
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^。！？；\n]{4,}[。！？；]?", source):
        text = match.group(0).strip()
        if len(text) >= 4:
            start = source.find(text, match.start(), match.end())
            spans.append((start, start + len(text), text))
    paragraphs = [
        (match.start(), match.end(), match.group(0).strip())
        for match in re.finditer(r"[^\n]{8,}", source)
        if match.group(0).strip()
    ]
    candidates: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for start, end, text in [*spans, *paragraphs]:
        if (start, end) in seen:
            continue
        seen.add((start, end))
        score = difflib.SequenceMatcher(None, quote, text).ratio()
        candidates.append(
            {
                "candidate_text": text,
                "start_offset": start,
                "end_offset": end,
                "similarity": round(float(score), 4),
                "verification_status": "仅供人工候选，不得自动认证",
            }
        )
    candidates.sort(key=lambda item: float(item["similarity"]), reverse=True)
    return candidates[: max(1, min(int(limit), 10))]


def evidence_for_events(
    connection: sqlite3.Connection,
    event_ids: Iterable[str],
) -> dict[str, list[dict[str, object]]]:
    ids = [str(value) for value in event_ids if str(value)]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""SELECT * FROM event_evidence WHERE event_id IN ({placeholders})
        ORDER BY event_id,start_offset,evidence_id""",
        ids,
    ).fetchall()
    result: dict[str, list[dict[str, object]]] = {event_id: [] for event_id in ids}
    for row in rows:
        result.setdefault(str(row["event_id"]), []).append(dict(row))
    return result
