---
id: screener
type: module-design
title: 两级选股器
status: active
parent: guigui-architecture
depends-on: [data-collection]
---

# 两级选股器

**代码**：`scripts/screener_core.py`、`scripts/screener_config.py`

## 职责

**Tier 1 批量过滤**（全市场快照 ~1 分钟）：排除 ST/PT/退市/银行（可配）→ 上市 ≥3 年 → 市值/成交额门槛 → 0<PB≤10 → 双通道 PE（主通道 0<PE≤50 + 股息率>0；观察通道亏损股按市值前 50）→ `0.4×dv + 0.3×(1/PE) + 0.3×(1/PB)` 排名取前 150。

**Tier 2 逐只深度**（复用 AKShareClient + TTL 缓存）：硬否决质押比 >70%（只统计未解押）；财务质量（ROE/毛利率/负债率，新浪限流时从三大报表推导）；因子 2 归母支付率 M 与现金代理（`R_status`/`R_proxy_label` 区分）；因子 4 EV/EBITDA 等；历史参考价（非正/缺失排除，<3 项不合成，非保底价）；综合评分**默认四因子**，仅核实 R 时五因子并记录 `composite_factors`/`composite_r_included`。

## 边界

- PE 口径显式：`pe`/`pe_basis` 两列，TTM 优先、静态回退，不隐含混用
- 未核实输出 `degraded_flags`（如 `pledge_unverified`）标"未核实"，**不冒充通过**；观察通道候选 ≠ 买入资格
- 配置启动 `validate()`：非法权重/阈值/TTL 直接报错，不静默生效
- 不生成分析报告：默认 CSV，HTML 仅 `--html`（渲染归 [report-rendering](report-rendering.md)）；复用 [data-collection](data-collection.md)，不另建采集
