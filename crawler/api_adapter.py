from __future__ import annotations

from typing import Callable, Mapping
from urllib.parse import urljoin

from .base_adapter import DiscoveredItem
from .generic_list_adapter import normalize_url


class APIAdapter:
    def __init__(self, fetch_json: Callable[[str], object]):
        self.fetch_json = fetch_json

    def discover(self, source: Mapping[str, object], cancel=None) -> list[DiscoveredItem]:
        if cancel and cancel.cancelled:
            return []
        config = dict(source.get("adapter_config") or {})
        payload = self.fetch_json(str(source.get("list_page_url") or ""))
        items = payload
        for key in str(config.get("items_path") or "items").split("."):
            if key:
                items = items.get(key, []) if isinstance(items, dict) else []
        results = []
        if isinstance(items, list):
            for row in items:
                if not isinstance(row, dict):
                    continue
                raw_url = str(row.get(str(config.get("url_field") or "url")) or "")
                if raw_url:
                    results.append(DiscoveredItem(
                        normalize_url(urljoin(str(source.get("list_page_url") or ""), raw_url)),
                        str(row.get(str(config.get("title_field") or "title")) or ""),
                        str(row.get(str(config.get("date_field") or "published_at")) or ""),
                    ))
        return results[: max(1, int(source.get("max_articles_per_run") or 5))]
