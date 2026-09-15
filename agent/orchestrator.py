from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
import json
import re
import time
from typing import Mapping

import requests
from pydantic import ValidationError

from deepseek_service import load_settings, model_for_call

from .approval import decide_approval
from .conversation_store import append_message, load_messages
from .prompts import build_system_prompt
from .result_renderer import user_visible_result
from .tool_executor import ToolExecutor


@dataclass
class AgentTurn:
    answer: str
    plan: list[str] = field(default_factory=list)
    cards: list[dict[str, object]] = field(default_factory=list)
    protocol_messages: list[dict[str, object]] = field(default_factory=list)
    used_ai: bool = False
    stopped_reason: str = "completed"


class AgentOrchestrator:
    def __init__(
        self,
        executor: ToolExecutor,
        *,
        requester=None,
        max_tool_rounds: int = 4,
        time_budget_seconds: float = 90.0,
        max_tool_calls: int = 8,
    ) -> None:
        self.executor = executor
        self.registry = executor.registry
        self.requester = requester or executor.context.requester or requests
        self.max_tool_rounds = max(1, min(int(max_tool_rounds), 4))
        self.time_budget_seconds = max(5.0, min(float(time_budget_seconds), 90.0))
        self.max_tool_calls = max(1, min(int(max_tool_calls), 8))

    def _status(self) -> dict[str, object]:
        payload = self.executor.execute("get_system_status", {})
        return dict(payload.data)

    def _system_prompt(self) -> str:
        summaries = [f"{spec.tool_name}: {spec.description}" for spec in self.registry.specs()]
        return build_system_prompt(self.executor.context.workspace, self._status(), summaries)

    def _persist(self, role: str, content: str, metadata=None, tool_name: str = "") -> None:
        context = self.executor.context
        if context.conversation_id:
            append_message(context.conversation_id, context.workspace_id, role, content, context.db_path,
                           metadata=metadata, tool_name=tool_name)

    def run(self, user_input: str) -> AgentTurn:
        prompt = str(user_input or "").strip()
        if not prompt:
            return AgentTurn("请输入要完成的公开数据任务。", stopped_reason="empty_input")
        self._persist("user", prompt)
        settings = load_settings(self.executor.context.env_path)
        if not settings.configured or not bool(self.executor.context.workspace.get("ai_enabled")):
            turn = self._run_local(prompt)
        else:
            try:
                turn = self._run_deepseek(prompt, settings)
            except Exception as exc:
                self.executor.context.emit("AI规划暂不可用，正在切换本地任务路由")
                turn = self._run_local(prompt)
                turn.answer = f"AI规划暂不可用，已安全切换到本地模式。\n\n{turn.answer}"
                turn.stopped_reason = f"ai_fallback:{type(exc).__name__}"
        self._persist("assistant", turn.answer, {
            "plan": turn.plan, "cards": turn.cards,
            # Hidden protocol is persisted only for API continuity and tests; the UI never renders it.
            "protocol_messages": turn.protocol_messages,
            "used_ai": turn.used_ai, "stopped_reason": turn.stopped_reason,
        })
        return turn

    def _execute_card(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            result = self.executor.execute(name, arguments)
        except (KeyError, ValidationError, ValueError) as exc:
            return {"card_type": "error", "tool_name": name, "status": "error", "message": str(exc), "data": {}, "artifacts": []}
        return user_visible_result(name, result.model_dump())

    def _run_local(self, prompt: str) -> AgentTurn:
        today = date.today()
        start = (today - timedelta(days=6)).isoformat()
        end = today.isoformat()
        cards: list[dict[str, object]] = []
        plan: list[str] = []
        lower = prompt.lower()
        url_match = re.search(r"https?://[^\s，。]+", prompt)

        if "系统状态" in prompt or "当前状态" in prompt:
            plan = ["检查API、来源、文档、索引和报告状态"]
            cards.append(self._execute_card("get_system_status", {}))
        elif ("查看" in prompt or "列出" in prompt) and "来源" in prompt:
            plan = ["读取当前工作空间公开来源状态"]
            cards.append(self._execute_card("list_sources", {}))
        elif "配置" in prompt and ("api" in lower or "deepseek" in lower):
            plan = ["打开本机AI配置表单"]
            cards.append(self._execute_card("configure_ai", {"requested_action": "show_form"}))
        elif ("添加" in prompt or "加进" in prompt) and url_match:
            plan = ["安全检查单一栏目URL", "读取一张列表页并测试最多一篇正文", "生成适配器建议", "等待确认后添加来源"]
            cards.append(self._execute_card("analyze_source_url", {"url": url_match.group(0), "source_name": ""}))
        elif "初始化" in prompt and "来源" in prompt:
            plan = ["展示推荐来源", "确认合规边界", "写入当前工作空间"]
            cards.append(self._execute_card("initialize_recommended_sources", {}))
        elif "更新" in prompt or "采集" in prompt or "抓取" in prompt:
            plan = ["检查已启用公开来源", "增量采集并去重", "结构化与规则评分", "更新本地知识库"]
            cards.append(self._execute_card("run_crawl", {"source_ids": [], "max_articles": 3, "start_date": start if "7天" in prompt or "一周" in prompt else "", "end_date": end}))
        elif "待AI处理" in prompt and ("补跑" in prompt or "重新处理" in prompt or "处理一遍" in prompt):
            plan = ["读取已保存的待AI文档", "只补跑结构化抽取和评分", "不重新抓取网页"]
            cards.append(self._execute_card("retry_failed_sources", {"source_ids": [], "include_pending_ai": True}))
        elif "重建" in prompt and ("索引" in prompt or "知识库" in prompt):
            plan = ["核对现有文档", "重建本地向量索引"]
            cards.append(self._execute_card("rebuild_index", {}))
        elif "审核" in prompt or "待处理" in prompt:
            plan = ["列出需要人工处理的异常", "等待用户选择确认或退回"]
            cards.append(self._execute_card("list_pending_reviews", {}))
        elif "生成" in prompt and ("报告" in prompt or "周报" in prompt):
            client = "货代公司" if "货代" in prompt else "通用报告"
            plan = ["筛选本期可进入报告的数据", "生成可追溯报告草稿", "提供DOCX、HTML和Excel下载"]
            cards.append(self._execute_card("generate_report", {
                "report_title": "青岛港公开信息情报周报", "client_name": client,
                "start_date": start, "end_date": end, "categories": [],
                "include_risks": True, "include_opportunities": True, "include_sources": True,
                "human_verified_only": True, "formal_publish": False,
                "report_mode": "客户交付版" if "客户" in prompt or "正式" in prompt else "内部研究版",
            }))
        elif "商机" in prompt or "采购" in prompt or "招标" in prompt:
            plan = ["检索本地公开证据", "核对政策、招标和商业机会"]
            cards.append(self._execute_card("ask_knowledge_base", {"query": prompt, "mode": "商机分析", "limit": 8}))
        elif "风险" in prompt or "航行警告" in prompt or "出处" in prompt or "来源" in prompt and "为什么" not in prompt:
            plan = ["检索本地公开证据", "整理风险结论和来源引用"]
            cards.append(self._execute_card("ask_knowledge_base", {"query": prompt, "mode": "风险分析", "limit": 8}))
        elif "失败" in prompt or "错误" in prompt:
            plan = ["读取最近采集结果", "解释失败原因和可重试性"]
            latest = self._execute_card("get_crawl_result", {})
            cards.append(latest)
            error = str((latest.get("data") or {}).get("crawl_run", {}).get("error_summary", "未找到具体错误"))
            cards.append(self._execute_card("explain_error", {"error_text": error, "source_name": ""}))
        elif "导出" in prompt:
            plan = ["导出当前工作空间事件数据"]
            cards.append(self._execute_card("export_data", {"table": "events"}))
        else:
            plan = ["检索当前工作空间公开知识库", "仅根据证据回答"]
            cards.append(self._execute_card("ask_knowledge_base", {"query": prompt, "mode": "快速回答", "limit": 8}))

        messages = [str(card.get("message") or "") for card in cards]
        if any(card.get("status") == "pending_confirmation" for card in cards):
            messages.append("请在下方确认卡片中核对操作范围；确认后才会执行。")
        return AgentTurn("\n\n".join(item for item in messages if item), plan, cards, used_ai=False)

    @staticmethod
    def _response_message(response) -> dict[str, object]:
        payload = response.json()
        message = payload["choices"][0]["message"]
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "reasoning_content": message.get("reasoning_content") or "",
            "tool_calls": message.get("tool_calls") or [],
        }

    def _streaming_response_message(self, response) -> dict[str, object]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, object]] = {}
        for raw_line in response.iter_lines(decode_unicode=True):
            line = str(raw_line or "").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
            if delta.get("content"):
                piece = str(delta["content"])
                content_parts.append(piece)
                self.executor.context.emit("AI正在生成最终回答")
            if delta.get("reasoning_content"):
                reasoning_parts.append(str(delta["reasoning_content"]))
            for item in delta.get("tool_calls") or []:
                index = int(item.get("index") or 0)
                current = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if item.get("id"):
                    current["id"] = item["id"]
                function = item.get("function") or {}
                current_function = current["function"]
                current_function["name"] = str(current_function.get("name") or "") + str(function.get("name") or "")
                current_function["arguments"] = str(current_function.get("arguments") or "") + str(function.get("arguments") or "")
        return {
            "role": "assistant", "content": "".join(content_parts),
            "reasoning_content": "".join(reasoning_parts),
            "tool_calls": [calls[index] for index in sorted(calls)],
        }

    def _request_message(self, body: dict[str, object], settings) -> dict[str, object]:
        # The real requests transport streams SSE tokens and tool-call argument
        # fragments. Test/mocked transports keep the deterministic JSON path.
        use_stream = self.requester is requests
        request_body = {**body, "stream": use_stream}
        response = self.requester.post(
            f"{settings.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
            json=request_body, timeout=settings.timeout, **({"stream": True} if use_stream else {}),
        )
        response.raise_for_status()
        return self._streaming_response_message(response) if use_stream else self._response_message(response)

    def _run_deepseek(self, prompt: str, settings) -> AgentTurn:
        run_started = time.monotonic()
        bounded_settings = replace(
            settings,
            timeout=min(float(settings.timeout), 30.0),
            max_retries=min(int(settings.max_retries), 1),
        )
        actual_agent_model, model_note = model_for_call(
            bounded_settings,
            task="agent",
            requester=self.requester,
        )
        if not actual_agent_model:
            raise RuntimeError(model_note or "无法验证Agent模型")
        if model_note and (settings.agent_model != actual_agent_model or settings.agent_model.startswith("auto-")):
            self.executor.context.emit(f"Agent实际使用模型：{actual_agent_model}")
        history = []
        context = self.executor.context
        if context.conversation_id:
            for item in load_messages(context.conversation_id, context.workspace_id, context.db_path, limit=30):
                metadata = dict(item.get("metadata") or {})
                saved_protocol = metadata.get("protocol_messages")
                if item["role"] == "assistant" and isinstance(saved_protocol, list) and saved_protocol:
                    # A thinking-mode turn with tools must retain assistant
                    # reasoning_content + tool messages for later API requests.
                    history.extend(dict(message) for message in saved_protocol if isinstance(message, dict))
                elif item["role"] in {"user", "assistant"} and item["content"]:
                    history.append({"role": item["role"], "content": item["content"]})
        # The current user message was persisted before history was loaded.
        if not history or history[-1] != {"role": "user", "content": prompt}:
            history.append({"role": "user", "content": prompt})
        messages: list[dict[str, object]] = [{"role": "system", "content": self._system_prompt()}, *history]
        protocol: list[dict[str, object]] = []
        cards: list[dict[str, object]] = []
        repetitions: dict[str, int] = {}
        tool_call_count = 0
        previous_result_signature = ""
        unchanged_result_count = 0
        plan: list[str] = []

        for round_index in range(1, self.max_tool_rounds + 1):
            if (
                self.executor.context.cancel_check
                and self.executor.context.cancel_check()
            ):
                return AgentTurn(
                    "任务已按用户要求取消。",
                    plan,
                    cards,
                    protocol,
                    used_ai=True,
                    stopped_reason="cancelled",
                )
            elapsed = time.monotonic() - run_started
            if elapsed >= self.time_budget_seconds:
                return AgentTurn(
                    "AI工作台已达到90秒总时间预算并安全停止，已完成的工具结果仍然保留。",
                    plan,
                    cards,
                    protocol,
                    used_ai=True,
                    stopped_reason="time_budget",
                )
            self.executor.context.emit(f"AI正在规划第 {round_index} 轮")
            body = {
                "model": actual_agent_model,
                "messages": messages,
                "tools": self.registry.openai_tools(),
                "tool_choice": "auto",
                "reasoning_effort": "high",
                "thinking": {"type": "enabled"},
            }
            assistant = None
            last_error = None
            request_settings = replace(
                bounded_settings,
                timeout=max(
                    5.0,
                    min(
                        float(bounded_settings.timeout),
                        self.time_budget_seconds - (time.monotonic() - run_started),
                    ),
                ),
            )
            for attempt in range(request_settings.max_retries + 1):
                try:
                    assistant = self._request_message(body, request_settings)
                    break
                except (requests.RequestException, requests.Timeout) as exc:
                    last_error = exc
                    if attempt < request_settings.max_retries:
                        time.sleep(min(2 ** attempt, 4))
            if assistant is None:
                raise last_error or RuntimeError("DeepSeek请求未完成")
            # Keep reasoning_content in the assistant protocol message exactly as returned.
            messages.append(assistant)
            protocol.append(dict(assistant))
            content = str(assistant.get("content") or "").strip()
            if content and not plan and ("1." in content or "第一" in content):
                plan = [line.strip() for line in content.splitlines() if line.strip()][:8]
            tool_calls = list(assistant.get("tool_calls") or [])
            if not tool_calls:
                return AgentTurn(content or "任务已完成。", plan, cards, protocol, used_ai=True)
            for call in tool_calls:
                tool_call_count += 1
                if tool_call_count > self.max_tool_calls:
                    return AgentTurn(
                        "工具调用已达到8次上限并安全停止，请缩小任务范围后继续。",
                        plan,
                        cards,
                        protocol,
                        used_ai=True,
                        stopped_reason="max_tool_calls",
                    )
                function = dict(call.get("function") or {})
                name = str(function.get("name") or "")
                raw_arguments = str(function.get("arguments") or "{}")
                try:
                    arguments = json.loads(raw_arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是JSON对象")
                except (json.JSONDecodeError, ValueError) as exc:
                    result_payload = {"status": "error", "message": f"工具参数无效：{exc}", "data": {}}
                else:
                    fingerprint = name + ":" + json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                    repetitions[fingerprint] = repetitions.get(fingerprint, 0) + 1
                    if repetitions[fingerprint] >= 2:
                        answer = "检测到相同工具和参数重复2次，已安全停止。请缩小任务范围后重试。"
                        return AgentTurn(answer, plan, cards, protocol, used_ai=True, stopped_reason="loop_detected")
                    try:
                        result = self.executor.execute(name, arguments)
                        result_payload = self.executor.model_safe_payload(result)
                        cards.append(user_visible_result(name, result.model_dump()))
                    except (KeyError, ValidationError, ValueError) as exc:
                        result_payload = {"status": "error", "message": str(exc), "data": {}}
                        cards.append({"card_type": "error", "tool_name": name, "status": "error", "message": str(exc), "data": {}, "artifacts": []})
                result_signature = json.dumps(
                    {
                        "status": result_payload.get("status"),
                        "message": result_payload.get("message"),
                        "data": result_payload.get("data"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if result_signature == previous_result_signature:
                    unchanged_result_count += 1
                else:
                    unchanged_result_count = 0
                    previous_result_signature = result_signature
                if unchanged_result_count >= 1:
                    return AgentTurn(
                        "连续工具调用没有产生新结果，已安全停止。",
                        plan,
                        cards,
                        protocol,
                        used_ai=True,
                        stopped_reason="no_new_result",
                    )
                tool_message = {
                    "role": "tool", "tool_call_id": str(call.get("id") or ""),
                    "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                }
                messages.append(tool_message)
                protocol.append(dict(tool_message))
        return AgentTurn(
            f"工具调用达到{self.max_tool_rounds}轮上限，已停止。请缩小任务范围后继续。",
            plan,
            cards,
            protocol,
            used_ai=True,
            stopped_reason="max_rounds",
        )

    def resolve_approval(self, approval_id: str, approved: bool) -> AgentTurn:
        context = self.executor.context
        item = decide_approval(approval_id, context.workspace_id, approved, context.db_path)
        if not approved:
            turn = AgentTurn("操作已取消，未修改数据。", cards=[{
                "card_type": "result", "tool_name": item["tool_name"], "status": "cancelled",
                "message": "操作已取消。", "data": {}, "artifacts": [],
            }], stopped_reason="cancelled")
        else:
            result = self.executor.execute(str(item["tool_name"]), dict(item["arguments"]), confirmed=True)
            card = user_visible_result(str(item["tool_name"]), result.model_dump())
            turn = AgentTurn(result.message, cards=[card], stopped_reason="approved_executed")
        self._persist("assistant", turn.answer, {"cards": turn.cards, "approval_id": approval_id})
        return turn
