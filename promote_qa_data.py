from __future__ import annotations

import argparse
import json
from pathlib import Path

from qa_promotion import promote_qa_events


def main() -> int:
    parser = argparse.ArgumentParser(description="将已通过真人核验和证据门禁的QA事件受控晋升。")
    parser.add_argument("--qa-db", type=Path, required=True)
    parser.add_argument("--formal-db", type=Path, required=True)
    parser.add_argument("--source-workspace-id", required=True)
    parser.add_argument("--target-workspace-id", required=True)
    parser.add_argument("--qa-run-id", required=True)
    parser.add_argument("--event-id", action="append", required=True)
    parser.add_argument("--report-mode", choices=["内部研究版", "客户交付版"], default="内部研究版")
    parser.add_argument(
        "--confirm",
        required=True,
        help="必须填写“我已确认逐条核验”，避免误将整个QA结果视为已审核数据。",
    )
    args = parser.parse_args()
    if args.confirm != "我已确认逐条核验":
        parser.error("--confirm 必须准确填写“我已确认逐条核验”")
    result = promote_qa_events(
        args.qa_db,
        args.formal_db,
        source_workspace_id=args.source_workspace_id,
        target_workspace_id=args.target_workspace_id,
        event_ids=args.event_id,
        qa_run_id=args.qa_run_id,
        report_mode=args.report_mode,
    )
    print(json.dumps({"promoted": list(result.promoted), "blocked": result.blocked}, ensure_ascii=False))
    return 0 if not result.blocked else 2


if __name__ == "__main__":
    raise SystemExit(main())
