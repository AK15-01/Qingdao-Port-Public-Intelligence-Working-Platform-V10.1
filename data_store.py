from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Union
from uuid import uuid4

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_EVENTS_PATH = BASE_DIR / "data" / "events.csv"

STANDARD_FIELDS = [
    "event_id",
    "event_date",
    "collected_at",
    "category",
    "title",
    "summary",
    "impact",
    "affected_area",
    "affected_period",
    "source_name",
    "source_type",
    "source_url",
    "status",
    "related_event_id",
    "analyst_note",
    "workspace_id",
    "source_id",
    "intake_id",
    "human_confirmed_at",
    "ai_assisted",
    "last_modified_at",
    "report_included",
    "recommended_action",
]

REQUIRED_FIELDS = [
    "event_id",
    "event_date",
    "collected_at",
    "category",
    "title",
    "summary",
    "impact",
    "source_name",
    "source_type",
    "source_url",
    "status",
]

ALLOWED_CATEGORIES = [
    "航行警告",
    "海上气象",
    "港口作业",
    "航线运价",
    "政策监管",
    "招标采购",
    "企业动态",
]

ALLOWED_SOURCE_TYPES = [
    "政府/监管机构",
    "港口/企业官网",
    "交易所/行业机构",
    "主流媒体",
    "行业自媒体",
    "其他",
]

ALLOWED_STATUSES = ["新增", "持续", "更新", "解除", "已结束", "待核实"]


def empty_events_dataframe() -> pd.DataFrame:
    """Return an empty events table with the canonical column order."""
    return pd.DataFrame(columns=STANDARD_FIELDS)


def ensure_data_file(path: Union[str, Path] = DEFAULT_EVENTS_PATH) -> Path:
    """Create an empty real-data CSV when it does not exist."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        empty_events_dataframe().to_csv(target, index=False, encoding="utf-8-sig")
    return target


def _clean_cell(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.date().isoformat()
        return value.isoformat()
    return str(value).strip()


def _new_event_id(event_date: str, used_ids: set[str]) -> str:
    try:
        date_part = pd.to_datetime(event_date, errors="raise").strftime("%Y%m%d")
    except (TypeError, ValueError):
        date_part = datetime.now().strftime("%Y%m%d")
    while True:
        candidate = f"EVT-{date_part}-{uuid4().hex[:8].upper()}"
        if candidate not in used_ids:
            return candidate


def normalize_events(
    dataframe: Optional[pd.DataFrame],
    assign_ids: bool = True,
) -> pd.DataFrame:
    """Normalize imported or edited data to the canonical string schema."""
    if dataframe is None:
        normalized = empty_events_dataframe()
    else:
        normalized = dataframe.copy()

    for field in STANDARD_FIELDS:
        if field not in normalized.columns:
            normalized[field] = ""

    normalized = normalized[STANDARD_FIELDS]
    for field in STANDARD_FIELDS:
        normalized[field] = normalized[field].map(_clean_cell)

    if not assign_ids or normalized.empty:
        return normalized.reset_index(drop=True)

    used_ids = {value for value in normalized["event_id"] if value}
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for index in normalized.index:
        if not normalized.at[index, "event_id"]:
            event_id = _new_event_id(normalized.at[index, "event_date"], used_ids)
            normalized.at[index, "event_id"] = event_id
            used_ids.add(event_id)
        if not normalized.at[index, "collected_at"]:
            normalized.at[index, "collected_at"] = collected_at
        if not normalized.at[index, "human_confirmed_at"]:
            normalized.at[index, "human_confirmed_at"] = collected_at
        if not normalized.at[index, "ai_assisted"]:
            normalized.at[index, "ai_assisted"] = "否"
        if not normalized.at[index, "last_modified_at"]:
            normalized.at[index, "last_modified_at"] = collected_at
        if not normalized.at[index, "report_included"]:
            normalized.at[index, "report_included"] = "是"
    return normalized.reset_index(drop=True)


def load_events(path: Union[str, Path] = DEFAULT_EVENTS_PATH) -> pd.DataFrame:
    """Load only the real-data file; sample_events.csv is never used here."""
    target = ensure_data_file(path)
    try:
        dataframe = pd.read_csv(target, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        dataframe = empty_events_dataframe()
    return normalize_events(dataframe, assign_ids=False)


def save_events(
    dataframe: pd.DataFrame,
    path: Union[str, Path] = DEFAULT_EVENTS_PATH,
) -> pd.DataFrame:
    """Atomically persist canonical events and return the saved dataframe."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_events(dataframe, assign_ids=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    normalized.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(target)
    return normalized


def create_event(
    values: Mapping[str, object],
    path: Union[str, Path] = DEFAULT_EVENTS_PATH,
) -> pd.DataFrame:
    current = load_events(path)
    new_row = normalize_events(pd.DataFrame([dict(values)]), assign_ids=True)
    return save_events(pd.concat([current, new_row], ignore_index=True), path)


def update_event(
    event_id: str,
    values: Mapping[str, object],
    path: Union[str, Path] = DEFAULT_EVENTS_PATH,
) -> pd.DataFrame:
    current = load_events(path)
    matches = current.index[current["event_id"] == event_id].tolist()
    if len(matches) != 1:
        raise KeyError(f"未找到唯一事件：{event_id}")
    index = matches[0]
    for field, value in values.items():
        if field in STANDARD_FIELDS and field != "event_id":
            current.at[index, field] = _clean_cell(value)
    current.at[index, "last_modified_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    return save_events(current, path)


def delete_event(
    event_id: str,
    confirmed: bool = False,
    path: Union[str, Path] = DEFAULT_EVENTS_PATH,
) -> pd.DataFrame:
    if not confirmed:
        raise PermissionError("删除事件前必须明确确认。")
    current = load_events(path)
    if event_id not in set(current["event_id"]):
        raise KeyError(f"事件不存在：{event_id}")
    remaining = current[current["event_id"] != event_id].reset_index(drop=True)
    return save_events(remaining, path)


def csv_bytes(dataframe: pd.DataFrame) -> bytes:
    return normalize_events(dataframe, assign_ids=False).to_csv(
        index=False
    ).encode("utf-8-sig")
