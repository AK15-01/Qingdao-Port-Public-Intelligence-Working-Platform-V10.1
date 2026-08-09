from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_db import initialize_database  # noqa: E402
from state_audit import write_state_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="从当前SQLite生成PortScope状态审计")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "portscope.db")
    parser.add_argument("--output", type=Path, default=ROOT / "CURRENT_STATE_AUDIT.md")
    args = parser.parse_args()
    initialize_database(args.db)
    audit = write_state_audit(args.db, ROOT, args.output)
    print(
        f"已生成 {args.output}｜版本 {audit['code_version']}｜"
        f"文档 {audit['documents']['total']}｜事件 {audit['events']['total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
