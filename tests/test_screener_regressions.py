import sys

import pandas as pd
import pytest

from screener_core import AKShareScreener, main


@pytest.fixture
def screener(financial_client):
    result = AKShareScreener()
    result.client = financial_client
    return result


def test_missing_indicators_derive_quality_without_keyerror(screener):
    passed, metrics = screener._check_financial_quality("main")
    assert passed
    assert metrics["roe_metric"] == 20 and metrics["roe_waa"] is None
    assert metrics["roe_basis"] == "摊薄ROE（非加权）"


def test_missing_quality_metric_cannot_pass(screener):
    screener.client._store["balance_sheet"]["total_liab"] = float("nan")
    passed, metrics = screener._check_financial_quality("main")
    assert not passed and metrics["debt_to_assets"] is None


def test_loss_observation_not_implicitly_rejected_by_positive_roe(screener):
    screener.client._store["income"]["n_income_attr_p"] = -5e6
    passed, metrics = screener._check_financial_quality("observation")
    assert passed and metrics["roe_metric"] < 0
    # Explicit user thresholds still apply.
    screener.config.min_roe_obs = 0
    assert not screener._check_financial_quality("observation")[0]


def test_missing_rank_inputs_get_zero_not_best_rank(screener):
    frame = pd.DataFrame({"name": ["complete", "missing"], "roe_waa": [15, None], "fcf_yield": [5, None],
                          "R": [4, None], "ev_ebitda": [10, None], "floor_premium": [20, None]})
    ranked = screener._compute_rankings(frame)
    assert list(ranked["name"]) == ["complete", "missing"]
    assert ranked.iloc[-1]["composite_score"] == 0


def test_fcf_margin_units_and_signed_ebitda(screener):
    result = screener._extract_factor4_metrics(100000)
    assert result["fcf_margin"] == 20
    assert result["ebitda"] == 26  # 25 - 2 + 3 (millions), not 25 + abs(-2)


def test_missing_capex_and_da_not_replaced_by_zero(screener):
    screener.client._store["cashflow"]["c_pay_acq_const_fiolta"] = float("nan")
    screener.client._store["cashflow"]["amort_intang_assets"] = float("nan")
    result = screener._extract_factor4_metrics(100000)
    assert result["fcf"] is None and "fcf_yield" not in result
    assert "ev_ebitda" not in result
    passed, _ = screener._check_financial_quality("observation")
    assert not passed


def test_missing_goodwill_not_a_zero_risk_ratio(screener):
    screener.client._store["balance_sheet"]["goodwill"] = float("nan")
    assert "goodwill_ratio" not in screener._extract_factor4_metrics(100000)


def test_screener_aa_window_matches_section17(screener):
    result = screener._extract_factor2_metrics(100000)
    assert result["AA_proxy"] == 19
    # The consolidated-cash proxy is diagnostic only; it must never be scored
    # as a penetration return (that would silently dilute the composite).
    assert result["R"] is None
    assert result["R_status"] == "proxy_unverified"
    assert result["R_proxy"] == pytest.approx(19 * (45 / 100) / 1000 * 100)  # 0.855%
    assert "未核实" in result["R_proxy_label"]
    assert result["R_vs_II"] is None


def test_verified_r_uses_reconciled_cash_and_verified_market_cap(screener):
    store = screener.client._store
    store["owner_cash_records"] = [
        {"end_date": "20251231", "owner_surplus_yuan": 20e6},
        {"end_date": "20241231", "owner_surplus_yuan": 18e6},
    ]
    store["basic_info"] = pd.DataFrame([{
        "ts_code": "600001.SH", "close": 10.0, "total_mv": 120000.0,
        "shares_verified": True, "trade_date": "20261030"}])
    result = screener._extract_factor2_metrics(100000)
    assert result["R_status"] == "verified"
    # AA=19 (百万元), M=45%, verified market cap=120000万元=1200百万元, A股默认税率0%（持有>1年）
    assert result["R"] == pytest.approx(19 * (45 / 100) / 1200 * 100)
    assert result["R_tax_pct"] == 0
    assert result["R_proxy"] == pytest.approx(0.855)   # proxy keeps the unverified market cap
    assert result["R_vs_II"] == "below_rf"


def test_verified_r_requires_verified_shares(screener):
    store = screener.client._store
    store["owner_cash_records"] = [
        {"end_date": "20251231", "owner_surplus_yuan": 20e6},
        {"end_date": "20241231", "owner_surplus_yuan": 18e6},
    ]
    store["basic_info"] = pd.DataFrame([{
        "ts_code": "600001.SH", "close": 10.0, "total_mv": 120000.0}])
    result = screener._extract_factor2_metrics(100000)
    assert result["R"] is None
    assert result["R_status"] == "proxy_unverified"


def test_dividend_floor_uses_recent_years_not_largest(screener):
    divs = screener.client._store["dividends_all"]
    divs["cash_div_tax"] = [.1, .2, .3, 9]
    screener.client._store["dividends"] = divs.copy()
    result = screener._extract_floor_price(10, 100000)
    value = dict(result["baselines"])["dividend_implied"]
    assert value == pytest.approx(.2 / .03)


def test_missing_balance_items_do_not_create_fake_safety(screener):
    screener.client._store["balance_sheet"] = pd.DataFrame()
    screener.client._store["weekly_prices"] = pd.DataFrame()
    screener.client._store["cashflow"] = pd.DataFrame()
    result = screener._extract_floor_price(10, 100000)
    assert result["composite_baseline"] is None


def test_bulk_missing_apis_does_not_crash(screener, monkeypatch):
    monkeypatch.setattr(screener, "_fetch_spot", lambda: pd.DataFrame({"code": ["600001"], "name": ["测试"], "close": [10], "amount": [1e8]}))
    for name in ("_fetch_yjbb", "_fetch_listing_dates", "_fetch_fhps_market"):
        monkeypatch.setattr(screener, name, lambda: pd.DataFrame())
    result = screener._tier1_bulk_data()
    assert "pe_static" in result and "pe_basis" in result
    # No EPS/PE source at all → basis is honestly "缺失", never a fake ratio.
    assert result.iloc[0]["pe_basis"] == "缺失"
    assert pd.isna(result.iloc[0]["pe"])
    assert screener._tier1_filter(result).empty
    assert any(item["missing"] for item in screener.diagnostics)


def test_nonpayer_loss_stock_can_reach_observation(screener, monkeypatch):
    monkeypatch.setattr(screener, "_fetch_spot", lambda: pd.DataFrame({"code": ["600001"], "name": ["测试"], "close": [10], "amount": [1e8]}))
    monkeypatch.setattr(screener, "_fetch_yjbb", lambda: pd.DataFrame({"code": ["600001"], "eps": [-1.0], "bps": [5], "n_profit": [-1e8], "industry": ["医药"]}))
    monkeypatch.setattr(screener, "_fetch_listing_dates", lambda: pd.DataFrame({"code": ["600001"], "list_date": ["20000101"]}))
    monkeypatch.setattr(screener, "_fetch_fhps_market", lambda: pd.DataFrame())
    out = screener._tier1_filter(screener._tier1_bulk_data())
    assert out.iloc[0]["channel"] == "observation"
    assert "推算" in out.iloc[0]["shares_basis"]


def test_default_export_does_not_generate_html(screener, monkeypatch):
    import screener_core
    monkeypatch.setattr(sys, "argv", ["screener_core.py"])
    monkeypatch.setattr(screener_core, "AKShareScreener", lambda cfg: screener)
    monkeypatch.setattr(screener, "run", lambda **kwargs: pd.DataFrame({"ts_code": ["600001.SH"]}))
    exported = []
    monkeypatch.setattr(screener, "export_csv", lambda *args: exported.append("csv"))
    monkeypatch.setattr(screener, "export_html", lambda *args: exported.append("html"))
    assert main() == 0
    assert exported == ["csv"]


def test_cache_refresh_clears_both_caches(screener, monkeypatch):
    import screener_core
    monkeypatch.setattr(sys, "argv", ["screener_core.py", "--cache-refresh"])
    monkeypatch.setattr(screener_core, "AKShareScreener", lambda cfg: screener)
    calls = []
    monkeypatch.setattr(screener.cache, "clear", lambda: calls.append("bulk"))
    monkeypatch.setattr(screener.client._cache, "clear", lambda: calls.append("financial"))
    monkeypatch.setattr(screener, "run", lambda **kwargs: pd.DataFrame())
    assert main() == 1
    assert calls == ["bulk", "financial"]


def test_missing_pledge_is_unverified_not_a_risk_pass(screener, monkeypatch):
    monkeypatch.setattr(screener, "_bulk_cached", lambda *args: pd.DataFrame())
    not_rejected, _ = screener._check_hard_vetoes("600001.SH")
    # May remain a provisional research candidate, never a buy qualification...
    assert not_rejected
    assert screener._veto_checks["pledge_status"].startswith("未核实")
    assert "不等于无质押" in screener._veto_checks["pledge_status"]
    # ...and the degradation is exported, not silently treated as "no pledge".
    flags = screener._veto_checks["degraded_flags"].split(";")
    assert "pledge_unverified" in flags
    assert "audit_unverified" in flags


def test_pledge_fetch_failure_is_flagged(screener, monkeypatch):
    monkeypatch.setattr(screener, "_bulk_cached", lambda *args: pd.DataFrame())
    monkeypatch.setattr(screener, "_last_bulk_status", "failed")
    passed, _ = screener._check_hard_vetoes("600001.SH")
    assert passed
    assert "调用失败" in screener._veto_checks["pledge_status"]
    assert "pledge_unverified" in screener._veto_checks["degraded_flags"].split(";")


def test_pledge_above_threshold_is_vetoed(screener, monkeypatch):
    df = pd.DataFrame({"占总股本比例": [40.0, 40.0], "状态": ["未解押", "未解押"]})
    monkeypatch.setattr(screener, "_bulk_cached", lambda *args: df)
    passed, reason = screener._check_hard_vetoes("600001.SH")
    assert not passed and "pledge_ratio" in reason
    assert screener._veto_checks["pledge_status"].startswith("已统计未解押明细")
    assert "pledge_veto" in screener._veto_checks["degraded_flags"].split(";")


def test_pledge_uses_unpledged_scope_only(screener, monkeypatch):
    df = pd.DataFrame({"占总股本比例": [10.0, 90.0], "状态": ["未解押", "已解押"]})
    monkeypatch.setattr(screener, "_bulk_cached", lambda *args: df)
    passed, _ = screener._check_hard_vetoes("600001.SH")
    assert passed
    assert screener._veto_checks["pledge_ratio"] == pytest.approx(10.0)
    assert "pledge_unverified" not in screener._veto_checks["degraded_flags"].split(";")


def test_degraded_flags_reach_result_row(screener, monkeypatch):
    monkeypatch.setattr(screener, "_bulk_cached", lambda *args: pd.DataFrame())
    monkeypatch.setattr(screener, "_populate_store", lambda ts_code: None)
    monkeypatch.setattr(screener, "_check_financial_quality", lambda channel: (True, {}))
    monkeypatch.setattr(screener, "_extract_factor2_metrics", lambda mv: {
        "M": 45.0, "R": None, "R_status": "proxy_unverified", "R_proxy": 0.855})
    monkeypatch.setattr(screener, "_extract_factor4_metrics", lambda mv: {})
    monkeypatch.setattr(screener, "_extract_floor_price", lambda close, mv: {})
    row = pd.Series({"ts_code": "600001.SH", "name": "测试", "close": 10.0,
                     "total_mv": 100000.0, "pe": 10.0, "pe_basis": "TTM"})
    result = screener._analyze_single_stock(row)
    assert "pledge_unverified" in result["degraded_flags"].split(";")
    assert "audit_unverified" in result["degraded_flags"].split(";")
    assert "未核实" in result["data_status"]


def test_all_released_pledges_are_zero_not_unverified(screener, monkeypatch):
    df = pd.DataFrame({"占总股本比例": [90.0], "状态": ["已解押"]})
    monkeypatch.setattr(screener, "_bulk_cached", lambda *args: df)
    passed, _ = screener._check_hard_vetoes("600001.SH")
    assert passed
    assert screener._veto_checks["pledge_ratio"] == 0.0
    assert "未核实" not in screener._veto_checks["pledge_status"]
    assert "pledge_unverified" not in screener._veto_checks["degraded_flags"].split(";")
