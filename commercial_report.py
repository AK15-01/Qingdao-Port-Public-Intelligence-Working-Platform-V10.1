from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from html import escape
import json
from pathlib import Path
import re
from typing import Mapping, Optional, Sequence, Union
from urllib.parse import urlparse
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET

import pandas as pd

from data_store import STANDARD_FIELDS, normalize_events
from data_validator import ValidationReport, validate_events
from lifecycle import ACTIVE_STATUSES
from risk_engine import enrich_dataframe
from workspace_store import (
    WorkspacePaths,
    next_report_identity,
    record_report,
)


DEFAULT_DISCLAIMER = (
    "本报告基于公开来源、规则分析与人工核验记录（如有）整理，仅用于信息跟踪和业务讨论。"
    "规则分数不是任何港口、政府或监管机构发布的官方风险等级，也不构成准确预测、"
    "法律意见、投资意见或操作指令。使用者应回到原始公开来源核对并结合自身业务判断。"
)


@dataclass(frozen=True)
class ReportOptions:
    focus_categories_only: bool = False
    medium_high_only: bool = False
    include_risks: bool = True
    include_opportunities: bool = True
    show_score_details: bool = False
    include_source_appendix: bool = True
    report_mode: str = "标准报告"
    allow_empty_template: bool = False
    require_business_value: bool = False


@dataclass(frozen=True)
class ReportArtifacts:
    report_id: str
    version: int
    docx_path: Path
    html_path: Path
    xlsx_path: Path
    event_ids: tuple[str, ...]
    executive_summary: str


def _as_date(value: Union[str, date]) -> date:
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="raise")
    return parsed.date()


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _confirmed(value: object) -> bool:
    return _text(value).casefold() in {"1", "true", "yes", "是", "已确认"}


def _safe_filename(value: str, fallback: str = "report") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", _text(value)).strip("._")
    return cleaned[:60] or fallback


def _client_categories(client: Optional[Mapping[str, object]]) -> list[str]:
    if not client:
        return []
    value = client.get("focus_categories", [])
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    try:
        parsed = json.loads(_text(value) or "[]")
    except json.JSONDecodeError:
        parsed = []
    return [_text(item) for item in parsed if _text(item)] if isinstance(parsed, list) else []


def filter_report_events(
    events: pd.DataFrame,
    start_date: Union[str, date],
    end_date: Union[str, date],
    client: Optional[Mapping[str, object]] = None,
    options: ReportOptions = ReportOptions(),
) -> pd.DataFrame:
    start = _as_date(start_date)
    end = _as_date(end_date)
    if start > end:
        raise ValueError("报告开始日期不能晚于结束日期。")
    extra_columns = [
        column for column in (
            "extraction_confidence", "human_verified", "commercial_reuse_status",
            "quality_status", "extraction_quality", "report_quality_eligible",
            "business_value", "value_reason", "evidence_verified", "document_format",
            "source_file_url", "evidence_quotes", "evidence_failure_reasons",
        ) if column in events.columns
    ]
    extras = events.set_index("event_id")[extra_columns].to_dict("index") if extra_columns and "event_id" in events else {}
    data = enrich_dataframe(normalize_events(events, assign_ids=False))
    for column in extra_columns:
        data[column] = data["event_id"].map(lambda value: extras.get(value, {}).get(column, ""))
    if data.empty:
        return data
    dates = pd.to_datetime(data["event_date"], errors="coerce")
    mask = dates.notna() & (dates.dt.date >= start) & (dates.dt.date <= end)
    if "report_included" in data:
        mask &= data["report_included"].fillna("").ne("否")
    categories = _client_categories(client)
    if options.focus_categories_only and categories:
        mask &= data["category"].isin(categories)
    if options.medium_high_only:
        risk_mask = data["risk_level"].isin(["高", "中"])
        if options.include_opportunities:
            risk_mask |= data["opportunity_level"].isin(["高", "中"])
        mask &= risk_mask
    return data.loc[mask].sort_values(
        ["current_priority_score", "opportunity_score", "event_date"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def _core_events(data: pd.DataFrame) -> pd.DataFrame:
    core = data.copy()
    if {"extraction_confidence", "human_verified"}.issubset(core.columns):
        confidence = pd.to_numeric(core["extraction_confidence"], errors="coerce").fillna(0)
        low_unverified = confidence.lt(0.6) & ~core["human_verified"].map(_confirmed)
        core = core[~low_unverified].copy()
    if "evidence_verified" in core.columns:
        core = core[core["evidence_verified"].map(_confirmed)].copy()
    if "business_value" in core.columns:
        assessed = core["business_value"].isin(["高", "中", "低", "无关"])
        if assessed.any():
            core = core[~assessed | core["business_value"].isin(["高", "中"])].copy()
    return core


def report_groups(data: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if data.empty:
        return {name: data.copy() for name in ("risks", "opportunities", "resolved", "watchlist")}
    core = _core_events(data).head(5).copy()
    active = core["status"].isin(ACTIVE_STATUSES)
    risks = core[active & core["risk_level"].isin(["高", "中"])].copy()
    resolved = core[core["status"].isin(["解除", "已结束"])].copy()
    assigned = set(risks.get("event_id", [])) | set(resolved.get("event_id", []))
    opportunities = core[
        active
        & core["opportunity_level"].isin(["高", "中"])
        & ~core["event_id"].isin(assigned)
    ].copy()
    watchlist = core[
        active
        & ~core["event_id"].isin(assigned | set(opportunities.get("event_id", [])))
    ].copy()
    return {
        "risks": risks,
        "opportunities": opportunities,
        "resolved": resolved,
        "watchlist": watchlist,
    }


def build_executive_summary(
    data: pd.DataFrame,
    quality: Optional[ValidationReport] = None,
) -> str:
    quality = quality or validate_events(
        data[STANDARD_FIELDS] if not data.empty else normalize_events(data, assign_ids=False)
    )
    groups = report_groups(data)
    core = _core_events(data).head(5)
    if data.empty:
        attention = "当前没有满足门禁的核心事项"
        resolved = "无可核验的已解除事项"
        urgent = "当天动作：补充或复核公开来源"
        trends = "长期趋势：证据不足"
        gaps = "证据不足：所选周期没有满足正文、引用和报告模式门禁的资料"
    elif core.empty:
        attention = "当前没有具备逐字证据且达到中高业务价值的事项"
        resolved = "已解除事项未满足核心引用门禁"
        urgent = "当天动作：优先核对低置信度和引用未定位记录"
        trends = "长期趋势：仅保留在完整数据附件，不进入主体判断"
        gaps = f"证据不足：{len(data)} 条记录均未同时满足核心门禁"
    else:
        risks = groups["risks"].head(3)
        attention = (
            "当前重点：" + "、".join(_text(value) for value in risks["title"])
            if not risks.empty else "当前没有足够公开证据支持仍有效的中高风险"
        )
        resolved_frame = groups["resolved"].head(3)
        resolved = (
            "已解除：" + "、".join(_text(value) for value in resolved_frame["title"])
            if not resolved_frame.empty else "已解除：本期无可核验事项"
        )
        urgent_frame = risks[risks["risk_level"].isin(["高", "中"])].head(3)
        urgent = (
            "当天动作：回到原文并结合船期/作业通知核对"
            + "、".join(_text(value) for value in urgent_frame["title"])
            if not urgent_frame.empty else "当天动作：持续检查来源更新，无需用历史活动稿填充"
        )
        trend_frame = core[
            ~core["event_id"].isin(
                set(groups["risks"].get("event_id", []))
                | set(groups["resolved"].get("event_id", []))
                | set(groups["opportunities"].get("event_id", []))
            )
        ]
        trends = (
            "长期趋势：" + "、".join(_text(value) for value in trend_frame.head(2)["title"])
            if not trend_frame.empty else "长期趋势：本期无单列事项"
        )
        gaps_count = max(0, len(data) - len(core))
        gaps = f"证据不足：另有 {gaps_count} 条低价值、低置信度或引用未定位记录未进入核心正文"
    if quality.error_count:
        quality_text = f"存在 {quality.error_count} 项必须修复的数据错误"
    elif quality.warning_count:
        quality_text = f"整体可用，但有 {quality.warning_count} 项警告待复核"
    else:
        quality_text = f"完整率 {quality.completeness_rate:.1f}%，未发现错误或警告"
    verified_count = int(data.get("human_verified", pd.Series(dtype=object)).map(_confirmed).sum())
    unverified_count = max(0, len(data) - verified_count)
    return (
        f"本期共收录 {len(data)} 条，人工确认 {verified_count} 条、未确认 {unverified_count} 条。"
        f"{attention}。{resolved}。{urgent}。{trends}。{gaps}。"
        f"政策或采购机会 {len(groups['opportunities'])} 条；数据质量状态：{quality_text}。"
        "影响与建议均为分析，不是官方事实或预测；使用前必须回到原始公开来源核对。"
    )


def _business_link(category: str) -> str:
    return {
        "航行警告": "船舶计划、航线安排、靠离泊前核对",
        "海上气象": "船期、装卸窗口、堆场和陆运衔接",
        "港口作业": "码头作业、集疏运、订舱和客户沟通",
        "航线运价": "舱位、报价、合同和成本测算",
        "政策监管": "申报、通关、合规和单证流程",
        "招标采购": "供应商准入、投标准备和合作机会",
        "企业动态": "客户关系、供应链合作和长期观察",
    }.get(_text(category), "相关业务流程需结合原文人工判断")


def _urgency(row: Mapping[str, object]) -> str:
    if _text(row.get("status")) in {"解除", "已结束"}:
        return "已解除/结束，保留历史跟踪"
    return {"高": "高：建议当天核对", "中": "中：建议本期核对"}.get(_text(row.get("risk_level")), "低：持续观察")


def _evidence_excerpt(row: Mapping[str, object], limit: int = 220) -> str:
    """Return the stored source quote, never substitute an analytical summary."""
    bound = _text(row.get("evidence_quotes"))
    if bound:
        return bound[:limit]
    failure = _text(row.get("evidence_failure_reasons"))
    if failure:
        return f"需回原文核验：{failure[:max(20, limit - 8)]}"
    note = _text(row.get("analyst_note"))
    match = re.search(
        r"证据片段[：:]\s*(.+?)(?:\s+证据片段未通过|\s+重复候选[：:]|\s+人工QA复核[：:]|$)",
        note,
    )
    value = _text(match.group(1) if match else "")
    if not value:
        return "未保存可定位证据片段，需回到原文核验。"
    return value[:limit]


def _source_rows(data: pd.DataFrame) -> list[list[object]]:
    rows: list[list[object]] = []
    if data.empty:
        return rows
    columns = ["event_id", "event_date", "source_name", "source_type", "source_url", "title"]
    for _, row in data.drop_duplicates(subset=["event_id", "source_url"])[columns].iterrows():
        rows.append([_text(row[column]) for column in columns])
    return rows


def _quality_rows(quality: ValidationReport) -> list[list[object]]:
    issues = quality.issues_dataframe()
    if issues.empty:
        return [["通过", "", "", "", "未发现数据质量问题"]]
    return issues.astype(str).values.tolist()


def _event_rows(data: pd.DataFrame) -> list[list[object]]:
    columns = [
        "event_id", "event_date", "category", "title", "summary", "impact",
        "affected_area", "affected_period", "source_name", "source_type",
        "source_url", "status", "related_event_id", "risk_level",
        "opportunity_level", "current_priority_score", "opportunity_score",
        "quality_status", "extraction_confidence", "human_verified",
        "business_value", "value_reason", "evidence_verified", "document_format",
    ]
    if data.empty:
        return []
    return [
        [_text(row.get(column, "")) for column in columns] + [_evidence_excerpt(row)]
        for _, row in data.iterrows()
    ]


def _customer_event_rows(data: pd.DataFrame) -> list[list[object]]:
    """Customer attachments omit internal IDs, rule scores and debug fields."""

    columns = [
        "event_date", "category", "title", "summary", "impact",
        "affected_area", "affected_period", "source_name", "source_type",
        "source_url", "status", "risk_level", "opportunity_level",
        "quality_status", "extraction_confidence", "human_verified",
        "business_value", "value_reason", "evidence_verified", "document_format",
    ]
    if data.empty:
        return []
    return [
        [_text(row.get(column, "")) for column in columns] + [_evidence_excerpt(row)]
        for _, row in data.iterrows()
    ]


def _customer_source_rows(data: pd.DataFrame) -> list[list[object]]:
    if data.empty:
        return []
    columns = ["event_date", "source_name", "source_type", "source_url", "title"]
    return [
        [_text(row.get(column, "")) for column in columns]
        for _, row in data.drop_duplicates(subset=["source_url", "title"])[columns].iterrows()
    ]


def _customer_quality_rows(quality: ValidationReport) -> list[list[object]]:
    issues = quality.issues_dataframe()
    if issues.empty:
        return [["通过", "", "未发现数据质量问题"]]
    return [
        [_text(row.get("级别")), _text(row.get("字段")), _text(row.get("问题"))]
        for _, row in issues.iterrows()
    ]


def _build_html(
    title: str,
    client_name: str,
    company_name: str,
    analyst: str,
    start: date,
    end: date,
    data: pd.DataFrame,
    quality: ValidationReport,
    summary: str,
    options: ReportOptions,
    disclaimer: str,
    report_id: str,
    version: int,
) -> str:
    groups = report_groups(data)
    core = _core_events(data).head(5)
    categorized_ids = set(groups["risks"].get("event_id", [])) | set(
        groups["opportunities"].get("event_id", [])
    ) | set(groups["resolved"].get("event_id", []))
    other_core = core[~core["event_id"].isin(categorized_ids)].copy() if not core.empty else core
    source_numbers = {str(row[0]): index for index, row in enumerate(_source_rows(data), 1)}

    def cards(frame: pd.DataFrame, label: str) -> str:
        if frame.empty:
            return '<p class="empty">本期无相关事项。</p>'
        parts: list[str] = []
        for _, row in frame.iterrows():
            url = _text(row.get("source_url"))
            source = escape(_text(row.get("source_name")) or "来源待补充")
            source_html = f'<a href="{escape(url, quote=True)}">{source}</a>' if url else source
            score_line = ""
            if options.show_score_details:
                score_line = (
                    f'<p class="score">当前优先分 {_text(row.get("current_priority_score"))}｜'
                    f'商机分 {_text(row.get("opportunity_score"))}｜{escape(_text(row.get("score_explanation")))}</p>'
                )
            permission = _text(row.get("commercial_reuse_status")) or "未明确"
            permission_line = ""
            if permission not in {"允许", "已确认允许"}:
                permission_note = (
                    "仅限内部研究，需人工复核"
                    if options.report_mode == "内部研究版"
                    else "使用状态未明确；仅提供必要事实摘要和短引用，不附第三方全文"
                )
                permission_line = (
                    f'<p class="score"><strong>来源使用提示：</strong>'
                    f'{escape(permission)}（{escape(permission_note)}）</p>'
                )
            parts.append(
                '<article class="card">'
                f'<div class="eyebrow">{escape(label)}｜{escape(_text(row.get("event_date")))}｜{escape(_text(row.get("status")))}</div>'
                f'<h3>{escape(_text(row.get("title")))}</h3>'
                f'<p><strong>发生了什么：</strong>{escape(_text(row.get("summary")))}</p>'
                f'<p><strong>影响对象：</strong>{escape(_text(row.get("affected_area")) or "需结合业务范围核对")}</p>'
                f'<p><strong>可能影响的业务环节：</strong>{escape(_business_link(_text(row.get("category"))))}</p>'
                f'<p><strong>紧急程度：</strong>{escape(_urgency(row))}</p>'
                f'<p><strong>[分析] 潜在影响：</strong>{escape(_text(row.get("impact")) or "证据不足，待人工评估")}</p>'
                f'<p><strong>[分析] 建议核对动作：</strong>{escape(_text(row.get("action")) or "回到原始来源核对并持续跟踪状态")}</p>'
                f'<p><strong>证据片段：</strong>{escape(_evidence_excerpt(row))}</p>'
                f'<p><strong>数据质量：</strong>{escape(_text(row.get("quality_status")) or _text(row.get("extraction_quality")) or "已通过基础门禁")}｜'
                f'<strong>AI置信度：</strong>{escape(_text(row.get("extraction_confidence")) or "未提供")}｜'
                f'<strong>人工确认：</strong>{"是" if _confirmed(row.get("human_verified")) else "否"}</p>'
                f'<p><strong>业务价值：</strong>{escape(_text(row.get("business_value")) or "未评估")}｜'
                f'<strong>价值理由：</strong>{escape(_text(row.get("value_reason")) or "待人工判断")}｜'
                f'<strong>证据可追溯：</strong>{"是" if _confirmed(row.get("evidence_verified")) else "待核验"}</p>'
                f'<p class="source">[来源{source_numbers.get(_text(row.get("event_id")), "-")}] {source_html}</p>{permission_line}{score_line}</article>'
            )
        return "".join(parts)

    source_appendix = ""
    if options.include_source_appendix:
        items = "".join(
            f'<li>{escape(_text(row[1]))}｜{escape(_text(row[5]))}｜'
            f'<a href="{escape(_text(row[4]), quote=True)}">{escape(_text(row[2]))}</a></li>'
            for row in _source_rows(data)
        ) or "<li>本期无来源记录。</li>"
        source_appendix = f"<h2>完整来源附录</h2><ol>{items}</ol>"
    action_items = "".join(
        f"<li><strong>{escape(_text(row.get('title')))}</strong>："
        f"{escape(_text(row.get('action')) or '回到原始公开来源核对最新状态。')}</li>"
        for _, row in core.iterrows()
    ) or "<li>当前无满足证据门禁的核心行动项。</li>"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>
body{{font-family:"Microsoft YaHei",Arial,sans-serif;color:#17212b;background:#f4f7f9;margin:0;line-height:1.7}}
.page{{max-width:920px;margin:28px auto;background:white;padding:52px 64px;box-shadow:0 4px 22px #cbd5df}}
.kicker,.eyebrow{{color:#0d7490;font-size:13px;font-weight:700;letter-spacing:.04em}}
h1{{font-size:34px;line-height:1.25;margin:12px 0;color:#0b2545}} h2{{margin-top:38px;color:#174b66;border-bottom:1px solid #d9e5ea;padding-bottom:8px}}
h3{{margin:6px 0 10px;color:#163b52}} .meta{{display:grid;grid-template-columns:1fr 1fr;gap:8px 28px;background:#eef4f7;padding:18px 22px;margin:28px 0}}
.summary{{font-size:17px;background:#f8fafb;border-left:4px solid #168aad;padding:18px 22px}}
.card{{border:1px solid #dce7ed;border-radius:8px;padding:18px 22px;margin:14px 0;page-break-inside:avoid}}
.source,.score,.empty{{font-size:13px;color:#5b6975}} a{{color:#096b8a}} .footer{{margin-top:46px;color:#6b7780;font-size:12px}}
@media print{{body{{background:white}}.page{{box-shadow:none;margin:0;max-width:none;padding:16mm}}}}
</style></head><body><main class="page">
<div class="kicker">{escape(options.report_mode)}｜公开信息情报报告</div><h1>{escape(title)}</h1>
<div class="meta"><div><strong>客户：</strong>{escape(client_name or '通用报告')}</div><div><strong>统计周期：</strong>{start.isoformat()} 至 {end.isoformat()}</div><div><strong>编制：</strong>{escape(analyst or company_name or '本地分析团队')}</div><div><strong>版本：</strong>{escape(report_id)} / v{version}</div></div>
<h2>执行摘要</h2><p class="summary">{escape(summary)}</p>
<h2>核心风险</h2>{cards(groups['risks'], '风险') if options.include_risks else '<p class="empty">本报告设置为不包含风险事项。</p>'}
<h2>已解除事项</h2>{cards(groups['resolved'], '已解除')}
<h2>政策与采购机会</h2>{cards(groups['opportunities'], '机会') if options.include_opportunities else '<p class="empty">本报告设置为不包含政策和采购机会。</p>'}
<h2>长期趋势与其他核心事项</h2>{cards(other_core, '趋势/背景')}
<h2>行动清单</h2><ol>{action_items}</ol>
<h2>数据质量说明</h2><p>总记录 {quality.total_records} 条，完整率 {quality.completeness_rate:.1f}%，错误 {quality.error_count} 项，警告 {quality.warning_count} 项，建议 {quality.suggestion_count} 项。</p>
{source_appendix}
<h2>方法与免责声明</h2><p>{escape(disclaimer or DEFAULT_DISCLAIMER)}</p>
<p class="footer">生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}｜所有事实应以原始公开来源为准。</p>
</main></body></html>"""


def _set_cell_shading(cell, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    shading = tc_pr.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        tc_pr.append(shading)
    shading.set(qn("w:fill"), fill)


def _set_table_geometry(table, widths_dxa: Sequence[int]) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    table.autofit = False
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:type"), "dxa")
    tbl_w.set(qn("w:w"), str(sum(widths_dxa)))
    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:type"), "dxa")
    tbl_ind.set(qn("w:w"), "120")
    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths_dxa:
        grid_column = OxmlElement("w:gridCol")
        grid_column.set(qn("w:w"), str(width))
        grid.append(grid_column)
    for row in table.rows:
        for cell, width in zip(row.cells, widths_dxa):
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_w = tc_pr.find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.append(tc_w)
            tc_w.set(qn("w:type"), "dxa")
            tc_w.set(qn("w:w"), str(width))
            margins = tc_pr.find(qn("w:tcMar"))
            if margins is None:
                margins = OxmlElement("w:tcMar")
                tc_pr.append(margins)
            for name, value in (("top", 80), ("bottom", 80), ("start", 120), ("end", 120)):
                element = margins.find(qn(f"w:{name}"))
                if element is None:
                    element = OxmlElement(f"w:{name}")
                    margins.append(element)
                element.set(qn("w:w"), str(value))
                element.set(qn("w:type"), "dxa")


def _add_page_field(paragraph) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instruction, separate, end])


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE

    relationship = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "096B8A")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.extend([color, underline])
    text_element = OxmlElement("w:t")
    text_element.text = text
    run.extend([properties, text_element])
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _build_docx(
    output: Path,
    title: str,
    client_name: str,
    company_name: str,
    analyst: str,
    start: date,
    end: date,
    data: pd.DataFrame,
    quality: ValidationReport,
    summary: str,
    options: ReportOptions,
    disclaimer: str,
    report_id: str,
    version: int,
) -> None:
    from docx import Document
    from docx.enum.section import WD_SECTION_START
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    navy = RGBColor(0x0B, 0x25, 0x45)
    blue = RGBColor(0x2E, 0x74, 0xB5)
    muted = RGBColor(0x5B, 0x69, 0x75)
    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = section.right_margin = section.bottom_margin = section.left_margin = Inches(1)
    section.header_distance = section.footer_distance = Inches(0.492)

    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(11)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.10
    for name, size, color, before, after in (
        ("Heading 1", 16, blue, 16, 8),
        ("Heading 2", 13, blue, 12, 6),
        ("Heading 3", 12, RGBColor(0x1F, 0x4D, 0x78), 8, 4),
    ):
        style = document.styles[name]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.size = Pt(size)
        style.font.color.rgb = color
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
    for list_style_name in ("List Number", "List Bullet"):
        list_style = document.styles[list_style_name]
        list_style.font.name = "Calibri"
        list_style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        list_style.font.size = Pt(11)
        list_style.paragraph_format.left_indent = Inches(0.5)
        list_style.paragraph_format.first_line_indent = Inches(-0.25)
        list_style.paragraph_format.space_after = Pt(8)
        list_style.paragraph_format.line_spacing = 1.167

    header = section.header.paragraphs[0]
    header.text = f"{company_name or 'PortScope'}｜{options.report_mode}"
    header.alignment = WD_ALIGN_PARAGRAPH.LEFT
    header.runs[0].font.size = Pt(9)
    header.runs[0].font.color.rgb = muted
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer_run = footer.add_run(f"{report_id} / v{version}   第 ")
    footer_run.font.size = Pt(9)
    footer_run.font.color.rgb = muted
    _add_page_field(footer)
    footer.add_run(" 页")

    kicker = document.add_paragraph()
    kicker.paragraph_format.space_after = Pt(0)
    run = kicker.add_run(f"{options.report_mode}｜公开信息情报报告")
    run.bold = True
    run.font.size = Pt(11)
    run.font.color.rgb = blue
    title_paragraph = document.add_paragraph()
    title_paragraph.paragraph_format.space_after = Pt(8)
    run = title_paragraph.add_run(title)
    run.bold = True
    run.font.size = Pt(31)
    run.font.color.rgb = navy
    subtitle = document.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(22)
    verified_total = int(data.get("human_verified", pd.Series(dtype=object)).map(_confirmed).sum())
    run = subtitle.add_run(
        "基于公开来源、规则分析与已记录人工确认的阶段性整理"
        if verified_total
        else "基于公开来源与规则/AI分析的阶段性整理（本期尚未人工确认）"
    )
    run.font.size = Pt(13.5)
    run.font.color.rgb = muted

    meta = document.add_table(rows=4, cols=2)
    meta.style = "Table Grid"
    values = [
        ("客户", client_name or "通用报告"),
        ("统计周期", f"{start.isoformat()} 至 {end.isoformat()}"),
        ("编制", analyst or company_name or "本地分析团队"),
        ("报告版本", f"{report_id} / v{version}"),
    ]
    for row, (label, value) in zip(meta.rows, values):
        row.cells[0].text = label
        row.cells[1].text = value
        row.cells[0].paragraphs[0].runs[0].bold = True
        _set_cell_shading(row.cells[0], "E8EEF5")
        for cell in row.cells:
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    _set_table_geometry(meta, [2700, 6660])
    document.add_paragraph()
    document.add_page_break()

    document.add_heading("执行摘要", level=1)
    paragraph = document.add_paragraph(summary)
    paragraph.paragraph_format.space_after = Pt(10)
    groups = report_groups(data)
    source_numbers = {str(row[0]): index for index, row in enumerate(_source_rows(data), 1)}
    metrics = document.add_table(rows=2, cols=4)
    metrics.style = "Table Grid"
    labels = ["本期事件", "当前中高风险", "政策/采购机会", "已解除事项"]
    numbers = [len(data), len(groups["risks"]), len(groups["opportunities"]), len(groups["resolved"])]
    for index, label in enumerate(labels):
        metrics.cell(0, index).text = label
        metrics.cell(1, index).text = str(numbers[index])
        _set_cell_shading(metrics.cell(0, index), "E8EEF5")
        metrics.cell(0, index).paragraphs[0].runs[0].bold = True
        metrics.cell(1, index).paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        metrics.cell(1, index).paragraphs[0].runs[0].font.size = Pt(18)
        metrics.cell(1, index).paragraphs[0].runs[0].font.color.rgb = navy
    _set_table_geometry(metrics, [2340, 2340, 2340, 2340])

    def add_events_section(heading: str, frame: pd.DataFrame, include: bool = True) -> None:
        document.add_heading(heading, level=1)
        if not include:
            document.add_paragraph("本报告设置为不包含此类事项。")
            return
        if frame.empty:
            document.add_paragraph("本期无相关事项。")
            return
        for _, row in frame.iterrows():
            document.add_heading(_text(row.get("title")), level=2)
            meta_line = document.add_paragraph(
                f"{_text(row.get('event_date'))}｜{_text(row.get('category'))}｜状态：{_text(row.get('status'))}"
            )
            meta_line.runs[0].font.size = Pt(9.5)
            meta_line.runs[0].font.color.rgb = muted
            document.add_paragraph(_text(row.get("summary")))
            p = document.add_paragraph()
            p.add_run("影响对象：").bold = True
            p.add_run(_text(row.get("affected_area")) or "需结合业务范围核对。")
            p = document.add_paragraph()
            p.add_run("可能影响的业务环节：").bold = True
            p.add_run(_business_link(_text(row.get("category"))))
            p = document.add_paragraph()
            p.add_run("紧急程度：").bold = True
            p.add_run(_urgency(row))
            p = document.add_paragraph()
            p.add_run("[分析] 潜在影响：").bold = True
            p.add_run(_text(row.get("impact")) or "待结合业务场景评估。")
            p = document.add_paragraph()
            p.add_run("[分析] 建议核对动作：").bold = True
            p.add_run(_text(row.get("action")) or "回到原始来源核对并持续跟踪状态。")
            p = document.add_paragraph()
            p.add_run("证据片段：").bold = True
            p.add_run(_evidence_excerpt(row))
            p = document.add_paragraph()
            p.add_run("数据质量：").bold = True
            p.add_run(_text(row.get("quality_status")) or _text(row.get("extraction_quality")) or "已通过基础门禁")
            p.add_run("｜AI置信度：").bold = True
            p.add_run(_text(row.get("extraction_confidence")) or "未提供")
            p.add_run("｜人工确认：").bold = True
            p.add_run("是" if _confirmed(row.get("human_verified")) else "否")
            p = document.add_paragraph()
            p.add_run("业务价值：").bold = True
            p.add_run(_text(row.get("business_value")) or "未评估")
            p.add_run("｜价值理由：").bold = True
            p.add_run(_text(row.get("value_reason")) or "待人工判断")
            p.add_run("｜证据可追溯：").bold = True
            p.add_run("是" if _confirmed(row.get("evidence_verified")) else "待核验")
            if options.show_score_details:
                score = document.add_paragraph(
                    f"评分明细：当前优先分 {_text(row.get('current_priority_score'))}；"
                    f"商机分 {_text(row.get('opportunity_score'))}；{_text(row.get('score_explanation'))}"
                )
                score.runs[0].font.size = Pt(9)
                score.runs[0].font.color.rgb = muted
            url = _text(row.get("source_url"))
            source = _text(row.get("source_name")) or "来源待补充"
            source_paragraph = document.add_paragraph(
                f"[来源{source_numbers.get(_text(row.get('event_id')), '-')}] "
            )
            if urlparse(url).scheme in {"http", "https"}:
                _add_hyperlink(source_paragraph, source, url)
            else:
                source_paragraph.add_run(source)
            permission = _text(row.get("commercial_reuse_status")) or "未明确"
            if permission not in {"允许", "已确认允许"}:
                permission_note = (
                    "仅限内部研究，需人工复核。"
                    if options.report_mode == "内部研究版"
                    else "使用状态未明确；仅提供必要事实摘要和短引用，不附第三方全文。"
                )
                warning = document.add_paragraph(f"来源使用提示：{permission}。{permission_note}")
                warning.runs[0].bold = True
                warning.runs[0].font.color.rgb = RGBColor(0xA6, 0x5A, 0x00)

    core_data = _core_events(data).head(5)
    categorized_ids = set(groups["risks"].get("event_id", [])) | set(
        groups["opportunities"].get("event_id", [])
    ) | set(groups["resolved"].get("event_id", []))
    other_core = (
        core_data[~core_data["event_id"].isin(categorized_ids)].copy()
        if not core_data.empty else core_data
    )
    add_events_section("当前有效风险", groups["risks"], options.include_risks)
    add_events_section("已解除事项", groups["resolved"])
    add_events_section("政策与业务机会", groups["opportunities"], options.include_opportunities)
    add_events_section("长期趋势与其他核心事项", other_core)

    document.add_heading("行动清单", level=1)
    if core_data.empty:
        document.add_paragraph("当前无满足证据门禁的核心行动项。")
    else:
        for _, row in core_data.iterrows():
            paragraph = document.add_paragraph(style="List Number")
            paragraph.add_run(_text(row.get("title")) + "：").bold = True
            paragraph.add_run(
                _text(row.get("action")) or "回到原始公开来源核对最新状态。"
            )

    document.add_heading("数据质量说明", level=1)
    document.add_paragraph(
        f"总记录 {quality.total_records} 条，完整率 {quality.completeness_rate:.1f}%，"
        f"错误 {quality.error_count} 项，警告 {quality.warning_count} 项，建议 {quality.suggestion_count} 项。"
    )
    if quality.issues:
        document.add_paragraph("详细问题保留在Excel数据附件中，DOCX主体只呈现业务结论。")

    if options.include_source_appendix:
        document.add_heading("完整来源附录", level=1)
        sources = _source_rows(data)
        if not sources:
            document.add_paragraph("本期无来源记录。")
        else:
            for source_index, (
                event_id, event_date, source_name, source_type, source_url, event_title
            ) in enumerate(sources, 1):
                paragraph = document.add_paragraph()
                paragraph.paragraph_format.space_after = Pt(2)
                number_run = paragraph.add_run(f"{source_index}. ")
                number_run.bold = True
                paragraph.add_run(f"{event_date}｜{event_title}｜{source_name}（{source_type}） ")
                if source_url:
                    _add_hyperlink(paragraph, "打开原文", source_url)
                if options.report_mode != "客户交付版":
                    small = paragraph.add_run(f"  [{event_id}]")
                    small.font.size = Pt(8)
                    small.font.color.rgb = muted
                for run in paragraph.runs:
                    if run.font.size is None:
                        run.font.size = Pt(9)

    document.add_heading("方法与免责声明", level=1)
    document.add_paragraph(
        "方法：系统只访问用户启用且合规状态允许的白名单公开来源，执行增量发现、安全提取、"
        "内容去重与版本保存；机器抽取仅生成待审核结果，风险与商机排序继续使用透明确定性规则。"
        "商业报告默认只使用人工确认且来源许可允许的当前版本。"
    )
    document.add_paragraph(disclaimer or DEFAULT_DISCLAIMER)
    generated = document.add_paragraph(
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}"
    )
    generated.runs[0].font.size = Pt(9)
    generated.runs[0].font.color.rgb = muted

    temporary = output.with_suffix(output.suffix + ".tmp")
    document.save(temporary)
    temporary.replace(output)


def _column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xml_text(value: object) -> str:
    text = _text(value)
    return "".join(character for character in text if ord(character) in {9, 10, 13} or ord(character) >= 32)


def _worksheet_xml(headers: Sequence[str], rows: Sequence[Sequence[object]], widths: Sequence[float]) -> bytes:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ET.register_namespace("", namespace)
    worksheet = ET.Element(f"{{{namespace}}}worksheet")
    views = ET.SubElement(worksheet, f"{{{namespace}}}sheetViews")
    view = ET.SubElement(views, f"{{{namespace}}}sheetView", workbookViewId="0", showGridLines="0")
    ET.SubElement(view, f"{{{namespace}}}pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen")
    columns = ET.SubElement(worksheet, f"{{{namespace}}}cols")
    for index, width in enumerate(widths, 1):
        ET.SubElement(columns, f"{{{namespace}}}col", min=str(index), max=str(index), width=str(width), customWidth="1")
    sheet_data = ET.SubElement(worksheet, f"{{{namespace}}}sheetData")
    all_rows = [list(headers)] + [list(row) for row in rows]
    for row_number, values in enumerate(all_rows, 1):
        row_element = ET.SubElement(sheet_data, f"{{{namespace}}}row", r=str(row_number))
        if row_number == 1:
            row_element.set("ht", "26")
            row_element.set("customHeight", "1")
        for column_number, value in enumerate(values, 1):
            reference = f"{_column_letter(column_number)}{row_number}"
            cell = ET.SubElement(
                row_element,
                f"{{{namespace}}}c",
                r=reference,
                t="inlineStr",
                s="1" if row_number == 1 else "2",
            )
            inline = ET.SubElement(cell, f"{{{namespace}}}is")
            text = ET.SubElement(inline, f"{{{namespace}}}t")
            value_text = _xml_text(value)
            if value_text.startswith(" ") or value_text.endswith(" "):
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = value_text
    if headers:
        last = _column_letter(len(headers))
        end_row = max(len(all_rows), 1)
        ET.SubElement(worksheet, f"{{{namespace}}}autoFilter", ref=f"A1:{last}{end_row}")
    margins = ET.SubElement(
        worksheet,
        f"{{{namespace}}}pageMargins",
        left="0.35", right="0.35", top="0.5", bottom="0.5", header="0.2", footer="0.2",
    )
    _ = margins
    return ET.tostring(worksheet, encoding="utf-8", xml_declaration=True)


def _build_xlsx(
    output: Path,
    data: pd.DataFrame,
    quality: ValidationReport,
    *,
    extended: bool = False,
    crawl_runs: Optional[pd.DataFrame] = None,
    report_mode: str = "标准报告",
) -> None:
    groups = report_groups(data)
    event_headers = [
        "事件ID", "发布日期", "类别", "标题", "事实摘要", "潜在影响", "受影响区域", "受影响时段",
        "来源名称", "来源类型", "原文链接", "状态", "关联事件", "风险等级", "商机等级", "当前优先分", "商机分",
        "数据质量", "AI置信度", "人工确认", "业务价值", "价值理由", "证据可追溯", "文档格式",
        "证据片段",
    ]
    source_headers = ["事件ID", "发布日期", "来源名称", "来源类型", "原文链接", "事件标题"]
    issue_headers = ["级别", "记录序号", "事件ID", "字段", "问题"]
    if report_mode == "客户交付版":
        customer_headers = [
            "发布日期", "类别", "标题", "事实摘要", "潜在影响", "受影响区域",
            "受影响时段", "来源名称", "来源类型", "原文链接", "状态",
            "风险等级", "商机等级", "数据质量", "AI置信度", "人工确认",
            "业务价值", "价值理由", "证据可追溯", "文档格式", "证据片段",
        ]
        customer_source_headers = [
            "发布日期", "来源名称", "来源类型", "原文链接", "事件标题"
        ]
        customer_issue_headers = ["级别", "字段", "问题"]
        sheets = [
            ("本期事件", customer_headers, _customer_event_rows(data)),
            ("当前风险", customer_headers, _customer_event_rows(groups["risks"])),
            ("商机清单", customer_headers, _customer_event_rows(groups["opportunities"])),
            ("来源清单", customer_source_headers, _customer_source_rows(data)),
            ("数据质量问题", customer_issue_headers, _customer_quality_rows(quality)),
        ]
    else:
        sheets = [
            ("本期事件", event_headers, _event_rows(data)),
            ("当前风险", event_headers, _event_rows(groups["risks"])),
            ("商机清单", event_headers, _event_rows(groups["opportunities"])),
            ("来源清单", source_headers, _source_rows(data)),
            ("数据质量问题", issue_headers, _quality_rows(quality)),
        ]
    if extended:
        run_headers = [
            "采集运行ID", "开始时间", "结束时间", "状态", "来源数", "发现数", "获取数",
            "跳过数", "失败数", "新增文档", "更新文档", "错误摘要",
        ]
        run_rows: list[list[object]] = []
        if crawl_runs is not None and not crawl_runs.empty:
            run_columns = [
                "crawl_run_id", "started_at", "finished_at", "status", "source_count",
                "discovered_count", "fetched_count", "skipped_count", "failed_count",
                "new_document_count", "updated_document_count", "error_summary",
            ]
            run_rows = [[_text(row.get(column, "")) for column in run_columns] for _, row in crawl_runs.iterrows()]
        sheets = [
            ("事件明细", event_headers, _event_rows(data)),
            ("当前风险", event_headers, _event_rows(groups["risks"])),
            ("已解除风险", event_headers, _event_rows(groups["resolved"])),
            ("商机清单", event_headers, _event_rows(groups["opportunities"])),
            ("来源清单", source_headers, _source_rows(data)),
            ("数据质量", issue_headers, _quality_rows(quality)),
            ("抓取运行记录", run_headers, run_rows),
        ]
    if report_mode == "内部研究版":
        sheets.insert(0, (
            "内部使用说明", ["使用限制", "说明"], [[
                "仅供内部研究",
                "附件不包含网页原文；仅保留必要事实摘要、分析标签和来源链接，不得作为原始公开数据再销售。许可未明确来源须醒目标记并人工复核。",
            ]],
        ))
    widths_by_count = {
        2: [24, 96],
        24: [20, 12, 12, 34, 48, 42, 16, 16, 20, 18, 38, 12, 20, 12, 12, 12, 10, 18, 12, 12, 12, 34, 12, 12],
        25: [20, 12, 12, 34, 48, 42, 16, 16, 20, 18, 38, 12, 20, 12, 12, 12, 10, 18, 12, 12, 12, 34, 12, 12, 52],
        21: [12, 12, 34, 48, 42, 16, 16, 20, 18, 38, 12, 12, 12, 18, 12, 12, 12, 34, 12, 12, 52],
        6: [20, 12, 20, 18, 42, 38],
        5: [10, 12, 20, 18, 52],
        3: [12, 20, 72],
        12: [22, 22, 22, 12, 10, 10, 10, 10, 10, 12, 12, 48],
    }
    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>']
    for index in range(1, len(sheets) + 1):
        content_types.append(f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    content_types.append('</Types>')
    workbook_sheets = "".join(
        f'<sheet name="{escape(name, quote=True)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _, _) in enumerate(sheets, 1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    styles_id = len(sheets) + 1
    relationships += f'<Relationship Id="rId{styles_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3"><font><sz val="10"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Microsoft YaHei"/></font><font><color rgb="FF17212B"/><sz val="10"/><name val="Microsoft YaHei"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF0B4F6C"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border/><border><bottom style="thin"><color rgb="FFD9E5EA"/></bottom></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    temporary = output.with_suffix(output.suffix + ".tmp")
    with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>''')
        archive.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets>{workbook_sheets}</sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}</Relationships>''')
        archive.writestr("xl/styles.xml", styles)
        for index, (_, headers, rows) in enumerate(sheets, 1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(headers, rows, widths_by_count[len(headers)]),
            )
        archive.writestr("docProps/core.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>公开信息报告数据附件</dc:title><dc:creator>PortScope</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>''')
        archive.writestr("docProps/app.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>PortScope</Application><TitlesOfParts><vt:vector xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes" size="{len(sheets)}" baseType="lpstr">{''.join(f'<vt:lpstr>{escape(name)}</vt:lpstr>' for name, _, _ in sheets)}</vt:vector></TitlesOfParts></Properties>''')
    temporary.replace(output)


def generate_commercial_report(
    events: pd.DataFrame,
    workspace: Mapping[str, object],
    paths: WorkspacePaths,
    *,
    client: Optional[Mapping[str, object]],
    start_date: Union[str, date],
    end_date: Union[str, date],
    report_title: str,
    analyst: str,
    executive_summary: str = "",
    options: ReportOptions = ReportOptions(),
    db_path: Optional[Union[str, Path]] = None,
) -> ReportArtifacts:
    start = _as_date(start_date)
    end = _as_date(end_date)
    data = filter_report_events(events, start, end, client, options)
    if options.report_mode in {"内部研究版", "客户交付版"} and data.empty and not options.allow_empty_template:
        raise ValueError("当前没有足够证据生成报告；如仅需版式，请明确选择生成空白模板。")
    raw_for_quality = data[STANDARD_FIELDS] if not data.empty else normalize_events(data, assign_ids=False)
    quality = validate_events(raw_for_quality)
    summary = _text(executive_summary) or build_executive_summary(data, quality)
    title = _text(report_title) or _text(workspace.get("default_report_title")) or "公开信息商业情报报告"
    client_id = _text(client.get("client_id")) if client else ""
    client_name = _text(client.get("client_name")) if client else "通用报告"
    company_name = _text(client.get("company_name")) if client else ""
    disclaimer = _text(client.get("disclaimer")) if client else DEFAULT_DISCLAIMER
    if data.empty and options.allow_empty_template:
        title = "【空白模板】" + title
        summary = "当前没有足够证据生成正式报告。本文件仅为空白模板，不包含事实性结论。"
        disclaimer = "【空白模板，不是正式报告】" + disclaimer
    if options.report_mode == "内部研究版":
        disclaimer = (
            "【仅供内部研究】本报告可包含商业再利用许可尚未完全明确的公开资料，仅提供必要事实摘要、分析和来源链接；"
            "不全文转载网页，不提供可作为原始数据再销售的附件。许可状态未明确的来源必须另行人工核对。" + disclaimer
        )
    report_id, version = next_report_identity(
        str(workspace["workspace_id"]), client_id, title, start.isoformat(), end.isoformat(), db_path
    )
    base_name = _safe_filename(f"{title}_{report_id}_v{version}")
    paths.reports.mkdir(parents=True, exist_ok=True)
    docx_path = paths.reports / f"{base_name}.docx"
    html_path = paths.reports / f"{base_name}.html"
    xlsx_path = paths.reports / f"{base_name}_数据附件.xlsx"
    for path in (docx_path, html_path, xlsx_path):
        if path.exists():
            raise FileExistsError(f"报告文件已存在，拒绝覆盖：{path.name}")

    html = _build_html(
        title, client_name, company_name, analyst, start, end, data, quality, summary,
        options, disclaimer, report_id, version,
    )
    html_temp = html_path.with_suffix(html_path.suffix + ".tmp")
    html_temp.write_text(html, encoding="utf-8")
    html_temp.replace(html_path)
    try:
        _build_docx(
            docx_path, title, client_name, company_name, analyst, start, end, data,
            quality, summary, options, disclaimer, report_id, version,
        )
        _build_xlsx(xlsx_path, data, quality)
    except Exception:
        for path in (docx_path, html_path, xlsx_path):
            if path.exists():
                path.unlink()
        raise
    event_ids = tuple(_text(value) for value in data.get("event_id", pd.Series(dtype=str)) if _text(value))
    record_report(
        {
            "report_id": report_id,
            "workspace_id": workspace["workspace_id"],
            "client_id": client_id,
            "report_title": title,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "event_ids": list(event_ids),
            "docx_file": docx_path.name,
            "html_file": html_path.name,
            "xlsx_file": xlsx_path.name,
            "version": version,
            "report_mode": options.report_mode,
            "analysis_model": "deterministic-rules",
            "status": "template" if data.empty else "active",
            "status_note": "空白模板，不是正式报告" if data.empty else "",
            "export_mode": "summary_only",
            "permission_risk_count": int(
                data.get("commercial_reuse_status", pd.Series(dtype=str))
                .astype(str)
                .isin(["未明确", "需取得许可", ""])
                .sum()
            ),
        },
        db_path,
    )
    return ReportArtifacts(
        report_id, version, docx_path, html_path, xlsx_path, event_ids, summary
    )
