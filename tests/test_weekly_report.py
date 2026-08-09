from datetime import date

import pandas as pd

from data_store import normalize_events
from generate_weekly_report import build_html_report, build_weekly_report


def event(event_id: str, event_date: str, title: str, status: str = "新增"):
    return {
        "event_id": event_id,
        "event_date": event_date,
        "collected_at": f"{event_date}T09:00:00+10:00",
        "category": "航行警告",
        "title": title,
        "summary": "公开来源显示部分海域实施临时交通管制。",
        "impact": "相关船舶可能需要调整计划并复核作业安排。",
        "affected_area": "测试海域",
        "affected_period": "测试时段",
        "source_name": "测试公开来源",
        "source_type": "政府/监管机构",
        "source_url": f"https://example.com/{event_id}",
        "status": status,
        "related_event_id": "EVT-1" if status == "解除" else "",
        "analyst_note": "",
    }


def test_markdown_and_html_use_only_selected_date_range():
    data = normalize_events(
        pd.DataFrame(
            [
                event("EVT-1", "2026-07-10", "范围外事件"),
                event("EVT-2", "2026-07-18", "范围内风险事件"),
                event("EVT-3", "2026-07-19", "范围内解除事件", "解除"),
            ]
        ),
        assign_ids=False,
    )
    markdown = build_weekly_report(
        data, "自定义测试周报", date(2026, 7, 18), date(2026, 7, 19), "测试团队"
    )
    html = build_html_report(
        data, "自定义测试周报", date(2026, 7, 18), date(2026, 7, 19), "测试团队"
    )

    assert "范围内风险事件" in markdown
    assert "范围内解除事件" in markdown
    assert "范围外事件" not in markdown
    assert "## 三、已解除风险" in markdown
    assert "## 六、完整来源清单" in markdown
    assert "错误 0 项" in markdown
    assert "范围外事件" not in html
    assert "href=\"https://example.com/EVT-2\"" in html
    assert "不是青岛港官方风险等级" in html


def test_empty_range_still_generates_both_reports():
    data = normalize_events(pd.DataFrame(), assign_ids=False)
    markdown = build_weekly_report(data, "空报告", "2026-07-01", "2026-07-07")
    html = build_html_report(data, "空报告", "2026-07-01", "2026-07-07")
    assert "没有已录入事件" in markdown
    assert "<html" in html
