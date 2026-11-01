---
id: guigui-goal
type: goal-and-requirements
title: 龟龟投资框架 — 目标与需求
status: active
---

# 龟龟投资框架 — 目标与需求

## 目标

AI 辅助的 A 股基本面分析系统：**Python 完成确定性数据采集与估值计算，LLM（OpenCode 运行，模型继承会话）按提示词资产驱动定性分析与多因子评估**，产出带审计哈希绑定的投资分析报告。

## 问题与背景

- 手工基本面分析耗时且数值易错；LLM 直接做算术不可靠，需确定性层兜底。
- 复刻自 [Turtle Investment Framework](https://github.com/terancejiang/Turtle_investment_framework)（MIT），两项核心替换：
  1. 数据源 Tushare → **AKShare**（免费、无需 Token）
  2. 运行时 Claude Code → **OpenCode**（提示词资产保留原 §1–§17 数据包契约）

## 受众

通过 OpenCode slash 命令（`/business-analysis` 等）运行分析的个人投资者/分析者。

## 能力（代码现有）

1. **商业分析** `/business-analysis`：D1–D6 六维度定性 → `qualitative_report.md`（PDF-first）
2. **独立估值** `/valuation`：依赖商业分析产出，估值引擎 + LLM 定性调整
3. **完整龟龟策略** `/turtle-analysis`：定性 + 定量四因子穿透回报率 + 估值 + 报告组装
4. **两级选股器**：Tier 1 全市场批量过滤排名（~1 分钟）→ Tier 2 逐只深度（复用采集器）
5. **年报 PDF 管线**：搜索下载 + 章节摘要/全文分片文本提取（GLM 文本优先）
6. **确定性质量门禁**：CM 预计算、跨段数值一致性扫描、SHA256 审计绑定校验
7. **MD→HTML 仪表盘**：纯 Python 转换，按需生成

## 非目标

- **多市场（港/美股）**：架构预留扩展位，当前 coordinator 明确拒绝执行港美股分支——开放而非永久排除
- **实时行情 / 交易执行**
- **保证盈利或本金安全**（措辞禁令见 [analysis-contract](../shared/analysis_contract.md)）
- **HTML 默认交付**：可再生衍生品，仅用户明确要求时生成

## 持久原则

- **数值纪律**：LLM 只引用预计算结果（§17 / 网格选行），禁止自算——约束全文见 [analysis-contract](../shared/analysis_contract.md)
- **未核实 ≠ 0 ≠ 通过**：缺失降级标注，不中断流程，不为模板编造
- **数据包契约**：§1–§17 段号冻结；单位链路为原始元 → 数据包百万元 → 正文亿元
- **同次分析同次快照**：估值引擎与报告门禁读同一 `data_snapshot.json`
