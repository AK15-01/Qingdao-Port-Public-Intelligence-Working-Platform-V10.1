from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sync_readme_status import (  # noqa: E402
    END_MARKER,
    START_MARKER,
    count_test_functions,
    count_tracked_python_lines,
    inject,
    render_status_block,
)


SAMPLE_AUDIT = {
    "generated_at": "2026-09-15T12:00:00+10:00",
    "code_version": "0.5.0-beta",
    "database_sha256": "abcdef0123456789" * 4,
    "documents": {"total": 45, "active": 9, "quarantined": 36, "ai_complete": 9},
    "events": {
        "total": 13,
        "active": 9,
        "quarantined": 4,
        "human_verified": 0,
        "internal_eligible": 0,
        "customer_eligible": 0,
    },
    "evidence": {"verified": 21, "unresolved": 0, "total": 21},
    "enabled_sources": [{"source_id": "SRC-1", "source_name": "示例来源"}],
    "latest_successful_crawl": {"finished_at": "2026-07-29T20:58:35+10:00", "new_document_count": 0},
    "reports": 2,
}


def _block() -> str:
    return render_status_block(SAMPLE_AUDIT, python_lines=38966, test_functions=258)


def test_readme_contains_status_markers():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count(START_MARKER) == 1
    assert readme.count(END_MARKER) == 1
    assert readme.index(START_MARKER) < readme.index(END_MARKER)


def test_zero_gate_counters_are_rendered_as_emphasised_zero():
    """0 必须醒目显示，不能被静悄悄地混在数字里。"""
    block = _block()
    assert "| 独立真人审核事件 | **0** |" in block
    assert "| 客户报告资格 | **0** |" in block
    assert "| 内部使用资格 | **0** |" in block


def test_non_zero_counters_are_not_emphasised():
    block = _block()
    assert "| 启用来源 | 1 |" in block
    assert "| 证据已验证 / 未解决 | 21 / 0 |" in block


def test_injection_replaces_only_the_marked_region_and_is_idempotent():
    original = f"前言\n\n{START_MARKER}\n旧内容\n{END_MARKER}\n\n## 后续章节\n"
    block = _block()
    once = inject(original, block)
    twice = inject(once, block)
    assert once == twice, "重复同步不得产生重复区块"
    assert once.startswith("前言")
    assert once.endswith("## 后续章节\n")
    assert "旧内容" not in once


def test_injection_refuses_a_readme_without_markers():
    try:
        inject("没有标记的 README", _block())
    except SystemExit as exc:
        assert START_MARKER in str(exc)
    else:  # pragma: no cover - 仅在回归时触发
        raise AssertionError("缺少标记时必须报错，而不是静默追加")


def test_missing_latest_crawl_is_shown_explicitly_not_omitted():
    """没有任何成功采集时必须显式写出来，不能静默少一行。"""
    audit = dict(SAMPLE_AUDIT, latest_successful_crawl=None)
    block = render_status_block(audit, python_lines=1, test_functions=1)
    assert "| 最近一次成功采集 | **尚未有成功采集** |" in block


def test_missing_database_is_a_graceful_skip(tmp_path, capsys, monkeypatch):
    """数据库不存在时退出码为 0，且不得改动 README。"""
    import scripts.sync_readme_status as module

    readme = tmp_path / "README.md"
    readme.write_text("\n".join([START_MARKER, "占位", END_MARKER, ""]), encoding="utf-8")
    before = readme.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["sync_readme_status.py", "--db", str(tmp_path / "absent.db"), "--readme", str(readme), "--apply"],
    )
    assert module.main() == 0
    assert "[跳过]" in capsys.readouterr().out
    assert readme.read_text(encoding="utf-8") == before


def test_malformed_database_is_a_graceful_skip(tmp_path, capsys, monkeypatch):
    """数据库存在但结构不完整时不抛栈，也不写入半截状态。"""
    import scripts.sync_readme_status as module

    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a sqlite database")
    readme = tmp_path / "README.md"
    readme.write_text("\n".join([START_MARKER, "占位", END_MARKER, ""]), encoding="utf-8")
    before = readme.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["sync_readme_status.py", "--db", str(broken), "--readme", str(readme), "--apply"],
    )
    assert module.main() == 0
    assert "[跳过]" in capsys.readouterr().out
    assert readme.read_text(encoding="utf-8") == before


def test_counters_read_the_real_project():
    assert count_tracked_python_lines(PROJECT_ROOT) > 10_000
    assert count_test_functions(PROJECT_ROOT) > 100
