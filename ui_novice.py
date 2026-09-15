from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
from typing import Callable, Mapping, Optional

import pandas as pd
import streamlit as st

from ai_assistant import ai_available, build_visible_payload, request_deepseek_suggestions
from data_store import ALLOWED_CATEGORIES, ALLOWED_SOURCE_TYPES, ALLOWED_STATUSES
from file_intake import UnsupportedFileType, parse_uploaded_public_file
from intake_analyzer import analyze_draft
from intake_store import (
    AlreadyConfirmedError,
    HighDuplicateError,
    IntakeValidationError,
    SourceVerificationRequired,
    confirm_intake,
    create_intake,
    load_intakes,
)
from risk_engine import calculate_scores
from web_extractor import URLSafetyError, fetch_url, validate_url
from workspace_store import (
    WorkspacePaths,
    create_workspace,
    legacy_csv_counts,
    log_action,
)


SOURCE_CHECK_TEXT = "我已核对原始公开来源，确认事实摘要与原文基本一致。"


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _date_value(value: object, fallback: date = date.today()) -> date:
    parsed = pd.to_datetime(value, errors="coerce")
    return parsed.date() if pd.notna(parsed) else fallback


def deterministic_fact_summary(text: str, limit: int = 280) -> str:
    cleaned = re.sub(r"\s+", " ", _text(text))
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit("。", 1)[0].strip()
    return (cut or cleaned[:limit]).rstrip("，；：") + "。"


def deterministic_impact(scores: Mapping[str, object]) -> str:
    if _text(scores.get("status")) in {"解除", "已结束"}:
        return "相关事项当前影响可能已下降或结束，仍需核对关联原事件及恢复范围。"
    if _text(scores.get("opportunity_level")) in {"高", "中"}:
        return "可能形成政策响应或采购参与机会，需核对适用范围、资格条件和截止时间。"
    if _text(scores.get("risk_level")) in {"高", "中"}:
        return "可能影响相关运输、作业或合规安排，具体范围和时段需结合原文及业务暴露复核。"
    return "暂未发现明确重大影响，建议持续跟踪后续公开更新。"


def analyze_and_save_draft(
    *,
    source_url: str,
    title: str,
    published_date: str,
    source_name: str,
    text: str,
    events: pd.DataFrame,
    intake_path: Path,
    source_id: str = "",
    category_hint: str = "",
    fetch_status: str = "手工粘贴",
    fetch_note: str = "内容由用户主动提交，必须回到原始公开来源核对。",
    reviewer_status: str = "待审核",
    fetched_at: str = "",
    description: str = "",
    canonical_url: str = "",
    http_status: str = "",
) -> tuple[str, dict[str, object]]:
    analysis = analyze_draft(
        title,
        text,
        source_url,
        published_date,
        source_name,
        events,
        category_hint,
    )
    saved = create_intake(
        {
            "source_id": source_id,
            "source_url": source_url,
            "fetched_title": title,
            "fetched_date": published_date,
            "fetched_source_name": source_name,
            "fetched_text": text,
            "fetched_description": description,
            "fetched_canonical_url": canonical_url,
            "fetched_at": fetched_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            "http_status": http_status,
            "fetch_status": fetch_status,
            "fetch_note": fetch_note,
            "category_suggestion": analysis["category_suggestion"],
            "status_suggestion": analysis["status_suggestion"],
            "duplicate_suggestion": analysis["duplicate_suggestion"],
            "related_event_suggestion": analysis["related_event_suggestion"],
            "reviewer_status": reviewer_status,
        },
        path=intake_path,
    )
    return _text(saved.iloc[-1]["intake_id"]), analysis


def confirm_novice_draft(
    intake_id: str,
    event_values: Mapping[str, object],
    *,
    source_checked: bool,
    workspace_id: str,
    paths: WorkspacePaths,
    db_path: Optional[Path] = None,
    ai_assisted: bool = False,
) -> tuple[str, object]:
    values = dict(event_values)
    values["workspace_id"] = workspace_id
    values["ai_assisted"] = "是" if ai_assisted else "否"
    values["report_included"] = "是"
    event_id, report = confirm_intake(
        intake_id,
        values,
        intake_path=paths.intakes,
        events_path=paths.events,
        source_verified=source_checked,
    )
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
        {"intake_id": intake_id, "ai_assisted": ai_assisted},
        db_path,
    )
    return event_id, report


def render_first_launch_wizard(
    db_path: Optional[Path] = None,
    root: Optional[Path] = None,
) -> None:
    st.title("欢迎使用 PortScope")
    st.subheader("用 3 分钟建立第一个公开信息工作空间")
    st.info(
        "本工具帮助您把公开网页或公告整理为可追溯的商业情报报告。所有自动结果先进入草稿，"
        "必须由您核对原文后才能加入正式成果。"
    )
    legacy_counts = legacy_csv_counts(root)
    has_legacy = any(legacy_counts.values())
    with st.form("first_launch_wizard"):
        st.markdown("**1. 创建第一个工作空间**")
        col1, col2 = st.columns(2)
        workspace_name = col1.text_input("工作空间名称 *", "我的公开情报工作空间")
        industry = col2.text_input("关注行业", "港口、物流与外贸")
        region = st.text_input("关注区域", "青岛及相关航运市场")
        st.markdown("**2. 设置报告默认值**")
        report_title = st.text_input("默认报告名称", "公开信息商业情报周报")
        analyst = st.text_input("默认编制名称", "")
        categories = st.multiselect(
            "默认关注类别",
            ALLOWED_CATEGORIES,
            default=["航行警告", "港口作业", "政策监管", "招标采购"],
        )
        period = st.selectbox("默认统计周期", [7, 14, 30, 90], format_func=lambda value: f"最近 {value} 天")
        st.markdown("**3. 可选 AI 辅助**")
        ai_enabled = st.checkbox(
            "启用可选 DeepSeek 辅助（默认关闭；仍需另行配置密钥并逐次确认发送）",
            value=False,
        )
        migrate_legacy = False
        if has_legacy:
            st.markdown("**4. 安全迁移现有 CSV**")
            st.info(
                "检测到旧数据："
                + "，".join(f"{name} {count} 条" for name, count in legacy_counts.items())
                + "。迁移会先备份再复制到新工作空间，不删除原文件。"
            )
            migrate_legacy = st.checkbox("创建后复制现有 CSV 到第一个工作空间", value=True)
        st.caption("关闭 AI 时，网页提取、规则分析、人工审核和全部报告功能仍可正常使用。")
        submitted = st.form_submit_button("创建工作空间并开始第一条采集", type="primary")
    if submitted:
        try:
            create_workspace(
                {
                    "workspace_name": workspace_name,
                    "industry": industry,
                    "region": region,
                    "default_categories": categories,
                    "default_report_title": report_title,
                    "default_analyst": analyst,
                    "default_period_days": period,
                    "ai_enabled": ai_enabled,
                },
                db_path=db_path,
                root=root,
                migrate_legacy=migrate_legacy,
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state["ui_mode"] = "新手模式"
            st.session_state["novice_page"] = "新手工作台"
            st.rerun()


def _render_progress(active_step: int) -> None:
    labels = ["添加公开资料", "确认分析结果", "查看本期成果", "生成客户报告"]
    columns = st.columns(4)
    for index, (column, label) in enumerate(zip(columns, labels), 1):
        if index < active_step:
            column.success(f"{index}. {label}")
        elif index == active_step:
            column.info(f"{index}. {label}")
        else:
            column.caption(f"{index}. {label}")


def _source_options(sources: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    enabled = sources[sources["enabled"] == "是"] if not sources.empty else sources
    ids = [""] + enabled["source_id"].tolist()
    labels = {"": "不关联预设数据源"}
    labels.update(
        {
            _text(row["source_id"]): f"{_text(row['source_name'])}｜{_text(row['category_hint']) or '未设类别'}"
            for _, row in enabled.iterrows()
        }
    )
    return ids, labels


def _source_row(sources: pd.DataFrame, source_id: str) -> dict[str, str]:
    if not source_id or sources.empty:
        return {}
    matches = sources[sources["source_id"] == source_id]
    return matches.iloc[0].to_dict() if len(matches) else {}


def _save_url_submission(
    url: str,
    source_id: str,
    sources: pd.DataFrame,
    events: pd.DataFrame,
    paths: WorkspacePaths,
    fetcher: Callable[..., object] = fetch_url,
) -> tuple[str, str]:
    result = fetcher(url)
    source = _source_row(sources, source_id)
    source_name = _text(getattr(result, "source_name", "")) or _text(source.get("source_name"))
    intake_id, _ = analyze_and_save_draft(
        source_url=_text(getattr(result, "requested_url", url)),
        title=_text(getattr(result, "title", "")),
        published_date=_text(getattr(result, "published_date", "")),
        source_name=source_name,
        text=_text(getattr(result, "text", "")),
        events=events,
        intake_path=paths.intakes,
        source_id=source_id,
        category_hint=_text(source.get("category_hint")),
        fetch_status=_text(getattr(result, "fetch_status", "网络失败")),
        fetch_note=_text(getattr(result, "quality_note", "网页提取未完成。")),
        reviewer_status="待审核" if bool(getattr(result, "ok", False)) else "提取失败",
        fetched_at=_text(getattr(result, "fetched_at", "")),
        description=_text(getattr(result, "description", "")),
        canonical_url=_text(getattr(result, "canonical_url", "")),
        http_status=_text(getattr(result, "http_status", "")),
    )
    return intake_id, _text(getattr(result, "quality_note", ""))


def _render_add_public_material(
    sources: pd.DataFrame,
    events: pd.DataFrame,
    paths: WorkspacePaths,
) -> None:
    st.subheader("1. 添加公开资料")
    st.caption("选择一种入口。点击统一按钮后只会形成待审核草稿，不会直接加入正式成果。")
    mode = st.radio(
        "资料来源",
        ["粘贴公开网页链接", "粘贴公告正文", "上传本地文件"],
        horizontal=True,
        key="novice_input_mode",
    )
    source_ids, source_labels = _source_options(sources)
    with st.form("novice_add_material_form", clear_on_submit=False):
        source_id = st.selectbox(
            "关联已保存来源（可选）",
            source_ids,
            format_func=lambda item: source_labels[item],
        )
        source = _source_row(sources, source_id)
        if mode == "粘贴公开网页链接":
            url = st.text_input("公开网页链接 *", placeholder="https://...")
            title = published_date = source_name = text = ""
            uploaded = None
        elif mode == "粘贴公告正文":
            url = st.text_input("原文链接 *", placeholder="https://...")
            title = st.text_input("标题 *")
            cols = st.columns(2)
            published_date = cols[0].date_input("发布日期 *", date.today()).isoformat()
            source_name = cols[1].text_input("来源名称 *", _text(source.get("source_name")))
            text = st.text_area("公告正文或事实内容 *", height=220)
            uploaded = None
        else:
            st.info("支持 TXT、HTML、CSV 或 DOCX。CSV只读取第一行；暂不解析PDF和图片。")
            uploaded = st.file_uploader("选择本地公开资料文件", type=["txt", "html", "htm", "csv", "docx"])
            url = st.text_input("原文公开链接 *", placeholder="请补充可追溯的公开URL")
            title = st.text_input("标题（可留空，程序会尝试读取）")
            cols = st.columns(2)
            published_date = cols[0].date_input("发布日期", date.today()).isoformat()
            source_name = cols[1].text_input("来源名称", _text(source.get("source_name")))
            text = ""
        submitted = st.form_submit_button("提取并分析", type="primary")
    if not submitted:
        return
    try:
        if mode == "粘贴公开网页链接":
            intake_id, note = _save_url_submission(url, source_id, sources, events, paths)
        else:
            if url:
                validate_url(url, resolve_dns=False)
            if mode == "上传本地文件":
                if uploaded is None:
                    raise ValueError("请先选择一个支持的本地文件。")
                parsed = parse_uploaded_public_file(uploaded.name, uploaded.getvalue())
                title = title.strip() or parsed.title
                source_name = source_name.strip() or parsed.source_name
                text = parsed.text
                note = parsed.note
            else:
                note = "内容由用户手工粘贴，必须打开原文复核。"
            if not title.strip() or not source_name.strip() or not text.strip():
                raise ValueError("标题、来源名称和正文内容不能为空。")
            intake_id, _ = analyze_and_save_draft(
                source_url=url,
                title=title,
                published_date=published_date,
                source_name=source_name,
                text=text,
                events=events,
                intake_path=paths.intakes,
                source_id=source_id,
                category_hint=_text(source.get("category_hint")),
                fetch_status="本地文件" if mode == "上传本地文件" else "手工粘贴",
                fetch_note=note,
                description=deterministic_fact_summary(text),
            )
    except (ValueError, UnsupportedFileType, URLSafetyError) as exc:
        st.error(f"未能形成草稿：{exc}")
        return
    st.session_state["novice_intake_id"] = intake_id
    st.session_state.pop("novice_ai_suggestions", None)
    st.session_state.pop("novice_ai_used", None)
    st.success(f"已完成提取与规则分析。{note}")
    st.rerun()


def _render_ai_assist(row: pd.Series, workspace: Mapping[str, object]) -> None:
    if not bool(workspace.get("ai_enabled")) or not ai_available():
        return
    intake_id = _text(row["intake_id"])
    with st.expander("可选 AI 辅助（不会自动入库）", expanded=False):
        payload = build_visible_payload(_text(row["fetched_text"])[:8000], "生成事实摘要、潜在影响、建议动作和类别建议")
        st.warning("以下公开文本将发送给 DeepSeek。请确认其中不含个人信息、客户内部数据或非公开数据。")
        st.code(payload["public_text"], language=None)
        confirmed = st.checkbox("我确认上方文本来自公开来源，并同意本次发送。", key=f"ai_send_{intake_id}")
        if st.button("生成 AI 辅助初稿", key=f"ai_button_{intake_id}"):
            try:
                result = request_deepseek_suggestions(
                    payload["public_text"], payload["task"], user_confirmed=confirmed
                )
            except PermissionError as exc:
                st.error(str(exc))
            else:
                if result.ok:
                    st.session_state["novice_ai_suggestions"] = result.suggestions
                    st.session_state["novice_ai_used"] = True
                    st.success(result.note)
                    st.rerun()
                else:
                    st.info(result.note)


def _render_review(
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    events: pd.DataFrame,
    sources: pd.DataFrame,
    db_path: Optional[Path],
) -> None:
    intake_id = st.session_state.get("novice_intake_id", "")
    if not intake_id:
        return
    intakes = load_intakes(paths.intakes)
    matches = intakes[intakes["intake_id"] == intake_id]
    if matches.empty:
        st.session_state.pop("novice_intake_id", None)
        return
    row = matches.iloc[0]
    st.divider()
    st.subheader("2. 确认分析结果")
    if row["fetch_note"]:
        if row["reviewer_status"] == "提取失败":
            st.error(row["fetch_note"])
            st.info("请返回上方选择“粘贴公告正文”，仍可继续形成草稿。")
        else:
            st.info(row["fetch_note"])
    if row["source_url"]:
        st.link_button("打开原文", row["source_url"])
    st.warning("自动提取和分析只用于起草。请打开原文逐项核对，尤其是标题、日期、事实摘要和影响判断。")
    with st.expander("查看提取到的正文", expanded=False):
        st.text(_text(row["fetched_text"])[:10000] or "未提取到正文。")

    analysis = analyze_draft(
        _text(row["fetched_title"]),
        _text(row["fetched_text"]),
        _text(row["source_url"]),
        _text(row["fetched_date"]),
        _text(row["fetched_source_name"]),
        events,
        _text(row["category_suggestion"]),
    )
    duplicate = analysis["duplicate_result"]
    if duplicate.level == "高度疑似重复":
        st.error(f"高度疑似重复，当前不能直接确认入库。{duplicate.summary}")
    elif duplicate.level == "可能重复":
        st.warning(duplicate.summary)
    _render_ai_assist(row, workspace)
    ai_values = st.session_state.get("novice_ai_suggestions", {})
    ai_used = bool(st.session_state.get("novice_ai_used"))
    source = _source_row(sources, _text(row["source_id"]))
    source_type_default = _text(source.get("source_type")) or "其他"
    category_default = _text(ai_values.get("category")) or _text(row["category_suggestion"]) or "企业动态"
    if category_default not in ALLOWED_CATEGORIES:
        category_default = "企业动态"
    status_default = _text(row["status_suggestion"]) or "待核实"
    if status_default not in ALLOWED_STATUSES:
        status_default = "待核实"
    score_defaults = calculate_scores(
        {
            "category": category_default,
            "title": row["fetched_title"],
            "summary": row["fetched_text"],
            "impact": "",
            "source_type": source_type_default,
            "status": status_default,
        }
    )
    relation_ids = [candidate.event_id for candidate in analysis["relation_candidates"]]
    related_options = [""] + relation_ids + [
        event_id for event_id in events.get("event_id", pd.Series(dtype=str)).tolist()
        if event_id not in relation_ids
    ]
    summary_default = _text(ai_values.get("summary")) or _text(row["fetched_description"]) or deterministic_fact_summary(row["fetched_text"])
    impact_default = _text(ai_values.get("impact")) or deterministic_impact(score_defaults)
    action_default = _text(ai_values.get("action")) or _text(score_defaults["action"])

    with st.form(f"novice_review_form_{intake_id}"):
        title = st.text_input("标题 *", _text(row["fetched_title"]))
        cols = st.columns(3)
        event_date = cols[0].date_input("发布日期 *", _date_value(row["fetched_date"]))
        source_name = cols[1].text_input("来源名称 *", _text(row["fetched_source_name"]) or _text(source.get("source_name")))
        category = cols[2].selectbox(
            "事件类别 *",
            ALLOWED_CATEGORIES,
            index=ALLOWED_CATEGORIES.index(category_default),
        )
        summary = st.text_area("事实摘要 *", summary_default, height=130)
        impact = st.text_area("潜在影响 *", impact_default, height=100)
        area_col, status_col = st.columns(2)
        affected_area = area_col.text_input("受影响区域")
        status = status_col.selectbox(
            "状态 *", ALLOWED_STATUSES, index=ALLOWED_STATUSES.index(status_default)
        )
        st.text_input("风险等级", _text(score_defaults["risk_level"]), disabled=True)
        recommended_action = st.text_area("建议动作", action_default, height=90)
        source_url = st.text_input("原文链接 *", _text(row["source_url"]))

        with st.expander("高级信息", expanded=False):
            st.text_input("event_id", "入库后自动生成", disabled=True)
            st.text_input("source_id", _text(row["source_id"]) or "未关联", disabled=True)
            related_event_id = st.selectbox("related_event_id", related_options)
            source_type = st.selectbox(
                "来源类型",
                ALLOWED_SOURCE_TYPES,
                index=ALLOWED_SOURCE_TYPES.index(source_type_default)
                if source_type_default in ALLOWED_SOURCE_TYPES else len(ALLOWED_SOURCE_TYPES) - 1,
            )
            st.caption(f"命中关键词：{analysis['matched_terms'] or '无'}")
            st.caption(f"评分解释：{analysis['score_explanation']}")
            st.caption(
                f"分数明细：历史风险 {score_defaults['historical_risk_score']}｜"
                f"当前优先 {score_defaults['current_priority_score']}｜商机 {score_defaults['opportunity_score']}"
            )
            affected_period = st.text_input("受影响时段")
            analyst_note = st.text_area("分析备注", _text(row["reviewer_note"]))

        checked = st.checkbox(SOURCE_CHECK_TEXT)
        submitted = st.form_submit_button("确认加入本期报告", type="primary")
    if not submitted:
        return
    if not checked:
        st.error("未确认：请先勾选原始公开来源核对声明。")
        return
    if duplicate.level == "高度疑似重复":
        st.error("未确认：该草稿高度疑似重复，请返回修改或先处理重复记录。")
        return
    event_values = {
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
        "source_id": _text(row["source_id"]),
        "recommended_action": recommended_action,
        "reviewer_note": analyst_note,
    }
    try:
        event_id, report = confirm_novice_draft(
            intake_id,
            event_values,
            source_checked=checked,
            workspace_id=str(workspace["workspace_id"]),
            paths=paths,
            db_path=db_path,
            ai_assisted=ai_used,
        )
    except IntakeValidationError as exc:
        st.error(f"未确认：存在 {exc.report.error_count} 项必须修复的数据错误。")
        st.dataframe(exc.report.issues_dataframe(), hide_index=True, width="stretch")
    except (AlreadyConfirmedError, HighDuplicateError, SourceVerificationRequired, ValueError) as exc:
        st.error(str(exc))
    else:
        if report.warning_count:
            st.warning(f"已加入本期成果（{event_id}），另有 {report.warning_count} 项警告建议后续复核。")
        else:
            st.success("已确认加入本期报告。")
        for key in ("novice_intake_id", "novice_ai_suggestions", "novice_ai_used"):
            st.session_state.pop(key, None)
        st.rerun()


def render_novice_workbench(
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    events: pd.DataFrame,
    sources: pd.DataFrame,
    db_path: Optional[Path] = None,
) -> None:
    st.header("新手工作台")
    st.write("下一步：添加一条公开网页、公告正文或本地文件。程序分析后，请在同一页面核对并确认。")
    active_step = (
        2
        if st.session_state.get("novice_intake_id")
        else 3
        if not events.empty
        else 1
    )
    _render_progress(active_step)
    _render_add_public_material(sources, events, paths)
    _render_review(workspace, paths, events, sources, db_path)
