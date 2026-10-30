---
description: 独立估值分析（公司分类 + WACC + DCF/DDM/PE Band 多方法估值）
---

Run a Valuation Analysis (估值分析) on stock: $ARGUMENTS

## Input Validation
- Stock code must be a valid A-share (e.g., 600887, 000858.SZ)
- If $ARGUMENTS is empty or invalid, ask the user for a valid stock code before proceeding
- If only digits are given, the code will be normalized by scripts/config.py

## Prerequisite Check
Before executing, verify these files exist in output/{code}_{company}/:
- **qualitative_report.md** — required. If missing, inform user to run `/business-analysis {stock_code}` first, then stop.
- **data_pack_market.md** — required. If missing, same as above.

## Execution Instructions

Read strategies/valuation/coordinator.md for the full pipeline specification, then execute each step:
先读 `shared/analysis_contract.md`；未核实买点用“未估值/待核实”，不强行生成数值。

### Step 1: Python Valuation Computation
```bash
.venv/bin/python scripts/valuation_engine.py --code $ARGUMENTS --output-dir output/{code}_{company}/
```
- 先用采集器刷新行情/§17并保存共享快照；估值引擎读取同目录快照，不独立改换行情。模型不适用时显示原因。
- Outputs: output/{code}_{company}/valuation_computed.md
- Contains: company type, WACC, each method's result + 5×5 sensitivity tables, cross-validation

### Step 2: LLM Qualitative Adjustment + Report
- Read strategies/valuation/phase2_valuation.md for qualitative adjustment instructions
- Read strategies/valuation/references/valuation_methods.md for methodology reference
- Read strategies/valuation/references/report_template.md for output format
- Read output/{code}_{company}/qualitative_report.md for qualitative insights (D1-D6)
- Read output/{code}_{company}/valuation_computed.md for all computed numbers
- Apply qualitative adjustments: D1 revenue quality → growth rate, D2 moat → terminal growth, D3 cycle → scenario weights, D4 management → governance discount
- Select adjusted scenarios from sensitivity tables (no arithmetic needed — 查表选行，禁止自算)
- Output: output/{code}_{company}/{company}_{code}_估值报告.md
- 最终单独审计估值报告并生成valuation_audit.md，绑定报告/metrics哈希；按valuation类型运行report_consistency.py --gates --metrics <dir>/decision_metrics.json --require-audit <dir>/valuation_audit.md --require-cleanroom <dir>/cleanroom_metrics.md。

## Error Recovery
- Missing qualitative_report.md → stop and prompt user to run /business-analysis first
- valuation_engine.py fails → check network (AKShare needs no token), retry
- Classification ambiguous → Python defaults to 混合型
- A valuation method fails → Python skips it, weights redistributed
- qualitative_report.md has no structured parameters → skip qualitative adjustments, use Python defaults
- Always produce a final report even with partial data

## Output
Final report: output/{code}_{company}/{company}_{code}_估值报告.md

Usage: /valuation 600887
