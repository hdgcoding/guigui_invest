"""Tests for AKShareClient infrastructure helpers (no network)."""

from datetime import date

import pandas as pd
import pytest

from akshare_collector import AKShareClient
from akshare_modules.other_data import unreleased_pledges


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

    def test_pd_na(self):
        """pd.NA must not raise through a `val == val` coercion."""
        assert AKShareClient._safe_float(pd.NA) is None

    def test_fmt_fixed_pd_na(self):
        c = AKShareClient()
        assert c._fmt_fixed(pd.NA) == "—"
        assert c._fmt_fixed(None) == "—"
        assert c._fmt_fixed(1.234) == "1.23"

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


class TestFieldMapping:
    def test_pd_na_eps_missing_does_not_raise(self, client, monkeypatch):
        """Old `val == val` raised TypeError when 基本每股收益 was unmapped (pd.NA)."""
        raw = pd.DataFrame({
            "报告日": ["2025-12-31", "2024-12-31"],
            "营业总收入": ["1000000000.00", "900000000.00"],
            "归属于母公司所有者的净利润": ["120000000.00", "110000000.00"],
            "类型": ["合并报表", "合并报表"],
        })
        monkeypatch.setattr(client, "_cached_call",
                            lambda endpoint, fetch, key=None: raw)
        md = client.get_income("600001.SH")
        assert "基本EPS" in md
        # Mapped fields still render; the whole section is not "数据获取失败".
        assert "1,000.00" in md and "数据缺失" not in md
        assert "[字段映射缺失]" in md

    def test_mapping_hit_rate_warning_in_section(self, client, monkeypatch):
        raw = pd.DataFrame({
            "报告日": ["2025-12-31"],
            "营业总收入": ["1000000000.00"],
            "营业成本": ["600000000.00"],
            "类型": ["合并报表"],
        })
        monkeypatch.setattr(client, "_cached_call",
                            lambda endpoint, fetch, key=None: raw)
        md = client.get_income("600001.SH")
        assert "字段命中" in md and "字段映射缺失" in md
        misses = client._store["_field_mapping_misses"]["利润表"]
        assert any(internal == "n_income_attr_p" for internal, _ in misses)
        assert not any(internal == "revenue" for internal, _ in misses)

    def test_fina_indicators_pd_na_does_not_raise(self, client, monkeypatch):
        frame = pd.DataFrame({
            "end_date": ["20251231", "20241231"],
            "roe": [pd.NA, pd.NA],
            "grossprofit_margin": [pd.NA, 41.5],
            "payout_ratio": [pd.NA, pd.NA],
        })
        monkeypatch.setattr(client, "_fetch_fina_indicators_df", lambda num: frame)
        md = client.get_fina_indicators("600001.SH")
        assert "ROE 摊薄 (%)" in md
        assert "—" in md


class TestSegments:
    @staticmethod
    def _em_frame():
        return pd.DataFrame({
            "股票代码": ["600001"] * 6,
            "报告日期": [date(2025, 12, 31), date(2025, 12, 31), date(2025, 12, 31),
                       date(2024, 12, 31), date(2025, 12, 31), date(2025, 12, 31)],
            "分类类型": ["按行业分类", "按行业分类", "按产品分类",
                       "按行业分类", "按行业分类", "按地区分类"],
            "主营构成": ["乳制品", "其他", "液奶", "旧行业", "未分类", "华东"],
            "主营收入": [1e10, 2e9, 8e9, 9e9, float("nan"), 3e9],
            "收入比例": [83.33, 16.67, 66.67, 80.0, float("nan"), 25.0],
            "主营成本": [7e9, 1.6e9, 6e9, 6.5e9, float("nan"), 2e9],
            "毛利率": [30.5, 20.0, 28.0, 29.0, float("nan"), 33.3],
        })

    def test_new_columns_latest_period_industry_only(self, client, monkeypatch):
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: self._em_frame())
        md = client.get_segments("600001.SH")
        # Latest report period only (2024 row and its 旧行业 must be gone).
        assert "2025-12-31" in md
        assert "旧行业" not in md
        # 按行业分类 only: product/region dimensions excluded.
        assert "液奶" not in md and "华东" not in md
        # 金额 ÷1e6 → 百万元; 比例/毛利率带 %.
        assert "10,000.00" in md
        assert "83.33%" in md and "30.50%" in md
        # Missing segment values are explicit, never a silent "—".
        assert "[字段映射缺失]" in md and "—[缺失]" in md
        assert client._store["segments_period"] == "2025-12-31"
        assert len(client._store["segments"]) == 3
        assert client._store["segments"].iloc[0]["主营收入(元)"] == pytest.approx(1e10)

    def test_legacy_column_names_supported(self, client, monkeypatch):
        raw = pd.DataFrame({
            "报告日期": [date(2025, 12, 31)] * 2,
            "分类方向": ["按行业分", "按行业分"],
            "分类": ["乳制品", "其他"],
            "主营收入": [5e8, 2e8],
            "收入比例": [71.43, 28.57],
            "毛利率": [50.0, 40.0],
        })
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: raw)
        md = client.get_segments("600001.SH")
        assert "乳制品" in md and "500.00" in md
        assert "按行业分" in md  # legacy 分类方向 value accepted

    def test_fraction_ratio_column_normalised(self, client, monkeypatch):
        raw = pd.DataFrame({
            "报告日期": [date(2025, 12, 31)] * 2,
            "分类类型": ["按行业分类"] * 2,
            "主营构成": ["A", "B"],
            "主营收入": [3e8, 1e8],
            "收入比例": [0.75, 0.25],  # fraction form, not percentage points
            "毛利率": [0.60, 0.20],
        })
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: raw)
        md = client.get_segments("600001.SH")
        assert "75.00%" in md and "60.00%" in md

    def test_missing_columns_warn_not_silent(self, client, monkeypatch):
        raw = pd.DataFrame({
            "报告日期": [date(2025, 12, 31)],
            "主营构成": ["A"],
            "主营收入": [1e8],
        })
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: raw)
        md = client.get_segments("600001.SH")
        assert "[字段映射缺失]" in md
        assert "毛利率" in md  # explicitly listed as missing
        assert "数据缺失" not in md


class TestPledge:
    @staticmethod
    def _detail_frame():
        return pd.DataFrame({
            "股东名称": ["甲", "甲", "乙", "丙", "丁"],
            "质押股份数量": [1e7, 1e7, 2e7, 3e7, 4e7],
            "占总股本比例": [5.0, 3.0, 2.0, 1.0, 4.0],
            "状态": ["未解押", "已解押", "未解押", None, None],
            "质押结束日期": [None, pd.Timestamp("2024-01-01"), None,
                        pd.Timestamp("2020-01-01"), pd.Timestamp("2030-01-01")],
        })

    def test_outstanding_only_counts_and_ratio(self, client, monkeypatch):
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: self._detail_frame())
        md = client.get_pledge_stat("600001.SH")
        # Outstanding: 甲(5.0) + 乙(2.0) + 丁(4.0) = 11.0%, 3 of 5 events.
        assert "| 未解押笔数 | 3 |" in md
        assert "11.00%" in md
        assert "| 质押事件笔数(全部) | 5 |" in md
        assert client._store["pledge_unreleased"].shape[0] == 3
        # Same-shareholder rows aggregated in the shareholder table.
        assert "未解押质押主要股东" in md

    def test_released_row_never_outstanding(self):
        df = pd.DataFrame({
            "状态": ["未解押", "已解押", None, None],
            "质押结束日期": [pd.NaT, pd.NaT,
                        pd.Timestamp("2020-01-01"), pd.Timestamp("2030-01-01")],
        })
        out = unreleased_pledges(df)
        assert len(out) == 2
        assert set(out.index) == {0, 3}

    def test_unknown_status_returns_no_claims(self):
        df = pd.DataFrame({"股东名称": ["甲"], "占总股本比例": [10.0]})
        assert unreleased_pledges(df).empty

    def test_assembly_pledge_risk_uses_unreleased_only(self, client):
        # Released pledges summing to 60% must not trigger PLEDGE_RISK.
        client._store["pledge"] = pd.DataFrame({
            "股东名称": ["甲", "乙"],
            "占总股本比例": [40.0, 20.0],
            "状态": ["已解押", "已解押"],
            "质押结束日期": [pd.Timestamp("2020-01-01"), pd.Timestamp("2021-01-01")],
        })
        client._store.pop("pledge_unreleased", None)
        assert client._check_pledge_risk() is None
        # Outstanding 60% does trigger the warning.
        client._store["pledge"] = pd.DataFrame({
            "股东名称": ["甲", "乙"],
            "占总股本比例": [40.0, 20.0],
            "状态": ["未解押", "未解押"],
            "质押结束日期": [pd.Timestamp("2030-01-01"), pd.Timestamp("2031-01-01")],
        })
        client._store.pop("pledge_unreleased", None)
        warning = client._check_pledge_risk()
        assert warning is not None and warning["type"] == "PLEDGE_RISK"
        assert "未解押" in warning["message"] and "60.0%" in warning["message"]


class TestFinancialCollectedAt:
    def test_real_financial_fetch_stamps_timestamp(self, client, monkeypatch):
        monkeypatch.setattr(client, "_fetch_with_retry",
                            lambda fetch: pd.DataFrame({"end_date": ["20251231"]}))
        df = client._cached_call("income", lambda: pd.DataFrame())
        assert len(df) == 1
        stamp = client._store.get("_financial_collected_at")
        assert stamp is not None
        assert pd.Timestamp(stamp) <= pd.Timestamp.now()

    def test_market_fetch_does_not_stamp(self, client, monkeypatch):
        monkeypatch.setattr(client, "_fetch_with_retry",
                            lambda fetch: pd.DataFrame({"close": [10.0]}))
        client._cached_call("daily_raw", lambda: pd.DataFrame())
        assert "_financial_collected_at" not in client._store


class TestCacheWriteWarning:
    def test_failed_put_warns_once_on_stderr(self, tmp_path, capsys):
        import cache_utils
        cache_utils._PUT_FAILURE_WARNED = False
        cache = cache_utils.ScreenerCache(str(tmp_path))
        bad = pd.DataFrame({"x": [object()]})  # pyarrow cannot serialise this
        cache.put("k1", bad)
        cache.put("k2", bad)
        err = capsys.readouterr().err
        assert "缓存写入失败" in err
        assert err.count("缓存写入失败") == 1


def test_all_pledges_released_shows_zero_not_missing(client, monkeypatch):
    raw = pd.DataFrame({
        "股东名称": ["甲", "乙"],
        "质押股份数量": [1e7, 2e7],
        "占总股本比例": [10.0, 5.0],
        "状态": ["已解押", "已解押"],
        "质押结束日期": [pd.Timestamp("2020-01-01"), pd.Timestamp("2021-01-01")],
    })
    monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: raw)
    md = client.get_pledge_stat("600001.SH")
    assert "| 未解押笔数 | 0 |" in md
    # Known status with nothing outstanding is 0%, not a missing ratio.
    assert "未解押质押股份占总股本比例(合计) | 0.00%" in md
    # Released events are never summed into the outstanding ratio.
    assert "15.00%" not in md


class TestPledgeUnverified:
    """A failed pledge endpoint must never look like 'no pledges'."""

    def test_interface_failure_is_unverified_not_zero(self, client, monkeypatch):
        monkeypatch.setattr(client, "_fetch_with_retry", lambda fetch: None)
        md = client.get_pledge_stat("600001.SH")
        assert "[数据未核实]" in md
        assert "不等于无质押" in md
        assert client._store["pledge_unverified"] is True

    def test_successful_empty_response_is_distinguishable(self, client, monkeypatch):
        monkeypatch.setattr(client, "_fetch_with_retry",
                            lambda fetch: pd.DataFrame())
        md = client.get_pledge_stat("600001.SH")
        assert "接口成功返回空表" in md
        assert "不等于全市场无质押" in md
        assert client._store["pledge_unverified"] is False
