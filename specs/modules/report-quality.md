---
id: report-quality
type: module-design
title: 报告质量门禁（确定性核验杠杆）
status: draft
parent: guigui-architecture
depends-on:
  - data-contracts
references:
  - analysis-contract
---

# 报告质量门禁（确定性核验杠杆）

**代码**：`scripts/quality_control.py`（杠杆 1）、`scripts/report_consistency.py`（杠杆 6）

## 职责

围绕报告的确定性核验，两把杠杆：

- **quality_control**：易错指标预计算（CM §1–5），LLM 只引用不重算
- **report_consistency**：跨段数值冲突扫描（advisory）+ 按报告类型（qualitative / valuation / turtle）的交付 gates + 审计产物校验——`final_audit.md`/`valuation_audit.md` 须含绑定**当前**报告与 `decision_metrics.json` 的 `REPORT_SHA256`/`METRICS_SHA256`；cleanroom 产物须含 `CLEANROOM_DONE` 且哈希一致

## 边界

- 正则冲突提示是 advisory，**不冒充完整财务审计**
- 哈希绑定只证明产物与当前文件一致，不等于独立审计本身；修改报告/数据后须重新审计，不能复用上期 PASS
- 只校验不生成投资结论；输入为报告 + [data-contracts](data-contracts.md) 的 metrics 格式

交付规则（决策卡、审计绑定细节）见 [analysis-contract](../../shared/analysis_contract.md)。
