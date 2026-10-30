"""Load explicitly source-reconciled PDF/announcement evidence.

This does not create an audit PASS. It supplies cited facts and cash-flow
adjustments that the network endpoints cannot identify reliably. All amounts
are raw yuan, all periods/scopes explicit; absent fields are never zero-filled.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from financial_contracts import number


def _evidence(record):
    if record.get("review_status") != "source_reconciled":
        raise ValueError("补充记录未声明source_reconciled（不能用猜测填补）")
    if not all(record.get(key) for key in ("source", "locator", "reconciliation_note")):
        raise ValueError("补充记录必须有source、locator（页码/公告位置）、reconciliation_note")
    period = str(record.get("end_date", ""))
    if len(period) != 8 or not period.isdigit():
        raise ValueError("补充记录必须明确YYYYMMDD报告期")
    pd.to_datetime(period, format="%Y%m%d", errors="raise")
    return period


def apply_supplement(client, path, ts_code):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("ts_code") != ts_code or payload.get("amount_unit") != "yuan":
        raise ValueError("补充文件版本/股票代码/金额单位不匹配（必须为yuan）")
    # Validate into a new store first. Bad evidence must not partially mutate
    # a working collector snapshot.
    store = {key: value.copy() if isinstance(value, pd.DataFrame) else value for key, value in client._store.items()}
    reconciled = dict(store.get("statement_reconciliations", {}))
    for record in payload.get("statement_reconciliations", []):
        period = _evidence(record)
        if record.get("scope") != "consolidated" or not record.get("same_reporting_basis"):
            raise ValueError("三表对齐必须确认同一合并/重述范围")
        reconciled[period] = record
    store["statement_reconciliations"] = reconciled

    for record in payload.get("historical_shares", []):
        period = _evidence(record)
        count = number(record.get("total_shares"))
        balance = store.get("balance_sheet", pd.DataFrame()).copy()
        mask = balance["end_date"].astype(str).eq(period) if not balance.empty else pd.Series(dtype=bool)
        if record.get("scope") != "consolidated" or count is None or count <= 0 or not mask.any():
            raise ValueError("历史股数必须为同报表期实际总股数（不能用股本金额代替）")
        balance.loc[mask, "total_shares"] = count
        store["balance_sheet"] = balance

    for record in payload.get("parent_statements", []):
        period = _evidence(record)
        if record.get("scope") != "parent" or record.get("kind") not in ("income_parent", "balance_sheet_parent"):
            raise ValueError("母公司补充的kind/scope不匹配")
        values = {key: number(value) for key, value in record["values"].items()}
        if any(value is None for value in values.values()):
            raise ValueError("母公司补充含非数值/非有限金额")
        values.update(end_date=period, statement_scope="parent", source=record["source"], source_locator=record["locator"])
        old = store.get(record["kind"], pd.DataFrame())
        if not old.empty:
            old = old[old["end_date"] != period]
        store[record["kind"]] = pd.concat([old, pd.DataFrame([values])], ignore_index=True).sort_values("end_date", ascending=False)

    cash = []
    frames = store.get("cashflow", pd.DataFrame())
    for record in payload.get("owner_cashflow", []):
        period = _evidence(record)
        if not period.endswith("1231") or record.get("scope") != "parent_attributable":
            raise ValueError("归母现金补充必须为完整年报、parent_attributable范围")
        fields = ("ocf_yuan", "capex_yuan", "interest_paid_yuan", "minority_claim_yuan",
                  "restricted_cash_increase_yuan", "nonrecurring_net_inflow_yuan")
        values = {key: number(record.get(key)) for key in fields}
        if any(value is None for value in values.values()):
            raise ValueError("归母现金补充缺少明确调整项（零值也必须有来源依据）")
        if any(values[key] < 0 for key in ("capex_yuan", "interest_paid_yuan", "minority_claim_yuan")):
            raise ValueError("资本支出/利息/少数股东现金主张须按正支出填列")
        if record.get("interest_classification") not in ("operating", "financing"):
            raise ValueError("必须说明利息已计入经营现金流还是融资现金流")
        matched = frames[frames["end_date"].astype(str) == period] if not frames.empty else pd.DataFrame()
        if matched.empty:
            raise ValueError(f"{period}: 缺少可核对的基础现金流量表")
        row = matched.iloc[0]
        for key, source in (("ocf_yuan", "n_cashflow_act"), ("capex_yuan", "c_pay_acq_const_fiolta")):
            original = number(row.get(source))
            if original is None or abs(values[key] - abs(original) if key == "capex_yuan" else values[key] - original) > max(1, abs(original) * 1e-8):
                raise ValueError(f"{period}: {key}与同次快照不一致，必须先统一报表版本")
        interest = values["interest_paid_yuan"] if record["interest_classification"] == "financing" else 0
        owner = values["ocf_yuan"] - values["capex_yuan"] - interest - values["minority_claim_yuan"] - values["restricted_cash_increase_yuan"] - values["nonrecurring_net_inflow_yuan"]
        cash.append({**record, "owner_surplus_yuan": owner})
    if cash:
        if len({r["end_date"] for r in cash}) != len(cash):
            raise ValueError("归母现金补充存在重复年度")
        store["owner_cash_records"] = sorted(cash, key=lambda r: r["end_date"], reverse=True)

    fcff_records = payload.get("fcff", [])
    if fcff_records:
        if frames.empty:
            raise ValueError("FCFF补充缺少基础现金流表")
        updated = frames.copy()
        for record in fcff_records:
            period = _evidence(record)
            if record.get("scope") != "consolidated" or not all(record.get(k) for k in
                ("interest_treatment", "tax_treatment", "working_capital_treatment", "minority_treatment")):
                raise ValueError("FCFF必须说明利息/税盾/营运资本/少数股东处理")
            value = number(record.get("fcff_yuan"))
            mask = updated["end_date"].astype(str).eq(period)
            if value is None or not mask.any():
                raise ValueError("FCFF金额/期间无效")
            updated.loc[mask, "fcff"] = value
        store["cashflow"] = updated
        store["fcff_verified"] = True
        store["fcff_evidence"] = fcff_records

    shares = payload.get("shares")
    if shares:
        _evidence(shares)
        basic = store.get("basic_info", pd.DataFrame()).copy()
        count = number(shares.get("total_shares"))
        if basic.empty or count is None or count <= 0 or str(shares.get("quote_date")) != str(basic.iloc[0].get("trade_date")):
            raise ValueError("股本必须明确对应行情日期的实际总股数")
        basic.loc[basic.index[0], "total_share"] = count / 1e4
        basic.loc[basic.index[0], "total_mv"] = count * float(basic.iloc[0]["close"]) / 1e4
        basic["shares_verified"] = True
        basic["shares_basis"] = shares["source"] + " " + shares["locator"]
        store["basic_info"] = basic

    audit = payload.get("audit")
    if audit:
        _evidence(audit)
        if not audit.get("opinion"):
            raise ValueError("审计意见补充缺少opinion")
        store["audit_evidence"] = audit
    store["supplement_path"] = str(path)
    client._store = store
