from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.content_fetcher import ContentFetcher
from web_extractor import URLSafetyError, validate_url


def _candidate_links(html: str, page_url: str, domain: str, limit: int = 5) -> list[dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in soup.select("a[href]"):
        target = urljoin(page_url, str(node.get("href") or "").strip())
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() != domain:
            continue
        target = parsed._replace(fragment="").geturl()
        title = node.get_text(" ", strip=True)
        if target in seen or target.rstrip("/") == page_url.rstrip("/") or not title:
            continue
        seen.add(target)
        results.append({"url": target, "title": title[:200]})
        if len(results) >= limit:
            break
    return results


def propose_adapter_config(url: str, candidates: list[dict[str, str]]) -> dict[str, object]:
    parsed = urlparse(url)
    paths = [urlparse(item["url"]).path for item in candidates if item.get("url")]
    common = "/"
    if paths:
        segments = paths[0].strip("/").split("/")
        for path in paths[1:]:
            other = path.strip("/").split("/")
            segments = [a for a, b in zip(segments, other) if a == b]
        common = "/" + "/".join(segments)
        if not common.endswith("/"):
            common = common.rsplit("/", 1)[0] + "/" if "/" in common[1:] else "/"
    host = re.escape((parsed.hostname or "").lower())
    prefix = re.escape(common.rstrip("/"))
    pattern = rf"^https?://{host}{prefix}/.+" if prefix else rf"^https?://{host}/.+"
    return {
        "adapter_type": "generic_html",
        "article_link_selector": "a[href]",
        "allowed_path_prefix": common,
        "url_pattern": pattern,
        "next_page_selector": "",
        "content_selector": "article, main, .content, .article-content",
        "exclude_selectors": ["script", "style", "nav", "footer"],
    }


def analyze_single_source(
    url: str,
    *,
    fetcher: Optional[ContentFetcher] = None,
    resolver=None,
    max_candidates: int = 5,
) -> dict[str, object]:
    """Fetch one user-supplied list page and at most one candidate article."""
    safe_url = validate_url(str(url)) if resolver is None else validate_url(str(url), resolver=resolver, resolve_dns=True)
    domain = (urlparse(safe_url).hostname or "").lower()
    if not domain:
        raise URLSafetyError("URL缺少有效域名")
    fetcher = fetcher or (ContentFetcher() if resolver is None else ContentFetcher(resolver=resolver))
    list_result = fetcher.fetch(safe_url, [domain], 2.0)
    if not list_result.raw_html:
        return {
            "ok": False, "url": safe_url, "domain": domain,
            "status": list_result.status, "note": list_result.note,
            "candidates": [], "adapter_suggestion": {},
        }
    candidates = _candidate_links(list_result.raw_html, safe_url, domain, max(1, min(max_candidates, 5)))
    article_test: dict[str, object] = {}
    if candidates:
        tested = fetcher.fetch(candidates[0]["url"], [domain], 2.0)
        article_test = {
            "url": candidates[0]["url"], "ok": tested.ok, "status": tested.status,
            "title": tested.title, "text_preview": tested.text[:300], "note": tested.note,
        }
    return {
        "ok": True,
        "url": safe_url,
        "domain": domain,
        "status": "分析完成",
        "note": "只读取了用户提供的一张列表页，并最多测试一篇候选正文；结果需人工确认。",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "article_test": article_test,
        "adapter_suggestion": propose_adapter_config(safe_url, candidates),
    }
