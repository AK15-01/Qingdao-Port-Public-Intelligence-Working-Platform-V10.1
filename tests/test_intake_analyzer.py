import pandas as pd

from intake_analyzer import (
    analyze_draft,
    find_duplicates,
    relation_candidates,
    suggest_category,
    suggest_status,
)


def events():
    return pd.DataFrame(
        [
            {
                "event_id": "EVT-1",
                "event_date": "2026-07-19",
                "category": "海上气象",
                "title": "近海大雾橙色预警",
                "summary": "公开预警显示近海可能出现大雾天气。",
                "source_name": "测试气象来源",
                "source_url": "https://example.com/fog",
                "status": "持续",
            }
        ]
    )


def test_category_and_status_suggestions_are_deterministic():
    category, terms = suggest_category("智慧港口服务采购公告", "现公开招标采购数字化服务")
    status, status_terms = suggest_status("大雾预警解除", "相关作业恢复正常")
    assert category == "招标采购"
    assert "采购" in terms or "招标" in terms
    assert status == "解除"
    assert status_terms


def test_same_url_is_highly_suspected_duplicate():
    result = find_duplicates(
        {
            "source_url": "https://example.com/fog",
            "title": "不同标题",
            "summary": "不同摘要",
            "event_date": "2026-07-20",
            "source_name": "其他来源",
            "category": "企业动态",
        },
        events(),
    )
    assert result.level == "高度疑似重复"
    assert result.candidates[0].event_id == "EVT-1"


def test_similar_title_returns_possible_duplicate_candidate():
    result = find_duplicates(
        {
            "source_url": "https://example.com/fog-update",
            "title": "近海大雾橙色预警更新",
            "summary": "这是新的更新信息，与原摘要内容不同。",
            "event_date": "2026-07-20",
            "source_name": "测试气象来源",
            "category": "海上气象",
        },
        events(),
    )
    assert result.level in {"可能重复", "高度疑似重复"}
    assert result.candidates[0].event_id == "EVT-1"


def test_relief_draft_gets_original_event_relation_candidate():
    candidates = relation_candidates(
        "近海大雾橙色预警解除",
        "相关预警解除并恢复正常",
        events(),
        category="海上气象",
        source_name="测试气象来源",
    )
    assert candidates
    assert candidates[0].event_id == "EVT-1"
    analysis = analyze_draft(
        "近海大雾橙色预警解除",
        "相关预警解除并恢复正常",
        "https://example.com/fog-relief",
        "2026-07-20",
        "测试气象来源",
        events(),
    )
    assert analysis["status_suggestion"] == "解除"
    assert "EVT-1" in analysis["related_event_suggestion"]

