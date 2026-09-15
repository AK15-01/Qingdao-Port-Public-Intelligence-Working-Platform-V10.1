from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from pathlib import Path

import pandas as pd
import streamlit as st

from data_store import (
    ALLOWED_CATEGORIES,
    ALLOWED_SOURCE_TYPES,
    ALLOWED_STATUSES,
    DEFAULT_EVENTS_PATH,
    STANDARD_FIELDS,
    csv_bytes,
    delete_event,
    empty_events_dataframe,
    ensure_data_file,
    load_events,
    normalize_events,
    save_events,
)
from data_validator import ValidationReport, is_valid_source_url, validate_events
from generate_weekly_report import build_html_report, build_weekly_report
from lifecycle import ACTIVE_STATUSES, current_risks, related_timeline, resolved_risks
from risk_engine import enrich_dataframe, load_rules
from intake_analyzer import potential_duplicate_event_ids
from intake_store import ensure_intake_file, load_intakes
from source_store import ensure_sources_file, load_sources
from ui_collection import (
    render_collection_workbench,
    render_draft_inbox,
    render_source_management,
    render_today_dashboard,
)
from ui_novice import render_novice_workbench
from ui_product import (
    render_clients_and_reports,
    render_current_outcomes,
    render_settings,
)
from workspace_store import (
    create_workspace,
    database_path,
    data_root,
    get_active_workspace,
    initialize_database,
    load_workspaces,
    log_action,
    set_active_workspace,
    workspace_paths,
)
from ui_agent import render_agent_settings, render_ai_workbench, render_tasks_and_reports
from platform_db import (
    initialize_database as initialize_platform_database,
)
from security_audit import release_env_warning
from operation_store import recover_stale_operations
from ui_platform import (
    render_home as render_platform_home,
    render_professional_settings,
    render_qa as render_platform_qa,
    render_reports as render_platform_reports,
    render_review as render_platform_review,
    render_update as render_platform_update,
)
from ui_daily import (
    DAILY_PAGES,
    render_daily_home,
    render_document_library,
    render_event_library,
    render_evidence_repair,
    render_history,
    render_human_review,
    render_single_import,
    render_system_check,
)
from ui_commercial_readiness import render_commercial_readiness
from runtime_config import is_public_demo_mode, redact_sensitive_text


APP_DIR = Path(__file__).resolve().parent
SOURCES_PATH = APP_DIR / "sources.md"
TEMPLATE_PATH = APP_DIR / "data" / "events_template.csv"
APP_VERSION = (
    (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    if (APP_DIR / "VERSION").is_file()
    else "unknown"
)
PUBLIC_DEMO_MODE = is_public_demo_mode()
LOGGER = logging.getLogger("portscope.public_demo")

st.set_page_config(
    page_title=(
        "港航公开信息监测与分析平台"
        if PUBLIC_DEMO_MODE
        else "PortScope｜港航公开信息监测与分析平台"
    ),
    page_icon="⚓",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.8rem; padding-bottom: 3rem;}
    [data-testid="stMetric"] {border: 1px solid #dce7ed; padding: 0.75rem; border-radius: 0.4rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


if PUBLIC_DEMO_MODE:
    try:
        from ui_public_demo import render_public_demo_app

        render_public_demo_app(APP_VERSION)
    except Exception as exc:
        # Public visitors get a stable boundary; details stay server-side and are redacted.
        LOGGER.error(
            "public_demo_render_failed type=%s message=%s",
            type(exc).__name__,
            redact_sensitive_text(exc),
        )
        st.error("数据暂时无法加载，请稍后重试。")
        st.caption("应用已安全降级，未执行采集、AI调用或后台写入。")
    st.stop()


def _value(row: pd.Series, field: str) -> str:
    value = row.get(field, "")
    return "" if value is None or pd.isna(value) else str(value).strip()


def _valid_date(value: object, fallback: date = date.today()) -> date:
    parsed = pd.to_datetime(value, errors="coerce")
    return parsed.date() if pd.notna(parsed) else fallback


def _option_index(options: list[str], value: str) -> int:
    return options.index(value) if value in options else 0


def _display_events(dataframe: pd.DataFrame, columns: list[str]) -> None:
    if dataframe.empty:
        st.info("暂无符合条件的事件。")
        return
    available = [column for column in columns if column in dataframe.columns]
    display = dataframe[available].copy()
    if "event_date" in display:
        display["event_date"] = pd.to_datetime(
            display["event_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
    column_config = {}
    if "source_url" in display:
        column_config["source_url"] = st.column_config.LinkColumn(
            "来源原文", display_text="打开原文"
        )
    st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        column_config=column_config,
    )


def _source_link(row: pd.Series) -> None:
    url = _value(row, "source_url")
    source = _value(row, "source_name") or "未填写来源"
    if is_valid_source_url(url):
        st.markdown(f"来源：[{source}]({url})｜{_value(row, 'source_type')}")
    else:
        st.caption(f"来源：{source}｜链接缺失或异常")


def _quality_metrics(report: ValidationReport) -> None:
    columns = st.columns(5)
    columns[0].metric("总记录数", report.total_records)
    columns[1].metric("完整率", f"{report.completeness_rate:.1f}%")
    columns[2].metric("重复记录数", report.duplicate_record_count)
    columns[3].metric("缺失来源数", report.missing_source_count)
    columns[4].metric("待核实记录数", report.pending_verification_count)


def _event_label(row: pd.Series) -> str:
    return f"{_value(row, 'event_id')}｜{_value(row, 'title')}"


def _candidate_with_new_event(raw: pd.DataFrame, values: dict[str, object]) -> pd.DataFrame:
    new_row = normalize_events(pd.DataFrame([values]), assign_ids=True)
    return pd.concat([raw, new_row], ignore_index=True)


def _render_overview(scored: pd.DataFrame, quality: ValidationReport) -> None:
    st.header("首页概览")
    st.caption("本页只统计当前工作空间中经人工确认的数据，不会自动载入虚构演示 CSV。")
    active = current_risks(scored, minimum_score=40)
    resolved = resolved_risks(scored)
    opportunities = (
        scored[
            scored["status"].isin(ACTIVE_STATUSES)
            & scored["opportunity_level"].isin(["高", "中"])
        ]
        if not scored.empty
        else scored
    )
    metrics = st.columns(5)
    metrics[0].metric("事件总数", len(scored))
    metrics[1].metric("当前中高风险", len(active))
    metrics[2].metric("已解除/结束", len(resolved))
    metrics[3].metric("中高等级商机", len(opportunities))
    metrics[4].metric("数据完整率", f"{quality.completeness_rate:.1f}%")

    if scored.empty:
        st.info("真实数据文件目前为空。请前往“事件管理”新增事件或导入 CSV。")
        return

    left, right = st.columns([3, 2])
    with left:
        st.subheader("最近事件")
        recent = scored.sort_values(
            ["event_date", "collected_at"], ascending=False
        ).head(8)
        _display_events(
            recent,
            [
                "event_date",
                "category",
                "title",
                "status",
                "current_priority_score",
                "opportunity_score",
                "source_url",
            ],
        )
    with right:
        st.subheader("状态分布")
        st.bar_chart(scored["status"].value_counts())
        if quality.error_count:
            st.error(f"当前有 {quality.error_count} 项必须修复的数据错误。")
        elif quality.warning_count:
            st.warning(f"当前有 {quality.warning_count} 项数据警告需要复核。")
        else:
            st.success("当前数据未发现错误或警告。")


def _render_current_risks(scored: pd.DataFrame, minimum_score: int) -> None:
    st.header("当前风险")
    st.caption("规则分数仅用于公开信息排序，不是青岛港官方风险等级。")
    active = current_risks(scored, minimum_score=minimum_score)
    resolved = resolved_risks(scored)
    tab_active, tab_resolved, tab_recent, tab_timeline = st.tabs(
        ["当前仍有效", "已解除", "最近更新", "关联事件时间线"]
    )

    with tab_active:
        if active.empty:
            st.info("当前没有达到筛选阈值的有效风险。")
        for _, row in active.iterrows():
            pending = "｜待核实" if row["status"] == "待核实" else ""
            with st.expander(
                f"{row['risk_level']}风险｜{row['current_priority_score']}分｜{row['title']}{pending}",
                expanded=row["risk_level"] == "高",
            ):
                if row["status"] == "待核实":
                    st.warning("该事件尚待核实，评分已降低可信度和当前优先级。")
                st.write(row["summary"])
                st.markdown(f"**潜在影响：** {row['impact']}")
                st.markdown(f"**建议动作：** {row['action']}")
                st.markdown(f"**评分解释：** {row['score_explanation']}")
                st.caption(
                    f"事件ID：{row['event_id']}｜状态：{row['status']}｜"
                    f"历史风险分：{row['historical_risk_score']}｜命中：{row['matched_terms']}"
                )
                _source_link(row)

    with tab_resolved:
        st.caption("解除和已结束事件的当前优先分为 0，仅保留历史风险分与关联记录。")
        _display_events(
            resolved,
            [
                "event_date",
                "event_id",
                "title",
                "status",
                "related_event_id",
                "historical_risk_score",
                "current_priority_score",
                "source_url",
            ],
        )

    with tab_recent:
        recent = (
            scored.sort_values(["event_date", "collected_at"], ascending=False).head(15)
            if not scored.empty
            else scored
        )
        _display_events(
            recent,
            [
                "event_date",
                "event_id",
                "category",
                "title",
                "status",
                "current_priority_score",
                "source_url",
            ],
        )

    with tab_timeline:
        if scored.empty:
            st.info("暂无事件，不能生成关联时间线。")
        else:
            labels = {
                row["event_id"]: _event_label(row) for _, row in scored.iterrows()
            }
            selected = st.selectbox(
                "选择一个事件",
                list(labels),
                format_func=lambda event_id: labels[event_id],
                key="timeline_event",
            )
            timeline = related_timeline(scored, selected)
            if len(timeline) == 1:
                st.info("该事件尚无关联更新、解除或恢复记录。")
            for _, row in timeline.iterrows():
                st.markdown(
                    f"**{_valid_date(row['event_date']).isoformat()}｜{row['status']}｜{row['title']}**  \n"
                    f"事件ID：`{row['event_id']}`｜关联：`{row['related_event_id'] or '无'}`｜"
                    f"当前优先分：{row['current_priority_score']}｜历史风险分：{row['historical_risk_score']}"
                )
                _source_link(row)
                st.divider()


def _render_opportunities(scored: pd.DataFrame) -> None:
    st.header("政策与商机")
    opportunities = (
        scored[
            scored["status"].isin(ACTIVE_STATUSES)
            & scored["opportunity_level"].isin(["高", "中"])
        ].sort_values(["opportunity_score", "event_date"], ascending=[False, False])
        if not scored.empty
        else scored
    )
    _display_events(
        opportunities,
        [
            "event_date",
            "category",
            "title",
            "opportunity_score",
            "opportunity_level",
            "status",
            "source_name",
            "source_url",
            "action",
        ],
    )


def _render_all_events(scored: pd.DataFrame) -> None:
    st.header("全部事件")
    if scored.empty:
        st.info("正式事件库当前为空。")
        st.download_button(
            "导出完整原始 CSV",
            data=csv_bytes(empty_events_dataframe()),
            file_name="events.csv",
            mime="text/csv",
        )
        return
    filters = st.columns(4)
    keyword = filters[0].text_input("标题关键词", key="events_keyword")
    source_options = ["全部"] + sorted(
        value for value in scored["source_name"].astype(str).unique() if value
    )
    source_filter = filters[1].selectbox("来源筛选", source_options, key="events_source")
    category_filter = filters[2].selectbox(
        "类别筛选", ["全部"] + ALLOWED_CATEGORIES, key="events_category"
    )
    status_filter = filters[3].selectbox(
        "状态筛选", ["全部"] + ALLOWED_STATUSES, key="events_status"
    )
    second = st.columns(4)
    risk_filter = second[0].selectbox(
        "风险等级", ["全部", "高", "中", "低"], key="events_risk"
    )
    only_pending = second[1].checkbox("只看待核实", key="events_pending")
    only_duplicate = second[2].checkbox("只看可能重复", key="events_duplicate")
    valid_dates = pd.to_datetime(scored["event_date"], errors="coerce")
    available_dates = valid_dates.dropna()
    default_start = available_dates.min().date() if len(available_dates) else date.today() - timedelta(days=30)
    default_end = available_dates.max().date() if len(available_dates) else date.today()
    date_cols = st.columns(2)
    start_date = date_cols[0].date_input("日期起", default_start, key="events_date_start")
    end_date = date_cols[1].date_input("日期止", default_end, key="events_date_end")

    mask = pd.Series(True, index=scored.index)
    if keyword:
        mask &= scored["title"].astype(str).str.contains(
            keyword, case=False, na=False, regex=False
        )
    if source_filter != "全部":
        mask &= scored["source_name"] == source_filter
    if category_filter != "全部":
        mask &= scored["category"] == category_filter
    if status_filter != "全部":
        mask &= scored["status"] == status_filter
    if risk_filter != "全部":
        mask &= scored["risk_level"] == risk_filter
    if only_pending:
        mask &= scored["status"] == "待核实"
    if only_duplicate:
        duplicate_ids = potential_duplicate_event_ids(scored)
        mask &= scored["event_id"].isin(duplicate_ids)
    mask &= valid_dates.notna() & (valid_dates.dt.date >= start_date) & (
        valid_dates.dt.date <= end_date
    )
    filtered = scored.loc[mask].copy()
    st.caption(f"筛选结果：{len(filtered)} / {len(scored)} 条")
    _display_events(
        filtered,
        [
            "event_id",
            "event_date",
            "category",
            "title",
            "status",
            "related_event_id",
            "raw_risk_score",
            "historical_risk_score",
            "current_priority_score",
            "source_confidence",
            "opportunity_score",
            "source_name",
            "source_url",
        ],
    )
    st.download_button(
        "导出完整原始 CSV",
        data=csv_bytes(scored[STANDARD_FIELDS] if not scored.empty else empty_events_dataframe()),
        file_name="events.csv",
        mime="text/csv",
    )


def _render_new_event(
    raw: pd.DataFrame,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    st.subheader("新增事件")
    with st.form("new_event_form", clear_on_submit=False):
        col1, col2, col3 = st.columns(3)
        event_date = col1.date_input("事件日期 *", value=date.today())
        category = col2.selectbox("类别 *", ALLOWED_CATEGORIES)
        status = col3.selectbox("状态 *", ALLOWED_STATUSES)
        title = st.text_input("标题 *")
        summary = st.text_area("事实摘要 *", help="只写可从公开来源核验的事实。")
        impact = st.text_area("潜在影响 *", help="明确这是分析判断，不要写成已证实事实。")
        area_col, period_col = st.columns(2)
        affected_area = area_col.text_input("受影响区域")
        affected_period = period_col.text_input("受影响时段")
        source_col1, source_col2 = st.columns(2)
        source_name = source_col1.text_input("来源名称 *")
        source_type = source_col2.selectbox("来源类型 *", ALLOWED_SOURCE_TYPES)
        source_url = st.text_input("来源链接 *", placeholder="https://...")
        related_options = [""] + raw["event_id"].tolist()
        related_event_id = st.selectbox(
            "关联原事件 ID",
            related_options,
            help="解除、恢复或更新类事件应选择原事件。",
        )
        analyst_note = st.text_area("分析备注", help="不得写入个人信息或敏感数据。")
        submitted = st.form_submit_button("校验并保存事件", type="primary")

    if submitted:
        values = {
            "event_date": event_date.isoformat(),
            "category": category,
            "title": title,
            "summary": summary,
            "impact": impact,
            "affected_area": affected_area,
            "affected_period": affected_period,
            "source_name": source_name,
            "source_type": source_type,
            "source_url": source_url,
            "status": status,
            "related_event_id": related_event_id,
            "analyst_note": analyst_note,
            "workspace_id": workspace_id,
            "ai_assisted": "否",
            "report_included": "是",
        }
        candidate = _candidate_with_new_event(raw, values)
        report = validate_events(candidate)
        if report.error_count:
            st.error(f"未保存：存在 {report.error_count} 项必须修复的错误。")
            st.dataframe(report.issues_dataframe(), hide_index=True, width="stretch")
        else:
            save_events(candidate, events_path)
            new_id = candidate.iloc[-1]["event_id"]
            if workspace_id:
                log_action(workspace_id, "创建", "event", new_id, {}, db_path)
            if report.warning_count:
                st.warning(f"已保存；同时发现 {report.warning_count} 项警告，请继续复核。")
            else:
                st.success("事件已保存。")
            st.rerun()


def _render_edit_event(
    raw: pd.DataFrame,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    st.subheader("编辑已有事件")
    if raw.empty:
        st.info("暂无可编辑事件。")
        return
    labels = {row["event_id"]: _event_label(row) for _, row in raw.iterrows()}
    selected_id = st.selectbox(
        "选择事件", list(labels), format_func=lambda item: labels[item], key="edit_select"
    )
    row = raw[raw["event_id"] == selected_id].iloc[0]
    related_options = [""] + [
        event_id for event_id in raw["event_id"].tolist() if event_id != selected_id
    ]
    current_related = _value(row, "related_event_id")
    if current_related and current_related not in related_options:
        related_options.append(current_related)

    with st.form(f"edit_event_form_{selected_id}"):
        st.caption(
            f"事件ID：{selected_id}｜采集时间：{_value(row, 'collected_at')}（两者保持不变）"
        )
        col1, col2, col3 = st.columns(3)
        event_date = col1.date_input("事件日期 *", _valid_date(row["event_date"]))
        category = col2.selectbox(
            "类别 *",
            ALLOWED_CATEGORIES,
            index=_option_index(ALLOWED_CATEGORIES, _value(row, "category")),
        )
        status = col3.selectbox(
            "状态 *",
            ALLOWED_STATUSES,
            index=_option_index(ALLOWED_STATUSES, _value(row, "status")),
        )
        title = st.text_input("标题 *", _value(row, "title"))
        summary = st.text_area("事实摘要 *", _value(row, "summary"))
        impact = st.text_area("潜在影响 *", _value(row, "impact"))
        area_col, period_col = st.columns(2)
        affected_area = area_col.text_input("受影响区域", _value(row, "affected_area"))
        affected_period = period_col.text_input("受影响时段", _value(row, "affected_period"))
        source_col1, source_col2 = st.columns(2)
        source_name = source_col1.text_input("来源名称 *", _value(row, "source_name"))
        source_type = source_col2.selectbox(
            "来源类型 *",
            ALLOWED_SOURCE_TYPES,
            index=_option_index(ALLOWED_SOURCE_TYPES, _value(row, "source_type")),
        )
        source_url = st.text_input("来源链接 *", _value(row, "source_url"))
        related_event_id = st.selectbox(
            "关联原事件 ID",
            related_options,
            index=_option_index(related_options, current_related),
        )
        analyst_note = st.text_area("分析备注", _value(row, "analyst_note"))
        submitted = st.form_submit_button("校验并保存修改", type="primary")

    if submitted:
        updated = raw.copy()
        index = updated.index[updated["event_id"] == selected_id][0]
        values = {
            "event_date": event_date.isoformat(),
            "category": category,
            "status": status,
            "title": title,
            "summary": summary,
            "impact": impact,
            "affected_area": affected_area,
            "affected_period": affected_period,
            "source_name": source_name,
            "source_type": source_type,
            "source_url": source_url,
            "related_event_id": related_event_id,
            "analyst_note": analyst_note,
        }
        for field, value in values.items():
            updated.at[index, field] = str(value).strip()
        updated.at[index, "last_modified_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        report = validate_events(updated)
        if report.error_count:
            st.error(f"未保存：存在 {report.error_count} 项必须修复的错误。")
            st.dataframe(report.issues_dataframe(), hide_index=True, width="stretch")
        else:
            save_events(updated, events_path)
            if workspace_id:
                action_type = "状态变更" if status != _value(row, "status") else "修改"
                log_action(workspace_id, action_type, "event", selected_id, {}, db_path)
            st.success("修改已保存。")
            st.rerun()


def _render_delete_event(
    raw: pd.DataFrame,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    st.subheader("删除事件")
    st.warning("删除会改写本地 events.csv。建议先导出备份；删除前必须明确确认。")
    if raw.empty:
        st.info("暂无可删除事件。")
        return
    labels = {row["event_id"]: _event_label(row) for _, row in raw.iterrows()}
    with st.form("delete_event_form"):
        selected_id = st.selectbox(
            "选择事件", list(labels), format_func=lambda item: labels[item], key="delete_select"
        )
        confirmed = st.checkbox("我确认删除上述事件，并理解该操作会修改本地 CSV。")
        submitted = st.form_submit_button("删除事件")
    if submitted:
        if not confirmed:
            st.error("未删除：请先勾选确认。")
        else:
            delete_event(selected_id, confirmed=True, path=events_path)
            if workspace_id:
                log_action(workspace_id, "删除", "event", selected_id, {}, db_path)
            st.success("事件已删除。")
            st.rerun()


def _render_import_export(
    raw: pd.DataFrame,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    st.subheader("导入、导出与模板")
    export_col, template_col = st.columns(2)
    export_col.download_button(
        "导出完整 CSV",
        data=csv_bytes(raw),
        file_name="events.csv",
        mime="text/csv",
        key="management_export",
    )
    template_data = (
        TEMPLATE_PATH.read_bytes()
        if TEMPLATE_PATH.exists()
        else csv_bytes(empty_events_dataframe())
    )
    template_col.download_button(
        "下载空白录入模板",
        data=template_data,
        file_name="events_template.csv",
        mime="text/csv",
    )

    uploaded = st.file_uploader("选择外部 CSV", type=["csv"], key="import_csv")
    strategy = st.radio(
        "导入方式",
        ["合并到现有数据", "替换现有数据"],
        horizontal=True,
        help="保存前会先运行完整校验；含错误的数据不会写入。",
    )
    if uploaded is None:
        return
    try:
        imported = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        st.error(f"无法读取 CSV：{exc}")
        return

    normalized_import = normalize_events(imported, assign_ids=True)
    if workspace_id:
        normalized_import.loc[
            normalized_import["workspace_id"].eq(""), "workspace_id"
        ] = workspace_id
    candidate = (
        pd.concat([raw, normalized_import], ignore_index=True)
        if strategy == "合并到现有数据"
        else normalized_import
    )
    report = validate_events(candidate)
    _quality_metrics(report)
    _display_events(
        normalized_import.head(20),
        ["event_id", "event_date", "category", "title", "status", "source_url"],
    )
    if report.error_count:
        st.error(f"导入候选数据有 {report.error_count} 项错误，修复后才能保存。")
        st.dataframe(report.issues_dataframe(), hide_index=True, width="stretch")
        return
    if report.warning_count:
        st.warning(f"发现 {report.warning_count} 项警告；允许保存，但建议先复核。")
    if st.button("确认写入 events.csv", type="primary"):
        save_events(candidate, events_path)
        if workspace_id:
            log_action(
                workspace_id,
                "导入",
                "event",
                "batch",
                {"record_count": len(candidate)},
                db_path,
            )
        st.success(f"导入完成，共保存 {len(candidate)} 条事件。")
        st.rerun()


def _render_event_management(
    raw: pd.DataFrame,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    st.header("事件管理")
    st.caption(f"当前工作空间事件文件：{events_path}")
    tabs = st.tabs(["新增", "编辑", "删除", "导入/导出"])
    with tabs[0]:
        _render_new_event(raw, events_path, workspace_id, db_path)
    with tabs[1]:
        _render_edit_event(raw, events_path, workspace_id, db_path)
    with tabs[2]:
        _render_delete_event(raw, events_path, workspace_id, db_path)
    with tabs[3]:
        _render_import_export(raw, events_path, workspace_id, db_path)


def _render_quality(report: ValidationReport) -> None:
    st.header("数据质量")
    _quality_metrics(report)
    counts = st.columns(3)
    counts[0].metric("错误（必须修复）", report.error_count)
    counts[1].metric("警告（允许保存）", report.warning_count)
    counts[2].metric("建议（不影响保存）", report.suggestion_count)
    if not report.issues:
        st.success("未发现数据质量问题。")
        return
    issues = report.issues_dataframe()
    for label in ("错误", "警告", "建议"):
        subset = issues[issues["级别"] == label]
        if len(subset):
            st.subheader(f"{label}（{len(subset)} 项）")
            st.dataframe(subset, hide_index=True, width="stretch")


def _render_report(raw: pd.DataFrame) -> None:
    st.header("周报生成")
    st.caption("报告只纳入所选日期范围内的 events.csv 记录，并提供 Markdown 与独立 HTML。")
    report_title = st.text_input("报告标题", "青岛港公开情报周报")
    analyst = st.text_input("分析人/团队", "PortScope")
    if raw.empty:
        default_start = date.today() - timedelta(days=6)
        default_end = date.today()
    else:
        valid_dates = pd.to_datetime(raw["event_date"], errors="coerce").dropna()
        default_start = valid_dates.min().date() if len(valid_dates) else date.today() - timedelta(days=6)
        default_end = valid_dates.max().date() if len(valid_dates) else date.today()
    start_col, end_col = st.columns(2)
    start_date = start_col.date_input("统计开始日期", default_start)
    end_date = end_col.date_input("统计结束日期", default_end)
    if start_date > end_date:
        st.error("统计开始日期不能晚于结束日期。")
        return

    markdown = build_weekly_report(raw, report_title, start_date, end_date, analyst)
    html = build_html_report(raw, report_title, start_date, end_date, analyst)
    filtered_count = int(
        (
            (pd.to_datetime(raw["event_date"], errors="coerce").dt.date >= start_date)
            & (pd.to_datetime(raw["event_date"], errors="coerce").dt.date <= end_date)
        ).sum()
    ) if not raw.empty else 0
    st.info(f"当前报告将纳入 {filtered_count} 条事件。")
    download_col1, download_col2 = st.columns(2)
    download_col1.download_button(
        "下载 Markdown 周报",
        data=markdown.encode("utf-8"),
        file_name="qingdao_port_weekly_report.md",
        mime="text/markdown",
    )
    download_col2.download_button(
        "下载 HTML 周报",
        data=html.encode("utf-8"),
        file_name="qingdao_port_weekly_report.html",
        mime="text/html",
    )
    with st.expander("预览 Markdown 内容"):
        st.code(markdown, language="markdown")


def _render_sources() -> None:
    st.header("使用说明")
    st.info(
        "只处理用户主动提交的一条公开网页。不会自动定时访问、批量扫描、登录、保存 Cookie、绕过验证码或把正文发送给第三方 API。"
    )
    st.warning("网页提取、分类、状态、重复和关联结果都只是草稿建议；用户必须打开原文核对后才能确认入库。")
    if SOURCES_PATH.exists():
        st.markdown(SOURCES_PATH.read_text(encoding="utf-8"))
    else:
        st.warning("未找到 sources.md。")


def _render_rule_explanation() -> None:
    st.header("规则说明")
    st.info("风险与商机分数只用于公开信息排序，不是官方等级，也不构成准确预测。")
    rules = load_rules()
    st.markdown(
        "规则由类别基础分、风险/解除/商机关键词、来源可信度、状态系数和阈值组成。"
        "解除与已结束事项的当前优先分降为 0，但历史风险分仍保留；待核实事项会降低可信度。"
    )
    with st.expander("查看当前透明规则配置", expanded=False):
        st.json(rules)


DB_PATH = database_path()
DATA_ROOT = data_root()
initialize_database(DB_PATH)
active_workspace = get_active_workspace(DB_PATH)
if active_workspace is None:
    # Productized first launch: create a local default workspace so the user
    # immediately sees the three-step AI/API/source/command onboarding flow.
    active_workspace = create_workspace(
        {
            "workspace_name": "青岛港公开信息监测工作空间",
            "industry": "港口物流与外贸",
            "region": "青岛",
            "default_report_title": "青岛港公开信息情报周报",
            "default_period_days": 7,
            "ai_enabled": False,
        },
        DB_PATH,
        DATA_ROOT,
    )

initialize_platform_database(DB_PATH)
recover_stale_operations(DB_PATH)

release_warning = release_env_warning(APP_DIR)
if release_warning:
    st.error(release_warning)

all_workspaces = load_workspaces(DB_PATH)
active_paths = workspace_paths(str(active_workspace["workspace_id"]), DATA_ROOT)
ensure_data_file(active_paths.events)
ensure_sources_file(active_paths.sources)
ensure_intake_file(active_paths.intakes)
raw_events = load_events(active_paths.events)
scored_events = enrich_dataframe(raw_events)
quality_report = validate_events(raw_events)
source_records = load_sources(active_paths.sources)
intake_records = load_intakes(active_paths.intakes)

st.title("⚓ PortScope｜港航公开信息监测与分析平台")
st.caption("青岛港 / 山东港航公开数据试验场景｜公开信息采集、监测、核验与分析")

with st.sidebar:
    st.header("工作空间")
    workspace_labels = {
        str(item["workspace_id"]): str(item["workspace_name"]) for item in all_workspaces
    }
    selected_workspace = st.selectbox(
        "当前工作空间",
        list(workspace_labels),
        index=list(workspace_labels).index(str(active_workspace["workspace_id"])),
        format_func=lambda item: workspace_labels[item],
        label_visibility="collapsed",
    )
    if selected_workspace != active_workspace["workspace_id"]:
        set_active_workspace(selected_workspace, DB_PATH)
        for key in ("novice_intake_id", "last_report_artifacts", "outcome_edit_id"):
            st.session_state.pop(key, None)
        st.rerun()
    st.header("页面")
    mode = st.session_state.setdefault("ui_mode", "普通模式")
    if mode == "普通模式":
        pending_page = st.session_state.pop("pending_daily_navigation", None)
        if pending_page in DAILY_PAGES:
            st.session_state["daily_navigation"] = pending_page
        page = st.radio(
            "导航",
            DAILY_PAGES,
            label_visibility="collapsed",
            key="daily_navigation",
        )
        with st.expander("高级工具"):
            st.caption("旧版事件、草稿、规则、迁移与开发诊断工具。日常使用无需进入。")
            if st.button("打开高级管理", width="stretch"):
                st.session_state["ui_mode"] = "专业模式"
                st.rerun()
    else:
        advanced_pages = [
            "AI工作台（旧版）", "专业设置", "更新公开数据", "智能问答", "数据审核", "报告中心",
            "首页概览", "今日采集", "采集工作台", "待审核草稿", "当前风险",
            "政策与商机", "全部事件", "事件管理", "数据源管理", "数据质量",
            "周报生成", "规则说明", "使用说明",
        ]
        pending_page = st.session_state.pop("pending_platform_navigation", None)
        if pending_page in advanced_pages:
            st.session_state["professional_navigation"] = pending_page
        page = st.radio(
            "导航",
            advanced_pages,
            label_visibility="collapsed",
            key="professional_navigation",
        )
        if st.button("返回普通模式", width="stretch"):
            st.session_state["ui_mode"] = "普通模式"
            st.rerun()
    st.divider()
    minimum_score = (
        st.slider("当前风险最低优先分", 0, 100, 40)
        if mode == "专业模式" else 40
    )
    st.caption("默认只显示 active 数据；隔离项不会进入首页、RAG或报告。")
    st.caption(f"PortScope {APP_VERSION}")

if page == "工作台":
    render_daily_home(active_workspace, DB_PATH, DATA_ROOT)
elif page == "采集与导入":
    automatic_tab, single_tab = st.tabs(["自动采集", "单份资料导入"])
    with automatic_tab:
        render_platform_update(active_workspace, DB_PATH, DATA_ROOT)
    with single_tab:
        render_single_import(active_workspace, DB_PATH, DATA_ROOT)
elif page == "文档库":
    render_document_library(active_workspace, DB_PATH)
elif page == "事件库":
    render_event_library(active_workspace, DB_PATH)
elif page == "人工审核":
    render_human_review(active_workspace, DB_PATH)
elif page == "证据修复":
    render_evidence_repair(active_workspace, DB_PATH)
elif page == "智能问答":
    render_platform_qa(active_workspace, DB_PATH, DATA_ROOT)
elif page == "报告中心":
    render_platform_reports(active_workspace, active_paths, DB_PATH)
elif page == "商用准备":
    render_commercial_readiness(active_workspace, DB_PATH, APP_DIR)
elif page == "历史记录":
    render_history(active_workspace, DB_PATH, DATA_ROOT)
elif page == "来源与系统设置":
    render_system_check(active_workspace, DB_PATH, DATA_ROOT)
elif page == "AI工作台（旧版）":
    render_ai_workbench(active_workspace, active_paths, DB_PATH, DATA_ROOT)
elif page == "任务与报告":
    render_tasks_and_reports(active_workspace, active_paths, DB_PATH, DATA_ROOT)
elif page == "设置":
    render_agent_settings(active_workspace, active_paths, DB_PATH, DATA_ROOT)
elif page == "首页":
    render_platform_home(active_workspace, DB_PATH, DATA_ROOT)
elif page == "更新公开数据":
    render_platform_update(active_workspace, DB_PATH, DATA_ROOT)
elif page == "智能问答":
    render_platform_qa(active_workspace, DB_PATH, DATA_ROOT)
elif page == "数据审核":
    render_platform_review(active_workspace, DB_PATH)
elif page == "报告中心":
    render_platform_reports(active_workspace, active_paths, DB_PATH)
elif page == "专业设置":
    render_professional_settings(active_workspace, DB_PATH, DATA_ROOT)
elif page == "新手工作台":
    render_novice_workbench(
        active_workspace,
        active_paths,
        raw_events,
        source_records,
        DB_PATH,
    )
    st.divider()
    render_current_outcomes(
        active_workspace,
        active_paths,
        raw_events,
        DB_PATH,
        embedded=True,
    )
elif page == "本期成果":
    render_current_outcomes(active_workspace, active_paths, raw_events, DB_PATH)
elif page == "客户与报告":
    render_clients_and_reports(active_workspace, active_paths, raw_events, DB_PATH)
elif page == "设置":
    render_settings(active_workspace, active_paths, DB_PATH, DATA_ROOT)
elif page == "首页概览":
    _render_overview(scored_events, quality_report)
elif page == "今日采集":
    render_today_dashboard(source_records, intake_records, raw_events)
elif page == "采集工作台":
    render_collection_workbench(
        source_records,
        intake_records,
        raw_events,
        active_paths.intakes,
        active_paths.events,
        str(active_workspace["workspace_id"]),
        DB_PATH,
    )
elif page == "待审核草稿":
    render_draft_inbox(
        intake_records,
        raw_events,
        source_records,
        active_paths.intakes,
        active_paths.events,
        str(active_workspace["workspace_id"]),
        DB_PATH,
    )
elif page == "当前风险":
    _render_current_risks(scored_events, minimum_score)
elif page == "政策与商机":
    _render_opportunities(scored_events)
elif page == "全部事件":
    _render_all_events(scored_events)
elif page == "事件管理":
    _render_event_management(
        raw_events,
        active_paths.events,
        str(active_workspace["workspace_id"]),
        DB_PATH,
    )
elif page == "数据源管理":
    render_source_management(source_records, active_paths.sources)
elif page == "数据质量":
    _render_quality(quality_report)
elif page == "周报生成":
    _render_report(raw_events)
elif page == "规则说明":
    _render_rule_explanation()
else:
    _render_sources()

st.divider()
st.caption("自动提取与规则建议仅供本地审核；不采集个人信息，不保存登录凭据，确认入库前必须核对原文。")
