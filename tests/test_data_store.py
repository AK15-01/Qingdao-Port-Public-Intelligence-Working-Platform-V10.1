from pathlib import Path

import pandas as pd
import pytest

from data_store import (
    STANDARD_FIELDS,
    create_event,
    delete_event,
    ensure_data_file,
    load_events,
)


def valid_event(title: str = "测试事件") -> dict[str, str]:
    return {
        "event_date": "2026-07-18",
        "category": "港口作业",
        "title": title,
        "summary": "这是用于持久化测试的公开事实摘要。",
        "impact": "这是用于持久化测试的潜在影响判断。",
        "affected_area": "测试区域",
        "affected_period": "测试时段",
        "source_name": "测试公开来源",
        "source_type": "港口/企业官网",
        "source_url": "https://example.com/event",
        "status": "新增",
        "related_event_id": "",
        "analyst_note": "",
    }


def test_empty_real_data_file_is_created_with_standard_schema(tmp_path: Path):
    path = tmp_path / "data" / "events.csv"
    ensure_data_file(path)
    loaded = load_events(path)
    assert path.exists()
    assert loaded.empty
    assert loaded.columns.tolist() == STANDARD_FIELDS


def test_created_event_persists_and_keeps_generated_id(tmp_path: Path):
    path = tmp_path / "events.csv"
    saved = create_event(valid_event(), path)
    event_id = saved.iloc[0]["event_id"]

    restarted = load_events(path)
    assert len(restarted) == 1
    assert restarted.iloc[0]["title"] == "测试事件"
    assert restarted.iloc[0]["event_id"] == event_id
    assert event_id.startswith("EVT-20260718-")
    assert restarted.iloc[0]["collected_at"]


def test_delete_requires_explicit_confirmation(tmp_path: Path):
    path = tmp_path / "events.csv"
    saved = create_event(valid_event(), path)
    event_id = saved.iloc[0]["event_id"]

    with pytest.raises(PermissionError):
        delete_event(event_id, path=path)
    assert len(load_events(path)) == 1

    delete_event(event_id, confirmed=True, path=path)
    assert load_events(path).empty


def test_empty_real_file_never_falls_back_to_sample_data(tmp_path: Path):
    path = tmp_path / "events.csv"
    loaded = load_events(path)
    assert loaded.empty
    assert not any("演示" in value for value in loaded.get("title", pd.Series(dtype=str)))

