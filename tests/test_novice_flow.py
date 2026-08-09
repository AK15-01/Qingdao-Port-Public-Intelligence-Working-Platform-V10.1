from pathlib import Path

import pandas as pd
import pytest

from commercial_report import generate_commercial_report
from data_store import create_event, load_events
from intake_store import HighDuplicateError, SourceVerificationRequired, load_intakes
from ui_novice import analyze_and_save_draft, confirm_novice_draft
from workspace_store import create_workspace, load_action_history, workspace_paths


def reviewed_event(title: str, url: str) -> dict[str, str]:
    return {
        "event_date": "2026-07-20",
        "category": "政策监管",
        "title": title,
        "summary": "公开来源显示这是一条经过人工审核的测试事件。",
        "impact": "可能影响相关政策响应，需要继续核对适用范围。",
        "affected_area": "测试区域",
        "affected_period": "测试时段",
        "source_name": "测试公开来源",
        "source_type": "政府/监管机构",
        "source_url": url,
        "status": "新增",
        "related_event_id": "",
        "analyst_note": "测试记录",
        "recommended_action": "继续核对原文。",
    }


def test_novice_input_analysis_confirmation_and_persistence(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "新手流程"}, db, root)
    paths = workspace_paths(workspace["workspace_id"], root)
    intake_id, analysis = analyze_and_save_draft(
        source_url="https://example.com/public",
        title="公开政策测试通知",
        published_date="2026-07-20",
        source_name="测试公开来源",
        text="公开来源发布政策测试通知，相关单位可按公开要求办理。",
        events=load_events(paths.events),
        intake_path=paths.intakes,
    )
    assert analysis["category_suggestion"] == "政策监管"
    event_id, report = confirm_novice_draft(
        intake_id,
        reviewed_event("公开政策测试通知", "https://example.com/public"),
        source_checked=True,
        workspace_id=workspace["workspace_id"],
        paths=paths,
        db_path=db,
    )
    assert report.can_save
    restarted = load_events(paths.events).iloc[0]
    assert restarted["event_id"] == event_id
    assert restarted["workspace_id"] == workspace["workspace_id"]
    assert restarted["human_confirmed_at"]
    assert restarted["report_included"] == "是"
    assert load_intakes(paths.intakes).iloc[0]["confirmed_event_id"] == event_id
    assert any(item["action_type"] == "确认入库" for item in load_action_history(workspace["workspace_id"], db))
    artifacts = generate_commercial_report(
        load_events(paths.events),
        workspace,
        paths,
        client=None,
        start_date="2026-07-14",
        end_date="2026-07-20",
        report_title="新手完整流程测试报告",
        analyst="测试团队",
        db_path=db,
    )
    assert artifacts.docx_path.exists()
    assert artifacts.html_path.exists()
    assert artifacts.xlsx_path.exists()


def test_novice_confirmation_requires_source_check(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "核对测试"}, db, root)
    paths = workspace_paths(workspace["workspace_id"], root)
    intake_id, _ = analyze_and_save_draft(
        source_url="https://example.com/check",
        title="需要核对的政策通知",
        published_date="2026-07-20",
        source_name="测试公开来源",
        text="这是一条公开政策内容，用于核对勾选测试。",
        events=load_events(paths.events), intake_path=paths.intakes,
    )
    with pytest.raises(SourceVerificationRequired):
        confirm_novice_draft(
            intake_id,
            reviewed_event("需要核对的政策通知", "https://example.com/check"),
            source_checked=False,
            workspace_id=workspace["workspace_id"], paths=paths, db_path=db,
        )
    assert load_events(paths.events).empty


def test_high_duplicate_blocks_novice_confirmation(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "重复测试"}, db, root)
    paths = workspace_paths(workspace["workspace_id"], root)
    duplicate = reviewed_event("完全重复政策通知", "https://example.com/duplicate")
    create_event(duplicate, paths.events)
    intake_id, _ = analyze_and_save_draft(
        source_url=duplicate["source_url"], title=duplicate["title"], published_date="2026-07-20",
        source_name=duplicate["source_name"], text=duplicate["summary"],
        events=load_events(paths.events), intake_path=paths.intakes,
    )
    with pytest.raises(HighDuplicateError):
        confirm_novice_draft(
            intake_id, duplicate, source_checked=True,
            workspace_id=workspace["workspace_id"], paths=paths, db_path=db,
        )
    assert len(load_events(paths.events)) == 1
