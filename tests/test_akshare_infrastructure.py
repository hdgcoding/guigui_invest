"""Tests for AKShareClient infrastructure helpers (no network)."""

import pandas as pd
import pytest

from akshare_collector import AKShareClient


@pytest.fixture
def client():
    c = AKShareClient()
    c._no_cache = True
    return c


def _income_df():
    return pd.DataFrame({
        "end_date": ["20251231", "20241231", "20231231", "20221231", "20211231"],
        "revenue": [115931e6, 115779e6, 126179e6, 123171e6, 110595e6],
        "n_income_attr_p": [11565e6, 8452e6, 10428e6, 9431e6, 8704e6],
        "basic_eps": [1.83, 1.33, 1.64, 1.48, 1.43],
    })


def _dividend_df():
    # FY2025: 中期 + 末期 → DPS 合计 1.38
    return pd.DataFrame({
        "end_date": ["20251231", "20250930", "20241231", "20231231"],
        "cash_div_tax": [0.90, 0.48, 1.22, 1.20],
        "stk_div": [0, 0, 0, 0],
        "base_share": [632536.5, 632536.5, 636590.0, 636610.0],
        "div_proc": ["实施", "实施", "实施", "实施"],
        "ex_date": ["2026-06-05", "2025-12-17", "2025-06-06", "2024-06-06"],
    })


class TestSafeFloat:
    def test_none(self):
        assert AKShareClient._safe_float(None) is None

    def test_nan(self):
        assert AKShareClient._safe_float(float("nan")) is None

    def test_string_number(self):
        assert AKShareClient._safe_float("1.5") == 1.5

    def test_garbage_string(self):
        assert AKShareClient._safe_float("abc") is None


class TestMarketDetection:
    def test_always_a_share(self):
        assert AKShareClient._is_hk("600887.SH") is False
        assert AKShareClient._is_us("600887.SH") is False
        assert AKShareClient._detect_currency("600887.SH") == "CNY"

    def test_unit_labels(self):
        c = AKShareClient()
        assert c._unit_label() == "百万元"
        assert c._price_unit() == "元"


class TestPrepareDisplayPeriods:
    def test_interim_plus_annual(self, client):
        df = pd.DataFrame({
            "end_date": ["20250930", "20241231", "20231231", "20221231",
                         "20211231", "20201231", "20191231"],
            "v": [1, 2, 3, 4, 5, 6, 7],
        })
        disp, labels = client._prepare_display_periods(df, max_annual=5)
        # interim (2025Q3) + 5 annuals
        assert labels[0] == "2025Q3"
        assert "2024" in labels and "2020" in labels
        assert "2019" not in labels
        assert len(disp) == len(labels)

    def test_h1_label(self, client):
        df = pd.DataFrame({"end_date": ["20250630", "20241231"], "v": [1, 2]})
        _disp, labels = client._prepare_display_periods(df)
        assert labels == ["2025H1", "2024"]

    def test_empty(self, client):
        disp, labels = client._prepare_display_periods(pd.DataFrame())
        assert disp.empty and labels == []


class TestAnnualHelpers:
    def test_get_annual_df_filters_interim(self, client):
        df = _income_df()
        # Insert an interim period between annuals
        df = pd.concat([df, pd.DataFrame({
            "end_date": ["20250930"], "revenue": [1e9],
            "n_income_attr_p": [5e8], "basic_eps": [0.85],
        })], ignore_index=True)
        client._store["income"] = df
        annual = client._get_annual_df("income")
        assert len(annual) == 5
        assert all(d.endswith("1231") for d in annual["end_date"])

    def test_get_annual_series_desc(self, client):
        client._store["income"] = _income_df()
        series = client._get_annual_series("income", "n_income_attr_p")
        assert series[0][0] == "2025"
        assert series[-1][0] == "2021"
        assert series[0][1] == pytest.approx(11565e6)


class TestPayoutByYear:
    def test_interim_plus_final_same_fy(self, client):
        """中期(0930) + 末期(1231) 同属 FY2025，payout 应合并计算。"""
        client._store["income"] = _income_df()
        client._store["dividends"] = _dividend_df()
        client._store["as_of"] = "2026-10-30"

        payout = client._get_payout_by_year()
        # FY2025: (0.9 + 0.48) 元/股 × 632536.5 万股 vs 归母净利 11565 百万
        expected_2025 = (0.9 + 0.48) * 632536.5 * 1e4 / (11565e6) * 100
        assert payout["2025"] == pytest.approx(expected_2025, rel=0.01)
        assert 70 < payout["2025"] < 80  # sanity: 伊利 ~75%

    def test_unimplemented_excluded(self, client):
        client._store["income"] = _income_df()
        divs = _dividend_df()
        divs.loc[divs.index[-1], "div_proc"] = "预案"
        client._store["dividends"] = divs
        payout = client._get_payout_by_year()
        assert "2023" not in payout

    def test_missing_income(self, client):
        client._store["dividends"] = _dividend_df()
        assert client._get_payout_by_year() == {}


class TestCachedCall:
    def test_unknown_endpoint_not_cached(self, client, monkeypatch):
        calls = []

        def fetch():
            calls.append(1)
            return pd.DataFrame({"a": [1]})

        df = client._cached_call("unknown_endpoint", fetch)
        assert len(df) == 1
        assert calls == [1]

    def test_per_stock_key_isolation(self, client, monkeypatch, tmp_path):
        from cache_utils import ScreenerCache
        client._cache = ScreenerCache(str(tmp_path))
        client._no_cache = False

        def fetch_600887():
            return pd.DataFrame({"code": ["600887"]})

        def fetch_000858():
            return pd.DataFrame({"code": ["000858"]})

        a = client._cached_call("income", fetch_600887, key="income:600887")
        b = client._cached_call("income", fetch_000858, key="income:000858")
        assert a.iloc[0]["code"] == "600887"
        assert b.iloc[0]["code"] == "000858"
