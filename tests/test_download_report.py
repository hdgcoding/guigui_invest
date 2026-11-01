"""Tests for scripts/download_report.py.

All network access is mocked (``requests.get`` monkeypatched); these tests never
touch the network.

Covers:
- URL 校验：路径以 .pdf 结尾（允许 ?download=1 等 query），拒绝非 PDF 路径/非法 host；
- 文件名安全：--stock-code/--report-type/--year 不得路径穿越（basename + 校验）；
- 下载字节校验：空 body / 0 字节 / HTML 错误页 / 过小文件 / Content-Length 不符
  均视为失败并清理临时文件；成功的小 PDF 需 magic + 最小体积校验通过；
- main() 失败返回非 0，成功返回 0，且路径穿越参数被拒绝。
"""

import pytest
import requests

import download_report
from download_report import (
    EXIT_BAD_ARGUMENTS,
    EXIT_NETWORK_FAILURE,
    EXIT_PDF_VALIDATION_FAILURE,
    MIN_VALID_FILE_SIZE,
    build_filename,
    download_annual_report,
    main,
    validate_url,
)

VALID_URL = "https://stockn.xueqiu.com/reports/600001_2024.pdf"
PDF_BODY = b"%PDF-1.4\n" + b"0" * (MIN_VALID_FILE_SIZE + 64)


class FakeResponse:
    """Minimal requests.Response stand-in (supports stream=True + iter_content)."""

    def __init__(self, chunks, headers=None, raise_exc=None, iter_exc=None):
        self._chunks = list(chunks)
        self.headers = dict(headers or {})
        self._raise_exc = raise_exc
        self._iter_exc = iter_exc
        self.status_code = 200

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def iter_content(self, chunk_size=8192, decode_content=True):
        for chunk in self._chunks:
            if self._iter_exc:
                raise self._iter_exc
            yield chunk


def _install_response(monkeypatch, response):
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return response

    monkeypatch.setattr(download_report.requests, "get", fake_get)
    monkeypatch.setattr(download_report.time, "sleep", lambda *_: None)
    return calls


def _install_request_error(monkeypatch, exc):
    def fake_get(url, **kwargs):
        raise exc

    monkeypatch.setattr(download_report.requests, "get", fake_get)
    monkeypatch.setattr(download_report.time, "sleep", lambda *_: None)


def _run_main(argv):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    return excinfo.value.code


def _assert_no_download_artifacts(tmp_path, filename):
    assert not (tmp_path / filename).exists()
    assert not (tmp_path / (filename + ".tmp")).exists()


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://stockn.xueqiu.com/reports/report.pdf",
    "https://stockn.xueqiu.com/reports/report.pdf?download=1",
    "http://stockn.xueqiu.com/2024/report.PDF?x=1",
    "https://notice.10jqka.com.cn/api/stockpdf/x/report.pdf?f=1",
    "https://www.cninfo.com.cn/new/announcement/report.pdf",
])
def test_validate_url_accepts_pdf_path_with_query(url):
    ok, message = validate_url(url)
    assert ok is True, message


@pytest.mark.parametrize("url", [
    "https://stockn.xueqiu.com/download.php?file=report.pdf",  # query 不是 path
    "https://stockn.xueqiu.com/reports/report.pdf.exe",
    "https://stockn.xueqiu.com/reports/report",
    "https://evil.example/report.pdf",
    "https://evil10jqka.com.cn/report.pdf",  # 不是 10jqka.com.cn 的子域
    "ftp://stockn.xueqiu.com/report.pdf",
    "https://user:pass@stockn.xueqiu.com/report.pdf",
    "",
])
def test_validate_url_rejects_invalid_urls(url):
    ok, _ = validate_url(url)
    assert ok is False


# ---------------------------------------------------------------------------
# Filename / path traversal
# ---------------------------------------------------------------------------

def test_build_filename_normalizes_stock_code():
    assert build_filename("SH600887", "年报", "2024") == "600887_2024_年报.pdf"
    assert build_filename("SZ000858", "annual", "2024") == "000858_2024_年报.pdf"


@pytest.mark.parametrize("stock_code", [
    "../../etc/passwd",
    "/etc/passwd",
    "..\\..\\windows",
    "SH/../../x",
    "..",
    "",
])
def test_build_filename_rejects_path_traversal(stock_code):
    with pytest.raises(ValueError):
        build_filename(stock_code, "年报", "2024")


@pytest.mark.parametrize("year,report_type", [
    ("../2024", "年报"),
    ("2024/01", "年报"),
    ("2024", "../../x"),
    ("", "年报"),
])
def test_build_filename_rejects_bad_year_or_report_type(year, report_type):
    with pytest.raises(ValueError):
        build_filename("600887", report_type, year)


# ---------------------------------------------------------------------------
# Download byte validation
# ---------------------------------------------------------------------------

def test_empty_body_fails_without_leaving_file(monkeypatch, tmp_path):
    _install_response(monkeypatch, FakeResponse(
        [], headers={"Content-Type": "application/pdf"}
    ))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, message, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is False
    assert size == 0
    assert "validation failed" in message.lower()
    _assert_no_download_artifacts(tmp_path, "600001_2024_年报.pdf")


def test_zero_byte_chunk_fails(monkeypatch, tmp_path):
    _install_response(monkeypatch, FakeResponse(
        [b""], headers={"Content-Type": "application/pdf"}
    ))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, message, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is False and size == 0
    assert "empty response body" in message.lower()
    _assert_no_download_artifacts(tmp_path, "600001_2024_年报.pdf")


def test_html_error_page_rejected(monkeypatch, tmp_path):
    _install_response(monkeypatch, FakeResponse(
        [b"<!DOCTYPE html><html><body>error</body></html>"],
        headers={"Content-Type": "text/html"},
    ))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, message, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is False and size == 0
    assert "magic bytes" in message.lower()
    _assert_no_download_artifacts(tmp_path, "600001_2024_年报.pdf")


def test_pdf_below_min_size_rejected(monkeypatch, tmp_path):
    _install_response(monkeypatch, FakeResponse(
        [b"%PDF-1.4\ntruncated"], headers={"Content-Type": "application/pdf"}
    ))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, message, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is False and size == 0
    assert "too small" in message.lower()
    _assert_no_download_artifacts(tmp_path, "600001_2024_年报.pdf")


def test_content_length_mismatch_rejected(monkeypatch, tmp_path):
    _install_response(monkeypatch, FakeResponse(
        [PDF_BODY],
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(len(PDF_BODY) + 128),
        },
    ))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, message, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is False and size == 0
    assert "mismatch" in message.lower()
    _assert_no_download_artifacts(tmp_path, "600001_2024_年报.pdf")


def test_content_length_ignored_for_compressed_body(monkeypatch, tmp_path):
    """gzip 响应由 requests 解压，Content-Length 是压缩后大小，不应误判失败。"""
    _install_response(monkeypatch, FakeResponse(
        [PDF_BODY],
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": "10",
            "Content-Encoding": "gzip",
        },
    ))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, _, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is True
    assert size == len(PDF_BODY)


def test_valid_small_pdf_succeeds(monkeypatch, tmp_path):
    calls = _install_response(monkeypatch, FakeResponse(
        [PDF_BODY[:100], PDF_BODY[100:]],
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(len(PDF_BODY)),
        },
    ))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, message, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is True, message
    assert size == len(PDF_BODY)
    assert (tmp_path / "600001_2024_年报.pdf").read_bytes() == PDF_BODY
    _assert_no_download_artifacts(tmp_path, "600001_2024_年报.pdf.tmp")
    assert calls and calls[0]["kwargs"].get("stream") is True


def test_network_error_returns_failure(monkeypatch, tmp_path):
    _install_request_error(monkeypatch, requests.exceptions.ConnectionError("boom"))
    save_path = str(tmp_path / "600001_2024_年报.pdf")

    ok, message, size = download_annual_report(VALID_URL, save_path, max_retries=1)

    assert ok is False and size == 0
    assert "failed" in message.lower()
    _assert_no_download_artifacts(tmp_path, "600001_2024_年报.pdf")


# ---------------------------------------------------------------------------
# main() exit codes (no network)
# ---------------------------------------------------------------------------

def test_main_success_exit_zero_with_query_url(monkeypatch, tmp_path):
    _install_response(monkeypatch, FakeResponse(
        [PDF_BODY], headers={"Content-Type": "application/pdf"}
    ))

    code = _run_main([
        "--url", VALID_URL + "?download=1",
        "--stock-code", "SH600001",
        "--report-type", "年报",
        "--year", "2024",
        "--save-dir", str(tmp_path),
        "--max-retries", "1",
    ])

    assert code == 0
    saved = tmp_path / "600001_2024_年报.pdf"
    assert saved.read_bytes() == PDF_BODY
    assert not (tmp_path / "600001_2024_年报.pdf.tmp").exists()


def test_main_empty_body_exits_nonzero(monkeypatch, tmp_path):
    _install_response(monkeypatch, FakeResponse([], headers={"Content-Type": "application/pdf"}))

    code = _run_main([
        "--url", VALID_URL,
        "--stock-code", "SH600001",
        "--report-type", "年报",
        "--year", "2024",
        "--save-dir", str(tmp_path),
        "--max-retries", "1",
    ])

    assert code == EXIT_PDF_VALIDATION_FAILURE
    assert code != 0
    assert list(tmp_path.iterdir()) == []


def test_main_network_failure_exits_nonzero(monkeypatch, tmp_path):
    _install_request_error(monkeypatch, requests.exceptions.Timeout("timeout"))

    code = _run_main([
        "--url", VALID_URL,
        "--stock-code", "SH600001",
        "--report-type", "年报",
        "--year", "2024",
        "--save-dir", str(tmp_path),
        "--max-retries", "1",
    ])

    assert code == EXIT_NETWORK_FAILURE
    assert code != 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stock_code", ["../../etc", "/tmp/evil", "..\\..\\evil"])
def test_main_rejects_path_traversal_stock_code(monkeypatch, tmp_path, stock_code):
    # requests.get must never be called for rejected arguments
    calls = _install_response(monkeypatch, FakeResponse([PDF_BODY]))

    code = _run_main([
        "--url", VALID_URL,
        "--stock-code", stock_code,
        "--report-type", "年报",
        "--year", "2024",
        "--save-dir", str(tmp_path),
    ])

    assert code == EXIT_BAD_ARGUMENTS
    assert calls == []
    assert not any(tmp_path.iterdir())


def test_main_rejects_pdf_only_in_query(monkeypatch, tmp_path):
    calls = _install_response(monkeypatch, FakeResponse([PDF_BODY]))

    code = _run_main([
        "--url", "https://stockn.xueqiu.com/download.php?file=report.pdf",
        "--stock-code", "SH600001",
        "--report-type", "年报",
        "--year", "2024",
        "--save-dir", str(tmp_path),
    ])

    assert code == EXIT_BAD_ARGUMENTS
    assert calls == []
