from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from importlib import metadata
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parent
MINIMUM_PYTHON = (3, 9)
REQUIRED_DISTRIBUTIONS = {
    "streamlit": "streamlit",
    "pandas": "pandas",
    "requests": "requests",
    "beautifulsoup4": "bs4",
    "python-docx": "docx",
    "pydantic": "pydantic",
    "python-dotenv": "dotenv",
    "pypdf": "pypdf",
    "chromadb": "chromadb",
    "sentence-transformers": "sentence_transformers",
}
SCAN_EXCLUDED_DIRECTORIES = {
    ".git", ".venv", "backups", "data", "dist", "logs", "output",
    "__pycache__", ".pytest_cache",
}
SCAN_TEXT_SUFFIXES = {
    ".bat", ".csv", ".html", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml",
}
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{32,}")
COMMERCIAL_ALLOWED = {"允许", "已确认允许"}


@dataclass(frozen=True)
class CheckResult:
    code: str
    status: str
    message: str


def _result(code: str, status: str, message: str) -> CheckResult:
    return CheckResult(code=code, status=status, message=message)


def _parse_lock(path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    if not path.is_file():
        return locked
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        locked[name.casefold().replace("_", "-")] = version.strip()
    return locked


def check_runtime(root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    if sys.version_info[:2] < MINIMUM_PYTHON:
        results.append(_result(
            "python.version", "fail",
            f"Python版本过低：当前{sys.version_info.major}.{sys.version_info.minor}，最低要求3.9。",
        ))
    else:
        results.append(_result(
            "python.version", "pass",
            f"Python {sys.version_info.major}.{sys.version_info.minor}满足最低要求。",
        ))

    lock_path = root / "requirements-lock.txt"
    locked = _parse_lock(lock_path)
    if not locked:
        results.append(_result("dependencies.lock", "fail", "缺少有效的requirements-lock.txt。"))
        return results
    results.append(_result("dependencies.lock", "pass", f"依赖锁定清单包含{len(locked)}个发行包。"))

    missing: list[str] = []
    mismatched: list[str] = []
    for distribution in REQUIRED_DISTRIBUTIONS:
        normalized = distribution.casefold().replace("_", "-")
        try:
            installed = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            missing.append(distribution)
            continue
        expected = locked.get(normalized)
        if expected and installed != expected:
            mismatched.append(f"{distribution}={installed}（锁定{expected}）")
    if missing:
        results.append(_result("dependencies.installed", "fail", "缺少运行依赖：" + "、".join(missing)))
    elif mismatched:
        results.append(_result("dependencies.installed", "fail", "依赖版本不一致：" + "；".join(mismatched)))
    else:
        results.append(_result("dependencies.installed", "pass", "核心运行依赖已安装且与锁定版本一致。"))
    return results


def check_release_files(root: Path) -> list[CheckResult]:
    required = {
        "VERSION": root / "VERSION",
        "README": root / "README.md",
        "第三方许可证": root / "THIRD_PARTY_LICENSES.md",
        "商用清单": root / "COMMERCIAL_DEPLOYMENT_CHECKLIST.md",
        "隐私与数据处理": root / "PRIVACY_AND_DATA_HANDLING.md",
        "环境变量示例": root / ".env.example",
        "备份脚本": root / "backup_workspace.py",
        "发行打包脚本": root / "package_release.py",
    }
    missing = [label for label, path in required.items() if not path.is_file()]
    result = (
        _result("release.files", "fail", "缺少商用交付文件：" + "、".join(missing))
        if missing else
        _result("release.files", "pass", "商用交付、备份、许可和版本文件齐全。")
    )
    ignore_path = root / ".gitignore"
    ignored = ignore_path.is_file() and ".env" in {
        line.strip() for line in ignore_path.read_text(encoding="utf-8").splitlines()
    }
    ignore_result = (
        _result("secrets.env_ignored", "pass", ".env已被版本控制排除。")
        if ignored else
        _result("secrets.env_ignored", "fail", ".gitignore未明确排除.env。")
    )
    return [result, ignore_result]


def check_commercial_profile(root: Path) -> CheckResult:
    profile_path = root / "config" / "commercial_profile.json"
    if not profile_path.is_file():
        return _result(
            "commercial.profile", "warn",
            "尚未填写经营主体、支持联系人、隐私联系人和合同版本；代码可运行，但不能视为已完成正式商用签署。",
        )
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _result("commercial.profile", "fail", "config/commercial_profile.json无法读取或JSON格式错误。")
    required = {
        "legal_entity_name": "经营主体",
        "product_owner": "产品权利人",
        "support_contact": "支持联系人",
        "privacy_contact": "隐私联系人",
        "contract_terms_version": "合同条款版本",
        "governing_law": "适用法律",
        "approved_at": "批准日期",
        "approved_by": "批准人",
    }
    missing = [label for key, label in required.items() if not str(profile.get(key) or "").strip()]
    if missing:
        return _result("commercial.profile", "fail", "商用主体配置不完整：" + "、".join(missing))
    return _result("commercial.profile", "pass", "经营主体、支持、隐私和合同版本信息已登记。")


def _iter_scan_files(root: Path) -> Iterable[Path]:
    for current, directory_names, file_names in os.walk(root):
        directory_names[:] = [
            name for name in directory_names
            if name not in SCAN_EXCLUDED_DIRECTORIES and "pycache" not in name.casefold()
        ]
        current_path = Path(current)
        for file_name in file_names:
            path = current_path / file_name
            if path.name == ".env" or path.suffix.casefold() not in SCAN_TEXT_SUFFIXES:
                continue
            yield path


def check_secret_hygiene(root: Path) -> CheckResult:
    matches: list[str] = []
    for path in _iter_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if SECRET_PATTERN.search(text):
            matches.append(path.relative_to(root).as_posix())
    if matches:
        return _result(
            "secrets.source_scan", "fail",
            "源码或文档中发现疑似真实API密钥：" + "、".join(matches[:5]),
        )
    return _result("secrets.source_scan", "pass", "源码、文档和配置模板中未发现疑似真实API密钥。")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def check_database(db_path: Path) -> list[CheckResult]:
    if not db_path.is_file():
        return [_result("database.present", "warn", "尚未创建业务数据库；首次启动时将自动初始化。")]
    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [_result("database.open", "fail", f"数据库无法只读打开：{type(exc).__name__}。")]
    try:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        results = [
            _result("database.integrity", "pass", "SQLite完整性检查通过。")
            if integrity == "ok" else
            _result("database.integrity", "fail", "SQLite完整性检查失败。")
        ]
        if not _table_exists(connection, "sources"):
            results.append(_result("sources.schema", "warn", "数据库尚无sources表；需先完成初始化。"))
            return results
        source_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(sources)").fetchall()
        }
        has_split_permissions = {
            "customer_summary_allowed",
            "short_quote_allowed",
            "fulltext_redistribution_allowed",
            "raw_data_resale_allowed",
        }.issubset(source_columns)
        permission_select = (
            "customer_summary_allowed,short_quote_allowed,"
            "fulltext_redistribution_allowed,raw_data_resale_allowed"
            if has_split_permissions
            else "report_use_allowed,report_use_allowed,0,0"
        )
        rows = connection.execute(
            f"""SELECT source_name,domain,list_page_url,enabled,crawl_allowed,robots_status,
            commercial_reuse_status,report_use_allowed,terms_status,license_note,last_license_checked_at,
            {permission_select} FROM sources"""
        ).fetchall()
        enabled = [row for row in rows if bool(row[3]) and bool(row[4])]
        invalid_crawl = [
            str(row[0]) for row in enabled
            if not str(row[1] or "").strip()
            or not str(row[2] or "").startswith(("http://", "https://"))
            or str(row[5] or "") != "允许"
        ]
        invalid_report: list[str] = []
        permission_warnings: list[str] = []
        for row in rows:
            customer_summary = bool(row[11]) and bool(row[12])
            fulltext_or_resale = bool(row[13]) or bool(row[14])
            if not customer_summary and not fulltext_or_resale:
                continue
            if str(row[6] or "") in {"禁止", "不允许"} or str(row[8] or "") in {"禁止", "不允许"}:
                invalid_report.append(str(row[0]))
            elif str(row[6] or "") not in COMMERCIAL_ALLOWED:
                permission_warnings.append(str(row[0]))
        if invalid_crawl:
            results.append(_result(
                "sources.crawl_gate", "fail",
                "已启用来源缺少白名单、列表页或robots许可：" + "、".join(invalid_crawl),
            ))
        elif enabled:
            results.append(_result(
                "sources.crawl_gate", "pass",
                f"{len(enabled)}个自动采集来源通过白名单和robots配置检查。",
            ))
        else:
            results.append(_result("sources.crawl_gate", "warn", "当前没有启用的自动采集来源。"))
        if invalid_report:
            results.append(_result(
                "sources.report_gate", "fail",
                "存在许可/条款证据缺失或过期却允许进入客户报告的来源：" + "、".join(invalid_report),
            ))
        else:
            results.append(_result(
                "sources.report_gate", "pass",
                "未发现来源明确禁止却仍允许客户摘要、短引用或原始数据导出的配置。",
            ))
        if permission_warnings:
            results.append(_result(
                "sources.permission_risk", "warn",
                "以下来源使用状态尚未明确；内部工作台可用，客户报告应提示风险，"
                "全文和原始数据导出保持关闭：" + "、".join(permission_warnings),
            ))
        return results
    except sqlite3.Error as exc:
        return [_result("database.query", "fail", f"数据库结构检查失败：{type(exc).__name__}。")]
    finally:
        connection.close()


def check_fts5() -> CheckResult:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE preflight_fts USING fts5(content)")
        return _result("sqlite.fts5", "pass", "当前SQLite支持FTS5关键词索引。")
    except sqlite3.Error:
        return _result("sqlite.fts5", "fail", "当前SQLite不支持FTS5，关键词检索无法商用运行。")
    finally:
        connection.close()


def run_preflight(root: Path = ROOT, db_path: Path | None = None) -> dict[str, object]:
    project_root = Path(root).resolve()
    database = Path(db_path).resolve() if db_path else project_root / "data" / "portscope.db"
    checks: list[CheckResult] = []
    checks.extend(check_runtime(project_root))
    checks.extend(check_release_files(project_root))
    checks.append(check_commercial_profile(project_root))
    checks.append(check_secret_hygiene(project_root))
    checks.append(check_fts5())
    checks.extend(check_database(database))
    counts = {
        status: sum(item.status == status for item in checks)
        for status in ("pass", "warn", "fail")
    }
    return {
        "product": "PortScope",
        "version": (project_root / "VERSION").read_text(encoding="utf-8").strip()
        if (project_root / "VERSION").is_file() else "unknown",
        "ready": counts["fail"] == 0,
        "counts": counts,
        "checks": [asdict(item) for item in checks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="PortScope商用部署前检查")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="将警告也视为阻断")
    args = parser.parse_args()
    report = run_preflight(args.root, args.db)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"PortScope {report['version']} 商用部署前检查")
        for item in report["checks"]:
            print(f"[{item['status'].upper()}] {item['message']}")
        counts = report["counts"]
        print(f"汇总：通过{counts['pass']}，警告{counts['warn']}，失败{counts['fail']}")
    if report["counts"]["fail"] or (args.strict and report["counts"]["warn"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
