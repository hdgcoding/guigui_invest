"""guigui_invest - OtherDataMixin (AKShare edition).

Sections: §7 股东与治理 / §9 主营业务构成 / §14 无风险利率 /
§15 股份回购 / §16 股权质押 / 审计意见占位.
"""

from __future__ import annotations

import pandas as pd

from config import split_code
from format_utils import format_number, format_table, format_header


def _percent_series(series: pd.Series) -> pd.Series:
    """Normalise a ratio column to percentage points (45.0 means 45%).

    Eastmoney sometimes returns fractions (0.45) and sometimes percentage
    points (45.0) for the same column; decide once per column via the largest
    absolute value (segment shares/sums make the two ranges unambiguous).
    """
    values = pd.to_numeric(series, errors="coerce")
    finite = values.dropna()
    if not finite.empty and float(finite.abs().max()) <= 1.5:
        values = values * 100
    return values


def unreleased_pledges(df: pd.DataFrame, as_of: str | None = None) -> pd.DataFrame:
    """Return only pledge events that are still unreleased.

    A pledge counts as outstanding when ``状态 == "未解押"`` or its 结束日期 is
    empty / not in the past. Explicitly released rows never count, even when
    their end date is missing. When neither column exists the status cannot be
    determined, so an empty frame is returned instead of treating every event
    as outstanding (missing is not zero and is not "still pledged").
    """
    if df is None or df.empty:
        return pd.DataFrame()
    if "状态" not in df.columns and "质押结束日期" not in df.columns:
        return df.iloc[0:0]

    today = pd.Timestamp(as_of if as_of is not None else pd.Timestamp.now()).normalize()
    status = (df["状态"].astype(str).str.strip()
              if "状态" in df.columns else pd.Series("", index=df.index))
    end = (pd.to_datetime(df["质押结束日期"], errors="coerce")
           if "质押结束日期" in df.columns else pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]"))
    still_out = status.eq("未解押") | end.isna() | (end >= today)
    released_explicit = status.eq("已解押")
    return df[still_out & ~released_explicit]


def _pledge_cell(value, column: str) -> str:
    """Render one pledge-detail cell without leaking NaT/nan into the pack."""
    if value is None:
        return "—"
    try:
        missing = bool(pd.isna(value))
    except (TypeError, ValueError):
        missing = False
    if missing:
        return "—"
    if column in ("质押开始日期", "质押结束日期", "公告日期"):
        ts = pd.to_datetime(value, errors="coerce")
        return "—" if pd.isna(ts) else ts.strftime("%Y-%m-%d")
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if column in ("质押股份数量",):
        return f"{num:,.0f}"
    if column in ("占总股本比例", "占所持股份比例"):
        return f"{num:.2f}%"
    return str(value)


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
        """Section 9: main business composition from em 主营构成.

        akshare >= 1.20 returns ``报告日期 / 分类类型 / 主营构成 / 主营收入 /
        收入比例 / 主营成本 / 毛利率`` with several reporting periods mixed
        together. Keep the newest period and the 按行业分类 dimension only.
        """
        num, prefix = split_code(ts_code)

        def _fetch():
            import akshare as ak
            return ak.stock_zygc_em(symbol=f"{prefix.upper()}{num}")

        df = self._fetch_with_retry(_fetch)
        lines = [format_header(2, "9. 主营业务构成"), ""]

        if df is None or df.empty:
            lines.append("*[§9 主营构成数据缺失, 待Agent WebSearch补充]*\n")
            return "\n".join(lines)

        def _pick(*names):
            return next((name for name in names if name in df.columns), None)

        # New (>=1.20.21) and legacy column names.
        date_col = _pick("报告日期", "REPORT_DATE")
        type_col = _pick("分类类型", "分类方向")
        item_col = _pick("主营构成", "分类")
        revenue_col = _pick("主营收入")
        cost_col = _pick("主营成本")
        ratio_col = _pick("收入比例")
        gross_col = _pick("毛利率")

        missing_cols = [label for label, col in (
            ("报告日期", date_col), ("分类类型", type_col), ("主营构成", item_col),
            ("主营收入", revenue_col), ("收入比例", ratio_col), ("毛利率", gross_col),
        ) if col is None]
        if missing_cols:
            lines.append("⚠️ [字段映射缺失] 主营构成缺少列：" + "、".join(missing_cols)
                         + "；接口实际列：" + "、".join(str(c) for c in df.columns))
            lines.append("")
        if item_col is None or revenue_col is None:
            lines.append("*[§9 主营构成数据缺失, 待Agent WebSearch补充]*\n")
            return "\n".join(lines)

        work = df.copy()
        period_label = "最新报告期未标注"
        if date_col is not None:
            dates = pd.to_datetime(work[date_col], errors="coerce")
            if dates.notna().any():
                latest = dates.max()
                work = work[dates == latest]
                period_label = latest.strftime("%Y-%m-%d")
            else:
                lines.append("⚠️ [字段映射缺失] 报告日期全部无法解析，无法确认最新一期；已回退全部行。")
                lines.append("")

        selected_type = None
        if type_col is not None:
            types = work[type_col].astype(str).str.strip()
            for candidate in ("按行业分类", "按行业分"):
                if types.eq(candidate).any():
                    selected_type = candidate
                    break
            if selected_type is None:
                lines.append(f"⚠️ [字段映射缺失] 报告期 {period_label} 无「按行业分类」明细；"
                             "已保留全部维度，须在报告中标注口径。")
                lines.append("")
            else:
                work = work[types == selected_type]
        else:
            lines.append("⚠️ [字段映射缺失] 缺少分类类型列，「按行业分类」无法筛选；已保留全部行。")
            lines.append("")

        if work.empty:
            lines.append("⚠️ [字段映射缺失] 最新报告期筛选后无主营构成行。")
            lines.append("")
            lines.append("*[§9 主营构成数据缺失, 待Agent WebSearch补充]*")
            return "\n".join(lines)

        revenue = pd.to_numeric(work[revenue_col], errors="coerce")
        cost = (pd.to_numeric(work[cost_col], errors="coerce") if cost_col is not None
                else pd.Series(float("nan"), index=work.index))
        ratio = (_percent_series(work[ratio_col]) if ratio_col is not None
                 else pd.Series(float("nan"), index=work.index))
        gross = (_percent_series(work[gross_col]) if gross_col is not None
                 else pd.Series(float("nan"), index=work.index))

        out = pd.DataFrame({
            "分类": work[item_col].astype(str),
            "主营收入(元)": revenue,
            "主营成本(元)": cost,
            "收入比例(%)": ratio,
            "毛利率(%)": gross,
        }).sort_values("主营收入(元)", ascending=False, na_position="last")
        self._store["segments"] = out
        self._store["segments_period"] = period_label

        headers = ["分类", "主营收入(百万元)", "收入比例", "毛利率"]
        rows = []
        for _, r in out.head(15).iterrows():
            rows.append([
                str(r["分类"]),
                self._fmt_segment_amount(r["主营收入(元)"]),
                self._fmt_segment_pct(r["收入比例(%)"]),
                self._fmt_segment_pct(r["毛利率(%)"]),
            ])
        lines.append(format_table(headers, rows, alignments=["l", "r", "r", "r"]))
        lines.append("")

        # Missing values must be visible, never a silent "—".
        missing_cells = []
        for label, series in (("主营收入", revenue), ("收入比例", ratio), ("毛利率", gross)):
            count = int(series.isna().sum())
            if count:
                missing_cells.append(f"{label} {count} 行")
        if missing_cells:
            lines.append("⚠️ [字段映射缺失] 下列数值为空，已按缺失显示，不得当作 0："
                         + "、".join(missing_cells))
            lines.append("")

        type_note = f"；分类类型 {selected_type}" if selected_type else "；分类类型未确认"
        lines.append(f"*来源: AKShare 东财主营构成；报告期 {period_label}{type_note}"
                     "；金额百万元（原始元÷1e6），收入比例/毛利率带%。*")
        return "\n".join(lines)

    def _fmt_segment_amount(self, val) -> str:
        f = self._safe_float(val)
        return format_number(f) if f is not None else "—[缺失]"

    def _fmt_segment_pct(self, val) -> str:
        f = self._safe_float(val)
        return f"{f:.2f}%" if f is not None else "—[缺失]"


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
        """Section 16: pledge statistics — outstanding (未解押) events only."""
        num, _ = split_code(ts_code)

        def _fetch():
            import akshare as ak
            return ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=num)

        df = self._fetch_with_retry(_fetch)
        lines = [format_header(2, "16. 股权质押"), ""]

        if df is None:
            # Endpoint failure is NOT "no pledges": fail closed and make the
            # downstream report/LLM see an unverified state instead of a pass.
            self._store["pledge_unverified"] = True
            lines.append("⚠️ [数据未核实] 质押明细接口调用失败，**不等于无质押**；"
                         "不得据此判定无质押风险，需 WebSearch/公告或再次采集补充。\n")
            return "\n".join(lines)
        if df.empty:
            self._store["pledge_unverified"] = False
            lines.append("提示：接口成功返回空表（本次未取得质押记录）；空表不等于全市场无质押，重大事项仍需公告复核。\n")
            return "\n".join(lines)

        self._store["pledge"] = df
        has_status = "状态" in df.columns
        has_end = "质押结束日期" in df.columns
        unreleased = unreleased_pledges(df)
        self._store["pledge_unreleased"] = unreleased

        if not has_status and not has_end:
            lines.append("⚠️ [字段映射缺失] 缺少「状态」与「质押结束日期」，无法区分是否解押；"
                         "以下仅为全部事件汇总，不构成未解押质押风险口径。")
            lines.append("")
        elif unreleased.empty:
            lines.append("提示：明细中无未解押质押事件（全部事件均已解押）。")
            lines.append("")

        rows = [["质押事件笔数(全部)", f"{len(df):,}"]]
        if has_status or has_end:
            rows.append(["未解押笔数", f"{len(unreleased):,}"])
        else:
            rows.append(["未解押笔数", "—[缺失]"])

        ratio_missing = "占总股本比例" not in df.columns
        if not ratio_missing:
            if has_status or has_end:
                if unreleased.empty:
                    # Status is known and nothing is outstanding → genuinely 0%.
                    total_ratio = 0.0
                else:
                    unrel_ratios = pd.to_numeric(unreleased["占总股本比例"], errors="coerce")
                    total_ratio = (float(unrel_ratios.sum())
                                   if unrel_ratios.notna().any() else None)
                label = "未解押质押股份占总股本比例(合计)"
            else:
                all_ratios = pd.to_numeric(df["占总股本比例"], errors="coerce")
                total_ratio = float(all_ratios.sum()) if all_ratios.notna().any() else None
                label = "质押股份占总股本比例(全部事件合计，口径不可用)"
            rows.append([label, f"{total_ratio:.2f}%" if total_ratio is not None else "—[缺失]"])
        else:
            lines.append("⚠️ [字段映射缺失] 缺少「占总股本比例」，不输出质押比例合计（缺列≠0）。")
            lines.append("")

        lines.append(format_table(["指标", "数值"], rows, alignments=["l", "r"]))
        lines.append("")

        if not unreleased.empty and "股东名称" in unreleased.columns:
            ratio_based = "占总股本比例" in unreleased.columns
            if ratio_based:
                group = unreleased.copy()
                group["_ratio"] = pd.to_numeric(group["占总股本比例"], errors="coerce")
                headers = ["股东名称", "未解押笔数", "占总股本比例(合计)"]
                suffix = "%"
            elif "质押股份数量" in unreleased.columns:
                group = unreleased.copy()
                group["_ratio"] = pd.to_numeric(group["质押股份数量"], errors="coerce")
                headers = ["股东名称", "未解押笔数", "质押股份数量(合计)"]
                suffix = ""
            else:
                group = None
                headers = None
                suffix = ""
            if group is not None and group["_ratio"].notna().any():
                grouped = (group.groupby(group["股东名称"].astype(str), dropna=False)
                           .agg(未解押笔数=("_ratio", "size"), 合计=("_ratio", "sum"))
                           .sort_values("合计", ascending=False).head(5).reset_index())
                rows2 = [[str(r["股东名称"]), str(int(r["未解押笔数"])), f"{r['合计']:.2f}{suffix}"]
                         for _, r in grouped.iterrows()]
                lines.append("**未解押质押主要股东 (按合计值前5):**")
                lines.append("")
                lines.append(format_table(headers, rows2, alignments=["l", "r", "r"]))
                lines.append("")

            show_cols = [c for c in ("股东名称", "质押股份数量", "占总股本比例",
                                     "质押开始日期", "质押结束日期", "状态")
                         if c in unreleased.columns]
            if show_cols:
                lines.append("**未解押明细 (前5笔):**")
                lines.append("")
                rows3 = [[_pledge_cell(r.get(c), c) for c in show_cols]
                         for _, r in unreleased.head(5).iterrows()]
                lines.append(format_table(show_cols, rows3))
                lines.append("")

        lines.append("*来源: AKShare 东财质押 (个股明细)；比例与笔数仅统计未解押事件，"
                     "已解押事件及同一股东多笔不重复计入。*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Audit opinion (sub-section of §13 pipeline; AKShare 无稳定审计接口)
    # ------------------------------------------------------------------

    def get_audit(self, ts_code: str) -> str:
        """Audit opinion placeholder — best filled by Agent WebSearch."""
        evidence = self._store.get("audit_evidence")
        if evidence:
            return (format_header(3, "7B. 审计意见") +
                    f"\n\n{evidence['end_date']}：{evidence['opinion']}；{evidence['source']}；{evidence['locator']}\n"
                    "\n*此项是财报审计意见，不等于本分析报告的数字审计PASS。*\n")
        return (format_header(3, "7B. 审计意见")
                + "\n\n审计状态：未核实（不等于通过）。须以最新年报/审计报告标明意见类型、年度、来源与页码；不能因接口缺失跳过后宣称无审计风险。\n")
