from __future__ import annotations

import argparse
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from data_store import DEFAULT_EVENTS_PATH, load_events, normalize_events
from data_validator import is_valid_source_url, validate_events
from lifecycle import ACTIVE_STATUSES, current_risks, resolved_risks
from risk_engine import enrich_dataframe


BASE_DIR = Path(__file__).resolve().parent
SAMPLE_DATA_PATH = BASE_DIR / "data" / "sample_events.csv"
OUTPUT_DIR = BASE_DIR / "output"


def _as_date(value: Union[str, date]) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def filter_events_by_date(
    dataframe: pd.DataFrame,
    start_date: Union[str, date],
    end_date: Union[str, date],
) -> pd.DataFrame:
    start = _as_date(start_date)
    end = _as_date(end_date)
    if start > end:
        raise ValueError("统计开始日期不能晚于结束日期。")
    if dataframe.empty:
        return dataframe.copy()
    dates = pd.to_datetime(dataframe["event_date"], errors="coerce")
    mask = dates.notna() & (dates.dt.date >= start) & (dates.dt.date <= end)
    return dataframe.loc[mask].copy().reset_index(drop=True)


def prepare_report_data(
    dataframe: pd.DataFrame,
    start_date: Union[str, date],
    end_date: Union[str, date],
) -> pd.DataFrame:
    normalized = normalize_events(dataframe, assign_ids=False)
    filtered = filter_events_by_date(normalized, start_date, end_date)
    enriched = enrich_dataframe(filtered)
    if enriched.empty:
        return enriched
    enriched["event_date"] = pd.to_datetime(enriched["event_date"], errors="coerce")
    return enriched.sort_values(
        ["current_priority_score", "opportunity_score", "event_date"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def _markdown_text(value: object) -> str:
    return str(value or "").replace("\n", " ").replace("|", "｜").strip()


def _date_text(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return parsed.date().isoformat() if pd.notna(parsed) else "未知"


def _source_markdown(row: pd.Series) -> str:
    name = _markdown_text(row.get("source_name")) or "未填写来源"
    url = str(row.get("source_url", "")).strip()
    if is_valid_source_url(url):
        return f"[{name}]({url})"
    return f"{name}（链接缺失或异常）"


def _report_groups(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    active = current_risks(data, minimum_score=40)
    resolved = resolved_risks(data)
    if data.empty:
        opportunities = data.copy()
    else:
        opportunities = data[
            data["status"].isin(ACTIVE_STATUSES)
            & data["opportunity_level"].isin(["高", "中"])
        ].sort_values("opportunity_score", ascending=False)
    return active, resolved, opportunities


def build_weekly_report(
    dataframe: pd.DataFrame,
    title: str,
    start_date: Union[str, date],
    end_date: Union[str, date],
    analyst: str = "PortScope",
    demo_data: bool = False,
) -> str:
    start = _as_date(start_date)
    end = _as_date(end_date)
    data = prepare_report_data(dataframe, start, end)
    all_event_ids = normalize_events(dataframe, assign_ids=False)["event_id"].tolist()
    quality = validate_events(data, today=date.today(), known_event_ids=all_event_ids)
    active, resolved, opportunities = _report_groups(data)

    lines = [
        f"# {_markdown_text(title)}",
        "",
        f"- 统计周期：{start.isoformat()} 至 {end.isoformat()}",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 分析人/团队：{_markdown_text(analyst)}",
        f"- 纳入事件：{len(data)} 条",
        "",
    ]
    if demo_data:
        lines += ["> **虚构演示数据：不代表青岛港真实运行状态。**", ""]
    lines += [
        "> 本报告基于人工录入的公开信息和透明规则排序，不是青岛港官方风险等级，也不构成航行、安全、投资或经营决策依据。",
        "",
        "## 一、本期核心结论",
    ]
    if data.empty:
        lines.append("- 指定日期范围内没有已录入事件，无法形成事实结论。")
    else:
        high_active = int((active["risk_level"] == "高").sum()) if len(active) else 0
        lines.append(
            f"- 共纳入 {len(data)} 条公开事件；当前有效中高风险 {len(active)} 条，其中高优先级 {high_active} 条。"
        )
        lines.append(
            f"- 统计期内已解除或结束风险 {len(resolved)} 条；中高等级政策/采购商机 {len(opportunities)} 条。"
        )
        if len(active):
            top = active.iloc[0]
            lines.append(
                f"- 当前优先核对：**{_markdown_text(top['title'])}**（当前优先分 {top['current_priority_score']}）。"
            )

    lines += ["", "## 二、当前有效风险"]
    if active.empty:
        lines.append("- 指定范围内暂无当前有效的中高风险事件。")
    else:
        for _, row in active.iterrows():
            pending = "｜**待核实**" if row["status"] == "待核实" else ""
            lines += [
                f"### {row['risk_level']}风险｜{_markdown_text(row['title'])}",
                f"- 日期/状态：{_date_text(row['event_date'])}｜{row['status']}{pending}",
                f"- 当前优先分：{row['current_priority_score']}；历史风险分：{row['historical_risk_score']}；来源可信度：{row['source_confidence']}",
                f"- 摘要：{_markdown_text(row['summary'])}",
                f"- 潜在影响：{_markdown_text(row['impact'])}",
                f"- 建议动作：{_markdown_text(row['action'])}",
                f"- 评分说明：{_markdown_text(row['score_explanation'])}",
                f"- 来源：{_source_markdown(row)}",
                "",
            ]

    lines += ["## 三、已解除风险"]
    if resolved.empty:
        lines.append("- 指定范围内没有解除或已结束记录。")
    else:
        for _, row in resolved.iterrows():
            relation = _markdown_text(row["related_event_id"]) or "未关联"
            lines.append(
                f"- **{_markdown_text(row['title'])}**｜{_date_text(row['event_date'])}｜状态 {row['status']}｜"
                f"当前优先分 {row['current_priority_score']}｜历史风险分 {row['historical_risk_score']}｜关联 {relation}｜{_source_markdown(row)}"
            )

    lines += ["", "## 四、政策与采购商机"]
    if opportunities.empty:
        lines.append("- 指定范围内暂无中高等级政策或采购商机。")
    else:
        for _, row in opportunities.iterrows():
            lines.append(
                f"- **{_markdown_text(row['title'])}**｜商机分 {row['opportunity_score']}｜"
                f"{_markdown_text(row['action'])}｜{_source_markdown(row)}"
            )

    lines += [
        "",
        "## 五、数据质量说明",
        f"- 总记录数：{quality.total_records}",
        f"- 必填字段完整率：{quality.completeness_rate:.1f}%",
        f"- 重复记录数：{quality.duplicate_record_count}",
        f"- 缺失来源数：{quality.missing_source_count}",
        f"- 待核实记录数：{quality.pending_verification_count}",
        f"- 校验问题：错误 {quality.error_count} 项、警告 {quality.warning_count} 项、建议 {quality.suggestion_count} 项。",
        "",
        "## 六、完整来源清单",
    ]
    if data.empty:
        lines.append("- 无。")
    else:
        for _, row in data.sort_values("event_date", ascending=False).iterrows():
            lines.append(
                f"- {_date_text(row['event_date'])}｜{_markdown_text(row['title'])}｜"
                f"{_source_markdown(row)}（{_markdown_text(row['source_type'])}）"
            )

    lines += [
        "",
        "## 七、方法与免责声明",
        "- 信息范围仅限本地 CSV 中人工录入并保留来源链接的公开事件，不代表完整、实时的港口运行数据。",
        "- 风险与商机分数来自可检查的类别、关键词、来源可信度和状态系数，仅用于排序和人工复核。",
        "- 解除、恢复和更新信息应关联原事件；业务人员在采取行动前应回到原始来源核验。",
        "- 本报告不采集个人信息，不构成航行、安全、投资、法律或经营建议。",
    ]
    return "\n".join(lines)


def _html_event_cards(dataframe: pd.DataFrame, resolved: bool = False) -> str:
    if dataframe.empty:
        return '<p class="empty">无符合条件的记录。</p>'
    cards = []
    for _, row in dataframe.iterrows():
        url = str(row.get("source_url", "")).strip()
        source_name = escape(str(row.get("source_name", "") or "未填写来源"))
        source = (
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{source_name}</a>'
            if is_valid_source_url(url)
            else f"{source_name}（链接缺失或异常）"
        )
        score_label = (
            f"当前优先分 {row['current_priority_score']} / 历史风险分 {row['historical_risk_score']}"
            if not resolved
            else f"当前优先分 {row['current_priority_score']} / 历史风险分 {row['historical_risk_score']}"
        )
        cards.append(
            '<article class="card">'
            f"<h3>{escape(str(row['title']))}</h3>"
            f"<p class=\"meta\">{_date_text(row['event_date'])} · {escape(str(row['category']))} · {escape(str(row['status']))}</p>"
            f"<p><strong>{escape(score_label)}</strong></p>"
            f"<p>{escape(str(row['summary']))}</p>"
            f"<p><strong>影响：</strong>{escape(str(row['impact']))}</p>"
            f"<p><strong>来源：</strong>{source}</p>"
            "</article>"
        )
    return "".join(cards)


def build_html_report(
    dataframe: pd.DataFrame,
    title: str,
    start_date: Union[str, date],
    end_date: Union[str, date],
    analyst: str = "PortScope",
    demo_data: bool = False,
) -> str:
    start = _as_date(start_date)
    end = _as_date(end_date)
    data = prepare_report_data(dataframe, start, end)
    all_event_ids = normalize_events(dataframe, assign_ids=False)["event_id"].tolist()
    quality = validate_events(data, today=date.today(), known_event_ids=all_event_ids)
    active, resolved, opportunities = _report_groups(data)
    demo_notice = (
        '<div class="notice demo"><strong>虚构演示数据：不代表青岛港真实运行状态。</strong></div>'
        if demo_data
        else ""
    )

    if opportunities.empty:
        opportunity_html = '<p class="empty">无符合条件的记录。</p>'
    else:
        rows = []
        for _, row in opportunities.iterrows():
            url = str(row["source_url"]).strip()
            link = (
                f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">查看原文</a>'
                if is_valid_source_url(url)
                else "链接缺失或异常"
            )
            rows.append(
                "<tr>"
                f"<td>{_date_text(row['event_date'])}</td>"
                f"<td>{escape(str(row['title']))}</td>"
                f"<td>{row['opportunity_score']}</td>"
                f"<td>{escape(str(row['status']))}</td>"
                f"<td>{link}</td>"
                "</tr>"
            )
        opportunity_html = (
            "<table><thead><tr><th>日期</th><th>事件</th><th>商机分</th><th>状态</th><th>来源</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    source_items = []
    for _, row in data.sort_values("event_date", ascending=False).iterrows():
        url = str(row["source_url"]).strip()
        source_name = escape(str(row["source_name"] or "未填写来源"))
        source = (
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{source_name}</a>'
            if is_valid_source_url(url)
            else f"{source_name}（链接缺失或异常）"
        )
        source_items.append(
            f"<li>{_date_text(row['event_date'])}｜{escape(str(row['title']))}｜{source}</li>"
        )
    sources_html = "".join(source_items) or "<li>无。</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; color: #17324d; background: #f4f7fa; font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif; line-height: 1.65; }}
    main {{ max-width: 980px; margin: 32px auto; padding: 36px; background: white; box-shadow: 0 8px 30px rgba(23,50,77,.08); }}
    h1, h2, h3 {{ color: #0b4f6c; }} h2 {{ border-bottom: 2px solid #d7e5ec; padding-bottom: 8px; margin-top: 34px; }}
    .meta, .empty {{ color: #60778b; }} .notice {{ padding: 12px 16px; background: #edf6fa; border-left: 4px solid #168aad; margin: 18px 0; }}
    .demo {{ background: #fff4df; border-color: #e39b22; }} .card {{ border: 1px solid #dce7ed; border-radius: 6px; padding: 16px 20px; margin: 12px 0; }}
    table {{ border-collapse: collapse; width: 100%; }} th, td {{ border: 1px solid #dce7ed; padding: 9px; text-align: left; vertical-align: top; }} th {{ background: #edf6fa; }}
    a {{ color: #087ca7; }} footer {{ margin-top: 34px; color: #60778b; font-size: .92rem; }}
  </style>
</head>
<body><main>
  <h1>{escape(title)}</h1>
  <p class="meta">统计周期：{start.isoformat()} 至 {end.isoformat()}｜生成日期：{date.today().isoformat()}｜分析人/团队：{escape(analyst)}｜纳入事件：{len(data)} 条</p>
  {demo_notice}
  <div class="notice">本报告基于人工录入的公开信息和透明规则排序，不是青岛港官方风险等级，也不构成航行、安全、投资或经营决策依据。</div>
  <h2>一、本期核心结论</h2>
  <ul><li>当前有效中高风险 {len(active)} 条。</li><li>已解除或结束风险 {len(resolved)} 条。</li><li>中高等级政策/采购商机 {len(opportunities)} 条。</li></ul>
  <h2>二、当前有效风险</h2>{_html_event_cards(active)}
  <h2>三、已解除风险</h2>{_html_event_cards(resolved, resolved=True)}
  <h2>四、政策与采购商机</h2>{opportunity_html}
  <h2>五、数据质量说明</h2>
  <ul><li>必填字段完整率：{quality.completeness_rate:.1f}%</li><li>重复记录：{quality.duplicate_record_count} 条</li><li>缺失来源：{quality.missing_source_count} 条</li><li>待核实：{quality.pending_verification_count} 条</li><li>错误 {quality.error_count} 项、警告 {quality.warning_count} 项、建议 {quality.suggestion_count} 项</li></ul>
  <h2>六、完整来源清单</h2><ul>{sources_html}</ul>
  <h2>七、方法与免责声明</h2>
  <ul><li>仅使用本地 CSV 中人工录入的公开事件，不代表完整、实时的港口运行数据。</li><li>规则分数只用于排序和人工复核；采取行动前应回到原始来源核验。</li><li>本报告不采集个人信息，不构成航行、安全、投资、法律或经营建议。</li></ul>
  <footer>PortScope 青岛港公开情报试验台 · 真实数据试跑版</footer>
</main></body></html>"""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成青岛港公开情报周报")
    parser.add_argument("--start", default=(date.today() - timedelta(days=6)).isoformat())
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--title", default="青岛港公开情报周报")
    parser.add_argument("--analyst", default="PortScope")
    parser.add_argument("--demo", action="store_true", help="明确使用虚构演示数据")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    if args.demo:
        dataframe = pd.read_csv(SAMPLE_DATA_PATH, dtype=str, keep_default_na=False)
    else:
        dataframe = load_events(DEFAULT_EVENTS_PATH)
    markdown = build_weekly_report(
        dataframe, args.title, args.start, args.end, args.analyst, demo_data=args.demo
    )
    html = build_html_report(
        dataframe, args.title, args.start, args.end, args.analyst, demo_data=args.demo
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = args.output_dir / "weekly_report.md"
    html_path = args.output_dir / "weekly_report.html"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    print(f"Generated: {markdown_path}")
    print(f"Generated: {html_path}")
    print(f"Included events: {len(filter_events_by_date(dataframe, args.start, args.end))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
