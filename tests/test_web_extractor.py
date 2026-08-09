import requests

from web_extractor import extract_html, fetch_url, validate_url, URLSafetyError


def public_resolver(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class FakeResponse:
    def __init__(self, body=b"", status=200, content_type="text/html", headers=None):
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size=16384):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class FakeRequester:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def test_non_http_local_private_and_metadata_urls_are_rejected():
    blocked = [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://localhost/test",
        "http://127.0.0.1/test",
        "http://0.0.0.0/test",
        "http://10.0.0.8/test",
        "http://192.168.1.8/test",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1/",
    ]
    for url in blocked:
        try:
            validate_url(url, resolver=public_resolver)
        except URLSafetyError:
            pass
        else:
            raise AssertionError(f"未拒绝危险 URL：{url}")


def test_html_title_date_source_and_body_extraction():
    html = """
    <html><head>
      <title>备用标题</title>
      <meta property="og:title" content="公开公告标题">
      <meta property="og:site_name" content="公开测试机构">
      <meta property="article:published_time" content="2026-07-20T08:00:00+08:00">
      <meta name="description" content="页面描述">
      <link rel="canonical" href="/notice/1">
      <style>hidden</style><script>ignored()</script>
    </head><body><nav>导航内容</nav><article>
      <h1>公开公告标题</h1>
      <p>这是公开公告正文第一段，包含足够长度用于测试正文提取功能。</p>
      <p>这是公开公告正文第二段，脚本、导航和页脚不应进入提取结果。</p>
    </article><footer>页脚</footer></body></html>
    """
    result = extract_html(html, "https://example.com/original")
    assert result.ok
    assert result.title == "公开公告标题"
    assert result.published_date == "2026-07-20"
    assert result.source_name == "公开测试机构"
    assert result.canonical_url == "https://example.com/notice/1"
    assert "正文第一段" in result.text
    assert "导航内容" not in result.text
    assert "ignored" not in result.text


def test_response_too_large_stops_safely():
    response = FakeResponse(body=b"x" * 200)
    requester = FakeRequester(response)
    result = fetch_url(
        "https://example.com/large",
        max_bytes=100,
        requester=requester,
        resolver=public_resolver,
    )
    assert not result.ok
    assert result.fetch_status == "页面内容过大"


def test_hostname_resolving_to_private_ip_is_rejected():
    def private_resolver(host, port):
        return [(2, 1, 6, "", ("10.0.0.9", port))]

    try:
        validate_url("https://apparently-public.example/page", resolver=private_resolver)
    except URLSafetyError:
        pass
    else:
        raise AssertionError("解析到私有 IP 的域名未被拒绝")


def test_non_html_response_has_clear_status():
    response = FakeResponse(body=b"pdf", content_type="application/pdf")
    result = fetch_url(
        "https://example.com/file.pdf",
        requester=FakeRequester(response),
        resolver=public_resolver,
    )
    assert result.fetch_status == "页面是PDF"
    assert "不解析 PDF" in result.quality_note


def test_mocked_html_fetch_and_network_failure_do_not_crash():
    html = (
        "<html><head><title>测试页面</title></head><body><main>"
        "这是一段足够长的公开网页正文，用于验证模拟网络请求可以完成标题和正文提取。"
        "正文还包含第二段测试信息，以确保内容长度达到有效提取条件并可供人工审核。"
        "</main></body></html>"
    ).encode("utf-8")
    success = fetch_url(
        "https://example.com/page",
        requester=FakeRequester(FakeResponse(html)),
        resolver=public_resolver,
    )
    assert success.ok
    assert success.title == "测试页面"

    failed = fetch_url(
        "https://example.com/fail",
        requester=FakeRequester(error=requests.ConnectionError("mock failure")),
        resolver=public_resolver,
    )
    assert not failed.ok
    assert failed.fetch_status == "网络失败"
