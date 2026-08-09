from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Optional

import pandas as pd
import streamlit as st

from commercial_readiness import (
    add_pilot_feedback,
    candidate_event_ids_for_period,
    commercial_readiness_metrics,
    human_review_accuracy,
    list_delivery_snapshots,
    list_pilot_feedback,
    mark_delivery_snapshot_delivered,
    preflight_customer_report,
    run_restore_drill,
    save_source_compliance,
)
from platform_db import list_sources, now_iso


def _rate(value: object) -> str:
    if value is None:
        return "无真实样本"
    return f"{float(value):.1%}"


def _metric_row(items: list[tuple[str, object]]) -> None:
    for column, (label, value) in zip(st.columns(len(items)), items):
        column.metric(label, value)


def _gate_value(value: object) -> str:
    if value is None:
        return "无真实样本"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and 0 <= value <= 1:
        return _rate(value)
    return str(value)


def _render_readiness(
    workspace_id: str,
    db_path,
    project_root: Path,
) -> None:
    metrics = commercial_readiness_metrics(
        workspace_id,
        db_path,
        project_root=project_root,
    )
    status = str(metrics["readiness_status"])
    if status == "正式交付可用":
        st.success("当前状态：正式交付可用")
    elif status == "试点可用":
        st.warning("当前状态：试点可用；仍需继续积累至正式交付门槛。")
    else:
        st.error("当前状态：未达到可收费试点门槛。代码测试通过不等于商用就绪。")
    _metric_row(
        [
            ("连续真实运行", f"{metrics['consecutive_run_days']} 天"),
            ("来源成功率", _rate(metrics["source_success_rate"])),
            ("合格正文率", _rate(metrics["qualified_body_rate"])),
            ("有效事件产出率", _rate(metrics["valid_event_yield_rate"])),
        ]
    )
    _metric_row(
        [
            ("真人审核事件", metrics["human_review_event_count"]),
            ("核心事件真人复核率", _rate(metrics["customer_core_human_review_rate"])),
            ("核心证据定位率", _rate(metrics["core_evidence_verbatim_rate"])),
            (
                "关键字段准确率",
                (
                    _rate(metrics["key_field_accuracy"])
                    if metrics["accuracy_sample_sufficient"]
                    else "样本不足"
                ),
            ),
        ]
    )
    _metric_row(
        [
            ("重复事件率", _rate(metrics["duplicate_event_rate"])),
            ("报告按时率", _rate(metrics["report_on_time_rate"])),
            (
                "最近备份",
                str(metrics["last_backup_at"])[:16] if metrics["last_backup_at"] else "尚无",
            ),
            (
                "最近恢复演练",
                str(metrics["last_restore_drill_at"])[:16]
                if metrics["last_restore_drill_at"]
                else "尚未成功",
            ),
        ]
    )
    st.metric(
        "当前客户报告合格事件",
        int(metrics["customer_report_eligible_event_count"]),
    )
    st.subheader("门槛明细")
    mode = st.radio(
        "查看门槛",
        ["可收费试点", "正式长期交付"],
        horizontal=True,
        key="commercial_gate_mode",
    )
    gates = (
        metrics["pilot_gates"] if mode == "可收费试点" else metrics["formal_gates"]
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "状态": "达到" if item["passed"] else "未达到",
                    "门槛": item["name"],
                    "当前真实值": _gate_value(item["actual"]),
                    "目标": item["target"],
                }
                for item in gates
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    if metrics["missing_gates"]:
        st.warning("试点尚缺：" + "；".join(str(item) for item in metrics["missing_gates"]))

    st.subheader("真人审核准确率")
    accuracy = human_review_accuracy(workspace_id, db_path)
    st.caption(
        f"仅统计 human_user / industry_reviewer 的最新逐项审核，共 "
        f"{accuracy['human_review_count']} 条。少于50条时只显示样本不足，不作商用准确率声明。"
    )
    field_frame = pd.DataFrame(
        [
            {
                "字段": field,
                "准确率": _rate(value) if value is not None else "无真实样本",
            }
            for field, value in accuracy["field_accuracy"].items()
        ]
    )
    st.dataframe(field_frame, hide_index=True, width="stretch")
    decisions = accuracy["decision_counts"]
    _metric_row(
        [
            ("直接接受", decisions.get("accept", 0)),
            ("修改后接受", decisions.get("accept_modified", 0)),
            ("驳回", decisions.get("reject", 0)),
            ("待二审", decisions.get("needs_second_review", 0)),
        ]
    )


def _render_preflight(workspace_id: str, db_path) -> None:
    st.subheader("客户报告预检")
    st.caption("预检失败时只能生成内部草稿，不能标记为客户正式版。")
    columns = st.columns(2)
    end = columns[1].date_input("数据结束日期", date.today(), key="commercial_preflight_end")
    start = columns[0].date_input(
        "数据开始日期",
        end - timedelta(days=6),
        key="commercial_preflight_start",
    )
    event_ids = candidate_event_ids_for_period(
        workspace_id,
        db_path,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
    )
    result = preflight_customer_report(
        workspace_id,
        event_ids,
        db_path,
        reference_date=end,
    )
    if result.passed:
        st.success(f"预检通过：{len(result.event_ids)} 条事件可进入正式报告生成。")
    else:
        st.error(
            f"预检未通过：{len(result.blockers)} 项阻断。"
            "报告中心仍可生成明确标记的内部研究草稿。"
        )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "状态": "通过" if item["passed"] else item["severity"],
                    "检查项": item["label"],
                    "问题数": item["count"],
                    "示例": "；".join(str(value) for value in item["details"][:3]),
                }
                for item in result.checks
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    st.subheader("不可变交付快照")
    snapshots = list_delivery_snapshots(workspace_id, db_path)
    if not snapshots:
        st.info("尚无客户正式报告交付快照。内部研究版不会计作客户交付。")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "报告": item["report_id"],
                    "版本": item["report_version"],
                    "客户/试点": item["client_id"] or item["pilot_id"] or "通用",
                    "周期": f"{item['start_date']} 至 {item['end_date']}",
                    "数据截止": item["data_cutoff_at"],
                    "状态": item["delivery_status"],
                    "生成时间": item["generated_at"],
                    "交付时间": item["delivered_at"],
                    "快照哈希": str(item["payload_hash"])[:16],
                }
                for item in snapshots
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    ready = [item for item in snapshots if item["delivery_status"] != "delivered"]
    if ready:
        labels = {
            str(item["snapshot_id"]): (
                f"{item['report_id']} v{item['report_version']}｜{item['generated_at']}"
            )
            for item in ready
        }
        selected = st.selectbox(
            "待交付快照",
            list(labels),
            format_func=lambda value: labels[value],
            key="commercial_delivery_snapshot",
        )
        confirmation = st.checkbox(
            "确认这些不可变文件已实际交付给对应试点",
            key="commercial_delivery_confirm",
        )
        if st.button("记录实际交付", disabled=not confirmation):
            mark_delivery_snapshot_delivered(
                selected,
                workspace_id,
                db_path,
                changed_by="human_user",
            )
            st.success("已记录真实交付时间；快照内容和文件哈希未被修改。")
            st.rerun()


def _render_restore_drill(workspace_id: str, db_path, project_root: Path) -> None:
    st.subheader("安全恢复演练")
    st.caption(
        "恢复目标固定在 output/restore_drills 的独立目录，不覆盖生产数据库。"
        "只有SQLite完整性检查和数量读取真实成功才记录为成功演练。"
    )
    backups = sorted(
        (project_root / "backups").glob("*.zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        st.warning("没有可用备份，请先从系统工具生成工作台备份。")
        return
    selected = st.selectbox(
        "选择备份",
        backups,
        format_func=lambda value: value.name,
        key="commercial_restore_backup",
    )
    confirmed = st.checkbox(
        "确认只恢复到隔离测试目录并执行真实完整性检查",
        key="commercial_restore_confirm",
    )
    if st.button("开始恢复演练", disabled=not confirmed):
        with st.spinner("正在恢复到隔离目录并核对数量……"):
            result = run_restore_drill(
                workspace_id,
                db_path,
                selected,
                project_root=project_root,
                created_by="human_user",
            )
        if result["status"] == "succeeded":
            st.success(f"恢复演练成功：{result['restore_drill_id']}")
            st.code(str(result["report_path"].relative_to(project_root)))
        else:
            st.error("恢复演练失败：" + str(result["error_summary"]))
        st.rerun()


def _render_feedback(workspace_id: str, db_path) -> None:
    st.subheader("客户试点反馈")
    st.caption("只记录你实际收到并手工录入的反馈；系统和AI不会自动生成。")
    with st.form("commercial_feedback"):
        top = st.columns(2)
        customer = top[0].text_input("客户或匿名试点编号")
        report_id = top[1].text_input("报告或提醒编号")
        options = ["未记录", "是", "否", "未知"]
        row1 = st.columns(3)
        viewed = row1[0].selectbox("是否查看", options)
        already_known = row1[1].selectbox("是否原本已知", options)
        action_taken = row1[2].selectbox("是否采取行动", options)
        row2 = st.columns(3)
        time_saved = row2[0].selectbox("是否节省时间", options)
        false_positive = row2[1].selectbox("是否误报", options)
        omission = row2[2].selectbox("客户认为是否有遗漏", options)
        willing = st.selectbox("是否愿意继续使用", options)
        note = st.text_area("反馈备注")
        truthful = st.checkbox("确认这是实际收到的反馈，不是模拟或AI生成")
        submitted = st.form_submit_button("保存真实反馈", disabled=not truthful)
    if submitted:
        try:
            add_pilot_feedback(
                workspace_id,
                db_path,
                customer_label=customer,
                report_or_alert_id=report_id,
                viewed=viewed,
                already_known=already_known,
                action_taken=action_taken,
                time_saved=time_saved,
                false_positive=false_positive,
                omission_reported=omission,
                willing_to_continue=willing,
                feedback_note=note,
                created_by="human_user",
            )
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success("真实试点反馈已保存。")
            st.rerun()
    feedback = list_pilot_feedback(workspace_id, db_path)
    if feedback:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "日期": item["feedback_date"],
                        "客户/试点": item["customer_label"],
                        "报告/提醒": item["report_or_alert_id"],
                        "已查看": item["viewed"],
                        "采取行动": item["action_taken"],
                        "节省时间": item["time_saved"],
                        "误报": item["false_positive"],
                        "遗漏": item["omission_reported"],
                        "愿意继续": item["willing_to_continue"],
                        "备注": item["feedback_note"],
                    }
                    for item in feedback
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("尚未录入任何真实客户反馈。")


def _render_compliance(workspace_id: str, db_path) -> None:
    st.subheader("来源与交付合规清单")
    st.caption(
        "内部采集、客户摘要、短引用、全文再分发和原始数据转售分别控制；"
        "未经明确依据，全文再分发和原始数据转售保持关闭。"
    )
    sources = list_sources(workspace_id, db_path)
    if not sources:
        st.info("尚无来源。")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "来源": item["source_name"],
                    "公开访问": item.get("public_access_status") or "未复核",
                    "访问频率": item.get("access_frequency_compliant") or "未复核",
                    "个人信息": item.get("personal_information_status") or "未复核",
                    "重要数据": item.get("important_data_status") or "未复核",
                    "内部分析": "允许" if item.get("internal_analysis_allowed") else "关闭",
                    "客户摘要": "允许" if item.get("customer_summary_allowed") else "关闭",
                    "必要短引用": "允许" if item.get("short_quote_allowed") else "关闭",
                    "全文再分发": "允许" if item.get("fulltext_redistribution_allowed") else "关闭",
                    "原始数据转售": "允许" if item.get("raw_data_resale_allowed") else "关闭",
                    "复核时间": item.get("compliance_reviewed_at") or "",
                }
                for item in sources
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    by_id = {str(item["source_id"]): item for item in sources}
    selected_id = st.selectbox(
        "复核来源",
        list(by_id),
        format_func=lambda value: str(by_id[value]["source_name"]),
        key="commercial_compliance_source",
    )
    source = by_id[selected_id]
    with st.form("commercial_compliance_form"):
        row = st.columns(2)
        public = row[0].selectbox(
            "是否合法公开访问",
            ["未复核", "公开可访问", "需要登录", "禁止访问"],
            index=["未复核", "公开可访问", "需要登录", "禁止访问"].index(
                str(source.get("public_access_status") or "未复核")
            ),
        )
        frequency = row[1].selectbox(
            "是否尊重访问频率",
            ["未复核", "是", "否"],
            index=["未复核", "是", "否"].index(
                str(source.get("access_frequency_compliant") or "未复核")
            ),
        )
        risk = st.columns(2)
        pii = risk[0].selectbox(
            "个人信息",
            ["未复核", "未发现", "可能涉及", "存在"],
            index=["未复核", "未发现", "可能涉及", "存在"].index(
                str(source.get("personal_information_status") or "未复核")
            ),
        )
        important = risk[1].selectbox(
            "可能的重要数据",
            ["未复核", "未发现", "可能涉及", "存在"],
            index=["未复核", "未发现", "可能涉及", "存在"].index(
                str(source.get("important_data_status") or "未复核")
            ),
        )
        internal = st.columns(2)
        collection = internal[0].checkbox(
            "允许内部采集",
            bool(source.get("internal_collection_allowed", True)),
        )
        analysis = internal[1].checkbox(
            "允许内部分析",
            bool(source.get("internal_analysis_allowed", True)),
        )
        customer = st.columns(2)
        summary = customer[0].checkbox(
            "允许客户事实摘要", bool(source.get("customer_summary_allowed", False))
        )
        quote = customer[1].checkbox(
            "允许必要短引用", bool(source.get("short_quote_allowed", False))
        )
        redistribution = st.columns(2)
        fulltext = redistribution[0].checkbox(
            "允许全文再分发",
            bool(source.get("fulltext_redistribution_allowed", False)),
        )
        resale = redistribution[1].checkbox(
            "允许原始数据转售",
            bool(source.get("raw_data_resale_allowed", False)),
        )
        basis = st.text_area("使用依据", str(source.get("permission_basis") or ""))
        note = st.text_area("复核备注", str(source.get("permission_note") or ""))
        reviewed = st.date_input(
            "复核日期",
            date.today(),
            key="commercial_compliance_date",
        )
        acknowledged = st.checkbox(
            "确认以上结论由本人复核；高风险权限必须有明确依据"
        )
        submitted = st.form_submit_button("保存合规复核", disabled=not acknowledged)
    if submitted:
        try:
            save_source_compliance(
                workspace_id,
                selected_id,
                db_path,
                {
                    "public_access_status": public,
                    "access_frequency_compliant": frequency,
                    "personal_information_status": pii,
                    "important_data_status": important,
                    "internal_collection_allowed": collection,
                    "internal_analysis_allowed": analysis,
                    "customer_summary_allowed": summary,
                    "short_quote_allowed": quote,
                    "fulltext_redistribution_allowed": fulltext,
                    "raw_data_resale_allowed": resale,
                    "permission_basis": basis,
                    "permission_note": note,
                    "compliance_reviewed_at": reviewed.isoformat(),
                },
            )
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success(f"合规复核已保存：{now_iso()}")
            st.rerun()


def render_commercial_readiness(
    workspace: Mapping[str, object],
    db_path,
    project_root: Path,
) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("商用准备")
    st.caption(
        "本页只根据真实运行、真人审核、证据、报告和恢复演练记录判断。"
        "没有数据时显示未达到，不会用测试或模拟数字填充。"
    )
    tabs = st.tabs(
        ["准备度", "报告预检与交付", "恢复演练", "试点反馈", "合规清单"]
    )
    with tabs[0]:
        _render_readiness(workspace_id, db_path, project_root)
    with tabs[1]:
        _render_preflight(workspace_id, db_path)
    with tabs[2]:
        _render_restore_drill(workspace_id, db_path, project_root)
    with tabs[3]:
        _render_feedback(workspace_id, db_path)
    with tabs[4]:
        _render_compliance(workspace_id, db_path)
