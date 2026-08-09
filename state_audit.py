from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


AUDIT_MARKER = "portscope-state-audit"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_sha256(path: Path) -> str:
    """Fingerprint logical SQLite contents, including committed WAL rows.

    Raw SQLite/WAL bytes may change after a read-only integrity check or WAL
    checkpoint even when business data did not. A deterministic SQL dump
    therefore provides a stable audit hash for the current logical database.
    """

    database = Path(path).resolve()
    digest = hashlib.sha256()
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("PRAGMA query_only=ON")
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        connection.close()
    return digest.hexdigest()


def project_version(project_root: Path) -> str:
    version_path = Path(project_root) / "VERSION"
    return version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else "unknown"


def _group_counts(connection: sqlite3.Connection, table: str, field: str) -> dict[str, int]:
    return {
        str(row[0] or "未设置"): int(row[1])
        for row in connection.execute(
            f"SELECT {field},COUNT(*) FROM {table} GROUP BY {field} ORDER BY {field}"
        ).fetchall()
    }


def collect_state_audit(db_path: Path, project_root: Path) -> dict[str, Any]:
    db = Path(db_path).resolve()
    root = Path(project_root).resolve()
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    result: dict[str, Any]
    try:
        documents = connection.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN record_state='active' THEN 1 ELSE 0 END) active,
            SUM(CASE WHEN record_state='quarantined' THEN 1 ELSE 0 END) quarantined,
            SUM(CASE WHEN record_state='active' AND ai_status='已完成' THEN 1 ELSE 0 END) ai_complete
            FROM documents"""
        ).fetchone()
        events = connection.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN record_state='active' THEN 1 ELSE 0 END) active,
            SUM(CASE WHEN record_state='quarantined' THEN 1 ELSE 0 END) quarantined,
            SUM(CASE WHEN record_state='active' AND human_verified=1
                AND reviewer_type IN ('human_user','industry_reviewer') THEN 1 ELSE 0 END) human_verified,
            SUM(CASE WHEN record_state='active' AND active_for_internal_use=1 THEN 1 ELSE 0 END) internal_eligible,
            SUM(CASE WHEN record_state='active' AND eligible_for_customer_report=1 THEN 1 ELSE 0 END) customer_eligible
            FROM events"""
        ).fetchone()
        evidence = connection.execute(
            """SELECT
            SUM(CASE WHEN x.verification_status='已验证' THEN 1 ELSE 0 END) verified,
            SUM(CASE WHEN x.verification_status!='已验证' THEN 1 ELSE 0 END) unresolved,
            COUNT(*) total
            FROM event_evidence x JOIN events e ON e.event_id=x.event_id
            WHERE e.record_state='active'"""
        ).fetchone()
        latest = connection.execute(
            """SELECT crawl_run_id,finished_at,status,new_document_count,new_event_count
            FROM crawl_runs WHERE status IN ('完成','成功','succeeded','部分完成','部分成功','partially_succeeded')
            AND finished_at!='' ORDER BY finished_at DESC LIMIT 1"""
        ).fetchone()
        enabled_sources = [
            dict(row)
            for row in connection.execute(
                """SELECT source_id,source_name,health_status,last_success_at
                FROM sources WHERE enabled=1 AND crawl_allowed=1 ORDER BY source_name"""
            ).fetchall()
        ]
        report_count = int(connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0])
        result = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database_path": "data/portscope.db",
            "code_version": project_version(root),
            "documents": {key: int(documents[key] or 0) for key in documents.keys()},
            "events": {key: int(events[key] or 0) for key in events.keys()},
            "evidence": {key: int(evidence[key] or 0) for key in evidence.keys()},
            "operation_statuses": _group_counts(connection, "operation_runs", "status"),
            "crawl_source_statuses": _group_counts(connection, "crawl_source_runs", "status"),
            "enabled_sources": enabled_sources,
            "latest_successful_crawl": dict(latest) if latest else None,
            "reports": report_count,
        }
    finally:
        connection.close()
    # Hash only after closing the reporting connection. The logical hash is
    # stable across read-only WAL checkpoints but changes with committed data.
    result["database_sha256"] = database_sha256(db)
    return result


def render_state_audit(audit: dict[str, Any]) -> str:
    marker = json.dumps(
        {
            "database_sha256": audit["database_sha256"],
            "code_version": audit["code_version"],
            "generated_at": audit["generated_at"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    docs = audit["documents"]
    events = audit["events"]
    evidence = audit["evidence"]
    latest = audit["latest_successful_crawl"]
    source_lines = "\n".join(
        f"- `{row['source_id']}` {row['source_name']}｜健康：{row['health_status'] or '未设置'}"
        f"｜最近成功：{row['last_success_at'] or '尚无'}"
        for row in audit["enabled_sources"]
    ) or "- 当前没有同时启用并允许采集的来源。"
    operation_rows = "\n".join(
        f"| {status} | {count} |" for status, count in audit["operation_statuses"].items()
    )
    crawl_rows = "\n".join(
        f"| {status} | {count} |" for status, count in audit["crawl_source_statuses"].items()
    )
    latest_text = (
        f"`{latest['crawl_run_id']}`，{latest['finished_at']}，"
        f"新增文档 {latest['new_document_count']}，新增事件 {latest['new_event_count']}"
        if latest
        else "尚无成功采集。"
    )
    return f"""<!-- {AUDIT_MARKER}:{marker} -->
# PortScope 当前真实状态审计

> 本文件由当前 SQLite 动态生成。数据库哈希或代码版本变化后，本文件即为过期快照，必须重新生成。

- 生成时间：{audit['generated_at']}
- 代码版本：`{audit['code_version']}`
- 数据库：`{audit['database_path']}`
- 数据库 SHA-256：`{audit['database_sha256']}`

## 数据状态

| 项目 | 总数 | active | quarantined |
|---|---:|---:|---:|
| 文档 | {docs['total']} | {docs['active']} | {docs['quarantined']} |
| 事件 | {events['total']} | {events['active']} | {events['quarantined']} |

- active 文档中 AI 已完成：{docs['ai_complete']}
- active 事件证据：已验证 {evidence['verified']}，未解决 {evidence['unresolved']}，共 {evidence['total']}
- 独立真人审核事件：{events['human_verified']}
- 内部使用资格：{events['internal_eligible']}
- 客户报告资格：{events['customer_eligible']}
- 历史报告记录：{audit['reports']}

## 任务状态

| operation_runs 状态 | 数量 |
|---|---:|
{operation_rows}

| crawl_source_runs 状态 | 数量 |
|---|---:|
{crawl_rows}

## 启用来源

{source_lines}

## 最新成功采集

{latest_text}

## 口径说明

- `active`不等于客户报告可用；客户报告资格继续受真人审核、证据和来源规则约束。
- 本审计不把自动或代理标注计为独立真人审核。
- QA工作区的5条未定位证据不因本文件生成而改变。
"""


def write_state_audit(db_path: Path, project_root: Path, output_path: Path) -> dict[str, Any]:
    audit = collect_state_audit(db_path, project_root)
    target = Path(output_path)
    target.write_text(render_state_audit(audit), encoding="utf-8")
    return audit


def audit_snapshot_is_stale(
    audit_path: Path,
    db_path: Path,
    project_root: Path,
) -> tuple[bool, str]:
    path = Path(audit_path)
    if not path.is_file():
        return True, "审计文件不存在"
    head = path.read_text(encoding="utf-8")[:3000]
    match = re.search(rf"<!-- {re.escape(AUDIT_MARKER)}:(\{{.*?\}}) -->", head)
    if not match:
        return True, "审计文件缺少机器可读版本标记"
    try:
        marker = json.loads(match.group(1))
    except json.JSONDecodeError:
        return True, "审计版本标记损坏"
    current_version = project_version(project_root)
    if str(marker.get("code_version") or "") != current_version:
        return True, "代码版本已变化"
    current_hash = database_sha256(Path(db_path))
    if str(marker.get("database_sha256") or "") != current_hash:
        return True, "数据库已变化"
    return False, "审计快照与当前数据库和代码版本一致"
