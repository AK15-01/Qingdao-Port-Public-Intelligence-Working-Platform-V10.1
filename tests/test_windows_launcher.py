from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from scripts.launch_portscope import (
    CORE_MODULES,
    find_available_port,
    inspect_environment,
    paths_match,
    runtime_paths,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def _daily_launch_text() -> str:
    files = (
        PROJECT_ROOT / "run.bat",
        PROJECT_ROOT / "scripts" / "launch_portscope.py",
    )
    return "\n".join(path.read_text(encoding="utf-8") for path in files).casefold()


def test_daily_launcher_is_rooted_at_batch_directory_and_has_no_installer_chain():
    run_text = (PROJECT_ROOT / "run.bat").read_text(encoding="utf-8").casefold()
    launch_text = _daily_launch_text()
    assert 'set "project_root=%~dp0"' in run_text
    assert 'set "project_python=%project_root%.venv\\scripts\\python.exe"' in run_text
    assert 'cd /d "%project_root%"' in run_text
    assert "ensure_environment.bat" not in run_text
    assert "requirements-lock" not in launch_text
    assert "pip install" not in launch_text
    assert "pip upgrade" not in launch_text
    assert " -m venv" not in launch_text


def test_compatibility_environment_check_never_installs_or_creates_environment():
    text = (PROJECT_ROOT / "ensure_environment.bat").read_text(
        encoding="utf-8"
    ).casefold()
    assert "--check-only" in text
    assert "pip install" not in text
    assert "requirements-lock" not in text
    assert " -m venv" not in text


@pytest.mark.parametrize(
    "folder",
    ["中文目录", "space path", "hash#path", "and&path", "paren(path)"],
)
def test_runtime_paths_support_windows_special_characters(tmp_path: Path, folder: str):
    root = tmp_path / folder / "青岛港 工作台#(1)&"
    paths = runtime_paths(root)
    assert paths.project_root == root.resolve()
    assert paths.expected_python == root.resolve() / ".venv" / "Scripts" / "python.exe"
    assert paths.app_path == root.resolve() / "app.py"
    assert paths.database_path == root.resolve() / "data" / "portscope.db"


def test_port_selection_moves_to_next_port_when_preferred_is_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied = int(listener.getsockname()[1])
        selected = find_available_port(occupied, maximum=min(65535, occupied + 20))
    assert selected != occupied
    assert selected > occupied


def test_project_python_identity_is_path_based_not_current_working_directory():
    paths = runtime_paths(PROJECT_ROOT)
    assert paths_match(paths.expected_python, PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
    assert not paths_match(paths.expected_python, PROJECT_ROOT.parent / ".venv" / "Scripts" / "python.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows batch integration test")
@pytest.mark.skipif(
    not PROJECT_PYTHON.is_file(),
    reason="需要已配置的项目 .venv：先运行 setup_environment.bat，本集成用例才有可断言的对象",
)
def test_check_only_from_unrelated_directory_uses_project_venv_offline(tmp_path: Path):
    unrelated = tmp_path / "错误 工作目录#(1)&"
    (unrelated / ".venv" / "Scripts").mkdir(parents=True)
    (unrelated / ".venv" / "Scripts" / "python.exe").write_bytes(b"not a python")
    environment = dict(os.environ)
    environment.update(
        {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "PORTSCOPE_NO_PAUSE": "1",
        }
    )
    result = subprocess.run(
        [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            str(PROJECT_ROOT / "run.bat"),
            "--check-only",
        ],
        cwd=unrelated,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    expected = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
    assert str(unrelated / ".venv" / "Scripts" / "python.exe") not in result.stdout
    assert "核心启动条件通过" in result.stdout


def test_current_project_environment_has_all_declared_core_modules():
    paths = runtime_paths(PROJECT_ROOT)
    if not paths_match(Path(sys.executable), paths.expected_python):
        pytest.skip("This assertion is exercised by the Windows batch integration test")
    report = inspect_environment(paths, check_port=False)
    assert report.python_belongs_to_project
    assert all(report.core_modules[name] == "可用" for name in CORE_MODULES)
