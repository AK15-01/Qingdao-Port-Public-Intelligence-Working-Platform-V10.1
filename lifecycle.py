from __future__ import annotations

import pandas as pd


ACTIVE_STATUSES = {"新增", "持续", "更新", "待核实"}
RESOLVED_STATUSES = {"解除", "已结束"}


def current_risks(dataframe: pd.DataFrame, minimum_score: int = 40) -> pd.DataFrame:
    """Return active lifecycle records only; resolved rows never leak through."""
    if dataframe.empty:
        return dataframe.copy()
    score_field = (
        "current_priority_score"
        if "current_priority_score" in dataframe.columns
        else "priority_score"
    )
    scores = pd.to_numeric(dataframe[score_field], errors="coerce").fillna(0)
    resolved_targets = set(
        dataframe.loc[
            dataframe["status"].isin(RESOLVED_STATUSES), "related_event_id"
        ].astype(str)
    )
    resolved_targets.discard("")
    mask = (
        dataframe["status"].isin(ACTIVE_STATUSES)
        & ~dataframe["event_id"].astype(str).isin(resolved_targets)
        & (scores >= minimum_score)
    )
    return dataframe.loc[mask].sort_values(score_field, ascending=False).copy()


def resolved_risks(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe.copy()
    result = dataframe[dataframe["status"].isin(RESOLVED_STATUSES)].copy()
    if "historical_risk_score" in result.columns:
        result = result.sort_values("historical_risk_score", ascending=False)
    return result


def related_timeline(dataframe: pd.DataFrame, event_id: str) -> pd.DataFrame:
    """Find the connected event chain in either relation direction."""
    if dataframe.empty or not event_id:
        return dataframe.iloc[0:0].copy()
    ids = {event_id}
    changed = True
    while changed:
        changed = False
        for _, row in dataframe.iterrows():
            row_id = str(row.get("event_id", "")).strip()
            related_id = str(row.get("related_event_id", "")).strip()
            if (row_id in ids or related_id in ids) and row_id:
                before = len(ids)
                ids.add(row_id)
                if related_id:
                    ids.add(related_id)
                changed = changed or len(ids) > before
    result = dataframe[dataframe["event_id"].astype(str).isin(ids)].copy()
    sort_fields = [field for field in ("event_date", "collected_at") if field in result]
    if sort_fields:
        result = result.sort_values(sort_fields, ascending=True)
    return result
