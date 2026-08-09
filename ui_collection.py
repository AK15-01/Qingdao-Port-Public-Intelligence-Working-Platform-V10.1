from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from data_store import (
    ALLOWED_CATEGORIES,
    ALLOWED_SOURCE_TYPES,
    ALLOWED_STATUSES,
    DEFAULT_EVENTS_PATH,
)
from intake_analyzer import analyze_draft
from intake_store import (
    AlreadyConfirmedError,
    HighDuplicateError,
    SourceVerificationRequired,
    DEFAULT_INTAKE_PATH,
    IntakeValidationError,
    create_intake,
    confirm_intake,
    mark_intake_ignored,
)
from risk_engine import calculate_scores
from source_store import (
    CHECK_FREQUENCIES,
    DEFAULT_SOURCES_PATH,
    SOURCE_PRIORITIES,
    SOURCE_STATUSES,
    create_source,
    delete_source,
    due_sources,
    empty_sources_dataframe,
    high_priority_unchecked,
    mark_source_checked,
    normalize_sources,
    save_sources,
    set_source_enabled,
    sources_csv_bytes,
    update_source,
    validate_sources,
)
from web_extractor import fetch_url, validate_url, URLSafetyError
from workspace_store import log_action


APP_DIR = Path(__file__).resolve().parent
SOURCES_TEMPLATE_PATH = APP_DIR / "data" / "sources_template.csv"


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _date_value(value: object, fallback: date = date.today()) -> date:
    parsed = pd.to_datetime(value, errors="coerce")
    return parsed.date() if pd.notna(parsed) else fallback


def _index(options: list[str], value: str) -> int:
    return options.index(value) if value in options else 0


def _display_sources(dataframe: pd.DataFrame) -> None:
    if dataframe.empty:
        st.info("暂无数据源。")
        return
    columns = [
        "source_id", "source_name", "source_type", "category_hint", "priority",
        "check_frequency", "enabled", "last_checked_at", "last_status",
        "homepage_url", "list_page_url",
    ]
    st.dataframe(
        dataframe[columns],
        width="stretch",
        hide_index=True,
        column_config={
            "homepage_url": st.column_config.LinkColumn("官网", display_text="打开官网"),
            "list_page_url": st.column_config.LinkColumn("列表页", display_text="打开列表页"),
        },
    )


def render_today_dashboard(
    sources: pd.DataFrame, intakes: pd.DataFrame, events: pd.DataFrame
) -> None:
    st.header("今日采集看板")
    today = date.today()
    enabled = sources[sources["enabled"] == "是"] if not sources.empty else sources
    due = due_sources(sources, today)
    checked_count = 0
    if not sources.empty:
        checked_dates = pd.to_datetime(sources["last_checked_at"], errors="coerce")
        checked_count = int((checked_dates.dt.date == today).sum())
    high_pending = high_priority_unchecked(sources, today)
    pending_count = int((intakes["reviewer_status"] == "待审核").sum()) if not intakes.empty else 0
    failed_count = int((intakes["reviewer_status"] == "提取失败").sum()) if not intakes.empty else 0
    confirmed_today = 0
    if not intakes.empty:
        confirmed_dates = pd.to_datetime(intakes["confirmed_at"], errors="coerce")
        confirmed_today = int((confirmed_dates.dt.date == today).sum())

    first = st.columns(4)
    first[0].metric("已启用数据源", len(enabled))
    first[1].metric("今天应检查", len(due))
    first[2].metric("今天已检查", checked_count)
    first[3].metric("高优先级尚未检查", len(high_pending))
    second = st.columns(3)
    second[0].metric("待审核草稿", pending_count)
    second[1].metric("提取失败", failed_count)
    second[2].metric("今日确认入库", confirmed_today)

    left, right = st.columns([3, 2])
    with left:
        st.subheader("最近 7 天新增事件")
        days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        counts = pd.Series(0, index=[day.isoformat() for day in days], dtype=int)
        if not events.empty:
            event_dates = pd.to_datetime(events["event_date"], errors="coerce").dt.date
            grouped = event_dates.value_counts()
            for day in days:
                counts.loc[day.isoformat()] = int(grouped.get(day, 0))
        st.bar_chart(counts)
    with right:
        st.subheader("今日工作清单")
        st.markdown(
            f"1. 查看高优先级未检查来源（**{len(high_pending)}**）  \n"
            f"2. 处理待审核草稿（**{pending_count}**）  \n"
            f"3. 复核提取失败记录（**{failed_count}**）  \n"
            f"4. 检查待核实事件（**{int((events['status'] == '待核实').sum()) if not events.empty else 0}**）  \n"
            "5. 生成本期周报"
        )
    st.subheader("高优先级尚未检查来源")
    _display_sources(high_pending)


def _source_selector(sources: pd.DataFrame, key: str) -> str:
    enabled = sources[sources["enabled"] == "是"] if not sources.empty else sources
    ids = [""] + enabled["source_id"].tolist()
    labels = {"": "不关联数据源"}
    labels.update(
        {
            row["source_id"]: f"{row['source_name']}｜{row['priority']}｜{row['category_hint']}"
            for _, row in enabled.iterrows()
        }
    )
    return st.selectbox("关联数据源", ids, format_func=lambda item: labels[item], key=key)


def _source_row(sources: pd.DataFrame, source_id: str) -> dict[str, str]:
    if not source_id or sources.empty:
        return {}
    matches = sources[sources["source_id"] == source_id]
    return matches.iloc[0].to_dict() if len(matches) else {}


def _show_analysis(analysis: dict[str, object]) -> None:
    cols = st.columns(3)
    cols[0].metric("分类建议", analysis["category_suggestion"])
    cols[1].metric("状态建议", analysis["status_suggestion"])
    cols[2].metric("规则风险等级", analysis["risk_level"])
    st.caption(
        f"分类命中：{'、'.join(analysis['category_terms']) or '无'}｜"
        f"状态命中：{'、'.join(analysis['status_terms']) or '无'}｜"
        f"风险/商机词：{analysis['matched_terms']}"
    )
    duplicate = analysis["duplicate_result"]
    if duplicate.level == "高度疑似重复":
        st.error(duplicate.summary)
    elif duplicate.level == "可能重复":
        st.warning(duplicate.summary)
    else:
        st.success(duplicate.summary)
    relations = analysis["relation_candidates"]
    if relations:
        st.markdown("**关联事件候选（仅建议，需人工选择）：**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "event_id": item.event_id,
                        "日期": item.event_date,
                        "状态": item.status,
                        "标题": item.title,
                    }
                    for item in relations
                ]
            ),
            hide_index=True,
            width="stretch",
        )


def _render_url_intake(
    sources: pd.DataFrame,
    events: pd.DataFrame,
    intake_path: Path = DEFAULT_INTAKE_PATH,
) -> None:
    st.subheader("单条 URL 提取")
    st.info("只请求本次主动提交的一条公开 URL；不会扫描列表页、登录、保存 Cookie 或绕过访问限制。")
    with st.form("url_extract_form"):
        source_id = _source_selector(sources, "extract_source")
        url = st.text_input("公开公告 URL", placeholder="https://...")
        submitted = st.form_submit_button("执行安全检查并提取", type="primary")
    if submitted:
        result = fetch_url(url)
        st.session_state["last_extraction"] = result.to_dict()
        st.session_state["last_extraction_source_id"] = source_id

    result = st.session_state.get("last_extraction")
    if not result:
        return
    if result["ok"]:
        st.success(f"提取状态：{result['fetch_status']}｜HTTP {result['http_status']}")
    else:
        st.error(f"提取状态：{result['fetch_status']}｜{result['quality_note']}")
    st.markdown(f"**标题：** {result['title'] or '未提取'}")
    st.markdown(f"**发布日期：** {result['published_date'] or '未提取'}")
    st.markdown(f"**发布机构：** {result['source_name'] or '未提取'}")
    st.caption(f"原始 URL：{result['requested_url']}｜最终 URL：{result['final_url'] or '无'}")
    st.caption(f"提取质量：{result['quality_note']}")
    with st.expander("正文纯文本预览", expanded=False):
        st.text(result["text"][:8000] or "未提取到正文，请使用手工粘贴。")

    saved_source_id = st.session_state.get("last_extraction_source_id", "")
    source = _source_row(sources, saved_source_id)
    source_name = result["source_name"] or source.get("source_name", "")
    analysis = analyze_draft(
        result["title"], result["text"], result["requested_url"],
        result["published_date"], source_name, events, source.get("category_hint", ""),
    )
    _show_analysis(analysis)
    if st.button("保存到待审核草稿箱", key="save_extracted_draft", type="primary"):
        create_intake(
            {
                "source_id": saved_source_id,
                "source_url": result["requested_url"],
                "fetched_title": result["title"],
                "fetched_date": result["published_date"],
                "fetched_source_name": source_name,
                "fetched_text": result["text"],
                "fetched_description": result["description"],
                "fetched_canonical_url": result["canonical_url"],
                "fetched_at": result["fetched_at"],
                "http_status": result["http_status"],
                "fetch_status": result["fetch_status"],
                "fetch_note": result["quality_note"],
                "category_suggestion": analysis["category_suggestion"],
                "status_suggestion": analysis["status_suggestion"],
                "duplicate_suggestion": analysis["duplicate_suggestion"],
                "related_event_suggestion": analysis["related_event_suggestion"],
                "reviewer_status": "待审核" if result["ok"] else "提取失败",
            },
            path=intake_path,
        )
        st.session_state.pop("last_extraction", None)
        st.success("已保存为草稿，没有写入正式 events.csv。")
        st.rerun()


def _render_manual_intake(
    sources: pd.DataFrame,
    events: pd.DataFrame,
    intake_path: Path = DEFAULT_INTAKE_PATH,
) -> None:
    st.subheader("手工粘贴备用流程")
    st.caption("适用于 PDF、图片、JavaScript、登录限制、超时或正文结构无法识别的页面。")
    with st.form("manual_intake_form"):
        source_id = _source_selector(sources, "manual_source")
        source_url = st.text_input("原文 URL *", key="manual_url")
        title = st.text_input("标题 *", key="manual_title")
        published_date = st.date_input("发布日期 *", value=date.today(), key="manual_date")
        source_name = st.text_input("来源名称 *", key="manual_source_name")
        text = st.text_area("公告正文或事实摘要 *", height=240, key="manual_text")
        note = st.text_area("草稿备注", help="不要记录个人信息、Cookie、账号或敏感数据。")
        submitted = st.form_submit_button("保存为待审核草稿", type="primary")
    if not submitted:
        return
    try:
        validate_url(source_url, resolve_dns=False)
    except URLSafetyError as exc:
        st.error(f"未保存：URL 被安全规则拒绝：{exc}")
        return
    if not title.strip() or not source_name.strip() or not text.strip():
        st.error("标题、来源名称和正文/摘要均为必填项。")
        return
    source = _source_row(sources, source_id)
    analysis = analyze_draft(
        title, text, source_url, published_date.isoformat(), source_name, events,
        source.get("category_hint", ""),
    )
    create_intake(
        {
            "source_id": source_id,
            "source_url": source_url,
            "fetched_title": title,
            "fetched_date": published_date.isoformat(),
            "fetched_source_name": source_name,
            "fetched_text": text,
            "fetched_description": text if len(text) <= 1200 else "",
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "fetch_status": "手工粘贴",
            "fetch_note": "内容由用户手工粘贴，必须回到原文核对。",
            "category_suggestion": analysis["category_suggestion"],
            "status_suggestion": analysis["status_suggestion"],
            "duplicate_suggestion": analysis["duplicate_suggestion"],
            "related_event_suggestion": analysis["related_event_suggestion"],
            "reviewer_status": "待审核",
            "reviewer_note": note,
        },
        path=intake_path,
    )
    st.success("草稿已保存，尚未写入正式事件库。")
    st.rerun()


def _draft_risk_level(row: pd.Series) -> str:
    return calculate_scores(
        {
            "category": row.get("category_suggestion", "企业动态"),
            "title": row.get("fetched_title", ""),
            "summary": row.get("fetched_text", ""),
            "impact": "",
            "source_type": "其他",
            "status": row.get("status_suggestion", "待核实"),
        }
    )["risk_level"]


def _filter_drafts(intakes: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    if intakes.empty:
        return intakes.copy()
    data = intakes.copy()
    data["规则风险等级"] = data.apply(_draft_risk_level, axis=1)
    columns = st.columns(4)
    keyword = columns[0].text_input("标题关键词", key=f"{key_prefix}_keyword")
    source_options = ["全部"] + sorted(value for value in data["fetched_source_name"].unique() if value)
    source_filter = columns[1].selectbox("来源筛选", source_options, key=f"{key_prefix}_source")
    category_options = ["全部"] + ALLOWED_CATEGORIES
    category_filter = columns[2].selectbox("类别筛选", category_options, key=f"{key_prefix}_category")
    review_options = ["全部", "待审核", "提取失败", "已确认入库", "已忽略"]
    review_filter = columns[3].selectbox("审核状态", review_options, key=f"{key_prefix}_review")
    second = st.columns(4)
    status_filter = second[0].selectbox(
        "状态建议", ["全部"] + ALLOWED_STATUSES, key=f"{key_prefix}_status"
    )
    risk_filter = second[1].selectbox(
        "风险等级", ["全部", "高", "中", "低"], key=f"{key_prefix}_risk"
    )
    only_pending = second[2].checkbox("只看待核实", key=f"{key_prefix}_pending")
    only_duplicate = second[3].checkbox("只看可能重复", key=f"{key_prefix}_duplicate")
    dates = pd.to_datetime(data["fetched_date"], errors="coerce")
    created_dates = pd.to_datetime(data["created_at"], errors="coerce")
    dates = dates.fillna(created_dates)
    valid_dates = dates.dropna()
    default_start = valid_dates.min().date() if len(valid_dates) else date.today() - timedelta(days=30)
    default_end = valid_dates.max().date() if len(valid_dates) else date.today()
    date_cols = st.columns(2)
    start = date_cols[0].date_input("日期起", default_start, key=f"{key_prefix}_date_start")
    end = date_cols[1].date_input("日期止", default_end, key=f"{key_prefix}_date_end")

    mask = pd.Series(True, index=data.index)
    if keyword:
        mask &= data["fetched_title"].str.contains(keyword, case=False, na=False, regex=False)
    if source_filter != "全部":
        mask &= data["fetched_source_name"] == source_filter
    if category_filter != "全部":
        mask &= data["category_suggestion"] == category_filter
    if review_filter != "全部":
        mask &= data["reviewer_status"] == review_filter
    if status_filter != "全部":
        mask &= data["status_suggestion"] == status_filter
    if risk_filter != "全部":
        mask &= data["规则风险等级"] == risk_filter
    if only_pending:
        mask &= data["status_suggestion"] == "待核实"
    if only_duplicate:
        mask &= data["duplicate_suggestion"].str.contains("重复", na=False) & ~data[
            "duplicate_suggestion"
        ].str.startswith("未发现", na=False)
    mask &= dates.notna() & (dates.dt.date >= start) & (dates.dt.date <= end)
    return data.loc[mask].copy()


def _render_draft_review(
    intakes: pd.DataFrame,
    events: pd.DataFrame,
    sources: pd.DataFrame,
    key_prefix: str,
    intake_path: Path = DEFAULT_INTAKE_PATH,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    if intakes.empty:
        st.info("当前筛选范围内没有草稿。")
        return
    labels = {
        row["intake_id"]: f"{row['reviewer_status']}｜{row['fetched_date'] or '日期未知'}｜{row['fetched_title'] or '无标题'}"
        for _, row in intakes.iterrows()
    }
    intake_id = st.selectbox(
        "选择草稿", list(labels), format_func=lambda item: labels[item], key=f"{key_prefix}_select"
    )
    row = intakes[intakes["intake_id"] == intake_id].iloc[0]
    st.caption(
        f"草稿ID：{intake_id}｜抓取状态：{row['fetch_status']}｜审核状态：{row['reviewer_status']}｜"
        f"正式事件ID：{row['confirmed_event_id'] or '无'}"
    )
    if row["source_url"]:
        st.markdown(f"[打开原文]({row['source_url']})")
    if row["fetch_note"]:
        st.info(row["fetch_note"])
    with st.expander("原始提取正文预览", expanded=False):
        st.text(row["fetched_text"][:10000] or "无正文。")

    analysis = analyze_draft(
        row["fetched_title"], row["fetched_text"], row["source_url"], row["fetched_date"],
        row["fetched_source_name"], events, row["category_suggestion"],
    )
    _show_analysis(analysis)
    if row["reviewer_status"] == "已确认入库":
        st.success(f"该草稿已确认入库：{row['confirmed_event_id']}，不能再次确认。")
        return
    if row["reviewer_status"] == "已忽略":
        st.warning("该草稿已忽略并保留历史记录。")
        return

    source = _source_row(sources, row["source_id"])
    source_type_default = source.get("source_type", "其他") or "其他"
    category_default = row["category_suggestion"] if row["category_suggestion"] in ALLOWED_CATEGORIES else "企业动态"
    status_default = row["status_suggestion"] if row["status_suggestion"] in ALLOWED_STATUSES else "待核实"
    suggested_ids = [item.event_id for item in analysis["relation_candidates"]]
    related_options = [""] + suggested_ids + [
        event_id for event_id in events["event_id"].tolist() if event_id not in suggested_ids
    ]
    summary_default = row["fetched_description"] or (
        row["fetched_text"] if len(row["fetched_text"]) <= 1200 else ""
    )
    with st.form(f"{key_prefix}_review_form_{intake_id}"):
        st.warning("自动提取和规则建议只用于草稿。确认前必须打开原文，人工核对全部字段。")
        col1, col2, col3 = st.columns(3)
        event_date = col1.date_input("事件日期 *", _date_value(row["fetched_date"]))
        category = col2.selectbox(
            "类别 *", ALLOWED_CATEGORIES, index=_index(ALLOWED_CATEGORIES, category_default)
        )
        status = col3.selectbox(
            "状态 *", ALLOWED_STATUSES, index=_index(ALLOWED_STATUSES, status_default)
        )
        title = st.text_input("标题 *", row["fetched_title"])
        summary = st.text_area("事实摘要 *", summary_default, height=150)
        impact = st.text_area("潜在影响 *", "", height=120)
        area_col, period_col = st.columns(2)
        affected_area = area_col.text_input("受影响区域")
        affected_period = period_col.text_input("受影响时段")
        source_col1, source_col2 = st.columns(2)
        source_name = source_col1.text_input("来源名称 *", row["fetched_source_name"] or source.get("source_name", ""))
        source_type = source_col2.selectbox(
            "来源类型 *", ALLOWED_SOURCE_TYPES,
            index=_index(ALLOWED_SOURCE_TYPES, source_type_default),
        )
        source_url = st.text_input("来源链接 *", row["source_url"])
        related_event_id = st.selectbox("关联原事件 ID", related_options)
        analyst_note = st.text_area("分析备注", row["reviewer_note"])
        checked_original = st.checkbox("我已打开并核对原文，确认自动提取内容仅作为草稿参考。")
        duplicate_ack = True
        if analysis["duplicate_result"].level != "未发现明显重复":
            duplicate_ack = st.checkbox("我已核对重复候选，仍确认这是需要单独入库的事件。")
        submitted = st.form_submit_button("确认写入正式事件库", type="primary")
    if submitted:
        if not checked_original or not duplicate_ack:
            st.error("未入库：必须完成原文和重复候选核对确认。")
            return
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
            "reviewer_note": analyst_note,
            "workspace_id": workspace_id,
            "ai_assisted": "否",
            "report_included": "是",
        }
        try:
            event_id, report = confirm_intake(
                intake_id,
                values,
                intake_path=intake_path,
                events_path=events_path,
                source_verified=True,
            )
        except IntakeValidationError as exc:
            st.error(f"未入库：存在 {exc.report.error_count} 项必须修复的错误。")
            st.dataframe(exc.report.issues_dataframe(), hide_index=True, width="stretch")
        except AlreadyConfirmedError as exc:
            st.error(str(exc))
        except (HighDuplicateError, SourceVerificationRequired) as exc:
            st.error(str(exc))
        else:
            if workspace_id:
                log_action(
                    workspace_id,
                    "创建",
                    "event",
                    event_id,
                    {"source": "intake", "intake_id": intake_id},
                    db_path,
                )
                log_action(
                    workspace_id,
                    "确认入库",
                    "event",
                    event_id,
                    {"intake_id": intake_id, "ai_assisted": False},
                    db_path,
                )
            if report.warning_count:
                st.warning(f"已入库为 {event_id}；仍有 {report.warning_count} 项警告需要后续复核。")
            else:
                st.success(f"已确认入库：{event_id}")
            st.rerun()

    with st.form(f"{key_prefix}_ignore_form_{intake_id}"):
        ignore_note = st.text_input("忽略原因", key=f"{key_prefix}_ignore_note_{intake_id}")
        ignore_confirm = st.checkbox("确认将草稿标记为已忽略（保留历史，不物理删除）")
        ignore_submit = st.form_submit_button("标记为已忽略")
    if ignore_submit:
        if not ignore_confirm:
            st.error("请先确认忽略操作。")
        else:
            mark_intake_ignored(intake_id, ignore_note, path=intake_path)
            st.success("草稿已标记为已忽略并保留。")
            st.rerun()


def render_collection_workbench(
    sources: pd.DataFrame,
    intakes: pd.DataFrame,
    events: pd.DataFrame,
    intake_path: Path = DEFAULT_INTAKE_PATH,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    st.header("采集工作台")
    st.caption("URL/手工内容 → 本地草稿 → 人工审核 → 校验通过后正式入库。任何提取结果都不会自动进入 events.csv。")
    tabs = st.tabs(["URL 提取", "手工粘贴", "草稿审核"])
    with tabs[0]:
        _render_url_intake(sources, events, intake_path)
    with tabs[1]:
        _render_manual_intake(sources, events, intake_path)
    with tabs[2]:
        pending = intakes[intakes["reviewer_status"].isin(["待审核", "提取失败"])]
        _render_draft_review(
            pending,
            events,
            sources,
            "workbench",
            intake_path,
            events_path,
            workspace_id,
            db_path,
        )


def render_draft_inbox(
    intakes: pd.DataFrame,
    events: pd.DataFrame,
    sources: pd.DataFrame,
    intake_path: Path = DEFAULT_INTAKE_PATH,
    events_path: Path = DEFAULT_EVENTS_PATH,
    workspace_id: str = "",
    db_path: Path | None = None,
) -> None:
    st.header("待审核草稿")
    st.caption("草稿包含网页提取文本，仅用于本地审核；周报不会转载草稿全文。")
    filtered = _filter_drafts(intakes, "drafts")
    if filtered.empty:
        st.info("当前筛选范围内没有草稿。")
        return
    display_columns = [
        "intake_id", "created_at", "fetched_date", "fetched_title", "fetched_source_name",
        "category_suggestion", "status_suggestion", "规则风险等级", "duplicate_suggestion",
        "fetch_status", "reviewer_status", "confirmed_event_id", "source_url",
    ]
    st.dataframe(
        filtered[display_columns],
        hide_index=True,
        width="stretch",
        column_config={"source_url": st.column_config.LinkColumn("原文", display_text="打开原文")},
    )
    st.divider()
    _render_draft_review(
        filtered,
        events,
        sources,
        "drafts",
        intake_path,
        events_path,
        workspace_id,
        db_path,
    )


def _source_values_form(prefix: str, row: dict[str, str] | None = None) -> tuple[dict[str, object], bool]:
    row = row or {}
    col1, col2 = st.columns(2)
    source_name = col1.text_input("来源名称 *", row.get("source_name", ""), key=f"{prefix}_name")
    organization = col2.text_input("机构名称", row.get("organization", ""), key=f"{prefix}_org")
    col3, col4 = st.columns(2)
    source_type = col3.selectbox(
        "来源类型 *", ALLOWED_SOURCE_TYPES,
        index=_index(ALLOWED_SOURCE_TYPES, row.get("source_type", "政府/监管机构")),
        key=f"{prefix}_type",
    )
    category_options = [""] + ALLOWED_CATEGORIES
    category_hint = col4.selectbox(
        "类别提示", category_options,
        index=_index(category_options, row.get("category_hint", "")), key=f"{prefix}_category",
    )
    homepage_url = st.text_input("官网 URL", row.get("homepage_url", ""), key=f"{prefix}_home")
    list_page_url = st.text_input("公告列表页 URL", row.get("list_page_url", ""), key=f"{prefix}_list")
    col5, col6, col7 = st.columns(3)
    region = col5.text_input("地区", row.get("region", ""), key=f"{prefix}_region")
    check_frequency = col6.selectbox(
        "检查频率", CHECK_FREQUENCIES,
        index=_index(CHECK_FREQUENCIES, row.get("check_frequency", "每日")), key=f"{prefix}_freq",
    )
    priority = col7.selectbox(
        "优先级", SOURCE_PRIORITIES,
        index=_index(SOURCE_PRIORITIES, row.get("priority", "中")), key=f"{prefix}_priority",
    )
    enabled = st.selectbox(
        "启用状态", ["是", "否"], index=_index(["是", "否"], row.get("enabled", "是")),
        key=f"{prefix}_enabled",
    )
    notes = st.text_area("备注", row.get("notes", ""), key=f"{prefix}_notes")
    values = {
        "source_name": source_name,
        "organization": organization,
        "source_type": source_type,
        "category_hint": category_hint,
        "homepage_url": homepage_url,
        "list_page_url": list_page_url,
        "region": region,
        "check_frequency": check_frequency,
        "priority": priority,
        "enabled": enabled,
        "last_status": row.get("last_status", "未检查") or "未检查",
        "notes": notes,
    }
    submitted = st.form_submit_button("保存", type="primary")
    return values, submitted


def render_source_management(
    sources: pd.DataFrame,
    sources_path: Path = DEFAULT_SOURCES_PATH,
) -> None:
    st.header("数据源管理")
    st.caption(f"当前工作空间来源清单：{sources_path}。程序不会自动或定时访问这些网址。")
    _display_sources(sources)
    tabs = st.tabs(["新增", "编辑", "检查/启停", "删除", "导入/导出"])
    with tabs[0]:
        with st.form("new_source_form"):
            values, submitted = _source_values_form("new_source")
        if submitted:
            try:
                create_source(values, path=sources_path)
            except ValueError as exc:
                st.error(f"未保存：{exc}")
            else:
                st.success("数据源已保存。")
                st.rerun()

    with tabs[1]:
        if sources.empty:
            st.info("暂无可编辑数据源。")
        else:
            labels = {row["source_id"]: f"{row['source_name']}｜{row['source_id']}" for _, row in sources.iterrows()}
            selected = st.selectbox(
                "选择数据源", list(labels), format_func=lambda item: labels[item], key="edit_source_select"
            )
            row = sources[sources["source_id"] == selected].iloc[0].to_dict()
            with st.form(f"edit_source_form_{selected}"):
                values, submitted = _source_values_form("edit_source", row)
            if submitted:
                try:
                    update_source(selected, values, path=sources_path)
                except ValueError as exc:
                    st.error(f"未保存：{exc}")
                else:
                    st.success("数据源修改已保存。")
                    st.rerun()

    with tabs[2]:
        if sources.empty:
            st.info("暂无数据源。")
        else:
            labels = {row["source_id"]: f"{row['source_name']}｜当前{row['enabled']}｜{row['last_status']}" for _, row in sources.iterrows()}
            selected = st.selectbox(
                "选择数据源", list(labels), format_func=lambda item: labels[item], key="source_action_select"
            )
            selected_row = sources[sources["source_id"] == selected].iloc[0]
            action_cols = st.columns(2)
            new_enabled = selected_row["enabled"] != "是"
            if action_cols[0].button("启用" if new_enabled else "停用", key="toggle_source"):
                set_source_enabled(selected, new_enabled, path=sources_path)
                st.rerun()
            status = action_cols[1].selectbox(
                "本次检查结果", ["正常", "失败", "需人工访问"], key="check_status"
            )
            if st.button("标记本次已检查", type="primary"):
                mark_source_checked(selected, status, path=sources_path)
                st.success("检查时间和状态已记录。")
                st.rerun()

    with tabs[3]:
        if sources.empty:
            st.info("暂无可删除数据源。")
        else:
            labels = {row["source_id"]: f"{row['source_name']}｜{row['source_id']}" for _, row in sources.iterrows()}
            with st.form("delete_source_form"):
                selected = st.selectbox(
                    "选择数据源", list(labels), format_func=lambda item: labels[item], key="delete_source_select"
                )
                confirmed = st.checkbox("我确认删除该数据源；历史草稿中的 source_id 不会被改写。")
                submitted = st.form_submit_button("删除数据源")
            if submitted:
                if not confirmed:
                    st.error("请先确认删除。")
                else:
                    delete_source(selected, confirmed=True, path=sources_path)
                    st.success("数据源已删除。")
                    st.rerun()

    with tabs[4]:
        template = (
            SOURCES_TEMPLATE_PATH.read_bytes()
            if SOURCES_TEMPLATE_PATH.exists()
            else sources_csv_bytes(empty_sources_dataframe())
        )
        cols = st.columns(2)
        cols[0].download_button(
            "导出 sources.csv", sources_csv_bytes(sources), "sources.csv", "text/csv"
        )
        cols[1].download_button(
            "下载空白模板", template, "sources_template.csv", "text/csv"
        )
        uploaded = st.file_uploader("导入 sources.csv", type=["csv"], key="source_import")
        strategy = st.radio("导入方式", ["合并", "替换"], horizontal=True, key="source_import_mode")
        if uploaded is not None:
            try:
                imported = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
                normalized = normalize_sources(imported, assign_ids=True)
                candidate = (
                    pd.concat([sources, normalized], ignore_index=True)
                    if strategy == "合并" else normalized
                )
                errors = validate_sources(candidate)
            except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
                st.error(f"无法读取 CSV：{exc}")
            else:
                _display_sources(normalized.head(20))
                if errors:
                    st.error("；".join(errors))
                elif st.button("确认写入 sources.csv", type="primary"):
                    save_sources(candidate, path=sources_path)
                    st.success("数据源导入完成。")
                    st.rerun()
