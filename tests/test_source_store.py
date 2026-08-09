from datetime import datetime
from pathlib import Path

import pytest

from source_store import (
    SOURCE_FIELDS,
    create_source,
    delete_source,
    due_sources,
    ensure_sources_file,
    load_sources,
    mark_source_checked,
    set_source_enabled,
    update_source,
)


def source_values(name: str = "测试公开来源") -> dict[str, str]:
    return {
        "source_name": name,
        "organization": "测试机构",
        "source_type": "政府/监管机构",
        "category_hint": "政策监管",
        "homepage_url": "https://example.com",
        "list_page_url": "https://example.com/notices",
        "region": "测试地区",
        "check_frequency": "每日",
        "priority": "高",
        "enabled": "是",
        "last_status": "未检查",
        "notes": "仅用于测试",
    }


def test_missing_sources_csv_is_created(tmp_path: Path):
    path = tmp_path / "data" / "sources.csv"
    ensure_sources_file(path)
    loaded = load_sources(path)
    assert path.exists()
    assert loaded.empty
    assert loaded.columns.tolist() == SOURCE_FIELDS


def test_source_create_edit_disable_and_reload(tmp_path: Path):
    path = tmp_path / "sources.csv"
    saved = create_source(source_values(), path)
    source_id = saved.iloc[0]["source_id"]
    assert source_id.startswith("SRC-")

    update_source(source_id, {"source_name": "修改后的来源"}, path)
    set_source_enabled(source_id, False, path)
    restarted = load_sources(path)
    assert restarted.iloc[0]["source_name"] == "修改后的来源"
    assert restarted.iloc[0]["enabled"] == "否"


def test_mark_checked_and_delete_confirmation(tmp_path: Path):
    path = tmp_path / "sources.csv"
    saved = create_source(source_values(), path)
    source_id = saved.iloc[0]["source_id"]
    marked = mark_source_checked(
        source_id,
        "正常",
        path,
        checked_at=datetime(2026, 7, 20, 9, 0, 0),
    )
    assert marked.iloc[0]["last_status"] == "正常"
    assert marked.iloc[0]["last_success_at"]
    assert due_sources(marked, today=datetime(2026, 7, 20).date()).empty

    with pytest.raises(PermissionError):
        delete_source(source_id, path=path)
    delete_source(source_id, confirmed=True, path=path)
    assert load_sources(path).empty

