from __future__ import annotations

from pathlib import Path

from crawler import CrawlManager
from platform_db import initialize_database, list_sources
from rag.indexer import KnowledgeIndexer
from workspace_store import data_root, database_path, get_active_workspace


def main() -> int:
    db_path = database_path()
    root = data_root()
    initialize_database(db_path)
    workspace = get_active_workspace(db_path)
    if workspace is None:
        print("[PortScope] 尚未创建工作空间，请先运行 run.bat 完成首次设置。")
        return 2
    workspace_id = str(workspace["workspace_id"])
    runnable_sources = [
        source for source in list_sources(workspace_id, db_path, enabled_only=True)
        if bool(source.get("crawl_allowed"))
    ]
    if not runnable_sources:
        print("[PortScope] 尚未启用可自动采集的公开数据源。")
        print("[PortScope] 请先运行 run.bat，在“更新公开数据”页点击“一键初始化推荐来源”。")
        return 2
    print("[PortScope] 正在执行白名单公开数据增量更新。可按 Ctrl+C 安全停止；已提交事务不会损坏。")
    print("[PortScope] 首次向量化可能下载 BAAI/bge-small-zh-v1.5；模型下载后仅在本地运行。")
    try:
        indexer = KnowledgeIndexer(db_path, Path(root) / "chroma")
        manager = CrawlManager(db_path, Path(root), vector_indexer=indexer, ai_enabled=bool(workspace.get("ai_enabled", False)))
        result = manager.run(workspace_id, progress=lambda phase, _: print(f"[PortScope] {phase}"))
    except KeyboardInterrupt:
        print("[PortScope] 用户已中止本次更新。")
        return 130
    print(
        f"[PortScope] {result.status}｜发现 {result.discovered_count}｜获取 {result.fetched_count}｜"
        f"新增文档 {result.new_document_count}｜更新 {result.updated_document_count}｜"
        f"跳过 {result.skipped_count}｜失败 {result.failed_count}｜新增事件 {result.new_event_count}"
    )
    if result.errors:
        print("[PortScope] 需人工处理：")
        for error in result.errors:
            print(f"  - {error}")
    return 0 if result.status in {"完成", "部分完成"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
