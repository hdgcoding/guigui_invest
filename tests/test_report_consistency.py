"""Hard-gate regression tests (P0/P1 bypasses fixed in report_consistency/quality_control).

Pinned regressions — each test fails on the pre-fix scripts:

* decision-card title written as ``**粗体**`` / ``#`` / indented / ``###`` used to
  make ``check_decision_card`` return early, silently skipping ①–⑦ / 买点 / 买入指令;
* a ③ buy price whose unit sits only in the column header (``(元)``) bypassed the
  unverified-metrics check because only the line containing ③ was scanned;
* a bold target price (``**12.34** 元``) and the ``R`` inside ``IRR 8%`` were false
  positives of the verified-target / M-R matchers;
* ``AUDIT_RESULT: PASS`` / ``CLEANROOM_DONE`` alone were self-certifying: no binding
  to the current report/metrics SHA256;
* 万元 was not normalised and a bare 元 could not be separated from 元/股 prices;
* quality_control CM§3 silently used §12 摊薄 ROE instead of 加权 ROE.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from quality_control import build_report, find_weighted_roe
from report_consistency import (
    check_decision_card,
    check_gates,
    extract,
    find_conflicts,
    main as consistency_main,
)

STRATEGY_METRICS = {
    "status": "verified",
    "target_price": 20.0,
    "payout_pct": 35.0,
    "penetration_pct": 6.1,
}
UNVERIFIED_METRICS = {"status": "unverified", "target_price": None}

CARD_ITEMS = [
    ("① 现在能买吗？", "本报告不能确认买入条件"),
    ("② 为什么？", "证据链未闭合，状态待核实"),
    ("③ 多少钱能买？", "待核实"),
    ("④ 到价时的确认信号", "补齐数据并完成独立审计"),
    ("⑤ 如果已经持有？", "按成本/仓位/期限复核"),
    ("⑥ 最大的风险", "数据口径未闭合"),
    ("⑦ 什么时候重新看？", "新定期报告披露后"),
]

HEADINGS = [
    "## 🎯 投资决策卡",
    "### 🎯 投资决策卡",
    "# 🎯 投资决策卡",
    "**🎯 投资决策卡**",
    "   ## 🎯 投资决策卡",
    "\t## 🎯 投资决策卡",
]


# ─────────────────────────── report builders ──────────────────────────

def _card_body(third="待核实", third_label="③ 多少钱能买？", continuation=""):
    items = list(CARD_ITEMS)
    items[2] = (third_label, third)
    lines = [f"| **{q}** | {a} |" for q, a in items[:3]]
    if continuation:
        lines.append(continuation)
    lines += [f"| **{q}** | {a} |" for q, a in items[3:]]
    return "\n".join(lines)


def _report(heading="## 🎯 投资决策卡", third="待核实", third_label="③ 多少钱能买？",
            continuation="", extra=""):
    return (
        "# 测试龟龟分析报告\n\n"
        "分析日期：2026-10-30\n\n"
        f"{heading}\n\n"
        "| 问题 | 答案 |\n|---|---|\n"
        f"{_card_body(third, third_label, continuation)}\n"
        f"{extra}\n\n"
        "## 执行摘要\n\n降级报告：数据未核实。\n\n"
        "## 数字溯源汇总\n\n无。\n\n"
        "## 结构化参数\n\nnull\n\n"
        "## 分析边界\n\n未审计。\n"
    )


# ─────────────── P0-1: decision-card title must never disable gating ───────────

@pytest.mark.parametrize("heading", HEADINGS)
def test_heading_variants_do_not_skip_decision_card_checks(heading):
    # Unverified metrics + numeric buy point: must fail no matter the title syntax.
    report = _report(heading=heading, third="24–27 元")
    failures = check_gates(report, "turtle", UNVERIFIED_METRICS)
    assert any("买点" in failure for failure in failures), failures


@pytest.mark.parametrize("heading", HEADINGS)
def test_heading_variants_still_accept_a_compliant_card(heading):
    assert check_gates(_report(heading=heading), "turtle", UNVERIFIED_METRICS) == []


@pytest.mark.parametrize("heading", HEADINGS)
def test_gates_and_decision_card_share_one_locator(heading):
    report = _report(heading=heading, third="24–27 元")
    card_failures = check_decision_card(report, UNVERIFIED_METRICS)
    assert "未核实模型却在决策卡③区段输出数值买点（含列头/括号单位）：24、27" in card_failures
    assert check_gates(report, "turtle", UNVERIFIED_METRICS) == card_failures


def test_unlocatable_decision_card_fails_closed():
    report = _report().replace("## 🎯 投资决策卡", "🎯 投资决策卡（此处不是 markdown 标题）")
    failures = check_gates(report, "turtle", UNVERIFIED_METRICS)
    assert any("投资决策卡" in failure and "无法定位" in failure for failure in failures), failures
    assert check_decision_card(report, UNVERIFIED_METRICS)


def test_gates_without_metrics_still_reject_numeric_buy_point():
    report = _report(third="24–27 元")
    failures = check_gates(report, "turtle")  # no decision_metrics.json at all
    assert any("买点" in failure for failure in failures), failures


def test_gates_without_metrics_accept_a_compliant_card():
    # Mirrors the real degraded reports (status=unverified, no metrics artifact).
    assert check_gates(_report(), "turtle") == []


def test_decision_card_after_first_screen_fails():
    padding = "前置说明。" * 500  # > 2000 chars before the card
    report = _report().replace("# 测试龟龟分析报告\n", padding + "\n", 1)
    failures = check_decision_card(report, UNVERIFIED_METRICS)
    assert any("第一屏" in failure for failure in failures), failures


# ─────────── P0-2: scan the whole ③ segment, header unit included ─────────────

@pytest.mark.parametrize("third_label,third", [
    ("③ 多少钱能买？(元)", "22.50"),
    ("③ 多少钱能买？（元/股）", "**22.50**"),
    ("③ 多少钱能买？单位：元", "22.50"),
])
def test_buy_price_with_unit_only_in_column_header_is_caught(third_label, third):
    # Old code only scanned the line containing ③ and only a value directly
    # followed by 元 — "22.50" separated from "(元)" by a table cell escaped.
    report = _report(third_label=third_label, third=third)
    failures = check_gates(report, "turtle", UNVERIFIED_METRICS)
    assert any("买点" in failure for failure in failures), failures


def test_buy_price_on_a_continuation_line_of_item_three_is_caught():
    report = _report(third="待核实", continuation="> 区间参考 24–27 元，回调后复核")
    failures = check_gates(report, "turtle", UNVERIFIED_METRICS)
    assert any("买点" in failure for failure in failures), failures


def test_item_three_scan_stops_at_item_four():
    report = _report(third="待核实").replace(
        "| **④ 到价时的确认信号** | 补齐数据并完成独立审计 |",
        "| **④ 到价时的确认信号** | 回调 10% 后再复核 |")
    assert not any("买点" in failure for failure in check_decision_card(report, UNVERIFIED_METRICS))


def test_temporal_reference_is_not_a_numeric_buy_point():
    # "2026年7月" / "2026-07" are dates; only a price-like number is a bypass.
    report = _report(third="待核实，2026年7月复核，2026-07 前不设买点")
    assert not any("买点" in failure for failure in check_decision_card(report, UNVERIFIED_METRICS))


# ─────────────── P1-3: bold target / IRR false positives ──────────────────────

def test_bold_target_price_is_not_a_false_positive():
    report = _report(third="**12.34** 元")
    metrics = dict(STRATEGY_METRICS, target_price=12.34)
    assert check_decision_card(report, metrics) == []


def test_bold_target_price_mismatch_is_still_caught():
    report = _report(third="**12.34** 元")
    metrics = dict(STRATEGY_METRICS, target_price=12.50)
    assert any("target_price" in failure for failure in check_decision_card(report, metrics))


def test_bolded_and_header_unit_targets_still_have_to_match_target_price():
    report = _report(third_label="③ 多少钱能买？(元)", third="20.00")
    assert check_decision_card(report, STRATEGY_METRICS) == []
    stale = dict(STRATEGY_METRICS, target_price=25.0)
    assert any("target_price" in failure for failure in check_decision_card(report, stale))


def test_irr_is_not_mistaken_for_penetration_return():
    report = _report(third="20.00元", extra="> 参考：IRR 8% 为内部收益率示例，与精算穿透回报率无关。")
    assert not any("penetration_pct" in failure
                   for failure in check_decision_card(report, STRATEGY_METRICS))


def test_labelled_m_and_r_are_bound_to_metrics():
    good = _report(third="20.00元", extra="> 支付率M = 35.0%；精算穿透回报率 R = 6.1%")
    assert check_decision_card(good, STRATEGY_METRICS) == []
    bad_payout = good.replace("35.0%", "40.0%")
    assert any("payout_pct" in failure
               for failure in check_decision_card(bad_payout, STRATEGY_METRICS))
    bad_penetration = good.replace("6.1%", "7.0%")
    assert any("penetration_pct" in failure
               for failure in check_decision_card(bad_penetration, STRATEGY_METRICS))


def test_chinese_predicate_m_and_r_are_recognised():
    good = _report(third="20.00元", extra="> 支付率M为35.0%；精算穿透回报率R为6.1%")
    assert check_decision_card(good, STRATEGY_METRICS) == []
    assert any("payout_pct" in failure
               for failure in check_decision_card(good.replace("35.0%", "40.0%"),
                                                  STRATEGY_METRICS))


def test_bare_word_letter_does_not_masquerade_as_m_or_r():
    # "IRR" contains R; "ROE"/"PPP" style words must not be read as labelled M/R.
    report = _report(third="20.00元", extra="> IRR 8%、ROE 15%、PPP 3% 均为无关示例。")
    assert check_decision_card(report, STRATEGY_METRICS) == []


# ─────────────── P1-4: audit / cleanroom must bind current hashes ────────────

def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write(path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


def _delivery(tmp_path, third="20.00元"):
    report_text = _report(third=third)
    metrics_text = json.dumps(STRATEGY_METRICS)
    report_path = _write(tmp_path / "report.md", report_text)
    metrics_path = _write(tmp_path / "decision_metrics.json", metrics_text)
    return report_text, metrics_text, report_path, metrics_path


def _audit_text(report_text, metrics_text, result="PASS", bind_metrics=True):
    text = f"AUDIT_RESULT: {result}\nREPORT_SHA256: {_sha256_text(report_text)}\n"
    if bind_metrics:
        text += f"METRICS_SHA256: {_sha256_text(metrics_text)}\n"
    return text


def _cleanroom_text(report_text, metrics_text,
                    marker="CLEANROOM_DONE: 6 项已重算 / 0 项缺源",
                    report_hash=None, metrics_hash=None):
    text = (marker or "") + "\n"
    text += f"REPORT_SHA256: {report_hash or _sha256_text(report_text)}\n"
    if metrics_hash is not False:
        text += f"METRICS_SHA256: {metrics_hash or _sha256_text(metrics_text)}\n"
    return text


def _run(tmp_path, report_path, metrics_path, extra_args):
    out = tmp_path / "consistency_report.md"
    args = ["--report", report_path, "--report-type", "turtle", "--gates",
            "--metrics", metrics_path, "--output", str(out)]
    args.extend(extra_args)
    return consistency_main(args), out


def test_complete_flow_binds_hashes_and_states_it_is_not_an_independent_audit(tmp_path):
    report_text, metrics_text, report_path, metrics_path = _delivery(tmp_path)
    audit = _write(tmp_path / "final_audit.md", _audit_text(report_text, metrics_text))
    cleanroom = _write(tmp_path / "cleanroom_metrics.md", _cleanroom_text(report_text, metrics_text))
    rc, out = _run(tmp_path, report_path, metrics_path,
                   ["--require-audit", audit, "--require-cleanroom", cleanroom])
    assert rc == 0
    rendered = out.read_text(encoding="utf-8")
    assert "全部硬门槛通过" in rendered
    assert "不代表独立第三方审计" in rendered


def test_audit_must_bind_the_current_metrics_hash(tmp_path):
    report_text, metrics_text, report_path, metrics_path = _delivery(tmp_path)
    audit = _write(tmp_path / "final_audit.md",
                   _audit_text(report_text, metrics_text, bind_metrics=False))
    rc, out = _run(tmp_path, report_path, metrics_path, ["--require-audit", audit])
    assert rc == 1
    assert "decision_metrics" in out.read_text(encoding="utf-8")


def test_audit_with_stale_metrics_hash_fails(tmp_path):
    report_text, metrics_text, report_path, metrics_path = _delivery(tmp_path)
    audit = _write(tmp_path / "final_audit.md",
                   _audit_text(report_text, metrics_text, bind_metrics=False)
                   + f"METRICS_SHA256: {'0' * 64}\n")
    rc, _ = _run(tmp_path, report_path, metrics_path, ["--require-audit", audit])
    assert rc == 1


def test_audit_must_bind_the_current_report_hash(tmp_path):
    report_text, metrics_text, report_path, metrics_path = _delivery(tmp_path)
    audit = _write(tmp_path / "final_audit.md",
                   f"AUDIT_RESULT: PASS\nREPORT_SHA256: {'0' * 64}\n"
                   f"METRICS_SHA256: {_sha256_text(metrics_text)}\n")
    rc, out = _run(tmp_path, report_path, metrics_path, ["--require-audit", audit])
    assert rc == 1
    assert "当前报告SHA256" in out.read_text(encoding="utf-8")


def test_cleanroom_marker_alone_is_only_self_certification(tmp_path):
    report_text, metrics_text, report_path, metrics_path = _delivery(tmp_path)
    cleanroom = _write(tmp_path / "cleanroom_metrics.md", "CLEANROOM_DONE\n")
    rc, out = _run(tmp_path, report_path, metrics_path, ["--require-cleanroom", cleanroom])
    assert rc == 1
    rendered = out.read_text(encoding="utf-8")
    assert "未绑定当前报告SHA256" in rendered
    assert "未绑定当前decision_metrics.json的SHA256" in rendered


def test_cleanroom_with_stale_report_hash_fails(tmp_path):
    report_text, metrics_text, report_path, metrics_path = _delivery(tmp_path)
    cleanroom = _write(tmp_path / "cleanroom_metrics.md",
                       _cleanroom_text(report_text, metrics_text, report_hash="0" * 64))
    rc, out = _run(tmp_path, report_path, metrics_path, ["--require-cleanroom", cleanroom])
    assert rc == 1
    assert "旧产物" in out.read_text(encoding="utf-8")


def test_cleanroom_must_bind_the_current_metrics_hash(tmp_path):
    report_text, metrics_text, report_path, metrics_path = _delivery(tmp_path)
    cleanroom = _write(tmp_path / "cleanroom_metrics.md",
                       _cleanroom_text(report_text, metrics_text, metrics_hash="0" * 64))
    rc, out = _run(tmp_path, report_path, metrics_path, ["--require-cleanroom", cleanroom])
    assert rc == 1
    assert "未绑定当前decision_metrics.json的SHA256" in out.read_text(encoding="utf-8")


def test_cleanroom_without_metrics_cannot_claim_complete_flow(tmp_path):
    report_text = _report()  # compliant unverified card, so only the cleanroom gate fails
    report_path = _write(tmp_path / "report.md", report_text)
    cleanroom = _write(
        tmp_path / "cleanroom_metrics.md",
        "CLEANROOM_DONE: 6 项已重算 / 0 项缺源\n"
        f"REPORT_SHA256: {_sha256_text(report_text)}\n")
    out = tmp_path / "consistency_report.md"
    rc = consistency_main(["--report", report_path, "--report-type", "turtle", "--gates",
                           "--require-cleanroom", cleanroom, "--output", str(out)])
    assert rc == 1
    assert "未提供--metrics" in out.read_text(encoding="utf-8")


# ─────────────── P1-5: 万元 / bare 元 unit classification ────────────────────

def test_wan_yuan_is_normalised_to_yi():
    clusters = extract("2025年营业收入 4,052,900 万元。")
    assert clusters[("营业收入", "2025")][0].value == pytest.approx(405.29)


def test_baiwan_yuan_is_normalised_to_yi():
    clusters = extract("2025年营业收入 40,528.51 百万元。")
    assert clusters[("营业收入", "2025")][0].value == pytest.approx(405.2851)


def test_bare_yuan_is_not_treated_as_a_yi_amount():
    # 345.03 元 next to 归母净利润 would otherwise be a 1e-8 unit bug.
    clusters = extract("2025年归母净利润 345.03 元。")
    assert ("归母净利润", "2025") not in clusters


def test_bare_yuan_is_per_share_only_inside_price_windows():
    clusters = extract("2025年目标价 22.50 元。")
    assert clusters[("目标价", "2025")][0].value == pytest.approx(22.50)


def test_per_share_and_money_categories_do_not_mix():
    clusters = extract("2025年归母净利润 345.03 亿元；2025年目标价 22.50 元。")
    assert [obs.value for obs in clusters[("归母净利润", "2025")]] == [345.03]
    assert [obs.value for obs in clusters[("目标价", "2025")]] == [22.50]


def test_wan_yuan_unit_mixing_is_a_cross_passage_conflict():
    text = "2025年营业收入 405.29 亿元。\n\n另处：2025年营业收入 40,529 万元。"
    conflicts = find_conflicts(extract(text), 0.05)
    assert any(conflict["category"] == "营业收入" for conflict in conflicts), conflicts


def test_per_share_price_conflict_is_detected():
    text = "2025年目标价 22.50 元。\n\n2025年目标价 25.00 元。"
    conflicts = find_conflicts(extract(text), 0.05)
    assert any(conflict["category"] == "目标价" for conflict in conflicts), conflicts


# ─────────────── P1-6: CM§3 must use weighted ROE (摊薄 forbidden) ───────────

_INCOME_SECTION = (
    "## 3. 合并利润表\n"
    "| 项目 | 2025 | 2024 |\n"
    "| --- | --- | --- |\n"
    "| 营业收入 | 100 | 90 |\n"
    "| 归母净利润 | 20 | 18 |\n"
)

_FIN_BOTH = (
    "## 12. 关键财务指标\n"
    "| 指标 | 2025 | 2024 |\n"
    "| --- | --- | --- |\n"
    "| ROE 摊薄 (%) | 9.11 | 8.00 |\n"
    "| ROE 加权 (%) | 12.00 | 10.00 |\n"
    "| 毛利率 (%) | 30.00 | 29.00 |\n"
)

_FIN_DILUTED_ONLY = (
    "## 12. 关键财务指标\n"
    "| 指标 | 2025 | 2024 |\n"
    "| --- | --- | --- |\n"
    "| ROE 摊薄 (%) | 9.11 | 8.00 |\n"
)


def test_find_weighted_roe_prefers_weighted_and_never_diluted():
    assert find_weighted_roe({"ROE 摊薄 (%)": {}, "ROE 加权 (%)": {}}) == "ROE 加权 (%)"
    assert find_weighted_roe({"加权净资产收益率(%)": {}, "ROE 摊薄 (%)": {}}) == "加权净资产收益率(%)"
    assert find_weighted_roe({"ROE 摊薄 (%)": {}}) is None
    assert find_weighted_roe({"ROE(%)": {}}) is None


def test_cm3_uses_weighted_roe_not_diluted():
    md = build_report({"3": _INCOME_SECTION, "12": _FIN_BOTH}, [10, 20], [1])
    roe_line = next(line for line in md.splitlines() if "ROE加权" in line)
    assert "11.00" in roe_line  # mean(12.00, 10.00), NOT mean(9.11, 8.00)
    assert "8.55" not in roe_line


def test_cm3_marks_weighted_roe_missing_instead_of_silently_using_diluted():
    md = build_report({"3": _INCOME_SECTION, "12": _FIN_DILUTED_ONLY}, [10, 20], [1])
    roe_line = next(line for line in md.splitlines() if "ROE加权" in line)
    assert "未核实" in roe_line
    assert "不得用摊薄" in md
    diluted_mean = (9.11 + 8.00) / 2  # 8.555 must not be presented as ROE
    assert f"{diluted_mean:.2f}" not in roe_line


def test_cm3_marks_roe_missing_when_section12_absent():
    md = build_report({"3": _INCOME_SECTION}, [10, 20], [1])
    roe_line = next(line for line in md.splitlines() if "ROE加权" in line)
    assert "未核实" in roe_line
    assert "不得用摊薄" in md
