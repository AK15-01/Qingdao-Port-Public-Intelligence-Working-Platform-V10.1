from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from public_demo_store import DemoDataUnavailable, PublicDemoRepository
from runtime_config import load_runtime_config

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

config = load_runtime_config()
repository = PublicDemoRepository(config.demo_database_path)

app = FastAPI(
    title="PortScope Web V2 Preview",
    version="0.1.0-preview",
    docs_url="/api/docs",
    redoc_url=None,
)


def _service_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail="演示数据暂时不可用")


@app.get("/api/health")
def health() -> dict[str, object]:
    return repository.health()


@app.get("/api/dashboard")
def dashboard() -> dict[str, object]:
    try:
        metrics = repository.metrics()
        events = repository.list_events(limit=200)
        risk_counter = Counter(str(item.get("risk_level") or "未标记") for item in events)
        pending_review = sum(
            1
            for item in events
            if not bool(item.get("human_verified")) or not bool(item.get("evidence_verified"))
        )
        date_counter = Counter(
            str(item.get("event_date") or "")[:10]
            for item in events
            if str(item.get("event_date") or "").strip()
        )
        trend = [
            {"date": date, "events": count}
            for date, count in sorted(date_counter.items())[-14:]
        ]
        return {
            "documents": metrics.document_count,
            "qualified_documents": metrics.qualified_document_count,
            "events": metrics.event_count,
            "enabled_sources": metrics.enabled_source_count,
            "latest_update": metrics.latest_update,
            "risk": dict(risk_counter),
            "pending_review": pending_review,
            "trend": trend,
        }
    except (DemoDataUnavailable, OSError) as exc:
        raise _service_error(exc) from exc


@app.get("/api/events")
def events(
    keyword: str = Query(default="", max_length=80),
    category: Optional[str] = Query(default=None, max_length=50),
    source: Optional[str] = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    try:
        items = repository.list_events(
            keyword=keyword,
            categories=[category] if category else [],
            sources=[source] if source else [],
            limit=limit,
        )
        return {
            "items": items,
            "count": len(items),
            "filters": repository.filter_options(),
        }
    except (DemoDataUnavailable, OSError) as exc:
        raise _service_error(exc) from exc


@app.get("/api/events/{event_key}/evidence")
def evidence(event_key: str) -> dict[str, object]:
    try:
        return {"items": repository.evidence(event_key)}
    except (DemoDataUnavailable, OSError) as exc:
        raise _service_error(exc) from exc


@app.get("/api/sources")
def sources() -> dict[str, object]:
    try:
        items = repository.list_sources()
        return {"items": items, "count": len(items)}
    except (DemoDataUnavailable, OSError) as exc:
        raise _service_error(exc) from exc


# API routes must be registered before the static root mount.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
