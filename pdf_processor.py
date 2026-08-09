from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import math
import re
from typing import Mapping, Sequence
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup


PDF_MAGIC = b"%PDF-"
PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}
DEFAULT_MAX_PDF_BYTES = 15 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 200


class PDFProcessingError(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PDFExtractionResult:
    text: str
    title: str
    published_at: str
    page_count: int
    removed_repeated_lines: int
    file_size_bytes: int
    file_sha256: str
    quality_note: str


def _content_type(value: object) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def pdf_filename_hint(url: str, headers: Mapping[str, object] | None = None) -> bool:
    parsed = urlparse(str(url or ""))
    path_and_query = f"{parsed.path}?{parsed.query}".casefold()
    if ".pdf" in path_and_query:
        return True
    disposition = str((headers or {}).get("Content-Disposition") or "").casefold()
    if re.search(r"filename\*?=[^;\r\n]*\.pdf(?:[\"']|;|$)", disposition):
        return True
    for values in parse_qs(parsed.query).values():
        if any(str(value).casefold().endswith(".pdf") for value in values):
            return True
    return False


def validate_pdf_payload(
    payload: bytes,
    *,
    url: str,
    content_type: str,
    headers: Mapping[str, object] | None = None,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
) -> None:
    if len(payload) > int(max_bytes):
        raise PDFProcessingError("PDF内容过大", "PDF超过配置的最大下载体积。")
    mime = _content_type(content_type)
    disposition_pdf = ".pdf" in str((headers or {}).get("Content-Disposition") or "").casefold()
    mime_ok = mime in PDF_CONTENT_TYPES or (mime == "application/octet-stream" and disposition_pdf)
    if not mime_ok:
        raise PDFProcessingError("PDF类型异常", f"Content-Type不是可信PDF类型：{mime or '缺失'}。")
    if not pdf_filename_hint(url, headers):
        raise PDFProcessingError("PDF扩展名异常", "URL或Content-Disposition没有可核验的.pdf文件名。")
    if not payload.startswith(PDF_MAGIC):
        raise PDFProcessingError("PDF文件头异常", "响应体不是有效的%PDF文件头。")


def _normalized_line(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _remove_repeated_margins(page_lines: Sequence[list[str]]) -> tuple[list[list[str]], int]:
    if len(page_lines) < 2:
        return [list(lines) for lines in page_lines], 0
    candidates: Counter[str] = Counter()
    original: dict[str, str] = {}
    for lines in page_lines:
        edge_lines = [*lines[:2], *lines[-2:]]
        for line in edge_lines:
            normalized = _normalized_line(line)
            if 2 <= len(normalized) <= 120:
                candidates[normalized] += 1
                original.setdefault(normalized, line)
    threshold = max(2, math.ceil(len(page_lines) * 0.6))
    repeated = {line for line, count in candidates.items() if count >= threshold}
    cleaned: list[list[str]] = []
    removed = 0
    for lines in page_lines:
        kept: list[str] = []
        for line in lines:
            if _normalized_line(line) in repeated:
                removed += 1
            else:
                kept.append(line)
        cleaned.append(kept)
    return cleaned, removed


def _published_date(text: str) -> str:
    match = re.search(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?", text[:8000])
    if not match:
        return ""
    try:
        from datetime import date

        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def _join_pdf_lines(lines: Sequence[str]) -> str:
    """Join visual PDF line wraps while preserving real paragraph/sentence boundaries."""

    output = ""
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if not output:
            output = line
            continue
        previous = output[-1]
        heading_like = len(line) <= 28 and not re.search(r"[。！？!?；;，,：:]$", line)
        if previous in "。！？!?；;" or heading_like:
            output += "\n" + line
        elif previous == "-" and re.match(r"[A-Za-z]", line):
            output = output[:-1] + line
        elif re.match(r"[，。！？；：、,.;:!?）)]", line):
            output += line
        else:
            output += line
    return output


def extract_pdf_text(
    payload: bytes,
    *,
    trusted_title: str = "",
    trusted_published_at: str = "",
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
) -> PDFExtractionResult:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PDFProcessingError("PDF依赖缺失", "未安装pypdf，无法解析文本型PDF。") from exc
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
    except Exception as exc:
        raise PDFProcessingError("PDF解析失败", f"PDF结构无法解析：{type(exc).__name__}。") from exc
    if bool(getattr(reader, "is_encrypted", False)):
        raise PDFProcessingError("加密PDF不支持", "加密PDF不会尝试解密或绕过访问限制。")
    if len(reader.pages) > max(1, int(max_pages)):
        raise PDFProcessingError("PDF页数过多", f"PDF页数超过{max_pages}页安全上限。")
    page_lines: list[list[str]] = []
    for page in reader.pages:
        try:
            extracted = str(page.extract_text() or "")
        except Exception:
            extracted = ""
        lines = [
            re.sub(r"[ \t\u3000]+", " ", line).strip()
            for line in extracted.replace("\r", "\n").splitlines()
        ]
        page_lines.append([line for line in lines if line])
    cleaned_pages, removed = _remove_repeated_margins(page_lines)
    text = "\n\n".join(_join_pdf_lines(lines) for lines in cleaned_pages if lines).strip()
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 30:
        raise PDFProcessingError("扫描PDF或无可提取文本", "未提取到足够文本；本轮不支持OCR或图片型PDF。")
    if len(compact) < 80:
        raise PDFProcessingError(
            "PDF有效文本不足",
            "PDF有效文本不足：仅包含标题、图注或极短说明，无法支持结构化抽取和业务分析。",
        )
    metadata = getattr(reader, "metadata", None)
    metadata_title = str(getattr(metadata, "title", "") or "").strip() if metadata else ""
    first_line = next((line for lines in cleaned_pages for line in lines if 4 <= len(line) <= 200), "")
    title = str(trusted_title or metadata_title or first_line).strip()
    published_at = str(trusted_published_at or _published_date(text)).strip()
    return PDFExtractionResult(
        text=text,
        title=title,
        published_at=published_at,
        page_count=len(reader.pages),
        removed_repeated_lines=removed,
        file_size_bytes=len(payload),
        file_sha256=sha256(payload).hexdigest(),
        quality_note=f"文本型PDF解析成功，共{len(reader.pages)}页；移除重复页眉页脚{removed}行。结果仍需人工核对。",
    )


def find_pdf_links(html: str, base_url: str, selector: str = "", limit: int = 3) -> list[str]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    try:
        nodes = soup.select(selector) if selector else soup.select(
            "a[href$='.pdf'],a[href*='.pdf?'],a[href*='filename='][href*='.pdf'],a[href*='downfile']"
        )
    except Exception:
        return []
    links: list[str] = []
    for node in nodes:
        href = str(node.get("href") or "").strip()
        target = urljoin(base_url, href)
        if target and pdf_filename_hint(target) and target not in links:
            links.append(target)
        if len(links) >= max(1, int(limit)):
            break
    return links
