#!/usr/bin/env python3
"""Valuation Engine — deterministic valuation computations.

Computes company classification, WACC, and multiple valuation methods
(DCF, DDM, PE Band, PEG, PS) with sensitivity tables.

Usage:
    python3 scripts/valuation_engine.py --code 600887 --output-dir output/600887_伊利/
"""

import argparse
import math
import os
import statistics
import sys
from datetime import datetime

import pandas as pd

from config import validate_stock_code
from format_utils import format_number, format_table, format_header
from financial_contracts import annual_dividends, debt, statement_issues, number

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Classification thresholds (matching classification_rules.md)
ROE_BLUE_CHIP_THRESHOLD = 15.0       # ROE 5yr avg > 15%
PAYOUT_BLUE_CHIP_THRESHOLD = 30.0    # Dividend payout 3yr avg > 30%
REV_GROWTH_THRESHOLD = 20.0          # Revenue CAGR 5yr
PROFIT_GROWTH_THRESHOLD = 25.0       # Net profit CAGR 5yr

# A classification dimension needs at least this many annual observations
# before it may earn a score (classification_rules.md: 不足 3 年 → 该维度 0 分).
MIN_SAMPLE_YEARS = 3

# DCF assumptions. The formulary gives no history-growth ceiling, so use a
# conservative cap and disclose the clipping in the generated report.
HISTORY_GROWTH_CAP = 30.0
FCF_OUTLIER_DEVIATION = 0.5          # negative FCF deviating >50% from 3yr mean
TV_SHARE_WARN_PCT = 75.0             # terminal-value share warning threshold

# DDM terminal growth = min(3%, long-run GDP growth) per valuation_methods.md.
DDM_TERMINAL_CAP = 3.0

FAILED_PREFIX = "计算失败"            # method raised; distinct from "not applicable"

# Method weights by company type
METHOD_WEIGHTS = {
    "蓝筹价值型": {"DCF": 40, "DDM": 30, "PE_Band": 30},
    "成长型": {"PEG": 35, "DCF_Scenarios": 35, "PS": 30},
    "混合型": {"DCF": 35, "PE_Band": 25, "PEG": 25, "DDM": 15},
}

# WACC parameters by market
MARKET_PARAMS = {
    "A": {"erp": 6.0, "rf_default": 2.5, "tax_default": 25.0, "g_terminal": 3.0,
          "gdp_long_run": 4.5},
    "HK": {"erp": 5.5, "rf_default": 4.0, "tax_default": 16.5, "g_terminal": 2.5,
           "gdp_long_run": 2.5},
    "US": {"erp": 5.0, "rf_default": 4.0, "tax_default": 21.0, "g_terminal": 2.5,
           "gdp_long_run": 2.5},
}

# Beta by market cap (百万元)
BETA_BY_CAP = [
    (100_000, 0.8),     # > 1000亿 (= 100,000 百万元)
    (10_000, 1.0),      # 100-1000亿
    (0, 1.2),           # < 100亿
]


# ---------------------------------------------------------------------------
# ValuationEngine
# ---------------------------------------------------------------------------

class ValuationEngine:
    """Deterministic valuation computation engine.

    Requires a TushareClient with populated _store (call assemble_data_pack first).
    """

    def __init__(self, ts_code: str, output_dir: str, client):
        self.ts_code = ts_code
        self.output_dir = output_dir
        self.client = client

        # Detect market
        if ts_code.endswith(".HK"):
            self.market = "HK"
        elif ts_code.endswith(".US") or (not ts_code.endswith(".SH") and not ts_code.endswith(".SZ")):
            self.market = "US"
        else:
            self.market = "A"

        self.params = MARKET_PARAMS[self.market]
        self._sf = client._safe_float  # shorthand
        self.unavailable: dict[str, str] = {}
        self.failed: dict[str, str] = {}
        self.notes: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _annual_df(self, key: str) -> pd.DataFrame:
        return self.client._get_annual_df(key)

    def _annual_series(self, key: str, col: str) -> list[tuple[str, float | None]]:
        return self.client._get_annual_series(key, col)

    def _unit(self) -> str:
        return self.client._unit_label()

    def _price_unit(self) -> str:
        return self.client._price_unit()

    def _basic_info(self) -> dict:
        """Extract key basic info fields."""
        bi_df = self.client._store.get("basic_info")
        if bi_df is None or bi_df.empty:
            return {}
        row = bi_df.iloc[0]
        close = self._sf(row.get("close"))

        if self.market == "HK":
            total_mv = self._sf(row.get("total_market_cap"))  # 百万港元
            total_shares = (total_mv * 1e6 / close) if (total_mv and close) else None
            mkt_cap_mm = total_mv
        elif self.market == "US":
            total_mv = self._sf(row.get("total_mv"))  # raw USD
            total_shares = (total_mv / close) if (total_mv and close) else None
            mkt_cap_mm = total_mv / 1e6 if total_mv else None
        else:
            total_mv_wan = self._sf(row.get("total_mv"))  # 万元
            total_share_wan = self._sf(row.get("total_share"))  # 万股
            mkt_cap_mm = total_mv_wan / 100 if total_mv_wan else None  # 百万元
            total_shares = total_share_wan * 10000 if total_share_wan else None
            close = close

        return {
            "close": close,
            "pe_ttm": self._sf(row.get("pe_ttm") or row.get("pe")),
            "pb": self._sf(row.get("pb")),
            "mkt_cap_mm": mkt_cap_mm,
            "total_shares": total_shares,
            "name": row.get("name", ""),
        }

    @staticmethod
    def _cagr(vals: list[float | None], years=None) -> float | None:
        """Compute CAGR from a desc-ordered list [latest, ..., oldest]."""
        if len(vals) < 2:
            return None
        latest, oldest = number(vals[0]), number(vals[-1])
        n = int(str(years[0])[:4]) - int(str(years[-1])[:4]) if years is not None else len(vals) - 1
        if latest is None or oldest is None or latest <= 0 or oldest <= 0 or n <= 0:
            return None
        return number((latest / oldest) ** (1 / n) - 1)

    def _growth(self, key, col, count=5):
        frame = self._annual_df(key).head(count)
        return self._cagr([self._sf(r.get(col)) for _, r in frame.iterrows()],
                          frame["end_date"].tolist()) if not frame.empty else None

    def _unavailable(self, method, reason):
        self.unavailable[method] = reason
        return None

    def _clear_unavailable(self, *methods):
        for method in methods:
            self.unavailable.pop(method, None)
            self.failed.pop(method, None)

    @staticmethod
    def _redistribute(weights: dict, removed: str) -> dict:
        """Drop a method and spread its weight proportionally over the rest."""
        removed_w = weights.get(removed, 0)
        remaining = {m: w for m, w in weights.items() if m != removed}
        if not remaining:
            return {}
        base = sum(remaining.values())
        if base <= 0:
            return {m: round(100 / len(remaining), 2) for m in remaining}
        return {m: round(w + removed_w * w / base, 2) for m, w in remaining.items()}

    def _roe_stats(self) -> tuple[float | None, int]:
        """ROE 5yr average from §12, else derived from §3/§4. Returns (avg, n)."""
        roe_vals: list[float] = []
        fi_df = self._annual_df("fina_indicators")
        if not fi_df.empty and "roe_waa" in fi_df.columns:
            for _, r in fi_df.head(5).iterrows():
                v = self._sf(r.get("roe_waa"))
                if v is not None:
                    roe_vals.append(v)
        if not roe_vals and not statement_issues(self.client._store):
            income_df = self._annual_df("income")
            bs_df = self._annual_df("balance_sheet")
            if not income_df.empty and not bs_df.empty:
                bs_by_year = {str(r["end_date"])[:4]: r for _, r in bs_df.iterrows()}
                for _, r in income_df.head(5).iterrows():
                    yr = str(r["end_date"])[:4]
                    np_val = self._sf(r.get("n_income_attr_p"))
                    eq = self._sf(bs_by_year.get(yr, {}).get("total_hldr_eqy_exc_min_int"))
                    if np_val and eq and eq > 0:
                        roe_vals.append(np_val / eq * 100)
        return (statistics.mean(roe_vals) if roe_vals else None), len(roe_vals)

    def _sample_count(self, df, col) -> int:
        """Annual non-missing observations available for a metric."""
        if df is None or df.empty or col not in df.columns:
            return 0
        return sum(1 for _, r in df.head(5).iterrows() if self._sf(r.get(col)) is not None)

    def _fcf_base(self, fcf_list: list[float]) -> tuple[float, list[float]]:
        """3yr FCF mean with the formulary's negative-outlier exclusion.

        valuation_methods.md 方法1 Step1: a year whose FCF is negative and
        deviates from the mean by >50% is an abnormal year and is excluded.
        """
        pool = fcf_list[:3]
        mean_all = statistics.mean(pool)
        kept_idx: list[int] = []
        excluded: list[float] = []
        for i, value in enumerate(pool):
            outlier = (value < 0 and mean_all != 0
                       and abs(value - mean_all) > FCF_OUTLIER_DEVIATION * abs(mean_all))
            if outlier:
                excluded.append(value)
            else:
                kept_idx.append(i)
        if not kept_idx:  # never drop the whole base; fall back to the documented mean
            kept_idx, excluded = list(range(len(pool))), []
        return statistics.mean(pool[i] for i in kept_idx), excluded

    def _fcff_context(self, method):
        """Do not mix an unidentified OCF−Capex stream with enterprise WACC."""
        issues = statement_issues(self.client._store)
        if issues:
            self._unavailable(method, "; ".join(issues))
            return None
        cf = self._annual_df("cashflow")
        bs = self._annual_df("balance_sheet")
        if cf.empty or bs.empty or "fcff" not in cf or not self.client._store.get("fcff_verified", False):
            self._unavailable(method, "缺少经来源核对的FCFF（须对齐利息、税盾、营运资本与少数股东）；OCF−Capex不自动视为FCFF")
            return None
        row = bs.iloc[0]
        cash, ibd, minority = self._sf(row.get("money_cap")), debt(row), self._sf(row.get("minority_int"))
        if cash is None or ibd is None or minority is None:
            self._unavailable(method, "现金/有息债务/少数股东权益缺失，不能从企业价值转换归母股权价值")
            return None
        return cash, ibd, minority

    # ------------------------------------------------------------------
    # 1. Classification
    # ------------------------------------------------------------------

    def classify(self) -> dict:
        """Classify company and select valuation methods (classification_rules.md)."""
        income_df = self._annual_df("income")

        # D1: ROE 5yr avg (§12, else derived from §3/§4)
        roe_avg, roe_years = self._roe_stats()

        # D2: Payout 3yr avg (complete fiscal years only)
        payout_lookup = self.client._get_payout_by_year()
        years = [str(r["end_date"])[:4] for _, r in income_df.iterrows()] if not income_df.empty else []
        payout_vals = [payout_lookup[y] for y in years[:3] if y in payout_lookup]
        payout_avg = statistics.mean(payout_vals) if payout_vals else None
        payout_years = len(payout_vals)

        # D3/D4: 5yr revenue / net-profit CAGR
        rev_cagr = self._growth("income", "revenue")
        rev_cagr_pct = rev_cagr * 100 if rev_cagr is not None else None
        np_cagr = self._growth("income", "n_income_attr_p")
        np_cagr_pct = np_cagr * 100 if np_cagr is not None else None

        sample_years = {
            "roe": roe_years, "payout": payout_years,
            "revenue": self._sample_count(income_df, "revenue"),
            "profit": self._sample_count(income_df, "n_income_attr_p"),
        }
        insufficient = [label for label, key in (
            ("ROE_5yr_avg", "roe"), ("Payout_3yr_avg", "payout"),
            ("Revenue_CAGR_5yr", "revenue"), ("Net_Profit_CAGR_5yr", "profit"))
            if sample_years[key] < MIN_SAMPLE_YEARS]

        # A dimension with <3 annual observations earns 0 (classification_rules.md 2.3).
        def _scored(key: str, value, compare) -> bool:
            return (sample_years[key] >= MIN_SAMPLE_YEARS and value is not None and compare(value))

        checks = {
            "roe_blue": _scored("roe", roe_avg, lambda v: v > ROE_BLUE_CHIP_THRESHOLD),
            "payout_blue": _scored("payout", payout_avg, lambda v: v > PAYOUT_BLUE_CHIP_THRESHOLD),
            "rev_blue": _scored("revenue", rev_cagr_pct, lambda v: v < REV_GROWTH_THRESHOLD),
            "rev_growth": _scored("revenue", rev_cagr_pct, lambda v: v > REV_GROWTH_THRESHOLD),
            "np_growth": _scored("profit", np_cagr_pct, lambda v: v > PROFIT_GROWTH_THRESHOLD),
        }
        blue_score = sum(checks[k] for k in ("roe_blue", "payout_blue", "rev_blue"))
        growth_score = sum(checks[k] for k in ("rev_growth", "np_growth"))

        # Determine type (classification_rules.md 2.2)
        if blue_score >= 2 and growth_score == 0:
            company_type = "蓝筹价值型"
        elif growth_score >= 2 and blue_score <= 1:
            company_type = "成长型"
        else:
            company_type = "混合型"

        notes = []
        # Loss company override: growth type, PEG not applicable → weight moves to PS.
        latest_np = self._sf(income_df.iloc[0].get("n_income_attr_p")) if not income_df.empty else None
        loss_override = latest_np is not None and latest_np < 0
        if loss_override:
            company_type = "成长型"
            notes.append("最新年度归母净利润为负：强制成长型，PEG 不适用，其权重转 PS")
        if insufficient:
            notes.append(f"样本年数不足 {MIN_SAMPLE_YEARS} 年（该维度按 0 分）：{'、'.join(insufficient)}")

        weights = dict(METHOD_WEIGHTS.get(company_type, METHOD_WEIGHTS["混合型"]))
        if loss_override:
            peg_weight = weights.pop("PEG", 0)
            weights["PS"] = weights.get("PS", 0) + peg_weight

        # 混合型 DDM is a supplement only when payout > 20%; missing payout must
        # not silently keep a DDM weight (classification_rules.md 3.2).
        ddm_supplement = False
        if company_type == "混合型":
            if payout_avg is not None and payout_avg > 20:
                ddm_supplement = True
            else:
                weights = self._redistribute(weights, "DDM")
        methods = list(weights)

        return {
            "type": company_type,
            "blue_score": blue_score,
            "growth_score": growth_score,
            "roe_avg": roe_avg,
            "payout_avg": payout_avg,
            "rev_cagr_pct": rev_cagr_pct,
            "np_cagr_pct": np_cagr_pct,
            "sample_years": sample_years,
            "insufficient": insufficient,
            "checks": checks,
            "notes": notes,
            "ddm_supplement": ddm_supplement,
            "loss_override": loss_override,
            "methods": methods,
            "weights": weights,
        }

    # ------------------------------------------------------------------
    # 2. WACC
    # ------------------------------------------------------------------

    def compute_wacc(self) -> dict:
        """WACC per valuation_methods.md; missing debt/market cap is never 0."""
        self._clear_unavailable("WACC")
        bi = self._basic_info()
        mkt_cap_mm = bi.get("mkt_cap_mm")
        notes: list[str] = []
        blockers: list[str] = []

        # Rf (§14, default per market)
        rf_df = self.client._store.get("risk_free_rate")
        rf = None
        if rf_df is not None and not rf_df.empty:
            rf = self._sf(rf_df.iloc[0].get("yield"))
        if rf is None:
            rf = self.params["rf_default"]
            notes.append(f"§14无风险利率缺失，采用市场默认 {rf:.2f}%")

        # Beta by market cap; without market cap the default bucket is undefined.
        beta = None
        if mkt_cap_mm is not None and mkt_cap_mm > 0:
            for threshold, b in BETA_BY_CAP:
                if mkt_cap_mm > threshold:
                    beta = b
                    break
        else:
            blockers.append("总市值缺失/非正：无法确定Beta档位与资本结构")

        erp = self.params["erp"]
        ke = rf + beta * erp if beta is not None else None

        income_df = self._annual_df("income")
        bs_df = self._annual_df("balance_sheet")
        finance_exp = None
        if not income_df.empty:
            finance_exp = self._sf(income_df.iloc[0].get("finance_exp"))

        # §4 有息负债 through the shared contract: lease_liab included, and a
        # missing field is "unknown", never 0 (financial_contracts.debt).
        debt_latest = None
        debt_prev = None
        if bs_df.empty:
            blockers.append("资产负债表缺失：有息负债不可得")
        else:
            debt_latest = debt(bs_df.iloc[0])
            if len(bs_df) > 1:
                debt_prev = debt(bs_df.iloc[1])
            if debt_latest is None:
                blockers.append("有息负债字段缺失（缺失≠0）：无法计算债务成本与资本结构")

        avg_debt = None
        if debt_latest is not None and debt_prev is not None and debt_latest + debt_prev > 0:
            avg_debt = (debt_latest + debt_prev) / 2
        elif debt_latest is not None and debt_latest > 0:
            avg_debt = debt_latest
            notes.append("仅最新一年有息负债完整，Kd 用最新年金额近似（文档要求两年均值）")

        if finance_exp is not None and finance_exp > 0 and avg_debt:
            kd_pre = finance_exp / avg_debt * 100
        else:
            kd_pre = rf + 1.0
            notes.append("财务费用非正/缺失或有息负债为0：Kd 取 Rf+1% 参考假设")

        e_val = mkt_cap_mm if mkt_cap_mm is not None and mkt_cap_mm > 0 else None
        d_val = debt_latest / 1e6 if debt_latest is not None else None  # 元 → 百万元
        total = (e_val or 0) + (d_val or 0)
        e_weight = None
        d_weight = None

        # Tax rate (§3 所得税/利润总额 5yr avg, loss years excluded)
        tax_rates = []
        for _, r in income_df.head(5).iterrows():
            tax = self._sf(r.get("income_tax"))
            pre_tax = self._sf(r.get("total_profit"))
            if tax is not None and pre_tax and pre_tax > 0:
                tax_rates.append(tax / pre_tax * 100)
        tax_rate = statistics.mean(tax_rates) if tax_rates else self.params["tax_default"]
        tax_rate = tax_rate if 0 <= tax_rate <= 100 else self.params["tax_default"]

        wacc = None
        if e_val is not None and d_val is not None and total > 0:
            e_weight = e_val / total * 100
            d_weight = d_val / total * 100
            if d_val <= 0:
                wacc = ke
                notes.append("有息负债为0：按文档跳过Kd，WACC = Ke")
            else:
                wacc = ke * e_weight / 100 + kd_pre * (1 - tax_rate / 100) * d_weight / 100

        blockers = list(dict.fromkeys(blockers))
        if blockers:
            wacc = None
            e_weight = d_weight = None
            self._unavailable("WACC", "；".join(blockers))
        elif wacc is None:
            self._unavailable("WACC", "资本结构不完整，WACC 无法计算")

        return {
            "rf": rf, "beta": beta, "erp": erp, "ke": ke,
            "kd_pre": kd_pre, "tax_rate": tax_rate,
            "e_weight": e_weight, "d_weight": d_weight,
            "wacc": wacc,
            "status": "参考假设，非实测融资成本；净财务费用含利息收入/汇兑，Kd仅近似",
            "mkt_cap_mm": mkt_cap_mm,
            "debt_mm": d_val,
            "e_val": e_val, "d_val": d_val,
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # 3. DCF (Stable)
    # ------------------------------------------------------------------

    def dcf_stable(self, wacc_data: dict) -> dict | None:
        """DCF for stable companies (蓝筹/混合), valuation_methods.md 方法1."""
        self._clear_unavailable("DCF")
        wacc = wacc_data.get("wacc")
        if wacc is None:
            return self._unavailable("DCF", "WACC不可用（市值或有息负债缺失），不做企业价值折现")
        context = self._fcff_context("DCF")
        if context is None:
            return None
        cash, debt_raw, minority = context

        cf_df = self._annual_df("cashflow")
        bi = self._basic_info()
        if cf_df.empty or not bi.get("total_shares"):
            return self._unavailable("DCF", "现金流表缺失或总股本缺失")
        total_shares = bi["total_shares"]

        # FCF series (verified FCFF only, enforced by _fcff_context)
        fcf_list = [self._sf(r.get("fcff")) for _, r in cf_df.head(5).iterrows()]
        if len(fcf_list) < 3 or any(v is None for v in fcf_list):
            return self._unavailable("DCF", "有效FCFF样本不足3年或含缺失年份")

        # Step 1: 3yr mean, excluding negative years deviating >50% from the mean.
        fcf_base, fcf_excluded = self._fcf_base(fcf_list)
        negative_years = sum(1 for f in fcf_list if f <= 0)
        if fcf_base <= 0:
            return self._unavailable(
                "DCF", "历史FCF基线非正（全部为负/异常）：不输出负目标价，按分类规则降级为DCF情景版")

        # Step 2: growth assumptions. Doc gives no history cap → conservative 30%.
        growth_capped = False
        fcf_cagr = self._growth("cashflow", "fcff")
        if fcf_cagr is not None and fcf_cagr > 0:
            g_hist = fcf_cagr * 100
            g_basis = "5年FCFF CAGR"
        else:
            rev_cagr = self._growth("income", "revenue")
            if rev_cagr is not None:
                g_hist = rev_cagr * 100
                g_basis = "FCFF存在负值，改用营收CAGR"
            else:
                g_hist = 5.0
                g_basis = "历史增速不可得，取5%参考假设"
        if g_hist > HISTORY_GROWTH_CAP:
            g_hist = HISTORY_GROWTH_CAP
            growth_capped = True
            g_basis += f"；超过{HISTORY_GROWTH_CAP:.0f}%上限，已裁剪（文档未设上限）"

        g_conservative = g_hist * 0.8
        g_terminal = min(self.params["g_terminal"], self.params["gdp_long_run"])
        if wacc <= 0 or g_conservative <= -100:
            return self._unavailable("DCF", "折现率或增长假设无效")

        # Step 4: g_terminal must stay below WACC, else clamp to WACC-2%.
        terminal_clamped = False
        if g_terminal >= wacc:
            g_terminal = wacc - 2.0
            terminal_clamped = True

        g_fade = (g_conservative + g_terminal) / 2
        g_fade2 = (g_fade + g_terminal) / 2

        # Step 3: project FCF
        growth_rates = [g_conservative, g_conservative, g_fade, g_fade2, g_terminal]
        projected = []
        prev = fcf_base
        for g in growth_rates:
            prev = prev * (1 + g / 100)
            projected.append(prev)

        # Step 4/5: terminal value and discounting
        tv = projected[-1] * (1 + g_terminal / 100) / (wacc / 100 - g_terminal / 100)
        pv_fcf = sum(f / (1 + wacc / 100) ** (i + 1) for i, f in enumerate(projected))
        pv_tv = tv / (1 + wacc / 100) ** 5
        ev = pv_fcf + pv_tv
        tv_pct = pv_tv / ev * 100 if ev > 0 else 0

        # Step 6: cash and debt
        equity_value = ev + cash - debt_raw - minority
        if equity_value <= 0:
            return self._unavailable("DCF", "归母股权价值非正，不输出负目标价")
        intrinsic = equity_value / total_shares if total_shares > 0 else 0

        # Sensitivity matrix 5x5
        wacc_range = [wacc - 2, wacc - 1, wacc, wacc + 1, wacc + 2]
        g_range = [g_terminal - 1, g_terminal - 0.5, g_terminal, g_terminal + 0.5, g_terminal + 1]
        sensitivity = []
        for w in wacc_range:
            row = []
            for g in g_range:
                if g >= w or w <= 0 or g <= -100:
                    row.append(None)
                    continue
                # Simplified: re-project with same cash flows but different TV
                tv_s = projected[-1] * (1 + g / 100) / (w / 100 - g / 100)
                pv_fcf_s = sum(f / (1 + w / 100) ** (i + 1) for i, f in enumerate(projected))
                pv_tv_s = tv_s / (1 + w / 100) ** 5
                ev_s = pv_fcf_s + pv_tv_s
                eq_s = ev_s + cash - debt_raw - minority
                row.append(eq_s / total_shares if eq_s > 0 else None)
            sensitivity.append(row)

        return {
            "method": "DCF",
            "intrinsic": intrinsic,
            "fcf_base": fcf_base,
            "fcf_excluded": fcf_excluded,
            "negative_years": negative_years,
            "g_hist": g_hist,
            "g_basis": g_basis,
            "growth_capped": growth_capped,
            "g_conservative": g_conservative,
            "g_terminal": g_terminal,
            "terminal_clamped": terminal_clamped,
            "tv_pct": tv_pct,
            "tv_warning": tv_pct > TV_SHARE_WARN_PCT,
            "all_negative": all(f <= 0 for f in fcf_list),
            "projected_fcf": projected,
            "growth_rates": growth_rates,
            "pv_fcf": pv_fcf,
            "pv_tv": pv_tv,
            "ev": ev,
            "cash": cash,
            "debt_raw": debt_raw,
            "equity_value": equity_value,
            "sensitivity": sensitivity,
            "wacc_range": wacc_range,
            "g_range": g_range,
        }

    # ------------------------------------------------------------------
    # 4. DCF (Scenarios)
    # ------------------------------------------------------------------

    def dcf_scenarios(self, wacc_data: dict) -> dict | None:
        """DCF with 3 scenarios for growth companies (valuation_methods.md 方法5)."""
        self._clear_unavailable("DCF_Scenarios")
        wacc = wacc_data.get("wacc")
        if wacc is None:
            return self._unavailable("DCF_Scenarios", "WACC不可用（市值或有息负债缺失）")
        cf_df = self._annual_df("cashflow")
        income_df = self._annual_df("income")
        bi = self._basic_info()
        context = self._fcff_context("DCF_Scenarios")
        if context is None:
            return None
        cash, debt_raw, minority = context

        if cf_df.empty or income_df.empty or not bi.get("total_shares"):
            return self._unavailable("DCF_Scenarios", "现金流/利润表缺失或总股本缺失")

        total_shares = bi["total_shares"]
        g_terminal = min(self.params["g_terminal"], self.params["gdp_long_run"])
        if g_terminal >= wacc:
            g_terminal = wacc - 2.0

        # Historical metrics
        rev_cagr = self._growth("income", "revenue")
        rev_cagr_pct = rev_cagr * 100 if rev_cagr is not None else 10.0

        # Latest financials
        latest_inc = income_df.iloc[0]
        latest_cf = cf_df.iloc[0]
        revenue = self._sf(latest_inc.get("revenue")) or 0
        fcff = self._sf(latest_cf.get("fcff"))
        if revenue <= 0 or fcff is None or fcff <= 0 or wacc <= 0 or rev_cagr is None:
            return self._unavailable("DCF_Scenarios", "收入/已验证FCFF/增长率/折现率不满足情景模型条件")
        np_val = self._sf(latest_inc.get("n_income_attr_p")) or 0
        net_margin = np_val / revenue * 100 if revenue > 0 else 5.0

        capex = self._sf(latest_cf.get("c_pay_acq_const_fiolta"))
        if capex is None:
            return self._unavailable("DCF_Scenarios", "资本支出缺失，不能按0建立情景")
        capex_rev = abs(capex) / revenue

        def _project_scenario(rev_g_factors, margin_adj, capex_factor):
            projected = []
            rev = revenue
            for i in range(5):
                g = rev_g_factors[i] if i < len(rev_g_factors) else rev_g_factors[-1]
                rev = rev * (1 + g / 100)
                # Explicit FCFF-margin scenario. Includes working-capital,
                # tax/interest treatment embedded in the verified base FCFF.
                margin = fcff / revenue + margin_adj / 100 * (i + 1)
                fcf_s = rev * margin - rev * capex_rev * (capex_factor - 1)
                projected.append(fcf_s)
            return projected

        # Optimistic: historical CAGR maintained, margin improves
        opt_g = [rev_cagr_pct] * 5
        opt_fcf = _project_scenario(opt_g, 0.5, 1.0)

        # Base: CAGR × 0.7, margin flat
        base_g = [rev_cagr_pct * 0.7] * 5
        base_fcf = _project_scenario(base_g, 0.0, 1.0)

        # Pessimistic: CAGR × 0.4 for 2yr, 0% after, margin compresses
        pess_g = [rev_cagr_pct * 0.4, rev_cagr_pct * 0.4, 0, 0, 0]
        pess_fcf = _project_scenario(pess_g, -0.3, 1.2)

        def _dcf_value(projected_fcf):
            if not projected_fcf or projected_fcf[-1] <= 0:
                tv = 0
            else:
                tv = projected_fcf[-1] * (1 + g_terminal / 100) / (wacc / 100 - g_terminal / 100)
            pv_fcf = sum(f / (1 + wacc / 100) ** (i + 1) for i, f in enumerate(projected_fcf))
            pv_tv = tv / (1 + wacc / 100) ** 5
            ev = pv_fcf + pv_tv
            eq = ev + cash - debt_raw - minority
            return eq / total_shares if total_shares > 0 else 0

        v_opt = _dcf_value(opt_fcf)
        v_base = _dcf_value(base_fcf)
        v_pess = _dcf_value(pess_fcf)

        weighted = 0.25 * v_opt + 0.50 * v_base + 0.25 * v_pess
        if min(v_opt, v_base, v_pess) <= 0:
            return self._unavailable("DCF_Scenarios", "存在非正股权价值情景，不生成概率加权买点")

        return {
            "method": "DCF_Scenarios",
            "intrinsic": weighted,
            "v_optimistic": v_opt,
            "v_base": v_base,
            "v_pessimistic": v_pess,
            "rev_cagr_pct": rev_cagr_pct,
            "net_margin": net_margin,
            "scenarios": {
                "optimistic": {"growth": opt_g, "fcf": opt_fcf, "value": v_opt},
                "base": {"growth": base_g, "fcf": base_fcf, "value": v_base},
                "pessimistic": {"growth": pess_g, "fcf": pess_fcf, "value": v_pess},
            },
        }

    # ------------------------------------------------------------------
    # 5. DDM
    # ------------------------------------------------------------------

    def _aggregate_annual_dps(self) -> list[tuple[str, float]]:
        """Complete implemented fiscal years only; never backfill from CF."""
        store = self.client._store
        rows = annual_dividends(store.get("dividends_all", store.get("dividends")), store.get("as_of"))
        return [(r["year"], r["dps"]) for r in rows[:5]]

    def ddm(self, ke: float | None, terminal_growth: float | None = None) -> dict | None:
        """Dividend Discount Model (Gordon or two-stage), valuation_methods.md 方法2.

        Terminal growth defaults to min(3%, long-run GDP growth); it is an
        explicit scenario assumption, not a forecast.
        """
        self._clear_unavailable("DDM")
        if ke is None or ke <= 0:
            return self._unavailable("DDM", "Ke不可用（无风险利率/市值缺失），不能折现股利")
        gdp_long_run = self.params["gdp_long_run"]
        if terminal_growth is None:
            terminal_growth = min(DDM_TERMINAL_CAP, gdp_long_run)
            terminal_basis = f"min(3%, 长期GDP {gdp_long_run:.1f}%) = {terminal_growth:.2f}%"
        else:
            terminal_basis = "外部指定假设"
        if not -100 < terminal_growth < ke:
            return self._unavailable(
                "DDM", f"终值增速{terminal_growth:.2f}%须落在(-100%, Ke={ke:.2f}%)区间内")

        # Aggregate annual DPS (fixes interim+final double-counting bug)
        annual_dps = self._aggregate_annual_dps()
        if len(annual_dps) < 3:
            return self._unavailable("DDM", "完整分红年度不足3年")

        dps_latest = annual_dps[0][1]
        dps_list = [v for _, v in annual_dps]
        if any(v <= 0 for v in dps_list[:3]):
            return self._unavailable("DDM", "最近完整年度存在零/负分红，稳定分红模型不适用")

        # Payout ratio stats (3yr avg / std)
        payout_lookup = self.client._get_payout_by_year()
        payout_vals = list(payout_lookup.values())[:3]
        payout_avg = statistics.mean(payout_vals) if payout_vals else None
        payout_std = statistics.stdev(payout_vals) if len(payout_vals) >= 2 else 0

        # DPS CAGR
        dps_cagr = self._cagr(dps_list, [y for y, _ in annual_dps])
        if dps_cagr is None:
            return self._unavailable("DDM", "分红增长样本端点无效")
        dps_cagr_pct = dps_cagr * 100

        # ROE for sustainable growth: g = min(DPS_CAGR, ROE × (1 - payout))
        roe_avg, _ = self._roe_stats()
        sustainable = (roe_avg * (1 - payout_avg / 100)
                       if roe_avg is not None and payout_avg is not None else None)

        # Model selection (Step 2)
        cv = payout_std / payout_avg if payout_avg and payout_avg > 0 else 0
        use_gordon = 0 <= dps_cagr_pct < 5 and cv < 0.20
        g1 = dps_cagr_pct
        g2 = terminal_growth
        g_terminal = terminal_growth

        if use_gordon:
            # Step 3a: g = min(DPS_CAGR, ROE_avg × (1 - Payout_avg))
            g = min(dps_cagr_pct, sustainable) if sustainable is not None else dps_cagr_pct
            if g < ke:
                intrinsic = dps_latest * (1 + g / 100) / (ke / 100 - g / 100)
                model_type = "Gordon"
                g_used = g
                g_terminal = g
            else:
                use_gordon = False  # g >= Ke → fall back to two-stage

        if not use_gordon:
            # Step 3b: phase 1 growth is historical, phase 2 is the explicit terminal assumption
            g_used = g1
            pv_phase1 = 0.0
            dps_t = dps_latest
            for t in range(1, 6):
                dps_t = dps_t * (1 + g1 / 100)
                pv_phase1 += dps_t / (1 + ke / 100) ** t
            dps_6 = dps_t * (1 + g2 / 100)
            pv_phase2 = (dps_6 / (ke / 100 - g2 / 100)) / (1 + ke / 100) ** 5
            intrinsic = pv_phase1 + pv_phase2
            model_type = "Two-stage"

        # Sensitivity 5x5
        ke_range = [ke - 2, ke - 1, ke, ke + 1, ke + 2]
        g_sens_range = [g_used - 1, g_used - 0.5, g_used, g_used + 0.5, g_used + 1]
        sensitivity = []
        for k in ke_range:
            row = []
            for gs in g_sens_range:
                if k <= 0 or gs <= -100 or (use_gordon and gs >= k) or (not use_gordon and g2 >= k):
                    row.append(None)
                    continue
                if use_gordon:
                    v = dps_latest * (1 + gs / 100) / (k / 100 - gs / 100)
                else:
                    pv1 = 0.0
                    d = dps_latest
                    for t in range(1, 6):
                        d = d * (1 + gs / 100)
                        pv1 += d / (1 + k / 100) ** t
                    d6 = d * (1 + g2 / 100)
                    pv2 = (d6 / (k / 100 - g2 / 100)) / (1 + k / 100) ** 5
                    v = pv1 + pv2
                row.append(v)
            sensitivity.append(row)

        if use_gordon:
            growth_basis = ("Gordon：g=min(历史DPS CAGR, ROE×(1−支付率))=%.2f%%"
                            % g_used)
        else:
            growth_basis = (f"两阶段：前5年 g1=历史DPS CAGR {g1:.2f}%；"
                            f"终值 g2={terminal_basis}")
        return {
            "method": "DDM",
            "intrinsic": intrinsic,
            "model_type": model_type,
            "dps_latest": dps_latest,
            "dps_cagr_pct": dps_cagr_pct,
            "g_used": g_used,
            "g1": g_used if use_gordon else g1,
            "g2": g2,
            "g_terminal": g_terminal,
            "g1_basis": "历史DPS CAGR（前5年）" if not use_gordon else "Gordon 单阶段",
            "g2_basis": terminal_basis,
            "sustainable_growth": sustainable,
            "growth_basis": growth_basis,
            "dividend_year": annual_dps[0][0],
            "assumptions": ("完整年度分红；增长为显式情景假设（不是预测或买点）："
                            + growth_basis + "；Ke=%.2f%%" % ke),
            "payout_avg": payout_avg,
            "payout_std": payout_std,
            "sensitivity": sensitivity,
            "ke_range": ke_range,
            "g_range": g_sens_range,
        }

    # ------------------------------------------------------------------
    # 6. PE Band
    # ------------------------------------------------------------------

    def pe_band(self) -> dict | None:
        """Historical PE range analysis (all positive-PE years retained)."""
        self._clear_unavailable("PE_Band")
        income_df = self._annual_df("income")
        wp_df = self.client._store.get("weekly_prices")
        bi = self._basic_info()

        if income_df.empty or wp_df is None or wp_df.empty:
            return self._unavailable("PE_Band", "利润表或周线价格缺失")
        if self.client._store.get("price_basis") != "unadjusted":
            return self._unavailable("PE_Band", "历史价格未确认未复权口径，不能与同期EPS配对")

        # Build year-end price lookup
        wp_sorted = wp_df.sort_values("trade_date", ascending=False)
        year_end_prices = {}
        for _, r in wp_sorted.iterrows():
            yr = str(r["trade_date"])[:4]
            if yr not in year_end_prices:
                close = self._sf(r.get("close"))
                if close:
                    year_end_prices[yr] = close

        # Compute PE series. Only loss years (EPS <= 0) are excluded: dropping
        # high-PE years one-sidedly drags the median/reference price down, so all
        # positive PEs are kept unless a documented two-sided winsorization is added.
        pe_series = []
        pe_years = []
        eps_vals = []
        for _, r in income_df.iterrows():
            yr = str(r["end_date"])[:4]
            eps = self._sf(r.get("basic_eps"))
            if eps and eps > 0:
                eps_vals.append(eps)
                if yr in year_end_prices:
                    pe_series.append(year_end_prices[yr] / eps)
                    pe_years.append(yr)

        if len(pe_series) < 3:
            return self._unavailable(
                "PE_Band", "可核实的未复权年末价格×同期正EPS样本不足3年（不输出单年PE区间）")

        pe_sorted = sorted(pe_series)
        n = len(pe_sorted)

        def _percentile(data, p):
            k = (len(data) - 1) * p / 100
            f = math.floor(k)
            c = math.ceil(k)
            if f == c:
                return data[int(k)]
            return data[f] * (c - k) + data[c] * (k - f)

        pe_stats = {
            "min": pe_sorted[0],
            "p25": _percentile(pe_sorted, 25),
            "median": _percentile(pe_sorted, 50),
            "p75": _percentile(pe_sorted, 75),
            "max": pe_sorted[-1],
            "avg": statistics.mean(pe_series),
        }

        # Normalized EPS (3yr avg)
        eps_norm = statistics.mean(eps_vals[:3]) if len(eps_vals) >= 3 else eps_vals[0] if eps_vals else None
        if eps_norm is None:
            return self._unavailable("PE_Band", "正常化EPS不可得（无正EPS年份）")

        intrinsic = pe_stats["median"] * eps_norm
        low = pe_stats["p25"] * eps_norm
        high = pe_stats["p75"] * eps_norm

        # Current PE percentile
        current_eps = self._sf(income_df.iloc[0].get("basic_eps"))
        current_pe = bi.get("close") / current_eps if bi.get("close") and current_eps and current_eps > 0 else None
        current_pct = None
        if current_pe and current_pe > 0:
            below = sum(1 for p in pe_series if p <= current_pe)
            current_pct = below / len(pe_series) * 100

        high_pe_years = [y for y, p in zip(pe_years, pe_series) if p >= 100]
        return {
            "method": "PE_Band",
            "intrinsic": intrinsic,
            "low": low,
            "high": high,
            "pe_stats": pe_stats,
            "eps_norm": eps_norm,
            "current_pe": current_pe,
            "current_pct": current_pct,
            "pe_count": len(pe_series),
            "pe_years": pe_years,
            "high_pe_years": high_pe_years,
            "pe_note": "仅剔除EPS≤0（亏损）年度；全部正PE年份保留（含PE≥100），未做单侧截断",
        }

    # ------------------------------------------------------------------
    # 7. PEG
    # ------------------------------------------------------------------

    def peg(self) -> dict | None:
        """PEG ratio analysis (loss years disqualify the method per the formulary)."""
        self._clear_unavailable("PEG")
        bi = self._basic_info()
        income_df = self._annual_df("income")

        pe = bi.get("pe_ttm")
        if pe is None or pe <= 0:
            return self._unavailable("PEG", "PE(TTM)缺失或非正，PEG不适用")

        # Step 1: a loss year makes the growth series meaningless → skip PEG.
        np_series = [self._sf(r.get("n_income_attr_p")) for _, r in income_df.head(5).iterrows()]
        loss_years = [str(r["end_date"])[:4] for (_, r), value in
                      zip(income_df.head(5).iterrows(), np_series) if value is not None and value < 0]
        if loss_years:
            return self._unavailable(
                "PEG", f"历史净利润存在亏损年份（{'、'.join(loss_years)}），PEG不适用")

        # Earnings growth (3yr CAGR first, fallback to 5yr; >80% is abnormal → 5yr)
        g = self._growth("income", "n_income_attr_p", 4)
        if g is None or g <= 0:
            g = self._growth("income", "n_income_attr_p")
        if g is None or g <= 0:
            return self._unavailable("PEG", "归母净利润3/5年CAGR缺失或非正，PEG不适用")

        g_pct = g * 100
        if g_pct > 80:
            g5 = self._growth("income", "n_income_attr_p")
            if g5 is not None and g5 > 0:
                g_pct = g5 * 100

        peg_val = pe / g_pct if g_pct > 0 else None
        if peg_val is None:
            return self._unavailable("PEG", "盈利增速非正，PEG无定义")

        # Fair price at PEG = 1
        eps_ttm = bi.get("close", 0) / pe if pe > 0 else None
        fair_pe = g_pct * 1.0
        fair_price = fair_pe * eps_ttm if eps_ttm else None

        # Sensitivity
        sensitivity = []
        for g_adj in [-10, -5, 0, 5, 10]:
            g_s = g_pct + g_adj
            if g_s <= 0:
                sensitivity.append({"g": g_s, "peg": None, "fair_pe": None, "fair_price": None})
                continue
            peg_s = pe / g_s
            fp_s = g_s * eps_ttm if eps_ttm else None
            sensitivity.append({"g": g_s, "peg": peg_s, "fair_pe": g_s, "fair_price": fp_s})

        return {
            "method": "PEG",
            "intrinsic": fair_price,
            "peg_value": peg_val,
            "pe": pe,
            "g_pct": g_pct,
            "eps_ttm": eps_ttm,
            "fair_pe": fair_pe,
            "fair_price": fair_price,
            "sensitivity": sensitivity,
        }

    # ------------------------------------------------------------------
    # 8. PS
    # ------------------------------------------------------------------

    def ps(self) -> dict | None:
        """Price-to-Sales analysis."""
        self._clear_unavailable("PS")
        income_df = self._annual_df("income")
        wp_df = self.client._store.get("weekly_prices")
        bi = self._basic_info()
        if statement_issues(self.client._store):
            return self._unavailable("PS", "三表期间/公告版本未对齐，不拼接收入与历史股本")

        if income_df.empty or not bi.get("total_shares") or not bi.get("mkt_cap_mm"):
            return self._unavailable("PS", "利润表/总股本/总市值缺失，PS不适用")

        total_shares = bi["total_shares"]
        mkt_cap_mm = bi["mkt_cap_mm"]

        # Current PS
        latest_rev = self._sf(income_df.iloc[0].get("revenue"))
        if not latest_rev or latest_rev <= 0:
            return self._unavailable("PS", "最新年营业收入缺失或非正，PS不适用")

        rev_mm = latest_rev / 1e6 if self.market == "A" else latest_rev / 1e6
        ps_current = mkt_cap_mm / rev_mm if rev_mm > 0 else None

        # Historical PS
        year_end_prices = {}
        if wp_df is not None and not wp_df.empty:
            for _, r in wp_df.sort_values("trade_date", ascending=False).iterrows():
                yr = str(r["trade_date"])[:4]
                if yr not in year_end_prices:
                    close = self._sf(r.get("close"))
                    if close:
                        year_end_prices[yr] = close

        ps_series = []
        if self.client._store.get("price_basis") != "unadjusted":
            return self._unavailable("PS", "历史价格未确认未复权口径")
        bs = self._annual_df("balance_sheet")
        shares_by_year = {str(r["end_date"])[:4]: self._sf(r.get("total_shares")) for _, r in bs.iterrows()}
        for _, r in income_df.iterrows():
            yr = str(r["end_date"])[:4]
            rev = self._sf(r.get("revenue"))
            historical_shares = shares_by_year.get(yr)
            if rev and rev > 0 and yr in year_end_prices and historical_shares and historical_shares > 0:
                # Actual historical shares, not monetary paid-in capital:
                # A-share par value is not universally one yuan per share.
                mc_yr = year_end_prices[yr] * historical_shares
                rev_yr = rev
                ps_yr = mc_yr / rev_yr if rev_yr > 0 else None
                if ps_yr and ps_yr > 0:
                    ps_series.append(ps_yr)

        if len(ps_series) < 3:
            return self._unavailable("PS", "可核实的历史价格/实际股数/收入同期间样本不足3年；不把股本金額当股数")

        ps_sorted = sorted(ps_series)

        def _pctl(data, p):
            k = (len(data) - 1) * p / 100
            f = math.floor(k)
            c = math.ceil(k)
            if f == c:
                return data[int(k)]
            return data[f] * (c - k) + data[c] * (k - f)

        ps_stats = {
            "min": ps_sorted[0],
            "p25": _pctl(ps_sorted, 25),
            "median": _pctl(ps_sorted, 50),
            "p75": _pctl(ps_sorted, 75),
            "max": ps_sorted[-1],
        }

        fair_value_mm = ps_stats["median"] * rev_mm
        fair_price = fair_value_mm * 1e6 / total_shares if total_shares > 0 else None
        low_price = ps_stats["p25"] * rev_mm * 1e6 / total_shares if total_shares > 0 else None
        high_price = ps_stats["p75"] * rev_mm * 1e6 / total_shares if total_shares > 0 else None

        return {
            "method": "PS",
            "intrinsic": fair_price,
            "low": low_price,
            "high": high_price,
            "ps_current": ps_current,
            "ps_stats": ps_stats,
            "revenue_mm": rev_mm,
        }

    # ------------------------------------------------------------------
    # 9. Reverse Valuation (反向估值)
    # ------------------------------------------------------------------

    def reverse_valuation(self, wacc_data: dict, classification: dict) -> dict:
        """Reverse-engineer what growth the current price implies.

        R1/R2/R3 per valuation_methods.md; a proxy cash stream is never used to
        imply an enterprise-DCF growth rate (only a source-reconciled FCFF is).
        """
        bi = self._basic_info()
        close = bi.get("close")
        pe = bi.get("pe_ttm")
        mkt_cap_mm = bi.get("mkt_cap_mm")
        if not close or not mkt_cap_mm or mkt_cap_mm <= 0:
            return {}

        wacc = wacc_data.get("wacc")
        ke = wacc_data.get("ke")

        # Actual growth rates (from classification)
        actual_np_cagr = classification.get("np_cagr_pct")
        actual_rev_cagr = classification.get("rev_cagr_pct")
        actual_growth = actual_np_cagr if actual_np_cagr is not None else actual_rev_cagr

        # --- R2: FCF yield / implied perpetual FCF growth ---
        cf_df = self._annual_df("cashflow")
        fcf_base_mm = None
        fcf_basis = None
        if self.client._store.get("fcff_verified", False) and not cf_df.empty and "fcff" in cf_df:
            verified_values = [self._sf(r.get("fcff")) for _, r in cf_df.head(3).iterrows()]
            if verified_values and all(v is not None for v in verified_values):
                fcf_base_mm = statistics.mean(verified_values) / 1e6
                fcf_basis = "verified_fcff"
        if fcf_base_mm is None:
            proxy_values = []
            for _, r in cf_df.head(3).iterrows():
                ocf = self._sf(r.get("n_cashflow_act"))
                capex = self._sf(r.get("c_pay_acq_const_fiolta"))
                if ocf is not None and capex is not None:
                    proxy_values.append(ocf - abs(capex))
            if proxy_values:
                fcf_base_mm = statistics.mean(proxy_values) / 1e6
                fcf_basis = "consolidated_proxy"

        fcf_yield = (fcf_base_mm / mkt_cap_mm * 100
                     if fcf_base_mm is not None and mkt_cap_mm > 0 else None)
        implied_g_fcf = None
        implied_fcf_note = None
        if fcf_yield is None:
            implied_fcf_note = "现金流样本缺失，无法反解"
        elif fcf_basis == "verified_fcff" and wacc is not None:
            # Doc R2: g = WACC − FCF_yield; note this uses market cap as the
            # enterprise-value proxy (net debt not deducted in the formulary).
            implied_g_fcf = wacc - fcf_yield
            implied_fcf_note = "文档口径：以市值近似企业价值（未扣净债务）"
        else:
            implied_fcf_note = "FCFF未经来源核对（OCF−Capex仅为合并现金参考代理），不反解增长；仅列参考收益率"

        # --- R1: Reverse PE (E/P = Ke - g) ---
        implied_g_earnings = None
        ep_ratio = None
        if pe and pe > 0 and ke is not None:
            ep_ratio = 1 / pe * 100
            implied_g_earnings = ke - ep_ratio

        # --- R3: Reverse DDM (P = DPS*(1+g)/(Ke-g)) ---
        annual_dps = self._aggregate_annual_dps()
        implied_g_dividend = None
        dps_latest = None
        div_yield = None
        if annual_dps and close > 0 and ke is not None:
            dps_latest = annual_dps[0][1]
            div_yield = dps_latest / close * 100
            implied_g_dividend = (close * ke / 100 - dps_latest) / (close + dps_latest) * 100

        # --- Implied required return (doc: r_implied = FCF_yield + g_actual) ---
        implied_wacc = None
        if fcf_yield is not None and actual_growth is not None:
            implied_wacc = fcf_yield + actual_growth

        # --- Growth discount: thresholds from valuation_methods.md ---
        growth_discount = None
        growth_discount_label = None
        if actual_np_cagr is not None and implied_g_earnings is not None:
            growth_discount = actual_np_cagr - implied_g_earnings
            growth_discount_label = self._growth_discount_label(growth_discount)

        return {
            "fcf_yield": fcf_yield,
            "fcf_basis": fcf_basis,
            "implied_g_fcf": implied_g_fcf,
            "implied_fcf_note": implied_fcf_note,
            "ep_ratio": ep_ratio,
            "implied_g_earnings": implied_g_earnings,
            "div_yield": div_yield,
            "implied_g_dividend": implied_g_dividend,
            "implied_wacc": implied_wacc,
            "actual_np_cagr": actual_np_cagr,
            "actual_rev_cagr": actual_rev_cagr,
            "actual_growth": actual_growth,
            "growth_discount": growth_discount,
            "growth_discount_label": growth_discount_label,
            "dps_latest": dps_latest,
            "wacc": wacc,
            "ke": ke,
        }

    @staticmethod
    def _growth_discount_label(gap_pct: float) -> str:
        """valuation_methods.md 增长折价分档: >8 / 3-8 / 0-3 / <0 pct."""
        if gap_pct > 8:
            return "市场几乎未为增长付费（典型蓝筹困境）"
        if gap_pct > 3:
            return "市场部分认可增长，但有显著折价"
        if gap_pct > 0:
            return "市场基本定价了增长"
        return "市场给予增长溢价（成长股特征）"

    # ------------------------------------------------------------------
    # 10. Cross-validation
    # ------------------------------------------------------------------

    def cross_validate(self, results: list[dict], weights: dict) -> dict:
        """Cross-validate results and expose which configured methods were dropped.

        A method that raised is reported as failed (not "数据不足"); both failed
        and unavailable methods are listed so weight redistribution is auditable.
        """
        valid = [(r["method"], r["intrinsic"]) for r in results
                 if r is not None and number(r.get("intrinsic")) is not None and r["intrinsic"] > 0]

        configured = {m for m, w in weights.items() if w and w > 0}
        available_methods = {m for m, _ in valid}
        failed = {m: reason for m, reason in self.unavailable.items()
                  if m in configured and str(reason).startswith(FAILED_PREFIX)}
        dropped = {m: self.unavailable.get(m, "未产出参考价（数据/口径不满足）")
                   for m in configured if m not in available_methods}

        if not valid:
            return {"weighted_avg": None, "cv": None, "consistency": "N/A", "range": {},
                    "methods": [], "active_weights": {}, "dropped": dropped, "failed": failed}

        # Redistribute weights to available methods
        active_weights = {m: max(0, weights.get(m, 0)) for m in available_methods}
        total_w = sum(active_weights.values())
        if total_w > 0:
            active_weights = {m: w / total_w * 100 for m, w in active_weights.items()}
        else:
            active_weights = {m: 100 / len(available_methods) for m in available_methods}

        # Weighted average
        weighted_sum = sum(v * active_weights.get(m, 100 / len(valid)) / 100 for m, v in valid)

        # CV
        values = [v for _, v in valid]
        mean_v = statistics.mean(values)
        std_v = statistics.stdev(values) if len(values) >= 2 else 0
        cv = std_v / mean_v * 100 if mean_v > 0 else 0

        if len(valid) < 2:
            consistency = "单一方法，未形成交叉验证"
        elif cv < 15:
            consistency = "高"
        elif cv < 30:
            consistency = "中"
        else:
            consistency = "低"

        return {
            "weighted_avg": weighted_sum,
            "cv": cv,
            "consistency": consistency,
            "methods": valid,
            "active_weights": active_weights,
            "dropped": dropped,
            "failed": failed,
            "range": {
                "conservative": min(values),
                "central": weighted_sum,
                "optimistic": max(values),
            },
        }

    # ------------------------------------------------------------------
    # 10. Markdown Output
    # ------------------------------------------------------------------

    def _fmt(self, val, divider=1, decimals=2) -> str:
        """Format a number for display."""
        if val is None:
            return "—"
        return f"{val / divider:,.{decimals}f}"

    def _fmt_pct(self, val) -> str:
        if val is None:
            return "—"
        return f"{val:.2f}%"

    @staticmethod
    def _fmt_weight(value) -> str:
        if value is None:
            return "—"
        value = float(value)
        return f"{int(value)}%" if value.is_integer() else f"{value:.1f}%"

    @staticmethod
    def _mark(ok) -> str:
        return "✓" if ok else "✗"

    def _render_sensitivity(self, matrix, row_labels, col_labels, row_name, col_name) -> str:
        """Render a sensitivity matrix as markdown table."""
        headers = [f"{row_name}\\{col_name}"] + [self._fmt_pct(c) for c in col_labels]
        rows = []
        for i, r_label in enumerate(row_labels):
            row = [self._fmt_pct(r_label)]
            for j in range(len(col_labels)):
                val = matrix[i][j] if i < len(matrix) and j < len(matrix[i]) else None
                row.append(self._fmt(val) if val else "N/A")
            rows.append(row)
        return format_table(headers, rows, alignments=["l"] + ["r"] * len(col_labels))

    def generate_output(self, classification, wacc_data, method_results, xval, reverse) -> str:
        """Generate valuation_computed.md."""
        bi = self._basic_info()
        lines = []

        lines.append(f"# 估值计算结果 — {bi.get('name', '')}（{self.ts_code}）")
        lines.append("")
        lines.append(f"*计算时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        lines.append(f"*计算引擎: valuation_engine.py v1.0*")
        lines.append("")
        issues = statement_issues(self.client._store)
        if issues:
            lines.append("> ⚠️ 三表口径未闭合；下列可运行模型仅为参考情景，不构成已验证分类/评级/买点。")
            lines.extend(f"- {issue}" for issue in issues)
            lines.append("")
        lines.append("---")
        lines.append("")

        # --- Section 1: Classification ---
        lines.append(format_header(2, "一、公司分类"))
        lines.append("")

        cls = classification
        checks = cls.get("checks", {})
        cls_rows = [
            ["ROE_5yr_avg", self._fmt_pct(cls["roe_avg"]), "> 15%",
             self._mark(checks.get("roe_blue")), "—", "—"],
            ["Payout_3yr_avg", self._fmt_pct(cls["payout_avg"]), "> 30%",
             self._mark(checks.get("payout_blue")), "—", "—"],
            ["Revenue_CAGR_5yr", self._fmt_pct(cls["rev_cagr_pct"]),
             f"< {REV_GROWTH_THRESHOLD}%", self._mark(checks.get("rev_blue")),
             f"> {REV_GROWTH_THRESHOLD}%", self._mark(checks.get("rev_growth"))],
            ["Net_Profit_CAGR_5yr", self._fmt_pct(cls["np_cagr_pct"]), "—", "—",
             f"> {PROFIT_GROWTH_THRESHOLD}%", self._mark(checks.get("np_growth"))],
        ]
        lines.append(format_table(
            ["指标", "值", "蓝筹条件", "达标", "成长条件", "达标"],
            cls_rows, alignments=["l", "r", "l", "c", "l", "c"]))
        lines.append("")
        lines.append(f"**蓝筹得分**: {cls['blue_score']}/3 | **成长得分**: {cls['growth_score']}/2")
        lines.append(f"**分类结果**: {cls['type']}")
        if cls.get("insufficient"):
            lines.append(f"> ⚠️ 样本年数不足 {MIN_SAMPLE_YEARS} 年（该维度按 0 分）：{'、'.join(cls['insufficient'])}")
        for note in cls.get("notes", []):
            lines.append(f"> {note}")
        if cls["type"] == "混合型":
            lines.append("> DDM 为补充方法：仅当 3 年支付率均值 > 20% 时追加；缺失/偏低时不保留 DDM 权重。")
        lines.append("")

        method_rows = []
        for m in cls["methods"]:
            method_rows.append([m, self._fmt_weight(cls["weights"].get(m, 0))])
        lines.append(format_table(["方法", "权重"], method_rows, alignments=["l", "r"]))
        lines.append("")
        lines.append("---")
        lines.append("")

        # --- Section 2: WACC ---
        lines.append(format_header(2, "二、WACC 计算"))
        lines.append("")
        lines.append("> WACC为市值分档Beta、默认风险溢价/融资成本的参考假设，不是已验证市场回报要求。")
        w = wacc_data
        beta_text = f"{w['beta']:.1f}" if w.get("beta") is not None else "—"
        wacc_rows = [
            ["Rf (无风险利率)", self._fmt_pct(w["rf"]), "§14"],
            ["Beta", beta_text, "市值分档"],
            ["ERP (股权风险溢价)", self._fmt_pct(w["erp"]), f"{self.market}市场默认"],
            ["**Ke (权益成本)**", f"**{self._fmt_pct(w['ke'])}**", "CAPM"],
            ["Kd (税前债务成本)", self._fmt_pct(w["kd_pre"]), "§3/§4"],
            ["有效税率", self._fmt_pct(w["tax_rate"]), "§3 五年均值"],
            ["E/(D+E)", self._fmt_pct(w["e_weight"]), "市值权重"],
            ["D/(D+E)", self._fmt_pct(w["d_weight"]), "债务权重"],
            ["**WACC**", f"**{self._fmt_pct(w['wacc'])}**", "加权平均"],
        ]
        lines.append(format_table(["参数", "值", "来源"], wacc_rows, alignments=["l", "r", "l"]))
        if w.get("wacc") is None:
            lines.append("")
            lines.append(f"> ⚠️ WACC 不可用：{self.unavailable.get('WACC', '输入缺失')}。"
                         "依赖 WACC 的 DCF 不输出参考价（缺失≠0，不按 0 债务硬算）。")
        for note in w.get("notes", []):
            lines.append(f"- {note}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # --- Section 3: Method Details ---
        lines.append(format_header(2, "三、估值方法详情"))
        lines.append("")
        failed = {name: reason for name, reason in self.unavailable.items()
                  if str(reason).startswith(FAILED_PREFIX)}
        if failed:
            lines.append("> ⚠️ 以下方法计算失败，权重已重分配给其余方法：")
            lines.extend(f"- ❌ {name}: {reason}" for name, reason in failed.items())
            lines.append("")
        lines.extend(f"- ⚠️ {name} 不适用：{reason}" for name, reason in self.unavailable.items()
                     if not str(reason).startswith(FAILED_PREFIX))

        for i, result in enumerate(method_results):
            if result is None:
                continue
            method_name = result["method"]
            lines.append(format_header(3, f"方法 {i+1}: {method_name}"))
            lines.append("")

            if method_name == "DCF":
                lines.append(f"- FCF基线 (3yr均值): {format_number(result['fcf_base'])}")
                lines.append(f"- 历史增速口径: {result.get('g_basis', '—')}")
                lines.append(f"- 保守增长率: {self._fmt_pct(result['g_conservative'])}")
                lines.append(f"- 永续增长率: {self._fmt_pct(result['g_terminal'])}")
                lines.append(f"- 终值占比: {result['tv_pct']:.1f}%")
                if result.get("fcf_excluded"):
                    lines.append(f"- FCF异常年份剔除: {len(result['fcf_excluded'])} 年"
                                 f"（负值且偏离3年均值>50%），值 {', '.join(format_number(v) for v in result['fcf_excluded'])}")
                if result.get("growth_capped"):
                    lines.append(f"- 历史增速已裁剪至 {HISTORY_GROWTH_CAP:.0f}%（文档未设上限，取保守上限）")
                if result.get("negative_years"):
                    lines.append(f"> ⚠️ 历史FCFF有 {result['negative_years']} 年 ≤0，基线已按文档口径剔除异常年份")
                if result.get("tv_warning"):
                    lines.append(f"> ⚠️ 终值占比 {result['tv_pct']:.1f}% > {TV_SHARE_WARN_PCT:.0f}%："
                                 "估值高度依赖永续假设，须在定性报告中另行压力测试")
                if result.get("terminal_clamped"):
                    lines.append("- 永续增长率原本 ≥ WACC，已按文档下调至 WACC-2%")
                lines.append("")
                lines.append(f"**内在价值: {self._fmt(result['intrinsic'])} {self._price_unit()}/股**")
                lines.append("")
                lines.append("#### 敏感性分析 (WACC × g_terminal)")
                lines.append("")
                lines.append(self._render_sensitivity(
                    result["sensitivity"], result["wacc_range"], result["g_range"],
                    "WACC", "g_terminal"))

            elif method_name == "DCF_Scenarios":
                if self.notes.get("DCF_downgrade"):
                    lines.append(f"> ⚠️ {self.notes['DCF_downgrade']}")
                    lines.append("")
                lines.append(f"- 历史营收CAGR: {self._fmt_pct(result['rev_cagr_pct'])}")
                lines.append(f"- 当前净利率: {self._fmt_pct(result['net_margin'])}")
                lines.append("")
                sc = result["scenarios"]
                sc_rows = [
                    ["乐观 (25%)", self._fmt_pct(sc["optimistic"]["growth"][0]),
                     self._fmt(sc["optimistic"]["value"])],
                    ["基准 (50%)", self._fmt_pct(sc["base"]["growth"][0]),
                     self._fmt(sc["base"]["value"])],
                    ["悲观 (25%)", self._fmt_pct(sc["pessimistic"]["growth"][0]),
                     self._fmt(sc["pessimistic"]["value"])],
                ]
                lines.append(format_table(["情景", "营收增速", "每股价值"], sc_rows, alignments=["l", "r", "r"]))
                lines.append("")
                lines.append(f"**概率加权内在价值: {self._fmt(result['intrinsic'])} {self._price_unit()}/股**")

            elif method_name == "DDM":
                lines.append(f"- 模型: {result['model_type']}")
                lines.append(f"- 最新DPS: {result['dps_latest']:.2f}")
                lines.append(f"- DPS CAGR: {self._fmt_pct(result['dps_cagr_pct'])}")
                lines.append(f"- 前5年增速 g1: {self._fmt_pct(result.get('g1'))}（{result.get('g1_basis', '—')}）")
                lines.append(f"- 终值增速 g2: {self._fmt_pct(result.get('g2'))}（{result.get('g2_basis', '—')}）")
                lines.append(f"- 完整分红年度: {result['dividend_year']}；终值增长: {self._fmt_pct(result['g_terminal'])}")
                lines.append(f"- 假设边界: {result['assumptions']}")
                lines.append("")
                lines.append(f"**内在价值: {self._fmt(result['intrinsic'])} {self._price_unit()}/股**")
                lines.append("")
                lines.append("#### 敏感性分析 (Ke × g)")
                lines.append("")
                lines.append(self._render_sensitivity(
                    result["sensitivity"], result["ke_range"], result["g_range"],
                    "Ke", "g"))

            elif method_name == "PE_Band":
                ps = result["pe_stats"]
                lines.append(f"- 历史PE范围: {ps['min']:.1f} - {ps['max']:.1f}")
                lines.append(f"- PE中位数: {ps['median']:.1f}")
                lines.append(f"- 正常化EPS: {result['eps_norm']:.2f}")
                lines.append(f"- PE样本: {result['pe_count']} 个年度（{', '.join(result.get('pe_years', []))}）")
                lines.append(f"- 去极值口径: {result.get('pe_note', '—')}")
                if result.get("high_pe_years"):
                    lines.append(f"- 含 PE≥100 的年份: {', '.join(result['high_pe_years'])}"
                                 "（保留不截断；高PE会推高中位数，勿单独作为目标价依据）")
                if result.get("current_pe"):
                    lines.append(f"- 当前PE: {result['current_pe']:.1f} (分位: {result['current_pct']:.0f}%)")
                lines.append("")
                pe_rows = [
                    ["PE_25 (低估)", f"{ps['p25']:.1f}", self._fmt(result['low'])],
                    ["PE_median (合理)", f"{ps['median']:.1f}", self._fmt(result['intrinsic'])],
                    ["PE_75 (高估)", f"{ps['p75']:.1f}", self._fmt(result['high'])],
                ]
                lines.append(format_table(["分位", "PE", "对应股价"], pe_rows, alignments=["l", "r", "r"]))
                lines.append("")
                lines.append(f"**内在价值: {self._fmt(result['intrinsic'])} {self._price_unit()}/股**")

            elif method_name == "PEG":
                lines.append(f"- PE (TTM): {result['pe']:.1f}")
                lines.append(f"- 盈利增速G: {self._fmt_pct(result['g_pct'])}")
                lines.append(f"- PEG: {result['peg_value']:.2f}")
                lines.append(f"- PEG=1合理PE: {result['fair_pe']:.1f}")
                lines.append("")
                peg_judge = "显著低估" if result["peg_value"] < 0.5 else \
                    "低估" if result["peg_value"] < 1.0 else \
                    "合理" if result["peg_value"] < 1.5 else \
                    "偏高" if result["peg_value"] < 2.0 else "高估"
                lines.append(f"PEG判断: **{peg_judge}**")
                lines.append("")
                sens_rows = []
                for s in result["sensitivity"]:
                    sens_rows.append([
                        self._fmt_pct(s["g"]),
                        f"{s['peg']:.2f}" if s["peg"] else "—",
                        self._fmt(s["fair_price"]) if s["fair_price"] else "—",
                    ])
                lines.append(format_table(["G假设", "PEG", "合理股价"], sens_rows, alignments=["r", "r", "r"]))
                lines.append("")
                lines.append(f"**内在价值 (PEG=1): {self._fmt(result['intrinsic'])} {self._price_unit()}/股**")

            elif method_name == "PS":
                ps = result["ps_stats"]
                lines.append(f"- 当前PS: {result['ps_current']:.2f}" if result.get("ps_current") else "- 当前PS: —")
                lines.append(f"- 历史PS范围: {ps['min']:.2f} - {ps['max']:.2f}")
                lines.append(f"- PS中位数: {ps['median']:.2f}")
                lines.append("")
                ps_rows = [
                    ["PS_25 (低估)", f"{ps['p25']:.2f}", self._fmt(result['low'])],
                    ["PS_median (合理)", f"{ps['median']:.2f}", self._fmt(result['intrinsic'])],
                    ["PS_75 (高估)", f"{ps['p75']:.2f}", self._fmt(result['high'])],
                ]
                lines.append(format_table(["分位", "PS", "对应股价"], ps_rows, alignments=["l", "r", "r"]))
                lines.append("")
                lines.append(f"**内在价值: {self._fmt(result['intrinsic'])} {self._price_unit()}/股**")

            lines.append("")
            lines.append("---")
            lines.append("")

        # --- Section 4: Cross-validation ---
        lines.append(format_header(2, "四、交叉验证"))
        lines.append("")

        xv = xval
        failed_xv = xv.get("failed") or {}
        dropped_xv = xv.get("dropped") or {}
        if failed_xv:
            lines.append("> ⚠️ 下列方法计算失败，未纳入加权，其权重已重分配给其余方法：")
            lines.extend(f"- ❌ {name}: {reason}" for name, reason in failed_xv.items())
            lines.append("")
        other_dropped = {m: r for m, r in dropped_xv.items() if m not in failed_xv}
        if other_dropped:
            lines.append("> 未纳入加权（数据/口径不适用），其权重已重分配：")
            lines.extend(f"- {name}: {reason}" for name, reason in other_dropped.items())
            lines.append("")
        if xv.get("methods"):
            xv_rows = []
            for m, v in xv["methods"]:
                w = xv["active_weights"].get(m, 0)
                xv_rows.append([m, self._fmt(v), f"{w:.0f}%", self._fmt(v * w / 100)])
            xv_rows.append(["**加权平均**", "—", "100%", f"**{self._fmt(xv['weighted_avg'])}**"])
            lines.append(format_table(
                ["方法", f"内在价值 ({self._price_unit()}/股)", "权重", "加权贡献"],
                xv_rows, alignments=["l", "r", "r", "r"]))
            lines.append("")
            lines.append(f"- 变异系数 (CV): {xv['cv']:.1f}%")
            lines.append(f"- 一致性: **{xv['consistency']}**")
        else:
            lines.append("> ⚠️ 没有任何方法产出可用参考价：不合成内在价值、不生成买点。")
        lines.append("")

        # Range
        rng = xv.get("range", {})
        current = bi.get("close", 0)
        if rng.get("conservative"):
            rng_rows = [
                ["保守", self._fmt(rng["conservative"]),
                 f"{(rng['conservative'] / current - 1) * 100:+.1f}%" if current else "—"],
                ["中性", self._fmt(rng["central"]),
                 f"{(rng['central'] / current - 1) * 100:+.1f}%" if current else "—"],
                ["乐观", self._fmt(rng["optimistic"]),
                 f"{(rng['optimistic'] / current - 1) * 100:+.1f}%" if current else "—"],
            ]
            lines.append(format_table(
                ["情景", f"内在价值 ({self._price_unit()}/股)", "vs 当前股价"],
                rng_rows, alignments=["l", "r", "r"]))
            lines.append("")

            safety = (rng["central"] / current - 1) * 100 if current else 0
            if safety > 30:
                judgment = "显著低估"
            elif safety > 10:
                judgment = "低估"
            elif safety > -10:
                judgment = "合理"
            elif safety > -30:
                judgment = "偏高"
            else:
                judgment = "高估"
            lines.append(f"**初步判断（未经定性调整）: {judgment}**")
            lines.append(f"**安全边际: {safety:+.1f}%**")
        lines.append("")
        lines.append("---")
        lines.append("")

        # --- Section 5: Reverse Valuation ---
        lines.append(format_header(2, "五、反向估值：当前价格隐含了什么？"))
        lines.append("")
        lines.append("> 用当前股价反推市场隐含的增长假设，揭示市场定价中包含了多少增长预期。")
        lines.append("")

        rv = reverse
        if rv:
            rv_rows = []

            # Row 1: Implied earnings growth (R1)
            ig_e = rv.get("implied_g_earnings")
            actual_np = rv.get("actual_np_cagr")
            gap = rv.get("growth_discount")
            rv_rows.append([
                "盈利增长 (E/P法)",
                self._fmt_pct(ig_e) if ig_e is not None else "—",
                self._fmt_pct(actual_np) if actual_np is not None else "—",
                f"{gap:+.1f} pct" if gap is not None else "—",
            ])

            # Row 2: Implied dividend growth (R3)
            ig_d = rv.get("implied_g_dividend")
            rv_rows.append([
                "分红增长 (DDM反解)",
                self._fmt_pct(ig_d) if ig_d is not None else "—",
                 "最近完整分红年度DPS（非预测）",
                "—",
            ])

            # Row 3: Implied FCF growth (R2) — formula printed only when implemented
            ig_f = rv.get("implied_g_fcf")
            fcf_basis_label = {"verified_fcff": "已验证FCFF",
                               "consolidated_proxy": "合并现金代理（未验证）"}.get(rv.get("fcf_basis"))
            yield_text = f"FCF收益率 {self._fmt_pct(rv.get('fcf_yield'))}"
            if fcf_basis_label:
                yield_text += f"（{fcf_basis_label}）"
            remark = (f"WACC − FCF收益率 = {self._fmt_pct(ig_f)}（文档口径：以市值近似企业价值）"
                      if ig_f is not None else (rv.get("implied_fcf_note") or "—"))
            rv_rows.append([
                "FCF增长 (永续DCF反解)",
                self._fmt_pct(ig_f) if ig_f is not None else "—",
                yield_text,
                remark,
            ])

            lines.append(format_table(
                ["维度", "市场隐含增长率", "实际/参考值", "备注"],
                rv_rows, alignments=["l", "r", "l", "l"]))
            lines.append("")

            # Implied required return (doc: r_implied = FCF_yield + g_actual)
            iw = rv.get("implied_wacc")
            if iw is not None:
                basis = "已验证FCFF" if rv.get("fcf_basis") == "verified_fcff" else "合并现金代理"
                lines.append(f"**隐含要求回报率**: FCF收益率（{basis}）+ 实际增速 "
                             f"{self._fmt_pct(rv.get('actual_growth'))} = {iw:.2f}%"
                             f"（vs 模型 WACC {self._fmt_pct(rv.get('wacc'))}）；"
                             "差额是模型未捕获风险溢价的参考，不是已验证结论。")
                lines.append("")

            # Growth discount summary (thresholds: >8 / 3-8 / 0-3 / <0 pct)
            if gap is not None and rv.get("growth_discount_label"):
                lines.append(f"**结论**: {rv['growth_discount_label']}。"
                             f"市场隐含盈利增长 {self._fmt_pct(ig_e)} vs 实际 {self._fmt_pct(actual_np)}，"
                             f"增长折价 **{gap:+.1f} pct**（分档：>8 / 3-8 / 0-3 / <0 pct）。")
                lines.append("")

        lines.append("---")
        lines.append("")

        # --- Section 6: Assumptions list ---
        lines.append(format_header(2, "六、关键假设清单（待定性调整）"))
        lines.append("")
        lines.append("> LLM 分析师：请根据定性报告（qualitative_report.md）调整以下假设，")
        lines.append("> 并从上方敏感性矩阵中选择最合理的情景坐标。")
        lines.append("")

        dcf_result = next((r for r in method_results if r and "g_conservative" in r), None)
        dcf_growth = self._fmt_pct(dcf_result["g_conservative"]) if dcf_result else "—"
        terminal_default = (dcf_result["g_terminal"] if dcf_result
                            else min(self.params["g_terminal"], self.params["gdp_long_run"]))
        assumption_rows = [
            ["1", "FCF增长率", dcf_growth,
             "↓/→/↑", "D1 收入质量 + 核心利润分解"],
            ["2", "永续增长率", self._fmt_pct(terminal_default),
             "↓/→", "D2 护城河评级 + 可持续性"],
            ["3", "Beta", (f"{wacc_data['beta']:.1f}"
                            if wacc_data.get("beta") is not None else "—"),
             "↑/→", "D3 周期性"],
            ["4", "ERP溢价", self._fmt_pct(wacc_data["erp"]),
             "↑/→", "D4 管理层 + 治理风险"],
            ["5", "DPS增长率", "见DDM敏感性表",
             "↓/→/↑", "D5 分红信号"],
            ["6", "控股折价", "0%",
             "0-30%", "D6 控股结构"],
        ]
        lines.append(format_table(
            ["#", "假设", "Python默认值", "调整方向", "定性依据"],
            assumption_rows, alignments=["c", "l", "r", "c", "l"]))
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 11. Orchestrator
    # ------------------------------------------------------------------

    def run(self) -> str:
        """Execute full pipeline and return markdown output."""
        self.unavailable.clear()
        self.failed.clear()
        self.notes.clear()

        # 1. Classify
        cls = self.classify()
        print(f"  分类: {cls['type']} (蓝筹{cls['blue_score']}/成长{cls['growth_score']})", file=sys.stderr)

        # 2. WACC
        wacc_data = self.compute_wacc()
        if wacc_data.get("wacc") is None:
            print(f"  WACC: 不可用 ({self.unavailable.get('WACC', '')})", file=sys.stderr)
        else:
            print(f"  WACC: {wacc_data['wacc']:.2f}%", file=sys.stderr)

        # 3. Run selected methods. A raised exception is a method failure and is
        #    recorded (not discarded) so weights can be re-audited downstream.
        method_map = {
            "DCF": lambda: self.dcf_stable(wacc_data),
            "DCF_Scenarios": lambda: self.dcf_scenarios(wacc_data),
            "DDM": lambda: self.ddm(wacc_data.get("ke")),
            "PE_Band": lambda: self.pe_band(),
            "PEG": lambda: self.peg(),
            "PS": lambda: self.ps(),
        }

        results = []
        for method_name in cls["methods"]:
            fn = method_map.get(method_name)
            if not fn:
                results.append(None)
                continue
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 — surface failure in the report
                reason = f"{FAILED_PREFIX} {type(e).__name__}: {e}"
                self.failed[method_name] = reason
                self.unavailable[method_name] = reason
                print(f"  {method_name}: 计算失败 ({type(e).__name__}: {e})", file=sys.stderr)
                results.append(None)
                continue
            if result:
                print(f"  {method_name}: {result['intrinsic']:.2f}/股", file=sys.stderr)
            else:
                if method_name not in self.unavailable:
                    self.unavailable[method_name] = "数据/口径不满足，未产出参考价"
                print(f"  {method_name}: 跳过 ({self.unavailable[method_name]})", file=sys.stderr)
            results.append(result)

        # classification_rules.md 2.3: FCF fully negative → downgrade stable DCF
        # to the scenario version instead of silently dropping the method.
        weights = dict(cls["weights"])
        if "DCF" in cls["methods"]:
            dcf_index = cls["methods"].index("DCF")
            if (results[dcf_index] is None
                    and self.unavailable.get("DCF", "").startswith("历史FCF基线非正")):
                print("  DCF: 历史FCF为负，降级为情景分析版", file=sys.stderr)
                try:
                    scenario = self.dcf_scenarios(wacc_data)
                except Exception as e:  # noqa: BLE001
                    reason = f"{FAILED_PREFIX} {type(e).__name__}: {e}"
                    scenario = None
                    self.failed["DCF_Scenarios"] = reason
                    self.unavailable["DCF_Scenarios"] = reason
                if scenario is not None:
                    results[dcf_index] = scenario
                    weights["DCF_Scenarios"] = weights.pop("DCF", 0)
                    self.notes["DCF_downgrade"] = (
                        "稳定版DCF因历史FCF全负，按分类规则降级为情景分析版；"
                        "原DCF权重已转给情景版")
                elif "DCF_Scenarios" not in self.unavailable:
                    self.unavailable["DCF_Scenarios"] = "情景版同样不满足已核对FCFF/收入口径，不能输出目标价"

        # 4. Cross-validate (with documented weights after any downgrade)
        xval = self.cross_validate([r for r in results if r is not None], weights)

        # 5. Reverse valuation
        reverse = self.reverse_valuation(wacc_data, cls)
        if reverse.get("implied_g_earnings") is not None:
            actual = reverse.get("actual_np_cagr")
            print("  反向估值: 市场隐含盈利增长 "
                  f"{reverse['implied_g_earnings']:.1f}%"
                  + (f" (实际 {actual:.1f}%)" if actual is not None else ""), file=sys.stderr)

        # 6. Generate output
        return self.generate_output(cls, wacc_data, results, xval, reverse)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Valuation computation engine")
    parser.add_argument("--code", required=True, help="Stock code (e.g., 600887)")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--snapshot", help="Use the collector's JSON snapshot (no independent recollection)")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the TTL disk cache for financial endpoints (always fetch live)",
    )
    args = parser.parse_args()

    ts_code = validate_stock_code(args.code)

    # Import AKShareClient here to keep module importable without network for testing
    from akshare_collector import AKShareClient

    client = AKShareClient(no_cache=args.no_cache)
    snapshot = args.snapshot or os.path.join(args.output_dir, "data_snapshot.json")
    if os.path.exists(snapshot):
        print(f"[valuation_engine] 读取同次采集快照: {snapshot}", file=sys.stderr)
        from data_snapshot import load_snapshot
        load_snapshot(client, snapshot, ts_code)
    elif args.snapshot:
        parser.error(f"快照不存在: {snapshot}")
    else:
        # Standalone invocation still works, but also saves its exact inputs.
        print(f"[valuation_engine] 正在采集 {ts_code} 数据 (AKShare)...", file=sys.stderr)
        client.assemble_data_pack(ts_code)
        from data_snapshot import write_snapshot
        write_snapshot(client, args.output_dir, ts_code)

    print(f"[valuation_engine] 正在计算估值...", file=sys.stderr)
    engine = ValuationEngine(ts_code, args.output_dir, client)
    output_md = engine.run()

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "valuation_computed.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output_md)

    print(f"[valuation_engine] 完成: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
