"""Shared financial conventions. Raw statement amounts are yuan, never millions.

Missing is not zero. Dividend fiscal years are usable only after a final plan
has been implemented (including an explicitly implemented zero-dividend plan).
"""
from __future__ import annotations

import math
import re

import pandas as pd

DEBT_FIELDS = ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y", "lease_liab")
DA_FIELDS = ("depr_fa_coga_dpba", "amort_intang_assets", "lt_amort_deferred_exp")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def yuan(value):
    """Parse THS's yuan/万/亿 strings; do not silently treat missing as zero."""
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        match = re.fullmatch(r"([+-]?[\d.]+)\s*(亿|万)?(?:元)?", value)
        if not match:
            return None
        return number(match[1]) * {None: 1, "万": 1e4, "亿": 1e8}[match[2]]
    return number(value)


def strict_sum(row, fields):
    values = [number(row.get(field)) for field in fields]
    return sum(values) if all(v is not None for v in values) else None


def debt(row):
    return strict_sum(row, DEBT_FIELDS)


def depreciation(row):
    return strict_sum(row, DA_FIELDS)


def annual_dividends(plans, as_of=None):
    """Return complete FY records, newest first, retaining zero DPS and metadata.

    A prior calendar year is NOT automatically complete. A pending final plan
    or missing ex-date excludes it. Never infer dividends from the cash-flow
    distribution line, which also contains interest/minority distributions.
    """
    if plans is None or plans.empty:
        return []
    now = pd.Timestamp(as_of if as_of is not None else pd.Timestamp.now()).normalize()
    frame = plans.copy()
    frame["_year"] = frame["end_date"].astype(str).str[:4]
    results = []
    for year, rows in frame.groupby("_year"):
        if not re.fullmatch(r"\d{4}", year) or int(year) >= now.year:
            continue
        rows = rows.drop_duplicates(subset=[c for c in ("end_date", "ex_date", "cash_div_tax", "div_proc") if c in rows])
        final = rows[rows["end_date"].astype(str).str.endswith("1231")]
        if final.empty:
            continue
        dates = pd.to_datetime(rows.get("ex_date", pd.Series(pd.NaT, index=rows.index)), errors="coerce")
        dps = pd.to_numeric(rows["cash_div_tax"], errors="coerce")
        implemented = rows["div_proc"].astype(str).eq("实施")
        # An explicit zero final plan needs no ex-date, but must be implemented.
        known = implemented & dps.notna() & (((dates.notna()) & (dates <= now)) | dps.eq(0))
        if not known.all():
            continue
        payments = []
        for (_, row), amount in zip(rows.iterrows(), dps):
            shares = number(row.get("base_share"))
            payments.append(amount * shares * 1e4 if shares is not None and shares > 0 else (0 if amount == 0 else None))
        results.append({"year": year, "dps": float(dps.sum()),
                        "total_yuan": sum(payments) if all(v is not None for v in payments) else None,
                        "completed_at": dates.max().strftime("%Y-%m-%d") if dates.notna().any() else None})
    return sorted(results, key=lambda item: item["year"], reverse=True)


def statement_issues(store):
    """Detect incompatible periods/versions. A mismatch blocks combined models.

    Different announcement dates alone need not be a restatement. A >45-day
    spread for the same annual period requires source reconciliation first.
    """
    frames = {key: store.get(key) for key in ("income", "balance_sheet", "cashflow")}
    issues = []
    latest = {}
    announcements = {}
    for key, frame in frames.items():
        if frame is None or frame.empty:
            issues.append(f"{key}: 报表缺失")
            continue
        annual = frame[frame["end_date"].astype(str).str.endswith("1231")]
        if annual.empty:
            issues.append(f"{key}: 年报缺失")
            continue
        latest[key] = str(annual.iloc[0]["end_date"])
        for _, row in annual.iterrows():
            date = pd.to_datetime(row.get("ann_date"), errors="coerce")
            if pd.notna(date):
                announcements.setdefault(str(row["end_date"]), []).append(date)
    if len(set(latest.values())) > 1:
        issues.append(f"三表最新年报期间不一致: {latest}")
    for period, dates in announcements.items():
        resolved = store.get("statement_reconciliations", {}).get(period)
        if len(dates) >= 2 and (max(dates) - min(dates)).days > 45 and not resolved:
            issues.append(f"{period}: 三表公告版本相差超过45天，须核对重述/合并范围")
    return issues


def decision_metrics(client):
    """Machine-readable evidence for the first-screen report card.

    A blocked model has no target price. Missing data must never be converted
    into an unconditional buy/hold/sell instruction by a prose generator.
    """
    store = client._store
    basic = store.get("basic_info", pd.DataFrame())
    row = basic.iloc[0] if not basic.empty else {}
    summary = store.get("factor3_sensitivity", {})
    payout = store.get("payout_crosscheck", {})
    cap = number(row.get("total_mv"))
    close = number(row.get("close"))
    m = number(payout.get("m_rec"))
    aa = number(summary.get("aa_selected"))
    issues = statement_issues(store)
    proxy = aa * (m / 100) / (cap * 1e4) * 100 if aa is not None and m is not None and cap and cap > 0 else None
    shares_verified = str(row.get("shares_verified", False)).lower() == "true"
    eligible = bool(summary.get("decision_eligible")) and shares_verified and not issues and close is not None and proxy is not None
    _, threshold = client._get_rf_ii(str(row.get("ts_code", "")))
    eligible = eligible and payout.get("m_rec_label") == "法3" and threshold is not None and threshold > 0
    # Default A-share holding-period tax assumption from §17.11.
    _, tax = client._unique_tax_rates(str(row.get("ts_code", "")))
    after_tax = proxy * (1 - tax / 100) if proxy is not None else None
    target = close * after_tax / threshold if eligible and threshold and after_tax > 0 else None
    return {"schema_version": 1, "status": "verified" if eligible else "unverified",
            "as_of": row.get("trade_date"), "price": close, "payout_pct": m,
            "aa_proxy_yuan": aa, "penetration_proxy_pct": after_tax,
            "penetration_pct": after_tax if eligible else None,
            "threshold_pct": threshold, "target_price": target,
            "drawdown_pct": (1 - target / close) * 100 if target and close else None,
            "issues": issues + ([] if eligible else ["归母现金/实际股本穿透未完成，不生成买点"]) +
                      (["现金结余非正，无正买点"] if eligible and after_tax <= 0 else [])}
