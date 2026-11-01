"""Persist one collector snapshot so valuation and report gates share inputs.

Integrity: every stored DataFrame is hashed (SHA256 over canonical JSON) at
write time and re-verified at load time, so a hand-edited snapshot cannot
silently feed the valuation/decision gates.

Freshness: a snapshot older than 7 days is a hard error; older than 1 day it
still loads but the age is reported (warning + ``client._snapshot_age_days``).
This matches AGENTS.md #11: financials TTL is 7 days, market data 24h.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from financial_contracts import decision_metrics

SCHEMA_VERSION = 2  # v2: mandatory per-frame SHA256; v1 snapshots must be re-collected
MAX_AGE_DAYS = 7
WARN_AGE_DAYS = 1
STATE_KEYS = ("as_of", "price_basis", "fcff_verified", "fcff_evidence",
              "owner_cash_records", "statement_reconciliations", "audit_evidence",
              "supplement_path")


def _canonical_json(payload) -> str:
    """Stable JSON representation used for hashing (order/whitespace independent)."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _frame_sha256(rows) -> str:
    return hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()


def _market_as_of(client):
    """as_of for dividend-completeness checks = 行情 trade_date of this snapshot."""
    basic = client._store.get("basic_info")
    if isinstance(basic, pd.DataFrame) and not basic.empty:
        value = basic.iloc[0].get("trade_date")
        text = "" if value is None else str(value).strip()
        if text and text.lower() not in {"nan", "nat", "none"}:
            return text
    return None


def write_snapshot(client, directory, ts_code):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    as_of = _market_as_of(client)
    if as_of:
        client._store["as_of"] = as_of
    frames = {key: json.loads(value.to_json(orient="records", date_format="iso"))
              for key, value in client._store.items() if isinstance(value, pd.DataFrame)}
    frame_sha256 = {key: _frame_sha256(rows) for key, rows in frames.items()}
    state = {key: client._store[key] for key in STATE_KEYS if key in client._store}
    snapshot = {"schema_version": SCHEMA_VERSION, "ts_code": ts_code,
                "collected_at": pd.Timestamp.now().isoformat(),
                "frames": frames, "frame_sha256": frame_sha256, "state": state}
    (directory / "data_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = decision_metrics(client)
    (directory / "decision_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _verify_frames(frames, hashes) -> None:
    if not isinstance(frames, dict) or not isinstance(hashes, dict) or not hashes:
        raise ValueError("快照缺少 frames 哈希校验字段（旧版本或文件损坏），请重新采集")
    missing = sorted(key for key in frames if key not in hashes)
    if missing:
        raise ValueError(f"快照 frames 缺少 SHA256 校验值: {', '.join(missing)}，请重新采集")
    mismatched = sorted(key for key, rows in frames.items()
                        if hashes.get(key) != _frame_sha256(rows))
    if mismatched:
        raise ValueError(f"快照校验失败：frames SHA256 不匹配（{', '.join(mismatched)}），"
                         "文件可能被手动修改，请重新采集")


def load_snapshot(client, path, ts_code):
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    if snapshot.get("schema_version") != SCHEMA_VERSION or snapshot.get("ts_code") != ts_code:
        raise ValueError("快照版本/股票代码不匹配")
    frames = snapshot.get("frames")
    _verify_frames(frames, snapshot.get("frame_sha256"))

    try:
        timestamp = pd.Timestamp(snapshot["collected_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"快照 collected_at 无法解析: {snapshot.get('collected_at')!r}") from exc
    age_days = max(0.0, (pd.Timestamp.now() - timestamp).total_seconds() / 86400)
    if age_days > MAX_AGE_DAYS:
        raise ValueError(f"快照已 {age_days:.1f} 天，超过 {MAX_AGE_DAYS} 天硬过期阈值，"
                         "请先刷新采集器（--refresh-market 或重跑完整采集）")

    client._store = {key: pd.DataFrame(rows) for key, rows in frames.items()}
    client._store.update(snapshot.get("state", {}))
    if not client._store.get("as_of"):
        as_of = _market_as_of(client)
        if as_of:
            client._store["as_of"] = as_of

    client._snapshot_age_days = round(age_days, 2)
    client._snapshot_warnings = []
    if age_days > WARN_AGE_DAYS:
        warning = (f"快照已 {age_days:.1f} 天（> {WARN_AGE_DAYS} 天）：行情可能滞后，"
                   "继续加载但报告中必须标注数据年龄")
        client._snapshot_warnings.append(warning)
        print(f"⚠️ {warning}")

    client.compute_derived_metrics(ts_code)
    return client
