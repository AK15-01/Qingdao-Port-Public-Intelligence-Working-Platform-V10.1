from __future__ import annotations

from dataclasses import dataclass, asdict, field, replace
import json
from typing import Optional

from crawler.content_fetcher import ContentFetcher
from crawler.robots_checker import RobotsChecker
from crawler.site_adapter import build_adapter
from document_quality import assess_document
from pdf_processor import find_pdf_links
from platform_db import list_sources, now_iso, transaction


def _looks_mojibake(value: str) -> bool:
    text = str(value or "")
    return any(marker in text for marker in ("Ã", "Â", "å±", "ä¸", "ç«", "é", "æ¸"))


@dataclass
class SourceHealthResult:
    source_id: str
    source_name: str
    health_status: str = "未验收"
    list_accessible: bool = False
    discovered_count: int = 0
    fetched_count: int = 0
    extraction_success_rate: float = 0.0
    articles: list[dict[str, object]] = field(default_factory=list)
    template_changed: bool = False
    selector_update_needed: bool = False
    note: str = ""


def validate_enabled_sources(
    workspace_id: str,
    db_path,
    *,
    source_ids: Optional[list[str]] = None,
    fetcher: Optional[ContentFetcher] = None,
    robots_checker: Optional[RobotsChecker] = None,
) -> list[dict[str, object]]:
    """Very small live acceptance: one list page, five links, two articles, >=3 seconds/request."""
    fetcher = fetcher or ContentFetcher(timeout=15, retries=0)
    robots_checker = robots_checker or RobotsChecker()
    selected = set(source_ids or [])
    sources = [
        item for item in list_sources(workspace_id, db_path, enabled_only=True)
        if item.get("crawl_allowed") and (not selected or item["source_id"] in selected)
    ]
    results: list[dict[str, object]] = []
    for source in sources:
        item = SourceHealthResult(str(source["source_id"]), str(source["source_name"]))
        domain = str(source.get("domain") or "")
        limited = {**source, "max_pages_per_run": 1, "max_articles_per_run": 5}
        robots = robots_checker.check(str(source.get("list_page_url") or ""), str(source.get("robots_status") or "未检查"))
        if not robots.allowed:
            item.health_status = "robots或条款待确认"
            item.note = robots.note or "robots状态未明确允许，未访问。"
        else:
            try:
                def fetch_html(url: str) -> str:
                    response = fetcher.fetch(url, [domain], max(3.0, float(source.get("rate_limit_seconds") or 3.0)))
                    if not response.raw_html:
                        raise RuntimeError(f"{response.status}：{response.note}")
                    item.list_accessible = True
                    return response.raw_html

                adapter = build_adapter(limited, fetch_html)
                discovered = adapter.discover(limited)[:5]
                extraction_config = (
                    adapter.article_extraction_config(limited)
                    if hasattr(adapter, "article_extraction_config")
                    else dict(source.get("adapter_config") or {})
                )
                item.discovered_count = len(discovered)
                if not discovered:
                    item.health_status = "结构变化"
                    item.template_changed = True
                    item.selector_update_needed = True
                    item.note = "列表页可访问，但当前选择器没有发现文章链接。"
                else:
                    metadata_issues = 0
                    pdf_successes = 0
                    for candidate in discovered[:2]:
                        trusted_config = {
                            **extraction_config,
                            "trusted_title": candidate.title,
                            "trusted_published_at": candidate.published_at,
                            "trusted_publisher": source.get("organization") or source.get("source_name"),
                        }
                        response = fetcher.fetch(
                            candidate.url, [domain], max(3.0, float(source.get("rate_limit_seconds") or 3.0)),
                            extraction_config=trusted_config, allow_pdf=True,
                        )
                        if response.document_format == "html" and response.raw_html and extraction_config.get("follow_pdf_attachments"):
                            links = find_pdf_links(
                                response.raw_html, response.final_url or candidate.url,
                                str(extraction_config.get("pdf_link_selector") or ""), limit=1,
                            )
                            if links:
                                pdf_response = fetcher.fetch(
                                    links[0], [domain], max(3.0, float(source.get("rate_limit_seconds") or 3.0)),
                                    extraction_config=trusted_config, allow_pdf=True,
                                )
                                if pdf_response.raw_bytes:
                                    response = replace(
                                        pdf_response,
                                        canonical_url=response.canonical_url or response.final_url or candidate.url,
                                        title=response.title or candidate.title or pdf_response.title,
                                        published_at=response.published_at or candidate.published_at or pdf_response.published_at,
                                        publisher=response.publisher or pdf_response.publisher
                                        or str(source.get("organization") or source.get("source_name") or ""),
                                    )
                        preferred_title = (
                            candidate.title
                            if extraction_config.get("prefer_discovered_title") and candidate.title
                            else (response.title or candidate.title)
                        )
                        quality = assess_document(
                            title=preferred_title,
                            text=response.text,
                            published_at=response.published_at or candidate.published_at,
                            raw_html=response.raw_html,
                            site_names=[str(value) for value in extraction_config.get("site_names", []) or []],
                        )
                        article = {
                            "url": candidate.url,
                            "ok": response.ok,
                            "title": preferred_title,
                            "published_at": response.published_at or candidate.published_at,
                            "publisher": response.publisher or source.get("organization") or source.get("source_name"),
                            "text_length": len(response.text or ""),
                            "status": response.status,
                            "note": response.note,
                            "quality_status": quality.status,
                            "quality_note": quality.note,
                            "document_format": response.document_format,
                            "pdf_pages": response.pdf_page_count,
                            "title_extracted": bool(preferred_title) and not _looks_mojibake(preferred_title)
                            and not str(preferred_title).strip().endswith(("官网", "网站首页")),
                            "date_extracted": bool(response.published_at),
                            "publisher_extracted": bool(response.publisher or source.get("organization") or source.get("source_name")),
                        }
                        item.articles.append(article)
                        if response.ok and response.text and quality.processing_allowed:
                            item.fetched_count += 1
                            if response.document_format == "pdf":
                                pdf_successes += 1
                        if not article["title_extracted"] or not article["date_extracted"] or not article["publisher_extracted"]:
                            metadata_issues += 1
                    attempted = min(2, len(discovered))
                    item.extraction_success_rate = round(item.fetched_count / attempted, 3) if attempted else 0.0
                    if item.fetched_count == attempted and metadata_issues == 0:
                        item.health_status = "PDF可用" if pdf_successes else "正常"
                    elif item.fetched_count == attempted:
                        item.health_status = "部分可用"
                        item.selector_update_needed = True
                        item.note = "正文可提取，但标题、日期或发布机构元数据不完整。"
                    elif item.fetched_count:
                        item.health_status = "部分可用"
                        item.selector_update_needed = True
                    else:
                        if metadata_issues == 0:
                            item.health_status = "部分可用"
                            item.note = "列表、标题、日期和发布机构可提取，但正文为附件、视频或空模板，已被质量门禁拦截。"
                        else:
                            item.health_status = "结构变化"
                            item.template_changed = True
                            item.selector_update_needed = True
                    if str(source.get("terms_status") or "") not in {"允许", "已确认允许"}:
                        item.note = (item.note + " 网站条款仍需人工确认。").strip()
            except Exception as exc:
                item.health_status = "连接失败"
                item.note = f"{type(exc).__name__}: {str(exc)[:300]}"
        payload = asdict(item)
        with transaction(db_path) as connection:
            connection.execute(
                "UPDATE sources SET health_status=?,last_health_check_at=?,health_details_json=?,updated_at=? WHERE source_id=? AND workspace_id=?",
                (item.health_status, now_iso(), json.dumps(payload, ensure_ascii=False), now_iso(), item.source_id, workspace_id),
            )
        results.append(payload)
    return results
