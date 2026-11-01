# 商业定性分析（D1–D6，/business-analysis）

**提示词资产**：`shared/qualitative/`（coordinator / qualitative_assessment / data_collection + `agents/`（numeric_audit / cleanroom_audit / writing_style）+ `references/`（output_schema / 判断锚点 / 行业指标 / 写作规则）+ `templates/`）；入口 `.opencode/command/business-analysis.md`

## 职责

独立商业定性分析的提示程序，coordinator 编排完整流程：

1. **数据采集**（Phase 1A）：调 [data-collection](data-collection.md) CLI 产出数据包 + [report-quality](report-quality.md) 的 quality_control 预计算
2. **PDF-first 阅读**（Phase 2）：先读 [pdf-pipeline](pdf-pipeline.md) 章节摘要 → 按需读全文分片 → 需要核表再看原 PDF；产出 `data_pack_report.md`
3. **WebSearch 补充** §7–§10（治理/行业/竞争/子公司）
4. **D1–D6 定性评估** → `qualitative_report.md`
5. **审计门**：numeric_audit / cleanroom（SHA 绑定）+ report_consistency qualitative gates

## 边界

- 先读 [analysis-contract](../../shared/analysis_contract.md)，冲突以其为准
- 只引用预计算数值（§17 / CM），禁止自算；扫描件降级 WebSearch 且明确标注未读原文
- 仅 A 股，不执行港美股分支；产出（qualitative_report.md + data_pack_market.md）是 [turtle-strategy](turtle-strategy.md) 与 [valuation-analysis](valuation-analysis.md) 的前置依赖
