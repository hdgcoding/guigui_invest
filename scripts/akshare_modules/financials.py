"""guigui_invest - FinancialsMixin (AKShare edition).

Financial statement get_* methods producing the same §1–§12 markdown
sections as the original Tushare-based framework (A-share only):

  §1  basic info        §3/§3P income (consolidated/parent)
  §2  market data       §4/§4P balance sheet
  §5  cash flow + FCF   §6  dividends
  §11 10y weekly        §12 financial indicators
"""

from __future__ import annotations

import pandas as pd

from config import split_code
from format_utils import format_number, format_table, format_header

from .constants import (
    SINA_INCOME_MAP,
    SINA_BALANCE_MAP,
    SINA_CASHFLOW_MAP,
    SINA_FINA_INDICATOR_MAP,
)


class FinancialsMixin:
    """Financial statement methods for AKShareClient."""

    # ------------------------------------------------------------------
    # Shared: fetch + normalize a Sina statement
    # ------------------------------------------------------------------

    def _fetch_sina_statement(self, ts_code: str, symbol: str,
                              col_map: dict) -> pd.DataFrame:
        """Fetch a Sina financial statement and normalize columns.

        Returns DataFrame with internal column names plus end_date (YYYYMMDD),
        rows newest-first. Amounts stay in raw yuan (百万元 via display div).
        """
        num, prefix = split_code(ts_code)
        endpoint = {"利润表": "income", "资产负债表": "balancesheet",
                    "现金流量表": "cashflow"}[symbol]

        def _fetch():
            import akshare as ak
            return ak.stock_financial_report_sina(stock=f"{prefix}{num}", symbol=symbol)

        df = self._cached_call(endpoint, _fetch, key=f"{endpoint}:{num}")
        if df is None or df.empty:
            return pd.DataFrame()

        # Some Sina responses include parent-company rows; keep consolidated
        for meta_col in ("类型",):
            if meta_col in df.columns:
                types = df[meta_col].astype(str)
                if (types == "合并报表").any():
                    df = df[types == "合并报表"]

        # Build normalized frame
        out = pd.DataFrame()
        out["end_date"] = (
            pd.to_datetime(df["报告日"]).dt.strftime("%Y%m%d")
        )
        for internal, cn in col_map.items():
            if cn in df.columns:
                out[internal] = pd.to_numeric(df[cn], errors="coerce")
            else:
                out[internal] = pd.NA
        # Keep raw values numeric (they are already in yuan)
        for c in out.columns:
            if c != "end_date":
                out[c] = pd.to_numeric(out[c], errors="coerce")

        out = out.drop_duplicates(subset=["end_date"])
        out = out.sort_values("end_date", ascending=False).reset_index(drop=True)
        return out

    # ------------------------------------------------------------------
    # §1 — Basic info
    # ------------------------------------------------------------------

    def get_basic_info(self, ts_code: str) -> str:
        """Section 1: basic info + latest price/valuation."""
        num, prefix = split_code(ts_code)
        name, list_date = self._fetch_name_and_list_date(ts_code)

        price_df = self._fetch_latest_daily(num)
        val = self._fetch_valuation(num)

        close = price_df.get("close")
        pe_ttm = val.get("pe_ttm")
        pb = val.get("pb")
        total_mv_wan = val.get("total_mv_wan")   # 万元

        total_share_wan = None
        if total_mv_wan and close:
            total_share_wan = total_mv_wan * 1e4 / close / 1e4  # 万股

        # Store for downstream (valuation engine / derived metrics)
        self._store["basic_info"] = pd.DataFrame([{
            "ts_code": ts_code,
            "name": name,
            "trade_date": price_df.get("date"),
            "close": close,
            "pe_ttm": pe_ttm,
            "pb": pb,
            "total_mv": total_mv_wan,          # 万元 (Tushare convention)
            "circ_mv": total_mv_wan,
            "total_share": total_share_wan,    # 万股
            "list_date": list_date,
        }])

        lines = [format_header(2, "1. 基本信息"), ""]
        rows = [
            ["股票代码", ts_code],
            ["公司名称", name or "—"],
            ["交易所", "上交所" if prefix == "sh" else "深交所"],
            ["上市日期", list_date or "—"],
            ["当前价格", f"{close:.2f}" if close else "—"],
            ["PE (TTM)", f"{pe_ttm:.2f}" if pe_ttm else "—"],
            ["PB", f"{pb:.2f}" if pb else "—"],
            ["总市值 (万元)", format_number(total_mv_wan, divider=1)],
            ["总股本 (万股)", format_number(total_share_wan, divider=1)],
        ]
        lines.append(format_table(["项目", "内容"], rows, alignments=["l", "r"]))
        lines.append("")
        lines.append("*来源: AKShare (百度估值/东财行情), PE/PB 为 TTM 口径*")
        return "\n".join(lines)

    def _fetch_name_and_list_date(self, ts_code: str) -> tuple[str | None, str | None]:
        """Resolve stock name + listing date via exchange tables."""
        num, prefix = split_code(ts_code)

        def _fetch():
            import akshare as ak
            if prefix == "sh":
                df = ak.stock_info_sh_name_code(symbol="主板A股")
                row = df[df["证券代码"] == num]
                if row.empty:
                    for board in ("科创板", "主板B股"):
                        try:
                            df2 = ak.stock_info_sh_name_code(symbol=board)
                            row = df2[df2["证券代码"] == num]
                            if not row.empty:
                                break
                        except Exception:
                            continue
                if not row.empty:
                    return row
            else:
                df = ak.stock_info_sz_name_code(symbol="A股列表")
                row = df[df["A股代码"] == num]
                if not row.empty:
                    return row
            # fallback: code-name table (no listing date)
            cn = ak.stock_info_a_code_name()
            hit = cn[cn["code"] == num]
            if not hit.empty:
                return pd.DataFrame([{"name": hit.iloc[0]["name"],
                                      "list_date": None}])
            return pd.DataFrame()

        df = self._cached_call("basic_name", _fetch, key=f"basic_name:{num}")
        if df is None or df.empty:
            return None, None

        name = None
        list_date = None
        for col in ("证券简称", "A股简称", "name", "股票简称"):
            if col in df.columns and pd.notna(df.iloc[0].get(col)):
                name = str(df.iloc[0][col])
                break
        for col in ("上市日期", "A股上市日期", "list_date"):
            if col in df.columns and pd.notna(df.iloc[0].get(col)):
                list_date = str(df.iloc[0][col])
                break
        return name, list_date

    def _fetch_daily(self, num: str) -> pd.DataFrame:
        """Full daily OHLCV (qfq) from Sina, normalized + cached.

        Returns trade_date/open/high/low/close/vol/amount (vol in 股).
        """
        def _fetch():
            import akshare as ak
            # Determine prefix from code: 6 -> sh, else sz
            pfx = "sh" if num.startswith("6") else "sz"
            return ak.stock_zh_a_daily(symbol=f"{pfx}{num}", adjust="qfq")

        df = self._cached_call("daily_hist", _fetch, key=f"daily_hist:{num}")
        if df is None or df.empty:
            return pd.DataFrame()

        out = pd.DataFrame()
        out["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
        out["open"] = pd.to_numeric(df["open"], errors="coerce")
        out["high"] = pd.to_numeric(df["high"], errors="coerce")
        out["low"] = pd.to_numeric(df["low"], errors="coerce")
        out["close"] = pd.to_numeric(df["close"], errors="coerce")
        out["vol"] = pd.to_numeric(df["volume"], errors="coerce")
        out["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        return out.sort_values("trade_date", ascending=True).reset_index(drop=True)

    def _fetch_latest_daily(self, num: str) -> dict:
        """Latest daily bar (close + date) from the Sina daily series."""
        df = self._fetch_daily(num)
        if df.empty:
            return {}
        return {"close": self._safe_float(df.iloc[-1]["close"]),
                "date": str(df.iloc[-1]["trade_date"])}

    def _fetch_valuation(self, num: str) -> dict:
        """Latest PE-TTM / PB / total market cap from Baidu valuation."""
        def _fetch():
            import akshare as ak
            pe = ak.stock_zh_valuation_baidu(symbol=num, indicator="市盈率(TTM)", period="近一年")
            pb = ak.stock_zh_valuation_baidu(symbol=num, indicator="市净率", period="近一年")
            mv = ak.stock_zh_valuation_baidu(symbol=num, indicator="总市值", period="近一年")
            return (pe, pb, mv)

        try:
            pe_df, pb_df, mv_df = _fetch()
        except Exception as e:
            print(f"  ⚠️ 百度估值接口失败: {e}")
            return {}

        def _latest(df):
            if df is None or df.empty:
                return None
            df = df.sort_values("date", ascending=False)
            return self._safe_float(df.iloc[0]["value"])

        pe = _latest(pe_df)
        pb = _latest(pb_df)
        mv_yi = _latest(mv_df)  # 亿元
        return {
            "pe_ttm": pe,
            "pb": pb,
            "total_mv_wan": mv_yi * 10000 if mv_yi else None,  # 万元
        }

    # ------------------------------------------------------------------
    # §2 — Market data (52-week range)
    # ------------------------------------------------------------------

    def get_market_data(self, ts_code: str) -> str:
        """Section 2: 52-week market data from weekly history."""
        num, _ = split_code(ts_code)
        df = self._fetch_weekly(num, years=2)

        lines = [format_header(2, "2. 市场行情"), ""]
        if df.empty:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        # last 52 calendar weeks
        df["ts"] = pd.to_datetime(df["trade_date"])
        cutoff = df["ts"].max() - pd.DateOffset(weeks=52)
        w = df[df["ts"] >= cutoff]

        latest_close = float(w.iloc[-1]["close"])
        high = float(w["high"].max())
        low = float(w["low"].min())
        high_date = str(w.loc[w["high"].idxmax(), "trade_date"])
        low_date = str(w.loc[w["low"].idxmin(), "trade_date"])
        avg_vol = float(w["vol"].mean())

        lines.append("*来源: AKShare 新浪日线 (前复权) 重采样周线*\n")
        lines.append(format_table(
            ["指标", "数值"],
            [
                ["最新收盘价 (元)", f"{latest_close:.2f}"],
                ["52周最高", f"{high:.2f} ({high_date})"],
                ["52周最低", f"{low:.2f} ({low_date})"],
                ["52周涨跌幅", f"{(latest_close / low - 1) * 100:.1f}% (自低点)"],
                ["周均成交量 (股)", f"{avg_vol:,.0f}"],
            ],
            alignments=["l", "r"],
        ))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §3 / §3P — Income statement
    # ------------------------------------------------------------------

    def get_income(self, ts_code: str) -> str:
        """Section 3: five-year consolidated income statement."""
        raw = self._fetch_sina_statement(ts_code, "利润表", SINA_INCOME_MAP)
        lines = [format_header(2, "3. 合并利润表"), ""]

        if raw.empty:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        df, years = self._prepare_display_periods(raw)
        self._store["income"] = df
        self._store["income_years"] = years

        if not years:
            lines.append("无年报数据\n")
            return "\n".join(lines)

        fields = [
            ("营业总收入", "revenue"),
            ("营业成本", "oper_cost"),
            ("税金及附加", "biz_tax_surch"),
            ("销售费用", "sell_exp"),
            ("管理费用", "admin_exp"),
            ("研发费用", "rd_exp"),
            ("财务费用", "finance_exp"),
            ("资产减值损失", "assets_impair_loss"),
            ("信用减值损失", "credit_impair_loss"),
            ("投资收益", "invest_income"),
            ("营业利润", "operate_profit"),
            ("利润总额", "total_profit"),
            ("所得税费用", "income_tax"),
            ("净利润", "n_income"),
            ("归母净利润", "n_income_attr_p"),
            ("少数股东损益", "minority_gain"),
            ("基本EPS", "basic_eps"),
            ("稀释EPS", "diluted_eps"),
        ]
        headers = ["项目 (百万元)"] + years
        rows = []
        for label, col in fields:
            row = [label]
            for _, r in df.iterrows():
                val = r.get(col)
                if col in ("basic_eps", "diluted_eps"):
                    row.append(f"{val:.2f}" if val is not None and val == val else "—")
                else:
                    row.append(format_number(val))
            rows.append(row)

        lines.append(format_table(headers, rows, alignments=["l"] + ["r"] * len(years)))
        lines.append("")
        lines.append("*单位: 百万元 (原始数据 / 1,000,000), EPS为元/股; 来源: AKShare 新浪财经*")
        return "\n".join(lines)

    def get_income_parent(self, ts_code: str) -> str:
        """Section 3P: parent-company income (degraded — AKShare 无母公司报表)."""
        return (format_header(2, "3P. 母公司利润表")
                + "\n\n数据缺失 (AKShare 新浪接口仅提供合并报表；如需母公司口径请阅读年报 PDF 附注补充)\n")

    # ------------------------------------------------------------------
    # §4 / §4P — Balance sheet
    # ------------------------------------------------------------------

    def get_balance_sheet(self, ts_code: str) -> str:
        """Section 4: five-year consolidated balance sheet."""
        raw = self._fetch_sina_statement(ts_code, "资产负债表", SINA_BALANCE_MAP)
        lines = [format_header(2, "4. 合并资产负债表"), ""]

        if raw.empty:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        df, years = self._prepare_display_periods(raw)
        self._store["balance_sheet"] = df
        self._store["balance_sheet_years"] = years

        if not years:
            lines.append("无年报数据\n")
            return "\n".join(lines)

        fields = [
            ("货币资金", "money_cap"),
            ("交易性金融资产", "trad_asset"),
            ("应收账款", "accounts_receiv"),
            ("存货", "inventories"),
            ("流动资产合计", "total_cur_assets"),
            ("固定资产净额", "fix_assets"),
            ("在建工程", "cip"),
            ("无形资产", "intang_assets"),
            ("商誉", "goodwill"),
            ("递延所得税资产", "defer_tax_assets"),
            ("资产总计", "total_assets"),
            ("短期借款", "st_borr"),
            ("应付账款", "acct_payable"),
            ("合同负债", "contract_liab"),
            ("流动负债合计", "total_cur_liab"),
            ("长期借款", "lt_borr"),
            ("应付债券", "bond_payable"),
            ("租赁负债", "lease_liab"),
            ("负债合计", "total_liab"),
            ("实收资本(或股本)", "total_share_capital"),
            ("未分配利润", "undistributed_profit"),
            ("归母股东权益", "total_hldr_eqy_exc_min_int"),
            ("少数股东权益", "minority_int"),
            ("所有者权益合计", "total_holders_eq"),
        ]
        headers = ["项目 (百万元)"] + years
        rows = []
        for label, col in fields:
            row = [label]
            for _, r in df.iterrows():
                row.append(format_number(r.get(col)))
            rows.append(row)

        lines.append(format_table(headers, rows, alignments=["l"] + ["r"] * len(years)))
        lines.append("")
        lines.append("*单位: 百万元; 来源: AKShare 新浪财经*")
        return "\n".join(lines)

    def get_balance_sheet_parent(self, ts_code: str) -> str:
        """Section 4P: parent-company balance sheet (degraded)."""
        return (format_header(2, "4P. 母公司资产负债表")
                + "\n\n数据缺失 (AKShare 新浪接口仅提供合并报表；如需母公司口径请阅读年报 PDF 附注补充)\n")

    # ------------------------------------------------------------------
    # §5 — Cash flow
    # ------------------------------------------------------------------

    def get_cashflow(self, ts_code: str) -> str:
        """Section 5: five-year cash flow statement with FCF."""
        raw = self._fetch_sina_statement(ts_code, "现金流量表", SINA_CASHFLOW_MAP)
        lines = [format_header(2, "5. 现金流量表"), ""]

        if raw.empty:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        df, years = self._prepare_display_periods(raw)
        self._store["cashflow"] = df
        self._store["cashflow_years"] = years

        if not years:
            lines.append("无年报数据\n")
            return "\n".join(lines)

        headers = ["项目 (百万元)"] + years
        rows = []

        simple_fields = [
            ("经营活动现金流 (OCF)", "n_cashflow_act"),
            ("  销售商品收到的现金", "c_recp_sale_goods"),
            ("投资活动现金流", "n_cashflow_inv_act"),
            ("  购建固定资产等资本支出", "c_pay_acq_const_fiolta"),
            ("筹资活动现金流", "n_cash_flows_fnc_act"),
            ("  分配股利/利息支付的现金", "c_pay_dist_dpcp_int_exp"),
            ("支付给职工现金", "c_pay_to_staff"),
            ("支付的各项税费", "c_paid_for_taxes"),
        ]
        for label, col in simple_fields:
            row = [label]
            for _, r in df.iterrows():
                row.append(format_number(r.get(col)))
            rows.append(row)

        # FCF = OCF - |Capex|
        fcf_row = ["自由现金流 (FCF)"]
        for _, r in df.iterrows():
            ocf = self._safe_float(r.get("n_cashflow_act"))
            capex = self._safe_float(r.get("c_pay_acq_const_fiolta"))
            if ocf is not None and capex is not None:
                fcf_row.append(format_number(ocf - abs(capex)))
            else:
                fcf_row.append("—")
        rows.append(fcf_row)

        lines.append(format_table(headers, rows, alignments=["l"] + ["r"] * len(years)))
        lines.append("")
        lines.append("*单位: 百万元; FCF = OCF - |Capex|; 来源: AKShare 新浪财经*")
        lines.append("*注: 新浪接口不含折旧摊销附表，D&A 相关衍生指标降级*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §6 — Dividends
    # ------------------------------------------------------------------

    def get_dividends(self, ts_code: str) -> str:
        """Section 6: dividend history from em 分红送配详情."""
        num, _ = split_code(ts_code)

        def _fetch():
            import akshare as ak
            return ak.stock_fhps_detail_em(symbol=num)

        df = self._fetch_with_retry(_fetch)
        if df is None:
            df = pd.DataFrame()
        lines = [format_header(2, "6. 分红历史"), ""]

        if df.empty:
            lines.append("暂无分红数据\n")
            return "\n".join(lines)

        # Normalize to internal contract
        recs = []
        for _, r in df.iterrows():
            report_period = str(r.get("报告期", ""))
            if not report_period or report_period == "NaT":
                continue
            end_date = pd.Timestamp(report_period).strftime("%Y%m%d")
            cash_per_10 = self._safe_float(r.get("现金分红-现金分红比例")) or 0
            stk_per_10 = self._safe_float(r.get("送转股份-送股比例")) or 0
            total_shares = self._safe_float(r.get("总股本")) or 0
            proc = str(r.get("方案进度", ""))
            recs.append({
                "end_date": end_date,
                # 现金分红比例为每10股派息(含税, 元) → 转为每股(元)
                "cash_div_tax": cash_per_10 / 10 if cash_per_10 else 0,
                "stk_div": stk_per_10 / 10 if stk_per_10 else 0,
                "base_share": total_shares / 1e4 if total_shares else 0,  # 万股
                "div_proc": "实施" if "实施" in proc else proc,
                "ex_date": str(r.get("除权除息日", "")),
                "plan": str(r.get("现金分红-现金分红比例描述", "")),
            })

        all_div = pd.DataFrame(recs)
        if all_div.empty:
            lines.append("暂无分红数据\n")
            return "\n".join(lines)

        self._store["dividends_all"] = all_div

        # Implemented only, for display + derived metrics
        impl = all_div[all_div["div_proc"] == "实施"].copy()
        impl = impl.sort_values("end_date", ascending=False)
        # Limit to recent 5 fiscal years (by 报告期 year, 中期+末期同属一个年度)
        impl["_year"] = impl["end_date"].astype(str).str[:4]
        top_years = impl["_year"].drop_duplicates().head(6).tolist()
        impl = impl[impl["_year"].isin(top_years)].drop(columns=["_year"])

        self._store["dividends"] = impl

        if impl.empty:
            lines.append("暂无已实施分红\n")
            return "\n".join(lines)

        headers = ["报告期", "每股现金分红(税前,元)", "每股送股", "除权除息日", "方案"]
        rows = []
        for _, r in impl.iterrows():
            rows.append([
                str(r["end_date"]),
                f"{r['cash_div_tax']:.4f}" if r["cash_div_tax"] else "—",
                f"{r['stk_div']:.2f}" if r["stk_div"] else "—",
                r["ex_date"][:10] if r["ex_date"] else "—",
                r.get("plan", "—"),
            ])
        lines.append(format_table(headers, rows, alignments=["l", "r", "r", "l", "l"]))
        lines.append("")
        lines.append("*来源: AKShare 东财分红送配; 中期+末期分红同属一个报告年度*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §11 — 10-year weekly prices
    # ------------------------------------------------------------------

    def _fetch_weekly(self, num: str, years: int = 10) -> pd.DataFrame:
        """Weekly OHLCV resampled from the Sina daily series (qfq)."""
        df = self._fetch_daily(num)
        if df.empty:
            return pd.DataFrame()

        d = df.copy()
        d["ts"] = pd.to_datetime(d["trade_date"])
        start = d["ts"].max() - pd.DateOffset(years=years)
        d = d[d["ts"] >= start]

        # Resample to weeks ending Friday
        d = d.set_index("ts")
        weekly = pd.DataFrame({
            "open": d["open"].resample("W-FRI").first(),
            "high": d["high"].resample("W-FRI").max(),
            "low": d["low"].resample("W-FRI").min(),
            "close": d["close"].resample("W-FRI").last(),
            "vol": d["vol"].resample("W-FRI").sum(),
            "amount": d["amount"].resample("W-FRI").sum(),
        }).dropna(subset=["close"])
        weekly = weekly.reset_index()
        weekly["trade_date"] = weekly["ts"].dt.strftime("%Y%m%d")
        weekly["vol"] = weekly["vol"].fillna(0)
        return weekly[["trade_date", "open", "high", "low", "close", "vol", "amount"]]

    def get_weekly_prices(self, ts_code: str) -> str:
        """Section 11: 10-year weekly summary + annual aggregates."""
        num, _ = split_code(ts_code)
        df = self._fetch_weekly(num, years=10)

        lines = [format_header(2, "11. 十年周线行情"), ""]
        if df.empty:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        self._store["weekly_prices"] = df

        high_10y = df["high"].max()
        low_10y = df["low"].min()
        high_date = df.loc[df["high"].idxmax(), "trade_date"]
        low_date = df.loc[df["low"].idxmin(), "trade_date"]
        latest_close = float(df.iloc[-1]["close"])

        lines.append(format_table(
            ["指标", "数值"],
            [
                ["10年最高 (元)", f"{high_10y:.2f} ({high_date})"],
                ["10年最低 (元)", f"{low_10y:.2f} ({low_date})"],
                ["最新收盘 (元)", f"{latest_close:.2f}"],
                ["距最高回撤", f"{(1 - latest_close / high_10y) * 100:.1f}%"],
                ["距最低涨幅", f"{(latest_close / low_10y - 1) * 100:.1f}%"],
            ],
            alignments=["l", "r"],
        ))
        lines.append("")

        # Annual aggregation
        d = df.copy()
        d["year"] = d["trade_date"].str[:4]
        annual = d.groupby("year").agg(
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            avg_vol=("vol", "mean"),
        ).reset_index().sort_values("year", ascending=False)

        lines.append(format_header(3, "年度行情汇总"))
        lines.append("")
        lines.append(format_table(
            ["年度", "最高", "最低", "年末收盘", "周均成交量(股)"],
            [[r["year"], f"{r['high']:.2f}", f"{r['low']:.2f}",
              f"{r['close']:.2f}", f"{r['avg_vol']:,.0f}"] for _, r in annual.iterrows()],
            alignments=["l", "r", "r", "r", "r"],
        ))
        lines.append("")
        lines.append("*来源: AKShare 东财历史行情 (周线, 前复权)*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # §12 — Financial indicators
    # ------------------------------------------------------------------

    def get_fina_indicators(self, ts_code: str) -> str:
        """Section 12: key financial indicators (新浪财务指标)."""
        num, _ = split_code(ts_code)

        def _fetch():
            import akshare as ak
            return ak.stock_financial_analysis_indicator(symbol=num)

        df = self._fetch_with_retry(_fetch)
        if df is None:
            df = pd.DataFrame()
        lines = [format_header(2, "12. 关键财务指标"), ""]

        if df.empty:
            lines.append("数据缺失\n")
            return "\n".join(lines)

        # Normalize: 报告日(1996-12-31 格式) -> end_date YYYYMMDD
        out = pd.DataFrame()
        out["end_date"] = pd.to_datetime(df["日期"]).dt.strftime("%Y%m%d")
        for internal, cn in SINA_FINA_INDICATOR_MAP.items():
            out[internal] = pd.to_numeric(df[cn], errors="coerce") if cn in df.columns else pd.NA
        out = out.drop_duplicates(subset=["end_date"])
        out = out.sort_values("end_date", ascending=False).reset_index(drop=True)

        disp, years = self._prepare_display_periods(out)
        self._store["fina_indicators"] = disp
        self._store["fina_indicators_years"] = years

        if not years:
            lines.append("无年报数据\n")
            return "\n".join(lines)

        pct_fields = [
            ("ROE 摊薄 (%)", "roe"),
            ("ROE 加权 (%)", "roe_waa"),
            ("毛利率 (%)", "grossprofit_margin"),
            ("净利率 (%)", "netprofit_margin"),
            ("资产负债率 (%)", "debt_to_assets"),
            ("股息发放率 (%)", "payout_ratio"),
        ]
        ratio_fields = [
            ("流动比率", "current_ratio"),
            ("速动比率", "quick_ratio"),
            ("总资产周转率", "assets_turn"),
        ]
        growth_fields = [
            ("营收同比增长率 (%)", "revenue_yoy"),
            ("净利润同比增长率 (%)", "netprofit_yoy"),
        ]
        per_share_fields = [
            ("每股收益 (元)", "eps"),
            ("每股净资产 (元)", "bps"),
            ("每股经营现金流 (元)", "ocfps"),
        ]

        headers = ["指标"] + years
        rows = []
        for group in (pct_fields, ratio_fields, growth_fields, per_share_fields):
            for label, col in group:
                row = [label]
                for _, r in disp.iterrows():
                    val = r.get(col)
                    row.append(f"{val:.2f}" if val is not None and val == val else "—")
                rows.append(row)

        lines.append(format_table(headers, rows, alignments=["l"] + ["r"] * len(years)))
        lines.append("")
        lines.append("*来源: AKShare 新浪财务指标; ROE 为年报口径*")
        return "\n".join(lines)
