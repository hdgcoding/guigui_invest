"""Configuration and utility functions for guigui_invest (AKShare edition).

Adapted from Turtle Investment Framework scripts/config.py:
- No API token required (AKShare is free)
- A-share market only (MVP scope)
"""

from __future__ import annotations

import glob
import os
import re
from typing import Optional


def _load_env_file() -> None:
    """Load .env file from project root if it exists."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    env_path = os.path.normpath(env_path)
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and value and key not in os.environ:
                    os.environ[key] = value


def get_rate_delay() -> float:
    """Get the inter-request delay (seconds) for AKShare calls.

    Read from AKSHARE_RATE_DELAY env var at call time (default 0.5).
    """
    _load_env_file()
    try:
        return float(os.environ.get("AKSHARE_RATE_DELAY", "0.5"))
    except ValueError:
        return 0.5


def get_cache_dir() -> str:
    """Return the TTL cache directory for collector data."""
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(root, "output", ".collector_cache")


def validate_stock_code(code: str) -> str:
    """Validate and normalize a stock code to Tushare-style format.

    Supports (A-share only, MVP scope):
        - 600887.SH, 000858.SZ, 300750.SZ
        - Plain codes: 600887 -> 600887.SH, 000858 -> 000858.SZ

    Returns:
        str: Normalized code (e.g., '600887.SH').

    Raises:
        ValueError: If the code format is not recognized.
    """
    code = str(code).strip().upper()

    if re.match(r"^\d{6}\.(SH|SZ)$", code):
        return code

    # Plain 6-digit A-share code
    if re.match(r"^\d{6}$", code):
        if code.startswith("6"):
            return f"{code}.SH"
        elif code.startswith(("0", "3")):
            return f"{code}.SZ"
        else:
            raise ValueError(
                f"Unrecognized A-share code prefix: {code}. "
                "Expected 6xxxxx (SH), 0xxxxx or 3xxxxx (SZ)."
            )

    raise ValueError(
        f"Unrecognized stock code format: '{code}'. "
        "Expected: 600887.SH, 000858.SZ, or plain 6 digits."
    )


def split_code(ts_code: str) -> tuple[str, str]:
    """Split '600887.SH' -> ('600887', 'sh').

    The second element is the Sina-style prefix ('sh'/'sz') used by
    stock_financial_report_sina.
    """
    num, _, suffix = ts_code.partition(".")
    return num, suffix.lower()


def get_stock_name(code: str) -> Optional[str]:
    """Look up the stock name for a 6-digit code via AKShare code-name table.

    Cached on disk (7-day TTL handled by caller). Returns None on failure.
    """
    import akshare as ak

    try:
        df = ak.stock_info_a_code_name()
        row = df[df["code"] == code]
        if not row.empty:
            return str(row.iloc[0]["name"])
    except Exception:
        pass
    return None


def check_local_pdf(stock_code: str, year: int, search_dir: str = ".",
                    report_type: str = "年报") -> Optional[str]:
    """Check if a report PDF exists locally.

    Args:
        stock_code: Stock code (e.g., '600887' or '600887.SH').
        year: Fiscal year to look for.
        search_dir: Directory to search in.
        report_type: Type of report to search for ('年报' or '中报').

    Returns:
        Path to the PDF if found, None otherwise.
    """
    numeric_code = stock_code.split(".")[0]

    if report_type == "中报":
        patterns = [
            f"*{numeric_code}*{year}*中报*.pdf",
            f"*{numeric_code}*{year}*半年*.pdf",
            f"*{numeric_code}*{year}*interim*.pdf",
            f"*{numeric_code}*{year}*H1*.pdf",
            f"{numeric_code}_{year}_中报.pdf",
        ]
    else:
        patterns = [
            f"*{numeric_code}*{year}*年报*.pdf",
            f"*{numeric_code}*{year}*.pdf",
            f"{numeric_code}_{year}_*.pdf",
        ]

    for pattern in patterns:
        matches = glob.glob(os.path.join(search_dir, pattern))
        if matches:
            return matches[0]

    return None


def validate_pdf(filepath: str) -> "tuple[bool, str]":
    """Validate that a file is a real PDF.

    Args:
        filepath: Path to the file.

    Returns:
        Tuple of (is_valid, reason).
    """
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}"

    size = os.path.getsize(filepath)
    if size < 100 * 1024:  # 100KB minimum
        return False, f"File too small ({size} bytes), likely not a real annual report"

    with open(filepath, "rb") as f:
        magic = f.read(20)
        if b"%PDF-" not in magic:
            return False, "File does not start with %PDF- magic bytes"

    return True, "Valid PDF"
