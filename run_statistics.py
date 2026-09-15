from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from platform_db import connect, initialize_database, new_id, now_iso, transaction
from review_provenance import HUMAN_REVIEWER_TYPES


OPERATIONAL_STATUS_LABELS = {
    "stable": "稳定",
    "degraded": "降级",
    "manual_only": "仅人工导入",
    "disabled": "已停用",
    "needs_adapter": "需要适配器",
    "permission_review": "许可待复核",
}


def source_run_statistics(
    workspace_id: str,
    db_path,
    *,
    start_date: str = "",
    end_date: str = "",
) -> list[dict[str, object]]:
    """Aggregate persisted, real crawl executions only."""

    initialize_database(db_path)
    where = ["r.workspace_id=?"]
    params: list[object] = [workspace_id]
    if start_date:
        where.append("substr(r.started_at,1,10)>=?")
        params.append(start_date)
    if end_date:
        where.append("substr(r.started_at,1,10)<=?")
        params.append(end_date)
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT s.source_id,s.source_name,s.operational_status,s.last_success_at,
            s.consecutive_failures,COUNT(r.source_run_id) AS run_count,
            COALESCE(SUM(r.request_count),0) AS request_count,
            COALESCE(SUM(r.fetched_count),0) AS fetched_count,
            COALESCE(SUM(r.new_document_count+r.updated_document_count),0) AS document_count,
            COALESCE(SUM(r.quality_failed_count),0) AS quality_failed_count,
            COALESCE(SUM(r.qualified_document_count),0) AS qualified_document_count,
            COALESCE(SUM(r.pdf_discovered_count),0) AS pdf_discovered_count,
            COALESCE(SUM(r.pdf_success_count),0) AS pdf_success_count,
            COALESCE(SUM(r.pdf_rejected_count),0) AS pdf_rejected_count,
            COALESCE(SUM(r.new_event_count),0) AS event_count,
            COALESCE(SUM(r.failed_count),0) AS failed_count,
            COALESCE(SUM(r.duration_ms),0) AS duration_ms,
            COALESCE(SUM(CASE WHEN r.status IN ('succeeded','partially_succeeded') THEN 1 ELSE 0 END),0)
                AS successful_runs
            FROM sources s LEFT JOIN crawl_source_runs r ON r.source_id=s.source_id
            AND {' AND '.join(where)}
            WHERE s.workspace_id=?
            GROUP BY s.source_id,s.source_name,s.operational_status,s.last_success_at,s.consecutive_failures
            ORDER BY s.source_name""",
            [*params, workspace_id],
        ).fetchall()
    result: list[dict[str, object]] = []
    for raw in rows:
        item = dict(raw)
        run_count = int(item["run_count"] or 0)
        fetched = int(item["fetched_count"] or 0)
        qualified = int(item["qualified_document_count"] or 0)
        item.update(
            {
                "status_label": OPERATIONAL_STATUS_LABELS.get(
                    str(item.get("operational_status") or ""),
                    str(item.get("operational_status") or "未运行"),
                ) if run_count else "尚未真实运行",
                "crawl_success_rate": (
                    float(item["successful_runs"] or 0) / run_count if run_count else None
                ),
                "valid_content_rate": (
                    qualified / fetched if fetched else None
                ),
                "event_yield_rate": (
                    int(item["event_count"] or 0) / qualified if qualified else None
                ),
                "average_response_seconds": (
                    int(item["duration_ms"] or 0) / 1000 / max(1, int(item["request_count"] or 0))
                    if int(item["request_count"] or 0)
                    else None
                ),
            }
        )
        result.append(item)
    return result


def four_week_evaluation(workspace_id: str, db_path) -> dict[str, object]:
    initialize_database(db_path)
    start = (date.today() - timedelta(days=27)).isoformat()
    with connect(db_path) as connection:
        run_days = int(
            connection.execute(
                """SELECT COUNT(DISTINCT run_day) FROM (
                    SELECT substr(started_at,1,10) AS run_day
                    FROM crawl_source_runs WHERE workspace_id=? AND substr(started_at,1,10)>=?
                    UNION
                    SELECT substr(started_at,1,10) AS run_day
                    FROM crawl_runs WHERE workspace_id=? AND substr(started_at,1,10)>=?
                ) WHERE run_day!=''""",
                (workspace_id, start, workspace_id, start),
            ).fetchone()[0]
        )
        totals = connection.execute(
            """SELECT
            (SELECT COUNT(*) FROM documents WHERE workspace_id=? AND created_at>=?) AS documents,
            (SELECT COUNT(*) FROM events WHERE workspace_id=? AND created_at>=?) AS events,
            (SELECT COUNT(*) FROM events WHERE workspace_id=? AND created_at>=?
             AND human_verified=1 AND reviewer_type IN ('human_user','industry_reviewer')) AS human_events,
            (SELECT COUNT(*) FROM event_evidence x JOIN events e ON e.event_id=x.event_id
             WHERE e.workspace_id=? AND x.created_at>=?) AS evidence_total,
            (SELECT COUNT(*) FROM event_evidence x JOIN events e ON e.event_id=x.event_id
             WHERE e.workspace_id=? AND x.created_at>=? AND x.verification_status='已验证') AS evidence_verified,
            (SELECT COUNT(*) FROM reports WHERE workspace_id=? AND generated_at>=?
             AND status='active') AS reports,
            (SELECT COUNT(*) FROM customer_feedback WHERE workspace_id=? AND feedback_date>=?) AS feedback
            """,
            (
                workspace_id, start, workspace_id, start, workspace_id, start,
                workspace_id, start, workspace_id, start, workspace_id, start,
                workspace_id, start,
            ),
        ).fetchone()
    event_count = int(totals["events"] or 0)
    evidence_total = int(totals["evidence_total"] or 0)
    return {
        "real_run_days": run_days,
        "remaining_days": max(0, 28 - run_days),
        "documents": int(totals["documents"] or 0),
        "events": event_count,
        "human_review_pass_rate": (
            int(totals["human_events"] or 0) / event_count if event_count else None
        ),
        "evidence_success_rate": (
            int(totals["evidence_verified"] or 0) / evidence_total if evidence_total else None
        ),
        "reports_generated": int(totals["reports"] or 0),
        "customer_feedback_count": int(totals["feedback"] or 0),
        "period_start": start,
        "period_end": date.today().isoformat(),
    }


def add_customer_feedback(
    workspace_id: str,
    db_path,
    *,
    feedback_text: str,
    customer_label: str = "",
    feedback_date: Optional[str] = None,
    created_by: str = "human_user",
) -> str:
    if created_by not in HUMAN_REVIEWER_TYPES:
        raise ValueError("客户反馈只能由用户或行业复核人手工录入")
    if not feedback_text.strip():
        raise ValueError("客户反馈不能为空")
    feedback_id = new_id("FDB")
    with transaction(db_path) as connection:
        connection.execute(
            """INSERT INTO customer_feedback(
            feedback_id,workspace_id,feedback_date,customer_label,feedback_text,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                feedback_id,
                workspace_id,
                feedback_date or date.today().isoformat(),
                customer_label.strip(),
                feedback_text.strip(),
                created_by,
                now_iso(),
            ),
        )
    return feedback_id
