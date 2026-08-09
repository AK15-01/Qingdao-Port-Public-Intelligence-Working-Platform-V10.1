from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import ipaddress
import re
import socket
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests


USER_AGENT = "PortScope/2.0 (single-page public-information intake; local review required)"
DEFAULT_TIMEOUT = (4, 12)
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TEXT = 30000
DEFAULT_MAX_REDIRECTS = 3

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data",
}


class URLSafetyError(ValueError):
    pass


@dataclass
class ExtractionResult:
    ok: bool
    requested_url: str
    final_url: str = ""
    canonical_url: str = ""
    title: str = ""
    published_date: str = ""
    source_name: str = ""
    text: str = ""
    description: str = ""
    fetched_at: str = ""
    http_status: str = ""
    content_type: str = ""
    fetch_status: str = ""
    quality_note: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    return not ip.is_global


def validate_url(
    url: str,
    resolver: Optional[Callable[..., list[tuple]]] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> str:
    """Reject non-public targets before any request is sent."""
    value = str(url or "").strip()
    if not value or any(character.isspace() for character in value):
        raise URLSafetyError("URL 为空或包含空白字符。")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise URLSafetyError("只允许 http 或 https URL。")
    if not parsed.hostname:
        raise URLSafetyError("URL 缺少有效主机名。")
    if parsed.username or parsed.password:
        raise URLSafetyError("不允许在 URL 中携带用户名或密码。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLSafetyError("URL 端口格式无效。") from exc
    if port is not None and not (1 <= port <= 65535):
        raise URLSafetyError("URL 端口不在有效范围。")

    hostname = parsed.hostname.rstrip(".").casefold()
    if (
        hostname in BLOCKED_HOSTNAMES
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise URLSafetyError("URL 指向本地、内部或云元数据主机，已拒绝访问。")

    try:
        literal_ip = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise URLSafetyError("URL 指向本地、私有、保留或链路本地 IP，已拒绝访问。")

    if resolve_dns:
        if resolver is None:
            raise URLSafetyError("无法执行 DNS 安全检查。")
        try:
            answers = resolver(hostname, port or (443 if parsed.scheme == "https" else 80))
        except (OSError, socket.gaierror) as exc:
            raise URLSafetyError("域名无法解析，未发起网页请求。") from exc
        addresses = {answer[4][0] for answer in answers if len(answer) >= 5 and answer[4]}
        if not addresses:
            raise URLSafetyError("域名没有可用地址，未发起网页请求。")
        if any(_is_blocked_ip(address) for address in addresses):
            raise URLSafetyError("域名解析到本地、私有、保留或云元数据地址，已拒绝访问。")
    return value


def _meta_content(soup: BeautifulSoup, selectors: list[tuple[str, str]]) -> str:
    for attribute, value in selectors:
        tag = soup.find("meta", attrs={attribute: re.compile(f"^{re.escape(value)}$", re.I)})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def _normalize_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    patterns = [
        r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})",
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return datetime(
                    int(match.group(1)), int(match.group(2)), int(match.group(3))
                ).date().isoformat()
            except ValueError:
                return ""
    return ""


def _clean_text(container, max_text: int) -> tuple[str, bool]:
    for tag in container.find_all(
        ["script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "form"]
    ):
        tag.decompose()
    raw_lines = container.get_text("\n", strip=True).splitlines()
    lines: list[str] = []
    previous = ""
    for raw_line in raw_lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or line == previous:
            continue
        lines.append(line)
        previous = line
    text = "\n".join(lines)
    if len(text) > max_text:
        return text[:max_text].rstrip(), True
    return text, False


def extract_html(
    html: str,
    requested_url: str,
    final_url: Optional[str] = None,
    http_status: int = 200,
    content_type: str = "text/html",
    max_text: int = DEFAULT_MAX_TEXT,
    fetched_at: Optional[str] = None,
) -> ExtractionResult:
    """Extract basic metadata and readable text from one supplied HTML document."""
    fetched_at = fetched_at or datetime.now().astimezone().isoformat(timespec="seconds")
    final_url = final_url or requested_url
    soup = BeautifulSoup(html or "", "html.parser")

    title = _meta_content(soup, [("property", "og:title"), ("name", "twitter:title")])
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else ""
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = re.sub(r"\s+", " ", title).strip()

    description = _meta_content(
        soup,
        [("name", "description"), ("property", "og:description")],
    )
    source_name = _meta_content(
        soup,
        [
            ("property", "og:site_name"),
            ("name", "publisher"),
            ("name", "author"),
        ],
    )
    published_raw = _meta_content(
        soup,
        [
            ("property", "article:published_time"),
            ("name", "publishdate"),
            ("name", "pubdate"),
            ("name", "date"),
        ],
    )
    if not published_raw:
        date_tag = soup.find("time")
        if date_tag:
            published_raw = str(date_tag.get("datetime") or date_tag.get_text(" ", strip=True))
    published_date = _normalize_date(published_raw)

    canonical_url = ""
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        candidate = urljoin(final_url, str(canonical["href"]).strip())
        candidate_parts = urlparse(candidate)
        if candidate_parts.scheme in {"http", "https"} and candidate_parts.netloc:
            canonical_url = candidate

    content = soup.find("article") or soup.find("main") or soup.body or soup
    text, truncated = _clean_text(content, max_text)
    if not published_date:
        published_date = _normalize_date(text[:1000])

    notes: list[str] = []
    if truncated:
        notes.append(f"正文超过 {max_text} 字符，已截断供本地审核")
    if not title:
        notes.append("未可靠提取页面标题")
    if not published_date:
        notes.append("未可靠提取发布日期")
    if not source_name:
        notes.append("未可靠提取发布机构")
    if len(re.sub(r"\s+", "", text)) < 40:
        script_count = len(soup.find_all("script"))
        reason = "页面可能需要 JavaScript，或正文结构无法识别" if script_count else "未提取到有效正文"
        notes.append(reason)
        return ExtractionResult(
            ok=False,
            requested_url=requested_url,
            final_url=final_url,
            canonical_url=canonical_url,
            title=title,
            published_date=published_date,
            source_name=source_name,
            text=text,
            description=description,
            fetched_at=fetched_at,
            http_status=str(http_status),
            content_type=content_type,
            fetch_status="未提取到有效正文",
            quality_note="；".join(notes) + "。请核对原文或使用手工粘贴。",
            truncated=truncated,
        )
    notes.append("自动提取仅供形成草稿，标题、日期、机构和正文必须人工核对")
    return ExtractionResult(
        ok=True,
        requested_url=requested_url,
        final_url=final_url,
        canonical_url=canonical_url,
        title=title,
        published_date=published_date,
        source_name=source_name,
        text=text,
        description=description,
        fetched_at=fetched_at,
        http_status=str(http_status),
        content_type=content_type,
        fetch_status="成功",
        quality_note="；".join(notes) + "。",
        truncated=truncated,
    )


def _failure(
    requested_url: str,
    status: str,
    note: str,
    final_url: str = "",
    http_status: object = "",
    content_type: str = "",
) -> ExtractionResult:
    return ExtractionResult(
        ok=False,
        requested_url=requested_url,
        final_url=final_url,
        fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        http_status=str(http_status or ""),
        content_type=content_type,
        fetch_status=status,
        quality_note=note,
    )


def fetch_url(
    url: str,
    timeout=DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_text: int = DEFAULT_MAX_TEXT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    requester=requests,
    resolver: Optional[Callable[..., list[tuple]]] = socket.getaddrinfo,
) -> ExtractionResult:
    """Fetch exactly one user-submitted URL with SSRF and size protections."""
    requested_url = str(url or "").strip()
    try:
        current_url = validate_url(requested_url, resolver=resolver, resolve_dns=True)
    except URLSafetyError as exc:
        return _failure(requested_url, "URL被安全规则拒绝", str(exc))

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    }
    response = None
    try:
        for redirect_count in range(max_redirects + 1):
            response = requester.get(
                current_url,
                headers=headers,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                if hasattr(response, "close"):
                    response.close()
                if not location:
                    return _failure(
                        requested_url,
                        "HTTP错误",
                        "页面返回重定向但没有提供目标地址。",
                        current_url,
                        status_code,
                    )
                if redirect_count >= max_redirects:
                    return _failure(requested_url, "重定向过多", "页面重定向次数超过安全限制。")
                candidate = urljoin(current_url, location)
                try:
                    current_url = validate_url(candidate, resolver=resolver, resolve_dns=True)
                except URLSafetyError as exc:
                    return _failure(requested_url, "URL被安全规则拒绝", f"重定向目标不安全：{exc}")
                continue
            break

        status_code = int(getattr(response, "status_code", 0) or 0)
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if status_code in {401, 403}:
            return _failure(
                requested_url,
                "页面拒绝访问",
                "页面拒绝访问或需要登录；程序不会绕过权限。",
                current_url,
                status_code,
                content_type,
            )
        if status_code >= 400 or status_code == 0:
            return _failure(
                requested_url,
                "HTTP错误",
                f"页面返回 HTTP {status_code or '未知'}，未提取正文。",
                current_url,
                status_code,
                content_type,
            )
        if content_type == "application/pdf" or content_type.endswith("+pdf"):
            return _failure(
                requested_url, "页面是PDF", "当前版本不解析 PDF 全文，请人工查看后粘贴摘要。",
                current_url, status_code, content_type,
            )
        if content_type.startswith("image/"):
            return _failure(
                requested_url, "页面是图片", "当前版本不识别图片文字，请人工查看后粘贴摘要。",
                current_url, status_code, content_type,
            )
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return _failure(
                requested_url,
                "非HTML响应",
                f"响应类型为 {content_type or '未知'}，当前只提取 HTML 页面。",
                current_url,
                status_code,
                content_type,
            )

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    return _failure(
                        requested_url,
                        "页面内容过大",
                        f"响应体超过 {max_bytes} 字节安全上限，已停止读取。",
                        current_url,
                        status_code,
                        content_type,
                    )
            except ValueError:
                pass

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=16384):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return _failure(
                    requested_url,
                    "页面内容过大",
                    f"响应体超过 {max_bytes} 字节安全上限，已停止读取。",
                    current_url,
                    status_code,
                    content_type,
                )
            chunks.append(chunk)
        encoding = getattr(response, "encoding", None) or "utf-8"
        try:
            html = b"".join(chunks).decode(encoding, errors="replace")
        except LookupError:
            html = b"".join(chunks).decode("utf-8", errors="replace")
        return extract_html(
            html,
            requested_url=requested_url,
            final_url=current_url,
            http_status=status_code,
            content_type=content_type,
            max_text=max_text,
        )
    except requests.Timeout:
        return _failure(requested_url, "页面超时", "页面请求超时；请稍后重试或手工粘贴。", current_url)
    except requests.RequestException as exc:
        return _failure(requested_url, "网络失败", f"网页请求失败：{exc}", current_url)
    except Exception as exc:
        return _failure(requested_url, "网络失败", f"网页提取未完成：{exc}", current_url)
    finally:
        if response is not None and hasattr(response, "close"):
            response.close()

