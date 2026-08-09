from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

import requests
from pydantic import ValidationError

from workspace_store import WorkspacePaths, workspace_paths

from .approval import request_approval
from .tool_registry import ToolRegistry, build_default_registry
from .tool_schemas import ToolPayload


@dataclass
class AgentContext:
    workspace: Mapping[str, object]
    db_path: object
    data_root: Path
    paths: Optional[WorkspacePaths] = None
    conversation_id: str = ""
    env_path: Optional[Path] = None
    requester: object = requests
    source_fetcher: object = None
    resolver: object = None
    source_analyzer: Optional[Callable[[str], Mapping[str, object]]] = None
    crawl_manager_factory: Optional[Callable[[], object]] = None
    retriever_factory: Optional[Callable[[], object]] = None
    indexer_factory: Optional[Callable[[], object]] = None
    progress_callback: Optional[Callable[[str], None]] = None
    cancel_check: Optional[Callable[[], bool]] = None

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["workspace_id"])

    def emit(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(str(message))


class ToolExecutor:
    def __init__(self, context: AgentContext, registry: Optional[ToolRegistry] = None) -> None:
        self.context = context
        self.registry = registry or build_default_registry()

    def execute(self, tool_name: str, arguments: Mapping[str, object], *, confirmed: bool = False) -> ToolPayload:
        spec = self.registry.get(tool_name)  # Unregistered tools never reach Python execution.
        validated = spec.input_model.model_validate(dict(arguments or {}))
        if spec.requires_confirmation and not confirmed:
            summary = {
                "operation": spec.description,
                "source_ids": getattr(validated, "source_ids", []),
                "source_url": str(getattr(validated, "url", "") or ""),
                "source_name": str(getattr(validated, "source_name", "") or ""),
                "max_articles": getattr(validated, "max_articles", None),
                "uses_network": spec.uses_network,
                "incurs_api_cost": spec.incurs_api_cost,
                "impact": "只在当前工作空间内执行；不会绕过安全、robots或审核门禁。",
            }
            approval_id = request_approval(
                self.context.workspace_id, self.context.conversation_id, tool_name,
                validated.model_dump(mode="json"), summary, self.context.db_path,
            )
            return ToolPayload(
                status="pending_confirmation", message="该操作需要用户确认后才能执行。",
                data={"confirmation": summary}, approval_id=approval_id,
            )
        self.context.emit(f"正在调用：{tool_name}")
        try:
            value = spec.handler(self.context, validated)
            result = value if isinstance(value, ToolPayload) else ToolPayload.model_validate(value)
            return spec.result_model.model_validate(result.model_dump())
        except ValidationError:
            raise
        except Exception as exc:
            return ToolPayload(
                status="error",
                message=f"工具执行失败：{type(exc).__name__}: {str(exc)[:500]}",
                data={"error_type": type(exc).__name__},
            )

    @staticmethod
    def model_safe_payload(payload: ToolPayload) -> dict[str, object]:
        data = payload.model_dump()
        safe_artifacts = []
        for artifact in data.get("artifacts", []):
            safe_artifacts.append({key: value for key, value in artifact.items() if key != "path"})
        data["artifacts"] = safe_artifacts
        return data
