from __future__ import annotations

"""Read-only access to the small, sanitized database shipped with the public demo."""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Optional, Sequence


class DemoDataUnavailable(RuntimeError):
    pass


def _read_only_connection(path: Path) -> sqlite3.Connection:
    target = Path(path)
    if not target.is_file():
        raise DemoDataUnavailable("演示数据尚未部署。")
    try:
        connection = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise DemoDataUnavailable("演示数据库暂时不可读。") from exc
    if not integrity or str(integrity[0]).lower() != "ok":
        connection.close()
        raise DemoDataUnavailable("演示数据库完整性检查未通过。")
    return connection


@dataclass(frozen=True)
class DemoMetrics:
    document_count: int
    qualified_document_count: int
    event_count: int
    enabled_source_count: int
    latest_update: str


class PublicDemoRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def health(self) -> dict[str, object]:
        try:
            with _read_only_connection(self.database_path) as connection:
                schema_version = connection.execute(
                    "SELECT value FROM demo_metadata WHERE key='schema_version'"
                ).fetchone()
        except (DemoDataUnavailable, sqlite3.Error) as exc:
            return {"ok": False, "message": str(exc)}
        return {
            "ok": bool(schema_version and schema_version[0]),
            "message": "演示数据可用" if schema_version else "演示数据版本缺失",
        }

    def metrics(self) -> DemoMetrics:
        with _read_only_connection(self.database_path) as connection:
            document_count = connection.execute("SELECT COUNT(*) FROM demo_documents").fetchone()[0]
            qualified_count = connection.execute(
                "SELECT COUNT(*) FROM demo_documents WHERE quality_status='合格'"
            ).fetchone()[0]
            event_count = connection.execute("SELECT COUNT(*) FROM demo_events").fetchone()[0]
            enabled_sources = connection.execute(
                "SELECT COUNT(*) FROM demo_sources WHERE enabled=1"
            ).fetchone()[0]
            latest = connection.execute(
                "SELECT COALESCE(MAX(fetched_at),'') FROM demo_documents"
            ).fetchone()[0]
        return DemoMetrics(
            document_count=int(document_count),
            qualified_document_count=int(qualified_count),
            event_count=int(event_count),
            enabled_source_count=int(enabled_sources),
            latest_update=str(latest or ""),
        )

    def filter_options(self) -> dict[str, list[str]]:
        with _read_only_connection(self.database_path) as connection:
            categories = [row[0] for row in connection.execute(
                "SELECT DISTINCT category FROM demo_events WHERE category<>'' ORDER BY category"
            )]
            sources = [row[0] for row in connection.execute(
                "SELECT DISTINCT source_name FROM demo_events WHERE source_name<>'' ORDER BY source_name"
            )]
        return {"categories": categories, "sources": sources}

    def list_events(
        self,
        *,
        keyword: str = "",
        categories: Sequence[str] = (),
        sources: Sequence[str] = (),
        limit: int = 100,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if keyword.strip():
            clauses.append("(title LIKE ? OR summary LIKE ? OR affected_area LIKE ? OR subject LIKE ?)")
            value = f"%{keyword.strip()}%"
            parameters.extend([value, value, value, value])
        if categories:
            clauses.append(f"category IN ({','.join('?' for _ in categories)})")
            parameters.extend(categories)
        if sources:
            clauses.append(f"source_name IN ({','.join('?' for _ in sources)})")
            parameters.extend(sources)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, min(int(limit), 200)))
        with _read_only_connection(self.database_path) as connection:
            rows = connection.execute(
                f"""SELECT event_key,event_date,category,title,summary,impact,affected_area,
                status,source_name,source_url,subject,extraction_confidence,risk_level,
                opportunity_level,ai_generated,human_verified,evidence_verified,business_value,
                recommended_action,published_at,fetched_at
                FROM demo_events {where}
                ORDER BY event_date DESC,title LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def evidence(self, event_key: str) -> list[dict[str, object]]:
        with _read_only_connection(self.database_path) as connection:
            rows = connection.execute(
                """SELECT quote_text,verification_status,source_url,published_at
                FROM demo_evidence WHERE event_key=? ORDER BY evidence_order""",
                (event_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sources(self) -> list[dict[str, object]]:
        with _read_only_connection(self.database_path) as connection:
            rows = connection.execute(
                """SELECT source_name,organization,homepage_url,list_page_url,source_type,
                category_hint,region,enabled,last_success_at
                FROM demo_sources ORDER BY enabled DESC,source_name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def metadata(self, key: str) -> Optional[str]:
        with _read_only_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT value FROM demo_metadata WHERE key=?", (key,)
            ).fetchone()
        return None if row is None else str(row[0])
