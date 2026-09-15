from __future__ import annotations

from io import BytesIO
from pathlib import Path
import socket

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from business_value import classify_business_value
from commercial_report import ReportOptions, _core_events, _evidence_excerpt
from crawler.content_fetcher import ContentFetcher
from crawler.crawl_manager import CrawlManager
from crawler.robots_checker import RobotsDecision
from document_processor import store_document
from pdf_processor import PDFProcessingError, extract_pdf_text, validate_pdf_payload
from platform_db import connect, initialize_database, upsert_source
from platform_report import ReportEligibilityError, generate_platform_report
from qa.evaluate_real_acceptance import calculate_human_metrics
from workspace_store import create_workspace, workspace_paths


PUBLIC_DNS = lambda *args, **kwargs: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
]


def test_report_evidence_uses_stored_source_quote_not_summary():
    row = {
        "summary": "这是分析摘要，不应冒充原文证据。",
        "analyst_note": "机器抽取结果，需人工确认。证据片段：原文中的短证据｜第二段证据 重复候选：EVT-1",
    }
    excerpt = _evidence_excerpt(row)
    assert "原文中的短证据" in excerpt
    assert "分析摘要" not in excerpt


def _pdf_bytes(text: str = "", *, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 11 Tf 72 760 Td ({safe}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class _Response:
    def __init__(self, payload: bytes, *, content_type="application/pdf", status=200, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = {"Content-Type": content_type, **(headers or {})}

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.payload), chunk_size):
            yield self.payload[start : start + chunk_size]

    def close(self):
        return None


class _Requester:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **kwargs):
        return self.responses[url]


class _AllowRobots:
    def check(self, url, configured_status="未检查"):
        return RobotsDecision(True, "允许")


def _workspace(tmp_path: Path):
    db = tmp_path / "qa.db"
    root = tmp_path / "qa-data"
    workspace = create_workspace({"workspace_name": "PDF QA"}, db, root)
    initialize_database(db)
    source_id = upsert_source(
        {
            "workspace_id": workspace["workspace_id"],
            "source_name": "测试官方来源",
            "organization": "测试官方来源",
            "domain": "example.com",
            "list_page_url": "https://example.com/list",
            "source_type": "政府/监管机构",
            "enabled": True,
            "crawl_allowed": True,
            "robots_status": "允许",
            "terms_status": "允许",
            "commercial_reuse_status": "允许",
            "report_use_allowed": True,
            "license_note": "测试来源已确认允许，仅用于自动测试。",
            "last_license_checked_at": "2026-07-23",
        },
        db,
    )
    return db, root, workspace, source_id


def test_pdf_requires_trusted_content_type_extension_and_magic_header():
    payload = _pdf_bytes("Official public notice 2026-07-20 with enough factual content for extraction.")
    validate_pdf_payload(
        payload,
        url="https://example.com/notice.pdf",
        content_type="application/pdf",
    )
    with pytest.raises(PDFProcessingError, match="Content-Type"):
        validate_pdf_payload(payload, url="https://example.com/notice.pdf", content_type="text/html")
    with pytest.raises(PDFProcessingError, match="pdf文件名"):
        validate_pdf_payload(payload, url="https://example.com/download", content_type="application/pdf")
    with pytest.raises(PDFProcessingError, match="%PDF"):
        validate_pdf_payload(b"<html>not pdf</html>", url="https://example.com/notice.pdf", content_type="application/pdf")


def test_pdf_redirect_is_revalidated_against_whitelist():
    requester = _Requester(
        {
            "https://example.com/notice.pdf": _Response(
                b"", status=302, headers={"Location": "https://evil.test/notice.pdf"}
            )
        }
    )
    result = ContentFetcher(requester=requester, resolver=PUBLIC_DNS, sleeper=lambda _: None).fetch(
        "https://example.com/notice.pdf", ["example.com"], allow_pdf=True
    )
    assert not result.ok
    assert result.status == "非白名单域名"


def test_pdf_size_limit_stops_stream_safely():
    payload = _pdf_bytes("Official public notice 2026-07-20 " * 5)
    result = ContentFetcher(
        requester=_Requester({"https://example.com/notice.pdf": _Response(payload)}),
        resolver=PUBLIC_DNS,
        sleeper=lambda _: None,
    ).fetch("https://example.com/notice.pdf", ["example.com"], allow_pdf=True, max_pdf_bytes=32)
    assert not result.ok
    assert result.status == "PDF内容过大"


def test_encrypted_and_scanned_pdf_are_rejected_without_bypass():
    with pytest.raises(PDFProcessingError, match="加密PDF"):
        extract_pdf_text(_pdf_bytes("Sensitive text", encrypted=True))
    with pytest.raises(PDFProcessingError, match="OCR"):
        extract_pdf_text(_pdf_bytes())
    with pytest.raises(PDFProcessingError, match="有效文本不足"):
        extract_pdf_text(_pdf_bytes("Short public PDF caption 2026-07-20."))


def test_text_pdf_extracts_text_hash_and_metadata():
    payload = _pdf_bytes(
        "Official navigation warning published 2026-07-20. Vessels should verify the public source."
    )
    result = extract_pdf_text(payload, trusted_title="Official navigation warning")
    assert "Vessels should verify" in result.text
    assert result.title == "Official navigation warning"
    assert result.published_at == "2026-07-20"
    assert result.file_size_bytes == len(payload)
    assert len(result.file_sha256) == 64


def test_pdf_hash_deduplicates_and_changed_file_creates_version(tmp_path: Path):
    db, root, workspace, source_id = _workspace(tmp_path)
    first_pdf = _pdf_bytes("Official public PDF version one 2026-07-20 with factual shipping information.")
    second_pdf = _pdf_bytes("Official public PDF version two 2026-07-20 with updated shipping information.")

    def values(payload, text):
        return {
            "workspace_id": workspace["workspace_id"],
            "source_id": source_id,
            "canonical_url": "https://example.com/notice.pdf",
            "original_url": "https://example.com/notice.pdf",
            "source_file_url": "https://example.com/notice.pdf",
            "title": "Official public PDF",
            "publisher": "测试官方来源",
            "published_at": "2026-07-20",
            "raw_bytes": payload,
            "document_format": "pdf",
            "mime_type": "application/pdf",
            "cleaned_text": text,
            "extraction_status": "成功",
            "quality_status": "合格",
            "report_quality_eligible": True,
        }

    first = store_document(values(first_pdf, "version one"), db, root)
    duplicate = store_document(values(first_pdf, "version one"), db, root)
    changed = store_document(values(second_pdf, "version two"), db, root)
    assert first.disposition == "new"
    assert duplicate.disposition == "duplicate_content"
    assert changed.disposition == "updated" and changed.version == 2
    with connect(db) as connection:
        rows = connection.execute(
            "SELECT document_version,is_current,raw_file_path,file_sha256 FROM documents ORDER BY document_version"
        ).fetchall()
    assert [row["document_version"] for row in rows] == [1, 2]
    assert [row["is_current"] for row in rows] == [0, 1]
    assert all(Path(row["raw_file_path"]).suffix == ".pdf" and Path(row["raw_file_path"]).is_file() for row in rows)
    assert all(len(row["file_sha256"]) == 64 for row in rows)


def test_mojibake_pdf_is_archived_but_never_sent_to_ai_or_indexed(tmp_path: Path):
    db, root, workspace, _ = _workspace(tmp_path)
    bad_pdf = _pdf_bytes("Ã" * 50 + " 2026-07-20 public notice with broken encoding.")
    pages = {
        "https://example.com/list": _Response(
            b'<a href="/notice/broken.pdf">Broken encoding PDF</a>',
            content_type="text/html; charset=utf-8",
        ),
        "https://example.com/notice/broken.pdf": _Response(bad_pdf),
    }

    class _NeverAI:
        def post(self, *args, **kwargs):
            raise AssertionError("低质量PDF不得调用DeepSeek")

    result = CrawlManager(
        db,
        root,
        fetcher=ContentFetcher(
            requester=_Requester(pages), resolver=PUBLIC_DNS, sleeper=lambda _: None
        ),
        robots_checker=_AllowRobots(),
        ai_enabled=True,
        ai_requester=_NeverAI(),
    ).run(workspace["workspace_id"])
    assert result.encoding_blocked_count == 1
    assert result.new_event_count == 0
    assert result.api_call_count == 0
    with connect(db) as connection:
        document = connection.execute(
            "SELECT document_format,quality_status,raw_file_path FROM documents"
        ).fetchone()
        chunks = connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
    assert tuple(document[:2]) == ("pdf", "编码异常")
    assert Path(document["raw_file_path"]).is_file()
    assert chunks == 0


def test_low_value_promotion_is_not_a_core_report_event():
    import pandas as pd

    event = {
        "title": "开展党建主题活动并获评荣誉称号",
        "summary": "公司组织党建活动并获得荣誉。",
        "impact": "用于企业文化宣传。",
        "category": "企业动态",
    }
    value = classify_business_value(event)
    assert value.level == "低"
    frame = pd.DataFrame([{**event, "business_value": value.level}])
    assert _core_events(frame).empty


def test_promotion_with_major_risk_phrase_does_not_false_match_gale():
    from risk_engine import calculate_scores

    event = {
        "title": "青年联盟联合攻坚活动",
        "summary": "活动围绕重大风险防控体系开展交流，并举办青年说节目。",
        "impact": "推动青年协作。",
        "category": "企业动态",
        "source_type": "政府/监管机构",
        "status": "新增",
    }
    value = classify_business_value(event)
    scores = calculate_scores(event)
    assert value.level == "低"
    assert "大风" not in scores["risk_terms"]


def test_formal_sample_requires_at_least_one_medium_or_high_value_event(tmp_path: Path):
    db, root, workspace, source_id = _workspace(tmp_path)
    payload = _pdf_bytes("Public company ceremony announcement 2026-07-20 with ordinary publicity content.")
    stored = store_document(
        {
            "workspace_id": workspace["workspace_id"],
            "source_id": source_id,
            "canonical_url": "https://example.com/ceremony.pdf",
            "original_url": "https://example.com/ceremony.pdf",
            "title": "企业荣誉表彰活动",
            "publisher": "测试官方来源",
            "published_at": "2026-07-20",
            "raw_bytes": payload,
            "document_format": "pdf",
            "mime_type": "application/pdf",
            "cleaned_text": "企业开展党建活动并获得荣誉称号，属于普通宣传信息。",
            "extraction_status": "成功",
            "quality_status": "合格",
            "report_quality_eligible": True,
        },
        db,
        root,
    )
    now = "2026-07-20T10:00:00+08:00"
    with connect(db) as connection:
        connection.execute(
            """INSERT INTO events(
            event_id,workspace_id,document_id,event_date,collected_at,category,title,summary,impact,
            source_name,source_type,source_url,status,extraction_method,extraction_confidence,
            ai_generated,human_verified,report_eligible,duplicate_level,business_value,value_reason,
            created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "EVT-LOW", workspace["workspace_id"], stored.document_id, "2026-07-20", now,
                "企业动态", "企业荣誉表彰活动", "企业开展党建活动并获得荣誉称号。",
                "属于企业宣传，未识别到明确业务影响。", "测试官方来源", "政府/监管机构",
                "https://example.com/ceremony.pdf", "新增", "rules", 0.8, 0, 0, 0,
                "未发现明显重复", "低", "党建、荣誉或活动宣传，默认不进入核心风险。",
                now, now,
            ),
        )
        connection.commit()
    with pytest.raises(ReportEligibilityError, match="没有足够高价值内容"):
        generate_platform_report(
            workspace,
            workspace_paths(workspace["workspace_id"], root),
            client=None,
            start_date="2026-07-01",
            end_date="2026-07-31",
            report_title="真实内部研究样刊",
            analyst="PortScope QA",
            options=ReportOptions(report_mode="内部研究版", require_business_value=True),
            db_path=db,
            human_verified_only=False,
            report_mode="内部研究版",
        )


def test_incomplete_human_labels_never_claim_real_accuracy():
    records = [
        {
            "document_id": "DOC-QA",
            "title": "公开航行警告",
            "published_at": "2026-07-20",
            "publisher": "测试官方来源",
            "quality_status": "合格",
            "category": "航行警告",
            "evidence_verified": 1,
        }
    ]
    metrics = calculate_human_metrics(
        records,
        [{"document_id": "DOC-QA", "人工标注状态": "待人工核验"}],
    )
    assert metrics["status"] == "待人工核验"
    assert metrics["title_accuracy"] is None
    assert metrics["completed_count"] == 0


def test_real_acceptance_database_is_isolated_from_formal_database(tmp_path: Path):
    formal_db, formal_root, formal_workspace, formal_source = _workspace(tmp_path / "formal")
    qa_db, qa_root, qa_workspace, qa_source = _workspace(tmp_path / "qa")
    qa_pdf = _pdf_bytes("QA-only official PDF 2026-07-20 with valid public shipping information.")
    store_document(
        {
            "workspace_id": qa_workspace["workspace_id"],
            "source_id": qa_source,
            "canonical_url": "https://example.com/qa-only.pdf",
            "original_url": "https://example.com/qa-only.pdf",
            "title": "QA-only PDF",
            "publisher": "测试官方来源",
            "published_at": "2026-07-20",
            "raw_bytes": qa_pdf,
            "document_format": "pdf",
            "mime_type": "application/pdf",
            "cleaned_text": "QA-only public shipping information.",
            "extraction_status": "成功",
            "quality_status": "合格",
        },
        qa_db,
        qa_root,
    )
    with connect(formal_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    with connect(qa_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert formal_db.resolve() != qa_db.resolve()
    assert formal_root.resolve() != qa_root.resolve()
