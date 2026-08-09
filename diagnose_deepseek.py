from __future__ import annotations

"""Safe, standalone DeepSeek connectivity diagnosis.

Reads the effective local configuration through deepseek_service. It never
prints the API key, Authorization header, hidden prompt, or request body.
"""

from deepseek_service import load_settings, mask_api_key, test_deepseek_connection


def _http(value: int) -> str:
    return str(value) if value else "未执行"


def main() -> int:
    settings = load_settings()
    print("PortScope DeepSeek 连接诊断")
    print(f"API Key来源：{settings.api_key_source}")
    print(f"API Key状态：{mask_api_key(settings.api_key)}")
    print(f"Base URL：{settings.base_url}")
    print("POST API端点（不能通过浏览器直接打开测试）："
          f"{settings.base_url}/chat/completions")

    result = test_deepseek_connection(settings)
    print(f"GET /models HTTP：{_http(result.models_http_status)}")
    print("模型列表状态：" + ("、".join(result.available_models) if result.available_models else "未取得可用模型列表"))
    print(f"GET /user/balance HTTP：{_http(result.balance_http_status)}")
    balance = "可用" if result.balance_available is True else "不可用" if result.balance_available is False else "未知"
    print(f"余额状态：{balance}" + (f"（{result.balance_summary}）" if result.balance_summary else ""))
    print(f"POST /chat/completions HTTP：{_http(result.generation_http_status)}")
    print(f"实际模型：{result.model_name or '未确定'}")
    print(f"Content-Type：{result.content_type or '未返回'}")
    print(f"响应文本长度：{result.response_text_length}")
    print(f"finish_reason：{result.finish_reason or '未返回'}")
    print(f"content长度：{result.content_length}")
    print(f"reasoning_content长度：{result.reasoning_content_length}")
    if result.request_id:
        print(f"request-id：{result.request_id}")
    print(f"最终结果：{'连接测试通过' if result.ok else '连接测试未通过'}")
    print(f"诊断说明：{result.status}")
    if result.error_code or result.error_type:
        print(f"错误代码：{result.error_code or result.error_type}")
    if result.safe_error_message:
        print(f"脱敏错误原因：{result.safe_error_message}")
    if result.response_preview:
        print(f"脱敏响应预览（最多200字符）：{result.response_preview}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
