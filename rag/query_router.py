from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence


INTENTS = (
    "当前风险",
    "历史风险",
    "已解除风险",
    "政策机会",
    "招标采购",
    "港口运营动态",
    "长期行业趋势",
    "来源核验",
    "时间对比",
)

ACTIVITY_NOISE_TERMS = (
    "党建", "党日", "荣誉", "获奖", "表彰", "培训", "青年活动",
    "参观", "慰问", "庆祝", "运动会",
)
QINGDAO_DIRECT_TERMS = (
    "青岛港", "青岛辖区", "青岛海事局", "胶州湾", "青岛湾", "前湾港", "董家口",
)
YELLOW_SEA_TERMS = ("黄海", "黄海北部", "黄海中部", "黄海中南部")
OTHER_SHANDONG_CITIES = ("威海", "日照", "烟台", "潍坊", "东营", "滨州")


@dataclass(frozen=True)
class QueryRoute:
    intent: str
    filters: dict[str, object]
    qingdao_focus: bool


def _contains(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def detect_intent(question: str, mode: str = "") -> str:
    text = f"{question} {mode}"
    if _contains(text, ("已解除", "已经解除", "解除风险", "已经结束", "恢复正常")):
        return "已解除风险"
    if _contains(text, ("历史风险", "历史上", "曾经", "过去风险")):
        return "历史风险"
    if _contains(text, ("对比", "相比", "变化", "上周", "上一期")):
        return "时间对比"
    if _contains(text, ("招标", "采购", "中标", "供应商")):
        return "招标采购"
    if _contains(text, ("机会", "商机", "便利", "新航线", "数字化项目")):
        return "政策机会"
    if _contains(text, ("出处", "来源", "原文", "核验")):
        return "来源核验"
    if _contains(text, ("长期", "趋势", "行业变化")):
        return "长期行业趋势"
    if _contains(text, ("运营", "作业", "靠泊", "港口动态", "航线")):
        return "港口运营动态"
    if _contains(text, ("风险", "预警", "限航", "停航", "天气影响")):
        return "当前风险"
    return "港口运营动态"


def route_query(
    question: str,
    mode: str = "",
    filters: Mapping[str, object] | None = None,
) -> QueryRoute:
    intent = detect_intent(question, mode)
    routed = dict(filters or {})
    if intent == "当前风险":
        routed.update(
            {
                "statuses": ["新增", "持续", "更新", "待核实"],
                "risk_levels": ["高", "中"],
                "business_value": ["高", "中"],
                "exclude_resolved_by_relation": True,
            }
        )
    elif intent == "已解除风险":
        routed.update({"statuses": ["解除", "已结束"]})
    elif intent == "历史风险":
        routed.update({
            "risk_levels": ["高", "中"],
            "business_value": ["高", "中"],
        })
    elif intent == "招标采购":
        routed.update({"categories": ["招标采购"]})
    elif intent == "政策机会":
        routed.update({
            "opportunity_levels": ["高", "中"],
            "business_value": ["高", "中"],
        })
    elif intent == "港口运营动态":
        routed.update({
            "categories": ["港口作业", "航线运价", "企业动态"],
            "business_value": ["高", "中"],
        })
    elif intent in {"长期行业趋势", "来源核验", "时间对比"}:
        routed.update({"business_value": ["高", "中"]})
    qingdao_focus = bool(re.search(r"青岛港|青岛辖区|青岛航线|青岛", question))
    return QueryRoute(intent, routed, qingdao_focus)


def classify_geography(text: object) -> dict[str, object]:
    value = str(text or "")
    if _contains(value, QINGDAO_DIRECT_TERMS):
        return {
            "geography_level": 1,
            "geography_scope": "青岛港和青岛辖区",
            "qingdao_relation": "直接涉及青岛港或青岛辖区",
            "impact_relation": "直接影响",
        }
    if _contains(value, YELLOW_SEA_TERMS):
        return {
            "geography_level": 2,
            "geography_scope": "黄海相关海域",
            "qingdao_relation": "可能影响青岛进出港航线，需结合具体航线核对",
            "impact_relation": "潜在航线影响",
        }
    if _contains(value, OTHER_SHANDONG_CITIES):
        return {
            "geography_level": 4,
            "geography_scope": "其他山东城市",
            "qingdao_relation": "未发现与青岛港业务链的明确直接关系",
            "impact_relation": "背景信息",
        }
    if "山东" in value:
        return {
            "geography_level": 3,
            "geography_scope": "山东全省",
            "qingdao_relation": "省级背景，是否影响青岛港需另行核对",
            "impact_relation": "补充背景",
        }
    return {
        "geography_level": 5,
        "geography_scope": "全国或其他地区",
        "qingdao_relation": "未发现对青岛港的直接指向",
        "impact_relation": "背景信息",
    }


def route_results(
    results: Sequence[Mapping[str, object]],
    route: QueryRoute,
) -> list[dict[str, object]]:
    routed: list[dict[str, object]] = []
    for raw in results:
        item = dict(raw)
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        for key in (
            "document_id", "event_id", "source_id", "source_name", "category",
            "published_at", "status", "source_url", "human_verified", "report_eligible",
            "business_value", "risk_level", "opportunity_level", "affected_area",
            "evidence_verified",
        ):
            if not item.get(key) and metadata.get(key) not in (None, ""):
                item[key] = metadata[key]
        status = str(item.get("status") or metadata.get("status") or "")
        text = " ".join(
            str(item.get(key) or metadata.get(key) or "")
            for key in ("title", "chunk_text", "affected_area", "category")
        )
        if route.intent == "当前风险":
            if status in {"解除", "已结束"}:
                continue
            if _contains(text, ACTIVITY_NOISE_TERMS):
                continue
        geography = classify_geography(text)
        item.update(geography)
        level = int(geography["geography_level"])
        if route.qingdao_focus and level == 4:
            continue
        item["answer_section"] = "主答案" if not route.qingdao_focus or level in {1, 2} else "补充背景"
        routed.append(item)
    routed.sort(
        key=lambda item: (
            item.get("answer_section") == "主答案",
            -int(item.get("geography_level") or 5),
            float(item.get("hybrid_score") or item.get("keyword_score") or 0),
            str(item.get("published_at") or ""),
        ),
        reverse=True,
    )
    return routed


def opportunity_bucket(item: Mapping[str, object]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("title", "chunk_text", "category"))
    if _contains(text, ("招标", "采购", "中标", "供应商")):
        return "直接商业机会"
    if _contains(text, ("便利", "通关", "申报", "一件事", "流程", "政策")):
        return "政策或流程便利"
    if _contains(text, ("新航线", "数字化", "绿色", "规划", "建设")):
        return "长期行业趋势"
    if _contains(text, ("成本", "费用", "运价", "时效")):
        return "直接商业机会"
    return "尚无明确商业动作的信息"
