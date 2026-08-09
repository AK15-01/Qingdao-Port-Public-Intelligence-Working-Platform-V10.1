"""Generate a clearly fictional SQLite-backed report bundle for visual QA."""

from pathlib import Path

from commercial_report import ReportOptions
from document_chunker import build_chunk_rows
from document_processor import store_document
from platform_db import initialize_database, new_id, now_iso, transaction, upsert_source
from platform_report import generate_platform_report
from risk_engine import calculate_scores
from workspace_store import create_client, create_workspace, workspace_paths


def main() -> None:
    qa_root = Path("output/qa_validation")
    qa_db = qa_root / "qa.db"
    qa_data = qa_root / "data"
    workspace = create_workspace(
        {
            "workspace_name": "虚构演示QA工作空间",
            "default_report_title": "虚构演示：青岛港公开数据情报报告",
            "default_analyst": "PortScope QA",
        },
        qa_db,
        qa_data,
    )
    initialize_database(qa_db)
    paths = workspace_paths(str(workspace["workspace_id"]), qa_data)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "虚构演示公开来源",
            "organization": "虚构演示公开机构",
            "domain": "example.invalid",
            "homepage_url": "https://example.invalid/",
            "list_page_url": "https://example.invalid/list",
            "source_type": "政府/监管机构",
            "category_hint": "港口作业",
            "robots_status": "允许",
            "terms_status": "允许",
            "commercial_reuse_status": "允许",
            "license_note": "仅用于本地虚构演示QA，不代表真实网站许可。",
            "report_use_allowed": True,
        },
        qa_db,
    )
    event_ids = []
    fixtures = [
        ("港口作业", "虚构演示：计划检修可能影响部分作业窗口", "新增", "虚构演示事实：某公开测试页面说明计划检修安排，仅用于报告验收。"),
        ("招标采购", "虚构演示：数字化服务采购公告", "新增", "虚构演示事实：测试采购公告列出服务范围和响应期限。"),
        ("海上气象", "虚构演示：测试海上天气事项已结束", "已结束", "虚构演示事实：测试页面说明天气事项已结束，仅用于报告版式验收。"),
    ]
    for index, (category, title, status, text) in enumerate(fixtures, 1):
        document = {
            "workspace_id": workspace["workspace_id"],
            "source_id": source_id,
            "canonical_url": f"https://example.invalid/qa/{index}",
            "original_url": f"https://example.invalid/qa/{index}",
            "title": title,
            "publisher": "虚构演示公开机构",
            "published_at": f"2026-07-{17 + index:02d}",
            "fetched_at": f"2026-07-{17 + index:02d}T09:00:00+08:00",
            "raw_html": f"<html><body><article><p>{text}</p></article></body></html>",
            "cleaned_text": text,
            "extraction_status": "成功",
            "extraction_quality": "虚构演示QA",
            "http_status": 200,
        }
        stored = store_document(
            document,
            qa_db,
            qa_data,
            build_chunk_rows(text, {"workspace_id": workspace["workspace_id"], "source_id": source_id, "source_url": document["canonical_url"]}),
        )
        document["document_id"] = stored.document_id
        event_id = new_id("EVT")
        event = {
            "category": category, "title": title, "summary": text,
            "impact": "虚构演示判断：相关业务安排可能变化，需要核对正式公开来源。",
            "source_type": "政府/监管机构", "status": status,
            "recommended_action": "虚构演示建议：核对原始来源并持续跟踪。",
        }
        scores = calculate_scores(event)
        timestamp = now_iso()
        with transaction(qa_db) as connection:
            connection.execute(
                """INSERT INTO events(event_id,workspace_id,document_id,event_date,collected_at,category,title,summary,
                impact,affected_area,affected_period,source_name,source_type,source_url,status,extraction_method,
                extraction_confidence,ai_generated,human_verified,verified_at,report_eligible,duplicate_level,
                raw_risk_score,historical_risk_score,current_priority_score,source_confidence,opportunity_score,
                risk_level,opportunity_level,matched_terms,score_explanation,recommended_action,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, workspace["workspace_id"], stored.document_id, document["published_at"], document["fetched_at"],
                 category, title, text, event["impact"], "虚构演示区域", "虚构演示时段", "虚构演示公开来源",
                 "政府/监管机构", document["canonical_url"], status, "rules", 1.0, 0, 1, timestamp, 1,
                 "未发现明显重复", scores["raw_risk_score"], scores["historical_risk_score"], scores["current_priority_score"],
                 scores["source_confidence"], scores["opportunity_score"], scores["risk_level"], scores["opportunity_level"],
                 scores["matched_terms"], scores["score_explanation"], scores["action"], timestamp, timestamp),
            )
        event_ids.append(event_id)
    client = create_client(
        str(workspace["workspace_id"]),
        {
            "client_name": "虚构演示客户",
            "industry": "虚构演示行业",
            "focus_categories": ["港口作业", "招标采购", "海上气象"],
            "report_title": "虚构演示：青岛港公开数据情报报告",
            "analyst_name": "PortScope QA",
            "company_name": "虚构演示机构",
            "enabled": True,
        },
        qa_db,
    )
    artifacts = generate_platform_report(
        workspace,
        paths,
        client=client,
        start_date="2026-07-14",
        end_date="2026-07-20",
        report_title="虚构演示：青岛港公开数据情报报告",
        analyst="PortScope QA",
        options=ReportOptions(show_score_details=True),
        db_path=qa_db,
    )
    print(artifacts.docx_path.resolve())
    print(artifacts.html_path.resolve())
    print(artifacts.xlsx_path.resolve())


if __name__ == "__main__":
    main()
