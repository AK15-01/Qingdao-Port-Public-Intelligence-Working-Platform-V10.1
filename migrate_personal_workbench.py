from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sqlite3

from evidence_repair import write_evidence_repair_report
from platform_db import connect, initialize_database


ROOT = Path(__file__).resolve().parent


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    source_connection = sqlite3.connect(str(source))
    target_connection = sqlite3.connect(str(temporary))
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    temporary.replace(target)


def migrate(db_path: Path, output_dir: Path) -> dict[str, object]:
    db_path = db_path.resolve()
    if not db_path.exists():
        initialize_database(db_path)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "backups" / "migrations" / f"{db_path.stem}-{stamp}.db"
    _backup_sqlite(db_path, backup)

    # Schema creation and data convergence are intentionally idempotent.
    initialize_database(db_path)
    initialize_database(db_path)
    with connect(db_path) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "sources",
                "documents",
                "events",
                "event_evidence",
                "event_reviews",
                "reports",
                "operation_runs",
                "technical_logs",
            )
        }
        states = {
            str(row["record_state"]): int(row["count"])
            for row in connection.execute(
                """SELECT record_state,COUNT(*) AS count FROM documents
                GROUP BY record_state"""
            ).fetchall()
        }
        workspace_rows = connection.execute("SELECT workspace_id FROM workspaces").fetchall()
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_reports: list[str] = []
    for row in workspace_rows:
        workspace_id = str(row["workspace_id"])
        path = output_dir / f"evidence_repair_{workspace_id}.md"
        write_evidence_repair_report(db_path, path, workspace_id=workspace_id)
        evidence_reports.append(str(path))
    return {
        "database": str(db_path),
        "backup": str(backup),
        "quick_check": quick_check,
        "counts": counts,
        "document_states": states,
        "evidence_reports": evidence_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="安全、可重复地迁移个人工作台数据模型并生成证据修复报告。"
    )
    parser.add_argument("--db", default=str(ROOT / "data" / "portscope.db"))
    parser.add_argument(
        "--output",
        default=str(ROOT / "output" / "personal_workbench_migration"),
    )
    args = parser.parse_args()
    result = migrate(Path(args.db), Path(args.output))
    print(f"数据库完整性：{result['quick_check']}")
    print(f"迁移前备份：{result['backup']}")
    print(f"表计数：{result['counts']}")
    print(f"文档状态：{result['document_states']}")
    print(f"证据修复报告：{result['evidence_reports']}")
    return 0 if result["quick_check"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
