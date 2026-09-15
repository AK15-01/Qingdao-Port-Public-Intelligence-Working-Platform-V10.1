from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pytest
import requests

from crawler.content_fetcher import ContentFetcher
from crawler.crawl_manager import CrawlManager
from deepseek_service import (
    DeepSeekSettings, available_models_for_call, clear_local_api_key, load_model_cache,
    load_settings, save_model_cache, save_settings_to_env, test_deepseek_connection as diagnose,
)
from event_pipeline import batch_confirm_events
from evidence_binding import replace_event_evidence
from platform_db import connect
from platform_report import ReportEligibilityError, generate_platform_report
from source_health import validate_enabled_sources
from tests.test_crawler_pipeline import AllowRobots, PUBLIC_DNS, Requester, _setup as crawler_setup
from tests.test_deepseek_and_rag import _store
from workspace_store import load_reports, workspace_paths


class JSONResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class DiagnosticRequester:
    def __init__(self, *, models=None, balance_available=True, fail_status=0, fail_stage=""):
        self.models = models or ["deepseek-v5-pro", "deepseek-v5-fast"]
        self.balance_available = balance_available
        self.fail_status = fail_status
        self.fail_stage = fail_stage
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        stage = "models" if url.endswith("/models") else "balance"
        if self.fail_stage == stage:
            return JSONResponse({"error": {"code": f"E{self.fail_status}", "message": "safe detail sk-secret-LEAK"}}, self.fail_status)
        if stage == "models":
            return JSONResponse({"object": "list", "data": [{"id": item} for item in self.models]})
        return JSONResponse({"is_available": self.balance_available, "balance_infos": [{"currency": "CNY", "total_balance": "12.50"}]})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self.fail_stage == "chat":
            return JSONResponse({"error": {"code": f"E{self.fail_status}", "message": "chat failed sk-secret-LEAK"}}, self.fail_status)
        return JSONResponse({"choices": [{"message": {"content": "OK"}}], "model": kwargs["json"]["model"]})


def test_three_stage_diagnostic_dynamic_models_balance_and_short_chat(tmp_path: Path):
    requester = DiagnosticRequester()
    settings = DeepSeekSettings("sk-secret-LEAK", "auto-extraction", max_retries=0, api_key_source="项目.env")
    result = diagnose(settings, requester=requester, cache_path=tmp_path / "models.json")
    assert result.ok and result.model_name == "deepseek-v5-fast"
    assert result.available_models == ("deepseek-v5-pro", "deepseek-v5-fast")
    assert result.balance_available is True and result.balance_summary == "CNY 12.50"
    assert [method for method, _, _ in requester.calls] == ["GET", "GET", "POST"]
    assert requester.calls[-1][2]["json"]["max_tokens"] >= 32
    assert requester.calls[-1][2]["json"]["thinking"] == {"type": "disabled"}
    assert requester.calls[-1][2]["json"]["stream"] is False
    assert load_model_cache(tmp_path / "models.json")["models"] == requester.models


@pytest.mark.parametrize("status,expected", [
    (400, "invalid_request"), (401, "invalid_api_key"), (402, "insufficient_balance"),
    (403, "permission_denied"), (404, "endpoint_not_found"),
    (422, "unsupported_parameters"), (429, "rate_limited"), (503, "provider_unavailable"),
])
def test_diagnostic_http_errors_are_distinct_and_redacted(tmp_path: Path, status: int, expected: str):
    requester = DiagnosticRequester(fail_status=status, fail_stage="models")
    result = diagnose(DeepSeekSettings("sk-secret-LEAK", "auto-extraction"), requester=requester, cache_path=tmp_path / f"{status}.json")
    assert not result.ok and result.http_status == status and result.error_type == expected
    assert result.error_code == f"E{status}" and "sk-secret-LEAK" not in result.safe_error_message
    assert result.failed_stage == "模型列表"


def test_diagnostic_timeout_and_balance_unavailable_are_distinct(tmp_path: Path):
    class TimeoutRequester(DiagnosticRequester):
        def get(self, url, **kwargs):
            raise requests.Timeout("do not expose request")

    timed_out = diagnose(DeepSeekSettings("key", "auto-extraction"), requester=TimeoutRequester(), cache_path=tmp_path / "timeout.json")
    assert timed_out.error_type == "timeout" and "网络超时" in timed_out.status
    balance = diagnose(
        DeepSeekSettings("key", "auto-extraction"), requester=DiagnosticRequester(balance_available=False),
        cache_path=tmp_path / "balance.json",
    )
    assert balance.error_type == "insufficient_balance" and balance.balance_available is False


def test_model_cache_and_custom_model_do_not_require_python_whitelist(tmp_path: Path):
    cache = tmp_path / "models.json"
    save_model_cache(["provider-new-model-2027"], cache)
    requester = DiagnosticRequester(models=["should-not-be-called"])
    models, source = available_models_for_call(DeepSeekSettings("key", "provider-new-model-2027"), requester, cache)
    assert models == ["provider-new-model-2027"] and "缓存" in source and requester.calls == []
    env = tmp_path / ".env"
    saved = save_settings_to_env(api_key="key", agent_model="provider-new-agent", extraction_model="provider-new-fast", env_path=env)
    assert saved.agent_model == "provider-new-agent" and saved.extraction_model == "provider-new-fast"


def test_project_env_overrides_stale_system_key_and_clear_does_not_resurrect(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-system-old")
    save_settings_to_env(api_key="sk-project-new", env_path=env)
    settings = load_settings(env)
    assert settings.api_key == "sk-project-new" and settings.api_key_source == "指定配置文件"
    clear_local_api_key(env)
    assert load_settings(env).api_key == ""
    assert "sk-system-old" not in env.read_text(encoding="utf-8")


def test_pipeline_records_all_stages_and_ai_usage_without_breaking_local_mode(tmp_path: Path):
    db, root, workspace, _ = crawler_setup(tmp_path)
    pages = {
        "https://example.com/list": '<a class="news" href="/notice/1">公开风险测试</a>',
        "https://example.com/notice/1": '<html><head><title>公开风险测试</title></head><body><article><p>这是公开测试正文，用于验证完整流水线统计，不代表任何真实港口运行信息。相关水域临时限航，请人工核对原文。</p></article></body></html>',
    }
    manager = CrawlManager(
        db, root, fetcher=ContentFetcher(requester=Requester(pages), resolver=PUBLIC_DNS, sleeper=lambda _: None),
        robots_checker=AllowRobots(), ai_enabled=False,
    )
    result = manager.run(workspace["workspace_id"])
    expected = {"数据源检查", "列表发现", "正文抓取", "内容清洗", "URL与正文去重", "原文归档", "结构化抽取", "风险与商机评分", "Chunk", "FTS5", "向量索引", "待审核", "报告候选"}
    assert expected.issubset(result.stage_stats)
    assert result.api_call_count == 0 and result.new_document_count == 1 and result.new_event_count == 1
    with connect(db) as connection:
        row = connection.execute("SELECT stage_stats_json,duration_ms,estimated_usage FROM crawl_runs WHERE crawl_run_id=?", (result.crawl_run_id,)).fetchone()
    assert expected.issubset(json.loads(row["stage_stats_json"])) and row["duration_ms"] >= 0 and "0次API调用" in row["estimated_usage"]


def test_source_health_acceptance_is_bounded_and_persisted_with_mock(tmp_path: Path):
    db, _, workspace, source_id = crawler_setup(tmp_path)
    pages = {
        "https://example.com/list": '<a class="news" href="/notice/1">第一篇</a><a class="news" href="/notice/2">第二篇</a><a class="news" href="/notice/3">第三篇</a>',
        "https://example.com/notice/1": '<article><p>第一篇公开正文足够用于健康检查，内容不代表真实港口信息，必须人工核验。本文继续补充公开测试说明，以满足正文提取的最小质量要求，绝不作为真实事件。</p></article>',
        "https://example.com/notice/2": '<article><p>第二篇公开正文足够用于健康检查，内容不代表真实港口信息，必须人工核验。本文继续补充公开测试说明，以满足正文提取的最小质量要求，绝不作为真实事件。</p></article>',
    }
    requester = Requester(pages)
    health = validate_enabled_sources(
        workspace["workspace_id"], db,
        fetcher=ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None),
        robots_checker=AllowRobots(),
    )
    assert len(requester.calls) == 3 and health[0]["discovered_count"] == 3 and health[0]["fetched_count"] == 2
    with connect(db) as connection:
        row = connection.execute("SELECT health_status,health_details_json FROM sources WHERE source_id=?", (source_id,)).fetchone()
    assert row["health_status"] == "部分可用" and json.loads(row["health_details_json"])["list_accessible"] is True


def _sheet_names(path: Path) -> list[str]:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("xl/workbook.xml"))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [item.attrib["name"] for item in root.findall("x:sheets/x:sheet", ns)]


def test_internal_report_has_content_and_customer_report_enforces_gates(tmp_path: Path):
    db, root, workspace, source_id, document_id, event_id, _ = _store(tmp_path)
    paths = workspace_paths(workspace["workspace_id"], root)
    with connect(db) as connection:
        connection.execute("UPDATE sources SET commercial_reuse_status='未明确',report_use_allowed=0 WHERE source_id=?", (source_id,))
        connection.execute("UPDATE events SET extraction_confidence=0.8 WHERE event_id=?", (event_id,))
        connection.commit()
    internal = generate_platform_report(
        workspace, paths, client=None, start_date="2026-07-01", end_date="2026-07-31",
        report_title="内部研究测试", analyst="测试", db_path=db, report_mode="内部研究版",
    )
    assert internal.event_ids == (event_id,)
    html = internal.html_path.read_text(encoding="utf-8")
    assert "仅供内部研究" in html and "来源使用提示" in html and "[分析]" in html
    assert _sheet_names(internal.xlsx_path)[0] == "内部使用说明"
    with pytest.raises(ReportEligibilityError) as blocked:
        generate_platform_report(
            workspace, paths, client=None, start_date="2026-07-01", end_date="2026-07-31",
            report_title="客户交付测试", analyst="测试", db_path=db, report_mode="客户交付版",
        )
    assert "未人工确认" in str(blocked.value) and "内部研究版" in str(blocked.value)
    with connect(db) as connection:
        connection.execute(
            """UPDATE sources SET commercial_reuse_status='未明确',report_use_allowed=0,
            customer_summary_allowed=1,short_quote_allowed=1,
            terms_status='未检查',license_note='使用状态未明确。',
            last_license_checked_at='2020-01-01' WHERE source_id=?""",
            (source_id,),
        )
        replace_event_evidence(
            connection,
            event_id,
            document_id,
            ["相关水域临时限航"],
        )
        connection.commit()
    old_confirmation = batch_confirm_events(
        [event_id], workspace["workspace_id"], db,
        reviewer_type="human_user", reviewer_name="测试人员",
        review_method="测试中逐项核对",
    )
    assert old_confirmation["confirmed"] == [event_id]
    assert old_confirmation["report_ineligible"] == {}
    unknown_permission = generate_platform_report(
        workspace, paths, client=None, start_date="2026-07-01", end_date="2026-07-31",
        report_title="使用状态待核对测试", analyst="测试", db_path=db, report_mode="客户交付版",
    )
    assert "使用状态未明确" in unknown_permission.html_path.read_text(encoding="utf-8")
    with connect(db) as connection:
        connection.execute(
            """UPDATE sources SET commercial_reuse_status='允许',report_use_allowed=1,
            customer_summary_allowed=1,short_quote_allowed=1,terms_status='允许',
            license_note='测试来源已确认允许，仅用于自动测试。',
            last_license_checked_at='2026-07-23' WHERE source_id=?""",
            (source_id,),
        )
        connection.commit()
    current_confirmation = batch_confirm_events(
        [event_id], workspace["workspace_id"], db,
        reviewer_type="human_user", reviewer_name="测试人员",
        review_method="测试中逐项核对",
    )
    assert current_confirmation["confirmed"] == [event_id]
    assert current_confirmation["report_ineligible"] == {}
    client = generate_platform_report(
        workspace, paths, client=None, start_date="2026-07-01", end_date="2026-07-31",
        report_title="客户交付测试", analyst="测试", db_path=db, report_mode="客户交付版",
    )
    assert client.event_ids == (event_id,)
    reports = load_reports(workspace["workspace_id"], db)
    assert {item["report_mode"] for item in reports} >= {"内部研究版", "客户交付版"}
    assert all(item["analysis_model"] == "deterministic-rules" for item in reports)
    with connect(db) as connection:
        snapshots = connection.execute(
            """SELECT report_id,report_version,payload_hash,file_hashes_json,delivery_status
            FROM report_delivery_snapshots ORDER BY generated_at"""
        ).fetchall()
    assert len(snapshots) == 2
    assert all(row["payload_hash"] and json.loads(row["file_hashes_json"]) for row in snapshots)
    assert all(row["delivery_status"] == "ready_for_delivery" for row in snapshots)
