from pathlib import Path

from streamlit.testing.v1 import AppTest

from workspace_store import create_workspace
from platform_db import list_sources


def _environment(tmp_path: Path, monkeypatch, name: str):
    db = tmp_path / f"{name}.db"
    root = tmp_path / f"{name}_data"
    env = tmp_path / f"{name}.env"
    monkeypatch.setenv("PORTSCOPE_DB_PATH", str(db))
    monkeypatch.setenv("PORTSCOPE_DATA_ROOT", str(root))
    monkeypatch.setenv("PORTSCOPE_ENV_PATH", str(env))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return db, root, env


def test_first_empty_start_enters_clean_daily_workbench(tmp_path: Path, monkeypatch):
    db, _, _ = _environment(tmp_path, monkeypatch, "wizard")
    initial = AppTest.from_file("app.py", default_timeout=30).run()
    assert not initial.exception
    assert any("今日工作台" in header.value for header in initial.header)
    metric_labels = {metric.label for metric in initial.metric}
    assert {"今日新增文档", "今日有效事件", "待AI处理", "待人工核验", "证据异常"}.issubset(metric_labels)
    assert any(button.label == "开始采集" for button in initial.button)
    assert any(button.label == "人工审核" for button in initial.button)


def test_daily_and_all_advanced_pages_render_without_exception(tmp_path: Path, monkeypatch):
    db, root, _ = _environment(tmp_path, monkeypatch, "smoke")
    create_workspace({"workspace_name": "烟雾测试空间"}, db, root)
    app = AppTest.from_file("app.py", default_timeout=30).run()
    assert not app.exception
    navigation = next(radio for radio in app.radio if "工作台" in list(radio.options))
    assert list(navigation.options) == [
        "工作台", "采集与导入", "文档库", "事件库", "人工审核",
        "证据修复", "智能问答", "报告中心", "商用准备", "历史记录", "来源与系统设置",
    ]
    for page in list(navigation.options):
        next(radio for radio in app.radio if "工作台" in list(radio.options)).set_value(page).run()
        assert not app.exception, f"普通页面渲染失败：{page}"

    next(button for button in app.button if button.label == "打开高级管理").click().run()
    assert not app.exception
    advanced = next(radio for radio in app.radio if "首页概览" in list(radio.options))
    assert len(advanced.options) == 19
    assert {"更新公开数据", "智能问答", "数据审核", "报告中心", "专业设置"}.issubset(set(advanced.options))
    for page in list(advanced.options):
        next(radio for radio in app.radio if "首页概览" in list(radio.options)).set_value(page).run()
        assert not app.exception, f"高级页面渲染失败：{page}"


def test_home_shortcuts_stay_in_daily_workflow(tmp_path: Path, monkeypatch):
    db, root, _ = _environment(tmp_path, monkeypatch, "commands")
    create_workspace({"workspace_name": "命令测试空间"}, db, root)
    app = AppTest.from_file("app.py", default_timeout=30).run()
    assert not app.exception
    command = next(button for button in app.button if button.label == "人工审核")
    command.click().run()
    assert not app.exception
    navigation = next(radio for radio in app.radio if "工作台" in list(radio.options))
    assert navigation.value == "人工审核"


def test_empty_sources_initialize_inside_collection_page(tmp_path: Path, monkeypatch):
    db, root, _ = _environment(tmp_path, monkeypatch, "source_setup")
    workspace = create_workspace({"workspace_name": "来源初始化测试"}, db, root)
    app = AppTest.from_file("app.py", default_timeout=30).run()
    assert not app.exception
    next(radio for radio in app.radio if "工作台" in list(radio.options)).set_value("采集与导入").run()
    confirmation = next(checkbox for checkbox in app.checkbox if checkbox.label.startswith("我确认仅进行低频公开信息核对"))
    confirmation.set_value(True).run()
    next(button for button in app.button if button.label == "一键初始化推荐来源").click().run()
    assert not app.exception
    sources = list_sources(workspace["workspace_id"], db)
    assert len(sources) == 4
    assert sum(bool(item["enabled"]) and bool(item["crawl_allowed"]) for item in sources) == 1


def test_no_api_mode_is_clear_and_advanced_pages_remain_available(tmp_path: Path, monkeypatch):
    db, root, _ = _environment(tmp_path, monkeypatch, "no_api")
    create_workspace({"workspace_name": "无API测试"}, db, root)
    app = AppTest.from_file("app.py", default_timeout=30).run()
    assert not app.exception
    next(radio for radio in app.radio if "工作台" in list(radio.options)).set_value("来源与系统设置").run()
    assert any("配置DeepSeek API" in expander.label for expander in app.expander)
    assert any(button.label == "打开高级管理" for button in app.button)
