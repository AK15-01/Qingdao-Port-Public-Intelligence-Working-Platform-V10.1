from __future__ import annotations

from pathlib import Path

import pytest

from document_chunker import chunk_text, complete_sentence_excerpt
from document_processor import store_document
from event_pipeline import batch_confirm_events, create_event_for_document
from evidence_binding import locate_evidence_quote
from package_release import should_include
from platform_db import connect, initialize_database, upsert_source
from qa.evaluate_real_acceptance import calculate_human_metrics
from qa_promotion import promote_qa_events
from rag.query_router import classify_geography, detect_intent, route_query, route_results
from security_audit import release_env_warning
from workspace_store import create_workspace


def _store_event(tmp_path: Path, database_name: str = "qa.db"):
    db = tmp_path / database_name
    root = tmp_path / f"{database_name}-data"
    workspace = create_workspace({"workspace_name": database_name}, db, root)
    initialize_database(db)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "测试官方来源",
            "organization": "测试官方来源",
            "domain": "example.gov.cn",
            "list_page_url": "https://example.gov.cn/notices",
            "source_type": "政府/监管机构",
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
            "terms_status": "允许",
            "commercial_reuse_status": "允许",
            "report_use_allowed": True,
            "license_note": "自动测试来源许可说明。",
            "last_license_checked_at": "2026-07-26",
        },
        db,
    )
    text = "青岛辖区发布临时限航通知。船舶应核对公开原文并调整进出港计划。"
    document = {
        "workspace_id": workspace["workspace_id"],
        "source_id": source_id,
        "canonical_url": "https://example.gov.cn/notices/1",
        "original_url": "https://example.gov.cn/notices/1",
        "title": "青岛辖区临时限航通知",
        "publisher": "测试官方来源",
        "published_at": "2026-07-26",
        "fetched_at": "2026-07-26T09:00:00+08:00",
        "raw_html": f"<article><p>{text}</p></article>",
        "cleaned_text": text,
        "extraction_status": "成功",
        "extraction_quality": "正文结构清晰",
        "http_status": 200,
    }
    stored = store_document(document, db, root)
    document["document_id"] = stored.document_id
    event_id, _ = create_event_for_document(
        stored.document_id,
        document,
        {
            "source_id": source_id,
            "source_name": "测试官方来源",
            "source_type": "政府/监管机构",
            "category_hint": "航行警告",
        },
        workspace["workspace_id"],
        db,
        ai_enabled=False,
    )
    return db, root, workspace, source_id, stored.document_id, event_id


def _complete_label(reviewer_type: str) -> dict[str, str]:
    return {
        "document_id": "DOC-1",
        "原文URL": "https://example.gov.cn/1",
        "正确标题": "标题",
        "正确发布日期": "2026-07-26",
        "正确发布机构": "机构",
        "正文是否合格": "是",
        "是否包含乱码": "否",
        "是否包含导航噪声": "否",
        "正确类别": "航行警告",
        "事实摘要是否准确": "是",
        "evidence_quotes是否存在于原文": "是",
        "潜在影响是否合理": "是",
        "是否具有业务价值": "高",
        "是否允许进入内部研究版": "是",
        "是否允许进入客户交付版": "否",
        "人工标注状态": "已完成",
        "reviewer_type": reviewer_type,
        "reviewer_name": "复核人",
        "reviewed_at": "2026-07-26T10:00:00+08:00",
        "review_method": "打开原文逐项核对",
        "review_version": "0.4.0-beta",
    }


def test_agent_label_never_counts_as_independent_human_accuracy():
    records = [{
        "document_id": "DOC-1", "title": "标题", "published_at": "2026-07-26",
        "publisher": "机构", "quality_status": "合格", "event_id": "EVT-1",
        "category": "航行警告", "evidence_verified": 1,
    }]
    automated = calculate_human_metrics(records, [_complete_label("codex_agent")])
    assert automated["completed_count"] == 0
    assert automated["automated_or_agent_completed_count"] == 1
    assert automated["title_accuracy"] is None
    assert "待独立真人核验" in automated["status"]

    human = calculate_human_metrics(records, [_complete_label("human_user")])
    assert human["completed_count"] == 1
    assert human["title_accuracy"] == 1


def test_evidence_binding_only_allows_exact_or_whitespace_normalization():
    source = "第一句事实。\n第二句 公开事实。"
    exact = locate_evidence_quote("第一句事实。", source, document_id="DOC-1")
    whitespace = locate_evidence_quote("第二句公开事实。", source, document_id="DOC-1")
    changed = locate_evidence_quote("第二句公开事实！", source, document_id="DOC-1")
    assert exact.verification_status == "已验证" and exact.normalization_method == "exact"
    assert whitespace.verification_status == "已验证"
    assert whitespace.normalization_method == "whitespace_only"
    assert changed.verification_status == "未定位"


def test_chunks_and_citation_previews_begin_with_complete_sentence():
    text = "第一句完整事实。第二句完整事实，包含补充说明；第三句完整事实。第四句完整事实。"
    chunks = chunk_text(text, target_size=100, overlap=20, minimum=20)
    assert chunks
    assert all(not item.startswith(("设，", "动，", "的，", "了，")) for item in chunks)
    assert complete_sentence_excerpt(text, 30).startswith("第一句完整事实。")


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("当前有哪些风险？", "当前风险"),
        ("过去有哪些历史风险？", "历史风险"),
        ("哪些风险已经解除？", "已解除风险"),
        ("有什么政策机会？", "政策机会"),
        ("最近有哪些招标采购？", "招标采购"),
        ("港口作业有什么动态？", "港口运营动态"),
        ("有哪些长期行业趋势？", "长期行业趋势"),
        ("这条信息的原文出处是什么？", "来源核验"),
        ("本周和上周相比有什么变化？", "时间对比"),
    ],
)
def test_deterministic_query_intents(question: str, intent: str):
    assert detect_intent(question) == intent


def test_current_qingdao_risk_route_excludes_resolved_activity_and_other_city():
    route = route_query("青岛港当前有哪些风险？")
    results = route_results(
        [
            {"title": "青岛辖区临时限航", "chunk_text": "青岛辖区临时限航。", "status": "新增", "hybrid_score": 0.8},
            {"title": "解除青岛限航", "chunk_text": "青岛辖区解除限航。", "status": "解除", "hybrid_score": 1},
            {"title": "党建培训活动", "chunk_text": "青岛港举行党建培训。", "status": "新增", "hybrid_score": 1},
            {"title": "威海海域预警", "chunk_text": "威海海域预警。", "status": "新增", "hybrid_score": 1},
            {"title": "黄海大风预警", "chunk_text": "黄海中部可能影响青岛航线。", "status": "持续", "hybrid_score": 0.7},
        ],
        route,
    )
    assert [item["title"] for item in results] == ["青岛辖区临时限航", "黄海大风预警"]
    assert all(item["geography_level"] in {1, 2} for item in results)
    assert classify_geography("日照港活动")["geography_level"] == 4


def test_controlled_qa_promotion_requires_human_and_preserves_lineage(tmp_path: Path):
    qa_db, _, qa_workspace, _, document_id, event_id = _store_event(tmp_path, "qa.db")
    formal_db = tmp_path / "formal.db"
    formal_root = tmp_path / "formal-data"
    formal_workspace = create_workspace({"workspace_name": "正式"}, formal_db, formal_root)
    initialize_database(formal_db)

    with connect(qa_db) as connection:
        connection.execute(
            """UPDATE events SET human_verified=1,reviewer_type='codex_agent',
            reviewer_name='Codex',verified_at='2026-07-26T10:00:00+08:00' WHERE event_id=?""",
            (event_id,),
        )
        connection.commit()
    blocked = promote_qa_events(
        qa_db,
        formal_db,
        source_workspace_id=qa_workspace["workspace_id"],
        target_workspace_id=formal_workspace["workspace_id"],
        event_ids=[event_id],
        qa_run_id="QA-RUN-1",
    )
    assert not blocked.promoted and "独立真人" in blocked.blocked[event_id]

    batch_confirm_events(
        [event_id],
        qa_workspace["workspace_id"],
        qa_db,
        reviewer_type="human_user",
        reviewer_name="真实项目使用者",
        review_method="打开原文逐项勾选",
    )
    result = promote_qa_events(
        qa_db,
        formal_db,
        source_workspace_id=qa_workspace["workspace_id"],
        target_workspace_id=formal_workspace["workspace_id"],
        event_ids=[event_id],
        qa_run_id="QA-RUN-1",
    )
    assert result.promoted == (event_id,)
    with connect(formal_db) as connection:
        event = connection.execute(
            "SELECT promotion_source_event_id,qa_run_id FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        document = connection.execute(
            "SELECT promotion_source_document_id,content_hash FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        history = connection.execute("SELECT reviewer_type FROM promotion_history").fetchone()
    assert tuple(event) == (event_id, "QA-RUN-1")
    assert document["promotion_source_document_id"] == document_id and document["content_hash"]
    assert history["reviewer_type"] == "human_user"


def test_release_excludes_secrets_databases_and_qa_labels(tmp_path: Path):
    assert not should_include(Path(".env"))
    assert not should_include(Path("data/portscope.db"))
    assert not should_include(Path("qa/real_acceptance_labels.xlsx"))
    assert not should_include(Path("reports/sample.docx"))
    assert should_include(Path(".env.example"))
    root = tmp_path / "release"
    root.mkdir()
    (root / "release_manifest.json").write_text("{}", encoding="utf-8")
    (root / ".env").write_text("not-read-by-check", encoding="utf-8")
    assert "安全警告" in release_env_warning(root)
