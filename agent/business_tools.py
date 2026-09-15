from __future__ import annotations

from datetime import date
from urllib.parse import urlparse


from commercial_report import ReportOptions
from crawler import CrawlManager
from deepseek_service import build_rag_answerer, load_settings, mask_api_key, test_deepseek_connection
from event_pipeline import batch_confirm_events, reprocess_pending_ai
from platform_db import (
    connect, initialize_recommended_sources as initialize_sources,
    list_sources as db_list_sources, now_iso, recommended_source_issues,
    table_counts, table_csv_bytes, transaction, upsert_source,
)
from platform_report import ReportEligibilityError, generate_platform_report
from rag.hybrid_retriever import HybridRetriever
from rag.keyword_search import KeywordSearcher
from rag.rag_service import RAGService
from source_health import validate_enabled_sources
from task_runtime import enqueue_background_task
from workspace_store import load_reports, workspace_paths

from .source_assistant import analyze_single_source
from .tool_schemas import ToolPayload


def _success(message: str, data=None, artifacts=None) -> ToolPayload:
    return ToolPayload(status="success", message=message, data=data or {}, artifacts=artifacts or [])


def _filters(values) -> dict[str, object]:
    return {key: getattr(values, key) for key in (
        "start_date", "end_date", "source_id", "category", "status", "human_verified", "report_eligible"
    ) if hasattr(values, key) and getattr(values, key) not in {"", None}}


def get_system_status(context, _args) -> ToolPayload:
    counts = table_counts(context.workspace_id, context.db_path)
    sources = db_list_sources(context.workspace_id, context.db_path)
    with connect(context.db_path) as connection:
        indexed = int(connection.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE workspace_id=? AND embedding_status='成功'",
            (context.workspace_id,),
        ).fetchone()[0])
        last = connection.execute(
            "SELECT * FROM crawl_runs WHERE workspace_id=? ORDER BY started_at DESC LIMIT 1",
            (context.workspace_id,),
        ).fetchone()
    settings = load_settings(context.env_path)
    data = {
        "api_status": "已配置" if settings.configured else "未配置",
        "api_key_display": mask_api_key(settings.api_key),
        "agent_model": settings.agent_model,
        "extraction_model": settings.extraction_model,
        "database": "正常",
        "sources": {"total": len(sources), "enabled": sum(bool(x["enabled"]) and bool(x["crawl_allowed"]) for x in sources)},
        "documents": counts["documents"], "events": counts["events"], "indexed_chunks": indexed,
        "reports": counts["reports"], "last_crawl": dict(last) if last else {},
    }
    return _success("系统状态已读取。", data)


def configure_ai(_context, _args) -> ToolPayload:
    return _success("请使用AI工作台顶部的“配置DeepSeek API”表单。密钥只会原子写入本机.env，模型无法读取完整密钥。", {"show_ai_form": True})


def test_ai(context, _args) -> ToolPayload:
    result = test_deepseek_connection(load_settings(context.env_path), requester=context.requester)
    status = "success" if result.ok else "error"
    return ToolPayload(status=status, message=result.status, data={
        "model": result.model_name, "elapsed_ms": result.elapsed_ms, "error_type": result.error_type,
        "http_status": result.http_status, "error_code": result.error_code,
        "safe_error_message": result.safe_error_message, "failed_stage": result.failed_stage,
        "available_models": list(result.available_models), "balance_available": result.balance_available,
        "balance_summary": result.balance_summary, "configured_source": result.configured_source,
    })


def list_sources_tool(context, _args) -> ToolPayload:
    rows = db_list_sources(context.workspace_id, context.db_path)
    sources = [{
        "source_id": row["source_id"], "source_name": row["source_name"],
        "enabled": bool(row["enabled"] and row["crawl_allowed"]), "robots_status": row["robots_status"],
        "commercial_reuse_status": row["commercial_reuse_status"],
        "last_success_at": row["last_success_at"], "last_error": row["last_error"],
    } for row in rows]
    return _success(f"当前共有 {len(sources)} 个公开来源。", {"sources": sources})


def initialize_recommended(context, _args) -> ToolPayload:
    result = initialize_sources(context.workspace_id, context.db_path)
    return _success(f"推荐来源已初始化，当前可自动采集 {result['enabled']} 个。", dict(result))


def analyze_source(context, args) -> ToolPayload:
    if context.source_analyzer:
        result = context.source_analyzer(str(args.url))
    else:
        result = analyze_single_source(str(args.url), fetcher=context.source_fetcher, resolver=context.resolver)
    return _success("单一栏目分析完成，尚未写入或启用来源。", dict(result))


def propose_source(context, args) -> ToolPayload:
    result = context.source_analyzer(str(args.url)) if context.source_analyzer else analyze_single_source(
        str(args.url), fetcher=context.source_fetcher, resolver=context.resolver,
    )
    return _success("已生成适配器建议；安全、robots与许可仍需人工确认。", {
        "url": str(args.url), "candidate_count": result.get("candidate_count", 0),
        "candidates": result.get("candidates", []), "adapter_suggestion": result.get("adapter_suggestion", {}),
        "article_test": result.get("article_test", {}),
    })


def add_source(context, args) -> ToolPayload:
    domain = (urlparse(str(args.list_page_url)).hostname or "").lower()
    values = args.model_dump(mode="json")
    values.update({
        "workspace_id": context.workspace_id, "domain": domain,
        "adapter_config": {
            "article_link_selector": args.article_link_selector,
            "allowed_path_prefix": args.allowed_path_prefix,
            "url_pattern": args.url_pattern,
        },
        "enabled": bool(args.enable_after_add), "crawl_allowed": bool(args.enable_after_add),
        "report_use_allowed": False, "terms_status": "需人工确认",
    })
    if args.enable_after_add and args.robots_status != "允许":
        return ToolPayload(status="error", message="robots状态尚未确认允许，来源只能先保存为停用。")
    source_id = upsert_source(values, context.db_path)
    return _success("公开来源已添加。" + ("已启用。" if args.enable_after_add else "当前保持停用。"), {"source_id": source_id, "source_name": args.source_name})


def _toggle_source(context, source_id: str, enabled: bool) -> ToolPayload:
    source = next((row for row in db_list_sources(context.workspace_id, context.db_path) if row["source_id"] == source_id), None)
    if not source:
        return ToolPayload(status="error", message="来源不存在或不属于当前工作空间。")
    if enabled:
        issues = recommended_source_issues(source)
        if issues:
            return ToolPayload(status="error", message="来源配置不完整：" + "；".join(issues))
        if source["robots_status"] != "允许":
            return ToolPayload(status="error", message="robots状态尚未确认允许，不能启用。")
    upsert_source({**source, "workspace_id": context.workspace_id, "enabled": enabled, "crawl_allowed": enabled}, context.db_path)
    return _success(f"{source['source_name']}已{'启用' if enabled else '停用'}。", {"source_id": source_id})


def enable_source(context, args) -> ToolPayload:
    return _toggle_source(context, args.source_id, True)


def disable_source(context, args) -> ToolPayload:
    return _toggle_source(context, args.source_id, False)


def _crawl_manager(context):
    if context.crawl_manager_factory:
        return context.crawl_manager_factory()
    vector_indexer = None
    try:
        from rag.indexer import KnowledgeIndexer
        vector_indexer = KnowledgeIndexer(context.db_path, context.data_root / "chroma")
    except Exception:
        pass
    return CrawlManager(
        context.db_path, context.data_root, vector_indexer=vector_indexer,
        ai_enabled=bool(context.workspace.get("ai_enabled")),
        ai_settings=load_settings(context.env_path), ai_requester=context.requester,
    )


def run_crawl(context, args) -> ToolPayload:
    if context.crawl_manager_factory is None:
        queued = enqueue_background_task(
            context.workspace_id,
            "crawl",
            context.db_path,
            context.data_root,
            source_id=(args.source_ids or [""])[0] if len(args.source_ids or []) == 1 else "",
            input_summary=(
                f"AI工作台增量采集；来源{len(args.source_ids or []) or '全部'}；"
                f"最多{args.max_articles}篇"
            ),
            metadata={
                "source_ids": list(args.source_ids or []),
                "max_articles": min(int(args.max_articles), 20),
                "start_date": args.start_date,
                "end_date": args.end_date,
                "refresh_mode": "new_only",
                "auto_ai": False,
                "source_time_budget_seconds": 120,
            },
        )
        return _success(
            (
                "增量采集任务已创建。"
                if queued["created"]
                else "已有采集任务正在运行，未重复提交。"
            ),
            {
                "operation_run_id": queued["operation_run_id"],
                "queued": bool(queued["created"]),
                "max_articles": min(int(args.max_articles), 20),
                "refresh_mode": "new_only",
            },
        )
    progress_events: list[str] = []
    result = _crawl_manager(context).run(
        context.workspace_id, source_ids=args.source_ids or None,
        max_articles=args.max_articles, start_date=args.start_date, end_date=args.end_date,
        refresh_mode="new_only",
        progress=lambda phase, _value: (progress_events.append(phase), context.emit(phase)),
    )
    data = dict(vars(result))
    data["progress"] = list(dict.fromkeys(progress_events))
    return ToolPayload(status="success" if result.status in {"完成", "部分完成"} else "error", message=f"公开数据更新{result.status}。", data=data)


def get_crawl(context, _args) -> ToolPayload:
    with connect(context.db_path) as connection:
        row = connection.execute("SELECT * FROM crawl_runs WHERE workspace_id=? ORDER BY started_at DESC LIMIT 1", (context.workspace_id,)).fetchone()
    return _success("已读取最近一次采集状态。" if row else "尚无采集记录。", {"crawl_run": dict(row) if row else {}})


def retry_failed(context, args) -> ToolPayload:
    if getattr(args, "include_pending_ai", False):
        settings = load_settings(context.env_path)
        if not settings.configured or not context.workspace.get("ai_enabled"):
            return ToolPayload(status="error", message="DeepSeek尚未配置或AI辅助已关闭；文档继续保留在待AI处理队列。")
        if context.crawl_manager_factory is None:
            queued = enqueue_background_task(
                context.workspace_id,
                "ai_reprocess",
                context.db_path,
                context.data_root,
                input_summary="AI工作台补跑待AI文档，最多2篇",
                metadata={"limit": 2, "time_budget_seconds": 120},
            )
            return _success(
                "待AI补跑任务已创建，不会重新抓取网页。"
                if queued["created"]
                else "已有AI补跑任务正在运行，未重复提交。",
                {
                    "operation_run_id": queued["operation_run_id"],
                    "queued": bool(queued["created"]),
                    "limit": 2,
                },
            )
        result = reprocess_pending_ai(
            context.workspace_id, context.db_path, limit=2,
            settings=settings, requester=context.requester,
        )
        status = "success" if int(result["failed"]) == 0 else "error"
        return ToolPayload(status=status, message=f"待AI文档补跑完成：成功 {result['success']}，失败 {result['failed']}；未重新抓取网页。", data=result)
    failed = [row["source_id"] for row in db_list_sources(context.workspace_id, context.db_path) if row["last_error"]]
    selected = [item for item in (args.source_ids or failed) if item in failed]
    if not selected:
        return _success("没有可重试的失败来源。", {"source_ids": []})
    with transaction(context.db_path) as connection:
        connection.executemany("UPDATE sources SET consecutive_failures=0 WHERE source_id=? AND workspace_id=?", [(item, context.workspace_id) for item in selected])
    crawl_args = type("RetryArgs", (), {"source_ids": selected, "max_articles": 5, "start_date": "", "end_date": ""})()
    return run_crawl(context, crawl_args)


def retry_pending_ai(context, args) -> ToolPayload:
    settings = load_settings(context.env_path)
    if not settings.configured or not context.workspace.get("ai_enabled"):
        return ToolPayload(status="error", message="DeepSeek尚未配置或AI辅助已关闭；文档继续保留在待AI处理队列。")
    if context.crawl_manager_factory is None:
        queued = enqueue_background_task(
            context.workspace_id,
            "ai_reprocess",
            context.db_path,
            context.data_root,
            input_summary=f"AI工作台补跑待AI文档，最多{min(int(args.limit), 5)}篇",
            metadata={
                "limit": min(int(args.limit), 5),
                "time_budget_seconds": 120,
            },
        )
        return _success(
            "待AI补跑任务已创建，不会重新抓取网页。"
            if queued["created"]
            else "已有AI补跑任务正在运行，未重复提交。",
            {
                "operation_run_id": queued["operation_run_id"],
                "queued": bool(queued["created"]),
                "limit": min(int(args.limit), 5),
            },
        )
    result = reprocess_pending_ai(
        context.workspace_id, context.db_path, limit=min(int(args.limit), 5),
        settings=settings, requester=context.requester,
    )
    status = "success" if int(result["failed"]) == 0 else "error"
    return ToolPayload(status=status, message=f"待AI文档补跑完成：成功 {result['success']}，失败 {result['failed']}；未重新抓取网页。", data=result)


def validate_source_health(context, args) -> ToolPayload:
    results = validate_enabled_sources(context.workspace_id, context.db_path, source_ids=args.source_ids or None)
    normal = sum(item["health_status"] in {"正常", "部分可用"} for item in results)
    return _success(f"克制验收完成：{normal}/{len(results)} 个来源可读取部分或全部正文。", {"sources": results})


def _retriever(context):
    keyword = KeywordSearcher(context.db_path)
    if context.retriever_factory:
        return context.retriever_factory()
    chroma = context.data_root / "chroma"
    if chroma.exists():
        try:
            from rag.embeddings import LocalBGEEmbedding
            from rag.vector_store import ChromaVectorStore
            return HybridRetriever(keyword, LocalBGEEmbedding(), ChromaVectorStore(chroma))
        except Exception:
            pass
    return HybridRetriever(keyword)


def search_documents(context, args) -> ToolPayload:
    results = _retriever(context).retrieve(args.query, context.workspace_id, args.limit, _filters(args))
    safe = [{
        "document_id": item.get("document_id") or (item.get("metadata") or {}).get("document_id"),
        "title": item.get("title", ""), "publisher": item.get("publisher", ""),
        "published_at": item.get("published_at", ""), "source_url": item.get("canonical_url") or (item.get("metadata") or {}).get("source_url", ""),
        "snippet": str(item.get("chunk_text") or "")[:300],
    } for item in results]
    return _success(f"检索到 {len(safe)} 条证据。", {"results": safe})


def ask_knowledge(context, args) -> ToolPayload:
    settings = load_settings(context.env_path)
    answerer = build_rag_answerer(context.db_path, context.workspace_id, requester=context.requester, settings=settings) if settings.configured and context.workspace.get("ai_enabled") else None
    answer = RAGService(context.db_path, _retriever(context), answerer=answerer).ask(
        args.query, context.workspace_id, args.mode, _filters(args),
    )
    return _success(answer.answer, {"answer": answer.answer, "citations": answer.citations, "model_name": answer.model_name, "evidence_sufficient": answer.evidence_sufficient})


def rebuild_index(context, _args) -> ToolPayload:
    if context.indexer_factory:
        result = context.indexer_factory().rebuild(context.workspace_id)
    else:
        queued = enqueue_background_task(
            context.workspace_id,
            "index_rebuild",
            context.db_path,
            context.data_root,
            input_summary="AI工作台增量构建知识库索引，最多100篇",
            metadata={"limit": 100},
        )
        return _success(
            "知识库索引任务已创建。"
            if queued["created"]
            else "已有索引任务正在运行，未重复提交。",
            {
                "operation_run_id": queued["operation_run_id"],
                "queued": bool(queued["created"]),
            },
        )
    return _success("知识库索引重建完成。", dict(result))


def list_pending(context, _args) -> ToolPayload:
    with connect(context.db_path) as connection:
        rows = connection.execute(
            """SELECT event_id,event_date,title,category,status,risk_level,extraction_confidence,duplicate_level,
            source_url,human_verified,report_eligible FROM events WHERE workspace_id=? AND
            (human_verified=0 OR extraction_confidence<0.6 OR duplicate_level!='未发现明显重复' OR risk_level IN ('中','高'))
            ORDER BY current_priority_score DESC,created_at DESC LIMIT 100""", (context.workspace_id,),
        ).fetchall()
    return _success(f"共有 {len(rows)} 条需要关注。", {"events": [dict(row) for row in rows]})


def approve_events(context, args) -> ToolPayload:
    result = batch_confirm_events(
        args.event_ids,
        context.workspace_id,
        context.db_path,
        report_eligible=True,
        reviewer_type="human_user",
        reviewer_name=str(context.workspace.get("default_analyst") or "本地用户"),
        review_method="AI工作台确认卡片",
        review_version="0.5.0-beta",
        reviewer_note=str(getattr(args, "reviewer_note", "") or ""),
        checklist={"approval_card_confirmed": True},
    )
    return _success(f"已确认 {len(result['confirmed'])} 条，阻止 {len(result['blocked'])} 条。", result)


def reject_events(context, args) -> ToolPayload:
    with transaction(context.db_path) as connection:
        placeholders = ",".join("?" for _ in args.event_ids)
        rows = connection.execute(
            f"SELECT event_id FROM events WHERE workspace_id=? AND event_id IN ({placeholders})",
            [context.workspace_id, *args.event_ids],
        ).fetchall()
        ids = [str(row[0]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"UPDATE events SET report_eligible=0,human_verified=0,analyst_note=?,updated_at=? WHERE workspace_id=? AND event_id IN ({placeholders})",
                [args.reviewer_note or "通过AI工作台退回", now_iso(), context.workspace_id, *ids],
            )
            connection.execute(
                f"UPDATE documents SET review_status='已退回',updated_at=? WHERE document_id IN (SELECT document_id FROM events WHERE workspace_id=? AND event_id IN ({placeholders}))",
                [now_iso(), context.workspace_id, *ids],
            )
    return _success(f"已退回 {len(ids)} 条，历史记录保留。", {"event_ids": ids})


def _event_analysis(context, args, opportunity: bool = False) -> ToolPayload:
    clauses = ["workspace_id=?"]
    params: list[object] = [context.workspace_id]
    if args.start_date:
        clauses.append("event_date>=?"); params.append(args.start_date)
    if args.end_date:
        clauses.append("event_date<=?"); params.append(args.end_date)
    if args.categories:
        placeholders = ",".join("?" for _ in args.categories)
        clauses.append(f"category IN ({placeholders})"); params.extend(args.categories)
    if opportunity:
        clauses.append("opportunity_level IN ('中','高')")
        order = "opportunity_score DESC"
    else:
        clauses.extend(["risk_level IN ('中','高')", "status NOT IN ('解除','已结束')"])
        order = "current_priority_score DESC"
    with connect(context.db_path) as connection:
        rows = connection.execute(
            f"SELECT event_id,event_date,title,category,status,risk_level,opportunity_level,current_priority_score,opportunity_score,summary,impact,source_name,source_url FROM events WHERE {' AND '.join(clauses)} ORDER BY {order},event_date DESC LIMIT 100",
            params,
        ).fetchall()
    items = [dict(row) for row in rows]
    label = "政策与商业机会" if opportunity else "当前中高风险"
    return _success(f"所选期间共发现 {len(items)} 条{label}。", {"count": len(items), "items": items})


def analyze_risks(context, args) -> ToolPayload:
    return _event_analysis(context, args, False)


def analyze_opportunities(context, args) -> ToolPayload:
    return _event_analysis(context, args, True)


def compare_periods(context, args) -> ToolPayload:
    def counts(start, end):
        with connect(context.db_path) as connection:
            row = connection.execute(
                """SELECT COUNT(*) total,SUM(CASE WHEN risk_level IN ('中','高') AND status NOT IN ('解除','已结束') THEN 1 ELSE 0 END) risks,
                SUM(CASE WHEN opportunity_level IN ('中','高') THEN 1 ELSE 0 END) opportunities FROM events
                WHERE workspace_id=? AND event_date>=? AND event_date<=?""", (context.workspace_id, start, end),
            ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "risks", "opportunities")}
    first, second = counts(args.first_start, args.first_end), counts(args.second_start, args.second_end)
    delta = {key: second[key] - first[key] for key in first}
    return _success("两个时期的公开信息变化已完成对比。", {"first": first, "second": second, "change": delta})


def generate_report(context, args) -> ToolPayload:
    if args.formal_publish:
        return ToolPayload(status="error", message="AI工作台只生成报告草稿；正式发布仍需人工确认。")
    paths = context.paths or workspace_paths(context.workspace_id, context.data_root)
    client = {"client_name": args.client_name, "focus_categories": args.categories}
    options = ReportOptions(
        focus_categories_only=bool(args.categories), include_risks=args.include_risks,
        include_opportunities=args.include_opportunities, include_source_appendix=args.include_sources,
    )
    try:
        artifacts = generate_platform_report(
            context.workspace, paths, client=client, start_date=args.start_date, end_date=args.end_date,
            report_title=args.report_title, analyst=str(context.workspace.get("default_analyst") or "PortScope"),
            options=options, db_path=context.db_path, human_verified_only=args.human_verified_only,
            report_mode=args.report_mode,
        )
    except ReportEligibilityError as exc:
        return ToolPayload(status="error", message=str(exc), data={"missing_conditions": exc.reasons, "suggested_mode": "内部研究版"})
    files = [
        {"kind": "DOCX", "filename": artifacts.docx_path.name, "path": str(artifacts.docx_path)},
        {"kind": "HTML", "filename": artifacts.html_path.name, "path": str(artifacts.html_path)},
        {"kind": "Excel", "filename": artifacts.xlsx_path.name, "path": str(artifacts.xlsx_path)},
    ]
    return _success(f"报告草稿已生成：{args.report_title}，共纳入 {len(artifacts.event_ids)} 条事件。", {
        "report_id": artifacts.report_id, "version": artifacts.version, "event_count": len(artifacts.event_ids),
        "start_date": args.start_date, "end_date": args.end_date,
        "report_mode": args.report_mode, "analysis_model": "deterministic-rules",
    }, files)


def list_reports_tool(context, _args) -> ToolPayload:
    reports = load_reports(context.workspace_id, context.db_path)
    return _success(f"共有 {len(reports)} 份历史报告。", {"reports": reports})


def export_data(context, args) -> ToolPayload:
    paths = context.paths or workspace_paths(context.workspace_id, context.data_root)
    output_dir = paths.root / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{args.table}_{date.today().isoformat()}.csv"
    target = output_dir / filename
    temporary = target.with_suffix(".csv.tmp")
    temporary.write_bytes(table_csv_bytes(args.table, context.workspace_id, context.db_path))
    temporary.replace(target)
    return _success("数据已导出。", {"table": args.table, "filename": filename}, [{"kind": "CSV", "filename": filename, "path": str(target)}])


def explain_error(_context, args) -> ToolPayload:
    text = args.error_text.lower()
    if "robots" in text:
        reason, retry = "网站robots规则未确认允许，系统按合规边界停止访问。", False
    elif "403" in text or "拒绝" in text or "login" in text:
        reason, retry = "页面拒绝公开访问或需要登录，系统不会绕过访问控制。", False
    elif "timeout" in text or "超时" in text:
        reason, retry = "网站响应超时，可能是临时网络或站点负载问题。", True
    elif "selector" in text or "选择器" in text:
        reason, retry = "网页结构与当前适配规则不匹配，需要重新分析栏目结构。", False
    elif "embedding" in text or "向量" in text:
        reason, retry = "本地向量模型或索引尚未就绪，关键词检索仍可使用。", True
    else:
        reason, retry = "任务未完成，已安全停止且不会影响其他来源或已保存数据。", True
    action = "可以在确认网络恢复后重试。" if retry else "请人工核对网站公开访问条件或适配配置。"
    return _success(reason + action, {"source_name": args.source_name, "can_retry": retry, "recommended_action": action})


TOOL_HANDLERS = {
    "get_system_status": get_system_status,
    "configure_ai": configure_ai,
    "test_ai_connection": test_ai,
    "list_sources": list_sources_tool,
    "initialize_recommended_sources": initialize_recommended,
    "analyze_source_url": analyze_source,
    "propose_source_adapter": propose_source,
    "add_source": add_source,
    "enable_source": enable_source,
    "disable_source": disable_source,
    "run_crawl": run_crawl,
    "get_crawl_progress": get_crawl,
    "get_crawl_result": get_crawl,
    "retry_failed_sources": retry_failed,
    "search_documents": search_documents,
    "ask_knowledge_base": ask_knowledge,
    "rebuild_index": rebuild_index,
    "list_pending_reviews": list_pending,
    "approve_events": approve_events,
    "reject_events": reject_events,
    "analyze_risks": analyze_risks,
    "analyze_opportunities": analyze_opportunities,
    "compare_periods": compare_periods,
    "generate_report": generate_report,
    "list_reports": list_reports_tool,
    "export_data": export_data,
    "explain_error": explain_error,
}
