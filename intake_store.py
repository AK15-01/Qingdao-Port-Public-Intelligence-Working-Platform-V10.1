from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Optional, Union
from uuid import uuid4

import pandas as pd

from data_store import DEFAULT_EVENTS_PATH, load_events, normalize_events, save_events
from data_validator import ValidationReport, validate_events
from intake_analyzer import find_duplicates


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INTAKE_PATH = BASE_DIR / "data" / "intake.csv"

INTAKE_FIELDS = [
    "intake_id",
    "created_at",
    "source_id",
    "source_url",
    "fetched_title",
    "fetched_date",
    "fetched_source_name",
    "fetched_text",
    "fetched_description",
    "fetched_canonical_url",
    "fetched_at",
    "http_status",
    "fetch_status",
    "fetch_note",
    "category_suggestion",
    "status_suggestion",
    "duplicate_suggestion",
    "related_event_suggestion",
    "reviewer_status",
    "reviewer_note",
    "confirmed_event_id",
    "confirmed_at",
]

REVIEWER_STATUSES = ["待审核", "已确认入库", "已忽略", "提取失败"]


class AlreadyConfirmedError(RuntimeError):
    pass


class IntakeValidationError(ValueError):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__(f"正式入库校验失败：{report.error_count} 项错误。")


class SourceVerificationRequired(PermissionError):
    pass


class HighDuplicateError(ValueError):
    def __init__(self, summary: str):
        self.summary = summary
        super().__init__(f"高度疑似重复，必须返回修改或先处理重复候选：{summary}")


def empty_intakes_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=INTAKE_FIELDS)


def ensure_intake_file(path: Union[str, Path] = DEFAULT_INTAKE_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        empty_intakes_dataframe().to_csv(target, index=False, encoding="utf-8-sig")
    return target


def _clean(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _new_intake_id(used_ids: set[str]) -> str:
    while True:
        candidate = f"INT-{uuid4().hex[:10].upper()}"
        if candidate not in used_ids:
            return candidate


def normalize_intakes(
    dataframe: Optional[pd.DataFrame], assign_ids: bool = True
) -> pd.DataFrame:
    normalized = empty_intakes_dataframe() if dataframe is None else dataframe.copy()
    for field in INTAKE_FIELDS:
        if field not in normalized.columns:
            normalized[field] = ""
    normalized = normalized[INTAKE_FIELDS]
    for field in INTAKE_FIELDS:
        normalized[field] = normalized[field].map(_clean)
    if assign_ids and not normalized.empty:
        used_ids = {value for value in normalized["intake_id"] if value}
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for index in normalized.index:
            if not normalized.at[index, "intake_id"]:
                intake_id = _new_intake_id(used_ids)
                normalized.at[index, "intake_id"] = intake_id
                used_ids.add(intake_id)
            if not normalized.at[index, "created_at"]:
                normalized.at[index, "created_at"] = timestamp
            if not normalized.at[index, "reviewer_status"]:
                normalized.at[index, "reviewer_status"] = "待审核"
    return normalized.reset_index(drop=True)


def load_intakes(path: Union[str, Path] = DEFAULT_INTAKE_PATH) -> pd.DataFrame:
    target = ensure_intake_file(path)
    try:
        data = pd.read_csv(target, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        data = empty_intakes_dataframe()
    return normalize_intakes(data, assign_ids=False)


def save_intakes(
    dataframe: pd.DataFrame,
    path: Union[str, Path] = DEFAULT_INTAKE_PATH,
) -> pd.DataFrame:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_intakes(dataframe, assign_ids=True)
    ids = [value for value in normalized["intake_id"] if value]
    if len(ids) != len(set(ids)):
        raise ValueError("intake_id 存在重复。")
    invalid = sorted(set(normalized["reviewer_status"]) - set(REVIEWER_STATUSES))
    if invalid:
        raise ValueError(f"reviewer_status 不在允许范围：{'、'.join(invalid)}")
    temporary = target.with_suffix(target.suffix + ".tmp")
    normalized.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(target)
    return normalized


def create_intake(
    values: Mapping[str, object],
    path: Union[str, Path] = DEFAULT_INTAKE_PATH,
) -> pd.DataFrame:
    current = load_intakes(path)
    row = normalize_intakes(pd.DataFrame([dict(values)]), assign_ids=True)
    return save_intakes(pd.concat([current, row], ignore_index=True), path)


def update_intake(
    intake_id: str,
    values: Mapping[str, object],
    path: Union[str, Path] = DEFAULT_INTAKE_PATH,
) -> pd.DataFrame:
    current = load_intakes(path)
    matches = current.index[current["intake_id"] == intake_id].tolist()
    if len(matches) != 1:
        raise KeyError(f"未找到唯一草稿：{intake_id}")
    index = matches[0]
    for field, value in values.items():
        if field in INTAKE_FIELDS and field not in {"intake_id", "created_at"}:
            current.at[index, field] = _clean(value)
    return save_intakes(current, path)


def mark_intake_ignored(
    intake_id: str,
    note: str = "",
    path: Union[str, Path] = DEFAULT_INTAKE_PATH,
) -> pd.DataFrame:
    current = load_intakes(path)
    matches = current.index[current["intake_id"] == intake_id].tolist()
    if len(matches) != 1:
        raise KeyError(f"未找到唯一草稿：{intake_id}")
    row = current.loc[matches[0]]
    if row["reviewer_status"] == "已确认入库":
        raise AlreadyConfirmedError("已确认入库的草稿不能改为忽略。")
    return update_intake(
        intake_id,
        {"reviewer_status": "已忽略", "reviewer_note": note},
        path,
    )


def confirm_intake(
    intake_id: str,
    event_values: Mapping[str, object],
    intake_path: Union[str, Path] = DEFAULT_INTAKE_PATH,
    events_path: Union[str, Path] = DEFAULT_EVENTS_PATH,
    validator: Callable[[pd.DataFrame], ValidationReport] = validate_events,
    source_verified: Optional[bool] = None,
) -> tuple[str, ValidationReport]:
    """Validate and persist one reviewed draft; never auto-confirms extracted data."""
    intakes = load_intakes(intake_path)
    matches = intakes.index[intakes["intake_id"] == intake_id].tolist()
    if len(matches) != 1:
        raise KeyError(f"未找到唯一草稿：{intake_id}")
    intake_index = matches[0]
    intake_row = intakes.loc[intake_index]
    if intake_row["reviewer_status"] == "已确认入库" or intake_row["confirmed_event_id"]:
        raise AlreadyConfirmedError(
            f"草稿 {intake_id} 已确认入库，不能重复写入。"
        )
    if intake_row["reviewer_status"] == "已忽略":
        raise ValueError("已忽略草稿需要先恢复为待审核后才能入库。")
    if source_verified is False:
        raise SourceVerificationRequired("确认入库前必须勾选已核对原始公开来源。")

    current_events = load_events(events_path)
    duplicate_result = find_duplicates(event_values, current_events)
    if duplicate_result.level == "高度疑似重复":
        raise HighDuplicateError(duplicate_result.summary)
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    traced_values = dict(event_values)
    traced_values.setdefault("intake_id", intake_id)
    traced_values.setdefault("human_confirmed_at", timestamp)
    traced_values.setdefault("last_modified_at", timestamp)
    traced_values.setdefault("ai_assisted", "否")
    traced_values.setdefault("report_included", "是")
    new_event = normalize_events(pd.DataFrame([traced_values]), assign_ids=True)
    candidate = pd.concat([current_events, new_event], ignore_index=True)
    report = validator(candidate)
    if report.error_count:
        raise IntakeValidationError(report)

    event_id = new_event.iloc[0]["event_id"]
    updated_intakes = intakes.copy()
    updated_intakes.at[intake_index, "reviewer_status"] = "已确认入库"
    updated_intakes.at[intake_index, "confirmed_event_id"] = event_id
    updated_intakes.at[intake_index, "confirmed_at"] = timestamp
    if "reviewer_note" in event_values:
        updated_intakes.at[intake_index, "reviewer_note"] = _clean(
            event_values.get("reviewer_note")
        )

    save_events(candidate, events_path)
    try:
        save_intakes(updated_intakes, intake_path)
    except Exception:
        # Best-effort rollback keeps a failed second write from enabling duplicate confirmation.
        save_events(current_events, events_path)
        raise
    return event_id, report


def intake_csv_bytes(dataframe: pd.DataFrame) -> bytes:
    return normalize_intakes(dataframe, assign_ids=False).to_csv(index=False).encode("utf-8-sig")
