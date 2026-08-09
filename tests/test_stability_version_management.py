from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from document_processor import store_document
from operation_store import (
    add_technical_log,
    claim_operation,
    close_operation,
    create_operation,
    finish_operation,
    get_operation,
    list_operations,
    list_technical_logs,
    recover_stale_operations,
    retry_operation,
    start_operation,
)
from platform_db import connect, initialize_database, now_iso, upsert_source
from package_release import should_include
from scripts.cleanup_artifacts import plan_cleanup
from state_audit import (
    audit_snapshot_is_stale,
    collect_state_audit,
    write_state_audit,
)
from task_runtime import enqueue_background_task, read_task_log
from tests.test_deepseek_and_rag import _db
from warning_quality_transition import (
    confirm_warning_transition,
    list_warning_transition_candidates,
)
from workspace_store import create_workspace


def _workspace(tmp_path: Path):
    db = tmp_path / "portscope.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "稳定性测试"}, db, root)
    initialize_database(db)
    return db, root, workspace


@pytest.mark.parametrize("final_status", ["succeeded", "failed"])
def test_close_preserves_original_execution_result(tmp_path: Path, final_status: str):
    db, _, workspace = _workspace(tmp_path)
    operation_id = start_operation(workspace["workspace_id"], "system_check", db)
    finish_operation(operation_id, db, status=final_status)
    before = get_operation(operation_id, db)
    close_operation(operation_id, workspace["workspace_id"], db)
    after = get_operation(operation_id, db)
    assert after["status"] == final_status
    assert after["finished_at"] == before["finished_at"]
    assert after["home_visible"] == 0 and after["is_archived"] == 1
    assert after["archived_at"]


def test_archived_success_is_not_retryable_and_history_filter_uses_result(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    success_id = start_operation(workspace["workspace_id"], "system_check", db)
    finish_operation(success_id, db, status="succeeded")
    close_operation(success_id, workspace["workspace_id"], db)
    failed_id = start_operation(workspace["workspace_id"], "file_import", db)
    finish_operation(failed_id, db, status="failed")
    close_operation(failed_id, workspace["workspace_id"], db)
    succeeded = list_operations(
        workspace["workspace_id"], db, statuses=["succeeded"]
    )
    failed = list_operations(workspace["workspace_id"], db, statuses=["failed"])
    assert success_id in {row["operation_run_id"] for row in succeeded}
    assert failed_id in {row["operation_run_id"] for row in failed}
    with pytest.raises(ValueError):
        retry_operation(success_id, workspace["workspace_id"], "S", db)
    assert retry_operation(failed_id, workspace["workspace_id"], "S", db)


def test_legacy_archived_status_is_conservative(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    known = create_operation(
        workspace["workspace_id"], "system_check", db, status="succeeded"
    )
    unknown = create_operation(
        workspace["workspace_id"], "business_action", db, status="succeeded"
    )
    with connect(db) as connection:
        connection.execute(
            """UPDATE operation_runs SET status='archived',result_summary='系统检查成功',
            archived_at=?,home_visible=0 WHERE operation_run_id=?""",
            (now_iso(), known),
        )
        connection.execute(
            """UPDATE operation_runs SET status='archived',result_summary='',
            archived_at=?,home_visible=0 WHERE operation_run_id=?""",
            (now_iso(), unknown),
        )
        connection.commit()
    initialize_database(db)
    assert get_operation(known, db)["status"] == "succeeded"
    legacy = get_operation(unknown, db)
    assert legacy["status"] == "archived"
    assert legacy["metadata"]["legacy_unknown"] is True


def _make_duplicate_crawl_mapping(db: Path, workspace_id: str) -> tuple[str, str, str]:
    crawl_id = "CRAWL-DUPLICATE"
    started = now_iso()
    with connect(db) as connection:
        connection.execute(
            """INSERT INTO crawl_runs(crawl_run_id,workspace_id,started_at,finished_at,
            status,updated_at) VALUES(?,?,?,?,?,?)""",
            (crawl_id, workspace_id, started, started, "完成", started),
        )
        connection.commit()
    initialize_database(db)
    with connect(db) as connection:
        migration_id = str(
            connection.execute(
                """SELECT operation_run_id FROM operation_runs
                WHERE external_ref_type='crawl_run' AND external_ref_id=?""",
                (crawl_id,),
            ).fetchone()[0]
        )
    normal_id = create_operation(
        workspace_id,
        "crawl",
        db,
        status="succeeded",
        metadata={"crawl_run_id": crawl_id},
    )
    add_technical_log(
        workspace_id,
        db,
        "旧迁移日志",
        operation_run_id=migration_id,
        component="migration",
    )
    return crawl_id, normal_id, migration_id


def test_duplicate_crawl_operations_merge_idempotently(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    crawl_id, normal_id, migration_id = _make_duplicate_crawl_mapping(
        db, workspace["workspace_id"]
    )
    initialize_database(db)
    initialize_database(db)
    with connect(db) as connection:
        rows = connection.execute(
            """SELECT operation_run_id,created_by FROM operation_runs
            WHERE external_ref_type='crawl_run' AND external_ref_id=?""",
            (crawl_id,),
        ).fetchall()
        duplicate_exists = connection.execute(
            "SELECT 1 FROM operation_runs WHERE operation_run_id=?",
            (migration_id,),
        ).fetchone()
    assert [(row["operation_run_id"], row["created_by"]) for row in rows] == [
        (normal_id, "local_user")
    ]
    assert duplicate_exists is None
    assert all(
        row["operation_run_id"] == normal_id
        for row in list_technical_logs(workspace["workspace_id"], db)
        if row["message"] == "旧迁移日志"
    )


class _ImmediateFailure:
    pid = 9876

    def poll(self):
        return 7


def test_worker_preclaim_crash_fails_immediately_and_log_is_redacted(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)

    def factory(_command, **kwargs):
        kwargs["stderr"].write(b"import failed; api_key=sk-THIS-IS-A-SECRET-123456\\n")
        kwargs["stderr"].flush()
        return _ImmediateFailure()

    queued = enqueue_background_task(
        workspace["workspace_id"],
        "index_rebuild",
        db,
        root,
        metadata={"limit": 1},
        popen_factory=factory,
        early_exit_grace_seconds=0,
    )
    operation = get_operation(str(queued["operation_run_id"]), db)
    assert operation["status"] == "failed"
    assert operation["metadata"]["worker_exit_code"] == 7
    assert operation["log_path"].startswith("output/task_logs/")
    log = read_task_log(str(operation["log_path"]))
    assert "sk-THIS-IS-A-SECRET-123456" not in log
    assert "***" in log
    assert "退出码 7" in operation["error_summary"]


def test_queued_and_running_stale_recovery_use_different_thresholds(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    queued = create_operation(workspace["workspace_id"], "index_rebuild", db)
    running = create_operation(
        workspace["workspace_id"], "ai_reprocess", db, status="running"
    )
    five_minutes_ago = (
        datetime.now().astimezone() - timedelta(minutes=5)
    ).isoformat(timespec="seconds")
    with connect(db) as connection:
        connection.execute(
            """UPDATE operation_runs SET heartbeat_at=?,updated_at=?
            WHERE operation_run_id IN (?,?)""",
            (five_minutes_ago, five_minutes_ago, queued, running),
        )
        connection.commit()
    assert recover_stale_operations(
        db, timeout_minutes=30, queued_timeout_minutes=3
    ) == 1
    assert get_operation(queued, db)["status"] == "failed"
    assert get_operation(running, db)["status"] == "running"


def test_state_audit_matches_database_and_detects_staleness(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    version_root = tmp_path / "project"
    version_root.mkdir()
    (version_root / "VERSION").write_text("0.5.0-beta\n", encoding="utf-8")
    audit_path = version_root / "CURRENT_STATE_AUDIT.md"
    audit = write_state_audit(db, version_root, audit_path)
    with connect(db) as connection:
        operation_count = int(
            connection.execute("SELECT COUNT(*) FROM operation_runs").fetchone()[0]
        )
    assert sum(audit["operation_statuses"].values()) == operation_count
    assert audit["documents"]["total"] == 0
    assert audit_snapshot_is_stale(audit_path, db, version_root)[0] is False
    create_operation(workspace["workspace_id"], "system_check", db)
    stale, reason = audit_snapshot_is_stale(audit_path, db, version_root)
    assert stale and reason == "数据库已变化"
    (version_root / "VERSION").write_text("0.6.0-beta\n", encoding="utf-8")
    assert audit_snapshot_is_stale(audit_path, db, version_root) == (
        True,
        "代码版本已变化",
    )


def test_cleanup_dry_run_only_targets_artifacts(tmp_path: Path):
    (tmp_path / "output" / "pytest-old").mkdir(parents=True)
    (tmp_path / "output" / "pytest-old" / "temp.db").write_bytes(b"x" * 100)
    (tmp_path / "output" / "qa_real").mkdir()
    (tmp_path / "output" / "qa_real" / "acceptance.db").write_bytes(b"keep")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portscope.db").write_bytes(b"business")
    candidates = plan_cleanup(tmp_path)
    paths = {item.path for item in candidates}
    assert (tmp_path / "output" / "pytest-old").resolve() in paths
    assert (tmp_path / "output" / "qa_real" / "acceptance.db").exists()
    assert (tmp_path / "data" / "portscope.db").exists()
    assert not should_include(Path("output/task_logs/OP-TEST.log"))
    assert not should_include(Path("artifacts/pytest-old/temp.txt"))
    assert not should_include(Path("artifacts/test.db"))


def _quality_candidate(tmp_path: Path, monkeypatch):
    db, root, workspace, source_id = _db(tmp_path)
    text = (
        "山东海上风险预警信息2026年第20期。发布时间2026年6月14日。"
        "黄海中部风力达到七级，有效时间为六月十四日至十五日，请相关船舶注意航行安全。"
    )
    stored = store_document(
        {
            "workspace_id": workspace["workspace_id"],
            "source_id": source_id,
            "canonical_url": "https://example.com/warning.pdf",
            "original_url": "https://example.com/warning.html",
            "title": "山东-海上风险预警信息(2026年第20期)-黄色",
            "publisher": "测试公开机构",
            "published_at": "2026-06-14",
            "fetched_at": now_iso(),
            "raw_html": "",
            "cleaned_text": text,
            "extraction_status": "成功",
            "extraction_quality": "PDF文本",
            "http_status": 200,
            "document_format": "pdf",
        },
        db,
        root,
        [],
    )
    with connect(db) as connection:
        connection.execute(
            """UPDATE documents SET record_state='quarantined',
            quality_status='正文质量不足',extraction_status='正文质量不足',
            ai_status='质量门禁拦截',review_status='质量异常',
            report_quality_eligible=0,quarantine_reason='模板相似度过高'
            WHERE document_id=?""",
            (stored.document_id,),
        )
        connection.commit()

    def fake_evaluate(_db, _source):
        return {
            "documents": [
                {
                    "document_id": stored.document_id,
                    "title": "山东-海上风险预警信息(2026年第20期)-黄色",
                    "new_status": "合格",
                    "processing_allowed": True,
                    "structured_fact_fields": "issue_number、area、risk_level",
                }
            ]
        }

    monkeypatch.setattr("warning_quality_transition.evaluate", fake_evaluate)
    return db, workspace, source_id, stored.document_id


def test_warning_pdf_requires_confirmation_and_only_enters_internal_pending_flow(
    tmp_path: Path, monkeypatch
):
    db, workspace, source_id, document_id = _quality_candidate(tmp_path, monkeypatch)
    assert len(
        list_warning_transition_candidates(
            db, workspace["workspace_id"], source_id=source_id
        )
    ) == 1
    with pytest.raises(PermissionError):
        confirm_warning_transition(
            db,
            workspace["workspace_id"],
            [document_id],
            source_id=source_id,
        )
    with connect(db) as connection:
        before = connection.execute(
            "SELECT record_state FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()[0]
    assert before == "quarantined"
    result = confirm_warning_transition(
        db,
        workspace["workspace_id"],
        [document_id],
        source_id=source_id,
        confirmed=True,
    )
    assert result["human_verified_count"] == 0
    assert result["customer_eligible_count"] == 0
    with connect(db) as connection:
        document = connection.execute(
            """SELECT record_state,quality_status,review_status,ai_status,
            quarantine_reason FROM documents WHERE document_id=?""",
            (document_id,),
        ).fetchone()
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE document_id=?",
                (document_id,),
            ).fetchone()[0]
        )
        reevaluation_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM document_quality_reevaluations
                WHERE document_id=? AND decision='转入内部待审核'""",
                (document_id,),
            ).fetchone()[0]
        )
    assert dict(document) == {
        "record_state": "active",
        "quality_status": "合格",
        "review_status": "待人工审核",
        "ai_status": "待AI处理",
        "quarantine_reason": "模板相似度过高",
    }
    assert event_count == 0
    assert reevaluation_count == 1
