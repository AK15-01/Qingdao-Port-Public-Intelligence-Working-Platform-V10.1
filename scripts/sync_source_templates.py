from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_db import (  # noqa: E402
    backup_database_before_source_maintenance,
    initialize_recommended_sources,
    now_iso,
    transaction,
)
from workspace_store import database_path, get_active_workspace  # noqa: E402


UNSTABLE_SHANDONG_PORT_SOURCE = "山东省港口集团要闻"


def zero_yield_source_plan(workspace_id: str, db_path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT s.source_id,s.source_name,s.enabled,s.crawl_allowed,
            s.operational_status,s.health_status,
            COUNT(d.document_id) AS current_documents,
            SUM(CASE WHEN d.record_state='active' AND d.quality_status='合格'
                THEN 1 ELSE 0 END) AS qualified_documents,
            SUM(CASE WHEN d.record_state='quarantined' THEN 1 ELSE 0 END)
                AS quarantined_documents
            FROM sources s LEFT JOIN documents d ON d.source_id=s.source_id
            AND d.workspace_id=s.workspace_id AND d.is_current=1
            WHERE s.workspace_id=? AND s.source_name=?
            GROUP BY s.source_id""",
            (workspace_id, UNSTABLE_SHANDONG_PORT_SOURCE),
        ).fetchall()
    finally:
        connection.close()
    plans: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        if (
            int(item.get("current_documents") or 0) > 0
            and int(item.get("qualified_documents") or 0) == 0
            and (
                int(item.get("enabled") or 0) != 0
                or int(item.get("crawl_allowed") or 0) != 0
                or str(item.get("operational_status") or "") != "needs_adapter"
                or str(item.get("health_status") or "") != "需要适配器"
            )
        ):
            plans.append(
                {
                    **item,
                    "new_enabled": 0,
                    "new_crawl_allowed": 0,
                    "new_operational_status": "needs_adapter",
                    "new_health_status": "需要适配器",
                    "reason": (
                        "既有真实采集没有产生合格正文；暂停自动批量采集，"
                        "保留人工URL、HTML或文件导入。"
                    ),
                }
            )
    return plans


def apply_zero_yield_source_plan(
    workspace_id: str,
    db_path: Path,
    plans: list[dict[str, object]],
) -> int:
    if not plans:
        return 0
    timestamp = now_iso()
    with transaction(db_path) as connection:
        for plan in plans:
            connection.execute(
                """UPDATE sources SET enabled=0,crawl_allowed=0,
                operational_status='needs_adapter',health_status='需要适配器',
                last_error=?,updated_at=? WHERE workspace_id=? AND source_id=?""",
                (
                    str(plan["reason"]),
                    timestamp,
                    workspace_id,
                    str(plan["source_id"]),
                ),
            )
    return len(plans)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="保守同步推荐来源模板；默认dry-run，不覆盖用户管理字段"
    )
    parser.add_argument("--db", type=Path, default=database_path())
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--degrade-zero-yield-sources",
        action="store_true",
        help="把有真实失败样本且0篇合格正文的山东港口来源降级并停用自动采集",
    )
    args = parser.parse_args(argv)
    db_path = args.db.resolve()
    workspace_id = str(args.workspace_id or "")
    if not workspace_id:
        workspace = get_active_workspace(db_path)
        if not workspace:
            print(json.dumps({"ok": False, "error": "未找到活动工作空间"}, ensure_ascii=False))
            return 2
        workspace_id = str(workspace["workspace_id"])

    dry_run = not args.apply
    result = initialize_recommended_sources(
        workspace_id,
        db_path,
        dry_run=dry_run,
    )
    degradation_plan = zero_yield_source_plan(workspace_id, db_path)
    degradation_backup = ""
    degraded = 0
    if args.apply and args.degrade_zero_yield_sources and degradation_plan:
        if not str(result.get("backup_path") or ""):
            degradation_backup = backup_database_before_source_maintenance(db_path)
        degraded = apply_zero_yield_source_plan(
            workspace_id,
            db_path,
            degradation_plan,
        )
    payload = {
        "ok": True,
        "dry_run": dry_run,
        "database": str(db_path),
        "workspace_id": workspace_id,
        "template_result": result,
        "zero_yield_source_plan": degradation_plan,
        "zero_yield_sources_degraded": degraded,
        "backup_path": str(result.get("backup_path") or degradation_backup),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
