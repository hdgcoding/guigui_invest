"""Tests for scripts/report_to_html.py.

Covers:
- 当前 turtle 报告结构解析：投资决策卡（七项，第一屏）、较上期变化、执行摘要、
  D1–D6、数字溯源汇总、结构化参数、分析边界，且不再渲染旧模板写死章节；
- XSS 加固：markdown 原始 HTML 经白名单 sanitize、Jinja2 autoescape 生效，
  <script>/<iframe>/onerror/javascript: 一律被中和；
- 旧 qualitative_report.md 结构兼容（维度卡片、KPI 快照、附录）；
- 正常 Markdown（标题/表格/列表/加粗/代码块）仍正确渲染。

真实报告在 output/ 下（gitignored）；缺失时相关用例 skip，其余用例仍执行。
"""

from pathlib import Path

import pytest

import report_to_html
from report_to_html import (
    build_html,
    extract_kpi_cards,
    md_to_html,
    parse_report,
    sanitize_html,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "shared" / "qualitative" / "templates" / "dashboard.html"
FRAMEWORK_PATH = ROOT / "shared" / "qualitative" / "references" / "framework_guide.md"

REAL_REPORTS = [
    ROOT / "output" / "601088_中国神华" / "中国神华_601088_分析报告.md",
    ROOT / "output" / "600900_长江电力" / "长江电力_600900_分析报告.md",
]

# 投资决策卡七项
DECISION_ITEMS = [
    "现在能买吗",
    "为什么",
    "多少钱能买",
    "到价时的确认信号",
    "如果已经持有",
    "最大的风险",
    "什么时候重新看",
]

# 当前报告必须保留的关键章节
REQUIRED_SECTIONS = [
    "执行摘要",
    "较上期变化",
    "定性分析",
    "定量分析",
    "深度总结",
    "数字溯源汇总",
    "结构化参数",
    "分析边界",
]

# 旧模板写死的章节，当前 turtle 报告不应再渲染
STALE_SECTIONS = [
    "Quality Snapshot",
    "Greenwald",
    "双层护城河",
    "评级标准定义",
]

LEGACY_REPORT = """# 定性分析 — 测试公司 (600001.SH)

*生成时间: 2026-01-01*

## 执行摘要

公司具备**强**护城河，现金流稳定。

## 维度一：商业模式与资本特征（D1）

先款后货，现金流好。

**综合评价：强**

## 维度二：竞争优势与护城河（D2）

### 行业地图

行业集中度高，龙头优势明显。

综合评价：较强

## 结构化参数

| 参数 | 值 |
|---|---|
| moat_rating | 强 |
| moat_sustainability | 高可持续 |
| roe_5y_avg | 18.5 |
| management_rating | 优秀 |

## 总结与投资启示

一句话最终结论：值得长期跟踪。

## 数字溯源汇总

| 正文数字 | 来源 |
|---|---|
| ROE | 年报 |
"""

XSS_REPORT = """# 恶意公司（000001.SZ）龟龟投资策略分析报告

*分析日期：2026-10-30*

<script>alert('preamble-xss')</script>

## 🎯 投资决策卡

| 问题 | 答案 |
|---|---|
| **① 现在能买吗？** | 待核实 <script>alert(1)</script> |

## 执行摘要

正常段落 **加粗文本**。

<img src=x onerror="alert(1)">

<iframe src="https://evil.example"></iframe>

[click](javascript:alert(1))

<a href="javascript:alert(1)" onclick="alert(1)">bad link</a>

## 一、定性分析（D1–D6 精要）

**D1 商业模式：<script>alert(2)</script>。** 正常内容 D1。

**D2 护城河：正常。** 正常内容 D2。

**D3 外部环境：正常。** 正常内容 D3。

**D4 管理层与治理：正常。** 正常内容 D4。

**D5 MD&A：正常。** 正常内容 D5。

**D6 控股结构：正常。** 正常内容 D6。
"""


@pytest.fixture(scope="module")
def template_text():
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def framework_text():
    if not FRAMEWORK_PATH.exists():
        pytest.skip(f"framework guide missing: {FRAMEWORK_PATH}")
    return FRAMEWORK_PATH.read_text(encoding="utf-8")


def _report_id(path):
    return path.parent.name


# ---------------------------------------------------------------------------
# 当前 turtle 报告结构
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("report_path", REAL_REPORTS, ids=_report_id)
def test_parse_current_turtle_report(report_path):
    if not report_path.exists():
        pytest.skip(f"real report not available: {report_path}")

    parsed = parse_report(report_path.read_text(encoding="utf-8"))

    assert parsed["report_kind"] == "turtle"
    assert parsed["is_legacy_qualitative"] is False
    assert parsed["company_name"]
    assert parsed["stock_code"].endswith((".SH", ".SZ"))

    # 投资决策卡是第一个渲染分节（第一屏），且七项齐全
    first_section = parsed["sections"][0]
    assert first_section["kind"] == "decision"
    assert "投资决策卡" in first_section["title"]
    assert parsed["decision_card"] == first_section["content"]
    for item in DECISION_ITEMS:
        assert item in parsed["decision_card"]
    # 表头 + 七项 = 8 行
    assert parsed["decision_card"].count("<tr>") == 8

    # D1–D6 解析为 6 个维度
    assert len(parsed["dimensions"]) == 6
    dim_titles = " ".join(dim["title"] for dim in parsed["dimensions"])
    for idx in range(1, 7):
        assert f"D{idx}" in dim_titles
    # 维度正文不丢失
    assert "定性分析" in parsed["sections"][3]["title"] or any(
        "定性分析" in sec["title"] for sec in parsed["sections"]
    )
    assert all(dim["content"].strip() for dim in parsed["dimensions"])

    # 关键章节全部保留（不静默丢弃）
    section_titles = " ".join(sec["title"] for sec in parsed["sections"])
    for keyword in REQUIRED_SECTIONS:
        assert keyword in section_titles, f"missing section: {keyword}"
    assert len(parsed["sections"]) == 9

    # 结构化参数 / 数字溯源 / 分析边界有内容
    assert "decision_status" in parsed["parameters_raw"]
    assert parsed["parameters_table"]
    assert parsed["traceability"]
    assert parsed["boundary"]


@pytest.mark.parametrize("report_path", REAL_REPORTS, ids=_report_id)
def test_render_current_turtle_report(report_path, template_text, framework_text):
    if not report_path.exists():
        pytest.skip(f"real report not available: {report_path}")

    md_text = report_path.read_text(encoding="utf-8")
    html, stats = build_html(
        md_text,
        template_text,
        appendix_md=framework_text,
        standalone_css="/* test css */",
    )

    assert stats["has_decision_card"] is True
    assert stats["dimensions"] == 6
    assert stats["is_legacy_qualitative"] is False
    assert stats["has_executive_summary"] is True
    assert stats["has_changes"] is True
    assert stats["has_traceability"] is True
    assert stats["has_parameters"] is True
    assert stats["has_boundary"] is True
    # 自动探测的旧附录不应污染当前 turtle 报告
    assert stats["has_appendix"] is False

    assert "投资决策卡" in html
    for item in DECISION_ITEMS:
        assert item in html
    for keyword in REQUIRED_SECTIONS:
        assert keyword in html, f"missing rendered section: {keyword}"
    for stale in STALE_SECTIONS:
        assert stale not in html, f"stale template section leaked: {stale}"

    # 第一屏：决策卡出现在执行摘要之前
    assert html.index("投资决策卡") < html.index("执行摘要")
    assert "/* test css */" in html


def test_main_cli_renders_real_turtle_report(tmp_path):
    """CLI 端到端：真实 turtle 报告 → HTML，决策卡七项存在、旧章节不出现。"""
    report_path = REAL_REPORTS[0]
    if not report_path.exists():
        pytest.skip(f"real report not available: {report_path}")

    out_path = tmp_path / "report.html"
    report_to_html.main([
        "--input", str(report_path),
        "--output", str(out_path),
        "--standalone",
    ])

    html = out_path.read_text(encoding="utf-8")
    assert "投资决策卡" in html
    for item in DECISION_ITEMS:
        assert item in html
    for keyword in REQUIRED_SECTIONS:
        assert keyword in html
    for stale in STALE_SECTIONS:
        assert stale not in html
    assert "<script" not in html.lower()


def test_explicit_appendix_still_allowed_for_turtle(template_text, framework_text):
    """显式传 --appendix 时，即使当前报告也允许渲染框架附录（用户主动要求）。"""
    md_text = REAL_REPORTS[0].read_text(encoding="utf-8") if REAL_REPORTS[0].exists() else XSS_REPORT
    html, stats = build_html(
        md_text,
        template_text,
        appendix_md=framework_text,
        force_appendix=True,
    )
    assert stats["has_appendix"] is True
    assert "Greenwald" in html


# ---------------------------------------------------------------------------
# XSS
# ---------------------------------------------------------------------------

def test_sanitize_allowlist_strips_dangerous_markup():
    dirty = (
        '<p onclick="evil()">hi <strong>bold</strong></p>'
        "<script>alert(1)</script>"
        '<iframe src="https://evil.example"></iframe>'
        '<a href="javascript:alert(1)">link</a>'
        '<a href="https://example.com" title="ok">ok</a>'
    )
    cleaned = sanitize_html(dirty)

    assert "<p>hi <strong>bold</strong></p>" in cleaned
    assert "<script" not in cleaned
    assert "iframe" not in cleaned
    assert "onclick" not in cleaned
    assert "javascript:" not in cleaned
    assert 'href="https://example.com"' in cleaned
    assert ">link</a>" in cleaned


def test_xss_payloads_neutralized_in_report(template_text):
    html, stats = build_html(XSS_REPORT, template_text, standalone_css="")

    lowered = html.lower()
    assert "<script" not in lowered
    assert "onerror" not in lowered
    assert "onclick" not in lowered
    assert "<iframe" not in lowered
    assert "javascript:" not in lowered
    assert "alert('preamble-xss')" not in lowered

    # 正常 Markdown / 报告结构仍然可用
    assert stats["has_decision_card"] is True
    assert stats["dimensions"] == 6
    assert "<strong>加粗文本</strong>" in html
    assert "现在能买吗" in html


def test_title_injection_is_escaped(template_text):
    md_text = (
        "# <script>alert(1)</script>Evil（000001.SZ）龟龟投资策略分析报告\n\n"
        "## 执行摘要\n\n正常段落。\n"
    )
    parsed = parse_report(md_text)
    html, _ = build_html(md_text, template_text)

    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html
    # 标题被解析出来但渲染时转义，不会丢掉公司名
    assert "Evil" in parsed["company_name"]


# ---------------------------------------------------------------------------
# 正常 Markdown 渲染
# ---------------------------------------------------------------------------

def test_markdown_features_render():
    html = md_to_html(
        "## 标题\n\n"
        "**加粗** 与 `行内代码`\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "- 列表项\n\n"
        "```\ncode block\n```\n"
    )
    assert "<h2>标题</h2>" in html
    assert "<strong>加粗</strong>" in html
    assert "<code>行内代码</code>" in html
    assert "<table>" in html
    assert "<li>列表项</li>" in html
    assert "<pre><code>code block" in html


# ---------------------------------------------------------------------------
# 旧版 qualitative_report.md 兼容
# ---------------------------------------------------------------------------

def test_legacy_qualitative_report_compatibility(template_text, framework_text):
    parsed = parse_report(LEGACY_REPORT)

    assert parsed["report_kind"] == "qualitative"
    assert parsed["is_legacy_qualitative"] is True
    assert parsed["company_name"] == "测试公司"
    assert parsed["stock_code"] == "600001.SH"
    assert parsed["generated_date"] == "2026-01-01"

    # 维度卡片与评级徽标
    assert len(parsed["dimensions"]) == 2
    assert parsed["dimensions"][0]["badge"] == "强"
    assert parsed["dimensions"][1]["badge"] == "较强"
    assert parsed["parameters_table"]
    assert "moat_rating" in parsed["parameters_table"]

    # KPI 快照来自旧结构化参数表
    cards = extract_kpi_cards(LEGACY_REPORT)
    labels = {card["label"] for card in cards}
    assert {"5Y Avg ROE", "护城河评级", "可持续性", "管理层评价"} <= labels

    html, stats = build_html(
        LEGACY_REPORT,
        template_text,
        appendix_md=framework_text,
        standalone_css="",
    )
    assert stats["is_legacy_qualitative"] is True
    assert stats["has_decision_card"] is False
    assert stats["dimensions"] == 2
    assert stats["kpi_cards"] >= 4
    assert stats["has_appendix"] is True

    # 旧报告保留维度、附录与一句话结论
    assert "维度一" in html
    assert "维度二" in html
    assert "Greenwald" in html
    assert "值得长期跟踪" in html
    assert "STRONG MOAT" in html


# 新流程的 qualitative_report.md 也要求第一屏带投资决策卡；旧格式兼容不能因此失效
LEGACY_WITH_DECISION_CARD = """# 定性分析 — 测试公司 (600001.SH)

## 🎯 投资决策卡

| 问题 | 答案 |
|---|---|
| **① 现在能买吗？** | 未估值/待核实 |
| **② 为什么？** | 纯定性报告 |

## 维度一：商业模式与资本特征（D1）

先款后货，现金流好。

**综合评价：强**

## 结构化参数

| 参数 | 值 |
|---|---|
| moat_rating | 强 |
| moat_sustainability | 高可持续 |
"""


def test_legacy_qualitative_with_decision_card_still_compatible(template_text, framework_text):
    parsed = parse_report(LEGACY_WITH_DECISION_CARD)

    assert parsed["report_kind"] == "qualitative"
    assert parsed["is_legacy_qualitative"] is True
    assert parsed["decision_card"]
    assert len(parsed["dimensions"]) == 1
    assert parsed["dimensions"][0]["badge"] == "强"

    html, stats = build_html(
        LEGACY_WITH_DECISION_CARD,
        template_text,
        appendix_md=framework_text,
    )
    assert stats["has_decision_card"] is True
    assert stats["is_legacy_qualitative"] is True
    assert stats["has_appendix"] is True
    assert "投资决策卡" in html
    assert "维度一" in html
    assert "Greenwald" in html
