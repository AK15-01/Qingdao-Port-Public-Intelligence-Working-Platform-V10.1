from pathlib import Path

import pytest

from data_store import load_events
from data_validator import validate_events
from intake_store import (
    INTAKE_FIELDS,
    AlreadyConfirmedError,
    IntakeValidationError,
    confirm_intake,
    create_intake,
    ensure_intake_file,
    load_intakes,
    mark_intake_ignored,
    update_intake,
)


def draft_values(title: str = "测试草稿") -> dict[str, str]:
    return {
        "source_url": "https://example.com/draft",
        "fetched_title": title,
        "fetched_date": "2026-07-20",
        "fetched_source_name": "测试公开来源",
        "fetched_text": "这是通过模拟网页得到的公开信息正文，仅用于测试草稿流程。",
        "fetch_status": "成功",
        "category_suggestion": "港口作业",
        "status_suggestion": "新增",
        "duplicate_suggestion": "未发现明显重复",
        "reviewer_status": "待审核",
    }


def event_values() -> dict[str, str]:
    return {
        "event_date": "2026-07-20",
        "category": "港口作业",
        "title": "审核后测试事件",
        "summary": "公开来源显示这是一条经过人工审核的测试事件。",
        "impact": "可能影响相关测试作业安排，需要继续人工复核。",
        "affected_area": "测试区域",
        "affected_period": "测试时段",
        "source_name": "测试公开来源",
        "source_type": "港口/企业官网",
        "source_url": "https://example.com/confirmed",
        "status": "新增",
        "related_event_id": "",
        "analyst_note": "测试记录，不含个人信息",
    }


def test_missing_intake_csv_is_created(tmp_path: Path):
    path = tmp_path / "data" / "intake.csv"
    ensure_intake_file(path)
    loaded = load_intakes(path)
    assert path.exists()
    assert loaded.empty
    assert loaded.columns.tolist() == INTAKE_FIELDS


def test_draft_save_update_ignore_and_reload(tmp_path: Path):
    path = tmp_path / "intake.csv"
    saved = create_intake(draft_values(), path)
    intake_id = saved.iloc[0]["intake_id"]
    update_intake(intake_id, {"reviewer_note": "已经人工查看"}, path)
    restarted = load_intakes(path)
    assert restarted.iloc[0]["reviewer_note"] == "已经人工查看"

    mark_intake_ignored(intake_id, "内容不在范围内", path)
    assert load_intakes(path).iloc[0]["reviewer_status"] == "已忽略"


def test_confirm_runs_validator_records_event_id_and_prevents_repeat(tmp_path: Path):
    intake_path = tmp_path / "intake.csv"
    events_path = tmp_path / "events.csv"
    saved = create_intake(draft_values(), intake_path)
    intake_id = saved.iloc[0]["intake_id"]
    called = {"value": False}

    def spy_validator(dataframe):
        called["value"] = True
        return validate_events(dataframe)

    event_id, report = confirm_intake(
        intake_id,
        event_values(),
        intake_path=intake_path,
        events_path=events_path,
        validator=spy_validator,
    )
    assert called["value"]
    assert report.can_save
    assert load_events(events_path).iloc[0]["event_id"] == event_id
    confirmed = load_intakes(intake_path).iloc[0]
    assert confirmed["reviewer_status"] == "已确认入库"
    assert confirmed["confirmed_event_id"] == event_id

    with pytest.raises(AlreadyConfirmedError):
        confirm_intake(
            intake_id,
            event_values(),
            intake_path=intake_path,
            events_path=events_path,
        )
    assert len(load_events(events_path)) == 1


def test_validation_error_blocks_formal_event_write(tmp_path: Path):
    intake_path = tmp_path / "intake.csv"
    events_path = tmp_path / "events.csv"
    intake_id = create_intake(draft_values(), intake_path).iloc[0]["intake_id"]
    invalid = event_values()
    invalid["source_url"] = "not-a-url"
    with pytest.raises(IntakeValidationError):
        confirm_intake(
            intake_id,
            invalid,
            intake_path=intake_path,
            events_path=events_path,
        )
    assert load_events(events_path).empty
    assert load_intakes(intake_path).iloc[0]["reviewer_status"] == "待审核"

