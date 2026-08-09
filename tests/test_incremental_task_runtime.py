from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import socket
import time

from agent import AgentContext, AgentOrchestrator, ToolExecutor
from crawler.content_fetcher import ContentFetcher
from crawler.crawl_manager import CancelToken, CrawlManager
from crawler.robots_checker import RobotsDecision
from document_quality import assess_document
from operation_store import (
    claim_operation,
    finish_operation,
    get_operation,
    operation_cancel_requested,
    request_operation_cancel,
)
from platform_db import connect, initialize_database, upsert_source
from task_runtime import enqueue_background_task
from tests.test_pdf_quality_and_value import _pdf_bytes
from tests.test_source_template_pdf_upgrade import Response
from workspace_store import create_workspace


PUBLIC_DNS = lambda *args, **kwargs: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
]


class AllowRobots:
    def check(self, url, configured_status="未检查"):
        return RobotsDecision(True, "允许")


class ConditionalPDFRequester:
    def __init__(self, published_at: str) -> None:
        self.published_at = published_at
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        self.calls.append((url, headers))
        if url.endswith("/list"):
            return Response(
                '<a class="notice" href="/article/1">海上风险预警第1期</a>'
            )
        if url.endswith("/article/1"):
            if headers.get("If-None-Match") == '"article-v1"':
                return Response(
                    b"",
                    status=304,
                    headers={"ETag": '"article-v1"'},
                )
            return Response(
                f"""<html><head><title>海上风险预警第1期</title>
                <meta property="article:published_time" content="{self.published_at}">
                </head><body><div id="zoom">
                <a class="pdf" href="/files/warning-1.pdf">官方PDF附件</a>
                </div></body></html>""",
                headers={"ETag": '"article-v1"'},
            )
        if url.endswith("/files/warning-1.pdf"):
            return Response(
                _pdf_bytes(
                    "Official maritime warning issue 1. The published notice "
                    "contains public navigation facts, affected waters, valid "
                    "time, weather conditions and verification instructions. "
                    "This sentence is repeated only to provide enough text for "
                    "the deterministic quality gate. "
                ),
                content_type="application/pdf",
            )
        raise AssertionError(f"unexpected URL: {url}")


def _workspace(tmp_path: Path):
    db = tmp_path / "portscope.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "增量任务测试"}, db, root)
    initialize_database(db)
    return db, root, workspace


def _source(db: Path, workspace_id: str, *, count: int = 5) -> str:
    return upsert_source(
        {
            "workspace_id": workspace_id,
            "source_name": "官方预警测试来源",
            "organization": "官方预警测试机构",
            "domain": "example.com",
            "homepage_url": "https://example.com/",
            "list_page_url": "https://example.com/list",
            "source_type": "政府/监管机构",
            "category_hint": "海上气象",
            "adapter_type": "generic_html",
            "adapter_config": {
                "article_link_selector": "a.notice",
                "url_pattern": r"/article/",
                "content_selector": "#zoom",
                "follow_pdf_attachments": True,
                "pdf_link_selector": "a.pdf[href]",
                "pdf_max_bytes": 5 * 1024 * 1024,
                "pdf_max_pages": 10,
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
            "max_articles_per_run": count,
            "rate_limit_seconds": 2,
        },
        db,
    )


def test_http_304_skips_pdf_version_event_and_index_work(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)
    source_id = _source(db, workspace["workspace_id"])
    requester = ConditionalPDFRequester(date.today().isoformat())
    manager = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=requester,
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
            retries=0,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    )
    first = manager.run(
        workspace["workspace_id"],
        source_ids=[source_id],
        max_articles=1,
    )
    with connect(db) as connection:
        first_documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        first_events = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        first_chunks = int(connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0])
    second = manager.run(
        workspace["workspace_id"],
        source_ids=[source_id],
        max_articles=1,
        refresh_mode="recent",
    )
    with connect(db) as connection:
        counts = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "events", "document_chunks")
        )
        current = connection.execute(
            "SELECT last_http_status,last_checked_at FROM documents WHERE is_current=1"
        ).fetchone()
    pdf_calls = [
        url for url, _headers in requester.calls if url.endswith(".pdf")
    ]
    article_calls = [
        headers for url, headers in requester.calls if url.endswith("/article/1")
    ]
    assert first.new_document_count == 1
    assert second.skipped_count == 1 and second.updated_document_count == 0
    assert counts == (first_documents, first_events, first_chunks)
    assert len(pdf_calls) == 1
    assert article_calls[-1]["If-None-Match"] == '"article-v1"'
    assert tuple(current)[0] == 304 and tuple(current)[1]


def test_unchanged_content_hash_creates_no_new_version_or_event(tmp_path: Path):
    from tests.test_crawler_pipeline import Requester

    db, root, workspace = _workspace(tmp_path)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "哈希去重来源",
            "organization": "哈希去重来源",
            "domain": "example.com",
            "list_page_url": "https://example.com/list",
            "source_type": "政府/监管机构",
            "adapter_type": "generic_html",
            "adapter_config": {
                "article_link_selector": "a.notice",
                "url_pattern": r"/article/",
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
        },
        db,
    )
    article = f"""<html><head><title>公开航行信息</title>
    <meta property="article:published_time" content="{date.today().isoformat()}"></head>
    <body><article><p>第一段公开事实说明相关水域需要核对通航安排。</p>
    <p>第二段用于验证相同正文不会重复创建事件或索引。</p></article></body></html>"""
    requester = Requester(
        {
            "https://example.com/list": '<a class="notice" href="/article/1">公开航行信息</a>',
            "https://example.com/article/1": article,
        }
    )
    manager = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=requester,
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
            retries=0,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    )
    manager.run(workspace["workspace_id"], source_ids=[source_id], max_articles=1)
    calls_after_first = len(requester.calls)
    immediate = manager.run(
        workspace["workspace_id"],
        source_ids=[source_id],
        max_articles=1,
    )
    assert len(requester.calls) == calls_after_first
    assert immediate.skipped_count == 1
    duplicate = manager.run(
        workspace["workspace_id"],
        source_ids=[source_id],
        max_articles=1,
        refresh_mode="force",
    )
    with connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert duplicate.skipped_count == 1
    assert duplicate.new_event_count == 0


def test_durable_queue_prevents_duplicate_and_persists_progress_and_cancel(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)
    metadata = {
        "source_ids": [],
        "max_articles": 3,
        "refresh_mode": "new_only",
        "auto_ai": False,
    }
    first = enqueue_background_task(
        workspace["workspace_id"],
        "crawl",
        db,
        root,
        metadata=metadata,
        launch_worker=False,
    )
    rerun = enqueue_background_task(
        workspace["workspace_id"],
        "crawl",
        db,
        root,
        metadata=metadata,
        launch_worker=False,
    )
    assert first["created"] is True
    assert rerun["created"] is False
    assert first["operation_run_id"] == rerun["operation_run_id"]
    operation_id = str(first["operation_run_id"])
    claim_operation(operation_id, db, worker_pid=123)
    from operation_store import update_operation_progress

    update_operation_progress(
        operation_id,
        db,
        current_stage="下载PDF",
        current_item="第1篇",
        completed_items=1,
        total_items=3,
        success_count=1,
    )
    reloaded = get_operation(operation_id, db)
    assert reloaded["current_stage"] == "下载PDF"
    assert reloaded["completed_items"] == 1
    assert reloaded["heartbeat_at"]
    assert request_operation_cancel(operation_id, workspace["workspace_id"], db)
    assert operation_cancel_requested(operation_id, db)
    finish_operation(operation_id, db, status="cancelled")
    assert get_operation(operation_id, db)["status"] == "cancelled"


def test_stale_unclaimed_worker_task_is_recoverable(tmp_path: Path):
    from datetime import datetime, timedelta
    from operation_store import recover_stale_operations

    db, root, workspace = _workspace(tmp_path)
    queued = enqueue_background_task(
        workspace["workspace_id"],
        "index_rebuild",
        db,
        root,
        metadata={"limit": 5},
        launch_worker=False,
    )
    old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    with connect(db) as connection:
        connection.execute(
            """UPDATE operation_runs SET heartbeat_at=?,updated_at=?
            WHERE operation_run_id=?""",
            (old, old, queued["operation_run_id"]),
        )
        connection.commit()
    assert recover_stale_operations(db, timeout_minutes=30) == 1
    recovered = get_operation(str(queued["operation_run_id"]), db)
    assert recovered["status"] == "failed"
    assert "Worker未在3分钟内领取" in recovered["error_summary"]


def test_cancel_request_stops_at_next_article_boundary(tmp_path: Path):
    from tests.test_crawler_pipeline import Requester

    db, root, workspace = _workspace(tmp_path)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "取消边界来源",
            "organization": "取消边界来源",
            "domain": "example.com",
            "list_page_url": "https://example.com/list",
            "adapter_type": "generic_html",
            "adapter_config": {
                "article_link_selector": "a.notice",
                "url_pattern": r"/article/",
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
        },
        db,
    )
    links = "".join(
        f'<a class="notice" href="/article/{index}">第{index}篇</a>'
        for index in range(1, 4)
    )
    pages = {"https://example.com/list": links}
    for index in range(1, 4):
        pages[f"https://example.com/article/{index}"] = f"""<html><head>
        <title>第{index}篇公开信息</title>
        <meta property="article:published_time" content="{date.today().isoformat()}">
        </head><body><article><p>第一段公开事实内容用于取消边界测试。</p>
        <p>第二段说明已完成内容保留，后续文章停止。</p></article></body></html>"""
    requester = Requester(pages)
    queued = enqueue_background_task(
        workspace["workspace_id"],
        "crawl",
        db,
        root,
        metadata={"max_articles": 3},
        launch_worker=False,
    )
    operation_id = str(queued["operation_run_id"])
    claim_operation(operation_id, db)
    token = CancelToken(lambda: operation_cancel_requested(operation_id, db))
    partial_visible: list[int] = []

    def progress(phase, _payload):
        if phase == "文章处理完成":
            with connect(db) as connection:
                partial_visible.append(
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM documents"
                        ).fetchone()[0]
                    )
                )
            request_operation_cancel(operation_id, workspace["workspace_id"], db)

    result = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=requester,
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
            retries=0,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    ).run(
        workspace["workspace_id"],
        source_ids=[source_id],
        max_articles=3,
        cancel=token,
        progress=progress,
        operation_run_id=operation_id,
    )
    article_calls = [url for url in requester.calls if "/article/" in url]
    assert result.status == "已中止"
    assert article_calls == ["https://example.com/article/1"]
    operation = get_operation(operation_id, db)
    assert operation["status"] == "cancelled"
    assert operation["heartbeat_at"] and operation["completed_items"] == 1
    assert partial_visible == [1]
    with connect(db) as connection:
        linked_operations = int(
            connection.execute(
                """SELECT COUNT(*) FROM operation_runs
                WHERE external_ref_type='crawl_run'
                AND external_ref_id=?""",
                (result.crawl_run_id,),
            ).fetchone()[0]
        )
    assert linked_operations == 1


def test_legacy_migration_running_snapshot_converges_after_crawl_finishes(
    tmp_path: Path,
):
    from platform_db import now_iso

    db = tmp_path / "migration.db"
    initialize_database(db)
    started = now_iso()
    with connect(db) as connection:
        connection.execute(
            """INSERT INTO crawl_runs(
            crawl_run_id,workspace_id,started_at,status,updated_at
            ) VALUES(?,?,?,?,?)""",
            ("CRAWL-LIVE", "WS-LIVE", started, "运行中", started),
        )
        connection.commit()
    initialize_database(db)
    with connect(db) as connection:
        first = connection.execute(
            """SELECT operation_run_id,status FROM operation_runs
            WHERE external_ref_type='crawl_run' AND external_ref_id='CRAWL-LIVE'"""
        ).fetchone()
        connection.execute(
            """UPDATE crawl_runs SET status='完成',finished_at=?,duration_ms=25,
            updated_at=? WHERE crawl_run_id='CRAWL-LIVE'""",
            (now_iso(), now_iso()),
        )
        connection.commit()
    assert first["status"] == "running"
    initialize_database(db)
    with connect(db) as connection:
        final = connection.execute(
            """SELECT operation_run_id,status,finished_at FROM operation_runs
            WHERE external_ref_type='crawl_run' AND external_ref_id='CRAWL-LIVE'"""
        ).fetchone()
    assert final["operation_run_id"] == first["operation_run_id"]
    assert final["status"] == "succeeded" and final["finished_at"]


def test_source_time_budget_stops_remaining_articles_as_partial(tmp_path: Path):
    from tests.test_crawler_pipeline import Requester

    db, root, workspace = _workspace(tmp_path)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "时间预算来源",
            "organization": "时间预算来源",
            "domain": "example.com",
            "list_page_url": "https://example.com/list",
            "adapter_type": "generic_html",
            "adapter_config": {
                "article_link_selector": "a.notice",
                "url_pattern": r"/article/",
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
        },
        db,
    )
    body = f"""<html><head><title>时间预算测试</title>
    <meta property="article:published_time" content="{date.today().isoformat()}">
    </head><body><article><p>第一段公开事实用于时间预算测试。</p>
    <p>第二段保证正文质量门禁可以正常通过。</p></article></body></html>"""

    class SlowRequester(Requester):
        def get(self, url, **kwargs):
            if "/article/" in url:
                time.sleep(1.05)
            return super().get(url, **kwargs)

    requester = SlowRequester(
        {
            "https://example.com/list": (
                '<a class="notice" href="/article/1">第一篇</a>'
                '<a class="notice" href="/article/2">第二篇</a>'
            ),
            "https://example.com/article/1": body,
            "https://example.com/article/2": body.replace("测试", "测试二"),
        }
    )
    result = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=requester,
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
            retries=0,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    ).run(
        workspace["workspace_id"],
        source_ids=[source_id],
        max_articles=2,
        source_time_budget_seconds=1,
    )
    with connect(db) as connection:
        source_run = connection.execute(
            "SELECT status,request_count FROM crawl_source_runs"
        ).fetchone()
    assert result.status == "部分完成"
    assert [url for url in requester.calls if "/article/" in url] == [
        "https://example.com/article/1"
    ]
    assert tuple(source_run) == ("partially_succeeded", 2)


def test_official_warning_template_similarity_is_warning_when_facts_change():
    common = (
        "山东海事局发布海上风险预警。请相关单位密切关注天气变化，"
        "加强值班值守，船舶应核对最新航行安排并落实安全措施。"
    )
    previous = (
        "海上风险预警第17期\n2026年7月20日10时发布黄色预警。\n"
        "预计20日夜间黄海中部能见度低于500米。" + common * 3
    )
    current = (
        "海上风险预警第18期\n2026年7月21日10时发布橙色预警。\n"
        "预计21日夜间黄海北部能见度低于300米。" + common * 3
    )
    quality = assess_document(
        title="海上风险预警第18期",
        text=current,
        published_at="2026-07-21",
        template_texts=[previous],
    )
    assert quality.processing_allowed
    assert quality.metrics["template_similarity"] >= 0.88
    assert quality.metrics["template_similarity_warning"] == 1


def test_ai_single_document_exception_does_not_block_next_document(
    tmp_path: Path,
    monkeypatch,
):
    from deepseek_service import (
        DeepSeekSettings,
        EventExtraction,
        ExtractionOutcome,
    )
    from document_chunker import build_chunk_rows
    from document_processor import store_document
    from event_pipeline import create_event_for_document, reprocess_pending_ai

    db, root, workspace = _workspace(tmp_path)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "AI补跑测试来源",
            "organization": "AI补跑测试机构",
            "domain": "example.com",
            "list_page_url": "https://example.com/list",
            "source_type": "政府/监管机构",
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
        },
        db,
    )
    document_ids = []
    for index in (1, 2):
        text = (
            f"第{index}篇公开测试信息显示相关水域需要核对航行安排。"
            "第二句提供完整事实上下文，仅用于验证单篇失败不会阻塞整批。"
        )
        values = {
            "workspace_id": workspace["workspace_id"],
            "source_id": source_id,
            "canonical_url": f"https://example.com/ai/{index}",
            "original_url": f"https://example.com/ai/{index}",
            "title": f"第{index}篇公开航行信息",
            "publisher": "AI补跑测试机构",
            "published_at": date.today().isoformat(),
            "fetched_at": f"{date.today().isoformat()}T09:00:00+08:00",
            "raw_html": f"<article><p>{text}</p></article>",
            "cleaned_text": text,
            "extraction_status": "成功",
            "quality_status": "合格",
            "processing_allowed": True,
            "http_status": 200,
        }
        chunks = build_chunk_rows(
            text,
            {
                "workspace_id": workspace["workspace_id"],
                "source_id": source_id,
            },
        )
        stored = store_document(values, db, root, chunks)
        document_ids.append(stored.document_id)
        values["document_id"] = stored.document_id
        create_event_for_document(
            stored.document_id,
            values,
            {
                "source_id": source_id,
                "source_name": "AI补跑测试来源",
                "source_type": "政府/监管机构",
                "category_hint": "航行警告",
            },
            workspace["workspace_id"],
            db,
            ai_enabled=False,
        )

    calls = {"count": 0}

    def fake_extract(_text, _metadata, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("mock single document timeout")
        return ExtractionOutcome(
            True,
            EventExtraction(
                title="第二篇公开航行信息",
                event_date=date.today().isoformat(),
                category="航行警告",
                factual_summary="公开信息显示相关水域需要核对航行安排。",
                potential_impact="分析：可能影响通航核对流程。",
                affected_area="相关水域",
                affected_period="待核实",
                status="待核实",
                risk_terms=["航行"],
                opportunity_terms=[],
                suggested_action="核对原始公开来源。",
                related_event_keywords=[],
                confidence=0.8,
                evidence_quotes=["公开测试信息显示相关水域需要核对航行安排"],
            ),
            "mock-fast-model",
        )

    monkeypatch.setattr("event_pipeline.extract_event", fake_extract)
    result = reprocess_pending_ai(
        workspace["workspace_id"],
        db,
        limit=2,
        settings=DeepSeekSettings("mock-key", "mock-fast-model", max_retries=0),
        time_budget_seconds=30,
    )
    with connect(db) as connection:
        statuses = {
            row["document_id"]: row["ai_status"]
            for row in connection.execute(
                "SELECT document_id,ai_status FROM documents"
            )
        }
    assert result["input"] == 2
    assert result["failed"] == 1 and result["success"] == 1
    assert statuses[document_ids[0]] == "待AI处理"
    assert statuses[document_ids[1]] == "已完成"


def test_daily_limits_agent_limits_and_removed_streamlit_deprecation():
    config = json.loads(
        (Path(__file__).parents[1] / "config" / "default_sources.json").read_text(
            encoding="utf-8"
        )
    )
    maritime = next(
        item for item in config if item["source_name"] == "山东海事局海上风险预警"
    )
    assert maritime["max_articles_per_run"] == 5
    context = AgentContext(
        {"workspace_id": "WS-LIMIT", "ai_enabled": False},
        Path("unused.db"),
        Path("."),
    )
    orchestrator = AgentOrchestrator(ToolExecutor(context))
    assert orchestrator.max_tool_rounds == 4
    assert orchestrator.max_tool_calls == 8
    assert orchestrator.time_budget_seconds == 90
    project_root = Path(__file__).parents[1]
    offenders = [
        path
        for path in project_root.rglob("*.py")
        if ".venv" not in path.parts
        and "tests" not in path.parts
        and "use_container_width" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
