# 龟龟策略编排（/turtle-analysis）

**提示词资产**：`strategies/turtle/`（coordinator / phase2_PDF解析 / phase3_quantitative / phase3_valuation + `references/`（factor_interface / shared_tables / judgment_examples））；入口 `.opencode/command/turtle-analysis.md` + `.opencode/skills/turtle-analysis/`

## 职责

完整龟龟策略（定性 + 定量 + 估值 + 报告组装）的编排提示程序。串行 Agent 架构：**preflight → Agent B 定量（四因子/穿透回报率 11 步精算）→ Agent C 估值 + 报告组装**。

流程要点：

- 定性前置：缺 `qualitative_report.md` 时自动先跑完整 /business-analysis
- Step A 强制 `akshare_collector --refresh-market`；估值前必跑 [valuation-engine](valuation-engine.md)（同次 `data_snapshot.json`）
- 最终报告单独审计生成 `final_audit.md`（**不复用 BA 的 audit.md**），report_consistency 按 turtle 类型 gates + cleanroom 校验

## 边界

- coordinator 只调度不计算、不采集；所有数值来自确定性层（数值纪律见 [analysis-contract](../../shared/analysis_contract.md)）
- 缺审计产物或哈希未绑定当前文件时，只能交付明确标识的降级/撤回说明，不能伪造 PASS
- 报告更新须含"较上期变化"（含撤销失效价格/评级的原因）
- 依赖：[qualitative-analysis](qualitative-analysis.md)（前置产出）、[data-collection](data-collection.md)、[report-quality](report-quality.md)
