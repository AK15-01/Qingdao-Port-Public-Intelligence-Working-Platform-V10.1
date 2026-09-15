from __future__ import annotations

from datetime import date, datetime
import argparse
import json
from pathlib import Path
import sys
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from commercial_report import ReportOptions
from business_value import classify_business_value
from crawler import CrawlManager
from deepseek_service import build_rag_answerer, load_settings
from event_pipeline import reprocess_pending_ai
from platform_db import (
    connect,
    initialize_database,
    initialize_recommended_sources,
    list_sources,
    now_iso,
    upsert_source,
)
from platform_report import ReportEligibilityError, generate_platform_report
from risk_engine import calculate_scores
from qa.evaluate_real_acceptance import (
    calculate_human_metrics,
    collect_records,
    read_labels_xlsx,
)
from rag.embeddings import LocalBGEEmbedding
from rag.hybrid_retriever import HybridRetriever
from rag.indexer import KnowledgeIndexer
from rag.keyword_search import KeywordSearcher
from rag.rag_service import RAGService
from rag.vector_store import ChromaVectorStore
from workspace_store import create_workspace, get_workspace, workspace_paths


PHASE_LIMITS = {"A": 2, "B": 5, "C": 15}
RUN_INFO = "qa_run.json"
SOURCE_STATS = "source_stats.json"


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _new_run_root(output_root: Path) -> Path:
    target = output_root / f"run-{datetime.now():%Y%m%d-%H%M%S}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def ensure_qa_notice_source(db_path: Path, workspace_id: str) -> str:
    """Add the static official-notice column used by the isolated QA run."""
    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT source_id FROM sources WHERE workspace_id=? AND source_name=? LIMIT 1",
            (workspace_id, "山东海事局通知公告"),
        ).fetchone()
    return upsert_source(
        {
            "source_id": str(existing["source_id"]) if existing else "",
            "workspace_id": workspace_id,
            "source_name": "山东海事局通知公告",
            "organization": "中华人民共和国山东海事局",
            "domain": "www.sd.msa.gov.cn",
            "homepage_url": "https://www.sd.msa.gov.cn/",
            "list_page_url": "https://www.sd.msa.gov.cn/",
            "source_type": "政府/监管机构",
            "category_hint": "政策监管",
            "region": "山东沿海",
            "business_value_level": "高",
            "primary_users": ["货代", "货主", "船舶代理", "港航供应商"],
            "information_types": ["航运政策", "通航管理", "危货监管", "采购公告"],
            "list_stability": "待阶段A验证",
            "content_stability": "待阶段A验证",
            "adapter_type": "shandong_maritime",
            "adapter_config": {
                "article_link_selector": "a[href*='art_124_']",
                "allowed_path_prefix": "/art/",
                "url_pattern": r"^https://www\.sd\.msa\.gov\.cn/art/20\d{2}/.+art_124_\d+\.html$",
                "content_selector": "#zoom",
                "content_fallback_selector": "article, main",
                "title_selector": "meta[name='ArticleTitle']",
                "date_selector": "meta[name='pubdate']",
                "publisher_selector": "meta[name='ContentSource']",
                "follow_pdf_attachments": True,
                "pdf_link_selector": "#zoom a[href*='.pdf'], #zoom a[href*='downfile']",
                "site_names": ["山东海事局", "中华人民共和国山东海事局"],
                "exclude_selectors": ["script", "style", "nav", "footer"],
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
            "terms_status": "需人工确认",
            "commercial_reuse_status": "未明确",
            "license_note": "仅用于低频内部研究；商业引用和转载边界需逐项人工复核。",
            "report_use_allowed": False,
            "rate_limit_seconds": 3,
            "max_pages_per_run": 1,
            "max_articles_per_run": 15,
        },
        db_path,
    )


def ensure_qa_safety_analysis_source(db_path: Path, workspace_id: str) -> str:
    """Add the official safety-analysis series exposed as static links."""
    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT source_id FROM sources WHERE workspace_id=? AND source_name=? LIMIT 1",
            (workspace_id, "山东海事局安全形势分析"),
        ).fetchone()
    return upsert_source(
        {
            "source_id": str(existing["source_id"]) if existing else "",
            "workspace_id": workspace_id,
            "source_name": "山东海事局安全形势分析",
            "organization": "中华人民共和国山东海事局",
            "domain": "www.sd.msa.gov.cn",
            "homepage_url": "https://www.sd.msa.gov.cn/",
            "list_page_url": "https://www.sd.msa.gov.cn/col/col4629/index.html",
            "source_type": "政府/监管机构",
            "category_hint": "航行警告",
            "region": "山东沿海",
            "business_value_level": "高",
            "primary_users": ["货代", "货主", "航运企业", "港航供应商"],
            "information_types": ["事故形势", "航运公司监管", "安全趋势"],
            "list_stability": "待阶段A验证",
            "content_stability": "待阶段A验证",
            "adapter_type": "shandong_maritime",
            "adapter_config": {
                "article_link_selector": "a[href*='art_4631_']",
                "allowed_path_prefix": "/art/",
                "url_pattern": r"^https://www\.sd\.msa\.gov\.cn/art/20\d{2}/.+art_4631_\d+\.html$",
                "content_selector": "#zoom",
                "content_fallback_selector": "article, main",
                "title_selector": "meta[name='ArticleTitle']",
                "date_selector": "meta[name='pubdate']",
                "publisher_selector": "meta[name='ContentSource']",
                "follow_pdf_attachments": True,
                "pdf_link_selector": "#zoom a[href*='.pdf'], #zoom a[href*='downfile']",
                "site_names": ["山东海事局", "中华人民共和国山东海事局"],
                "exclude_selectors": ["script", "style", "nav", "footer"],
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
            "terms_status": "需人工确认",
            "commercial_reuse_status": "未明确",
            "license_note": "仅用于低频内部研究；事故与监管形势资料的商业引用边界需人工复核。",
            "report_use_allowed": False,
            "rate_limit_seconds": 3,
            "max_pages_per_run": 1,
            "max_articles_per_run": 15,
        },
        db_path,
    )


def ensure_qa_public_affairs_source(db_path: Path, workspace_id: str) -> str:
    """Add the official public-affairs column with stable static article bodies."""
    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT source_id FROM sources WHERE workspace_id=? AND source_name=? LIMIT 1",
            (workspace_id, "山东海事局政务动态"),
        ).fetchone()
    return upsert_source(
        {
            "source_id": str(existing["source_id"]) if existing else "",
            "workspace_id": workspace_id,
            "source_name": "山东海事局政务动态",
            "organization": "中华人民共和国山东海事局",
            "domain": "www.sd.msa.gov.cn",
            "homepage_url": "https://www.sd.msa.gov.cn/",
            "list_page_url": "https://www.sd.msa.gov.cn/col/col1443/index.html",
            "source_type": "政府/监管机构",
            "category_hint": "企业动态",
            "region": "山东沿海",
            "business_value_level": "中",
            "primary_users": ["货代", "货主", "船舶代理", "港航供应商"],
            "information_types": ["海事监管", "通航服务", "港航动态"],
            "list_stability": "待阶段A验证",
            "content_stability": "待阶段A验证",
            "adapter_type": "shandong_maritime",
            "adapter_config": {
                "article_link_selector": "a.font14[href]",
                "allowed_path_prefix": "/art/",
                "url_pattern": r"^https://www\.sd\.msa\.gov\.cn/art/20\d{2}/",
                "content_selector": "#zoom",
                "content_fallback_selector": "article, main",
                "title_selector": "meta[name='ArticleTitle']",
                "date_selector": "meta[name='pubdate']",
                "publisher_selector": "meta[name='ContentSource']",
                "site_names": ["山东海事局", "中华人民共和国山东海事局"],
                "exclude_selectors": ["script", "style", "nav", "footer"],
            },
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
            "terms_status": "需人工确认",
            "commercial_reuse_status": "未明确",
            "license_note": "仅用于低频内部研究；宣传类低价值稿默认不进入执行摘要或核心风险。",
            "report_use_allowed": False,
            "rate_limit_seconds": 3,
            "max_pages_per_run": 1,
            "max_articles_per_run": 15,
        },
        db_path,
    )


def initialize_qa_run(run_root: Path) -> dict[str, object]:
    data_root = run_root / "data"
    db_path = run_root / "portscope_qa.db"
    workspace = create_workspace(
        {
            "workspace_name": "PortScope真实资料QA",
            "industry": "港航物流公开信息",
            "region": "山东及青岛",
            "default_report_title": "青岛港公开信息内部研究样刊",
            "default_analyst": "PortScope QA",
            "ai_enabled": True,
        },
        db_path,
        data_root,
    )
    initialize_database(db_path)
    initialized = initialize_recommended_sources(workspace["workspace_id"], db_path)
    ensure_qa_notice_source(db_path, str(workspace["workspace_id"]))
    ensure_qa_safety_analysis_source(db_path, str(workspace["workspace_id"]))
    ensure_qa_public_affairs_source(db_path, str(workspace["workspace_id"]))
    payload = {
        "run_root": str(run_root.resolve()),
        "db_path": str(db_path.resolve()),
        "data_root": str(data_root.resolve()),
        "workspace_id": workspace["workspace_id"],
        "initialized_sources": initialized,
        "created_at": now_iso(),
    }
    _atomic_json(run_root / RUN_INFO, payload)
    _atomic_json(run_root / SOURCE_STATS, {"source_stats": []})
    return payload


def load_run(run_root: Path) -> dict[str, object]:
    path = run_root / RUN_INFO
    if not path.is_file():
        raise FileNotFoundError("QA运行目录缺少qa_run.json；请先执行阶段A。")
    payload = json.loads(path.read_text(encoding="utf-8"))
    db_path = Path(str(payload["db_path"]))
    data_root = Path(str(payload["data_root"]))
    if run_root.resolve() not in db_path.resolve().parents or run_root.resolve() not in data_root.resolve().parents:
        raise ValueError("QA数据库或数据目录不在当前独立QA运行目录中，已停止。")
    return payload


def _source_stats(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("source_stats", []))


def pause_qa_source(
    db_path: Path,
    workspace_id: str,
    source_id: str,
    stats_path: Path,
    *,
    health_status: str,
    reason: str,
) -> None:
    with connect(db_path) as connection:
        connection.execute(
            """UPDATE sources SET enabled=0,crawl_allowed=0,health_status=?,content_stability=?,
            last_health_check_at=?,last_error=?,updated_at=? WHERE workspace_id=? AND source_id=?""",
            (
                health_status,
                health_status,
                now_iso(),
                reason[:1000],
                now_iso(),
                workspace_id,
                source_id,
            ),
        )
        connection.commit()
    payload = {"source_stats": _source_stats(stats_path)}
    for row in payload["source_stats"]:
        if str(row.get("source_id")) == source_id:
            row["active"] = False
            row["health"] = health_status
    _atomic_json(stats_path, payload)


def quarantine_qa_document(
    db_path: Path,
    document_id: str,
    *,
    reason: str,
    chroma_path: Path | None = None,
) -> None:
    """Remove a post-review quality failure from indexes without deleting its audit record."""
    with connect(db_path) as connection:
        connection.execute(
            """UPDATE documents SET quality_status='正文质量不足',extraction_status='PDF有效文本不足',
            report_quality_eligible=0,ai_status='质量门禁拦截',review_status='质量异常',
            extraction_quality=TRIM(extraction_quality || '｜人工QA复核：' || ?),updated_at=?
            WHERE document_id=?""",
            (reason[:1000], now_iso(), document_id),
        )
        connection.execute(
            """UPDATE events SET report_eligible=0,human_verified=0,
            analyst_note=TRIM(analyst_note || ' 人工QA复核：原文有效文本不足，禁止进入报告。'),
            updated_at=? WHERE document_id=?""",
            (now_iso(), document_id),
        )
        connection.execute("DELETE FROM document_chunks_fts WHERE document_id=?", (document_id,))
        connection.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
        connection.commit()
    if chroma_path:
        ChromaVectorStore(chroma_path).delete_document(document_id)


def refresh_qa_event_analysis(db_path: Path, workspace_id: str) -> int:
    """Reapply current deterministic business-value and risk rules to QA events."""
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
        for raw in rows:
            event = dict(raw)
            value = classify_business_value(event)
            scores = calculate_scores(event)
            connection.execute(
                """UPDATE events SET business_value=?,value_reason=?,raw_risk_score=?,
                historical_risk_score=?,current_priority_score=?,source_confidence=?,
                opportunity_score=?,risk_level=?,opportunity_level=?,matched_terms=?,
                score_explanation=?,recommended_action=?,updated_at=? WHERE event_id=?""",
                (
                    value.level,
                    value.reason,
                    scores["raw_risk_score"],
                    scores["historical_risk_score"],
                    scores["current_priority_score"],
                    scores["source_confidence"],
                    scores["opportunity_score"],
                    scores["risk_level"],
                    scores["opportunity_level"],
                    scores["matched_terms"],
                    scores["score_explanation"],
                    scores["action"],
                    now_iso(),
                    event["event_id"],
                ),
            )
        connection.commit()
    return len(rows)


def _health(result) -> str:
    quality = result.stage_stats.get("内容质量门禁", {})
    inputs = int(quality.get("input", 0) or 0)
    success = int(quality.get("success", 0) or 0)
    if any("页面需要JavaScript" in str(error) for error in result.errors):
        return "需要JavaScript"
    if result.failed_count and not success:
        return "连接失败"
    if result.pdf_success_count and success == inputs:
        return "PDF可用"
    if inputs and success / inputs >= 0.9:
        return "正常"
    if inputs and success / inputs >= 0.5:
        return "部分可用"
    if result.noise_blocked_count:
        return "正文结构变化"
    return "暂停使用"


def _update_source_health(db_path: Path, workspace_id: str, source_id: str, result) -> None:
    health = _health(result)
    details = {
        "discovered": result.discovered_count,
        "fetched": result.fetched_count,
        "quality": result.stage_stats.get("内容质量门禁", {}),
        "pdf_discovered": result.pdf_discovered_count,
        "pdf_success": result.pdf_success_count,
        "pdf_rejected": result.pdf_rejected_count,
        "errors": result.errors[:5],
    }
    with connect(db_path) as connection:
        connection.execute(
            """UPDATE sources SET health_status=?,last_health_check_at=?,health_details_json=?,
            list_stability=?,content_stability=?,updated_at=?
            WHERE workspace_id=? AND source_id=?""",
            (
                health,
                now_iso(),
                json.dumps(details, ensure_ascii=False),
                "正常" if result.discovered_count else "正文结构变化",
                health,
                now_iso(),
                workspace_id,
                source_id,
            ),
        )
        connection.commit()


def _phase_a_gate(stats: list[dict[str, object]]) -> tuple[bool, str]:
    latest: dict[str, dict[str, object]] = {}
    for row in stats:
        if row.get("phase") == "A":
            latest[str(row.get("source_id") or row.get("source_name"))] = row
    rows = [row for row in latest.values() if bool(row.get("active", True))]
    if not rows:
        return False, "阶段A尚未执行。"
    failed = []
    for row in rows:
        discovered = int(row.get("discovered", 0) or 0)
        success = int(row.get("success", 0) or 0)
        if not discovered or success / discovered < 0.5:
            failed.append(str(row.get("source_name") or row.get("source_id")))
    if failed:
        return False, "以下来源阶段A正文合格率低于50%：" + "、".join(failed)
    return True, ""


def _phase_b_gate(
    db_path: Path,
    workspace_id: str,
    labels_path: Path,
) -> tuple[bool, str, dict[str, object]]:
    records = collect_records(db_path, workspace_id)
    human = calculate_human_metrics(records, read_labels_xlsx(labels_path))
    with connect(db_path) as connection:
        logs = connection.execute(
            "SELECT success FROM ai_call_logs WHERE workspace_id=? AND task_type='extract_event'",
            (workspace_id,),
        ).fetchall()
    ai_rate = sum(int(row["success"] or 0) for row in logs) / len(logs) if logs else 0
    conditions = {
        "人工核验数量至少10篇": human["completed_count"] >= min(10, len(records)),
        "标题正确率不低于95%": human["title_accuracy"] is not None and human["title_accuracy"] >= 0.95,
        "日期正确率不低于90%": human["date_accuracy"] is not None and human["date_accuracy"] >= 0.90,
        "机构正确率不低于95%": human["publisher_accuracy"] is not None and human["publisher_accuracy"] >= 0.95,
        "正文合格率不低于90%": human["body_quality_accuracy"] is not None and human["body_quality_accuracy"] >= 0.90,
        "DeepSeek成功率不低于90%": ai_rate >= 0.90,
        "引用可在原文定位": human["evidence_traceability"] is not None and human["evidence_traceability"] >= 0.90,
    }
    failed = [label for label, passed in conditions.items() if not passed]
    return not failed, "；".join(failed), {**human, "deepseek_success_rate": round(ai_rate, 4)}


def run_phase(
    run_root: Path,
    phase: str,
    *,
    start_date: str,
    end_date: str,
    labels_path: Path,
) -> dict[str, object]:
    info = load_run(run_root)
    db_path = Path(str(info["db_path"]))
    data_root = Path(str(info["data_root"]))
    workspace_id = str(info["workspace_id"])
    sources = list_sources(workspace_id, db_path, enabled_only=True)
    stats_path = run_root / SOURCE_STATS
    all_stats = _source_stats(stats_path)
    if phase == "B":
        allowed, reason = _phase_a_gate(all_stats)
        records = collect_records(db_path, workspace_id)
        human = calculate_human_metrics(records, read_labels_xlsx(labels_path))
        if not allowed:
            raise RuntimeError(reason)
        if human["completed_count"] < len(records):
            raise RuntimeError("阶段A尚未完成人工金标准标注，不能扩大到阶段B。")
    if phase == "C":
        allowed, reason, _ = _phase_b_gate(db_path, workspace_id, labels_path)
        if not allowed:
            raise RuntimeError("阶段B未达到扩大采集门槛：" + reason)

    settings = load_settings()
    if not settings.api_key:
        raise RuntimeError("项目.env未配置DeepSeek API Key；为避免低质量数据扩量，真实QA已停止。")
    chroma_path = run_root / "chroma"
    indexer = KnowledgeIndexer(db_path, chroma_path)
    phase_rows: list[dict[str, object]] = []
    manager = CrawlManager(
        db_path,
        data_root,
        vector_indexer=indexer,
        ai_enabled=True,
        ai_settings=settings,
    )
    for source in sources:
        result = manager.run(
            workspace_id,
            source_ids=[str(source["source_id"])],
            max_articles=PHASE_LIMITS[phase],
            start_date=start_date,
            end_date=end_date,
        )
        quality = result.stage_stats.get("内容质量门禁", {})
        success = int(quality.get("success", 0) or 0)
        rejected = int(quality.get("failed", 0) or 0) + int(result.failed_count)
        row = {
            "phase": phase,
            "active": True,
            "source_id": source["source_id"],
            "source_name": source["source_name"],
            "discovered": result.discovered_count,
            "fetched": result.fetched_count,
            "success": success,
            "rejected": rejected,
            "new_documents": result.new_document_count,
            "new_events": result.new_event_count,
            "pdf_discovered": result.pdf_discovered_count,
            "pdf_success": result.pdf_success_count,
            "pdf_rejected": result.pdf_rejected_count,
            "encoding_blocked": result.encoding_blocked_count,
            "noise_blocked": result.noise_blocked_count,
            "out_of_range": result.out_of_range_count,
            "api_calls": result.api_call_count,
            "actual_models": result.actual_models,
            "health": _health(result),
            "errors": result.errors[:5],
        }
        phase_rows.append(row)
        _update_source_health(db_path, workspace_id, str(source["source_id"]), result)
    all_stats.extend(phase_rows)
    _atomic_json(stats_path, {"source_stats": all_stats})

    # API failure does not trigger a second network fetch. Retry only the stored,
    # quality-approved pending documents, then refresh their local vector metadata.
    retry = reprocess_pending_ai(workspace_id, db_path, limit=30, settings=settings)
    if retry["success"]:
        with connect(db_path) as connection:
            document_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT document_id FROM events WHERE workspace_id=? AND event_id IN (%s)"
                    % ",".join("?" for _ in retry["event_ids"]),
                    [workspace_id, *retry["event_ids"]],
                ).fetchall()
            ]
        for document_id in document_ids:
            indexer.index_document(document_id)

    summary = {
        "phase": phase,
        "run_root": str(run_root),
        "workspace_id": workspace_id,
        "source_results": phase_rows,
        "pending_ai_retry": {
            "input": retry["input"],
            "success": retry["success"],
            "failed": retry["failed"],
            "models": retry["models"],
        },
    }
    _atomic_json(run_root / f"phase_{phase}_summary.json", summary)
    return summary


def validate_rag_and_report(run_root: Path, start_date: str, end_date: str) -> dict[str, object]:
    info = load_run(run_root)
    db_path = Path(str(info["db_path"]))
    data_root = Path(str(info["data_root"]))
    workspace_id = str(info["workspace_id"])
    workspace = get_workspace(workspace_id, db_path)
    if not workspace:
        raise RuntimeError("QA工作空间不存在。")
    settings = load_settings()
    vector_store = ChromaVectorStore(run_root / "chroma")
    retriever = HybridRetriever(KeywordSearcher(db_path), LocalBGEEmbedding(), vector_store)
    answerer = build_rag_answerer(settings) if settings.api_key else None
    question = "本期公开资料中有哪些对港航、货代或物流业务值得关注的风险和机会？"
    answer = RAGService(db_path, retriever, answerer=answerer).ask(
        question,
        workspace_id,
        "风险分析",
        filters={
            "start_date": start_date,
            "end_date": end_date,
            "business_value": ["高", "中"],
        },
    )
    report = {"generated": False, "error": "", "docx": "", "html": "", "xlsx": ""}
    try:
        artifacts = generate_platform_report(
            workspace,
            workspace_paths(workspace_id, data_root),
            client=None,
            start_date=start_date,
            end_date=end_date,
            report_title="青岛港公开信息真实数据内部研究样刊",
            analyst="PortScope QA",
            options=ReportOptions(
                report_mode="内部研究版",
                require_business_value=True,
                include_source_appendix=True,
            ),
            db_path=db_path,
            human_verified_only=False,
            report_mode="内部研究版",
        )
        report.update(
            {
                "generated": True,
                "docx": str(artifacts.docx_path),
                "html": str(artifacts.html_path),
                "xlsx": str(artifacts.xlsx_path),
                "event_count": len(artifacts.event_ids),
            }
        )
    except ReportEligibilityError as exc:
        report["error"] = str(exc)
    result = {
        "question": question,
        "answer": answer.answer,
        "citation_count": len(answer.citations),
        "citations": answer.citations,
        "model_name": answer.model_name,
        "report": report,
    }
    _atomic_json(run_root / "rag_report_validation.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="独立QA工作空间中的分阶段真实公开资料验收。")
    parser.add_argument("--phase", choices=["A", "B", "C", "report"], required=True)
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--output-root", type=Path, default=ROOT / "output" / "qa_real")
    parser.add_argument("--labels", type=Path, default=ROOT / "qa" / "real_acceptance_labels.xlsx")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    args = parser.parse_args()
    if args.phase == "A" and args.run_root is None:
        run_root = _new_run_root(args.output_root)
        initialize_qa_run(run_root)
    elif args.run_root is not None:
        run_root = args.run_root.resolve()
    else:
        parser.error("阶段B、C和report必须提供--run-root。")
    if args.phase == "report":
        result = validate_rag_and_report(run_root, args.start_date, args.end_date)
    else:
        result = run_phase(
            run_root,
            args.phase,
            start_date=args.start_date,
            end_date=args.end_date,
            labels_path=args.labels,
        )
    safe = {
        "phase": args.phase,
        "run_root": str(run_root),
        "result": result if args.phase == "report" else {
            "source_results": result["source_results"],
            "pending_ai_retry": result["pending_ai_retry"],
        },
    }
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
