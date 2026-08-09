from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import posixpath
import re
import sqlite3
import sys
from typing import Iterable, Mapping, Sequence
from zipfile import ZipFile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_db import connect, initialize_database
from review_provenance import HUMAN_REVIEWER_TYPES


LABEL_HEADERS = [
    "document_id",
    "原文URL",
    "正确标题",
    "正确发布日期",
    "正确发布机构",
    "正文是否合格",
    "是否包含乱码",
    "是否包含导航噪声",
    "正确类别",
    "事实摘要是否准确",
    "evidence_quotes是否存在于原文",
    "潜在影响是否合理",
    "是否具有业务价值",
    "是否允许进入内部研究版",
    "是否允许进入客户交付版",
    "人工标注状态",
    "人工备注",
    "reviewer_type",
    "reviewer_name",
    "reviewed_at",
    "review_method",
    "review_version",
    "reviewer_note",
]

REQUIRED_LABEL_FIELDS = (
    "document_id", "原文URL", "正确标题", "正确发布日期", "正确发布机构",
    "正文是否合格", "是否包含乱码", "是否包含导航噪声", "正确类别",
    "事实摘要是否准确", "evidence_quotes是否存在于原文", "潜在影响是否合理",
    "是否具有业务价值", "是否允许进入内部研究版", "是否允许进入客户交付版",
)
REQUIRED_REVIEW_FIELDS = (
    "reviewer_type", "reviewer_name", "reviewed_at", "review_method", "review_version",
)
TRUE_VALUES = {"1", "true", "yes", "是", "合格", "允许", "有", "存在"}
FALSE_VALUES = {"0", "false", "no", "否", "不合格", "不允许", "无", "不存在"}


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", _text(value)).casefold()


def _bool(value: object) -> bool | None:
    normalized = _text(value).casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return ["".join(node.itertext()) for node in root.findall("x:si", ns)]


def read_labels_xlsx(path: Path, sheet_name: str = "人工标注") -> list[dict[str, str]]:
    """Read the small QA label workbook without adding a runtime Excel dependency."""
    if not path.is_file():
        return []
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    relation_namespace = {
        "r": "http://schemas.openxmlformats.org/package/2006/relationships"
    }
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        target_id = ""
        for sheet in workbook.findall("x:sheets/x:sheet", namespace):
            if sheet.attrib.get("name") == sheet_name:
                target_id = sheet.attrib.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                    "",
                )
                break
        if not target_id:
            return []
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = ""
        for item in relationships.findall("r:Relationship", relation_namespace):
            if item.attrib.get("Id") == target_id:
                target = item.attrib.get("Target", "")
                break
        if not target:
            return []
        normalized_target = target.lstrip("/")
        sheet_path = posixpath.normpath(
            normalized_target
            if normalized_target.startswith("xl/")
            else f"xl/{normalized_target}"
        )
        shared = _shared_strings(archive)
        sheet_root = ET.fromstring(archive.read(sheet_path))
        rows: list[list[str]] = []
        for row in sheet_root.findall("x:sheetData/x:row", namespace):
            values: dict[int, str] = {}
            for cell in row.findall("x:c", namespace):
                reference = cell.attrib.get("r", "A1")
                letters = re.match(r"[A-Z]+", reference)
                column = 0
                for character in (letters.group(0) if letters else "A"):
                    column = column * 26 + ord(character) - 64
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    value = "".join(cell.itertext())
                else:
                    value_node = cell.find("x:v", namespace)
                    value = value_node.text if value_node is not None and value_node.text else ""
                    if cell_type == "s" and value.isdigit():
                        index = int(value)
                        value = shared[index] if index < len(shared) else ""
                values[column - 1] = value
            width = max(values, default=-1) + 1
            rows.append([values.get(index, "") for index in range(width)])
    if not rows:
        return []
    headers = [_text(value) for value in rows[0]]
    result: list[dict[str, str]] = []
    for values in rows[1:]:
        item = {header: _text(values[index] if index < len(values) else "") for index, header in enumerate(headers) if header}
        if any(item.values()):
            result.append(item)
    return result


def label_content_is_complete(label: Mapping[str, object]) -> bool:
    explicit = _text(label.get("人工标注状态"))
    if explicit and explicit not in {"已完成", "完成"}:
        return False
    return all(_text(label.get(field)) for field in REQUIRED_LABEL_FIELDS)


def label_is_complete(label: Mapping[str, object]) -> bool:
    """Only independently completed human labels qualify as the gold standard."""

    return (
        label_content_is_complete(label)
        and _text(label.get("reviewer_type")) in HUMAN_REVIEWER_TYPES
        and all(_text(label.get(field)) for field in REQUIRED_REVIEW_FIELDS)
    )


def calculate_human_metrics(
    records: Sequence[Mapping[str, object]],
    labels: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    by_document = {_text(item.get("document_id")): item for item in records if _text(item.get("document_id"))}
    relevant = [item for item in labels if _text(item.get("document_id")) in by_document]
    completed = [item for item in relevant if label_is_complete(item)]
    nonhuman_completed = [
        item for item in relevant
        if label_content_is_complete(item) and _text(item.get("reviewer_type")) not in HUMAN_REVIEWER_TYPES
    ]
    provenance_counts = Counter(_text(item.get("reviewer_type")) or "unknown" for item in relevant)
    if not completed:
        return {
            "status": "仅自动/代理标注，待独立真人核验" if nonhuman_completed else "待人工核验",
            "completed_count": 0,
            "automated_or_agent_completed_count": len(nonhuman_completed),
            "reviewer_type_counts": dict(provenance_counts),
            "unverified_count": len(by_document),
            "title_accuracy": None,
            "date_accuracy": None,
            "publisher_accuracy": None,
            "body_quality_accuracy": None,
            "category_accuracy": None,
            "evidence_traceability": None,
            "evidence_validator_accuracy": None,
            "summary_accuracy": None,
            "impact_reasonableness": None,
        }
    counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    for label in completed:
        record = by_document[_text(label.get("document_id"))]
        totals["metadata"] += 1
        counts["title"] += int(_normalized(record.get("title")) == _normalized(label.get("正确标题")))
        counts["date"] += int(_text(record.get("published_at"))[:10] == _text(label.get("正确发布日期"))[:10])
        counts["publisher"] += int(_normalized(record.get("publisher")) == _normalized(label.get("正确发布机构")))
        expected_body = _bool(label.get("正文是否合格"))
        actual_body = _text(record.get("quality_status")) == "合格"
        counts["body"] += int(expected_body is not None and actual_body == expected_body)
        # Extraction accuracy is only meaningful when the source body is
        # manually judged usable and a structured event was actually created.
        if expected_body is True and _text(record.get("event_id")):
            totals["extraction"] += 1
            counts["category"] += int(_text(record.get("category")) == _text(label.get("正确类别")))
            expected_evidence = _bool(label.get("evidence_quotes是否存在于原文"))
            actual_evidence = bool(int(record.get("evidence_verified") or 0))
            counts["evidence"] += int(expected_evidence is True)
            counts["evidence_validator"] += int(
                expected_evidence is not None and actual_evidence == expected_evidence
            )
            counts["summary"] += int(_bool(label.get("事实摘要是否准确")) is True)
            counts["impact"] += int(_bool(label.get("潜在影响是否合理")) is True)
    total = len(completed)
    extraction_total = totals["extraction"]
    return {
        "status": "已人工核验",
        "completed_count": total,
        "automated_or_agent_completed_count": len(nonhuman_completed),
        "reviewer_type_counts": dict(provenance_counts),
        "unverified_count": max(0, len(by_document) - total),
        "title_accuracy": round(counts["title"] / total, 4),
        "date_accuracy": round(counts["date"] / total, 4),
        "publisher_accuracy": round(counts["publisher"] / total, 4),
        "body_quality_accuracy": round(counts["body"] / total, 4),
        "category_accuracy": round(counts["category"] / extraction_total, 4) if extraction_total else None,
        "evidence_traceability": round(counts["evidence"] / extraction_total, 4) if extraction_total else None,
        "evidence_validator_accuracy": (
            round(counts["evidence_validator"] / extraction_total, 4) if extraction_total else None
        ),
        "summary_accuracy": round(counts["summary"] / extraction_total, 4) if extraction_total else None,
        "impact_reasonableness": round(counts["impact"] / extraction_total, 4) if extraction_total else None,
    }


def collect_records(db_path: Path, workspace_id: str) -> list[dict[str, object]]:
    initialize_database(db_path)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT d.document_id,d.canonical_url,d.source_file_url,d.title,d.publisher,d.published_at,
            d.quality_status,d.extraction_status,d.document_format,d.ai_status,d.created_at,
            e.event_id,e.category,e.summary,e.impact,e.business_value,e.value_reason,e.evidence_verified,
            e.extraction_method,e.extraction_confidence,e.ai_generated,e.human_verified,e.report_eligible,
            s.source_name,s.commercial_reuse_status
            FROM documents d
            JOIN sources s ON s.source_id=d.source_id
            LEFT JOIN events e ON e.document_id=d.document_id
            WHERE d.workspace_id=? AND d.is_current=1
            ORDER BY d.published_at,d.document_id""",
            (workspace_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def collect_automatic_metrics(
    db_path: Path,
    workspace_id: str,
    *,
    source_stats: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    records = collect_records(db_path, workspace_id)
    with connect(db_path) as connection:
        runs = connection.execute(
            "SELECT * FROM crawl_runs WHERE workspace_id=? ORDER BY started_at",
            (workspace_id,),
        ).fetchall()
        logs = connection.execute(
            """SELECT input_tokens,output_tokens,elapsed_ms,success,model_name
            FROM ai_call_logs
            WHERE workspace_id=? AND task_type IN ('extract_event','event_extraction')""",
            (workspace_id,),
        ).fetchall()
        chunk_status = connection.execute(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN embedding_status='成功' THEN 1 ELSE 0 END) AS successful
            FROM document_chunks WHERE workspace_id=?""",
            (workspace_id,),
        ).fetchone()
        fts_count = int(connection.execute(
            """SELECT COUNT(*) FROM document_chunks_fts f
            JOIN document_chunks c USING(chunk_id) WHERE c.workspace_id=?""",
            (workspace_id,),
        ).fetchone()[0])
        qa_log_count = int(connection.execute(
            "SELECT COUNT(*) FROM qa_logs WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0])
        reports = [
            dict(row)
            for row in connection.execute(
                """SELECT report_id,version,report_mode,analysis_model
                FROM reports WHERE workspace_id=? ORDER BY version""",
                (workspace_id,),
            ).fetchall()
        ]
    ai_success = sum(int(row["success"] or 0) for row in logs)
    total_tokens = sum(int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0) for row in logs)
    valid_records = [item for item in records if _text(item.get("quality_status")) == "合格"]
    pdf_records = [item for item in records if _text(item.get("document_format")).casefold() == "pdf"]
    value_counts = Counter(
        _text(item.get("business_value")) or "未评估"
        for item in valid_records
        if item.get("event_id")
    )
    latest_source_stats: dict[tuple[str, str], Mapping[str, object]] = {}
    for item in source_stats:
        latest_source_stats[(_text(item.get("phase")), _text(item.get("source_id")))] = item
    return {
        "document_count": len(records),
        "event_count": sum(bool(item.get("event_id")) for item in records),
        "source_stats": list(latest_source_stats.values()),
        "pdf_discovered": len(pdf_records),
        "pdf_success": sum(
            _text(item.get("quality_status")) == "合格"
            and _text(item.get("extraction_status")) == "成功"
            for item in pdf_records
        ),
        "pdf_rejected": sum(_text(item.get("quality_status")) != "合格" for item in pdf_records),
        "encoding_blocked": sum(int(row["encoding_blocked_count"] or 0) for row in runs),
        "navigation_noise_blocked": sum(int(row["noise_blocked_count"] or 0) for row in runs),
        "out_of_range_blocked": sum(int(row["out_of_range_count"] or 0) for row in runs),
        "deepseek_calls": len(logs),
        "deepseek_success": ai_success,
        "deepseek_valid_documents": sum(
            bool(item.get("event_id"))
            and _text(item.get("quality_status")) == "合格"
            and _text(item.get("extraction_method")) == "deepseek"
            for item in records
        ),
        "deepseek_success_rate": round(ai_success / len(logs), 4) if logs else None,
        "actual_models": sorted({_text(row["model_name"]) for row in logs if _text(row["model_name"])}),
        "average_tokens_per_call": round(total_tokens / len(logs), 2) if logs else None,
        "average_processing_ms_per_call": round(
            sum(int(row["elapsed_ms"] or 0) for row in logs) / len(logs), 2
        ) if logs else None,
        "business_value_counts": dict(value_counts),
        "chunk_count": int(chunk_status["total"] or 0),
        "embedded_chunk_count": int(chunk_status["successful"] or 0),
        "fts_row_count": fts_count,
        "qa_log_count": qa_log_count,
        "reports": reports,
    }


def build_label_rows(records: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in records:
        rows.append(
            {
                "document_id": _text(record.get("document_id")),
                "原文URL": _text(record.get("source_file_url") or record.get("canonical_url")),
                "正确标题": "",
                "正确发布日期": "",
                "正确发布机构": "",
                "正文是否合格": "",
                "是否包含乱码": "",
                "是否包含导航噪声": "",
                "正确类别": "",
                "事实摘要是否准确": "",
                "evidence_quotes是否存在于原文": "",
                "潜在影响是否合理": "",
                "是否具有业务价值": "",
                "是否允许进入内部研究版": "",
                "是否允许进入客户交付版": "",
                "人工标注状态": "待人工核验",
                "人工备注": "",
                "reviewer_type": "unknown",
                "reviewer_name": "",
                "reviewed_at": "",
                "review_method": "",
                "review_version": "0.5.0-beta",
                "reviewer_note": "",
            }
        )
    return rows


def _metric(value: object) -> str:
    if value is None:
        return "待人工核验"
    if isinstance(value, float) and 0 <= value <= 1:
        return f"{value:.1%}"
    return _text(value)


def write_markdown(
    output_path: Path,
    automatic: Mapping[str, object],
    human: Mapping[str, object],
) -> None:
    lines = [
        "# PortScope 真实数据质量验收报告",
        "",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- 说明：自动指标、人工金标准和待核验项分开列示；未完成人工标注时不得宣称真实准确率。",
        "",
        "## 自动流水线指标",
        "",
        f"- 当前文档：{automatic.get('document_count', 0)}",
        f"- 当前事件：{automatic.get('event_count', 0)}",
        f"- PDF发现/成功/拒绝：{automatic.get('pdf_discovered', 0)}/{automatic.get('pdf_success', 0)}/{automatic.get('pdf_rejected', 0)}",
        f"- 乱码拦截：{automatic.get('encoding_blocked', 0)}",
        f"- 导航或模板噪声拦截：{automatic.get('navigation_noise_blocked', 0)}",
        f"- 日期范围外拦截：{automatic.get('out_of_range_blocked', 0)}",
        f"- DeepSeek调用/成功：{automatic.get('deepseek_calls', 0)}/{automatic.get('deepseek_success', 0)}",
        f"- 通过质量门禁的DeepSeek文档：{automatic.get('deepseek_valid_documents', 0)}",
        f"- DeepSeek成功率：{_metric(automatic.get('deepseek_success_rate'))}",
        f"- 实际模型：{', '.join(automatic.get('actual_models', [])) or '未调用'}",
        f"- 每篇平均Token：{_metric(automatic.get('average_tokens_per_call'))}",
        f"- 每篇平均AI耗时(ms)：{_metric(automatic.get('average_processing_ms_per_call'))}",
        f"- Chunk/已向量化/FTS5：{automatic.get('chunk_count', 0)}/{automatic.get('embedded_chunk_count', 0)}/{automatic.get('fts_row_count', 0)}",
        f"- RAG问答日志：{automatic.get('qa_log_count', 0)}",
        f"- 业务价值分布：{json.dumps(automatic.get('business_value_counts', {}), ensure_ascii=False)}",
        "",
        "## 来源验收",
        "",
        "| 来源 | 阶段 | 发现 | 正文/PDF成功 | 拒绝 | 提取成功率 | 健康状态 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in automatic.get("source_stats", []):
        discovered = int(row.get("discovered", 0) or 0)
        success = int(row.get("success", 0) or 0)
        rate = success / discovered if discovered else 0
        lines.append(
            f"| {_text(row.get('source_name'))} | {_text(row.get('phase'))} | {discovered} | "
            f"{success} | {int(row.get('rejected', 0) or 0)} | {rate:.1%} | {_text(row.get('health'))} |"
        )
    lines.extend(
        [
            "",
            "## 人工金标准",
            "",
            f"- 状态：{human.get('status')}",
            f"- 已人工核验：{human.get('completed_count', 0)}",
            f"- AI/Agent/自动规则已填但不计人工金标准：{human.get('automated_or_agent_completed_count', 0)}",
            f"- 标注来源分布：{json.dumps(human.get('reviewer_type_counts', {}), ensure_ascii=False)}",
            f"- 待人工核验：{human.get('unverified_count', 0)}",
            f"- 标题准确率：{_metric(human.get('title_accuracy'))}",
            f"- 日期准确率：{_metric(human.get('date_accuracy'))}",
            f"- 发布机构准确率：{_metric(human.get('publisher_accuracy'))}",
            f"- 正文合格判断准确率：{_metric(human.get('body_quality_accuracy'))}",
            f"- 分类准确率：{_metric(human.get('category_accuracy'))}",
            f"- evidence_quotes可追溯率：{_metric(human.get('evidence_traceability'))}",
            f"- evidence_quotes自动校验准确率：{_metric(human.get('evidence_validator_accuracy'))}",
            f"- 事实摘要人工通过率：{_metric(human.get('summary_accuracy'))}",
            f"- 潜在影响合理率：{_metric(human.get('impact_reasonableness'))}",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(output_path)


def evaluate(
    db_path: Path,
    workspace_id: str,
    labels_path: Path,
    *,
    source_stats_path: Path | None = None,
    output_md: Path | None = None,
    output_json: Path | None = None,
) -> dict[str, object]:
    source_stats: list[dict[str, object]] = []
    if source_stats_path and source_stats_path.is_file():
        payload = json.loads(source_stats_path.read_text(encoding="utf-8"))
        source_stats = list(payload.get("source_stats", payload if isinstance(payload, list) else []))
    records = collect_records(db_path, workspace_id)
    automatic = collect_automatic_metrics(db_path, workspace_id, source_stats=source_stats)
    labels = read_labels_xlsx(labels_path)
    human = calculate_human_metrics(records, labels)
    result = {
        "workspace_id": workspace_id,
        "automatic_metrics": automatic,
        "human_metrics": human,
        "documents": records,
        "label_rows": build_label_rows(records),
    }
    if output_md:
        write_markdown(output_md, automatic, human)
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_json.with_suffix(output_json.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output_json)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="依据QA数据库和人工Excel标签计算真实质量指标。")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--labels", type=Path, default=ROOT / "qa" / "real_acceptance_labels.xlsx")
    parser.add_argument("--source-stats", type=Path)
    parser.add_argument("--output-md", type=Path, default=ROOT / "qa" / "real_acceptance_report.md")
    parser.add_argument("--output-json", type=Path, default=ROOT / "qa" / "real_acceptance_report.json")
    args = parser.parse_args(argv)
    result = evaluate(
        args.db,
        args.workspace_id,
        args.labels,
        source_stats_path=args.source_stats,
        output_md=args.output_md,
        output_json=args.output_json,
    )
    print(
        json.dumps(
            {
                "documents": result["automatic_metrics"]["document_count"],
                "human_status": result["human_metrics"]["status"],
                "completed_labels": result["human_metrics"]["completed_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
