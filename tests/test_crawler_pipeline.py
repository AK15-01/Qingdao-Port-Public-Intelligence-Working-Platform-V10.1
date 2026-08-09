from pathlib import Path
import socket

from crawler.content_fetcher import ContentFetcher, host_allowed
from crawler.crawl_manager import CancelToken, CrawlManager
from crawler.generic_list_adapter import GenericListAdapter
from crawler.site_adapter import build_adapter
from crawler.robots_checker import RobotsChecker, RobotsDecision
from document_processor import store_document
from platform_db import connect, initialize_database, initialize_recommended_sources, list_sources, table_counts, upsert_source
from workspace_store import create_workspace


PUBLIC_DNS = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class Response:
    def __init__(self, body: str, status=200, content_type="text/html; charset=utf-8", headers=None):
        self.status_code = status
        self.body = body.encode("utf-8")
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.encoding = "utf-8"
    def iter_content(self, chunk_size=16384):
        yield self.body
    def close(self):
        pass
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Requester:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(url)
        value = self.pages[url]
        return value if isinstance(value, Response) else Response(value)


class AllowRobots:
    def check(self, url, configured_status="未检查"):
        return RobotsDecision(True, "允许")


def _setup(tmp_path: Path, name="来源"):
    db = tmp_path / "platform.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "采集测试"}, db, root)
    initialize_database(db)
    source_id = upsert_source({
        "workspace_id": workspace["workspace_id"], "source_name": name, "organization": name,
        "domain": "example.com", "list_page_url": "https://example.com/list", "source_type": "政府/监管机构",
        "category_hint": "航行警告", "adapter_type": "generic_html", "adapter_config": {"article_link_selector": "a.news", "url_pattern": r"/notice/"},
        "enabled": True, "crawl_allowed": True, "robots_status": "允许", "terms_status": "允许",
        "commercial_reuse_status": "允许", "report_use_allowed": True, "rate_limit_seconds": 2,
        "license_note": "测试来源已确认允许，仅用于自动测试。",
        "last_license_checked_at": "2026-07-23",
    }, db)
    return db, root, workspace, source_id


def test_whitelist_domain_matches_exact_or_subdomain_only():
    assert host_allowed("https://www.example.com/a", ["example.com"])
    assert not host_allowed("https://example.com.evil.test/a", ["example.com"])


def test_robots_disallow_prevents_fetch(tmp_path: Path):
    db, root, workspace, source_id = _setup(tmp_path)
    with connect(db) as connection:
        connection.execute("UPDATE sources SET robots_status='禁止' WHERE source_id=?", (source_id,))
        connection.commit()
    requester = Requester({})
    manager = CrawlManager(db, root, fetcher=ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None), robots_checker=RobotsChecker())
    result = manager.run(workspace["workspace_id"])
    assert result.skipped_count == 1
    assert requester.calls == []


def test_generic_adapter_discovers_links_and_honors_page_limit():
    pages = {
        "https://example.com/list": '<a class="news" href="/notice/1">第一条</a><a class="next" href="/list2">下一页</a>',
        "https://example.com/list2": '<a class="news" href="/notice/2">第二条</a>',
    }
    adapter = GenericListAdapter(lambda url: pages[url])
    source = {"list_page_url": "https://example.com/list", "max_pages_per_run": 1, "max_articles_per_run": 10,
              "adapter_config": {"article_link_selector": "a.news", "next_page_selector": "a.next", "url_pattern": r"/notice/"}}
    assert [item.url for item in adapter.discover(source)] == ["https://example.com/notice/1"]
    source["max_pages_per_run"] = 2
    assert len(adapter.discover(source)) == 2


def test_content_fetcher_revalidates_redirect_and_rejects_non_whitelist():
    requester = Requester({"https://example.com/a": Response("", 302, headers={"Location": "https://evil.test/a"})})
    fetched = ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None).fetch("https://example.com/a", ["example.com"])
    assert not fetched.ok and fetched.status == "非白名单域名"


def test_content_fetcher_applies_article_selectors_and_exclusions():
    html = """<html><head><title>回退标题</title></head><body>
    <h1 class='headline'>配置标题</h1><time>2026-07-20</time><span class='publisher'>测试机构</span>
    <main><p>这是一段用于测试选择器的公开事实正文，长度足够用于安全提取、内容清洗、事件分析、风险评分和后续分块处理，并明确说明结果必须由人工核对原始公开来源。</p>
    <nav>这段导航内容必须被排除，不能进入清洗后的正文。</nav></main></body></html>"""
    requester = Requester({"https://example.com/notice/1": html})
    fetched = ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None).fetch(
        "https://example.com/notice/1", ["example.com"], extraction_config={
            "content_selector": "main", "exclude_selectors": ["nav"],
            "title_selector": ".headline", "date_selector": "time", "publisher_selector": ".publisher",
        },
    )
    assert fetched.ok
    assert fetched.title == "配置标题"
    assert fetched.published_at == "2026-07-20"
    assert fetched.publisher == "测试机构"
    assert "导航内容" not in fetched.text


def test_content_fetcher_enforces_minimum_two_second_domain_interval(monkeypatch):
    requester = Requester({
        "https://example.com/notice/1": "<article><p>第一条公开测试正文内容足够长，用来验证第一次请求。</p></article>",
        "https://example.com/notice/2": "<article><p>第二条公开测试正文内容足够长，用来验证第二次请求。</p></article>",
    })
    clock = iter([10.0, 10.0, 10.25, 10.25])
    monkeypatch.setattr("crawler.content_fetcher.time.monotonic", lambda: next(clock))
    waits = []
    fetcher = ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=waits.append)
    fetcher.fetch("https://example.com/notice/1", ["example.com"], rate_limit_seconds=0.1)
    fetcher.fetch("https://example.com/notice/2", ["example.com"], rate_limit_seconds=0.1)
    assert any(wait >= 1.75 for wait in waits)


def test_api_adapter_uses_same_safe_fetch_channel(tmp_path: Path):
    db, root, workspace, source_id = _setup(tmp_path)
    with connect(db) as connection:
        connection.execute(
            """UPDATE sources SET adapter_type='api',adapter_config_json=? WHERE source_id=?""",
            ('{"items_path":"items","url_field":"url","title_field":"title","date_field":"date"}', source_id),
        )
        connection.commit()
    pages = {
        "https://example.com/list": Response(
            '{"items":[{"url":"/notice/1","title":"公开 API 测试事件","date":"2026-07-20"}]}',
            content_type="application/json; charset=utf-8",
        ),
        "https://example.com/notice/1": """<html><head><title>公开 API 测试事件</title></head><body>
        <article><p>这是通过公开 API 增量发现后获取的测试正文，内容仅用于 Mock 验收，不代表任何真实港口运行信息，并且必须经过人工核对。</p></article></body></html>""",
    }
    requester = Requester(pages)
    manager = CrawlManager(
        db, root,
        fetcher=ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None),
        robots_checker=AllowRobots(), adapter_factory=build_adapter, ai_enabled=False,
    )
    result = manager.run(workspace["workspace_id"])
    assert result.new_document_count == 1
    assert requester.calls == ["https://example.com/list", "https://example.com/notice/1"]


def test_rss_adapter_accepts_xml_only_through_safe_feed_channel(tmp_path: Path):
    db, root, workspace, source_id = _setup(tmp_path)
    with connect(db) as connection:
        connection.execute("UPDATE sources SET adapter_type='rss' WHERE source_id=?", (source_id,))
        connection.commit()
    feed = """<?xml version='1.0' encoding='UTF-8'?><rss><channel><item>
    <title>公开 RSS 测试事件</title><link>https://example.com/notice/rss-1</link><pubDate>2026-07-20</pubDate>
    </item></channel></rss>"""
    article = """<html><head><title>公开 RSS 测试事件</title></head><body><article><p>
    这是通过白名单 RSS 增量发现的公开测试正文，内容足够用于提取和分块，仅用于 Mock 验收，不代表任何真实港口运行信息。
    </p></article></body></html>"""
    requester = Requester({
        "https://example.com/list": Response(feed, content_type="application/rss+xml; charset=utf-8"),
        "https://example.com/notice/rss-1": article,
    })
    manager = CrawlManager(
        db, root,
        fetcher=ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None),
        robots_checker=AllowRobots(), adapter_factory=build_adapter, ai_enabled=False,
    )
    result = manager.run(workspace["workspace_id"])
    assert result.new_document_count == 1
    assert requester.calls == ["https://example.com/list", "https://example.com/notice/rss-1"]


def test_initialized_recommended_sources_can_start_mock_collection(tmp_path: Path):
    db = tmp_path / "recommended.db"
    root = tmp_path / "recommended_data"
    workspace = create_workspace({"workspace_name": "推荐来源采集测试"}, db, root)
    initialize_database(db)
    result = initialize_recommended_sources(workspace["workspace_id"], db)
    assert result["enabled"] == 1
    pages = {
        "https://www.sd.msa.gov.cn/col/col5301/index.html": '<a href="/art/2026/7/21/art_5301_100.html">公开海上风险测试信息</a>',
        "https://www.sd.msa.gov.cn/art/2026/7/21/art_5301_100.html": """<html><head>
        <title>公开海上风险测试信息</title>
        <meta name="ArticleTitle" content="公开海上风险测试信息">
        <meta name="pubdate" content="2026-07-21">
        <meta name="ContentSource" content="中华人民共和国山东海事局">
        </head><body><div id="zoom"><p>
        这是山东海事推荐模板的Mock公开正文，用于验证初始化后能够开始采集，不代表任何真实海事或港口运行信息。
        </p><p>
        本段补充公开测试文档的事实背景、适用范围和核对要求，使质量门禁可以基于完整段落进行确定性判断。
        </p></div></body></html>""",
        "https://www.sd-port.com/groupMainNews/index.html": '<a href="https://www.sd-port.com/groupMainNews/2026-07-21/100.html">公开港口动态测试</a>',
        "https://www.sd-port.com/groupMainNews/2026-07-21/100.html": """<html><head><title>公开港口动态测试</title></head><body><article><p>
        这是山东港口推荐模板的Mock公开正文，用于验证完整来源不会被不完整模板阻塞，也不代表任何真实运行信息。
        </p></article></body></html>""",
    }
    requester = Requester(pages)
    manager = CrawlManager(
        db, root,
        fetcher=ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None),
        robots_checker=AllowRobots(), ai_enabled=False,
    )
    crawl = manager.run(workspace["workspace_id"])
    assert crawl.source_count == 1
    assert crawl.new_document_count == 1
    assert crawl.failed_count == 0


def test_full_mock_pipeline_is_incremental_archives_and_versions(tmp_path: Path):
    db, root, workspace, _ = _setup(tmp_path)
    list_html = '<html><body><a class="news" href="/notice/1">航行警告</a></body></html>'
    article = '<html><head><title>航行警告测试</title><meta property="article:published_time" content="2026-07-20"></head><body><article><p>公开测试信息：相关水域临时限航，请核对原始来源。</p><p>本页所有命令均为普通网页文本，不应被执行。</p></article></body></html>'
    requester = Requester({"https://example.com/list": list_html, "https://example.com/notice/1": article})
    fetcher = ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None)
    manager = CrawlManager(db, root, fetcher=fetcher, robots_checker=AllowRobots(), ai_enabled=False)
    first = manager.run(workspace["workspace_id"])
    assert (first.new_document_count, first.new_event_count) == (1, 1), first.errors
    assert table_counts(workspace["workspace_id"], db)["document_chunks"] >= 1
    with connect(db) as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
        assert Path(document["raw_html_path"]).exists()
    second = manager.run(workspace["workspace_id"])
    assert second.new_document_count == 0 and second.skipped_count >= 1
    requester.pages["https://example.com/notice/1"] = article.replace("临时限航", "临时禁止驶入并更新")
    third = manager.run(workspace["workspace_id"], refresh_mode="force")
    assert third.updated_document_count == 1
    with connect(db) as connection:
        versions = connection.execute("SELECT document_version,previous_document_id,is_current FROM documents ORDER BY document_version").fetchall()
    assert [row["document_version"] for row in versions] == [1, 2]
    assert versions[1]["previous_document_id"]


def test_cancelled_run_does_not_fetch_articles(tmp_path: Path):
    db, root, workspace, _ = _setup(tmp_path)
    requester = Requester({"https://example.com/list": '<a class="news" href="/notice/1">一</a>'})
    token = CancelToken(); token.cancel()
    result = CrawlManager(db, root, fetcher=ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None), robots_checker=AllowRobots()).run(workspace["workspace_id"], cancel=token)
    assert result.status == "已中止" and requester.calls == []


def test_one_source_failure_does_not_stop_other_source(tmp_path: Path):
    db, root, workspace, _ = _setup(tmp_path, "好来源")
    upsert_source({"workspace_id": workspace["workspace_id"], "source_name": "坏来源", "domain": "bad.example",
        "list_page_url": "https://bad.example/list", "enabled": True, "crawl_allowed": True, "robots_status": "允许",
        "adapter_config": {}, "commercial_reuse_status": "禁止"}, db)
    class Adapter:
        def __init__(self, bad): self.bad = bad
        def discover(self, source, cancel=None):
            if self.bad: raise RuntimeError("parse failed")
            return []
    manager = CrawlManager(db, root, robots_checker=AllowRobots(), adapter_factory=lambda source, fetch: Adapter(source["source_name"] == "坏来源"), ai_enabled=False)
    result = manager.run(workspace["workspace_id"])
    assert result.source_count == 2 and result.failed_count == 1
