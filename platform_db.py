from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Iterable, Iterator, Mapping, Optional, Sequence, Union
from urllib.parse import urlparse
from uuid import uuid4

from workspace_store import database_path, initialize_database as initialize_workspace_database
from document_quality import assess_document


PathLike = Union[str, Path]
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "portscope.db"
SOURCE_CATALOG_PATH = BASE_DIR / "config" / "source_catalog.json"
DEFAULT_SOURCES_PATH = BASE_DIR / "config" / "default_sources.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12].upper()}"


def content_hash(text: object) -> str:
    normalized = "\n".join(str(text or "").replace("\r\n", "\n").splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def connect(path: Optional[PathLike] = None) -> sqlite3.Connection:
    target = database_path(path or DEFAULT_DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def transaction(path: Optional[PathLike] = None) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_columns(connection: sqlite3.Connection, table: str, definitions: Mapping[str, str]) -> None:
    existing = _columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _recover_stale_runs_connection(connection: sqlite3.Connection, timeout_minutes: int = 30) -> int:
    now = datetime.now().astimezone()
    recovered = 0
    rows = connection.execute(
        "SELECT crawl_run_id,started_at,finished_at,updated_at,error_summary FROM crawl_runs WHERE status='运行中'"
    ).fetchall()
    for row in rows:
        reference = str(row["updated_at"] or row["started_at"] or "")
        try:
            timestamp = datetime.fromisoformat(reference)
            if timestamp.tzinfo is None:
                timestamp = timestamp.astimezone()
            stale = now - timestamp > timedelta(minutes=max(1, timeout_minutes))
        except ValueError:
            stale = True
        if not str(row["finished_at"] or "") and not stale:
            continue
        explanation = "启动检查发现采集记录长期未更新或已有结束时间，已自动恢复为异常中断。"
        previous = str(row["error_summary"] or "").strip()
        connection.execute(
            """UPDATE crawl_runs SET status='异常中断',finished_at=CASE WHEN finished_at='' THEN ? ELSE finished_at END,
            updated_at=?,error_summary=? WHERE crawl_run_id=?""",
            (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
             (previous + "\n" + explanation).strip()[:8000], row["crawl_run_id"]),
        )
        recovered += 1
    return recovered


def _harden_unchecked_documents_connection(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """SELECT d.document_id,d.title,d.cleaned_text,d.published_at,d.raw_html_path,
        s.source_name,s.organization FROM documents d JOIN sources s ON s.source_id=d.source_id
        WHERE d.quality_status='未检查'"""
    ).fetchall()
    hardened = 0
    for row in rows:
        raw_html = ""
        raw_path = Path(str(row["raw_html_path"] or ""))
        if raw_path.is_file():
            try:
                raw_html = raw_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raw_html = ""
        quality = assess_document(
            title=str(row["title"] or ""),
            text=str(row["cleaned_text"] or ""),
            published_at=str(row["published_at"] or ""),
            raw_html=raw_html,
            site_names=[str(row["source_name"] or ""), str(row["organization"] or "")],
        )
        extraction_status = quality.status if not quality.processing_allowed else "成功"
        ai_status = "质量门禁拦截" if not quality.processing_allowed else "待AI处理"
        connection.execute(
            """UPDATE documents SET extraction_status=?,quality_status=?,quality_metrics_json=?,
            report_quality_eligible=?,ai_status=?,review_status=CASE WHEN ?=0 THEN '质量异常' ELSE review_status END,
            updated_at=? WHERE document_id=?""",
            (
                extraction_status, quality.status, json.dumps(quality.metrics, ensure_ascii=False),
                int(quality.report_allowed), ai_status, int(quality.processing_allowed), now_iso(), row["document_id"],
            ),
        )
        if not quality.processing_allowed:
            connection.execute(
                """UPDATE events SET report_eligible=0,analyst_note=TRIM(analyst_note || ' 历史文档经质量硬化复核后被拦截。'),
                updated_at=? WHERE document_id=?""",
                (now_iso(), row["document_id"]),
            )
            connection.execute("DELETE FROM document_chunks_fts WHERE document_id=?", (row["document_id"],))
            connection.execute("DELETE FROM document_chunks WHERE document_id=?", (row["document_id"],))
        hardened += 1
    return hardened


def _stable_migration_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(f"{prefix}|{value}".encode("utf-8")).hexdigest()[:16].upper()
    return f"OP-MIG-{digest}"


def _normalized_operation_status(value: object) -> str:
    text = str(value or "").strip().lower()
    return {
        "运行中": "running",
        "queued": "queued",
        "running": "running",
        "完成": "succeeded",
        "success": "succeeded",
        "succeeded": "succeeded",
        "部分完成": "partially_succeeded",
        "partially_succeeded": "partially_succeeded",
        "失败": "failed",
        "异常中断": "failed",
        "failed": "failed",
        "已中止": "cancelled",
        "取消": "cancelled",
        "cancelled": "cancelled",
        "archived": "archived",
    }.get(text, "succeeded")


def _metadata_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _infer_archived_operation_status(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> tuple[str, str]:
    """Conservatively recover the result hidden by the legacy archived status."""

    metadata = _metadata_object(row["metadata_json"])
    original = str(metadata.get("original_status") or "").strip()
    if original in {"succeeded", "partially_succeeded", "failed", "cancelled"}:
        return original, "metadata.original_status"
    if str(row["external_ref_type"] or "") == "crawl_run" and str(row["external_ref_id"] or ""):
        crawl = connection.execute(
            "SELECT status FROM crawl_runs WHERE crawl_run_id=?",
            (row["external_ref_id"],),
        ).fetchone()
        if crawl:
            return _normalized_operation_status(crawl["status"]), "crawl_runs.status"
    if str(row["external_ref_type"] or "") in {"report_record", "action_history"}:
        return "succeeded", f"{row['external_ref_type']}存在"
    combined = " ".join(
        [
            str(row["result_summary"] or ""),
            str(row["error_summary"] or ""),
            str(row["current_stage"] or ""),
        ]
    )
    if any(word in combined for word in ("取消", "中止", "已撤销")):
        return "cancelled", "结果摘要明确记录取消或中止"
    successful_items = sum(
        int(row[name] or 0)
        for name in (
            "documents_created",
            "documents_updated",
            "events_created",
            "events_updated",
            "success_count",
        )
    )
    if int(row["failed_count"] or 0) > 0 or str(row["error_summary"] or "").strip():
        return (
            ("partially_succeeded" if successful_items > 0 else "failed"),
            "失败计数或错误摘要",
        )
    if any(word in combined for word in ("完成", "成功", "已生成", "已保存")):
        return "succeeded", "结果摘要明确记录完成或成功"
    return "archived", "legacy_unknown"


def _migrate_archived_operation_results(connection: sqlite3.Connection) -> int:
    changed = 0
    rows = connection.execute(
        "SELECT * FROM operation_runs WHERE status='archived'"
    ).fetchall()
    for row in rows:
        restored, basis = _infer_archived_operation_status(connection, row)
        metadata = _metadata_object(row["metadata_json"])
        metadata["legacy_archived_migration"] = True
        metadata["legacy_status_basis"] = basis
        metadata["legacy_unknown"] = restored == "archived"
        connection.execute(
            """UPDATE operation_runs SET status=?,is_archived=1,
            metadata_json=?,updated_at=? WHERE operation_run_id=?""",
            (
                restored,
                json.dumps(metadata, ensure_ascii=False, default=str),
                now_iso(),
                row["operation_run_id"],
            ),
        )
        changed += 1
    return changed


def _crawl_operation_candidates(
    connection: sqlite3.Connection,
    workspace_id: str,
    crawl_run_id: str,
) -> list[sqlite3.Row]:
    rows = connection.execute(
        """SELECT * FROM operation_runs WHERE workspace_id=? AND operation_type='crawl'
        AND (
            (external_ref_type='crawl_run' AND external_ref_id=?)
            OR operation_run_id=?
            OR metadata_json LIKE ?
        ) ORDER BY created_at,operation_run_id""",
        (
            workspace_id,
            crawl_run_id,
            _stable_migration_id("crawl", crawl_run_id),
            f"%{crawl_run_id}%",
        ),
    ).fetchall()
    result: list[sqlite3.Row] = []
    for row in rows:
        metadata = _metadata_object(row["metadata_json"])
        exact = (
            str(row["external_ref_type"] or "") == "crawl_run"
            and str(row["external_ref_id"] or "") == crawl_run_id
        )
        if exact or str(metadata.get("crawl_run_id") or "") == crawl_run_id:
            result.append(row)
        elif str(row["operation_run_id"]) == _stable_migration_id("crawl", crawl_run_id):
            result.append(row)
    return result


def _merge_duplicate_crawl_operations(
    connection: sqlite3.Connection,
    workspace_id: str,
    crawl_run_id: str,
) -> tuple[str, int]:
    """Keep a real worker operation and remove only generated migration duplicates."""

    candidates = _crawl_operation_candidates(connection, workspace_id, crawl_run_id)
    if not candidates:
        return "", 0
    normal = [row for row in candidates if str(row["created_by"]) != "legacy_migration"]
    keeper = normal[0] if normal else candidates[0]
    keeper_id = str(keeper["operation_run_id"])
    removed = 0
    for duplicate in candidates:
        duplicate_id = str(duplicate["operation_run_id"])
        if duplicate_id == keeper_id or str(duplicate["created_by"]) != "legacy_migration":
            continue
        connection.execute(
            "UPDATE technical_logs SET operation_run_id=? WHERE operation_run_id=?",
            (keeper_id, duplicate_id),
        )
        connection.execute(
            "UPDATE operation_runs SET parent_run_id=? WHERE parent_run_id=?",
            (keeper_id, duplicate_id),
        )
        keeper_metadata = _metadata_object(keeper["metadata_json"])
        duplicate_metadata = _metadata_object(duplicate["metadata_json"])
        merged_ids = list(keeper_metadata.get("merged_legacy_operation_ids") or [])
        if duplicate_id not in merged_ids:
            merged_ids.append(duplicate_id)
        keeper_metadata["merged_legacy_operation_ids"] = merged_ids
        if duplicate_metadata:
            keeper_metadata.setdefault("legacy_migration_metadata", duplicate_metadata)
        connection.execute(
            """UPDATE operation_runs SET result_summary=CASE WHEN result_summary=''
                THEN ? ELSE result_summary END,
                structured_log_json=CASE WHEN structured_log_json IN ('','[]')
                THEN ? ELSE structured_log_json END,
                log_path=CASE WHEN log_path='' THEN ? ELSE log_path END,
                metadata_json=?,updated_at=? WHERE operation_run_id=?""",
            (
                duplicate["result_summary"],
                duplicate["structured_log_json"],
                duplicate["log_path"],
                json.dumps(keeper_metadata, ensure_ascii=False, default=str),
                now_iso(),
                keeper_id,
            ),
        )
        connection.execute(
            "DELETE FROM operation_runs WHERE operation_run_id=? AND created_by='legacy_migration'",
            (duplicate_id,),
        )
        removed += 1
    # Clear a legacy mapping before assigning it to the real worker row. The
    # unique partial index then provides the final database-level guard.
    connection.execute(
        """UPDATE operation_runs SET external_ref_type='',external_ref_id=''
        WHERE workspace_id=? AND operation_type='crawl'
        AND external_ref_type='crawl_run' AND external_ref_id=?
        AND operation_run_id!=? AND created_by='legacy_migration'""",
        (workspace_id, crawl_run_id, keeper_id),
    )
    connection.execute(
        """UPDATE operation_runs SET external_ref_type='crawl_run',
        external_ref_id=?,updated_at=? WHERE operation_run_id=?""",
        (crawl_run_id, now_iso(), keeper_id),
    )
    return keeper_id, removed


def _apply_personal_workbench_migration_connection(connection: sqlite3.Connection) -> dict[str, int]:
    """Idempotently converge legacy data without deleting source records."""

    timestamp = now_iso()
    result = {
        "sources_migrated": 0,
        "documents_quarantined": 0,
        "events_quarantined": 0,
        "operations_migrated": 0,
        "operation_duplicates_merged": 0,
        "legacy_archived_reclassified": 0,
        "reports_obsoleted": 0,
    }

    source_rows = connection.execute(
        """SELECT source_id,crawl_allowed,report_use_allowed,terms_status,
        commercial_reuse_status,license_note,last_license_checked_at,
        permission_basis,permission_note,internal_collection_allowed,
        internal_analysis_allowed,customer_summary_allowed,short_quote_allowed
        FROM sources"""
    ).fetchall()
    for row in source_rows:
        explicitly_blocked = (
            str(row["terms_status"] or "") in {"禁止", "不允许"}
            or str(row["commercial_reuse_status"] or "") in {"禁止", "不允许"}
        )
        basis = str(row["permission_basis"] or "").strip()
        already_migrated = bool(basis)
        if not already_migrated:
            basis = (
                "历史明确客户摘要许可配置迁移，仍需定期复核"
                if bool(row["report_use_allowed"])
                else "公开来源必要事实摘要与短引用；不代表商业再利用或全文再分发授权"
            )
        note = str(row["permission_note"] or row["license_note"] or "").strip()
        internal_collection = (
            int(bool(row["internal_collection_allowed"]) and not explicitly_blocked)
            if already_migrated
            else int(bool(row["crawl_allowed"]) and not explicitly_blocked)
        )
        internal_analysis = (
            int(bool(row["internal_analysis_allowed"]) and not explicitly_blocked)
            if already_migrated
            else int(not explicitly_blocked)
        )
        customer_summary = (
            int(bool(row["customer_summary_allowed"]) and not explicitly_blocked)
            if already_migrated
            else int(bool(row["report_use_allowed"]) or not explicitly_blocked)
        )
        short_quote = (
            int(bool(row["short_quote_allowed"]) and not explicitly_blocked)
            if already_migrated
            else int(not explicitly_blocked)
        )
        connection.execute(
            """UPDATE sources SET
            internal_collection_allowed=?,
            internal_analysis_allowed=?,
            customer_summary_allowed=?,
            short_quote_allowed=?,
            fulltext_redistribution_allowed=CASE WHEN fulltext_redistribution_allowed=1 THEN 1 ELSE 0 END,
            raw_data_resale_allowed=CASE WHEN raw_data_resale_allowed=1 THEN 1 ELSE 0 END,
            permission_basis=?,permission_note=?,
            permission_reviewed_at=CASE WHEN permission_reviewed_at='' THEN ? ELSE permission_reviewed_at END
            WHERE source_id=?""",
            (
                internal_collection,
                internal_analysis,
                customer_summary,
                short_quote,
                basis,
                note,
                str(row["last_license_checked_at"] or ""),
                row["source_id"],
            ),
        )
        result["sources_migrated"] += 1

    dirty_documents = connection.execute(
        """SELECT document_id,quality_status,extraction_status,title FROM documents
        WHERE record_state='active' AND (
            quality_status!='合格'
            OR extraction_status NOT IN ('成功','提取成功')
            OR TRIM(title)='' OR title IN ('首页','未提取标题','山东省港口集团官网')
        )"""
    ).fetchall()
    for row in dirty_documents:
        reasons: list[str] = []
        if str(row["quality_status"] or "") != "合格":
            reasons.append(str(row["quality_status"] or "正文质量未通过"))
        if str(row["extraction_status"] or "") not in {"成功", "提取成功"}:
            reasons.append(str(row["extraction_status"] or "正文提取未成功"))
        if str(row["title"] or "").strip() in {"", "首页", "未提取标题", "山东省港口集团官网"}:
            reasons.append("通用、空白或占位标题")
        reason = "；".join(dict.fromkeys(reasons)) or "历史质量门禁未通过"
        connection.execute(
            """UPDATE documents SET record_state='quarantined',quarantine_reason=?,
            quarantined_at=CASE WHEN quarantined_at='' THEN ? ELSE quarantined_at END,
            report_quality_eligible=0,review_status='已隔离',updated_at=?
            WHERE document_id=?""",
            (reason, timestamp, timestamp, row["document_id"]),
        )
        connection.execute("DELETE FROM document_chunks_fts WHERE document_id=?", (row["document_id"],))
        connection.execute("DELETE FROM document_chunks WHERE document_id=?", (row["document_id"],))
        result["documents_quarantined"] += 1

    cursor = connection.execute(
        """UPDATE events SET record_state='quarantined',
        quarantine_reason=COALESCE(NULLIF(quarantine_reason,''),'关联文档已被质量门禁隔离'),
        quarantined_at=CASE WHEN quarantined_at='' THEN ? ELSE quarantined_at END,
        report_eligible=0,review_state='rejected',updated_at=?
        WHERE record_state='active' AND document_id IN (
            SELECT document_id FROM documents WHERE record_state!='active'
        )""",
        (timestamp, timestamp),
    )
    result["events_quarantined"] += max(0, int(cursor.rowcount or 0))

    # Python's hashlib keeps this compatible with SQLite builds lacking sha3().
    for row in connection.execute(
        """SELECT evidence_id,document_id,document_version,quote_text
        FROM event_evidence WHERE document_version_id='' OR quote_hash=''"""
    ).fetchall():
        connection.execute(
            """UPDATE event_evidence SET document_version_id=?,quote_hash=? WHERE evidence_id=?""",
            (
                f"{row['document_id']}:v{int(row['document_version'] or 1)}",
                hashlib.sha256(str(row["quote_text"] or "").encode("utf-8")).hexdigest(),
                row["evidence_id"],
            ),
        )

    result["legacy_archived_reclassified"] = _migrate_archived_operation_results(connection)

    for row in connection.execute("SELECT * FROM crawl_runs ORDER BY started_at").fetchall():
        operation_id = _stable_migration_id("crawl", row["crawl_run_id"])
        status = _normalized_operation_status(row["status"])
        archived_at = str(row["finished_at"] or row["started_at"] or timestamp)
        existing_id, merged = _merge_duplicate_crawl_operations(
            connection,
            str(row["workspace_id"]),
            str(row["crawl_run_id"]),
        )
        result["operation_duplicates_merged"] += merged
        if existing_id:
            operation_id = existing_id
        else:
            connection.execute(
                """INSERT OR IGNORE INTO operation_runs(
                operation_run_id,workspace_id,operation_type,status,started_at,finished_at,
                duration_ms,created_by,input_summary,result_summary,documents_created,
                documents_updated,events_created,skipped_count,failed_count,warning_count,
                error_summary,structured_log_json,archived_at,home_visible,is_archived,metadata_json,
                external_ref_type,external_ref_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    row["workspace_id"],
                    "crawl",
                    status,
                    row["started_at"],
                    row["finished_at"],
                    int(row["duration_ms"] or 0),
                    "legacy_migration",
                    "历史采集运行迁移",
                    f"新增文档{int(row['new_document_count'] or 0)}；新增事件{int(row['new_event_count'] or 0)}",
                    int(row["new_document_count"] or 0),
                    int(row["updated_document_count"] or 0),
                    int(row["new_event_count"] or 0),
                    int(row["skipped_count"] or 0),
                    int(row["failed_count"] or 0),
                    int(bool(str(row["error_summary"] or "").strip())),
                    str(row["error_summary"] or ""),
                    str(row["stage_stats_json"] or "{}"),
                    archived_at,
                    0,
                    1,
                    json.dumps({"migrated_from": "crawl_runs"}, ensure_ascii=False),
                    "crawl_run",
                    row["crawl_run_id"],
                    row["started_at"],
                    row["finished_at"] or row["started_at"],
                ),
            )
        # A crawl can be observed by an idempotent migration while it is still
        # running. Converge migration-owned rows on every pass so a historical
        # "running" snapshot cannot permanently block the durable task queue.
        connection.execute(
            """UPDATE operation_runs SET status=?,finished_at=?,duration_ms=?,
            result_summary=?,documents_created=?,documents_updated=?,events_created=?,
            skipped_count=?,failed_count=?,warning_count=?,error_summary=?,
            structured_log_json=?,archived_at=?,home_visible=0,updated_at=?
            WHERE operation_run_id=? AND created_by='legacy_migration'""",
            (
                status,
                row["finished_at"],
                int(row["duration_ms"] or 0),
                f"新增文档{int(row['new_document_count'] or 0)}；新增事件{int(row['new_event_count'] or 0)}",
                int(row["new_document_count"] or 0),
                int(row["updated_document_count"] or 0),
                int(row["new_event_count"] or 0),
                int(row["skipped_count"] or 0),
                int(row["failed_count"] or 0),
                int(bool(str(row["error_summary"] or "").strip())),
                str(row["error_summary"] or ""),
                str(row["stage_stats_json"] or "{}"),
                archived_at,
                row["finished_at"] or row["started_at"],
                operation_id,
            ),
        )
        result["operations_migrated"] += int(connection.total_changes > 0)

    for row in connection.execute(
        """SELECT report_record_id,report_id,workspace_id,report_title,generated_at,
        event_ids,status FROM reports ORDER BY generated_at"""
    ).fetchall():
        operation_id = _stable_migration_id("report", row["report_record_id"])
        connection.execute(
            """INSERT OR IGNORE INTO operation_runs(
            operation_run_id,workspace_id,operation_type,status,started_at,finished_at,
            created_by,input_summary,result_summary,archived_at,home_visible,metadata_json,
            external_ref_type,external_ref_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                row["workspace_id"],
                "report_generation",
                "succeeded",
                row["generated_at"],
                row["generated_at"],
                "legacy_migration",
                str(row["report_title"] or "历史报告"),
                f"历史报告 {row['report_id']}",
                row["generated_at"],
                0,
                json.dumps({"report_id": row["report_id"], "migrated_from": "reports"}, ensure_ascii=False),
                "report_record",
                row["report_record_id"],
                row["generated_at"],
                row["generated_at"],
            ),
        )
        event_ids: list[str] = []
        try:
            parsed = json.loads(str(row["event_ids"] or "[]"))
            if isinstance(parsed, list):
                event_ids = [str(value) for value in parsed]
        except json.JSONDecodeError:
            event_ids = []
        has_active = False
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            has_active = bool(
                connection.execute(
                    f"SELECT 1 FROM events WHERE event_id IN ({placeholders}) AND record_state='active' LIMIT 1",
                    event_ids,
                ).fetchone()
            )
        if (
            "Acceptance" in str(row["report_title"] or "")
            or (not event_ids and str(row["status"] or "active") == "active")
            or (event_ids and not has_active)
        ):
            cursor = connection.execute(
                """UPDATE reports SET status='obsolete',
                status_note='旧口径或所含事件已被质量门禁隔离，不可用于当前验收或客户交付'
                WHERE report_record_id=? AND status!='obsolete'""",
                (row["report_record_id"],),
            )
            result["reports_obsoleted"] += max(0, int(cursor.rowcount or 0))

    for row in connection.execute(
        """SELECT history_id,workspace_id,action_type,entity_type,entity_id,
        occurred_at,details FROM action_history WHERE action_type!='生成报告'"""
    ).fetchall():
        operation_id = _stable_migration_id("action", row["history_id"])
        operation_type = {
            "人工核验": "manual_review",
            "QA晋升": "qa_promotion",
            "创建": "create",
            "修改": "edit",
            "状态变更": "status_change",
            "移出报告": "report_exclusion",
        }.get(str(row["action_type"]), "business_action")
        connection.execute(
            """INSERT OR IGNORE INTO operation_runs(
            operation_run_id,workspace_id,operation_type,status,started_at,finished_at,
            created_by,input_summary,result_summary,archived_at,home_visible,metadata_json,
            external_ref_type,external_ref_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                row["workspace_id"],
                operation_type,
                "succeeded",
                row["occurred_at"],
                row["occurred_at"],
                "legacy_migration",
                str(row["action_type"]),
                f"{row['entity_type']}：{row['entity_id']}",
                row["occurred_at"],
                0,
                str(row["details"] or "{}"),
                "action_history",
                row["history_id"],
                row["occurred_at"],
                row["occurred_at"],
            ),
        )

    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_state ON documents(workspace_id,record_state,is_current)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_state ON events(workspace_id,record_state,event_date)"
    )
    return result


def initialize_database(path: Optional[PathLike] = None) -> Path:
    target = database_path(path or DEFAULT_DB_PATH)
    initialize_workspace_database(target)
    with connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL,
                organization TEXT NOT NULL DEFAULT '',
                domain TEXT NOT NULL DEFAULT '',
                homepage_url TEXT NOT NULL DEFAULT '',
                list_page_url TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '其他',
                category_hint TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                adapter_type TEXT NOT NULL DEFAULT 'generic_html',
                adapter_config_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 0,
                crawl_allowed INTEGER NOT NULL DEFAULT 0,
                robots_status TEXT NOT NULL DEFAULT '未检查',
                terms_status TEXT NOT NULL DEFAULT '未检查',
                commercial_reuse_status TEXT NOT NULL DEFAULT '未明确',
                license_note TEXT NOT NULL DEFAULT '',
                report_use_allowed INTEGER NOT NULL DEFAULT 0,
                rate_limit_seconds REAL NOT NULL DEFAULT 2.0,
                max_pages_per_run INTEGER NOT NULL DEFAULT 1,
                max_articles_per_run INTEGER NOT NULL DEFAULT 5,
                business_value_level TEXT NOT NULL DEFAULT '中',
                primary_users TEXT NOT NULL DEFAULT '[]',
                information_types TEXT NOT NULL DEFAULT '[]',
                list_stability TEXT NOT NULL DEFAULT '未验收',
                content_stability TEXT NOT NULL DEFAULT '未验收',
                last_license_checked_at TEXT NOT NULL DEFAULT '',
                last_crawled_at TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                template_version TEXT NOT NULL DEFAULT '',
                template_updated_at TEXT NOT NULL DEFAULT '',
                last_template_sync_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, domain, list_page_url)
            );

            CREATE TABLE IF NOT EXISTS crawl_runs (
                crawl_run_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                new_document_count INTEGER NOT NULL DEFAULT 0,
                updated_document_count INTEGER NOT NULL DEFAULT 0,
                new_event_count INTEGER NOT NULL DEFAULT 0,
                new_opportunity_count INTEGER NOT NULL DEFAULT 0,
                error_summary TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS crawl_source_runs (
                source_run_id TEXT PRIMARY KEY,
                crawl_run_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                new_document_count INTEGER NOT NULL DEFAULT 0,
                updated_document_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                http_error_count INTEGER NOT NULL DEFAULT 0,
                tls_error_count INTEGER NOT NULL DEFAULT 0,
                timeout_count INTEGER NOT NULL DEFAULT 0,
                javascript_blocked_count INTEGER NOT NULL DEFAULT 0,
                quality_failed_count INTEGER NOT NULL DEFAULT 0,
                qualified_document_count INTEGER NOT NULL DEFAULT 0,
                pdf_discovered_count INTEGER NOT NULL DEFAULT 0,
                pdf_success_count INTEGER NOT NULL DEFAULT 0,
                pdf_rejected_count INTEGER NOT NULL DEFAULT 0,
                new_event_count INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(crawl_run_id, source_id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                original_url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                raw_html_path TEXT NOT NULL DEFAULT '',
                raw_file_path TEXT NOT NULL DEFAULT '',
                document_format TEXT NOT NULL DEFAULT 'html',
                mime_type TEXT NOT NULL DEFAULT 'text/html',
                source_file_url TEXT NOT NULL DEFAULT '',
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                file_sha256 TEXT NOT NULL DEFAULT '',
                http_etag TEXT NOT NULL DEFAULT '',
                http_last_modified TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                last_content_changed_at TEXT NOT NULL DEFAULT '',
                last_http_status INTEGER NOT NULL DEFAULT 0,
                cleaned_text TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                extraction_status TEXT NOT NULL,
                extraction_quality TEXT NOT NULL DEFAULT '',
                ai_status TEXT NOT NULL DEFAULT '待AI处理',
                http_status INTEGER NOT NULL DEFAULT 0,
                document_version INTEGER NOT NULL DEFAULT 1,
                previous_document_id TEXT NOT NULL DEFAULT '',
                is_current INTEGER NOT NULL DEFAULT 1,
                review_status TEXT NOT NULL DEFAULT '待审核',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES sources(source_id)
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT '',
                document_id TEXT NOT NULL DEFAULT '',
                event_date TEXT NOT NULL DEFAULT '',
                collected_at TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                impact TEXT NOT NULL DEFAULT '',
                affected_area TEXT NOT NULL DEFAULT '',
                affected_period TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '其他',
                source_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '待核实',
                related_event_id TEXT NOT NULL DEFAULT '',
                analyst_note TEXT NOT NULL DEFAULT '',
                extraction_method TEXT NOT NULL DEFAULT 'rules',
                extraction_confidence REAL NOT NULL DEFAULT 0,
                ai_generated INTEGER NOT NULL DEFAULT 0,
                human_verified INTEGER NOT NULL DEFAULT 0,
                verified_at TEXT NOT NULL DEFAULT '',
                report_eligible INTEGER NOT NULL DEFAULT 0,
                active_for_internal_use INTEGER NOT NULL DEFAULT 0,
                eligible_for_customer_report INTEGER NOT NULL DEFAULT 0,
                duplicate_level TEXT NOT NULL DEFAULT '未发现明显重复',
                raw_risk_score INTEGER NOT NULL DEFAULT 0,
                historical_risk_score INTEGER NOT NULL DEFAULT 0,
                current_priority_score INTEGER NOT NULL DEFAULT 0,
                source_confidence INTEGER NOT NULL DEFAULT 0,
                opportunity_score INTEGER NOT NULL DEFAULT 0,
                risk_level TEXT NOT NULL DEFAULT '低',
                opportunity_level TEXT NOT NULL DEFAULT '低',
                matched_terms TEXT NOT NULL DEFAULT '',
                score_explanation TEXT NOT NULL DEFAULT '',
                recommended_action TEXT NOT NULL DEFAULT '',
                business_value TEXT NOT NULL DEFAULT '未评估',
                value_reason TEXT NOT NULL DEFAULT '',
                evidence_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(document_id)
            );

            CREATE TABLE IF NOT EXISTS document_chunks (
                chunk_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT '',
                document_id TEXT NOT NULL,
                event_id TEXT NOT NULL DEFAULT '',
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                token_or_character_count INTEGER NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                embedding_status TEXT NOT NULL DEFAULT '待向量化',
                created_at TEXT NOT NULL,
                UNIQUE(document_id, chunk_index),
                FOREIGN KEY(document_id) REFERENCES documents(document_id)
            );

            CREATE TABLE IF NOT EXISTS qa_logs (
                qa_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                retrieved_document_ids TEXT NOT NULL DEFAULT '[]',
                citations_json TEXT NOT NULL DEFAULT '[]',
                model_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_call_logs (
                ai_call_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT '',
                document_id TEXT NOT NULL DEFAULT '',
                task_type TEXT NOT NULL,
                model_name TEXT NOT NULL DEFAULT '',
                input_characters INTEGER NOT NULL DEFAULT 0,
                output_characters INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                error_type TEXT NOT NULL DEFAULT '',
                safe_error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_conversations (
                conversation_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '新对话',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                tool_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES agent_conversations(conversation_id)
            );

            CREATE TABLE IF NOT EXISTS agent_approvals (
                approval_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT '待确认',
                created_at TEXT NOT NULL,
                decided_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS event_evidence (
                evidence_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                quote_text TEXT NOT NULL,
                document_id TEXT NOT NULL,
                document_version INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                start_offset INTEGER NOT NULL DEFAULT -1,
                end_offset INTEGER NOT NULL DEFAULT -1,
                verification_status TEXT NOT NULL DEFAULT '未定位',
                verified_at TEXT NOT NULL DEFAULT '',
                normalization_method TEXT NOT NULL DEFAULT 'none',
                failure_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(event_id),
                FOREIGN KEY(document_id) REFERENCES documents(document_id)
            );

            CREATE TABLE IF NOT EXISTS event_reviews (
                review_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                reviewer_type TEXT NOT NULL,
                reviewer_name TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL,
                review_method TEXT NOT NULL DEFAULT '',
                review_version TEXT NOT NULL DEFAULT '',
                reviewer_note TEXT NOT NULL DEFAULT '',
                checklist_json TEXT NOT NULL DEFAULT '{}',
                decision TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            );

            CREATE TABLE IF NOT EXISTS promotion_history (
                promotion_id TEXT PRIMARY KEY,
                qa_run_id TEXT NOT NULL,
                source_workspace_id TEXT NOT NULL,
                target_workspace_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                source_document_id TEXT NOT NULL,
                target_event_id TEXT NOT NULL,
                target_document_id TEXT NOT NULL,
                reviewer_type TEXT NOT NULL,
                reviewer_name TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                promoted_at TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS qa_label_reviews (
                review_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                event_id TEXT NOT NULL DEFAULT '',
                reviewer_type TEXT NOT NULL,
                reviewer_name TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                review_method TEXT NOT NULL,
                review_version TEXT NOT NULL,
                reviewer_note TEXT NOT NULL DEFAULT '',
                decision TEXT NOT NULL,
                checklist_json TEXT NOT NULL DEFAULT '{}',
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                changed_fields_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS qa_review_progress (
                workspace_id TEXT PRIMARY KEY,
                current_document_id TEXT NOT NULL DEFAULT '',
                filter_status TEXT NOT NULL DEFAULT '未审核',
                reviewer_type TEXT NOT NULL DEFAULT 'human_user',
                reviewer_name TEXT NOT NULL DEFAULT '',
                draft_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customer_feedback (
                feedback_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                feedback_date TEXT NOT NULL,
                customer_label TEXT NOT NULL DEFAULT '',
                feedback_text TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT 'human_user',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS report_delivery_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                report_record_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT '',
                pilot_id TEXT NOT NULL DEFAULT '',
                report_version INTEGER NOT NULL,
                report_mode TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                data_cutoff_at TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                delivery_due_at TEXT NOT NULL DEFAULT '',
                delivered_at TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL DEFAULT 'ready_for_delivery',
                event_ids_json TEXT NOT NULL DEFAULT '[]',
                event_versions_json TEXT NOT NULL DEFAULT '{}',
                source_urls_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                reviewer_json TEXT NOT NULL DEFAULT '[]',
                file_hashes_json TEXT NOT NULL DEFAULT '{}',
                preflight_json TEXT NOT NULL DEFAULT '{}',
                immutable_payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, report_record_id)
            );

            CREATE TABLE IF NOT EXISTS report_delivery_amendments (
                amendment_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                previous_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                changed_by TEXT NOT NULL DEFAULT 'human_user',
                created_at TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES report_delivery_snapshots(snapshot_id)
            );

            CREATE TABLE IF NOT EXISTS restore_drills (
                restore_drill_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                backup_file TEXT NOT NULL,
                restore_target TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                quick_check TEXT NOT NULL DEFAULT '',
                production_counts_json TEXT NOT NULL DEFAULT '{}',
                restored_counts_json TEXT NOT NULL DEFAULT '{}',
                count_differences_json TEXT NOT NULL DEFAULT '{}',
                report_file TEXT NOT NULL DEFAULT '',
                error_summary TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT 'human_user',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS operation_runs (
                operation_run_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT 'local_user',
                ui_session_id TEXT NOT NULL DEFAULT '',
                input_summary TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                documents_created INTEGER NOT NULL DEFAULT 0,
                documents_updated INTEGER NOT NULL DEFAULT 0,
                events_created INTEGER NOT NULL DEFAULT 0,
                events_updated INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                error_summary TEXT NOT NULL DEFAULT '',
                log_path TEXT NOT NULL DEFAULT '',
                structured_log_json TEXT NOT NULL DEFAULT '[]',
                parent_run_id TEXT NOT NULL DEFAULT '',
                archived_at TEXT NOT NULL DEFAULT '',
                home_visible INTEGER NOT NULL DEFAULT 1,
                is_archived INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                external_ref_type TEXT NOT NULL DEFAULT '',
                external_ref_id TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                heartbeat_at TEXT NOT NULL DEFAULT '',
                current_stage TEXT NOT NULL DEFAULT '',
                current_item TEXT NOT NULL DEFAULT '',
                completed_items INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                isolated_count INTEGER NOT NULL DEFAULT 0,
                worker_pid INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS technical_logs (
                technical_log_id TEXT PRIMARY KEY,
                operation_run_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                component TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS document_quality_reevaluations (
                reevaluation_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                previous_record_state TEXT NOT NULL,
                previous_quality_status TEXT NOT NULL,
                previous_quarantine_reason TEXT NOT NULL DEFAULT '',
                reevaluated_quality_status TEXT NOT NULL,
                reevaluation_result_json TEXT NOT NULL DEFAULT '{}',
                decision TEXT NOT NULL DEFAULT 'dry_run',
                confirmed_by TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(workspace_id, enabled, crawl_allowed);
            CREATE INDEX IF NOT EXISTS idx_documents_current ON documents(workspace_id, canonical_url, is_current);
            CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(workspace_id, content_hash, is_current);
            CREATE INDEX IF NOT EXISTS idx_events_period ON events(workspace_id, event_date, human_verified, report_eligible);
            CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id, chunk_index);
            CREATE INDEX IF NOT EXISTS idx_agent_conversations_workspace ON agent_conversations(workspace_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_conversation ON agent_messages(workspace_id, conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_approvals_pending ON agent_approvals(workspace_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_evidence_event ON event_evidence(event_id, verification_status);
            CREATE INDEX IF NOT EXISTS idx_reviews_event ON event_reviews(workspace_id, event_id, reviewed_at);
            CREATE INDEX IF NOT EXISTS idx_qa_label_reviews_document
                ON qa_label_reviews(workspace_id, document_id, reviewed_at);
            CREATE INDEX IF NOT EXISTS idx_promotion_target ON promotion_history(target_workspace_id, promoted_at);
            CREATE INDEX IF NOT EXISTS idx_source_runs_workspace
                ON crawl_source_runs(workspace_id, source_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_customer_feedback_workspace
                ON customer_feedback(workspace_id, feedback_date);
            CREATE INDEX IF NOT EXISTS idx_delivery_snapshots_workspace
                ON report_delivery_snapshots(workspace_id, generated_at);
            CREATE INDEX IF NOT EXISTS idx_delivery_amendments_snapshot
                ON report_delivery_amendments(snapshot_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_restore_drills_workspace
                ON restore_drills(workspace_id, finished_at);
            CREATE INDEX IF NOT EXISTS idx_operations_workspace
                ON operation_runs(workspace_id, status, started_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_operations_external
                ON operation_runs(workspace_id, external_ref_type, external_ref_id)
                WHERE external_ref_type!='' AND external_ref_id!='';
            CREATE INDEX IF NOT EXISTS idx_technical_logs_workspace
                ON technical_logs(workspace_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_quality_reevaluation_document
                ON document_quality_reevaluations(workspace_id, document_id, created_at);
            """
        )
        try:
            connection.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
                    chunk_id UNINDEXED, workspace_id UNINDEXED, document_id UNINDEXED,
                    event_id UNINDEXED, chunk_text, metadata_json UNINDEXED, tokenize='unicode61'
                )"""
            )
        except sqlite3.OperationalError:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS document_chunks_fts(
                    chunk_id TEXT PRIMARY KEY, workspace_id TEXT, document_id TEXT,
                    event_id TEXT, chunk_text TEXT, metadata_json TEXT
                )"""
            )
        _add_columns(
            connection,
            "reports",
            {
                "document_ids": "TEXT NOT NULL DEFAULT '[]'",
                "report_version": "INTEGER NOT NULL DEFAULT 1",
                "docx_path": "TEXT NOT NULL DEFAULT ''",
                "html_path": "TEXT NOT NULL DEFAULT ''",
                "excel_path": "TEXT NOT NULL DEFAULT ''",
                "client_name": "TEXT NOT NULL DEFAULT ''",
                "report_mode": "TEXT NOT NULL DEFAULT '标准报告'",
                "analysis_model": "TEXT NOT NULL DEFAULT 'deterministic-rules'",
            },
        )
        _add_columns(
            connection,
            "documents",
            {
                "ai_status": "TEXT NOT NULL DEFAULT '待AI处理'",
                "quality_status": "TEXT NOT NULL DEFAULT '未检查'",
                "quality_metrics_json": "TEXT NOT NULL DEFAULT '{}'",
                "report_quality_eligible": "INTEGER NOT NULL DEFAULT 0",
                "raw_file_path": "TEXT NOT NULL DEFAULT ''",
                "document_format": "TEXT NOT NULL DEFAULT 'html'",
                "mime_type": "TEXT NOT NULL DEFAULT 'text/html'",
                "source_file_url": "TEXT NOT NULL DEFAULT ''",
                "file_size_bytes": "INTEGER NOT NULL DEFAULT 0",
                "file_sha256": "TEXT NOT NULL DEFAULT ''",
                "http_etag": "TEXT NOT NULL DEFAULT ''",
                "http_last_modified": "TEXT NOT NULL DEFAULT ''",
                "last_checked_at": "TEXT NOT NULL DEFAULT ''",
                "last_content_changed_at": "TEXT NOT NULL DEFAULT ''",
                "last_http_status": "INTEGER NOT NULL DEFAULT 0",
                "qa_run_id": "TEXT NOT NULL DEFAULT ''",
                "promotion_source_document_id": "TEXT NOT NULL DEFAULT ''",
                "record_state": "TEXT NOT NULL DEFAULT 'active'",
                "quarantine_reason": "TEXT NOT NULL DEFAULT ''",
                "quarantined_at": "TEXT NOT NULL DEFAULT ''",
                "restored_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "events",
            {
                "business_value": "TEXT NOT NULL DEFAULT '未评估'",
                "value_reason": "TEXT NOT NULL DEFAULT ''",
                "evidence_verified": "INTEGER NOT NULL DEFAULT 0",
                "reviewer_type": "TEXT NOT NULL DEFAULT 'unknown'",
                "reviewer_name": "TEXT NOT NULL DEFAULT ''",
                "review_method": "TEXT NOT NULL DEFAULT ''",
                "review_version": "TEXT NOT NULL DEFAULT ''",
                "reviewer_note": "TEXT NOT NULL DEFAULT ''",
                "qa_run_id": "TEXT NOT NULL DEFAULT ''",
                "promotion_source_event_id": "TEXT NOT NULL DEFAULT ''",
                "record_state": "TEXT NOT NULL DEFAULT 'active'",
                "quarantine_reason": "TEXT NOT NULL DEFAULT ''",
                "quarantined_at": "TEXT NOT NULL DEFAULT ''",
                "restored_at": "TEXT NOT NULL DEFAULT ''",
                "review_state": "TEXT NOT NULL DEFAULT 'pending'",
                "requires_second_review": "INTEGER NOT NULL DEFAULT 0",
                "involved_entities": "TEXT NOT NULL DEFAULT ''",
                "manual_risk_level": "TEXT NOT NULL DEFAULT ''",
                "manual_keywords": "TEXT NOT NULL DEFAULT ''",
                "active_for_internal_use": "INTEGER NOT NULL DEFAULT 0",
                "eligible_for_customer_report": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _add_columns(
            connection,
            "event_evidence",
            {
                "document_version_id": "TEXT NOT NULL DEFAULT ''",
                "quote_hash": "TEXT NOT NULL DEFAULT ''",
                "candidate_start_offset": "INTEGER NOT NULL DEFAULT -1",
                "candidate_end_offset": "INTEGER NOT NULL DEFAULT -1",
                "candidate_text": "TEXT NOT NULL DEFAULT ''",
                "candidate_score": "REAL NOT NULL DEFAULT 0",
                "repair_status": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "event_reviews",
            {
                "before_json": "TEXT NOT NULL DEFAULT '{}'",
                "after_json": "TEXT NOT NULL DEFAULT '{}'",
                "changed_fields_json": "TEXT NOT NULL DEFAULT '[]'",
                "requires_second_review": "INTEGER NOT NULL DEFAULT 0",
                "review_stage": "TEXT NOT NULL DEFAULT 'first_review'",
            },
        )
        _add_columns(
            connection,
            "ai_call_logs",
            {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "safe_error_message": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "crawl_runs",
            {
                "stage_stats_json": "TEXT NOT NULL DEFAULT '{}'",
                "duration_ms": "INTEGER NOT NULL DEFAULT 0",
                "api_call_count": "INTEGER NOT NULL DEFAULT 0",
                "api_input_characters": "INTEGER NOT NULL DEFAULT 0",
                "api_output_characters": "INTEGER NOT NULL DEFAULT 0",
                "actual_models_json": "TEXT NOT NULL DEFAULT '[]'",
                "estimated_usage": "TEXT NOT NULL DEFAULT ''",
                "report_candidate_count": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
                "out_of_range_count": "INTEGER NOT NULL DEFAULT 0",
                "encoding_blocked_count": "INTEGER NOT NULL DEFAULT 0",
                "noise_blocked_count": "INTEGER NOT NULL DEFAULT 0",
                "pdf_discovered_count": "INTEGER NOT NULL DEFAULT 0",
                "pdf_success_count": "INTEGER NOT NULL DEFAULT 0",
                "pdf_rejected_count": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _add_columns(
            connection,
            "sources",
            {
                "health_status": "TEXT NOT NULL DEFAULT '未验收'",
                "last_health_check_at": "TEXT NOT NULL DEFAULT ''",
                "health_details_json": "TEXT NOT NULL DEFAULT '{}'",
                "business_value_level": "TEXT NOT NULL DEFAULT '中'",
                "primary_users": "TEXT NOT NULL DEFAULT '[]'",
                "information_types": "TEXT NOT NULL DEFAULT '[]'",
                "list_stability": "TEXT NOT NULL DEFAULT '未验收'",
                "content_stability": "TEXT NOT NULL DEFAULT '未验收'",
                "internal_collection_allowed": "INTEGER NOT NULL DEFAULT 1",
                "internal_analysis_allowed": "INTEGER NOT NULL DEFAULT 1",
                "customer_summary_allowed": "INTEGER NOT NULL DEFAULT 0",
                "short_quote_allowed": "INTEGER NOT NULL DEFAULT 0",
                "fulltext_redistribution_allowed": "INTEGER NOT NULL DEFAULT 0",
                "raw_data_resale_allowed": "INTEGER NOT NULL DEFAULT 0",
                "permission_basis": "TEXT NOT NULL DEFAULT ''",
                "permission_note": "TEXT NOT NULL DEFAULT ''",
                "permission_reviewed_at": "TEXT NOT NULL DEFAULT ''",
                "operational_status": "TEXT NOT NULL DEFAULT 'degraded'",
                "template_version": "TEXT NOT NULL DEFAULT ''",
                "template_updated_at": "TEXT NOT NULL DEFAULT ''",
                "last_template_sync_at": "TEXT NOT NULL DEFAULT ''",
                "public_access_status": "TEXT NOT NULL DEFAULT '未复核'",
                "access_frequency_compliant": "TEXT NOT NULL DEFAULT '未复核'",
                "personal_information_status": "TEXT NOT NULL DEFAULT '未复核'",
                "important_data_status": "TEXT NOT NULL DEFAULT '未复核'",
                "compliance_reviewed_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "customer_feedback",
            {
                "report_or_alert_id": "TEXT NOT NULL DEFAULT ''",
                "viewed": "TEXT NOT NULL DEFAULT '未记录'",
                "already_known": "TEXT NOT NULL DEFAULT '未记录'",
                "action_taken": "TEXT NOT NULL DEFAULT '未记录'",
                "time_saved": "TEXT NOT NULL DEFAULT '未记录'",
                "false_positive": "TEXT NOT NULL DEFAULT '未记录'",
                "omission_reported": "TEXT NOT NULL DEFAULT '未记录'",
                "willing_to_continue": "TEXT NOT NULL DEFAULT '未记录'",
                "feedback_note": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "technical_logs",
            {
                "resolved_at": "TEXT NOT NULL DEFAULT ''",
                "resolved_by": "TEXT NOT NULL DEFAULT ''",
            },
        )
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS prevent_delivery_snapshot_payload_update
            BEFORE UPDATE OF
                report_record_id,report_id,client_id,pilot_id,report_version,report_mode,
                start_date,end_date,data_cutoff_at,generated_at,event_ids_json,
                event_versions_json,source_urls_json,evidence_json,reviewer_json,
                file_hashes_json,preflight_json,immutable_payload_json,payload_hash
            ON report_delivery_snapshots
            BEGIN
                SELECT RAISE(ABORT, '交付快照内容不可覆盖；请生成新报告版本');
            END;
            """
        )
        _add_columns(
            connection,
            "crawl_source_runs",
            {
                "qualified_document_count": "INTEGER NOT NULL DEFAULT 0",
                "pdf_discovered_count": "INTEGER NOT NULL DEFAULT 0",
                "pdf_success_count": "INTEGER NOT NULL DEFAULT 0",
                "pdf_rejected_count": "INTEGER NOT NULL DEFAULT 0",
                "new_event_count": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _add_columns(
            connection,
            "operation_runs",
            {
                "is_archived": "INTEGER NOT NULL DEFAULT 0",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
                "current_stage": "TEXT NOT NULL DEFAULT ''",
                "current_item": "TEXT NOT NULL DEFAULT ''",
                "completed_items": "INTEGER NOT NULL DEFAULT 0",
                "total_items": "INTEGER NOT NULL DEFAULT 0",
                "success_count": "INTEGER NOT NULL DEFAULT 0",
                "isolated_count": "INTEGER NOT NULL DEFAULT 0",
                "worker_pid": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        connection.execute(
            """UPDATE operation_runs SET is_archived=1
            WHERE home_visible=0 AND archived_at!=''"""
        )
        _add_columns(
            connection,
            "reports",
            {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "status_note": "TEXT NOT NULL DEFAULT ''",
                "export_mode": "TEXT NOT NULL DEFAULT 'internal_brief'",
                "permission_risk_count": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        # A legacy boolean flag without independent reviewer provenance is not
        # sufficient to claim human verification or customer-report eligibility.
        connection.execute(
            """UPDATE events SET human_verified=0,report_eligible=0
            WHERE human_verified=1 AND reviewer_type NOT IN ('human_user','industry_reviewer')"""
        )
        connection.execute(
            """UPDATE events SET active_for_internal_use=0,eligible_for_customer_report=0
            WHERE human_verified=0 OR reviewer_type NOT IN ('human_user','industry_reviewer')"""
        )
        _apply_personal_workbench_migration_connection(connection)
        from evidence_binding import migrate_legacy_event_evidence

        migrate_legacy_event_evidence(connection)
        _harden_unchecked_documents_connection(connection)
        _recover_stale_runs_connection(connection)
    return target


def recover_stale_crawl_runs(path: Optional[PathLike] = None, timeout_minutes: int = 30) -> int:
    target = database_path(path or DEFAULT_DB_PATH)
    if not target.exists():
        initialize_database(target)
        return 0
    with connect(target) as connection:
        return _recover_stale_runs_connection(connection, timeout_minutes)


def upsert_source(values: Mapping[str, object], path: Optional[PathLike] = None) -> str:
    initialize_database(path)
    source_id = str(values.get("source_id") or new_id("SRC"))
    timestamp = now_iso()
    explicitly_blocked = (
        str(values.get("terms_status") or "") in {"禁止", "不允许"}
        or str(values.get("commercial_reuse_status") or "") in {"禁止", "不允许"}
    )
    row = {
        "source_id": source_id,
        "workspace_id": str(values.get("workspace_id") or ""),
        "source_name": str(values.get("source_name") or "").strip(),
        "organization": str(values.get("organization") or "").strip(),
        "domain": str(values.get("domain") or "").strip().lower(),
        "homepage_url": str(values.get("homepage_url") or "").strip(),
        "list_page_url": str(values.get("list_page_url") or "").strip(),
        "source_type": str(values.get("source_type") or "其他").strip(),
        "category_hint": str(values.get("category_hint") or "").strip(),
        "region": str(values.get("region") or "").strip(),
        "adapter_type": str(values.get("adapter_type") or "generic_html").strip(),
        "adapter_config_json": json.dumps(values.get("adapter_config") or {}, ensure_ascii=False),
        "enabled": int(bool(values.get("enabled", False))),
        "crawl_allowed": int(bool(values.get("crawl_allowed", False))),
        "robots_status": str(values.get("robots_status") or "未检查"),
        "terms_status": str(values.get("terms_status") or "未检查"),
        "commercial_reuse_status": str(values.get("commercial_reuse_status") or "未明确"),
        "license_note": str(values.get("license_note") or ""),
        "report_use_allowed": int(bool(values.get("report_use_allowed", False))),
        "internal_collection_allowed": int(
            bool(values.get("internal_collection_allowed", values.get("crawl_allowed", False)))
            and not explicitly_blocked
        ),
        "internal_analysis_allowed": int(
            bool(values.get("internal_analysis_allowed", True)) and not explicitly_blocked
        ),
        "customer_summary_allowed": int(
            bool(values.get("customer_summary_allowed", not explicitly_blocked))
            and not explicitly_blocked
        ),
        "short_quote_allowed": int(
            bool(values.get("short_quote_allowed", not explicitly_blocked))
            and not explicitly_blocked
        ),
        "fulltext_redistribution_allowed": int(
            bool(values.get("fulltext_redistribution_allowed", False))
        ),
        "raw_data_resale_allowed": int(bool(values.get("raw_data_resale_allowed", False))),
        "permission_basis": str(
            values.get("permission_basis")
            or "公开来源必要事实摘要与短引用；不代表商业再利用或全文再分发授权"
        ),
        "permission_note": str(values.get("permission_note") or values.get("license_note") or ""),
        "permission_reviewed_at": str(
            values.get("permission_reviewed_at") or values.get("last_license_checked_at") or ""
        ),
        "rate_limit_seconds": max(float(values.get("rate_limit_seconds") or 2.0), 2.0),
        "max_pages_per_run": max(1, int(values.get("max_pages_per_run") or 1)),
        "max_articles_per_run": max(1, int(values.get("max_articles_per_run") or 5)),
        "business_value_level": str(values.get("business_value_level") or "中"),
        "primary_users": json.dumps(values.get("primary_users") or [], ensure_ascii=False),
        "information_types": json.dumps(values.get("information_types") or [], ensure_ascii=False),
        "list_stability": str(values.get("list_stability") or "未验收"),
        "content_stability": str(values.get("content_stability") or "未验收"),
        "last_license_checked_at": str(values.get("last_license_checked_at") or ""),
        "created_at": str(values.get("created_at") or timestamp),
        "updated_at": timestamp,
    }
    if not row["source_name"]:
        raise ValueError("source_name 不能为空")
    if row["enabled"] and (not row["domain"] or not row["list_page_url"] or row["domain"].endswith(".invalid")):
        raise ValueError("启用自动采集前必须填写白名单域名和列表页")
    with transaction(path) as connection:
        existing = connection.execute("SELECT source_id, created_at FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if existing:
            row["created_at"] = existing["created_at"]
        connection.execute(
            """INSERT INTO sources(
                source_id,workspace_id,source_name,organization,domain,homepage_url,list_page_url,
                source_type,category_hint,region,adapter_type,adapter_config_json,enabled,crawl_allowed,
                robots_status,terms_status,commercial_reuse_status,license_note,report_use_allowed,
                internal_collection_allowed,internal_analysis_allowed,customer_summary_allowed,
                short_quote_allowed,fulltext_redistribution_allowed,raw_data_resale_allowed,
                permission_basis,permission_note,permission_reviewed_at,
                rate_limit_seconds,max_pages_per_run,max_articles_per_run,business_value_level,primary_users,
                information_types,list_stability,content_stability,last_license_checked_at,created_at,updated_at
            ) VALUES(
                :source_id,:workspace_id,:source_name,:organization,:domain,:homepage_url,:list_page_url,
                :source_type,:category_hint,:region,:adapter_type,:adapter_config_json,:enabled,:crawl_allowed,
                :robots_status,:terms_status,:commercial_reuse_status,:license_note,:report_use_allowed,
                :internal_collection_allowed,:internal_analysis_allowed,:customer_summary_allowed,
                :short_quote_allowed,:fulltext_redistribution_allowed,:raw_data_resale_allowed,
                :permission_basis,:permission_note,:permission_reviewed_at,
                :rate_limit_seconds,:max_pages_per_run,:max_articles_per_run,:business_value_level,:primary_users,
                :information_types,:list_stability,:content_stability,:last_license_checked_at,:created_at,:updated_at
            ) ON CONFLICT(source_id) DO UPDATE SET
                workspace_id=excluded.workspace_id,source_name=excluded.source_name,organization=excluded.organization,
                domain=excluded.domain,homepage_url=excluded.homepage_url,list_page_url=excluded.list_page_url,
                source_type=excluded.source_type,category_hint=excluded.category_hint,region=excluded.region,
                adapter_type=excluded.adapter_type,adapter_config_json=excluded.adapter_config_json,
                enabled=excluded.enabled,crawl_allowed=excluded.crawl_allowed,robots_status=excluded.robots_status,
                terms_status=excluded.terms_status,commercial_reuse_status=excluded.commercial_reuse_status,
                license_note=excluded.license_note,report_use_allowed=excluded.report_use_allowed,
                internal_collection_allowed=excluded.internal_collection_allowed,
                internal_analysis_allowed=excluded.internal_analysis_allowed,
                customer_summary_allowed=excluded.customer_summary_allowed,
                short_quote_allowed=excluded.short_quote_allowed,
                fulltext_redistribution_allowed=excluded.fulltext_redistribution_allowed,
                raw_data_resale_allowed=excluded.raw_data_resale_allowed,
                permission_basis=excluded.permission_basis,permission_note=excluded.permission_note,
                permission_reviewed_at=excluded.permission_reviewed_at,
                rate_limit_seconds=excluded.rate_limit_seconds,max_pages_per_run=excluded.max_pages_per_run,
                max_articles_per_run=excluded.max_articles_per_run,business_value_level=excluded.business_value_level,
                primary_users=excluded.primary_users,information_types=excluded.information_types,
                list_stability=excluded.list_stability,content_stability=excluded.content_stability,
                last_license_checked_at=excluded.last_license_checked_at,
                updated_at=excluded.updated_at""",
            row,
        )
    return source_id


def list_sources(workspace_id: str = "", path: Optional[PathLike] = None, enabled_only: bool = False) -> list[dict[str, object]]:
    initialize_database(path)
    query = "SELECT * FROM sources WHERE workspace_id=?"
    params: list[object] = [workspace_id]
    if enabled_only:
        query += " AND enabled=1"
    query += " ORDER BY source_name"
    with connect(path) as connection:
        rows = connection.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["adapter_config"] = json.loads(item.pop("adapter_config_json") or "{}")
        except json.JSONDecodeError:
            item["adapter_config"] = {}
        for field in ("primary_users", "information_types"):
            try:
                item[field] = json.loads(item.get(field) or "[]")
            except json.JSONDecodeError:
                item[field] = []
        result.append(item)
    return result


def seed_source_entries(workspace_id: str, path: Optional[PathLike] = None) -> int:
    """Create editable, disabled source entry placeholders; no event content is seeded."""
    entries = json.loads(SOURCE_CATALOG_PATH.read_text(encoding="utf-8"))
    existing_names = {str(item["source_name"]) for item in list_sources(workspace_id, path)}
    # Seed only for an entirely new workspace. Once the user starts managing the
    # catalogue, deleted placeholders must stay deleted instead of reappearing.
    if existing_names:
        return 0
    created = 0
    for index, entry in enumerate(entries, 1):
        name = str(entry["source_name"])
        if name in existing_names:
            continue
        upsert_source(
            {
                "workspace_id": workspace_id,
                "source_name": name,
                "organization": entry.get("organization", ""),
                "source_type": entry.get("source_type", "其他"),
                "category_hint": entry.get("category_hint", ""),
                "adapter_type": entry.get("adapter_type", "generic_html"),
                "domain": f"unconfigured-{index}.invalid",
                "commercial_reuse_status": entry.get("commercial_reuse_status", "未明确"),
                "license_note": "来源入口占位；启用采集前须由用户核对官网、robots、条款和许可。",
                "enabled": False,
                "crawl_allowed": False,
                "report_use_allowed": False,
            },
            path,
        )
        created += 1
    return created


def load_recommended_sources(config_path: Optional[PathLike] = None) -> list[dict[str, object]]:
    target = Path(config_path or DEFAULT_SOURCES_PATH)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("推荐数据源模板必须是JSON数组")
    return [dict(item) for item in payload if isinstance(item, dict)]


def recommended_source_issues(template: Mapping[str, object]) -> list[str]:
    issues: list[str] = []
    labels = {
        "source_name": "来源名称", "organization": "机构", "domain": "白名单域名",
        "homepage_url": "首页地址", "list_page_url": "列表页地址",
        "source_type": "来源类别", "adapter_type": "适配器类型",
    }
    for field, label in labels.items():
        if not str(template.get(field) or "").strip():
            issues.append(f"缺少{label}")
    domain = str(template.get("domain") or "").lower().strip()
    for field, label in (("homepage_url", "首页"), ("list_page_url", "列表页")):
        parsed = urlparse(str(template.get(field) or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            issues.append(f"{label}必须是HTTP/HTTPS地址")
        elif domain and not (parsed.hostname == domain or parsed.hostname.endswith("." + domain)):
            issues.append(f"{label}不属于白名单域名")
    config = template.get("adapter_config")
    config = config if isinstance(config, Mapping) else {}
    adapter_type = str(template.get("adapter_type") or "generic_html").strip().lower()
    # RSS/API adapters do not use HTML article selectors. Only require these
    # fields for list-page HTML adapters so a valid non-HTML custom source is
    # not incorrectly labelled as an incomplete template.
    if adapter_type not in {"rss", "api"}:
        for field, label in (
            ("article_link_selector", "文章链接选择器"),
            ("allowed_path_prefix", "允许访问路径"),
            ("url_pattern", "文章URL规则"),
        ):
            if not str(config.get(field) or "").strip():
                issues.append(f"缺少{label}")
    if template.get("template_complete") is False:
        issues.append(str(template.get("template_issue") or "模板标记为配置不完整"))
    return list(dict.fromkeys(issues))


TEMPLATE_MANAGED_SOURCE_FIELDS = (
    "adapter_type",
    "homepage_url",
    "list_page_url",
    "domain",
    "robots_status",
    "organization",
    "source_type",
    "category_hint",
    "region",
    "business_value_level",
    "primary_users",
    "information_types",
    "list_stability",
    "content_stability",
)


def _missing_template_value(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_missing_template_keys(
    current: Mapping[str, object],
    template: Mapping[str, object],
    *,
    prefix: str = "adapter_config",
) -> tuple[dict[str, object], list[dict[str, object]], list[str], list[dict[str, object]]]:
    merged = dict(current)
    additions: list[dict[str, object]] = []
    kept: list[str] = []
    conflicts: list[dict[str, object]] = []
    for key, template_value in template.items():
        field = f"{prefix}.{key}"
        if key not in current or _missing_template_value(current.get(key)):
            merged[key] = template_value
            additions.append({"field": field, "value": template_value})
            continue
        current_value = current[key]
        if isinstance(current_value, Mapping) and isinstance(template_value, Mapping):
            child, child_additions, child_kept, child_conflicts = _merge_missing_template_keys(
                current_value,
                template_value,
                prefix=field,
            )
            merged[key] = child
            additions.extend(child_additions)
            kept.extend(child_kept)
            conflicts.extend(child_conflicts)
        elif current_value == template_value:
            kept.append(field)
        else:
            conflicts.append(
                {
                    "field": field,
                    "database_value": current_value,
                    "template_value": template_value,
                    "action": "保留数据库现值",
                }
            )
    return merged, additions, kept, conflicts


def _plan_existing_template_sync(
    current: Mapping[str, object],
    template: Mapping[str, object],
) -> dict[str, object]:
    updates: dict[str, object] = {}
    additions: list[dict[str, object]] = []
    kept: list[str] = []
    conflicts: list[dict[str, object]] = []
    for field in TEMPLATE_MANAGED_SOURCE_FIELDS:
        template_value = template.get(field)
        if _missing_template_value(template_value):
            continue
        current_value = current.get(field)
        if _missing_template_value(current_value):
            updates[field] = template_value
            additions.append({"field": field, "value": template_value})
        elif current_value == template_value:
            kept.append(field)
        else:
            conflicts.append(
                {
                    "field": field,
                    "database_value": current_value,
                    "template_value": template_value,
                    "action": "保留数据库现值",
                }
            )
    current_config = current.get("adapter_config")
    current_config = current_config if isinstance(current_config, Mapping) else {}
    template_config = template.get("adapter_config")
    template_config = template_config if isinstance(template_config, Mapping) else {}
    merged_config, config_additions, config_kept, config_conflicts = (
        _merge_missing_template_keys(current_config, template_config)
    )
    additions.extend(config_additions)
    kept.extend(config_kept)
    conflicts.extend(config_conflicts)
    if merged_config != dict(current_config):
        updates["adapter_config_json"] = json.dumps(merged_config, ensure_ascii=False)

    template_version = str(template.get("template_version") or "")
    template_updated_at = str(template.get("template_updated_at") or "")
    if template_version and str(current.get("template_version") or "") != template_version:
        updates["template_version"] = template_version
        additions.append({"field": "template_version", "value": template_version})
    elif template_version:
        kept.append("template_version")
    if template_updated_at and str(current.get("template_updated_at") or "") != template_updated_at:
        updates["template_updated_at"] = template_updated_at
        additions.append({"field": "template_updated_at", "value": template_updated_at})
    elif template_updated_at:
        kept.append("template_updated_at")
    if updates:
        updates["last_template_sync_at"] = now_iso()
        updates["updated_at"] = now_iso()
    return {
        "source_id": str(current.get("source_id") or ""),
        "source_name": str(current.get("source_name") or ""),
        "template_version": template_version,
        "updates": updates,
        "added_fields": additions,
        "kept_fields": sorted(set(kept)),
        "conflicts": conflicts,
        "changed": bool(updates),
    }


def backup_database_before_source_maintenance(
    path: Optional[PathLike] = None,
) -> str:
    source_path = database_path(path or DEFAULT_DB_PATH)
    if not source_path.exists():
        return ""
    backup_dir = source_path.parent / "template_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target_path = backup_dir / (
        f"{source_path.stem}-before-source-template-sync-"
        f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.db"
    )
    source = sqlite3.connect(source_path, timeout=30)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        check = str(target.execute("PRAGMA quick_check").fetchone()[0])
        if check != "ok":
            raise RuntimeError(f"模板同步备份完整性检查失败：{check}")
    finally:
        target.close()
        source.close()
    return str(target_path)


def _apply_existing_template_plans(
    plans: Sequence[Mapping[str, object]],
    path: Optional[PathLike],
) -> None:
    with transaction(path) as connection:
        for plan in plans:
            updates = dict(plan.get("updates") or {})
            if not updates:
                continue
            for field in ("primary_users", "information_types"):
                if field in updates and not isinstance(updates[field], str):
                    updates[field] = json.dumps(updates[field], ensure_ascii=False)
            assignments = ",".join(f"{field}=?" for field in updates)
            connection.execute(
                f"UPDATE sources SET {assignments} WHERE source_id=?",
                (*updates.values(), str(plan["source_id"])),
            )


def _list_sources_for_template_dry_run(
    workspace_id: str,
    path: Optional[PathLike],
) -> list[dict[str, object]]:
    target = database_path(path or DEFAULT_DB_PATH)
    if not target.exists():
        return []
    connection = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        has_table = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchone()[0]
        if not has_table:
            return []
        rows = connection.execute(
            "SELECT * FROM sources WHERE workspace_id=? ORDER BY source_name",
            (workspace_id,),
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        try:
            item["adapter_config"] = json.loads(
                str(item.pop("adapter_config_json", "") or "{}")
            )
        except json.JSONDecodeError:
            item["adapter_config"] = {}
        for field in ("primary_users", "information_types"):
            try:
                item[field] = json.loads(str(item.get(field) or "[]"))
            except json.JSONDecodeError:
                item[field] = []
        result.append(item)
    return result


def initialize_recommended_sources(
    workspace_id: str,
    path: Optional[PathLike] = None,
    config_path: Optional[PathLike] = None,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    """Install or conservatively sync audited templates.

    Existing non-placeholder sources only receive missing template-managed
    fields and missing adapter-config keys. User-managed fields and existing
    adapter values are never overwritten.
    """
    templates = load_recommended_sources(config_path)
    target = database_path(path or DEFAULT_DB_PATH)
    # Read the existing source state without running schema migrations. This
    # lets the apply path create a byte-consistent backup before either source
    # rows or schema metadata are changed.
    source_rows = _list_sources_for_template_dry_run(workspace_id, path)
    existing = {str(item["source_name"]): item for item in source_rows}
    result: dict[str, object] = {
        "total": len(templates), "added": 0, "updated": 0, "enabled": 0,
        "incomplete": [], "disabled": [], "skipped_existing": [],
        "dry_run": dry_run, "would_add": [], "would_update": [],
        "template_sync": [], "backup_path": "",
    }
    existing_plans: list[dict[str, object]] = []
    pending_new: list[tuple[dict[str, object], Optional[dict[str, object]], bool, list[str]]] = []
    for template in templates:
        name = str(template.get("source_name") or "未命名模板")
        issues = recommended_source_issues(template)
        current = existing.get(name)
        if current is None:
            aliases = template.get("legacy_names") or []
            if isinstance(aliases, list):
                current = next((existing.get(str(alias)) for alias in aliases if existing.get(str(alias))), None)
        current_is_placeholder = bool(
            current and (
                not str(current.get("list_page_url") or "").strip()
                or str(current.get("domain") or "").endswith(".invalid")
            )
        )
        if current and not current_is_placeholder:
            plan = _plan_existing_template_sync(current, template)
            result["template_sync"].append(  # type: ignore[union-attr]
                {key: value for key, value in plan.items() if key != "updates"}
            )
            if bool(plan["changed"]):
                existing_plans.append(plan)
                result["would_update"].append(name)  # type: ignore[union-attr]
            else:
                result["skipped_existing"].append(name)  # type: ignore[union-attr]
            continue
        robots_ready = str(template.get("robots_status") or "") == "允许"
        should_enable = bool(template.get("default_enabled")) and not issues and robots_ready
        pending_new.append((template, current, should_enable, issues))
        result["would_add" if current is None else "would_update"].append(name)  # type: ignore[union-attr]

    if dry_run:
        result["enabled"] = sum(
            bool(item.get("enabled")) and bool(item.get("crawl_allowed"))
            for item in existing.values()
        )
        return result

    if target.exists() and (existing_plans or pending_new):
        result["backup_path"] = backup_database_before_source_maintenance(path)
    initialize_database(path)
    if existing_plans:
        _apply_existing_template_plans(existing_plans, path)
        result["updated"] = len(existing_plans)

    for template, current, should_enable, issues in pending_new:
        name = str(template.get("source_name") or "未命名模板")
        note_parts = [str(template.get("license_note") or "").strip(), str(template.get("robots_check_note") or "").strip()]
        values = {
            **template,
            "source_id": str((current or {}).get("source_id") or ""),
            "workspace_id": workspace_id,
            "enabled": should_enable,
            "crawl_allowed": should_enable,
            "report_use_allowed": bool(template.get("report_use_allowed")) and should_enable,
            "license_note": " ".join(item for item in note_parts if item),
            "last_license_checked_at": str(template.get("verified_at") or ""),
        }
        source_id = upsert_source(values, path)
        sync_time = now_iso()
        with transaction(path) as connection:
            connection.execute(
                """UPDATE sources SET template_version=?,template_updated_at=?,
                last_template_sync_at=?,updated_at=? WHERE source_id=?""",
                (
                    str(template.get("template_version") or ""),
                    str(template.get("template_updated_at") or ""),
                    sync_time,
                    sync_time,
                    source_id,
                ),
            )
        key = "updated" if current else "added"
        result[key] = int(result[key]) + 1
        if not should_enable and issues:
            result["incomplete"].append({"source_name": name, "issues": issues})  # type: ignore[union-attr]
        elif not should_enable:
            result["disabled"].append({  # type: ignore[union-attr]
                "source_name": name,
                "reason": str(template.get("robots_check_note") or "模板默认停用，需在专业设置中复核。"),
            })
    result["enabled"] = sum(
        bool(item.get("enabled")) and bool(item.get("crawl_allowed"))
        for item in list_sources(workspace_id, path)
    )
    return result


def insert_chunks(connection: sqlite3.Connection, chunks: Sequence[Mapping[str, object]]) -> None:
    for chunk in chunks:
        row = dict(chunk)
        connection.execute(
            """INSERT OR REPLACE INTO document_chunks(
                chunk_id,workspace_id,document_id,event_id,chunk_index,chunk_text,
                token_or_character_count,metadata_json,embedding_status,created_at
            ) VALUES(:chunk_id,:workspace_id,:document_id,:event_id,:chunk_index,:chunk_text,
                :token_or_character_count,:metadata_json,:embedding_status,:created_at)""",
            row,
        )
        connection.execute("DELETE FROM document_chunks_fts WHERE chunk_id=?", (row["chunk_id"],))
        connection.execute(
            "INSERT INTO document_chunks_fts(chunk_id,workspace_id,document_id,event_id,chunk_text,metadata_json) VALUES(?,?,?,?,?,?)",
            (row["chunk_id"], row["workspace_id"], row["document_id"], row.get("event_id", ""), row["chunk_text"], row["metadata_json"]),
        )


def migrate_csv_to_sqlite(
    workspace_id: str,
    csv_paths: Mapping[str, PathLike],
    path: Optional[PathLike] = None,
    backup_root: Optional[PathLike] = None,
) -> dict[str, int]:
    initialize_database(path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(backup_root or BASE_DIR / "data" / "legacy_backup") / timestamp
    counts = {"sources": 0, "events": 0}
    existing = [(name, Path(value)) for name, value in csv_paths.items() if Path(value).exists()]
    if existing:
        backup.mkdir(parents=True, exist_ok=True)
        for _, csv_path in existing:
            shutil.copy2(csv_path, backup / csv_path.name)
    with transaction(path) as connection:
        for kind, csv_path in existing:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            if kind == "sources":
                for item in rows:
                    if not str(item.get("source_name") or "").strip():
                        continue
                    source_id = str(item.get("source_id") or new_id("SRC"))
                    connection.execute(
                        """INSERT OR IGNORE INTO sources(source_id,workspace_id,source_name,organization,domain,
                        homepage_url,list_page_url,source_type,category_hint,region,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (source_id, workspace_id, item.get("source_name", ""), item.get("organization", ""), "",
                         item.get("homepage_url", ""), item.get("list_page_url", ""), item.get("source_type", "其他"),
                         item.get("category_hint", ""), item.get("region", ""), now_iso(), now_iso()),
                    )
                    counts["sources"] += 1
            elif kind == "events":
                legacy_source_id = f"SRC-LEGACY-{workspace_id.replace('WS-', '')[:8]}"
                connection.execute(
                    """INSERT OR IGNORE INTO sources(source_id,workspace_id,source_name,organization,domain,
                    source_type,commercial_reuse_status,license_note,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (legacy_source_id, workspace_id, "旧版CSV迁移来源", "", f"legacy-{workspace_id.lower()}.invalid",
                     "其他", "未明确", "从旧版CSV迁入；进入报告前需重新核对来源许可。", now_iso(), now_iso()),
                )
                for item in rows:
                    if not str(item.get("title") or "").strip():
                        continue
                    event_id = str(item.get("event_id") or new_id("EVT"))
                    document_id = f"DOC-LEGACY-{hashlib.sha256((workspace_id + '|' + event_id).encode('utf-8')).hexdigest()[:12].upper()}"
                    timestamp = now_iso()
                    source_url = item.get("source_url", "")
                    document_text = "\n".join([item.get("title", ""), item.get("summary", ""), item.get("impact", "")]).strip()
                    connection.execute(
                        """INSERT INTO documents(document_id,workspace_id,source_id,canonical_url,original_url,title,publisher,
                        published_at,fetched_at,cleaned_text,content_hash,extraction_status,extraction_quality,http_status,
                        document_version,is_current,review_status,created_at,updated_at,
                        quality_status,report_quality_eligible,record_state,quarantine_reason,quarantined_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (document_id, workspace_id, legacy_source_id, source_url, source_url, item.get("title", ""),
                         item.get("source_name", ""), item.get("event_date", ""), item.get("collected_at", "") or timestamp,
                         document_text, content_hash(document_text), "CSV迁移", "待重新核对原文", 0, 1, 1,
                         "已隔离", timestamp, timestamp, "待人工核对", 0, "quarantined",
                         "旧版CSV迁移记录，需重新核对原文与审核来源", timestamp),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO events(event_id,workspace_id,document_id,event_date,collected_at,category,title,
                        summary,impact,affected_area,affected_period,source_name,source_type,source_url,status,
                        related_event_id,analyst_note,human_verified,verified_at,report_eligible,created_at,updated_at,
                        reviewer_type,record_state,quarantine_reason,quarantined_at,review_state)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (event_id, workspace_id, document_id, item.get("event_date", ""), item.get("collected_at", ""), item.get("category", ""),
                         item.get("title", ""), item.get("summary", ""), item.get("impact", ""), item.get("affected_area", ""),
                         item.get("affected_period", ""), item.get("source_name", ""), item.get("source_type", "其他"),
                         item.get("source_url", ""), item.get("status", "待核实"), item.get("related_event_id", ""),
                         item.get("analyst_note", ""), 0, "", 0, now_iso(), now_iso(), "migration",
                         "quarantined", "旧版CSV迁移记录，不能继承历史人工确认口径", timestamp, "pending"),
                    )
                    counts["events"] += 1
    counts["backup_dir"] = str(backup)  # type: ignore[assignment]
    return counts


def table_counts(workspace_id: str = "", path: Optional[PathLike] = None) -> dict[str, int]:
    initialize_database(path)
    tables = [
        "sources", "crawl_runs", "documents", "events", "document_chunks",
        "reports", "qa_logs", "event_evidence", "event_reviews", "qa_label_reviews", "promotion_history",
        "crawl_source_runs", "qa_review_progress", "customer_feedback",
    ]
    result: dict[str, int] = {}
    with connect(path) as connection:
        for table in tables:
            columns = _columns(connection, table)
            if "workspace_id" in columns:
                result[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE workspace_id=?", (workspace_id,)).fetchone()[0])
            else:
                result[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return result


def table_csv_bytes(
    table: str,
    workspace_id: str = "",
    path: Optional[PathLike] = None,
    *,
    export_mode: str = "metadata",
    include_inactive: bool = False,
    confirmed: bool = False,
) -> bytes:
    allowed = {
        "sources", "documents", "events", "document_chunks", "crawl_runs",
        "qa_logs", "ai_call_logs", "reports", "event_evidence",
        "event_reviews", "qa_label_reviews", "promotion_history", "operation_runs", "technical_logs",
        "crawl_source_runs", "qa_review_progress", "customer_feedback",
    }
    if table not in allowed:
        raise ValueError("不允许导出该表")
    initialize_database(path)
    with connect(path) as connection:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        if export_mode in {"fulltext", "raw_database"}:
            if not confirmed:
                raise PermissionError("全文或原始数据库导出必须二次确认")
            permission_column = (
                "fulltext_redistribution_allowed"
                if export_mode == "fulltext"
                else "raw_data_resale_allowed"
            )
            blocked = int(
                connection.execute(
                    f"""SELECT COUNT(*) FROM sources WHERE workspace_id=?
                    AND {permission_column}=0""",
                    (workspace_id,),
                ).fetchone()[0]
            )
            if blocked:
                raise PermissionError(
                    f"当前有 {blocked} 个来源未允许"
                    + ("全文再分发" if export_mode == "fulltext" else "原始数据转售")
                )
        if export_mode == "metadata":
            if table == "documents":
                blocked_columns = {
                    "cleaned_text", "raw_html_path", "raw_file_path",
                    "quality_metrics_json",
                }
                columns = [column for column in columns if column not in blocked_columns]
            elif table in {"document_chunks", "document_chunks_fts"}:
                columns = [
                    column for column in columns
                    if column not in {"chunk_text", "metadata_json"}
                ]
        query = f"SELECT * FROM {table}"
        params: tuple[object, ...] = ()
        if "workspace_id" in columns:
            query += " WHERE workspace_id=?"
            params = (workspace_id,)
            if not include_inactive and table in {"documents", "events"}:
                query += " AND record_state='active'"
        query = query.replace("SELECT *", "SELECT " + ",".join(f'"{column}"' for column in columns))
        rows = connection.execute(query, params).fetchall()
    import io
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([[row[column] for column in columns] for row in rows])
    return output.getvalue().encode("utf-8-sig")
