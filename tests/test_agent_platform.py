from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from agent import AgentContext, AgentOrchestrator, ToolExecutor, build_default_registry
from agent.approval import list_pending_approvals
from agent.conversation_store import append_message, create_conversation, load_messages
from agent.source_assistant import analyze_single_source
from crawler.content_fetcher import FetchedContent
from crawler.crawl_manager import CrawlResult
from deepseek_service import DeepSeekConnectionResult, clear_local_api_key, load_settings, save_settings_to_env
from document_chunker import build_chunk_rows
from document_processor import store_document
from event_pipeline import batch_confirm_events, create_event_for_document
from package_release import build_release
from platform_db import connect, initialize_database, list_sources, now_iso, transaction, upsert_source
from web_extractor import URLSafetyError
from workspace_store import create_workspace, get_workspace, workspace_paths


def _setup(tmp_path: Path, name="Agent", ai=False):
    db = tmp_path / f"{name}.db"
    root = tmp_path / f"{name}_data"
    env = tmp_path / f"{name}.env"
    workspace = create_workspace({"workspace_name": name, "ai_enabled": ai}, db, root)
    initialize_database(db)
    paths = workspace_paths(workspace["workspace_id"], root)
    conversation_id = create_conversation(workspace["workspace_id"], db)
    context = AgentContext(workspace, db, root, paths, conversation_id, env_path=env)
    return db, root, env, workspace, paths, context


def _seed_eligible_report_event(context: AgentContext, *, event_date: str = "2026-07-20") -> str:
    source_id = upsert_source({
        "workspace_id": context.workspace_id, "source_name": "测试公开机构", "organization": "测试公开机构",
        "domain": "example.com", "list_page_url": "https://example.com/list",
        "source_type": "政府/监管机构", "category_hint": "航行警告",
        "enabled": True, "crawl_allowed": True, "robots_status": "允许", "terms_status": "允许",
        "commercial_reuse_status": "允许", "report_use_allowed": True,
        "license_note": "测试来源已确认允许，仅用于自动测试。",
        "last_license_checked_at": "2026-07-23",
    }, context.db_path)
    text = "公开测试信息提示相关水域临时限航，请核对原文安排。第二段仅用于报告工具测试，不代表任何真实港口情况。"
    values = {
        "workspace_id": context.workspace_id, "source_id": source_id,
        "canonical_url": "https://example.com/report-event", "original_url": "https://example.com/report-event",
        "title": "测试航行警告", "publisher": "测试公开机构", "published_at": event_date,
        "fetched_at": f"{event_date}T09:00:00+08:00", "raw_html": f"<article><p>{text}</p></article>",
        "cleaned_text": text, "extraction_status": "成功", "extraction_quality": "测试门禁通过",
        "quality_status": "合格", "report_quality_eligible": True, "http_status": 200,
    }
    chunks = build_chunk_rows(text, {"workspace_id": context.workspace_id, "source_id": source_id})
    stored = store_document(values, context.db_path, context.data_root, chunks)
    values["document_id"] = stored.document_id
    event_id, _ = create_event_for_document(
        stored.document_id, values,
        {"source_id": source_id, "source_name": "测试公开机构", "source_type": "政府/监管机构", "category_hint": "航行警告"},
        context.workspace_id, context.db_path, ai_enabled=False,
    )
    assert batch_confirm_events(
        [event_id], context.workspace_id, context.db_path,
        reviewer_type="human_user", reviewer_name="测试人员",
        review_method="测试中逐项核对",
    )["confirmed"] == [event_id]
    return event_id


def test_api_settings_atomically_save_reload_and_clear_without_database_secret(tmp_path: Path):
    db, _, env, workspace, _, _ = _setup(tmp_path)
    secret = "sk-test-only-12345678"
    saved = save_settings_to_env(
        api_key=secret, agent_model="deepseek-v4-pro", extraction_model="deepseek-v4-flash", env_path=env,
    )
    assert saved.api_key == secret
    assert load_settings(env).api_key == secret
    assert env.read_text(encoding="utf-8").count(secret) == 1
    with connect(db) as connection:
        dump = "\n".join(str(tuple(row)) for table in ("agent_messages", "ai_call_logs") for row in connection.execute(f"SELECT * FROM {table}"))
    assert secret not in dump
    clear_local_api_key(env)
    assert load_settings(env).api_key == ""
    assert secret not in env.read_text(encoding="utf-8")


def test_api_key_can_be_saved_from_workbench_and_reloaded_without_restart(tmp_path: Path, monkeypatch):
    db = tmp_path / "ui.db"
    root = tmp_path / "ui_data"
    env = tmp_path / ".env"
    monkeypatch.setenv("PORTSCOPE_DB_PATH", str(db))
    monkeypatch.setenv("PORTSCOPE_DATA_ROOT", str(root))
    monkeypatch.setenv("PORTSCOPE_ENV_PATH", str(env))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    create_workspace({"workspace_name": "UI密钥测试"}, db, root)
    app = AppTest.from_file("app.py", default_timeout=30).run()
    next(
        radio for radio in app.radio
        if "来源与系统设置" in list(radio.options)
    ).set_value("来源与系统设置").run()
    secret = "sk-test-ui-only-00000000"
    next(item for item in app.text_input if item.label == "API Key").set_value(secret)
    next(item for item in app.checkbox if item.label == "在当前工作空间启用AI辅助").set_value(True)
    next(button for button in app.button if button.label == "保存到本机").click().run()
    assert not app.exception
    assert load_settings(env).api_key == secret
    assert get_workspace(get_workspace_id(db), db)["ai_enabled"] is True
    with connect(db) as connection:
        database_text = "\n".join(str(tuple(row)) for table in ("agent_messages", "ai_call_logs", "settings") for row in connection.execute(f"SELECT * FROM {table}"))
    assert secret not in database_text
    app = AppTest.from_file("app.py", default_timeout=30).run()
    next(
        radio for radio in app.radio
        if "来源与系统设置" in list(radio.options)
    ).set_value("来源与系统设置").run()
    next(button for button in app.button if button.label == "清除本机密钥").click().run()
    assert not app.exception
    assert load_settings(env).api_key == ""


def test_workbench_connection_button_uses_mock_and_never_calls_network(tmp_path: Path, monkeypatch):
    import ui_agent

    db = tmp_path / "connection.db"
    root = tmp_path / "connection_data"
    env = tmp_path / "connection.env"
    monkeypatch.setenv("PORTSCOPE_DB_PATH", str(db))
    monkeypatch.setenv("PORTSCOPE_DATA_ROOT", str(root))
    monkeypatch.setenv("PORTSCOPE_ENV_PATH", str(env))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    create_workspace({"workspace_name": "连接测试"}, db, root)
    seen = {}

    def mocked(settings, **kwargs):
        seen["masked_only"] = settings.api_key.endswith("5678")
        assert kwargs["include_balance"] is False
        return DeepSeekConnectionResult(True, "连接成功", settings.agent_model, 8)

    monkeypatch.setattr(ui_agent, "test_deepseek_connection", mocked)
    app = AppTest.from_file("app.py", default_timeout=30).run()
    next(
        radio for radio in app.radio
        if "来源与系统设置" in list(radio.options)
    ).set_value("来源与系统设置").run()
    next(item for item in app.text_input if item.label == "API Key").set_value("sk-mock-12345678")
    next(button for button in app.button if button.label == "测试连接").click().run()
    assert not app.exception and seen["masked_only"]
    assert any("连接成功" in item.value and "8 ms" in item.value for item in app.success)


def get_workspace_id(db: Path) -> str:
    with connect(db) as connection:
        return str(connection.execute("SELECT workspace_id FROM workspaces LIMIT 1").fetchone()[0])


def test_registry_status_sources_and_unregistered_tool_boundary(tmp_path: Path):
    db, _, _, workspace, _, context = _setup(tmp_path)
    upsert_source({"workspace_id": workspace["workspace_id"], "source_name": "测试来源", "domain": "example.com", "list_page_url": "https://example.com/list"}, db)
    executor = ToolExecutor(context, build_default_registry())
    status = executor.execute("get_system_status", {})
    sources = executor.execute("list_sources", {})
    assert status.status == "success" and status.data["database"] == "正常"
    assert sources.data["sources"][0]["source_name"] == "测试来源"
    with pytest.raises(KeyError):
        executor.execute("run_arbitrary_python", {})


def test_tool_parameters_are_strictly_validated_and_confirmation_is_enforced(tmp_path: Path):
    db, _, _, workspace, _, context = _setup(tmp_path)
    executor = ToolExecutor(context)
    with pytest.raises(ValidationError):
        executor.execute("run_crawl", {"max_articles": 9999, "unknown": True})
    pending = executor.execute("initialize_recommended_sources", {})
    assert pending.status == "pending_confirmation"
    assert list_sources(workspace["workspace_id"], db) == []
    assert list_pending_approvals(workspace["workspace_id"], db)[0]["approval_id"] == pending.approval_id
    completed = executor.execute("initialize_recommended_sources", {}, confirmed=True)
    assert completed.status == "success" and len(list_sources(workspace["workspace_id"], db)) == 4


class FakeManager:
    def __init__(self):
        self.calls = []

    def run(self, workspace_id, **kwargs):
        self.calls.append((workspace_id, kwargs))
        return CrawlResult("CRAWL-TEST", "完成", source_count=2, discovered_count=3, fetched_count=2, new_document_count=2, new_event_count=2)


def test_agent_run_crawl_uses_registered_bounded_tool(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    manager = FakeManager()
    context.crawl_manager_factory = lambda: manager
    executor = ToolExecutor(context)
    pending = executor.execute("run_crawl", {"source_ids": [], "max_articles": 12, "start_date": "2026-07-15", "end_date": "2026-07-21"})
    assert pending.status == "pending_confirmation" and not manager.calls
    result = executor.execute("run_crawl", {"source_ids": [], "max_articles": 12, "start_date": "2026-07-15", "end_date": "2026-07-21"}, confirmed=True)
    assert result.data["new_document_count"] == 2
    assert manager.calls[0][1]["max_articles"] == 12


def test_agent_search_and_generate_report_return_three_artifacts(tmp_path: Path):
    _, _, _, _, paths, context = _setup(tmp_path)
    executor = ToolExecutor(context)
    search = executor.execute("search_documents", {"query": "航行警告", "limit": 5})
    assert search.status == "success" and search.data["results"] == []
    _seed_eligible_report_event(context)
    report = executor.execute("generate_report", {
        "report_title": "测试周报", "client_name": "货代公司", "start_date": "2026-07-15", "end_date": "2026-07-21",
    })
    assert report.status == "success"
    assert {item["kind"] for item in report.artifacts} == {"DOCX", "HTML", "Excel"}
    assert all(Path(item["path"]).is_file() for item in report.artifacts)


def test_conversations_are_workspace_isolated_and_secrets_are_redacted(tmp_path: Path):
    db = tmp_path / "conversation.db"
    root = tmp_path / "data"
    one = create_workspace({"workspace_name": "一"}, db, root)
    two = create_workspace({"workspace_name": "二"}, db, root)
    initialize_database(db)
    first = create_conversation(one["workspace_id"], db)
    second = create_conversation(two["workspace_id"], db)
    append_message(first, one["workspace_id"], "user", "key=sk-secret-12345678", db, metadata={"api_key": "sk-secret-12345678"})
    append_message(second, two["workspace_id"], "user", "另一个空间", db)
    messages = load_messages(first, one["workspace_id"], db)
    assert len(messages) == 1 and "sk-secret-12345678" not in json.dumps(messages, ensure_ascii=False)
    assert load_messages(first, two["workspace_id"], db) == []


class MockResponse:
    def __init__(self, message):
        self.message = message

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": self.message}]}


class SequenceRequester:
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        return MockResponse(self.messages.pop(0))


def test_tool_call_reasoning_content_is_returned_without_being_displayed(tmp_path: Path):
    _, _, env, workspace, _, context = _setup(tmp_path, ai=True)
    save_settings_to_env(api_key="sk-mock-12345678", env_path=env)
    context.workspace = get_workspace(workspace["workspace_id"], context.db_path)
    requester = SequenceRequester([
        {"content": "我先检查系统。", "reasoning_content": "hidden-reasoning", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_system_status", "arguments": "{}"}}]},
        {"content": "系统状态正常。", "reasoning_content": "hidden-final", "tool_calls": []},
        {"content": "继续正常。", "reasoning_content": "hidden-next", "tool_calls": []},
    ])
    turn = AgentOrchestrator(ToolExecutor(context), requester=requester).run("检查当前系统状态")
    assert turn.answer == "系统状态正常。" and turn.used_ai
    assert requester.calls[0]["json"]["thinking"] == {"type": "enabled"}
    second_messages = requester.calls[1]["json"]["messages"]
    assistant = next(item for item in second_messages if item.get("tool_calls"))
    assert assistant["reasoning_content"] == "hidden-reasoning"
    assert "hidden-reasoning" not in turn.answer
    AgentOrchestrator(ToolExecutor(context), requester=requester).run("继续检查")
    next_turn_messages = requester.calls[2]["json"]["messages"]
    assert any(item.get("reasoning_content") == "hidden-reasoning" for item in next_turn_messages)


def test_repeated_identical_tool_calls_stop_after_two(tmp_path: Path):
    _, _, env, workspace, _, context = _setup(tmp_path, ai=True)
    save_settings_to_env(api_key="sk-mock-12345678", env_path=env)
    context.workspace = get_workspace(workspace["workspace_id"], context.db_path)
    repeated = {"content": "", "reasoning_content": "hidden", "tool_calls": [{"id": "same", "type": "function", "function": {"name": "get_system_status", "arguments": "{}"}}]}
    requester = SequenceRequester([repeated, repeated, repeated])
    turn = AgentOrchestrator(ToolExecutor(context), requester=requester).run("反复检查")
    assert turn.stopped_reason == "loop_detected"
    assert len(requester.calls) == 2


def test_no_api_uses_local_agent_mode_and_cannot_read_key(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    turn = AgentOrchestrator(ToolExecutor(context)).run("查看当前系统状态")
    assert not turn.used_ai and turn.cards[0]["tool_name"] == "get_system_status"
    config = ToolExecutor(context).execute("configure_ai", {"requested_action": "status"})
    assert "API" in config.message and "DEEPSEEK_API_KEY" not in json.dumps(config.model_dump(), ensure_ascii=False)


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, url, allowed_domains, rate_limit_seconds, **kwargs):
        self.calls.append(url)
        if len(self.calls) == 1:
            links = "".join(f'<a href="/news/{i}.html">公告{i}</a>' for i in range(10))
            return FetchedContent(True, url, url, url, raw_html=f"<html>{links}</html>", text="列表", status="成功")
        return FetchedContent(True, url, url, url, title="测试公告", text="这是足够长的公开正文内容，用于验证单篇测试提取。", raw_html="<article>正文</article>", status="成功")


def public_resolver(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def test_source_assistant_fetches_one_list_and_at_most_one_article():
    fetcher = FakeFetcher()
    result = analyze_single_source("https://example.com/news", fetcher=fetcher, resolver=public_resolver)
    assert result["ok"] and result["candidate_count"] == 5
    assert len(fetcher.calls) == 2
    assert result["adapter_suggestion"]["article_link_selector"] == "a[href]"


def test_explain_crawl_failure_returns_plain_language(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    result = ToolExecutor(context).execute("explain_error", {"error_text": "HTTP 403 页面拒绝访问", "source_name": "测试来源"})
    assert result.status == "success" and not result.data["can_retry"]
    assert "不会绕过" in result.message


def test_release_package_excludes_environment_git_secret_and_real_data(tmp_path: Path):
    root = tmp_path / "project"
    (root / ".venv").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "data" / "raw").mkdir(parents=True)
    (root / "data").joinpath("events_template.csv").write_text("header\n", encoding="utf-8")
    (root / "data").joinpath("portscope.db").write_bytes(b"secret-db")
    (root / ".env").write_text("DEEPSEEK_API_KEY=sk-secret", encoding="utf-8")
    (root / "app.py").write_text("print('ok')", encoding="utf-8")
    (root / ".venv" / "huge.bin").write_bytes(b"x" * 100)
    (root / ".git" / "config").write_text("git", encoding="utf-8")
    output = tmp_path / "release.zip"
    result = build_release(root, output)
    with ZipFile(output) as archive:
        names = archive.namelist()
    assert any(name.endswith("app.py") for name in names)
    assert any(name.endswith("events_template.csv") for name in names)
    assert not any(".venv" in name or "/.git/" in name or name.endswith(".env") or "portscope.db" in name for name in names)
    assert result["size_bytes"] < 10000


def test_registry_contains_all_declared_business_tools():
    names = {item.tool_name for item in build_default_registry().specs()}
    assert len(names) == 27
    assert {"run_crawl", "ask_knowledge_base", "approve_events", "generate_report", "export_data", "explain_error"}.issubset(names)


def test_test_ai_connection_tool_uses_mock_only(tmp_path: Path):
    _, _, env, _, _, context = _setup(tmp_path)
    save_settings_to_env(api_key="sk-mock-12345678", env_path=env)
    context.requester = SequenceRequester([{"content": "OK", "tool_calls": []}])
    result = ToolExecutor(context).execute("test_ai_connection", {})
    assert result.status == "success" and result.data["model"] == "deepseek-v4-flash"
    assert len(context.requester.calls) == 1


def test_add_and_enable_source_both_respect_confirmation_and_robots(tmp_path: Path):
    db, _, _, workspace, _, context = _setup(tmp_path)
    executor = ToolExecutor(context)
    arguments = {
        "source_name": "官方测试栏目", "organization": "测试机构",
        "homepage_url": "https://example.com", "list_page_url": "https://example.com/news",
        "url_pattern": "^https://example\\.com/news/.+", "robots_status": "未检查",
    }
    pending = executor.execute("add_source", arguments)
    assert pending.status == "pending_confirmation" and list_sources(workspace["workspace_id"], db) == []
    added = executor.execute("add_source", arguments, confirmed=True)
    source_id = added.data["source_id"]
    enable_pending = executor.execute("enable_source", {"source_id": source_id})
    assert enable_pending.status == "pending_confirmation"
    blocked = executor.execute("enable_source", {"source_id": source_id}, confirmed=True)
    assert blocked.status == "error" and "robots" in blocked.message


def test_local_workbench_completes_update_query_and_report_without_advanced_pages(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    manager = FakeManager()
    context.crawl_manager_factory = lambda: manager
    orchestrator = AgentOrchestrator(ToolExecutor(context))
    update = orchestrator.run("更新最近7天青岛港公开数据")
    assert update.cards[0]["status"] == "pending_confirmation"
    approval = list_pending_approvals(context.workspace_id, context.db_path)[0]
    executed = orchestrator.resolve_approval(approval["approval_id"], True)
    assert executed.cards[0]["data"]["new_document_count"] == 2
    answer = orchestrator.run("这些信息里最重要的风险是什么？")
    assert answer.cards[0]["tool_name"] == "ask_knowledge_base"
    _seed_eligible_report_event(context, event_date=(date.today() - timedelta(days=3)).isoformat())
    report = orchestrator.run("生成一份面向货代公司的周报")
    assert {item["kind"] for item in report.cards[0]["artifacts"]} == {"DOCX", "HTML", "Excel"}


def test_export_is_confined_to_workspace_exports_directory(tmp_path: Path):
    _, _, _, _, paths, context = _setup(tmp_path)
    result = ToolExecutor(context).execute("export_data", {"table": "events"})
    target = Path(result.artifacts[0]["path"]).resolve()
    target.relative_to((paths.root / "exports").resolve())
    assert target.is_file() and target.suffix == ".csv"


class FailingRequester:
    def post(self, *args, **kwargs):
        import requests
        raise requests.ConnectionError("mock network down")


def test_agent_api_failure_safely_falls_back_to_local_mode(tmp_path: Path):
    _, _, env, workspace, _, context = _setup(tmp_path, ai=True)
    save_settings_to_env(api_key="sk-mock-12345678", max_retries=0, env_path=env)
    context.workspace = get_workspace(workspace["workspace_id"], context.db_path)
    turn = AgentOrchestrator(ToolExecutor(context), requester=FailingRequester()).run("查看当前系统状态")
    assert not turn.used_ai
    assert "切换到本地模式" in turn.answer


def test_batch_approve_and_reject_tools_cannot_run_without_confirmation(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    executor = ToolExecutor(context)
    approve = executor.execute("approve_events", {"event_ids": ["EVT-X"], "reviewer_note": "已核对"})
    reject = executor.execute("reject_events", {"event_ids": ["EVT-Y"], "reviewer_note": "退回"})
    assert approve.status == reject.status == "pending_confirmation"
    assert len(list_pending_approvals(context.workspace_id, context.db_path)) == 2


class FakeIndexer:
    def __init__(self):
        self.called = False

    def rebuild(self, workspace_id):
        self.called = True
        return {"documents": 2, "success": 2, "failed": 0, "chunks": 4}


def test_rebuild_index_is_confirmed_before_execution(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    indexer = FakeIndexer()
    context.indexer_factory = lambda: indexer
    executor = ToolExecutor(context)
    assert executor.execute("rebuild_index", {}).status == "pending_confirmation"
    assert not indexer.called
    result = executor.execute("rebuild_index", {}, confirmed=True)
    assert indexer.called and result.data["chunks"] == 4


def test_crawl_progress_and_result_read_latest_sqlite_run(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    with transaction(context.db_path) as connection:
        connection.execute(
            """INSERT INTO crawl_runs(crawl_run_id,workspace_id,started_at,finished_at,status,source_count,
            new_document_count,failed_count) VALUES(?,?,?,?,?,?,?,?)""",
            ("CRAWL-LATEST", context.workspace_id, now_iso(), now_iso(), "部分完成", 3, 2, 1),
        )
    executor = ToolExecutor(context)
    progress = executor.execute("get_crawl_progress", {})
    result = executor.execute("get_crawl_result", {})
    assert progress.data["crawl_run"]["crawl_run_id"] == "CRAWL-LATEST"
    assert result.data["crawl_run"]["failed_count"] == 1


def test_list_reports_returns_generated_report_history(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    executor = ToolExecutor(context)
    _seed_eligible_report_event(context)
    executor.execute("generate_report", {
        "report_title": "历史报告", "start_date": "2026-07-15", "end_date": "2026-07-21",
    })
    history = executor.execute("list_reports", {})
    assert len(history.data["reports"]) == 1
    assert history.data["reports"][0]["report_title"] == "历史报告"


def test_source_assistant_reuses_ssrf_guard_for_localhost():
    with pytest.raises(URLSafetyError):
        analyze_single_source("http://127.0.0.1/private", fetcher=FakeFetcher(), resolver=public_resolver)


def test_agent_cannot_mark_report_as_formally_published(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    result = ToolExecutor(context).execute("generate_report", {
        "report_title": "正式发布测试", "start_date": "2026-07-15", "end_date": "2026-07-21", "formal_publish": True,
    })
    assert result.status == "error" and "人工确认" in result.message


def test_agent_conversation_survives_new_orchestrator_instance(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    AgentOrchestrator(ToolExecutor(context)).run("查看当前系统状态")
    messages = load_messages(context.conversation_id, context.workspace_id, context.db_path)
    assert [item["role"] for item in messages] == ["user", "assistant"]
    second = AgentOrchestrator(ToolExecutor(context)).run("列出公开来源")
    assert second.cards[0]["tool_name"] == "list_sources"
    assert len(load_messages(context.conversation_id, context.workspace_id, context.db_path)) == 4


def test_streaming_tool_call_fragments_are_reassembled_without_showing_reasoning(tmp_path: Path):
    _, _, _, _, _, context = _setup(tmp_path)
    orchestrator = AgentOrchestrator(ToolExecutor(context))

    class SSE:
        def iter_lines(self, decode_unicode=True):
            chunks = [
                {"choices": [{"delta": {"reasoning_content": "hidden-"}}]},
                {"choices": [{"delta": {"content": "准备执行", "tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "get_system_", "arguments": "{"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "status", "arguments": "}"}}]}}]},
            ]
            for chunk in chunks:
                yield "data: " + json.dumps(chunk, ensure_ascii=False)
            yield "data: [DONE]"

    message = orchestrator._streaming_response_message(SSE())
    assert message["content"] == "准备执行"
    assert message["reasoning_content"] == "hidden-"
    assert message["tool_calls"][0]["function"] == {"name": "get_system_status", "arguments": "{}"}
