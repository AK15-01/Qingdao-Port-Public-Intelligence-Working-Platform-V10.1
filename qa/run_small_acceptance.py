from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commercial_report import ReportOptions
from crawler import CrawlManager
from deepseek_service import load_settings
from platform_db import connect, initialize_database, upsert_source
from platform_report import generate_platform_report
from rag.embeddings import LocalBGEEmbedding
from rag.hybrid_retriever import HybridRetriever
from rag.indexer import KnowledgeIndexer
from rag.keyword_search import KeywordSearcher
from rag.rag_service import RAGService
from rag.vector_store import ChromaVectorStore
from workspace_store import create_workspace, workspace_paths


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    settings = load_settings()
    if not settings.api_key:
        print(json.dumps({"status": "blocked", "reason": "DeepSeek API Key未配置"}, ensure_ascii=False))
        return 2
    run_root = ROOT / "output" / "qa_validation" / f"run-{datetime.now():%Y%m%d-%H%M%S}"
    data_root = run_root / "data"
    db_path = run_root / "portscope_acceptance.db"
    workspace = create_workspace({"workspace_name": "真实小规模验收", "ai_enabled": True}, db_path, data_root)
    initialize_database(db_path)
    source_id = upsert_source({
        "workspace_id": workspace["workspace_id"],
        "source_name": "山东海事局政务动态（小规模验收）",
        "organization": "中华人民共和国山东海事局",
        "domain": "www.sd.msa.gov.cn",
        "homepage_url": "https://www.sd.msa.gov.cn/",
        "list_page_url": "https://www.sd.msa.gov.cn/col/col1443/index.html",
        "source_type": "政府/监管机构",
        "category_hint": "政策监管",
        "region": "山东沿海",
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
        "report_use_allowed": False,
        "rate_limit_seconds": 3,
        "max_pages_per_run": 1,
        "max_articles_per_run": 2,
    }, db_path)
    chroma_path = run_root / "chroma"
    indexer = KnowledgeIndexer(db_path, chroma_path)
    manager = CrawlManager(
        db_path, data_root, vector_indexer=indexer, ai_enabled=True, ai_settings=settings,
    )
    result = manager.run(
        workspace["workspace_id"], source_ids=[source_id], max_articles=2,
        start_date="2026-07-17", end_date="2026-07-23",
    )

    vector_store = ChromaVectorStore(chroma_path)
    retriever = HybridRetriever(KeywordSearcher(db_path), LocalBGEEmbedding(), vector_store)
    rag_answer = RAGService(db_path, retriever).ask(
        "最近公开信息中有哪些需要关注的海事风险？",
        workspace["workspace_id"], "风险分析",
        filters={"start_date": "2026-07-17", "end_date": "2026-07-23"},
    )
    report_path = ""
    report_error = ""
    try:
        artifacts = generate_platform_report(
            workspace, workspace_paths(workspace["workspace_id"], data_root),
            client=None, start_date="2026-07-17", end_date="2026-07-23",
            report_title="青岛港公开信息小规模内部研究样刊", analyst="PortScope QA",
            options=ReportOptions(report_mode="内部研究版"),
            db_path=db_path, human_verified_only=False, report_mode="内部研究版",
        )
        report_path = str(artifacts.docx_path)
    except Exception as exc:
        report_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    with connect(db_path) as connection:
        documents = [dict(row) for row in connection.execute(
            """SELECT document_id,title,published_at,extraction_status,quality_status,ai_status,
            report_quality_eligible FROM documents ORDER BY published_at DESC"""
        ).fetchall()]
        events = [dict(row) for row in connection.execute(
            """SELECT event_id,extraction_method,extraction_confidence,ai_generated,human_verified,
            risk_level,opportunity_level,analyst_note FROM events ORDER BY event_date DESC"""
        ).fetchall()]
        logs = [dict(row) for row in connection.execute(
            """SELECT model_name,input_characters,output_characters,elapsed_ms,success,error_type
            FROM ai_call_logs ORDER BY created_at"""
        ).fetchall()]
        embedded = int(connection.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE embedding_status='成功'"
        ).fetchone()[0])
    summary = {
        "status": result.status,
        "run_root": str(run_root),
        "source_count": result.source_count,
        "discovered_count": result.discovered_count,
        "fetched_count": result.fetched_count,
        "new_document_count": result.new_document_count,
        "new_event_count": result.new_event_count,
        "out_of_range_count": result.out_of_range_count,
        "encoding_blocked_count": result.encoding_blocked_count,
        "noise_blocked_count": result.noise_blocked_count,
        "api_call_count": result.api_call_count,
        "actual_models": result.actual_models,
        "stage_stats": result.stage_stats,
        "documents": documents,
        "events": events,
        "ai_call_logs": logs,
        "embedded_chunks": embedded,
        "rag_evidence_sufficient": rag_answer.evidence_sufficient,
        "rag_citation_count": len(rag_answer.citations),
        "report_path": report_path,
        "report_error": report_error,
    }
    result_path = run_root / "acceptance_summary.json"
    result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_output = {
        key: summary[key] for key in (
            "status", "run_root", "source_count", "discovered_count", "fetched_count",
            "new_document_count", "new_event_count", "out_of_range_count",
            "encoding_blocked_count", "noise_blocked_count", "api_call_count",
            "actual_models", "embedded_chunks", "rag_evidence_sufficient",
            "rag_citation_count", "report_path", "report_error",
        )
    }
    print(json.dumps(safe_output, ensure_ascii=False, indent=2))
    return 0 if result.status in {"完成", "部分完成"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
