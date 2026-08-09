from __future__ import annotations

from pathlib import Path
from typing import Mapping


def user_visible_result(tool_name: str, payload: Mapping[str, object]) -> dict[str, object]:
    """Return a UI-safe card model without local absolute paths or secrets."""
    data = dict(payload.get("data") or {})
    for key in list(data):
        if key.endswith("_path"):
            data[key] = Path(str(data[key])).name
    return {
        "card_type": (
            "crawl" if tool_name in {"run_crawl", "get_crawl_result", "retry_failed_sources"}
            else "report" if tool_name == "generate_report"
            else "error" if payload.get("status") == "error"
            else "result"
        ),
        "tool_name": tool_name,
        "status": payload.get("status", "success"),
        "message": payload.get("message", ""),
        "data": data,
        "artifacts": [
            {key: value for key, value in dict(item).items() if key != "path"}
            for item in list(payload.get("artifacts") or [])
        ],
        "approval_id": payload.get("approval_id", ""),
    }
