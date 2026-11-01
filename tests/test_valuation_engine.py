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


# ---------------------------------------------------------------------------
# DDM: terminal growth from the formulary, not a hardcoded 0%
# ---------------------------------------------------------------------------

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
    assert result["g_used"] < 0 and result["g_terminal"] == pytest.approx(3.0)
    assert "GDP" in result["g2_basis"]
    assert result["intrinsic"] > 0


def test_ddm_terminal_growth_uses_documented_min(engine):
    result = engine.ddm(8)
    assert result["g2"] == pytest.approx(3.0)  # min(3%, A股长期GDP 4.5%)
    assert result["g_terminal"] == pytest.approx(3.0)
    assert "长期GDP" in result["g2_basis"]
    # run() must pass Ke and use the documented terminal assumption, not 0%
    output = engine.run()
    assert "终值增速" in output and "长期GDP" in output


def test_ddm_gordon_uses_min_of_historical_and_sustainable_growth(engine):
    engine.client._store["dividends_all"]["cash_div_tax"] = [.1, .098, .096, .094]
    result = engine.ddm(8)
    assert result["model_type"] == "Gordon"
    assert result["sustainable_growth"] == pytest.approx(10.2, abs=0.5)  # 20%×(1-49%)
    assert result["g_used"] == pytest.approx(2.08, abs=0.1)  # min(DPS CAGR, ROE×(1-payout))
    assert result["intrinsic"] > 0


@pytest.mark.parametrize("ke,g", [(0, 0), (8, 8), (8, -100)])
def test_ddm_invalid_assumptions_are_ineligible(engine, ke, g):
    assert engine.ddm(ke, g) is None


def test_zero_dividend_disqualifies_stable_ddm(engine):
    engine.client._store["dividends_all"].loc[0, "cash_div_tax"] = 0
    assert engine.ddm(8) is None


# ---------------------------------------------------------------------------
# WACC: shared debt contract, missing debt/market cap is unavailable (not 0)
# ---------------------------------------------------------------------------

def test_wacc_uses_contract_debt_including_lease(engine):
    engine.client._store["balance_sheet"]["lease_liab"] = 100e6
    data = engine.compute_wacc()
    assert data["wacc"] is not None and data["ke"] is not None
    assert data["debt_mm"] == pytest.approx(100.0)
    assert data["d_weight"] > 0
    # 旧自建口径漏 lease_liab → 债务=0 → WACC=Ke；这里必须真正加权债务成本
    assert data["wacc"] != pytest.approx(data["ke"])
    expected = 9.2 * (1000 / 1100) + 3.0 * (1 - 0.2) * (100 / 1100)
    assert data["wacc"] == pytest.approx(expected, abs=1e-3)


def test_wacc_unavailable_without_debt_never_defaults_zero(engine):
    engine.client._store["balance_sheet"] = \
        engine.client._store["balance_sheet"].drop(columns="lease_liab")
    data = engine.compute_wacc()
    assert data["wacc"] is None and data["ke"] is not None
    assert "有息负债" in engine.unavailable["WACC"]
    assert engine.dcf_stable(data) is None
    assert "WACC" in engine.unavailable["DCF"]
    # DDM only needs Ke, so it may still produce a reference value
    assert engine.ddm(data["ke"]) is not None


def test_wacc_unavailable_without_market_cap(engine):
    engine.client._store["basic_info"].loc[0, "total_mv"] = None
    data = engine.compute_wacc()
    assert data["wacc"] is None and data["beta"] is None and data["ke"] is None
    assert "总市值" in engine.unavailable["WACC"]
    assert engine.dcf_stable(data) is None
    assert engine.ddm(data["ke"]) is None


@pytest.mark.parametrize("cap_wan,beta", [(2000e4, 0.8), (500e4, 1.0), (50e4, 1.2)])
def test_beta_market_cap_yi_to_millions_conversion(engine, cap_wan, beta):
    engine.client._store["basic_info"].loc[0, "total_mv"] = cap_wan
    assert engine.compute_wacc()["beta"] == beta


# ---------------------------------------------------------------------------
# DCF: outlier exclusion, growth cap, terminal-value warning
# ---------------------------------------------------------------------------

def test_dcf_requires_identified_fcff(engine):
    assert engine.dcf_stable(engine.compute_wacc()) is None
    assert "FCFF" in engine.unavailable["DCF"]
    assert engine.dcf_scenarios(engine.compute_wacc()) is None


def test_verified_positive_fcff_is_supported(engine):
    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [20e6, 19e6, 18e6, 17e6]
    result = engine.dcf_stable(engine.compute_wacc())
    assert result and result["intrinsic"] > 0
    assert len(result["sensitivity"]) == 5
    assert engine.dcf_scenarios(engine.compute_wacc())["intrinsic"] > 0


def test_dcf_excludes_negative_outlier_year(engine):
    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [20e6, 19e6, -100e6, 18e6]
    result = engine.dcf_stable(engine.compute_wacc())
    # 文档：负 FCF 且偏离三年均值 >50% → 视为异常年份剔除
    assert result["fcf_base"] == pytest.approx(19.5e6)
    assert result["fcf_excluded"] == pytest.approx([-100e6])


def test_dcf_growth_cap_and_terminal_value_warning(engine):
    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [30e6, 10e6, 5e6, 2e6]
    result = engine.dcf_stable(engine.compute_wacc())
    assert result["g_hist"] == pytest.approx(30.0)  # 146.6% → 裁剪到 30%
    assert result["growth_capped"] is True
    assert result["g_conservative"] == pytest.approx(24.0)
    assert result["tv_pct"] > 75 and result["tv_warning"] is True
    output = engine.run()
    assert "裁剪" in output and "终值占比" in output


def test_negative_fcff_does_not_emit_negative_target(engine):
    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [-20e6] * 4
    assert engine.dcf_stable(engine.compute_wacc()) is None
    assert "降级为DCF情景版" in engine.unavailable["DCF"]


def test_fcf_all_negative_downgrades_to_scenarios_in_report(engine):
    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [-20e6] * 4
    output = engine.run()
    # 文档口径：FCF 全负 → 标注现金流风险并降级情景版，而不是静默跳过
    assert "降级" in output


def test_negative_dcf_base_downgrades_to_scenario_and_transfers_weight(engine):
    engine.client._store["fcff_verified"] = True
    # 最新年为正但三年基线为负（-30e6 按 >50% 偏离剔除后仍为负）→ 触发降级
    engine.client._store["cashflow"]["fcff"] = [5e6, -20e6, -30e6, 10e6]
    output = engine.run()
    assert engine.notes.get("DCF_downgrade")
    assert "降级" in output
    assert "DCF_Scenarios" in output
    # 原DCF权重转给情景版：情景版出现在交叉验证表中
    assert "| DCF_Scenarios |" in output


# ---------------------------------------------------------------------------
# PE Band: all positive PE years retained (P0)
# ---------------------------------------------------------------------------

def test_pe_band_keeps_high_pe_years(engine):
    baseline = engine.pe_band()
    assert baseline["pe_count"] == 4
    engine.client._store["weekly_prices"].loc[0, "close"] = 30.0  # 2025 年末 PE=150
    result = engine.pe_band()
    assert result["pe_count"] == 4  # 旧逻辑 `0<pe<100` 会丢弃该年
    assert result["pe_stats"]["max"] == pytest.approx(150.0)
    assert "2025" in result["pe_years"] and "2025" in result["high_pe_years"]
    assert result["pe_stats"]["median"] == pytest.approx(42.5)
    # 旧逻辑 PE 序列=[45,40,35] → median 40 → 参考价 8.0（系统性偏低）
    assert result["intrinsic"] > 40 * 0.2
    output = engine.run()
    assert "PE≥100" in output and "单侧截断" in output


def test_adjusted_price_history_is_rejected(engine):
    engine.client._store["price_basis"] = "qfq"
    assert engine.pe_band() is None
    assert engine.ps() is None


# ---------------------------------------------------------------------------
# PEG: loss years exclude the method
# ---------------------------------------------------------------------------

def test_peg_excludes_loss_years(engine):
    engine.client._store["income"]["n_income_attr_p"] = [20e6, -1e6, 18e6, 22e6]
    assert engine.peg() is None
    assert "亏损" in engine.unavailable["PEG"]


def test_peg_uses_positive_profit_growth(engine):
    engine.client._store["income"]["n_income_attr_p"] = [20e6, 15e6, 12e6, 10e6]
    result = engine.peg()
    assert result and result["g_pct"] == pytest.approx(25.99, abs=0.1)
    assert result["intrinsic"] > 0


# ---------------------------------------------------------------------------
# PS
# ---------------------------------------------------------------------------

def test_historical_ps_uses_historical_share_count(engine):
    result = engine.ps()
    engine.client._store["balance_sheet"].loc[3, "total_shares"] *= 2
    changed = engine.ps()
    assert changed["ps_stats"]["max"] > result["ps_stats"]["max"]


def test_monetary_paid_in_capital_is_not_a_share_count(engine):
    engine.client._store["balance_sheet"] = engine.client._store["balance_sheet"].drop(columns="total_shares")
    assert engine.ps() is None
    assert "实际股数" in engine.unavailable["PS"]


# ---------------------------------------------------------------------------
# Classification: 3yr sample gating, DDM supplement rule
# ---------------------------------------------------------------------------

def test_classification_requires_three_sample_years(engine):
    engine.client._store["income"] = engine.client._store["income"].head(2).copy()
    cls = engine.classify()
    assert cls["blue_score"] == 0 and cls["growth_score"] == 0
    assert cls["type"] == "混合型"
    assert "ROE_5yr_avg" in cls["insufficient"]
    assert cls["checks"]["rev_blue"] is False and cls["checks"]["np_growth"] is False


def test_mixed_type_without_payout_drops_ddm_weight(engine, monkeypatch):
    engine.client._store["income"]["revenue"] = [100e6, 75e6, 55e6, 40e6]
    monkeypatch.setattr(engine.client, "_get_payout_by_year", lambda: {})
    cls = engine.classify()
    assert cls["type"] == "混合型"
    assert cls["payout_avg"] is None
    # 文档：payout > 20% 才追加 DDM；缺失时不得保留 DDM 权重
    assert "DDM" not in cls["methods"]
    assert "DDM" not in cls["weights"]
    assert sum(cls["weights"].values()) == pytest.approx(100.0)


def test_loss_company_forces_growth_and_moves_peg_weight_to_ps(engine):
    engine.client._store["income"]["n_income_attr_p"] = [-1e6, 20e6, 20e6, 20e6]
    cls = engine.classify()
    assert cls["type"] == "成长型" and cls["loss_override"] is True
    assert "PEG" not in cls["methods"]  # 亏损时 PEG 不适用
    assert cls["weights"]["PS"] == pytest.approx(65.0)  # 原 PEG 35% 转给 PS
    assert any("PEG" in note for note in cls["notes"])


# ---------------------------------------------------------------------------
# Cross-validation: failures are reported, weights redistributed
# ---------------------------------------------------------------------------

def test_cross_validation_handles_unknown_weights_and_nan(engine):
    results = [{"method": "A", "intrinsic": 10}, {"method": "B", "intrinsic": 20},
               {"method": "C", "intrinsic": float("nan")}]
    out = engine.cross_validate(results, {"A": 1})
    assert sum(out["active_weights"].values()) == pytest.approx(100)
    assert out["weighted_avg"] == 10
    assert "单一方法" in engine.cross_validate(results[:1], {})["consistency"]


def test_cross_validation_reports_failed_and_dropped_methods(engine):
    engine.unavailable["DDM"] = "计算失败 ValueError: 模拟"
    out = engine.cross_validate([{"method": "PE_Band", "intrinsic": 10.0}],
                                {"DCF": 40, "DDM": 30, "PE_Band": 30})
    assert out["failed"]["DDM"].startswith("计算失败")
    assert "DCF" in out["dropped"]
    assert out["active_weights"]["PE_Band"] == pytest.approx(100.0)


def test_method_exception_recorded_and_weight_redistributed(engine, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("模拟失败")

    monkeypatch.setattr(engine, "ddm", boom)
    output = engine.run()
    assert engine.failed["DDM"].startswith("计算失败")
    assert "计算失败" in output and "权重已重分配" in output
    assert "交叉验证" in output


# ---------------------------------------------------------------------------
# Reverse valuation: documented thresholds, proxy never treated as verified FCFF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("actual_np,label", [
    (20.0, "市场几乎未为增长付费"),
    (12.0, "市场部分认可增长，但有显著折价"),
    (9.0, "市场基本定价了增长"),
    (5.0, "市场给予增长溢价"),
])
def test_reverse_valuation_growth_discount_thresholds(engine, actual_np, label):
    wacc_data = engine.compute_wacc()  # Ke=9.2%, E/P=2% → implied g=7.2%
    rv = engine.reverse_valuation(wacc_data, {"np_cagr_pct": actual_np, "rev_cagr_pct": 10.0})
    assert label in rv["growth_discount_label"]
    assert rv["growth_discount"] == pytest.approx(actual_np - 7.2, abs=1e-6)


def test_reverse_valuation_implied_fcf_requires_verified_fcff(engine):
    wacc_data = engine.compute_wacc()
    proxy = engine.reverse_valuation(wacc_data, {})
    # OCF−Capex 只是合并现金代理，不得反解 WACC/企业价值 DCF 增长
    assert proxy["fcf_basis"] == "consolidated_proxy"
    assert proxy["implied_g_fcf"] is None
    assert "未经来源核对" in proxy["implied_fcf_note"]

    engine.client._store["fcff_verified"] = True
    engine.client._store["cashflow"]["fcff"] = [20e6, 19e6, 18e6, 17e6]
    verified = engine.reverse_valuation(wacc_data, {})
    assert verified["fcf_basis"] == "verified_fcff"
    assert verified["implied_g_fcf"] == pytest.approx(
        wacc_data["wacc"] - verified["fcf_yield"])


def test_full_engine_runs_without_network(engine):
    output = engine.run()
    assert "DDM" in output and "FCFF" in output and "不适用" in output
    assert "交叉验证" in output and "未经来源核对" in output
    # 未验证 FCFF 时不打印反解公式
    assert "WACC − FCF收益率" not in output
