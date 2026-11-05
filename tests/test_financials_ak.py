"""Tests for financials normalization (mocked AKShare responses, no network)."""

import pandas as pd
import pytest

from akshare_collector import AKShareClient


@pytest.fixture
def client():
    c = AKShareClient()
    c._no_cache = True
    return c


def _sina_income_raw():
    return pd.DataFrame({
        "报告日": ["2025-12-31", "2025-09-30", "2024-12-31", "2023-12-31",
                    "2022-12-31", "2021-12-31", "2020-12-31"],
        "营业总收入": ["115931110000.00", "88000000000.00", "115779550000.00",
                        "126179460000.00", "123171040000.00", "110595200000.00", "96500000000.00"],
        "归属于母公司所有者的净利润": ["11565170000.00", "8000000000.00", "8452860000.00",
                                     "10428540000.00", "9431060000.00", "8704920000.00", "7000000000.00"],
        "基本每股收益": ["1.83", "1.27", "1.33", "1.64", "1.48", "1.43", "1.17"],
        "类型": ["合并报表"] * 7,
    })


class TestSinaStatementNormalization:
    def test_columns_and_dates(self, client, monkeypatch):
        monkeypatch.setattr(
            client, "_cached_call",
            lambda endpoint, fetch, key=None: _sina_income_raw()
        )
        df = client._fetch_sina_statement("600887.SH", "利润表",
                                          {"revenue": "营业总收入",
                                           "n_income_attr_p": "归属于母公司所有者的净利润",
                                           "basic_eps": "基本每股收益"})
        assert list(df["end_date"])[:3] == ["20251231", "20250930", "20241231"]
        assert df.iloc[0]["revenue"] == pytest.approx(115931.11e6)
        assert df.iloc[0]["n_income_attr_p"] == pytest.approx(11565.17e6)
        assert df.iloc[0]["basic_eps"] == pytest.approx(1.83)

    def test_max_five_annuals_in_display(self, client, monkeypatch):
        raw = _sina_income_raw()
        # interim NEWER than latest annual (2026Q3 vs FY2025) → shown
        raw = pd.concat([raw, pd.DataFrame({
            "报告日": ["2026-09-30"],
            "营业总收入": ["80000000000.00"],
            "归属于母公司所有者的净利润": ["6000000000.00"],
            "基本每股收益": ["0.95"],
            "类型": ["合并报表"],
        })], ignore_index=True)
        monkeypatch.setattr(
            client, "_cached_call",
            lambda endpoint, fetch, key=None: raw
        )
        df = client._fetch_sina_statement("600887.SH", "利润表",
                                          {"revenue": "营业总收入"})
        disp, labels = client._prepare_display_periods(df)
        assert labels[0] == "2026Q3"
        assert len([l for l in labels if l.isdigit()]) == 5

    def test_interim_older_than_annual_hidden(self, client, monkeypatch):
        """2025Q3 (0930) 早于 2025 年报 (1231) → 不显示（年报已覆盖）。"""
        monkeypatch.setattr(
            client, "_cached_call",
            lambda endpoint, fetch, key=None: _sina_income_raw()
        )
        df = client._fetch_sina_statement("600887.SH", "利润表",
                                          {"revenue": "营业总收入"})
        _disp, labels = client._prepare_display_periods(df)
        assert "2025Q3" not in labels
        assert labels[0] == "2025"

    def test_parent_type_rows_filtered(self, client, monkeypatch):
        raw = _sina_income_raw()
        raw["类型"] = ["母公司报表"] + ["合并报表"] * 6
        monkeypatch.setattr(
            client, "_cached_call",
            lambda endpoint, fetch, key=None: raw
        )
        df = client._fetch_sina_statement("600887.SH", "利润表",
                                          {"revenue": "营业总收入"})
        assert "20251231" not in set(df["end_date"])  # parent row dropped


class TestWeeklyResample:
    def test_resample_from_daily(self, client, monkeypatch):
        days = pd.date_range("2025-01-01", "2025-03-31", freq="B")
        daily = pd.DataFrame({
            "trade_date": [d.strftime("%Y%m%d") for d in days],
            "open": [10.0] * len(days),
            "high": [11.0] * len(days),
            "low": [9.0] * len(days),
            "close": [10.5] * len(days),
            "vol": [100.0] * len(days),
            "amount": [1000.0] * len(days),
        })
        monkeypatch.setattr(client, "_fetch_daily", lambda num: daily)
        weekly = client._fetch_weekly("600887", years=10)
        # ~13 weeks in a quarter
        assert 12 <= len(weekly) <= 14
        assert set(weekly.columns) == {"trade_date", "open", "high", "low", "close", "vol", "amount"}
        assert weekly.iloc[0]["high"] == 11.0
        assert weekly.iloc[0]["low"] == 9.0


class TestDividendsNormalization:
    def test_fhps_to_internal(self, client, monkeypatch):
        raw = pd.DataFrame({
            "报告期": ["2025-12-31", "2025-09-30", "2024-12-31"],
            "现金分红-现金分红比例": [9.0, 4.8, 12.2],
            "送转股份-送股比例": [0.0, 0.0, 0.0],
            "总股本": [6325365000, 6325365000, 6365900000],
            "方案进度": ["实施分配", "实施分配", "实施分配"],
            "除权除息日": ["2026-06-05", "2025-12-17", "2025-06-06"],
            "现金分红-现金分红比例描述": ["10派9.00元", "10派4.80元", "10派12.20元"],
        })
        monkeypatch.setattr(
            client, "_fetch_with_retry", lambda fetch: raw
        )
        md = client.get_dividends("600887.SH")

        divs = client._store["dividends"]
        # per-10 → per-share conversion
        assert divs.iloc[0]["cash_div_tax"] == pytest.approx(0.90)
        assert divs.iloc[2]["cash_div_tax"] == pytest.approx(1.22)
        # base_share in 万股
        assert divs.iloc[0]["base_share"] == pytest.approx(632536.5)
        assert "| 20251231 | 0.9000 |" in md

    def test_plan_only_excluded_from_store(self, client, monkeypatch):
        raw = pd.DataFrame({
            "报告期": ["2025-12-31"],
            "现金分红-现金分红比例": [9.0],
            "送转股份-送股比例": [0.0],
            "总股本": [6325365000],
            "方案进度": ["董事会预案"],
            "除权除息日": [pd.NaT],
            "现金分红-现金分红比例描述": ["10派9.00元"],
        })
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: raw)
        md = client.get_dividends("600887.SH")
        assert client._store["dividends"].empty
        assert "暂无已实施分红" in md


def _weekly_frame(scale=1.0):
    return pd.DataFrame({
        "trade_date": [f"20{16 + i // 2}{6 if i % 2 == 0 else 12:02d}01" for i in range(20)],
        "open": [10.0 * scale] * 20,
        "high": [(11.0 + i * 0.1) * scale for i in range(20)],
        "low": [(9.0 + i * 0.1) * scale for i in range(20)],
        "close": [(10.0 + i * 0.2) * scale for i in range(20)],
        "vol": [1e6] * 20,
        "amount": [1e7] * 20,
    })


class TestDividendDisplayWindow:
    def test_display_capped_to_five_fiscal_years(self, client, monkeypatch):
        years = [2025, 2024, 2023, 2022, 2021, 2020, 2019]
        raw = pd.DataFrame({
            "报告期": [f"{y}-12-31" for y in years],
            "现金分红-现金分红比例": [9.0] * len(years),
            "送转股份-送股比例": [0.0] * len(years),
            "总股本": [6325365000] * len(years),
            "方案进度": ["实施分配"] * len(years),
            "除权除息日": [f"{y + 1}-06-01" for y in years],
            "现金分红-现金分红比例描述": ["10派9.00元"] * len(years),
        })
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: raw)
        md = client.get_dividends("600887.SH")
        shown = client._store["dividends"]["end_date"].str[:4].nunique()
        assert shown == 5
        # Only the display table is capped; the complete-FY note may list more.
        assert "20201231" not in md and "20191231" not in md
        assert "20251231" in md


class TestAdjustedWeeklyPrices:
    def test_hfq_adjust_passed_to_daily(self, client, monkeypatch):
        captured = {}

        def fake_daily(num, adjust=""):
            captured["adjust"] = adjust
            return pd.DataFrame({
                "trade_date": ["20250103", "20250110"],
                "open": [1.0 * 2, 2.0 * 2], "high": [2, 4], "low": [1, 2],
                "close": [2, 4], "vol": [1e6, 1e6], "amount": [1e6, 1e6],
            })

        monkeypatch.setattr(client, "_fetch_daily", fake_daily)
        weekly = client._fetch_weekly("600887", years=10, adjust="hfq")
        assert captured["adjust"] == "hfq"
        assert len(weekly) >= 1

    def test_weekly_prices_use_hfq_for_stats_but_keep_unadjusted_contract(
            self, client, monkeypatch):
        raw = _weekly_frame()
        hfq = _weekly_frame(scale=2.0)

        def fake_weekly(num, years=10, adjust=""):
            return hfq if adjust == "hfq" else raw

        monkeypatch.setattr(client, "_fetch_weekly", fake_weekly)
        md = client.get_weekly_prices("600887.SH")
        # Downstream PE Band / PS require the unadjusted store contract.
        assert client._store["weekly_prices"] is raw
        assert client._store["price_basis"] == "unadjusted"
        assert client._store["weekly_prices_basis"] == "hfq"
        assert "weekly_prices_hfq" in client._store
        assert "后复权" in md
        assert "最新收盘（未复权，实际价）" in md

    def test_weekly_prices_fallback_labels_unadjusted(self, client, monkeypatch):
        raw = _weekly_frame()

        def fake_weekly(num, years=10, adjust=""):
            return pd.DataFrame() if adjust == "hfq" else raw

        monkeypatch.setattr(client, "_fetch_weekly", fake_weekly)
        md = client.get_weekly_prices("600887.SH")
        assert "后复权周线获取失败" in md
        assert client._store["weekly_prices_basis"] == "unadjusted"


class TestValuationDateAlignment:
    """Baidu valuation forward-fills non-trading days (e.g. Saturday = Friday)."""

    @staticmethod
    def _frames():
        dates = ["2026-10-29", "2026-10-30", "2026-10-31"]
        return {
            "valuation:601088:市盈率(TTM)": pd.DataFrame({"date": dates, "value": [19.24, 19.38, 19.38]}),
            "valuation:601088:市净率": pd.DataFrame({"date": dates, "value": [2.52, 2.54, 2.54]}),
            "valuation:601088:总市值": pd.DataFrame({"date": dates, "value": [11196.09, 11276.34, 11276.34]}),
        }

    def test_aligned_to_price_date_skips_forward_filled_day(self, client, monkeypatch):
        frames = self._frames()
        monkeypatch.setattr(client, "_cached_call",
                            lambda endpoint, fetch, key=None: frames[key])
        val = client._fetch_valuation("601088", price_date="20261030")
        assert val["date"] == "20261030"
        assert round(val["pe_ttm"], 2) == 19.38
        assert round(val["total_mv_wan"]) == 112_763_400  # 11276.34 亿元 → 万元

    def test_no_history_before_price_date_returns_empty(self, client, monkeypatch):
        frames = self._frames()
        monkeypatch.setattr(client, "_cached_call",
                            lambda endpoint, fetch, key=None: frames[key])
        # Price date older than every valuation row → cannot align, do not fake it.
        assert client._fetch_valuation("601088", price_date="20261001") == {}

    def test_basic_info_keeps_market_cap_when_aligned(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(client, "_fetch_name_and_list_date",
                            lambda ts_code: ("中国神华", "2007-10-09"))
        monkeypatch.setattr(client, "_fetch_latest_daily",
                            lambda num: {"close": 51.99, "date": "20261030"})

        def fake_valuation(num, price_date=None):
            captured["price_date"] = price_date
            return {"date": "20261030", "pe_ttm": 19.38, "pb": 2.54,
                    "total_mv_wan": 112_763_400.0}

        monkeypatch.setattr(client, "_fetch_valuation", fake_valuation)
        md = client.get_basic_info("601088.SH")
        info = client._store["basic_info"].iloc[0]
        assert captured["price_date"] == "20261030"
        assert info["total_mv"] == pytest.approx(112_763_400.0)
        assert info["pe_ttm"] == pytest.approx(19.38)
        assert info["total_share"] == pytest.approx(112_763_400.0 / 51.99, rel=1e-6)
        assert "总市值 (万元)" in md


class TestActualShares:
    def test_eastmoney_latest_price_match_is_verified(self, client, monkeypatch):
        em = pd.DataFrame({"item": ["最新", "总股本"], "value": [38.70, 5_601_405_741]})
        calls = []

        def fake_fetch(fetch, retries=1):
            calls.append(1)
            return em if len(calls) == 1 else pd.DataFrame()

        monkeypatch.setattr(client, "_fetch_with_retry", fake_fetch)
        out = client._fetch_actual_shares("000651", 38.70, "20261105")
        assert out is not None
        assert out["total_share_wan"] == pytest.approx(560_140.5741)
        assert out["source"] == "eastmoney_individual_info"
        assert len(calls) == 1  # no cninfo fallback needed

    def test_eastmoney_price_mismatch_falls_back_to_stale_cninfo_and_rejects(self, client, monkeypatch):
        em = pd.DataFrame({"item": ["最新", "总股本"], "value": [38.90, 5_601_405_741]})
        cn = pd.DataFrame({"变动日期": ["2026-06-30"], "总股本": [560_140.5741]})
        calls = []

        def fake_fetch(fetch, retries=1):
            calls.append(1)
            return em if len(calls) == 1 else cn

        monkeypatch.setattr(client, "_fetch_with_retry", fake_fetch)
        assert client._fetch_actual_shares("000651", 38.70, "20261105") is None
        assert len(calls) == 2

    def test_recent_cninfo_change_is_accepted(self, client, monkeypatch):
        em = pd.DataFrame()
        cn = pd.DataFrame({"变动日期": ["2026-11-03"], "总股本": [560_140.5741]})
        calls = []

        def fake_fetch(fetch, retries=1):
            calls.append(1)
            return em if len(calls) == 1 else cn

        monkeypatch.setattr(client, "_fetch_with_retry", fake_fetch)
        out = client._fetch_actual_shares("000651", 38.70, "20261105")
        assert out is not None
        assert out["source"] == "cninfo_share_change"
        assert out["total_share_wan"] == pytest.approx(560_140.5741)

    def test_basic_info_uses_verified_shares_over_market_cap_inference(self, client, monkeypatch):
        monkeypatch.setattr(client, "_fetch_name_and_list_date", lambda ts_code: ("格力电器", "1996-11-18"))
        monkeypatch.setattr(client, "_fetch_latest_daily", lambda num: {"close": 38.70, "date": "20261105"})
        monkeypatch.setattr(client, "_fetch_valuation", lambda num, price_date=None: {
            "date": "20261105", "pe_ttm": 7.5, "pb": 1.45, "total_mv_wan": 21_823_100.0})
        monkeypatch.setattr(client, "_fetch_actual_shares", lambda num, close, quote_date: {
            "total_share_wan": 560_140.5741, "basis": "test actual shares",
            "change_date": "20261105", "source": "test"})
        client.get_basic_info("000651.SZ")
        row = client._store["basic_info"].iloc[0]
        assert bool(row["shares_verified"]) is True
        assert row["total_share"] == pytest.approx(560_140.5741)
        assert row["total_mv"] == pytest.approx(560_140.5741 * 38.70)
