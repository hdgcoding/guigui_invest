#!/usr/bin/env python3
"""Shared disk cache utilities.

Houses ``ScreenerCache`` — a Parquet-based disk cache with TTL — used by both
the screener (Tier 1/2) and the Tushare collector's heavy-endpoint TTL cache.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

import pandas as pd

_PUT_FAILURE_WARNED = False


class ScreenerCache:
    """Parquet-based disk cache with TTL."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, key: str) -> str:
        safe_key = hashlib.md5(key.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{safe_key}.parquet")

    def _meta_path(self, key: str) -> str:
        safe_key = hashlib.md5(key.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{safe_key}.meta")

    def get(self, key: str, ttl_seconds: int) -> pd.DataFrame | None:
        """Return cached DataFrame if within TTL, else None."""
        path = self._path(key)
        meta_path = self._meta_path(key)
        if not os.path.exists(path) or not os.path.exists(meta_path):
            return None
        try:
            with open(meta_path) as f:
                ts = float(f.read().strip().split("\n")[0])
            if time.time() - ts > ttl_seconds:
                return None
            return pd.read_parquet(path)
        except Exception:
            return None

    def put(self, key: str, df: pd.DataFrame) -> None:
        """Store DataFrame to cache; write failures warn once on stderr."""
        global _PUT_FAILURE_WARNED
        path = self._path(key)
        meta_path = self._meta_path(key)
        try:
            df.to_parquet(path, index=False)
            with open(meta_path, "w") as f:
                f.write(f"{time.time()}\n{key}")
        except Exception as exc:  # noqa: BLE001 — cache write failure is non-fatal
            if not _PUT_FAILURE_WARNED:
                _PUT_FAILURE_WARNED = True
                print(f"⚠️ 缓存写入失败（不影响数据采集，后续不再重复提示）: {exc}",
                      file=sys.stderr)

    def clear(self) -> None:
        """Remove all cache entries."""
        if os.path.isdir(self.cache_dir):
            for f in os.listdir(self.cache_dir):
                fp = os.path.join(self.cache_dir, f)
                if os.path.isfile(fp):
                    os.remove(fp)
