from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Mapping, Optional

import pandas as pd


RULES_PATH = Path(__file__).resolve().parent / "config" / "risk_rules.json"

SCORE_FIELDS = [
    "raw_risk_score",
    "historical_risk_score",
    "current_priority_score",
    "source_confidence",
    "opportunity_score",
    "matched_terms",
    "score_explanation",
    "risk_level",
    "opportunity_level",
    "risk_score",
    "confidence",
    "priority_score",
    "risk_terms",
    "opportunity_terms",
    "action",
]


@lru_cache(maxsize=1)
def load_rules(path: Optional[str] = None) -> dict[str, object]:
    rules_path = Path(path) if path else RULES_PATH
    with rules_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _clamp(value: float, low: int = 0, high: int = 100) -> int:
    return int(max(low, min(high, round(value))))


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _contains(text: str, term: str) -> bool:
    if term == "大风":
        return bool(re.search(r"(?<!重)大风(?!险)", text))
    return term.casefold() in text.casefold()


def _level(score: int, thresholds: Mapping[str, object]) -> str:
    if score >= int(thresholds["high"]):
        return "高"
    if score >= int(thresholds["medium"]):
        return "中"
    return "低"


def calculate_scores(
    event: Mapping[str, object],
    rules: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    """Apply transparent, explainable ranking rules to one public event."""
    config = dict(rules) if rules is not None else load_rules()
    category = _text(event.get("category")) or "企业动态"
    source_type = _text(event.get("source_type")) or "其他"
    status = _text(event.get("status")) or "新增"
    text = " ".join(
        _text(event.get(field)) for field in ("title", "summary", "impact")
    )

    category_risk = int(config["category_base_risk"].get(category, 10))
    risk_without_relief = category_risk
    risk_with_relief = category_risk
    risk_matches: list[tuple[str, int]] = []
    relief_matches: list[tuple[str, int]] = []

    for level_name in ("high", "medium", "low"):
        terms = config["risk_keywords"].get(level_name, {})
        for term, points in terms.items():
            if _contains(text, term):
                points = int(points)
                risk_without_relief += points
                risk_with_relief += points
                risk_matches.append((term, points))

    for term, points in config["relief_keywords"].items():
        if _contains(text, term):
            points = int(points)
            risk_with_relief += points
            relief_matches.append((term, points))

    opportunity = int(config["category_base_opportunity"].get(category, 5))
    opportunity_matches: list[tuple[str, int]] = []
    for term, points in config["opportunity_keywords"].items():
        if _contains(text, term):
            points = int(points)
            opportunity += points
            opportunity_matches.append((term, points))

    base_confidence = int(config["source_confidence"].get(source_type, 40))
    status_coefficient = float(config["status_coefficients"].get(status, 0.5))
    confidence_coefficient = float(
        config.get("status_confidence_coefficients", {}).get(status, 1.0)
    )
    source_confidence = _clamp(base_confidence * confidence_coefficient)

    raw_risk_score = int(risk_with_relief)
    historical_risk_score = _clamp(risk_without_relief)
    adjusted_risk = _clamp(risk_with_relief)
    current_priority_score = _clamp(
        adjusted_risk * source_confidence / 100 * status_coefficient
    )
    opportunity_score = _clamp(opportunity)

    risk_thresholds = config["thresholds"]["risk"]
    opportunity_thresholds = config["thresholds"]["opportunity"]
    risk_level = _level(current_priority_score, risk_thresholds)
    opportunity_level = _level(opportunity_score, opportunity_thresholds)

    risk_terms = "、".join(term for term, _ in risk_matches + relief_matches) or "无"
    opportunity_terms = "、".join(term for term, _ in opportunity_matches) or "无"
    matched_parts = [
        f"风险:{term}({points:+d})" for term, points in risk_matches + relief_matches
    ] + [f"商机:{term}({points:+d})" for term, points in opportunity_matches]
    matched_terms = "；".join(matched_parts) or "无"

    explanation_parts = [f"类别“{category}”基础风险 {category_risk} 分"]
    if risk_matches:
        explanation_parts.append(
            "风险词 "
            + "、".join(f"“{term}”{points:+d}" for term, points in risk_matches)
        )
    if relief_matches:
        explanation_parts.append(
            "解除词 "
            + "、".join(f"“{term}”{points:+d}" for term, points in relief_matches)
        )
    explanation_parts.append(
        f"来源“{source_type}”可信度 {source_confidence} 分"
    )
    explanation_parts.append(f"状态“{status}”系数 {status_coefficient:g}")
    if confidence_coefficient != 1:
        explanation_parts.append(f"待核实可信度系数 {confidence_coefficient:g}")
    explanation_parts.append(f"当前优先分 {current_priority_score} 分")
    score_explanation = "；".join(explanation_parts) + "。"

    return {
        "raw_risk_score": raw_risk_score,
        "historical_risk_score": historical_risk_score,
        "current_priority_score": current_priority_score,
        "source_confidence": source_confidence,
        "opportunity_score": opportunity_score,
        "matched_terms": matched_terms,
        "score_explanation": score_explanation,
        "risk_level": risk_level,
        "opportunity_level": opportunity_level,
        # Compatibility aliases for the original MVP and external CSV users.
        "risk_score": adjusted_risk,
        "confidence": source_confidence,
        "priority_score": current_priority_score,
        "risk_terms": risk_terms,
        "opportunity_terms": opportunity_terms,
        "action": _text(event.get("recommended_action"))
        or suggest_action(category, risk_level, opportunity_level, status),
    }


def suggest_action(
    category: str,
    risk_level: str,
    opportunity_level: str,
    status: str = "新增",
) -> str:
    if status in {"解除", "已结束"}:
        return "保留历史记录，并核对关联事件是否已同步关闭后续跟踪。"
    if status == "待核实":
        return "先核对原文、发布时间和影响范围；未核实前不要作为确定事实传播。"
    if risk_level == "高":
        if category in {"航行警告", "海上气象"}:
            return "立即核对涉及海域、时段和船期，联系承运人确认绕航、限航或延误安排。"
        if category == "港口作业":
            return "核对码头作业窗口和集疏港计划，准备替代提还箱或仓储方案。"
        return "当天确认业务暴露范围，并指定负责人持续跟踪。"
    if opportunity_level == "高":
        return "核对资格、预算、截止时间与公开联系人，判断是否进入商机清单。"
    if risk_level == "中":
        return "加入本周重点观察清单，下一次更新时复核状态。"
    return "归档并保持常规监测。"


def enrich_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        result = dataframe.copy()
        for field in SCORE_FIELDS:
            if field not in result.columns:
                result[field] = pd.Series(dtype="object")
        return result
    rows = []
    for _, row in dataframe.iterrows():
        values = row.to_dict()
        rows.append({**values, **calculate_scores(values)})
    return pd.DataFrame(rows)
