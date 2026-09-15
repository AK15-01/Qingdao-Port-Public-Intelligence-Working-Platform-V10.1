from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from threading import Event
import time
from typing import Callable, Mapping, Optional

from document_chunker import build_chunk_rows
from document_processor import store_document
from document_quality import assess_document
from event_pipeline import create_event_for_document
from operation_store import (
    add_technical_log,
    finish_operation,
    start_operation,
    update_operation_progress,
)
from pdf_processor import find_pdf_links
from platform_db import connect, initialize_database, list_sources, new_id, now_iso, transaction

from .base_adapter import DiscoveredItem
from .content_fetcher import ContentFetcher
from .robots_checker import RobotsChecker
from .site_adapter import build_adapter


class CancelToken:
    def __init__(self, persistent_check: Optional[Callable[[], bool]] = None) -> None:
        self._event = Event()
        self._persistent_check = persistent_check

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(
            self._persistent_check and self._persistent_check()
        )


@dataclass
class CrawlResult:
    crawl_run_id: str
    operation_run_id: str = ""
    status: str = "运行中"
    source_count: int = 0
    discovered_count: int = 0
    fetched_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    new_document_count: int = 0
    updated_document_count: int = 0
    new_event_count: int = 0
    new_opportunity_count: int = 0
    report_candidate_count: int = 0
    out_of_range_count: int = 0
    encoding_blocked_count: int = 0
    noise_blocked_count: int = 0
    pdf_discovered_count: int = 0
    pdf_success_count: int = 0
    pdf_rejected_count: int = 0
    qualified_document_count: int = 0
    duration_ms: int = 0
    api_call_count: int = 0
    api_input_characters: int = 0
    api_output_characters: int = 0
    actual_models: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    estimated_usage: str = ""
    stage_stats: dict[str, dict[str, object]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def list_pdf_attachment_rework_candidates(
    workspace_id: str,
    db_path,
    *,
    document_ids: Optional[list[str]] = None,
    source_ids: Optional[list[str]] = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Return only current quarantined HTML stubs that look like PDF attachment names."""

    initialize_database(db_path)
    clauses = [
        "d.workspace_id=?",
        "d.is_current=1",
        "d.record_state='quarantined'",
        "d.document_format='html'",
        "d.source_file_url=''",
        "length(trim(d.cleaned_text)) BETWEEN 1 AND 300",
        "lower(d.cleaned_text) LIKE '%.pdf%'",
    ]
    params: list[object] = [workspace_id]
    if document_ids is not None:
        normalized = [str(value) for value in document_ids if str(value).strip()]
        if not normalized:
            return []
        clauses.append(f"d.document_id IN ({','.join('?' for _ in normalized)})")
        params.extend(normalized)
    if source_ids is not None:
        normalized_sources = [str(value) for value in source_ids if str(value).strip()]
        if not normalized_sources:
            return []
        clauses.append(f"d.source_id IN ({','.join('?' for _ in normalized_sources)})")
        params.extend(normalized_sources)
    params.append(max(1, min(int(limit), 200)))
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""SELECT d.document_id,d.source_id,d.canonical_url,d.original_url,
            d.title,d.published_at,d.quality_status,d.record_state,d.document_version,
            s.source_name,s.enabled,s.crawl_allowed,s.adapter_config_json
            FROM documents d JOIN sources s ON s.source_id=d.source_id
            WHERE {' AND '.join(clauses)}
            ORDER BY d.published_at DESC,d.fetched_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


class CrawlManager:
    def __init__(
        self,
        db_path,
        data_root: Path,
        fetcher: Optional[ContentFetcher] = None,
        robots_checker: Optional[RobotsChecker] = None,
        adapter_factory: Optional[Callable[..., object]] = None,
        vector_indexer=None,
        ai_enabled: bool = False,
        ai_settings=None,
        ai_requester=None,
    ) -> None:
        self.db_path = db_path
        self.data_root = Path(data_root)
        self.fetcher = fetcher or ContentFetcher()
        self.robots_checker = robots_checker or RobotsChecker()
        self.adapter_factory = adapter_factory or build_adapter
        self.vector_indexer = vector_indexer
        self.ai_enabled = ai_enabled
        self.ai_settings = ai_settings
        self.ai_requester = ai_requester

    @staticmethod
    def _stage(result: CrawlResult, name: str, *, inputs: int = 0, success: int = 0,
               failed: int = 0, skipped: int = 0, reason: str = "", elapsed_ms: int = 0) -> None:
        row = result.stage_stats.setdefault(name, {
            "input": 0, "success": 0, "failed": 0, "skipped": 0,
            "failure_reasons": [], "duration_ms": 0,
        })
        row["input"] = int(row["input"]) + int(inputs)
        row["success"] = int(row["success"]) + int(success)
        row["failed"] = int(row["failed"]) + int(failed)
        row["skipped"] = int(row["skipped"]) + int(skipped)
        row["duration_ms"] = int(row["duration_ms"]) + max(0, int(elapsed_ms))
        if reason:
            reasons = list(row["failure_reasons"])
            if reason not in reasons:
                reasons.append(reason[:500])
            row["failure_reasons"] = reasons[:20]

    def _ai_snapshot(self, workspace_id: str) -> tuple[int, int, int]:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(input_characters),0),COALESCE(SUM(output_characters),0) FROM ai_call_logs WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def _record_start(self, workspace_id: str, result: CrawlResult) -> None:
        with transaction(self.db_path) as connection:
            connection.execute(
                """UPDATE operation_runs SET external_ref_type='crawl_run',
                external_ref_id=?,updated_at=? WHERE operation_run_id=?
                AND external_ref_id=''""",
                (result.crawl_run_id, now_iso(), result.operation_run_id),
            )
            connection.execute(
                "INSERT INTO crawl_runs(crawl_run_id,workspace_id,started_at,status,updated_at) VALUES(?,?,?,?,?)",
                (result.crawl_run_id, workspace_id, now_iso(), result.status, now_iso()),
            )

    def _heartbeat(self, result: CrawlResult) -> None:
        with transaction(self.db_path) as connection:
            connection.execute(
                "UPDATE crawl_runs SET updated_at=? WHERE crawl_run_id=?",
                (now_iso(), result.crawl_run_id),
            )

    def _record_finish(self, result: CrawlResult) -> None:
        with transaction(self.db_path) as connection:
            connection.execute(
                """UPDATE crawl_runs SET finished_at=?,status=?,source_count=?,discovered_count=?,fetched_count=?,
                skipped_count=?,failed_count=?,new_document_count=?,updated_document_count=?,new_event_count=?,
                new_opportunity_count=?,error_summary=?,stage_stats_json=?,duration_ms=?,api_call_count=?,
                api_input_characters=?,api_output_characters=?,actual_models_json=?,estimated_usage=?,
                report_candidate_count=?,out_of_range_count=?,encoding_blocked_count=?,noise_blocked_count=?,
                pdf_discovered_count=?,pdf_success_count=?,pdf_rejected_count=?,updated_at=? WHERE crawl_run_id=?""",
                (now_iso(), result.status, result.source_count, result.discovered_count, result.fetched_count,
                 result.skipped_count, result.failed_count, result.new_document_count, result.updated_document_count,
                 result.new_event_count, result.new_opportunity_count, "\n".join(result.errors)[:8000],
                 json.dumps(result.stage_stats, ensure_ascii=False), result.duration_ms, result.api_call_count,
                 result.api_input_characters, result.api_output_characters,
                 json.dumps(result.actual_models, ensure_ascii=False), result.estimated_usage,
                 result.report_candidate_count, result.out_of_range_count, result.encoding_blocked_count,
                 result.noise_blocked_count, result.pdf_discovered_count, result.pdf_success_count,
                 result.pdf_rejected_count, now_iso(), result.crawl_run_id),
            )

    def _source_success(self, source_id: str) -> None:
        with transaction(self.db_path) as connection:
            connection.execute(
                """UPDATE sources SET last_crawled_at=?,last_success_at=?,last_error='',
                consecutive_failures=0,operational_status='stable',health_status='正常',
                updated_at=? WHERE source_id=?""",
                (now_iso(), now_iso(), now_iso(), source_id),
            )

    def _source_failure(self, source_id: str, error: str) -> None:
        status = self._operational_status(error)
        health = {
            "manual_only": "仅人工导入",
            "needs_adapter": "需要适配器",
            "permission_review": "许可或robots待确认",
            "disabled": "暂停使用",
        }.get(status, "部分可用")
        with transaction(self.db_path) as connection:
            connection.execute(
                """UPDATE sources SET last_crawled_at=?,last_error=?,
                consecutive_failures=consecutive_failures+1,operational_status=?,
                health_status=?,updated_at=? WHERE source_id=?""",
                (now_iso(), error[:2000], status, health, now_iso(), source_id),
            )

    def _source_partial(self, source_id: str, error: str) -> None:
        status = self._operational_status(error)
        health = {
            "manual_only": "仅人工导入",
            "needs_adapter": "需要适配器",
            "permission_review": "许可或robots待确认",
            "disabled": "暂停使用",
        }.get(status, "部分可用")
        with transaction(self.db_path) as connection:
            connection.execute(
                """UPDATE sources SET last_crawled_at=?,last_success_at=?,last_error=?,
                consecutive_failures=0,operational_status=?,health_status=?,updated_at=?
                WHERE source_id=?""",
                (
                    now_iso(),
                    now_iso(),
                    error[:2000],
                    status,
                    health,
                    now_iso(),
                    source_id,
                ),
            )

    @staticmethod
    def _operational_status(error: str) -> str:
        text = str(error or "").lower()
        if any(term in text for term in ("robots", "条款", "许可")):
            return "permission_review"
        if any(term in text for term in ("javascript", "微信", "登录", "验证码")):
            return "manual_only"
        if any(
            term in text
            for term in (
                "选择器",
                "结构",
                "0条",
                "未发现链接",
                "合格正文0",
                "正文质量",
            )
        ):
            return "needs_adapter"
        if any(term in text for term in ("明确禁止", "暂停")):
            return "disabled"
        return "degraded"

    @staticmethod
    def _should_recheck_existing(
        current: Mapping[str, object],
        *,
        refresh_mode: str,
        rework: bool = False,
    ) -> tuple[bool, str]:
        if rework or refresh_mode == "force":
            return True, "用户明确要求强制复查"
        if refresh_mode == "new_only":
            return False, "只抓新增：URL已存在"
        event_status = str(current.get("event_status") or "")
        if event_status in {"解除", "已结束"}:
            return False, "历史事项已解除或结束"
        published = str(current.get("published_at") or "")[:10]
        try:
            age_days = (date.today() - date.fromisoformat(published)).days
        except ValueError:
            age_days = 9999
        if age_days > 30 and str(current.get("file_sha256") or ""):
            return False, "超过30天且已有完整文件哈希"
        if age_days > 7:
            return False, "超过最近7天低频复查范围"
        has_validator = bool(
            str(current.get("http_etag") or "").strip()
            or str(current.get("http_last_modified") or "").strip()
        )
        if not has_validator:
            try:
                last_checked = datetime.fromisoformat(
                    str(current.get("last_checked_at") or "")
                )
                if datetime.now().astimezone() - (
                    last_checked
                    if last_checked.tzinfo
                    else last_checked.astimezone()
                ) < timedelta(hours=24):
                    return False, "站点无条件请求标识，距上次检查不足24小时"
            except ValueError:
                pass
        return True, "最近7天内容使用条件请求复查"

    def _progress(
        self,
        result: CrawlResult,
        callback: Optional[Callable[[str, Mapping[str, object]], None]],
        phase: str,
        payload: Optional[Mapping[str, object]] = None,
        *,
        current_item: str = "",
        completed_items: Optional[int] = None,
        total_items: Optional[int] = None,
    ) -> None:
        values = {
            **dict(payload or {}),
            "crawl_run_id": result.crawl_run_id,
            "operation_run_id": result.operation_run_id,
            "completed_items": int(completed_items or 0),
            "total_items": int(total_items or 0),
            "success_count": result.qualified_document_count,
            "isolated_count": result.encoding_blocked_count + result.noise_blocked_count,
            "skipped_count": result.skipped_count,
            "failed_count": result.failed_count,
            "documents_created": result.new_document_count,
            "documents_updated": result.updated_document_count,
            "events_created": result.new_event_count,
        }
        update_operation_progress(
            result.operation_run_id,
            self.db_path,
            current_stage=phase,
            current_item=current_item,
            completed_items=completed_items,
            total_items=total_items,
            success_count=result.qualified_document_count,
            isolated_count=result.encoding_blocked_count + result.noise_blocked_count,
            skipped_count=result.skipped_count,
            failed_count=result.failed_count,
            metadata={"live_progress": values},
        )
        self._heartbeat(result)
        if callback:
            callback(phase, values)

    def _touch_current_document(
        self,
        document_id: str,
        *,
        http_status: int,
        etag: str = "",
        last_modified: str = "",
    ) -> None:
        with transaction(self.db_path) as connection:
            connection.execute(
                """UPDATE documents SET last_checked_at=?,last_http_status=?,
                http_etag=CASE WHEN ?!='' THEN ? ELSE http_etag END,
                http_last_modified=CASE WHEN ?!='' THEN ? ELSE http_last_modified END,
                updated_at=? WHERE document_id=?""",
                (
                    now_iso(),
                    int(http_status),
                    etag,
                    etag,
                    last_modified,
                    last_modified,
                    now_iso(),
                    document_id,
                ),
            )

    def _record_source_run(
        self,
        *,
        crawl_run_id: str,
        workspace_id: str,
        source_id: str,
        started_at: str,
        status: str,
        request_count: int,
        retry_count: int,
        before: Mapping[str, int],
        result: CrawlResult,
        error_summary: str = "",
        duration_ms: int = 0,
    ) -> None:
        text = str(error_summary or "")
        lower = text.lower()
        values = {
            "discovered_count": max(0, result.discovered_count - int(before["discovered_count"])),
            "fetched_count": max(0, result.fetched_count - int(before["fetched_count"])),
            "new_document_count": max(0, result.new_document_count - int(before["new_document_count"])),
            "updated_document_count": max(0, result.updated_document_count - int(before["updated_document_count"])),
            "skipped_count": max(0, result.skipped_count - int(before["skipped_count"])),
            "failed_count": max(0, result.failed_count - int(before["failed_count"])),
            "quality_failed_count": max(
                0,
                result.encoding_blocked_count
                + result.noise_blocked_count
                - int(before["quality_failed_count"]),
            ),
            "qualified_document_count": max(
                0,
                result.qualified_document_count
                - int(before["qualified_document_count"]),
            ),
            "pdf_discovered_count": max(
                0,
                result.pdf_discovered_count - int(before["pdf_discovered_count"]),
            ),
            "pdf_success_count": max(
                0,
                result.pdf_success_count - int(before["pdf_success_count"]),
            ),
            "pdf_rejected_count": max(
                0,
                result.pdf_rejected_count - int(before["pdf_rejected_count"]),
            ),
            "new_event_count": max(
                0,
                result.new_event_count - int(before["new_event_count"]),
            ),
        }
        with transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO crawl_source_runs(
                source_run_id,crawl_run_id,workspace_id,source_id,started_at,finished_at,status,
                request_count,discovered_count,fetched_count,new_document_count,updated_document_count,
                skipped_count,failed_count,http_error_count,tls_error_count,timeout_count,
                javascript_blocked_count,quality_failed_count,qualified_document_count,
                pdf_discovered_count,pdf_success_count,pdf_rejected_count,new_event_count,
                retry_count,duration_ms,error_summary,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(crawl_run_id,source_id) DO UPDATE SET
                finished_at=excluded.finished_at,status=excluded.status,
                request_count=excluded.request_count,discovered_count=excluded.discovered_count,
                fetched_count=excluded.fetched_count,new_document_count=excluded.new_document_count,
                updated_document_count=excluded.updated_document_count,skipped_count=excluded.skipped_count,
                failed_count=excluded.failed_count,http_error_count=excluded.http_error_count,
                tls_error_count=excluded.tls_error_count,timeout_count=excluded.timeout_count,
                javascript_blocked_count=excluded.javascript_blocked_count,
                quality_failed_count=excluded.quality_failed_count,
                qualified_document_count=excluded.qualified_document_count,
                pdf_discovered_count=excluded.pdf_discovered_count,
                pdf_success_count=excluded.pdf_success_count,
                pdf_rejected_count=excluded.pdf_rejected_count,
                new_event_count=excluded.new_event_count,retry_count=excluded.retry_count,
                duration_ms=excluded.duration_ms,error_summary=excluded.error_summary""",
                (
                    new_id("SRUN"), crawl_run_id, workspace_id, source_id, started_at, now_iso(), status,
                    max(0, int(request_count)), values["discovered_count"], values["fetched_count"],
                    values["new_document_count"], values["updated_document_count"], values["skipped_count"],
                    values["failed_count"], int("http" in lower and any(code in lower for code in (" 4", " 5", "http"))),
                    int(any(term in lower for term in ("tls", "ssl", "certificate"))),
                    int(any(term in lower for term in ("timeout", "timed out", "超时"))),
                    int("javascript" in lower), values["quality_failed_count"],
                    values["qualified_document_count"], values["pdf_discovered_count"],
                    values["pdf_success_count"], values["pdf_rejected_count"],
                    values["new_event_count"], max(0, int(retry_count)),
                    max(0, int(duration_ms)), text[:4000], now_iso(),
                ),
            )

    def run(self, workspace_id: str, source_ids: Optional[list[str]] = None,
            cancel: Optional[CancelToken] = None, progress: Optional[Callable[[str, Mapping[str, object]], None]] = None,
            max_articles: Optional[int] = None, start_date: str = "", end_date: str = "",
            ui_session_id: str = "",
            rework_document_ids: Optional[list[str]] = None,
            refresh_mode: str = "new_only",
            source_time_budget_seconds: float = 120.0,
            source_cooldown_seconds: float = 120.0,
            operation_run_id: str = "") -> CrawlResult:
        initialize_database(self.db_path)
        cancel = cancel or CancelToken()
        if refresh_mode not in {"new_only", "recent", "force"}:
            raise ValueError("refresh_mode必须是new_only、recent或force")
        result = CrawlResult(new_id("CRAWL"))
        pipeline_started = time.monotonic()
        api_before = self._ai_snapshot(workspace_id)
        result.operation_run_id = str(operation_run_id or "")
        if not result.operation_run_id:
            result.operation_run_id = start_operation(
                workspace_id,
                "crawl",
                self.db_path,
                ui_session_id=ui_session_id,
                source_id=(source_ids or [""])[0] if len(source_ids or []) == 1 else "",
                input_summary=(
                    f"来源：{len(source_ids or []) or '全部已启用'}；"
                    f"最多文章：{max_articles or '按来源配置'}；日期：{start_date or '不限'}至{end_date or '不限'}"
                ),
                metadata={
                    "source_ids": list(source_ids or []),
                    "max_articles": max_articles,
                    "start_date": start_date,
                    "end_date": end_date,
                    "refresh_mode": refresh_mode,
                    "rework_document_ids": list(rework_document_ids or []),
                },
                external_ref_type="crawl_run",
                external_ref_id=result.crawl_run_id,
            )
        self._record_start(workspace_id, result)
        add_technical_log(
            workspace_id,
            self.db_path,
            "采集任务开始。",
            operation_run_id=result.operation_run_id,
            level="info",
            component="crawler",
            details={
                "max_articles": max_articles,
                "refresh_mode": refresh_mode,
                "source_count": len(source_ids or []),
            },
        )
        sources = list_sources(workspace_id, self.db_path, enabled_only=True)
        rework_by_source: dict[str, list[DiscoveredItem]] = {}
        if rework_document_ids is not None:
            candidates = list_pdf_attachment_rework_candidates(
                workspace_id,
                self.db_path,
                document_ids=rework_document_ids,
                limit=max(1, min(len(rework_document_ids) or 1, 200)),
            )
            for candidate in candidates:
                rework_by_source.setdefault(str(candidate["source_id"]), []).append(
                    DiscoveredItem(
                        str(candidate["canonical_url"] or candidate["original_url"]),
                        str(candidate["title"] or ""),
                        str(candidate["published_at"] or ""),
                    )
                )
            requested = set(rework_document_ids)
            accepted = {str(item["document_id"]) for item in candidates}
            rejected = sorted(requested - accepted)
            if rejected:
                result.errors.append(
                    "以下文档不符合“当前隔离HTML仅含PDF附件名”的返工条件："
                    + "、".join(rejected)
                )
            source_ids = sorted(rework_by_source)
        if source_ids is not None:
            sources = [source for source in sources if source["source_id"] in set(source_ids)]
        sources = [source for source in sources if bool(source["crawl_allowed"])]
        result.source_count = len(sources)
        self._stage(result, "数据源检查", inputs=len(sources))
        try:
            for source in sources:
                if cancel.cancelled:
                    result.status = "已中止"
                    break
                source_id = str(source["source_id"])
                source_started_at = now_iso()
                source_started_monotonic = time.monotonic()
                source_request_count = [0]
                source_retry_count = [0]
                source_finished = [False]
                source_budget_exceeded = [False]
                source_error_start = len(result.errors)
                add_technical_log(
                    workspace_id,
                    self.db_path,
                    f"开始采集来源：{source['source_name']}",
                    operation_run_id=result.operation_run_id,
                    level="info",
                    component="crawler.source",
                    details={"source_id": source_id},
                )
                source_before = {
                    "discovered_count": result.discovered_count,
                    "fetched_count": result.fetched_count,
                    "new_document_count": result.new_document_count,
                    "updated_document_count": result.updated_document_count,
                    "skipped_count": result.skipped_count,
                    "failed_count": result.failed_count,
                    "quality_failed_count": result.encoding_blocked_count + result.noise_blocked_count,
                    "qualified_document_count": result.qualified_document_count,
                    "pdf_discovered_count": result.pdf_discovered_count,
                    "pdf_success_count": result.pdf_success_count,
                    "pdf_rejected_count": result.pdf_rejected_count,
                    "new_event_count": result.new_event_count,
                }

                def finalize_source_run(status: str, error: str = "") -> None:
                    if source_finished[0]:
                        return
                    source_finished[0] = True
                    messages = result.errors[source_error_start:]
                    summary = error or "\n".join(messages)
                    self._record_source_run(
                        crawl_run_id=result.crawl_run_id,
                        workspace_id=workspace_id,
                        source_id=source_id,
                        started_at=source_started_at,
                        status=status,
                        request_count=source_request_count[0],
                        retry_count=source_retry_count[0],
                        before=source_before,
                        result=result,
                        error_summary=summary,
                        duration_ms=int((time.monotonic() - source_started_monotonic) * 1000),
                    )
                    add_technical_log(
                        workspace_id,
                        self.db_path,
                        f"来源处理结束：{source['source_name']}（{status}）",
                        operation_run_id=result.operation_run_id,
                        level="info" if status == "succeeded" else "warning",
                        component="crawler.source",
                        details={
                            "source_id": source_id,
                            "status": status,
                            "request_count": source_request_count[0],
                            "error_summary": summary[:1000],
                        },
                    )

                self._progress(
                    result,
                    progress,
                    "正在检查数据源",
                    source,
                    current_item=str(source["source_name"]),
                )
                if (
                    refresh_mode == "new_only"
                    and rework_document_ids is None
                    and not str(source.get("last_error") or "").strip()
                    and str(source.get("last_crawled_at") or "").strip()
                ):
                    try:
                        last_crawled = datetime.fromisoformat(
                            str(source["last_crawled_at"])
                        )
                        if last_crawled.tzinfo is None:
                            last_crawled = last_crawled.astimezone()
                        seconds_since_check = (
                            datetime.now().astimezone() - last_crawled
                        ).total_seconds()
                    except ValueError:
                        seconds_since_check = float(source_cooldown_seconds) + 1
                    if seconds_since_check < max(
                        0.0, float(source_cooldown_seconds)
                    ):
                        result.skipped_count += 1
                        reason = (
                            f"距上次成功增量检查仅{max(0, int(seconds_since_check))}秒，"
                            f"低于{int(source_cooldown_seconds)}秒防重复间隔"
                        )
                        self._stage(
                            result,
                            "来源防重复",
                            inputs=1,
                            skipped=1,
                            reason=reason,
                        )
                        self._progress(
                            result,
                            progress,
                            "来源刚刚完成检查，已避免重复提交",
                            {"source": source, "reason": reason},
                            current_item=str(source["source_name"]),
                            completed_items=1,
                            total_items=1,
                        )
                        finalize_source_run("succeeded", reason)
                        continue
                if int(source.get("consecutive_failures") or 0) >= 3:
                    result.skipped_count += 1
                    result.errors.append(f"{source['source_name']}：连续失败达到3次，已跳过，需人工复核后重置。")
                    self._stage(result, "数据源检查", skipped=1, reason="连续失败达到3次")
                    finalize_source_run("skipped", "连续失败达到3次，等待人工复核")
                    continue
                robots = self.robots_checker.check(str(source["list_page_url"]), str(source.get("robots_status") or "未检查"))
                if not robots.allowed:
                    result.skipped_count += 1
                    message = f"{source['source_name']}：robots {robots.status}，未访问。{robots.note}"
                    result.errors.append(message)
                    self._source_failure(source_id, message)
                    self._stage(result, "数据源检查", failed=1, reason=message)
                    finalize_source_run("failed", message)
                    continue
                self._stage(result, "数据源检查", success=1)
                domain = str(source["domain"])

                def fetch_html(url: str) -> str:
                    source_request_count[0] += 1
                    fetched = self.fetcher.fetch(url, [domain], float(source.get("rate_limit_seconds") or 2.0))
                    source_retry_count[0] += int(getattr(self.fetcher, "last_retry_count", 0) or 0)
                    # List pages need links, not article-quality body text. A valid HTML
                    # response may therefore be usable even when article extraction is low quality.
                    if not fetched.raw_html:
                        raise RuntimeError(f"{fetched.status}：{fetched.note}")
                    return fetched.raw_html

                def fetch_json(url: str) -> object:
                    source_request_count[0] += 1
                    fetched = self.fetcher.fetch(
                        url, [domain], float(source.get("rate_limit_seconds") or 2.0), allow_json=True,
                    )
                    source_retry_count[0] += int(getattr(self.fetcher, "last_retry_count", 0) or 0)
                    if not fetched.ok:
                        raise RuntimeError(f"{fetched.status}：{fetched.note}")
                    try:
                        return json.loads(fetched.raw_html)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"公开 API 返回的 JSON 无法解析：{exc}") from exc

                def fetch_feed(url: str) -> str:
                    source_request_count[0] += 1
                    fetched = self.fetcher.fetch(
                        url, [domain], float(source.get("rate_limit_seconds") or 2.0), allow_xml=True,
                    )
                    source_retry_count[0] += int(getattr(self.fetcher, "last_retry_count", 0) or 0)
                    if not fetched.ok:
                        raise RuntimeError(f"{fetched.status}：{fetched.note}")
                    return fetched.raw_html

                try:
                    if str(source.get("adapter_type") or "") == "api":
                        adapter = self.adapter_factory(source, fetch_html, fetch_json)
                    elif str(source.get("adapter_type") or "") == "rss":
                        adapter = self.adapter_factory(source, fetch_feed)
                    else:
                        adapter = self.adapter_factory(source, fetch_html)
                    article_config = (
                        adapter.article_extraction_config(source)
                        if hasattr(adapter, "article_extraction_config")
                        else dict(source.get("adapter_config") or {})
                    )
                    self._progress(
                        result,
                        progress,
                        "正在发现新链接",
                        source,
                        current_item=str(source["source_name"]),
                    )
                    stage_started = time.monotonic()
                    if rework_document_ids is not None:
                        discovered = list(rework_by_source.get(source_id, []))
                    else:
                        discovery_source = dict(source)
                        if max_articles is not None:
                            # An explicit custom run limit (1—20 in the UI) is
                            # authoritative for this run only; it does not
                            # mutate the source's normal default.
                            discovery_source["max_articles_per_run"] = max(
                                1, min(int(max_articles), 20)
                            )
                        discovered = adapter.discover(discovery_source, cancel)
                    if start_date:
                        discovered = [item for item in discovered if not item.published_at or item.published_at[:10] >= start_date]
                    if end_date:
                        discovered = [item for item in discovered if not item.published_at or item.published_at[:10] <= end_date]
                    if max_articles is not None:
                        discovered = discovered[:max(1, min(int(max_articles), 200))]
                    result.discovered_count += len(discovered)
                    self._progress(
                        result,
                        progress,
                        "已发现待处理文章",
                        {"source": source, "discovered_count": len(discovered)},
                        current_item=str(source["source_name"]),
                        completed_items=0,
                        total_items=len(discovered),
                    )
                    self._stage(result, "列表发现", inputs=1, success=len(discovered),
                                elapsed_ms=int((time.monotonic() - stage_started) * 1000))
                    for item_index, item in enumerate(discovered, start=1):
                        if cancel.cancelled:
                            result.status = "已中止"
                            break
                        if time.monotonic() - source_started_monotonic >= max(
                            1.0, float(source_time_budget_seconds)
                        ):
                            message = (
                                f"{source['source_name']}：达到单一来源"
                                f"{int(source_time_budget_seconds)}秒时间预算，"
                                "剩余文章留待下次增量处理。"
                            )
                            result.errors.append(message)
                            source_budget_exceeded[0] = True
                            add_technical_log(
                                workspace_id,
                                self.db_path,
                                message,
                                operation_run_id=result.operation_run_id,
                                level="warning",
                                component="crawler.budget",
                                details={
                                    "source_id": source_id,
                                    "completed_items": item_index - 1,
                                    "total_items": len(discovered),
                                },
                            )
                            break
                        with connect(self.db_path) as connection:
                            current = connection.execute(
                                """SELECT d.*,e.status AS event_status
                                FROM documents d LEFT JOIN events e ON e.document_id=d.document_id
                                WHERE d.workspace_id=? AND d.canonical_url=? AND d.is_current=1
                                ORDER BY e.created_at DESC LIMIT 1""",
                                (workspace_id, item.url),
                            ).fetchone()
                        if current:
                            should_recheck, skip_reason = self._should_recheck_existing(
                                dict(current),
                                refresh_mode=refresh_mode,
                                rework=rework_document_ids is not None,
                            )
                            if not should_recheck:
                                result.skipped_count += 1
                                self._stage(
                                    result,
                                    "增量策略",
                                    inputs=1,
                                    skipped=1,
                                    reason=skip_reason,
                                )
                                self._progress(
                                    result,
                                    progress,
                                    "已跳过未变化历史URL",
                                    {"source": source, "item": item, "reason": skip_reason},
                                    current_item=str(item.title or item.url),
                                    completed_items=item_index,
                                    total_items=len(discovered),
                                )
                                continue
                        self._progress(
                            result,
                            progress,
                            "正在下载HTML正文页",
                            {"source": source, "item": item, "index": item_index},
                            current_item=str(item.title or item.url),
                            completed_items=item_index - 1,
                            total_items=len(discovered),
                        )
                        fetch_started = time.monotonic()
                        trusted_config = {
                            **article_config,
                            "trusted_title": item.title,
                            "trusted_published_at": item.published_at,
                            "trusted_publisher": source.get("organization") or source.get("source_name"),
                        }
                        source_request_count[0] += 1
                        conditional_headers = {}
                        if current and refresh_mode == "recent":
                            if str(current["http_etag"] or ""):
                                conditional_headers["If-None-Match"] = str(current["http_etag"])
                            if str(current["http_last_modified"] or ""):
                                conditional_headers["If-Modified-Since"] = str(
                                    current["http_last_modified"]
                                )
                        fetched = self.fetcher.fetch(
                            item.url,
                            [domain],
                            float(source.get("rate_limit_seconds") or 2.0),
                            extraction_config=trusted_config,
                            allow_pdf=True,
                            max_pdf_bytes=int(article_config.get("pdf_max_bytes") or 15 * 1024 * 1024),
                            max_pdf_pages=int(article_config.get("pdf_max_pages") or 200),
                            request_headers=conditional_headers,
                        )
                        article_retries = int(getattr(self.fetcher, "last_retry_count", 0) or 0)
                        source_retry_count[0] += article_retries
                        if article_retries:
                            add_technical_log(
                                workspace_id,
                                self.db_path,
                                f"正文请求发生{article_retries}次重试。",
                                operation_run_id=result.operation_run_id,
                                level="warning",
                                component="crawler.retry",
                                details={"source_id": source_id, "url": item.url},
                            )
                        if fetched.not_modified and current:
                            self._touch_current_document(
                                str(current["document_id"]),
                                http_status=304,
                                etag=fetched.http_etag,
                                last_modified=fetched.http_last_modified,
                            )
                            result.skipped_count += 1
                            self._stage(result, "条件请求", inputs=1, skipped=1, reason="HTTP 304")
                            self._progress(
                                result,
                                progress,
                                "HTTP 304，内容未变化",
                                {"source": source, "item": item},
                                current_item=str(item.title or item.url),
                                completed_items=item_index,
                                total_items=len(discovered),
                            )
                            continue
                        source_page_url = fetched.canonical_url or fetched.final_url or item.url
                        source_page_raw_html = fetched.raw_html
                        source_page_etag = fetched.http_etag
                        source_page_last_modified = fetched.http_last_modified
                        source_file_url = fetched.final_url if fetched.document_format == "pdf" else ""
                        attachment_failure_note = ""
                        pdf_counted = fetched.document_format == "pdf"
                        if pdf_counted:
                            result.pdf_discovered_count += 1
                        if (
                            fetched.document_format == "html"
                            and fetched.raw_html
                            and bool(article_config.get("follow_pdf_attachments"))
                        ):
                            pdf_links = find_pdf_links(
                                fetched.raw_html,
                                fetched.final_url or item.url,
                                str(article_config.get("pdf_link_selector") or ""),
                                limit=1,
                            )
                            if pdf_links:
                                if cancel.cancelled:
                                    result.status = "已中止"
                                    self._progress(
                                        result,
                                        progress,
                                        "已取消",
                                        {"source": source, "item": item},
                                        current_item=str(item.title or item.url),
                                        completed_items=item_index - 1,
                                        total_items=len(discovered),
                                    )
                                    break
                                result.pdf_discovered_count += 1
                                pdf_counted = True
                                self._progress(
                                    result,
                                    progress,
                                    "正在下载PDF附件",
                                    {"source": source, "item": item, "pdf_url": pdf_links[0]},
                                    current_item=str(item.title or item.url),
                                    completed_items=item_index - 1,
                                    total_items=len(discovered),
                                )
                                source_request_count[0] += 1
                                pdf_fetched = self.fetcher.fetch(
                                    pdf_links[0], [domain],
                                    float(source.get("rate_limit_seconds") or 2.0),
                                    extraction_config=trusted_config,
                                    allow_pdf=True,
                                    max_pdf_bytes=int(article_config.get("pdf_max_bytes") or 15 * 1024 * 1024),
                                    max_pdf_pages=int(article_config.get("pdf_max_pages") or 200),
                                    timeout_override=(5, 25),
                                )
                                pdf_retries = int(getattr(self.fetcher, "last_retry_count", 0) or 0)
                                source_retry_count[0] += pdf_retries
                                if pdf_retries:
                                    add_technical_log(
                                        workspace_id,
                                        self.db_path,
                                        f"PDF请求发生{pdf_retries}次重试。",
                                        operation_run_id=result.operation_run_id,
                                        level="warning",
                                        component="crawler.retry",
                                        details={"source_id": source_id, "url": pdf_links[0]},
                                    )
                                source_file_url = pdf_fetched.final_url or pdf_links[0]
                                if pdf_fetched.raw_bytes:
                                    fetched = replace(
                                        pdf_fetched,
                                        canonical_url=source_page_url,
                                        raw_html=source_page_raw_html,
                                        http_etag=source_page_etag,
                                        http_last_modified=source_page_last_modified,
                                        title=fetched.title or item.title or pdf_fetched.title,
                                        published_at=fetched.published_at or item.published_at or pdf_fetched.published_at,
                                        publisher=fetched.publisher or pdf_fetched.publisher
                                        or str(source.get("organization") or source.get("source_name") or ""),
                                        note=f"来自官方正文页的PDF附件。{pdf_fetched.note}",
                                    )
                                elif not pdf_fetched.ok:
                                    result.pdf_rejected_count += 1
                                    attachment_failure_note = (
                                        f"{pdf_fetched.status}：{pdf_fetched.note}"
                                    )
                                    result.errors.append(
                                        f"{source['source_name']}｜{pdf_links[0]}：{pdf_fetched.status} {pdf_fetched.note}"
                                    )
                                    add_technical_log(
                                        workspace_id,
                                        self.db_path,
                                        f"PDF处理失败：{pdf_fetched.status}",
                                        operation_run_id=result.operation_run_id,
                                        level="error",
                                        component="crawler.pdf",
                                        details={
                                            "source_id": source_id,
                                            "url": pdf_links[0],
                                            "reason": pdf_fetched.note[:500],
                                        },
                                    )
                        if not fetched.ok and not fetched.raw_html and not fetched.raw_bytes:
                            result.failed_count += 1
                            result.errors.append(f"{source['source_name']}｜{item.url}：{fetched.status} {fetched.note}")
                            self._stage(result, "正文抓取", inputs=1, failed=1, reason=f"{fetched.status} {fetched.note}",
                                        elapsed_ms=int((time.monotonic() - fetch_started) * 1000))
                            add_technical_log(
                                workspace_id,
                                self.db_path,
                                f"正文抓取失败：{fetched.status}",
                                operation_run_id=result.operation_run_id,
                                level="error",
                                component="crawler.network",
                                details={
                                    "source_id": source_id,
                                    "url": item.url,
                                    "reason": fetched.note[:500],
                                },
                            )
                            self._progress(
                                result,
                                progress,
                                "正文抓取失败",
                                {"source": source, "item": item, "reason": fetched.note},
                                current_item=str(item.title or item.url),
                                completed_items=item_index,
                                total_items=len(discovered),
                            )
                            continue
                        if not fetched.ok and fetched.status == "页面需要JavaScript":
                            result.errors.append(
                                f"{source['source_name']}｜{item.url}：页面需要JavaScript；"
                                "未启用浏览器自动化，来源应暂停或改用静态公开栏目。"
                            )
                        result.fetched_count += 1
                        fetch_elapsed = int((time.monotonic() - fetch_started) * 1000)
                        if fetched.ok:
                            self._stage(result, "正文抓取", inputs=1, success=1, elapsed_ms=fetch_elapsed)
                        else:
                            self._stage(result, "正文抓取", inputs=1, failed=1,
                                        reason=f"{fetched.status} {fetched.note}", elapsed_ms=fetch_elapsed)
                        canonical = fetched.canonical_url or fetched.final_url or item.url
                        prefer_discovered_title = bool(article_config.get("prefer_discovered_title"))
                        document_values = {
                            "workspace_id": workspace_id,
                            "source_id": source_id,
                            "canonical_url": canonical,
                            "original_url": item.url,
                            "title": item.title if prefer_discovered_title and item.title else (fetched.title or item.title),
                            "publisher": fetched.publisher or source.get("organization") or source["source_name"],
                            "published_at": fetched.published_at or item.published_at,
                            "fetched_at": fetched.fetched_at or now_iso(),
                            "raw_html": fetched.raw_html,
                            "raw_bytes": fetched.raw_bytes,
                            "document_format": fetched.document_format,
                            "mime_type": fetched.content_type,
                            "source_file_url": source_file_url,
                            "file_size_bytes": fetched.file_size_bytes,
                            "file_sha256": fetched.file_sha256,
                            "http_etag": fetched.http_etag,
                            "http_last_modified": fetched.http_last_modified,
                            "last_checked_at": fetched.fetched_at or now_iso(),
                            "last_content_changed_at": fetched.fetched_at or now_iso(),
                            "last_http_status": fetched.http_status,
                            "content_hash": fetched.file_sha256 if fetched.document_format == "pdf" else "",
                            "cleaned_text": fetched.text,
                            "extraction_status": (
                                f"PDF附件失败：{attachment_failure_note}"
                                if attachment_failure_note
                                else fetched.status or "成功"
                            ),
                            "extraction_quality": "｜".join(
                                item
                                for item in (fetched.note, attachment_failure_note)
                                if item
                            ),
                            "http_status": fetched.http_status,
                        }
                        with connect(self.db_path) as connection:
                            template_rows = connection.execute(
                                """SELECT cleaned_text FROM documents
                                WHERE workspace_id=? AND source_id=? AND is_current=1 AND canonical_url!=?
                                ORDER BY fetched_at DESC LIMIT 5""",
                                (workspace_id, source_id, canonical),
                            ).fetchall()
                        quality = assess_document(
                            title=str(document_values["title"]),
                            text=str(document_values["cleaned_text"]),
                            published_at=str(document_values["published_at"]),
                            raw_html=str(document_values["raw_html"]),
                            site_names=[str(value) for value in article_config.get("site_names", []) or []],
                            template_texts=[str(row[0]) for row in template_rows],
                        )
                        document_values.update({
                            "quality_status": quality.status,
                            "quality_metrics_json": json.dumps(quality.metrics, ensure_ascii=False),
                            "report_quality_eligible": quality.report_allowed,
                            "processing_allowed": quality.processing_allowed,
                            "extraction_quality": "｜".join(
                                item
                                for item in (
                                    str(document_values["extraction_quality"]),
                                    quality.note,
                                )
                                if item
                            ),
                        })
                        if fetched.document_format == "pdf":
                            metrics = dict(quality.metrics)
                            metrics.update({
                                "pdf_page_count": fetched.pdf_page_count,
                                "pdf_removed_repeated_lines": fetched.pdf_removed_repeated_lines,
                                "pdf_file_size_bytes": fetched.file_size_bytes,
                            })
                            document_values["quality_metrics_json"] = json.dumps(metrics, ensure_ascii=False)
                            if fetched.ok and bool(fetched.text.strip()):
                                result.pdf_success_count += 1
                            elif pdf_counted:
                                result.pdf_rejected_count += 1
                        if quality.processing_allowed:
                            result.qualified_document_count += 1
                        self._stage(
                            result, "内容质量门禁", inputs=1,
                            success=1 if quality.processing_allowed else 0,
                            failed=0 if quality.processing_allowed else 1,
                            reason="" if quality.processing_allowed else quality.note,
                            elapsed_ms=fetch_elapsed,
                        )

                        published_day = str(document_values["published_at"] or "")[:10]
                        outside_range = bool(
                            published_day and (
                                (start_date and published_day < start_date)
                                or (end_date and published_day > end_date)
                            )
                        )
                        if outside_range:
                            document_values.update({
                                "extraction_status": "日期范围外",
                                "ai_status": "不处理",
                                "review_status": "日期范围外",
                                "report_quality_eligible": False,
                            })
                            stored = store_document(document_values, self.db_path, self.data_root, ())
                            if stored.document_id not in result.document_ids:
                                result.document_ids.append(stored.document_id)
                            result.out_of_range_count += 1
                            result.skipped_count += 1
                            self._stage(result, "日期范围复核", inputs=1, skipped=1, reason="正文发布日期不在本次范围")
                            self._stage(result, "原文归档", inputs=1, success=1)
                            if stored.disposition == "new":
                                result.new_document_count += 1
                            elif stored.disposition == "updated":
                                result.updated_document_count += 1
                            self._progress(
                                result,
                                progress,
                                "日期范围外，已归档",
                                {"source": source, "item": item},
                                current_item=str(item.title or item.url),
                                completed_items=item_index,
                                total_items=len(discovered),
                            )
                            continue
                        self._stage(result, "日期范围复核", inputs=1, success=1)

                        if not quality.processing_allowed:
                            document_values.update({
                                "extraction_status": fetched.status if not fetched.ok else quality.status,
                                "ai_status": "质量门禁拦截",
                                "review_status": "质量异常",
                                "report_quality_eligible": False,
                            })
                            stored = store_document(document_values, self.db_path, self.data_root, ())
                            if stored.document_id not in result.document_ids:
                                result.document_ids.append(stored.document_id)
                            if quality.status == "编码异常":
                                result.encoding_blocked_count += 1
                            else:
                                result.noise_blocked_count += 1
                            result.skipped_count += 1
                            self._stage(result, "原文归档", inputs=1, success=1)
                            self._stage(result, "结构化抽取", inputs=1, skipped=1, reason="正文质量门禁未通过")
                            self._stage(result, "向量索引", inputs=1, skipped=1, reason="正文质量门禁未通过")
                            self._stage(result, "报告候选", inputs=1, skipped=1, reason=quality.note)
                            if stored.disposition == "new":
                                result.new_document_count += 1
                            elif stored.disposition == "updated":
                                result.updated_document_count += 1
                            self._progress(
                                result,
                                progress,
                                "正文质量未通过，已隔离",
                                {"source": source, "item": item, "reason": quality.note},
                                current_item=str(item.title or item.url),
                                completed_items=item_index,
                                total_items=len(discovered),
                            )
                            continue

                        self._stage(result, "内容清洗", inputs=1, success=1, elapsed_ms=fetch_elapsed)
                        chunk_rows = build_chunk_rows(fetched.text, {
                            "workspace_id": workspace_id, "source_id": source_id,
                            "source_name": fetched.publisher or source["source_name"],
                            "category": source.get("category_hint", ""), "published_at": document_values["published_at"],
                            "status": "待核实", "source_url": canonical, "human_verified": False,
                            "report_eligible": quality.report_allowed,
                        })
                        self._progress(
                            result,
                            progress,
                            "正在去重和归档",
                            document_values,
                            current_item=str(item.title or item.url),
                            completed_items=item_index - 1,
                            total_items=len(discovered),
                        )
                        store_started = time.monotonic()
                        stored = store_document(document_values, self.db_path, self.data_root, chunk_rows)
                        if stored.document_id not in result.document_ids:
                            result.document_ids.append(stored.document_id)
                        if stored.disposition == "duplicate_content":
                            if current:
                                self._touch_current_document(
                                    str(current["document_id"]),
                                    http_status=fetched.http_status,
                                    etag=fetched.http_etag,
                                    last_modified=fetched.http_last_modified,
                                )
                            result.skipped_count += 1
                            self._stage(result, "URL与正文去重", inputs=1, skipped=1)
                            self._progress(
                                result,
                                progress,
                                "内容哈希未变化",
                                {"source": source, "item": item},
                                current_item=str(item.title or item.url),
                                completed_items=item_index,
                                total_items=len(discovered),
                            )
                            continue
                        store_elapsed = int((time.monotonic() - store_started) * 1000)
                        self._stage(result, "URL与正文去重", inputs=1, success=1, elapsed_ms=store_elapsed)
                        self._stage(result, "原文归档", inputs=1, success=1, elapsed_ms=store_elapsed)
                        self._stage(result, "Chunk", inputs=1, success=len(chunk_rows), elapsed_ms=store_elapsed)
                        self._stage(result, "FTS5", inputs=len(chunk_rows), success=len(chunk_rows), elapsed_ms=store_elapsed)
                        if stored.disposition == "new":
                            result.new_document_count += 1
                        else:
                            result.updated_document_count += 1
                            if self.vector_indexer and stored.previous_document_id:
                                self.vector_indexer.delete_document(stored.previous_document_id)
                        document_values["document_id"] = stored.document_id
                        if cancel.cancelled:
                            result.status = "已中止"
                            self._progress(
                                result,
                                progress,
                                "已取消",
                                {"source": source, "item": item},
                                current_item=str(item.title or item.url),
                                completed_items=item_index,
                                total_items=len(discovered),
                            )
                            break
                        self._progress(
                            result,
                            progress,
                            "正在生成规则事件"
                            if not self.ai_enabled
                            else "正在进行AI结构化抽取",
                            document_values,
                            current_item=str(item.title or item.url),
                            completed_items=item_index - 1,
                            total_items=len(discovered),
                        )
                        event_started = time.monotonic()
                        event_id, event = create_event_for_document(
                            stored.document_id, document_values, source, workspace_id, self.db_path,
                            ai_enabled=self.ai_enabled,
                            settings=self.ai_settings, requester=self.ai_requester,
                        )
                        if event_id and event_id not in result.event_ids:
                            result.event_ids.append(event_id)
                        result.new_event_count += 1
                        event_elapsed = int((time.monotonic() - event_started) * 1000)
                        extraction_method = str(event.get("extraction_method") or "rules")
                        self._stage(result, "结构化抽取", inputs=1, success=1,
                                    skipped=1 if extraction_method == "rules" and self.ai_enabled else 0,
                                    reason="DeepSeek不可用，已使用规则并进入待AI处理" if extraction_method == "rules" and self.ai_enabled else "",
                                    elapsed_ms=event_elapsed)
                        self._stage(result, "风险与商机评分", inputs=1, success=1, elapsed_ms=event_elapsed)
                        self._stage(result, "待审核", inputs=1, success=1)
                        self._stage(result, "报告候选", inputs=1, skipped=1, reason="自动事件需人工确认")
                        if str(event.get("opportunity_level")) in {"中", "高"}:
                            result.new_opportunity_count += 1
                        if self.vector_indexer:
                            if progress:
                                progress("正在更新知识库", {"document_id": stored.document_id})
                            try:
                                self.vector_indexer.index_document(stored.document_id)
                                self._stage(result, "向量索引", inputs=1, success=1)
                            except Exception as exc:
                                result.errors.append(f"向量化待处理 {stored.document_id}：{exc}")
                                self._stage(result, "向量索引", inputs=1, failed=1, reason=str(exc))
                        else:
                            self._stage(result, "向量索引", inputs=1, skipped=1, reason="本地向量组件未启用，FTS5仍可用")
                        self._progress(
                            result,
                            progress,
                            "文章处理完成",
                            {"source": source, "item": item, "document_id": stored.document_id},
                            current_item=str(item.title or item.url),
                            completed_items=item_index,
                            total_items=len(discovered),
                        )
                    if result.status == "已中止":
                        finalize_source_run("cancelled", "用户中止本次采集")
                        continue
                    source_discovered = (
                        result.discovered_count - int(source_before["discovered_count"])
                    )
                    source_fetched = result.fetched_count - int(source_before["fetched_count"])
                    source_skipped = result.skipped_count - int(source_before["skipped_count"])
                    source_qualified = (
                        result.qualified_document_count
                        - int(source_before["qualified_document_count"])
                    )
                    source_quality_failed = (
                        result.encoding_blocked_count
                        + result.noise_blocked_count
                        - int(source_before["quality_failed_count"])
                    )
                    source_failed = result.failed_count - int(source_before["failed_count"])
                    message = "\n".join(result.errors[source_error_start:])
                    if source_budget_exceeded[0]:
                        summary = (
                            f"达到{int(source_time_budget_seconds)}秒来源预算；"
                            f"已处理{source_fetched}篇，剩余内容留待下次增量运行"
                        )
                        self._source_partial(source_id, summary)
                        finalize_source_run("partially_succeeded", summary)
                    elif source_discovered == 0:
                        self._source_success(source_id)
                        finalize_source_run(
                            "succeeded",
                            "适配器明确返回0篇新内容，未发生抓取或质量失败",
                        )
                    elif source_fetched == 0 and source_skipped >= source_discovered:
                        self._source_success(source_id)
                        finalize_source_run(
                            "succeeded",
                            f"发现{source_discovered}篇，均按增量策略跳过；未重复下载PDF",
                        )
                    elif source_qualified == 0:
                        summary = (
                            f"发现{source_discovered}篇、抓取{source_fetched}篇、"
                            f"合格正文0篇、正文质量失败{source_quality_failed}篇"
                        )
                        message = "\n".join(item for item in (summary, message) if item)
                        result.failed_count += 1
                        result.errors.append(f"{source['source_name']}：{summary}")
                        self._source_failure(source_id, message)
                        finalize_source_run("failed", message)
                    elif source_failed > 0 or source_quality_failed > 0 or message:
                        summary = (
                            f"发现{source_discovered}篇、抓取{source_fetched}篇、"
                            f"合格正文{source_qualified}篇、正文质量失败{source_quality_failed}篇"
                        )
                        message = "\n".join(item for item in (summary, message) if item)
                        self._source_partial(source_id, message)
                        finalize_source_run("partially_succeeded", message)
                    else:
                        self._source_success(source_id)
                        finalize_source_run("succeeded")
                except Exception as exc:
                    result.failed_count += 1
                    message = f"{source['source_name']}：{type(exc).__name__}: {exc}"
                    result.errors.append(message)
                    self._source_failure(source_id, message)
                    self._stage(result, "列表发现", inputs=1, failed=1, reason=message)
                    finalize_source_run("failed", message)
                    continue
            if result.status == "运行中":
                result.status = "完成" if not result.errors else "部分完成"
        except Exception as exc:
            result.status = "失败"
            result.errors.append(f"流水线异常：{type(exc).__name__}: {exc}")
        finally:
            if result.status == "运行中":
                result.status = "异常中断"
                result.errors.append("采集未进入正常结束状态，已由 finally 统一记录为异常中断。")
            api_after = self._ai_snapshot(workspace_id)
            result.api_call_count = max(0, api_after[0] - api_before[0])
            result.api_input_characters = max(0, api_after[1] - api_before[1])
            result.api_output_characters = max(0, api_after[2] - api_before[2])
            with connect(self.db_path) as connection:
                model_rows = connection.execute(
                    "SELECT DISTINCT model_name FROM ai_call_logs WHERE workspace_id=? AND created_at>=(SELECT started_at FROM crawl_runs WHERE crawl_run_id=?) AND model_name!=''",
                    (workspace_id, result.crawl_run_id),
                ).fetchall()
                result.report_candidate_count = int(connection.execute(
                    "SELECT COUNT(*) FROM events WHERE workspace_id=? AND report_eligible=1",
                    (workspace_id,),
                ).fetchone()[0])
            result.actual_models = [str(row[0]) for row in model_rows]
            result.estimated_usage = (
                f"{result.api_call_count}次API调用；输入{result.api_input_characters}字符；"
                f"输出{result.api_output_characters}字符；未按动态价格换算金额"
            )
            result.duration_ms = int((time.monotonic() - pipeline_started) * 1000)
            self._record_finish(result)
            operation_status = {
                "完成": "succeeded",
                "部分完成": "partially_succeeded",
                "失败": "failed",
                "异常中断": "failed",
                "已中止": "cancelled",
            }.get(result.status, "failed")
            finish_operation(
                result.operation_run_id,
                self.db_path,
                status=operation_status,
                result_summary=(
                    f"新增文档{result.new_document_count}；更新文档{result.updated_document_count}；"
                    f"新增事件{result.new_event_count}；跳过{result.skipped_count}；失败{result.failed_count}"
                ),
                counts={
                    "documents_created": result.new_document_count,
                    "documents_updated": result.updated_document_count,
                    "events_created": result.new_event_count,
                    "skipped_count": result.skipped_count,
                    "failed_count": result.failed_count,
                },
                warning_count=len(result.errors),
                error_summary="\n".join(result.errors),
                structured_log=[
                    {"stage": name, **dict(values)}
                    for name, values in result.stage_stats.items()
                ],
                metadata={
                    "crawl_run_id": result.crawl_run_id,
                    "document_ids": result.document_ids,
                    "event_ids": result.event_ids,
                    "actual_models": result.actual_models,
                    "api_call_count": result.api_call_count,
                    "report_candidate_count": result.report_candidate_count,
                },
            )
            add_technical_log(
                workspace_id,
                self.db_path,
                f"采集任务结束：{result.status}",
                operation_run_id=result.operation_run_id,
                level=(
                    "info"
                    if operation_status == "succeeded"
                    else ("warning" if operation_status in {"partially_succeeded", "cancelled"} else "error")
                ),
                component="crawler",
                details={
                    "status": result.status,
                    "new_documents": result.new_document_count,
                    "updated_documents": result.updated_document_count,
                    "skipped": result.skipped_count,
                    "failed": result.failed_count,
                },
            )
        return result
