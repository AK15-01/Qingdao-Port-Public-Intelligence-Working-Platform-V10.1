from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from document_quality import assess_document  # noqa: E402
from platform_db import connect, initialize_database  # noqa: E402


def evaluate(db_path: Path, source_id: str) -> dict[str, object]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        source = connection.execute(
            "SELECT source_name,adapter_config_json FROM sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        rows = connection.execute(
            """SELECT document_id,title,published_at,cleaned_text,canonical_url,
            quality_status,quality_metrics_json FROM documents
            WHERE source_id=? AND is_current=1 AND record_state='quarantined'
            ORDER BY published_at DESC""",
            (source_id,),
        ).fetchall()
        template_rows = connection.execute(
            """SELECT document_id,cleaned_text FROM documents
            WHERE source_id=? AND is_current=1 AND document_format='pdf'""",
            (source_id,),
        ).fetchall()
    config = json.loads(str(source["adapter_config_json"] or "{}")) if source else {}
    site_names = [str(value) for value in config.get("site_names", []) or []]
    all_templates = {
        str(row["document_id"]): str(row["cleaned_text"] or "")
        for row in template_rows
    }
    evaluated: list[dict[str, object]] = []
    for row in rows:
        old_metrics = json.loads(str(row["quality_metrics_json"] or "{}"))
        if float(old_metrics.get("template_similarity") or 0) < 0.88:
            continue
        result = assess_document(
            title=str(row["title"] or ""),
            text=str(row["cleaned_text"] or ""),
            published_at=str(row["published_at"] or ""),
            site_names=site_names,
            template_texts=[
                text
                for document_id, text in all_templates.items()
                if document_id != str(row["document_id"])
            ],
        )
        evaluated.append(
            {
                "document_id": str(row["document_id"]),
                "title": str(row["title"]),
                "canonical_url": str(row["canonical_url"]),
                "old_similarity": old_metrics.get("template_similarity", 0),
                "new_status": result.status,
                "processing_allowed": result.processing_allowed,
                "reason": result.note,
                "structured_fact_count": result.metrics.get(
                    "structured_fact_count", 0
                ),
                "structured_fact_fields": result.metrics.get(
                    "structured_fact_fields", ""
                ),
                "template_similarity_warning": result.metrics.get(
                    "template_similarity_warning", 0
                ),
            }
        )
    return {
        "dry_run": True,
        "source_id": source_id,
        "source_name": str(source["source_name"] if source else ""),
        "evaluated_count": len(evaluated),
        "still_quarantined": sum(
            not bool(item["processing_allowed"]) for item in evaluated
        ),
        "eligible_for_manual_review": sum(
            bool(item["processing_allowed"]) for item in evaluated
        ),
        "note": (
            "本脚本只重新计算质量结论，不修改文档、事件、审核、证据或报告资格。"
        ),
        "documents": evaluated,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读复评官方预警PDF的模板相似度门禁"
    )
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "portscope.db")
    parser.add_argument("--source-id", default="SRC-2FF28D3D7FC5")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "qa" / "warning_template_reevaluation.json",
    )
    args = parser.parse_args()
    payload = evaluate(args.db.resolve(), args.source_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
