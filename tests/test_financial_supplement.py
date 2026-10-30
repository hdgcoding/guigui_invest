import json

import pandas as pd
import pytest

from financial_supplement import apply_supplement
from financial_contracts import statement_issues, decision_metrics
from data_snapshot import write_snapshot, load_snapshot


def _evidence(period):
    return {"end_date": period, "source": "annual_report.pdf", "locator": "p.123",
            "review_status": "source_reconciled", "reconciliation_note": "同一合并范围、已核对原文分项"}


def _payload():
    cash = []
    for period, ocf in (("20251231", 30e6), ("20241231", 28e6)):
        cash.append({**_evidence(period), "scope": "parent_attributable", "ocf_yuan": ocf,
                     "capex_yuan": 10e6, "interest_paid_yuan": 1e6, "interest_classification": "financing",
                     "minority_claim_yuan": 2e6, "restricted_cash_increase_yuan": 0,
                     "nonrecurring_net_inflow_yuan": 0})
    return {"schema_version": 1, "ts_code": "600001.SH", "amount_unit": "yuan", "owner_cashflow": cash,
            "shares": {**_evidence("20251231"), "quote_date": "20261030", "total_shares": 100e6}}


def _write(tmp_path, payload):
    path = tmp_path / "financial_supplement.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return path


def test_source_reconciled_cash_can_restore_verified_buy_inputs(financial_client, tmp_path):
    path = _write(tmp_path, _payload())
    apply_supplement(financial_client, path, "600001.SH")
    financial_client.compute_derived_metrics("600001.SH")
    summary = financial_client._store["factor3_sensitivity"]
    assert summary["decision_eligible"]
    assert summary["aa_selected"] == 16e6  # 19 average FCF - 1 interest - 2 minority
    metrics = decision_metrics(financial_client)
    assert metrics["status"] == "verified" and metrics["target_price"] > 0
    write_snapshot(financial_client, tmp_path, "600001.SH")
    other = type(financial_client)(no_cache=True)
    load_snapshot(other, tmp_path / "data_snapshot.json", "600001.SH")
    assert decision_metrics(other)["target_price"] == pytest.approx(metrics["target_price"])


@pytest.mark.parametrize("missing", ["locator", "source", "reconciliation_note", "interest_classification", "minority_claim_yuan"])
def test_missing_evidence_or_adjustment_rejected_atomically(financial_client, tmp_path, missing):
    payload = _payload()
    del payload["owner_cashflow"][1][missing]
    before = financial_client._store["cashflow"].copy()
    with pytest.raises((ValueError, KeyError)):
        apply_supplement(financial_client, _write(tmp_path, payload), "600001.SH")
    pd.testing.assert_frame_equal(financial_client._store["cashflow"], before)
    assert "owner_cash_records" not in financial_client._store


def test_supplement_cannot_change_base_ocf_silently(financial_client, tmp_path):
    payload = _payload()
    payload["owner_cashflow"][0]["ocf_yuan"] = 1e12
    with pytest.raises(ValueError, match="快照不一致"):
        apply_supplement(financial_client, _write(tmp_path, payload), "600001.SH")


def test_old_share_count_not_accepted_as_current(financial_client, tmp_path):
    payload = _payload()
    payload["shares"]["quote_date"] = "20251030"
    with pytest.raises(ValueError, match="行情日期"):
        apply_supplement(financial_client, _write(tmp_path, payload), "600001.SH")


def test_wrong_company_or_units_rejected(financial_client, tmp_path):
    payload = _payload()
    payload["amount_unit"] = "million_yuan"
    with pytest.raises(ValueError, match="单位不匹配"):
        apply_supplement(financial_client, _write(tmp_path, payload), "600001.SH")


def test_version_issue_requires_explicit_reconciliation(financial_client, tmp_path):
    for key in ("income", "cashflow", "balance_sheet"):
        financial_client._store[key]["ann_date"] = "2026-03-31"
    financial_client._store["balance_sheet"].loc[0, "ann_date"] = "2026-10-24"
    assert statement_issues(financial_client._store)
    payload = _payload()
    payload["statement_reconciliations"] = [{**_evidence("20251231"), "scope": "consolidated", "same_reporting_basis": True}]
    apply_supplement(financial_client, _write(tmp_path, payload), "600001.SH")
    assert statement_issues(financial_client._store) == []


def test_parent_and_audit_sources_stay_separate_from_analysis_audit(financial_client, tmp_path):
    payload = _payload()
    payload["parent_statements"] = [{**_evidence("20251231"), "scope": "parent", "kind": "income_parent", "values": {"revenue": 25e6}}]
    payload["audit"] = {**_evidence("20251231"), "opinion": "标准无保留意见"}
    apply_supplement(financial_client, _write(tmp_path, payload), "600001.SH")
    parent = financial_client._store["income_parent"]
    assert parent.iloc[0]["revenue"] == 25e6
    text = financial_client.get_audit("600001.SH")
    assert "标准无保留意见" in text and "不等于" in text


def test_verified_cash_without_threshold_cannot_make_a_verified_decision(financial_client, tmp_path):
    apply_supplement(financial_client, _write(tmp_path, _payload()), "600001.SH")
    financial_client.compute_derived_metrics("600001.SH")
    financial_client._store.pop("risk_free_rate")
    assert decision_metrics(financial_client)["status"] == "unverified"
    assert decision_metrics(financial_client)["target_price"] is None
