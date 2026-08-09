from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import difflib
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from platform_db import content_hash, new_id, now_iso, transaction
from evidence_binding import invalidate_superseded_document_evidence


@dataclass(frozen=True)
class StoredDocument:
    document_id: str
    disposition: str
    version: int
    previous_document_id: str = ""
    changes: str = ""


def archive_html(document_id: str, html: str, data_root: Path) -> Path:
    now = datetime.now().astimezone()
    target = data_root / "raw" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d") / f"{document_id}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".html.tmp")
    temporary.write_text(html or "", encoding="utf-8")
    temporary.replace(target)
    return target


def archive_binary(document_id: str, payload: bytes, data_root: Path, suffix: str = ".pdf") -> Path:
    now = datetime.now().astimezone()
    safe_suffix = suffix if suffix.startswith(".") and suffix[1:].isalnum() else ".bin"
    target = data_root / "raw" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d") / f"{document_id}{safe_suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(bytes(payload))
    temporary.replace(target)
    return target


def summarize_changes(previous: str, current: str, max_lines: int = 12) -> str:
    diff = difflib.unified_diff((previous or "").splitlines(), (current or "").splitlines(), lineterm="")
    return "\n".join(list(diff)[:max_lines])


def store_document(
    values: Mapping[str, object],
    db_path,
    data_root: Path,
    chunks: Sequence[Mapping[str, object]] = (),
) -> StoredDocument:
    workspace_id = str(values.get("workspace_id") or "")
    canonical_url = str(values.get("canonical_url") or values.get("original_url") or "")
    cleaned_text = str(values.get("cleaned_text") or "")
    document_format = str(values.get("document_format") or "html").lower()
    raw_bytes = bytes(values.get("raw_bytes") or b"")
    file_sha256 = str(values.get("file_sha256") or (hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else ""))
    digest = str(values.get("content_hash") or (file_sha256 if document_format == "pdf" and file_sha256 else content_hash(cleaned_text)))
    extraction_status = str(values.get("extraction_status") or "成功")
    default_quality_ok = extraction_status in {"成功", "提取成功"}
    quality_status = str(
        values.get("quality_status") or ("合格" if default_quality_ok else "未检查")
    )
    report_quality_eligible = bool(
        values.get("report_quality_eligible", default_quality_ok and quality_status == "合格")
    )
    processing_allowed = bool(
        values.get(
            "processing_allowed",
            default_quality_ok and quality_status in {"合格", "报告门禁未通过"},
        )
    )
    record_state = str(
        values.get("record_state")
        or (
            "active"
            if default_quality_ok and processing_allowed
            else "quarantined"
        )
    )
    quarantine_reason = str(
        values.get("quarantine_reason")
        or (
            ""
            if record_state == "active"
            else quality_status or extraction_status or "正文质量门禁未通过"
        )
    )
    with transaction(db_path) as connection:
        same_hash = connection.execute(
            "SELECT document_id, document_version FROM documents WHERE workspace_id=? AND content_hash=? AND is_current=1 LIMIT 1",
            (workspace_id, digest),
        ).fetchone()
        if same_hash:
            return StoredDocument(str(same_hash["document_id"]), "duplicate_content", int(same_hash["document_version"]))
        previous = connection.execute(
            "SELECT * FROM documents WHERE workspace_id=? AND canonical_url=? AND is_current=1 ORDER BY document_version DESC LIMIT 1",
            (workspace_id, canonical_url),
        ).fetchone()
        document_id = new_id("DOC")
        version = int(previous["document_version"]) + 1 if previous else 1
        previous_id = str(previous["document_id"]) if previous else ""
        raw_html = str(values.get("raw_html") or "")
        if document_format == "pdf" and raw_bytes:
            raw_file_path = archive_binary(document_id, raw_bytes, data_root, ".pdf")
            raw_html_path = (
                archive_html(document_id, raw_html, data_root)
                if raw_html
                else raw_file_path
            )
        else:
            raw_file_path = archive_html(document_id, raw_html, data_root)
            raw_html_path = raw_file_path
        if previous:
            connection.execute("UPDATE documents SET is_current=0, updated_at=? WHERE document_id=?", (now_iso(), previous_id))
            connection.execute("DELETE FROM document_chunks_fts WHERE document_id=?", (previous_id,))
            invalidate_superseded_document_evidence(connection, previous_id)
        timestamp = now_iso()
        connection.execute(
            """INSERT INTO documents(document_id,workspace_id,source_id,canonical_url,original_url,title,publisher,
            published_at,fetched_at,raw_html_path,raw_file_path,document_format,mime_type,source_file_url,
            file_size_bytes,file_sha256,http_etag,http_last_modified,last_checked_at,
            last_content_changed_at,last_http_status,cleaned_text,content_hash,extraction_status,extraction_quality,
            ai_status,quality_status,quality_metrics_json,report_quality_eligible,http_status,document_version,
            previous_document_id,is_current,review_status,record_state,quarantine_reason,
            quarantined_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (document_id, workspace_id, values.get("source_id", ""), canonical_url, values.get("original_url", ""),
             values.get("title", ""), values.get("publisher", ""), values.get("published_at", ""),
             values.get("fetched_at", timestamp), str(raw_html_path), str(raw_file_path), document_format,
             values.get("mime_type", "application/pdf" if document_format == "pdf" else "text/html"),
             values.get("source_file_url", ""), int(values.get("file_size_bytes") or len(raw_bytes)),
             file_sha256, values.get("http_etag", ""), values.get("http_last_modified", ""),
             values.get("last_checked_at", timestamp), values.get("last_content_changed_at", timestamp),
             int(values.get("last_http_status") or values.get("http_status") or 0),
             cleaned_text, digest, extraction_status,
             values.get("extraction_quality", ""), values.get("ai_status", "待AI处理"),
             quality_status, values.get("quality_metrics_json", "{}"),
             int(report_quality_eligible), int(values.get("http_status") or 0), version, previous_id, 1,
             values.get("review_status", "待审核" if record_state == "active" else "已隔离"),
             record_state, quarantine_reason, timestamp if record_state != "active" else "",
             timestamp, timestamp),
        )
        for chunk in chunks if record_state == "active" else ():
            row = dict(chunk)
            row.update({"document_id": document_id, "workspace_id": workspace_id})
            connection.execute(
                """INSERT INTO document_chunks(chunk_id,workspace_id,document_id,event_id,chunk_index,chunk_text,
                token_or_character_count,metadata_json,embedding_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (row["chunk_id"], workspace_id, document_id, row.get("event_id", ""), row["chunk_index"], row["chunk_text"],
                 row["token_or_character_count"], row.get("metadata_json", "{}"), row.get("embedding_status", "待向量化"), row.get("created_at", timestamp)),
            )
            connection.execute(
                "INSERT INTO document_chunks_fts(chunk_id,workspace_id,document_id,event_id,chunk_text,metadata_json) VALUES(?,?,?,?,?,?)",
                (row["chunk_id"], workspace_id, document_id, row.get("event_id", ""), row["chunk_text"], row.get("metadata_json", "{}")),
            )
    changes = summarize_changes(str(previous["cleaned_text"]) if previous else "", cleaned_text) if previous else ""
    return StoredDocument(document_id, "updated" if previous else "new", version, previous_id, changes)
