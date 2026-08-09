from pathlib import Path
import csv

import pytest

from platform_db import connect, initialize_database, initialize_recommended_sources, list_sources, load_recommended_sources, migrate_csv_to_sqlite, recommended_source_issues, seed_source_entries, table_counts, transaction, upsert_source
from workspace_store import create_workspace


def _workspace(tmp_path: Path):
    db = tmp_path / "portscope.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "SQLite测试"}, db, root)
    initialize_database(db)
    return db, root, workspace


def test_sqlite_initializes_required_core_tables(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    counts = table_counts(workspace["workspace_id"], db)
    assert {
        "sources", "crawl_runs", "documents", "events", "document_chunks",
        "reports", "qa_logs", "event_evidence", "event_reviews", "promotion_history",
    }.issubset(set(counts))
    with connect(db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"sources", "crawl_runs", "documents", "events", "document_chunks", "reports", "qa_logs", "document_chunks_fts"}.issubset(tables)


def test_source_seed_is_editable_disabled_and_license_safe(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    assert seed_source_entries(workspace["workspace_id"], db) == 5
    sources = list_sources(workspace["workspace_id"], db)
    assert all(not item["enabled"] and not item["crawl_allowed"] for item in sources)
    shanghai = next(item for item in sources if "上海航运交易所" in item["source_name"])
    assert shanghai["commercial_reuse_status"] == "需取得许可"
    assert not shanghai["report_use_allowed"]


def test_csv_migration_backs_up_and_imports(tmp_path: Path):
    db, root, workspace = _workspace(tmp_path)
    events = tmp_path / "events.csv"
    events.write_text("event_id,event_date,title,summary,impact,source_name,source_type,source_url,status\nEVT-1,2026-07-20,迁移测试,公开事实摘要足够长,潜在影响说明足够长,测试来源,政府/监管机构,https://example.com/a,新增\n", encoding="utf-8-sig")
    result = migrate_csv_to_sqlite(workspace["workspace_id"], {"events": events}, db, root / "backup")
    assert result["events"] == 1
    assert Path(result["backup_dir"]).joinpath("events.csv").exists()
    assert table_counts(workspace["workspace_id"], db)["events"] == 1


def test_transaction_rolls_back_all_changes(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    with pytest.raises(RuntimeError):
        with transaction(db) as connection:
            connection.execute("INSERT INTO qa_logs(qa_id,workspace_id,question,answer,created_at) VALUES('QA-X',?,?,?,?)", (workspace["workspace_id"], "q", "a", "now"))
            raise RuntimeError("rollback")
    assert table_counts(workspace["workspace_id"], db)["qa_logs"] == 0


def test_enabled_source_requires_real_whitelist_domain(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    with pytest.raises(ValueError):
        upsert_source({"workspace_id": workspace["workspace_id"], "source_name": "不完整", "enabled": True, "crawl_allowed": True}, db)


def test_recommended_sources_initialize_complete_entries_and_isolate_incomplete(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    templates = load_recommended_sources()
    assert len(templates) == 4
    assert all(str(item["homepage_url"]).startswith(("http://", "https://")) for item in templates)
    incomplete_template = next(item for item in templates if item.get("template_complete") is False)
    assert recommended_source_issues(incomplete_template)

    result = initialize_recommended_sources(workspace["workspace_id"], db)
    assert result["added"] == 4
    assert result["enabled"] == 1
    assert len(result["incomplete"]) == 1
    sources = list_sources(workspace["workspace_id"], db)
    assert len(sources) == 4
    assert sum(bool(item["enabled"]) and bool(item["crawl_allowed"]) for item in sources) == 1
    ocean = next(item for item in sources if "海洋预报" in item["source_name"])
    assert not ocean["enabled"] and "超时" in ocean["license_note"]


def test_recommended_sources_upgrade_legacy_placeholders_without_duplicates(tmp_path: Path):
    db, _, workspace = _workspace(tmp_path)
    assert seed_source_entries(workspace["workspace_id"], db) == 5
    result = initialize_recommended_sources(workspace["workspace_id"], db)
    sources = list_sources(workspace["workspace_id"], db)
    assert result["updated"] == 4
    assert len(sources) == 5  # The separate Shanghai exchange placeholder remains untouched.
    assert sum(".invalid" not in str(item["domain"]) for item in sources) == 4


def test_recommended_source_validation_does_not_require_html_selectors_for_rss():
    assert recommended_source_issues({
        "source_name": "公开订阅测试",
        "organization": "测试机构",
        "domain": "example.com",
        "homepage_url": "https://example.com",
        "list_page_url": "https://example.com/feed.xml",
        "source_type": "RSS",
        "adapter_type": "rss",
    }) == []
