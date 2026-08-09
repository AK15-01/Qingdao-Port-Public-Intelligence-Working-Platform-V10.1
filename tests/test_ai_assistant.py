import json
from pathlib import Path

import pytest

from ai_assistant import AI_DRAFT_LABEL, request_deepseek_suggestions


class MockResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class MockRequester:
    def __init__(self, content):
        self.content = content
        self.called = False
        self.last_json = None

    def post(self, *args, **kwargs):
        self.called = True
        self.last_json = kwargs["json"]
        return MockResponse(self.content)


def test_unconfigured_ai_safely_falls_back(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = request_deepseek_suggestions("公开文本", "摘要", user_confirmed=True, api_key="")
    assert not result.ok
    assert "继续使用" in result.note


def test_ai_requires_explicit_send_confirmation():
    with pytest.raises(PermissionError):
        request_deepseek_suggestions("公开文本", "摘要", user_confirmed=False, api_key="fake")


def test_ai_valid_json_uses_mock_and_marks_draft():
    requester = MockRequester(json.dumps({"summary": "公开事实摘要", "category": "政策监管"}, ensure_ascii=False))
    result = request_deepseek_suggestions(
        "公开文本", "摘要", user_confirmed=True, api_key="fake", requester=requester
    )
    assert requester.called
    assert result.ok and result.used_ai
    assert result.suggestions["summary"].startswith(AI_DRAFT_LABEL)
    assert result.suggestions["category"] == "政策监管"
    assert requester.last_json["thinking"] == {"type": "disabled"}
    assert requester.last_json["stream"] is False


def test_ai_invalid_output_safely_falls_back():
    result = request_deepseek_suggestions(
        "公开文本", "摘要", user_confirmed=True, api_key="fake", requester=MockRequester("not-json")
    )
    assert not result.ok
    assert "安全回退" in result.note


def test_personal_information_is_blocked_before_request():
    requester = MockRequester("{}")
    result = request_deepseek_suggestions(
        "联系人手机号：13800138000", "摘要", user_confirmed=True, api_key="fake", requester=requester
    )
    assert not result.ok
    assert not requester.called


def test_env_file_is_ignored_and_example_contains_no_key():
    ignored = Path(".gitignore").read_text(encoding="utf-8")
    example = Path(".env.example").read_text(encoding="utf-8")
    assert ".env" in ignored.splitlines()
    assert "DEEPSEEK_API_KEY=\n" in example
    assert "DEEPSEEK_MODEL=deepseek-v4-flash" in example
    assert "DEEPSEEK_BASE_URL=https://api.deepseek.com" in example
    assert "DEEPSEEK_TIMEOUT=60" in example
