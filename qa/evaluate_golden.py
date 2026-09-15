from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_quality import assess_document
from intake_analyzer import suggest_category
from platform_db import content_hash


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "golden_manifest.json"
OUTPUT_JSON = ROOT / "quality_report.json"
OUTPUT_MD = ROOT / "quality_report.md"


def _extract(path: Path) -> dict[str, str]:
    html = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    def meta(name: str) -> str:
        node = soup.select_one(f"meta[name='{name}']")
        return str(node.get("content") or "").strip() if node else ""

    title = meta("ArticleTitle") or (soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")
    date = meta("pubdate")
    if not date:
        match = re.search(r"20\d{2}-\d{2}-\d{2}", soup.get_text(" ", strip=True))
        date = match.group(0) if match else ""
    source = meta("ContentSource")
    node = soup.select_one("article, main, #zoom, .content")
    text = "\n".join(item.get_text(" ", strip=True) for item in node.select("p")) if node else ""
    return {"title": title, "date": date[:10], "source": source, "text": text, "html": html}


def evaluate() -> dict[str, object]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    extracted = {item["file"]: _extract(ROOT / "golden_documents" / item["file"]) for item in manifest}
    by_canonical: dict[str, list[str]] = defaultdict(list)
    for item in manifest:
        by_canonical[str(item["canonical_group"])].append(item["file"])

    counters = defaultdict(int)
    duplicate_predictions: dict[str, bool] = {}
    seen: list[tuple[str, str, str]] = []
    rows: list[dict[str, object]] = []
    for item in manifest:
        result = extracted[item["file"]]
        template_texts = [
            extracted[other["file"]]["text"] for other in manifest
            if other["file"] != item["file"] and other["canonical_group"] != item["canonical_group"]
        ]
        quality = assess_document(
            title=result["title"], text=result["text"], published_at=result["date"],
            raw_html=result["html"], site_names=["黄金测试网站"], template_texts=template_texts,
        )
        category, _ = suggest_category(result["title"], result["text"], "")
        digest = content_hash(result["text"])
        duplicate = any(
            digest == previous_digest or result["title"] == previous_title
            for _, previous_title, previous_digest in seen
        )
        duplicate_predictions[item["file"]] = duplicate
        seen.append((item["file"], result["title"], digest))
        counters["title"] += int(result["title"] == item["title"])
        counters["date"] += int(result["date"] == item["date"])
        counters["source"] += int(result["source"] == item["source"])
        expected_body_ok = not item["report_blocked"] or not item["file"].startswith(("noise_", "mojibake_"))
        counters["body"] += int(quality.processing_allowed == expected_body_ok)
        if not item["report_blocked"]:
            counters["category"] += int(category == item["category"])
            counters["category_total"] += 1
        counters["report"] += int((not quality.report_allowed) == bool(item["report_blocked"]))
        rows.append({
            "file": item["file"], "quality_status": quality.status, "quality_note": quality.note,
            "category_expected": item["category"], "category_actual": category,
            "report_blocked_expected": item["report_blocked"], "report_blocked_actual": not quality.report_allowed,
        })

    duplicate_total = 0
    duplicate_correct = 0
    first_by_group: set[str] = set()
    for item in manifest:
        if item["report_blocked"]:
            continue
        group = str(item["duplicate_group"])
        expected = bool(group and group in first_by_group)
        if group:
            first_by_group.add(group)
        duplicate_total += 1
        duplicate_correct += int(duplicate_predictions[item["file"]] == expected)

    total = len(manifest)
    metrics = {
        "fixture_count": total,
        "title_accuracy": round(counters["title"] / total, 4),
        "date_accuracy": round(counters["date"] / total, 4),
        "source_accuracy": round(counters["source"] / total, 4),
        "body_quality_accuracy": round(counters["body"] / total, 4),
        "category_accuracy": round(counters["category"] / max(1, counters["category_total"]), 4),
        "duplicate_accuracy": round(duplicate_correct / duplicate_total, 4),
        "report_block_accuracy": round(counters["report"] / total, 4),
    }
    payload = {"metrics": metrics, "documents": rows}
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 黄金文档质量评估", ""]
    for key, value in metrics.items():
        lines.append(f"- {key}: {value}")
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(evaluate()["metrics"], ensure_ascii=False))
