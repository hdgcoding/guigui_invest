# 混合架构 — Python 确定性层 × LLM 定性层

## 拓扑：四阶段流水线

```
Phase 1A  Python: akshare_collector → data_pack_market.md(§1–§17) + data_snapshot.json + decision_metrics.json
Phase 1B  Agent:  WebSearch 补充 §7–§10（治理/行业/竞争/子公司）
Phase 2   Python: pdf_preprocessor → 章节摘要 + 全文分片（PDF-first，GLM 文本优先）
Phase 3   LLM:    定性(D1–D6) → 定量(四因子/穿透回报率) → 估值 → 报告组装
```

分层原则：**确定性计算归 Python，定性判断与编排归 LLM**；LLM 的数值纪律、审计与交付约束由 [analysis-contract](../shared/analysis_contract.md) 统一规定（全仓优先）。

## 模块边界与依赖 DAG

Python 确定性层（`scripts/`）：

| 模块 | 职责一句话 |
|---|---|
| [data-contracts](modules/data-contracts.md) | 内部数据契约：单位/字段映射/快照完整性/补充文件 |
| [data-collection](modules/data-collection.md) | §1–§17 数据包唯一生产者（AKShareClient） |
| [valuation-engine](modules/valuation-engine.md) | DCF/DDM/PE Band 等确定性估值计算 |
| [screener](modules/screener.md) | 两级选股（批量过滤 + 逐只深度） |
| [pdf-pipeline](modules/pdf-pipeline.md) | 年报 PDF 下载与文本提取 |
| [report-quality](modules/report-quality.md) | 确定性核验杠杆（CM 预计算 / 一致性 gates） |
| [report-rendering](modules/report-rendering.md) | MD→HTML 仪表盘（可再生） |

LLM 提示词资产层（Markdown 提示程序，经 `.opencode/command/` slash 命令入口调度）：

| 模块 | 职责一句话 |
|---|---|
| [qualitative-analysis](modules/qualitative-analysis.md) | D1–D6 商业定性分析（/business-analysis） |
| [turtle-strategy](modules/turtle-strategy.md) | 完整龟龟策略编排（/turtle-analysis） |
| [valuation-analysis](modules/valuation-analysis.md) | 独立估值编排（/valuation） |

依赖边（代码/命令实际所示）：

```
data-contracts ← data-collection ← {screener, qualitative-analysis, turtle-strategy, valuation-analysis}
data-contracts ← {valuation-engine, report-quality}
pdf-pipeline   ← qualitative-analysis
valuation-engine ← {turtle-strategy, valuation-analysis}
report-quality ← {qualitative-analysis, turtle-strategy, valuation-analysis}
qualitative-analysis ← {turtle-strategy, valuation-analysis}
```

`.opencode/command/` 与 `.opencode/skills/` 是薄入口层（参数校验 + 转读 coordinator），不设独立 spec。

## 不变量（代码已强制）

1. **§1–§17 段号不可变**——全部提示词依赖此契约
2. **单位链路**：原始三表元 → 数据包百万元 → 正文亿元；每股元/股，不可混用
3. **`_store` 内部列名与原 Tushare 版兼容**（revenue/n_income_attr_p/…），使 derived_metrics 与 valuation_engine 无缝移植
4. **缓存 TTL**：财报 7 天 / 行情 24 小时（`output/.collector_cache/`，Parquet）
5. **同次快照**：估值引擎校验每帧 SHA256，>7 天硬过期、>1 天告警
6. **产出入库范围**：仅最终报告 `.md` 入库；HTML 与中间产物可再生不入库
7. **选股器口径显式**：`pe`/`pe_basis` 两列不隐含混用；配置启动 `validate()` 直接报错
8. **模型继承会话**：运行前确认实际 provider/model，不得无依据声称 GLM

## 基础设施（无独立 spec）

`config.py`（代码校验/环境）、`cache_utils.py`（TTL 缓存）、`format_utils.py`（数字/表格格式化）为共享工具层，被各模块直接引用；其约束体现在所属模块 spec 与上列不变量中。

## 测试

`tests/` 全 Mock 回归（`AKSHARE_RATE_DELAY=0`，无网络），覆盖 store 契约、§17 全链路、数据包分段解析、选股器过滤/排名、PDF 文本导出等。
