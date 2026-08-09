from __future__ import annotations

from datetime import datetime
import re
from typing import Callable, Mapping, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .base_adapter import DiscoveredItem


def normalize_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host if not port or (scheme == "http" and port == 80) or (scheme == "https" and port == 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _date(text: str) -> str:
    match = re.search(r"(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})", text or "")
    if not match:
        return ""
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date().isoformat()
    except ValueError:
        return ""


class GenericListAdapter:
    def __init__(self, fetch_html: Callable[[str], str]):
        self.fetch_html = fetch_html

    def parse_page(self, html: str, page_url: str, source: Mapping[str, object]) -> tuple[list[DiscoveredItem], str]:
        config = dict(source.get("adapter_config") or {})
        soup = BeautifulSoup(html or "", "html.parser")
        link_selector = str(config.get("article_link_selector") or "a[href]")
        title_selector = str(config.get("title_selector") or "")
        date_selector = str(config.get("date_selector") or "")
        next_selector = str(config.get("next_page_selector") or "")
        allowed_path = str(config.get("allowed_path_prefix") or "")
        url_pattern = str(config.get("url_pattern") or "")
        seen: set[str] = set()
        items: list[DiscoveredItem] = []
        for node in soup.select(link_selector):
            href = node.get("href", "")
            if not href:
                continue
            target = normalize_url(urljoin(page_url, href))
            if target in seen:
                continue
            parsed = urlparse(target)
            if allowed_path and not parsed.path.startswith(allowed_path):
                continue
            if url_pattern and not re.search(url_pattern, target):
                continue
            title_node = node.select_one(title_selector) if title_selector else None
            title = (title_node or node).get_text(" ", strip=True)
            date_node = node.select_one(date_selector) if date_selector else None
            if date_node is None and date_selector:
                date_node = node.parent.select_one(date_selector) if node.parent else None
            published_at = _date(date_node.get_text(" ", strip=True) if date_node else "")
            seen.add(target)
            items.append(DiscoveredItem(target, title, published_at))
        next_url = ""
        if next_selector:
            next_node = soup.select_one(next_selector)
            if next_node and next_node.get("href"):
                next_url = normalize_url(urljoin(page_url, next_node["href"]))
        return items, next_url

    def discover(self, source: Mapping[str, object], cancel=None) -> list[DiscoveredItem]:
        max_pages = max(1, int(source.get("max_pages_per_run") or 1))
        max_articles = max(1, int(source.get("max_articles_per_run") or 5))
        current = normalize_url(str(source.get("list_page_url") or ""))
        visited: set[str] = set()
        results: list[DiscoveredItem] = []
        for _ in range(max_pages):
            if not current or current in visited or (cancel and cancel.cancelled):
                break
            visited.add(current)
            html = self.fetch_html(current)
            page_items, next_url = self.parse_page(html, current, source)
            existing = {item.url for item in results}
            results.extend(item for item in page_items if item.url not in existing)
            if len(results) >= max_articles:
                return results[:max_articles]
            current = next_url
        return results
