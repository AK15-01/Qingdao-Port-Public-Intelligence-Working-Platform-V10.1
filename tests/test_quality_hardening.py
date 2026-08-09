from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from commercial_report import ReportOptions
from crawler.content_fetcher import ContentFetcher
from crawler.crawl_manager import CrawlManager
from crawler.official_adapters import ShandongMaritimeAdapter, ShandongPortAdapter
from document_quality import assess_document
from platform_db import connect, initialize_database, recover_stale_crawl_runs
from platform_report import ReportEligibilityError, generate_platform_report
from qa.evaluate_golden import evaluate
from tests.test_crawler_pipeline import AllowRobots, PUBLIC_DNS, Requester, _setup
from tests.test_deepseek_and_rag import _store
from workspace_store import create_workspace, workspace_paths


def test_encoding_and_navigation_noise_are_blocked_before_processing():
    broken = assess_document(
        title="å±±ä¸æ¸¯å£Ã�",
        text="å±±ä¸æ¸¯å£Ã�" * 20,
        published_at="2026-07-20",
    )
    assert broken.status == "编码异常"
    assert not broken.processing_allowed and not broken.report_allowed
    noisy = assess_document(
        title="测试网站",
        text=("首页 新闻资讯 集团概况 业务服务 联系我们 " * 5),
        published_at="2026-07-20",
        site_names=["测试网站"],
    )
    assert noisy.status == "正文质量不足"
    assert "导航" in noisy.note and not noisy.processing_allowed


def test_generic_title_and_missing_date_fail_report_gate_without_becoming_encoding_error():
    quality = assess_document(
        title="首页",
        text="第一段公开测试正文内容用于验证报告门禁。\n第二段正文结构完整但故意不提供发布日期。",
        site_names=["测试网站"],
    )
    assert quality.processing_allowed
    assert not quality.report_allowed
    assert "标题" in quality.note and "发布日期" in quality.note


def test_shandong_maritime_adapter_reads_xml_cdata_and_article_profile():
    html = """<script type="text/xml"><datastore><![CDATA[
    <li><a class="font14" href="/art/2026/7/14/art_5301_1.html">解除测试预警</a><span>2026-07-14</span></li>
    ]]></datastore></script>"""
    source = {
        "list_page_url": "https://www.sd.msa.gov.cn/col/col5301/index.html",
        "max_pages_per_run": 1, "max_articles_per_run": 5,
        "adapter_config": {
            "article_link_selector": "a.font14[href]", "allowed_path_prefix": "/art/",
            "url_pattern": r"/art/20\d{2}/",
        },
    }
    adapter = ShandongMaritimeAdapter(lambda _url: html)
    items = adapter.discover(source)
    assert [(item.title, item.published_at) for item in items] == [("解除测试预警", "2026-07-14")]
    profile = adapter.article_extraction_config(source)
    assert profile["content_selector"] == "#zoom" and "pubdate" in profile["date_selector"]


def test_shandong_port_adapter_uses_list_item_date_and_discovered_title():
    html = """<div class="right_content"><ul><li>
    <a href="/groupMainNews/2026-07-20/123.html">公开港口动态</a><span>2026-07-20</span>
    </li></ul></div>"""
    source = {
        "list_page_url": "https://www.sd-port.com/groupMainNews/index.html",
        "max_pages_per_run": 1, "max_articles_per_run": 5,
        "adapter_config": {
            "article_link_selector": ".right_content li a[href]",
            "allowed_path_prefix": "/groupMainNews/",
            "url_pattern": r"/groupMainNews/20\d{2}-\d{2}-\d{2}/\d+\.html$",
        },
    }
    adapter = ShandongPortAdapter(lambda _url: html)
    item = adapter.discover(source)[0]
    assert item.title == "公开港口动态" and item.published_at == "2026-07-20"
    assert adapter.article_extraction_config(source)["prefer_discovered_title"] is True


def test_post_fetch_date_range_gate_archives_but_creates_no_event(tmp_path: Path):
    db, root, workspace, _ = _setup(tmp_path)
    pages = {
        "https://example.com/list": '<a class="news" href="/notice/old">无列表日期的旧公告</a>',
        "https://example.com/notice/old": """<html><head><title>旧公告</title>
        <meta property="article:published_time" content="2025-01-02"></head><body><article>
        <p>第一段公开测试事实足够用于正文质量检查。</p>
        <p>第二段说明该内容日期范围之外，不应创建本期事件。</p></article></body></html>""",
    }
    manager = CrawlManager(
        db, root,
        fetcher=ContentFetcher(requester=Requester(pages), resolver=PUBLIC_DNS, sleeper=lambda _: None),
        robots_checker=AllowRobots(), ai_enabled=False,
    )
    result = manager.run(workspace["workspace_id"], start_date="2026-07-01", end_date="2026-07-31")
    assert result.out_of_range_count == 1 and result.new_event_count == 0
    with connect(db) as connection:
        document = connection.execute("SELECT extraction_status,ai_status FROM documents").fetchone()
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert tuple(document) == ("日期范围外", "不处理") and event_count == 0


@pytest.mark.parametrize(
    ("title", "body", "expected_status", "counter"),
    [
        ("å±±ä¸æ¸¯å£Ã�", "å±±ä¸æ¸¯å£Ã�" * 20, "编码异常", "encoding_blocked_count"),
        ("测试网站", "首页 新闻资讯 集团概况 业务服务 联系我们 " * 6, "正文质量不足", "noise_blocked_count"),
    ],
)
def test_pipeline_archives_quality_failures_without_ai_chunks_or_events(
    tmp_path: Path, title: str, body: str, expected_status: str, counter: str,
):
    db, root, workspace, _ = _setup(tmp_path)
    pages = {
        "https://example.com/list": '<a class="news" href="/notice/bad">质量异常测试</a>',
        "https://example.com/notice/bad": (
            f"<html><head><title>{title}</title><meta property='article:published_time' content='2026-07-20'>"
            f"</head><body><article><p>{body}</p><p>{body}</p></article></body></html>"
        ),
    }
    result = CrawlManager(
        db, root,
        fetcher=ContentFetcher(requester=Requester(pages), resolver=PUBLIC_DNS, sleeper=lambda _: None),
        robots_checker=AllowRobots(), ai_enabled=True,
    ).run(workspace["workspace_id"])
    assert getattr(result, counter) == 1 and result.new_event_count == 0 and result.api_call_count == 0
    with connect(db) as connection:
        document = connection.execute("SELECT extraction_status,ai_status FROM documents").fetchone()
        chunks = connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
    assert tuple(document) == (expected_status, "质量门禁拦截") and chunks == 0


def test_stale_crawl_run_is_recovered_as_interrupted(tmp_path: Path):
    db = tmp_path / "stale.db"
    initialize_database(db)
    old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
    with connect(db) as connection:
        connection.execute(
            "INSERT INTO crawl_runs(crawl_run_id,workspace_id,started_at,status,updated_at) VALUES(?,?,?,?,?)",
            ("CRAWL-STALE", "WS-TEST", old, "运行中", old),
        )
        connection.commit()
    assert recover_stale_crawl_runs(db) == 1
    with connect(db) as connection:
        row = connection.execute("SELECT status,finished_at,error_summary FROM crawl_runs").fetchone()
    assert row["status"] == "异常中断" and row["finished_at"] and "自动恢复" in row["error_summary"]


def test_zero_event_internal_report_is_blocked_unless_blank_template_is_explicit(tmp_path: Path):
    db = tmp_path / "empty.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "空报告测试"}, db, root)
    paths = workspace_paths(workspace["workspace_id"], root)
    with pytest.raises(ReportEligibilityError, match="没有足够证据"):
        generate_platform_report(
            workspace, paths, client=None, start_date="2026-07-01", end_date="2026-07-31",
            report_title="内部研究", analyst="测试", db_path=db, report_mode="内部研究版",
        )
    artifacts = generate_platform_report(
        workspace, paths, client=None, start_date="2026-07-01", end_date="2026-07-31",
        report_title="内部研究", analyst="测试", db_path=db, report_mode="内部研究版",
        options=ReportOptions(allow_empty_template=True),
    )
    assert "空白模板" in artifacts.html_path.read_text(encoding="utf-8")


def test_internal_report_never_labels_unverified_event_as_human_confirmed(tmp_path: Path):
    db, root, workspace, _, _, event_id, _ = _store(tmp_path)
    with connect(db) as connection:
        connection.execute("UPDATE events SET extraction_confidence=0.8 WHERE event_id=?", (event_id,))
        connection.commit()
    artifacts = generate_platform_report(
        workspace, workspace_paths(workspace["workspace_id"], root), client=None,
        start_date="2026-07-01", end_date="2026-07-31", report_title="真实性测试",
        analyst="测试", db_path=db, report_mode="内部研究版",
    )
    html = artifacts.html_path.read_text(encoding="utf-8")
    assert event_id in artifacts.event_ids
    assert "人工确认 0 条、未确认 1 条" in html
    assert "人工确认：</strong>否" in html
    assert "1 条经人工确认" not in html


def test_golden_corpus_has_at_least_thirty_documents_and_full_gate_accuracy():
    payload = evaluate()
    metrics = payload["metrics"]
    assert metrics["fixture_count"] >= 30
    assert metrics["title_accuracy"] == 1.0
    assert metrics["report_block_accuracy"] == 1.0
