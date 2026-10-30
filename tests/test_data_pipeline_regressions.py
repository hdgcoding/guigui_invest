import json
from pathlib import Path

import pandas as pd
import pytest

from quality_control import parse_sections, parse_matrix, find_item, _dividends_by_year, build_report
from report_consistency import check_gates, extract, check_decision_card, main as consistency_main
from pdf_preprocessor import write_text_exports, SECTION_NAMES, run_pipeline


def test_em_da_aliases_not_added_twice(financial_client, monkeypatch):
    raw = financial_client._store["cashflow"].drop(columns=["depr_fa_coga_dpba", "amort_intang_assets", "lt_amort_deferred_exp"])
    em = pd.DataFrame({"REPORT_DATE": ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"],
                       "FA_IR_DEPR": [10e6]*4, "OILGAS_BIOLOGY_DEPR": [10e6]*4,
                       "IA_AMORTIZE": [2e6]*4, "LPE_AMORTIZE": [1e6]*4})
    calls = []
    def cached(endpoint, fetch, key=None):
        calls.append(endpoint)
        return em
    monkeypatch.setattr(financial_client, "_cached_call", cached)
    out = financial_client._supplement_da("600001.SH", raw)
    assert out.iloc[0]["depr_fa_coga_dpba"] == 10e6
    assert calls == ["cashflow_da_em"]
    assert out.iloc[0]["da_source"] == "Eastmoney"


def test_ths_da_fallback_parses_units(financial_client, monkeypatch):
    raw = pd.DataFrame([{"end_date": "20251231", "n_cashflow_act": 1e9}])
    ths = pd.DataFrame([{"报告期": "2025-12-31", "固定资产折旧、油气资产折耗、生产性生物资产折旧": "192.03亿",
                         "无形资产摊销": "3.05亿", "长期待摊费用摊销": "2155.31万"}])
    monkeypatch.setattr(financial_client, "_cached_call", lambda endpoint, fetch, key=None:
                        ths if endpoint.endswith("ths") else pd.DataFrame())
    out = financial_client._supplement_da("600001.SH", raw)
    assert out.iloc[0]["depr_fa_coga_dpba"] == pytest.approx(192.03e8)
    assert out.iloc[0]["lt_amort_deferred_exp"] == pytest.approx(2155.31e4)


def test_statement_scope_versions_preserved(financial_client, monkeypatch):
    raw = pd.DataFrame({"报告日": ["2025-12-31"]*3, "营业总收入": [100, 200, 10],
        "类型": ["合并期末", "合并期末", "母公司期末"],
        "公告日期": ["20260331", "20261024", "20261024"]})
    monkeypatch.setattr(financial_client, "_cached_call", lambda endpoint, fetch, key=None: raw)
    out = financial_client._fetch_sina_statement("600001.SH", "利润表", {"revenue": "营业总收入"})
    assert len(out) == 1 and out.iloc[0]["revenue"] == 200
    assert out.iloc[0]["ann_date"] == "2026-10-24"
    parent = financial_client._fetch_sina_statement("600001.SH", "利润表", {"revenue": "营业总收入"}, scope="parent")
    assert parent.iloc[0]["revenue"] == 10


def test_financial_indicators_derive_partial_fields_without_fake_weighted_roe(financial_client):
    partial = pd.DataFrame([{"end_date": "20251231", "roe_waa": None, "grossprofit_margin": 55.0}])
    result = financial_client._merge_derived_indicators(partial)
    row = result.iloc[0]
    assert row["grossprofit_margin"] == 55
    assert row["roe"] == 20 and pd.isna(row["roe_waa"])
    assert row["debt_to_assets"] == pytest.approx(100 / 3)


def test_quote_date_mismatch_blocks_share_inference(financial_client, monkeypatch):
    monkeypatch.setattr(financial_client, "_fetch_name_and_list_date", lambda code: ("测试", "20000101"))
    monkeypatch.setattr(financial_client, "_fetch_latest_daily", lambda num: {"close": 10, "date": "20261030"})
    monkeypatch.setattr(financial_client, "_fetch_valuation", lambda num: {"total_mv_wan": 100000, "date": "20261029"})
    md = financial_client.get_basic_info("600001.SH")
    row = financial_client._store["basic_info"].iloc[0]
    assert row["total_share"] is None and row["total_mv"] is None
    assert row["circ_mv"] is None and "未核实" in md


def test_quality_control_reads_actual_collector_contract(financial_client):
    periods = financial_client._store["income"]
    # Use the production renderer; source mocked at normalized-frame boundary.
    financial_client._fetch_sina_statement = lambda code, symbol, mapping: periods
    income = financial_client.get_income("600001.SH")
    cols, matrix = parse_matrix(income)
    assert matrix[find_item(matrix, "营业收入")]["2025"] == 100
    assert find_item({"归母股东权益": {}}, "归母所有者权益") == "归母股东权益"
    assert find_item({"资产总计": {}}, "总资产") == "资产总计"
    md = build_report({"3": income}, [10, 20], [1])
    assert "100.00" in md


def test_dividend_markdown_parser_uses_totals_and_complete_years():
    block = """| 报告期 | 每股现金分红(税前,元) | 分红总额(百万元) |
| --- | --- | --- |
| 20260630 | 0.98 | 98 |
| 20251231 | 1.00 | 100 |
| 20250630 | 0.50 | 50 |
*完整分红年度: 2025；仅完整年度可用于M。*
"""
    assert _dividends_by_year(block) == {"2025": 150}
    assert _dividends_by_year("| 年度 | DPS |\n| --- | --- |\n| 2025 | 5 |") == {}


def test_missing_dividend_amount_not_normalized_to_zero(financial_client, monkeypatch):
    from financial_contracts import annual_dividends
    raw = pd.DataFrame([{"报告期": "2025-12-31", "现金分红-现金分红比例": None,
                         "总股本": 1e8, "方案进度": "实施分配", "除权除息日": pd.NaT}])
    monkeypatch.setattr(financial_client, "_cached_call", lambda endpoint, fetch, key=None: raw)
    md = financial_client.get_dividends("600001.SH")
    assert "nan" not in md.lower()
    assert annual_dividends(financial_client._store["dividends_all"], "2026-10-30") == []


def test_pdf_names_match_keyword_meanings():
    assert [SECTION_NAMES[key] for key in ("P2", "P3", "P4", "P6", "P13")] == [
        "受限资产", "应收账款账龄", "关联交易", "或有负债与承诺", "非经常性损益"]


def test_pdf_parts_actual_count_manifest_and_stale_cleanup(tmp_path):
    output = str(tmp_path / "sections.md")
    pages = [(1, "正文"*200), (2, "正文"*200), (3, "")]
    files = write_text_exports(pages, {}, {}, output, full_text=True, full_text_part_chars=200)
    count = len(files) - 1
    assert count > 2
    for idx, path in enumerate(files[1:], 1):
        assert f"分片 {idx}/{count}" in Path(path).read_text()
    manifest = json.loads((tmp_path / "sections_manifest.json").read_text())
    assert manifest["empty_pages"] == [3]
    assert all(item["pages"] for item in manifest["parts"])
    user_file = tmp_path / "sections_fulltext_part99.md"
    user_file.write_text("USER AUTHORED", encoding="utf-8")
    write_text_exports([(1, "正文")], {}, {}, output, full_text=True, full_text_part_chars=200)
    assert not (tmp_path / "sections_fulltext_part2.md").exists()
    assert user_file.read_text() == "USER AUTHORED"


def test_scanned_pdf_cannot_return_success(tmp_path, monkeypatch):
    import config
    import pdf_preprocessor as pdf
    monkeypatch.setattr(config, "validate_pdf", lambda path: (True, ""))
    # run_pipeline imports scripts.config when project root is on sys.path.
    import scripts.config as script_config
    monkeypatch.setattr(script_config, "validate_pdf", lambda path: (True, ""))
    monkeypatch.setattr(pdf, "extract_all_pages", lambda *args, **kwargs: [(1, ""), (2, "   ")])
    with pytest.raises(RuntimeError, match="OCR"):
        run_pipeline("scan.pdf", str(tmp_path / "sections.json"))


def _report(card="待核实"):
    return ("# 测试龟龟分析报告\n分析日期：2026-10-30\n## 🎯 投资决策卡\n"
            "| ① 现在能买吗 | 未核实 |\n| ② 为什么 | 资料不足 |\n"
            f"| ③ 多少钱能买 | {card} |\n| ④ 确认信号 | 复核现金 |\n"
            "| ⑤ 已持有 | 复核风险 |\n| ⑥ 风险 | 数据口径 |\n| ⑦ 何时复核 | 补齐后 |\n"
            "## 执行摘要\n降级\n## 数字溯源汇总\n无\n## 结构化参数\nnull\n## 分析边界\n未审计\n")


def test_report_type_aware_gate_does_not_require_qualitative_dimensions():
    assert check_gates(_report(), "turtle", {"status": "unverified"}) == []
    assert any("维度一" in failure for failure in check_gates(_report(), "qualitative"))


def test_unverified_card_cannot_invent_buy_range():
    failures = check_gates(_report("✅ 24–27 元"), "turtle", {"status": "unverified", "target_price": None})
    assert any("买点" in failure for failure in failures)


def test_verified_card_must_match_target():
    assert check_decision_card(_report("20.00元"), {"status": "verified", "target_price": 20}) == []
    assert check_decision_card(_report("24.00元"), {"status": "verified", "target_price": 20})


def test_regex_does_not_assign_generation_to_profit():
    observations = extract("2025归母净利润345.03亿元，发电量3071.94亿千瓦时。")
    assert [obs.value for obs in observations[("归母净利润", "2025")]] == [345.03]


def test_missing_audit_artifact_fails_complete_delivery(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(_report())
    assert consistency_main(["--report", str(path), "--gates", "--report-type", "turtle",
                             "--require-audit", str(tmp_path / "missing.md")]) == 1


def test_audit_is_bound_to_current_report_hash(tmp_path):
    import hashlib
    path = tmp_path / "report.md"
    audit = tmp_path / "audit.md"
    path.write_text(_report())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    audit.write_text(f"AUDIT_RESULT: PASS\nREPORT_SHA256: {digest}\n")
    args = ["--report", str(path), "--gates", "--report-type", "turtle", "--require-audit", str(audit)]
    assert consistency_main(args) == 0
    path.write_text(_report() + "\n修改后的报告\n")
    assert consistency_main(args) == 1


def test_cleanroom_required_when_claiming_complete_flow(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(_report())
    assert consistency_main(["--report", str(path), "--gates", "--report-type", "turtle",
                             "--require-cleanroom", str(tmp_path / "missing.md")]) == 1
