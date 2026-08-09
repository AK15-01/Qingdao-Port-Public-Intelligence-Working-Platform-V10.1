from __future__ import annotations

from typing import Mapping

from .api_adapter import APIAdapter
from .generic_list_adapter import GenericListAdapter
from .rss_adapter import RSSAdapter
from .official_adapters import (
    QingdaoGovernmentAdapter,
    QingdaoOceanForecastAdapter,
    ShandongMaritimeAdapter,
    ShandongPortAdapter,
)


def build_adapter(source: Mapping[str, object], fetch_html, fetch_json=None):
    adapter_type = str(source.get("adapter_type") or "generic_html")
    if adapter_type == "rss":
        return RSSAdapter(fetch_html)
    if adapter_type == "api":
        if fetch_json is None:
            raise ValueError("API适配器缺少JSON获取器")
        return APIAdapter(fetch_json)
    if adapter_type == "shandong_maritime":
        return ShandongMaritimeAdapter(fetch_html)
    if adapter_type == "shandong_port":
        return ShandongPortAdapter(fetch_html)
    if adapter_type == "qingdao_ocean":
        return QingdaoOceanForecastAdapter(fetch_html)
    if adapter_type == "qingdao_government":
        return QingdaoGovernmentAdapter(fetch_html)
    return GenericListAdapter(fetch_html)
