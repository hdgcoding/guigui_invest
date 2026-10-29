#!/usr/bin/env python3
"""guigui_invest — AKShare Data Collector (Phase 1A).

Facade module: defines AKShareClient which combines the mixin classes in
akshare_modules/. Collects financial data via AKShare (free, no token) and
outputs a structured data_pack_market.md (§1–§17) with the same contract
as the original Tushare-based framework.

Usage:
    python3 scripts/akshare_collector.py --code 600887.SH
    python3 scripts/akshare_collector.py --code 600887 --output output/data_pack.md
    python3 scripts/akshare_collector.py --code 600887 --refresh-market
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import validate_stock_code, get_stock_name  # noqa: E402

from akshare_modules.infrastructure import InfrastructureMixin  # noqa: E402
from akshare_modules.financials import FinancialsMixin  # noqa: E402
from akshare_modules.other_data import OtherDataMixin  # noqa: E402
from akshare_modules.derived_metrics import DerivedMetricsMixin  # noqa: E402
from akshare_modules.assembly import AssemblyMixin  # noqa: E402


class AKShareClient(InfrastructureMixin, FinancialsMixin, OtherDataMixin,
                    DerivedMetricsMixin, AssemblyMixin):
    """AKShare-backed data collector with Tushare-compatible store contract."""

    def __init__(self, no_cache: bool = False):
        self.__init_store__()
        self._no_cache = no_cache


def resolve_output_path(code_arg: str, output: str | None) -> tuple[str, str, str]:
    """Resolve (ts_code, company_name, output_path).

    Prefers an existing output/{num}_* directory so refresh-market always
    targets the same folder even if the name lookup flakes.
    """
    ts_code = validate_stock_code(code_arg)
    num = ts_code.split(".")[0]

    if output:
        return ts_code, "", output

    root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

    # Reuse existing per-stock directory if present
    import glob as _glob
    existing = sorted(_glob.glob(os.path.join(root, "output", f"{num}_*")))
    existing = [d for d in existing if os.path.isdir(d)]
    if existing:
        out_dir = existing[0]
        name = os.path.basename(out_dir).split("_", 1)[1] if "_" in os.path.basename(out_dir) else ""
        return ts_code, name, os.path.join(out_dir, "data_pack_market.md")

    name = get_stock_name(num) or ""
    if name:
        out_dir = os.path.join(root, "output", f"{num}_{name}")
    else:
        out_dir = os.path.join(root, "output", num)
    os.makedirs(out_dir, exist_ok=True)
    return ts_code, name, os.path.join(out_dir, "data_pack_market.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="guigui_invest AKShare data collector")
    parser.add_argument("--code", required=True, help="Stock code, e.g. 600887 or 600887.SH")
    parser.add_argument("--output", default=None, help="Output markdown path")
    parser.add_argument("--no-cache", action="store_true", help="Bypass TTL cache")
    parser.add_argument("--cache-refresh", action="store_true", help="Invalidate all cached data")
    parser.add_argument("--refresh-market", action="store_true",
                        help="Only refresh §1/§2/§11/§14 of an existing data pack")
    parser.add_argument("--dry-run", action="store_true", help="Validate code and exit")
    args = parser.parse_args()

    try:
        ts_code = validate_stock_code(args.code)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    if args.dry_run:
        print(f"✓ code valid: {ts_code}")
        return 0

    ts_code, name, output_path = resolve_output_path(args.code, args.output)

    client = AKShareClient(no_cache=args.no_cache)

    if args.cache_refresh:
        client._cache.clear()
        print("✓ cache cleared")

    print(f"=== guigui_invest 数据采集 — {ts_code} {name} ===")

    if args.refresh_market:
        if not os.path.exists(output_path):
            print(f"⚠️ 未找到已有数据包 {output_path}，执行完整采集")
        else:
            with open(output_path, encoding="utf-8") as f:
                existing = f.read()
            age_days = AssemblyMixin._check_staleness(existing)
            if age_days > 7:
                print(f"⚠️ 数据包已 {age_days} 天未更新，执行完整采集")
            else:
                content = client.refresh_market_sections(ts_code, existing)
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"✓ 市场数据已刷新: {output_path}")
                return 0

    content = client.assemble_data_pack(ts_code)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✓ 数据包已生成: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
