from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    "logs",
}
EXCLUDED_SUFFIXES = {".pyc", ".tmp", ".log", ".zip"}


def _backup_path_allowed(path: Path, *, relative_parts: tuple[str, ...] | None = None) -> bool:
    parts = tuple(relative_parts or path.parts)
    lowered = tuple(str(part).casefold() for part in parts)
    if any(
        part in EXCLUDED_DIRECTORY_NAMES
        or part.startswith("pytest-")
        for part in lowered[:-1]
    ):
        return False
    name = lowered[-1] if lowered else path.name.casefold()
    if name == ".env":
        return False
    return Path(name).suffix.casefold() not in EXCLUDED_SUFFIXES


def backup_workspace(output: Path | None = None) -> dict[str, object]:
    destination = output or ROOT / "backups" / f"PortScope-workspace-{datetime.now():%Y%m%d-%H%M%S}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_zip = destination.with_suffix(".zip.tmp")
    database = ROOT / "data" / "portscope.db"
    file_count = 0
    try:
        with tempfile.TemporaryDirectory(prefix="portscope-backup-") as temporary_directory:
            snapshot = Path(temporary_directory) / "portscope.db"
            if database.is_file():
                source = sqlite3.connect(database)
                target = sqlite3.connect(snapshot)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
            with ZipFile(temporary_zip, "w", ZIP_DEFLATED, compresslevel=9) as archive:
                if snapshot.is_file():
                    archive.write(snapshot, "data/portscope.db")
                    file_count += 1
                candidates = [
                    ROOT / "config",
                    ROOT / "data" / "raw",
                    ROOT / "data" / "workspaces",
                    ROOT / "output",
                ]
                for base in candidates:
                    if not base.is_dir():
                        continue
                    for path in base.rglob("*"):
                        if not path.is_file() or not _backup_path_allowed(
                            path,
                            relative_parts=path.relative_to(ROOT).parts,
                        ):
                            continue
                        archive.write(path, path.relative_to(ROOT))
                        file_count += 1
                for path in (ROOT / ".env.example", ROOT / "sources.md"):
                    if path.is_file():
                        archive.write(path, path.name)
                        file_count += 1
        temporary_zip.replace(destination)
    except Exception:
        temporary_zip.unlink(missing_ok=True)
        raise
    return {"path": str(destination), "file_count": file_count, "size_bytes": destination.stat().st_size}


def sanitize_backup_archive(archive_path: Path) -> dict[str, object]:
    """Remove secret/config and test-temporary members without reading their content."""

    archive_path = Path(archive_path).resolve()
    if not archive_path.is_file():
        raise FileNotFoundError("备份文件不存在")
    temporary = archive_path.with_suffix(archive_path.suffix + ".sanitize.tmp")
    kept = 0
    removed = 0
    try:
        with ZipFile(archive_path, "r") as source, ZipFile(
            temporary,
            "w",
            ZIP_DEFLATED,
            compresslevel=9,
        ) as target:
            for member in source.infolist():
                member_path = Path(member.filename)
                if member.is_dir() or not _backup_path_allowed(
                    member_path,
                    relative_parts=member_path.parts,
                ):
                    removed += 1
                    continue
                target.writestr(member, source.read(member))
                kept += 1
        temporary.replace(archive_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "path": str(archive_path),
        "kept": kept,
        "removed": removed,
        "size_bytes": archive_path.stat().st_size,
    }


def restore_database_snapshot(
    backup_zip: Path,
    target_database: Path,
    *,
    confirmed: bool = False,
) -> dict[str, object]:
    """Restore only the SQLite snapshot to an explicit target, never implicitly."""

    if not confirmed:
        raise PermissionError("恢复数据库前必须明确确认")
    backup_zip = Path(backup_zip).resolve()
    target_database = Path(target_database).resolve()
    if not backup_zip.is_file():
        raise FileNotFoundError("备份文件不存在")
    target_database.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_database.with_suffix(target_database.suffix + ".restore.tmp")
    with ZipFile(backup_zip) as archive:
        member = next(
            (item for item in archive.infolist() if item.filename == "data/portscope.db"),
            None,
        )
        if not member:
            raise ValueError("备份中没有SQLite快照")
        payload = archive.read(member)
    temporary.write_bytes(payload)
    connection = sqlite3.connect(str(temporary))
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()
    if quick_check != "ok":
        temporary.unlink(missing_ok=True)
        raise ValueError("备份中的SQLite快照完整性检查失败")
    previous = target_database.with_suffix(target_database.suffix + ".before-restore")
    if target_database.exists():
        if previous.exists():
            previous.unlink()
        target_database.replace(previous)
    temporary.replace(target_database)
    return {
        "target": str(target_database),
        "previous": str(previous) if previous.exists() else "",
        "quick_check": quick_check,
    }


if __name__ == "__main__":
    result = backup_workspace()
    print(f"工作台备份已生成：{result['path']}")
    print(f"文件数：{result['file_count']}，大小：{result['size_bytes']} 字节")
    print("安全说明：备份不包含 .venv，也不包含 .env/API密钥。")
