from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from document_chunker import build_chunk_rows
from document_processor import store_document
from document_quality import assess_document
from event_pipeline import create_event_for_document
from file_intake import parse_uploaded_public_file
from operation_store import finish_operation, start_operation
from platform_db import now_iso, upsert_source
from web_extractor import fetch_url


def _manual_source_id(workspace_id: str, domain: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}:{domain}".encode("utf-8")).hexdigest()[:12].upper()
    return f"SRC-MANUAL-{digest}"


def import_public_material(
    workspace_id: str,
    db_path,
    data_root: Path,
    *,
    source_url: str = "",
    title: str = "",
    published_at: str = "",
    source_name: str = "",
    pasted_text: str = "",
    filename: str = "",
    file_bytes: bytes = b"",
    fetch_single_url: bool = False,
    ui_session_id: str = "",
) -> dict[str, object]:
    operation_id = start_operation(
        workspace_id,
        "file_import",
        db_path,
        ui_session_id=ui_session_id,
        input_summary=(
            f"单条URL：{urlparse(source_url).hostname or '无'}"
            if fetch_single_url
            else f"本地公开资料：{filename or title or '手工粘贴'}"
        ),
    )
    try:
        fetched_at = now_iso()
        raw_html = ""
        note = ""
        if fetch_single_url:
            extraction = fetch_url(source_url)
            if not extraction.ok:
                raise ValueError(
                    f"{extraction.fetch_status}：{extraction.quality_note or '未提取到可用正文'}"
                )
            title = title or extraction.title
            published_at = published_at or extraction.published_date
            source_name = source_name or extraction.source_name
            pasted_text = extraction.text
            source_url = extraction.canonical_url or extraction.final_url or source_url
            fetched_at = extraction.fetched_at or fetched_at
            note = extraction.quality_note
        elif file_bytes:
            parsed = parse_uploaded_public_file(filename, file_bytes)
            title = title or parsed.title
            published_at = published_at or parsed.published_date
            source_name = source_name or parsed.source_name
            pasted_text = pasted_text or parsed.text
            note = parsed.note
        text = str(pasted_text or "").strip()
        if not text:
            raise ValueError("没有可导入的正文")
        title = str(title or Path(filename).stem or "公开资料待核对").strip()[:300]
        source_name = str(source_name or urlparse(source_url).hostname or "本地公开资料").strip()
        parsed_url = urlparse(source_url)
        domain = str(parsed_url.hostname or "local-public-import.invalid").lower()
        source_id = _manual_source_id(workspace_id, domain)
        upsert_source(
            {
                "source_id": source_id,
                "workspace_id": workspace_id,
                "source_name": source_name,
                "organization": source_name,
                "domain": domain,
                "homepage_url": f"{parsed_url.scheme}://{domain}" if parsed_url.scheme in {"http", "https"} else "",
                "list_page_url": "",
                "source_type": "其他",
                "enabled": False,
                "crawl_allowed": False,
                "robots_status": "未检查",
                "terms_status": "未检查",
                "commercial_reuse_status": "未明确",
                "internal_collection_allowed": True,
                "internal_analysis_allowed": True,
                "customer_summary_allowed": True,
                "short_quote_allowed": True,
                "fulltext_redistribution_allowed": False,
                "raw_data_resale_allowed": False,
                "permission_basis": "用户主动导入的单份公开资料；客户使用前逐条核对来源与证据",
            },
            db_path,
        )
        quality = assess_document(
            title=title,
            text=text,
            published_at=published_at,
            raw_html=raw_html,
            site_names=[source_name],
        )
        canonical_url = (
            source_url
            if parsed_url.scheme in {"http", "https"}
            else "urn:local-public-import:" + hashlib.sha256(
                (filename + text).encode("utf-8")
            ).hexdigest()
        )
        values = {
            "workspace_id": workspace_id,
            "source_id": source_id,
            "canonical_url": canonical_url,
            "original_url": source_url,
            "title": title,
            "publisher": source_name,
            "published_at": published_at,
            "fetched_at": fetched_at,
            "raw_html": raw_html,
            "cleaned_text": text,
            "extraction_status": "成功",
            "extraction_quality": note or quality.note,
            "quality_status": quality.status,
            "quality_metrics_json": json.dumps(quality.metrics, ensure_ascii=False),
            "report_quality_eligible": quality.report_allowed,
            "processing_allowed": quality.processing_allowed,
            "ai_status": "待AI处理",
            "http_status": 200 if fetch_single_url else 0,
        }
        chunks = (
            build_chunk_rows(
                text,
                {
                    "workspace_id": workspace_id,
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_url": source_url,
                },
            )
            if quality.processing_allowed
            else []
        )
        stored = store_document(values, db_path, data_root, chunks)
        event_id = ""
        if stored.disposition not in {"duplicate_content"} and quality.processing_allowed:
            event_values = {**values, "document_id": stored.document_id}
            event_id, _ = create_event_for_document(
                stored.document_id,
                event_values,
                {
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_type": "其他",
                    "category_hint": "",
                },
                workspace_id,
                db_path,
                ai_enabled=False,
            )
        status = "partially_succeeded" if not quality.report_allowed else "succeeded"
        finish_operation(
            operation_id,
            db_path,
            status=status,
            result_summary=(
                f"已导入文档；正文质量：{quality.status}；"
                + (f"生成待审核事件 {event_id}" if event_id else "未生成事件")
            ),
            counts={
                "documents_created": int(stored.disposition in {"new", "updated"}),
                "events_created": int(bool(event_id)),
                "skipped_count": int(stored.disposition == "duplicate_content"),
                "warning_count": len(quality.issues),
            },
            metadata={
                "document_ids": [stored.document_id],
                "event_ids": [event_id] if event_id else [],
                "quality_status": quality.status,
            },
        )
        return {
            "operation_run_id": operation_id,
            "document_id": stored.document_id,
            "event_id": event_id,
            "disposition": stored.disposition,
            "quality_status": quality.status,
            "quality_issues": list(quality.issues),
        }
    except Exception as exc:
        finish_operation(
            operation_id,
            db_path,
            status="failed",
            result_summary="单份公开资料未导入",
            error_summary=str(exc),
            counts={"failed_count": 1},
        )
        raise
