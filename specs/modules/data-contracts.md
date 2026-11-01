# 内部数据契约层

**代码**：`scripts/financial_contracts.py`、`scripts/data_snapshot.py`、`scripts/financial_supplement.py`、`scripts/akshare_modules/constants.py`

## 职责

跨模块共享的内部数据契约，四个组成部分：

1. **财务约定**（financial_contracts.py）：原始三表金额一律为**元**、永不百万；报表期/重述问题检测（`statement_issues`）、有息负债构成、折旧摊销口径
2. **快照持久化**（data_snapshot.py）：采集器一次快照落盘，供估值引擎与报告门禁共享——每帧 SHA256 校验，>7 天硬过期、>1 天告警但可加载
3. **来源核对补充**（financial_supplement.py）：加载标的目录 `financial_supplement.json`（母公司表/审计意见/归母现金/FCFF/实际股本）；不完整或矛盾记录**整体拒绝加载**，不许部分修改底表
4. **字段映射**（constants.py）：新浪/东财列名 → `_store` 内部列名的映射，保持与原 Tushare 版兼容

## 边界

- 纯约定与校验：**不采集、不估值、不产出报告**；被 collector / valuation-engine / report-quality 引用（screener 经 collector 间接使用）
- 补充文件的 JSON 规范文档为 `shared/financial_supplement_schema.md`——保留为输入文档，未采纳为 spec 节点（用户确认）；加载行为以本层实现为准

## 关键契约

单位与口径细则（百万元换算、未核实标注、SHA 绑定）统一见 [analysis-contract](../../shared/analysis_contract.md)；本层是其确定性数据的代码落地。
