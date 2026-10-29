"""Tests for assembly (§-parsing, staleness, warnings) and derived metrics smoke."""

import pandas as pd
import pytest

from akshare_collector import AKShareClient
from akshare_modules.assembly import AssemblyMixin, WarningsCollector


class TestParseSections:
    SAMPLE = """# 数据包 — 600887.SH

*生成时间: 2026-10-29 10:00:00*

---

## 1. 基本信息

内容A

## 2. 市场行情

内容B

## 3P. 母公司利润表

内容C

## 17. 衍生指标（Python 预计算）

内容D

---
*共 12/12 个数据板块成功获取*
"""

    def test_split_sections(self):
        _header, sections, footer = AssemblyMixin._parse_sections(self.SAMPLE)
        keys = [k for k, _ in sections]
        assert keys == ["1", "2", "3P", "17"]
        assert footer.startswith("\n---\n*共")

    def test_header_preserved(self):
        header, _sections, _footer = AssemblyMixin._parse_sections(self.SAMPLE)
        assert "数据包" in header
        assert "生成时间" in header

    def test_no_sections(self):
        header, sections, footer = AssemblyMixin._parse_sections("no sections")
        assert sections == []
        assert header == "no sections"


class TestCheckStaleness:
    def test_recent(self):
        from pandas import Timestamp
        content = f"*生成时间: {Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}*"
        assert AssemblyMixin._check_staleness(content) == 0

    def test_old(self):
        assert AssemblyMixin._check_staleness("*生成时间: 2020-01-01 00:00:00*") > 1000

    def test_missing(self):
        assert AssemblyMixin._check_staleness("no timestamp") == 999


class TestWarningsCollector:
    def test_missing_data(self):
        wc = WarningsCollector()
        wc.check_missing_data("利润表", pd.DataFrame())
        assert wc.warnings and wc.warnings[0]["type"] == "DATA_MISSING"

    def test_goodwill(self):
        wc = WarningsCollector()
        wc.check_goodwill_ratio(30e8, 100e8)
        assert wc.warnings and wc.warnings[0]["type"] == "GOODWILL_RISK"

    def test_goodwill_ok(self):
        wc = WarningsCollector()
        wc.check_goodwill_ratio(1e8, 100e8)
        assert wc.warnings == []

    def test_debt_ratio(self):
        wc = WarningsCollector()
        wc.check_debt_ratio(75e8, 100e8)
        assert wc.warnings and wc.warnings[0]["type"] == "LEVERAGE_RISK"

    def test_yoy_anomaly(self):
        wc = WarningsCollector()
        wc.check_yoy_change("利润表", "revenue", [100e8, 20e8], dates=["2025", "2024"])
        assert wc.warnings and wc.warnings[0]["type"] == "YOY_ANOMALY"

    def test_yoy_normal(self):
        wc = WarningsCollector()
        wc.check_yoy_change("利润表", "revenue", [110e8, 100e8], dates=["2025", "2024"])
        assert wc.warnings == []


class TestDerivedMetricsSmoke:
    """End-to-end §17 computation from a synthetic store (no network)."""

    @pytest.fixture
    def client(self):
        c = AKShareClient()
        c._no_cache = True
        c._store = {
            "basic_info": pd.DataFrame([{
                "ts_code": "600887.SH", "name": "伊利股份",
                "trade_date": "20261029", "close": 29.45,
                "pe_ttm": 18.4, "pb": 3.47,
                "total_mv": 18628200.0, "circ_mv": 18628200.0,  # 万元
                "total_share": 632536.5,  # 万股
            }]),
            "income": pd.DataFrame({
                "end_date": ["20251231", "20241231", "20231231", "20221231", "20211231"],
                "revenue": [115931e6, 115779e6, 126179e6, 123171e6, 110595e6],
                "oper_cost": [75653e6, 76298e6, 84788e6, 83118e6, 76416e6],
                "sell_exp": [21544e6, 21982e6, 22571e6, 22908e6, 19314e6],
                "admin_exp": [4695e6, 4465e6, 5154e6, 5342e6, 4227e6],
                "rd_exp": [915e6, 869e6, 850e6, 821e6, 601e6],
                "finance_exp": [-690e6, -591e6, -152e6, -254e6, -29e6],
                "operate_profit": [13527e6, 10181e6, 11873e6, 10859e6, 10230e6],
                "total_profit": [12993e6, 10126e6, 11721e6, 10630e6, 10112e6],
                "income_tax": [1479e6, 1663e6, 1436e6, 1311e6, 1380e6],
                "n_income": [11513e6, 8463e6, 10284e6, 9318e6, 8732e6],
                "n_income_attr_p": [11565e6, 8452e6, 10428e6, 9431e6, 8704e6],
                "minority_gain": [-51e6, 10e6, -144e6, -112e6, 27e6],
                "basic_eps": [1.83, 1.33, 1.64, 1.48, 1.43],
            }),
            "balance_sheet": pd.DataFrame({
                "end_date": ["20251231", "20241231", "20231231", "20221231", "20211231"],
                "money_cap": [19494e6, 25404e6, 43371e6, 33853e6, 31742e6],
                "trad_asset": [3.3e6, 6.1e6, 11.4e6, 30.1e6, 37.2e6],
                "accounts_receiv": [2987e6, 2878e6, 3084e6, 3088e6, 1958e6],
                "inventories": [10823e6, 10744e6, 12511e6, 14836e6, 8917e6],
                "goodwill": [2181e6, 2336e6, 5160e6, 4953e6, 306e6],
                "total_assets": [152098e6, 153718e6, 151620e6, 130965e6, 101962e6],
                "st_borr": [45630e6, 36354e6, 39754e6, 26799e6, 12596e6],
                "lt_borr": [472e6, 4686e6, 11705e6, 9298e6, 5380e6],
                "bond_payable": [None, None, 3541e6, 3482e6, 3187e6],
                "non_cur_liab_due_1y": [None, None, None, None, None],
                "contract_liab": [10564e6, 12072e6, 8695e6, 8912e6, 7891e6],
                "acct_payable": [11479e6, 12312e6, 13798e6, 15925e6, 13659e6],
                "total_liab": [93554e6, 96710e6, 94299e6, 76822e6, 53171e6],
                "total_hldr_eqy_exc_min_int": [54655e6, 53179e6, 53539e6, 50267e6, 47708e6],
                "minority_int": [3888e6, 3827e6, 3781e6, 3875e6, 1082e6],
            }),
            "cashflow": pd.DataFrame({
                "end_date": ["20251231", "20241231", "20231231", "20221231", "20211231"],
                "c_recp_sale_goods": [125775e6, 130651e6, 137239e6, 133594e6, 121968e6],
                "n_cashflow_act": [14343e6, 21739e6, 18290e6, 13420e6, 15527e6],
                "c_pay_acq_const_fiolta": [3036e6, 3978e6, 6955e6, 6646e6, 6682e6],
                "c_pay_to_staff": [14188e6, 13861e6, 14252e6, 13974e6, 11044e6],
                "c_paid_for_taxes": [6854e6, 6729e6, 6821e6, 6008e6, 5075e6],
                "c_pay_dist_dpcp_int_exp": [12024e6, 9092e6, 8415e6, 7500e6, 5692e6],
                "n_cash_flows_fnc_act": [-13472e6, -7277e6, 7258e6, 8781e6, 11945e6],
                "n_cashflow_inv_act": [-7796e6, -32709e6, -16044e6, -19513e6, -7796e6],
            }),
            "dividends": pd.DataFrame({
                "end_date": ["20251231", "20250930", "20241231", "20231231", "20221231"],
                "cash_div_tax": [0.90, 0.48, 1.22, 1.20, 1.04],
                "stk_div": [0, 0, 0, 0, 0],
                "base_share": [632536.5, 632536.5, 636590.0, 636610.0, 639935.0],
                "div_proc": ["实施"] * 5,
            }),
            "weekly_prices": pd.DataFrame({
                "trade_date": [f"{y}{m:02d}{d:02d}"
                               for y in range(2016, 2026)
                               for m, d in [(6, 1), (12, 1)]],
                "close": [10 + i * 0.4 for i in range(20)],
                "high": [11 + i * 0.4 for i in range(20)],
                "low": [9 + i * 0.4 for i in range(20)],
                "vol": [1e6] * 20,
            }),
            "risk_free_rate": pd.DataFrame({
                "trade_date": ["20261028", "20261027"],
                "yield": [1.6233, 1.6245],
            }),
        }
        return c

    def test_full_17_renders(self, client):
        md = client.compute_derived_metrics("600887.SH")
        assert "## 17." in md or "17." in md
        for sub in ("17.1", "17.2", "17.3", "17.4", "17.5", "17.10", "17.11"):
            assert sub in md, f"missing {sub}"
        # 17.6/17.8 need weekly + risk_free — both present
        assert "17.6" in md
        assert "17.8" in md
        # G grid (17.12) degrades: no D&A in store
        assert "17.12" not in md

    def test_17_1_payout(self, client):
        md = client.compute_derived_metrics("600887.SH")
        assert "股息支付率" in md
        # FY2025 payout ≈ 75.5%
        assert "75" in md

    def test_17_2_threshold_ii(self, client):
        md = client.compute_derived_metrics("600887.SH")
        assert "II（门槛值）" in md
        # A股: max(3.5%, Rf+2%) with Rf=1.62% → 3.62%
        assert "3.62" in md
