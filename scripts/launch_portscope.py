from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict, dataclass
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Iterable
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


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
CORE_PACKAGE_NAMES = {
    "streamlit": "streamlit",
    "pandas": "pandas",
    "pydantic": "pydantic",
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "dotenv": "python-dotenv",
    "docx": "python-docx",
    "pypdf": "pypdf",
    "openpyxl": "openpyxl",
}
ADVANCED_MODULES = ("chromadb", "sentence_transformers", "torch")
PROJECT_MARKER = ".portscope-project"
RUNTIME_STATE = "data/.portscope-runtime.json"


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    expected_python: Path
    app_path: Path
    database_path: Path
    runtime_state_path: Path


@dataclass(frozen=True)
class EnvironmentReport:
    project_root: str
    expected_python: str
    actual_python: str
    python_version: str
    python_belongs_to_project: bool
    marker_exists: bool
    app_exists: bool
    package_directories_exist: bool
    data_directory_exists: bool
    data_directory_writable: bool
    database_path: str
    database_exists: bool
    database_readable: bool
    database_writable: bool
    database_integrity: str
    core_modules: dict[str, str]
    advanced_modules: dict[str, str]
    streamlit_version: str
    onedrive_path: bool
    preferred_port: int
    selected_port: int
    existing_instance_url: str
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def core_ready(self) -> bool:
        return not self.errors


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def _normal_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def paths_match(left: Path, right: Path) -> bool:
    return _normal_path(left) == _normal_path(right)


def runtime_paths(project_root: Path | str) -> RuntimePaths:
    root = Path(project_root).resolve()
    configured_database = os.environ.get("PORTSCOPE_DB_PATH", "").strip()
    database = Path(configured_database) if configured_database else root / "data" / "portscope.db"
    if not database.is_absolute():
        database = root / database
    return RuntimePaths(
        project_root=root,
        expected_python=root / ".venv" / "Scripts" / "python.exe",
        app_path=root / "app.py",
        database_path=database.resolve(),
        runtime_state_path=root / RUNTIME_STATE,
    )


def _module_status(module_name: str, *, import_module: bool = True) -> str:
    try:
        if import_module:
            importlib.import_module(module_name)
        elif importlib.util.find_spec(module_name) is None:
            raise ModuleNotFoundError(module_name)
        return "可用"
    except Exception as exc:
        return f"不可用：{type(exc).__name__}: {str(exc)[:240]}"


def missing_core_modules() -> list[str]:
    return [
        name
        for name in CORE_MODULES
        if _module_status(name, import_module=True) != "可用"
    ]


def missing_core_packages() -> list[str]:
    return [CORE_PACKAGE_NAMES[name] for name in missing_core_modules()]


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def find_available_port(preferred: int = 8501, *, maximum: int = 8599) -> int:
    for port in range(preferred, maximum + 1):
        if _port_available(port):
            return port
    raise RuntimeError(f"{preferred}–{maximum}端口均被占用")


def _health_ok(port: int, timeout: float = 0.8) -> bool:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{port}/_stcore/health",
            timeout=timeout,
        ) as response:
            return response.status == 200 and response.read(32).strip().lower() == b"ok"
    except (OSError, URLError):
        return False


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def existing_instance(paths: RuntimePaths) -> str:
    try:
        payload = json.loads(paths.runtime_state_path.read_text(encoding="utf-8"))
        port = int(payload.get("port") or 0)
        pid = int(payload.get("pid") or 0)
        recorded_root = Path(str(payload.get("project_root") or ""))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ""
    if (
        port > 0
        and paths_match(recorded_root, paths.project_root)
        and _process_alive(pid)
        and _health_ok(port)
    ):
        return f"http://127.0.0.1:{port}"
    return ""


def _database_status(database_path: Path) -> tuple[bool, bool, str]:
    if not database_path.exists():
        parent = database_path.parent
        return False, os.access(parent, os.W_OK) if parent.exists() else os.access(parent.parent, os.W_OK), "尚未创建"
    readable = os.access(database_path, os.R_OK)
    writable = os.access(database_path, os.W_OK)
    if not readable:
        return False, writable, "不可读"
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "locked" in message.casefold():
            integrity = "数据库正被占用；请关闭重复实例后重试"
        else:
            integrity = f"读取失败：{message[:240]}"
    return readable, writable, integrity


def inspect_environment(
    paths: RuntimePaths,
    *,
    preferred_port: int = 8501,
    check_port: bool = True,
    deep_advanced_check: bool = False,
) -> EnvironmentReport:
    warnings: list[str] = []
    errors: list[str] = []
    actual_python = Path(sys.executable).resolve()
    belongs = paths_match(actual_python, paths.expected_python)
    if not belongs:
        errors.append(
            "当前解释器不是项目 .venv。"
            f"实际：{actual_python}；期望：{paths.expected_python}"
        )
    marker_exists = (paths.project_root / PROJECT_MARKER).is_file()
    if not marker_exists:
        errors.append(f"缺少项目身份标识 {PROJECT_MARKER}")
    app_exists = paths.app_path.is_file()
    if not app_exists:
        errors.append("项目 app.py 不存在")
    package_directories_exist = all(
        (paths.project_root / name).is_dir() for name in ("crawler", "rag", "scripts")
    )
    if not package_directories_exist:
        errors.append("crawler、rag 或 scripts 项目目录缺失")

    core = {name: _module_status(name, import_module=True) for name in CORE_MODULES}
    missing = [name for name, status in core.items() if status != "可用"]
    if missing:
        errors.append(
            "缺少或无法导入核心模块：" + "、".join(missing)
            + "。请主动运行 repair_environment.bat。"
        )
    advanced = {
        name: _module_status(name, import_module=deep_advanced_check)
        for name in ADVANCED_MODULES
    }
    advanced_missing = [name for name, status in advanced.items() if status != "可用"]
    if advanced_missing:
        warnings.append(
            "高级RAG模块不可用：" + "、".join(advanced_missing)
            + "；基础工作台仍可尝试启动，向量检索相关功能会受影响。"
        )
    try:
        streamlit_version = importlib.metadata.version("streamlit")
    except importlib.metadata.PackageNotFoundError:
        streamlit_version = "未安装"

    data_directory = paths.project_root / "data"
    data_exists = data_directory.is_dir()
    data_writable = os.access(data_directory, os.W_OK) if data_exists else os.access(paths.project_root, os.W_OK)
    if not data_writable:
        errors.append("data目录不存在且项目目录不可写，无法安全初始化本地数据目录")
    database_readable, database_writable, database_integrity = _database_status(paths.database_path)
    if paths.database_path.exists() and not database_readable:
        errors.append("SQLite数据库不可读")
    if paths.database_path.exists() and not database_writable:
        errors.append("SQLite数据库不可写")
    if database_integrity not in {"ok", "尚未创建"}:
        if "占用" in database_integrity:
            warnings.append(database_integrity)
        else:
            errors.append("SQLite完整性检查未通过：" + database_integrity)

    path_text = str(paths.project_root).casefold()
    onedrive = "onedrive" in path_text
    if onedrive:
        warnings.append(
            "项目位于OneDrive同步目录。避免同时运行多个实例；长期建议迁移到 C:\\PortScope\\。"
        )

    current_url = existing_instance(paths) if check_port else ""
    if check_port and current_url:
        selected_port = int(current_url.rsplit(":", 1)[1])
    elif check_port:
        try:
            selected_port = find_available_port(preferred_port)
        except RuntimeError as exc:
            selected_port = 0
            errors.append(str(exc))
    else:
        selected_port = preferred_port

    return EnvironmentReport(
        project_root=str(paths.project_root),
        expected_python=str(paths.expected_python),
        actual_python=str(actual_python),
        python_version=sys.version.replace("\n", " "),
        python_belongs_to_project=belongs,
        marker_exists=marker_exists,
        app_exists=app_exists,
        package_directories_exist=package_directories_exist,
        data_directory_exists=data_exists,
        data_directory_writable=data_writable,
        database_path=str(paths.database_path),
        database_exists=paths.database_path.exists(),
        database_readable=database_readable,
        database_writable=database_writable,
        database_integrity=database_integrity,
        core_modules=core,
        advanced_modules=advanced,
        streamlit_version=streamlit_version,
        onedrive_path=onedrive,
        preferred_port=preferred_port,
        selected_port=selected_port,
        existing_instance_url=current_url,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def print_report(report: EnvironmentReport, *, detailed: bool = False) -> None:
    print("[PortScope] 项目根目录：", report.project_root)
    print("[PortScope] 实际Python：", report.actual_python)
    print("[PortScope] Python版本：", report.python_version)
    print("[PortScope] Streamlit版本：", report.streamlit_version)
    print("[PortScope] SQLite数据库：", report.database_path)
    print("[PortScope] 数据库完整性：", report.database_integrity)
    if report.existing_instance_url:
        print("[PortScope] 已有工作台实例：", report.existing_instance_url)
    else:
        print("[PortScope] 实际启动端口：", report.selected_port)
    if detailed:
        print("[PortScope] 核心模块：")
        for name, status in report.core_modules.items():
            print(f"  - {name}: {status}")
        print("[PortScope] 高级RAG模块：")
        for name, status in report.advanced_modules.items():
            print(f"  - {name}: {status}")
        print("[PortScope] 数据目录可写：", "是" if report.data_directory_writable else "否")
        print("[PortScope] 数据库可读/可写：",
              f"{'是' if report.database_readable else '否'}/"
              f"{'是' if report.database_writable else '否'}")
        print("[PortScope] OneDrive目录：", "是" if report.onedrive_path else "否")
    for warning in report.warnings:
        print("[PortScope] 警告：", warning)
    for error in report.errors:
        print("[PortScope] 错误：", error)
    if report.core_ready:
        print("[PortScope] 核心启动条件通过。")
    else:
        print("[PortScope] 核心启动条件未通过；未安装、删除或重建任何环境。")


def _write_runtime_state(paths: RuntimePaths, process: subprocess.Popen[bytes], port: int) -> None:
    paths.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.runtime_state_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "project_root": str(paths.project_root),
                "python": str(paths.expected_python),
                "pid": process.pid,
                "port": port,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(paths.runtime_state_path)


def _remove_runtime_state(paths: RuntimePaths, process_pid: int) -> None:
    try:
        payload = json.loads(paths.runtime_state_path.read_text(encoding="utf-8"))
        if int(payload.get("pid") or 0) == process_pid:
            paths.runtime_state_path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return


def launch_streamlit(
    paths: RuntimePaths,
    report: EnvironmentReport,
    *,
    open_browser: bool = True,
) -> int:
    if not report.core_ready:
        return 2
    if report.existing_instance_url:
        print("[PortScope] 当前项目已经运行，不再启动第二个实例。")
        print("[PortScope] 访问地址：", report.existing_instance_url)
        if open_browser:
            webbrowser.open(report.existing_instance_url)
        return 0
    port = report.selected_port
    if not port:
        return 3
    data_directory = paths.project_root / "data"
    data_directory.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}"
    command = [
        str(paths.expected_python),
        "-m",
        "streamlit",
        "run",
        str(paths.app_path),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
        "--server.headless",
        "true",
    ]
    print("[PortScope] 正在启动工作台：", url)
    print("[PortScope] 日常启动不会安装依赖或修改包版本。")
    try:
        process = subprocess.Popen(command, cwd=str(paths.project_root))
    except OSError as exc:
        print(f"[PortScope] 无法启动Streamlit：{type(exc).__name__}: {exc}")
        return 4
    _write_runtime_state(paths, process, port)
    try:
        for _ in range(40):
            return_code = process.poll()
            if return_code is not None:
                print("[PortScope] Streamlit在启动完成前退出，退出码：", return_code)
                return int(return_code or 1)
            if _health_ok(port):
                print("[PortScope] 工作台已可访问：", url)
                if open_browser:
                    webbrowser.open(url)
                break
            time.sleep(0.25)
        else:
            print("[PortScope] Streamlit进程仍在运行，但10秒内未通过本地健康检查。")
            print("[PortScope] 请查看上方Streamlit输出或运行 diagnose_environment.bat。")
        return int(process.wait())
    except KeyboardInterrupt:
        print("\n[PortScope] 收到停止请求，正在关闭Streamlit。")
        process.terminate()
        try:
            return int(process.wait(timeout=10))
        except subprocess.TimeoutExpired:
            process.kill()
            return int(process.wait())
    finally:
        _remove_runtime_state(paths, process.pid)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PortScope Windows本地启动器")
    parser.add_argument("--check-only", action="store_true", help="只检查，不启动Streamlit")
    parser.add_argument("--diagnose", action="store_true", help="显示详细环境诊断")
    parser.add_argument("--list-missing-core", action="store_true", help="输出缺失核心依赖对应的包名")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--skip-port-check", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-pause", action="store_true", help="供批处理自动验收使用")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.list_missing_core:
        print(" ".join(missing_core_packages()))
        return 0
    paths = runtime_paths(project_root_from_script())
    report = inspect_environment(
        paths,
        preferred_port=max(1, min(int(args.port), 65535)),
        check_port=not args.skip_port_check,
        deep_advanced_check=args.diagnose,
    )
    print_report(report, detailed=args.diagnose)
    if args.check_only or args.diagnose:
        return 0 if report.core_ready else 2
    return launch_streamlit(paths, report, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
