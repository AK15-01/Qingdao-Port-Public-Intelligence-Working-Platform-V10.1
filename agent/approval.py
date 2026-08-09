from __future__ import annotations

import json
from typing import Mapping

from platform_db import connect, initialize_database, new_id, now_iso, transaction


def request_approval(
    workspace_id: str,
    conversation_id: str,
    tool_name: str,
    arguments: Mapping[str, object],
    summary: Mapping[str, object],
    db_path,
) -> str:
    initialize_database(db_path)
    approval_id = new_id("APR")
    with transaction(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_approvals(
            approval_id,workspace_id,conversation_id,tool_name,arguments_json,summary_json,status,created_at,decided_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (approval_id, workspace_id, conversation_id, tool_name,
             json.dumps(dict(arguments), ensure_ascii=False, default=str),
             json.dumps(dict(summary), ensure_ascii=False, default=str), "待确认", now_iso(), ""),
        )
    return approval_id


def decide_approval(approval_id: str, workspace_id: str, approved: bool, db_path) -> dict[str, object]:
    initialize_database(db_path)
    with transaction(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM agent_approvals WHERE approval_id=? AND workspace_id=?",
            (approval_id, workspace_id),
        ).fetchone()
        if not row:
            raise KeyError("确认请求不存在或不属于当前工作空间")
        if row["status"] != "待确认":
            raise ValueError("该确认请求已经处理")
        status = "已确认" if approved else "已取消"
        connection.execute(
            "UPDATE agent_approvals SET status=?,decided_at=? WHERE approval_id=?",
            (status, now_iso(), approval_id),
        )
    item = dict(row)
    item["status"] = status
    item["arguments"] = json.loads(item.pop("arguments_json") or "{}")
    item["summary"] = json.loads(item.pop("summary_json") or "{}")
    return item


def list_pending_approvals(workspace_id: str, db_path) -> list[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM agent_approvals WHERE workspace_id=? AND status='待确认' ORDER BY created_at",
            (workspace_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["arguments"] = json.loads(item.pop("arguments_json") or "{}")
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
        result.append(item)
    return result
