"""Persist one collector snapshot so valuation and report gates share inputs."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from financial_contracts import decision_metrics


def write_snapshot(client, directory, ts_code):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    frames = {key: json.loads(value.to_json(orient="records", date_format="iso"))
              for key, value in client._store.items() if isinstance(value, pd.DataFrame)}
    state = {key: client._store[key] for key in ("as_of", "price_basis", "fcff_verified", "fcff_evidence",
             "owner_cash_records", "statement_reconciliations", "audit_evidence", "supplement_path") if key in client._store}
    snapshot = {"schema_version": 1, "ts_code": ts_code,
                "collected_at": pd.Timestamp.now().isoformat(), "frames": frames, "state": state}
    (directory / "data_snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = decision_metrics(client)
    (directory / "decision_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_snapshot(client, path, ts_code):
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    if snapshot.get("schema_version") != 1 or snapshot.get("ts_code") != ts_code:
        raise ValueError("快照版本/股票代码不匹配")
    timestamp = pd.Timestamp(snapshot["collected_at"])
    if pd.Timestamp.now() - timestamp > pd.Timedelta(days=1):
        raise ValueError("快照超过24小时，请先刷新采集器")
    client._store = {key: pd.DataFrame(rows) for key, rows in snapshot["frames"].items()}
    client._store.update(snapshot.get("state", {}))
    client.compute_derived_metrics(ts_code)
    return client
