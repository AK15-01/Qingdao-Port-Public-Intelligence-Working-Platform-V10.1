from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

import runtime_config
from deepseek_service import load_settings, save_settings_to_env
from deployment_check import run_checks
from public_demo_store import PublicDemoRepository
from package_release import should_include
from runtime_config import is_public_demo_mode, runtime_value
from scripts.scan_public_demo_safety import scan_project


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_DB = PROJECT_ROOT / "data" / "demo" / "portscope_demo.db"


def _public_environment(monkeypatch, database: Path = DEMO_DB) -> None:
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("PORTSCOPE_DEMO_DB_PATH", str(database))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_public_mode_environment_wins_over_streamlit_secret():
    assert is_public_demo_mode(
        environ={"PUBLIC_DEMO_MODE": "false"},
        secret_reader=lambda _: "true",
    ) is False
    value, source = runtime_value(
        "DEEPSEEK_API_KEY",
        environ={"DEEPSEEK_API_KEY": "from-env"},
        secret_reader=lambda _: "from-secret",
    )
    assert value == "from-env"
    assert source == "环境变量"


def test_public_deepseek_settings_ignore_project_dotenv_and_support_secrets(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=must-not-win\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("PORTSCOPE_ENV_PATH", str(env_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        runtime_config,
        "streamlit_secret",
        lambda name: "secret-value" if name == "DEEPSEEK_API_KEY" else None,
    )
    settings = load_settings()
    assert settings.api_key == "secret-value"
    assert settings.api_key_source == "Streamlit secrets"
    assert settings.config_file == ""


def test_public_mode_cannot_write_local_ai_settings(tmp_path: Path, monkeypatch):
    _public_environment(monkeypatch)
    target = tmp_path / ".env"
    with pytest.raises(PermissionError):
        save_settings_to_env(api_key="not-written", env_path=target)
    assert not target.exists()


def test_sanitized_demo_database_contains_no_fulltext_or_local_paths():
    assert DEMO_DB.is_file()
    connection = sqlite3.connect(DEMO_DB)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert tables == {
        "demo_metadata",
        "demo_sources",
        "demo_documents",
        "demo_events",
        "demo_evidence",
    }
    assert "cleaned_text" not in {
        row[1] for row in connection.execute("PRAGMA table_info(demo_documents)")
    }
    payload = DEMO_DB.read_bytes()
    assert b"C:\\Users" not in payload
    assert b"OneDrive" not in payload
    assert b"DEEPSEEK_API_KEY" not in payload
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_demo_repository_supports_unicode_space_hash_and_ampersand_path(tmp_path: Path):
    directory = tmp_path / "中文 空格 # 与 & 符号"
    directory.mkdir()
    database = directory / "演示 数据.db"
    shutil.copy2(DEMO_DB, database)
    metrics = PublicDemoRepository(database).metrics()
    assert metrics.document_count >= 1
    assert metrics.event_count >= 1


def test_public_demo_home_is_read_only_and_hides_dangerous_actions(monkeypatch):
    _public_environment(monkeypatch)
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert any("港航公开信息监测与分析平台" in title.value for title in app.title)
    assert any("只读演示" in caption.value for caption in app.caption)
    labels = {button.label for button in app.button}
    assert not {
        "开始采集",
        "立即更新公开数据",
        "提取并分析",
        "生成客户报告",
        "测试DeepSeek连接",
    }.intersection(labels)
    assert "file_uploader" not in (PROJECT_ROOT / "ui_public_demo.py").read_text(encoding="utf-8")
    assert not any("URL" in item.label or "网址" in item.label for item in app.text_input)
    metric_labels = {metric.label for metric in app.metric}
    assert {"已采集文档", "合格正文", "结构化事件", "启用来源", "最近更新时间"}.issubset(metric_labels)


def test_all_public_pages_render_without_exception_and_no_admin_navigation(monkeypatch):
    _public_environment(monkeypatch)
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
    navigation = next(radio for radio in app.radio if "平台概览" in list(radio.options))
    assert list(navigation.options) == ["平台概览", "事件浏览", "数据来源", "报告能力", "关于"]
    for page in navigation.options:
        next(radio for radio in app.radio if "平台概览" in list(radio.options)).set_value(page).run()
        assert not app.exception, f"公网页面渲染失败：{page}"
        assert not any("采集" == option for radio in app.radio for option in radio.options)


def test_missing_demo_database_fails_gracefully_without_traceback(tmp_path: Path, monkeypatch):
    _public_environment(monkeypatch, tmp_path / "missing.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert any("数据暂时无法加载" in error.value for error in app.error)
    page_text = " ".join(str(item.value) for item in [*app.error, *app.caption])
    assert "Traceback" not in page_text
    assert str(tmp_path) not in page_text


def test_deployment_check_passes_without_deepseek_key_in_public_mode(monkeypatch):
    _public_environment(monkeypatch)
    checks = run_checks()
    assert all(item.ok for item in checks if item.required)


def test_public_safety_scan_has_no_blocking_findings():
    assert [item for item in scan_project() if item.blocking] == []


def test_env_example_and_gitignore_cover_public_secrets():
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "PUBLIC_DEMO_MODE=true" in example
    assert "DEEPSEEK_API_KEY=" in example
    assert "sk-" not in example
    assert ".env.*" in ignore
    assert "!.env.example" in ignore
    assert ".streamlit/secrets.toml" in ignore
    assert should_include(Path("data/demo/portscope_demo.db"))
    assert not should_include(Path("data/portscope.db"))
