from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import socket
import time
from typing import Callable, Optional, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from pdf_processor import (
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    PDF_CONTENT_TYPES,
    PDFProcessingError,
    extract_pdf_text,
    pdf_filename_hint,
    validate_pdf_payload,
)
from web_extractor import DEFAULT_MAX_BYTES, DEFAULT_MAX_REDIRECTS, DEFAULT_MAX_TEXT, DEFAULT_TIMEOUT, USER_AGENT, URLSafetyError, extract_html, validate_url


@dataclass
class FetchedContent:
    ok: bool
    requested_url: str
    final_url: str = ""
    canonical_url: str = ""
    title: str = ""
    published_at: str = ""
    publisher: str = ""
    text: str = ""
    raw_html: str = ""
    fetched_at: str = ""
    http_status: int = 0
    content_type: str = ""
    status: str = ""
    note: str = ""
    raw_bytes: bytes = b""
    document_format: str = "html"
    file_size_bytes: int = 0
    file_sha256: str = ""
    pdf_page_count: int = 0
    pdf_removed_repeated_lines: int = 0
    http_etag: str = ""
    http_last_modified: str = ""
    not_modified: bool = False


def host_allowed(url: str, allowed_domains: Sequence[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain.lower().rstrip(".") or host.endswith("." + domain.lower().rstrip(".")) for domain in allowed_domains if domain)


def _decode_body(raw: bytes, response) -> str:
    content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
    header_match = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9._-]+)", content_type, re.I)
    meta_match = re.search(br"charset\s*=\s*['\"]?([a-zA-Z0-9._-]+)", raw[:8192], re.I)
    declared = header_match.group(1) if header_match else ""
    meta = meta_match.group(1).decode("ascii", errors="ignore") if meta_match else ""
    response_encoding = str(getattr(response, "encoding", None) or "")
    try:
        apparent = str(getattr(response, "apparent_encoding", None) or "")
    except RuntimeError:
        # Streaming responses have already been consumed into ``raw``; requests'
        # apparent_encoding property may try to consume them a second time.
        apparent = ""
    # requests may default text/html without charset to ISO-8859-1; prefer the page declaration/apparent detector.
    candidates = [declared, meta]
    if response_encoding.lower() not in {"iso-8859-1", "latin-1"}:
        candidates.append(response_encoding)
    candidates.extend([apparent, "utf-8", "gb18030", response_encoding])
    for encoding in dict.fromkeys(item for item in candidates if item):
        try:
            return raw.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _normalized_date(value: str) -> str:
    match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", str(value or ""))
    if not match:
        return str(value or "").strip()
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date().isoformat()
    except ValueError:
        return ""


class ContentFetcher:
    def __init__(
        self,
        requester=requests,
        resolver: Optional[Callable[..., list[tuple]]] = socket.getaddrinfo,
        sleeper: Callable[[float], None] = time.sleep,
        timeout=DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        retries: int = 2,
    ):
        self.requester = requester
        self.resolver = resolver
        self.sleeper = sleeper
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.retries = max(0, retries)
        self.last_retry_count = 0
        self.last_request_at: dict[str, float] = {}

    def _wait(self, domain: str, rate_limit_seconds: float) -> None:
        minimum = max(2.0, float(rate_limit_seconds or 2.0))
        elapsed = time.monotonic() - self.last_request_at.get(domain, 0.0)
        if elapsed < minimum:
            self.sleeper(minimum - elapsed)
        self.last_request_at[domain] = time.monotonic()

    def fetch(
        self,
        url: str,
        allowed_domains: Sequence[str],
        rate_limit_seconds: float = 2.0,
        extraction_config: Optional[dict[str, object]] = None,
        allow_json: bool = False,
        allow_xml: bool = False,
        allow_pdf: bool = False,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
        max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
        request_headers: Optional[dict[str, str]] = None,
        timeout_override: Optional[tuple[float, float]] = None,
    ) -> FetchedContent:
        requested = str(url or "").strip()
        self.last_retry_count = 0
        try:
            current = validate_url(requested, resolver=self.resolver, resolve_dns=True)
        except URLSafetyError as exc:
            return FetchedContent(False, requested, status="URL被安全规则拒绝", note=str(exc))
        if not host_allowed(current, allowed_domains):
            return FetchedContent(False, requested, status="非白名单域名", note="URL 不属于当前来源的白名单域名。")
        accept = "text/html,application/xhtml+xml;q=0.9"
        if allow_pdf:
            accept += ",application/pdf;q=0.9"
        headers = {
            "User-Agent": USER_AGENT.replace("single-page public-information intake", "whitelist incremental collector"),
            "Accept": accept + ",*/*;q=0.1",
        }
        for key in ("If-None-Match", "If-Modified-Since"):
            value = str((request_headers or {}).get(key) or "").strip()
            if value:
                headers[key] = value
        for attempt in range(self.retries + 1):
            self.last_retry_count = attempt
            response = None
            try:
                for redirects in range(self.max_redirects + 1):
                    domain = urlparse(current).hostname or ""
                    self._wait(domain, rate_limit_seconds)
                    response = self.requester.get(
                        current,
                        headers=headers,
                        timeout=timeout_override or self.timeout,
                        stream=True,
                        allow_redirects=False,
                    )
                    code = int(getattr(response, "status_code", 0) or 0)
                    if code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "")
                        response.close()
                        if not location or redirects >= self.max_redirects:
                            return FetchedContent(False, requested, current, status="重定向过多", note="重定向目标缺失或超过限制。")
                        current = validate_url(urljoin(current, location), resolver=self.resolver, resolve_dns=True)
                        if not host_allowed(current, allowed_domains):
                            return FetchedContent(False, requested, current, status="非白名单域名", note="重定向离开白名单域名，已停止。")
                        continue
                    break
                code = int(getattr(response, "status_code", 0) or 0)
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
                response_etag = str(response.headers.get("ETag", "") or "").strip()
                response_last_modified = str(response.headers.get("Last-Modified", "") or "").strip()
                if code == 304:
                    return FetchedContent(
                        True,
                        requested,
                        current,
                        current,
                        fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        http_status=304,
                        content_type=content_type,
                        status="未变化",
                        note="服务器返回HTTP 304，沿用当前文档版本。",
                        http_etag=response_etag
                        or str((request_headers or {}).get("If-None-Match") or ""),
                        http_last_modified=response_last_modified
                        or str((request_headers or {}).get("If-Modified-Since") or ""),
                        not_modified=True,
                    )
                if code in {401, 403}:
                    return FetchedContent(False, requested, current, http_status=code, content_type=content_type, status="页面拒绝访问", note="不会绕过登录或访问控制。")
                if code >= 400 or code == 0:
                    raise requests.RequestException(f"HTTP {code}")
                is_json = allow_json and (content_type == "application/json" or content_type.endswith("+json"))
                is_xml = allow_xml and content_type in {
                    "application/rss+xml", "application/atom+xml", "application/xml", "text/xml",
                }
                pdf_candidate = content_type in PDF_CONTENT_TYPES or content_type == "application/octet-stream" or pdf_filename_hint(current, response.headers)
                if allow_pdf and pdf_candidate:
                    length = response.headers.get("Content-Length")
                    if length and int(length) > int(max_pdf_bytes):
                        return FetchedContent(
                            False, requested, current, http_status=code, content_type=content_type,
                            status="PDF内容过大", note="PDF超过配置的最大下载体积。",
                            document_format="pdf",
                        )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(chunk_size=65536):
                        total += len(chunk)
                        if total > int(max_pdf_bytes):
                            return FetchedContent(
                                False, requested, current, http_status=code, content_type=content_type,
                                status="PDF内容过大", note="读取达到PDF体积安全上限，已停止。",
                                document_format="pdf",
                            )
                        chunks.append(chunk)
                    raw_pdf = b"".join(chunks)
                    try:
                        validate_pdf_payload(
                            raw_pdf, url=current, content_type=content_type,
                            headers=response.headers, max_bytes=int(max_pdf_bytes),
                        )
                        parsed_pdf = extract_pdf_text(
                            raw_pdf,
                            trusted_title=str((extraction_config or {}).get("trusted_title") or ""),
                            trusted_published_at=str((extraction_config or {}).get("trusted_published_at") or ""),
                            max_pages=int(max_pdf_pages),
                        )
                        fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
                        return FetchedContent(
                            True, requested, current, current, parsed_pdf.title, parsed_pdf.published_at,
                            str((extraction_config or {}).get("trusted_publisher") or ""),
                            parsed_pdf.text, "", fetched_at, code, content_type, "成功", parsed_pdf.quality_note,
                            raw_bytes=raw_pdf, document_format="pdf",
                            file_size_bytes=parsed_pdf.file_size_bytes,
                            file_sha256=parsed_pdf.file_sha256,
                            pdf_page_count=parsed_pdf.page_count,
                            pdf_removed_repeated_lines=parsed_pdf.removed_repeated_lines,
                            http_etag=response_etag,
                            http_last_modified=response_last_modified,
                        )
                    except PDFProcessingError as exc:
                        return FetchedContent(
                            False, requested, current, current,
                            str((extraction_config or {}).get("trusted_title") or ""),
                            str((extraction_config or {}).get("trusted_published_at") or ""),
                            str((extraction_config or {}).get("trusted_publisher") or ""),
                            "", "", datetime.now().astimezone().isoformat(timespec="seconds"),
                            code, content_type, exc.status, str(exc),
                            raw_bytes=raw_pdf, document_format="pdf",
                            file_size_bytes=len(raw_pdf),
                        )
                if content_type not in {"text/html", "application/xhtml+xml"} and not is_json and not is_xml:
                    label = "页面是PDF" if "pdf" in content_type else "非HTML响应"
                    return FetchedContent(False, requested, current, http_status=code, content_type=content_type, status=label, note="自动采集层只处理 HTML。")
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_bytes:
                    return FetchedContent(False, requested, current, http_status=code, content_type=content_type, status="页面内容过大", note="超过响应体安全上限。")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=16384):
                    total += len(chunk)
                    if total > self.max_bytes:
                        return FetchedContent(False, requested, current, http_status=code, content_type=content_type, status="页面内容过大", note="读取达到响应体安全上限，已停止。")
                    chunks.append(chunk)
                html = _decode_body(b"".join(chunks), response)
                if is_json or is_xml:
                    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    return FetchedContent(
                        True, requested, current, current, text=html, raw_html=html,
                        fetched_at=fetched_at, http_status=code, content_type=content_type,
                        status="成功",
                        note=("已通过白名单安全通道获取公开 JSON；字段仍需校验。" if is_json
                              else "已通过白名单安全通道获取公开 RSS/Atom XML；字段仍需校验。"),
                    )
                parsed = extract_html(html, requested, current, code, content_type, max_text=DEFAULT_MAX_TEXT)
                title = parsed.title
                published_at = parsed.published_date
                publisher = parsed.source_name
                text = parsed.text
                ok = parsed.ok
                status = parsed.fetch_status
                note = parsed.quality_note

                # Important official pages often need a stable, user-configured
                # selector. This remains deterministic and never executes page code.
                config = extraction_config or {}
                content_selector = str(config.get("content_selector") or "").strip()
                if content_selector:
                    soup = BeautifulSoup(html, "html.parser")
                    for selector in config.get("exclude_selectors", []) or []:
                        try:
                            for element in soup.select(str(selector)):
                                element.decompose()
                        except Exception:
                            continue
                    try:
                        nodes = soup.select(content_selector)
                    except Exception as exc:
                        return FetchedContent(
                            False, requested, parsed.final_url, parsed.canonical_url or parsed.final_url,
                            title, published_at, publisher, "", html, parsed.fetched_at, code, content_type,
                            "正文选择器错误", f"无法应用 content_selector：{exc}",
                        )
                    selected_lines: list[str] = []
                    for node in nodes:
                        blocks = node.select("p, h1, h2, h3, li")
                        values = [block.get_text(" ", strip=True) for block in blocks] if blocks else [node.get_text(" ", strip=True)]
                        for value in values:
                            value = re.sub(r"\s+", " ", value).strip()
                            if value and value not in selected_lines:
                                selected_lines.append(value)
                    selected_text = "\n".join(selected_lines).strip()
                    if len("".join(selected_text.split())) < 40:
                        fallback_selector = str(config.get("content_fallback_selector") or "").strip()
                        fallback_node = soup.select_one(fallback_selector) if fallback_selector else None
                        if fallback_node is not None:
                            blocks = fallback_node.select("p, h1, h2, h3, li")
                            fallback_values = [
                                re.sub(r"\s+", " ", block.get_text(" ", strip=True)).strip()
                                for block in blocks
                            ] or [re.sub(r"\s+", " ", fallback_node.get_text(" ", strip=True)).strip()]
                            selected_text = "\n".join(
                                value for index, value in enumerate(fallback_values)
                                if value and value not in fallback_values[:index]
                            )
                    selected_text = "\n".join(line.strip() for line in selected_text.splitlines() if line.strip())
                    text = selected_text[:DEFAULT_MAX_TEXT]
                    ok = len("".join(text.split())) >= 40
                    likely_javascript = (
                        not ok
                        and len(soup.select("script[src], script:not([src])")) >= 3
                        and any(keyword in html.casefold() for keyword in ("jquery", "ajax", "$(", "document.ready"))
                    )
                    status = "成功" if ok else ("页面需要JavaScript" if likely_javascript else "未提取到有效正文")
                    note = (
                        "已按数据源正文选择器提取；结果仍需人工核对。"
                        if ok
                        else (
                            "服务器返回的正文容器为空且页面依赖脚本；本项目不会启用浏览器自动化或绕过站点限制。"
                            if likely_javascript
                            else "正文选择器未提取到足够文本，请调整配置或人工处理。"
                        )
                    )

                    def selected_value(key: str) -> str:
                        selector = str(config.get(key) or "").strip()
                        if not selector:
                            return ""
                        try:
                            node = soup.select_one(selector)
                        except Exception:
                            return ""
                        if node is None:
                            return ""
                        return str(node.get("content") or node.get_text(" ", strip=True)).strip()

                    title = selected_value("title_selector") or title
                    published_at = _normalized_date(selected_value("date_selector") or published_at)
                    publisher = selected_value("publisher_selector") or publisher
                return FetchedContent(
                    ok, requested, parsed.final_url, parsed.canonical_url or parsed.final_url, title,
                    published_at, publisher, text, html, parsed.fetched_at, code, content_type, status, note,
                    http_etag=response_etag,
                    http_last_modified=response_last_modified,
                )
            except (requests.Timeout, requests.RequestException, URLSafetyError, ValueError) as exc:
                if attempt >= self.retries:
                    return FetchedContent(False, requested, current, status="网络失败", note=str(exc))
                self.sleeper(min(2 ** attempt, 8))
            finally:
                if response is not None and hasattr(response, "close"):
                    response.close()
        return FetchedContent(False, requested, current, status="网络失败", note="请求未完成。")
