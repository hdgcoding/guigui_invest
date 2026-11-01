---
id: data-collection
type: module-design
title: 数据采集（§1–§17 数据包生产者）
status: active
parent: guigui-architecture
depends-on: [data-contracts]
references: [analysis-contract]
---

# 数据采集（§1–§17 数据包生产者）

**代码**：`scripts/akshare_collector.py`（AKShareClient 门面 + CLI）、`scripts/akshare_modules/`（Infrastructure / Financials / OtherData / DerivedMetrics / Assembly 五个 mixin——内部实现分层，非独立边界）

## 职责

`data_pack_market.md`（§1–§17）的唯一生产者，同次产出 `data_snapshot.json` 与 `decision_metrics.json`。数据源全 AKShare、无需 Token：新浪财报/日线（重采样周线）+ 东财分红/质押/回购/业绩 + 百度估值 + 中美国债收益率。

**降级策略**（标注而非中断）：母公司报表（§3P/§4P）缺失 → 占位标注，由 Agent 从年报 PDF 补充；接口被代理拦截 → 绕过代理重试 → 仍失败标注"数据缺失"；§7–§10 治理/行业/竞争/子公司留给 Agent WebSearch。

## 边界

- 只做**确定性采集与预计算**（§17 M/R 网格、底价等），不做任何投资判断
- 自动加载标的目录 `financial_supplement.json`（经 [data-contracts](data-contracts.md)），不得手改 decision_metrics 把 unverified 变 verified

## 契约

§1–§17 段号冻结；数据包金额百万元；`_store` 列名兼容原版；缓存 TTL 财报 7 天/行情 24 小时。CLI：`--refresh-market`（强制行情 + 重算 §17）/ `--cache-refresh` / `--no-cache`。数据与交付约束见 [analysis-contract](../../shared/analysis_contract.md)。
