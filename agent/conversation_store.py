from __future__ import annotations

import json
import re
from typing import Mapping, Optional

from platform_db import connect, initialize_database, new_id, now_iso, transaction


SECRET_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _redact(value: object) -> object:
    if isinstance(value, str):
        return SECRET_PATTERN.sub("sk-****REDACTED", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): ("***" if "key" in str(key).lower() or "secret" in str(key).lower() else _redact(item))
            for key, item in value.items()
        }
    return value


def create_conversation(workspace_id: str, db_path, title: str = "新对话") -> str:
    initialize_database(db_path)
    conversation_id = new_id("CONV")
    timestamp = now_iso()
    with transaction(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_conversations(conversation_id,workspace_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
            (conversation_id, workspace_id, str(title or "新对话")[:200], timestamp, timestamp),
        )
    return conversation_id


def get_or_create_conversation(workspace_id: str, db_path) -> str:
    initialize_database(db_path)
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT conversation_id FROM agent_conversations WHERE workspace_id=? ORDER BY updated_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
    return str(row[0]) if row else create_conversation(workspace_id, db_path)


def append_message(
    conversation_id: str,
    workspace_id: str,
    role: str,
    content: str,
    db_path,
    *,
    metadata: Optional[Mapping[str, object]] = None,
    tool_name: str = "",
) -> str:
    if role not in {"user", "assistant", "tool", "system"}:
        raise ValueError("不允许的对话角色")
    clean_content = str(_redact(str(content or "")))
    clean_metadata = _redact(dict(metadata or {}))
    message_id = new_id("MSG")
    timestamp = now_iso()
    with transaction(db_path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM agent_conversations WHERE conversation_id=? AND workspace_id=?",
            (conversation_id, workspace_id),
        ).fetchone()
        if not exists:
            raise ValueError("对话不属于当前工作空间")
        connection.execute(
            """INSERT INTO agent_messages(message_id,conversation_id,workspace_id,role,content,metadata_json,tool_name,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (message_id, conversation_id, workspace_id, role, clean_content,
             json.dumps(clean_metadata, ensure_ascii=False, default=str), tool_name, timestamp),
        )
        connection.execute(
            "UPDATE agent_conversations SET updated_at=? WHERE conversation_id=?",
            (timestamp, conversation_id),
        )
    return message_id


def load_messages(conversation_id: str, workspace_id: str, db_path, limit: int = 100) -> list[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT * FROM agent_messages WHERE conversation_id=? AND workspace_id=?
            ORDER BY created_at,rowid LIMIT ?""",
            (conversation_id, workspace_id, max(1, min(int(limit), 500))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        result.append(item)
    return result


def list_conversations(workspace_id: str, db_path, limit: int = 30) -> list[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM agent_conversations WHERE workspace_id=? ORDER BY updated_at DESC LIMIT ?",
            (workspace_id, max(1, min(int(limit), 100))),
        ).fetchall()]
