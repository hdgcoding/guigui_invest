"""guigui_invest - OtherDataMixin (AKShare edition).

Sections: §7 股东与治理 / §9 主营业务构成 / §14 无风险利率 /
§15 股份回购 / §16 股权质押 / 审计意见占位.
"""

from __future__ import annotations

import pandas as pd

from config import split_code
from format_utils import format_table, format_header


class OtherDataMixin:
    """Auxiliary data sections for AKShareClient."""

    # ------------------------------------------------------------------
    # §7 — Top holders (十大股东)
    # ------------------------------------------------------------------

    def get_holders(self, ts_code: str) -> str:
        """Section 7: top-10 holders + governance placeholder."""
        num, prefix = split_code(ts_code)

        def _fetch():
            import akshare as ak
            return ak.stock_main_stock_holder(stock=f"{prefix}{num}")

        df = self._fetch_with_retry(_fetch)
        if df is None:
            df = pd.DataFrame()
        lines = [format_header(2, "7. 股东与治理"), ""]

        if df is None or df.empty:
            lines.append("*十大股东数据缺失*\n")
        else:
            # Keep the most recent 股东大会公告日 snapshot
            date_col = next((c for c in df.columns if "公告日期" in c or "截止" in c), None)
            if date_col:
                latest = df[date_col].dropna().max()
                df = df[df[date_col] == latest]

            name_col = next((c for c in df.columns if "股东名称" in c), None)
            pct_col = next((c for c in df.columns if "持股比例" in c), None)
            if name_col:
                rows = []
                for _, r in df.head(10).iterrows():
                    rows.append([
                        str(r.get(name_col, "—")),
                        f"{r.get(pct_col, '—')}%" if pct_col else "—",
                    ])
                lines.append(format_table(["股东名称", "持股比例"], rows))
                lines.append("")

        lines.append("*[治理结构/股东背景 详情待Agent WebSearch补充]*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §9 — Main business segments (主营构成)
    # ------------------------------------------------------------------

    def get_segments(self, ts_code: str) -> str:
        """Section 9: main business composition from em 主营构成."""
        num, prefix = split_code(ts_code)
        em_symbol = f"{prefix.upper()}{num}"

        def _fetch():
            import akshare as ak
            return ak.stock_zygc_em(symbol=f"{'SH' if prefix == 'sh' else 'SZ'}{num}")

        df = self._fetch_with_retry(_fetch)
        if df is None:
            df = pd.DataFrame()
        lines = [format_header(2, "9. 主营业务构成"), ""]

        if df.empty:
            lines.append("*[§9 主营构成数据缺失, 待Agent WebSearch补充]*\n")
            return "\n".join(lines)

        # df columns: 分类方向, 分类, 主营收入, 收入比例, 主营成本, 毛利率
        if "分类方向" in df.columns:
            by_dir = df[df["分类方向"] == "按行业分"] if (df["分类方向"] == "按行业分").any() else df
        else:
            by_dir = df

        headers = ["分类", "主营收入", "收入比例", "毛利率"]
        rows = []
        for _, r in by_dir.head(15).iterrows():
            rows.append([
                str(r.get("分类", "—")),
                str(r.get("主营收入", "—")),
                str(r.get("收入比例", "—")),
                str(r.get("毛利率", "—")),
            ])
        lines.append(format_table(headers, rows))
        lines.append("")
        lines.append("*来源: AKShare 东财主营构成 (最新报告期)*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §14 — Risk-free rate
    # ------------------------------------------------------------------

    def get_risk_free_rate(self, ts_code: str = "") -> str:
        """Section 14: CN 10-year government bond yield."""
        lines = [format_header(2, "14. 无风险利率"), ""]

        def _fetch():
            import akshare as ak
            start = (pd.Timestamp.now() - pd.DateOffset(months=1)).strftime("%Y%m%d")
            return ak.bond_zh_us_rate(start_date=start)

        df = self._cached_call("risk_free_rate", _fetch)
        if df is None or df.empty or "中国国债收益率10年" not in df.columns:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        df = df.dropna(subset=["中国国债收益率10年"])
        if df.empty:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        # Store in Tushare-compatible shape: trade_date / yield
        store_df = pd.DataFrame({
            "trade_date": pd.to_datetime(df["日期"]).dt.strftime("%Y%m%d"),
            "yield": pd.to_numeric(df["中国国债收益率10年"], errors="coerce"),
        }).dropna().sort_values("trade_date", ascending=False)
        self._store["risk_free_rate"] = store_df

        latest = store_df.iloc[0]
        lines.append(format_table(
            ["日期", "10年期国债收益率 (%)"],
            [[str(latest["trade_date"]), f"{latest['yield']:.4f}"]],
            alignments=["l", "r"],
        ))
        lines.append("")
        lines.append("*数据来源: AKShare 中美国债收益率 (中国10年期)*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §15 — Repurchase
    # ------------------------------------------------------------------

    def get_repurchase(self, ts_code: str) -> str:
        """Section 15: repurchase records filtered by code."""
        num, _ = split_code(ts_code)

        def _fetch():
            import akshare as ak
            return ak.stock_repurchase_em()

        df = self._fetch_with_retry(_fetch)
        if df is None:
            df = pd.DataFrame()
        lines = [format_header(2, "15. 股票回购"), ""]

        if df.empty or "股票代码" not in df.columns:
            lines.append("暂无回购数据或接口不可用\n")
            return "\n".join(lines)

        hits = df[df["股票代码"].astype(str).str.zfill(6) == num]
        if hits.empty:
            lines.append("近年无回购记录\n")
            return "\n".join(lines)

        hits = hits.sort_values("最新公告日期", ascending=False).head(10)
        self._store["repurchase"] = hits

        headers = ["公告日期", "已回购金额(元)", "已回购数量(股)", "实施进度"]
        rows = []
        for _, r in hits.iterrows():
            rows.append([
                str(r.get("最新公告日期", "—"))[:10],
                str(r.get("已回购金额", "—")),
                str(r.get("已回购股份数量", "—")),
                str(r.get("实施进度", "—")),
            ])
        lines.append(format_table(headers, rows, alignments=["l", "r", "r", "l"]))
        lines.append("")
        lines.append("*来源: AKShare 东财回购 (全市场接口过滤)*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §16 — Pledge
    # ------------------------------------------------------------------

    def get_pledge_stat(self, ts_code: str) -> str:
        """Section 16: pledge statistics for the stock."""
        num, _ = split_code(ts_code)

        def _fetch():
            import akshare as ak
            return ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=num)

        df = self._fetch_with_retry(_fetch)
        if df is None:
            df = pd.DataFrame()
        lines = [format_header(2, "16. 股权质押"), ""]

        if df.empty:
            lines.append("无质押记录或接口不可用\n")
            return "\n".join(lines)

        # 质押比例 = Σ(质押股份数量)/总股本 — approximate via 占总股本比例 column
        self._store["pledge"] = df
        if "占总股本比例" in df.columns:
            ratios = pd.to_numeric(df["占总股本比例"], errors="coerce").dropna()
            total_ratio = float(ratios.sum())
        else:
            total_ratio = None

        lines.append(format_table(
            ["指标", "数值"],
            [
                ["质押笔数", f"{len(df):,}"],
                ["质押股份占总股本比例(合计)", f"{total_ratio:.2f}%" if total_ratio is not None else "—"],
                ["未解押笔数", str((df.get("状态", pd.Series()) == "未解押").sum() if "状态" in df.columns else "—")],
            ],
            alignments=["l", "r"],
        ))
        lines.append("")

        # Top 3 pledging shareholders
        show_cols = [c for c in ("股东名称", "质押股份数量", "占总股本比例", "状态") if c in df.columns]
        if show_cols:
            lines.append("**主要质押股东 (前5):**")
            lines.append("")
            rows = [[str(r[c]) for c in show_cols] for _, r in df.head(5).iterrows()]
            lines.append(format_table(show_cols, rows))
        lines.append("")
        lines.append("*来源: AKShare 东财质押 (个股明细)*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Audit opinion (sub-section of §13 pipeline; AKShare 无稳定审计接口)
    # ------------------------------------------------------------------

    def get_audit(self, ts_code: str) -> str:
        """Audit opinion placeholder — best filled by Agent WebSearch."""
        return (format_header(3, "7B. 审计意见")
                + "\n\n*[审计意见待Agent WebSearch补充 (最新年报审计意见类型)]*\n")
