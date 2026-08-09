from __future__ import annotations

from typing import Callable, Mapping
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

from .base_adapter import DiscoveredItem
from .generic_list_adapter import normalize_url


class RSSAdapter:
    def __init__(self, fetch_text: Callable[[str], str]):
        self.fetch_text = fetch_text

    def discover(self, source: Mapping[str, object], cancel=None) -> list[DiscoveredItem]:
        if cancel and cancel.cancelled:
            return []
        url = str(source.get("list_page_url") or "")
        root = ET.fromstring(self.fetch_text(url))
        results: list[DiscoveredItem] = []
        for item in list(root.findall(".//item")) + list(root.findall(".//{*}entry")):
            link = item.findtext("link") or ""
            link_node = item.find("{*}link")
            if not link and link_node is not None:
                link = link_node.attrib.get("href", "")
            if not link:
                continue
            title = item.findtext("title") or item.findtext("{*}title") or ""
            published = item.findtext("pubDate") or item.findtext("{*}updated") or ""
            results.append(DiscoveredItem(normalize_url(urljoin(url, link)), title.strip(), published.strip()))
        return results[: max(1, int(source.get("max_articles_per_run") or 5))]
