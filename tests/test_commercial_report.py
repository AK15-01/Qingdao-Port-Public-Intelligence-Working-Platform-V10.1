from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from docx import Document
import pandas as pd

from commercial_report import (
    ReportOptions,
    build_executive_summary,
    filter_report_events,
    generate_commercial_report,
)
from data_store import create_event, load_events
from workspace_store import create_workspace, load_reports, workspace_paths


def event(title: str, category: str, status: str = "新增", url: str = "https://example.com/item") -> dict[str, str]:
    return {
        "event_date": "2026-07-20",
        "category": category,
        "title": title,
        "summary": "公开来源显示这是一条用于商业报告测试的事实摘要。",
        "impact": "可能影响相关业务计划，需要继续核对公开信息。",
        "affected_area": "测试区域",
        "affected_period": "测试时段",
        "source_name": "测试公开来源",
        "source_type": "政府/监管机构",
        "source_url": url,
        "status": status,
        "report_included": "是",
    }


def _sheet_names(path: Path) -> list[str]:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("xl/workbook.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [sheet.attrib["name"] for sheet in root.findall("x:sheets/x:sheet", namespace)]


def test_client_focus_categories_filter_report_content():
    events = pd.DataFrame([
        event("政策事项", "政策监管", url="https://example.com/policy"),
        event("企业事项", "企业动态", url="https://example.com/company"),
    ])
    filtered = filter_report_events(
        events,
        "2026-07-01",
        "2026-07-31",
        {"focus_categories": ["政策监管"]},
        ReportOptions(focus_categories_only=True),
    )
    assert filtered["title"].tolist() == ["政策事项"]


def test_deterministic_summary_is_not_just_an_event_list():
    events = pd.DataFrame([event("测试政策事项", "政策监管")])
    filtered = filter_report_events(events, "2026-07-01", "2026-07-31")
    summary = build_executive_summary(filtered)
    assert "本期共收录 1 条" in summary
    assert "数据质量状态" in summary


def test_docx_html_and_xlsx_bundle_can_be_reopened(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "测试报告空间"}, db, root)
    paths = workspace_paths(workspace["workspace_id"], root)
    create_event(event("测试政策事项", "政策监管"), paths.events)
    artifacts = generate_commercial_report(
        load_events(paths.events),
        workspace,
        paths,
        client=None,
        start_date="2026-07-01",
        end_date="2026-07-31",
        report_title="测试商业报告",
        analyst="测试团队",
        db_path=db,
    )
    document = Document(artifacts.docx_path)
    assert any("测试商业报告" in paragraph.text for paragraph in document.paragraphs)
    assert "执行摘要" in artifacts.html_path.read_text(encoding="utf-8")
    assert _sheet_names(artifacts.xlsx_path) == [
        "本期事件", "当前风险", "商机清单", "来源清单", "数据质量问题"
    ]


def test_regenerating_same_report_creates_v2_files(tmp_path: Path):
    db = tmp_path / "meta.db"
    root = tmp_path / "data"
    workspace = create_workspace({"workspace_name": "版本空间"}, db, root)
    paths = workspace_paths(workspace["workspace_id"], root)
    create_event(event("版本测试事项", "政策监管"), paths.events)
    kwargs = dict(
        events=load_events(paths.events), workspace=workspace, paths=paths,
        client=None, start_date="2026-07-01", end_date="2026-07-31",
        report_title="版本测试报告", analyst="测试团队", db_path=db,
    )
    first = generate_commercial_report(**kwargs)
    second = generate_commercial_report(**kwargs)
    assert first.report_id == second.report_id
    assert (first.version, second.version) == (1, 2)
    assert first.docx_path != second.docx_path
    assert len(load_reports(workspace["workspace_id"], db)) == 2
