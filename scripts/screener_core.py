#!/usr/bin/env python3
"""guigui_invest — AKShare Screener Core (龟龟选股器, 两级筛选).

AKShare edition of the original Tushare screener. Tier 1 builds a full-market
snapshot from bulk endpoints (sina spot + em 业绩报表 + em 分红送配 +
exchange listing tables); Tier 2 reuses AKShareClient's per-stock fetchers
for deep analysis (hard vetoes, quality, Factor 2/4, floor price, scoring).

Data-source notes (vs original):
- PE basis: TTM PE is preferred when the bulk source supplies it; otherwise a
  static PE (close / latest annual EPS) is used. The basis is exported per row
  as ``pe_basis`` so the approximation is never implicit.
- The comprehensive score uses four always-available factors; the penetration
  return R participates only when R is verified (never as the consolidated
  cash proxy). Weights are renormalized over the participating factors.
- Turnover-rate filter is replaced by a daily-turnover-amount liquidity
  proxy (sina spot has no float shares).
- Audit opinion / pledge detail are unavailable via AKShare in some cases →
  such rows are flagged as unverified/degraded, not treated as a pass.

Usage:
    python3 scripts/screener_core.py --tier1-only
    python3 scripts/screener_core.py --tier2-limit 30 --csv output/screener.csv
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from akshare_collector import AKShareClient  # noqa: E402
from akshare_modules.constants import (  # noqa: E402
    CACHE_TTL_CATEGORY, SINA_INCOME_MAP, SINA_BALANCE_MAP, SINA_CASHFLOW_MAP,
)
from cache_utils import ScreenerCache  # noqa: E402
from screener_config import ScreenerConfig  # noqa: E402
from financial_contracts import (  # noqa: E402
    debt, depreciation, annual_dividends, number, statement_issues,
)

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# factor key (ScreenerConfig.renormalized_scoring_weights) → percentile column
_FACTOR_PCTILE_COLUMNS = {
    "roe": "roe_waa_pctile",
    "fcf_yield": "fcf_yield_pctile",
    "ev_ebitda": "ev_ebitda_pctile",
    "floor_premium": "floor_premium_pctile",
    "penetration_r": "R_pctile",
}


class _FixedTTLCache:
    """Cache facade that ignores the TTL passed by the caller.

    Lets ``_ScreenerAKShareClient._cached_call`` reuse the collector's cache
    contract unchanged while substituting the screener-configured TTL.
    """

    def __init__(self, cache, ttl_seconds: int):
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    def get(self, key, _ttl_seconds=None):
        return self._cache.get(key, self._ttl_seconds)

    def put(self, key, df):
        return self._cache.put(key, df)


class _ScreenerAKShareClient(AKShareClient):
    """AKShareClient whose disk-cache TTLs follow ScreenerConfig cache_* fields.

    Tier 2 reuses the collector's per-stock fetchers; only their TTL source
    differs (collector constants vs screener configuration). Delegating to the
    parent ``_cached_call`` keeps fetch-time bookkeeping/behaviour identical.
    """

    def __init__(self, ttl_seconds: dict[str, int]):
        super().__init__()
        self._screener_ttls = dict(ttl_seconds)

    def _ttl_seconds(self, endpoint: str, category: str | None) -> int:
        if endpoint == "risk_free_rate":
            return int(self._screener_ttls["rf"])
        if category == "financial":
            return int(self._screener_ttls["financial"])
        if category == "market":
            return int(self._screener_ttls["market"])
        return int(self._screener_ttls["tier2"])

    def _cached_call(self, endpoint: str, fetch, key: str | None = None) -> pd.DataFrame:
        ttl = self._ttl_seconds(endpoint, CACHE_TTL_CATEGORY.get(endpoint))
        real_cache = self._cache
        self._cache = _FixedTTLCache(real_cache, ttl)
        try:
            return super()._cached_call(endpoint, fetch, key=key)
        finally:
            self._cache = real_cache


class AKShareScreener:
    """Two-tier stock screener backed by AKShare bulk + per-stock endpoints."""

    def __init__(self, config: ScreenerConfig | None = None):
        self.config = config or ScreenerConfig()
        self._validate_config()
        self.cache = ScreenerCache(os.path.join(ROOT, self.config.cache_dir))
        self.client = _ScreenerAKShareClient(self.config.cache_ttl_seconds)
        self._rf_cache: float | None = None
        self.diagnostics = []
        # Last bulk-cache outcome: hit | fetched | empty | failed (failed means
        # the endpoint raised, which is different from an authoritative empty
        # answer — callers must not read empty as "no risk").
        self._last_bulk_status: str | None = None

    def _validate_config(self) -> None:
        """Reject invalid configuration instead of silently using wrong weights."""
        errors = self.config.validate()
        if errors:
            raise ValueError("Invalid ScreenerConfig: " + "; ".join(errors))

    # ------------------------------------------------------------------
    # Generic cached bulk fetch
    # ------------------------------------------------------------------

    def _bulk_cached(self, key: str, ttl_seconds: int, fetch) -> pd.DataFrame:
        cached = self.cache.get(key, ttl_seconds)
        if cached is not None:
            self._last_bulk_status = "hit"
            return cached
        df = self.client._fetch_with_retry(fetch)
        if df is not None and not df.empty:
            self.cache.put(key, df)
            self._last_bulk_status = "fetched"
            return df
        self._last_bulk_status = "empty" if df is not None else "failed"
        return pd.DataFrame()

    # ------------------------------------------------------------------
    # Tier 1: bulk market snapshot
    # ------------------------------------------------------------------

    def _fetch_spot(self) -> pd.DataFrame:
        """Sina full-market spot: code/name/price/amount (SH/SZ A-shares only)."""
        def _fetch():
            import akshare as ak
            return ak.stock_zh_a_spot()
        # same-day cache
        df = self._bulk_cached("spot_sina", self.config.cache_ttl_seconds["spot"], _fetch)
        if df.empty:
            return df

        raw = df["代码"].astype(str)
        # Sina codes carry an exchange prefix: sh600887 / sz000858 / bj920000
        prefix = raw.str[:2]
        num = raw.str[2:].where(prefix.isin(["sh", "sz"]), raw)
        keep = prefix.isin(["sh", "sz"]) | num.str.fullmatch(r"\d{6}")

        out = pd.DataFrame({
            "code": num[keep].str.zfill(6),
            "name": df["名称"].astype(str)[keep],
            "close": pd.to_numeric(df["最新价"], errors="coerce")[keep],
            "amount": pd.to_numeric(df["成交额"], errors="coerce")[keep],  # 元
        })
        # SH/SZ A-share codes only: 60/68xxxx (SH主板/科创板), 00xxxx/30xxxx (SZ主板/创业板)
        out = out[out["code"].str[:2].isin(["60", "68", "00", "30"])]
        return out.dropna(subset=["close"])

    def _latest_annual_date(self) -> str:
        """Latest fiscal-year end whose annual reports are broadly published."""
        now = datetime.now()
        fy = now.year - 1
        # Annual reports must be published by Apr 30 of the following year.
        if now.month < 5:
            fy -= 1
        return f"{fy}1231"

    def _fetch_yjbb(self) -> pd.DataFrame:
        """em 业绩报表 (full market, latest annual): EPS/BPS/ROE/行业."""
        def _make_fetch(date: str):
            def _fetch():
                import akshare as ak
                return ak.stock_yjbb_em(date=date)
            return _fetch

        date = self._latest_annual_date()
        df = self._bulk_cached(f"yjbb:{date}", self.config.cache_ttl_seconds["global"], _make_fetch(date))
        # Sparse (reports not yet published at year start) → fallback one year earlier
        if len(df) < 500:
            prev = f"{int(date[:4]) - 1}1231"
            df = self._bulk_cached(f"yjbb:{prev}", self.config.cache_ttl_seconds["global"], _make_fetch(prev))
        if df.empty:
            return df

        out = pd.DataFrame({
            "code": df["股票代码"].astype(str).str.zfill(6),
            "eps": pd.to_numeric(df["每股收益"], errors="coerce"),
            "bps": pd.to_numeric(df["每股净资产"], errors="coerce"),
            "roe": pd.to_numeric(df["净资产收益率"], errors="coerce"),
            "n_profit": pd.to_numeric(df["净利润-净利润"], errors="coerce"),
            "industry": df["所处行业"].astype(str),
        })
        return out.drop_duplicates("code")

    def _fetch_listing_dates(self) -> pd.DataFrame:
        """Exchange name/code tables → code + list_date (YYYYMMDD)."""
        def _fetch():
            import akshare as ak
            frames = []
            for board in ("主板A股", "科创板"):
                try:
                    sh = ak.stock_info_sh_name_code(symbol=board)
                    frames.append(pd.DataFrame(
                        {"code": sh["证券代码"].astype(str).str.zfill(6),
                         "list_date": sh["上市日期"].astype(str)}))
                except Exception:
                    pass
            try:
                sz = ak.stock_info_sz_name_code(symbol="A股列表")
                frames.append(pd.DataFrame(
                    {"code": sz["A股代码"].astype(str).str.zfill(6),
                     "list_date": sz["A股上市日期"].astype(str)}))
            except Exception:
                pass
            if not frames:
                return pd.DataFrame()
            return pd.concat(frames, ignore_index=True)

        df = self._bulk_cached("listing_dates", self.config.cache_ttl_seconds["stock_basic"], _fetch)
        if df.empty:
            return df
        df = df.copy()
        df["list_date"] = (df["list_date"].str.replace("-", "", regex=False)
                           .str.slice(0, 8))
        return df.drop_duplicates(subset=["code"])

    def _fetch_fhps_market(self) -> pd.DataFrame:
        """em 分红送配（按报告期，全市场带代码）→ per-stock TTM DPS + 总股本.

        TTM DPS: implemented plans across recent report periods whose
        ex-date (除权除息日) falls within the last 365 days.
        总股本: latest non-null share count across those periods.
        """
        import akshare as ak

        year = datetime.now().year
        periods = [f"{year}0630", f"{year - 1}1231", f"{year - 1}0630",
                   f"{year - 1}0930", f"{year}0930"]

        frames = []
        for period in periods:
            def _fetch(p=period):
                return ak.stock_fhps_em(date=p)
            df = self._bulk_cached(f"fhps:{period}", self.config.cache_ttl_seconds["global"], _fetch)
            if not df.empty and "代码" in df.columns:
                df = df.copy()
                df["_period"] = period
                frames.append(df)
        if not frames:
            return pd.DataFrame()

        all_plans = pd.concat(frames, ignore_index=True)
        all_plans["code"] = all_plans["代码"].astype(str).str.zfill(6)
        all_plans["dps"] = pd.to_numeric(
            all_plans["现金分红-现金分红比例"], errors="coerce") / 10  # → 元/股
        all_plans["shares"] = pd.to_numeric(all_plans["总股本"], errors="coerce")
        all_plans["ex_date"] = pd.to_datetime(all_plans["除权除息日"], errors="coerce")
        all_plans["implemented"] = all_plans["方案进度"].astype(str).str.contains(
            "实施", na=False)

        cutoff = pd.Timestamp.now() - pd.Timedelta(days=365)
        ttm = all_plans[all_plans["implemented"] & all_plans["dps"].notna()
                        & (all_plans["dps"] > 0)
                        & (all_plans["ex_date"] >= cutoff)
                        & (all_plans["ex_date"] <= pd.Timestamp.now())]

        dps_ttm = ttm.groupby("code")["dps"].sum().rename("dps_ttm")

        # Latest share count: most recent implemented plan per stock
        with_shares = all_plans[all_plans["shares"].notna() & (all_plans["shares"] > 0)].copy()
        with_shares = with_shares.sort_values("ex_date", ascending=False)
        latest_shares = (with_shares.groupby("code").first()["shares"]
                         .rename("total_shares"))

        out = pd.concat([dps_ttm, latest_shares], axis=1).reset_index()
        return out

    def _tier1_bulk_data(self) -> pd.DataFrame:
        """Merge spot + yjbb + listing + dividends into the Tier 1 universe."""
        spot = self._fetch_spot()
        if spot.empty:
            return pd.DataFrame()
        df = spot

        yjbb = self._fetch_yjbb()
        if not yjbb.empty:
            df = df.merge(yjbb, on="code", how="left")

        listing = self._fetch_listing_dates()
        if not listing.empty:
            df = df.merge(listing, on="code", how="left")

        fhps = self._fetch_fhps_market()
        if not fhps.empty:
            df = df.merge(fhps, on="code", how="left")

        for col in ("eps", "bps", "dps_ttm", "total_shares", "n_profit"):
            if col not in df:
                df[col] = float("nan")
        if "list_date" not in df:
            df["list_date"] = pd.Series(pd.NA, index=df.index, dtype="string")
        if "industry" not in df:
            df["industry"] = "未核实"
        self.diagnostics = [{"field": col, "missing": int(df[col].isna().sum()), "total": len(df)}
                            for col in ("eps", "bps", "total_shares", "list_date")]
        # Independent of dividend history: a loss-making non-payer can enter
        # observation via explicitly labelled annual weighted-share proxy.
        implied_shares = df["n_profit"] / df["eps"].where(df["eps"] != 0)
        needs_shares = df["total_shares"].isna()
        df["shares_basis"] = "分红方案股本（非实时）"
        df.loc[needs_shares, "shares_basis"] = "年报净利润/EPS推算加权平均股本（非实时）"
        df["total_shares"] = df["total_shares"].fillna(implied_shares.where(implied_shares > 0))
        df["market_cap_basis"] = "价格×历史股本代理；Tier2必须重新核实"

        # Derived valuation columns
        df["pe_static"] = df["close"] / df["eps"].where(df["eps"] > 0)
        df["pb"] = df["close"] / df["bps"].where(df["bps"] > 0)
        df["dv_ttm"] = df["dps_ttm"] / df["close"] * 100               # %
        df["total_mv"] = df["close"] * df["total_shares"] / 1e4        # 万元
        df["ts_code"] = df["code"] + df["code"].map(
            lambda c: ".SH" if c.startswith(("6",)) else ".SZ")
        return self._attach_pe_columns(df)

    # ------------------------------------------------------------------
    # PE basis (TTM preferred, static fallback)
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_pe_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Add canonical ``pe`` + honest ``pe_basis`` columns.

        A TTM PE wins whenever the bulk source supplies a finite value; the
        static PE (close / latest-annual EPS) is only the fallback. Both source
        columns stay available for diagnostics, and ``pe_basis`` records which
        one was actually used so the approximation is never implicit.
        """
        df = df.copy()
        if "pe_static" not in df.columns:
            eps = pd.to_numeric(df.get("eps"), errors="coerce") if "eps" in df.columns else None
            close = pd.to_numeric(df.get("close"), errors="coerce") if "close" in df.columns else None
            if eps is not None and close is not None:
                df["pe_static"] = close / eps.where(eps > 0)
            else:
                df["pe_static"] = float("nan")
        df["pe_static"] = pd.to_numeric(df["pe_static"], errors="coerce")
        if "pe_ttm" in df.columns:
            df["pe_ttm"] = pd.to_numeric(df["pe_ttm"], errors="coerce")
        else:
            df["pe_ttm"] = float("nan")
        df["pe"] = df["pe_ttm"].where(df["pe_ttm"].notna(), df["pe_static"])
        df["pe_basis"] = "缺失"
        df.loc[df["pe_static"].notna(), "pe_basis"] = "静态（最新年报EPS）"
        df.loc[df["pe_ttm"].notna(), "pe_basis"] = "TTM"
        return df

    # ------------------------------------------------------------------
    # Tier 1: filter / rank
    # ------------------------------------------------------------------

    def _tier1_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            df = df.copy()
            df["channel"] = pd.Series(dtype="object")
            return df

        cfg = self.config
        today = datetime.now()
        df = self._attach_pe_columns(df)

        # 1. Remove ST/PT/退市整理
        df = df[~df["name"].str.contains(r"\*ST|ST|PT|退市|退", na=False, regex=True)].copy()

        # 1b. Exclude banks unless include_bank (em 行业标签: 银行/银行Ⅱ)
        if not cfg.include_bank and "industry" in df.columns:
            df = df[~df["industry"].astype(str).str.contains("银行", na=False)].copy()

        # 2. Listing age
        if "list_date" in df.columns:
            cutoff = (today - timedelta(days=cfg.min_listing_years * 365)).strftime("%Y%m%d")
            df = df[df["list_date"].notna() & (df["list_date"] <= cutoff)].copy()

        # 3. Market cap ≥ min (total_mv in 万元)
        df = df[df["total_mv"].notna()].copy()
        df = df[df["total_mv"] / 10000 >= cfg.min_market_cap_yi].copy()

        # 4. Liquidity proxy: daily turnover amount ≥ 0.1亿 (sina spot无换手率)
        df = df[df["amount"].notna() & (df["amount"] >= cfg.min_daily_amount_yuan)].copy()

        # 5. PB window
        df = df[df["pb"].notna()].copy()
        df = df[(df["pb"] > 0) & (df["pb"] <= cfg.max_pb)].copy()

        # 6. Dual-channel PE split (canonical pe column: TTM-first, static fallback)
        pe_valid = df["pe"].notna() & (df["pe"] > 0)
        main_mask = pe_valid & (df["pe"] <= cfg.max_pe)
        obs_mask = ~pe_valid & df["eps"].notna() & (df["eps"] < 0)

        main_df = df[main_mask].copy()
        obs_df = df[obs_mask].copy()

        # 7. Dividend yield > 0 — main channel only
        main_df = main_df[main_df["dv_ttm"].notna() & (main_df["dv_ttm"] > 0)].copy()
        main_df["channel"] = "main"

        # 8. Observation channel: top N by market cap
        obs_df = obs_df.sort_values("total_mv", ascending=False).head(cfg.obs_channel_limit)
        obs_df["channel"] = "observation"

        return pd.concat([main_df, obs_df], ignore_index=True)

    def _tier1_rank_and_cut(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        cfg = self.config
        main = df[df["channel"] == "main"].copy()
        obs = df[df["channel"] == "observation"].copy()

        if not main.empty:
            dv_max = main["dv_ttm"].max()
            main["_dv_norm"] = main["dv_ttm"] / dv_max if dv_max > 0 else 0

            pe_inv = 1.0 / main["pe"]
            pe_max = pe_inv.max()
            main["_pe_norm"] = pe_inv / pe_max if pe_max > 0 else 0

            pb_inv = 1.0 / main["pb"]
            pb_max = pb_inv.max()
            main["_pb_norm"] = pb_inv / pb_max if pb_max > 0 else 0

            main["tier1_score"] = (
                cfg.dv_weight * main["_dv_norm"]
                + cfg.pe_weight * main["_pe_norm"]
                + cfg.pb_weight * main["_pb_norm"]
            )
            main = (main.sort_values("tier1_score", ascending=False)
                    .head(cfg.tier2_main_limit)
                    .drop(columns=["_dv_norm", "_pe_norm", "_pb_norm"]))

        obs["tier1_score"] = 0.0
        return pd.concat([main, obs], ignore_index=True)

    # ------------------------------------------------------------------
    # Tier 2: per-stock store population (reuses AKShareClient fetchers)
    # ------------------------------------------------------------------

    def _populate_store(self, ts_code: str) -> None:
        """Fetch statements/indicators/dividends/weekly into client._store."""
        num = ts_code.split(".")[0]
        c = self.client
        c._store = {}
        for symbol, col_map in (("利润表", SINA_INCOME_MAP),
                                ("资产负债表", SINA_BALANCE_MAP),
                                ("现金流量表", SINA_CASHFLOW_MAP)):
            key = {"利润表": "income", "资产负债表": "balance_sheet",
                   "现金流量表": "cashflow"}[symbol]
            c._store[key] = c._fetch_sina_statement(ts_code, symbol, col_map)
        if not c._store["cashflow"].empty:
            c._store["cashflow"] = c._supplement_da(ts_code, c._store["cashflow"])
        fina = c._merge_derived_indicators(c._fetch_fina_indicators_df(num))
        if not fina.empty:
            c._store["fina_indicators"] = fina
        divs = c._fetch_dividends_df(num)
        if not divs.empty:
            impl = divs[divs["div_proc"] == "实施"].copy()
            c._store["dividends"] = impl
            c._store["dividends_all"] = divs
        weekly = c._fetch_weekly(num, years=10)
        if not weekly.empty:
            c._store["weekly_prices"] = weekly
            c._store["price_basis"] = "unadjusted"

    def _latest_annual_row(self, key: str) -> pd.Series | None:
        df = self.client._get_annual_df(key)
        return None if df.empty else df.iloc[0]

    def _get_rf(self) -> float | None:
        if self._rf_cache is not None:
            return self._rf_cache
        try:
            rf_df = self.client._store.get("risk_free_rate")
            if rf_df is None or rf_df.empty:
                self.client.get_risk_free_rate("")
                rf_df = self.client._store.get("risk_free_rate")
            if rf_df is not None and not rf_df.empty:
                self._rf_cache = float(rf_df.iloc[0]["yield"])
        except Exception:
            pass
        return self._rf_cache

    # ------------------------------------------------------------------
    # Tier 2: hard vetoes
    # ------------------------------------------------------------------

    def _check_hard_vetoes(self, ts_code: str) -> tuple[bool, str]:
        """Pledge veto + explicit degradation flags.

        A failing/empty pledge endpoint must never be read as "no pledge risk".
        Such rows stay eligible as *candidates* (the API is flaky by nature)
        but carry unverified/degraded flags into the exported columns.
        """
        num = ts_code.split(".")[0]
        flags: list[str] = []
        self._veto_checks: dict[str, Any] = {
            "audit_status": "未核实（不等于通过）",
            "pledge_status": "未核实（不等于通过）",
            "degraded_flags": "",
        }
        # Audit opinion has no stable auto source via AKShare → degrade, never pass.
        flags.append("audit_unverified")
        try:
            def _fetch():
                import akshare as ak
                return ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=num)
            df = self._bulk_cached(
                f"pledge:{num}", self.config.cache_ttl_seconds["tier2"], _fetch)
            fetch_status = self._last_bulk_status
            if fetch_status == "failed":
                self._veto_checks["pledge_status"] = "未核实：质押接口调用失败（不等于无质押）"
                flags.append("pledge_unverified")
            elif df is None or df.empty or "占总股本比例" not in df.columns:
                self._veto_checks["pledge_status"] = "未核实：质押接口无数据/缺字段（不等于无质押）"
                flags.append("pledge_unverified")
            else:
                has_status = "状态" in df.columns
                if has_status:
                    scoped = df[df["状态"].astype(str).str.contains("未解押", na=False)]
                    scope_label = "未解押明细"
                else:
                    scoped = df
                    scope_label = "全部明细（未解押范围未核对）"
                    flags.append("pledge_scope_unverified")
                ratios = pd.to_numeric(scoped["占总股本比例"], errors="coerce").dropna()
                if ratios.empty:
                    if has_status:
                        # Every record is already released → 0% outstanding.
                        pledge_ratio = 0.0
                        self._veto_checks["pledge_ratio"] = pledge_ratio
                        self._veto_checks["pledge_status"] = (
                            "已统计未解押明细：0.00%（记录均为已解押）")
                    else:
                        self._veto_checks["pledge_status"] = (
                            f"未核实：{scope_label}无可用占比（不等于无质押）")
                        flags.append("pledge_unverified")
                else:
                    pledge_ratio = float(ratios.sum())
                    self._veto_checks["pledge_ratio"] = pledge_ratio
                    self._veto_checks["pledge_status"] = f"已统计{scope_label}：{pledge_ratio:.2f}%"
                    if pledge_ratio > self.config.max_pledge_pct:
                        flags.append("pledge_veto")
                        self._veto_checks["degraded_flags"] = ";".join(dict.fromkeys(flags))
                        return False, f"pledge_ratio={pledge_ratio:.1f}% > {self.config.max_pledge_pct}%"
        except Exception as exc:  # noqa: BLE001 - degrade, do not silently pass
            self._veto_checks["pledge_status"] = (
                f"未核实：质押接口异常 {type(exc).__name__}（不等于无质押）")
            flags.append("pledge_unverified")
        self._veto_checks["degraded_flags"] = ";".join(dict.fromkeys(flags))
        return True, ""

    # ------------------------------------------------------------------
    # Tier 2: financial quality
    # ------------------------------------------------------------------

    def _check_financial_quality(self, channel: str) -> tuple[bool, dict[str, Any]]:
        cfg = self.config
        metrics: dict[str, Any] = {k: None for k in ("roe_waa", "roe_metric", "gross_margin", "debt_to_assets")}
        if statement_issues(self.client._store):
            metrics["data_status"] = "报表期间/版本未核实"
            return False, metrics
        fi = self.client._merge_derived_indicators(self.client._store.get("fina_indicators", pd.DataFrame()))

        def _sf(v):
            try:
                f = float(v)
                return None if f != f else f
            except (TypeError, ValueError):
                return None

        if fi is not None and not fi.empty:
            annual = fi[fi["end_date"].astype(str).str.endswith("1231")]
            row = annual.iloc[0] if not annual.empty else fi.iloc[0]
            metrics["roe_waa"] = _sf(row.get("roe_waa"))
            metrics["roe_metric"] = metrics["roe_waa"] if metrics["roe_waa"] is not None else _sf(row.get("roe"))
            metrics["roe_basis"] = "加权ROE" if metrics["roe_waa"] is not None else "摊薄ROE（非加权）"
            metrics["gross_margin"] = _sf(row.get("grossprofit_margin"))
            metrics["debt_to_assets"] = _sf(row.get("debt_to_assets"))
        else:
            # 新浪财务指标接口缺失/限流 → 从三大报表直接推导
            inc = self._latest_annual_row("income")
            bs = self._latest_annual_row("balance_sheet")
            if inc is not None and bs is not None:
                np_p = _sf(inc.get("n_income_attr_p"))
                eq = _sf(bs.get("total_hldr_eqy_exc_min_int"))
                if np_p is not None and eq:
                    metrics["roe_metric"] = np_p / eq * 100
                    metrics["roe_basis"] = "摊薄ROE（非加权）"
                rev = _sf(inc.get("revenue"))
                cost = _sf(inc.get("oper_cost"))
                if rev and cost is not None:
                    metrics["gross_margin"] = (rev - cost) / rev * 100

        if metrics["debt_to_assets"] is None:
            bs = self._latest_annual_row("balance_sheet")
            if bs is not None:
                ta, tl = _sf(bs.get("total_assets")), _sf(bs.get("total_liab"))
                if ta and tl is not None:
                    metrics["debt_to_assets"] = tl / ta * 100

        if any(metrics.get(k) is None for k in
               ("roe_metric", "gross_margin", "debt_to_assets")):
            metrics["data_status"] = "财务质量必需指标缺失，不通过"
            return False, metrics

        if channel == "observation":
            return self._check_obs_quality(metrics, cfg)

        if metrics["roe_metric"] < cfg.min_roe:
            return False, metrics
        if metrics["gross_margin"] is not None and metrics["gross_margin"] < cfg.min_gross_margin:
            return False, metrics
        if metrics["debt_to_assets"] is not None and metrics["debt_to_assets"] > cfg.max_debt_ratio:
            return False, metrics
        return True, metrics

    def _check_obs_quality(self, metrics: dict[str, Any],
                           cfg: ScreenerConfig) -> tuple[bool, dict[str, Any]]:
        if cfg.min_roe_obs is not None and metrics["roe_metric"] is not None and metrics["roe_metric"] < cfg.min_roe_obs:
            return False, metrics
        if metrics["gross_margin"] is not None and metrics["gross_margin"] < cfg.min_gross_margin:
            return False, metrics
        if metrics["debt_to_assets"] is not None and metrics["debt_to_assets"] > cfg.max_debt_ratio:
            return False, metrics

        cf = self.client._store.get("cashflow")
        income = self.client._store.get("income")
        if cf is None or cf.empty:
            return False, metrics

        def _sf(v):
            try:
                f = float(v)
                return None if f != f else f
            except (TypeError, ValueError):
                return None

        annual_cf = self.client._get_annual_df("cashflow")
        if annual_cf.empty:
            return False, metrics
        latest_cf = annual_cf.iloc[0]
        ocf = _sf(latest_cf.get("n_cashflow_act"))
        capex = _sf(latest_cf.get("c_pay_acq_const_fiolta"))

        if cfg.obs_require_ocf_positive and (ocf is None or ocf <= 0):
            return False, metrics

        if ocf is None or capex is None:
            return False, metrics
        fcf = ocf - abs(capex)
        inc_row = self._latest_annual_row("income")
        revenue = _sf(inc_row.get("revenue")) if inc_row is not None else None
        if revenue is not None and revenue > 0:
            metrics["fcf_margin"] = fcf / revenue * 100
            if metrics["fcf_margin"] < cfg.min_fcf_margin_obs:
                return False, metrics
        else:
            return False, metrics

        fcf_list = []
        for _, r in annual_cf.head(5).iterrows():
            o, cc = _sf(r.get("n_cashflow_act")), _sf(r.get("c_pay_acq_const_fiolta"))
            if o is not None and cc is not None:
                fcf_list.append(o - abs(cc))
        if fcf_list:
            positive = sum(1 for f in fcf_list if f > 0)
            metrics["fcf_positive_years"] = positive
            if positive < cfg.min_fcf_positive_years_obs:
                return False, metrics
        _ = income
        return True, metrics

    # ------------------------------------------------------------------
    # Tier 2: Factor 2 (penetration return)
    # ------------------------------------------------------------------

    def _extract_factor2_metrics(self, total_mv_wan: float) -> dict[str, Any]:
        result: dict[str, Any] = {}
        mkt_cap = total_mv_wan * 10000 / 1e6  # 百万元

        rf = self._get_rf()
        result["Rf"] = rf
        result["II"] = max(3.5, rf + 2.0) if rf is not None else None

        income = self.client._store.get("income")
        if income is None or income.empty:
            return result

        # 3yr avg payout M via client helper (dividends already in store)
        payout = self.client._get_payout_by_year()
        annual_inc = self.client._get_annual_df("income")
        years = [str(r["end_date"])[:4] for _, r in annual_inc.head(3).iterrows()]
        ratios = [payout[y] for y in years if y in payout]
        result["M"] = sum(ratios) / 3 if len(ratios) == 3 else None

        # Same default AA window as §17 (latest two annual cash surpluses).
        self.client._compute_factor3_sensitivity_base()
        summary = self.client._store.get("factor3_sensitivity", {})
        aa_raw = summary.get("aa_selected")
        AA = aa_raw / 1e6 if aa_raw is not None else None
        result["AA_proxy"] = AA

        # R only becomes a scored factor when it is *verified*: source-reconciled
        # owner cash + verified share count/market cap. The consolidated OCF−Capex
        # proxy is kept as a diagnostic (R_proxy), never as a scored R.
        verified = self._verified_penetration_r(result.get("M"))
        if verified is not None:
            result.update(verified)
            result["R_status"] = "verified"
        else:
            result["R"] = None
            result["R_status"] = "missing"

        if AA is not None and result.get("M") is not None and mkt_cap > 0:
            result["R_proxy"] = AA * (result["M"] / 100) / mkt_cap * 100
            if result["R_status"] != "verified":
                result["R_status"] = "proxy_unverified"
            result["R_proxy_label"] = "合并OCF−Capex现金代理×M/市值（代理口径，未核实，不进入综合分）"
        else:
            result["R_proxy"] = None

        result["R_basis"] = (
            verified["R_basis"] if verified is not None
            else "合并OCF−Capex代理，未完成归母现金穿透")

        if result.get("R") is not None and result.get("II") is not None and rf is not None:
            R, II = result["R"], result["II"]
            if R < rf:
                result["R_vs_II"] = "below_rf"
            elif R < II * 0.5:
                result["R_vs_II"] = "fail"
            elif R < II:
                result["R_vs_II"] = "marginal"
            else:
                result["R_vs_II"] = "pass"
        else:
            result["R_vs_II"] = None
        return result

    def _verified_penetration_r(self, m_pct: float | None) -> dict[str, Any] | None:
        """Return a verified penetration return, else None.

        Verification requires evidence the network endpoints cannot supply:
        decision-eligible AA from source-reconciled owner cash, a verified
        share count/market cap (``basic_info.shares_verified``), and aligned
        statement periods. When missing, R must stay out of the composite.
        """
        store = self.client._store
        summary = store.get("factor3_sensitivity") or {}
        basic = store.get("basic_info")
        if not isinstance(basic, pd.DataFrame) or basic.empty:
            return None
        if not summary.get("decision_eligible"):
            return None
        row = basic.iloc[0]
        if str(row.get("shares_verified", False)).lower() != "true":
            return None
        if statement_issues(store):
            return None
        aa_yuan = number(summary.get("aa_selected"))
        mkt_cap_wan = number(row.get("total_mv"))
        if aa_yuan is None or mkt_cap_wan is None or mkt_cap_wan <= 0 or m_pct is None:
            return None
        mkt_cap_mm = mkt_cap_wan / 100  # 万元 → 百万元
        _, default_tax = self.client._unique_tax_rates(str(row.get("ts_code", "")))
        tax = default_tax if default_tax is not None else 0.0
        ratio = aa_yuan / 1e6 * (m_pct / 100) * (1 - tax / 100) / mkt_cap_mm * 100
        return {
            "R": ratio,
            "R_basis": "来源核对归母现金×M/核实股本市值（税后），已核实",
            "R_aa_verified": aa_yuan / 1e6,
            "R_tax_pct": tax,
            "R_mkt_cap_verified": mkt_cap_mm,
        }

    # ------------------------------------------------------------------
    # Tier 2: Factor 4 (valuation)
    # ------------------------------------------------------------------

    def _extract_factor4_metrics(self, total_mv_wan: float) -> dict[str, Any]:
        result: dict[str, Any] = {}
        mkt_cap = total_mv_wan * 10000 / 1e6
        c = self.client

        inc_row = self._latest_annual_row("income")
        bs_row = self._latest_annual_row("balance_sheet")
        annual_cf = c._get_annual_df("cashflow")
        cf_row = annual_cf.iloc[0] if not annual_cf.empty else None

        if inc_row is not None:
            result["oper_profit"] = c._safe_float(inc_row.get("operate_profit"))
            result["np_parent"] = c._safe_float(inc_row.get("n_income_attr_p"))
            result["revenue"] = c._safe_float(inc_row.get("revenue"))

        if bs_row is not None:
            cash = c._safe_float(bs_row.get("money_cap"))
            result["cash"] = cash / 1e6 if cash is not None else None
            ibd = debt(bs_row)
            result["ibd"] = ibd / 1e6 if ibd is not None else None
            minority = c._safe_float(bs_row.get("minority_int"))
            result["minority_equity"] = minority / 1e6 if minority is not None else None
            gw = c._safe_float(bs_row.get("goodwill"))
            ta = c._safe_float(bs_row.get("total_assets"))
            result["goodwill"] = gw / 1e6 if gw is not None else None
            result["total_assets"] = ta / 1e6 if ta is not None else None
            if gw is not None and ta is not None and ta > 0:
                result["goodwill_ratio"] = gw / ta * 100

        if cf_row is not None:
            ocf = c._safe_float(cf_row.get("n_cashflow_act"))
            capex = c._safe_float(cf_row.get("c_pay_acq_const_fiolta"))
            result["fcf"] = (ocf - abs(capex)) / 1e6 if ocf is not None and capex is not None else None
            if mkt_cap > 0 and result["fcf"] is not None:
                result["fcf_yield"] = result["fcf"] / mkt_cap * 100

            fcf_list = []
            for _, r in annual_cf.head(5).iterrows():
                o, cc = c._safe_float(r.get("n_cashflow_act")), c._safe_float(r.get("c_pay_acq_const_fiolta"))
                if o is not None and cc is not None:
                    fcf_list.append(o - abs(cc))
            if fcf_list:
                result["fcf_positive_years"] = sum(1 for f in fcf_list if f > 0)
                result["fcf_total_years"] = len(fcf_list)
                result["fcf_consistency"] = result["fcf_positive_years"] / len(fcf_list)

        rev = result.get("revenue")
        if rev and result.get("fcf") is not None:
            result["fcf_margin"] = result["fcf"] * 1e6 / rev * 100

        # EBITDA = 营业利润 + 财务费用 + D&A (sina 无 D&A → 0, 偏保守)
        if result.get("oper_profit") is not None and cf_row is not None:
            fin_exp = c._safe_float(inc_row.get("finance_exp"))
            da = depreciation(cf_row)
            ebitda = (result["oper_profit"] + fin_exp + da) / 1e6 if fin_exp is not None and da is not None else None
            result["ebitda_basis"] = "营业利润+带符号财务费用+D&A（非标准EBITDA）"
            if ebitda is not None and ebitda > 0 and all(result.get(key) is not None for key in ("ibd", "cash", "minority_equity")):
                net_debt = result["ibd"] - result["cash"]
                ev = mkt_cap + net_debt + result["minority_equity"]
                result["ebitda"] = ebitda
                result["ev"] = ev
                result["ev_ebitda"] = ev / ebitda
                result["net_debt_ebitda"] = net_debt / ebitda

        np_parent = result.get("np_parent")
        if np_parent is not None and np_parent > 0 and result.get("cash") is not None and result.get("ibd") is not None:
            net_cash = result["cash"] - result["ibd"]
            result["cash_adj_pe"] = (mkt_cap - net_cash) / (np_parent / 1e6)
        return result

    # ------------------------------------------------------------------
    # Tier 2: floor price (5 methods)
    # ------------------------------------------------------------------

    def _extract_floor_price(self, close: float,
                             total_mv_wan: float) -> dict[str, Any]:
        result: dict[str, Any] = {}
        c = self.client
        total_shares = total_mv_wan * 10000 / close if close > 0 else 0
        baselines: list[tuple[str, float]] = []

        bs_row = self._latest_annual_row("balance_sheet")
        if bs_row is not None and total_shares > 0:
            cash = c._safe_float(bs_row.get("money_cap"))
            trad = c._safe_float(bs_row.get("trad_asset"))
            ibd = debt(bs_row)
            if cash is not None and trad is not None and ibd is not None:
                baselines.append(("net_liquid_assets", (cash + trad - ibd) / total_shares))
            equity = c._safe_float(bs_row.get("total_hldr_eqy_exc_min_int"))
            if equity is not None:
                baselines.append(("bvps", equity / total_shares))

        weekly = c._store.get("weekly_prices")
        if weekly is not None and not weekly.empty:
            low = weekly["close"].dropna().min()
            if low == low:
                baselines.append(("10yr_low", float(low)))

        rf = self._get_rf()
        divs = c._store.get("dividends")
        if divs is not None and not divs.empty and rf is not None:
            complete = annual_dividends(c._store.get("dividends_all", divs), c._store.get("as_of"))
            recent = [r["dps"] for r in complete[:3]]
            if len(recent) == 3:
                avg_dps = sum(recent) / len(recent)
                discount = max(rf / 100, 0.03)
                baselines.append(("dividend_implied", avg_dps / discount))

        annual_cf = c._get_annual_df("cashflow")
        if not annual_cf.empty and rf is not None and rf > 0 and total_shares > 0:
            fcf_list = []
            for _, r in annual_cf.head(5).iterrows():
                o, cc = c._safe_float(r.get("n_cashflow_act")), c._safe_float(r.get("c_pay_acq_const_fiolta"))
                if o is not None and cc is not None:
                    fcf_list.append(o - abs(cc))
            if len(fcf_list) >= 3 and min(fcf_list) > 0:
                baselines.append(("pessimistic_fcf", min(fcf_list) / (rf / 100) / total_shares))

        baselines = [(name, value) for name, value in baselines if value > 0]
        result["baselines"] = baselines
        if len(baselines) >= 3:
            composite = sum(v for _, v in baselines) / len(baselines)
            result["composite_baseline"] = composite
            result["premium"] = (close / composite - 1) * 100 if composite > 0 else None
        else:
            result["composite_baseline"] = None
            result["premium"] = None
        return result

    # ------------------------------------------------------------------
    # Tier 2: single stock pipeline
    # ------------------------------------------------------------------

    def _analyze_single_stock(self, row: pd.Series) -> dict[str, Any] | None:
        ts_code = row["ts_code"]
        channel = row.get("channel", "main")
        close = float(row.get("close", 0) or 0)
        total_mv_wan = float(row.get("total_mv", 0) or 0)

        result: dict[str, Any] = {
            "ts_code": ts_code, "name": row.get("name", ""),
            "industry": row.get("industry", ""), "channel": channel,
            "close": close, "total_mv": total_mv_wan,
            "pe": row.get("pe"), "pe_basis": row.get("pe_basis", "缺失"),
            "pe_static": row.get("pe_static", row.get("pe_ttm")),
            "pe_ttm": row.get("pe_ttm"),
            "pb": row.get("pb"), "dv_ttm": row.get("dv_ttm"),
            "audit_status": "未核实（不等于通过）", "data_status": "候选，非买入建议",
            "shares_basis": row.get("shares_basis", "未核实"),
        }

        passed, reason = self._check_hard_vetoes(ts_code)
        result.update(getattr(self, "_veto_checks", {}))
        degraded = str(result.get("degraded_flags") or "")
        if degraded:
            result["data_status"] = f"候选，非买入建议；未核实/降级：{degraded}"
        if not passed:
            result["veto"] = reason
            return None

        self._populate_store(ts_code)

        fq_passed, fq_metrics = self._check_financial_quality(channel)
        result.update(fq_metrics)
        if not fq_passed:
            result["veto"] = "financial_quality"
            return None

        f2 = self._extract_factor2_metrics(total_mv_wan)
        result.update({k: f2.get(k) for k in (
            "M", "R", "R_status", "R_proxy", "R_proxy_label", "R_basis",
            "R_aa_verified", "R_tax_pct", "R_mkt_cap_verified",
            "II", "Rf", "R_vs_II", "AA_proxy")})

        f4 = self._extract_factor4_metrics(total_mv_wan)
        result.update({k: f4.get(k) for k in (
            "ev_ebitda", "cash_adj_pe", "fcf_yield", "net_debt_ebitda",
            "goodwill_ratio", "fcf_consistency", "fcf_margin")})

        fp = self._extract_floor_price(close, total_mv_wan)
        result["floor_baseline"] = fp.get("composite_baseline")
        result["floor_premium"] = fp.get("premium")
        return result

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _rank_pctile(values: pd.Series, ascending: bool = True) -> pd.Series:
        clean = pd.to_numeric(values, errors="coerce").replace(
            [float("inf"), -float("inf")], float("nan"))
        return clean.rank(pct=True, ascending=ascending, na_option="keep").fillna(0)

    def _compute_rankings(self, df: pd.DataFrame) -> pd.DataFrame:
        """Composite score over four factors + optional verified penetration R.

        Weight renormalization is the core contract here: when no row has a
        verified R, the 25% R weight is removed and the remaining four weights
        are rescaled to sum to 1.0 (the composite stays on a full [0, 1]
        scale). ``df.attrs["scoring"]`` records which factors were active.
        """
        if df.empty:
            return df
        cfg = self.config
        df = df.copy()

        if "roe_metric" in df.columns:
            roe_source = df["roe_metric"]
        elif "roe_waa" in df.columns:  # legacy callers / hand-built frames
            roe_source = df["roe_waa"]
        else:
            roe_source = pd.Series(float("nan"), index=df.index)
        df["roe_waa_pctile"] = self._rank_pctile(roe_source)
        for col in ("fcf_yield", "R", "ev_ebitda", "floor_premium"):
            values = (df[col] if col in df.columns
                      else pd.Series(float("nan"), index=df.index))
            df[f"{col}_pctile"] = self._rank_pctile(
                values, ascending=col not in ("ev_ebitda", "floor_premium"))

        r_values = (pd.to_numeric(df["R"], errors="coerce")
                    if "R" in df.columns else pd.Series(dtype=float))
        include_r = bool(cfg.weight_penetration_r > 0 and r_values.notna().any())
        if "R_status" not in df.columns:
            df["R_status"] = "missing"
        weights = cfg.renormalized_scoring_weights(include_penetration_r=include_r)
        df["composite_score"] = sum(
            weight * df[_FACTOR_PCTILE_COLUMNS[factor]]
            for factor, weight in weights.items())
        df["composite_n_factors"] = len(weights)
        df["composite_r_included"] = include_r
        df["composite_factors"] = "+".join(weights)
        out = df.sort_values("composite_score", ascending=False)
        out.attrs["scoring"] = {"factors": list(weights), "weights": weights,
                                "penetration_r_included": include_r}
        return out

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def _apply_tier2_limit(self, ranked: pd.DataFrame,
                           tier2_limit: int | None) -> pd.DataFrame:
        """Cap Tier 2 intake without starving the observation channel.

        The budget is split by channel in proportion to the configured channel
        caps (``tier2_main_limit`` : ``obs_channel_limit``), with the
        observation channel guaranteed at least one slot when it has names.
        A bare ``head(tier2_limit)`` would drop the whole observation channel
        whenever the main channel fills the list (e.g. ``--tier2-limit 3``).
        """
        if tier2_limit is None:
            return ranked
        if tier2_limit < 1:
            raise ValueError("tier2_limit must be >= 1")
        if ranked.empty:
            return ranked
        main = ranked[ranked["channel"] == "main"]
        obs = ranked[ranked["channel"] == "observation"]
        channel_caps = self.config.tier2_max_stocks
        obs_quota = (round(tier2_limit * self.config.obs_channel_limit
                           / channel_caps) if channel_caps > 0 else 0)
        if len(obs) and tier2_limit >= 1:
            obs_quota = max(obs_quota, 1)
        obs_quota = min(obs_quota, len(obs), tier2_limit)
        main_quota = tier2_limit - obs_quota
        main_keep = main.head(main_quota)
        obs_keep = obs.head(obs_quota)
        return pd.concat([main_keep, obs_keep], ignore_index=True)

    def run(self, tier1_only: bool = False, tier2_limit: int | None = None,
            progress_callback=None) -> pd.DataFrame:
        # Config may be mutated after __init__ (CLI overrides/tests): re-validate.
        self._validate_config()
        if tier2_limit is not None and tier2_limit < 1:
            raise ValueError("tier2_limit must be >= 1")
        print("=== Tier 1: Bulk screening (AKShare) ===")
        bulk_df = self._tier1_bulk_data()
        print(f"  Universe: {len(bulk_df)} stocks")
        for diagnostic in self.diagnostics:
            if diagnostic["missing"]:
                print(f"  ⚠️ {diagnostic['field']} missing: {diagnostic['missing']}/{diagnostic['total']} (not assumed zero/passed)")

        filtered = self._tier1_filter(bulk_df)
        n_main = len(filtered[filtered["channel"] == "main"]) if not filtered.empty else 0
        n_obs = len(filtered[filtered["channel"] == "observation"]) if not filtered.empty else 0
        print(f"  After filters: {len(filtered)} stocks (main: {n_main}, observation: {n_obs})")

        ranked = self._tier1_rank_and_cut(filtered)
        print(f"  After rank & cut: {len(ranked)} stocks")

        if tier1_only:
            return ranked

        if tier2_limit is not None:
            before = len(ranked)
            ranked = self._apply_tier2_limit(ranked, tier2_limit)
            n_main = len(ranked[ranked["channel"] == "main"])
            n_obs = len(ranked[ranked["channel"] == "observation"])
            print(f"  After --tier2-limit {tier2_limit} (dual-channel quota): "
                  f"{len(ranked)} stocks (main: {n_main}, observation: {n_obs}; "
                  f"candidates: {before})")

        total = len(ranked)
        print(f"\n=== Tier 2: Deep analysis ({total} stocks) ===")

        results = []
        for i, (_, row) in enumerate(ranked.iterrows()):
            ts_code = row["ts_code"]
            if progress_callback:
                progress_callback(i + 1, total, ts_code)
            else:
                print(f"  [{i+1}/{total}] {ts_code} {row.get('name', '')}...", end="",
                      flush=True)
            try:
                stock_result = self._analyze_single_stock(row)
            except Exception as e:  # noqa: BLE001
                print(f" ERROR ({e})")
                continue
            if stock_result is None:
                print(" VETOED")
            else:
                results.append(stock_result)
                print(" OK")

        if not results:
            return pd.DataFrame()
        result_df = self._compute_rankings(pd.DataFrame(results))
        scoring = result_df.attrs.get("scoring", {})
        if scoring:
            print(f"  Composite factors: {'+'.join(scoring.get('factors', []))}"
                  f" (R included: {scoring.get('penetration_r_included')})")
        if "degraded_flags" in result_df.columns:
            n_degraded = int(result_df["degraded_flags"].fillna("").astype(str)
                             .str.len().gt(0).sum())
            if n_degraded:
                print(f"  ⚠️ {n_degraded} rows carry unverified/degraded flags "
                      f"(candidates, never a risk pass)")
        print(f"\n=== Results: {len(result_df)} stocks passed ===")
        return result_df

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_csv(self, df: pd.DataFrame, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"✓ CSV exported: {path}")

    def export_html(self, df: pd.DataFrame, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        cols = ["ts_code", "name", "industry", "channel", "close",
                "pe", "pe_basis", "pb", "dv_ttm", "roe_metric", "roe_basis",
                "M", "R", "R_status", "R_proxy", "R_vs_II",
                "data_status", "audit_status", "pledge_status", "degraded_flags",
                "shares_basis", "ev_ebitda", "fcf_yield", "cash_adj_pe",
                "goodwill_ratio", "floor_baseline", "floor_premium",
                "composite_score", "composite_factors"]
        show = [c for c in cols if c in df.columns]
        html = df[show].to_html(index=False, border=0, classes="screener",
                                float_format=lambda x: f"{x:.2f}")
        r_included = bool(df["composite_r_included"].iloc[0]) if (
            "composite_r_included" in df.columns and len(df)) else False
        scoring_note = (
            "综合分 = 五因子（ROE/FCF收益率/EV-EBITDA逆/底价溢价逆/已核实穿透回报率R），权重和为1"
            if r_included else
            "综合分 = 四因子（ROE/FCF收益率/EV-EBITDA逆/底价溢价逆），R未核实不参与、剩余权重重归一化；"
            "R_proxy 仅为代理诊断（合并OCF−Capex），不进入综合分")
        pe_note = "PE 口径见 pe_basis：优先 TTM，源缺失时回退静态（最新年报EPS）；pe_static 为明细列"
        page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>龟龟选股器 · AKShare 版</title>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;margin:24px;background:#fafafa}}
h1{{font-size:20px}} p{{color:#666}}
table.screener{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
table.screener th,table.screener td{{padding:6px 10px;border-bottom:1px solid #eee;text-align:right}}
table.screener th:first-child,table.screener td:first-child{{text-align:left}}
table.screener th{{background:#1a1a2e;color:#fff;position:sticky;top:0}}
</style></head><body>
<h1>龟龟选股器 · AKShare 版</h1>
<p>生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(df)} 只</p>
<p>{scoring_note}</p>
<p>{pe_note}；degraded_flags 非空表示质押/审计等未核实，仅候选、不等于通过。</p>
{html}
</body></html>"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"✓ HTML exported: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="guigui_invest screener (AKShare)")
    parser.add_argument("--tier1-only", action="store_true")
    parser.add_argument("--tier2-limit", type=int, default=None,
                        help="Cap stocks entering Tier 2 (e.g. 30); observation "
                             "channel keeps its reserved quota, main fills the rest")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--html", default=None)
    parser.add_argument("--min-roe", type=float, default=None)
    parser.add_argument("--max-pe", type=float, default=None)
    parser.add_argument("--min-gross-margin", type=float, default=None)
    parser.add_argument("--cache-refresh", action="store_true")
    args = parser.parse_args()

    if args.tier2_limit is not None and args.tier2_limit < 1:
        parser.error("--tier2-limit must be >= 1")

    cfg = ScreenerConfig()
    if args.min_roe is not None:
        cfg.min_roe = args.min_roe
    if args.max_pe is not None:
        cfg.max_pe = args.max_pe
    if args.min_gross_margin is not None:
        cfg.min_gross_margin = args.min_gross_margin

    try:
        screener = AKShareScreener(cfg)
    except ValueError as exc:
        print(f"❌ 配置非法: {exc}")
        return 1
    if args.cache_refresh:
        screener.cache.clear()
        screener.client._cache.clear()
        print("✓ screener and collector caches cleared")

    df = screener.run(tier1_only=args.tier1_only, tier2_limit=args.tier2_limit)
    if df.empty:
        print("⚠️ 无结果")
        return 1

    default_base = os.path.join(ROOT, "output", "screener")
    if args.csv:
        screener.export_csv(df, args.csv)
    if args.html:
        screener.export_html(df, args.html)
    if not args.csv and not args.html and not args.tier1_only:
        screener.export_csv(df, f"{default_base}.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
