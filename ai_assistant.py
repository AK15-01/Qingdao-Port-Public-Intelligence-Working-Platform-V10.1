from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Mapping, Optional

import requests
from dotenv import dotenv_values

from data_store import ALLOWED_CATEGORIES
from deepseek_service import load_settings, model_for_call


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"
DEFAULT_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEPRECATED_MODELS = {"deepseek-chat", "deepseek-reasoner"}
AI_DRAFT_LABEL = "AI辅助初稿，需人工确认。"
ALLOWED_OUTPUT_FIELDS = {
    "summary",
    "impact",
    "action",
    "category",
    "executive_summary",
}


@dataclass(frozen=True)
class AIResult:
    ok: bool
    suggestions: dict[str, str] = field(default_factory=dict)
    note: str = ""
    used_ai: bool = False


def load_env_file(path: Optional[Path] = None) -> dict[str, str]:
    """Read .env with python-dotenv without logging or mutating process secrets."""
    target = path or DEFAULT_ENV_PATH
    if not target.exists():
        return {}
    return {str(key): str(value) for key, value in dotenv_values(target).items() if key and value}


def deepseek_api_key(env_path: Optional[Path] = None) -> str:
    return load_settings(env_path).api_key


def ai_available(env_path: Optional[Path] = None) -> bool:
    return bool(deepseek_api_key(env_path))


def deepseek_model(env_path: Optional[Path] = None) -> str:
    return load_settings(env_path).extraction_model


def deepseek_base_url(env_path: Optional[Path] = None) -> str:
    return load_settings(env_path).base_url


def deepseek_timeout(env_path: Optional[Path] = None) -> float:
    return load_settings(env_path).timeout


def contains_personal_data(text: str) -> bool:
    value = str(text or "")
    patterns = [
        r"\b1[3-9]\d{9}\b",
        r"\b\d{17}[0-9Xx]\b",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        r"(?:手机号|身份证|家庭住址|私人邮箱|客户内部|非公开数据)\s*[:：]",
    ]
    return any(re.search(pattern, value) for pattern in patterns)


def build_visible_payload(text: str, task: str) -> dict[str, str]:
    return {
        "task": str(task or "事件分析建议").strip(),
        "public_text": str(text or "").strip(),
        "notice": "仅发送上方公开文本；不会发送Cookie、登录凭据或本地客户档案。",
    }


def _strict_json(content: str) -> dict[str, str]:
    raw = str(content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("AI响应不是JSON对象。")
    unknown = set(parsed) - ALLOWED_OUTPUT_FIELDS
    if unknown:
        raise ValueError(f"AI响应包含未允许字段：{'、'.join(sorted(unknown))}")
    result: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(value, str):
            raise ValueError(f"AI字段 {key} 必须是字符串。")
        cleaned = value.strip()
        if cleaned:
            result[key] = cleaned
    if result.get("category") and result["category"] not in ALLOWED_CATEGORIES:
        raise ValueError("AI类别不在允许范围。")
    if not result:
        raise ValueError("AI响应没有可用建议。")
    return result


def request_deepseek_suggestions(
    text: str,
    task: str,
    *,
    user_confirmed: bool,
    api_key: Optional[str] = None,
    requester=requests,
    endpoint: Optional[str] = None,
    timeout: Optional[float] = None,
) -> AIResult:
    """Request optional suggestions; any failure safely returns to deterministic workflow."""
    if not user_confirmed:
        raise PermissionError("调用前必须确认将要发送的公开文本。")
    public_text = str(text or "").strip()
    if not public_text:
        return AIResult(False, note="没有可发送的公开文本，继续使用普通规则。")
    if contains_personal_data(public_text):
        return AIResult(False, note="检测到可能的个人或非公开信息，已阻止发送。")
    # An explicitly supplied blank value means "run without AI"; it must not
    # silently resurrect a key from the project environment.
    secret = (api_key if api_key is not None else deepseek_api_key()).strip()
    if not secret:
        return AIResult(False, note="AI辅助未配置，继续使用确定性规则和人工填写。")

    system_prompt = (
        "你是公开信息整理助手。只根据用户提供的公开文本给出草稿建议，不得添加未经文本支持的事实，"
        "不得声称官方结论或准确预测。严格返回JSON对象，只能包含summary、impact、action、"
        "category、executive_summary中的相关字段。category如出现必须属于："
        + "、".join(ALLOWED_CATEGORIES)
        + "。不要返回来源URL、发布日期或确认指令。"
    )
    settings = load_settings()
    if api_key:
        settings = type(settings)(
            api_key=secret, model=settings.model, base_url=settings.base_url, timeout=settings.timeout,
            max_retries=settings.max_retries, agent_model=settings.agent_model,
            extraction_model=settings.extraction_model,
        )
    actual_model, _ = model_for_call(settings, task="extraction", requester=requester)
    if not actual_model:
        return AIResult(False, note="无法验证当前模型，已安全回退到确定性规则和人工填写。")
    payload = {
        "model": actual_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"任务：{task}\n\n公开文本：\n{public_text}"},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "stream": False,
    }
    try:
        response = requester.post(
            endpoint or f"{settings.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout or settings.timeout,
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        suggestions = _strict_json(content)
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return AIResult(False, note="AI响应不可用，已安全回退到确定性规则和人工填写。")
    marked = {
        key: value if key == "category" else f"{AI_DRAFT_LABEL}\n{value}"
        for key, value in suggestions.items()
    }
    return AIResult(True, marked, AI_DRAFT_LABEL, used_ai=True)
