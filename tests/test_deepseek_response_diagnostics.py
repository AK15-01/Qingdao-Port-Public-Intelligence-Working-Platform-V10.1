from __future__ import annotations

from dataclasses import asdict
import json

from deepseek_service import DeepSeekSettings, test_deepseek_connection as diagnose


class Response:
    def __init__(self, payload=None, *, status=200, content_type="application/json", raw_text=None):
        self.payload = payload
        self.status_code = status
        self.headers = {"Content-Type": content_type, "x-request-id": "req-test-123"}
        self.text = raw_text if raw_text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self.payload is None:
            raise ValueError("invalid json")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))


class Requester:
    def __init__(self, chat_response):
        self.chat_response = chat_response
        self.chat_json = None

    def get(self, url, **kwargs):
        if url.endswith("/models"):
            return Response({"data": [{"id": "deepseek-current-fast"}]})
        return Response({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "1.00"}]})

    def post(self, url, **kwargs):
        self.chat_json = kwargs["json"]
        return self.chat_response


def settings():
    return DeepSeekSettings(
        api_key="sk-private-secret-123456", model="auto-extraction",
        extraction_model="auto-extraction", max_retries=0, api_key_source="项目.env",
    )


def test_http_200_content_ok_is_success_and_uses_non_thinking_request(tmp_path):
    response = Response({
        "model": "deepseek-current-fast",
        "choices": [{"message": {"content": "OK", "reasoning_content": ""}, "finish_reason": "stop"}],
    })
    requester = Requester(response)
    result = diagnose(settings(), requester=requester, cache_path=tmp_path / "models.json")
    assert result.ok and result.http_status == 200 and result.generation_http_status == 200
    assert result.model_name == "deepseek-current-fast" and result.finish_reason == "stop"
    assert result.content_length == 2 and result.reasoning_content_length == 0
    assert result.content_type == "application/json" and result.request_id == "req-test-123"
    assert requester.chat_json["thinking"] == {"type": "disabled"}
    assert requester.chat_json["max_tokens"] >= 32 and requester.chat_json["stream"] is False


def test_http_200_reasoning_length_explains_token_limit(tmp_path):
    response = Response({
        "model": "deepseek-current-fast",
        "choices": [{"message": {"content": "", "reasoning_content": "正在思考"}, "finish_reason": "length"}],
    })
    result = diagnose(settings(), requester=Requester(response), cache_path=tmp_path / "models.json")
    assert not result.ok and result.http_status == 200 and result.generation_http_status == 200
    assert result.error_type == "reasoning_truncated"
    assert "思考内容占满Token" in result.status
    assert result.reasoning_content_length > 0 and result.finish_reason == "length"


def test_http_200_invalid_json_preserves_status_and_safe_preview(tmp_path):
    response = Response(None, raw_text='{"partial":"sk-private-secret-123456"')
    result = diagnose(settings(), requester=Requester(response), cache_path=tmp_path / "models.json")
    assert not result.ok and result.http_status == 200 and result.generation_http_status == 200
    assert result.error_type == "invalid_response" and result.response_text_length > 0
    assert "sk-private-secret-123456" not in result.response_preview
    assert "[已脱敏]" in result.response_preview or "sk-****" in result.response_preview


def test_http_200_non_json_content_type_is_explicit(tmp_path):
    response = Response(None, content_type="text/html; charset=utf-8", raw_text="<html>gateway</html>")
    result = diagnose(settings(), requester=Requester(response), cache_path=tmp_path / "models.json")
    assert result.http_status == 200 and result.error_type == "invalid_content_type"
    assert "Content-Type不是JSON" in result.status and result.content_type.startswith("text/html")


def test_http_200_empty_choices_is_explicit(tmp_path):
    response = Response({"model": "deepseek-current-fast", "choices": []})
    result = diagnose(settings(), requester=Requester(response), cache_path=tmp_path / "models.json")
    assert result.http_status == 200 and result.error_type == "empty_choices"
    assert "choices 为空" in result.status


def test_api_key_never_enters_diagnostic_result(tmp_path):
    response = Response(None, content_type="text/plain", raw_text="Bearer sk-private-secret-123456")
    result = diagnose(settings(), requester=Requester(response), cache_path=tmp_path / "models.json")
    assert "sk-private-secret-123456" not in json.dumps(asdict(result), ensure_ascii=False)
