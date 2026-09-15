from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import CancelToken, CrawlManager  # noqa: E402
from crawler.content_fetcher import ContentFetcher  # noqa: E402
from deepseek_service import load_settings  # noqa: E402
from event_pipeline import reprocess_pending_ai  # noqa: E402
from operation_store import (  # noqa: E402
    add_technical_log,
    claim_operation,
    finish_operation,
    get_operation,
    operation_cancel_requested,
    update_operation_progress,
)
from platform_db import connect  # noqa: E402


def _crawl(operation: dict[str, object], db_path: Path, data_root: Path) -> None:
    operation_id = str(operation["operation_run_id"])
    workspace_id = str(operation["workspace_id"])
    metadata = dict(operation.get("metadata") or {})
    cancel = CancelToken(
        lambda: operation_cancel_requested(operation_id, db_path)
    )

    def progress(phase, payload):
        update_operation_progress(
            operation_id,
            db_path,
            current_stage=str(phase),
            current_item=str(
                payload.get("current_item")
                or payload.get("title")
                or ""
            ),
            completed_items=int(payload.get("completed_items") or 0),
            total_items=int(payload.get("total_items") or 0),
            success_count=int(payload.get("success_count") or 0),
            isolated_count=int(payload.get("isolated_count") or 0),
            skipped_count=int(payload.get("skipped_count") or 0),
            failed_count=int(payload.get("failed_count") or 0),
            metadata={"live_progress": dict(payload)},
        )

    auto_ai = bool(metadata.get("auto_ai", False))
    settings = load_settings()
    manager = CrawlManager(
        db_path,
        data_root,
        fetcher=ContentFetcher(timeout=(5, 15), retries=1),
        vector_indexer=None,
        ai_enabled=bool(auto_ai and settings.configured),
        ai_settings=(
            replace(
                settings,
                timeout=min(float(settings.timeout), 30.0),
                max_retries=min(int(settings.max_retries), 1),
            )
            if auto_ai and settings.configured
            else None
        ),
    )
    manager.run(
        workspace_id,
        source_ids=list(metadata.get("source_ids") or []) or None,
        max_articles=int(metadata.get("max_articles") or 3),
        start_date=str(metadata.get("start_date") or ""),
        end_date=str(metadata.get("end_date") or ""),
        refresh_mode=str(metadata.get("refresh_mode") or "new_only"),
        source_time_budget_seconds=float(
            metadata.get("source_time_budget_seconds") or 120
        ),
        source_cooldown_seconds=float(
            metadata.get("source_cooldown_seconds") or 120
        ),
        cancel=cancel,
        progress=progress,
        operation_run_id=operation_id,
    )


def _ai_reprocess(operation: dict[str, object], db_path: Path) -> None:
    operation_id = str(operation["operation_run_id"])
    workspace_id = str(operation["workspace_id"])
    metadata = dict(operation.get("metadata") or {})
    settings = load_settings()
    bounded_settings = replace(
        settings,
        timeout=min(float(settings.timeout), 30.0),
        max_retries=min(int(settings.max_retries), 1),
    )

    def progress(completed, total, document, result, stage):
        update_operation_progress(
            operation_id,
            db_path,
            current_stage=str(stage),
            current_item=str(document.get("title") or document.get("document_id") or ""),
            completed_items=int(completed),
            total_items=int(total),
            success_count=int(result.get("success") or 0),
            skipped_count=int(result.get("skipped") or 0),
            failed_count=int(result.get("failed") or 0),
            metadata={
                "document_id": str(document.get("document_id") or ""),
                "models": list(result.get("models") or []),
            },
        )

    reprocess_pending_ai(
        workspace_id,
        db_path,
        limit=max(1, min(int(metadata.get("limit") or 2), 5)),
        settings=bounded_settings,
        operation_run_id=operation_id,
        cancel_check=lambda: operation_cancel_requested(operation_id, db_path),
        progress=progress,
        time_budget_seconds=float(metadata.get("time_budget_seconds") or 120),
    )


def _index(operation: dict[str, object], db_path: Path, data_root: Path) -> None:
    from rag.indexer import KnowledgeIndexer

    operation_id = str(operation["operation_run_id"])
    workspace_id = str(operation["workspace_id"])
    metadata = dict(operation.get("metadata") or {})
    limit = max(1, min(int(metadata.get("limit") or 5), 100))
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT document_id,title FROM documents WHERE workspace_id=?
            AND is_current=1 AND record_state='active'
            AND extraction_status IN ('成功','提取成功')
            ORDER BY updated_at DESC LIMIT ?""",
            (workspace_id, limit),
        ).fetchall()
    indexer = KnowledgeIndexer(db_path, data_root / "chroma")
    success = failed = 0
    errors: list[str] = []
    for index, row in enumerate(rows, start=1):
        if operation_cancel_requested(operation_id, db_path):
            finish_operation(
                operation_id,
                db_path,
                status="cancelled",
                result_summary=f"索引已取消；成功{success}，失败{failed}",
                counts={"failed_count": failed},
            )
            return
        try:
            indexer.delete_document(str(row["document_id"]))
            indexer.rechunk_document(str(row["document_id"]))
            indexer.index_document(str(row["document_id"]))
            success += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{row['document_id']}：{type(exc).__name__}")
        update_operation_progress(
            operation_id,
            db_path,
            current_stage="更新向量索引",
            current_item=str(row["title"] or row["document_id"]),
            completed_items=index,
            total_items=len(rows),
            success_count=success,
            failed_count=failed,
        )
    finish_operation(
        operation_id,
        db_path,
        status=(
            "succeeded"
            if failed == 0
            else ("partially_succeeded" if success else "failed")
        ),
        result_summary=f"索引完成：成功{success}，失败{failed}",
        counts={"failed_count": failed},
        error_summary="\n".join(errors),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="PortScope本地持久化任务Worker")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    db_path = args.db.resolve()
    data_root = args.data_root.resolve()
    operation = claim_operation(
        args.operation_id,
        db_path,
        worker_pid=os.getpid(),
    )
    if str(operation.get("status")) == "cancelled":
        return 0
    workspace_id = str(operation["workspace_id"])
    add_technical_log(
        workspace_id,
        db_path,
        "本地Worker已领取任务。",
        operation_run_id=args.operation_id,
        level="info",
        component="task_worker",
        details={"operation_type": operation["operation_type"]},
    )
    try:
        operation_type = str(operation["operation_type"])
        if operation_type == "crawl":
            _crawl(operation, db_path, data_root)
        elif operation_type == "ai_reprocess":
            _ai_reprocess(operation, db_path)
        elif operation_type == "index_rebuild":
            _index(operation, db_path, data_root)
        else:
            raise ValueError(f"Worker不支持任务类型：{operation_type}")
    except Exception as exc:
        current = get_operation(args.operation_id, db_path) or {}
        if str(current.get("status")) == "running":
            finish_operation(
                args.operation_id,
                db_path,
                status="failed",
                result_summary="任务异常结束",
                error_summary=f"{type(exc).__name__}: {str(exc)[:1000]}",
                counts={"failed_count": 1},
            )
        add_technical_log(
            workspace_id,
            db_path,
            f"任务异常：{type(exc).__name__}",
            operation_run_id=args.operation_id,
            level="error",
            component="task_worker",
            details={"safe_error": str(exc)[:1000]},
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
