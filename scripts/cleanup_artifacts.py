from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
CONFIRMATION = "CLEAN_ARTIFACTS"
PROTECTED_OUTPUT_NAMES = {
    "qa_real",
    "current_evidence_repair",
    "reports",
    "business_reports",
    "restore_drills",
}


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    size_bytes: int
    reason: str


def path_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    return sum(
        file.stat().st_size
        for file in path.rglob("*")
        if file.is_file()
        for _ in [None]
        if _safe_stat(file)
    )


def _safe_stat(path: Path) -> bool:
    try:
        path.stat()
        return True
    except OSError:
        return False


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _protected(path: Path, root: Path = ROOT) -> bool:
    root = Path(root).resolve()
    protected_project_paths = {
        root / ".venv",
        root / ".git",
        root / "data",
        root / "qa",
        root / "backups",
        root / "dist",
    }
    resolved = path.resolve()
    if any(_is_within(resolved, protected) for protected in protected_project_paths):
        return True
    output = (root / "output").resolve()
    try:
        relative = resolved.relative_to(output)
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] in PROTECTED_OUTPUT_NAMES)


def _task_log_candidates(
    output: Path,
    *,
    cutoff: datetime,
    keep_task_logs: int,
) -> Iterable[CleanupCandidate]:
    log_root = output / "task_logs"
    if not log_root.is_dir():
        return []
    logs = sorted(
        (path for path in log_root.glob("*.log") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    result = []
    for index, path in enumerate(logs):
        modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        if index < max(1, keep_task_logs) or modified >= cutoff:
            continue
        result.append(CleanupCandidate(path, path.stat().st_size, "过期Worker任务日志"))
    return result


def plan_cleanup(
    project_root: Path = ROOT,
    *,
    older_than_days: int = 14,
    keep_task_logs: int = 50,
) -> list[CleanupCandidate]:
    root = Path(project_root).resolve()
    output = root / "output"
    cutoff = datetime.now().astimezone() - timedelta(days=max(1, older_than_days))
    candidates: list[CleanupCandidate] = []
    if output.is_dir():
        for path in output.iterdir():
            if _protected(path, root):
                continue
            if path.is_dir() and path.name.startswith("pytest-"):
                candidates.append(CleanupCandidate(path, path_size(path), "pytest临时输出目录"))
            elif path.is_file() and path.suffix.casefold() in {".log", ".tmp"}:
                modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
                if modified < cutoff:
                    candidates.append(CleanupCandidate(path, path.stat().st_size, "过期调试输出"))
        candidates.extend(
            _task_log_candidates(
                output,
                cutoff=cutoff,
                keep_task_logs=keep_task_logs,
            )
        )
    for cache in (root / ".pytest_cache",):
        if cache.exists():
            candidates.append(CleanupCandidate(cache, path_size(cache), "pytest缓存"))
    for cache in root.rglob("__pycache__"):
        if (
            cache.is_dir()
            and not _protected(cache, root)
            and not _is_within(cache, output)
        ):
            candidates.append(CleanupCandidate(cache, path_size(cache), "Python字节码缓存"))
    unique: dict[Path, CleanupCandidate] = {}
    for item in candidates:
        resolved = item.path.resolve()
        if _protected(resolved, root):
            continue
        unique[resolved] = CleanupCandidate(resolved, item.size_bytes, item.reason)
    return sorted(unique.values(), key=lambda item: str(item.path).casefold())


def apply_cleanup(
    candidates: Iterable[CleanupCandidate],
    *,
    confirmed: bool = False,
    project_root: Path = ROOT,
) -> dict[str, int]:
    if not confirmed:
        raise PermissionError(f"清理前必须提供确认词 {CONFIRMATION}")
    removed = 0
    released = 0
    for item in candidates:
        path = item.path.resolve()
        if _protected(path, project_root):
            raise PermissionError(f"拒绝删除受保护路径：{path}")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            continue
        removed += 1
        released += item.size_bytes
    return {"removed": removed, "released_bytes": released}


def _format_size(value: int) -> str:
    return f"{value / (1024 * 1024):.2f} MiB"


def main() -> int:
    parser = argparse.ArgumentParser(description="安全清理PortScope测试缓存与过期调试输出")
    parser.add_argument("--dry-run", action="store_true", help="只列出候选；这是默认行为")
    parser.add_argument("--apply", action="store_true", help="实际清理")
    parser.add_argument("--confirm", default="", help=f"实际清理时必须填写 {CONFIRMATION}")
    parser.add_argument("--older-than-days", type=int, default=14)
    parser.add_argument("--keep-task-logs", type=int, default=50)
    args = parser.parse_args()
    candidates = plan_cleanup(
        ROOT,
        older_than_days=args.older_than_days,
        keep_task_logs=args.keep_task_logs,
    )
    total = sum(item.size_bytes for item in candidates)
    mode = "执行预览" if not args.apply else "准备清理"
    print(f"{mode}：{len(candidates)}项，预计释放 {_format_size(total)}")
    for item in candidates:
        print(f"- {_format_size(item.size_bytes):>12} | {item.reason} | {item.path}")
    if not args.apply:
        print(f"未删除任何文件。实际清理需同时使用 --apply --confirm {CONFIRMATION}")
        return 0
    if args.confirm != CONFIRMATION:
        print(f"确认词错误；未删除任何文件。需要：--confirm {CONFIRMATION}", file=sys.stderr)
        return 2
    result = apply_cleanup(candidates, confirmed=True)
    print(f"已清理 {result['removed']} 项，释放 {_format_size(result['released_bytes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
