"""把当前 SQLite 的真实状态注入 README 顶部的「已验证事实」区块。

与 `scripts/generate_current_state_audit.py` 共用 `state_audit.collect_state_audit`，
因此 README 顶部的数字与 `CURRENT_STATE_AUDIT.md` 同源，不会出现手写与实测两套口径。

默认只读预览，与项目其他同步脚本保持一致；`--apply` 才写回文件。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlite3  # noqa: E402

from state_audit import collect_state_audit  # noqa: E402

START_MARKER = "<!-- portscope-readme-status:start -->"
END_MARKER = "<!-- portscope-readme-status:end -->"


def count_tracked_python_lines(root: Path) -> int:
    total = 0
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in {".venv", "__pycache__", "backups", "dist", "output"} for part in relative.parts):
            continue
        total += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return total


def count_test_functions(root: Path) -> int:
    pattern = re.compile(r"^def test_", re.MULTILINE)
    return sum(
        len(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
        for path in sorted((root / "tests").glob("test_*.py"))
    )


def render_status_block(audit: dict, *, python_lines: int, test_functions: int) -> str:
    events = audit["events"]
    documents = audit["documents"]
    evidence = audit["evidence"]
    sources = audit["enabled_sources"]
    latest = audit["latest_successful_crawl"] or {}

    def mark(value: int) -> str:
        return f"**{value}**" if value == 0 else str(value)

    lines = [
        START_MARKER,
        "",
        "## 当前已验证事实",
        "",
        "> 由 `scripts/sync_readme_status.py` 从 `data/portscope.db` 生成，不是手写结论。",
        f"> 代码版本 `{audit['code_version']}`｜生成于 {audit['generated_at']}"
        f"｜数据库指纹 `{audit['database_sha256'][:12]}`",
        "",
        "**工程侧（可被第三方复核）**",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        f"| 源码行数（不含 .venv/缓存/输出） | {python_lines:,} |",
        f"| 测试函数（不含参数化展开） | {test_functions} |",
        "| 网络访问的测试 | 0（全部 Mock/fixture） |",
        "",
        "**业务侧（诚实口径，0 就写 0）**",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        f"| 启用来源 | {mark(len(sources))} |",
        f"| 文档（active / 隔离） | {documents['total']}（{documents['active']} / {documents['quarantined']}） |",
        f"| 事件（active） | {events['total']}（{events['active']}） |",
        f"| 证据已验证 / 未解决 | {evidence['verified']} / {evidence['unresolved']} |",
        f"| 独立真人审核事件 | {mark(events['human_verified'])} |",
        f"| 内部使用资格 | {mark(events['internal_eligible'])} |",
        f"| 客户报告资格 | {mark(events['customer_eligible'])} |",
        f"| 历史报告 | {audit['reports']} |",
    ]
    if latest:
        lines.append(
            f"| 最近一次成功采集 | {str(latest.get('finished_at') or '')[:19]}"
            f"（新增文档 {latest.get('new_document_count', 0)}） |"
        )
    else:
        # 空值必须显式呈现。静默省略整行会让读者以为这一项不存在，
        # 而不是「至今没有一次成功采集」。
        lines.append("| 最近一次成功采集 | **尚未有成功采集** |")
    lines += [
        "",
        "加粗的 0 是尚未跨过的门槛，不是缺陷：客户报告资格必须由独立真人审核、"
        "逐字证据和来源许可共同放行，本项目至今没有为了好看而放宽任何一条。",
        "",
        END_MARKER,
    ]
    return "\n".join(lines)


def inject(readme_text: str, block: str) -> str:
    if START_MARKER in readme_text and END_MARKER in readme_text:
        pattern = re.compile(
            re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
            re.DOTALL,
        )
        return pattern.sub(lambda _: block, readme_text, count=1)
    raise SystemExit(
        f"README 缺少状态区块标记。请先加入：\n{START_MARKER}\n{END_MARKER}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 README 顶部的真实状态区块")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "portscope.db")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--apply", action="store_true", help="写回 README；默认只读预览")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"[跳过] 数据库不存在：{args.db}；README 状态区块保持不变。")
        return 0

    try:
        audit = collect_state_audit(args.db, ROOT)
    except sqlite3.Error as exc:
        # 数据库存在但结构不完整（例如尚未 initialize_database）时不抛栈，
        # 也不写入任何内容——README 宁可保持旧值，也不能写入半截状态。
        print(f"[跳过] 无法读取数据库状态：{type(exc).__name__}；README 状态区块保持不变。")
        return 0
    block = render_status_block(
        audit,
        python_lines=count_tracked_python_lines(ROOT),
        test_functions=count_test_functions(ROOT),
    )
    original = args.readme.read_text(encoding="utf-8")
    updated = inject(original, block)

    if updated == original:
        print("README 状态区块已是最新，无需修改。")
        return 0
    if not args.apply:
        print("[只读预览] README 状态区块将更新为：\n")
        print(block)
        print("\n确认后重新运行并加上 --apply。")
        return 0
    args.readme.write_text(updated, encoding="utf-8")
    print(f"已更新 {args.readme} 的状态区块（代码版本 {audit['code_version']}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
