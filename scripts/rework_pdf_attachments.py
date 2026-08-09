from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import CrawlManager  # noqa: E402
from crawler.crawl_manager import list_pdf_attachment_rework_candidates  # noqa: E402
from deepseek_service import load_settings  # noqa: E402
from platform_db import (  # noqa: E402
    backup_database_before_source_maintenance,
    connect,
    initialize_database,
    list_sources,
)
from rag.indexer import KnowledgeIndexer  # noqa: E402
from workspace_store import data_root, database_path, get_active_workspace  # noqa: E402


MARITIME_WARNING_SOURCE = "山东海事局海上风险预警"


def _source_id(workspace_id: str, db_path: Path, explicit: str) -> str:
    if explicit:
        return explicit
    match = next(
        (
            item
            for item in list_sources(workspace_id, db_path)
            if str(item["source_name"]) == MARITIME_WARNING_SOURCE
        ),
        None,
    )
    return str(match["source_id"]) if match else ""


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="仅返工当前隔离、HTML正文仅含PDF附件名的文档；默认只预览"
    )
    parser.add_argument("--db", type=Path, default=database_path())
    parser.add_argument("--data-root", type=Path, default=data_root())
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--source-id", default="")
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--skip-vector-index", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db.resolve()
    root = args.data_root.resolve()
    initialize_database(db_path)
    workspace = get_active_workspace(db_path)
    if not workspace:
        print(json.dumps({"ok": False, "error": "未找到活动工作空间"}, ensure_ascii=False))
        return 2
    workspace_id = str(args.workspace_id or workspace["workspace_id"])
    source_id = _source_id(workspace_id, db_path, str(args.source_id or ""))
    if not source_id:
        print(json.dumps({"ok": False, "error": "未找到山东海事局风险预警来源"}, ensure_ascii=False))
        return 2
    limit = max(1, min(int(args.limit), 20))
    candidates = list_pdf_attachment_rework_candidates(
        workspace_id,
        db_path,
        document_ids=list(args.document_id) or None,
        source_ids=[source_id],
        limit=limit,
    )
    preview = [
        {
            key: item.get(key)
            for key in (
                "document_id",
                "source_id",
                "source_name",
                "canonical_url",
                "title",
                "published_at",
                "quality_status",
                "document_version",
            )
        }
        for item in candidates
    ]
    if not args.apply:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "candidate_count": len(preview),
                    "candidates": preview,
                    "next_step": (
                        "确认后增加 --apply --confirm PDF_REWORK；"
                        "每次最多20篇，建议先用2篇验收。"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.confirm != "PDF_REWORK":
        print(
            json.dumps(
                {"ok": False, "error": "执行返工必须显式传入 --confirm PDF_REWORK"},
                ensure_ascii=False,
            )
        )
        return 3
    if not candidates:
        print(json.dumps({"ok": False, "error": "没有符合安全返工条件的文档"}, ensure_ascii=False))
        return 4

    backup_path = backup_database_before_source_maintenance(db_path)
    settings = load_settings()
    ai_enabled = bool(workspace.get("ai_enabled")) and settings.configured
    indexer = None
    if not args.skip_vector_index:
        indexer = KnowledgeIndexer(db_path, root / "chroma")
    manager = CrawlManager(
        db_path,
        root,
        vector_indexer=indexer,
        ai_enabled=ai_enabled,
        ai_settings=settings,
    )
    result = manager.run(
        workspace_id,
        source_ids=[source_id],
        max_articles=limit,
        rework_document_ids=[str(item["document_id"]) for item in candidates],
    )
    with connect(db_path) as connection:
        documents = [
            dict(row)
            for row in connection.execute(
                f"""SELECT document_id,canonical_url,source_file_url,document_format,
                file_size_bytes,file_sha256,length(cleaned_text) AS text_length,
                quality_status,record_state,document_version,previous_document_id
                FROM documents WHERE document_id IN (
                {','.join('?' for _ in result.document_ids)}
                ) ORDER BY created_at""",
                result.document_ids,
            ).fetchall()
        ] if result.document_ids else []
        source_run = connection.execute(
            """SELECT * FROM crawl_source_runs
            WHERE crawl_run_id=? AND source_id=?""",
            (result.crawl_run_id, source_id),
        ).fetchone()
    payload = {
        "ok": result.status in {"完成", "部分完成"},
        "dry_run": False,
        "backup_path": backup_path,
        "ai_enabled": ai_enabled,
        "crawl_run_id": result.crawl_run_id,
        "status": result.status,
        "pdf_discovered_count": result.pdf_discovered_count,
        "pdf_success_count": result.pdf_success_count,
        "pdf_rejected_count": result.pdf_rejected_count,
        "qualified_document_count": result.qualified_document_count,
        "new_document_count": result.new_document_count,
        "updated_document_count": result.updated_document_count,
        "new_event_count": result.new_event_count,
        "documents": documents,
        "source_run": dict(source_run) if source_run else {},
        "errors": result.errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
