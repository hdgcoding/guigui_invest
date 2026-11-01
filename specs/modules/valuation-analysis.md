# 独立估值编排（/valuation）

**提示词资产**：`strategies/valuation/`（coordinator / phase2_valuation + `references/`（classification_rules / valuation_methods / valuation_examples / report_template））；入口 `.opencode/command/valuation.md`

## 职责

独立估值模块的编排提示程序：前置检查（要求 `qualitative_report.md`，缺失则引导先跑 /business-analysis）→ 调 [valuation-engine](valuation-engine.md) 确定性计算 → LLM 定性调整 → 交付估值报告，`valuation_audit.md` 单独审计 + report_consistency 按 valuation 类型 gates + cleanroom 校验。

## 边界

- coordinator 不执行估值计算或数学运算——确定性数字全部来自估值引擎
- 同次 `data_snapshot.json` / `decision_metrics.json` 为估值与决策卡统一口径；**没有 verified 买点时价格/回调写"待核实"，不能靠定性调整补齐**
- 模型适用性约束（DDM/DCF 守门）继承 [valuation-engine](valuation-engine.md) 与 [analysis-contract](../../shared/analysis_contract.md)；仅 A 股
- 依赖：[qualitative-analysis](qualitative-analysis.md)（前置产出）、[data-collection](data-collection.md)、[report-quality](report-quality.md)
