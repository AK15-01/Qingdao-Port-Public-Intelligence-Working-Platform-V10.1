from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_db import connect, initialize_database


def audit(db_path: Path, workspace_id: str = "") -> dict[str, object]:
    initialize_database(db_path)
    workspace_clause = " AND e.workspace_id=?" if workspace_id else ""
    params = (workspace_id,) if workspace_id else ()
    with connect(db_path) as connection:
        event_counts = dict(connection.execute(
            f"""SELECT COUNT(*) AS events,SUM(e.human_verified) AS human_flags,
            SUM(CASE WHEN e.reviewer_type IN ('human_user','industry_reviewer') THEN 1 ELSE 0 END)
            AS independent_human,SUM(e.evidence_verified) AS verified_events
            FROM events e WHERE 1=1{workspace_clause}""",
            params,
        ).fetchone())
        quote_counts = dict(connection.execute(
            f"""SELECT COUNT(*) AS quotes,
            SUM(CASE WHEN x.verification_status='已验证' THEN 1 ELSE 0 END) AS verified,
            SUM(CASE WHEN x.verification_status!='已验证' THEN 1 ELSE 0 END) AS failed
            FROM event_evidence x JOIN events e ON e.event_id=x.event_id
            WHERE 1=1{workspace_clause}""",
            params,
        ).fetchone())
        failures = [
            dict(row)
            for row in connection.execute(
                f"""SELECT x.event_id,x.document_id,x.quote_text,x.document_version,
                x.content_hash,x.normalization_method,x.failure_reason
                FROM event_evidence x JOIN events e ON e.event_id=x.event_id
                WHERE x.verification_status!='已验证'{workspace_clause}
                ORDER BY x.event_id,x.evidence_id""",
                params,
            ).fetchall()
        ]
    return {
        "event_counts": {key: int(value or 0) for key, value in event_counts.items()},
        "quote_counts": {key: int(value or 0) for key, value in quote_counts.items()},
        "failures": failures,
        "core_rule": "只有事件全部引用在当前版本逐字定位时，才可进入执行摘要和客户报告。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="审计结构化证据绑定并逐条列出失败原因。")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.db, args.workspace_id)
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(args.output)
    print(content)
    return 0 if not result["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
