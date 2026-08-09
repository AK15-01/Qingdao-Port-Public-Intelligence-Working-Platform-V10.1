from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from typing import Iterable, Sequence

from bs4 import BeautifulSoup


MOJIBAKE_CHARACTERS = frozenset("åäçÃ�")
NAVIGATION_TERMS = (
    "首页", "网站首页", "新闻资讯", "集团概况", "业务服务", "信息公开",
    "联系我们", "网站地图", "返回顶部", "友情链接", "相关链接", "上一篇", "下一篇",
)


@dataclass(frozen=True)
class DocumentQualityResult:
    status: str
    processing_allowed: bool
    report_allowed: bool
    issues: tuple[str, ...] = ()
    metrics: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def note(self) -> str:
        return "；".join(self.issues) if self.issues else "正文质量门禁通过"


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _normalized_title(value: object) -> str:
    return re.sub(r"[\s\-_|·—]+", "", str(value or "")).casefold()


def _visible_paragraphs(raw_html: str) -> list[str]:
    if not raw_html:
        return []
    soup = BeautifulSoup(raw_html, "html.parser")
    for selector in ("script", "style", "nav", "header", "footer", "aside", "form", "noscript"):
        for node in soup.select(selector):
            node.decompose()
    paragraphs: list[str] = []
    for node in soup.select("article p, main p, #zoom p, .article p, .content p, .right_content p"):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if len(_compact(text)) >= 12:
            paragraphs.append(text)
    return paragraphs


def has_encoding_anomaly(title: str, text: str) -> tuple[bool, float, int]:
    sample = _compact(f"{title}{text}")
    if not sample:
        return False, 0.0, 0
    suspicious = sum(character in MOJIBAKE_CHARACTERS for character in sample)
    replacement = sample.count("�")
    ratio = suspicious / max(1, len(sample))
    return bool(replacement >= 1 or suspicious >= 5 and ratio >= 0.012), ratio, suspicious


def template_similarity(text: str, templates: Iterable[str]) -> float:
    current = _compact(text)[:6000]
    if len(current) < 120:
        return 0.0
    highest = 0.0
    for value in templates:
        other = _compact(value)[:6000]
        if len(other) < 120 or other == current:
            continue
        highest = max(highest, SequenceMatcher(None, current, other, autojunk=False).ratio())
    return highest


def structured_maritime_warning_facts(title: str, text: str) -> dict[str, str]:
    """Extract deterministic fact slots used to distinguish official notices."""

    combined = f"{title}\n{text}"
    if "预警" not in combined or not any(
        marker in combined for marker in ("海上风险", "海上大雾", "海上大风", "海浪")
    ):
        return {}
    patterns = {
        "issue_number": r"第\s*(\d+)\s*期",
        "published_time": (
            r"(20\d{2}年\d{1,2}月\d{1,2}日"
            r"(?:\d{1,2}时(?:\d{1,2}分)?)?)"
        ),
        "risk_level": r"(红色|橙色|黄色|蓝色)预警",
        "area": (
            r"(渤海海峡|渤海|黄海北部|黄海中部|黄海南部|山东半岛"
            r"|山东沿海|我省各预报海区)"
        ),
        "weather_metric": (
            r"(?:能见度(?:低于|不足)?\s*\d+\s*米|"
            r"\d+\s*(?:到|～|-)\s*\d+\s*级风|"
            r"阵风\s*\d+\s*(?:到|～|-)\s*\d+\s*级|"
            r"浪高\s*\d+(?:\.\d+)?\s*米)"
        ),
        "valid_time": (
            r"(?:预计[，,：:\s]*)([^。；\n]{2,60}"
            r"(?:日|时|夜间|白天|前半夜))"
        ),
        "lifecycle": r"(解除|继续发布|发布|延期|调整|更新)",
    }
    facts: dict[str, str] = {}
    for field, pattern in patterns.items():
        matches = re.findall(pattern, combined)
        if matches:
            values = [
                "".join(item) if isinstance(item, tuple) else str(item)
                for item in matches
            ]
            facts[field] = "、".join(dict.fromkeys(values))[:300]
    return facts


def assess_document(
    *,
    title: str,
    text: str,
    published_at: str = "",
    raw_html: str = "",
    site_names: Sequence[str] = (),
    template_texts: Sequence[str] = (),
) -> DocumentQualityResult:
    title = str(title or "").strip()
    text = str(text or "").strip()
    compact = _compact(text)
    encoding_bad, encoding_ratio, encoding_count = has_encoding_anomaly(title, text)
    if encoding_bad:
        return DocumentQualityResult(
            "编码异常", False, False,
            ("标题或正文包含大量疑似错误编码字符",),
            {
                "text_characters": len(compact),
                "encoding_anomaly_count": encoding_count,
                "encoding_anomaly_ratio": round(encoding_ratio, 4),
            },
        )

    processing_issues: list[str] = []
    report_issues: list[str] = []
    normalized = _normalized_title(title)
    normalized_sites = {_normalized_title(value) for value in site_names if str(value or "").strip()}
    generic_title = (
        not normalized
        or normalized in {"首页", "网站首页"}
        or normalized in normalized_sites
        or ("官网" in title and len(normalized) <= 18)
    )
    if generic_title:
        report_issues.append("标题为网站名称、首页或缺失")

    paragraph_lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in re.split(r"[\r\n]+", text)
        if len(_compact(line)) >= 12
    ]
    html_paragraphs = _visible_paragraphs(raw_html)
    punctuation_units = [part for part in re.split(r"[。！？!?；;]+", text) if len(_compact(part)) >= 12]
    effective_paragraphs = max(len(paragraph_lines), len(html_paragraphs), len(punctuation_units))
    # A concise government notice can be one paragraph. Block only when it is
    # both short and structurally thin, while still rejecting empty templates.
    if len(compact) < 30 or (effective_paragraphs < 2 and len(compact) < 70):
        processing_issues.append("正文有效段落过少")
    attachment_count = len(re.findall(r"\.(?:pdf|docx?|xlsx?|zip|rar)\b", text, flags=re.I))
    factual_sentences = len([
        part for part in re.split(r"[。！？!?；;]+", text)
        if len(_compact(part)) >= 18 and not re.search(r"\.(?:pdf|docx?|xlsx?|zip|rar)\b", part, flags=re.I)
    ])
    if attachment_count >= 2 and factual_sentences == 0:
        processing_issues.append("正文仅包含附件名称或下载链接")

    term_counts = {term: text.count(term) for term in NAVIGATION_TERMS}
    navigation_hits = sum(term_counts.values())
    navigation_characters = sum(len(term) * count for term, count in term_counts.items())
    navigation_ratio = navigation_characters / max(1, len(compact))
    repeated_navigation = any(count >= 3 for count in term_counts.values())
    if (navigation_hits >= 6 and navigation_ratio >= 0.06) or repeated_navigation:
        processing_issues.append("导航菜单、重复栏目或页头页脚占比过高")

    informative_characters = len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", compact))
    information_density = informative_characters / max(1, len(compact))
    if len(compact) >= 80 and information_density < 0.55:
        processing_issues.append("正文信息密度过低")

    similarity = template_similarity(text, template_texts)
    structured_facts = structured_maritime_warning_facts(title, text)
    template_fact_signatures = [
        structured_maritime_warning_facts("", value)
        for value in template_texts
        if str(value or "").strip()
    ]
    facts_differ = not template_fact_signatures or any(
        structured_facts != candidate
        for candidate in template_fact_signatures
        if candidate
    )
    structured_template_warning = bool(
        similarity >= 0.88
        and len(structured_facts) >= 4
        and facts_differ
        and len(compact) >= 180
        and effective_paragraphs >= 3
        and navigation_ratio < 0.06
    )
    if similarity >= 0.88:
        if not structured_template_warning:
            processing_issues.append("正文与其他页面模板高度相似")

    if not re.match(r"^20\d{2}-\d{2}-\d{2}", str(published_at or "").strip()):
        report_issues.append("发布日期缺失或不可识别")

    processing_issues = list(dict.fromkeys(processing_issues))
    report_issues = list(dict.fromkeys(report_issues))
    processing_allowed = not processing_issues
    all_issues = tuple(processing_issues + report_issues)
    status = "合格" if not all_issues else ("正文质量不足" if processing_issues else "报告门禁未通过")
    return DocumentQualityResult(
        status,
        processing_allowed,
        processing_allowed and not report_issues,
        all_issues,
        {
            "text_characters": len(compact),
            "effective_paragraphs": effective_paragraphs,
            "attachment_count": attachment_count,
            "navigation_hits": navigation_hits,
            "navigation_ratio": round(navigation_ratio, 4),
            "information_density": round(information_density, 4),
            "template_similarity": round(similarity, 4),
            "template_similarity_warning": int(structured_template_warning),
            "structured_fact_count": len(structured_facts),
            "structured_fact_fields": "、".join(sorted(structured_facts)),
            "generic_title": int(generic_title),
            "date_present": int(not bool(report_issues and "发布日期缺失或不可识别" in report_issues)),
        },
    )
