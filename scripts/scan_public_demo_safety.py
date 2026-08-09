from __future__ import annotations

"""Scan deployable files without ever echoing suspected secret values."""

from dataclasses import dataclass
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".bat", ".csv", ".example"}
SKIP_DIRECTORIES = {".venv", "venv", ".git", ".pytest_cache", "__pycache__", "output", "backups", "dist"}
SECRET_FILE_NAMES = {".env", "secrets.toml"}
SECRET_PATTERN = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b")
ABSOLUTE_USER_PATH = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\r\n]+")
SAFE_MARKERS = (
    "your_key", "example", "placeholder", "sk-test", "sk-fake", "sk-demo",
    "sk-mock", "sk-secret", "sk-private-secret", "sk-this-is-a-secret", "sk-****",
)


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str
    blocking: bool


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _scan_text(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    lowered = text.lower()
    for match in SECRET_PATTERN.finditer(text):
        token = match.group(0).lower()
        if not any(marker in token for marker in SAFE_MARKERS):
            findings.append(Finding(_relative(path), "疑似硬编码API密钥", True))
            break
    if ABSOLUTE_USER_PATH.search(text):
        findings.append(Finding(_relative(path), "个人Windows绝对路径", True))
    if "authorization" in lowered or "bearer" in lowered:
        findings.append(Finding(_relative(path), "认证字段名称（人工确认）", False))
    return findings


def scan_project() -> list[Finding]:
    findings: list[Finding] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRECTORIES for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        if path.name in SECRET_FILE_NAMES:
            findings.append(Finding(_relative(path), "本机Secret文件存在（必须保持忽略）", False))
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name == ".env.example":
            try:
                findings.extend(_scan_text(path, path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                findings.append(Finding(_relative(path), "文件无法读取", False))
        elif path.suffix.lower() == ".db":
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            if SECRET_PATTERN.search(payload.decode("latin-1", errors="ignore")):
                findings.append(Finding(_relative(path), "数据库中存在疑似密钥痕迹", True))
        elif path.suffix.lower() == ".zip":
            try:
                with ZipFile(path) as archive:
                    names = archive.namelist()
                    if any(Path(name).name in SECRET_FILE_NAMES for name in names):
                        findings.append(Finding(_relative(path), "ZIP包含Secret文件", True))
            except (BadZipFile, OSError):
                findings.append(Finding(_relative(path), "ZIP无法检查", False))
    return findings


def main() -> int:
    findings = scan_project()
    for finding in findings:
        level = "BLOCK" if finding.blocking else "INFO"
        print(f"[{level}] {finding.path}: {finding.kind}")
    blocking = [item for item in findings if item.blocking]
    print(f"Safety Scan: {'FAIL' if blocking else 'PASS'} · blocking={len(blocking)} · info={len(findings)-len(blocking)}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
