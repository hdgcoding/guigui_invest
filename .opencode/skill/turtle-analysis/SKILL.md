---
name: turtle-analysis
description: Run a full 龟龟投资策略 (Turtle Investment Framework) fundamental analysis on A-share stocks. 触发场景：用户要对 A 股做完整的基本面分析（定性 + 定量四因子 + 估值）时使用。
---

# Turtle Investment Framework — Skill Definition

## Skill Info
- **Name**: turtle-analysis
- **Description**: Run a full 龟龟投资策略 (Turtle Investment Framework) fundamental analysis on Chinese A-share stocks
- **Entry Point**: `strategies/turtle/coordinator.md`
- **Slash Command**: `/turtle-analysis <stock_code>`

## Dependencies
- **/business-analysis (prerequisite)**: Produces `qualitative_report.md` + `data_pack_market.md` (and optional `data_pack_report.md`). PDF acquisition and qualitative analysis live in `/business-analysis`, not in this skill.
- **Python venv**: `.venv/` with `akshare`, `pandas`, `pdfplumber` (created by `bash init.sh`; prefer `.venv/bin/python`, fall back to `python3`)
- **AKShare**: free data source, no API token required

## Pipeline Phases
1. **Prerequisite check**: Verify `output/{code}_{company}/qualitative_report.md` and `data_pack_market.md` exist (outputs of `/business-analysis`). **If missing → 不要停止，自动先执行 /business-analysis 的完整流程**（数据采集 → PDF 提取 → 6 维度定性 → 审计门），完成后再继续下面的龟龟策略步骤。`data_pack_report.md` is optional (Agent B degrades gracefully without PDF footnote data).
2. **Step A — market data refresh**: `scripts/akshare_collector.py --refresh-market` refreshes §1/§2/§11/§14 (price/market cap, 52-week range, weekly prices, risk-free rate). If the data pack is >7 days old, it auto-falls back to full collection.
3. **Phase 3.1 — Agent B (quantitative)**: Read `strategies/turtle/phase3_quantitative.md`. Runs Step 0 data validation + accounting anchoring, then the 11-step penetrating return rate calculation. Output: `phase3_quantitative.md`.
4. **Phase 3.2 — Agent C (valuation + report)**: Read `strategies/turtle/phase3_valuation.md`. Reads `qualitative_report.md` (from `/business-analysis`) and Agent B's output, then assembles the final report.

## Natural language triggering
用户自然语言请求（如"分析一下 600887"、"帮我看看伊利股份"）同样适用本技能：解析出股票代码后直接从 Phase 1 开始执行，全程无需用户手动调用其它命令。

## Output
- `output/{code}_{company}/` — all intermediate and final files
- Final report: `{company}_{code}_分析报告.md`
