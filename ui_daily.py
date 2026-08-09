from __future__ import annotations

from datetime import date, datetime, timedelta
import difflib
import html
from pathlib import Path
import uuid

import pandas as pd
import streamlit as st

from evidence_repair import (
    apply_exact_evidence_repair,
    list_evidence_issues,
    refresh_evidence_candidates,
)
from operation_store import (
    OPERATION_LABELS,
    STATUS_LABELS,
    cleanup_expired_technical_logs,
    clear_home_operations,
    close_operation,
    delete_operation_history,
    export_operation_json,
    home_operations,
    list_operations,
    list_technical_logs,
    request_operation_cancel,
    retry_operation,
)
from platform_db import connect, list_sources, now_iso
from review_service import (
    get_review_record,
    restore_quarantined_record,
    save_event_review,
)


DAILY_PAGES = [
    "工作台",
    "采集与导入",
    "文档库",
    "事件库",
    "人工审核",
    "证据修复",
    "智能问答",
    "报告中心",
    "商用准备",
    "历史记录",
    "来源与系统设置",
]

QA_EXCEL_REPAIR_MESSAGE = (
    "QA Excel读取组件缺失，请运行repair_environment.bat修复openpyxl"
)


def ui_session_id() -> str:
    """Return an intentionally session-scoped identifier, never a business ID."""

    return st.session_state.setdefault("daily_ui_session_id", uuid.uuid4().hex)


def navigate(page: str) -> None:
    st.session_state["pending_daily_navigation"] = page
    st.rerun()


def _metric_cards(items: list[tuple[str, object]]) -> None:
    for column, (label, value) in zip(st.columns(len(items)), items):
        column.metric(label, value)


def _qa_excel_available() -> bool:
    try:
        import openpyxl  # noqa: F401 - explicit optional UI boundary check
    except ModuleNotFoundError as exc:
        if exc.name == "openpyxl":
            return False
        raise
    return True


def _review_provenance_counts() -> dict[str, int] | None:
    if not _qa_excel_available():
        return None
    labels_path = Path(__file__).resolve().parent / "qa" / "real_acceptance_labels.xlsx"
    if not labels_path.exists():
        return {"human": 0, "automated": 0, "pending": 0}
    try:
        frame = pd.read_excel(labels_path, sheet_name="人工标注")
    except ModuleNotFoundError as exc:
        if exc.name == "openpyxl":
            return None
        raise
    except Exception:
        return {"human": 0, "automated": 0, "pending": 0}
    types = frame.get("reviewer_type", pd.Series(["unknown"] * len(frame))).fillna("unknown").astype(str)
    human = int(types.isin(["human_user", "industry_reviewer"]).sum())
    automated = int((~types.isin(["human_user", "industry_reviewer", "unknown", ""])).sum())
    return {"human": human, "automated": automated, "pending": max(0, len(frame) - human)}


def _status_icon(status: str) -> str:
    return {
        "queued": "🕓",
        "running": "🔄",
        "succeeded": "✅",
        "partially_succeeded": "⚠️",
        "failed": "❌",
        "cancelled": "⏹️",
        "archived": "📦",
    }.get(status, "•")


def needs_first_activation_guidance(
    db_path,
    workspace_id: str,
    qa_candidate_count: int,
) -> bool:
    """Read-only condition; never promotes or mutates candidate data."""

    if qa_candidate_count <= 0:
        return False
    with connect(db_path) as connection:
        has_events = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()[0]
        if not has_events:
            return True
        active = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE workspace_id=? AND record_state='active'",
                (workspace_id,),
            ).fetchone()[0]
        )
    return active == 0


def render_daily_home(workspace: dict[str, object], db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    today = date.today().isoformat()
    sources = list_sources(workspace_id, db_path)
    with connect(db_path) as connection:
        summary = connection.execute(
            """SELECT
            (SELECT COUNT(*) FROM documents WHERE workspace_id=? AND record_state='active'
             AND substr(created_at,1,10)=?) AS today_documents,
            (SELECT COUNT(*) FROM events WHERE workspace_id=? AND record_state='active'
             AND substr(created_at,1,10)=?) AS today_events,
            (SELECT COUNT(*) FROM documents WHERE workspace_id=? AND record_state='active'
             AND ai_status='待AI处理') AS ai_pending,
            (SELECT COUNT(*) FROM events WHERE workspace_id=? AND record_state='active'
             AND human_verified=0) AS review_pending,
            (SELECT COUNT(*) FROM event_evidence x JOIN events e ON e.event_id=x.event_id
             WHERE e.workspace_id=? AND e.record_state='active'
             AND x.verification_status!='已验证') AS evidence_issues""",
            (
                workspace_id,
                today,
                workspace_id,
                today,
                workspace_id,
                workspace_id,
                workspace_id,
            ),
        ).fetchone()
        last_success = connection.execute(
            """SELECT finished_at FROM crawl_runs WHERE workspace_id=?
            AND status IN ('完成','成功','部分成功') ORDER BY finished_at DESC LIMIT 1""",
            (workspace_id,),
        ).fetchone()
        quality_todo = int(
            connection.execute(
                """SELECT COUNT(*) FROM documents WHERE workspace_id=?
                AND record_state='quarantined'""",
                (workspace_id,),
            ).fetchone()[0]
        )
        source_failures = int(
            connection.execute(
                """SELECT COUNT(*) FROM sources WHERE workspace_id=? AND enabled=1
                AND consecutive_failures>=2""",
                (workspace_id,),
            ).fetchone()[0]
        )
        blocked_reports = int(
            connection.execute(
                """SELECT COUNT(*) FROM operation_runs WHERE workspace_id=?
                AND operation_type='report_generation' AND status='failed'
                AND home_visible=1""",
                (workspace_id,),
            ).fetchone()[0]
        )
    st.header("今日工作台")
    st.caption("采集、核验、修复证据和生成简报都从这里开始；历史记录已移到独立页面。")
    formal_active_events = int(summary["today_events"] or 0)
    with connect(db_path) as connection:
        formal_active_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE workspace_id=? AND record_state='active'",
                (workspace_id,),
            ).fetchone()[0]
        )
    if formal_active_events == 0:
        if not _qa_excel_available():
            st.warning(QA_EXCEL_REPAIR_MESSAGE)
            st.caption("QA候选数量暂不可读取；工作台其他功能仍可使用。")
        else:
            try:
                from qa_review_store import qa_context
            except ModuleNotFoundError as exc:
                if exc.name == "openpyxl":
                    st.warning(QA_EXCEL_REPAIR_MESSAGE)
                    st.caption("QA候选数量暂不可读取；工作台其他功能仍可使用。")
                else:
                    raise
            else:
                qa = qa_context()
                qa_candidates = len(qa["records"]) if qa else 0
                if needs_first_activation_guidance(db_path, workspace_id, qa_candidates):
                    with st.container(border=True):
                        st.warning(
                            "正式工作区暂无已通过审核的事件。请依次完成人工核验、"
                            "证据修复和受控晋升。"
                        )
                        st.caption(
                            f"隔离QA工作区现有 {qa_candidates} 条候选资料；系统不会自动审核或自动晋升。"
                        )
                        actions = st.columns(3)
                        if actions[0].button("1. 开始审核QA记录", width="stretch"):
                            st.session_state["pending_review_scope"] = "隔离QA人工金标准"
                            navigate("人工审核")
                        if actions[1].button("2. 处理证据问题", width="stretch"):
                            st.session_state["pending_evidence_scope"] = "QA工作区"
                            navigate("证据修复")
                        if actions[2].button("3. 查看待晋升队列", width="stretch"):
                            st.session_state["pending_review_scope"] = "待晋升队列"
                            navigate("人工审核")
    _metric_cards(
        [
            ("今日新增文档", int(summary["today_documents"] or 0)),
            ("今日有效事件", int(summary["today_events"] or 0)),
            ("待AI处理", int(summary["ai_pending"] or 0)),
            ("待人工核验", int(summary["review_pending"] or 0)),
        ]
    )
    _metric_cards(
        [
            ("证据异常", int(summary["evidence_issues"] or 0)),
            ("启用来源", sum(bool(item["enabled"]) and bool(item["crawl_allowed"]) for item in sources)),
            ("最近成功采集", str(last_success["finished_at"])[:16] if last_success else "尚无"),
        ]
    )

    st.subheader("快速操作")
    first = st.columns(4)
    shortcuts = [
        (first[0], "开始采集", "采集与导入", "检查白名单来源并执行增量采集"),
        (first[1], "导入文件或URL", "采集与导入", "导入一份本地资料或一个公开链接"),
        (first[2], "待AI补跑", "采集与导入", "只处理已归档文档，不重复抓网页"),
        (first[3], "证据修复", "证据修复", "处理无法逐字定位的引用"),
    ]
    second = st.columns(4)
    shortcuts.extend(
        [
            (second[0], "人工审核", "人工审核", "逐项对照原文并保留修改痕迹"),
            (second[1], "生成内部简报", "报告中心", "允许明确标记的待核验内容"),
            (second[2], "生成客户报告", "报告中心", "严格执行真人审核与证据门禁"),
            (second[3], "系统检查", "来源与系统设置", "检查数据库、来源、证据和备份"),
        ]
    )
    for column, label, page, help_text in shortcuts:
        with column.container(border=True):
            st.markdown(f"**{label}**")
            st.caption(help_text)
            if st.button(label, key=f"home_{label}", width="stretch"):
                if label.startswith("生成"):
                    st.session_state["pending_report_mode"] = (
                        "内部研究版" if "内部" in label else "客户交付版"
                    )
                navigate(page)
    with st.expander("高级参数"):
        st.caption("参数只在下一次采集或分析时生效；普通使用可以保留默认值。")
        columns = st.columns(3)
        columns[0].date_input("开始日期", date.today() - timedelta(days=7), key="home_advanced_start")
        columns[1].date_input("结束日期", date.today(), key="home_advanced_end")
        columns[2].number_input("最大抓取数量", 1, 20, 3, key="home_advanced_limit")
        st.multiselect(
            "来源选择",
            [str(item["source_id"]) for item in sources],
            format_func=lambda value: next(
                (str(item["source_name"]) for item in sources if str(item["source_id"]) == value),
                value,
            ),
            key="home_advanced_sources",
        )
        st.selectbox("去重策略", ["严格去重（推荐）", "仅URL去重"], key="home_dedup_strategy")
        st.checkbox("调试模式", False, key="home_debug_mode")

    st.subheader("当前任务")
    tasks = home_operations(workspace_id, ui_session_id(), db_path, limit=3)
    if not tasks:
        st.info("当前没有运行中或本次会话刚完成的任务。可从上方选择下一步。")
    for task in tasks:
        with st.container(border=True):
            header, counts, action = st.columns([4, 3, 1])
            header.markdown(
                f"**{_status_icon(str(task['status']))} {task['operation_label']}**  \n"
                f"{task.get('input_summary') or '无输入摘要'}"
            )
            counts.caption(
                f"新增文档 {task.get('documents_created', 0)}｜更新文档 {task.get('documents_updated', 0)}｜"
                f"新增事件 {task.get('events_created', 0)}｜跳过 {task.get('skipped_count', 0)}｜"
                f"失败 {task.get('failed_count', 0)}"
            )
            if task.get("result_summary"):
                st.write(str(task["result_summary"]))
            if str(task["status"]) in {"queued", "running"}:
                total = int(task.get("total_items") or 0)
                completed = int(task.get("completed_items") or 0)
                elapsed_seconds = 0
                try:
                    started = datetime.fromisoformat(
                        str(task.get("started_at") or task.get("created_at") or "")
                    )
                    if started.tzinfo is None:
                        started = started.astimezone()
                    elapsed_seconds = max(
                        0,
                        int((datetime.now().astimezone() - started).total_seconds()),
                    )
                except ValueError:
                    pass
                st.progress(
                    min(1.0, completed / total) if total else 0.0,
                    text=(
                        f"{task.get('current_stage') or task['status_label']}｜"
                        f"{completed}/{total or '?'}｜"
                        f"{task.get('current_item') or '等待处理'}"
                    ),
                )
                st.caption(
                    f"合格 {task.get('success_count', 0)}｜隔离 {task.get('isolated_count', 0)}｜"
                    f"跳过 {task.get('skipped_count', 0)}｜失败 {task.get('failed_count', 0)}｜"
                    f"已运行 {elapsed_seconds} 秒｜"
                    f"最近心跳 {task.get('heartbeat_at') or '尚未开始'}"
                )
                if action.button("取消", key=f"cancel_home_{task['operation_run_id']}"):
                    request_operation_cancel(
                        str(task["operation_run_id"]),
                        workspace_id,
                        db_path,
                    )
                    st.rerun()
            else:
                if action.button("完成并关闭", key=f"close_home_{task['operation_run_id']}"):
                    close_operation(str(task["operation_run_id"]), workspace_id, db_path)
                    st.rerun()
    if tasks and st.button("一键清空当前操作区"):
        clear_home_operations(workspace_id, ui_session_id(), db_path)
        st.success("仅清除了首页展示；文档、事件、审核和持久历史均未删除。")
        st.rerun()

    st.subheader("需要你处理")
    qa_counts = _review_provenance_counts()
    todos = [
        (int(summary["evidence_issues"] or 0), "证据无法逐字定位", "证据修复"),
        (int(summary["review_pending"] or 0), "正式工作区事件待真人核验", "人工审核"),
        (source_failures, "来源连续失败", "来源与系统设置"),
        (quality_todo, "文档处于质量隔离区", "文档库"),
        (blocked_reports, "报告因关键门禁未生成", "历史记录"),
    ]
    if qa_counts is None:
        st.warning(QA_EXCEL_REPAIR_MESSAGE)
        st.caption("未将依赖缺失误报为“没有QA数据”。")
    else:
        todos.insert(
            2,
            (qa_counts["pending"], "QA标签待独立真人核验", "人工审核"),
        )
    visible = [item for item in todos if item[0] > 0]
    if not visible:
        st.success("当前没有必须立即处理的待办。")
    for index, (count, label, page) in enumerate(visible):
        row = st.columns([6, 1])
        row[0].write(f"**{count} 条｜{label}**")
        if row[1].button("去处理", key=f"todo_{index}_{page}"):
            navigate(page)


def render_history(workspace: dict[str, object], db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("历史记录")
    st.caption("业务历史持久化在SQLite中；关闭首页卡片不会删除这里的记录或业务数据。")
    filters = st.columns(5)
    start = filters[0].date_input("开始日期", date.today() - timedelta(days=30), key="history_start")
    end = filters[1].date_input("结束日期", date.today(), key="history_end")
    operation_types = filters[2].multiselect(
        "操作类型",
        list(OPERATION_LABELS),
        format_func=lambda value: OPERATION_LABELS.get(value, value),
    )
    statuses = filters[3].multiselect(
        "状态",
        list(STATUS_LABELS),
        format_func=lambda value: STATUS_LABELS.get(value, value),
    )
    search = filters[4].text_input("任务ID或关键词")
    sources = list_sources(workspace_id, db_path)
    source_map = {str(item["source_id"]): str(item["source_name"]) for item in sources}
    source_id = st.selectbox(
        "来源",
        [""] + list(source_map),
        format_func=lambda value: "全部来源" if not value else source_map[value],
    )
    rows = list_operations(
        workspace_id,
        db_path,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        operation_types=operation_types,
        statuses=statuses,
        source_id=source_id,
        search=search,
    )
    if not rows:
        st.info("没有符合筛选条件的业务历史。")
        return
    display = pd.DataFrame(
        [
            {
                "任务ID": item["operation_run_id"],
                "操作": item["operation_label"],
                "状态": item["status_label"],
                "开始": item.get("started_at") or item.get("created_at"),
                "结束": item.get("finished_at") or "",
                "耗时(秒)": round(int(item.get("duration_ms") or 0) / 1000, 2),
                "新增文档": item.get("documents_created", 0),
                "更新文档": item.get("documents_updated", 0),
                "新增事件": item.get("events_created", 0),
                "跳过": item.get("skipped_count", 0),
                "失败": item.get("failed_count", 0),
            }
            for item in rows
        ]
    )
    st.dataframe(display, hide_index=True, width="stretch")
    labels = {
        str(item["operation_run_id"]): f"{item['operation_label']}｜{item['status_label']}｜{str(item.get('started_at') or item.get('created_at'))[:16]}"
        for item in rows
    }
    selected_id = st.selectbox("查看单次任务", list(labels), format_func=lambda value: labels[value])
    selected = next(item for item in rows if str(item["operation_run_id"]) == selected_id)
    with st.container(border=True):
        st.write(selected.get("result_summary") or "没有结果摘要。")
        if selected.get("error_summary"):
            st.error(str(selected["error_summary"]))
        related = dict(selected.get("metadata") or {})
        safe_related = {
            key: value
            for key, value in related.items()
            if key in {"document_ids", "event_ids", "report_id", "crawl_run_id", "source_ids", "version"}
        }
        if safe_related:
            st.caption("关联业务对象")
            st.json(safe_related)
        st.download_button(
            "导出本次任务日志",
            export_operation_json(selected_id, workspace_id, db_path),
            file_name=f"{selected_id}.json",
            mime="application/json",
        )
        if str(selected["status"]) in {"failed", "partially_succeeded", "cancelled"}:
            if st.button("重新运行失败任务", key=f"retry_{selected_id}"):
                retry_id = retry_operation(selected_id, workspace_id, ui_session_id(), db_path)
                st.success(f"已创建重试任务 {retry_id}。请进入相应业务页面核对参数后执行。")
        delete_confirmed = st.checkbox(
            "我确认只删除这条历史及其技术日志，不删除文档、事件或报告",
            key=f"delete_confirm_{selected_id}",
        )
        if st.button("删除这条历史记录", disabled=not delete_confirmed, key=f"delete_{selected_id}"):
            delete_operation_history(selected_id, workspace_id, db_path, confirmed=True)
            st.rerun()
    with st.expander("技术日志"):
        logs = list_technical_logs(workspace_id, db_path, operation_run_id=selected_id)
        if logs:
            st.dataframe(pd.DataFrame(logs), hide_index=True, width="stretch")
        else:
            st.caption("本任务没有单独保存的技术日志。")
        if selected.get("log_path"):
            from task_runtime import read_task_log

            try:
                worker_log = read_task_log(str(selected["log_path"]))
            except (OSError, ValueError) as exc:
                st.warning(f"Worker日志暂时无法读取：{exc}")
            else:
                st.caption(f"Worker日志：{selected['log_path']}")
                st.code(worker_log or "日志文件存在，但当前没有输出。", language="text")
        cleanup_confirmed = st.checkbox("确认清理已过保留期的技术日志")
        if st.button("清理过期调试日志", disabled=not cleanup_confirmed):
            count = cleanup_expired_technical_logs(workspace_id, db_path, confirmed=True)
            st.success(f"已清理 {count} 条过期技术日志。业务历史未删除。")


def render_single_import(workspace: dict[str, object], db_path, data_root: Path) -> None:
    from local_import_pipeline import import_public_material

    workspace_id = str(workspace["workspace_id"])
    st.subheader("导入一份公开资料")
    st.caption("一次只处理一个URL或一个本地文件；导入结果先进入待审核，不会自动进入客户报告。")
    mode = st.radio(
        "导入方式",
        ["公开网页URL", "粘贴公开正文", "本地TXT/HTML/CSV/DOCX"],
        horizontal=True,
    )
    source_url = st.text_input(
        "原文URL",
        help="本地文件可以稍后补充；缺少有效URL的记录不能进入客户报告。",
    )
    columns = st.columns(3)
    title = columns[0].text_input("标题（可由网页/文件提取）")
    published_at = columns[1].text_input("发布日期（YYYY-MM-DD）")
    source_name = columns[2].text_input("来源名称")
    pasted_text = ""
    filename = ""
    file_bytes = b""
    if mode == "粘贴公开正文":
        pasted_text = st.text_area("公告正文或事实材料", height=240)
    elif mode == "本地TXT/HTML/CSV/DOCX":
        uploaded = st.file_uploader(
            "选择一份本地公开资料",
            type=["txt", "html", "htm", "csv", "docx"],
        )
        if uploaded is not None:
            filename = uploaded.name
            file_bytes = uploaded.getvalue()
    confirmed = st.checkbox("我确认该材料来自公开来源，并会在审核时核对原文")
    disabled = not confirmed or (
        mode == "公开网页URL" and not source_url.strip()
    ) or (
        mode == "粘贴公开正文" and not pasted_text.strip()
    ) or (
        mode == "本地TXT/HTML/CSV/DOCX" and not file_bytes
    )
    if st.button("导入并进行质量检查", type="primary", disabled=disabled):
        try:
            result = import_public_material(
                workspace_id,
                db_path,
                data_root,
                source_url=source_url,
                title=title,
                published_at=published_at,
                source_name=source_name,
                pasted_text=pasted_text,
                filename=filename,
                file_bytes=file_bytes,
                fetch_single_url=mode == "公开网页URL",
                ui_session_id=ui_session_id(),
            )
        except Exception as exc:
            st.error(f"导入未完成：{exc}")
        else:
            st.success(
                f"资料已保存。正文质量：{result['quality_status']}；"
                f"{'已生成待审核事件' if result['event_id'] else '未生成事件'}。"
            )
            if result["quality_issues"]:
                st.warning("；".join(result["quality_issues"]))
            if st.button("进入人工审核", key="import_to_review"):
                navigate("人工审核")


def render_document_library(workspace: dict[str, object], db_path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("文档库")
    state = st.radio("数据范围", ["有效文档", "隔离区"], horizontal=True)
    target = "active" if state == "有效文档" else "quarantined"
    with connect(db_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT d.document_id,d.title,d.publisher,d.published_at,d.fetched_at,
                d.canonical_url,d.quality_status,d.extraction_status,d.ai_status,
                d.record_state,d.quarantine_reason,d.cleaned_text,d.document_version
                FROM documents d WHERE d.workspace_id=? AND d.is_current=1
                AND d.record_state=? ORDER BY d.created_at DESC""",
                (workspace_id, target),
            ).fetchall()
        ]
    if not rows:
        st.info("当前没有此状态的文档。")
        return
    display = pd.DataFrame(rows).rename(
        columns={
            "title": "标题",
            "publisher": "来源",
            "published_at": "发布时间",
            "quality_status": "正文质量",
            "ai_status": "AI状态",
            "quarantine_reason": "隔离原因",
            "canonical_url": "原文",
        }
    )
    st.dataframe(
        display[[column for column in ["标题", "来源", "发布时间", "正文质量", "AI状态", "隔离原因", "原文"] if column in display]],
        hide_index=True,
        width="stretch",
        column_config={"原文": st.column_config.LinkColumn("原文", display_text="打开")},
    )
    if target == "quarantined":
        from warning_quality_transition import (
            confirm_warning_transition,
            list_warning_transition_candidates,
        )

        with st.expander("官方预警PDF模板复评（受控转入待审核）", expanded=False):
            candidates = list_warning_transition_candidates(db_path, workspace_id)
            eligible = [item for item in candidates if item["transition_allowed"]]
            st.caption(
                "这里只处理已通过最新质量规则dry-run的官方预警PDF。"
                "确认后仅进入内部待AI/待人工审核流程，不会创建真人审核、证据通过或客户报告资格。"
            )
            if not eligible:
                st.info("当前没有等待用户确认的模板复评文档。")
            else:
                st.success(f"复评 {len(candidates)} 篇，其中 {len(eligible)} 篇可转入待人工审核。")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "文档": item["document_id"],
                                "标题": item["title"],
                                "发布日期": item["published_at"],
                                "原隔离原因": item["quarantine_reason"],
                                "结构化事实字段": item["structured_fact_fields"],
                                "新结论": item["new_status"],
                            }
                            for item in eligible
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
                options = [str(item["document_id"]) for item in eligible]
                selected_for_transition = st.multiselect(
                    "选择转入待人工审核的文档",
                    options,
                    format_func=lambda value: next(
                        str(item["title"])
                        for item in eligible
                        if str(item["document_id"]) == value
                    ),
                    key="warning_quality_transition_ids",
                )
                transition_confirmed = st.checkbox(
                    "我确认所选文档只转入内部待审核流程；AI、证据和真人审核需后续分别完成",
                    key="warning_quality_transition_confirmed",
                )
                if st.button(
                    "确认转入待人工审核",
                    type="primary",
                    disabled=not (selected_for_transition and transition_confirmed),
                ):
                    try:
                        transition = confirm_warning_transition(
                            db_path,
                            workspace_id,
                            selected_for_transition,
                            confirmed=True,
                            confirmed_by="local_user",
                        )
                    except Exception as exc:
                        st.error(f"状态转换未完成：{exc}")
                    else:
                        st.success(
                            f"已转入 {transition['documents_updated']} 篇；真人审核0，"
                            "客户报告资格0。下一步请单独运行AI补跑。"
                        )
                        st.rerun()
    selected_id = st.selectbox(
        "查看文档",
        [str(item["document_id"]) for item in rows],
        format_func=lambda value: next(str(item["title"]) for item in rows if item["document_id"] == value),
    )
    selected = next(item for item in rows if item["document_id"] == selected_id)
    with st.expander("正文预览", expanded=True):
        st.text_area("清洗正文", str(selected.get("cleaned_text") or ""), height=320, disabled=True)
    if target == "quarantined":
        st.warning(f"隔离原因：{selected.get('quarantine_reason') or '质量门禁未通过'}")
        reviewer = st.text_input("恢复操作人", key=f"restore_reviewer_{selected_id}")
        confirmed = st.checkbox("我已重新运行质量检查并确认恢复", key=f"restore_confirm_{selected_id}")
        if st.button("恢复到质量检查", disabled=not (reviewer and confirmed)):
            try:
                restore_quarantined_record(
                    document_id=selected_id,
                    workspace_id=workspace_id,
                    db_path=db_path,
                    reviewer_type="human_user",
                    reviewer_name=reviewer,
                    confirmed=True,
                )
            except Exception as exc:
                st.error(str(exc))
            else:
                st.success("文档已恢复为待审核状态。原有人工确认不会被继承。")
                st.rerun()


def render_event_library(workspace: dict[str, object], db_path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("事件库")
    state_label = st.radio("数据范围", ["有效事件", "已隔离/驳回"], horizontal=True)
    where = "e.record_state='active'" if state_label == "有效事件" else "e.record_state!='active'"
    with connect(db_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""SELECT e.event_id,e.event_date,e.title,e.summary,e.category,e.status,
                e.risk_level,e.opportunity_level,e.human_verified,e.evidence_verified,
                e.active_for_internal_use,e.eligible_for_customer_report,
                e.review_state,e.record_state,e.quarantine_reason,e.source_name,e.source_url
                FROM events e WHERE e.workspace_id=? AND {where}
                ORDER BY e.event_date DESC,e.created_at DESC""",
                (workspace_id,),
            ).fetchall()
        ]
    if not rows:
        st.info("当前没有此状态的事件。")
        return
    frame = pd.DataFrame(rows)
    keyword = st.text_input("标题关键词")
    if keyword:
        frame = frame[frame["title"].astype(str).str.contains(keyword, case=False, regex=False, na=False)]
    display = frame.rename(
        columns={
            "event_date": "日期",
            "title": "标题",
            "category": "类别",
            "status": "状态",
            "risk_level": "风险",
            "opportunity_level": "商机",
            "human_verified": "真人核验",
            "evidence_verified": "证据状态",
            "active_for_internal_use": "内部可用",
            "eligible_for_customer_report": "客户报告资格",
            "source_name": "来源",
            "source_url": "原文",
            "quarantine_reason": "隔离原因",
        }
    )
    for column in ("真人核验", "证据状态", "内部可用", "客户报告资格"):
        if column in display:
            display[column] = display[column].map(lambda value: "是" if bool(value) else "否")
    st.dataframe(
        display[[column for column in ["日期", "标题", "类别", "状态", "风险", "商机", "真人核验", "证据状态", "内部可用", "客户报告资格", "来源", "隔离原因", "原文"] if column in display]],
        hide_index=True,
        width="stretch",
        column_config={"原文": st.column_config.LinkColumn("原文", display_text="打开")},
    )


def _highlight_source(text: str, evidence: list[dict[str, object]]) -> str:
    verified = [
        item
        for item in evidence
        if str(item.get("verification_status")) == "已验证"
        and int(item.get("start_offset") or -1) >= 0
        and int(item.get("end_offset") or -1) > int(item.get("start_offset") or -1)
    ]
    if not verified:
        return f"<div style='white-space:pre-wrap'>{html.escape(text)}</div>"
    parts: list[str] = []
    cursor = 0
    for item in sorted(verified, key=lambda value: int(value["start_offset"])):
        start = max(cursor, int(item["start_offset"]))
        end = min(len(text), int(item["end_offset"]))
        parts.append(html.escape(text[cursor:start]))
        parts.append(f"<mark>{html.escape(text[start:end])}</mark>")
        cursor = end
    parts.append(html.escape(text[cursor:]))
    return "<div style='white-space:pre-wrap;line-height:1.7'>" + "".join(parts) + "</div>"


def _render_qa_human_review() -> None:
    if not _qa_excel_available():
        st.error(QA_EXCEL_REPAIR_MESSAGE)
        st.info("QA人工标签暂不可读；修复后可继续原审核进度。")
        return
    from qa_review_store import (
        load_review_progress,
        qa_context,
        qa_review_status,
        save_qa_label_review,
        save_review_progress,
    )

    pending_filter = st.session_state.pop("pending_qa_review_filter", None)
    pending_document = st.session_state.pop("pending_qa_review_document", None)
    if pending_filter is not None:
        st.session_state["qa_review_filter"] = pending_filter
    if pending_document is not None:
        st.session_state["qa_review_document"] = pending_document
    context = qa_context()
    if not context:
        st.info("未找到隔离QA数据库或人工标签文件。")
        return
    records = list(context["records"])
    pending = [item for item in records if qa_review_status(item) == "未审核"]
    progress = load_review_progress(context["db_path"], str(context["workspace_id"]))
    st.caption(
        f"隔离QA工作空间：{context['workspace_name']}｜待独立真人核验 {len(pending)} 条。"
        " 保存后会同时写入QA审计链和人工标签文件，不会直接晋升正式数据库。"
    )
    status_options = ["未审核", "接受", "修改后接受", "驳回", "待二审", "正文有问题", "全部"]
    saved_filter = str(progress.get("filter_status") or "未审核")
    if saved_filter not in status_options:
        saved_filter = "未审核"
    selected_filter = st.selectbox(
        "审核状态",
        status_options,
        index=status_options.index(saved_filter),
        key="qa_review_filter",
    )
    visible = records if selected_filter == "全部" else [
        item for item in records if qa_review_status(item) == selected_filter
    ]
    if not visible:
        if not pending:
            st.success("QA标签已全部由独立真人核验。")
        else:
            st.info("当前筛选没有记录，请切换审核状态。")
        return
    stored_document = str(progress.get("current_document_id") or "")
    visible_ids = [str(item["document_id"]) for item in visible]
    selected_index = visible_ids.index(stored_document) if stored_document in visible_ids else 0
    document_id = st.selectbox(
        "选择QA资料",
        visible_ids,
        index=selected_index,
        format_func=lambda value: next(
            str((item.get("document") or {}).get("title") or item.get("正确标题") or "公开资料")
            for item in visible
            if str(item["document_id"]) == value
        ),
        key="qa_review_document",
    )
    position = next(
        (index for index, item in enumerate(records) if str(item["document_id"]) == document_id),
        0,
    )
    navigation = st.columns([1, 2, 1])
    if navigation[0].button("← 上一条", disabled=position == 0, width="stretch"):
        previous_id = str(records[position - 1]["document_id"])
        save_review_progress(
            context["db_path"],
            str(context["workspace_id"]),
            current_document_id=previous_id,
            filter_status="全部",
            reviewer_type=str(progress.get("reviewer_type") or "human_user"),
            reviewer_name=str(progress.get("reviewer_name") or ""),
        )
        st.session_state["pending_qa_review_filter"] = "全部"
        st.session_state["pending_qa_review_document"] = previous_id
        st.rerun()
    navigation[1].markdown(f"<div style='text-align:center'>当前进度：{position + 1}/{len(records)}</div>", unsafe_allow_html=True)
    if navigation[2].button("下一条 →", disabled=position >= len(records) - 1, width="stretch"):
        next_id = str(records[position + 1]["document_id"])
        save_review_progress(
            context["db_path"],
            str(context["workspace_id"]),
            current_document_id=next_id,
            filter_status="全部",
            reviewer_type=str(progress.get("reviewer_type") or "human_user"),
            reviewer_name=str(progress.get("reviewer_name") or ""),
        )
        st.session_state["pending_qa_review_filter"] = "全部"
        st.session_state["pending_qa_review_document"] = next_id
        st.rerun()
    save_review_progress(
        context["db_path"],
        str(context["workspace_id"]),
        current_document_id=document_id,
        filter_status=selected_filter,
        reviewer_type=str(progress.get("reviewer_type") or "human_user"),
        reviewer_name=str(progress.get("reviewer_name") or ""),
        draft=progress.get("draft") if stored_document == document_id else {},
    )
    label = next(item for item in visible if str(item["document_id"]) == document_id)
    document = dict(label.get("document") or {})
    draft = dict(progress.get("draft") or {}) if stored_document == document_id else {}
    event_id = str(document.get("event_id") or "")
    event_record = (
        get_review_record(event_id, str(context["workspace_id"]), context["db_path"])
        if event_id
        else None
    )
    left, right = st.columns([1.15, 1])
    with left:
        st.subheader("原始资料")
        url = str(document.get("canonical_url") or label.get("原文URL") or "")
        if url.startswith(("http://", "https://")):
            st.link_button("打开原文", url)
        st.caption(
            f"{document.get('publisher') or '来源待核对'}｜"
            f"{str(document.get('published_at') or '')[:10] or '日期待核对'}｜"
            f"正文质量：{document.get('quality_status') or '待核对'}"
        )
        st.text_area(
            "清洗原文（只读）",
            str(document.get("cleaned_text") or ""),
            height=430,
            disabled=True,
            key=f"qa_source_{document_id}",
        )
    with right:
        st.subheader("人工金标准与事件")
        with st.expander("查看AI/代理原值"):
            st.json(
                {
                    "标题": document.get("event_title") or document.get("title"),
                    "发布日期": document.get("event_date") or document.get("published_at"),
                    "发布机构": document.get("publisher"),
                    "类别": document.get("category"),
                    "事实摘要": document.get("summary"),
                    "潜在影响": document.get("impact"),
                    "地点": document.get("affected_area"),
                    "原标注类型": label.get("reviewer_type") or "unknown",
                }
            )
        correct_title = st.text_input(
            "正确标题",
            str(draft.get("correct_title") or label.get("正确标题") or document.get("title") or ""),
            key=f"qa_title_{document_id}",
        )
        correct_date = st.text_input(
            "正确发布日期",
            str(draft.get("correct_date") or label.get("正确发布日期") or document.get("published_at") or "")[:10],
            key=f"qa_date_{document_id}",
        )
        publisher = st.text_input(
            "正确发布机构",
            str(draft.get("publisher") or label.get("正确发布机构") or document.get("publisher") or ""),
            key=f"qa_publisher_{document_id}",
        )
        categories = ["航行警告", "海上气象", "港口作业", "航线运价", "政策监管", "招标采购", "企业动态"]
        current_category = str(label.get("正确类别") or document.get("category") or "企业动态")
        category = st.selectbox(
            "正确类别",
            categories,
            index=categories.index(current_category) if current_category in categories else len(categories) - 1,
            key=f"qa_category_{document_id}",
        )
        body_ok = st.radio("正文是否合格", ["是", "否"], horizontal=True, key=f"qa_body_{document_id}")
        mojibake = st.radio("是否包含乱码", ["否", "是"], horizontal=True, key=f"qa_mojibake_{document_id}")
        noise = st.radio("是否包含导航噪声", ["否", "是"], horizontal=True, key=f"qa_noise_{document_id}")
        if event_record:
            summary = st.text_area(
                "事实摘要",
                str(draft.get("summary") or event_record.get("summary") or ""),
                height=110,
                key=f"qa_summary_{document_id}",
            )
            impact = st.text_area(
                "潜在影响（分析）",
                str(draft.get("impact") or event_record.get("impact") or ""),
                height=90,
                key=f"qa_impact_{document_id}",
            )
            area = st.text_input(
                "地点/受影响区域",
                str(draft.get("area") or event_record.get("affected_area") or ""),
                key=f"qa_area_{document_id}",
            )
            evidence_ok = st.radio(
                "evidence_quotes是否存在于原文",
                ["是", "否"],
                index=0 if bool(event_record.get("evidence_verified")) else 1,
                horizontal=True,
                key=f"qa_evidence_{document_id}",
            )
        else:
            st.info("该文档未通过正文门禁，因此没有结构化事件；可以标记正文有问题或驳回。")
            summary = impact = area = ""
            evidence_ok = "否"
        value = st.selectbox("业务价值", ["高", "中", "低", "无关"], key=f"qa_value_{document_id}")
        reviewer_type_label = st.radio("核验人类型", ["项目使用者", "行业复核人"], horizontal=True, key=f"qa_type_{document_id}")
        reviewer_name = st.text_input(
            "核验人姓名或内部代号",
            value=str(draft.get("reviewer_name") or progress.get("reviewer_name") or ""),
            key=f"qa_name_{document_id}",
        )
        opened = st.checkbox("我已打开原文", key=f"qa_opened_{document_id}")
        facts = st.checkbox("我已逐项核对以上字段", key=f"qa_facts_{document_id}")
        note = st.text_area(
            "审核备注或重大事实修改原因",
            value=str(draft.get("note") or ""),
            key=f"qa_note_{document_id}",
        )
        decision_label = st.radio(
            "处理结果",
            ["接受", "修改后接受", "驳回", "正文有问题", "需要第二人复核"],
            key=f"qa_decision_{document_id}",
        )
        decision = {
            "接受": "accept",
            "修改后接受": "accept_modified",
            "驳回": "reject",
            "正文有问题": "body_issue",
            "需要第二人复核": "needs_second_review",
        }[decision_label]
        confirmed = st.checkbox("确认保存本次独立真人核验", key=f"qa_confirm_{document_id}")
        draft_payload = {
            "correct_title": correct_title,
            "correct_date": correct_date,
            "publisher": publisher,
            "category": category,
            "summary": summary,
            "impact": impact,
            "area": area,
            "reviewer_name": reviewer_name,
            "note": note,
            "decision": decision,
        }
        if st.button("暂存当前填写", key=f"qa_draft_{document_id}"):
            save_review_progress(
                context["db_path"],
                str(context["workspace_id"]),
                current_document_id=document_id,
                filter_status=selected_filter,
                reviewer_type="human_user" if reviewer_type_label == "项目使用者" else "industry_reviewer",
                reviewer_name=reviewer_name,
                draft=draft_payload,
            )
            st.success("已暂存填写内容；暂存不计为人工审核完成。")
        if st.button(
            "保存QA人工核验",
            type="primary",
            disabled=not (reviewer_name.strip() and confirmed),
        ):
            try:
                result = save_qa_label_review(
                    document_id,
                    qa_db_path=context["db_path"],
                    labels_path=context["labels_path"],
                    reviewer_type="human_user" if reviewer_type_label == "项目使用者" else "industry_reviewer",
                    reviewer_name=reviewer_name,
                    decision=decision,
                    edits={
                        "正确标题": correct_title,
                        "正确发布日期": correct_date,
                        "正确发布机构": publisher,
                        "正文是否合格": body_ok,
                        "是否包含乱码": mojibake,
                        "是否包含导航噪声": noise,
                        "正确类别": category,
                        "事实摘要是否准确": "是" if event_record else "不适用",
                        "evidence_quotes是否存在于原文": evidence_ok,
                        "潜在影响是否合理": "是" if event_record else "不适用",
                        "是否具有业务价值": value,
                    },
                    event_edits={
                        "title": correct_title,
                        "event_date": correct_date,
                        "category": category,
                        "summary": summary,
                        "impact": impact,
                        "affected_area": area,
                    },
                    checklist={"source_opened": opened, "facts_checked": facts},
                    reviewer_note=note,
                )
            except Exception as exc:
                st.error(str(exc))
            else:
                st.success(
                    f"QA核验已保存，修改字段 {len(result['changed_fields'])} 项；"
                    "仍需通过受控晋升才能进入正式工作区。"
                )
                remaining_ids = [
                    str(item["document_id"])
                    for item in records
                    if str(item["document_id"]) != document_id
                    and qa_review_status(item) == "未审核"
                ]
                save_review_progress(
                    context["db_path"],
                    str(context["workspace_id"]),
                    current_document_id=remaining_ids[0] if remaining_ids else "",
                    filter_status="未审核",
                    reviewer_type="human_user" if reviewer_type_label == "项目使用者" else "industry_reviewer",
                    reviewer_name=reviewer_name,
                    draft={},
                )
                st.session_state["pending_qa_review_filter"] = "未审核"
                if remaining_ids:
                    st.session_state["pending_qa_review_document"] = remaining_ids[0]
                st.rerun()


def _render_qa_promotion_queue(workspace: dict[str, object], db_path) -> None:
    if not _qa_excel_available():
        st.error(QA_EXCEL_REPAIR_MESSAGE)
        st.info("QA晋升队列暂不可读；不会自动晋升或伪造候选数量。")
        return
    from qa_promotion import list_qa_promotion_candidates, promote_qa_events
    from qa_review_store import qa_context

    context = qa_context()
    if not context:
        st.info("未找到隔离QA数据库。")
        return
    candidates = list_qa_promotion_candidates(
        context["db_path"],
        source_workspace_id=str(context["workspace_id"]),
        report_mode="内部研究版",
    )
    if not candidates:
        st.info("QA工作区暂无可评估的事件。")
        return
    st.caption(
        "晋升只复制通过门禁的事件、当前文档版本、证据链与审核链；"
        "不会删除QA原记录，也不会自动赋予客户报告资格。"
    )
    frame = pd.DataFrame(
        [
            {
                "事件": item["title"],
                "真人审核": "通过" if item["human_reviewed"] else "未通过",
                "证据": "通过" if item["evidence_verified"] else "未通过",
                "正文质量": item["quality_status"],
                "来源与URL": "完整" if item["source_complete"] else "不完整",
                "重复状态": item["duplicate_level"],
                "青岛港业务相关性": item["qingdao_relevance"],
                "内部晋升": "可晋升" if item["internal_eligible"] else "阻断",
                "客户报告": "具备资格" if item["customer_eligible"] else "不具备",
                "阻断原因": "；".join(item["block_reasons"]) or "无",
            }
            for item in candidates
        ]
    )
    st.dataframe(frame, hide_index=True, width="stretch")
    eligible_ids = [str(item["event_id"]) for item in candidates if item["internal_eligible"]]
    selected_ids = st.multiselect(
        "选择要晋升的事件",
        eligible_ids,
        format_func=lambda value: next(
            str(item["title"]) for item in candidates if str(item["event_id"]) == value
        ),
    )
    if not eligible_ids:
        st.warning("目前没有同时通过真人审核、逐字证据、正文质量和去重门禁的事件。")
        return
    confirmed = st.checkbox(
        "我确认只晋升所选且全部通过门禁的记录；进入正式库不代表允许全文再分发或原始数据出售"
    )
    if st.button(
        "受控晋升到正式工作区",
        type="primary",
        disabled=not (selected_ids and confirmed),
    ):
        result = promote_qa_events(
            context["db_path"],
            db_path,
            source_workspace_id=str(context["workspace_id"]),
            target_workspace_id=str(workspace["workspace_id"]),
            event_ids=selected_ids,
            qa_run_id=Path(context["db_path"]).parent.name,
            report_mode="内部研究版",
            ui_session_id=ui_session_id(),
        )
        if result.blocked:
            st.error("晋升未执行，整批已回滚：" + "；".join(result.blocked.values()))
        else:
            st.success(
                f"已晋升 {len(result.promoted)} 条。FTS索引已写入；"
                "向量状态标记为待向量化，可在知识库维护时增量构建。"
            )
            st.rerun()


def render_human_review(workspace: dict[str, object], db_path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("人工审核")
    st.caption("AI、Codex和迁移记录不计入独立真人核验；只有你或明确的行业复核人可以保存确认。")
    provenance = _review_provenance_counts()
    if provenance is None:
        st.warning(QA_EXCEL_REPAIR_MESSAGE)
        st.caption("QA核验统计暂不可读取；正式工作区审核仍可使用。")
    else:
        _metric_cards(
            [
                ("独立真人核验", provenance["human"]),
                ("自动或代理标注", provenance["automated"]),
                ("待真人核验", provenance["pending"]),
            ]
        )
    pending_scope = st.session_state.pop("pending_review_scope", None)
    if pending_scope is not None:
        st.session_state["daily_review_scope"] = pending_scope
    scope = st.radio(
        "审核范围",
        ["正式工作区事件", "隔离QA人工金标准", "待晋升队列"],
        horizontal=True,
        key="daily_review_scope",
    )
    if scope == "隔离QA人工金标准":
        _render_qa_human_review()
        return
    if scope == "待晋升队列":
        _render_qa_promotion_queue(workspace, db_path)
        return
    with connect(db_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT event_id,title,event_date,category,status,risk_level,review_state
                FROM events WHERE workspace_id=? AND record_state='active'
                AND (human_verified=0 OR requires_second_review=1)
                ORDER BY CASE WHEN risk_level IN ('高','中') THEN 0 ELSE 1 END,created_at DESC""",
                (workspace_id,),
            ).fetchall()
        ]
    if not rows:
        st.info("正式工作区暂无待审核事件。QA历史标签仍需由真人在相应QA工作区复核，不能自动算入。")
        return
    selected_id = st.selectbox(
        "选择待审核事件",
        [str(item["event_id"]) for item in rows],
        format_func=lambda value: next(str(item["title"]) for item in rows if item["event_id"] == value),
    )
    item = get_review_record(selected_id, workspace_id, db_path)
    if not item:
        st.error("事件或当前文档版本不存在。")
        return
    left, right = st.columns([1.15, 1])
    with left:
        st.subheader("原始公开资料")
        st.link_button("打开原文", str(item["canonical_url"]))
        st.caption(
            f"{item.get('publisher') or '来源待核对'}｜{str(item.get('published_at') or '')[:10] or '日期待核对'}｜"
            f"文档版本 v{item.get('document_version') or 1}"
        )
        st.markdown(_highlight_source(str(item.get("cleaned_text") or ""), list(item["evidence"])), unsafe_allow_html=True)
    with right:
        st.subheader("抽取事件与人工修订")
        title = st.text_input("标题", str(item.get("title") or ""), key=f"review_title_{selected_id}")
        event_date = st.text_input("事件日期（YYYY-MM-DD）", str(item.get("event_date") or ""), key=f"review_date_{selected_id}")
        summary = st.text_area("事实摘要", str(item.get("summary") or ""), height=120, key=f"review_summary_{selected_id}")
        impact = st.text_area("潜在影响（分析）", str(item.get("impact") or ""), height=100, key=f"review_impact_{selected_id}")
        affected_area = st.text_input("地点/受影响区域", str(item.get("affected_area") or ""), key=f"review_area_{selected_id}")
        entities = st.text_input("涉及主体", str(item.get("involved_entities") or ""), key=f"review_entities_{selected_id}")
        category = st.selectbox(
            "事件类型",
            ["航行警告", "海上气象", "港口作业", "航线运价", "政策监管", "招标采购", "企业动态"],
            index=max(0, ["航行警告", "海上气象", "港口作业", "航线运价", "政策监管", "招标采购", "企业动态"].index(str(item.get("category"))) if str(item.get("category")) in ["航行警告", "海上气象", "港口作业", "航线运价", "政策监管", "招标采购", "企业动态"] else 0),
        )
        status = st.selectbox(
            "当前状态",
            ["新增", "持续", "更新", "解除", "已结束", "待核实"],
            index=max(0, ["新增", "持续", "更新", "解除", "已结束", "待核实"].index(str(item.get("status"))) if str(item.get("status")) in ["新增", "持续", "更新", "解除", "已结束", "待核实"] else 0),
        )
        risk = st.selectbox("人工风险等级", ["", "低", "中", "高"], index=["", "低", "中", "高"].index(str(item.get("manual_risk_level") or "")) if str(item.get("manual_risk_level") or "") in ["", "低", "中", "高"] else 0)
        keywords = st.text_input("关键词", str(item.get("manual_keywords") or ""), key=f"review_keywords_{selected_id}")
        related = st.text_input("关联事件（解除/更新时填写）", str(item.get("related_event_id") or ""), key=f"review_related_{selected_id}")
        reviewer_type_label = st.radio("核验人类型", ["项目使用者", "行业复核人"], horizontal=True)
        reviewer_type = "human_user" if reviewer_type_label == "项目使用者" else "industry_reviewer"
        reviewer_name = st.text_input("核验人姓名或内部代号", key=f"reviewer_name_{selected_id}")
        source_opened = st.checkbox("我已打开并核对原始公开来源", key=f"review_open_{selected_id}")
        facts_checked = st.checkbox("我已逐项核对事实字段", key=f"review_facts_{selected_id}")
        note = st.text_area("审核备注", key=f"review_note_{selected_id}")
        decision_label = st.radio(
            "处理结果",
            ["接受", "修改后接受", "驳回", "正文有问题", "需要第二人复核"],
        )
        decision = {
            "接受": "accept",
            "修改后接受": "accept_modified",
            "驳回": "reject",
            "正文有问题": "body_issue",
            "需要第二人复核": "needs_second_review",
        }[decision_label]
        destructive = decision in {"reject", "body_issue"}
        confirmed = st.checkbox("确认执行此处理结果", key=f"review_confirm_{selected_id}") if destructive else True
        if st.button(
            "保存审核结果",
            type="primary",
            disabled=not (reviewer_name.strip() and confirmed),
        ):
            try:
                result = save_event_review(
                    selected_id,
                    workspace_id,
                    db_path,
                    decision=decision,
                    reviewer_type=reviewer_type,
                    reviewer_name=reviewer_name,
                    reviewer_note=note,
                    edits={
                        "title": title,
                        "event_date": event_date,
                        "summary": summary,
                        "impact": impact,
                        "affected_area": affected_area,
                        "involved_entities": entities,
                        "category": category,
                        "status": status,
                        "manual_risk_level": risk,
                        "manual_keywords": keywords,
                        "related_event_id": related,
                    },
                    checklist={"source_opened": source_opened, "facts_checked": facts_checked},
                    ui_session_id=ui_session_id(),
                )
            except Exception as exc:
                st.error(str(exc))
            else:
                st.success(f"审核已保存。修改字段：{'、'.join(result['changed_fields']) or '无'}")
                if result["customer_gate_reasons"]:
                    st.warning("尚不能进入客户报告：" + "；".join(result["customer_gate_reasons"]))
                st.rerun()
    with st.expander("审核链"):
        reviews = list(item.get("reviews") or [])
        if reviews:
            st.dataframe(
                pd.DataFrame(reviews)[
                    [column for column in ["reviewed_at", "reviewer_type", "reviewer_name", "decision", "reviewer_note", "changed_fields_json"] if column in pd.DataFrame(reviews)]
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("尚无审核记录。")


def render_evidence_repair(workspace: dict[str, object], db_path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("证据修复")
    st.caption("模糊匹配只提供候选位置；只有当前文档版本中的连续逐字原文才能认证。")
    pending_scope = st.session_state.pop("pending_evidence_scope", None)
    if pending_scope is not None:
        st.session_state["daily_evidence_scope"] = pending_scope
    scope = st.radio(
        "工作区",
        ["正式工作区", "QA工作区"],
        horizontal=True,
        key="daily_evidence_scope",
    )
    target_db = db_path
    target_workspace_id = workspace_id
    if scope == "QA工作区":
        if not _qa_excel_available():
            st.error(QA_EXCEL_REPAIR_MESSAGE)
            st.info("正式工作区证据修复仍可使用。")
            return
        try:
            from qa_review_store import qa_context
        except ModuleNotFoundError as exc:
            if exc.name == "openpyxl":
                st.error(QA_EXCEL_REPAIR_MESSAGE)
                return
            raise
        qa = qa_context()
        if not qa:
            st.info("未找到隔离QA数据库。")
            return
        target_db = qa["db_path"]
        target_workspace_id = str(qa["workspace_id"])
    if st.button("重新扫描证据候选"):
        result = refresh_evidence_candidates(target_db, target_workspace_id)
        st.success(f"已刷新 {result['updated']} 条候选；没有自动认证任何模糊匹配。")
    issues = list_evidence_issues(target_db, target_workspace_id)
    if not issues:
        st.success(f"{scope}当前没有无法逐字定位的证据。")
        return
    selected_id = st.selectbox(
        "选择问题引用",
        [str(item["evidence_id"]) for item in issues],
        format_func=lambda value: next(
            f"{item.get('event_title')}｜{item.get('document_title')}"
            for item in issues
            if item["evidence_id"] == value
        ),
    )
    issue = next(item for item in issues if item["evidence_id"] == selected_id)
    header = st.columns([2, 2, 1])
    header[0].write(f"**文档：** {issue.get('document_title') or '标题待核对'}")
    header[1].write(f"**来源：** {issue.get('publisher') or '来源待核对'}")
    header[2].write(f"**版本：** v{issue.get('current_version') or 1}")
    if str(issue.get("canonical_url") or "").startswith(("http://", "https://")):
        st.link_button("打开原文", str(issue["canonical_url"]))
    st.error(str(issue["reason"]))
    if issue.get("meaning_change"):
        st.warning("该问题涉及事实含义变化。必须同步修正事件内容，并重新进入人工审核。")
    left, right = st.columns([1.25, 1])
    with left:
        st.text_area(
            "完整清洗原文（只读）",
            str(issue.get("cleaned_text") or ""),
            height=380,
            disabled=True,
            key=f"evidence_source_{scope}_{selected_id}",
        )
    with right:
        st.write(f"**事件：** {issue.get('event_title') or ''}")
        st.write(f"**事实摘要：** {issue.get('summary') or ''}")
        st.write(f"**当前引用：** {issue.get('quote_text') or ''}")
    candidates = list(issue.get("candidates") or [])
    for index, candidate in enumerate(candidates, 1):
        with st.container(border=True):
            st.caption(f"候选 {index}｜相似度 {float(candidate.get('similarity') or 0):.1%}（仅供人工定位）")
            st.write(str(candidate.get("candidate_text") or "未找到候选"))
            diff = difflib.HtmlDiff(wrapcolumn=70).make_table(
                [str(issue.get("quote_text") or "")],
                [str(candidate.get("candidate_text") or "")],
                fromdesc="当前引用",
                todesc="原文候选",
                context=True,
                numlines=1,
            )
            with st.expander("查看逐字差异"):
                st.markdown(diff, unsafe_allow_html=True)
    replacement = st.text_area(
        "从原文复制连续逐字证据",
        value=str((candidates or [{}])[0].get("candidate_text") or ""),
        height=140,
    )
    reviewer_type_label = st.radio("修复人类型", ["项目使用者", "行业复核人"], horizontal=True)
    reviewer = st.text_input("修复人姓名或内部代号")
    note = st.text_area("修复说明；涉及事实变化时必须填写原因")
    event_edits: dict[str, str] = {}
    if issue.get("meaning_change"):
        st.subheader("同步修正事件事实")
        event_edits = {
            "title": st.text_input("事件标题", str(issue.get("event_title") or "")),
            "summary": st.text_area("事实摘要", str(issue.get("summary") or ""), height=100),
            "impact": st.text_area("潜在影响（分析）", str(issue.get("impact") or ""), height=80),
            "affected_area": st.text_input("受影响区域", str(issue.get("affected_area") or "")),
        }
        st.caption("保存后该事件会被标记为待二次复核，不会直接取得晋升资格。")
    confirmed = st.checkbox("我确认该引用是当前文档版本中的连续原文")
    if st.button("保存逐字证据", type="primary", disabled=not (reviewer and replacement and confirmed)):
        try:
            result = apply_exact_evidence_repair(
                selected_id,
                replacement,
                target_db,
                reviewer_type="human_user" if reviewer_type_label == "项目使用者" else "industry_reviewer",
                reviewer_name=reviewer,
                reviewer_note=note,
                confirmed=True,
                ui_session_id=ui_session_id(),
                event_edits=event_edits,
            )
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success(
                f"证据已绑定到当前文档版本，偏移 {result['start_offset']}–{result['end_offset']}。"
            )
            if result["requires_second_review"]:
                st.warning("事件事实已同步修正并进入待二次复核；尚不能晋升。")
            elif result["post_audit_verified"]:
                st.success("该条证据已重新审计通过。请到待晋升队列查看其他门禁。")
            st.rerun()
    disposition = st.columns(2)
    reviewer_type = "human_user" if reviewer_type_label == "项目使用者" else "industry_reviewer"
    if disposition[0].button("驳回该事件", disabled=not (reviewer and confirmed)):
        try:
            save_event_review(
                str(issue["event_id"]),
                target_workspace_id,
                target_db,
                decision="reject",
                reviewer_type=reviewer_type,
                reviewer_name=reviewer,
                reviewer_note=note or "证据无法支持当前事件",
                checklist={},
                ui_session_id=ui_session_id(),
            )
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success("事件已驳回并保留审计链，不会进入晋升或报告。")
            st.rerun()
    if disposition[1].button("标记二次复核", disabled=not (reviewer and confirmed)):
        try:
            save_event_review(
                str(issue["event_id"]),
                target_workspace_id,
                target_db,
                decision="needs_second_review",
                reviewer_type=reviewer_type,
                reviewer_name=reviewer,
                reviewer_note=note or "证据问题需第二人复核",
                checklist={},
                ui_session_id=ui_session_id(),
            )
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success("已标记为待二次复核。")
            st.rerun()


def render_system_check(workspace: dict[str, object], db_path, data_root: Path) -> None:
    workspace_id = str(workspace["workspace_id"])
    st.header("来源与系统设置")
    tab_sources, tab_checks, tab_stats, tab_ai = st.tabs(
        ["来源状态", "系统检查", "四周评估", "AI设置"]
    )
    with tab_sources:
        sources = list_sources(workspace_id, db_path)
        if not sources:
            st.info("尚无来源，请先在采集页面初始化推荐来源。")
        else:
            frame = pd.DataFrame(
                [
                    {
                        "来源": item["source_name"],
                        "启用": "是" if item["enabled"] and item["crawl_allowed"] else "否",
                        "健康状态": item.get("health_status") or "未检查",
                        "运行状态": {
                            "stable": "稳定",
                            "degraded": "降级",
                            "manual_only": "仅人工导入",
                            "disabled": "已停用",
                            "needs_adapter": "需要适配器",
                            "permission_review": "许可待复核",
                        }.get(str(item.get("operational_status") or ""), "尚未形成真实运行结论"),
                        "连续失败": item.get("consecutive_failures") or 0,
                        "内部分析": "允许" if item.get("internal_analysis_allowed") else "禁用",
                        "客户摘要": "允许并提示风险" if item.get("customer_summary_allowed") else "不允许",
                        "全文再分发": "允许" if item.get("fulltext_redistribution_allowed") else "不允许",
                        "原始数据转售": "允许" if item.get("raw_data_resale_allowed") else "不允许",
                    }
                    for item in sources
                ]
            )
            st.dataframe(frame, hide_index=True, width="stretch")
            st.info("许可状态不明确不会阻止内部采集和分析；客户摘要、全文再分发与原始数据转售使用不同门禁。")
    with tab_checks:
        with connect(db_path) as connection:
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            counts = connection.execute(
                """SELECT
                (SELECT COUNT(*) FROM documents WHERE workspace_id=? AND record_state='quarantined') AS bad_docs,
                (SELECT COUNT(*) FROM event_evidence x JOIN events e ON e.event_id=x.event_id
                 WHERE e.workspace_id=? AND e.record_state='active' AND x.verification_status!='已验证') AS bad_evidence,
                (SELECT COUNT(*) FROM events WHERE workspace_id=? AND record_state='active' AND human_verified=0) AS pending_review,
                (SELECT COUNT(*) FROM events WHERE workspace_id=? AND record_state='active'
                 AND human_verified=1 AND evidence_verified=1 AND report_eligible=1) AS customer_candidates,
                (SELECT COUNT(*) FROM operation_runs WHERE workspace_id=? AND status='failed'
                 AND created_at>=?) AS recent_failures""",
                (
                    workspace_id,
                    workspace_id,
                    workspace_id,
                    workspace_id,
                    workspace_id,
                    (datetime.now().astimezone() - timedelta(days=7)).isoformat(timespec="seconds"),
                ),
            ).fetchone()
        from deepseek_service import load_settings

        settings = load_settings()
        sources = list_sources(workspace_id, db_path)
        backups = sorted((Path(__file__).resolve().parent / "backups").glob("*.zip"))
        checks = [
            ("数据库状态", "正常" if quick == "ok" else "异常", 0 if quick == "ok" else 1, "业务数据读写", "历史记录"),
            ("AI服务状态", "已配置" if settings.configured else "未配置（本地模式可用）", 0 if settings.configured else 1, "AI抽取与生成；不影响本地规则", "来源与系统设置"),
            ("来源状态", "已启用", sum(bool(item["enabled"]) and bool(item["crawl_allowed"]) for item in sources), "公开数据更新", "采集与导入"),
            ("正文质量", "有隔离项" if counts["bad_docs"] else "正常", int(counts["bad_docs"]), "隔离项不进入检索和报告", "文档库"),
            ("证据质量", "需修复" if counts["bad_evidence"] else "正常", int(counts["bad_evidence"]), "客户报告门禁", "证据修复"),
            ("人工审核", "业务待办" if counts["pending_review"] else "已处理", int(counts["pending_review"]), "客户报告资格；不是程序异常", "人工审核"),
            ("报告门禁", "可用候选", int(counts["customer_candidates"]), "客户交付版", "报告中心"),
            ("备份状态", "已有备份" if backups else "尚无备份", len(backups), "灾难恢复", "历史记录"),
            ("路径兼容", "正常", 0, f"当前路径：{Path(db_path).parent.name}", "来源与系统设置"),
            ("最近任务异常", "需查看" if counts["recent_failures"] else "正常", int(counts["recent_failures"]), "最近7天任务", "历史记录"),
        ]
        st.caption(f"最近检查时间：{now_iso()}")
        for index, (name, status, count, impact, page) in enumerate(checks):
            row = st.columns([2, 2, 1, 4, 1])
            row[0].write(f"**{name}**")
            row[1].write(status)
            row[2].write(count)
            row[3].caption(impact)
            if row[4].button("处理", key=f"check_{index}_{page}"):
                navigate(page)
        with st.expander("高级设置入口"):
            st.caption("来源选择器、AI模型、数据库诊断和迁移工具位于高级管理。")
            if st.button("打开高级管理"):
                st.session_state["ui_mode"] = "专业模式"
                st.rerun()
    with tab_stats:
        from run_statistics import (
            add_customer_feedback,
            four_week_evaluation,
            source_run_statistics,
        )

        evaluation = four_week_evaluation(workspace_id, db_path)
        st.caption(
            "仅统计 crawl_source_runs 中由真实采集产生的记录；不会补填模拟天数或虚构客户反馈。"
        )
        _metric_cards(
            [
                ("已真实运行天数", evaluation["real_run_days"]),
                ("距28天", evaluation["remaining_days"]),
                ("文档量", evaluation["documents"]),
                ("事件量", evaluation["events"]),
                ("真实报告生成", evaluation["reports_generated"]),
                ("手工客户反馈", evaluation["customer_feedback_count"]),
            ]
        )
        st.write(
            "人工审核通过率："
            + (
                f"{float(evaluation['human_review_pass_rate']):.1%}"
                if evaluation["human_review_pass_rate"] is not None
                else "暂无真实事件"
            )
            + "｜证据成功率："
            + (
                f"{float(evaluation['evidence_success_rate']):.1%}"
                if evaluation["evidence_success_rate"] is not None
                else "暂无证据"
            )
        )
        stats = source_run_statistics(
            workspace_id,
            db_path,
            start_date=str(evaluation["period_start"]),
            end_date=str(evaluation["period_end"]),
        )
        if stats:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "来源": item["source_name"],
                            "真实运行次数": item["run_count"],
                            "请求数": item["request_count"],
                            "抓取成功率": (
                                f"{float(item['crawl_success_rate']):.1%}"
                                if item["crawl_success_rate"] is not None
                                else "待运行"
                            ),
                            "有效正文率": (
                                f"{float(item['valid_content_rate']):.1%}"
                                if item["valid_content_rate"] is not None
                                else "待运行"
                            ),
                            "有效事件产出率": (
                                f"{float(item['event_yield_rate']):.1%}"
                                if item["event_yield_rate"] is not None
                                else "待运行"
                            ),
                            "平均响应(秒)": (
                                round(float(item["average_response_seconds"]), 2)
                                if item["average_response_seconds"] is not None
                                else ""
                            ),
                            "连续失败": item["consecutive_failures"],
                            "最近成功": item["last_success_at"],
                            "来源状态": item["status_label"],
                        }
                        for item in stats
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        with st.expander("手工记录客户反馈"):
            st.caption("只有你实际收到并手工录入的反馈才会计入；AI不会生成这里的内容。")
            feedback_customer = st.text_input("客户代号（可选）", key="feedback_customer")
            feedback_text = st.text_area("客户反馈", key="feedback_text")
            feedback_confirm = st.checkbox("确认这是实际收到的反馈", key="feedback_confirm")
            if st.button(
                "保存客户反馈",
                disabled=not (feedback_text.strip() and feedback_confirm),
            ):
                add_customer_feedback(
                    workspace_id,
                    db_path,
                    customer_label=feedback_customer,
                    feedback_text=feedback_text,
                    created_by="human_user",
                )
                st.success("已保存用户手工录入的客户反馈。")
                st.rerun()
    with tab_ai:
        from ui_agent import render_ai_configuration

        render_ai_configuration(
            workspace,
            db_path,
            expanded=True,
            key_prefix="daily_system",
        )
