#!/usr/bin/env python3
"""
财报PDF下载工具 (Financial Report PDF Downloader)

从 stockn.xueqiu.com、notice.10jqka.com.cn 或 cninfo.com.cn 下载A股/港股财报PDF文件。
支持年报、中报、一季报、三季报。

下载成功必须同时满足：HTTP 2xx、真实字节数超过最小体积、内容以 ``%PDF-`` 开头，
且（有 Content-Length 时）实际字节数与 Content-Length 一致。空 body / 0 字节 /
HTML 错误页 / 截断下载都会失败并清理临时文件，不会留下 0 字节的“成功”产物。

Usage:
    python3 scripts/download_report.py \
        --url "https://stockn.xueqiu.com/.../report.pdf?download=1" \
        --stock-code SH600887 \
        --report-type 年报 \
        --year 2024 \
        --save-dir .
"""

import argparse
import os
import re
import sys
import time
from urllib.parse import urlsplit

import requests

# Exit codes
EXIT_SUCCESS = 0
EXIT_NETWORK_FAILURE = 1
EXIT_PDF_VALIDATION_FAILURE = 2
EXIT_BAD_ARGUMENTS = 3

# Constants
PDF_MAGIC_BYTES = b"%PDF-"
MIN_VALID_FILE_SIZE = 1024  # 1KB: below this a "PDF" cannot be a real report
MIN_FILE_SIZE_WARNING = 100 * 1024  # 100KB
DOWNLOAD_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE = 3  # seconds

# Allowed source hosts (matched as exact host or dot-suffixed subdomain).
ALLOWED_PDF_HOSTS = (
    "stockn.xueqiu.com",
    "10jqka.com.cn",
    "cninfo.com.cn",
)

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def get_headers(url):
    """Return headers with Referer matching the URL domain."""
    headers = dict(BASE_HEADERS)
    if "cninfo.com.cn" in url:
        headers["Referer"] = "https://www.cninfo.com.cn/"
    elif "10jqka.com.cn" in url:
        headers["Referer"] = "https://10jqka.com.cn/"
    else:
        headers["Referer"] = "https://xueqiu.com/"
    return headers


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Download financial report PDF from cninfo.com.cn, stockn.xueqiu.com, or 10jqka.com.cn"
    )
    parser.add_argument(
        "--url", required=True, help="PDF URL from cninfo.com.cn, stockn.xueqiu.com, or 10jqka.com.cn"
    )
    parser.add_argument(
        "--stock-code", required=True, help="Stock code (e.g. SH600887, 00700)"
    )
    parser.add_argument(
        "--report-type",
        required=True,
        help="Report type (年报/中报/一季报/三季报/annual/interim)",
    )
    parser.add_argument(
        "--year", required=True, help="Report year (e.g. 2024)"
    )
    parser.add_argument(
        "--save-dir", default=".", help="Directory to save the PDF (default: .)"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Max download retries (default: {DEFAULT_MAX_RETRIES})",
    )
    return parser.parse_args(argv)


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    if not host:
        return False
    for allowed in ALLOWED_PDF_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def validate_url(url):
    """Validate that the URL points to a supported source and a .pdf path.

    The ``.pdf`` suffix is checked on the parsed URL *path*, so real links such
    as ``https://stockn.xueqiu.com/x/report.pdf?download=1`` are accepted while
    ``report.php?file=report.pdf`` is rejected.
    """
    if not isinstance(url, str) or not url.strip():
        return False, "Invalid URL: URL is empty"
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        return False, f"Invalid URL: {exc}"

    if parts.scheme.lower() not in ("http", "https"):
        return False, f"Invalid URL: unsupported scheme '{parts.scheme or '(missing)'}'"
    if parts.username or parts.password:
        return False, "Invalid URL: credentials in URL are not allowed"
    host = parts.hostname or ""
    if not _host_allowed(host):
        return False, (
            f"Invalid URL: unsupported host '{host}'\n"
            "URL must come from cninfo.com.cn, stockn.xueqiu.com, or 10jqka.com.cn"
        )
    if not (parts.path or "").lower().endswith(".pdf"):
        return False, (
            f"Invalid URL: path does not end with .pdf: {parts.path or '(empty)'}\n"
            "URL must point to a .pdf file (query string is allowed)"
        )
    return True, ""


def _safe_path_component(value, field_name):
    """Reject values that could escape the save directory when used in a filename."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"Invalid {field_name}: must not be empty")
    if os.path.isabs(text):
        raise ValueError(f"Invalid {field_name}: absolute paths are not allowed: {text!r}")
    if "/" in text or "\\" in text:
        raise ValueError(f"Invalid {field_name}: path separators are not allowed: {text!r}")
    if text in (".", ".."):
        raise ValueError(f"Invalid {field_name}: {text!r} is not a valid path component")
    if any(ord(char) < 32 or char == "\x7f" for char in text):
        raise ValueError(f"Invalid {field_name}: control characters are not allowed: {text!r}")
    return text


def validate_output_filename(filename):
    """Ensure *filename* is a plain basename with no traversal components."""
    text = str(filename or "")
    if not text:
        raise ValueError("Invalid output filename: must not be empty")
    if os.path.basename(text) != text:
        raise ValueError(f"Invalid output filename: must be a basename: {text!r}")
    if text in (".", ".."):
        raise ValueError(f"Invalid output filename: {text!r}")
    return text


def build_filename(stock_code, report_type, year):
    """Build output filename: {code}_{year}_{report_type}.pdf

    Strips SH/SZ prefix from stock_code to match coordinator.md convention
    (e.g. 600887_2024_年报.pdf). Raises ValueError for values that could
    traverse outside the target directory.
    """
    # Normalize report type
    type_map = {
        "annual": "年报",
        "interim": "中报",
        "q1": "一季报",
        "q3": "三季报",
    }
    code = _safe_path_component(stock_code, "stock-code")
    year_text = _safe_path_component(year, "year")
    report_type_text = _safe_path_component(report_type, "report-type")
    normalized = type_map.get(report_type_text.lower(), report_type_text)
    # Strip exchange prefix for filename
    code = re.sub(r"^(SH|SZ)", "", code, flags=re.IGNORECASE)
    if not code:
        raise ValueError("Invalid stock-code: empty after stripping exchange prefix")
    filename = f"{code}_{year_text}_{normalized}.pdf"
    return validate_output_filename(filename)


def _remove_quietly(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:  # pragma: no cover - best effort cleanup
        print(f"Warning: could not remove {path}: {exc}", file=sys.stderr)


def _expected_content_length(response):
    """Return Content-Length when it is safe to compare against downloaded bytes."""
    header = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if header is None:
        return None
    encoding = (response.headers.get("Content-Encoding") or "").lower().strip()
    if encoding not in ("", "identity"):
        # requests transparently decompresses gzip/br bodies; header then refers
        # to the compressed size and must not be compared with decoded bytes.
        return None
    try:
        expected = int(str(header).strip())
    except (TypeError, ValueError):
        return None
    return expected if expected >= 0 else None


def download_annual_report(url, save_path, max_retries=DEFAULT_MAX_RETRIES):
    """
    Download PDF with retry and strict validation.

    Returns:
        tuple: (success: bool, message: str, filesize: int)
    """
    last_error = None
    save_path = str(save_path)
    tmp_path = save_path + ".tmp"

    parent_dir = os.path.dirname(os.path.abspath(save_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        try:
            print(
                f"Downloading (attempt {attempt}/{max_retries}): {url}",
                file=sys.stderr,
            )

            response = requests.get(
                url,
                headers=get_headers(url),
                timeout=DOWNLOAD_TIMEOUT,
                stream=True,
            )
            response.raise_for_status()

            # Check Content-Type (warning only; magic bytes + size are authoritative)
            content_type = response.headers.get("Content-Type", "")
            if "pdf" not in content_type.lower() and "octet-stream" not in content_type.lower():
                print(
                    f"Warning: Content-Type is '{content_type}', expected PDF",
                    file=sys.stderr,
                )

            expected_size = _expected_content_length(response)
            total_size = 0
            first_chunk = True
            failure_message = ""

            with open(tmp_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    # Validate PDF magic bytes on first chunk
                    if first_chunk:
                        first_chunk = False
                        if not bytes(chunk[: len(PDF_MAGIC_BYTES)]).startswith(PDF_MAGIC_BYTES):
                            failure_message = (
                                "PDF validation failed: file does not start with "
                                "%PDF- magic bytes"
                            )
                            break
                    handle.write(chunk)
                    total_size += len(chunk)

            if failure_message:
                _remove_quietly(tmp_path)
                return False, failure_message, 0

            # Empty body / 0-byte "successful" response must not be treated as success
            if total_size == 0:
                _remove_quietly(tmp_path)
                return (
                    False,
                    "PDF validation failed: empty response body (0 bytes downloaded)",
                    0,
                )

            if total_size < MIN_VALID_FILE_SIZE:
                _remove_quietly(tmp_path)
                return (
                    False,
                    "PDF validation failed: file too small "
                    f"({total_size} bytes < {MIN_VALID_FILE_SIZE} bytes minimum)",
                    0,
                )

            if expected_size is not None and total_size != expected_size:
                _remove_quietly(tmp_path)
                return (
                    False,
                    "PDF validation failed: size mismatch, Content-Length says "
                    f"{expected_size} bytes but downloaded {total_size} bytes",
                    0,
                )

            # Replace atomically only after all validation passed
            if os.path.exists(save_path):
                os.remove(save_path)
            os.replace(tmp_path, save_path)

            # Size warning (still a valid download)
            if total_size < MIN_FILE_SIZE_WARNING:
                print(
                    f"Warning: file size ({total_size} bytes) is smaller than expected (<100KB)",
                    file=sys.stderr,
                )

            return True, "Download successful", total_size

        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            print(f"Attempt {attempt} failed: {last_error}", file=sys.stderr)
            _remove_quietly(tmp_path)

            if attempt < max_retries:
                wait_time = BACKOFF_BASE * attempt  # 3s, 6s, 9s
                print(f"Retrying in {wait_time}s...", file=sys.stderr)
                time.sleep(wait_time)
        except OSError as exc:
            last_error = str(exc)
            print(f"Attempt {attempt} failed while writing file: {last_error}", file=sys.stderr)
            _remove_quietly(tmp_path)
            if attempt < max_retries:
                time.sleep(BACKOFF_BASE * attempt)

    return False, f"Download failed after {max_retries} attempts: {last_error}", 0


def print_result(success, filepath="", filesize=0, url="", stock_code="",
                 report_type="", year="", message=""):
    """Print structured result block for the agent to parse."""
    status = "SUCCESS" if success else "FAILED"
    print("\n---RESULT---")
    print(f"status: {status}")
    print(f"filepath: {filepath}")
    print(f"filesize: {filesize}")
    print(f"url: {url}")
    print(f"stock_code: {stock_code}")
    print(f"report_type: {report_type}")
    print(f"year: {year}")
    print(f"message: {message}")
    print("---END---")


def _exit_code_for_message(message):
    lowered = (message or "").lower()
    if "validation" in lowered or "mismatch" in lowered:
        return EXIT_PDF_VALIDATION_FAILURE
    return EXIT_NETWORK_FAILURE


def main(argv=None):
    args = parse_args(argv)

    # Validate URL
    valid, err_msg = validate_url(args.url)
    if not valid:
        print(f"Error: {err_msg}", file=sys.stderr)
        print_result(
            success=False,
            url=args.url,
            stock_code=args.stock_code,
            report_type=args.report_type,
            year=args.year,
            message=err_msg,
        )
        sys.exit(EXIT_BAD_ARGUMENTS)

    # Build and validate filename BEFORE creating directories (path traversal guard)
    try:
        filename = build_filename(args.stock_code, args.report_type, args.year)
    except ValueError as exc:
        err_msg = str(exc)
        print(f"Error: {err_msg}", file=sys.stderr)
        print_result(
            success=False,
            url=args.url,
            stock_code=args.stock_code,
            report_type=args.report_type,
            year=args.year,
            message=err_msg,
        )
        sys.exit(EXIT_BAD_ARGUMENTS)

    save_dir = os.path.abspath(args.save_dir)
    save_path = os.path.join(save_dir, filename)
    if os.path.dirname(os.path.abspath(save_path)) != save_dir:
        err_msg = f"Refusing to write outside save-dir: {save_path}"
        print(f"Error: {err_msg}", file=sys.stderr)
        print_result(
            success=False,
            url=args.url,
            stock_code=args.stock_code,
            report_type=args.report_type,
            year=args.year,
            message=err_msg,
        )
        sys.exit(EXIT_BAD_ARGUMENTS)

    # Ensure save directory exists
    try:
        os.makedirs(save_dir, exist_ok=True)
    except OSError as exc:
        err_msg = f"Cannot create save directory {save_dir}: {exc}"
        print(f"Error: {err_msg}", file=sys.stderr)
        print_result(
            success=False,
            url=args.url,
            stock_code=args.stock_code,
            report_type=args.report_type,
            year=args.year,
            message=err_msg,
        )
        sys.exit(EXIT_BAD_ARGUMENTS)

    # Download
    success, message, filesize = download_annual_report(
        url=args.url,
        save_path=save_path,
        max_retries=args.max_retries,
    )

    # Print result
    print_result(
        success=success,
        filepath=os.path.abspath(save_path) if success else "",
        filesize=filesize,
        url=args.url,
        stock_code=args.stock_code,
        report_type=args.report_type,
        year=args.year,
        message=message,
    )

    if not success:
        sys.exit(_exit_code_for_message(message))

    sys.exit(EXIT_SUCCESS)


if __name__ == "__main__":
    main()
