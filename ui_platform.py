from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

from commercial_report import ReportOptions
from commercial_readiness import (
    candidate_event_ids_for_period,
    preflight_customer_report,
)
from event_pipeline import batch_confirm_events
from deepseek_service import build_rag_answerer, load_settings
from document_processor import summarize_changes
from platform_db import (
    connect,
    initialize_recommended_sources,
    list_sources,
    load_recommended_sources,
    recommended_source_issues,
    table_counts,
    table_csv_bytes,
    upsert_source,
)
from platform_report import ReportEligibilityError, generate_platform_report
from operation_store import list_operations, request_operation_cancel
from rag.embeddings import LocalBGEEmbedding
from rag.hybrid_retriever import HybridRetriever
from rag.keyword_search import KeywordSearcher
from rag.rag_service import RAGService
from source_health import validate_enabled_sources
from rag.vector_store import ChromaVectorStore
from workspace_store import load_clients, log_action
from ui_agent import render_ai_configuration
from ui_daily import ui_session_id
from task_runtime import enqueue_background_task


CORE_PAGES = ["首页", "更新公开数据", "智能问答", "数据审核", "报告中心"]


def _go(page: str) -> None:
    # The platform_navigation widget has already been instantiated when a
    # home-page shortcut is clicked. Writing that widget key in the same run
    # raises StreamlitAPIException, so defer the mutation to the next run.
    st.session_state["pending_platform_navigation"] = page
    st.rerun()


def _metric_row(values: list[tuple[str, object]]) -> None:
    for column, (label, value) in zip(st.columns(len(values)), values):
        column.metric(label, value)


def _render_live_tasks(workspace_id: str, db_path) -> None:
    tasks = list_operations(
        workspace_id,
        db_path,
        operation_types=["crawl", "ai_reprocess", "index_rebuild"],
        statuses=["queued", "running"],
        limit=3,
    )
    if not tasks:
        latest = list_operations(
            workspace_id,
            db_path,
            operation_types=["crawl", "ai_reprocess", "index_rebuild"],
            statuses=["succeeded", "partially_succeeded", "failed", "cancelled"],
            limit=1,
        )
        if latest:
            item = latest[0]
            st.caption(
                f"最近任务 {item['operation_run_id']}｜{item['status_label']}｜"
                f"{item.get('result_summary') or '没有结果摘要'}"
            )
        return
    for item in tasks:
        total = int(item.get("total_items") or 0)
        completed = int(item.get("completed_items") or 0)
        live_progress = dict(
            (item.get("metadata") or {}).get("live_progress") or {}
        )
        live_source = live_progress.get("source") or {}
        source_name = (
            str(live_source.get("source_name") or "")
            if isinstance(live_source, Mapping)
            else ""
        )
        elapsed_seconds = 0
        try:
            started = datetime.fromisoformat(
                str(item.get("started_at") or item.get("created_at") or "")
            )
            if started.tzinfo is None:
                started = started.astimezone()
            elapsed_seconds = max(
                0,
                int((datetime.now().astimezone() - started).total_seconds()),
            )
        except ValueError:
            pass
        st.write(
            f"{item['operation_label']}｜{item['operation_run_id']}｜"
            f"{item.get('current_stage') or item['status_label']}"
            + (f"｜来源：{source_name}" if source_name else "")
        )
        st.progress(
            min(1.0, completed / total) if total else 0.0,
            text=(
                f"{completed}/{total or '?'}｜{item.get('current_item') or '等待处理'}｜"
                f"合格 {item.get('success_count', 0)}｜隔离 {item.get('isolated_count', 0)}｜"
                f"跳过 {item.get('skipped_count', 0)}｜失败 {item.get('failed_count', 0)}"
            ),
        )
        st.caption(
            f"已运行：{elapsed_seconds}秒｜"
            f"最近进度：{item.get('heartbeat_at') or item.get('updated_at') or '尚未开始'}"
        )
        actions = st.columns(2)
        if actions[0].button(
            "刷新进度",
            key=f"refresh_{item['operation_run_id']}",
            width="stretch",
        ):
            st.rerun()
        if actions[1].button(
            "取消任务",
            key=f"cancel_{item['operation_run_id']}",
            width="stretch",
        ):
            request_operation_cancel(
                str(item["operation_run_id"]),
                workspace_id,
                db_path,
            )
            st.rerun()


def _source_status(source: Mapping[str, object]) -> str:
    issues = recommended_source_issues(source)
    if issues:
        return "模板配置不完整"
    if bool(source.get("enabled")) and bool(source.get("crawl_allowed")):
        return "已启用"
    if str(source.get("robots_status") or "") != "允许":
        return "待人工确认"
    return "已停用"


def _render_recommended_source_result(*, key_prefix: str) -> None:
    result_key = f"{key_prefix}_recommended_source_result"
    previous_result = st.session_state.pop(result_key, None)
    if previous_result:
        st.success(
            f"推荐来源初始化完成：新增 {previous_result['added']} 个，更新 {previous_result['updated']} 个，"
            f"当前可自动采集 {previous_result['enabled']} 个。"
        )
        for item in previous_result.get("incomplete", []):
            st.warning(f"{item['source_name']}：模板配置不完整——{'；'.join(item['issues'])}")
        for item in previous_result.get("disabled", []):
            st.info(f"{item['source_name']}：已添加但保持停用——{item['reason']}")


def _render_recommended_source_setup(workspace_id: str, db_path, *, key_prefix: str) -> None:
    result_key = f"{key_prefix}_recommended_source_result"

    st.warning("尚未启用公开数据源")
    st.write("可先安装已经核对过入口的推荐模板；许可不明确的来源不会进入商业报告。")
    templates = load_recommended_sources()
    preview = []
    for item in templates:
        issues = recommended_source_issues(item)
        robots_ready = str(item.get("robots_status") or "") == "允许"
        preview.append({
            "来源名称": item.get("source_name", ""),
            "模板状态": "可初始化并启用" if not issues and robots_ready and item.get("default_enabled") else (
                "模板配置不完整" if issues else "添加但保持停用"
            ),
        })
    st.dataframe(pd.DataFrame(preview), hide_index=True, width="stretch")
    confirmed = st.checkbox(
        "我确认仅进行低频公开信息核对，并会在商业使用前复核网站条款和许可。",
        key=f"{key_prefix}_source_template_confirm",
    )
    if st.button("一键初始化推荐来源", type="primary", disabled=not confirmed, key=f"{key_prefix}_source_template_button"):
        st.session_state[result_key] = initialize_recommended_sources(workspace_id, db_path)
        st.rerun()


def _render_simple_source_list(workspace_id: str, db_path, sources: list[dict[str, object]]) -> None:
    if not sources:
        return
    display = pd.DataFrame([
        {
            "来源名称": item["source_name"],
            "当前状态": _source_status(item),
            "启用/停用": "启用" if bool(item.get("enabled")) and bool(item.get("crawl_allowed")) else "停用",
            "最后更新时间": str(item.get("last_success_at") or item.get("last_crawled_at") or "尚未更新"),
        }
        for item in sources
    ])
    st.dataframe(display, hide_index=True, width="stretch")
    labels = {str(item["source_id"]): str(item["source_name"]) for item in sources}
    selected_id = st.selectbox(
        "启用或停用来源", [""] + list(labels),
        format_func=lambda value: "请选择来源" if not value else labels[value],
        key="simple_source_toggle_select",
    )
    if not selected_id:
        return
    selected = next(item for item in sources if str(item["source_id"]) == selected_id)
    is_enabled = bool(selected.get("enabled")) and bool(selected.get("crawl_allowed"))
    label = "停用此来源" if is_enabled else "启用此来源"
    if st.button(label, key="simple_source_toggle_button"):
        if not is_enabled:
            issues = recommended_source_issues(selected)
            if issues:
                st.error(f"{selected['source_name']}：模板配置不完整——{'；'.join(issues)}")
                return
            if str(selected.get("robots_status") or "") != "允许":
                st.error(f"{selected['source_name']}：robots 状态尚未确认允许，请在专业设置中核对。")
                return
        upsert_source({**selected, "workspace_id": workspace_id, "enabled": not is_enabled, "crawl_allowed": not is_enabled}, db_path)
        st.success(f"{selected['source_name']}已{'停用' if is_enabled else '启用'}。")
        st.rerun()


def _event_query(workspace_id: str, db_path, where: str = "1=1", params=()) -> pd.DataFrame:
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT e.*,d.fetched_at,d.extraction_status,d.review_status,d.extraction_quality,
            d.publisher,d.published_at,d.canonical_url,d.raw_html_path,d.cleaned_text,
            d.document_version,d.content_hash,s.source_name AS configured_source,
            (SELECT GROUP_CONCAT(x.quote_text,'｜') FROM event_evidence x
             WHERE x.event_id=e.event_id AND x.verification_status='已验证') AS verified_evidence_quotes,
            s.commercial_reuse_status,s.report_use_allowed,s.robots_status,s.terms_status
            FROM events e JOIN documents d ON d.document_id=e.document_id
            JOIN sources s ON s.source_id=d.source_id
            WHERE e.workspace_id=? AND d.is_current=1 AND ({where})
            ORDER BY e.created_at DESC""",
            (workspace_id, *params),
        ).fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def render_home(workspace: Mapping[str, object], db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    counts = table_counts(workspace_id, db_path)
    sources = list_sources(workspace_id, db_path)
    events = _event_query(workspace_id, db_path)
    with connect(db_path) as connection:
        last_run = connection.execute("SELECT * FROM crawl_runs WHERE workspace_id=? ORDER BY started_at DESC LIMIT 1", (workspace_id,)).fetchone()
        chunks = connection.execute("SELECT COUNT(*) FROM document_chunks WHERE workspace_id=? AND embedding_status='成功'", (workspace_id,)).fetchone()[0]
    current_risk = int(((events.get("human_verified", pd.Series(dtype=int)) == 1) & events.get("risk_level", pd.Series(dtype=str)).isin(["中", "高"]) & ~events.get("status", pd.Series(dtype=str)).isin(["解除", "已结束"])).sum()) if not events.empty else 0
    opportunities = int(events.get("opportunity_level", pd.Series(dtype=str)).isin(["中", "高"]).sum()) if not events.empty else 0
    pending = int((events.get("human_verified", pd.Series(dtype=int)) == 0).sum()) if not events.empty else 0
    st.header("首页")
    _render_recommended_source_result(key_prefix="home")
    st.write("下一步：先更新公开数据，再核对需要人工处理的内容；确认后即可问答和生成报告。")
    _metric_row([
        ("上次更新时间", str(last_run["finished_at"])[:16] if last_run else "尚未更新"),
        ("已启用来源", sum(bool(item["enabled"]) and bool(item["crawl_allowed"]) for item in sources)),
        ("公开文档", counts["documents"]),
        ("待处理文档", pending),
    ])
    _metric_row([
        ("已确认事件", int(events.get("human_verified", pd.Series(dtype=int)).sum()) if not events.empty else 0),
        ("当前中高风险", current_risk),
        ("本期商机", opportunities),
        ("已向量化文本块", int(chunks)),
    ])
    buttons = st.columns(3)
    if buttons[0].button("立即更新公开数据", type="primary", width="stretch"):
        if not any(bool(item["enabled"]) and bool(item["crawl_allowed"]) for item in sources):
            st.session_state["pending_platform_navigation"] = "更新公开数据"
            st.rerun()
        queued = enqueue_background_task(
            workspace_id,
            "crawl",
            db_path,
            data_root,
            ui_session_id=ui_session_id(),
            input_summary="首页快速增量更新：最多3篇，只抓新增，不自动调用AI",
            metadata={
                "source_ids": [],
                "max_articles": 3,
                "refresh_mode": "new_only",
                "auto_ai": False,
                "source_time_budget_seconds": 120,
            },
        )
        st.session_state["last_task_notice"] = (
            f"{'已创建' if queued['created'] else '已有'}采集任务："
            f"{queued['operation_run_id']}"
        )
        st.session_state["pending_platform_navigation"] = "更新公开数据"
        st.rerun()
    if buttons[1].button("进入智能问答", width="stretch"):
        _go("智能问答")
    if buttons[2].button("生成本期报告", width="stretch"):
        _go("报告中心")
    if not any(bool(item["enabled"]) and bool(item["crawl_allowed"]) for item in sources):
        _render_recommended_source_setup(workspace_id, db_path, key_prefix="home")
    st.caption("自动采集只访问用户明确启用的白名单；不登录、不保存 Cookie、不绕过 robots、验证码、付费墙或访问控制。")


def render_update(workspace: Mapping[str, object], db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("更新公开数据")
    _render_recommended_source_result(key_prefix="update")
    notice = st.session_state.pop("last_task_notice", "")
    if notice:
        st.success(notice)
    st.write("采集、AI处理和向量索引分开运行。任务由本地Worker执行，刷新页面不会中断或重复提交。")
    sources = [item for item in list_sources(workspace_id, db_path, enabled_only=True) if bool(item["crawl_allowed"])]
    all_sources = list_sources(workspace_id, db_path)
    if not sources:
        _render_recommended_source_setup(workspace_id, db_path, key_prefix="update")
    _render_simple_source_list(workspace_id, db_path, all_sources)

    st.subheader("任务进度")
    _render_live_tasks(workspace_id, db_path)

    st.subheader("增量采集")
    source_labels = {str(item["source_id"]): str(item["source_name"]) for item in sources}
    selected_sources = st.multiselect(
        "来源",
        options=list(source_labels),
        default=list(source_labels),
        format_func=lambda source_id: source_labels[source_id],
        key="crawl_source_ids",
    )
    update_mode = st.radio(
        "更新规模",
        ["快速更新（最多3篇）", "标准更新（最多5篇）", "自定义更新"],
        horizontal=True,
        key="crawl_update_mode",
    )
    if update_mode.startswith("快速"):
        max_articles = 3
    elif update_mode.startswith("标准"):
        max_articles = 5
    else:
        max_articles = int(
            st.number_input(
                "本次最多文章数",
                min_value=1,
                max_value=20,
                value=5,
                key="crawl_custom_limit",
            )
        )
    date_columns = st.columns(2)
    start_value = date_columns[0].date_input(
        "开始日期（可选）",
        value=None,
        key="crawl_start_date",
    )
    end_value = date_columns[1].date_input(
        "结束日期（可选）",
        value=None,
        key="crawl_end_date",
    )
    refresh_label = st.radio(
        "历史内容策略",
        ["只抓新增", "低频复查最近7天", "强制重新抓取"],
        horizontal=True,
        key="crawl_refresh_label",
    )
    refresh_mode = {
        "只抓新增": "new_only",
        "低频复查最近7天": "recent",
        "强制重新抓取": "force",
    }[refresh_label]
    force_confirmed = False
    if refresh_mode == "force":
        st.warning("强制重新抓取会重新请求历史HTML和PDF，显著增加耗时；质量门禁仍然保持。")
        force_confirmed = st.checkbox(
            "我确认需要强制重新抓取所选内容",
            key="crawl_force_confirm",
        )
    with st.expander("高级参数"):
        auto_ai = st.checkbox(
            "采集后自动执行AI抽取（默认关闭）",
            value=False,
            key="crawl_auto_ai",
        )
        if auto_ai:
            st.caption(f"预计最多调用 {max_articles} 篇；实际只处理正文质量通过且需要AI的文档。")
        source_budget = st.number_input(
            "单一来源总时间预算（秒）",
            min_value=30,
            max_value=300,
            value=120,
            step=15,
            key="crawl_source_budget",
        )
    crawl_disabled = (
        not selected_sources
        or (refresh_mode == "force" and not force_confirmed)
    )
    if st.button(
        "开始增量采集",
        type="primary",
        disabled=crawl_disabled,
        width="stretch",
    ):
        queued = enqueue_background_task(
            workspace_id,
            "crawl",
            db_path,
            data_root,
            source_id=selected_sources[0] if len(selected_sources) == 1 else "",
            ui_session_id=ui_session_id(),
            input_summary=(
                f"{refresh_label}；来源{len(selected_sources)}个；最多{max_articles}篇；"
                f"AI{'开启' if auto_ai else '关闭'}"
            ),
            metadata={
                "source_ids": selected_sources,
                "max_articles": max_articles,
                "start_date": start_value.isoformat() if start_value else "",
                "end_date": end_value.isoformat() if end_value else "",
                "refresh_mode": refresh_mode,
                "auto_ai": bool(auto_ai),
                "source_time_budget_seconds": int(source_budget),
            },
        )
        if queued["created"]:
            st.success(f"采集任务已进入队列：{queued['operation_run_id']}")
        else:
            st.info(f"当前工作空间已有采集任务，已打开现有任务：{queued['operation_run_id']}")
        st.rerun()

    with st.expander("待AI补跑与知识库索引", expanded=False):
        with connect(db_path) as connection:
            ai_pending = int(
                connection.execute(
                    """SELECT COUNT(*) FROM documents WHERE workspace_id=?
                    AND record_state='active' AND ai_status='待AI处理'""",
                    (workspace_id,),
                ).fetchone()[0]
            )
        st.write(f"当前有 {ai_pending} 篇合格文档待AI处理。补跑只读取本地归档，不重新抓取网页。")
        retry_limit = st.number_input(
            "本次最多处理",
            1,
            5,
            min(max(ai_pending, 1), 2),
            key="ai_retry_limit",
        )
        ai_settings = load_settings()
        retry_confirmed = st.checkbox(
            "我确认本次会调用DeepSeek并可能产生少量API费用",
            key="ai_retry_confirmed",
        )
        if st.button(
            "补跑待AI文档",
            disabled=not (ai_pending and ai_settings.configured and retry_confirmed),
        ):
            queued = enqueue_background_task(
                workspace_id,
                "ai_reprocess",
                db_path,
                data_root,
                ui_session_id=ui_session_id(),
                input_summary=f"补跑本地待AI文档，最多{int(retry_limit)}篇",
                metadata={
                    "limit": int(retry_limit),
                    "time_budget_seconds": 120,
                },
            )
            st.success(
                f"{'AI补跑任务已创建' if queued['created'] else '已有AI补跑任务'}："
                f"{queued['operation_run_id']}"
            )
            st.rerun()
        index_limit = st.number_input(
            "本次最多更新向量索引文档",
            min_value=1,
            max_value=100,
            value=5,
            key="index_update_limit",
        )
        if st.button("更新向量索引", key="queue_index_update"):
            queued = enqueue_background_task(
                workspace_id,
                "index_rebuild",
                db_path,
                data_root,
                ui_session_id=ui_session_id(),
                input_summary=f"增量更新最近{int(index_limit)}篇合格文档的向量索引",
                metadata={"limit": int(index_limit)},
            )
            st.success(
                f"{'索引任务已创建' if queued['created'] else '已有索引任务'}："
                f"{queued['operation_run_id']}"
            )
            st.rerun()
        if not ai_settings.configured:
            st.info("未配置DeepSeek时仍可采集、质量检查、规则评分和FTS5检索；可在“来源与系统设置”配置AI。")
    with st.expander("克制地验收真实来源", expanded=False):
        st.caption("只读取每个已启用来源的1个列表页，最多发现5篇、下载2篇；不翻页，请求间隔至少3秒。此操作不会在pytest中运行。")
        confirm_health = st.checkbox("我确认执行小规模真实网络验收", key="source_health_confirm")
        if st.button("开始来源健康验收", disabled=not confirm_health):
            with st.spinner("正在按低频限制检查真实公开来源……"):
                health = validate_enabled_sources(workspace_id, db_path)
            if health:
                st.dataframe([{
                    "来源": item["source_name"], "健康度": item["health_status"],
                    "列表可访问": item["list_accessible"], "发现链接": item["discovered_count"],
                    "正文成功": item["fetched_count"], "提取成功率": item["extraction_success_rate"],
                    "需更新选择器": item["selector_update_needed"], "说明": item["note"],
                } for item in health], hide_index=True, width="stretch")
            else:
                st.warning("当前没有已启用且允许采集的白名单来源。")
    st.info("取消请求会在来源、文章、HTML/PDF与AI文档边界生效；已提交结果保留，尚未开始的项目停止。")


def _rag_service(
    db_path,
    data_root: Path,
    workspace_id: str = "",
    *,
    use_vector: bool = True,
    use_ai: bool = False,
) -> RAGService:
    keyword = KeywordSearcher(db_path)
    if use_vector:
        embedder = LocalBGEEmbedding()
        vector = ChromaVectorStore(data_root / "chroma")
        retriever = HybridRetriever(keyword, embedder, vector)
    else:
        retriever = HybridRetriever(keyword)
    answerer = build_rag_answerer(db_path, workspace_id) if use_ai else None
    return RAGService(db_path, retriever, answerer=answerer)


def render_qa(workspace: Mapping[str, object], db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("智能问答")
    st.caption("回答只使用当前本地知识库。证据不足时会明确拒答；每条来源卡片都映射到真实文档记录。")
    settings = load_settings()
    with connect(db_path) as connection:
        document_count = int(connection.execute(
            "SELECT COUNT(*) FROM documents WHERE workspace_id=? AND is_current=1",
            (workspace_id,),
        ).fetchone()[0])
        chunk_status = connection.execute(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN embedding_status='成功' THEN 1 ELSE 0 END) AS successful
            FROM document_chunks WHERE workspace_id=?""",
            (workspace_id,),
        ).fetchone()
    chunk_total = int(chunk_status["total"] or 0)
    chunk_success = int(chunk_status["successful"] or 0)
    index_ready = bool(document_count and chunk_total and chunk_success == chunk_total and (data_root / "chroma").exists())
    api_ready = settings.configured
    ai_enabled = bool(workspace.get("ai_enabled"))
    prerequisites = st.columns(3)
    prerequisites[0].metric("DeepSeek API", "已配置" if api_ready else "未配置")
    prerequisites[1].metric("知识库文档", document_count)
    prerequisites[2].metric("向量索引", "已完成" if index_ready else f"{chunk_success}/{chunk_total}")
    if not api_ready:
        st.info("尚未配置AI服务，请在专业设置中配置，或继续使用关键词检索。")
    elif not ai_enabled:
        st.info("API密钥已配置，但当前工作空间尚未启用AI辅助；现在仍使用关键词/本地检索。")
    if document_count == 0:
        st.warning("知识库中还没有公开文档，请先点击“立即更新公开数据”。")
        return
    if not index_ready:
        st.warning("向量索引尚未完成；可以先使用关键词检索，或立即构建知识库。")
        if st.button("构建知识库", type="primary"):
            try:
                from rag.indexer import KnowledgeIndexer
                result = KnowledgeIndexer(db_path, data_root / "chroma").rebuild(workspace_id)
            except Exception as exc:
                st.error(f"知识库构建失败：{exc}")
            else:
                st.success(f"知识库构建完成：成功 {result['success']}，失败 {result['failed']}，文本块 {result['chunks']}。")
                st.rerun()
    examples = [
        "本周青岛港有哪些值得关注的风险？", "最近有哪些航行警告？", "哪些风险已经解除？",
        "本月有哪些港口采购或数字化商机？", "某条事件的原始出处是什么？",
    ]
    mode = st.radio("回答模式", ["快速回答", "风险分析", "商机分析", "时间对比", "来源核验"], horizontal=True)
    selected_example = st.selectbox("示例问题", ["自定义问题"] + examples)
    question = st.text_input("请输入问题", "" if selected_example == "自定义问题" else selected_example)
    query_label = "查询公开知识库" if api_ready and ai_enabled else "关键词检索"
    if st.button(query_label, type="primary", disabled=not question.strip()):
        with st.spinner("正在检索本地知识库并核对来源……"):
            answer = _rag_service(
                db_path, data_root, workspace_id,
                use_vector=index_ready,
                use_ai=api_ready and ai_enabled,
            ).ask(question, workspace_id, mode)
        st.markdown(answer.answer)
        if not answer.citations:
            st.warning("没有可展示的来源。")
        else:
            st.subheader("来源")
            for index, citation in enumerate(answer.citations, 1):
                with st.container(border=True):
                    st.markdown(f"**[{index}] {citation['title'] or '公开来源资料'}**")
                    st.caption(f"{citation['publisher'] or '来源待补充'}｜发布：{str(citation['published_at'])[:10] or '未知'}｜获取：{citation['fetched_at']}")
                    st.write(str(citation.get("quote") or ""))
                    st.caption(
                        f"证据：{citation.get('verification_status') or '待核对'}｜"
                        f"人工审核：{citation.get('human_review_status') or '待真人核验'}｜"
                        f"事件/文档：{citation.get('event_id') or citation.get('document_id')}"
                    )
                    if citation.get("canonical_url"):
                        st.link_button("打开原文", str(citation["canonical_url"]))


def render_review(workspace: Mapping[str, object], db_path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("数据审核")
    st.write("这里只集中展示必须或值得人工处理的异常；自动抽取结果不会直接进入商业报告。")
    events = _event_query(workspace_id, db_path)
    if events.empty:
        st.info("暂无待审核数据。")
        return
    events["审核原因"] = ""
    events.loc[events["extraction_confidence"] < 0.6, "审核原因"] += "AI/规则置信度较低；"
    events.loc[events["duplicate_level"].isin(["可能重复", "高度疑似重复"]), "审核原因"] += "疑似重复；"
    events.loc[events["event_date"].eq(""), "审核原因"] += "日期提取失败；"
    events.loc[events["status"].eq("解除") & events["related_event_id"].eq(""), "审核原因"] += "解除未关联；"
    events.loc[events["commercial_reuse_status"].isin(["未明确", "需取得许可"]), "审核原因"] += "商业许可未明确；"
    events.loc[events["risk_level"].isin(["中", "高"]), "审核原因"] += "中高风险；"
    pending = events[(events["human_verified"] == 0) | events["审核原因"].ne("")].copy()
    if pending.empty:
        st.success("当前没有待处理异常。")
        return
    display = pending[["event_id", "event_date", "title", "category", "status", "risk_level", "duplicate_level", "commercial_reuse_status", "审核原因", "canonical_url"]].copy()
    st.dataframe(display, hide_index=True, width="stretch", column_config={"canonical_url": st.column_config.LinkColumn("原文", display_text="打开")})
    selectable = pending[~pending["duplicate_level"].eq("高度疑似重复")]
    if selectable.empty:
        st.warning("当前待审核项均为高度疑似重复，必须先处理重复关系。")
        return
    st.subheader("逐项人工核验")
    selected_id = st.selectbox(
        "选择一条待核验信息",
        selectable["event_id"].tolist(),
        format_func=lambda value: f"{selectable.loc[selectable['event_id']==value, 'title'].iloc[0]}",
    )
    selected_row = selectable.loc[selectable["event_id"] == selected_id].iloc[0]
    if str(selected_row.get("canonical_url") or "").startswith(("http://", "https://")):
        st.link_button("1. 打开原文", str(selected_row["canonical_url"]))
    st.caption(
        f"文档版本 v{int(selected_row.get('document_version') or 1)}｜"
        f"发布：{str(selected_row.get('published_at') or '')[:10] or '待核对'}｜"
        f"来源：{selected_row.get('publisher') or selected_row.get('configured_source') or '待核对'}"
    )
    with st.expander("2. 对照原始正文与抽取字段", expanded=True):
        st.text_area(
            "清洗原文（只读）",
            str(selected_row.get("cleaned_text") or ""),
            height=260,
            disabled=True,
            key=f"review_source_{selected_id}",
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "标题": selected_row.get("title", ""),
                        "日期": selected_row.get("event_date", ""),
                        "类别": selected_row.get("category", ""),
                        "状态": selected_row.get("status", ""),
                        "事实摘要": selected_row.get("summary", ""),
                        "潜在影响（分析）": selected_row.get("impact", ""),
                        "已定位证据": selected_row.get("verified_evidence_quotes", "") or "尚无逐字定位证据",
                    }
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    reviewer_type_label = st.radio(
        "核验人类型",
        ["项目使用者", "行业复核人"],
        horizontal=True,
        key=f"reviewer_type_{selected_id}",
    )
    reviewer_name = st.text_input(
        "核验人姓名或内部代号",
        key=f"reviewer_name_{selected_id}",
        help="仅 human_user 或 industry_reviewer 计入人工准确率；AI和Codex不能代替此确认。",
    )
    checklist_labels = [
        ("source_opened", "我已打开并查看原始公开来源。"),
        ("title_checked", "标题与原文一致。"),
        ("date_checked", "发布日期已核对。"),
        ("publisher_checked", "发布机构已核对。"),
        ("summary_checked", "事实摘要与原文基本一致。"),
        ("evidence_checked", "证据片段可以在原文逐字定位。"),
        ("analysis_checked", "潜在影响和建议已明确标为分析，不是原文事实。"),
    ]
    checklist = {
        key: st.checkbox(label, key=f"review_{key}_{selected_id}")
        for key, label in checklist_labels
    }
    reviewer_note = st.text_area("核验备注", key=f"review_note_{selected_id}", height=80)
    ready = bool(reviewer_name.strip()) and all(checklist.values())
    if st.button("保存真人核验", type="primary", disabled=not ready, key=f"save_review_{selected_id}"):
        reviewer_type = "human_user" if reviewer_type_label == "项目使用者" else "industry_reviewer"
        result = batch_confirm_events(
            [selected_id],
            workspace_id,
            db_path,
            report_eligible=True,
            reviewer_type=reviewer_type,
            reviewer_name=reviewer_name,
            review_method="界面逐项对照原文",
            review_version="0.5.0-beta",
            reviewer_note=reviewer_note,
            checklist=checklist,
        )
        if result["confirmed"]:
            log_action(
                workspace_id,
                "人工核验",
                "event",
                selected_id,
                {"reviewer_type": reviewer_type, "review_method": "界面逐项对照原文"},
                db_path,
            )
            st.success("人工核验已保存。只有同时满足证据与来源许可门禁的内容才可进入客户报告。")
        if result["report_ineligible"]:
            st.warning(result["report_ineligible"].get(selected_id, "当前不具备客户报告资格。"))
        if result["blocked"]:
            st.error(result["blocked"].get(selected_id, "核验未保存。"))


def render_reports(workspace: Mapping[str, object], paths, db_path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("报告中心")
    clients = load_clients(workspace_id, db_path, include_disabled=False)
    client_options = [""] + [str(item["client_id"]) for item in clients]
    client_by_id = {str(item["client_id"]): item for item in clients}
    columns = st.columns(3)
    end = columns[1].date_input("结束日期", date.today())
    start = columns[0].date_input("开始日期", end - timedelta(days=int(workspace.get("default_period_days") or 7) - 1))
    client_id = columns[2].selectbox("客户", client_options, format_func=lambda value: "通用报告" if not value else str(client_by_id[value]["client_name"]))
    client = client_by_id.get(client_id)
    report_title = st.text_input("报告名称", str((client or {}).get("report_title") or workspace.get("default_report_title") or "青岛港公开信息情报报告"))
    pending_mode = st.session_state.pop("pending_report_mode", "")
    report_mode_options = ["内部研究版", "客户交付版"]
    if pending_mode in report_mode_options:
        st.session_state["platform_report_mode"] = pending_mode
    report_mode = st.radio(
        "报告模式", report_mode_options, horizontal=True,
        help="内部研究版允许许可尚待确认的公开资料，但仅供内部研究；客户交付版要求真人确认、逐字证据和摘要/短引门禁。",
        key="platform_report_mode",
    )
    report_type = st.selectbox("报告类型", ["综合情报", "风险专题", "商机专题", "来源核验"])
    options_cols = st.columns(3)
    include_risks = options_cols[0].checkbox("包含风险", True)
    include_opportunities = options_cols[1].checkbox("包含商机", True)
    source_appendix = options_cols[2].checkbox("包含完整来源", True)
    verified_only = st.checkbox("只使用人工确认且许可允许的数据", report_mode == "客户交付版", disabled=report_mode == "客户交付版")
    all_categories = ["航行警告", "海上气象", "港口作业", "航线运价", "政策监管", "招标采购", "企业动态"]
    default_categories = list((client or {}).get("focus_categories") or [])
    focus_categories = st.multiselect("关注类别", all_categories, default=[item for item in default_categories if item in all_categories])
    focus_only = st.checkbox("只包含所选关注类别", bool(focus_categories))
    allow_empty_template = st.checkbox(
        "没有合格事件时仅生成空白模板",
        False,
        help="默认会阻止0条事件的完整报告。勾选后文件会醒目标记为“空白模板，不是正式报告”。",
    )
    report_client = dict(client or {})
    report_client["focus_categories"] = focus_categories
    options = ReportOptions(
        focus_categories_only=focus_only,
        medium_high_only=report_type == "风险专题",
        include_risks=include_risks,
        include_opportunities=include_opportunities,
        show_score_details=st.checkbox("显示评分明细", False),
        include_source_appendix=source_appendix,
        allow_empty_template=allow_empty_template,
    )
    st.caption(
        "客户交付版仅纳入来源有效、非重复、真人确认、证据逐字可定位且允许必要事实摘要/短引用的数据；"
        "内部研究版会醒目标记使用限制，不全文转载，也不提供可再销售的原始数据。"
    )
    customer_preflight = None
    if report_mode == "客户交付版":
        candidate_ids = candidate_event_ids_for_period(
            workspace_id,
            db_path,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        customer_preflight = preflight_customer_report(
            workspace_id,
            candidate_ids,
            db_path,
            reference_date=end,
        )
        if customer_preflight.passed:
            st.success(f"客户报告预检通过：{len(candidate_ids)} 条候选事件。")
        else:
            st.error(
                f"客户报告预检未通过：{len(customer_preflight.blockers)} 项阻断。"
                "当前只能生成内部研究草稿。"
            )
            with st.expander("查看预检问题", expanded=True):
                for item in customer_preflight.blockers:
                    st.write(
                        f"- {item['label']}：{item['count']} 条"
                        + (
                            "（" + "；".join(str(value) for value in item["details"][:3]) + "）"
                            if item["details"]
                            else ""
                        )
                    )
            if st.button("改为内部研究草稿", width="content"):
                st.session_state["pending_report_mode"] = "内部研究版"
                st.rerun()
    if st.button(
        "生成客户报告",
        type="primary",
        disabled=bool(
            report_mode == "客户交付版"
            and customer_preflight is not None
            and not customer_preflight.passed
        ),
    ):
        try:
            artifacts = generate_platform_report(
                workspace, paths, client=report_client or None, start_date=start, end_date=end,
                report_title=report_title, analyst=str((client or {}).get("analyst_name") or workspace.get("default_analyst") or ""),
                options=options, db_path=db_path, human_verified_only=verified_only,
                report_mode=report_mode,
                ui_session_id=ui_session_id(),
            )
        except ReportEligibilityError as exc:
            st.error(str(exc))
            st.info("建议先完成数据审核与来源许可核对，或改为生成“内部研究版”。")
        except Exception as exc:
            st.error(f"报告生成失败：{exc}")
        else:
            st.success(f"报告已生成：{artifacts.report_id} v{artifacts.version}")
            downloads = st.columns(3)
            downloads[0].download_button("下载报告", artifacts.docx_path.read_bytes(), artifacts.docx_path.name, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            downloads[1].download_button("下载 HTML", artifacts.html_path.read_bytes(), artifacts.html_path.name, "text/html")
            downloads[2].download_button("下载 Excel 附件", artifacts.xlsx_path.read_bytes(), artifacts.xlsx_path.name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _source_form(source: Mapping[str, object], workspace_id: str, db_path, prefix: str) -> None:
    with st.form(prefix):
        source_id = str(source.get("source_id") or "")
        cols = st.columns(2)
        name = cols[0].text_input("来源名称", str(source.get("source_name") or ""))
        organization = cols[1].text_input("发布机构", str(source.get("organization") or ""))
        homepage = st.text_input("官网", str(source.get("homepage_url") or ""))
        list_url = st.text_input("列表页", str(source.get("list_page_url") or ""))
        domain = st.text_input("白名单域名", str(source.get("domain") or (urlparse(list_url).hostname or "")))
        classification = st.columns(2)
        source_types = ["政府/监管机构", "港口/企业官网", "交易所/行业机构", "主流媒体", "行业自媒体", "其他"]
        categories = ["", "航行警告", "海上气象", "港口作业", "航线运价", "政策监管", "招标采购", "企业动态"]
        current_type = str(source.get("source_type") or "其他")
        current_category = str(source.get("category_hint") or "")
        source_type = classification[0].selectbox("来源类型", source_types, index=source_types.index(current_type) if current_type in source_types else len(source_types)-1)
        category_hint = classification[1].selectbox("类别提示", categories, index=categories.index(current_category) if current_category in categories else 0)
        row = st.columns(4)
        adapter_options = ["generic_html", "rss", "api", "shandong_maritime", "qingdao_ocean", "qingdao_government"]
        current_adapter = str(source.get("adapter_type") or "generic_html")
        adapter_type = row[0].selectbox("适配器", adapter_options, index=adapter_options.index(current_adapter) if current_adapter in adapter_options else 0)
        robots = row[1].selectbox("robots", ["未检查", "允许", "禁止", "检查失败"], index=["未检查", "允许", "禁止", "检查失败"].index(str(source.get("robots_status") or "未检查")))
        terms = row[2].selectbox("网站条款", ["未检查", "允许", "禁止", "需人工确认"], index=["未检查", "允许", "禁止", "需人工确认"].index(str(source.get("terms_status") or "未检查")))
        reuse = row[3].selectbox("商业再利用", ["未明确", "允许", "禁止", "需取得许可"], index=["未明确", "允许", "禁止", "需取得许可"].index(str(source.get("commercial_reuse_status") or "未明确")))
        rate = st.number_input("每个域名请求间隔（秒，不得低于2）", min_value=2.0, value=max(2.0, float(source.get("rate_limit_seconds") or 2.0)), step=0.5)
        limits = st.columns(2)
        max_pages = limits[0].number_input("每次最多列表页", min_value=1, max_value=20, value=int(source.get("max_pages_per_run") or 1))
        max_articles = limits[1].number_input("每次最多文章", min_value=1, max_value=20, value=int(source.get("max_articles_per_run") or 5))
        config = st.text_area("适配器选择器 JSON", json.dumps(source.get("adapter_config") or {}, ensure_ascii=False, indent=2))
        note = st.text_area("许可备注", str(source.get("license_note") or ""))
        flags = st.columns(2)
        enabled = flags[0].checkbox("启用", bool(source.get("enabled", False)))
        crawl_allowed = flags[1].checkbox("允许自动采集", bool(source.get("crawl_allowed", False)))
        internal_flags = st.columns(2)
        internal_collection = internal_flags[0].checkbox(
            "允许内部采集",
            bool(source.get("internal_collection_allowed", True)),
        )
        internal_analysis = internal_flags[1].checkbox(
            "允许内部分析与检索",
            bool(source.get("internal_analysis_allowed", True)),
        )
        customer_flags = st.columns(2)
        customer_summary = customer_flags[0].checkbox(
            "允许客户报告必要事实摘要",
            bool(source.get("customer_summary_allowed", True)),
        )
        short_quote = customer_flags[1].checkbox(
            "允许客户报告必要短引用",
            bool(source.get("short_quote_allowed", True)),
        )
        redistribution = st.columns(2)
        fulltext = redistribution[0].checkbox(
            "允许第三方全文再分发",
            bool(source.get("fulltext_redistribution_allowed", False)),
        )
        raw_resale = redistribution[1].checkbox(
            "允许原始数据转售",
            bool(source.get("raw_data_resale_allowed", False)),
        )
        redistribution_confirm = st.checkbox(
            "如启用全文再分发或原始数据转售，我已核对明确依据并承担复核责任",
            value=not (fulltext or raw_resale),
        )
        submitted = st.form_submit_button("保存来源")
    if submitted:
        try:
            adapter_config = json.loads(config or "{}")
            candidate = {**source, "source_id": source_id, "workspace_id": workspace_id, "source_name": name,
                "organization": organization, "homepage_url": homepage, "list_page_url": list_url, "domain": domain,
                "source_type": source_type, "category_hint": category_hint,
                "adapter_type": adapter_type, "robots_status": robots, "terms_status": terms,
                "commercial_reuse_status": reuse, "rate_limit_seconds": rate, "max_pages_per_run": max_pages,
                "max_articles_per_run": max_articles, "adapter_config": adapter_config, "license_note": note,
                "enabled": enabled, "crawl_allowed": crawl_allowed,
                "internal_collection_allowed": internal_collection,
                "internal_analysis_allowed": internal_analysis,
                "customer_summary_allowed": customer_summary,
                "short_quote_allowed": short_quote,
                "fulltext_redistribution_allowed": fulltext,
                "raw_data_resale_allowed": raw_resale,
                "report_use_allowed": bool(customer_summary and short_quote)}
            issues = recommended_source_issues(candidate) if adapter_type not in {"rss", "api"} else []
            if (enabled or crawl_allowed) and issues:
                raise ValueError("模板配置不完整——" + "；".join(issues))
            if (enabled or crawl_allowed) and robots != "允许":
                raise ValueError("robots 状态尚未确认允许，不能启用自动采集")
            if (fulltext or raw_resale) and not redistribution_confirm:
                raise ValueError("全文再分发或原始数据转售必须二次确认许可依据")
            upsert_source(candidate, db_path)
        except Exception as exc:
            st.error(str(exc) if str(exc) else "来源配置无法保存，请核对必填项。")
        else:
            st.success("来源已保存。")
            st.rerun()


def render_professional_settings(workspace: Mapping[str, object], db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("专业设置")
    tabs = st.tabs(["白名单来源", "采集运行", "知识库索引", "AI设置", "原始归档", "导入与导出"])
    with tabs[0]:
        sources = list_sources(workspace_id, db_path)
        labels = {str(item["source_id"]): str(item["source_name"]) for item in sources}
        selected = st.selectbox("编辑来源", [""] + list(labels), format_func=lambda value: "新增来源" if not value else labels[value])
        row = next((item for item in sources if item["source_id"] == selected), {})
        _source_form(row, workspace_id, db_path, f"source_form_{selected or 'new'}")
        st.warning("上海航运交易所相关指数默认‘需取得许可’，在书面许可或明确条款确认前不得进入收费数据附件。")
    with tabs[1]:
        with connect(db_path) as connection:
            runs = pd.DataFrame([dict(row) for row in connection.execute("SELECT * FROM crawl_runs WHERE workspace_id=? ORDER BY started_at DESC LIMIT 100", (workspace_id,)).fetchall()])
        st.dataframe(runs, hide_index=True, width="stretch")
    with tabs[2]:
        with connect(db_path) as connection:
            status = pd.DataFrame([dict(row) for row in connection.execute("SELECT embedding_status,COUNT(*) AS count FROM document_chunks WHERE workspace_id=? GROUP BY embedding_status", (workspace_id,)).fetchall()])
        st.dataframe(status, hide_index=True)
        st.caption(f"Chroma 持久化目录：{data_root / 'chroma'}；默认模型 BAAI/bge-small-zh-v1.5 首次使用时下载。")
        if st.button("重建知识库索引"):
            try:
                from rag.indexer import KnowledgeIndexer
                result = KnowledgeIndexer(db_path, data_root / "chroma").rebuild(workspace_id)
            except Exception as exc:
                st.error(f"索引未完成：{exc}")
            else:
                st.success(str(result))
    with tabs[3]:
        render_ai_configuration(workspace, db_path, expanded=True, key_prefix="professional")
        st.divider()
        st.subheader("AI调用日志")
        with connect(db_path) as connection:
            calls = pd.DataFrame([dict(row) for row in connection.execute("SELECT * FROM ai_call_logs WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100", (workspace_id,)).fetchall()])
        st.dataframe(calls, hide_index=True, width="stretch")
        st.caption("日志仅记录模型、字符数、耗时和错误类型，不记录 API 密钥。")
    with tabs[4]:
        with connect(db_path) as connection:
            docs = pd.DataFrame([dict(row) for row in connection.execute("SELECT document_id,title,document_version,previous_document_id,raw_html_path,extraction_status FROM documents WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100", (workspace_id,)).fetchall()])
        st.dataframe(docs, hide_index=True, width="stretch")
        if not docs.empty:
            selected_document = st.selectbox("查看文档版本差异", docs["document_id"].tolist())
            with connect(db_path) as connection:
                current = connection.execute("SELECT * FROM documents WHERE document_id=?", (selected_document,)).fetchone()
                previous = connection.execute("SELECT cleaned_text FROM documents WHERE document_id=?", (current["previous_document_id"],)).fetchone() if current and current["previous_document_id"] else None
            if current and previous:
                st.code(summarize_changes(str(previous["cleaned_text"]), str(current["cleaned_text"])) or "正文规范化后未发现可显示差异。")
            elif current:
                st.caption("这是该 URL 的首个归档版本。")
    with tabs[5]:
        st.write("SQLite 是核心存储；CSV 仅用于备份、审阅和兼容迁移。导出不会包含 API 密钥或 Cookie。")
        downloads = st.columns(3)
        downloads[0].download_button("导出事件 CSV", table_csv_bytes("events", workspace_id, db_path), "events_sqlite_export.csv", "text/csv")
        downloads[1].download_button("导出来源 CSV", table_csv_bytes("sources", workspace_id, db_path), "sources_sqlite_export.csv", "text/csv")
        downloads[2].download_button("导出文档元数据 CSV", table_csv_bytes("documents", workspace_id, db_path), "documents_sqlite_export.csv", "text/csv")
        st.info("旧版 events.csv / sources.csv / intake.csv 的备份迁移入口仍保留在专业模式“设置”页面；迁移前自动复制到 data/legacy_backup。")
