from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import os
from pathlib import Path
from typing import Mapping

import streamlit as st

from agent import AgentContext, AgentOrchestrator, ToolExecutor, build_default_registry
from agent.approval import list_pending_approvals
from agent.conversation_store import get_or_create_conversation, load_messages
from deepseek_service import (
    MODEL_ALIASES, DeepSeekSettings, clear_local_api_key, load_model_cache, load_settings, mask_api_key,
    save_settings_to_env, settings_env_path, test_deepseek_connection,
)
from file_intake import UnsupportedFileType, parse_uploaded_public_file
from platform_db import connect, initialize_recommended_sources, list_sources, table_counts
from workspace_store import load_reports, update_workspace, workspace_paths


RECOMMENDED_COMMANDS = [
    "更新最近7天的青岛港公开信息",
    "检查今天有没有新的航行警告",
    "总结本周最重要的风险",
    "查找最近的港口数字化采购机会",
    "对比本周和上周的风险变化",
    "生成一份面向货代公司的周报",
    "为什么山东港口集团采集失败？",
    "帮我添加这个官方公告栏目作为新来源：https://example.com/news",
]


def _context(workspace: Mapping[str, object], paths, db_path, data_root: Path, conversation_id: str, progress=None) -> AgentContext:
    return AgentContext(
        workspace=workspace, db_path=db_path, data_root=Path(data_root), paths=paths,
        conversation_id=conversation_id, env_path=settings_env_path(), progress_callback=progress,
    )


def _temporary_settings(api_key: str, base_url: str, agent_model: str, extraction_model: str,
                        timeout: float, retries: int) -> DeepSeekSettings:
    return DeepSeekSettings(
        api_key=api_key, model=extraction_model, base_url=base_url.rstrip("/"), timeout=timeout,
        max_retries=retries, agent_model=agent_model, extraction_model=extraction_model,
    )


def render_ai_configuration(workspace: Mapping[str, object], db_path, *, expanded: bool = False, key_prefix: str = "agent") -> None:
    env_path = settings_env_path()
    settings = load_settings(env_path)
    if st.session_state.pop(f"{key_prefix}_clear_secret_widget", False):
        st.session_state.pop(f"{key_prefix}_api_key", None)
    diagnostic = dict(st.session_state.get(f"{key_prefix}_deepseek_diagnostic") or {})
    connection_verified = bool(diagnostic.get("ok"))
    if not settings.api_key:
        state_label = "未配置"
    elif connection_verified:
        state_label = "已连接"
    elif diagnostic.get("error_type") == "insufficient_balance":
        state_label = "余额不足"
    elif diagnostic.get("error_type") == "model_unavailable":
        state_label = "模型不可用"
    elif diagnostic.get("error_type") in {"timeout", "network_error", "tls_error", "proxy_error"}:
        state_label = "网络异常"
    else:
        state_label = "密钥存在，尚未验证" if settings.api_key else "未配置"
    with st.container(border=True):
        columns = st.columns(3)
        columns[0].metric("DeepSeek", state_label)
        columns[1].metric("AI辅助", "已启用" if workspace.get("ai_enabled") else "已关闭")
        columns[2].metric("模型", diagnostic.get("model_name") or settings.agent_model)
        st.caption(
            f"Key来源：{settings.api_key_source}｜模型来源：{settings.model_source}｜"
            f"Base URL来源：{settings.base_url_source}｜生效配置：{settings.config_file or '.env'}"
        )
        if settings.migration_warning:
            st.warning(settings.migration_warning)
    with st.expander("配置DeepSeek API（配置或更换Key）", expanded=expanded or not settings.configured):
        st.caption("密钥只保存在本机项目根目录 .env，不写入SQLite、对话记录、报告或日志。")
        st.write(f"当前密钥：{mask_api_key(settings.api_key)}")
        cached_models = list(load_model_cache().get("models") or [])
        model_options = list(dict.fromkeys(["auto-agent", "auto-extraction", *cached_models, settings.agent_model, settings.extraction_model]))
        with st.form(f"{key_prefix}_ai_config_form"):
            api_key = st.text_input(
                "API Key", type="password", value="", key=f"{key_prefix}_api_key",
                placeholder="粘贴新密钥；留空则保留当前密钥",
            )
            base_url = st.text_input("Base URL", settings.base_url or "https://api.deepseek.com")
            columns = st.columns(2)
            agent_selected = columns[0].selectbox(
                "Agent规划模型", model_options,
                index=model_options.index(settings.agent_model) if settings.agent_model in model_options else 0,
            )
            extraction_selected = columns[1].selectbox(
                "批量抽取模型", model_options,
                index=model_options.index(settings.extraction_model) if settings.extraction_model in model_options else 1,
            )
            manual = st.columns(2)
            manual_agent = manual[0].text_input("手工Agent模型名（可选）", placeholder="使用 /models 返回的 model_id")
            manual_extraction = manual[1].text_input("手工抽取模型名（可选）", placeholder="使用 /models 返回的 model_id")
            agent_model = manual_agent.strip() or agent_selected
            extraction_model = manual_extraction.strip() or extraction_selected
            limits = st.columns(2)
            timeout = limits[0].number_input("超时时间（秒）", min_value=5, max_value=300, value=int(settings.timeout), step=5)
            retries = limits[1].number_input("最大重试次数", min_value=0, max_value=5, value=int(settings.max_retries))
            ai_enabled = st.checkbox("在当前工作空间启用AI辅助", bool(workspace.get("ai_enabled")))
            actions = st.columns(3)
            test_clicked = actions[0].form_submit_button("测试连接", width="stretch")
            balance_clicked = actions[1].form_submit_button("查询余额", width="stretch")
            save_clicked = actions[2].form_submit_button("保存到本机", type="primary", width="stretch")
        candidate_key = api_key.strip() or settings.api_key
        candidate = _temporary_settings(candidate_key, base_url, agent_model, extraction_model, float(timeout), int(retries))
        if test_clicked:
            result = test_deepseek_connection(candidate, include_balance=False)
            st.session_state[f"{key_prefix}_deepseek_diagnostic"] = asdict(result)
            if result.ok:
                st.success(f"连接成功｜实际模型：{result.model_name}｜总耗时：{result.elapsed_ms} ms")
                st.write("可用模型：" + "、".join(result.available_models))
                if result.balance_available is not None:
                    st.write(f"余额状态：{'可用' if result.balance_available else '不足'}｜{result.balance_summary or '未返回币种明细'}")
                st.caption(
                    f"/models HTTP：{result.models_http_status or '无'}｜/user/balance HTTP：{result.balance_http_status or '无'}｜"
                    f"生成HTTP：{result.generation_http_status or result.http_status or '无'}｜finish_reason：{result.finish_reason or '未返回'}｜"
                    f"content长度：{result.content_length}｜reasoning_content长度：{result.reasoning_content_length}"
                )
                st.text(f"POST API端点（不能通过浏览器直接打开测试）：{result.endpoint or candidate.base_url}")
            else:
                st.error(result.status)
                st.caption(
                    f"失败阶段：{result.failed_stage or '未知'}｜HTTP：{result.http_status or '无'}｜"
                    f"错误代码：{result.error_code or result.error_type or '无'}"
                )
                st.text(f"POST API端点（不能通过浏览器直接打开测试）：{result.endpoint or candidate.base_url}")
                details = []
                if result.content_type:
                    details.append(f"Content-Type：{result.content_type}")
                if result.generation_http_status:
                    details.append(f"生成HTTP：{result.generation_http_status}")
                if result.finish_reason:
                    details.append(f"finish_reason：{result.finish_reason}")
                details.append(f"content长度：{result.content_length}")
                details.append(f"reasoning_content长度：{result.reasoning_content_length}")
                if result.request_id:
                    details.append(f"request-id：{result.request_id}")
                st.caption("｜".join(details))
                if result.response_preview:
                    st.code(result.response_preview, language=None)
        if balance_clicked:
            result = test_deepseek_connection(
                candidate,
                include_balance=True,
                generation_test=False,
            )
            st.session_state[f"{key_prefix}_deepseek_diagnostic"] = asdict(result)
            if result.ok:
                st.success(
                    f"余额查询完成｜{'可用' if result.balance_available else '不可用'}｜"
                    f"{result.balance_summary or '未返回币种明细'}"
                )
            else:
                st.error(
                    f"余额查询失败：{result.safe_error_message or result.status}"
                )
        if save_clicked:
            saved = save_settings_to_env(
                api_key=candidate_key, base_url=base_url, agent_model=agent_model,
                extraction_model=extraction_model, timeout=float(timeout), max_retries=int(retries), env_path=env_path,
            )
            update_workspace(str(workspace["workspace_id"]), {"ai_enabled": ai_enabled}, db_path)
            st.session_state[f"{key_prefix}_clear_secret_widget"] = True
            if saved.api_key != candidate_key or saved.base_url != base_url.rstrip("/"):
                st.error("配置文件已写入，但重新读取后的生效值不一致；AI保持关闭，请检查配置来源。")
            else:
                st.success(f"已原子保存并立即生效，当前密钥：{mask_api_key(saved.api_key)}")
            st.rerun()
        if st.button("清除本机密钥", key=f"{key_prefix}_clear_key"):
            clear_local_api_key(env_path)
            update_workspace(str(workspace["workspace_id"]), {"ai_enabled": False}, db_path)
            st.session_state[f"{key_prefix}_clear_secret_widget"] = True
            st.success("本机密钥已清除，系统已切换到本地规则模式。")
            st.rerun()
    actions = st.columns(2)
    if actions[0].button("查看诊断", key=f"{key_prefix}_show_diagnostic"):
        st.session_state[f"{key_prefix}_diagnostic_visible"] = not st.session_state.get(f"{key_prefix}_diagnostic_visible", False)
    if actions[1].button("关闭AI", key=f"{key_prefix}_disable_ai", disabled=not bool(workspace.get("ai_enabled"))):
        update_workspace(str(workspace["workspace_id"]), {"ai_enabled": False}, db_path)
        st.rerun()
    if st.session_state.get(f"{key_prefix}_diagnostic_visible"):
        if diagnostic:
            st.json({key: value for key, value in diagnostic.items() if key not in {"balance_summary"} or value})
        else:
            st.info("尚未执行连接诊断。测试将依次检查模型列表、余额和最短生成请求。")


def _safe_artifact_path(paths, artifact: Mapping[str, object]) -> Path | None:
    filename = Path(str(artifact.get("filename") or "")).name
    if not filename:
        return None
    for directory in (paths.reports, paths.root / "exports"):
        candidate = directory / filename
        try:
            candidate.resolve().relative_to(directory.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _render_card(card: Mapping[str, object], paths) -> None:
    status = str(card.get("status") or "success")
    with st.container(border=True):
        if status == "error":
            st.error(str(card.get("message") or "任务失败"))
        elif status == "pending_confirmation":
            st.warning(str(card.get("message") or "等待确认"))
        else:
            st.success(str(card.get("message") or "任务完成"))
        data = dict(card.get("data") or {})
        if card.get("card_type") == "crawl":
            fields = [
                ("检查来源", data.get("source_count", 0)), ("新增文档", data.get("new_document_count", 0)),
                ("更新文档", data.get("updated_document_count", 0)), ("重复跳过", data.get("skipped_count", 0)),
                ("失败", data.get("failed_count", 0)), ("新增事件", data.get("new_event_count", 0)),
            ]
            columns = st.columns(3)
            for index, (label, value) in enumerate(fields):
                columns[index % 3].metric(label, value)
        elif data:
            with st.expander("查看结果详情"):
                st.json(data)
        artifacts = list(card.get("artifacts") or [])
        if artifacts:
            columns = st.columns(min(3, len(artifacts)))
            for index, artifact in enumerate(artifacts):
                target = _safe_artifact_path(paths, artifact)
                if target:
                    columns[index % len(columns)].download_button(
                        f"下载 {artifact.get('kind', '文件')}", target.read_bytes(), target.name,
                        key=f"download_{target.name}_{index}",
                    )


def _render_approvals(orchestrator: AgentOrchestrator, workspace_id: str, db_path) -> None:
    approvals = list_pending_approvals(workspace_id, db_path)
    if not approvals:
        return
    st.subheader("需要你的确认")
    for item in approvals:
        with st.container(border=True):
            summary = dict(item.get("summary") or {})
            st.markdown(f"**{item['tool_name']}**｜{summary.get('operation', '')}")
            st.caption(
                f"数据源：{summary.get('source_name') or summary.get('source_url') or summary.get('source_ids') or '当前已启用来源'}｜"
                f"最大文章数：{summary.get('max_articles') or '按工具上限'}｜"
                f"调用DeepSeek：{'是' if summary.get('incurs_api_cost') else '否'}"
            )
            st.write(summary.get("impact", ""))
            buttons = st.columns(2)
            if buttons[0].button("确认执行", type="primary", key=f"approve_{item['approval_id']}"):
                orchestrator.resolve_approval(str(item["approval_id"]), True)
                st.rerun()
            if buttons[1].button("取消", key=f"cancel_{item['approval_id']}"):
                orchestrator.resolve_approval(str(item["approval_id"]), False)
                st.rerun()


def render_ai_workbench(workspace: Mapping[str, object], paths, db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("PortScope AI工作台")
    st.caption("用自然语言完成公开数据更新、检索、风险分析、审核和报告；安全与人工确认门禁始终有效。")
    settings = load_settings(settings_env_path())
    sources = list_sources(workspace_id, db_path)
    render_ai_configuration(workspace, db_path, expanded=not settings.configured, key_prefix="workbench")
    first_steps = st.columns(3)
    first_steps[0].metric("1. DeepSeek API", "已配置" if settings.configured else "可稍后配置")
    first_steps[1].metric("2. 推荐来源", sum(bool(x["enabled"] and x["crawl_allowed"]) for x in sources))
    first_steps[2].metric("3. 第一条指令", "可以开始")
    if not sources:
        with st.container(border=True):
            st.write("尚未初始化公开来源。可以先安装推荐模板；许可不明确的来源不会进入商业报告。")
            confirmed = st.checkbox("我确认仅进行低频公开信息核对，并会复核网站条款。", key="agent_source_init_confirm")
            if st.button("一键初始化推荐公开来源", disabled=not confirmed, type="primary"):
                result = initialize_recommended_sources(workspace_id, db_path)
                st.success(f"已添加 {result['added']} 个，当前可自动采集 {result['enabled']} 个。")
                st.rerun()

    conversation_id = get_or_create_conversation(workspace_id, db_path)
    progress_messages: list[str] = []
    progress_placeholder = st.empty()

    def progress(message: str) -> None:
        progress_messages.append(message)
        progress_placeholder.info(" → ".join(progress_messages[-5:]))

    context = _context(workspace, paths, db_path, data_root, conversation_id, progress)
    orchestrator = AgentOrchestrator(ToolExecutor(context, build_default_registry()))
    messages = load_messages(conversation_id, workspace_id, db_path)
    for message in messages:
        if message["role"] not in {"user", "assistant"}:
            continue
        with st.chat_message(message["role"]):
            st.markdown(str(message["content"]))
            for card in list((message.get("metadata") or {}).get("cards") or []):
                _render_card(card, paths)

    with st.expander("添加公开URL或本地文件", expanded=False):
        attached_url = st.text_input("公开网页或栏目URL", placeholder="https://...", key="agent_attached_url")
        uploaded = st.file_uploader("本地公开资料", type=["txt", "html", "htm", "csv", "docx"], key="agent_public_file")
        st.caption("单个URL或文件只作为当前指令的公开资料；不批量扫描，不解析PDF或图片。")

    st.caption("推荐指令")
    command_columns = st.columns(2)
    for index, command in enumerate(RECOMMENDED_COMMANDS):
        if command_columns[index % 2].button(command, key=f"agent_command_{index}", width="stretch"):
            st.session_state["pending_agent_prompt"] = command
            st.rerun()

    pending = st.session_state.pop("pending_agent_prompt", "")
    prompt = st.chat_input("告诉PortScope你要完成什么，例如：更新最近7天数据并总结风险")
    prompt = prompt or pending
    if prompt:
        additions = []
        if attached_url:
            additions.append(f"用户提供的公开URL：{attached_url}")
        if uploaded is not None:
            try:
                parsed = parse_uploaded_public_file(uploaded.name, uploaded.getvalue())
                additions.append(f"用户上传的公开资料《{parsed.title or uploaded.name}》：\n{parsed.text[:12000]}")
            except (UnsupportedFileType, ValueError) as exc:
                st.error(str(exc))
                additions = []
        full_prompt = prompt + ("\n\n" + "\n".join(additions) if additions else "")
        with st.chat_message("assistant"):
            with st.spinner("正在理解任务并选择受控业务工具……"):
                turn = orchestrator.run(full_prompt)
            if turn.plan:
                st.markdown("**执行计划**")
                for index, step in enumerate(turn.plan, 1):
                    st.write(f"{index}. {step}")
            st.markdown(turn.answer)
            for card in turn.cards:
                _render_card(card, paths)
        st.rerun()

    _render_approvals(orchestrator, workspace_id, db_path)

    with st.sidebar:
        st.divider()
        st.subheader("当前任务状态")
        counts = table_counts(workspace_id, db_path)
        st.metric("公开来源", len(sources))
        st.metric("知识库文档", counts["documents"])
        st.metric("待审核事件", _pending_count(workspace_id, db_path))
        with connect(db_path) as connection:
            run = connection.execute("SELECT status,finished_at FROM crawl_runs WHERE workspace_id=? ORDER BY started_at DESC LIMIT 1", (workspace_id,)).fetchone()
        st.caption(f"最近采集：{run['status']}｜{run['finished_at'] or '进行中'}" if run else "最近采集：尚无记录")


def _pending_count(workspace_id: str, db_path) -> int:
    with connect(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM events WHERE workspace_id=? AND human_verified=0", (workspace_id,)).fetchone()[0])


def render_tasks_and_reports(workspace: Mapping[str, object], paths, db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("任务与报告")
    approvals = list_pending_approvals(workspace_id, db_path)
    st.metric("等待确认的操作", len(approvals))
    reports = load_reports(workspace_id, db_path)
    if not reports:
        st.info("还没有报告。可以回到AI工作台说：生成一份面向货代公司的周报。")
    for report in reports:
        with st.container(border=True):
            st.markdown(f"**{report['report_title']}｜v{report['version']}**")
            st.caption(f"{report['start_date']} 至 {report['end_date']}｜生成：{report['generated_at']}")
            artifacts = [
                {"kind": "DOCX", "filename": report.get("docx_file", "")},
                {"kind": "HTML", "filename": report.get("html_file", "")},
                {"kind": "Excel", "filename": report.get("xlsx_file", "")},
            ]
            _render_card({"status": "success", "message": "报告文件", "artifacts": artifacts}, paths)


def render_agent_settings(workspace: Mapping[str, object], paths, db_path, data_root: Path) -> None:
    st.header("设置")
    render_ai_configuration(workspace, db_path, expanded=True, key_prefix="settings")
    st.subheader("推荐公开来源")
    sources = list_sources(str(workspace["workspace_id"]), db_path)
    if sources:
        st.dataframe([{
            "来源名称": item["source_name"], "状态": "已启用" if item["enabled"] and item["crawl_allowed"] else "已停用",
            "最后成功": item["last_success_at"] or "尚未更新",
        } for item in sources], hide_index=True, width="stretch")
    else:
        if st.button("一键初始化推荐公开来源", key="settings_source_init"):
            initialize_recommended_sources(str(workspace["workspace_id"]), db_path)
            st.rerun()
    st.info("CSS选择器、robots、请求频率和商业许可等技术配置保留在“高级管理”。")
