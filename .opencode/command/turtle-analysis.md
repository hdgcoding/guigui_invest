---
description: 完整龟龟策略分析（定性 + 定量四因子穿透回报率 + 估值 + 报告组装）
---

Run a full Turtle Investment Framework (龟龟投资策略) analysis on stock: $ARGUMENTS

## Input Validation
- Stock code must be a valid A-share (e.g., 600887, 000858.SZ)
- If $ARGUMENTS is empty or invalid, ask the user for a valid stock code before proceeding
- If only digits are given, the code will be normalized by scripts/config.py

## Execution Instructions

Read strategies/turtle/coordinator.md for the full pipeline specification, then execute each phase:

### Prerequisite: Check BA outputs
- **qualitative_report.md** — required. If missing → **自动先执行 /business-analysis 完整流程**（Step 1 数据采集 → PDF → Step 2 定性 → Step 3 审计门），完成后继续。
- **data_pack_market.md** — required. If missing → 同上。
- **data_pack_report.md** — optional. If missing, Agent B uses degraded mode (no PDF footnote data).

### Step A: Market Data Refresh
```bash
.venv/bin/python scripts/akshare_collector.py --code $ARGUMENTS --output output/{code}_{company}/data_pack_market.md --refresh-market
```
- Refreshes §1 (price/market cap), §2 (52-week range), §11 (weekly prices), §14 (risk-free rate)
- If data pack is >7 days old, auto-falls back to full collection

### Phase 3: Analysis and Report
- **Step 3.1 Agent B**: Read strategies/turtle/phase3_quantitative.md for penetrating return rate calculation
- **Step 3.2 Agent C**: Read strategies/turtle/phase3_valuation.md for valuation + report assembly
  - Reads qualitative_report.md (from /business-analysis) for qualitative parameters
  - Reads phase3_quantitative.md (from Agent B) for quantitative parameters
- Output: output/{code}_{company}/{company}_{code}_分析报告.md

## Error Recovery
- Missing BA outputs → stop and prompt user to run /business-analysis first
- Step A refresh failure → proceed with existing data（AKShare 个别接口失败标注"数据缺失"）
- Missing data_pack_report.md → Agent B uses degraded mode (no footnote data)
- Always produce a final report even if partial data

## Output
Final report: output/{code}_{company}/{company}_{code}_分析报告.md
Optional HTML (only if requested): `.venv/bin/python scripts/report_to_html.py --input <report>.md --output <report>.html --standalone`

Usage: /turtle-analysis 600887
