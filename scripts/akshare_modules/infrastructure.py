"""guigui_invest - InfrastructureMixin.

Base utilities for the AKShare collector: rate limiting, TTL disk cache,
store helpers, display-period selection. The helper surface is kept
compatible with the original Tushare-based framework so that
DerivedMetricsMixin / ValuationEngine work unchanged.
"""

from __future__ import annotations

import functools
import os
import time

import pandas as pd

from cache_utils import ScreenerCache
from config import get_cache_dir, get_rate_delay
from format_utils import format_number

from .constants import CACHE_TTL_CATEGORY, CACHE_TTL_HOURS


def rate_limit(func):
    """Decorator enforcing a delay between AKShare calls.

    Delay (seconds) read from AKSHARE_RATE_DELAY env var at call time
    (default 0.5). Cache hits do NOT sleep (handled in _cached_call).
    ``time.sleep`` is called through the module-level ``time`` import so
    tests can patch it.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        delay = get_rate_delay()
        if delay > 0:
            time.sleep(delay)
        return func(*args, **kwargs)
    return wrapper


class InfrastructureMixin:
    """Infrastructure utilities for AKShareClient."""

    _currency = "CNY"
    _fy_end_month = 12

    def __init_store__(self):
        self._store: dict = {}
        self._cache = ScreenerCache(get_cache_dir())
        self._no_cache = False

    # ------------------------------------------------------------------
    # Market detection (A-share only; kept for downstream compatibility)
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_currency(ts_code: str) -> str:
        return "CNY"

    @staticmethod
    def _is_hk(ts_code: str) -> bool:
        return False

    @staticmethod
    def _is_us(ts_code: str) -> bool:
        return False

    def _unit_label(self) -> str:
        return "百万元"

    def _price_unit(self) -> str:
        return "元"

    # ------------------------------------------------------------------
    # Cached AKShare invocation
    # ------------------------------------------------------------------

    def _cached_call(self, endpoint: str, fetch, key: str | None = None) -> pd.DataFrame:
        """Fetch a DataFrame through the TTL disk cache.

        Args:
            endpoint: logical endpoint name (must exist in CACHE_TTL_CATEGORY
                to be cached; unknown endpoints are never cached).
            fetch: zero-arg callable returning the DataFrame.
            key: explicit cache key (defaults to endpoint). Callers fetching
                per-stock data MUST pass a per-stock key.
        """
        category = CACHE_TTL_CATEGORY.get(endpoint)
        if self._no_cache or category is None:
            return self._fetch_with_retry(fetch)

        cache_key = key or endpoint
        ttl = CACHE_TTL_HOURS[category] * 3600
        cached = self._cache.get(cache_key, ttl)
        if cached is not None:
            return cached

        df = self._fetch_with_retry(fetch)
        if df is not None:
            self._cache.put(cache_key, df)
        return df if df is not None else pd.DataFrame()

    def _fetch_with_retry(self, fetch, retries: int = 2) -> pd.DataFrame | None:
        """Call fetch() with rate limiting and exponential-backoff retry.

        On proxy-related failures (local proxy flakiness with eastmoney),
        retries once with proxy env vars cleared (direct connection).
        """
        import akshare as ak  # noqa: F401  (ensure import exists for patching)

        last_err = None
        direct_mode = False
        for attempt in range(retries + 1):
            rate_limit(lambda: None)
            restore = None
            try:
                if direct_mode:
                    restore = self._clear_proxy_env()
                df = fetch()
                return df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
            except Exception as e:  # noqa: BLE001 — best-effort network call
                last_err = e
                if "ProxyError" in type(e).__name__ or "proxy" in str(e).lower():
                    direct_mode = True  # next attempt bypasses proxy
                if attempt < retries:
                    time.sleep(1.5 * (2 ** attempt))
            finally:
                if restore is not None:
                    self._restore_proxy_env(restore)
        print(f"  ⚠️ AKShare 接口调用失败: {last_err}")
        return None

    @staticmethod
    def _clear_proxy_env() -> dict[str, str | None]:
        """Temporarily clear proxy env vars; return the saved state."""
        saved: dict[str, str | None] = {}
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            if var in os.environ:
                saved[var] = os.environ.pop(var)
        return saved

    @staticmethod
    def _restore_proxy_env(saved: dict[str, str | None]) -> None:
        """Restore proxy env vars cleared by _clear_proxy_env."""
        for var, val in saved.items():
            if val is not None:
                os.environ[var] = val

    # ------------------------------------------------------------------
    # Display-period helpers (ported from original infrastructure.py)
    # ------------------------------------------------------------------

    def _prepare_display_periods(self, df, max_annual=5):
        """Select up to max_annual annual reports + any newer interim reports.

        Returns (display_df, column_labels) where column_labels are like:
        ["2025Q3", "2024", "2023", ...]
        """
        if df.empty:
            return df, []

        df = df.drop_duplicates(subset=["end_date"])

        fy_month_str = f"{self._fy_end_month:02d}"

        annual = df[df["end_date"].str[4:6] == fy_month_str].copy()
        non_annual = df[df["end_date"].str[4:6] != fy_month_str].copy()

        annual = annual.sort_values("end_date", ascending=False).head(max_annual)

        latest_annual_date = annual["end_date"].max() if not annual.empty else "00000000"

        interim = non_annual[non_annual["end_date"] > latest_annual_date].copy()
        interim = interim.sort_values("end_date", ascending=False)

        fy_month = self._fy_end_month

        def _label(end_date):
            mm = end_date[4:6]
            mmdd = end_date[4:]
            year = end_date[:4]
            if mm == fy_month_str:
                return year
            elif mmdd == "0630":
                return f"{year}H1"
            elif mmdd == "0331":
                return f"{year}Q1"
            elif mmdd == "0930":
                return f"{year}Q3"
            else:
                return f"{year}_{mmdd}"

        display_df = pd.concat([interim, annual], ignore_index=True)
        if display_df.empty:
            return display_df, []

        labels = [_label(d) for d in display_df["end_date"]]
        return display_df, labels

    # ------------------------------------------------------------------
    # Numeric helpers (contract-compatible with original)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_float(val) -> float | None:
        """Convert a value to float, returning None for NaN/None."""
        if val is None:
            return None
        try:
            f = float(val)
            return None if f != f else f  # NaN check
        except (TypeError, ValueError):
            return None

    def _get_annual_df(self, store_key: str) -> pd.DataFrame:
        """Get stored DataFrame filtered to annual periods only (desc)."""
        df = self._store.get(store_key)
        if df is None or df.empty:
            return pd.DataFrame()
        fy_month_str = f"{self._fy_end_month:02d}"
        annual = df[df["end_date"].astype(str).str[4:6] == fy_month_str].copy()
        return annual.sort_values("end_date", ascending=False)

    def _get_annual_series(self, store_key: str, col: str) -> list[tuple[str, float | None]]:
        """Extract (year_label, value) pairs for annual periods, sorted desc."""
        df = self._get_annual_df(store_key)
        if df.empty or col not in df.columns:
            return []
        result = []
        for _, r in df.iterrows():
            year = str(r["end_date"])[:4]
            result.append((year, self._safe_float(r.get(col))))
        return result

    # ------------------------------------------------------------------
    # Payout by year (A-share path; contract-compatible)
    # ------------------------------------------------------------------

    def _get_payout_by_year(self) -> dict[str, float]:
        """Payout ratio (%) by fiscal year from stored dividends + income.

        dividends store columns (AKShare edition):
            end_date     YYYYMMDD 报告期 (annual 1231; interim 0630 also
                        collapses to the same fiscal year via [:4])
            cash_div_tax 每股派息(税前, 元)  [NOT per-10-share]
            base_share   总股本(万股)
            div_proc     "实施" for implemented
        payout(FY) = Σ(dps × base_share × 1e4) / n_income_attr_p(FY) × 100
        """
        div_df = self._store.get("dividends")
        income_df = self._get_annual_df("income")
        if div_df is None or div_df.empty or income_df.empty:
            return {}

        div_lookup: dict[str, float] = {}
        for _, r in div_df.iterrows():
            if str(r.get("div_proc", "")) != "实施":
                continue
            year = str(r.get("end_date", ""))[:4]
            if not year:
                continue
            dps = self._safe_float(r.get("cash_div_tax")) or 0
            base_share = self._safe_float(r.get("base_share")) or 0
            payment = dps * base_share * 10000  # 元
            div_lookup[year] = div_lookup.get(year, 0) + payment

        np_lookup: dict[str, float | None] = {}
        for _, r in income_df.iterrows():
            year = str(r["end_date"])[:4]
            np_lookup[year] = self._safe_float(r.get("n_income_attr_p"))

        result: dict[str, float] = {}
        for year, div_total in div_lookup.items():
            np_val = np_lookup.get(year)
            if div_total and np_val and np_val > 0:
                result[year] = div_total / np_val * 100
        return result
