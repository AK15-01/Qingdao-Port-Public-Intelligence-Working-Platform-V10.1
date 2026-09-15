from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Type

from pydantic import BaseModel

from .tool_schemas import TOOL_INPUT_MODELS, ToolPayload


@dataclass(frozen=True)
class ToolSpec:
    tool_name: str
    description: str
    input_model: Type[BaseModel]
    handler: Callable
    read_only: bool = True
    uses_network: bool = False
    incurs_api_cost: bool = False
    requires_confirmation: bool = False
    result_model: Type[BaseModel] = ToolPayload

    def openai_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.tool_name in self._tools:
            raise ValueError(f"工具重复注册：{spec.tool_name}")
        self._tools[spec.tool_name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未注册工具：{name}") from exc

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def openai_tools(self) -> list[dict[str, object]]:
        return [spec.openai_schema() for spec in self.specs()]


DESCRIPTIONS = {
    "get_system_status": "查看当前工作空间的API、数据库、来源、文档、索引和报告状态。",
    "configure_ai": "返回安全的DeepSeek本机配置流程；永远不能读取或返回完整密钥。",
    "test_ai_connection": "发送极短请求测试当前DeepSeek连接。",
    "list_sources": "列出公开来源、启用状态和最近采集状态。",
    "initialize_recommended_sources": "初始化经过核对的推荐公开来源模板。",
    "analyze_source_url": "安全分析用户提供的单一公开栏目URL，不扫描全站。",
    "propose_source_adapter": "为单一栏目生成CSS选择器、允许路径和URL正则建议，不直接启用。",
    "add_source": "添加一个待核验公开来源。",
    "enable_source": "核对必要配置和robots状态后启用来源。",
    "disable_source": "停用公开来源。",
    "run_crawl": "对已启用白名单来源执行有上限的增量采集。",
    "get_crawl_progress": "查看最近采集进度。",
    "get_crawl_result": "查看最近采集新增、更新、重复、失败、风险和商机统计。",
    "retry_failed_sources": "有上限地重试失败来源。",
    "search_documents": "执行本地关键词检索，并在可用时融合向量检索。",
    "ask_knowledge_base": "执行只基于知识库证据、带真实引用的问答。",
    "rebuild_index": "根据当前SQLite文档重建本地知识库索引。",
    "list_pending_reviews": "列出需要人工处理的事件及原因。",
    "approve_events": "批量确认经过原文核对的事件。",
    "reject_events": "批量退回事件并保留历史。",
    "analyze_risks": "按时间和类别分析当前风险。",
    "analyze_opportunities": "按时间和类别分析政策、招标和商业机会。",
    "compare_periods": "对比两个时期的风险和商机变化。",
    "generate_report": "生成可追溯的DOCX、HTML和Excel报告草稿。",
    "list_reports": "列出当前工作空间历史报告。",
    "export_data": "导出当前工作空间筛选表数据。",
    "explain_error": "把爬虫、索引或API错误转换为普通用户说明。",
}

CONFIRMATION_TOOLS = {
    "initialize_recommended_sources", "analyze_source_url", "propose_source_adapter",
    "add_source", "enable_source", "disable_source", "run_crawl", "retry_failed_sources",
    "rebuild_index", "approve_events", "reject_events",
}
NETWORK_TOOLS = {"test_ai_connection", "analyze_source_url", "propose_source_adapter", "run_crawl", "retry_failed_sources"}
API_COST_TOOLS = {"test_ai_connection", "ask_knowledge_base", "retry_failed_sources"}
WRITE_TOOLS = CONFIRMATION_TOOLS | {"generate_report", "export_data"}


def build_default_registry() -> ToolRegistry:
    from .business_tools import TOOL_HANDLERS

    registry = ToolRegistry()
    for name, model in TOOL_INPUT_MODELS.items():
        registry.register(ToolSpec(
            tool_name=name,
            description=DESCRIPTIONS[name],
            input_model=model,
            handler=TOOL_HANDLERS[name],
            read_only=name not in WRITE_TOOLS,
            uses_network=name in NETWORK_TOOLS,
            incurs_api_cost=name in API_COST_TOOLS,
            requires_confirmation=name in CONFIRMATION_TOOLS,
        ))
    return registry
