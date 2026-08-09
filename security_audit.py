from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile


SECRET_PATTERN = re.compile(rb"sk-[A-Za-z0-9_-]{32,}")
TEXT_SUFFIXES = {
    ".bat", ".csv", ".html", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml", ".log",
}
SKIP_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache"}


def release_env_warning(root: Path) -> str:
    """Warn only for an unpacked release; development checkouts may legitimately use .env."""

    project = Path(root)
    if (project / "release_manifest.json").is_file() and (project / ".env").is_file():
        return (
            "安全警告：发行包目录中发现.env。请立即移除该文件并轮换其中的测试密钥；"
            "发行包不应携带任何API密钥。"
        )
    return ""


def audit_secret_traces(root: Path) -> dict[str, object]:
    project = Path(root).resolve()
    suspect_files: list[str] = []
    archives_with_env: list[str] = []
    archives_with_secret: list[str] = []
    scanned_files = 0
    scanned_archives = 0
    for current, directories, files in os.walk(project):
        directories[:] = [
            name for name in directories
            if name not in SKIP_DIRECTORIES
            and "pycache" not in name.casefold()
            and not name.casefold().startswith("pytest-")
        ]
        current_path = Path(current)
        for filename in files:
            path = current_path / filename
            relative = path.relative_to(project).as_posix()
            if path.name == ".env":
                # The active local secret is intentionally not read by this audit.
                continue
            if path.suffix.casefold() == ".zip":
                scanned_archives += 1
                try:
                    with ZipFile(path) as archive:
                        names = archive.namelist()
                        if any(Path(name).name == ".env" for name in names):
                            archives_with_env.append(relative)
                        for info in archive.infolist():
                            if info.file_size > 5 * 1024 * 1024:
                                continue
                            suffix = Path(info.filename).suffix.casefold()
                            if suffix not in TEXT_SUFFIXES and Path(info.filename).name != ".env":
                                continue
                            if SECRET_PATTERN.search(archive.read(info)):
                                archives_with_secret.append(relative)
                                break
                except (OSError, BadZipFile):
                    continue
                continue
            if path.suffix.casefold() not in TEXT_SUFFIXES:
                continue
            scanned_files += 1
            try:
                if SECRET_PATTERN.search(path.read_bytes()):
                    suspect_files.append(relative)
            except OSError:
                continue
    return {
        "ok": not suspect_files and not archives_with_env and not archives_with_secret,
        "scanned_files": scanned_files,
        "scanned_archives": scanned_archives,
        "suspect_files": sorted(set(suspect_files)),
        "archives_with_env": sorted(set(archives_with_env)),
        "archives_with_secret": sorted(set(archives_with_secret)),
        "note": "审计从不读取项目根目录.env，也不会输出任何密钥内容。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描源码、报告、日志和历史ZIP中的密钥痕迹。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    result = audit_secret_traces(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
