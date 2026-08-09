from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import pkgutil
import re
import sys
import sysconfig
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "backups",
    "dist",
    "output",
    "tests",
}
PACKAGE_BY_MODULE = {
    "bs4": "beautifulsoup4",
    "chromadb": "chromadb",
    "docx": "python-docx",
    "dotenv": "python-dotenv",
    "lxml": "lxml",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "pydantic": "pydantic",
    "pypdf": "pypdf",
    "requests": "requests",
    "sentence_transformers": "sentence-transformers",
    "streamlit": "streamlit",
}
CORE_MODULES = {
    "bs4",
    "docx",
    "dotenv",
    "lxml",
    "openpyxl",
    "pandas",
    "pydantic",
    "pypdf",
    "requests",
    "streamlit",
}
DYNAMIC_RUNTIME_IMPORTS = {
    # The Windows launcher imports lxml as a core readiness check.
    "lxml": "scripts/launch_portscope.py（核心启动检查）",
}


@dataclass(frozen=True)
class DependencyAuditRow:
    import_module: str
    package_name: str
    first_usage_file: str
    declared: bool
    core_dependency: bool
    action: str


def _normalized_package(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def declared_packages(requirements_path: Path) -> set[str]:
    declared: set[str] = set()
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(("-", "--")):
            continue
        name = re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].strip()
        if name:
            declared.add(_normalized_package(name))
    return declared


def _stdlib_modules() -> set[str]:
    names = set(sys.builtin_module_names)
    stdlib_path = Path(sysconfig.get_paths()["stdlib"])
    names.update(module.name for module in pkgutil.iter_modules([str(stdlib_path)]))
    return names


def _local_modules(project_root: Path) -> set[str]:
    modules = {
        path.stem
        for path in _python_files(project_root)
    }
    modules.update(
        path.name
        for path in project_root.iterdir()
        if path.is_dir() and any(path.glob("*.py"))
    )
    return modules


def _python_files(project_root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, filenames in os.walk(project_root):
        directories[:] = [
            name for name in directories if name not in EXCLUDED_PARTS
        ]
        base = Path(current)
        files.extend(base / name for name in filenames if name.endswith(".py"))
    return sorted(files)


def direct_runtime_imports(project_root: Path) -> dict[str, str]:
    stdlib = _stdlib_modules()
    local = _local_modules(project_root)
    imports: dict[str, str] = {}
    for path in _python_files(project_root):
        relative = path.relative_to(project_root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(relative))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".", 1)[0]]
            for name in names:
                if name in stdlib or name in local or name == "__future__":
                    continue
                imports.setdefault(name, relative.as_posix())
    for name, usage in DYNAMIC_RUNTIME_IMPORTS.items():
        imports.setdefault(name, usage)
    return imports


def audit_runtime_dependencies(
    project_root: Path = PROJECT_ROOT,
) -> list[DependencyAuditRow]:
    requirements = declared_packages(project_root / "requirements.txt")
    rows: list[DependencyAuditRow] = []
    for module, usage in sorted(direct_runtime_imports(project_root).items()):
        package = PACKAGE_BY_MODULE.get(module, module.replace("_", "-"))
        declared = _normalized_package(package) in requirements
        core = module in CORE_MODULES
        rows.append(
            DependencyAuditRow(
                import_module=module,
                package_name=package,
                first_usage_file=usage,
                declared=declared,
                core_dependency=core,
                action=(
                    "已声明，保持现状"
                    if declared
                    else "必须补充到requirements.txt"
                ),
            )
        )
    return rows


def print_audit(rows: Iterable[DependencyAuditRow]) -> None:
    print("导入模块\tPyPI包\t首次使用文件\t已声明\t核心依赖\t处理方式")
    for row in rows:
        payload = asdict(row)
        print(
            f"{payload['import_module']}\t{payload['package_name']}\t"
            f"{payload['first_usage_file']}\t"
            f"{'是' if payload['declared'] else '否'}\t"
            f"{'是' if payload['core_dependency'] else '否'}\t"
            f"{payload['action']}"
        )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计源码直接运行依赖；不会调用pip")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    rows = audit_runtime_dependencies(args.project_root.resolve())
    print_audit(rows)
    return 1 if any(not row.declared for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
