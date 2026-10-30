"""Shared test fixtures for guigui_invest (AKShare edition)."""

import os
import sys

import pytest

# Add scripts/ to path so we can import from there
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixtures_dir():
    """Path to the test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def sample_stock_code():
    """Standard test stock code (Yili 伊利股份)."""
    return "600887.SH"


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Temporary output directory for test file generation."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return output_dir


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Disable AKShare rate-limit sleeps in tests."""
    monkeypatch.setenv("AKSHARE_RATE_DELAY", "0")


# Mark integration tests that require live network
integration = pytest.mark.skipif(
    os.environ.get("GUILGUI_INTEGRATION", "") != "1",
    reason="integration tests opt-in only (set GUILGUI_INTEGRATION=1)",
)


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: mark test as requiring live network access")


@pytest.fixture(autouse=True)
def _isolate_env_file(monkeypatch, tmp_path):
    """Prevent _load_env_file from finding the real .env during tests."""
    import config as config_mod

    monkeypatch.setattr(config_mod, "__file__", str(tmp_path / "scripts" / "config.py"))


@pytest.fixture
def financial_client():
    """Complete small financial fixture; amounts are raw yuan, dates fixed."""
    import pandas as pd
    from akshare_collector import AKShareClient
    from financial_contracts import DEBT_FIELDS, DA_FIELDS
    c = AKShareClient(no_cache=True)
    periods = [f"{year}1231" for year in (2025, 2024, 2023, 2022)]
    c._store = {
        "as_of": "2026-10-30", "price_basis": "unadjusted",
        "income": pd.DataFrame({"end_date": periods, "revenue": [100e6, 95e6, 90e6, 85e6],
             "oper_cost": [60e6]*4, "n_income_attr_p": [20e6]*4, "n_income": [20e6]*4,
             "basic_eps": [0.2]*4, "operate_profit": [25e6]*4, "finance_exp": [-2e6]*4,
             "total_profit": [25e6]*4, "income_tax": [5e6]*4}),
        "balance_sheet": pd.DataFrame({"end_date": periods, "total_assets": [150e6]*4,
             "total_liab": [50e6]*4, "total_hldr_eqy_exc_min_int": [100e6]*4,
             "money_cap": [30e6]*4, "trad_asset": [0.0]*4, "goodwill": [0.0]*4,
             "minority_int": [0.0]*4, "total_share_capital": [100e6]*4, "total_shares": [100e6]*4,
             **{key: [0.0]*4 for key in DEBT_FIELDS}}),
        "cashflow": pd.DataFrame({"end_date": periods, "n_cashflow_act": [30e6, 28e6, 26e6, 24e6],
             "c_pay_acq_const_fiolta": [10e6]*4, "c_pay_dist_dpcp_int_exp": [90e6]*4,
             "c_recp_sale_goods": [100e6]*4, **{key: [1e6]*4 for key in DA_FIELDS}}),
        "dividends": pd.DataFrame({"end_date": periods, "cash_div_tax": [.1, .09, .08, .07],
             "base_share": [10000.0]*4, "div_proc": ["实施"]*4,
             "ex_date": [f"{year}-06-10" for year in (2026, 2025, 2024, 2023)]}),
        "basic_info": pd.DataFrame([{"ts_code": "600001.SH", "name": "测试公司", "trade_date": "20261030",
             "close": 10.0, "total_mv": 100000.0, "total_share": 10000.0, "pe_ttm": 50.0}]),
        "risk_free_rate": pd.DataFrame([{"yield": 2.0}]),
        "weekly_prices": pd.DataFrame({"trade_date": ["20251226", "20241227", "20231229", "20221230"],
             "close": [10., 9., 8., 7.], "high": [11., 10., 9., 8.], "low": [9., 8., 7., 6.]})}
    c._store["dividends_all"] = c._store["dividends"].copy()
    return c
