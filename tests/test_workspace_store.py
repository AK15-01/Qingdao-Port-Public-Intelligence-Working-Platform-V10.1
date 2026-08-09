from pathlib import Path

from data_store import create_event, load_events
from intake_store import create_intake, load_intakes
from source_store import create_source, load_sources
from workspace_store import (
    create_client,
    create_workspace,
    get_active_workspace,
    load_clients,
    load_workspaces,
    migrate_legacy_csvs,
    next_report_identity,
    record_report,
    set_active_workspace,
    workspace_paths,
)


def event_values(title: str) -> dict[str, str]:
    return {
        "event_date": "2026-07-20",
        "category": "政策监管",
        "title": title,
        "summary": "这是一条用于工作空间隔离测试的公开事实摘要。",
        "impact": "可能影响相关业务安排，需要继续核对公开来源。",
        "source_name": "测试公开来源",
        "source_type": "政府/监管机构",
        "source_url": f"https://example.com/{title}",
        "status": "新增",
    }


def test_workspace_can_be_created_reloaded_and_selected(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    first = create_workspace({"workspace_name": "空间一"}, db, root)
    second = create_workspace({"workspace_name": "空间二"}, db, root)
    assert [item["workspace_name"] for item in load_workspaces(db)] == ["空间一", "空间二"]
    assert get_active_workspace(db)["workspace_id"] == second["workspace_id"]
    set_active_workspace(first["workspace_id"], db)
    assert get_active_workspace(db)["workspace_name"] == "空间一"


def test_workspace_event_files_are_isolated(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    first = create_workspace({"workspace_name": "甲"}, db, root)
    second = create_workspace({"workspace_name": "乙"}, db, root)
    first_paths = workspace_paths(first["workspace_id"], root)
    second_paths = workspace_paths(second["workspace_id"], root)
    create_event(event_values("甲空间事件"), first_paths.events)
    assert len(load_events(first_paths.events)) == 1
    assert load_events(second_paths.events).empty
    assert first_paths.events != second_paths.events


def test_workspace_sources_and_intakes_are_isolated(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    first = create_workspace({"workspace_name": "甲"}, db, root)
    second = create_workspace({"workspace_name": "乙"}, db, root)
    first_paths = workspace_paths(first["workspace_id"], root)
    second_paths = workspace_paths(second["workspace_id"], root)
    create_source(
        {
            "source_name": "测试来源",
            "source_type": "政府/监管机构",
            "homepage_url": "https://example.com",
            "check_frequency": "每日",
            "priority": "高",
            "enabled": "是",
            "last_status": "未检查",
        },
        first_paths.sources,
    )
    create_intake(
        {
            "source_url": "https://example.com/draft",
            "fetched_title": "测试草稿",
            "fetched_text": "公开草稿正文",
            "reviewer_status": "待审核",
        },
        first_paths.intakes,
    )
    assert len(load_sources(first_paths.sources)) == 1
    assert len(load_intakes(first_paths.intakes)) == 1
    assert load_sources(second_paths.sources).empty
    assert load_intakes(second_paths.intakes).empty


def test_legacy_csv_migration_keeps_backup_and_adds_workspace_id(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    root.mkdir()
    create_event(event_values("旧版事件"), root / "events.csv")
    workspace = create_workspace({"workspace_name": "迁移空间"}, db, root)
    copied = migrate_legacy_csvs(workspace["workspace_id"], db, root)
    paths = workspace_paths(workspace["workspace_id"], root)
    migrated = load_events(paths.events)
    assert "events.csv" in copied
    assert migrated.iloc[0]["workspace_id"] == workspace["workspace_id"]
    assert list((root / "legacy_backup").rglob("events.csv"))
    assert load_events(root / "events.csv").iloc[0]["title"] == "旧版事件"


def test_client_is_scoped_to_workspace_and_reloadable(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    first = create_workspace({"workspace_name": "甲"}, db, root)
    second = create_workspace({"workspace_name": "乙"}, db, root)
    client = create_client(
        first["workspace_id"],
        {"client_name": "测试客户", "focus_categories": ["政策监管"], "enabled": True},
        db,
    )
    assert load_clients(first["workspace_id"], db)[0]["client_id"] == client["client_id"]
    assert load_clients(second["workspace_id"], db) == []


def test_report_identity_increments_version_without_overwriting(tmp_path: Path):
    db = tmp_path / "meta.db"
    workspace = create_workspace({"workspace_name": "报告空间"}, db, tmp_path / "data")
    args = (workspace["workspace_id"], "", "测试报告", "2026-07-01", "2026-07-07", db)
    report_id, version = next_report_identity(*args)
    record_report(
        {
            "report_id": report_id,
            "workspace_id": workspace["workspace_id"],
            "report_title": "测试报告",
            "start_date": "2026-07-01",
            "end_date": "2026-07-07",
            "version": version,
        },
        db,
    )
    repeated_id, repeated_version = next_report_identity(*args)
    assert repeated_id == report_id
    assert repeated_version == 2
