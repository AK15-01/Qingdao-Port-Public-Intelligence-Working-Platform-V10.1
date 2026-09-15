"""对真实项目目录构建一次交付包，并检查交付卫生。

与 `tests/test_agent_platform.py::test_release_package_excludes_environment_git_secret_and_real_data`
的区别：那条用例在合成目录上验证排除规则，本文件针对**当前真实仓库**构建 ZIP，
因此能发现「规则写对了但真实目录里仍混进东西」这一类问题。
两者互补，都保留。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from zipfile import ZipFile

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from package_release import build_release, main, should_include  # noqa: E402


# 运行 Demo / 安装依赖 / 阅读说明所必需的文件，缺一不可。
REQUIRED_RUNTIME_FILES = (
    "app.py",
    "README.md",
    "requirements.txt",
    "requirements-rag.txt",
    "requirements-lock.txt",
    "pyproject.toml",
    "LICENSE",
    "VERSION",
    ".env.example",
    ".streamlit/config.toml",
    "data/demo/portscope_demo.db",
)

# 交付包中绝对不允许出现的路径片段。
FORBIDDEN_FRAGMENTS = (
    "/.git/",
    "/.venv/",
    "/__pycache__/",
    "/.pytest_cache/",
    "/.ruff_cache/",
    "/.mypy_cache/",
    "/.vscode/",
    "/.idea/",
    "/htmlcov/",
    "/output/",
    "/backups/",
    "/dist/",
    "/data/raw/",
    "/data/chroma/",
    "/data/workspaces/",
)

FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".log", ".zip")


@pytest.fixture(scope="module")
def release(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, list[str]]:
    output = tmp_path_factory.mktemp("release") / "portscope-test-release.zip"
    result = build_release(PROJECT_ROOT, output)
    with ZipFile(output) as archive:
        names = archive.namelist()
    return result, names


def _relative(names: list[str]) -> set[str]:
    """去掉 ZIP 内的顶层项目目录前缀，便于按仓库相对路径断言。"""
    return {name.split("/", 1)[1] for name in names if "/" in name}


def test_release_builds_and_reports_consistent_counts(release):
    result, names = release
    assert Path(result["output"]).is_file()
    assert result["size_bytes"] > 0
    # manifest 自身额外占一个条目。
    assert len(names) == result["file_count"] + 1


def test_release_contains_no_environment_file(release):
    _, names = release
    offenders = [n for n in names if Path(n).name.casefold().startswith(".env")
                 and Path(n).name.casefold() != ".env.example"]
    assert offenders == [], f"交付包含有环境/密钥文件：{offenders}"


def test_release_contains_no_secret_toml(release):
    _, names = release
    offenders = [n for n in names if Path(n).name.casefold().startswith("secrets.")]
    assert offenders == [], f"交付包含有 Secret 配置：{offenders}"


def test_release_contains_no_vcs_cache_or_ide_directories(release):
    _, names = release
    offenders = [n for n in names if any(f in f"/{n}" for f in FORBIDDEN_FRAGMENTS)]
    assert offenders == [], f"交付包含有开发机目录：{offenders[:10]}"


def test_release_contains_no_compiled_or_log_artefacts(release):
    _, names = release
    offenders = [n for n in names if n.casefold().endswith(FORBIDDEN_SUFFIXES)]
    assert offenders == [], f"交付包含有编译产物或日志：{offenders[:10]}"


def test_release_contains_no_real_database(release):
    """只允许脱敏演示库；任何其他 .db 都不得进入交付包。"""
    _, names = release
    databases = [n for n in names if n.casefold().endswith((".db", ".db-shm", ".db-wal"))]
    relative = {n.split("/", 1)[1] for n in databases if "/" in n}
    assert relative == {"data/demo/portscope_demo.db"}, f"交付包中的数据库不符合预期：{sorted(relative)}"


def test_release_contains_required_runtime_files(release):
    _, names = release
    present = _relative(names)
    missing = [required for required in REQUIRED_RUNTIME_FILES if required not in present]
    assert missing == [], f"交付包缺少运行所必需的文件：{missing}"


def test_release_manifest_is_present_and_hashes_every_file(release):
    result, names = release
    manifest_entries = [n for n in names if n.endswith("release_manifest.json")]
    assert len(manifest_entries) == 1
    with ZipFile(result["output"]) as archive:
        manifest = json.loads(archive.read(manifest_entries[0]).decode("utf-8"))
    assert manifest["product"] == "PortScope"
    assert manifest["file_count"] == len(manifest["files"]) == result["file_count"]
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_release_warns_when_a_local_env_file_exists(release):
    """本机存在 .env 时必须给出轮换提醒——文件被排除不等于密钥已失效。"""
    result, _ = release
    if (PROJECT_ROOT / ".env").is_file():
        assert result["security_warnings"], "本机存在 .env 却没有给出轮换提醒"
    else:
        assert result["security_warnings"] == []


def test_secret_files_are_excluded_even_outside_the_real_tree():
    """规则级断言：真实目录里暂时没有这些文件，也必须提前拦住。"""
    for path in (
        ".env",
        ".env.local",
        ".env.production",
        ".streamlit/secrets.toml",
        "config/secrets.prod.toml",
    ):
        assert not should_include(Path(path)), f"{path} 必须被排除"
    assert should_include(Path(".env.example")), ".env.example 是模板，必须保留"


def test_cli_help_does_not_create_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """回归：修复前 `--help` 会被当成输出文件名。"""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert list(tmp_path.iterdir()) == [], "--help 不应产生任何文件"


def test_cli_accepts_output_flag_and_positional_path(tmp_path: Path):
    flag_output = tmp_path / "by-flag.zip"
    assert main(["--output", str(flag_output)]) == 0
    assert flag_output.is_file()

    positional_output = tmp_path / "by-position.zip"
    assert main([str(positional_output)]) == 0
    assert positional_output.is_file()


def test_cli_rejects_conflicting_output_arguments(tmp_path: Path):
    with pytest.raises(SystemExit) as exit_info:
        main(["--output", str(tmp_path / "a.zip"), str(tmp_path / "b.zip")])
    assert exit_info.value.code != 0
