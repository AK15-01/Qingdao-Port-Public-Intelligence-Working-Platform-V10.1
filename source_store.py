from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping, Optional, Union
from urllib.parse import urlparse
from uuid import uuid4

import pandas as pd

from data_store import ALLOWED_CATEGORIES, ALLOWED_SOURCE_TYPES


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCES_PATH = BASE_DIR / "data" / "sources.csv"

SOURCE_FIELDS = [
    "source_id",
    "source_name",
    "organization",
    "source_type",
    "category_hint",
    "homepage_url",
    "list_page_url",
    "region",
    "check_frequency",
    "priority",
    "enabled",
    "last_checked_at",
    "last_success_at",
    "last_status",
    "notes",
]

SOURCE_PRIORITIES = ["高", "中", "低"]
CHECK_FREQUENCIES = ["每日", "每周", "按需"]
SOURCE_STATUSES = ["未检查", "正常", "失败", "需人工访问"]
ENABLED_VALUES = ["是", "否"]


def empty_sources_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=SOURCE_FIELDS)


def ensure_sources_file(path: Union[str, Path] = DEFAULT_SOURCES_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        empty_sources_dataframe().to_csv(target, index=False, encoding="utf-8-sig")
    return target


def _clean(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_enabled(value: object) -> str:
    text = _clean(value).casefold()
    if text in {"是", "true", "1", "yes", "y", "启用"}:
        return "是"
    if text in {"否", "false", "0", "no", "n", "停用"}:
        return "否"
    return "是" if not text else _clean(value)


def _new_source_id(used_ids: set[str]) -> str:
    while True:
        candidate = f"SRC-{uuid4().hex[:10].upper()}"
        if candidate not in used_ids:
            return candidate


def normalize_sources(
    dataframe: Optional[pd.DataFrame], assign_ids: bool = True
) -> pd.DataFrame:
    normalized = empty_sources_dataframe() if dataframe is None else dataframe.copy()
    for field in SOURCE_FIELDS:
        if field not in normalized.columns:
            normalized[field] = ""
    normalized = normalized[SOURCE_FIELDS]
    for field in SOURCE_FIELDS:
        normalized[field] = normalized[field].map(_clean)
    if "enabled" in normalized:
        normalized["enabled"] = normalized["enabled"].map(_normalize_enabled)
    if assign_ids and not normalized.empty:
        used_ids = {value for value in normalized["source_id"] if value}
        for index in normalized.index:
            if not normalized.at[index, "source_id"]:
                source_id = _new_source_id(used_ids)
                normalized.at[index, "source_id"] = source_id
                used_ids.add(source_id)
            if not normalized.at[index, "last_status"]:
                normalized.at[index, "last_status"] = "未检查"
            if not normalized.at[index, "enabled"]:
                normalized.at[index, "enabled"] = "是"
    return normalized.reset_index(drop=True)


def load_sources(path: Union[str, Path] = DEFAULT_SOURCES_PATH) -> pd.DataFrame:
    target = ensure_sources_file(path)
    try:
        data = pd.read_csv(target, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        data = empty_sources_dataframe()
    return normalize_sources(data, assign_ids=False)


def validate_sources(dataframe: pd.DataFrame) -> list[str]:
    data = normalize_sources(dataframe, assign_ids=False)
    errors: list[str] = []
    ids = [value for value in data["source_id"] if value]
    if len(ids) != len(set(ids)):
        errors.append("source_id 存在重复。")
    for position, row in data.iterrows():
        number = int(position) + 1
        if not row["source_name"]:
            errors.append(f"第 {number} 条缺少 source_name。")
        if row["priority"] not in SOURCE_PRIORITIES:
            errors.append(f"第 {number} 条 priority 必须是高、中、低。")
        if row["source_type"] not in ALLOWED_SOURCE_TYPES:
            errors.append(f"第 {number} 条 source_type 不在允许范围。")
        if row["category_hint"] and row["category_hint"] not in ALLOWED_CATEGORIES:
            errors.append(f"第 {number} 条 category_hint 不在允许范围。")
        if row["check_frequency"] not in CHECK_FREQUENCIES:
            errors.append(f"第 {number} 条 check_frequency 不在允许范围。")
        if row["enabled"] not in ENABLED_VALUES:
            errors.append(f"第 {number} 条 enabled 必须是是或否。")
        if row["last_status"] not in SOURCE_STATUSES:
            errors.append(f"第 {number} 条 last_status 不在允许范围。")
        if not row["homepage_url"] and not row["list_page_url"]:
            errors.append(f"第 {number} 条至少需要官网或列表页 URL。")
        for field in ("homepage_url", "list_page_url"):
            url = row[field]
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"第 {number} 条 {field} 必须是完整的 HTTP(S) URL。")
            elif parsed.username or parsed.password:
                errors.append(f"第 {number} 条 {field} 不得包含用户名或密码。")
    return errors


def save_sources(
    dataframe: pd.DataFrame,
    path: Union[str, Path] = DEFAULT_SOURCES_PATH,
) -> pd.DataFrame:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_sources(dataframe, assign_ids=True)
    errors = validate_sources(normalized)
    if errors:
        raise ValueError("；".join(errors))
    temporary = target.with_suffix(target.suffix + ".tmp")
    normalized.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(target)
    return normalized


def create_source(
    values: Mapping[str, object],
    path: Union[str, Path] = DEFAULT_SOURCES_PATH,
) -> pd.DataFrame:
    current = load_sources(path)
    row = normalize_sources(pd.DataFrame([dict(values)]), assign_ids=True)
    return save_sources(pd.concat([current, row], ignore_index=True), path)


def update_source(
    source_id: str,
    values: Mapping[str, object],
    path: Union[str, Path] = DEFAULT_SOURCES_PATH,
) -> pd.DataFrame:
    current = load_sources(path)
    matches = current.index[current["source_id"] == source_id].tolist()
    if len(matches) != 1:
        raise KeyError(f"未找到唯一数据源：{source_id}")
    index = matches[0]
    for field, value in values.items():
        if field in SOURCE_FIELDS and field != "source_id":
            current.at[index, field] = _clean(value)
    return save_sources(current, path)


def set_source_enabled(
    source_id: str,
    enabled: bool,
    path: Union[str, Path] = DEFAULT_SOURCES_PATH,
) -> pd.DataFrame:
    return update_source(source_id, {"enabled": "是" if enabled else "否"}, path)


def delete_source(
    source_id: str,
    confirmed: bool = False,
    path: Union[str, Path] = DEFAULT_SOURCES_PATH,
) -> pd.DataFrame:
    if not confirmed:
        raise PermissionError("删除数据源前必须明确确认。")
    current = load_sources(path)
    if source_id not in set(current["source_id"]):
        raise KeyError(f"数据源不存在：{source_id}")
    return save_sources(current[current["source_id"] != source_id].reset_index(drop=True), path)


def mark_source_checked(
    source_id: str,
    status: str = "正常",
    path: Union[str, Path] = DEFAULT_SOURCES_PATH,
    checked_at: Optional[datetime] = None,
) -> pd.DataFrame:
    if status not in SOURCE_STATUSES or status == "未检查":
        raise ValueError("检查结果必须是正常、失败或需人工访问。")
    timestamp = (checked_at or datetime.now().astimezone()).isoformat(timespec="seconds")
    values = {"last_checked_at": timestamp, "last_status": status}
    if status == "正常":
        values["last_success_at"] = timestamp
    return update_source(source_id, values, path)


def _date_from_timestamp(value: object) -> Optional[date]:
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def checked_today(row: Mapping[str, object], today: Optional[date] = None) -> bool:
    return _date_from_timestamp(row.get("last_checked_at")) == (today or date.today())


def due_sources(dataframe: pd.DataFrame, today: Optional[date] = None) -> pd.DataFrame:
    today = today or date.today()
    data = normalize_sources(dataframe, assign_ids=False)
    due_indices: list[int] = []
    for index, row in data.iterrows():
        if row["enabled"] != "是":
            continue
        last_date = _date_from_timestamp(row["last_checked_at"])
        if row["check_frequency"] == "每日" and last_date != today:
            due_indices.append(index)
        elif row["check_frequency"] == "每周" and (
            last_date is None or last_date <= today - timedelta(days=7)
        ):
            due_indices.append(index)
    return data.loc[due_indices].reset_index(drop=True)


def high_priority_unchecked(
    dataframe: pd.DataFrame, today: Optional[date] = None
) -> pd.DataFrame:
    today = today or date.today()
    data = normalize_sources(dataframe, assign_ids=False)
    mask = (data["enabled"] == "是") & (data["priority"] == "高")
    result = data.loc[mask].copy()
    if result.empty:
        return result
    keep = [not checked_today(row, today) for _, row in result.iterrows()]
    return result.loc[keep].reset_index(drop=True)


def sources_csv_bytes(dataframe: pd.DataFrame) -> bytes:
    return normalize_sources(dataframe, assign_ids=False).to_csv(index=False).encode("utf-8-sig")
