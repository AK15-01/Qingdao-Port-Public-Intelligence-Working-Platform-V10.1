from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

import pandas as pd

from data_store import (
    ALLOWED_SOURCE_TYPES,
    ALLOWED_STATUSES,
    REQUIRED_FIELDS,
    STANDARD_FIELDS,
    normalize_events,
)


SEVERITY_LABELS = {"error": "错误", "warning": "警告", "suggestion": "建议"}
RISK_CATEGORIES = {"航行警告", "海上气象", "港口作业"}
RELIEF_TERMS = ("解除", "恢复", "取消预警", "恢复正常")


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    row: int
    event_id: str
    field: str
    message: str

    @property
    def severity_label(self) -> str:
        return SEVERITY_LABELS[self.severity]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["级别"] = self.severity_label
        result["记录序号"] = result.pop("row")
        result["事件ID"] = result.pop("event_id")
        result["字段"] = result.pop("field")
        result["问题"] = result.pop("message")
        result.pop("severity")
        result.pop("code")
        return result


@dataclass
class ValidationReport:
    total_records: int
    completeness_rate: float
    duplicate_record_count: int
    missing_source_count: int
    pending_verification_count: int
    issues: list[ValidationIssue]

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    @property
    def suggestion_count(self) -> int:
        return sum(issue.severity == "suggestion" for issue in self.issues)

    @property
    def can_save(self) -> bool:
        return self.error_count == 0

    def issues_dataframe(self) -> pd.DataFrame:
        columns = ["级别", "记录序号", "事件ID", "字段", "问题"]
        if not self.issues:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame([issue.to_dict() for issue in self.issues])[columns]


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _normalized_title(value: object) -> str:
    return re.sub(r"[\W_]+", "", _text(value).lower(), flags=re.UNICODE)


def is_valid_source_url(value: object) -> bool:
    text = _text(value)
    if not text or any(char.isspace() for char in text):
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_event_date(value: str) -> Optional[date]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _add_issue(
    issues: list[ValidationIssue],
    severity: str,
    code: str,
    row: int,
    event_id: str,
    field: str,
    message: str,
) -> None:
    issues.append(ValidationIssue(severity, code, row, event_id, field, message))


def validate_events(
    dataframe: pd.DataFrame,
    today: Optional[date] = None,
    similarity_threshold: float = 0.84,
    known_event_ids: Optional[Iterable[str]] = None,
) -> ValidationReport:
    """Validate the real-data schema without modifying the supplied dataframe."""
    today = today or date.today()
    source = dataframe.copy() if dataframe is not None else pd.DataFrame()
    missing_schema_fields = [field for field in STANDARD_FIELDS if field not in source.columns]
    data = normalize_events(source, assign_ids=False)
    issues: list[ValidationIssue] = []

    if data.empty:
        return ValidationReport(0, 100.0, 0, 0, 0, issues)

    for field in missing_schema_fields:
        severity = "error" if field in REQUIRED_FIELDS else "suggestion"
        _add_issue(
            issues,
            severity,
            "schema_field_missing",
            0,
            "",
            field,
            f"导入文件缺少标准字段“{field}”。",
        )

    duplicate_records: set[int] = set()
    known_ids = {_text(value) for value in data["event_id"] if _text(value)}
    if known_event_ids is not None:
        known_ids.update(_text(value) for value in known_event_ids if _text(value))
    id_first_row: dict[str, int] = {}
    title_first_row: dict[str, int] = {}
    normalized_titles: list[tuple[int, str, str]] = []

    for position, row in data.iterrows():
        record_number = int(position) + 1
        event_id = _text(row["event_id"])

        for field in REQUIRED_FIELDS:
            if not _text(row[field]):
                _add_issue(
                    issues,
                    "error",
                    "required_missing",
                    record_number,
                    event_id,
                    field,
                    f"必填字段“{field}”为空。",
                )

        if event_id:
            if event_id in id_first_row:
                duplicate_records.add(record_number)
                _add_issue(
                    issues,
                    "error",
                    "duplicate_event_id",
                    record_number,
                    event_id,
                    "event_id",
                    f"event_id 与第 {id_first_row[event_id]} 条记录重复。",
                )
            else:
                id_first_row[event_id] = record_number

        event_date_text = _text(row["event_date"])
        parsed_date = _parse_event_date(event_date_text) if event_date_text else None
        if event_date_text and parsed_date is None:
            _add_issue(
                issues,
                "error",
                "invalid_event_date",
                record_number,
                event_id,
                "event_date",
                "事件日期必须是有效的 YYYY-MM-DD 格式。",
            )
        elif parsed_date:
            if parsed_date > today:
                _add_issue(
                    issues,
                    "warning",
                    "future_event_date",
                    record_number,
                    event_id,
                    "event_date",
                    "事件日期晚于今天，请确认是否误填。",
                )
            if parsed_date < date(2000, 1, 1):
                _add_issue(
                    issues,
                    "warning",
                    "abnormal_event_date",
                    record_number,
                    event_id,
                    "event_date",
                    "事件日期早于 2000 年，请确认是否属于本项目范围。",
                )

        collected_at = _text(row["collected_at"])
        if collected_at:
            try:
                datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            except ValueError:
                _add_issue(
                    issues,
                    "warning",
                    "invalid_collected_at",
                    record_number,
                    event_id,
                    "collected_at",
                    "采集时间建议使用 ISO 8601 格式。",
                )

        title_key = _normalized_title(row["title"])
        if title_key:
            if title_key in title_first_row:
                duplicate_records.add(record_number)
                _add_issue(
                    issues,
                    "error",
                    "duplicate_title",
                    record_number,
                    event_id,
                    "title",
                    f"标题与第 {title_first_row[title_key]} 条记录完全重复。",
                )
            else:
                title_first_row[title_key] = record_number
            normalized_titles.append((record_number, event_id, title_key))

        source_type = _text(row["source_type"])
        if source_type and source_type not in ALLOWED_SOURCE_TYPES:
            _add_issue(
                issues,
                "error",
                "invalid_source_type",
                record_number,
                event_id,
                "source_type",
                f"来源类型不在允许范围：{'、'.join(ALLOWED_SOURCE_TYPES)}。",
            )

        status = _text(row["status"])
        if status and status not in ALLOWED_STATUSES:
            _add_issue(
                issues,
                "error",
                "invalid_status",
                record_number,
                event_id,
                "status",
                f"状态不在允许范围：{'、'.join(ALLOWED_STATUSES)}。",
            )

        source_url = _text(row["source_url"])
        if source_url and not is_valid_source_url(source_url):
            _add_issue(
                issues,
                "error",
                "invalid_source_url",
                record_number,
                event_id,
                "source_url",
                "来源链接必须是完整的 http:// 或 https:// 地址。",
            )

        related_id = _text(row["related_event_id"])
        combined_text = " ".join(_text(row[field]) for field in ("title", "summary"))
        is_relief = status == "解除" or any(term in combined_text for term in RELIEF_TERMS)
        if is_relief and not related_id:
            _add_issue(
                issues,
                "error",
                "relief_missing_relation",
                record_number,
                event_id,
                "related_event_id",
                "解除或恢复类事件必须填写 related_event_id 关联原事件。",
            )
        if related_id:
            if related_id == event_id:
                _add_issue(
                    issues,
                    "error",
                    "self_relation",
                    record_number,
                    event_id,
                    "related_event_id",
                    "事件不能关联自身。",
                )
            elif related_id not in known_ids:
                _add_issue(
                    issues,
                    "error",
                    "unknown_relation",
                    record_number,
                    event_id,
                    "related_event_id",
                    "related_event_id 在当前数据中不存在。",
                )

        for field, label in (("summary", "摘要"), ("impact", "影响")):
            content = re.sub(r"\s+", "", _text(row[field]))
            if content and len(content) < 10:
                _add_issue(
                    issues,
                    "warning",
                    f"short_{field}",
                    record_number,
                    event_id,
                    field,
                    f"{label}少于 10 个字符，可能不足以支持复核。",
                )

        if _text(row["category"]) in RISK_CATEGORIES:
            if not _text(row["affected_area"]):
                _add_issue(
                    issues,
                    "suggestion",
                    "affected_area_missing",
                    record_number,
                    event_id,
                    "affected_area",
                    "风险类事件建议补充受影响区域。",
                )
            if not _text(row["affected_period"]):
                _add_issue(
                    issues,
                    "suggestion",
                    "affected_period_missing",
                    record_number,
                    event_id,
                    "affected_period",
                    "风险类事件建议补充受影响时段。",
                )
        if status == "待核实" and not _text(row["analyst_note"]):
            _add_issue(
                issues,
                "suggestion",
                "verification_note_missing",
                record_number,
                event_id,
                "analyst_note",
                "待核实事件建议记录待核实事项或后续动作。",
            )

    for right_index, (right_row, right_id, right_title) in enumerate(normalized_titles):
        if len(right_title) < 6:
            continue
        for left_row, _, left_title in normalized_titles[:right_index]:
            if right_title == left_title or len(left_title) < 6:
                continue
            ratio = SequenceMatcher(None, left_title, right_title).ratio()
            if ratio >= similarity_threshold:
                duplicate_records.add(right_row)
                _add_issue(
                    issues,
                    "warning",
                    "similar_title",
                    right_row,
                    right_id,
                    "title",
                    f"标题与第 {left_row} 条记录高度相似（{ratio:.0%}），请确认是否重复。",
                )
                break

    required_cells = len(data) * len(REQUIRED_FIELDS)
    completed_cells = sum(
        bool(_text(data.at[index, field]))
        for index in data.index
        for field in REQUIRED_FIELDS
    )
    completeness = completed_cells / required_cells * 100 if required_cells else 100.0
    missing_sources = sum(
        not _text(row["source_name"]) or not _text(row["source_url"])
        for _, row in data.iterrows()
    )
    pending = int((data["status"] == "待核实").sum())

    return ValidationReport(
        total_records=len(data),
        completeness_rate=round(completeness, 1),
        duplicate_record_count=len(duplicate_records),
        missing_source_count=int(missing_sources),
        pending_verification_count=pending,
        issues=issues,
    )
