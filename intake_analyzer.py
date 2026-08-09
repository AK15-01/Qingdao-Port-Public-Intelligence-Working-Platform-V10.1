from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Mapping, Optional

import pandas as pd

from data_store import ALLOWED_CATEGORIES
from risk_engine import calculate_scores


RULES_PATH = Path(__file__).resolve().parent / "config" / "intake_rules.json"


@dataclass(frozen=True)
class DuplicateCandidate:
    event_id: str
    title: str
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateResult:
    level: str
    candidates: tuple[DuplicateCandidate, ...]

    @property
    def summary(self) -> str:
        if not self.candidates:
            return "未发现明显重复"
        items = "；".join(
            f"{item.event_id}｜{item.title}（{'、'.join(item.reasons)}）"
            for item in self.candidates[:5]
        )
        return f"{self.level}：{items}"


@dataclass(frozen=True)
class RelationCandidate:
    event_id: str
    event_date: str
    status: str
    title: str
    score: float


@lru_cache(maxsize=1)
def load_intake_rules(path: Optional[str] = None) -> dict[str, object]:
    rules_path = Path(path) if path else RULES_PATH
    with rules_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _normalized(value: object) -> str:
    return re.sub(r"[\W_]+", "", _text(value).casefold(), flags=re.UNICODE)


def _similarity(left: object, right: object) -> float:
    left_text = _normalized(left)
    right_text = _normalized(right)
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def suggest_category(
    title: str,
    text: str,
    category_hint: str = "",
    rules: Optional[Mapping[str, object]] = None,
) -> tuple[str, list[str]]:
    config = dict(rules) if rules is not None else load_intake_rules()
    combined = f"{_text(title)} {_text(text)}".casefold()
    scores: list[tuple[int, int, str, list[str]]] = []
    for order, (category, terms) in enumerate(config["category_keywords"].items()):
        matched = [term for term in terms if str(term).casefold() in combined]
        scores.append((len(matched), -order, category, matched))
    best = max(scores) if scores else (0, 0, "企业动态", [])
    if best[0] == 0 and category_hint in ALLOWED_CATEGORIES:
        return category_hint, []
    if best[0] == 0:
        return "企业动态", []
    return str(best[2]), list(best[3])


def suggest_status(
    title: str,
    text: str,
    rules: Optional[Mapping[str, object]] = None,
) -> tuple[str, list[str]]:
    config = dict(rules) if rules is not None else load_intake_rules()
    combined = f"{_text(title)} {_text(text)}".casefold()
    for status in ("解除", "已结束", "持续", "更新", "待核实"):
        matches = [
            term
            for term in config["status_keywords"].get(status, [])
            if str(term).casefold() in combined
        ]
        if matches:
            return status, matches
    return "新增", []


def find_duplicates(
    draft: Mapping[str, object],
    events: pd.DataFrame,
    limit: int = 5,
) -> DuplicateResult:
    if events is None or events.empty:
        return DuplicateResult("未发现明显重复", ())
    candidates: list[DuplicateCandidate] = []
    draft_url = _text(draft.get("source_url"))
    draft_title = _text(draft.get("title"))
    draft_summary = _text(draft.get("summary"))
    for _, event in events.iterrows():
        score = 0
        reasons: list[str] = []
        event_url = _text(event.get("source_url"))
        event_title = _text(event.get("title"))
        if draft_url and event_url and draft_url.casefold() == event_url.casefold():
            score = 100
            reasons.append("URL完全相同")
        if _normalized(draft_title) and _normalized(draft_title) == _normalized(event_title):
            score = max(score, 95)
            reasons.append("标题完全相同")
        else:
            title_ratio = _similarity(draft_title, event_title)
            if title_ratio >= 0.84:
                score += int(55 + title_ratio * 25)
                reasons.append(f"标题相似 {title_ratio:.0%}")
        same_tuple = all(
            _text(draft.get(field))
            and _text(draft.get(field)) == _text(event.get(field))
            for field in ("event_date", "source_name", "category")
        )
        if same_tuple:
            score += 35
            reasons.append("同日期、同来源、同类别")
        summary_ratio = _similarity(draft_summary[:2000], _text(event.get("summary"))[:2000])
        if summary_ratio >= 0.72:
            score += int(25 + summary_ratio * 20)
            reasons.append(f"摘要相似 {summary_ratio:.0%}")
        if reasons and score >= 40:
            candidates.append(
                DuplicateCandidate(
                    event_id=_text(event.get("event_id")),
                    title=event_title,
                    score=min(score, 100),
                    reasons=tuple(reasons),
                )
            )
    candidates.sort(key=lambda item: (item.score, item.event_id), reverse=True)
    selected = tuple(candidates[:limit])
    if any(item.score >= 90 for item in selected):
        level = "高度疑似重复"
    elif selected:
        level = "可能重复"
    else:
        level = "未发现明显重复"
    return DuplicateResult(level, selected)


def relation_candidates(
    title: str,
    text: str,
    events: pd.DataFrame,
    category: str = "",
    source_name: str = "",
    limit: int = 5,
    rules: Optional[Mapping[str, object]] = None,
) -> tuple[RelationCandidate, ...]:
    config = dict(rules) if rules is not None else load_intake_rules()
    combined = f"{_text(title)} {_text(text)}"
    if not any(term in combined for term in config["relation_triggers"]):
        return ()
    if events is None or events.empty:
        return ()
    candidates: list[RelationCandidate] = []
    for _, event in events.iterrows():
        score = _similarity(title, event.get("title")) * 0.7
        if category and category == _text(event.get("category")):
            score += 0.2
        if source_name and source_name == _text(event.get("source_name")):
            score += 0.1
        if _text(event.get("status")) in {"解除", "已结束"}:
            score -= 0.15
        if score >= 0.15:
            candidates.append(
                RelationCandidate(
                    event_id=_text(event.get("event_id")),
                    event_date=_text(event.get("event_date")),
                    status=_text(event.get("status")),
                    title=_text(event.get("title")),
                    score=round(score, 3),
                )
            )
    candidates.sort(key=lambda item: (item.score, item.event_date), reverse=True)
    return tuple(candidates[:limit])


def analyze_draft(
    title: str,
    text: str,
    source_url: str,
    event_date: str,
    source_name: str,
    events: pd.DataFrame,
    category_hint: str = "",
) -> dict[str, object]:
    category, category_terms = suggest_category(title, text, category_hint)
    status, status_terms = suggest_status(title, text)
    draft = {
        "source_url": source_url,
        "title": title,
        "summary": text,
        "event_date": event_date,
        "source_name": source_name,
        "category": category,
    }
    duplicates = find_duplicates(draft, events)
    relations = relation_candidates(title, text, events, category, source_name)
    scores = calculate_scores(
        {
            "category": category,
            "title": title,
            "summary": text,
            "impact": "",
            "source_type": "其他",
            "status": status,
        }
    )
    return {
        "category_suggestion": category,
        "category_terms": category_terms,
        "status_suggestion": status,
        "status_terms": status_terms,
        "duplicate_result": duplicates,
        "duplicate_suggestion": duplicates.summary,
        "relation_candidates": relations,
        "related_event_suggestion": "、".join(item.event_id for item in relations),
        "matched_terms": scores["matched_terms"],
        "score_explanation": scores["score_explanation"],
        "risk_level": scores["risk_level"],
    }


def potential_duplicate_event_ids(events: pd.DataFrame) -> set[str]:
    duplicate_ids: set[str] = set()
    if events is None or len(events) < 2:
        return duplicate_ids
    for index in range(1, len(events)):
        row = events.iloc[index]
        previous = events.iloc[:index]
        result = find_duplicates(
            {
                "source_url": row.get("source_url", ""),
                "title": row.get("title", ""),
                "summary": row.get("summary", ""),
                "event_date": row.get("event_date", ""),
                "source_name": row.get("source_name", ""),
                "category": row.get("category", ""),
            },
            previous,
        )
        if result.candidates:
            duplicate_ids.add(_text(row.get("event_id")))
            duplicate_ids.update(item.event_id for item in result.candidates)
    return duplicate_ids
