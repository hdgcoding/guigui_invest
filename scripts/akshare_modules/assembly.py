"""guigui_invest - AssemblyMixin + WarningsCollector (AKShare edition).

Data pack assembly (§1–§17), refresh-market merge, and warning collection.
Adapted from the original Turtle framework assembly.py (A-share only,
AKShare source, no yfinance fallback).
"""

from __future__ import annotations

import re

import pandas as pd
from financial_contracts import statement_issues

from format_utils import format_table, format_header


class AssemblyMixin:
    """Data pack assembly for AKShareClient."""

    def compute_derived_metrics(self, ts_code: str) -> str:
        """Compute §17: derived metrics from stored DataFrames.

        Must be called after all get_* methods have populated self._store.
        """
        lines = [
            format_header(2, "17. 衍生指标（Python 预计算）"),
            "",
            f"> 以下指标基于 §1-§16 原始数据确定性计算，无 LLM 判断成分。Phase 3 可直接引用。{self._unit_label()}。",
            "",
        ]
        issues = statement_issues(self._store)
        self._store["statement_issues"] = issues
        for key in ("factor3_sensitivity", "payout_crosscheck", "_true_cash_rev", "_w_total"):
            self._store.pop(key, None)
        if issues:
            lines.append("⚠️ 跨表口径未通过校验，暂停§17数值输出（不得据此生成买点）：")
            lines.extend(f"- {issue}" for issue in issues)
            return "\n".join(lines)

        sub_methods = [
            self._compute_financial_trends,
            lambda: self._compute_factor2_inputs(ts_code),
            self._compute_factor3_step1,
            self._compute_factor3_step4,
            self._compute_factor3_sensitivity_base,
            self._compute_factor4_inputs,
            self._compute_sotp_inputs,
            lambda: self._compute_factor4_ev_baseline(ts_code),
            lambda: self._compute_factor4_sensitivity(ts_code),
            self._compute_payout_crosscheck,
            lambda: self._compute_penetration_grid(ts_code),
            self._compute_g_grid,
            lambda: self._compute_revenue_sensitivity(ts_code),
        ]

        for method in sub_methods:
            try:
                result = method()
                if result:
                    lines.append(result)
                    lines.append("")
            except Exception as e:  # noqa: BLE001
                name = getattr(method, "__name__", str(method))
                lines.append(f"*{name} 计算失败: {e}*")
                lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Refresh-market helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sections(content: str):
        """Split data_pack_market.md into header, sections list, footer."""
        section_re = re.compile(r"^(## (\d+[A-Z]?)\. .*)$", re.MULTILINE)

        matches = list(section_re.finditer(content))
        if not matches:
            return content, [], ""

        header = content[: matches[0].start()]

        sections = []
        for i, m in enumerate(matches):
            key = m.group(2)
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            sections.append((key, content[start:end]))

        footer = ""
        if sections:
            last_key, last_text = sections[-1]
            footer_re = re.compile(r"\n---\n\*共 .+$", re.DOTALL)
            fm = footer_re.search(last_text)
            if fm:
                footer = last_text[fm.start():]
                sections[-1] = (last_key, last_text[: fm.start()])

        return header, sections, footer

    def _build_header(self, ts_code: str) -> str:
        """Build the data pack header block."""
        timestamp = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            format_header(1, f"数据包 — {ts_code}"),
            "",
            f"*生成时间: {timestamp}*",
            f"*财务采集时间: {self._store.get('_financial_collected_at', timestamp)}*",
            f"*数据来源: AKShare (新浪财经/东方财富/百度估值)*",
            f"*金额单位: {self._unit_label()} (除特殊标注)*",
            "",
            "---",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _check_staleness(content: str) -> int:
        """Days since the data pack was generated (999 if unparsable)."""
        m = re.search(r"\*财务采集时间:\s*(\d{4}-\d{2}-\d{2})", content)
        if not m:
            m = re.search(r"\*生成时间:\s*(\d{4}-\d{2}-\d{2})", content)
        if not m:
            return 999
        try:
            gen_date = pd.Timestamp(m.group(1))
            now = pd.Timestamp.now().normalize()
            return (now - gen_date.normalize()).days
        except Exception:
            return 999

    _REFRESH_SECTIONS = {"1", "2", "11", "14", "17"}

    def refresh_market_sections(self, ts_code: str, existing_content: str) -> str:
        """Rehydrate statements via TTL cache and recompute all dependencies.

        Market caches are bypassed. Never retain an old price-dependent §17 or
        silently carry forward a failed market request as fresh data.
        """
        _, old_sections, _ = self._parse_sections(existing_content)
        financial_date = re.search(r"\*财务采集时间:\s*([^*]+)\*", existing_content)
        if not financial_date:
            financial_date = re.search(r"\*生成时间:\s*([^*]+)\*", existing_content)
        self._store = {}
        if financial_date and self._check_staleness(existing_content) <= 7:
            self._store["_financial_collected_at"] = financial_date.group(1)
        self._refresh_market = True
        try:
            fresh = self.assemble_data_pack(ts_code)
        finally:
            self._refresh_market = False
        header, new_sections, footer = self._parse_sections(fresh)
        # Agent-only industry/MD&A research has its own as-of date. Preserve it
        # with a stale-source notice; never merge it into deterministic figures.
        old = dict(old_sections)
        parts = [header + "*刷新模式: 行情强制刷新，财报按TTL重新载入，§17全量重算*\n\n"]
        for key, text in new_sections:
            if key in {"8", "10"} and key in old and "待Agent" not in old[key]:
                text = old[key].rstrip()
                notice = "> 此研究沿用上期；须重新核查时效。"
                if notice not in text:
                    text += "\n\n" + notice
                text += "\n\n"
            elif key in {"7", "9"} and key in old:
                # Governance/segment sections may include human/Agent research
                # below endpoint tables. Keep it separately marked as historical
                # rather than silently erasing it during a market refresh.
                marker = "### 上期研究留存（非本次已核实数据）"
                previous = old[key]
                archive = previous.split(marker, 1)[1] if marker in previous else previous.split("\n", 1)[-1]
                text = text.rstrip() + "\n\n" + marker + "\n\n" + archive.strip() + "\n\n"
            parts.append(text)
        return "".join(parts).rstrip() + footer + "\n"

    # ------------------------------------------------------------------
    # Full assembly
    # ------------------------------------------------------------------

    def assemble_data_pack(self, ts_code: str) -> str:
        """Assemble complete data_pack_market.md combining all sections."""
        # A failed fetch for the next stock/refresh must not reuse the previous
        # stock's statements, dividends, supplemental evidence or derived grids.
        keep = ("as_of", "_financial_collected_at") if getattr(self, "_refresh_market", False) else ("as_of",)
        self._store = {key: self._store[key] for key in keep if key in self._store}
        lines = self._build_header(ts_code).split("\n")
        self._supplement_applied = False

        sections = [
            ("1. 基本信息", self.get_basic_info),
            ("2. 市场行情", self.get_market_data),
            ("3. 合并利润表", self.get_income),
            ("3P. 母公司利润表", self.get_income_parent),
            ("4. 合并资产负债表", self.get_balance_sheet),
            ("4P. 母公司资产负债表", self.get_balance_sheet_parent),
            ("5. 现金流量表", self.get_cashflow),
            ("6. 分红历史", self.get_dividends),
            ("7. 股东与治理", self.get_holders),
            ("9. 主营业务构成", self.get_segments),
            ("11. 十年周线行情", self.get_weekly_prices),
            ("12. 关键财务指标", self.get_fina_indicators),
            ("15. 股票回购", self.get_repurchase),
            ("16. 股权质押", self.get_pledge_stat),
        ]

        completed = 0
        for name, method in sections:
            try:
                print(f"  Collecting {name}...")
                section_md = method(ts_code)
                lines.append(section_md)
                lines.append("")
                if not any(marker in section_md for marker in ("数据缺失", "暂无", "无年报数据", "获取失败")):
                    completed += 1
            except Exception as e:  # noqa: BLE001
                lines.append(format_header(2, name))
                lines.append(f"\n数据获取失败: {e}\n")

        if getattr(self, "_supplement_path", None):
            from financial_supplement import apply_supplement
            apply_supplement(self, self._supplement_path, ts_code)
            self._supplement_applied = True

        # Audit info (sub-section of §7)
        try:
            lines.append(self.get_audit(ts_code))
            lines.append("")
        except Exception:  # noqa: BLE001
            pass

        # §14 risk-free rate
        try:
            print("  Collecting 14. 无风险利率...")
            lines.append(self.get_risk_free_rate(ts_code))
            lines.append("")
        except Exception as e:  # noqa: BLE001
            lines.append(format_header(2, "14. 无风险利率"))
            lines.append(f"\n数据获取失败: {e}\n")

        # Agent-only placeholder sections (§8, §10)
        for sec_num, sec_name in [
            ("8", "行业与竞争"),
            ("10", "管理层讨论与分析 (MD&A)"),
        ]:
            lines.append(format_header(2, f"{sec_num}. {sec_name}"))
            lines.append("")
            lines.append(f"*[§{sec_num} 待Agent WebSearch补充]*")
            lines.append("")

        # §17 derived metrics
        if self._supplement_applied:
            lines.append("> 已载入来源核对补充：" + str(self._supplement_path) + "。这不是独立审计PASS。")
        try:
            print("  Computing 17. 衍生指标...")
            lines.append(self.compute_derived_metrics(ts_code))
            lines.append("")
        except Exception as e:  # noqa: BLE001
            lines.append(format_header(2, "17. 衍生指标（Python 预计算）"))
            lines.append(f"\n计算失败: {e}\n")

        # §13 warnings
        wc = WarningsCollector()
        try:
            for label, store_key, cols in [
                ("合并利润表", "income", ["revenue", "n_income_attr_p"]),
                ("合并资产负债表", "balance_sheet", ["total_assets"]),
                ("现金流量表", "cashflow", ["n_cashflow_act"]),
            ]:
                df = self._store.get(store_key)
                if df is None:
                    df = pd.DataFrame()
                wc.check_missing_data(label, df)
                if not df.empty and "end_date" in df.columns:
                    annual = df[df["end_date"].astype(str).str.endswith("1231")].copy()
                    annual = annual.sort_values("end_date", ascending=False)
                    if not annual.empty:
                        dates = annual["end_date"].astype(str).str[:4].tolist()
                        for col in cols:
                            if col in annual.columns:
                                wc.check_yoy_change(label, col, annual[col].tolist(), dates=dates)

            # Balance sheet risk checks (store rows newest-first)
            bs_df = self._store.get("balance_sheet")
            if bs_df is not None and not bs_df.empty:
                latest = bs_df.iloc[0]
                gw = latest.get("goodwill", 0) or 0
                ta = latest.get("total_assets", 0) or 0
                tl = latest.get("total_liab", 0) or 0
                wc.check_goodwill_ratio(float(gw), float(ta))
                wc.check_debt_ratio(float(tl), float(ta))

            # ST check from name
            bi = self._store.get("basic_info")
            if bi is not None and not bi.empty:
                nm = str(bi.iloc[0].get("name", ""))
                if "ST" in nm.upper():
                    wc.warnings.append({
                        "type": "ST_RISK",
                        "severity": "高",
                        "message": f"股票名称含 ST: {nm}",
                    })

            # Pledge ratio check
            pl_df = self._store.get("pledge")
            if pl_df is not None and not pl_df.empty and "占总股本比例" in pl_df.columns:
                ratios = pd.to_numeric(pl_df["占总股本比例"], errors="coerce").dropna()
                if not ratios.empty and float(ratios.sum()) > 50:
                    wc.warnings.append({
                        "type": "PLEDGE_RISK",
                        "severity": "高",
                        "message": f"质押股份占总股本比例合计 {ratios.sum():.1f}% 超过 50%",
                    })
        except Exception:  # noqa: BLE001
            pass  # warnings are best-effort

        lines.append(format_header(2, "13. 风险警示"))
        lines.append("")
        lines.append("### 13.1 脚本自动检测")
        lines.append("")
        if wc.warnings:
            high = [w for w in wc.warnings if w["severity"] == "高"]
            medium = [w for w in wc.warnings if w["severity"] == "中"]
            for sev_label, items in [("高风险", high), ("中风险", medium)]:
                if items:
                    lines.append(f"**{sev_label}:**")
                    for w in items:
                        lines.append(f"- [{w['type']}|{w['severity']}] {w['message']}")
                    lines.append("")
        else:
            lines.append("已覆盖字段未检测到异常；不代表缺失项目通过审计。")
            lines.append("")

        lines.append("")
        lines.append("> AKShare 版数据覆盖说明：§3P/§4P 母公司报表缺失（请以年报 PDF 附注补充），"
                     "§5 D&A通过东财/同花顺补充，缺项保持未知；审计意见未核实，不得视为通过。")
        for issue in self._store.get("statement_issues", []):
            lines.append(f"- [STATEMENT_VERSION|高] {issue}")
        lines.append("")

        lines.append("### 13.2 Agent WebSearch 补充")
        lines.append("")
        lines.append("*[§13.2 待Agent WebSearch补充]*")
        lines.append("")

        lines.append("---")
        lines.append(f"*共 {completed}/{len(sections)} 个数据板块成功获取*")

        result = "\n".join(lines)
        if self._supplement_applied:
            from .constants import SINA_INCOME_MAP, SINA_BALANCE_MAP
            header, parts, footer = self._parse_sections(result)
            replacements = {}
            for key, section, symbol, mapping in (("income_parent", "3P", "利润表", SINA_INCOME_MAP),
                                                 ("balance_sheet_parent", "4P", "资产负债表", SINA_BALANCE_MAP)):
                if key in self._store:
                    replacements[section] = self._render_parent_data(section, symbol, mapping, key) + "\n"
            result = header + "".join(replacements.get(key, text) for key, text in parts) + footer
        return result


class WarningsCollector:
    """Auto-detect anomalies during data collection."""

    def __init__(self):
        self.warnings = []

    def check_missing_data(self, section_name: str, df: pd.DataFrame):
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            self.warnings.append({
                "type": "DATA_MISSING",
                "severity": "中",
                "message": f"{section_name} 数据缺失",
            })

    def check_yoy_change(self, section_name: str, field_name: str,
                         values: list, threshold: float = 3.0,
                         dates: list = None):
        for i in range(len(values) - 1):
            curr, prev = values[i], values[i + 1]
            if prev is not None and curr is not None and float(prev) != 0:
                try:
                    change = abs(float(curr) / float(prev) - 1)
                    if change > threshold:
                        period = ""
                        if dates and i + 1 < len(dates):
                            period = f"{dates[i+1]}→{dates[i]} "
                        self.warnings.append({
                            "type": "YOY_ANOMALY",
                            "severity": "高",
                            "message": f"{section_name}/{field_name}: "
                                       f"{period}同比变化 {change*100:.0f}% 超过 {threshold*100:.0f}% 阈值",
                        })
                except (ValueError, ZeroDivisionError):
                    pass

    def check_goodwill_ratio(self, goodwill: float, total_assets: float):
        if goodwill and total_assets and total_assets > 0:
            ratio = float(goodwill) / float(total_assets)
            if ratio > 0.20:
                self.warnings.append({
                    "type": "GOODWILL_RISK",
                    "severity": "高",
                    "message": f"商誉占总资产比例 {ratio*100:.1f}% 超过 20%",
                })

    def check_debt_ratio(self, total_liab: float, total_assets: float):
        if total_liab and total_assets and total_assets > 0:
            ratio = float(total_liab) / float(total_assets)
            if ratio > 0.70:
                self.warnings.append({
                    "type": "LEVERAGE_RISK",
                    "severity": "中",
                    "message": f"资产负债率 {ratio*100:.1f}% 超过 70%",
                })
