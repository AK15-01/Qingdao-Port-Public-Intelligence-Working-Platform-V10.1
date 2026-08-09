from __future__ import annotations

"""Build a small public, read-only demo DB from vetted public records.

The export deliberately excludes full text, local paths, task/AI logs, customer
records, conversations, reports and reviewer identities.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "data" / "portscope.db"
DEFAULT_TARGET = PROJECT_ROOT / "data" / "demo" / "portscope_demo.db"


SCHEMA = """
CREATE TABLE demo_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE demo_sources (
    source_key TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    organization TEXT NOT NULL,
    homepage_url TEXT NOT NULL,
    list_page_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    category_hint TEXT NOT NULL,
    region TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    last_success_at TEXT NOT NULL
);
CREATE TABLE demo_documents (
    document_key TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    quality_note TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    FOREIGN KEY(source_key) REFERENCES demo_sources(source_key)
);
CREATE TABLE demo_events (
    event_key TEXT PRIMARY KEY,
    document_key TEXT NOT NULL,
    event_date TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    impact TEXT NOT NULL,
    affected_area TEXT NOT NULL,
    status TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    subject TEXT NOT NULL,
    extraction_confidence REAL NOT NULL,
    risk_level TEXT NOT NULL,
    opportunity_level TEXT NOT NULL,
    ai_generated INTEGER NOT NULL,
    human_verified INTEGER NOT NULL,
    evidence_verified INTEGER NOT NULL,
    business_value TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    FOREIGN KEY(document_key) REFERENCES demo_documents(document_key)
);
CREATE TABLE demo_evidence (
    evidence_key TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    evidence_order INTEGER NOT NULL,
    quote_text TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    FOREIGN KEY(event_key) REFERENCES demo_events(event_key)
);
"""


def _public_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def build_demo_database(source: Path, target: Path, *, limit: int = 12) -> dict[str, int]:
    if not source.is_file():
        raise FileNotFoundError(f"Source database does not exist: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    source_connection.row_factory = sqlite3.Row
    rows = source_connection.execute(
        """SELECT e.*,d.title AS document_title,d.publisher,d.published_at AS document_published_at,
        d.fetched_at,d.canonical_url,d.extraction_quality,d.content_hash,d.source_id
        FROM events e JOIN documents d ON d.document_id=e.document_id
        WHERE e.record_state='active' AND d.record_state='active' AND d.is_current=1
          AND e.evidence_verified=1 AND d.extraction_status='成功'
        ORDER BY e.event_date DESC,e.created_at DESC LIMIT ?""",
        (max(1, min(limit, 50)),),
    ).fetchall()
    selected = [row for row in rows if _public_url(row["source_url"] or row["canonical_url"])]
    source_ids = sorted({str(row["source_id"]) for row in selected})
    sources = []
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        sources = source_connection.execute(
            f"""SELECT source_id,source_name,organization,homepage_url,list_page_url,
            source_type,category_hint,region,enabled,last_success_at
            FROM sources WHERE source_id IN ({placeholders})""",
            source_ids,
        ).fetchall()

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    Path(temporary_name).unlink(missing_ok=True)
    try:
        destination = sqlite3.connect(temporary_name)
        destination.executescript(SCHEMA)
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        destination.executemany(
            "INSERT INTO demo_metadata(key,value) VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("generated_at", generated_at),
                ("data_scope", "公开来源的事实摘要与已定位短证据；不含第三方全文"),
                ("review_notice", "AI辅助抽取结果，未计作独立真人核验"),
            ],
        )
        source_map: dict[str, str] = {}
        for index, row in enumerate(sources, start=1):
            source_key = f"DEMO-SRC-{index:03d}"
            source_map[str(row["source_id"])] = source_key
            destination.execute(
                "INSERT INTO demo_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    source_key,
                    str(row["source_name"] or "公开来源"),
                    str(row["organization"] or ""),
                    _public_url(row["homepage_url"]),
                    _public_url(row["list_page_url"]),
                    str(row["source_type"] or ""),
                    str(row["category_hint"] or ""),
                    str(row["region"] or ""),
                    int(bool(row["enabled"])),
                    str(row["last_success_at"] or ""),
                ),
            )
        evidence_count = 0
        for index, row in enumerate(selected, start=1):
            event_key = f"DEMO-EVT-{index:03d}"
            document_key = f"DEMO-DOC-{index:03d}"
            source_url = _public_url(row["source_url"] or row["canonical_url"])
            source_key = source_map[str(row["source_id"])]
            destination.execute(
                "INSERT INTO demo_documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    document_key,
                    source_key,
                    str(row["document_title"] or row["title"] or "公开信息"),
                    str(row["publisher"] or row["source_name"] or ""),
                    str(row["document_published_at"] or row["event_date"] or ""),
                    str(row["fetched_at"] or row["collected_at"] or ""),
                    source_url,
                    "合格",
                    "已通过正文质量门禁；AI字段仍需以原始来源为准。",
                    str(row["content_hash"] or ""),
                ),
            )
            destination.execute(
                "INSERT INTO demo_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_key,
                    document_key,
                    str(row["event_date"] or ""),
                    str(row["document_published_at"] or row["event_date"] or ""),
                    str(row["fetched_at"] or row["collected_at"] or ""),
                    str(row["category"] or ""),
                    str(row["title"] or "公开信息"),
                    str(row["summary"] or ""),
                    str(row["impact"] or ""),
                    str(row["affected_area"] or ""),
                    str(row["status"] or "待核实"),
                    str(row["source_name"] or row["publisher"] or ""),
                    source_url,
                    str(row["involved_entities"] or row["publisher"] or row["source_name"] or ""),
                    float(row["extraction_confidence"] or 0),
                    str(row["risk_level"] or "低"),
                    str(row["opportunity_level"] or "低"),
                    int(bool(row["ai_generated"])),
                    0,
                    int(bool(row["evidence_verified"])),
                    str(row["business_value"] or "未评估"),
                    str(row["recommended_action"] or ""),
                ),
            )
            evidence_rows = source_connection.execute(
                """SELECT quote_text,verification_status FROM event_evidence
                WHERE event_id=? AND verification_status='已验证'
                ORDER BY start_offset LIMIT 3""",
                (row["event_id"],),
            ).fetchall()
            for order, evidence in enumerate(evidence_rows, start=1):
                quote = str(evidence["quote_text"] or "").strip()[:240]
                if not quote:
                    continue
                evidence_count += 1
                quote_digest = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:12]
                destination.execute(
                    "INSERT INTO demo_evidence VALUES(?,?,?,?,?,?,?)",
                    (
                        f"DEMO-EVD-{quote_digest}",
                        event_key,
                        order,
                        quote,
                        "已逐字定位",
                        source_url,
                        str(row["document_published_at"] or row["event_date"] or ""),
                    ),
                )
        destination.execute(
            "INSERT INTO demo_metadata(key,value) VALUES('event_count',?)", (str(len(selected)),)
        )
        destination.commit()
        check = destination.execute("PRAGMA integrity_check").fetchone()[0]
        destination.close()
        if str(check).lower() != "ok":
            raise RuntimeError("Generated demo database failed integrity_check")
        Path(temporary_name).replace(target)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    finally:
        source_connection.close()
    return {"documents": len(selected), "events": len(selected), "evidence": evidence_count, "sources": len(sources)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a sanitized public demo database")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    result = build_demo_database(args.source, args.target, limit=args.limit)
    print("Public demo database built:", ", ".join(f"{key}={value}" for key, value in result.items()))
    print("Target: data/demo/portscope_demo.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
