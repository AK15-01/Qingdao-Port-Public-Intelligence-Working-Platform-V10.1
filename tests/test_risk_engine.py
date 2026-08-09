from risk_engine import calculate_scores


def test_high_risk_navigation_warning():
    event = {
        "category": "航行警告",
        "title": "部分海域禁止驶入",
        "summary": "临时交通管制",
        "impact": "可能造成绕航延误",
        "source_type": "政府/监管机构",
    }
    result = calculate_scores(event)
    assert result["risk_level"] == "高"
    assert result["priority_score"] >= 70

def test_relief_reduces_risk():
    event = {
        "category": "海上气象",
        "title": "大风蓝色预警解除",
        "summary": "相关预警已经解除，作业逐步恢复正常",
        "impact": "风险下降",
        "source_type": "政府/监管机构",
    }
    result = calculate_scores(event)
    assert result["priority_score"] < 40

def test_tender_is_opportunity():
    event = {
        "category": "招标采购",
        "title": "智慧港口升级改造项目招标",
        "summary": "采购数字化管理服务",
        "impact": "供应商可关注",
        "source_type": "港口/企业官网",
    }
    result = calculate_scores(event)
    assert result["opportunity_level"] == "高"


def test_score_result_contains_explainable_fields():
    event = {
        "category": "港口作业",
        "title": "设备检修可能造成延误",
        "summary": "公开公告显示设备进入计划检修",
        "impact": "作业窗口可能调整",
        "source_type": "港口/企业官网",
        "status": "更新",
    }
    result = calculate_scores(event)
    expected = {
        "raw_risk_score",
        "historical_risk_score",
        "current_priority_score",
        "source_confidence",
        "opportunity_score",
        "matched_terms",
        "score_explanation",
    }
    assert expected <= result.keys()
    assert "类别“港口作业”基础风险" in result["score_explanation"]
