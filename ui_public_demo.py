from __future__ import annotations

"""Minimal, read-only Streamlit surface used when PUBLIC_DEMO_MODE=true."""

from datetime import datetime
from typing import Mapping
from urllib.parse import urlparse

import streamlit as st

from public_demo_store import PublicDemoRepository
from runtime_config import RuntimeConfig, load_runtime_config


PUBLIC_PAGES = ["平台概览", "事件浏览", "数据来源", "报告能力", "关于"]


def _safe_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _date_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "暂无"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10]


def _confidence(value: object) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "未提供"


def _event_card(repository: PublicDemoRepository, event: Mapping[str, object]) -> None:
    with st.container(border=True):
        heading, labels = st.columns([4, 2])
        with heading:
            st.markdown(f"#### {event.get('title') or '公开信息'}")
            st.caption(
                f"{_date_text(event.get('event_date'))} · {event.get('source_name') or '公开来源'} · "
                f"{event.get('category') or '未分类'}"
            )
        with labels:
            risk = str(event.get("risk_level") or "低")
            status = str(event.get("status") or "待核实")
            st.markdown(f"**风险提示：{risk}**　｜　状态：{status}")
            st.caption(f"AI抽取置信度：{_confidence(event.get('extraction_confidence'))}")

        st.write(str(event.get("summary") or "暂无摘要。"))
        impact = str(event.get("impact") or "").strip()
        if impact:
            st.info(f"分析提示：{impact}")

        source_url = _safe_url(event.get("source_url"))
        source_col, detail_col = st.columns([1, 4])
        with source_col:
            if source_url:
                st.link_button("查看原始来源", source_url, width="stretch")
        with detail_col:
            review_text = "已独立人工复核" if bool(event.get("human_verified")) else "AI辅助抽取，待独立人工复核"
            evidence_text = "短证据已逐字定位" if bool(event.get("evidence_verified")) else "短证据待核对"
            st.caption(f"{review_text} · {evidence_text}")

        with st.expander("查看证据与结构化字段"):
            evidence_rows = repository.evidence(str(event.get("event_key") or ""))
            if evidence_rows:
                for index, evidence in enumerate(evidence_rows, start=1):
                    st.markdown(f"**证据 {index}**　{evidence.get('quote_text')}")
                    st.caption(str(evidence.get("verification_status") or "待核对"))
            else:
                st.warning("当前演示记录没有可展示的逐字短证据，请以原始来源为准。")
            left, right = st.columns(2)
            left.write(f"**涉及主体：** {event.get('subject') or '未提取'}")
            left.write(f"**影响区域：** {event.get('affected_area') or '未提取'}")
            right.write(f"**业务价值：** {event.get('business_value') or '未评估'}")
            right.write(f"**商机提示：** {event.get('opportunity_level') or '低'}")
            action = str(event.get("recommended_action") or "").strip()
            if action:
                st.write(f"**建议核对动作（分析）：** {action}")


def _render_home(repository: PublicDemoRepository, config: RuntimeConfig) -> None:
    st.subheader("将分散的公开资料转化为可检索、可追溯的行业情报")
    st.write(
        "平台对公开信息进行正文质量校验、结构化抽取与事件分析，"
        "帮助研究人员快速定位原始来源和关键证据。"
    )
    st.caption("本页面为只读演示：不接受任意网址，不触发采集、AI调用或后台管理任务。")

    st.markdown("### 数据处理流程")
    steps = ["信息采集", "正文质量门禁", "AI结构化抽取", "事件数据库", "分析与报告"]
    columns = st.columns(len(steps))
    for index, (column, step) in enumerate(zip(columns, steps), start=1):
        with column:
            st.markdown(f"**{index}. {step}**")

    metrics = repository.metrics()
    st.markdown("### 当前演示数据")
    metric_columns = st.columns(5)
    metric_columns[0].metric("已采集文档", metrics.document_count)
    metric_columns[1].metric("合格正文", metrics.qualified_document_count)
    metric_columns[2].metric("结构化事件", metrics.event_count)
    metric_columns[3].metric("启用来源", metrics.enabled_source_count)
    metric_columns[4].metric("最近更新时间", _date_text(metrics.latest_update))
    st.caption("以上数字从随项目部署的脱敏只读演示数据库实时计算，不包含正式工作区数据。")

    st.markdown("### 近期重点事件")
    events = repository.list_events(limit=5)
    if not events:
        st.info("当前没有可展示的演示事件。")
    for event in events:
        _event_card(repository, event)

    st.markdown("### 数据来源与边界")
    st.write("数据来自公开网络信息。平台仅用于公开信息整理、研究与技术演示。")
    if not config.deepseek_key_present:
        st.caption("AI实时分析功能当前未启用；已处理的历史演示数据仍可正常浏览。")


def _render_events(repository: PublicDemoRepository) -> None:
    st.subheader("事件浏览")
    st.caption("可检索演示库中的事实摘要和短证据；页面不提供原始全文或数据库导出。")
    options = repository.filter_options()
    filters = st.columns([2, 1, 1])
    keyword = filters[0].text_input("关键词", placeholder="例如：大风、预警、解除")
    categories = filters[1].multiselect("事件类型", options["categories"])
    sources = filters[2].multiselect("来源", options["sources"])
    events = repository.list_events(keyword=keyword, categories=categories, sources=sources)
    st.caption(f"找到 {len(events)} 条演示事件")
    if not events:
        st.info("没有匹配结果，请调整筛选条件。")
    for event in events:
        _event_card(repository, event)


def _render_sources(repository: PublicDemoRepository) -> None:
    st.subheader("数据来源")
    st.caption("公网演示仅展示来源入口和已有处理结果，不会代表访问者发起网络采集。")
    for source in repository.list_sources():
        with st.container(border=True):
            title = str(source.get("source_name") or "公开来源")
            status = "已启用" if bool(source.get("enabled")) else "演示库未启用"
            st.markdown(f"#### {title}")
            st.caption(f"{source.get('organization') or ''} · {source.get('region') or ''} · {status}")
            st.write(f"主要信息类型：{source.get('category_hint') or source.get('source_type') or '公开信息'}")
            homepage = _safe_url(source.get("homepage_url"))
            if homepage:
                st.link_button("访问官方网站", homepage)
    st.info("公开可访问不等同于允许全文再分发；本演示仅展示必要摘要、短证据和原始链接。")


def _render_reports(repository: PublicDemoRepository) -> None:
    st.subheader("报告能力")
    metrics = repository.metrics()
    st.write(
        "本地完整版可基于已筛选事件生成 DOCX、HTML 和 Excel 附件，并保留来源、证据与版本记录。"
    )
    st.metric("当前演示事件", metrics.event_count)
    st.warning("公网 Demo 不开放报告生成、文件写入或下载原始数据，以避免资源滥用和第三方全文再分发。")
    st.markdown(
        """
        报告处理原则：

        - 事实摘要与分析意见明确区分；
        - 每条核心信息保留发布机构、日期、原始链接和短证据；
        - 未经独立人工复核的内容不会标记为客户正式结论；
        - 重要信息应回到原始发布机构核验。
        """
    )


def _render_about(repository: PublicDemoRepository, config: RuntimeConfig, version: str) -> None:
    st.subheader("关于本 Demo")
    health = repository.health()
    if health["ok"]:
        st.success("应用、配置与演示数据库状态正常。")
    else:
        st.warning("数据暂时无法加载。")
    st.write(f"版本：{version} · 运行模式：{config.mode_label}")
    st.markdown(
        """
        **免责声明**

        本平台为公开信息智能处理技术 Demo。

        数据来源于公开网络信息，仅用于技术展示、研究与信息整理，不构成投资、商业、法律或其他专业建议。

        平台可能存在数据延迟、缺失或 AI 抽取误差，重要信息应以原始发布机构为准。
        """
    )


def render_public_demo_app(version: str) -> None:
    config = load_runtime_config()
    repository = PublicDemoRepository(config.demo_database_path)

    st.title("港航公开信息监测与分析平台")
    st.caption("Public Intelligence Monitoring & Industry Insight Platform")
    st.markdown("**青岛港 / 山东港航公开数据试验场景**")

    with st.sidebar:
        st.success("公网只读 Demo")
        page = st.radio("导航", PUBLIC_PAGES, label_visibility="collapsed")
        st.divider()
        st.caption("不开放采集、上传、AI调用、管理或写入操作")
        st.caption(f"版本 {version}")

    if page == "平台概览":
        _render_home(repository, config)
    elif page == "事件浏览":
        _render_events(repository)
    elif page == "数据来源":
        _render_sources(repository)
    elif page == "报告能力":
        _render_reports(repository)
    else:
        _render_about(repository, config, version)

    st.divider()
    st.caption("公开信息技术演示 · 仅展示脱敏摘要和必要短证据 · 请以原始发布机构为准")
