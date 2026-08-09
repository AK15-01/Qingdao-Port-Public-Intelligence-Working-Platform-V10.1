from __future__ import annotations

from pathlib import Path

import pytest

from event_pipeline import batch_confirm_events
from evidence_repair import apply_exact_evidence_repair
from platform_db import connect, content_hash, initialize_database, new_id, now_iso, transaction
from qa_promotion import list_qa_promotion_candidates, promote_qa_events
from qa_review_store import load_review_progress, save_review_progress
from run_statistics import add_customer_feedback, four_week_evaluation, source_run_statistics
from tests.test_review_evidence_rag_convergence import _store_event
from ui_daily import needs_first_activation_guidance
from workspace_store import create_workspace


def test_review_progress_is_resumable_but_never_counts_as_human_review(tmp_path: Path):
    db, _, workspace, _, document_id, event_id = _store_event(tmp_path, "progress.db")
    save_review_progress(
        db,
        workspace["workspace_id"],
        current_document_id=document_id,
        filter_status="未审核",
        reviewer_type="human_user",
        reviewer_name="项目使用者",
        draft={"summary": "尚未提交的修改"},
    )
    progress = load_review_progress(db, workspace["workspace_id"])
    assert progress["current_document_id"] == document_id
    assert progress["draft"]["summary"] == "尚未提交的修改"
    with connect(db) as connection:
        event = connection.execute(
            "SELECT human_verified,reviewer_type FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        reviews = connection.execute("SELECT COUNT(*) FROM event_reviews").fetchone()[0]
    assert tuple(event) == (0, "unknown")
    assert reviews == 0


def test_first_activation_guidance_is_read_only(tmp_path: Path):
    db = tmp_path / "formal.db"
    workspace = create_workspace({"workspace_name": "正式"}, db, tmp_path / "data")
    initialize_database(db)
    assert needs_first_activation_guidance(db, workspace["workspace_id"], 15) is True
    assert needs_first_activation_guidance(db, workspace["workspace_id"], 0) is False
    with connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM promotion_history").fetchone()[0] == 0


def test_geography_fact_change_cannot_be_fixed_by_offset_only(tmp_path: Path):
    db, _, workspace, _, document_id, event_id = _store_event(tmp_path, "evidence.db")
    source = "黄海中部发布航行警告，相关船舶应当核对原始公告。"
    digest = content_hash(source)
    evidence_id = new_id("EVD")
    with transaction(db) as connection:
        connection.execute(
            "UPDATE documents SET cleaned_text=?,content_hash=?,document_version=2 WHERE document_id=?",
            (source, digest, document_id),
        )
        connection.execute(
            "UPDATE events SET summary='黄海中南部发布航行警告',evidence_verified=0 WHERE event_id=?",
            (event_id,),
        )
        connection.execute("DELETE FROM event_evidence WHERE event_id=?", (event_id,))
        connection.execute(
            """INSERT INTO event_evidence(
            evidence_id,event_id,quote_text,document_id,document_version,content_hash,
            start_offset,end_offset,verification_status,verified_at,normalization_method,
            failure_reason,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id, event_id, "黄海中南部发布航行警告", document_id, 2, digest,
                -1, -1, "未定位", "", "none", "地域事实变化", now_iso(), now_iso(),
            ),
        )
    with pytest.raises(ValueError, match="必须先同步修正事件内容"):
        apply_exact_evidence_repair(
            evidence_id,
            "黄海中部发布航行警告",
            db,
            reviewer_type="human_user",
            reviewer_name="项目使用者",
            reviewer_note="核对原文",
            confirmed=True,
        )
    result = apply_exact_evidence_repair(
        evidence_id,
        "黄海中部发布航行警告",
        db,
        reviewer_type="human_user",
        reviewer_name="项目使用者",
        reviewer_note="原抽取扩大了地域范围，按原文修正",
        confirmed=True,
        event_edits={"summary": "黄海中部发布航行警告"},
    )
    assert result["verification_status"] == "已验证"
    assert result["requires_second_review"] is True
    with connect(db) as connection:
        event = connection.execute(
            """SELECT summary,human_verified,requires_second_review,
            active_for_internal_use,eligible_for_customer_report
            FROM events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
    assert tuple(event) == ("黄海中部发布航行警告", 0, 1, 0, 0)


def test_batch_promotion_rolls_back_and_internal_customer_flags_are_separate(tmp_path: Path):
    qa_db, _, qa_workspace, source_id, document_id, event_id = _store_event(tmp_path, "qa.db")
    formal_db = tmp_path / "formal.db"
    formal_workspace = create_workspace({"workspace_name": "正式"}, formal_db, tmp_path / "formal-data")
    with transaction(qa_db) as connection:
        connection.execute(
            """UPDATE sources SET internal_analysis_allowed=1,
            customer_summary_allowed=0,short_quote_allowed=0 WHERE source_id=?""",
            (source_id,),
        )
    confirmed = batch_confirm_events(
        [event_id],
        qa_workspace["workspace_id"],
        qa_db,
        reviewer_type="human_user",
        reviewer_name="项目使用者",
        review_method="打开原文逐项核对",
    )
    assert confirmed["confirmed"] == [event_id]
    queue = list_qa_promotion_candidates(
        qa_db,
        source_workspace_id=qa_workspace["workspace_id"],
    )
    assert queue[0]["internal_eligible"] is True
    assert queue[0]["customer_eligible"] is False

    rolled_back = promote_qa_events(
        qa_db,
        formal_db,
        source_workspace_id=qa_workspace["workspace_id"],
        target_workspace_id=formal_workspace["workspace_id"],
        event_ids=[event_id, "EVT-NOT-FOUND"],
        qa_run_id="QA-RUN",
    )
    assert not rolled_back.promoted
    with connect(formal_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    promoted = promote_qa_events(
        qa_db,
        formal_db,
        source_workspace_id=qa_workspace["workspace_id"],
        target_workspace_id=formal_workspace["workspace_id"],
        event_ids=[event_id],
        qa_run_id="QA-RUN",
    )
    assert promoted.promoted == (event_id,)
    with connect(formal_db) as connection:
        event = connection.execute(
            """SELECT active_for_internal_use,eligible_for_customer_report,
            report_eligible,promotion_source_event_id FROM events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        operation = connection.execute(
            "SELECT status FROM operation_runs WHERE operation_type='qa_promotion'"
        ).fetchone()
    assert tuple(event) == (1, 0, 0, event_id)
    assert operation["status"] == "succeeded"
    with connect(qa_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id=?", (event_id,)
        ).fetchone()[0] == 1
    repeated = promote_qa_events(
        qa_db,
        formal_db,
        source_workspace_id=qa_workspace["workspace_id"],
        target_workspace_id=formal_workspace["workspace_id"],
        event_ids=[event_id],
        qa_run_id="QA-RUN",
    )
    assert not repeated.promoted and "已经晋升" in repeated.blocked[event_id]


def test_four_week_statistics_only_use_persisted_real_runs_and_manual_feedback(tmp_path: Path):
    db, _, workspace, source_id, _, _ = _store_event(tmp_path, "stats.db")
    empty = four_week_evaluation(workspace["workspace_id"], db)
    assert empty["real_run_days"] == 0
    with transaction(db) as connection:
        connection.execute(
            """INSERT INTO crawl_source_runs(
            source_run_id,crawl_run_id,workspace_id,source_id,started_at,finished_at,status,
            request_count,discovered_count,fetched_count,new_document_count,updated_document_count,
            skipped_count,failed_count,http_error_count,tls_error_count,timeout_count,
            javascript_blocked_count,quality_failed_count,retry_count,duration_ms,error_summary,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SRUN-REAL", "CRAWL-REAL", workspace["workspace_id"], source_id,
                f"{now_iso()[:10]}T08:00:00+08:00", f"{now_iso()[:10]}T08:00:05+08:00",
                "succeeded", 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 5000, "", now_iso(),
            ),
        )
    evaluation = four_week_evaluation(workspace["workspace_id"], db)
    assert evaluation["real_run_days"] == 1
    stats = source_run_statistics(workspace["workspace_id"], db)
    source = next(item for item in stats if item["source_id"] == source_id)
    assert source["run_count"] == 1 and source["request_count"] == 2
    with pytest.raises(ValueError, match="手工录入"):
        add_customer_feedback(
            workspace["workspace_id"],
            db,
            feedback_text="不得由AI伪造",
            created_by="ai_assistant",
        )
    assert four_week_evaluation(workspace["workspace_id"], db)["customer_feedback_count"] == 0
