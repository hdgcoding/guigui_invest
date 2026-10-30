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
