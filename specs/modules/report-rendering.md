---
id: report-rendering
type: module-design
title: MD→HTML 仪表盘渲染
status: draft
parent: guigui-architecture
---

# MD→HTML 仪表盘渲染

**代码**：`scripts/report_to_html.py`

## 职责

分析报告 Markdown → 独立 HTML 仪表盘。纯 Python（markdown + jinja2）转换、零 token；已兼容当前报告结构（投资决策卡、较上期变化、执行摘要、D1–D6、数字溯源汇总等）。

## 边界

- **仅用户明确要求网页版/打印版时生成**（`--html` / `--standalone`）；HTML 不入库，属可再生衍生品
- 单向渲染：不修改报告内容、不做校验（校验归 [report-quality](report-quality.md)）
