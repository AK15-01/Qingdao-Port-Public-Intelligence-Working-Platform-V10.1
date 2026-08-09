from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from zipfile import ZIP_DEFLATED, ZipFile


EXCLUDED_DIRECTORIES = {
    ".venv", ".git", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache",
    "chroma", "raw", "logs", "task_logs", "dist", "reports", "exports",
    "workspaces", "legacy_backup", "backups", "output",
}
EXCLUDED_NAMES = {
    ".env", "portscope.db", "portscope.db-shm", "portscope.db-wal",
    "commercial_profile.json",
    "real_acceptance_labels.xlsx", "real_acceptance_report.md",
    "real_acceptance_report.json", "real_acceptance_report.xlsx",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".db"}
ALLOWED_DATA_FILES = {"events_template.csv", "sources_template.csv", "sample_events.csv"}
PUBLIC_DEMO_DATABASE = Path("data/demo/portscope_demo.db")


def should_include(relative: Path) -> bool:
    parts = relative.parts
    if any(part in EXCLUDED_DIRECTORIES or "pycache" in part.casefold() for part in parts[:-1]):
        return False
    if any(part.casefold().startswith(("pytest-", "pytest_")) for part in parts):
        return False
    if relative.as_posix() == PUBLIC_DEMO_DATABASE.as_posix():
        return True
    if relative.name in EXCLUDED_NAMES or relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if relative.name.lower().endswith(".inspect.ndjson"):
        return False
    if len(parts) == 1 and relative.name.startswith("WS-"):
        return False
    if parts and parts[0] == "data" and relative.name not in ALLOWED_DATA_FILES:
        return False
    if parts and parts[0] == "qa" and relative.suffix.casefold() in {".xlsx", ".xls"}:
        return False
    if relative.name.lower().endswith((".zip", ".tmp")):
        return False
    return True


def build_release(source_root: Path, output_path: Path) -> dict[str, object]:
    root = Path(source_root).resolve()
    output = Path(output_path).resolve()
    if not root.is_dir():
        raise ValueError("项目目录不存在")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(root):
        directory_names[:] = [name for name in directory_names if name not in EXCLUDED_DIRECTORIES]
        current_path = Path(current)
        for file_name in file_names:
            path = current_path / file_name
            relative = path.relative_to(root)
            if path.resolve() in {output, temporary} or not should_include(relative):
                continue
            files.append(path)
    version_path = root / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else "unversioned"
    manifest_files: list[dict[str, object]] = []
    for path in sorted(files):
        content = path.read_bytes()
        manifest_files.append({
            "path": path.relative_to(root).as_posix(),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    manifest = {
        "product": "PortScope",
        "version": version,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python_target": ">=3.9",
        "build_platform": platform.system(),
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            archive.write(path, Path(root.name) / path.relative_to(root))
        archive.writestr((Path(root.name) / "release_manifest.json").as_posix(), manifest_bytes)
    temporary.replace(output)
    return {
        "output": str(output),
        "version": version,
        "file_count": len(files),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "size_bytes": output.stat().st_size,
        "security_warnings": (
            ["源目录存在.env；文件已强制排除，交付前仍应轮换临时测试密钥。"]
            if (root / ".env").is_file() else []
        ),
    }


def main() -> int:
    root = Path(__file__).resolve().parent
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dist" / f"PortScope-release-{datetime.now():%Y%m%d-%H%M%S}.zip"
    result = build_release(root, output)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
