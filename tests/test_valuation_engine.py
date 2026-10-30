import pandas as pd
import pytest

from valuation_engine import ValuationEngine


@pytest.fixture
def engine(financial_client, tmp_path):
    return ValuationEngine("600001.SH", str(tmp_path), financial_client)


@pytest.mark.parametrize("values,years,expected", [([200, -50, 100], None, 2**.5-1),
    ([200, None, 100], None, 2**.5-1), ([200, 100], [2025, 2023], 2**.5-1),
    ([None, 100], None, None), ([200, 0], None, None), ([200, -50], None, None)])
def test_cagr_preserves_calendar_duration(values, years, expected):
    actual = ValuationEngine._cagr(values, years)
    assert actual == pytest.approx(expected) if expected is not None else actual is None


def test_ddm_excludes_current_interim_and_does_not_infer_from_cf(engine):
    client = engine.client
    partial = pd.DataFrame([{"end_date": "20260630", "cash_div_tax": .01,
                            "div_proc": "实施", "ex_date": "2026-08-01", "base_share": 10000}])
    client._store["dividends_all"] = pd.concat([partial, client._store["dividends_all"]])
    annual = engine._aggregate_annual_dps()
    assert annual[0] == ("2025", .1)
    client._store["cashflow"]["c_pay_dist_dpcp_int_exp"] = 1e15
    assert engine._aggregate_annual_dps() == annual
    result = engine.ddm(8)
    assert result["dps_latest"] == .1 and result["dividend_year"] == "2025"


def test_negative_dividend_trend_not_extrapolated_perpetually(engine):
    engine.client._store["dividends_all"]["cash_div_tax"] = [.1, .12, .14, .16]
    result = engine.ddm(8)
    assert result["model_type"] == "Two-stage"
    assert result["g_used"] < 0 and result["g_terminal"] == 0
    assert result["intrinsic"] > 0


@pytest.mark.parametrize("ke,g", [(0, 0), (8, 8), (8, -100)])
def test_ddm_invalid_assumptions_are_ineligible(engine, ke, g):
    assert engine.ddm(ke, g) is None


def test_zero_dividend_disqualifies_stable_ddm(engine):
    engine.client._store["dividends_all"].loc[0, "cash_div_tax"] = 0
    assert engine.ddm(8) is None


def test_dcf_requires_identified_fcff(engine):
    assert engine.dcf_stable(engine.compute_wacc()) is None
    assert "FCFF" in engine.unavailable["DCF"]
    assert engine.dcf_scenarios(engine.compute_wacc()) is None


@pytest.mark.parametrize("cap_wan,beta", [(2000e4, 0.8), (500e4, 1.0), (50e4, 1.2)])
def test_beta_market_cap_yi_to_millions_conversion(engine, cap_wan, beta):
    engine.client._store["basic_info"].loc[0, "total_mv"] = cap_wan
    assert engine.compute_wacc()["beta"] == beta


def test_verified_positive_fcff_is_supported(engine):
    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [20e6, 19e6, 18e6, 17e6]
    result = engine.dcf_stable(engine.compute_wacc())
    assert result and result["intrinsic"] > 0
    assert len(result["sensitivity"]) == 5
    assert engine.dcf_scenarios(engine.compute_wacc())["intrinsic"] > 0


def test_negative_fcff_does_not_emit_negative_target(engine):
    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [-20e6]*4
    assert engine.dcf_stable(engine.compute_wacc()) is None


def test_adjusted_price_history_is_rejected(engine):
    engine.client._store["price_basis"] = "qfq"
    assert engine.pe_band() is None
    assert engine.ps() is None


def test_historical_ps_uses_historical_share_count(engine):
    result = engine.ps()
    engine.client._store["balance_sheet"].loc[3, "total_shares"] *= 2
    changed = engine.ps()
    assert changed["ps_stats"]["max"] > result["ps_stats"]["max"]


def test_monetary_paid_in_capital_is_not_a_share_count(engine):
    engine.client._store["balance_sheet"] = engine.client._store["balance_sheet"].drop(columns="total_shares")
    assert engine.ps() is None
    assert "实际股数" in engine.unavailable["PS"]


def test_cross_validation_handles_unknown_weights_and_nan(engine):
    results = [{"method": "A", "intrinsic": 10}, {"method": "B", "intrinsic": 20},
               {"method": "C", "intrinsic": float("nan")}]
    out = engine.cross_validate(results, {"A": 1})
    assert sum(out["active_weights"].values()) == pytest.approx(100)
    assert out["weighted_avg"] == 10
    assert "单一方法" in engine.cross_validate(results[:1], {})["consistency"]


def test_full_engine_runs_without_network(engine):
    output = engine.run()
    assert "DDM" in output and "FCFF" in output and "不适用" in output
