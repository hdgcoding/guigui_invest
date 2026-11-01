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


class TestPEBasis:
    """The bulk path only supplies annual EPS, so the real path is static PE;
    TTM must be preferred whenever a source actually provides it."""

    def _mock_bulk_sources(self, monkeypatch, screener):
        monkeypatch.setattr(screener, "_fetch_spot", lambda: pd.DataFrame(
            {"code": ["600001"], "name": ["好公司"], "close": [10.0], "amount": [5e7]}))
        monkeypatch.setattr(screener, "_fetch_yjbb", lambda: pd.DataFrame(
            {"code": ["600001"], "eps": [1.0], "bps": [5.0], "roe": [15.0],
             "n_profit": [1e9], "industry": ["食品"]}))
        monkeypatch.setattr(screener, "_fetch_listing_dates", lambda: pd.DataFrame(
            {"code": ["600001"], "list_date": ["20000101"]}))
        monkeypatch.setattr(screener, "_fetch_fhps_market", lambda: pd.DataFrame(
            {"code": ["600001"], "dps_ttm": [0.5], "total_shares": [1e9]}))

    def test_real_bulk_path_computes_static_pe(self, screener, monkeypatch):
        self._mock_bulk_sources(monkeypatch, screener)
        bulk = screener._tier1_bulk_data()
        row = bulk.iloc[0]
        assert row["pe_static"] == pytest.approx(10.0)
        assert pd.isna(row["pe_ttm"])           # bulk source supplies no TTM PE
        assert row["pe_basis"] == "静态（最新年报EPS）"
        main = screener._tier1_filter(bulk)
        main = main[main["channel"] == "main"]
        assert "好公司" in set(main["name"])
        assert main.iloc[0]["pe"] == pytest.approx(10.0)

    def test_ttm_pe_preferred_over_static(self, screener):
        df = _bulk_df().head(2)                 # 好公司 (TTM 10), 贵公司 (TTM 200)
        df["pe_static"] = [200.0, 10.0]         # static values would flip the verdict
        out = screener._tier1_filter(df)
        main_names = set(out[out["channel"] == "main"]["name"])
        assert "好公司" in main_names           # TTM 10 wins over static 200
        assert "贵公司" not in main_names       # TTM 200 wins over static 10
        row = out[out["name"] == "好公司"].iloc[0]
        assert row["pe_basis"] == "TTM"
        assert row["pe"] == pytest.approx(10.0)

    def test_static_pe_fallback_when_ttm_missing(self, screener):
        df = _bulk_df().head(1)
        df["pe_static"] = [12.0]
        df["pe_ttm"] = float("nan")
        out = screener._tier1_filter(df)
        row = out[out["channel"] == "main"].iloc[0]
        assert row["pe_basis"] == "静态（最新年报EPS）"
        assert row["pe"] == pytest.approx(12.0)

    def test_negative_ttm_pe_still_observation(self, screener):
        df = _bulk_df()
        df.loc[df["name"] == "亏损股", "pe_ttm"] = -20.0
        out = screener._tier1_filter(df)
        obs = out[out["channel"] == "observation"]
        assert "亏损股" in set(obs["name"])


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

    def test_unverified_r_weight_dropped_and_renormalized(self, screener):
        """Previously R=None still consumed 25%: best score was capped at 0.75."""
        df = pd.DataFrame({
            "ts_code": ["A", "B"],
            "roe_metric": [20.0, 5.0],
            "fcf_yield": [8.0, 2.0],
            "R": [None, None],
            "ev_ebitda": [5.0, 20.0],
            "floor_premium": [-10.0, 80.0],
            "R_status": ["proxy_unverified", "missing"],
        })
        ranked = screener._compute_rankings(df)
        scoring = ranked.attrs["scoring"]
        assert scoring["penetration_r_included"] is False
        assert set(scoring["weights"]) == {"roe", "fcf_yield", "ev_ebitda", "floor_premium"}
        assert sum(scoring["weights"].values()) == pytest.approx(1.0)
        assert ranked["composite_score"].max() == pytest.approx(1.0)  # not 0.75
        assert bool(ranked["composite_r_included"].iloc[0]) is False

    def test_verified_r_weight_participates(self, screener):
        df = pd.DataFrame({
            "ts_code": ["A", "B"],
            "roe_metric": [20.0, 5.0],
            "fcf_yield": [8.0, 2.0],
            "R": [6.0, 1.0],
            "ev_ebitda": [5.0, 20.0],
            "floor_premium": [-10.0, 80.0],
            "R_status": ["verified", "verified"],
        })
        ranked = screener._compute_rankings(df)
        scoring = ranked.attrs["scoring"]
        assert scoring["penetration_r_included"] is True
        assert "penetration_r" in scoring["weights"]
        assert sum(scoring["weights"].values()) == pytest.approx(1.0)
        assert scoring["weights"]["penetration_r"] == pytest.approx(0.25)
        assert bool(ranked["composite_r_included"].iloc[0]) is True

    def test_rankings_without_r_column(self, screener):
        df = pd.DataFrame({"ts_code": ["A"], "roe_metric": [10.0],
                           "fcf_yield": [5.0], "ev_ebitda": [8.0],
                           "floor_premium": [10.0]})
        ranked = screener._compute_rankings(df)
        assert ranked.attrs["scoring"]["penetration_r_included"] is False
        assert bool(ranked["composite_r_included"].iloc[0]) is False

    def test_legacy_roe_waa_column_supported(self, screener):
        df = pd.DataFrame({
            "ts_code": ["A", "B"],
            "roe_waa": [20.0, 5.0],
            "fcf_yield": [8.0, 2.0],
            "R": [None, None],
            "ev_ebitda": [5.0, 20.0],
            "floor_premium": [-10.0, 80.0],
        })
        ranked = screener._compute_rankings(df)
        assert ranked["roe_waa_pctile"].iloc[0] == pytest.approx(1.0)


def _ranked_frame(n_main: int = 6, n_obs: int = 2) -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": [f"M{i}" for i in range(n_main)] + [f"O{i}" for i in range(n_obs)],
        "name": [f"main{i}" for i in range(n_main)] + [f"obs{i}" for i in range(n_obs)],
        "channel": ["main"] * n_main + ["observation"] * n_obs,
    })


class TestTier2Limit:
    def test_limit_keeps_observation_channel(self, screener):
        out = screener._apply_tier2_limit(_ranked_frame(6, 2), 3)
        assert len(out) == 3
        assert (out["channel"] == "observation").any()
        assert out["ts_code"].str.startswith("O").any()

    def test_quota_splits_by_channel_caps(self, screener):
        # default caps 150 main : 50 obs → 75%/25% of the budget
        out = screener._apply_tier2_limit(_ranked_frame(30, 30), 40)
        assert len(out) == 40
        assert (out["channel"] == "observation").sum() == 10
        assert (out["channel"] == "main").sum() == 30

    def test_none_keeps_everything(self, screener):
        frame = _ranked_frame(3, 2)
        assert len(screener._apply_tier2_limit(frame, None)) == 5

    def test_invalid_limit_rejected(self, screener):
        with pytest.raises(ValueError, match="tier2_limit"):
            screener._apply_tier2_limit(_ranked_frame(1, 1), 0)

    def test_run_with_limit_keeps_observation_stock(self, screener, monkeypatch):
        ranked = _ranked_frame(3, 1)
        monkeypatch.setattr(screener, "_tier1_bulk_data", lambda: pd.DataFrame({"dummy": [1]}))
        monkeypatch.setattr(screener, "_tier1_filter", lambda df: ranked)
        monkeypatch.setattr(screener, "_tier1_rank_and_cut", lambda df: df)
        analyzed = []

        def _fake_analyze(row):
            analyzed.append(row["ts_code"])
            return {"ts_code": row["ts_code"], "name": row["name"],
                    "channel": row["channel"], "roe_metric": 10.0,
                    "fcf_yield": 5.0, "R": None, "ev_ebitda": 10.0,
                    "floor_premium": 20.0, "R_status": "proxy_unverified"}

        monkeypatch.setattr(screener, "_analyze_single_stock", _fake_analyze)
        out = screener.run(tier2_limit=3)          # lead's reproducer: limit=3
        assert "O0" in analyzed                    # observation not starved by head()
        assert len(analyzed) == 3 and len(out) == 3


class TestConfigValidation:
    def test_default_weights_valid(self):
        cfg = ScreenerConfig()
        assert cfg.validate() == []

    def test_bad_weights(self):
        cfg = ScreenerConfig(weight_roe=0.5)
        assert any("weights" in e for e in cfg.validate())

    def test_four_factor_only_config_valid(self):
        cfg = ScreenerConfig(weight_roe=0.25, weight_fcf_yield=0.25,
                             weight_ev_ebitda=0.25, weight_floor_premium=0.25,
                             weight_penetration_r=0.0)
        assert cfg.validate() == []

    def test_invalid_thresholds_and_ttls_rejected(self):
        assert ScreenerConfig(max_pe=-1).validate()
        assert ScreenerConfig(tier2_main_limit=0).validate()
        assert ScreenerConfig(max_pledge_pct=150).validate()
        assert ScreenerConfig(cache_tier2_ttl_hours=-1).validate()
        assert ScreenerConfig(dv_weight=0.5).validate()

    def test_renormalized_weights(self):
        cfg = ScreenerConfig()
        four = cfg.renormalized_scoring_weights(include_penetration_r=False)
        assert sum(four.values()) == pytest.approx(1.0)
        assert "penetration_r" not in four
        assert four["roe"] == pytest.approx(0.20 / 0.75)
        five = cfg.renormalized_scoring_weights(include_penetration_r=True)
        assert sum(five.values()) == pytest.approx(1.0)
        assert five["penetration_r"] == pytest.approx(0.25)

    def test_invalid_config_raises_on_init(self):
        with pytest.raises(ValueError, match="Invalid ScreenerConfig"):
            AKShareScreener(ScreenerConfig(weight_roe=0.9))

    def test_run_revalidates_mutated_config(self, screener):
        screener.config.weight_roe = 0.9  # mutated after init (CLI/test pattern)
        with pytest.raises(ValueError, match="Invalid ScreenerConfig"):
            screener.run(tier1_only=True)


class TestCacheTTLConfig:
    """cache_* TTL fields must control the caches instead of hard-coded 6h/24h/7d."""

    def test_ttl_seconds_mapping(self):
        cfg = ScreenerConfig(cache_stock_basic_ttl_days=3,
                             cache_daily_basic_ttl_days=0.5,
                             cache_rf_ttl_days=2,
                             cache_tier2_ttl_hours=5,
                             cache_tier2_financial_ttl_hours=11,
                             cache_tier2_market_ttl_hours=13,
                             cache_tier2_global_ttl_hours=17)
        assert cfg.cache_ttl_seconds == {
            "spot": 43200, "stock_basic": 259200, "rf": 172800,
            "tier2": 18000, "financial": 39600, "market": 46800,
            "global": 61200,
        }

    def test_spot_uses_daily_basic_ttl(self, screener, monkeypatch):
        recorded = []

        class _Cache:
            def get(self, key, ttl):
                recorded.append(ttl)
                return None

            def put(self, key, df):
                pass

        monkeypatch.setattr(screener, "cache", _Cache())
        monkeypatch.setattr(screener.client, "_fetch_with_retry", lambda fetch: pd.DataFrame(
            {"代码": ["sh600001"], "名称": ["好公司"], "最新价": [10.0], "成交额": [5e7]}))
        screener._fetch_spot()
        assert recorded[-1] == screener.config.cache_ttl_seconds["spot"]

    def test_listing_uses_stock_basic_ttl(self, screener, monkeypatch):
        recorded = []

        class _Cache:
            def get(self, key, ttl):
                recorded.append(ttl)
                return None

            def put(self, key, df):
                pass

        monkeypatch.setattr(screener, "cache", _Cache())
        monkeypatch.setattr(screener.client, "_fetch_with_retry", lambda fetch: pd.DataFrame())
        screener._fetch_listing_dates()
        assert recorded[-1] == screener.config.cache_ttl_seconds["stock_basic"]

    def test_client_cached_call_uses_config_ttls(self, monkeypatch):
        cfg = ScreenerConfig(cache_tier2_financial_ttl_hours=5,
                             cache_tier2_market_ttl_hours=7,
                             cache_rf_ttl_days=2,
                             cache_tier2_ttl_hours=11)
        s = AKShareScreener(cfg)
        recorded = []

        class _Cache:
            def get(self, key, ttl):
                recorded.append(ttl)
                return None

            def put(self, key, df):
                pass

        monkeypatch.setattr(s.client, "_cache", _Cache())
        monkeypatch.setattr(s.client, "_fetch_with_retry",
                            lambda fetch: pd.DataFrame({"x": [1]}))
        s.client._cached_call("income", lambda: None)
        s.client._cached_call("daily_raw", lambda: None)
        s.client._cached_call("risk_free_rate", lambda: None)
        assert recorded == [5 * 3600, 7 * 3600, 2 * 86400]


class TestBulkCacheStatus:
    def test_fetch_failure_is_not_recorded_as_empty_success(self, screener, monkeypatch):
        monkeypatch.setattr(screener.client, "_fetch_with_retry", lambda fetch: None)
        out = screener._bulk_cached("key", 10, lambda: None)
        assert out.empty
        assert screener._last_bulk_status == "failed"
