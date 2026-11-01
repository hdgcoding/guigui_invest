"""report_consistency.py — 杠杆6: cross-passage numeric consistency audit.

Scans a finished qualitative report (markdown) for the same metric being
written with different values in different passages (e.g. 毛利率 47.6% 前文 /
49.2% 后文) — the "叙述内部矛盾" class that quality_control.py (算式正确) does
not cover. Optionally (``--gates``) enforces deterministic hard gates (杠杆7):
per-dimension 字数下限 + presence of required sections.

Method (see plan §4.2):
  1. mask spans that must be skipped ([src:...] / §N / P.X / 8-digit dates)
  2. extract numbers carrying a money/percent unit only
  3. classify to one of ~14 metric categories by keyword in a ±30 char window
  4. normalize money→亿元 / ratio→% ; require a year in the window to cluster
  5. cluster by (category, year); flag clusters spanning > tolerance

Exit codes: 0 no conflicts (& gates pass) · 1 conflicts / gate failures · 2 file error.
The exit-1 conflict list is **advisory** — a downstream numeric_audit agent
adjudicates 真错误 vs 口径差异.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import json
import hashlib
from typing import Dict, List, Optional, Tuple

WINDOW = 30

# (canonical, keywords, kind) — order matters: most-specific first.
CATEGORIES: List[Tuple[str, Tuple[str, ...], str]] = [
    ("扣非净利润", ("扣非净利润", "扣非归母", "扣非"), "money"),
    ("归母净利润", ("归母净利润", "归母"), "money"),
    ("营业利润", ("营业利润",), "money"),
    ("净利润", ("净利润",), "money"),
    ("营业收入", ("营业收入", "营收", "销售收入"), "money"),
    ("营业成本", ("营业成本",), "money"),
    ("总市值", ("总市值", "市值"), "money"),
    ("分红总额", ("总分红", "分红总额", "现金分红总额"), "money"),
    ("毛利率", ("毛利率",), "ratio"),
    ("净利率", ("净利率",), "ratio"),
    ("ROE", ("ROE", "净资产收益率", "加权roe"), "ratio"),
    ("资产负债率", ("资产负债率",), "ratio"),
    ("股息率", ("股息率",), "ratio"),
    ("分红率", ("分红支付率", "分红率", "支付率", "派息率"), "ratio"),
    # Per-share prices: a bare "元" is only meaningful inside these windows and
    # must never be bucketed with 亿元 amounts (or a 买点 can hide as a 金额).
    ("目标价", ("目标价", "买点", "买入价", "合理价", "参考价"), "pershare"),
    ("股价", ("股价", "现价", "收盘价", "每股价格", "当前价格"), "pershare"),
    ("每股收益", ("EPS", "每股收益", "每股盈利"), "pershare"),
]

# 万元 = 1e4 元 = 1e-4 亿元; 百万元 = 1e-2 亿元. Longest unit alternatives first.
MONEY_UNITS = {"亿元": 1.0, "亿": 1.0, "百万元": 0.01, "万元": 0.0001, "百万": 0.01}
NUM_UNIT_RE = re.compile(
    r"(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*"
    r"(亿元|亿|百万元|万元|百万|元/股|元|%|％)"
)
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_MASK_PATTERNS = [
    re.compile(r"\[src:[^\]]*\]"),
    re.compile(r"§\s*\d+(?:\.\d+)?"),
    re.compile(r"[Pp]\.?\s*\d+"),
    re.compile(r"\d{8}"),
    re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"),
    # Year-month periods ("2026-07") are dates, not values to compare/买点.
    re.compile(r"\d{4}[-/]\d{1,2}(?!\d)"),
]


# ─────────────────────────── extraction ───────────────────────────────

def _mask(text: str) -> str:
    """Blank out skip-spans, preserving character offsets."""
    out = list(text)
    for pat in _MASK_PATTERNS:
        for m in pat.finditer(text):
            for i in range(m.start(), m.end()):
                out[i] = " "
    return "".join(out)


def _classify(window: str, num_offset: int) -> Optional[Tuple[str, str]]:
    """Pick the category whose keyword occurrence is *nearest* to the number.

    Nearest-keyword (not list-order) avoids cross-contaminating adjacent
    metrics in the same sentence (e.g. 营业利润 122.51 亿, 归母 89.54 亿)."""
    low = window.lower()
    best = None  # (distance, canon, kind)
    for canon, kws, kind in CATEGORIES:
        for kw in kws:
            hay = low if kw.islower() else window
            start = 0
            while True:
                idx = hay.find(kw, start)
                if idx < 0:
                    break
                # A metric must immediately label this value, not occur in a
                # neighbouring table cell/sentence (发电量 is not 净利润).
                if idx >= num_offset:
                    start = idx + 1
                    continue
                bridge = window[idx + len(kw):num_offset]
                if len(bridge) > 10 or re.search(r"[|；;。\n%％]|\d", bridge):
                    start = idx + 1
                    continue
                if any(token in bridge for token in ("同比", "增长", "下降", "增速", "变化")):
                    start = idx + 1
                    continue
                dist = len(bridge)
                if best is None or dist < best[0]:
                    best = (dist, canon, kind)
                start = idx + 1
    if best is None:
        return None
    return best[1], best[2]


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


class Obs:
    __slots__ = ("value", "line", "snippet")

    def __init__(self, value: float, line: int, snippet: str):
        self.value = value
        self.line = line
        self.snippet = snippet


def extract(text: str) -> Dict[Tuple[str, str], List[Obs]]:
    """Return {(category, year): [Obs, ...]} of normalized values."""
    masked = _mask(text)
    clusters: Dict[Tuple[str, str], List[Obs]] = {}
    for m in NUM_UNIT_RE.finditer(masked):
        raw, unit = m.group(1), m.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        w0, w1 = max(0, m.start() - WINDOW), min(len(masked), m.end() + WINDOW)
        window = masked[w0:w1]
        cls = _classify(window, m.start() - w0)
        if cls is None:
            continue
        canon, kind = cls
        if kind == "pershare":
            # 元/股 or a bare 元 directly attached to a price word. Bare 元 is
            # *never* accepted for money categories (it would be a 1e-8 unit bug).
            if unit not in ("元", "元/股"):
                continue
            norm = value
        elif kind == "money":
            if unit not in MONEY_UNITS:
                continue
            norm = value * MONEY_UNITS[unit]
        else:  # ratio
            if unit not in ("%", "％"):
                continue
            norm = value
        ym = YEAR_RE.search(window)
        if not ym:
            continue  # no year in window → cannot cluster reliably (plan §4.2)
        year = ym.group(1)
        snippet = re.sub(r"\s+", " ", text[w0:w1]).strip()
        clusters.setdefault((canon, year), []).append(
            Obs(norm, _line_of(text, m.start()), snippet))
    return clusters


def _distinct_groups(values: List[float], tol: float) -> List[float]:
    """Greedy grouping: representatives that pairwise differ by > tol."""
    reps: List[float] = []
    for v in sorted(values):
        if not any(abs(v - r) / max(abs(v), abs(r), 1e-9) <= tol for r in reps):
            reps.append(v)
    return reps


def find_conflicts(clusters, tol: float) -> List[dict]:
    conflicts = []
    for (canon, year), obs in clusters.items():
        vals = [o.value for o in obs]
        reps = _distinct_groups(vals, tol)
        if len(reps) < 2:
            continue
        rng = max(vals) - min(vals)
        spread = rng / max(abs(max(vals)), abs(min(vals)), 1e-9)
        conflicts.append({
            "category": canon, "year": year, "spread": spread,
            "values": sorted(set(round(v, 4) for v in vals)),
            "locations": sorted({o.line for o in obs}),
            "obs": obs,
        })
    conflicts.sort(key=lambda c: (-c["spread"], c["category"]))
    return conflicts


# ─────────────────────────── hard gates (杠杆7) ────────────────────────

DIMENSION_MINS = [("维度一", 600), ("维度二", 800), ("维度六", 300)]
REQUIRED_SECTIONS = ["## 数字溯源汇总", "## 结构化参数"]
_DEGRADE_MARKERS = ("不适用", "未触发", "不属于控股结构", "不涉及控股")
FIRST_SCREEN_CHARS = 2000
_DECISION_CARD_NAME = "投资决策卡"
_DECISION_CARD_ITEMS = "①②③④⑤⑥⑦"

# ONE title grammar shared by every gate: ATX headings at ANY level (# … ######),
# standalone bold lines (**标题**) and indented variants. A heading written in a
# form this module cannot see is exactly how the decision-card gate used to be
# skipped entirely, so unable-to-locate is itself a gate failure.
_MD_HEADING_RE = re.compile(
    r"^[ \t]*(?P<hashes>#{1,6})[ \t]*(?P<title>[^\n]*?)[ \t]*#*[ \t]*$")
_BOLD_HEADING_RE = re.compile(
    r"^[ \t]*(?P<bold>\*\*|__)(?P<title>[^\n]*?)(?P=bold)[ \t]*[:：]?[ \t]*$")


def _heading(line: str) -> Optional[Tuple[str, int, str]]:
    """Return (kind, level, title) when *line* is a section-heading line."""
    md = _MD_HEADING_RE.match(line)
    if md and md.group("title").strip():
        return "md", len(md.group("hashes")), md.group("title").strip()
    bold = _BOLD_HEADING_RE.match(line)
    if bold and bold.group("title").strip():
        return "bold", 2, bold.group("title").strip()
    return None


def _locate_section(text: str, name: str) -> Optional[Tuple[str, int, int]]:
    """Locate a section by title fragment; returns (body, body_offset, level).

    Single source of truth for ``check_gates`` and ``check_decision_card``: the
    body runs until the next Markdown heading of the same-or-higher level (for a
    bold pseudo-heading: until the next bold heading as well).
    """
    lines = text.splitlines(keepends=True)
    offset = 0
    for idx, line in enumerate(lines):
        head = _heading(line)
        if head and name in head[2]:
            kind, level, _title = head
            body_start = offset + len(line)
            end = len(text)
            pos = body_start
            for nxt in lines[idx + 1:]:
                nhead = _heading(nxt)
                if nhead:
                    nkind, nlevel, _ = nhead
                    if nkind == "md" and nlevel <= level:
                        end = pos
                        break
                    if kind == "bold" and nkind == "bold":
                        end = pos
                        break
                pos += len(nxt)
            return text[body_start:end], body_start, level
        offset += len(line)
    return None


def _section_body(text: str, name: str) -> Optional[str]:
    located = _locate_section(text, name)
    return located[0] if located else None


def _cjk_count(s: str) -> int:
    return sum(1 for ch in s if "一" <= ch <= "鿿")


def check_gates(text: str, report_type: str = "qualitative", metrics=None) -> List[str]:
    failures = []
    if report_type == "auto":
        report_type = "qualitative" if "维度一" in text else "valuation" if "估值报告" in text.splitlines()[0] else "turtle"
    if report_type != "qualitative":
        for name in ("执行摘要", "数字溯源汇总", "结构化参数", "分析边界"):
            if name not in text:
                failures.append(f"缺少章节「{name}」")
        if not re.search(r"(?:分析|复核)日期[：:]\s*\d{4}-\d{2}-\d{2}", text):
            failures.append("缺少分析/复核日期")
        if re.search(r"(?:买入就是胜利|买入必胜|保证本金)", text):
            failures.append("包含保证式投资表述")
        # check_decision_card reports BOTH "标题无法定位" and content failures —
        # presence must never be a separate substring check (that is the bypass).
        failures.extend(check_decision_card(text, metrics))
        return failures
    for name, minimum in DIMENSION_MINS:
        body = _section_body(text, name)
        if body is None:
            if name == "维度六":  # conditional — absence is allowed
                continue
            failures.append(f"缺少章节「{name}」")
            continue
        if name == "维度六" and any(mk in body for mk in _DEGRADE_MARKERS):
            continue  # not triggered
        n = _cjk_count(body)
        if n < minimum:
            failures.append(f"「{name}」正文 {n} 字 < 门槛 {minimum} 字")
    for header in REQUIRED_SECTIONS:
        if header not in text:
            failures.append(f"缺少必备章节标题「{header}」")
    failures.extend(check_decision_card(text, metrics))
    return failures


# ── decision card helpers ─────────────────────────────────────────────

# A numeric token; masked spans (§N / P.X / dates / [src:]) are blanked first so
# "§17.5" or a date cannot masquerade as a buy point.
_BARE_NUMBER_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d.])")
_YEAR_ONLY_RE = re.compile(r"(?:19|20)\d{2}")
# "2026年7月复核" / "第4季度" are temporal references, not numeric buy points.
_TEMPORAL_SUFFIX_RE = re.compile(r"[ \t]*(?:年|月|日|号|季)")
# Money value with unit adjacent or after Markdown bold markers: **12.34** 元.
_MONEY_CANDIDATE_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d.])\s*"
    r"(?:\*{1,2}|_{1,2})?\s*(?:元/股|元|块)")
# Unit published in the column header / parenthesis, not next to the value:
# "| **③ 多少钱能买？(元)** | 22.50 |" or "单位：元/股".
_UNIT_IN_LABEL_RE = re.compile(
    r"(?:[（(]\s*(?:元/股|元|人民币)\s*[)）]|单位\s*[:：]?\s*(?:元/股|元)|元\s*/\s*股)")


def _decision_card_item_region(body: str, marker: str = "③") -> str:
    """The whole text of one decision-card item.

    Runs from the marker to the next item (④…), the next heading, a horizontal
    rule or the end of the card body. Scanning the *segment* — not just the one
    line that contains ③ — is required: a buy price can sit on a continuation
    line or carry its unit only in the column header.
    """
    idx = _DECISION_CARD_ITEMS.find(marker)
    if idx < 0:
        return ""
    match = re.search(re.escape(marker), body)
    if not match:
        return ""
    later = _DECISION_CARD_ITEMS[idx + 1:]
    lines = body[match.start():].splitlines(keepends=True)
    kept = [lines[0]]
    for line in lines[1:]:
        if any(token in line for token in later):
            break
        if re.match(r"^[ \t]*#{1,6}[ \t]", line):
            break
        if re.match(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", line):
            break
        kept.append(line)
    return "".join(kept)


def _skip_number(masked: str, match) -> bool:
    """True for years / temporal references that cannot be a numeric buy point."""
    raw = match.group(1)
    if _YEAR_ONLY_RE.fullmatch(raw):
        return True
    return bool(_TEMPORAL_SUFFIX_RE.match(masked, match.end()))


def _numeric_buy_points(region: str) -> List[float]:
    """Every non-year number in the ③ segment (unit may live in the header)."""
    masked = _mask(region)
    values: List[float] = []
    for match in _BARE_NUMBER_RE.finditer(masked):
        if _skip_number(masked, match):
            continue
        try:
            values.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return values


def _buy_price_candidates(region: str) -> List[float]:
    """Candidate 元/股 values in the ③ segment, including header-unit tables."""
    values: List[float] = []
    masked = _mask(region)
    for match in _MONEY_CANDIDATE_RE.finditer(masked):
        try:
            values.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    if _UNIT_IN_LABEL_RE.search(masked):
        for match in _BARE_NUMBER_RE.finditer(masked):
            if _skip_number(masked, match):
                continue
            try:
                value = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if value not in values:
                values.append(value)
    return values


def _fmt_values(values: List[float]) -> str:
    return "、".join(f"{value:g}" for value in values[:4])


def _unverified_buy_point_failure(region: str) -> Optional[str]:
    values = _numeric_buy_points(region)
    if not values:
        return None
    return (f"未核实模型却在决策卡③区段输出数值买点（含列头/括号单位）：{_fmt_values(values)}")


# M/R matching: an independent word boundary PLUS an equals/colon separator or an
# explicit Chinese label. A bare letter anywhere is what used to match the "R" in
# "IRR 8%" and report a fake 精算穿透回报率 mismatch.
_PAYOUT_LABEL_RE = re.compile(
    r"(?:分红)?支付率\s*(?:[（(]\s*M\s*[)）]|M(?![A-Za-z0-9]))?\s*(?:[=:：为]|[|｜])?\s*"
    r"\*{0,2}(-?\d+(?:\.\d+)?)\s*[%％]")
_PAYOUT_BARE_M_RE = re.compile(
    r"(?<![A-Za-z0-9])M(?![A-Za-z0-9])\s*[=:：]\s*\*{0,2}(-?\d+(?:\.\d+)?)\s*[%％]")
_PENETRATION_LABEL_RE = re.compile(
    r"(?:精算)?穿透回报率\s*(?:[（(]\s*(?:GG|R)\s*[)）]|(?:GG|R)(?![A-Za-z0-9]))?\s*"
    r"(?:[=:：为]|[|｜])?\s*\*{0,2}(-?\d+(?:\.\d+)?)\s*[%％]")
_PENETRATION_GG_RE = re.compile(
    r"(?<![A-Za-z0-9])GG(?![A-Za-z0-9])\s*(?:[=:：]|[|｜])?\s*"
    r"\*{0,2}(-?\d+(?:\.\d+)?)\s*[%％]")
_PENETRATION_BARE_R_RE = re.compile(
    r"(?<![A-Za-z0-9])R(?![A-Za-z0-9])\s*[=:：]\s*\*{0,2}(-?\d+(?:\.\d+)?)\s*[%％]")

_LABELLED_RATIOS = (
    ("payout_pct", (_PAYOUT_LABEL_RE, _PAYOUT_BARE_M_RE)),
    ("penetration_pct", (_PENETRATION_LABEL_RE, _PENETRATION_GG_RE, _PENETRATION_BARE_R_RE)),
)


def _labelled_ratio_matches(body: str, patterns) -> List["re.Match[str]"]:
    """Matches of one ratio's labels; an explicit label absorbs its bare M/R twin.

    ``支付率M = 40%`` is matched by both the Chinese-label pattern and the bare-M
    pattern — one occurrence must yield one verdict, so contained inner spans are
    dropped (outer span wins)."""
    spans = []
    for pattern in patterns:
        for match in pattern.finditer(body):
            spans.append((match.start(), match.end(), match))
    spans.sort(key=lambda item: (item[0], -item[1]))
    kept = []
    for start, end, match in spans:
        if any(k_start <= start and end <= k_end for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, match))
    return [match for _, _, match in kept]


def check_decision_card(text, metrics=None):
    failures: List[str] = []
    located = _locate_section(text, _DECISION_CARD_NAME)
    if located is None:
        # Fail closed: a card whose title cannot be located cannot be validated,
        # and silently skipping every ①–⑦ / 买点 / M / R check is the P0 bug.
        return ["第一屏缺少投资决策卡（无法定位 #/##/粗体/缩进 标题，不能跳过决策卡校验）"]
    body, body_offset, _level = located
    if body_offset > FIRST_SCREEN_CHARS:
        failures.append("投资决策卡未出现在第一屏（前 2000 字内）")
    for token in _DECISION_CARD_ITEMS:
        if token not in body:
            failures.append(f"投资决策卡缺少{token}")
    third = _decision_card_item_region(body, "③")
    unverified = metrics is None or metrics.get("status") != "verified" \
        or metrics.get("target_price") is None
    if unverified:
        failure = _unverified_buy_point_failure(third)
        if failure:
            failures.append(failure)
        if re.search(r"(?:✅|可以买|建议买入|立即买入)", body):
            details = "" if metrics is not None else "没有提供--metrics确定性证据"
            failures.append(f"未核实模型却给出买入指令{details}")
    else:
        try:
            target = float(metrics["target_price"])
        except (TypeError, ValueError):
            failures.append("decision_metrics.target_price 非数值，无法校验决策卡买点")
        else:
            candidates = _buy_price_candidates(third)
            if not any(abs(value - target) <= 0.02 for value in candidates):
                failures.append("决策卡买点与确定性target_price不一致")
    for key, patterns in _LABELLED_RATIOS:
        expected = metrics.get(key) if metrics is not None else None
        for match in _labelled_ratio_matches(body, patterns):
            if expected is None or abs(float(match[1]) - expected) > 0.02:
                failures.append(f"决策卡{key}缺少证据或与计算口径不一致")
    return failures


# ─────────────────────────── report render ────────────────────────────

EVIDENCE_NOTE = (
    "证据门说明：`AUDIT_RESULT: PASS`、`REPORT_SHA256`、`METRICS_SHA256`、"
    "`CLEANROOM_DONE` 只能证明审计/重算产物与**当前**报告、decision_metrics.json 文件一致"
    "（未复用旧产物、未被替换），**不代表独立第三方审计**，也不证明数据来源本身真实；"
    "独立审计结论仍须人工/另一 Agent 产出。"
)


def render(conflicts: List[dict], gate_failures: Optional[List[str]], tol: float,
           evidence_notes: Optional[List[str]] = None) -> str:
    out = ["# 一致性审计 — consistency_report.md\n",
           f"> 容差 {tol:.0%}；本表为 **advisory**，真错误 vs 口径差异由 numeric_audit 裁定。\n"]
    out.append("## 跨段数值冲突\n")
    if not conflicts:
        out.append("✅ 未发现跨段数值冲突。\n")
    else:
        out.append(f"发现 **{len(conflicts)}** 处潜在冲突：\n")
        out.append("| 指标 | 年份 | 冲突数值 | 相对差 | 出现行号 |")
        out.append("| --- | --- | --- | ---: | --- |")
        for c in conflicts:
            vals = " / ".join(f"{v:g}" for v in c["values"])
            locs = ", ".join(str(l) for l in c["locations"])
            out.append(f"| {c['category']} | {c['year']} | {vals} | {c['spread']:.1%} | {locs} |")
        out.append("")
    if gate_failures is not None:
        out.append("## 硬门槛检查（--gates）\n")
        if not gate_failures:
            out.append("✅ 全部硬门槛通过。\n")
        else:
            out.append(f"❌ **{len(gate_failures)}** 项门槛未达标：\n")
            for f in gate_failures:
                out.append(f"- {f}")
            out.append("")
        for note in (evidence_notes or []):
            out.append(f"> {note}\n")
    out.append("---\n*consistency_report.md · 由 scripts/report_consistency.py 生成 · 内部工件（非交付物）*")
    return "\n".join(out) + "\n"


# ─────────────────────────────── CLI ──────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="定性报告跨段一致性 + 硬门槛审计")
    ap.add_argument("--report", required=True, help="qualitative_report.md 路径")
    ap.add_argument("--output", help="consistency_report.md 输出路径（可选）")
    ap.add_argument("--tolerance", type=float, default=0.05, help="同类判定容差（默认 0.05）")
    ap.add_argument("--gates", action="store_true", help="附加执行硬门槛检查（杠杆7）")
    ap.add_argument("--report-type", choices=("auto", "qualitative", "turtle", "valuation"), default="auto")
    ap.add_argument("--metrics", help="decision_metrics.json：确定性买点/M/R证据（完整交付必填）")
    ap.add_argument("--require-audit", help="numeric_audit产物：AUDIT_RESULT: PASS + 绑定当前报告/metrics 的 SHA256")
    ap.add_argument("--require-cleanroom", help="独立重算产物：CLEANROOM_DONE + 绑定当前报告/metrics 的 SHA256")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.report):
        sys.stderr.write(f"[report_consistency] 文件不存在: {args.report}\n")
        return 2
    with open(args.report, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        sys.stderr.write("[report_consistency] 报告为空。\n")
        return 2

    conflicts = find_conflicts(extract(text), args.tolerance)
    report_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    metrics = None
    metrics_digest = None
    try:
        if args.metrics:
            with open(args.metrics, encoding="utf-8") as stream:
                metrics = json.load(stream)
            with open(args.metrics, "rb") as stream:
                metrics_digest = hashlib.sha256(stream.read()).hexdigest()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"[report_consistency] 无法读取metrics: {exc}\n")
        return 2
    gate_failures = check_gates(text, args.report_type, metrics) if args.gates else None
    evidence_notes: List[str] = []
    if args.require_audit or args.require_cleanroom:
        evidence_notes.append(EVIDENCE_NOTE)
    if args.require_audit:
        gate_failures = gate_failures or []
        try:
            with open(args.require_audit, encoding="utf-8") as stream:
                audit = stream.read()
        except OSError:
            gate_failures.append("numeric_audit产物缺失，不能交付为完整分析")
        else:
            if not re.search(r"^AUDIT_RESULT:\s*PASS\s*$", audit, re.M):
                gate_failures.append("numeric_audit未通过，不能交付为完整分析")
            if not re.search(r"^REPORT_SHA256:\s*" + report_digest + r"\s*$", audit, re.M):
                gate_failures.append("审计未绑定当前报告SHA256，可能复用了旧报告/其他报告的审计")
            if args.metrics:
                if not re.search(r"^METRICS_SHA256:\s*" + (metrics_digest or "") + r"\s*$",
                                 audit, re.M):
                    gate_failures.append("审计未绑定当前decision_metrics.json的SHA256")
    if args.require_cleanroom:
        gate_failures = gate_failures or []
        try:
            with open(args.require_cleanroom, encoding="utf-8") as stream:
                cleanroom = stream.read()
        except OSError:
            gate_failures.append("独立重算产物缺失，不能交付为完整流程")
        else:
            # CLEANROOM_DONE alone is self-certification: it must be BOUND to the
            # current report and metrics, else a stale clean-room passes a new report.
            if not re.search(r"^CLEANROOM_DONE(?:\s*[:：].*)?\s*$", cleanroom, re.M):
                gate_failures.append("独立重算产物未完成（缺 CLEANROOM_DONE 标记）")
            if not re.search(r"^REPORT_SHA256:\s*" + report_digest + r"\s*$", cleanroom, re.M):
                gate_failures.append(
                    "独立重算产物未绑定当前报告SHA256，可能是旧产物或来自其他报告")
            if args.metrics:
                if not re.search(r"^METRICS_SHA256:\s*" + (metrics_digest or "") + r"\s*$",
                                 cleanroom, re.M):
                    gate_failures.append(
                        "独立重算产物未绑定当前decision_metrics.json的SHA256")
            else:
                gate_failures.append(
                    "未提供--metrics，无法校验独立重算产物的指标哈希，不能宣称完整流程通过")

    report = render(conflicts, gate_failures, args.tolerance, evidence_notes)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
    else:
        sys.stdout.write(report)

    n_gate = len(gate_failures) if gate_failures else 0
    sys.stderr.write(
        f"[report_consistency] 冲突 {len(conflicts)} 处 · 门槛失败 {n_gate} 项\n")
    return 1 if (conflicts or n_gate) else 0


if __name__ == "__main__":
    sys.exit(main())
