from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Mapping, Optional, Sequence

from platform_db import connect, initialize_database, new_id, now_iso, transaction


OPERATION_STATUSES = {
    "queued",
    "running",
    "succeeded",
    "partially_succeeded",
    "failed",
    "cancelled",
    "archived",
}
TERMINAL_STATUSES = {"succeeded", "partially_succeeded", "failed", "cancelled", "archived"}
ALLOWED_TRANSITIONS = {
    "queued": {"running", "cancelled", "failed"},
    "running": {"succeeded", "partially_succeeded", "failed", "cancelled"},
    "succeeded": set(),
    "partially_succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "archived": set(),
}
STATUS_LABELS = {
    "queued": "等待执行",
    "running": "运行中",
    "succeeded": "成功",
    "partially_succeeded": "部分成功",
    "failed": "失败",
    "cancelled": "已取消",
    "archived": "已归档",
}
OPERATION_LABELS = {
    "crawl": "公开数据采集",
    "file_import": "本地文件或URL导入",
    "ai_reprocess": "待AI补跑",
    "evidence_repair": "证据修复",
    "manual_review": "人工审核",
    "report_generation": "报告生成",
    "system_check": "系统检查",
    "qa_promotion": "QA数据晋升",
    "quality_reevaluation": "官方预警PDF质量复评",
    "create": "创建",
    "edit": "修改",
    "status_change": "状态变更",
    "report_exclusion": "移出报告",
    "business_action": "业务操作",
}


def _json(value: object, fallback: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError):
        return fallback


def _row_to_operation(row) -> dict[str, object]:
    item = dict(row)
    item["metadata"] = _json(item.pop("metadata_json", "{}"), {})
    item["structured_log"] = _json(item.pop("structured_log_json", "[]"), [])
    item["status_label"] = STATUS_LABELS.get(str(item.get("status")), str(item.get("status") or ""))
    item["operation_label"] = OPERATION_LABELS.get(
        str(item.get("operation_type")), str(item.get("operation_type") or "")
    )
    return item


def _validate_status(status: str) -> str:
    value = str(status or "").strip()
    if value not in OPERATION_STATUSES:
        raise ValueError(f"不支持的任务状态：{value}")
    return value


def create_operation(
    workspace_id: str,
    operation_type: str,
    db_path,
    *,
    status: str = "queued",
    source_id: str = "",
    created_by: str = "local_user",
    ui_session_id: str = "",
    input_summary: str = "",
    parent_run_id: str = "",
    metadata: Optional[Mapping[str, object]] = None,
    external_ref_type: str = "",
    external_ref_id: str = "",
) -> str:
    initialize_database(db_path)
    status = _validate_status(status)
    timestamp = now_iso()
    started_at = timestamp if status == "running" else ""
    operation_run_id = new_id("OP")
    with transaction(db_path) as connection:
        if external_ref_type and external_ref_id:
            existing = connection.execute(
                """SELECT operation_run_id FROM operation_runs
                WHERE workspace_id=? AND external_ref_type=? AND external_ref_id=?""",
                (workspace_id, external_ref_type, external_ref_id),
            ).fetchone()
            if existing:
                return str(existing["operation_run_id"])
        connection.execute(
            """INSERT INTO operation_runs(
            operation_run_id,workspace_id,operation_type,source_id,status,started_at,
            created_by,ui_session_id,input_summary,parent_run_id,metadata_json,
            external_ref_type,external_ref_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_run_id,
                workspace_id,
                operation_type,
                source_id,
                status,
                started_at,
                created_by,
                ui_session_id,
                str(input_summary or "")[:2000],
                parent_run_id,
                json.dumps(dict(metadata or {}), ensure_ascii=False, default=str),
                external_ref_type,
                external_ref_id,
                timestamp,
                timestamp,
            ),
        )
    return operation_run_id


def start_operation(
    workspace_id: str,
    operation_type: str,
    db_path,
    **kwargs,
) -> str:
    return create_operation(
        workspace_id,
        operation_type,
        db_path,
        status="running",
        **kwargs,
    )


EXCLUSIVE_OPERATION_TYPES = {"crawl", "ai_reprocess", "index_rebuild"}


def enqueue_operation(
    workspace_id: str,
    operation_type: str,
    db_path,
    *,
    source_id: str = "",
    created_by: str = "local_user",
    ui_session_id: str = "",
    input_summary: str = "",
    parent_run_id: str = "",
    metadata: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    """Create one durable queued task, or return the already active task.

    The check and insert share one ``BEGIN IMMEDIATE`` transaction so repeated
    Streamlit reruns or double clicks cannot create a second worker job.
    """

    initialize_database(db_path)
    timestamp = now_iso()
    operation_run_id = new_id("OP")
    created = False
    with transaction(db_path) as connection:
        if operation_type in EXCLUSIVE_OPERATION_TYPES:
            existing = connection.execute(
                """SELECT operation_run_id,status FROM operation_runs
                WHERE workspace_id=? AND operation_type=?
                AND status IN ('queued','running')
                ORDER BY created_at LIMIT 1""",
                (workspace_id, operation_type),
            ).fetchone()
            if existing:
                operation_run_id = str(existing["operation_run_id"])
            else:
                created = True
        else:
            created = True
        if created:
            connection.execute(
                """INSERT INTO operation_runs(
                operation_run_id,workspace_id,operation_type,source_id,status,
                created_by,ui_session_id,input_summary,parent_run_id,metadata_json,
                heartbeat_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_run_id,
                    workspace_id,
                    operation_type,
                    source_id,
                    "queued",
                    created_by,
                    ui_session_id,
                    str(input_summary or "")[:2000],
                    parent_run_id,
                    json.dumps(dict(metadata or {}), ensure_ascii=False, default=str),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
    if not created:
        add_technical_log(
            workspace_id,
            db_path,
            "重复提交已阻止，返回现有任务。",
            operation_run_id=operation_run_id,
            level="info",
            component="task_queue",
        )
    return {
        "operation_run_id": operation_run_id,
        "created": created,
        "operation": get_operation(operation_run_id, db_path),
    }


def claim_operation(operation_run_id: str, db_path, *, worker_pid: int = 0) -> dict[str, object]:
    timestamp = now_iso()
    with transaction(db_path) as connection:
        row = connection.execute(
            "SELECT status,cancel_requested FROM operation_runs WHERE operation_run_id=?",
            (operation_run_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"任务不存在：{operation_run_id}")
        if int(row["cancel_requested"] or 0):
            connection.execute(
                """UPDATE operation_runs SET status='cancelled',finished_at=?,
                result_summary='任务开始前已取消',heartbeat_at=?,updated_at=?
                WHERE operation_run_id=?""",
                (timestamp, timestamp, timestamp, operation_run_id),
            )
        elif str(row["status"]) == "queued":
            connection.execute(
                """UPDATE operation_runs SET status='running',started_at=?,
                heartbeat_at=?,current_stage='启动任务',worker_pid=?,updated_at=?
                WHERE operation_run_id=?""",
                (timestamp, timestamp, max(0, int(worker_pid)), timestamp, operation_run_id),
            )
        elif str(row["status"]) != "running":
            raise ValueError(f"任务当前状态不可执行：{row['status']}")
    return get_operation(operation_run_id, db_path) or {}


def update_operation_progress(
    operation_run_id: str,
    db_path,
    *,
    current_stage: str = "",
    current_item: str = "",
    completed_items: Optional[int] = None,
    total_items: Optional[int] = None,
    success_count: Optional[int] = None,
    isolated_count: Optional[int] = None,
    skipped_count: Optional[int] = None,
    failed_count: Optional[int] = None,
    metadata: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    timestamp = now_iso()
    with transaction(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM operation_runs WHERE operation_run_id=?",
            (operation_run_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"任务不存在：{operation_run_id}")
        current_metadata = _json(row["metadata_json"], {})
        if not isinstance(current_metadata, dict):
            current_metadata = {}
        current_metadata.update(dict(metadata or {}))
        connection.execute(
            """UPDATE operation_runs SET heartbeat_at=?,current_stage=?,current_item=?,
            completed_items=?,total_items=?,success_count=?,isolated_count=?,
            skipped_count=?,failed_count=?,metadata_json=?,updated_at=?
            WHERE operation_run_id=?""",
            (
                timestamp,
                str(current_stage or row["current_stage"] or "")[:500],
                str(current_item or row["current_item"] or "")[:1000],
                int(row["completed_items"] if completed_items is None else completed_items),
                int(row["total_items"] if total_items is None else total_items),
                int(row["success_count"] if success_count is None else success_count),
                int(row["isolated_count"] if isolated_count is None else isolated_count),
                int(row["skipped_count"] if skipped_count is None else skipped_count),
                int(row["failed_count"] if failed_count is None else failed_count),
                json.dumps(current_metadata, ensure_ascii=False, default=str),
                timestamp,
                operation_run_id,
            ),
        )
    return get_operation(operation_run_id, db_path) or {}


def request_operation_cancel(
    operation_run_id: str,
    workspace_id: str,
    db_path,
) -> bool:
    timestamp = now_iso()
    with transaction(db_path) as connection:
        row = connection.execute(
            """SELECT status FROM operation_runs
            WHERE operation_run_id=? AND workspace_id=?""",
            (operation_run_id, workspace_id),
        ).fetchone()
        if not row:
            raise KeyError("任务不存在")
        if str(row["status"]) not in {"queued", "running"}:
            return False
        if str(row["status"]) == "queued":
            connection.execute(
                """UPDATE operation_runs SET cancel_requested=1,status='cancelled',
                finished_at=?,heartbeat_at=?,current_stage='已取消',updated_at=?
                WHERE operation_run_id=?""",
                (timestamp, timestamp, timestamp, operation_run_id),
            )
        else:
            connection.execute(
                """UPDATE operation_runs SET cancel_requested=1,current_stage='等待安全取消',
                heartbeat_at=?,updated_at=? WHERE operation_run_id=?""",
                (timestamp, timestamp, operation_run_id),
            )
    add_technical_log(
        workspace_id,
        db_path,
        "用户请求取消任务；将在当前安全边界生效。",
        operation_run_id=operation_run_id,
        level="warning",
        component="task_queue",
    )
    return True


def operation_cancel_requested(operation_run_id: str, db_path) -> bool:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT cancel_requested,status FROM operation_runs WHERE operation_run_id=?",
            (operation_run_id,),
        ).fetchone()
    return bool(
        row
        and (
            int(row["cancel_requested"] or 0)
            or str(row["status"]) == "cancelled"
        )
    )


def set_operation_worker_pid(operation_run_id: str, db_path, worker_pid: int) -> None:
    with transaction(db_path) as connection:
        connection.execute(
            """UPDATE operation_runs SET worker_pid=?,heartbeat_at=?,updated_at=?
            WHERE operation_run_id=? AND status='queued'""",
            (max(0, int(worker_pid)), now_iso(), now_iso(), operation_run_id),
        )


def get_operation(operation_run_id: str, db_path) -> dict[str, object] | None:
    initialize_database(db_path)
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM operation_runs WHERE operation_run_id=?",
            (operation_run_id,),
        ).fetchone()
    return _row_to_operation(row) if row else None


def transition_operation(
    operation_run_id: str,
    new_status: str,
    db_path,
    *,
    result_summary: str = "",
    counts: Optional[Mapping[str, object]] = None,
    error_summary: str = "",
    warning_count: Optional[int] = None,
    structured_log: Optional[Sequence[Mapping[str, object]]] = None,
    metadata: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    new_status = _validate_status(new_status)
    timestamp = now_iso()
    with transaction(db_path) as connection:
        current = connection.execute(
            "SELECT * FROM operation_runs WHERE operation_run_id=?",
            (operation_run_id,),
        ).fetchone()
        if not current:
            raise KeyError(f"任务不存在：{operation_run_id}")
        old_status = str(current["status"])
        if new_status != old_status and new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
            raise ValueError(f"不允许的任务状态转换：{old_status} → {new_status}")
        started_at = str(current["started_at"] or "")
        if new_status == "running" and not started_at:
            started_at = timestamp
        finished_at = timestamp if new_status in TERMINAL_STATUSES else ""
        duration_ms = int(current["duration_ms"] or 0)
        if started_at and finished_at:
            try:
                duration_ms = max(
                    0,
                    int(
                        (
                            datetime.fromisoformat(finished_at)
                            - datetime.fromisoformat(started_at)
                        ).total_seconds()
                        * 1000
                    ),
                )
            except ValueError:
                duration_ms = 0
        merged_metadata = _json(current["metadata_json"], {})
        if not isinstance(merged_metadata, dict):
            merged_metadata = {}
        merged_metadata.update(dict(metadata or {}))
        values = {
            "documents_created": int((counts or {}).get("documents_created", current["documents_created"]) or 0),
            "documents_updated": int((counts or {}).get("documents_updated", current["documents_updated"]) or 0),
            "events_created": int((counts or {}).get("events_created", current["events_created"]) or 0),
            "events_updated": int((counts or {}).get("events_updated", current["events_updated"]) or 0),
            "skipped_count": int((counts or {}).get("skipped_count", current["skipped_count"]) or 0),
            "failed_count": int((counts or {}).get("failed_count", current["failed_count"]) or 0),
            "warning_count": int(
                warning_count if warning_count is not None else current["warning_count"] or 0
            ),
        }
        connection.execute(
            """UPDATE operation_runs SET status=?,started_at=?,finished_at=?,duration_ms=?,
            result_summary=?,documents_created=?,documents_updated=?,events_created=?,
            events_updated=?,skipped_count=?,failed_count=?,warning_count=?,
            error_summary=?,structured_log_json=?,metadata_json=?,heartbeat_at=?,
            current_stage=?,updated_at=?
            WHERE operation_run_id=?""",
            (
                new_status,
                started_at,
                finished_at,
                duration_ms,
                str(result_summary or current["result_summary"] or "")[:4000],
                values["documents_created"],
                values["documents_updated"],
                values["events_created"],
                values["events_updated"],
                values["skipped_count"],
                values["failed_count"],
                values["warning_count"],
                str(error_summary or current["error_summary"] or "")[:8000],
                json.dumps(
                    list(structured_log)
                    if structured_log is not None
                    else _json(current["structured_log_json"], []),
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps(merged_metadata, ensure_ascii=False, default=str),
                timestamp,
                (
                    STATUS_LABELS.get(new_status, new_status)
                    if new_status in TERMINAL_STATUSES
                    else str(current["current_stage"] or "")
                ),
                timestamp,
                operation_run_id,
            ),
        )
    return get_operation(operation_run_id, db_path) or {}


def finish_operation(
    operation_run_id: str,
    db_path,
    *,
    status: str = "succeeded",
    **kwargs,
) -> dict[str, object]:
    return transition_operation(operation_run_id, status, db_path, **kwargs)


def list_operations(
    workspace_id: str,
    db_path,
    *,
    start_date: str = "",
    end_date: str = "",
    operation_types: Optional[Sequence[str]] = None,
    statuses: Optional[Sequence[str]] = None,
    source_id: str = "",
    search: str = "",
    include_archived: bool = True,
    limit: int = 200,
) -> list[dict[str, object]]:
    initialize_database(db_path)
    where = ["workspace_id=?"]
    params: list[object] = [workspace_id]
    if start_date:
        where.append("COALESCE(NULLIF(started_at,''),created_at)>=?")
        params.append(start_date)
    if end_date:
        where.append("COALESCE(NULLIF(started_at,''),created_at)<=?")
        params.append(end_date + "T23:59:59")
    if operation_types:
        values = [str(value) for value in operation_types]
        where.append("operation_type IN (%s)" % ",".join("?" for _ in values))
        params.extend(values)
    if statuses:
        values = [_validate_status(str(value)) for value in statuses]
        where.append("status IN (%s)" % ",".join("?" for _ in values))
        params.extend(values)
    if source_id:
        where.append("source_id=?")
        params.append(source_id)
    if search:
        where.append(
            "(operation_run_id LIKE ? OR external_ref_id LIKE ? OR input_summary LIKE ? OR result_summary LIKE ?)"
        )
        token = f"%{search}%"
        params.extend([token, token, token, token])
    if not include_archived:
        where.append("COALESCE(is_archived,0)=0")
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT * FROM operation_runs WHERE {' AND '.join(where)}
            ORDER BY COALESCE(NULLIF(started_at,''),created_at) DESC LIMIT ?""",
            [*params, max(1, min(int(limit), 2000))],
        ).fetchall()
    return [_row_to_operation(row) for row in rows]


def home_operations(
    workspace_id: str,
    ui_session_id: str,
    db_path,
    *,
    limit: int = 3,
) -> list[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT * FROM operation_runs WHERE workspace_id=? AND home_visible=1
            AND (
                status IN ('queued','running')
                OR (ui_session_id!='' AND ui_session_id=?)
            )
            ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END,
            COALESCE(NULLIF(started_at,''),created_at) DESC LIMIT ?""",
            (workspace_id, ui_session_id, max(1, min(int(limit), 3))),
        ).fetchall()
    return [_row_to_operation(row) for row in rows]


def close_operation(operation_run_id: str, workspace_id: str, db_path) -> None:
    timestamp = now_iso()
    with transaction(db_path) as connection:
        row = connection.execute(
            "SELECT status FROM operation_runs WHERE operation_run_id=? AND workspace_id=?",
            (operation_run_id, workspace_id),
        ).fetchone()
        if not row:
            raise KeyError("任务不存在")
        if str(row["status"]) in {"queued", "running"}:
            raise ValueError("运行中的任务不能直接关闭；请先取消或等待完成")
        connection.execute(
            """UPDATE operation_runs SET home_visible=0,archived_at=?,
            is_archived=1,updated_at=? WHERE operation_run_id=?""",
            (timestamp, timestamp, operation_run_id),
        )


def clear_home_operations(workspace_id: str, ui_session_id: str, db_path) -> int:
    with transaction(db_path) as connection:
        cursor = connection.execute(
            """UPDATE operation_runs SET home_visible=0,updated_at=?
            WHERE workspace_id=? AND ui_session_id=?
            AND status IN ('succeeded','partially_succeeded','failed','cancelled','archived')""",
            (now_iso(), workspace_id, ui_session_id),
        )
    return max(0, int(cursor.rowcount or 0))


def retry_operation(operation_run_id: str, workspace_id: str, ui_session_id: str, db_path) -> str:
    current = get_operation(operation_run_id, db_path)
    if not current or str(current.get("workspace_id")) != workspace_id:
        raise KeyError("任务不存在")
    if str(current.get("status")) not in {"failed", "partially_succeeded", "cancelled"}:
        raise ValueError("只有失败、部分成功或取消的任务可以重新运行")
    return create_operation(
        workspace_id,
        str(current["operation_type"]),
        db_path,
        status="queued",
        source_id=str(current.get("source_id") or ""),
        created_by="local_user",
        ui_session_id=ui_session_id,
        input_summary=str(current.get("input_summary") or ""),
        parent_run_id=operation_run_id,
        metadata={"retry_of": operation_run_id, **dict(current.get("metadata") or {})},
    )


def delete_operation_history(
    operation_run_id: str,
    workspace_id: str,
    db_path,
    *,
    confirmed: bool = False,
) -> bool:
    if not confirmed:
        raise PermissionError("删除历史记录前必须确认")
    with transaction(db_path) as connection:
        row = connection.execute(
            "SELECT status FROM operation_runs WHERE operation_run_id=? AND workspace_id=?",
            (operation_run_id, workspace_id),
        ).fetchone()
        if not row:
            return False
        if str(row["status"]) in {"queued", "running"}:
            raise ValueError("不能删除等待中或运行中的任务")
        connection.execute("DELETE FROM technical_logs WHERE operation_run_id=?", (operation_run_id,))
        connection.execute(
            "DELETE FROM operation_runs WHERE operation_run_id=? AND workspace_id=?",
            (operation_run_id, workspace_id),
        )
    return True


def add_technical_log(
    workspace_id: str,
    db_path,
    message: str,
    *,
    operation_run_id: str = "",
    level: str = "error",
    component: str = "",
    details: Optional[Mapping[str, object]] = None,
    retention_days: int = 30,
) -> str:
    log_id = new_id("LOG")
    created_at = now_iso()
    expires_at = (datetime.now().astimezone() + timedelta(days=max(1, retention_days))).isoformat(
        timespec="seconds"
    )
    with transaction(db_path) as connection:
        connection.execute(
            """INSERT INTO technical_logs(
            technical_log_id,operation_run_id,workspace_id,level,component,message,
            details_json,created_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                log_id,
                operation_run_id,
                workspace_id,
                level,
                component,
                str(message or "")[:4000],
                json.dumps(dict(details or {}), ensure_ascii=False, default=str),
                created_at,
                expires_at,
            ),
        )
    return log_id


def list_technical_logs(
    workspace_id: str,
    db_path,
    *,
    operation_run_id: str = "",
    limit: int = 200,
) -> list[dict[str, object]]:
    where = ["workspace_id=?"]
    params: list[object] = [workspace_id]
    if operation_run_id:
        where.append("operation_run_id=?")
        params.append(operation_run_id)
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT * FROM technical_logs WHERE {' AND '.join(where)}
            ORDER BY created_at DESC LIMIT ?""",
            [*params, max(1, min(int(limit), 1000))],
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = _json(item.pop("details_json", "{}"), {})
        result.append(item)
    return result


def cleanup_expired_technical_logs(workspace_id: str, db_path, *, confirmed: bool = False) -> int:
    if not confirmed:
        raise PermissionError("清理过期技术日志前必须确认")
    with transaction(db_path) as connection:
        cursor = connection.execute(
            """DELETE FROM technical_logs WHERE workspace_id=? AND expires_at!=''
            AND expires_at<?""",
            (workspace_id, now_iso()),
        )
    return max(0, int(cursor.rowcount or 0))


def export_operation_json(operation_run_id: str, workspace_id: str, db_path) -> bytes:
    operation = get_operation(operation_run_id, db_path)
    if not operation or str(operation.get("workspace_id")) != workspace_id:
        raise KeyError("任务不存在")
    payload = {
        "operation": operation,
        "technical_logs": list_technical_logs(
            workspace_id,
            db_path,
            operation_run_id=operation_run_id,
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def set_operation_log_path(operation_run_id: str, db_path, log_path: str) -> None:
    """Persist a project-relative worker log path without changing task state."""

    with transaction(db_path) as connection:
        connection.execute(
            """UPDATE operation_runs SET log_path=?,updated_at=?
            WHERE operation_run_id=?""",
            (str(log_path or "")[:2000], now_iso(), operation_run_id),
        )


def fail_unclaimed_operation(
    operation_run_id: str,
    db_path,
    *,
    exit_code: int,
    safe_error_summary: str,
) -> bool:
    """Fail a queued task whose worker exited before it could claim the row."""

    timestamp = now_iso()
    with transaction(db_path) as connection:
        row = connection.execute(
            "SELECT status,metadata_json FROM operation_runs WHERE operation_run_id=?",
            (operation_run_id,),
        ).fetchone()
        if not row or str(row["status"]) != "queued":
            return False
        metadata = _json(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["worker_exit_code"] = int(exit_code)
        metadata["worker_failed_before_claim"] = True
        connection.execute(
            """UPDATE operation_runs SET status='failed',finished_at=?,
            heartbeat_at=?,current_stage='Worker启动失败',failed_count=1,
            result_summary='Worker在领取任务前退出',error_summary=?,
            metadata_json=?,updated_at=? WHERE operation_run_id=? AND status='queued'""",
            (
                timestamp,
                timestamp,
                str(safe_error_summary or "Worker进程立即退出")[:2000],
                json.dumps(metadata, ensure_ascii=False, default=str),
                timestamp,
                operation_run_id,
            ),
        )
        return bool(connection.total_changes)


def recover_stale_operations(
    db_path,
    *,
    timeout_minutes: int = 30,
    queued_timeout_minutes: int = 3,
) -> int:
    initialize_database(db_path)
    now = datetime.now().astimezone()
    running_cutoff = now - timedelta(minutes=max(1, timeout_minutes))
    queued_cutoff = now - timedelta(minutes=max(1, queued_timeout_minutes))
    recovered = 0
    with transaction(db_path) as connection:
        rows = connection.execute(
            """SELECT operation_run_id,workspace_id,status,started_at,updated_at,
            heartbeat_at,cancel_requested FROM operation_runs
            WHERE status IN ('queued','running')"""
        ).fetchall()
        for row in rows:
            raw = str(row["heartbeat_at"] or row["updated_at"] or row["started_at"] or "")
            try:
                timestamp = datetime.fromisoformat(raw)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.astimezone()
            except ValueError:
                timestamp = datetime.min.replace(tzinfo=running_cutoff.tzinfo)
            cutoff = queued_cutoff if str(row["status"]) == "queued" else running_cutoff
            if timestamp > cutoff:
                continue
            finished = now_iso()
            connection.execute(
                """UPDATE operation_runs SET status=?,finished_at=?,error_summary=?,
                result_summary=?,current_stage=?,heartbeat_at=?,updated_at=?
                WHERE operation_run_id=?""",
                (
                    "cancelled" if int(row["cancel_requested"] or 0) else "failed",
                    finished,
                    (
                        "任务中断前已收到取消请求，启动检查已恢复为已取消。"
                        if int(row["cancel_requested"] or 0)
                        else (
                            f"应用启动时发现排队任务的Worker未在{max(1, queued_timeout_minutes)}分钟内领取，已恢复为异常中断。"
                            if str(row["status"]) == "queued"
                            else f"应用启动时发现任务超过{max(1, timeout_minutes)}分钟未更新，已恢复为异常中断。"
                        )
                    ),
                    (
                        "已取消，已提交的部分结果保留。"
                        if int(row["cancel_requested"] or 0)
                        else "异常中断，可在历史记录中重新运行。"
                    ),
                    "已取消" if int(row["cancel_requested"] or 0) else "异常中断",
                    finished,
                    finished,
                    row["operation_run_id"],
                ),
            )
            recovered += 1
    return recovered
