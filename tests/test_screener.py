"""Tests for AKShareScreener Tier 1 filter/rank + Tier 2 scoring (no network)."""

import pandas as pd
import pytest

from screener_config import ScreenerConfig
from screener_core import AKShareScreener


@pytest.fixture
def screener():
    return AKShareScreener(ScreenerConfig())


def _bulk_df(rows=None):
    """Synthetic Tier 1 universe snapshot."""
    default = [
        # code, name, close, amount, eps, bps, roe, industry, list_date, dps_ttm, total_shares
        ("600001", "好公司", 10.0, 5e7, 1.0, 5.0, 15.0, "食品", "20000101", 0.5, 1e9),
        ("600002", "贵公司", 100.0, 5e7, 0.5, 2.0, 8.0, "食品", "20000101", 0.5, 1e9),   # PE=200
        ("600003", "ST差司", 5.0, 5e7, 0.2, 3.0, 3.0, "食品", "20000101", 0.1, 1e9),     # ST
        ("600004", "次新股", 8.0, 5e7, 0.5, 4.0, 10.0, "食品", "20250101", 0.3, 1e9),    # 上市<3y
        ("600005", "银行股", 6.0, 5e7, 1.0, 6.0, 12.0, "银行Ⅱ", "20000101", 0.4, 1e9),  # 银行
        ("600006", "亏损股", 20.0, 5e7, -1.0, 8.0, -5.0, "医药", "20000101", None, 5e9),  # 观察通道
        ("600007", "微盘股", 3.0, 5e7, 0.2, 1.5, 5.0, "食品", "20000101", 0.1, 1e6),    # 市值3千万<5亿
        ("600008", "高PB股", 12.0, 5e7, 0.5, 0.5, 10.0, "食品", "20000101", 0.2, 1e9),  # PB=24
    ]
    rows = rows or default
    df = pd.DataFrame(rows, columns=[
        "code", "name", "close", "amount", "eps", "bps", "roe",
        "industry", "list_date", "dps_ttm", "total_shares"])
    df["pe_ttm"] = df["close"] / df["eps"].where(df["eps"] > 0)
    df["pb"] = df["close"] / df["bps"].where(df["bps"] > 0)
    df["dv_ttm"] = df["dps_ttm"] / df["close"] * 100
    df["total_mv"] = df["close"] * df["total_shares"] / 1e4
    df["n_profit"] = df["eps"] * df["total_shares"]
    df["ts_code"] = df["code"] + ".SH"
    return df


class TestTier1Filter:
    def test_st_excluded(self, screener):
        out = screener._tier1_filter(_bulk_df())
        assert "ST差司" not in set(out["name"])

    def test_young_listing_excluded(self, screener):
        out = screener._tier1_filter(_bulk_df())
        assert "次新股" not in set(out["name"])

    def test_bank_excluded_by_default(self, screener):
        out = screener._tier1_filter(_bulk_df())
        assert "银行股" not in set(out["name"])

    def test_bank_included_when_enabled(self, screener):
        screener.config.include_bank = True
        out = screener._tier1_filter(_bulk_df())
        assert "银行股" in set(out["name"])

    def test_high_pe_excluded_main(self, screener):
        out = screener._tier1_filter(_bulk_df())
        assert "贵公司" not in set(out[out["channel"] == "main"]["name"])

    def test_small_cap_excluded(self, screener):
        out = screener._tier1_filter(_bulk_df())
        assert "微盘股" not in set(out["name"])

    def test_high_pb_excluded(self, screener):
        out = screener._tier1_filter(_bulk_df())
        assert "高PB股" not in set(out["name"])

    def test_loss_making_to_observation_channel(self, screener):
        out = screener._tier1_filter(_bulk_df())
        obs = out[out["channel"] == "observation"]
        assert "亏损股" in set(obs["name"])

    def test_dividend_required_main_only(self, screener):
        out = screener._tier1_filter(_bulk_df())
        main = out[out["channel"] == "main"]
        assert (main["dv_ttm"] > 0).all()


class TestTier1Rank:
    def test_main_ranked_and_cut(self, screener):
        screener.config.tier2_main_limit = 1
        df = _bulk_df()
        filtered = screener._tier1_filter(df)
        ranked = screener._tier1_rank_and_cut(filtered)
        main = ranked[ranked["channel"] == "main"]
        assert len(main) == 1
        assert main.iloc[0]["name"] == "好公司"  # PE10/PB2/dv5% 最优

    def test_score_bounds(self, screener):
        filtered = screener._tier1_filter(_bulk_df())
        ranked = screener._tier1_rank_and_cut(filtered)
        assert ranked["tier1_score"].between(0, 1).all()


class TestRankings:
    def test_composite_score_ordering(self, screener):
        df = pd.DataFrame({
            "ts_code": ["A", "B", "C"],
            "roe_waa": [20.0, 10.0, 5.0],
            "fcf_yield": [8.0, 4.0, 2.0],
            "R": [6.0, 3.0, 1.0],
            "ev_ebitda": [5.0, 10.0, 20.0],     # lower better
            "floor_premium": [-10.0, 20.0, 80.0],  # lower better
        })
        ranked = screener._compute_rankings(df)
        assert list(ranked["ts_code"]) == ["A", "B", "C"]
        assert ranked["composite_score"].iloc[0] > ranked["composite_score"].iloc[2]

    def test_empty(self, screener):
        assert screener._compute_rankings(pd.DataFrame()).empty


class TestConfigValidation:
    def test_default_weights_valid(self):
        cfg = ScreenerConfig()
        assert cfg.validate() == []

    def test_bad_weights(self):
        cfg = ScreenerConfig(weight_roe=0.5)
        assert any("weights" in e for e in cfg.validate())
