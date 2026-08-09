from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from zipfile import ZipFile

import pytest

from commercial_readiness import (
    add_pilot_feedback,
    commercial_readiness_metrics,
    create_delivery_snapshot,
    human_review_accuracy,
    mark_delivery_snapshot_delivered,
    preflight_customer_report,
    run_restore_drill,
    save_source_compliance,
)
from platform_db import connect, new_id, now_iso, transaction
from tests.test_deepseek_and_rag import _store
from workspace_store import record_report


def _make_customer_eligible(tmp_path: Path):
    db, root, workspace, source_id, document_id, event_id, _ = _store(tmp_path)
    with connect(db) as connection:
        document = connection.execute(
            """SELECT cleaned_text,content_hash,document_version FROM documents
            WHERE document_id=?""",
            (document_id,),
        ).fetchone()
        quote = "相关水域临时限航"
        start = str(document["cleaned_text"]).index(quote)
        connection.execute("DELETE FROM event_evidence WHERE event_id=?", (event_id,))
        connection.execute(
            """INSERT INTO event_evidence(
            evidence_id,event_id,quote_text,document_id,document_version,content_hash,
            start_offset,end_offset,verification_status,verified_at,normalization_method,
            failure_reason,created_at,updated_at,document_version_id,quote_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("EVD"),
                event_id,
                quote,
                document_id,
                int(document["document_version"]),
                document["content_hash"],
                start,
                start + len(quote),
                "已验证",
                now_iso(),
                "none",
                "",
                now_iso(),
                now_iso(),
                f"{document_id}:v{document['document_version']}",
                hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            ),
        )
        connection.execute(
            """UPDATE sources SET customer_summary_allowed=1,short_quote_allowed=1,
            commercial_reuse_status='允许',terms_status='允许',
            permission_basis='仅用于自动测试的明确许可',permission_reviewed_at='2026-07-01'
            WHERE source_id=?""",
            (source_id,),
        )
        connection.execute(
            """UPDATE events SET human_verified=1,reviewer_type='human_user',
            reviewer_name='测试审核人',verified_at=?,review_state='accepted',
            report_eligible=1,eligible_for_customer_report=1,active_for_internal_use=1,
            evidence_verified=1,business_value='高',involved_entities='测试海事机构',
            extraction_confidence=0.9 WHERE event_id=?""",
            (now_iso(), event_id),
        )
        connection.execute(
            """INSERT INTO event_reviews(
            review_id,workspace_id,event_id,reviewer_type,reviewer_name,reviewed_at,
            review_method,review_version,reviewer_note,checklist_json,decision,created_at,
            before_json,after_json,changed_fields_json,requires_second_review,review_stage
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("REV"),
                workspace["workspace_id"],
                event_id,
                "human_user",
                "测试审核人",
                now_iso(),
                "逐字核对测试fixture",
                "test",
                "",
                json.dumps({"source_opened": True, "facts_checked": True}),
                "accept",
                now_iso(),
                json.dumps({"title": "测试航行警告"}, ensure_ascii=False),
                json.dumps({"title": "测试航行警告"}, ensure_ascii=False),
                "[]",
                0,
                "first_review",
            ),
        )
        connection.commit()
    return db, root, workspace, source_id, document_id, event_id


def test_readiness_uses_real_data_and_never_claims_commercial_accuracy(tmp_path: Path):
    db, root, workspace, source_id, _, _ = _make_customer_eligible(tmp_path)
    with transaction(db) as connection:
        connection.execute(
            """INSERT INTO crawl_source_runs(
            source_run_id,crawl_run_id,workspace_id,source_id,started_at,finished_at,status,
            request_count,discovered_count,fetched_count,new_document_count,
            updated_document_count,skipped_count,failed_count,http_error_count,
            tls_error_count,timeout_count,javascript_blocked_count,quality_failed_count,
            qualified_document_count,new_event_count,retry_count,duration_ms,error_summary,
            created_at
            ) VALUES('SR-1','CR-1',?,?, '2026-07-28T09:00:00+10:00',
            '2026-07-28T09:00:05+10:00','succeeded',2,1,1,1,0,0,0,0,0,0,0,0,1,1,0,5000,'',
            '2026-07-28T09:00:00+10:00')""",
            (workspace["workspace_id"], source_id),
        )
    metrics = commercial_readiness_metrics(
        workspace["workspace_id"], db, project_root=root
    )
    assert metrics["human_review_event_count"] == 1
    assert metrics["accuracy_sample_sufficient"] is False
    assert metrics["readiness_status"] == "未达到"
    assert "真人审核样本至少50条" in metrics["missing_gates"]


def test_human_accuracy_excludes_ai_and_uses_before_after_differences(tmp_path: Path):
    db, _, workspace, _, _, event_id = _make_customer_eligible(tmp_path)
    with transaction(db) as connection:
        connection.execute(
            """INSERT INTO event_reviews(
            review_id,workspace_id,event_id,reviewer_type,reviewer_name,reviewed_at,
            review_method,review_version,reviewer_note,checklist_json,decision,created_at,
            before_json,after_json,changed_fields_json,requires_second_review,review_stage
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "REV-AI",
                workspace["workspace_id"],
                event_id,
                "codex_agent",
                "agent",
                "2027-01-01T00:00:00+10:00",
                "自动",
                "test",
                "",
                "{}",
                "accept",
                "2027-01-01T00:00:00+10:00",
                "{}",
                "{}",
                '["title"]',
                0,
                "first_review",
            ),
        )
        connection.execute(
            """UPDATE event_reviews SET changed_fields_json='["title"]'
            WHERE event_id=? AND reviewer_type='human_user'""",
            (event_id,),
        )
    result = human_review_accuracy(workspace["workspace_id"], db)
    assert result["human_review_count"] == 1
    assert result["field_accuracy"]["标题"] == 0
    assert result["sample_sufficient"] is False


def test_customer_preflight_requires_current_verbatim_evidence_and_review(tmp_path: Path):
    db, _, workspace, _, document_id, event_id = _make_customer_eligible(tmp_path)
    passed = preflight_customer_report(
        workspace["workspace_id"], [event_id], db, reference_date=date(2026, 7, 31)
    )
    assert passed.passed
    with transaction(db) as connection:
        connection.execute(
            "UPDATE documents SET document_version=document_version+1 WHERE document_id=?",
            (document_id,),
        )
    failed = preflight_customer_report(
        workspace["workspace_id"], [event_id], db, reference_date=date(2026, 7, 31)
    )
    assert not failed.passed
    assert any(item["code"] == "evidence" and not item["passed"] for item in failed.checks)


def test_delivery_snapshot_is_immutable_and_delivery_is_audited(tmp_path: Path):
    db, root, workspace, _, _, event_id = _make_customer_eligible(tmp_path)
    preflight = preflight_customer_report(
        workspace["workspace_id"], [event_id], db, reference_date=date(2026, 7, 31)
    )
    report = record_report(
        {
            "report_id": "RPT-TEST",
            "workspace_id": workspace["workspace_id"],
            "client_id": "PILOT-01",
            "report_title": "测试客户报告",
            "start_date": "2026-07-01",
            "end_date": "2026-07-31",
            "event_ids": [event_id],
            "version": 1,
            "report_mode": "客户交付版",
        },
        db,
    )
    files = []
    for name in ("report.docx", "report.html", "report.xlsx"):
        path = root / name
        path.write_bytes(("immutable-" + name).encode("utf-8"))
        files.append(path)
    snapshot = create_delivery_snapshot(
        workspace["workspace_id"],
        "RPT-TEST",
        1,
        db,
        file_paths=files,
        preflight=preflight,
        pilot_id="PILOT-01",
    )
    with pytest.raises(sqlite3.IntegrityError, match="不可覆盖"):
        with transaction(db) as connection:
            connection.execute(
                """UPDATE report_delivery_snapshots SET immutable_payload_json='{}'
                WHERE snapshot_id=?""",
                (snapshot["snapshot_id"],),
            )
    mark_delivery_snapshot_delivered(
        snapshot["snapshot_id"], workspace["workspace_id"], db
    )
    with connect(db) as connection:
        stored = connection.execute(
            """SELECT delivery_status,payload_hash FROM report_delivery_snapshots
            WHERE snapshot_id=?""",
            (snapshot["snapshot_id"],),
        ).fetchone()
        amendments = connection.execute(
            """SELECT COUNT(*) FROM report_delivery_amendments WHERE snapshot_id=?""",
            (snapshot["snapshot_id"],),
        ).fetchone()[0]
    assert report["report_record_id"]
    assert stored["delivery_status"] == "delivered"
    assert stored["payload_hash"] == snapshot["payload_hash"]
    assert amendments == 1


def test_restore_drill_uses_isolated_directory_and_records_real_result(tmp_path: Path):
    db, root, workspace, _, _, _ = _make_customer_eligible(tmp_path)
    snapshot_db = tmp_path / "snapshot.db"
    source = sqlite3.connect(str(db))
    target = sqlite3.connect(str(snapshot_db))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    backup = tmp_path / "backup.zip"
    with ZipFile(backup, "w") as archive:
        archive.write(snapshot_db, "data/portscope.db")
    before_hash = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    result = run_restore_drill(
        workspace["workspace_id"],
        db,
        backup,
        project_root=root,
    )
    assert result["status"] == "succeeded" and result["quick_check"] == "ok"
    assert result["restored_counts"]["events"] == result["production_counts"]["events"]
    assert result["report_path"].is_file()
    assert hashlib.sha256(Path(db).read_bytes()).hexdigest() != before_hash
    # The production DB changed only because the successful drill record was added.
    with connect(db) as connection:
        row = connection.execute(
            "SELECT status,restore_target FROM restore_drills WHERE restore_drill_id=?",
            (result["restore_drill_id"],),
        ).fetchone()
    assert row["status"] == "succeeded"
    assert "output" in row["restore_target"] and str(db) != row["restore_target"]


def test_feedback_cannot_be_ai_generated_and_compliance_defaults_are_conservative(
    tmp_path: Path,
):
    db, _, workspace, source_id, _, _ = _make_customer_eligible(tmp_path)
    with pytest.raises(ValueError, match="真实"):
        add_pilot_feedback(
            workspace["workspace_id"],
            db,
            customer_label="匿名试点",
            report_or_alert_id="RPT-X",
            viewed="是",
            already_known="否",
            action_taken="是",
            time_saved="是",
            false_positive="否",
            omission_reported="否",
            willing_to_continue="是",
            created_by="ai_assistant",
        )
    with pytest.raises(ValueError, match="明确依据"):
        save_source_compliance(
            workspace["workspace_id"],
            source_id,
            db,
            {
                "public_access_status": "公开可访问",
                "access_frequency_compliant": "是",
                "personal_information_status": "未发现",
                "important_data_status": "未发现",
                "fulltext_redistribution_allowed": True,
                "raw_data_resale_allowed": False,
            },
        )
    with connect(db) as connection:
        row = connection.execute(
            """SELECT fulltext_redistribution_allowed,raw_data_resale_allowed
            FROM sources WHERE source_id=?""",
            (source_id,),
        ).fetchone()
        feedback_count = connection.execute("SELECT COUNT(*) FROM customer_feedback").fetchone()[0]
    assert tuple(row) == (0, 0)
    assert feedback_count == 0
