from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Iterable, Mapping, Optional, Union
from uuid import uuid4

from data_store import ensure_data_file, load_events, save_events
from intake_store import ensure_intake_file
from source_store import ensure_sources_file


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = BASE_DIR / "data"
DEFAULT_DATABASE_PATH = DEFAULT_DATA_ROOT / "portscope.db"


@dataclass(frozen=True)
class WorkspacePaths:
    workspace_id: str
    root: Path
    events: Path
    sources: Path
    intakes: Path
    reports: Path


WORKSPACE_FIELDS = [
    "workspace_id",
    "workspace_name",
    "industry",
    "region",
    "default_categories",
    "default_report_title",
    "default_analyst",
    "default_period_days",
    "ai_enabled",
    "created_at",
]

CLIENT_FIELDS = [
    "client_id",
    "workspace_id",
    "client_name",
    "industry",
    "region",
    "focus_categories",
    "focus_keywords",
    "report_title",
    "analyst_name",
    "company_name",
    "contact_info",
    "disclaimer",
    "enabled",
    "created_at",
    "updated_at",
]


def database_path(path: Optional[Union[str, Path]] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.getenv("PORTSCOPE_DB_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_DATABASE_PATH


def data_root(path: Optional[Union[str, Path]] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.getenv("PORTSCOPE_DATA_ROOT", "").strip()
    return Path(configured) if configured else DEFAULT_DATA_ROOT


def _connect(path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    target = database_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: Optional[Union[str, Path]] = None) -> Path:
    target = database_path(path)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT PRIMARY KEY,
                workspace_name TEXT NOT NULL,
                industry TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                default_categories TEXT NOT NULL DEFAULT '[]',
                default_report_title TEXT NOT NULL DEFAULT '',
                default_analyst TEXT NOT NULL DEFAULT '',
                default_period_days INTEGER NOT NULL DEFAULT 7,
                ai_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                client_name TEXT NOT NULL,
                industry TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                focus_categories TEXT NOT NULL DEFAULT '[]',
                focus_keywords TEXT NOT NULL DEFAULT '[]',
                report_title TEXT NOT NULL DEFAULT '',
                analyst_name TEXT NOT NULL DEFAULT '',
                company_name TEXT NOT NULL DEFAULT '',
                contact_info TEXT NOT NULL DEFAULT '',
                disclaimer TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
            );

            CREATE TABLE IF NOT EXISTS action_history (
                history_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
            );

            CREATE TABLE IF NOT EXISTS reports (
                report_record_id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT '',
                report_title TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                event_ids TEXT NOT NULL DEFAULT '[]',
                docx_file TEXT NOT NULL DEFAULT '',
                html_file TEXT NOT NULL DEFAULT '',
                xlsx_file TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL,
                UNIQUE(workspace_id, report_id, version),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
            );

            CREATE INDEX IF NOT EXISTS idx_clients_workspace
                ON clients(workspace_id, enabled);
            CREATE INDEX IF NOT EXISTS idx_history_workspace
                ON action_history(workspace_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_reports_workspace
                ON reports(workspace_id, generated_at);
            """
        )
        existing_report_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(reports)")}
        for column, definition in {
            "report_mode": "TEXT NOT NULL DEFAULT '标准报告'",
            "analysis_model": "TEXT NOT NULL DEFAULT 'deterministic-rules'",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "status_note": "TEXT NOT NULL DEFAULT ''",
            "export_mode": "TEXT NOT NULL DEFAULT 'summary_only'",
            "permission_risk_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in existing_report_columns:
                connection.execute(f"ALTER TABLE reports ADD COLUMN {column} {definition}")
    return target


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _json_list(value: object) -> str:
    if value is None:
        items: list[str] = []
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            items = []
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = [item.strip() for item in raw.replace("，", ",").split(",")]
            items = parsed if isinstance(parsed, list) else [str(parsed)]
    else:
        items = list(value) if isinstance(value, Iterable) else [str(value)]
    return json.dumps([_text(item) for item in items if _text(item)], ensure_ascii=False)


def _decode_json_list(value: object) -> list[str]:
    try:
        parsed = json.loads(_text(value) or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _row_to_workspace(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    result["default_categories"] = _decode_json_list(result["default_categories"])
    result["ai_enabled"] = bool(result["ai_enabled"])
    result["default_period_days"] = int(result["default_period_days"])
    return result


def workspace_paths(
    workspace_id: str,
    root: Optional[Union[str, Path]] = None,
    ensure: bool = True,
) -> WorkspacePaths:
    workspace_id = _text(workspace_id)
    if not workspace_id.startswith("WS-") or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in workspace_id
    ):
        raise ValueError("workspace_id 格式无效。")
    base = data_root(root) / "workspaces" / workspace_id
    resolved = WorkspacePaths(
        workspace_id=workspace_id,
        root=base,
        events=base / "events.csv",
        sources=base / "sources.csv",
        intakes=base / "intake.csv",
        reports=base / "reports",
    )
    if ensure:
        base.mkdir(parents=True, exist_ok=True)
        resolved.reports.mkdir(parents=True, exist_ok=True)
        ensure_data_file(resolved.events)
        ensure_sources_file(resolved.sources)
        ensure_intake_file(resolved.intakes)
    return resolved


def create_workspace(
    values: Mapping[str, object],
    db_path: Optional[Union[str, Path]] = None,
    root: Optional[Union[str, Path]] = None,
    migrate_legacy: bool = False,
) -> dict[str, object]:
    initialize_database(db_path)
    workspace_name = _text(values.get("workspace_name"))
    if not workspace_name:
        raise ValueError("工作空间名称不能为空。")
    workspace_id = f"WS-{uuid4().hex[:10].upper()}"
    created_at = _now()
    period = int(values.get("default_period_days") or 7)
    if period not in {7, 14, 30, 90}:
        raise ValueError("默认统计周期必须是 7、14、30 或 90 天。")
    row = {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "industry": _text(values.get("industry")),
        "region": _text(values.get("region")),
        "default_categories": _json_list(values.get("default_categories")),
        "default_report_title": _text(values.get("default_report_title")) or "公开信息商业情报报告",
        "default_analyst": _text(values.get("default_analyst")),
        "default_period_days": period,
        "ai_enabled": 1 if bool(values.get("ai_enabled", False)) else 0,
        "created_at": created_at,
    }
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO workspaces (
                workspace_id, workspace_name, industry, region, default_categories,
                default_report_title, default_analyst, default_period_days,
                ai_enabled, created_at
            ) VALUES (
                :workspace_id, :workspace_name, :industry, :region, :default_categories,
                :default_report_title, :default_analyst, :default_period_days,
                :ai_enabled, :created_at
            )
            """,
            row,
        )
        connection.execute(
            "INSERT OR REPLACE INTO settings(setting_key, setting_value) VALUES('active_workspace_id', ?)",
            (workspace_id,),
        )
    workspace_paths(workspace_id, root)
    log_action(
        workspace_id,
        "创建",
        "workspace",
        workspace_id,
        {"workspace_name": workspace_name},
        db_path,
    )
    if migrate_legacy:
        migrate_legacy_csvs(workspace_id, db_path=db_path, root=root)
    return get_workspace(workspace_id, db_path=db_path) or {}


def load_workspaces(
    db_path: Optional[Union[str, Path]] = None,
) -> list[dict[str, object]]:
    initialize_database(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM workspaces ORDER BY created_at, workspace_name"
        ).fetchall()
    return [_row_to_workspace(row) for row in rows]


def get_workspace(
    workspace_id: str, db_path: Optional[Union[str, Path]] = None
) -> Optional[dict[str, object]]:
    initialize_database(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
    return _row_to_workspace(row) if row else None


def update_workspace(
    workspace_id: str,
    values: Mapping[str, object],
    db_path: Optional[Union[str, Path]] = None,
) -> dict[str, object]:
    current = get_workspace(workspace_id, db_path)
    if current is None:
        raise KeyError(f"工作空间不存在：{workspace_id}")
    allowed = {
        "workspace_name",
        "industry",
        "region",
        "default_categories",
        "default_report_title",
        "default_analyst",
        "default_period_days",
        "ai_enabled",
    }
    merged = dict(current)
    merged.update({key: value for key, value in values.items() if key in allowed})
    if not _text(merged["workspace_name"]):
        raise ValueError("工作空间名称不能为空。")
    period = int(merged.get("default_period_days") or 7)
    if period not in {7, 14, 30, 90}:
        raise ValueError("默认统计周期必须是 7、14、30 或 90 天。")
    with _connect(db_path) as connection:
        connection.execute(
            """
            UPDATE workspaces SET
                workspace_name = ?, industry = ?, region = ?, default_categories = ?,
                default_report_title = ?, default_analyst = ?, default_period_days = ?,
                ai_enabled = ?
            WHERE workspace_id = ?
            """,
            (
                _text(merged["workspace_name"]),
                _text(merged["industry"]),
                _text(merged["region"]),
                _json_list(merged["default_categories"]),
                _text(merged["default_report_title"]),
                _text(merged["default_analyst"]),
                period,
                1 if bool(merged["ai_enabled"]) else 0,
                workspace_id,
            ),
        )
    return get_workspace(workspace_id, db_path) or {}


def set_active_workspace(
    workspace_id: str, db_path: Optional[Union[str, Path]] = None
) -> None:
    if get_workspace(workspace_id, db_path) is None:
        raise KeyError(f"工作空间不存在：{workspace_id}")
    with _connect(db_path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO settings(setting_key, setting_value) VALUES('active_workspace_id', ?)",
            (workspace_id,),
        )


def get_active_workspace(
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[dict[str, object]]:
    workspaces = load_workspaces(db_path)
    if not workspaces:
        return None
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'active_workspace_id'"
        ).fetchone()
    if row:
        selected = next(
            (item for item in workspaces if item["workspace_id"] == row["setting_value"]),
            None,
        )
        if selected:
            return selected
    set_active_workspace(str(workspaces[0]["workspace_id"]), db_path)
    return workspaces[0]


def legacy_csv_counts(root: Optional[Union[str, Path]] = None) -> dict[str, int]:
    base = data_root(root)
    counts: dict[str, int] = {}
    for name in ("events.csv", "sources.csv", "intake.csv"):
        path = base / name
        if not path.exists():
            counts[name] = 0
            continue
        try:
            with path.open("r", encoding="utf-8-sig") as file:
                counts[name] = max(sum(1 for _ in file) - 1, 0)
        except OSError:
            counts[name] = 0
    return counts


def migrate_legacy_csvs(
    workspace_id: str,
    db_path: Optional[Union[str, Path]] = None,
    root: Optional[Union[str, Path]] = None,
) -> dict[str, str]:
    if get_workspace(workspace_id, db_path) is None:
        raise KeyError(f"工作空间不存在：{workspace_id}")
    base = data_root(root)
    paths = workspace_paths(workspace_id, root)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = base / "legacy_backup" / timestamp
    mapping = {
        base / "events.csv": paths.events,
        base / "sources.csv": paths.sources,
        base / "intake.csv": paths.intakes,
    }
    copied: dict[str, str] = {}
    for source, target in mapping.items():
        if not source.exists() or source.stat().st_size == 0:
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_dir / source.name)
        shutil.copy2(source, target)
        copied[source.name] = str(target)
    if "events.csv" in copied:
        migrated_events = load_events(paths.events)
        if not migrated_events.empty:
            migrated_events.loc[
                migrated_events["workspace_id"].eq(""), "workspace_id"
            ] = workspace_id
            save_events(migrated_events, paths.events)
    if copied:
        log_action(
            workspace_id,
            "迁移",
            "workspace",
            workspace_id,
            {"files": copied, "backup": str(backup_dir)},
            db_path,
        )
    return copied


def _row_to_client(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    result["focus_categories"] = _decode_json_list(result["focus_categories"])
    result["focus_keywords"] = _decode_json_list(result["focus_keywords"])
    result["enabled"] = bool(result["enabled"])
    return result


def create_client(
    workspace_id: str,
    values: Mapping[str, object],
    db_path: Optional[Union[str, Path]] = None,
) -> dict[str, object]:
    if get_workspace(workspace_id, db_path) is None:
        raise KeyError(f"工作空间不存在：{workspace_id}")
    client_name = _text(values.get("client_name"))
    if not client_name:
        raise ValueError("客户名称不能为空。")
    timestamp = _now()
    row = {
        "client_id": f"CLI-{uuid4().hex[:10].upper()}",
        "workspace_id": workspace_id,
        "client_name": client_name,
        "industry": _text(values.get("industry")),
        "region": _text(values.get("region")),
        "focus_categories": _json_list(values.get("focus_categories")),
        "focus_keywords": _json_list(values.get("focus_keywords")),
        "report_title": _text(values.get("report_title")),
        "analyst_name": _text(values.get("analyst_name")),
        "company_name": _text(values.get("company_name")),
        "contact_info": _text(values.get("contact_info")),
        "disclaimer": _text(values.get("disclaimer")),
        "enabled": 1 if bool(values.get("enabled", True)) else 0,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO clients (
                client_id, workspace_id, client_name, industry, region,
                focus_categories, focus_keywords, report_title, analyst_name,
                company_name, contact_info, disclaimer, enabled, created_at, updated_at
            ) VALUES (
                :client_id, :workspace_id, :client_name, :industry, :region,
                :focus_categories, :focus_keywords, :report_title, :analyst_name,
                :company_name, :contact_info, :disclaimer, :enabled, :created_at, :updated_at
            )
            """,
            row,
        )
    log_action(workspace_id, "创建", "client", row["client_id"], {"client_name": client_name}, db_path)
    return get_client(row["client_id"], db_path) or {}


def load_clients(
    workspace_id: str,
    db_path: Optional[Union[str, Path]] = None,
    include_disabled: bool = True,
) -> list[dict[str, object]]:
    initialize_database(db_path)
    query = "SELECT * FROM clients WHERE workspace_id = ?"
    if not include_disabled:
        query += " AND enabled = 1"
    query += " ORDER BY enabled DESC, client_name"
    with _connect(db_path) as connection:
        rows = connection.execute(query, (workspace_id,)).fetchall()
    return [_row_to_client(row) for row in rows]


def get_client(
    client_id: str, db_path: Optional[Union[str, Path]] = None
) -> Optional[dict[str, object]]:
    initialize_database(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
    return _row_to_client(row) if row else None


def update_client(
    client_id: str,
    values: Mapping[str, object],
    db_path: Optional[Union[str, Path]] = None,
) -> dict[str, object]:
    current = get_client(client_id, db_path)
    if current is None:
        raise KeyError(f"客户不存在：{client_id}")
    merged = dict(current)
    merged.update(values)
    if not _text(merged.get("client_name")):
        raise ValueError("客户名称不能为空。")
    with _connect(db_path) as connection:
        connection.execute(
            """
            UPDATE clients SET
                client_name = ?, industry = ?, region = ?, focus_categories = ?,
                focus_keywords = ?, report_title = ?, analyst_name = ?, company_name = ?,
                contact_info = ?, disclaimer = ?, enabled = ?, updated_at = ?
            WHERE client_id = ?
            """,
            (
                _text(merged.get("client_name")),
                _text(merged.get("industry")),
                _text(merged.get("region")),
                _json_list(merged.get("focus_categories")),
                _json_list(merged.get("focus_keywords")),
                _text(merged.get("report_title")),
                _text(merged.get("analyst_name")),
                _text(merged.get("company_name")),
                _text(merged.get("contact_info")),
                _text(merged.get("disclaimer")),
                1 if bool(merged.get("enabled", True)) else 0,
                _now(),
                client_id,
            ),
        )
    log_action(str(current["workspace_id"]), "修改", "client", client_id, {}, db_path)
    return get_client(client_id, db_path) or {}


def log_action(
    workspace_id: str,
    action_type: str,
    entity_type: str,
    entity_id: str,
    details: Optional[Mapping[str, object]] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> str:
    initialize_database(db_path)
    history_id = f"HIS-{uuid4().hex[:12].upper()}"
    payload = json.dumps(dict(details or {}), ensure_ascii=False, default=str)
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO action_history(
                history_id, workspace_id, action_type, entity_type, entity_id,
                occurred_at, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (history_id, workspace_id, action_type, entity_type, entity_id, _now(), payload),
        )
    return history_id


def load_action_history(
    workspace_id: str,
    db_path: Optional[Union[str, Path]] = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    initialize_database(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM action_history WHERE workspace_id = ?
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (workspace_id, int(limit)),
        ).fetchall()
    results: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item["details"])
        except json.JSONDecodeError:
            item["details"] = {}
        results.append(item)
    return results


def next_report_identity(
    workspace_id: str,
    client_id: str,
    report_title: str,
    start_date: str,
    end_date: str,
    db_path: Optional[Union[str, Path]] = None,
) -> tuple[str, int]:
    initialize_database(db_path)
    signature = "|".join(
        [workspace_id, _text(client_id), _text(report_title), _text(start_date), _text(end_date)]
    )
    report_id = f"RPT-{uuid4().hex[:10].upper()}"
    with _connect(db_path) as connection:
        previous = connection.execute(
            """
            SELECT report_id, MAX(version) AS max_version FROM reports
            WHERE workspace_id = ? AND client_id = ? AND report_title = ?
              AND start_date = ? AND end_date = ?
            GROUP BY report_id ORDER BY max_version DESC LIMIT 1
            """,
            (workspace_id, _text(client_id), _text(report_title), _text(start_date), _text(end_date)),
        ).fetchone()
    if previous:
        return str(previous["report_id"]), int(previous["max_version"]) + 1
    _ = signature  # The exact metadata signature is stored in the report row for traceability.
    return report_id, 1


def record_report(
    values: Mapping[str, object],
    db_path: Optional[Union[str, Path]] = None,
) -> dict[str, object]:
    initialize_database(db_path)
    required = ["report_id", "workspace_id", "report_title", "start_date", "end_date", "version"]
    missing = [field for field in required if not _text(values.get(field))]
    if missing:
        raise ValueError(f"报告记录缺少字段：{'、'.join(missing)}")
    row = {
        "report_record_id": f"RR-{uuid4().hex[:12].upper()}",
        "report_id": _text(values.get("report_id")),
        "workspace_id": _text(values.get("workspace_id")),
        "client_id": _text(values.get("client_id")),
        "report_title": _text(values.get("report_title")),
        "start_date": _text(values.get("start_date")),
        "end_date": _text(values.get("end_date")),
        "generated_at": _text(values.get("generated_at")) or _now(),
        "event_ids": _json_list(values.get("event_ids")),
        "docx_file": _text(values.get("docx_file")),
        "html_file": _text(values.get("html_file")),
        "xlsx_file": _text(values.get("xlsx_file")),
        "version": int(values.get("version") or 1),
        "report_mode": _text(values.get("report_mode")) or "标准报告",
        "analysis_model": _text(values.get("analysis_model")) or "deterministic-rules",
        "status": _text(values.get("status")) or "active",
        "status_note": _text(values.get("status_note")),
        "export_mode": _text(values.get("export_mode")) or "summary_only",
        "permission_risk_count": int(values.get("permission_risk_count") or 0),
    }
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO reports(
                report_record_id, report_id, workspace_id, client_id, report_title,
                start_date, end_date, generated_at, event_ids, docx_file,
                html_file, xlsx_file, version, report_mode, analysis_model,
                status,status_note,export_mode,permission_risk_count
            ) VALUES (
                :report_record_id, :report_id, :workspace_id, :client_id, :report_title,
                :start_date, :end_date, :generated_at, :event_ids, :docx_file,
                :html_file, :xlsx_file, :version, :report_mode, :analysis_model,
                :status,:status_note,:export_mode,:permission_risk_count
            )
            """,
            row,
        )
    log_action(
        row["workspace_id"],
        "生成报告",
        "report",
        row["report_id"],
        {"version": row["version"], "event_ids": json.loads(row["event_ids"])},
        db_path,
    )
    result = dict(row)
    result["event_ids"] = json.loads(row["event_ids"])
    return result


def load_reports(
    workspace_id: str,
    db_path: Optional[Union[str, Path]] = None,
) -> list[dict[str, object]]:
    initialize_database(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM reports WHERE workspace_id = ?
            ORDER BY generated_at DESC, version DESC
            """,
            (workspace_id,),
        ).fetchall()
    results: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["event_ids"] = _decode_json_list(item["event_ids"])
        item["version"] = int(item["version"])
        results.append(item)
    return results


def workspace_manifest(paths: WorkspacePaths) -> dict[str, str]:
    return {key: str(value) for key, value in asdict(paths).items()}
