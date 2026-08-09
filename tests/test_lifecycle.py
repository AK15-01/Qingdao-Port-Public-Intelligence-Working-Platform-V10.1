import pandas as pd

from lifecycle import current_risks, related_timeline, resolved_risks
from risk_engine import calculate_scores, enrich_dataframe


def event(event_id: str, status: str, related: str = "") -> dict[str, str]:
    return {
        "event_id": event_id,
        "event_date": "2026-07-18" if status == "解除" else "2026-07-17",
        "collected_at": "2026-07-18T09:00:00+10:00",
        "category": "航行警告",
        "title": "部分海域禁止驶入" if status != "解除" else "部分海域管制解除",
        "summary": "临时交通管制可能造成船期延误",
        "impact": "相关船舶需要核对航行安排",
        "source_type": "政府/监管机构",
        "source_name": "测试来源",
        "source_url": "https://example.com/event",
        "status": status,
        "related_event_id": related,
    }


def test_resolved_event_has_history_but_zero_current_priority():
    result = calculate_scores(event("EVT-2", "解除", "EVT-1"))
    assert result["historical_risk_score"] > 0
    assert result["current_priority_score"] == 0
    assert "状态“解除”系数 0" in result["score_explanation"]


def test_resolved_event_is_not_a_current_risk():
    scored = enrich_dataframe(
        pd.DataFrame([event("EVT-1", "持续"), event("EVT-2", "解除", "EVT-1")])
    )
    # The active original is also suppressed once a related resolution exists.
    assert current_risks(scored, minimum_score=1).empty
    assert resolved_risks(scored)["event_id"].tolist() == ["EVT-2"]


def test_pending_verification_reduces_confidence_and_is_explained():
    confirmed = calculate_scores(event("EVT-1", "新增"))
    pending = calculate_scores(event("EVT-2", "待核实"))
    assert pending["source_confidence"] < confirmed["source_confidence"]
    assert pending["current_priority_score"] < confirmed["current_priority_score"]
    assert "待核实可信度系数" in pending["score_explanation"]


def test_related_timeline_contains_original_and_relief():
    scored = enrich_dataframe(
        pd.DataFrame([event("EVT-1", "持续"), event("EVT-2", "解除", "EVT-1")])
    )
    assert related_timeline(scored, "EVT-2")["event_id"].tolist() == ["EVT-1", "EVT-2"]
