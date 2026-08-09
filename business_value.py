from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


@dataclass(frozen=True)
class BusinessValue:
    level: str
    reason: str


LOW_VALUE_TERMS = (
    "党建", "党支部", "主题党日", "青年联盟", "青年说", "文体活动",
    "荣获", "获奖", "荣誉", "慰问", "参观", "学习贯彻", "座谈交流",
)
HIGH_IMPACT_TERMS = (
    "航行警告", "风险预警", "大风", "大雾", "台风", "停航", "限航",
    "交通管制", "封航", "作业调整", "停靠", "延误", "拥堵", "费率",
    "运价", "航线调整", "采购公告", "招标公告", "开标", "投标",
)
BUSINESS_TERMS = (
    "港口", "码头", "航运", "物流", "货代", "货主", "口岸", "通关",
    "船舶", "集装箱", "海铁联运", "多式联运", "供应链", "泊位", "锚地",
    "装卸", "运输", "采购", "招标", "监管", "申报",
)


def classify_business_value(event: Mapping[str, object]) -> BusinessValue:
    category = str(event.get("category") or "")
    title = str(event.get("title") or "")
    summary = str(event.get("summary") or "")
    impact = str(event.get("impact") or "")
    text = f"{title}\n{summary}\n{impact}"
    def hit(term: str, value: str = text) -> bool:
        if term == "大风":
            return bool(re.search(r"(?<!重)大风(?!险)", value))
        return term in value

    low_hits = [term for term in LOW_VALUE_TERMS if hit(term)]
    high_hits = [term for term in HIGH_IMPACT_TERMS if hit(term)]
    title_low_hits = [term for term in LOW_VALUE_TERMS if hit(term, title)]
    title_operational_hits = [
        term for term in HIGH_IMPACT_TERMS
        if term not in {"大风", "大雾"} and hit(term, title)
    ]
    business_hits = [term for term in BUSINESS_TERMS if term in text]
    if title_low_hits and not title_operational_hits:
        return BusinessValue(
            "低",
            f"标题表明主要为活动、党建或荣誉类动态（命中：{'、'.join(title_low_hits[:3])}），"
            "正文中的行业词不等于具体业务变更。",
        )
    if low_hits and not high_hits:
        return BusinessValue("低", f"主要为活动、党建或荣誉类动态（命中：{'、'.join(low_hits[:3])}），缺少明确业务影响。")
    if category in {"航行警告", "海上气象"} or high_hits:
        return BusinessValue("高", f"涉及航行安全、天气预警、作业变化或采购时点（命中：{'、'.join(high_hits[:4]) or category}）。")
    if category in {"招标采购", "航线运价", "港口作业"} and business_hits:
        return BusinessValue("高", f"直接影响供应链、作业、成本或采购机会（命中：{'、'.join(business_hits[:4])}）。")
    if category == "政策监管" and business_hits:
        return BusinessValue("中", f"涉及港航、物流、口岸或监管流程（命中：{'、'.join(business_hits[:4])}）。")
    if category == "企业动态" and business_hits:
        return BusinessValue("中", f"与港口、航运或供应链业务相关（命中：{'、'.join(business_hits[:4])}），仍需判断具体影响。")
    if business_hits:
        return BusinessValue("中", f"包含可供业务跟踪的信息（命中：{'、'.join(business_hits[:4])}）。")
    return BusinessValue("无关", "未识别到与港航、物流、海事、天气、政策或商业机会直接相关的事实。")
