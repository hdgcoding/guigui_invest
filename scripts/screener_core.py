#!/usr/bin/env python3
"""guigui_invest — AKShare Screener Core (龟龟选股器, 两级筛选).

AKShare edition of the original Tushare screener. Tier 1 builds a full-market
snapshot from bulk endpoints (sina spot + em 业绩报表 + em 分红送配 +
exchange listing tables); Tier 2 reuses AKShareClient's per-stock fetchers
for deep analysis (hard vetoes, quality, Factor 2/4, floor price, scoring).

Data-source notes (vs original):
- PE is static (latest annual EPS), not TTM — documented approximation.
- Turnover-rate filter is replaced by a daily-turnover-amount liquidity
  proxy (sina spot has no float shares).
- Audit opinion is unavailable via AKShare → audit veto skipped.

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
    SINA_INCOME_MAP, SINA_BALANCE_MAP, SINA_CASHFLOW_MAP,
)
from cache_utils import ScreenerCache  # noqa: E402
from screener_config import ScreenerConfig  # noqa: E402

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

SECTION_NAMES = {
    "P2": "公司治理", "P3": "会计政策", "P4": "应收账款", "P6": "合并报表",
    "P13": "风险提示", "MDA": "管理层讨论与分析", "SUB": "主要子公司",
}


class AKShareScreener:
    """Two-tier stock screener backed by AKShare bulk + per-stock endpoints."""

    def __init__(self, config: ScreenerConfig | None = None):
        self.config = config or ScreenerConfig()
        self.cache = ScreenerCache(os.path.join(ROOT, self.config.cache_dir))
        self.client = AKShareClient()
        # Reuse the collector's per-stock TTL cache (financial 7d / market 24h)
        self._rf_cache: float | None = None

    # ------------------------------------------------------------------
    # Generic cached bulk fetch
    # ------------------------------------------------------------------

    def _bulk_cached(self, key: str, ttl_seconds: int, fetch) -> pd.DataFrame:
        cached = self.cache.get(key, ttl_seconds)
        if cached is not None:
            return cached
        df = self.client._fetch_with_retry(fetch)
        if df is not None and not df.empty:
            self.cache.put(key, df)
            return df
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
        df = self._bulk_cached("spot_sina", 6 * 3600, _fetch)
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
        df = self._bulk_cached(f"yjbb:{date}", 24 * 3600, _make_fetch(date))
        # Sparse (reports not yet published at year start) → fallback one year earlier
        if len(df) < 500:
            prev = f"{int(date[:4]) - 1}1231"
            df = self._bulk_cached(f"yjbb:{prev}", 24 * 3600, _make_fetch(prev))
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
        return out

    def _fetch_listing_dates(self) -> pd.DataFrame:
        """Exchange name/code tables → code + list_date (YYYYMMDD)."""
        def _fetch():
            import akshare as ak
            frames = []
            try:
                sh = ak.stock_info_sh_name_code(symbol="主板A股")
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

        df = self._bulk_cached("listing_dates", 7 * 24 * 3600, _fetch)
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
            df = self._bulk_cached(f"fhps:{period}", 24 * 3600, _fetch)
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
                        & (all_plans["ex_date"] >= cutoff)]

        dps_ttm = ttm.groupby("code")["dps"].sum().rename("dps_ttm")

        # Latest share count: most recent implemented plan per stock
        with_shares = all_plans[all_plans["implemented"]
                                & all_plans["shares"].notna()].copy()
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

        # Derived valuation columns
        df["pe_ttm"] = df["close"] / df["eps"].where(df["eps"] > 0)   # 静态PE(近似)
        df["pb"] = df["close"] / df["bps"].where(df["bps"] > 0)
        df["dv_ttm"] = df["dps_ttm"] / df["close"] * 100               # %
        df["total_mv"] = df["close"] * df["total_shares"] / 1e4        # 万元
        df["ts_code"] = df["code"] + df["code"].map(
            lambda c: ".SH" if c.startswith(("6",)) else ".SZ")
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
        df = df[df["amount"].notna() & (df["amount"] >= 1e7)].copy()

        # 5. PB window
        df = df[df["pb"].notna()].copy()
        df = df[(df["pb"] > 0) & (df["pb"] <= cfg.max_pb)].copy()

        # 6. Dual-channel PE split
        pe_valid = df["pe_ttm"].notna()
        main_mask = pe_valid & (df["pe_ttm"] > 0) & (df["pe_ttm"] <= cfg.max_pe)
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

            pe_inv = 1.0 / main["pe_ttm"]
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
        fina = c._fetch_fina_indicators_df(num)
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
        num = ts_code.split(".")[0]
        try:
            def _fetch():
                import akshare as ak
                return ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=num)
            df = self._bulk_cached(f"pledge:{num}", 24 * 3600, _fetch)
            if df is not None and not df.empty and "占总股本比例" in df.columns:
                ratios = pd.to_numeric(df["占总股本比例"], errors="coerce").dropna()
                if not ratios.empty and float(ratios.sum()) > self.config.max_pledge_pct:
                    return False, f"pledge_ratio={ratios.sum():.1f}% > {self.config.max_pledge_pct}%"
        except Exception:
            pass
        # Audit opinion unavailable via AKShare → veto skipped (documented)
        return True, ""

    # ------------------------------------------------------------------
    # Tier 2: financial quality
    # ------------------------------------------------------------------

    def _check_financial_quality(self, channel: str) -> tuple[bool, dict[str, Any]]:
        cfg = self.config
        metrics: dict[str, Any] = {}
        fi = self.client._store.get("fina_indicators")

        def _sf(v):
            try:
                f = float(v)
                return None if f != f else f
            except (TypeError, ValueError):
                return None

        if fi is not None and not fi.empty:
            annual = fi[fi["end_date"].astype(str).str.endswith("1231")]
            row = annual.iloc[0] if not annual.empty else fi.iloc[0]
            metrics["roe_waa"] = _sf(row.get("roe_waa")) or _sf(row.get("roe"))
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
                    metrics["roe_waa"] = np_p / eq * 100
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

        if all(metrics.get(k) is None for k in
               ("roe_waa", "gross_margin", "debt_to_assets")):
            return False, metrics

        if channel == "observation":
            return self._check_obs_quality(metrics, cfg)

        if metrics["roe_waa"] is not None and metrics["roe_waa"] < cfg.min_roe:
            return False, metrics
        if metrics["gross_margin"] is not None and metrics["gross_margin"] < cfg.min_gross_margin:
            return False, metrics
        if metrics["debt_to_assets"] is not None and metrics["debt_to_assets"] > cfg.max_debt_ratio:
            return False, metrics
        return True, metrics

    def _check_obs_quality(self, metrics: dict[str, Any],
                           cfg: ScreenerConfig) -> tuple[bool, dict[str, Any]]:
        if metrics["roe_waa"] is not None and metrics["roe_waa"] < cfg.min_roe_obs:
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

        fcf = (ocf or 0) - abs(capex or 0)
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
        result["M"] = sum(ratios) / len(ratios) if ratios else None

        # AA = 真实可支配现金结余 (latest year) ≈ OCF + V1 - V_deduct - |Capex|
        AA = None
        annual_cf = self.client._get_annual_df("cashflow")
        if not annual_cf.empty and not annual_inc.empty:
            cf_row = annual_cf.iloc[0]
            inc_row = annual_inc.iloc[0]
            ocf = self.client._safe_float(cf_row.get("n_cashflow_act"))
            capex = abs(self.client._safe_float(cf_row.get("c_pay_acq_const_fiolta")) or 0)
            v1 = self.client._safe_float(inc_row.get("asset_disp_income")) or 0
            noi = self.client._safe_float(inc_row.get("non_oper_income")) or 0
            oi = self.client._safe_float(inc_row.get("oth_income")) or 0
            if ocf is not None:
                AA = (ocf + v1 - (noi + oi) - capex) / 1e6  # 百万元
        result["AA"] = AA

        if AA is not None and result.get("M") is not None and mkt_cap > 0:
            R = AA * (result["M"] / 100) / mkt_cap * 100
            result["R"] = R
        else:
            result["R"] = None

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
            cash = c._safe_float(bs_row.get("money_cap")) or 0
            result["cash"] = cash / 1e6
            ibd = 0.0
            for col in ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y"):
                v = c._safe_float(bs_row.get(col))
                if v:
                    ibd += v / 1e6
            result["ibd"] = ibd
            gw = c._safe_float(bs_row.get("goodwill")) or 0
            ta = c._safe_float(bs_row.get("total_assets")) or 0
            result["goodwill"] = gw / 1e6
            result["total_assets"] = ta / 1e6
            if ta > 0:
                result["goodwill_ratio"] = gw / ta * 100

        if cf_row is not None:
            ocf = c._safe_float(cf_row.get("n_cashflow_act")) or 0
            capex = abs(c._safe_float(cf_row.get("c_pay_acq_const_fiolta")) or 0)
            result["fcf"] = (ocf - capex) / 1e6
            if mkt_cap > 0:
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
            result["fcf_margin"] = result["fcf"] / rev * 100

        # EBITDA = 营业利润 + 财务费用 + D&A (sina 无 D&A → 0, 偏保守)
        if result.get("oper_profit") is not None:
            fin_exp = c._safe_float(inc_row.get("finance_exp")) or 0 if inc_row is not None else 0
            ebitda = (result["oper_profit"] + abs(fin_exp)) / 1e6
            if ebitda > 0:
                net_debt = result.get("ibd", 0) - result.get("cash", 0)
                ev = mkt_cap + net_debt
                result["ebitda"] = ebitda
                result["ev"] = ev
                result["ev_ebitda"] = ev / ebitda
                result["net_debt_ebitda"] = net_debt / ebitda

        np_parent = result.get("np_parent")
        if np_parent is not None and np_parent > 0:
            net_cash = result.get("cash", 0) - result.get("ibd", 0)
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
            cash = c._safe_float(bs_row.get("money_cap")) or 0
            trad = c._safe_float(bs_row.get("trad_asset")) or 0
            ibd = 0.0
            for col in ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y"):
                v = c._safe_float(bs_row.get(col))
                if v:
                    ibd += v
            baselines.append(("net_liquid_assets", (cash + trad - ibd) / total_shares))

            equity = c._safe_float(bs_row.get("total_hldr_eqy_exc_min_int")) or 0
            baselines.append(("bvps", equity / total_shares))

        weekly = c._store.get("weekly_prices")
        if weekly is not None and not weekly.empty:
            low = weekly["close"].dropna().min()
            if low == low:
                baselines.append(("10yr_low", float(low)))

        rf = self._get_rf()
        divs = c._store.get("dividends")
        if divs is not None and not divs.empty and rf is not None:
            annual_dps: dict[str, float] = {}
            for _, r in divs.iterrows():
                y = str(r.get("end_date", ""))[:4]
                dps = c._safe_float(r.get("cash_div_tax")) or 0
                annual_dps[y] = annual_dps.get(y, 0) + dps
            recent = sorted(annual_dps.values(), reverse=True)[:3]
            if recent:
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
            if fcf_list and min(fcf_list) > 0:
                baselines.append(("pessimistic_fcf", min(fcf_list) / (rf / 100) / total_shares))

        result["baselines"] = baselines
        if baselines:
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
            "pe_ttm": row.get("pe_ttm"), "pb": row.get("pb"),
            "dv_ttm": row.get("dv_ttm"),
        }

        passed, reason = self._check_hard_vetoes(ts_code)
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
        result.update({"M": f2.get("M"), "R": f2.get("R"), "II": f2.get("II"),
                       "Rf": f2.get("Rf"), "R_vs_II": f2.get("R_vs_II")})

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

    def _compute_rankings(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        cfg = self.config
        df = df.copy()

        for col in ("roe_waa", "fcf_yield", "R"):
            df[f"{col}_pctile"] = (df[col].rank(pct=True, na_option="bottom")
                                   if col in df.columns else 0.0)
        for col in ("ev_ebitda", "floor_premium"):
            df[f"{col}_pctile"] = (1.0 - df[col].rank(pct=True, na_option="top")
                                   if col in df.columns else 0.0)

        df["composite_score"] = (
            cfg.weight_roe * df["roe_waa_pctile"]
            + cfg.weight_fcf_yield * df["fcf_yield_pctile"]
            + cfg.weight_penetration_r * df["R_pctile"]
            + cfg.weight_ev_ebitda * df["ev_ebitda_pctile"]
            + cfg.weight_floor_premium * df["floor_premium_pctile"]
        )
        return df.sort_values("composite_score", ascending=False)

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def run(self, tier1_only: bool = False, tier2_limit: int | None = None,
            progress_callback=None) -> pd.DataFrame:
        print("=== Tier 1: Bulk screening (AKShare) ===")
        bulk_df = self._tier1_bulk_data()
        print(f"  Universe: {len(bulk_df)} stocks")

        filtered = self._tier1_filter(bulk_df)
        n_main = len(filtered[filtered["channel"] == "main"]) if not filtered.empty else 0
        n_obs = len(filtered[filtered["channel"] == "observation"]) if not filtered.empty else 0
        print(f"  After filters: {len(filtered)} stocks (main: {n_main}, observation: {n_obs})")

        ranked = self._tier1_rank_and_cut(filtered)
        print(f"  After rank & cut: {len(ranked)} stocks")

        if tier1_only:
            return ranked

        if tier2_limit is not None:
            ranked = ranked.head(tier2_limit)

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
                "pe_ttm", "pb", "dv_ttm", "roe_waa", "M", "R", "R_vs_II",
                "ev_ebitda", "fcf_yield", "cash_adj_pe", "goodwill_ratio",
                "floor_baseline", "floor_premium", "composite_score"]
        show = [c for c in cols if c in df.columns]
        html = df[show].to_html(index=False, border=0, classes="screener",
                                float_format=lambda x: f"{x:.2f}")
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
<p>生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(df)} 只 · PE 为静态口径(最新年报EPS)</p>
{html}
</body></html>"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"✓ HTML exported: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="guigui_invest screener (AKShare)")
    parser.add_argument("--tier1-only", action="store_true")
    parser.add_argument("--tier2-limit", type=int, default=None,
                        help="Cap stocks entering Tier 2 (e.g. 30)")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--html", default=None)
    parser.add_argument("--min-roe", type=float, default=None)
    parser.add_argument("--max-pe", type=float, default=None)
    parser.add_argument("--min-gross-margin", type=float, default=None)
    parser.add_argument("--cache-refresh", action="store_true")
    args = parser.parse_args()

    cfg = ScreenerConfig()
    if args.min_roe is not None:
        cfg.min_roe = args.min_roe
    if args.max_pe is not None:
        cfg.max_pe = args.max_pe
    if args.min_gross_margin is not None:
        cfg.min_gross_margin = args.min_gross_margin

    screener = AKShareScreener(cfg)
    if args.cache_refresh:
        screener.cache.clear()
        print("✓ screener cache cleared")

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
        screener.export_html(df, f"{default_base}.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
