from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


REVIEWER_TYPES = {
    "human_user",
    "industry_reviewer",
    "ai_assistant",
    "codex_agent",
    "automatic_rule",
    "llm",
    "auto_review",
    "migration",
    "unknown",
}
HUMAN_REVIEWER_TYPES = {"human_user", "industry_reviewer"}


@dataclass(frozen=True)
class ReviewIdentity:
    reviewer_type: str
    reviewer_name: str
    review_method: str
    review_version: str
    reviewer_note: str = ""

    @property
    def is_independent_human(self) -> bool:
        return self.reviewer_type in HUMAN_REVIEWER_TYPES


def normalize_review_identity(
    reviewer_type: object,
    reviewer_name: object,
    review_method: object,
    review_version: object,
    reviewer_note: object = "",
) -> ReviewIdentity:
    kind = str(reviewer_type or "unknown").strip()
    if kind not in REVIEWER_TYPES:
        raise ValueError(f"不支持的 reviewer_type：{kind}")
    return ReviewIdentity(
        reviewer_type=kind,
        reviewer_name=str(reviewer_name or "").strip(),
        review_method=str(review_method or "").strip(),
        review_version=str(review_version or "").strip(),
        reviewer_note=str(reviewer_note or "").strip(),
    )


def counts_as_human_review(values: Mapping[str, object]) -> bool:
    return (
        str(values.get("reviewer_type") or "") in HUMAN_REVIEWER_TYPES
        and bool(str(values.get("reviewer_name") or "").strip())
        and bool(str(values.get("reviewed_at") or values.get("verified_at") or "").strip())
        and bool(str(values.get("review_method") or "").strip())
    )
