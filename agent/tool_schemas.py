from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyInput(StrictModel):
    pass


class SourceIdsInput(StrictModel):
    source_ids: list[str] = Field(default_factory=list, max_length=50)
    include_pending_ai: bool = False


class SourceIdInput(StrictModel):
    source_id: str = Field(min_length=1, max_length=80)


class AnalyzeSourceInput(StrictModel):
    url: HttpUrl
    source_name: str = Field(default="", max_length=200)


class ProposeAdapterInput(StrictModel):
    url: HttpUrl
    source_name: str = Field(default="", max_length=200)


class AddSourceInput(StrictModel):
    source_name: str = Field(min_length=1, max_length=200)
    organization: str = Field(default="", max_length=200)
    homepage_url: HttpUrl
    list_page_url: HttpUrl
    source_type: str = Field(default="政府/监管机构", max_length=60)
    category_hint: str = Field(default="", max_length=60)
    adapter_type: Literal["generic_html", "rss", "api", "shandong_maritime", "qingdao_ocean", "qingdao_government"] = "generic_html"
    article_link_selector: str = Field(default="a[href]", max_length=300)
    allowed_path_prefix: str = Field(default="/", max_length=500)
    url_pattern: str = Field(default="", max_length=1000)
    rate_limit_seconds: float = Field(default=2.0, ge=2.0, le=3600)
    max_pages_per_run: int = Field(default=1, ge=1, le=20)
    max_articles_per_run: int = Field(default=5, ge=1, le=20)
    robots_status: Literal["未检查", "允许", "禁止", "检查失败"] = "未检查"
    commercial_reuse_status: Literal["未明确", "允许", "禁止", "需取得许可"] = "未明确"
    license_note: str = Field(default="", max_length=2000)
    enable_after_add: bool = False


class RunCrawlInput(StrictModel):
    source_ids: list[str] = Field(default_factory=list, max_length=50)
    max_articles: int = Field(default=3, ge=1, le=20)
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)


class SearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=8, ge=1, le=30)
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)
    source_id: str = Field(default="", max_length=80)
    category: str = Field(default="", max_length=60)
    status: str = Field(default="", max_length=30)
    human_verified: Optional[bool] = None
    report_eligible: Optional[bool] = None


class AskInput(SearchInput):
    mode: Literal["快速回答", "风险分析", "商机分析", "时间对比", "来源核验"] = "快速回答"


class ReviewInput(StrictModel):
    event_ids: list[str] = Field(min_length=1, max_length=100)
    reviewer_note: str = Field(default="", max_length=1000)


class AnalysisInput(StrictModel):
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)
    categories: list[str] = Field(default_factory=list, max_length=20)


class CompareInput(StrictModel):
    first_start: str = Field(min_length=10, max_length=10)
    first_end: str = Field(min_length=10, max_length=10)
    second_start: str = Field(min_length=10, max_length=10)
    second_end: str = Field(min_length=10, max_length=10)


class GenerateReportInput(StrictModel):
    report_title: str = Field(default="青岛港公开信息情报周报", max_length=200)
    client_name: str = Field(default="通用报告", max_length=200)
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    categories: list[str] = Field(default_factory=list, max_length=20)
    include_risks: bool = True
    include_opportunities: bool = True
    include_sources: bool = True
    human_verified_only: bool = True
    formal_publish: bool = False
    report_mode: Literal["内部研究版", "客户交付版"] = "内部研究版"


class ExportInput(StrictModel):
    table: Literal["sources", "documents", "events", "crawl_runs", "reports", "qa_logs"] = "events"


class ExplainErrorInput(StrictModel):
    error_text: str = Field(min_length=1, max_length=4000)
    source_name: str = Field(default="", max_length=200)


class ConfigureAIInput(StrictModel):
    requested_action: Literal["show_form", "status"] = "show_form"


class ToolPayload(StrictModel):
    status: Literal["success", "pending_confirmation", "error", "cancelled"]
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, str]] = Field(default_factory=list)
    approval_id: str = ""


TOOL_INPUT_MODELS = {
    "get_system_status": EmptyInput,
    "configure_ai": ConfigureAIInput,
    "test_ai_connection": EmptyInput,
    "list_sources": EmptyInput,
    "initialize_recommended_sources": EmptyInput,
    "analyze_source_url": AnalyzeSourceInput,
    "propose_source_adapter": ProposeAdapterInput,
    "add_source": AddSourceInput,
    "enable_source": SourceIdInput,
    "disable_source": SourceIdInput,
    "run_crawl": RunCrawlInput,
    "get_crawl_progress": EmptyInput,
    "get_crawl_result": EmptyInput,
    "retry_failed_sources": SourceIdsInput,
    "search_documents": SearchInput,
    "ask_knowledge_base": AskInput,
    "rebuild_index": EmptyInput,
    "list_pending_reviews": EmptyInput,
    "approve_events": ReviewInput,
    "reject_events": ReviewInput,
    "analyze_risks": AnalysisInput,
    "analyze_opportunities": AnalysisInput,
    "compare_periods": CompareInput,
    "generate_report": GenerateReportInput,
    "list_reports": EmptyInput,
    "export_data": ExportInput,
    "explain_error": ExplainErrorInput,
}
