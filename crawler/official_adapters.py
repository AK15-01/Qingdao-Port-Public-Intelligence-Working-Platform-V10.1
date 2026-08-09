from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base_adapter import DiscoveredItem
from .generic_list_adapter import GenericListAdapter, _date, normalize_url


class OfficialConfiguredAdapter(GenericListAdapter):
    """Named official-site adapter with selectors kept in editable source config."""

    profile_name = "official"

    def discover(self, source: Mapping[str, object], cancel=None):
        config = dict(source.get("adapter_config") or {})
        required = ["article_link_selector", "allowed_path_prefix", "url_pattern"]
        missing = [field for field in required if not str(config.get(field) or "").strip()]
        if missing:
            raise ValueError(f"{self.profile_name}适配器缺少可审计配置：{'、'.join(missing)}")
        items = super().discover(source, cancel)
        if not items:
            raise ValueError(
                f"{self.profile_name}列表结构未发现链接，需检查选择器或站点结构"
            )
        return items

    def article_extraction_config(self, source: Mapping[str, object]) -> dict[str, object]:
        return dict(source.get("adapter_config") or {})


class ShandongMaritimeAdapter(OfficialConfiguredAdapter):
    profile_name = "山东海事局"

    def parse_page(self, html: str, page_url: str, source: Mapping[str, object]):
        """Parse both ordinary HTML and the site's XML/CDATA list datastore."""
        ordinary, next_url = super().parse_page(html, page_url, source)
        if not ordinary:
            fallback_source = dict(source)
            fallback_config = dict(source.get("adapter_config") or {})
            fallback_config["article_link_selector"] = "a[href*='/art/']"
            fallback_source["adapter_config"] = fallback_config
            ordinary, next_url = GenericListAdapter.parse_page(self, html, page_url, fallback_source)
        soup = BeautifulSoup(html or "", "html.parser")
        embedded: list[DiscoveredItem] = []
        seen = {item.url for item in ordinary}
        for script in soup.find_all("script"):
            script_type = str(script.get("type") or "").lower()
            payload = script.string or script.get_text() or ""
            if "xml" not in script_type and "<![cdata[" not in payload.lower():
                continue
            fragments = re.findall(r"<!\[CDATA\[(.*?)\]\]>", payload, flags=re.I | re.S)
            for fragment in fragments:
                fragment_soup = BeautifulSoup(fragment, "html.parser")
                for node in fragment_soup.select("a.font14[href], a[href*='/art/']"):
                    target = normalize_url(urljoin(page_url, str(node.get("href") or "")))
                    if not target or target in seen or not re.search(r"/art/20\d{2}/", target):
                        continue
                    surrounding = node.parent.get_text(" ", strip=True) if node.parent else ""
                    if node.parent and not _date(surrounding):
                        surrounding = node.parent.parent.get_text(" ", strip=True) if node.parent.parent else surrounding
                    embedded.append(DiscoveredItem(target, node.get_text(" ", strip=True), _date(surrounding)))
                    seen.add(target)
        return ordinary + embedded, next_url

    def article_extraction_config(self, source: Mapping[str, object]) -> dict[str, object]:
        config = super().article_extraction_config(source)
        config.setdefault("content_selector", "#zoom")
        config.setdefault("title_selector", "meta[name='ArticleTitle']")
        config.setdefault("date_selector", "meta[name='pubdate']")
        config.setdefault("publisher_selector", "meta[name='ContentSource']")
        config.setdefault("site_names", ["山东海事局", "中华人民共和国山东海事局"])
        return config


class ShandongPortAdapter(OfficialConfiguredAdapter):
    profile_name = "山东省港口集团"

    def parse_page(self, html: str, page_url: str, source: Mapping[str, object]):
        items, next_url = super().parse_page(html, page_url, source)
        # Dates are sibling text in each list item, not nested inside the link.
        soup = BeautifulSoup(html or "", "html.parser")
        by_url: dict[str, DiscoveredItem] = {item.url: item for item in items}
        for node in soup.select(".right_content li a[href], a[href*='/groupMainNews/']"):
            target = normalize_url(urljoin(page_url, str(node.get("href") or "")))
            if not re.search(r"/groupMainNews/20\d{2}-\d{2}-\d{2}/\d+\.html$", target):
                continue
            parent_text = node.parent.get_text(" ", strip=True) if node.parent else ""
            by_url[target] = DiscoveredItem(target, node.get_text(" ", strip=True), _date(parent_text))
        return list(by_url.values()), next_url

    def article_extraction_config(self, source: Mapping[str, object]) -> dict[str, object]:
        config = super().article_extraction_config(source)
        config.setdefault("content_selector", ".right_content")
        config.setdefault("date_selector", ".right_new p[style*='text-align']")
        config.setdefault("prefer_discovered_title", True)
        config.setdefault("site_names", ["山东省港口集团官网", "山东省港口集团", "山东港口"])
        return config


class QingdaoOceanForecastAdapter(OfficialConfiguredAdapter):
    profile_name = "青岛海洋预报预警"


class QingdaoGovernmentAdapter(OfficialConfiguredAdapter):
    profile_name = "青岛市政府公开栏目"
