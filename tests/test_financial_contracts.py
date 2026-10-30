import json
from unittest.mock import patch

import pandas as pd
import pytest

from financial_contracts import annual_dividends, depreciation, debt, yuan, statement_issues, decision_metrics
from data_snapshot import write_snapshot, load_snapshot


@pytest.mark.parametrize("value,expected", [("192.03亿", 192.03e8), ("2155.31万", 2155.31e4),
    ("1,234.5元", 1234.5), ("--", None), (float("inf"), None), (None, None)])
def test_yuan_units(value, expected):
    assert yuan(value) == expected


def test_current_interim_and_pending_final_are_not_complete(financial_client):
    divs = financial_client._store["dividends_all"]
    more = pd.DataFrame([{"end_date": "20260630", "cash_div_tax": .98, "div_proc": "实施", "ex_date": "2026-08-01"}])
    divs = pd.concat([divs, more], ignore_index=True)
    divs.loc[0, "div_proc"] = "股东大会通过"
    results = annual_dividends(divs, "2026-10-30")
    assert [r["year"] for r in results] == ["2024", "2023", "2022"]


def test_missing_or_future_exdate_is_unknown(financial_client):
    divs = financial_client._store["dividends_all"]
    divs.loc[0, "ex_date"] = None
    divs.loc[1, "ex_date"] = "2027-01-01"
    assert [r["year"] for r in annual_dividends(divs, "2026-10-30")] == ["2023", "2022"]


def test_same_fy_interim_final_and_duplicate(financial_client):
    divs = financial_client._store["dividends_all"]
    interim = divs.iloc[[0]].copy()
    interim["end_date"] = "20250630"
    interim["cash_div_tax"] = .05
    interim["ex_date"] = "2025-09-01"
    results = annual_dividends(pd.concat([divs, interim, interim]), "2026-10-30")
    assert results[0]["dps"] == pytest.approx(.15)
    assert results[0]["total_yuan"] == pytest.approx(15e6)


def test_explicit_zero_final_is_not_dropped(financial_client):
    divs = financial_client._store["dividends_all"].copy()
    divs.loc[0, "cash_div_tax"] = 0
    divs.loc[0, "ex_date"] = None
    assert annual_dividends(divs, "2026-10-30")[0]["dps"] == 0


def test_missing_debt_and_da_are_not_zero():
    assert debt({"st_borr": 0}) is None
    assert depreciation({"depr_fa_coga_dpba": 100}) is None


def test_version_and_period_alignment(financial_client):
    assert statement_issues(financial_client._store) == []
    for key in ("income", "cashflow", "balance_sheet"):
        financial_client._store[key]["ann_date"] = "2026-03-31"
    financial_client._store["balance_sheet"].loc[0, "ann_date"] = "2026-10-24"
    assert any("20251231" in issue for issue in statement_issues(financial_client._store))
    md = financial_client.compute_derived_metrics("600001.SH")
    assert "暂停" in md and "重述" in md


def test_market_refresh_recomputes_instead_of_retaining_old17(financial_client, monkeypatch):
    old_time = (pd.Timestamp.now() - pd.Timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S")
    old = f"*生成时间: 2020-01-01*\n*财务采集时间: {old_time}*\n## 1. 基本信息\nprice=10\n## 17. 衍生指标\nSTALE_PE=10\n"
    called = []
    def assemble(code):
        called.append(financial_client._refresh_market)
        return financial_client._build_header(code) + "## 1. 基本信息\nprice=20\n## 17. 衍生指标\nFRESH_PE=20\n"
    monkeypatch.setattr(financial_client, "assemble_data_pack", assemble)
    fresh = financial_client.refresh_market_sections("600001.SH", old)
    assert called == [True]
    assert "STALE_PE" not in fresh and "FRESH_PE=20" in fresh
    assert f"财务采集时间: {old_time}" in fresh
    assert not financial_client._refresh_market


def test_financial_timestamp_controls_age():
    from akshare_modules.assembly import AssemblyMixin
    content = f"*生成时间: {pd.Timestamp.now():%Y-%m-%d}*\n*财务采集时间: 2020-01-01*"
    assert AssemblyMixin._check_staleness(content) > 1000


def test_market_refresh_preserves_prior_research_without_exponential_duplication(financial_client, monkeypatch):
    old = "*生成时间: 2026-10-30*\n## 7. 股东与治理\nUSER_RESEARCH\n## 17. 衍生指标\nold\n"
    monkeypatch.setattr(financial_client, "assemble_data_pack", lambda code:
        financial_client._build_header(code) + "## 7. 股东与治理\nNEW_HOLDERS\n## 17. 衍生指标\nnew\n")
    first = financial_client.refresh_market_sections("600001.SH", old)
    second = financial_client.refresh_market_sections("600001.SH", first)
    assert "USER_RESEARCH" in second and "NEW_HOLDERS" in second
    assert second.count("USER_RESEARCH") == 1
    assert second.count("上期研究留存") == 1


def test_rate_limit_actually_sleeps(financial_client, monkeypatch):
    monkeypatch.setenv("AKSHARE_RATE_DELAY", "0.5")
    with patch("akshare_modules.infrastructure.time.sleep") as sleep:
        financial_client._fetch_with_retry(lambda: pd.DataFrame({"a": [1]}))
    sleep.assert_called_once_with(.5)


def test_snapshot_shares_exact_inputs_and_blocks_unverified_buy(financial_client, tmp_path):
    financial_client.compute_derived_metrics("600001.SH")
    write_snapshot(financial_client, tmp_path, "600001.SH")
    other = type(financial_client)(no_cache=True)
    load_snapshot(other, tmp_path / "data_snapshot.json", "600001.SH")
    assert other._store["basic_info"].iloc[0]["close"] == 10
    metrics = json.loads((tmp_path / "decision_metrics.json").read_text())
    assert metrics["status"] == "unverified" and metrics["target_price"] is None
    assert metrics["penetration_pct"] is None
    with pytest.raises(ValueError, match="不匹配"):
        load_snapshot(other, tmp_path / "data_snapshot.json", "600900.SH")


def test_cash_surplus_no_double_counted_wages(financial_client):
    financial_client._store["_true_cash_rev"] = {"2025": 1}
    financial_client._store["_w_total"] = {"2025": 1e99}
    financial_client._compute_factor3_sensitivity_base()
    summary = financial_client._store["factor3_sensitivity"]
    assert summary["aa_selected"] == pytest.approx(19e6)
    assert not summary["decision_eligible"]


def test_missing_capex_cannot_be_cash_surplus(financial_client):
    financial_client._store["cashflow"]["c_pay_acq_const_fiolta"] = float("nan")
    assert "不足" in financial_client._compute_factor3_sensitivity_base()
    assert "factor3_sensitivity" not in financial_client._store


def test_payout_never_uses_interest_distribution(financial_client):
    md = financial_client._compute_payout_crosscheck()
    record = financial_client._store["payout_crosscheck"]
    assert record["m_rec_label"] == "法3"
    assert record["m_rec"] == pytest.approx(45)
    assert record["m2"] == pytest.approx(450)
    assert "不可替代" in md


def test_unknown_payout_not_filled_with_cf(financial_client):
    financial_client._store["dividends_all"] = pd.DataFrame()
    financial_client._store["dividends"] = pd.DataFrame()
    financial_client._compute_payout_crosscheck()
    assert financial_client._store["payout_crosscheck"]["m_rec"] is None


def test_new_assembly_does_not_reuse_previous_stock_on_failure(financial_client, monkeypatch):
    for name in dir(financial_client):
        if name.startswith("get_"):
            monkeypatch.setattr(financial_client, name, lambda code: "数据缺失\n")
    text = financial_client.assemble_data_pack("600900.SH")
    assert "basic_info" not in financial_client._store
    assert "income" not in financial_client._store
    assert "dividends" not in financial_client._store
    assert "暂停" in text


def test_complete_fixture_has_no_silently_swallowed_section17_failure(financial_client):
    text = financial_client.compute_derived_metrics("600001.SH")
    assert "计算失败" not in text
    assert "17.11" in text and "17.12" in text
