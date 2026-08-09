from datetime import date

import pandas as pd

from data_store import STANDARD_FIELDS
from data_validator import validate_events


def valid_event(event_id: str, title: str) -> dict[str, str]:
    return {
        "event_id": event_id,
        "event_date": "2026-07-18",
        "collected_at": "2026-07-18T09:00:00+10:00",
        "category": "港口作业",
        "title": title,
        "summary": "公开来源显示该事件正在按公告内容处理。",
        "impact": "可能影响相关作业安排，需要业务人员复核。",
        "affected_area": "测试区域",
        "affected_period": "测试时段",
        "source_name": "测试公开来源",
        "source_type": "港口/企业官网",
        "source_url": f"https://example.com/{event_id}",
        "status": "新增",
        "related_event_id": "",
        "analyst_note": "",
    }


def test_duplicate_ids_and_titles_are_blocking_errors():
    first = valid_event("EVT-1", "同一个公开事件标题")
    second = valid_event("EVT-1", "同一个公开事件标题")
    report = validate_events(pd.DataFrame([first, second]), today=date(2026, 7, 20))
    codes = {issue.code for issue in report.issues}
    assert "duplicate_event_id" in codes
    assert "duplicate_title" in codes
    assert report.duplicate_record_count == 1
    assert not report.can_save


def test_similar_title_is_warning_and_allows_save():
    first = valid_event("EVT-1", "前湾作业区夜间设备计划检修通知")
    second = valid_event("EVT-2", "前湾作业区夜间设备计划检修公告")
    report = validate_events(
        pd.DataFrame([first, second]),
        today=date(2026, 7, 20),
        similarity_threshold=0.75,
    )
    assert any(issue.code == "similar_title" for issue in report.issues)
    assert report.can_save


def test_relief_requires_existing_related_event():
    original = valid_event("EVT-1", "大雾预警持续")
    relief = valid_event("EVT-2", "大雾预警解除")
    relief["status"] = "解除"
    report = validate_events(pd.DataFrame([original, relief]), today=date(2026, 7, 20))
    assert any(issue.code == "relief_missing_relation" for issue in report.issues)

    relief["related_event_id"] = "EVT-1"
    report = validate_events(pd.DataFrame([original, relief]), today=date(2026, 7, 20))
    assert not any(issue.code == "relief_missing_relation" for issue in report.issues)
    assert report.can_save


def test_required_url_status_and_date_checks():
    event = valid_event("EVT-1", "待检查事件")
    event["event_date"] = "2099-99-99"
    event["source_url"] = "not-a-url"
    event["status"] = "未知状态"
    report = validate_events(pd.DataFrame([event]), today=date(2026, 7, 20))
    codes = {issue.code for issue in report.issues}
    assert {"invalid_event_date", "invalid_source_url", "invalid_status"} <= codes


def test_missing_schema_fields_are_reported():
    report = validate_events(pd.DataFrame([{"title": "只有标题"}]))
    assert report.error_count > 0
    assert set(STANDARD_FIELDS) - {"title"}

