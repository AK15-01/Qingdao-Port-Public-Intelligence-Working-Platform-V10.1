from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Mapping, Optional

from operation_store import (
    add_technical_log,
    enqueue_operation,
    fail_unclaimed_operation,
    finish_operation,
    set_operation_log_path,
    set_operation_worker_pid,
)


PROJECT_ROOT = Path(__file__).resolve().parent
WORKER_SCRIPT = PROJECT_ROOT / "scripts" / "task_worker.py"
TASK_LOG_ROOT = PROJECT_ROOT / "output" / "task_logs"
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"), r"\1***"),
    (re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s\"']+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-****"),
)


def _safe_worker_error(value: object, limit: int = 1200) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[-max(100, int(limit)):]


def _relative_task_log(operation_run_id: str) -> Path:
    return Path("output") / "task_logs" / f"{operation_run_id}.log"


def _read_log_tail(path: Path, max_bytes: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return _safe_worker_error(handle.read().decode("utf-8", errors="replace"))
    except OSError:
        return ""


def _sanitize_log_file(path: Path) -> None:
    """Atomically redact a completed early-crash log before exposing it."""

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        safe = _safe_worker_error(content, max(len(content), 1200))
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(safe, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        return


def read_task_log(log_path: str, max_bytes: int = 20000) -> str:
    """Read a redacted task log only when it resolves inside output/task_logs."""

    candidate = (PROJECT_ROOT / str(log_path or "")).resolve()
    allowed = TASK_LOG_ROOT.resolve()
    try:
        candidate.relative_to(allowed)
    except ValueError:
        raise ValueError("任务日志路径不在允许目录中")
    return _read_log_tail(candidate, max_bytes=max(1000, min(int(max_bytes), 100_000)))


def enqueue_background_task(
    workspace_id: str,
    operation_type: str,
    db_path,
    data_root,
    *,
    source_id: str = "",
    ui_session_id: str = "",
    input_summary: str = "",
    metadata: Optional[Mapping[str, object]] = None,
    launch_worker: bool = True,
    popen_factory=subprocess.Popen,
    early_exit_grace_seconds: float = 0.35,
    sleep_fn=time.sleep,
) -> dict[str, object]:
    """Persist a bounded task and start one independent local worker.

    The database insert is exclusive per workspace and operation type. A
    Streamlit rerun or double click therefore returns the existing task ID and
    never launches another process.
    """

    queued = enqueue_operation(
        workspace_id,
        operation_type,
        db_path,
        source_id=source_id,
        ui_session_id=ui_session_id,
        input_summary=input_summary,
        metadata=metadata,
    )
    if not queued["created"] or not launch_worker:
        return queued
    operation_id = str(queued["operation_run_id"])
    relative_log = _relative_task_log(operation_id)
    absolute_log = PROJECT_ROOT / relative_log
    absolute_log.parent.mkdir(parents=True, exist_ok=True)
    absolute_log.write_text(
        f"PortScope worker task={operation_id}\n",
        encoding="utf-8",
    )
    set_operation_log_path(operation_id, db_path, relative_log.as_posix())
    command = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--db",
        str(Path(db_path).resolve()),
        "--data-root",
        str(Path(data_root).resolve()),
        "--operation-id",
        operation_id,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with absolute_log.open("ab") as log_handle:
            process = popen_factory(
                command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                close_fds=os.name != "nt",
                creationflags=creationflags,
            )
    except Exception as exc:
        finish_operation(
            operation_id,
            db_path,
            status="failed",
            result_summary="本地Worker启动失败",
            error_summary=f"{type(exc).__name__}: {_safe_worker_error(exc, 500)}",
            counts={"failed_count": 1},
        )
        add_technical_log(
            workspace_id,
            db_path,
            "本地Worker启动失败。",
            operation_run_id=operation_id,
            level="error",
            component="task_queue",
            details={"error_type": type(exc).__name__},
        )
        raise
    set_operation_worker_pid(
        operation_id,
        db_path,
        int(getattr(process, "pid", 0) or 0),
    )
    queued["worker_pid"] = int(getattr(process, "pid", 0) or 0)
    queued["log_path"] = relative_log.as_posix()
    if early_exit_grace_seconds > 0:
        sleep_fn(min(float(early_exit_grace_seconds), 2.0))
    poll = getattr(process, "poll", None)
    exit_code = poll() if callable(poll) else None
    if exit_code is not None:
        _sanitize_log_file(absolute_log)
        log_tail = _read_log_tail(absolute_log)
        summary = (
            f"Worker在领取任务前退出（退出码 {int(exit_code)}）。"
            + (f"\n日志末尾：{log_tail}" if log_tail else "")
        )
        if fail_unclaimed_operation(
            operation_id,
            db_path,
            exit_code=int(exit_code),
            safe_error_summary=summary,
        ):
            add_technical_log(
                workspace_id,
                db_path,
                "Worker在领取任务前退出，任务已立即标记失败。",
                operation_run_id=operation_id,
                level="error",
                component="task_queue",
                details={
                    "exit_code": int(exit_code),
                    "log_path": relative_log.as_posix(),
                },
            )
            queued["immediate_exit"] = True
    return queued
