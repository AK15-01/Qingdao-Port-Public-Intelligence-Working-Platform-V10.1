from __future__ import annotations

"""Read-only preflight for local or hosted Streamlit deployments."""

from dataclasses import dataclass
import importlib
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Iterable

from public_demo_store import PublicDemoRepository
from runtime_config import PROJECT_ROOT, load_runtime_config, project_relative


CORE_MODULES = (
    "streamlit",
    "pandas",
    "pydantic",
    "requests",
    "bs4",
    "lxml",
    "dotenv",
    "docx",
    "pypdf",
    "openpyxl",
)
OPTIONAL_RAG_MODULES = ("chromadb", "sentence_transformers")


@dataclass(frozen=True)
class CheckItem:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _imports(names: Iterable[str], *, required: bool) -> list[CheckItem]:
    result: list[CheckItem] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:
            result.append(CheckItem(f"依赖 {name}", False, type(exc).__name__, required))
        else:
            result.append(CheckItem(f"依赖 {name}", True, "可导入", required))
    return result


def _sqlite_check(path: Path, *, required: bool) -> CheckItem:
    if not path.is_file():
        return CheckItem("数据库", False, f"未找到 {project_relative(path)}", required)
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        connection.close()
    except sqlite3.Error as exc:
        return CheckItem("数据库", False, f"只读检查失败：{type(exc).__name__}", required)
    return CheckItem("数据库", result.lower() == "ok", f"{project_relative(path)} · {result}", required)


def run_checks() -> list[CheckItem]:
    config = load_runtime_config()
    checks = [
        CheckItem("Python", sys.version_info >= (3, 9), sys.version.split()[0]),
        CheckItem("入口", (PROJECT_ROOT / "app.py").is_file(), "app.py"),
        CheckItem("配置目录", (PROJECT_ROOT / "config").is_dir(), "config/"),
        CheckItem("数据目录", (PROJECT_ROOT / "data").is_dir(), "data/"),
    ]
    checks.extend(_imports(CORE_MODULES, required=True))
    checks.extend(_imports(OPTIONAL_RAG_MODULES, required=False))
    if config.public_demo_mode:
        checks.append(_sqlite_check(config.demo_database_path, required=True))
        health = PublicDemoRepository(config.demo_database_path).health()
        checks.append(CheckItem("只读演示数据", bool(health["ok"]), str(health["message"])))
    else:
        checks.append(
            _sqlite_check(PROJECT_ROOT / "data" / "portscope.db", required=False)
        )
    try:
        with tempfile.TemporaryDirectory(prefix="portscope-deploy-check-") as directory:
            probe = Path(directory) / "write-test"
            probe.write_text("ok", encoding="utf-8")
            writable = probe.read_text(encoding="utf-8") == "ok"
    except OSError as exc:
        checks.append(CheckItem("临时目录", False, type(exc).__name__))
    else:
        checks.append(CheckItem("临时目录", writable, "可写" if writable else "不可写"))
    return checks


def main() -> int:
    config = load_runtime_config()
    print(f"Python: {sys.version.split()[0]}")
    print(f"Mode: {config.mode_label}")
    print(
        "DeepSeek API Key:",
        "CONFIGURED" if config.deepseek_key_present else "NOT CONFIGURED",
    )
    checks = run_checks()
    for item in checks:
        marker = "PASS" if item.ok else ("WARN" if not item.required else "FAIL")
        print(f"[{marker}] {item.name}: {item.detail}")
    failed = [item for item in checks if item.required and not item.ok]
    print("Deployment Check:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
