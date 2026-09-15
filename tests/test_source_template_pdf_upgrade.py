from __future__ import annotations

from pathlib import Path
import socket

from crawler.content_fetcher import ContentFetcher
from crawler.crawl_manager import (
    CrawlManager,
    list_pdf_attachment_rework_candidates,
)
from crawler.robots_checker import RobotsDecision
from document_processor import store_document
from platform_db import (
    connect,
    initialize_database,
    initialize_recommended_sources,
    list_sources,
    upsert_source,
)
from tests.test_pdf_quality_and_value import _pdf_bytes
from workspace_store import create_workspace


PUBLIC_DNS = lambda *args, **kwargs: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
]


class Response:
    def __init__(
        self,
        body: str | bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.encoding = "utf-8"

    @property
    def apparent_encoding(self) -> str:
        return "utf-8"

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        return None


class Requester:
    def __init__(self, responses: dict[str, Response | str | bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        value = self.responses[url]
        return value if isinstance(value, Response) else Response(value)


class AllowRobots:
    def check(self, url, configured_status="未检查"):
        return RobotsDecision(True, "允许")


def _workspace(tmp_path: Path):
    db = tmp_path / "portscope.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "来源升级测试"}, db, root)
    initialize_database(db)
    return db, root, workspace


def _old_maritime_source(db: Path, workspace_id: str, *, enabled: bool = False) -> str:
    return upsert_source(
        {
            "workspace_id": workspace_id,
            "source_name": "山东海事局海上风险预警",
            "organization": "中华人民共和国山东海事局",
            "domain": "www.sd.msa.gov.cn",
            "homepage_url": "https://www.sd.msa.gov.cn/",
            "list_page_url": "https://www.sd.msa.gov.cn/col/col5301/index.html",
            "source_type": "政府/监管机构",
            "category_hint": "海上气象",
            "adapter_type": "shandong_maritime",
            "adapter_config": {
                "article_link_selector": "a.font14[href]",
                "allowed_path_prefix": "/art/",
                "url_pattern": r"^https://www\.sd\.msa\.gov\.cn/art/20\d{2}/",
                "content_selector": ".user-custom-selector",
            },
            "enabled": enabled,
            "crawl_allowed": enabled,
            "robots_status": "允许",
            "terms_status": "用户已复核",
            "commercial_reuse_status": "用户判断未明确",
            "license_note": "用户自定义许可备注，不得覆盖。",
            "rate_limit_seconds": 9,
            "report_use_allowed": False,
            "customer_summary_allowed": False,
            "fulltext_redistribution_allowed": False,
            "raw_data_resale_allowed": False,
        },
        db,
    )


def test_existing_source_receives_missing_pdf_keys_without_overwriting_user_fields(
    tmp_path: Path,
):
    db, _, workspace = _workspace(tmp_path)
    source_id = _old_maritime_source(db, workspace["workspace_id"], enabled=False)
    dry = initialize_recommended_sources(
        workspace["workspace_id"],
        db,
        dry_run=True,
    )
    plan = next(
        item
        for item in dry["template_sync"]
        if item["source_id"] == source_id
    )
    added_names = {item["field"] for item in plan["added_fields"]}
    assert {
        "adapter_config.follow_pdf_attachments",
        "adapter_config.pdf_link_selector",
        "adapter_config.pdf_max_bytes",
        "adapter_config.pdf_max_pages",
    }.issubset(added_names)
    assert any(
        item["field"] == "adapter_config.content_selector"
        and item["action"] == "保留数据库现值"
        for item in plan["conflicts"]
    )

    applied = initialize_recommended_sources(workspace["workspace_id"], db)
    assert applied["updated"] == 1
    assert Path(applied["backup_path"]).is_file()
    source = next(
        item
        for item in list_sources(workspace["workspace_id"], db)
        if item["source_id"] == source_id
    )
    assert source["source_id"] == source_id
    assert source["adapter_config"]["follow_pdf_attachments"] is True
    assert source["adapter_config"]["pdf_link_selector"]
    assert source["adapter_config"]["pdf_max_bytes"] == 15728640
    assert source["adapter_config"]["pdf_max_pages"] == 200
    assert source["adapter_config"]["content_selector"] == ".user-custom-selector"
    assert source["enabled"] == 0 and source["crawl_allowed"] == 0
    assert source["rate_limit_seconds"] == 9
    assert source["license_note"] == "用户自定义许可备注，不得覆盖。"
    assert source["commercial_reuse_status"] == "用户判断未明确"


def test_source_template_sync_is_idempotent_and_dry_run_does_not_write(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    _old_maritime_source(db, workspace["workspace_id"])
    before = db.read_bytes()
    dry = initialize_recommended_sources(workspace["workspace_id"], db, dry_run=True)
    after = db.read_bytes()
    assert before == after
    assert dry["would_update"]

    first = initialize_recommended_sources(workspace["workspace_id"], db)
    source_before = next(
        item
        for item in list_sources(workspace["workspace_id"], db)
        if item["source_name"] == "山东海事局海上风险预警"
    )
    second = initialize_recommended_sources(workspace["workspace_id"], db)
    source_after = next(
        item
        for item in list_sources(workspace["workspace_id"], db)
        if item["source_name"] == "山东海事局海上风险预警"
    )
    assert first["updated"] == 1
    assert second["updated"] == 0
    assert source_before["last_template_sync_at"] == source_after["last_template_sync_at"]
    assert source_before["adapter_config"] == source_after["adapter_config"]


def _pdf_source(db: Path, workspace_id: str) -> str:
    return upsert_source(
        {
            "workspace_id": workspace_id,
            "source_name": "PDF附件测试官方来源",
            "organization": "PDF附件测试官方来源",
            "domain": "example.com",
            "homepage_url": "https://example.com/",
            "list_page_url": "https://example.com/list",
            "source_type": "政府/监管机构",
            "category_hint": "航行警告",
            "adapter_type": "generic_html",
            "adapter_config": {
                "article_link_selector": "a.notice",
                "allowed_path_prefix": "/article/",
                "url_pattern": r"^https://example\.com/article/",
                "content_selector": "#zoom",
                "title_selector": "meta[name='ArticleTitle']",
                "date_selector": "meta[name='pubdate']",
                "publisher_selector": "meta[name='ContentSource']",
                "follow_pdf_attachments": True,
                "pdf_link_selector": "#zoom a[href$='.pdf']",
                "pdf_max_bytes": 1024 * 1024,
                "pdf_max_pages": 20,
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
            "terms_status": "允许",
            "commercial_reuse_status": "未明确",
            "rate_limit_seconds": 2,
        },
        db,
    )


def _article_html(title: str = "官方海上风险预警测试") -> str:
    return f"""<html><head>
    <meta name="ArticleTitle" content="{title}">
    <meta name="pubdate" content="2026-07-20">
    <meta name="ContentSource" content="测试官方机构">
    </head><body><div id="zoom">
    <a href="/files/warning.pdf">{title}.pdf</a>
    </div></body></html>"""


def _valid_pdf() -> bytes:
    return _pdf_bytes(
        "Official maritime risk warning published 2026-07-20. "
        "Vessels in the affected sea area should check weather conditions and "
        "official navigation notices before departure. This public test text "
        "contains enough factual sentences for document quality validation."
    )


def test_html_attachment_is_followed_and_stored_as_qualified_pdf(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)
    source_id = _pdf_source(db, workspace["workspace_id"])
    list_url = "https://example.com/list"
    page_url = "https://example.com/article/1"
    pdf_url = "https://example.com/files/warning.pdf"
    requester = Requester(
        {
            list_url: '<a class="notice" href="/article/1">官方海上风险预警测试</a>',
            page_url: _article_html(),
            pdf_url: Response(_valid_pdf(), content_type="application/pdf"),
        }
    )
    result = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=requester,
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    ).run(workspace["workspace_id"], source_ids=[source_id], max_articles=1)
    assert result.pdf_discovered_count == 1
    assert result.pdf_success_count == 1
    assert result.qualified_document_count == 1
    assert result.new_event_count == 1
    with connect(db) as connection:
        document = dict(connection.execute("SELECT * FROM documents").fetchone())
        source_run = dict(
            connection.execute(
                "SELECT * FROM crawl_source_runs WHERE crawl_run_id=?",
                (result.crawl_run_id,),
            ).fetchone()
        )
        event = connection.execute(
            "SELECT human_verified,reviewer_type FROM events"
        ).fetchone()
    assert document["canonical_url"] == page_url
    assert document["source_file_url"] == pdf_url
    assert document["document_format"] == "pdf"
    assert Path(document["raw_html_path"]).suffix == ".html"
    assert Path(document["raw_html_path"]).is_file()
    assert Path(document["raw_file_path"]).suffix == ".pdf"
    assert Path(document["raw_file_path"]).is_file()
    assert document["file_size_bytes"] > 0 and len(document["file_sha256"]) == 64
    assert document["quality_status"] == "合格"
    assert document["record_state"] == "active"
    assert len(document["cleaned_text"]) > 100
    assert document["cleaned_text"] != "官方海上风险预警测试.pdf"
    assert source_run["status"] == "succeeded"
    assert source_run["pdf_discovered_count"] == 1
    assert source_run["pdf_success_count"] == 1
    assert source_run["qualified_document_count"] == 1
    assert source_run["new_event_count"] == 1
    assert tuple(event) == (0, "unknown")


def test_pdf_rework_creates_new_version_and_preserves_bad_history(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)
    source_id = _pdf_source(db, workspace["workspace_id"])
    page_url = "https://example.com/article/old"
    old = store_document(
        {
            "workspace_id": workspace["workspace_id"],
            "source_id": source_id,
            "canonical_url": page_url,
            "original_url": page_url,
            "title": "旧PDF附件占位",
            "publisher": "测试官方机构",
            "published_at": "2026-07-20",
            "raw_html": _article_html("旧PDF附件占位"),
            "document_format": "html",
            "cleaned_text": "旧PDF附件占位.pdf",
            "extraction_status": "正文质量不足",
            "quality_status": "正文质量不足",
            "processing_allowed": False,
        },
        db,
        root,
    )
    candidates = list_pdf_attachment_rework_candidates(
        workspace["workspace_id"],
        db,
        source_ids=[source_id],
    )
    assert [item["document_id"] for item in candidates] == [old.document_id]
    requester = Requester(
        {
            page_url: _article_html("旧PDF附件占位"),
            "https://example.com/files/warning.pdf": Response(
                _valid_pdf(),
                content_type="application/pdf",
            ),
        }
    )
    result = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=requester,
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    ).run(
        workspace["workspace_id"],
        source_ids=[source_id],
        rework_document_ids=[old.document_id],
        max_articles=1,
    )
    assert requester.calls == [
        page_url,
        "https://example.com/files/warning.pdf",
    ]
    assert result.updated_document_count == 1
    with connect(db) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT document_id,document_version,is_current,record_state,
                quality_status,document_format,source_file_url,previous_document_id
                FROM documents ORDER BY document_version"""
            ).fetchall()
        ]
    assert len(rows) == 2
    assert rows[0]["document_id"] == old.document_id
    assert rows[0]["is_current"] == 0
    assert rows[0]["record_state"] == "quarantined"
    assert rows[1]["document_version"] == 2
    assert rows[1]["previous_document_id"] == old.document_id
    assert rows[1]["record_state"] == "active"
    assert rows[1]["document_format"] == "pdf"
    assert rows[1]["source_file_url"].endswith("/warning.pdf")


def test_pdf_parse_failure_stays_quarantined_and_creates_no_event(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)
    source_id = _pdf_source(db, workspace["workspace_id"])
    requester = Requester(
        {
            "https://example.com/list": '<a class="notice" href="/article/bad">坏PDF</a>',
            "https://example.com/article/bad": _article_html("坏PDF"),
            "https://example.com/files/warning.pdf": Response(
                b"%PDF-1.4 invalid body",
                content_type="application/pdf",
            ),
        }
    )
    result = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=requester,
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    ).run(workspace["workspace_id"], source_ids=[source_id], max_articles=1)
    assert result.pdf_discovered_count == 1
    assert result.pdf_success_count == 0
    assert result.pdf_rejected_count == 1
    assert result.qualified_document_count == 0
    assert result.new_event_count == 0
    with connect(db) as connection:
        document = connection.execute(
            "SELECT record_state,quality_status,document_format FROM documents"
        ).fetchone()
        source_run = connection.execute(
            "SELECT status,pdf_rejected_count,qualified_document_count FROM crawl_source_runs"
        ).fetchone()
    assert document["record_state"] == "quarantined"
    assert document["quality_status"] == "正文质量不足"
    assert document["document_format"] == "pdf"
    assert tuple(source_run) == ("failed", 1, 0)


def test_twenty_quality_failures_never_mark_source_stable(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "山东港口零产出测试",
            "organization": "山东港口零产出测试",
            "domain": "example.com",
            "list_page_url": "https://example.com/list",
            "adapter_type": "generic_html",
            "adapter_config": {
                "article_link_selector": "a.notice",
                "url_pattern": r"/bad/",
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
        },
        db,
    )
    links = "".join(
        f'<a class="notice" href="/bad/{index}">附件{index}</a>'
        for index in range(20)
    )
    responses: dict[str, Response | str | bytes] = {
        "https://example.com/list": links
    }
    for index in range(20):
        responses[f"https://example.com/bad/{index}"] = f"""<html><head>
        <title>附件{index}</title><meta property="article:published_time"
        content="2026-07-20"></head><body><main>附件{index}.pdf</main></body></html>"""
    result = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=Requester(responses),
            resolver=PUBLIC_DNS,
            sleeper=lambda _: None,
        ),
        robots_checker=AllowRobots(),
        ai_enabled=False,
    ).run(workspace["workspace_id"], source_ids=[source_id], max_articles=20)
    assert result.discovered_count == 20
    assert result.fetched_count == 20
    assert result.qualified_document_count == 0
    assert result.noise_blocked_count == 20
    assert result.new_event_count == 0
    with connect(db) as connection:
        source = connection.execute(
            "SELECT operational_status,health_status FROM sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        source_run = connection.execute(
            """SELECT status,discovered_count,fetched_count,
            qualified_document_count,quality_failed_count,new_event_count
            FROM crawl_source_runs WHERE source_id=?""",
            (source_id,),
        ).fetchone()
    assert source["operational_status"] == "needs_adapter"
    assert source["health_status"] == "需要适配器"
    assert tuple(source_run) == ("failed", 20, 20, 0, 20, 0)
