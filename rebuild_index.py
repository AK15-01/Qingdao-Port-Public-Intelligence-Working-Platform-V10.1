from __future__ import annotations

import argparse
from pathlib import Path

from platform_db import initialize_database
from rag.indexer import KnowledgeIndexer
from workspace_store import data_root, database_path, get_active_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description="重建当前或指定工作空间的分块、FTS5和Chroma索引。")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--chroma", type=Path)
    args = parser.parse_args()
    db_path = args.db or database_path()
    root = data_root()
    initialize_database(db_path)
    workspace = get_active_workspace(db_path) if not args.workspace_id else {"workspace_id": args.workspace_id}
    if workspace is None:
        print("[PortScope] 尚未创建工作空间。")
        return 2
    chroma_path = args.chroma or (Path(root) / "chroma")
    print("[PortScope] 正在根据 SQLite 当前文档重建 Chroma 索引。首次运行可能下载本地中文 Embedding 模型。")
    try:
        result = KnowledgeIndexer(db_path, chroma_path).rebuild(str(workspace["workspace_id"]))
    except Exception as exc:
        print(f"[PortScope] 重建失败：{exc}")
        return 1
    print(f"[PortScope] 完成：文档 {result['documents']}，成功 {result['success']}，失败 {result['failed']}，Chunk {result['chunks']}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
