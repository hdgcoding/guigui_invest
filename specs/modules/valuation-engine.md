---
id: valuation-engine
type: module-design
title: 估值引擎（确定性估值计算）
status: active
parent: guigui-architecture
depends-on: [data-contracts]
references: [analysis-contract]
---

# 估值引擎（确定性估值计算）

**代码**：`scripts/valuation_engine.py`

## 职责

确定性估值计算：公司分类 + WACC + DCF / DDM / PE Band / PEG / PS + 敏感性表。输入为**同目录 `data_snapshot.json`**（与采集器同次快照）：逐帧校验 SHA256，>7 天硬过期、>1 天告警但可加载。

## 模型适用性守门（代码强制）

- **DDM** 仅用完整已实施分红年度；负历史增长只作有限期情景，终值增长显式列出（默认 0% 只是情景）
- **DCF** 只接受已核对的 FCFF 及完整现金/债务/少数股东转换数据；**不把 OCF−Capex 直接当 FCFF**
- 未核对时标"不适用"，不硬套 WACC

## 边界

- 不采集行情（依赖 [data-collection](data-collection.md) 的快照）、不做定性调整（LLM 侧归 [valuation-analysis](valuation-analysis.md) / [turtle-strategy](turtle-strategy.md)）
- 网格/表由引擎算好，LLM 只查表选行（数值纪律见 [analysis-contract](../../shared/analysis_contract.md)）
