from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Optional

import pandas as pd
import streamlit as st

from ai_assistant import (
    ai_available,
    build_visible_payload,
    request_deepseek_suggestions,
)
from commercial_report import (
    ReportOptions,
    build_executive_summary,
    filter_report_events,
    generate_commercial_report,
)
from data_store import (
    ALLOWED_CATEGORIES,
    ALLOWED_STATUSES,
    STANDARD_FIELDS,
    update_event,
)
from data_validator import validate_events
from lifecycle import ACTIVE_STATUSES, current_risks, resolved_risks
from risk_engine import enrich_dataframe
from workspace_store import (
    WorkspacePaths,
    create_client,
    create_workspace,
    load_action_history,
    load_clients,
    load_reports,
    log_action,
    update_client,
    update_workspace,
)


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _download_file(label: str, path: Path, mime: str, key: str) -> None:
    if not path.exists():
        st.warning(f"文件不存在：{path.name}")
        return
    st.download_button(
        label,
        data=path.read_bytes(),
        file_name=path.name,
        mime=mime,
        key=key,
    )


def _simple_event_edit(
    events: pd.DataFrame,
    event_id: str,
    paths: WorkspacePaths,
    workspace_id: str,
    db_path: Optional[Path],
) -> None:
    matches = events[events["event_id"] == event_id]
    if matches.empty:
        return
    row = matches.iloc[0]
    with st.form(f"outcome_edit_{event_id}"):
        st.subheader("编辑本期信息")
        title = st.text_input("标题", _text(row["title"]))
        summary = st.text_area("一句话摘要", _text(row["summary"]), height=100)
        impact = st.text_area("可能影响", _text(row["impact"]), height=90)
        action = st.text_area("建议动作", _text(row.get("recommended_action")), height=80)
        status = st.selectbox(
            "状态",
            ALLOWED_STATUSES,
            index=ALLOWED_STATUSES.index(_text(row["status"]))
            if _text(row["status"]) in ALLOWED_STATUSES else 0,
        )
        save = st.form_submit_button("保存修改", type="primary")
        cancel = st.form_submit_button("返回修改")
    if cancel:
        st.session_state.pop("outcome_edit_id", None)
        st.rerun()
    if save:
        candidate = events.copy()
        index = candidate.index[candidate["event_id"] == event_id][0]
        for field, value in {
            "title": title,
            "summary": summary,
            "impact": impact,
            "recommended_action": action,
            "status": status,
        }.items():
            candidate.at[index, field] = value.strip()
        report = validate_events(candidate[STANDARD_FIELDS])
        if report.error_count:
            st.error(f"未保存：存在 {report.error_count} 项必须修复的数据错误。")
            st.dataframe(report.issues_dataframe(), hide_index=True, width="stretch")
        else:
            update_event(
                event_id,
                {
                    "title": title,
                    "summary": summary,
                    "impact": impact,
                    "recommended_action": action,
                    "status": status,
                },
                path=paths.events,
            )
            action_type = "状态变更" if status != _text(row["status"]) else "修改"
            log_action(workspace_id, action_type, "event", event_id, {}, db_path)
            st.session_state.pop("outcome_edit_id", None)
            st.success("修改已保存。")
            st.rerun()


def render_current_outcomes(
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    events: pd.DataFrame,
    db_path: Optional[Path] = None,
    embedded: bool = False,
) -> None:
    if embedded:
        st.subheader("3. 本期成果")
    else:
        st.header("本期成果")
        st.write("下一步：复核最重要的信息；准备就绪后前往“客户与报告”生成可发送文件。")
    scored = enrich_dataframe(events)
    included = (
        scored[scored["report_included"].fillna("").ne("否")].copy()
        if not scored.empty and "report_included" in scored else scored
    )
    quality = validate_events(events)
    active_mid_high = current_risks(included, minimum_score=40)
    resolved = resolved_risks(included)
    opportunities = (
        included[
            included["status"].isin(ACTIVE_STATUSES)
            & included["opportunity_level"].isin(["高", "中"])
        ] if not included.empty else included
    )
    pending = int((included["status"] == "待核实").sum()) if not included.empty else 0
    first = st.columns(3)
    first[0].metric("本期已确认", len(included))
    first[1].metric("当前中高风险", len(active_mid_high))
    first[2].metric("已解除事项", len(resolved))
    second = st.columns(3)
    second[0].metric("政策及采购机会", len(opportunities))
    second[1].metric("待核实", pending)
    second[2].metric("数据完整率", f"{quality.completeness_rate:.1f}%")

    st.subheader("本期最重要的 5 条信息")
    if included.empty:
        st.info("尚无已确认信息。请先回到“新手工作台”完成第一条采集与核对。")
        return
    important = included.sort_values(
        ["current_priority_score", "opportunity_score", "event_date"],
        ascending=[False, False, False],
    ).head(5)
    for _, row in important.iterrows():
        risk_label = (
            f"{_text(row['risk_level'])}风险"
            if _text(row["risk_level"]) in {"高", "中"}
            else f"{_text(row['opportunity_level'])}商机"
            if _text(row["opportunity_level"]) in {"高", "中"}
            else _text(row["category"])
        )
        with st.container(border=True):
            st.subheader(_text(row["title"]))
            st.caption(f"{_text(row['event_date'])}｜{risk_label}｜状态：{_text(row['status'])}")
            st.write(_text(row["summary"]))
            st.markdown(f"**可能影响：** {_text(row['impact'])}")
            st.markdown(f"**建议动作：** {_text(row['action'])}")
            actions = st.columns([2, 1, 1])
            if _text(row["source_url"]):
                actions[0].link_button("查看原文", _text(row["source_url"]))
            if actions[1].button("编辑", key=f"edit_outcome_{row['event_id']}"):
                st.session_state["outcome_edit_id"] = _text(row["event_id"])
                st.rerun()
            if actions[2].button("移出本期报告", key=f"remove_outcome_{row['event_id']}"):
                update_event(row["event_id"], {"report_included": "否"}, path=paths.events)
                log_action(
                    str(workspace["workspace_id"]),
                    "移出报告",
                    "event",
                    _text(row["event_id"]),
                    {},
                    db_path,
                )
                st.rerun()
    editing = st.session_state.get("outcome_edit_id")
    if editing:
        st.divider()
        _simple_event_edit(
            events,
            editing,
            paths,
            str(workspace["workspace_id"]),
            db_path,
        )


def _client_form(
    workspace_id: str,
    db_path: Optional[Path],
    client: Optional[Mapping[str, object]] = None,
) -> None:
    client = dict(client or {})
    prefix = _text(client.get("client_id")) or "new"
    with st.form(f"client_form_{prefix}"):
        cols = st.columns(2)
        client_name = cols[0].text_input("客户名称 *", _text(client.get("client_name")))
        industry = cols[1].text_input("行业", _text(client.get("industry")))
        region = st.text_input("关注区域", _text(client.get("region")))
        focus_categories = st.multiselect(
            "关注类别",
            ALLOWED_CATEGORIES,
            default=client.get("focus_categories", []),
        )
        keywords_default = "、".join(client.get("focus_keywords", [])) if isinstance(client.get("focus_keywords"), list) else _text(client.get("focus_keywords"))
        focus_keywords = st.text_input("关注关键词（用逗号或顿号分隔）", keywords_default)
        report_title = st.text_input("默认报告名称", _text(client.get("report_title")))
        cols = st.columns(2)
        analyst_name = cols[0].text_input("分析人/团队", _text(client.get("analyst_name")))
        company_name = cols[1].text_input("出具机构名称", _text(client.get("company_name")))
        contact_info = st.text_input(
            "业务联系信息（可选）",
            _text(client.get("contact_info")),
            help="只填写可公开的企业业务联系信息，不要记录私人手机号或个人邮箱。",
        )
        disclaimer = st.text_area("客户专用免责声明", _text(client.get("disclaimer")), height=90)
        enabled = st.checkbox("启用此客户", bool(client.get("enabled", True)))
        submitted = st.form_submit_button("保存客户", type="primary")
    if not submitted:
        return
    values = {
        "client_name": client_name,
        "industry": industry,
        "region": region,
        "focus_categories": focus_categories,
        "focus_keywords": [item.strip() for item in focus_keywords.replace("、", ",").replace("，", ",").split(",") if item.strip()],
        "report_title": report_title,
        "analyst_name": analyst_name,
        "company_name": company_name,
        "contact_info": contact_info,
        "disclaimer": disclaimer,
        "enabled": enabled,
    }
    try:
        if client.get("client_id"):
            update_client(str(client["client_id"]), values, db_path)
        else:
            create_client(workspace_id, values, db_path)
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.success("客户档案已保存。")
        st.rerun()


def render_clients_and_reports(
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    events: pd.DataFrame,
    db_path: Optional[Path] = None,
) -> None:
    st.header("客户与报告")
    clients = load_clients(str(workspace["workspace_id"]), db_path)
    with st.expander("新建客户档案", expanded=not clients):
        _client_form(str(workspace["workspace_id"]), db_path)
    if clients:
        with st.expander("编辑客户档案", expanded=False):
            labels = {str(item["client_id"]): f"{item['client_name']}｜{'启用' if item['enabled'] else '停用'}" for item in clients}
            selected_edit = st.selectbox(
                "选择客户",
                list(labels),
                format_func=lambda item: labels[item],
                key="edit_client_select",
            )
            selected_client = next(item for item in clients if item["client_id"] == selected_edit)
            _client_form(str(workspace["workspace_id"]), db_path, selected_client)

    st.subheader("生成客户报告")
    active_clients = [item for item in clients if item["enabled"]]
    client_ids = [""] + [str(item["client_id"]) for item in active_clients]
    labels = {"": "通用报告，不关联客户"}
    labels.update({str(item["client_id"]): str(item["client_name"]) for item in active_clients})
    selected_id = st.selectbox("客户", client_ids, format_func=lambda item: labels[item])
    client = next((item for item in active_clients if item["client_id"] == selected_id), None)
    period_days = int(workspace.get("default_period_days") or 7)
    start_default = date.today() - timedelta(days=period_days - 1)
    dates = st.columns(2)
    start_date = dates[0].date_input("报告开始日期", start_default)
    end_date = dates[1].date_input("报告结束日期", date.today())
    report_title_default = (
        _text(client.get("report_title")) if client else ""
    ) or _text(workspace.get("default_report_title"))
    report_title = st.text_input("报告名称", report_title_default)
    analyst = st.text_input(
        "编制名称",
        (_text(client.get("analyst_name")) if client else "") or _text(workspace.get("default_analyst")),
    )
    options_cols = st.columns(2)
    focus_only = options_cols[0].checkbox(
        "只包含该客户关注的类别",
        value=bool(client and client.get("focus_categories")),
        disabled=client is None,
    )
    medium_high = options_cols[1].checkbox("只包含中高风险")
    include_opportunities = options_cols[0].checkbox("包含政策和采购机会", value=True)
    show_scores = options_cols[1].checkbox("显示评分明细", value=False)
    include_sources = options_cols[0].checkbox("包含完整来源附录", value=True)
    options = ReportOptions(
        focus_categories_only=focus_only,
        medium_high_only=medium_high,
        include_opportunities=include_opportunities,
        show_score_details=show_scores,
        include_source_appendix=include_sources,
    )
    if start_date > end_date:
        st.error("报告开始日期不能晚于结束日期。")
        return
    preview = filter_report_events(events, start_date, end_date, client, options)
    preview_quality = validate_events(preview[STANDARD_FIELDS] if not preview.empty else preview)
    suggested_summary = build_executive_summary(preview, preview_quality)
    summary_key = f"executive_summary_{selected_id}_{start_date}_{end_date}_{focus_only}_{medium_high}_{include_opportunities}"
    executive_summary = st.text_area(
        "执行摘要（生成前可编辑）",
        value=suggested_summary,
        height=150,
        key=summary_key,
    )
    if bool(workspace.get("ai_enabled")) and ai_available():
        ai_key = f"report_ai_{selected_id}_{start_date}_{end_date}"
        with st.expander("可选 AI 执行摘要润色", expanded=False):
            payload = build_visible_payload(executive_summary, "润色本期执行摘要，只返回 executive_summary")
            st.warning("以下公开信息摘要将发送给 DeepSeek。客户档案和联系信息不会包含在请求中。")
            st.code(payload["public_text"], language=None)
            confirmed = st.checkbox("我确认上方文本均可公开，并同意本次发送。", key=f"{ai_key}_confirm")
            if st.button("生成 AI 辅助初稿", key=f"{ai_key}_button"):
                try:
                    result = request_deepseek_suggestions(
                        payload["public_text"], payload["task"], user_confirmed=confirmed
                    )
                except PermissionError as exc:
                    st.error(str(exc))
                else:
                    if result.ok:
                        st.session_state[ai_key] = result.suggestions.get("executive_summary", "")
                        st.success(result.note)
                    else:
                        st.info(result.note)
            if st.session_state.get(ai_key):
                st.text_area(
                    "AI辅助初稿（核对后可复制到上方执行摘要）",
                    st.session_state[ai_key],
                    height=150,
                    disabled=True,
                )
    st.info(f"当前设置将纳入 {len(preview)} 条已确认信息。")
    if st.button("生成客户报告", type="primary"):
        try:
            artifacts = generate_commercial_report(
                events,
                workspace,
                paths,
                client=client,
                start_date=start_date,
                end_date=end_date,
                report_title=report_title,
                analyst=analyst,
                executive_summary=executive_summary,
                options=options,
                db_path=db_path,
            )
        except Exception as exc:
            st.error(f"报告生成失败，未覆盖已有文件：{exc}")
        else:
            st.session_state["last_report_artifacts"] = {
                "report_id": artifacts.report_id,
                "version": artifacts.version,
                "docx": str(artifacts.docx_path),
                "html": str(artifacts.html_path),
                "xlsx": str(artifacts.xlsx_path),
            }
            st.success(f"报告已生成：{artifacts.report_id} / v{artifacts.version}")

    last = st.session_state.get("last_report_artifacts")
    if last:
        st.subheader("下载报告")
        cols = st.columns(3)
        with cols[0]:
            _download_file("下载 DOCX 报告", Path(last["docx"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "download_docx")
        with cols[1]:
            _download_file("下载 HTML 报告", Path(last["html"]), "text/html", "download_html")
        with cols[2]:
            _download_file("下载 Excel 数据附件", Path(last["xlsx"]), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "download_xlsx")

    history = load_reports(str(workspace["workspace_id"]), db_path)
    if history:
        with st.expander("报告版本历史", expanded=False):
            st.dataframe(
                pd.DataFrame(history)[
                    ["report_id", "version", "report_title", "client_id", "start_date", "end_date", "generated_at", "docx_file", "html_file", "xlsx_file"]
                ],
                hide_index=True,
                width="stretch",
            )


def render_settings(
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    db_path: Optional[Path] = None,
    root: Optional[Path] = None,
) -> None:
    st.header("设置")
    st.subheader("当前工作空间")
    with st.form("workspace_settings"):
        cols = st.columns(2)
        name = cols[0].text_input("工作空间名称", _text(workspace.get("workspace_name")))
        industry = cols[1].text_input("行业", _text(workspace.get("industry")))
        region = st.text_input("区域", _text(workspace.get("region")))
        categories = st.multiselect(
            "默认关注类别",
            ALLOWED_CATEGORIES,
            default=workspace.get("default_categories", []),
        )
        title = st.text_input("默认报告名称", _text(workspace.get("default_report_title")))
        analyst = st.text_input("默认编制名称", _text(workspace.get("default_analyst")))
        period = st.selectbox(
            "默认统计周期",
            [7, 14, 30, 90],
            index=[7, 14, 30, 90].index(int(workspace.get("default_period_days") or 7)),
            format_func=lambda value: f"最近 {value} 天",
        )
        ai_enabled = st.checkbox("启用可选 DeepSeek 辅助", bool(workspace.get("ai_enabled")))
        saved = st.form_submit_button("保存设置", type="primary")
    if saved:
        update_workspace(
            str(workspace["workspace_id"]),
            {
                "workspace_name": name,
                "industry": industry,
                "region": region,
                "default_categories": categories,
                "default_report_title": title,
                "default_analyst": analyst,
                "default_period_days": period,
                "ai_enabled": ai_enabled,
            },
            db_path,
        )
        log_action(str(workspace["workspace_id"]), "修改", "workspace", str(workspace["workspace_id"]), {}, db_path)
        st.success("设置已保存。")
        st.rerun()

    if bool(workspace.get("ai_enabled")):
        if ai_available():
            st.success("AI辅助已启用且检测到本地密钥。每次发送前仍需单独预览并确认。")
        else:
            st.info("AI辅助已启用但未配置密钥；当前继续使用确定性规则和人工填写，不影响任何核心功能。")
            st.caption("如需使用，请回到AI工作台或设置页，在密码输入框中保存本机DeepSeek配置。")
    else:
        st.caption("AI辅助当前关闭。所有核心功能均使用本地确定性规则。")

    with st.expander("创建另一个本地工作空间", expanded=False):
        with st.form("create_additional_workspace"):
            new_name = st.text_input("新工作空间名称 *")
            new_industry = st.text_input("行业", key="new_workspace_industry")
            new_region = st.text_input("区域", key="new_workspace_region")
            create = st.form_submit_button("创建工作空间")
        if create:
            try:
                create_workspace(
                    {
                        "workspace_name": new_name,
                        "industry": new_industry,
                        "region": new_region,
                        "default_categories": [],
                        "default_report_title": "公开信息商业情报报告",
                        "default_period_days": 7,
                        "ai_enabled": False,
                    },
                    db_path,
                    root,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("新工作空间已创建并切换。")
                st.rerun()

    with st.expander("高级存储与操作历史", expanded=False):
        st.caption(f"工作空间ID：{workspace['workspace_id']}")
        st.caption(f"本地目录：{paths.root}")
        history = load_action_history(str(workspace["workspace_id"]), db_path, limit=100)
        if history:
            st.dataframe(
                pd.DataFrame(history)[["occurred_at", "action_type", "entity_type", "entity_id", "details"]],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("暂无操作历史。")
