# 龟龟投资框架 · AKShare + OpenCode/GLM 版（guigui_invest）

AI 辅助的 A 股基本面分析系统。混合架构：**Python 脚本完成确定性数据采集与估值计算，LLM（GLM，通过 OpenCode 运行）按提示词驱动定性分析与多因子评估**。

> 复刻自 [Turtle Investment Framework](https://github.com/terancejiang/Turtle_investment_framework)（MIT License），两项核心替换：
> 1. 数据源 **Tushare → AKShare**（免费、无需 Token）
> 2. Agent 运行时 **Claude Code → OpenCode + GLM**（提示词资产保留原有 §1–§17 数据包契约）
>
> 当前范围：**MVP（仅 A 股）**。架构上预留多市场扩展。

## 快速开始

```bash
bash init.sh        # 创建 .venv + 安装依赖 + 跑测试
```

在 OpenCode（配置 zhipuai-coding-plan 等 GLM provider）中：

```
/business-analysis 600887   # 独立商业分析（定性 6 维度 → qualitative_report.md）
/valuation 600887           # 独立估值分析（依赖 business-analysis 产出）
/turtle-analysis 600887     # 完整龟龟策略（定性 + 定量四因子 + 估值）
/download-annual-report 600887  # 搜索下载年报 PDF
```

## 架构

```
用户输入 (股票代码)
   │
   ▼
Phase 1A  Python: scripts/akshare_collector.py → data_pack_market.md（§1–§17）
   │         AKShare：新浪财报/行情 + 东财分红质押回购 + 百度估值 + 中美国债收益率
   ▼
Phase 1B  Agent: websearch 补充 §7–§10（治理/行业/竞争/子公司）
   │
   ▼
Phase 2   Python: scripts/pdf_preprocessor.py → 年报 PDF 章节提取（可选）
   │
   ▼
Phase 3   LLM: 定性（D1–D6）→ 定量（四因子/穿透回报率）→ 估值 → 报告
   │
   ▼
output/{code}_{company}/  报告 .md + .html
```

## 数据源映射（Tushare → AKShare）

| 数据段 | 内容 | AKShare 接口 |
|--------|------|--------------|
| §1 | 基本信息（价格/PE/PB/市值/股本/上市日期） | `stock_zh_a_daily` + `stock_zh_valuation_baidu` + `stock_info_sh/sz_name_code` |
| §2/§11 | 52 周范围 / 10 年周线 | `stock_zh_a_daily`（新浪日线重采样周线） |
| §3/§4/§5 | 三大报表（5 年年报 + 最新中期） | `stock_financial_report_sina`（新浪） |
| §3P/§4P | 母公司报表 | ⚠️ 降级占位（AKShare 无母公司口径，用年报 PDF 补充） |
| §6 | 分红历史（每股派息/送股） | `stock_fhps_detail_em`（东财，按报告期） |
| §12 | 财务指标（ROE/毛利率等） | `stock_financial_analysis_indicator`（新浪） |
| §14 | 无风险利率（10 年国债） | `bond_zh_us_rate` |
| §15/§16 | 回购 / 质押 | `stock_repurchase_em` / `stock_gpzy_individual_pledge_ratio_detail_em` |
| §17 | 衍生指标（M/R 网格/底价等） | Python 确定性计算（与原版 derived_metrics 同源） |

**已知降级项**（脚本自动标注，不中断流程）：
- 母公司报表（§3P/§4P）缺失 → Agent 从年报 PDF 附注补充
- 新浪现金流无折旧摊销附表 → §17.12 G 网格降级，LLM 改用行业 G 锚点（shared_tables.md）
- 个别接口被代理拦截 → 自动绕过代理重试，仍失败则标注"数据缺失"

## 脚本 CLI

```bash
# 数据采集（完整 §1–§17 数据包）
.venv/bin/python scripts/akshare_collector.py --code 600887

# 仅刷新市场数据段（§1/§2/§11/§14，<10s）
.venv/bin/python scripts/akshare_collector.py --code 600887 --refresh-market

# 刷新缓存 / 绕过缓存
.venv/bin/python scripts/akshare_collector.py --code 600887 --cache-refresh
.venv/bin/python scripts/akshare_collector.py --code 600887 --no-cache

# 估值引擎（分类 + WACC + DCF/DDM/PE Band + 敏感性表）
.venv/bin/python scripts/valuation_engine.py --code 600887 --output-dir output/600887_伊利股份/

# 质量杠杆脚本
.venv/bin/python scripts/quality_control.py --input <data_pack> --output <computed_metrics>
.venv/bin/python scripts/report_consistency.py --report <report> --output <consistency> --gates

# MD → HTML 仪表盘
.venv/bin/python scripts/report_to_html.py --input <report.md> --output <report.html> --standalone
```

## 目录结构

```
guigui_invest/
├── .opencode/
│   ├── command/                 # OpenCode slash 命令（business/valuation/turtle/download）
│   └── skill/turtle-analysis/   # turtle-analysis 技能定义
├── AGENTS.md                    # OpenCode 项目指令（入口文档）
├── shared/qualitative/          # 共享定性分析模块（D1–D6 六维度 + 审计门）
├── strategies/
│   ├── turtle/                  # 龟龟策略（四因子穿透回报率 + 估值组装）
│   └── valuation/               # 独立估值模块
├── scripts/
│   ├── akshare_collector.py     # 数据采集门面（AKShareClient + CLI）
│   ├── akshare_modules/         # 采集模块（financials/other_data/assembly/derived_metrics）
│   ├── valuation_engine.py      # 估值引擎
│   ├── quality_control.py       # 杠杆1: 确定性预计算 CM§1-5
│   ├── report_consistency.py    # 杠杆6: 跨段数值冲突扫描
│   ├── pdf_preprocessor.py      # 年报 PDF 章节提取
│   ├── download_report.py       # 年报 PDF 下载
│   └── report_to_html.py        # MD → HTML 仪表盘
├── tests/                       # 51 个测试（Mock 数据，无需网络）
└── output/                      # 输出目录（gitignored）
```

## 关键约定

1. **金额单位**：数据包内所有金额统一为**百万元**（原始元 ÷ 1e6）；正文报告用亿元
2. **数据包契约**：§1–§17 段号不可变动（所有提示词依赖此契约）
3. **内部数据契约**：`_store` 保持与原 Tushare 版相同的列名（revenue/n_income_attr_p/...），使 derived_metrics 与 valuation_engine 无缝移植
4. **缓存**：财报类 7 天、行情类 24 小时（`output/.collector_cache/`，Parquet TTL）
5. **数值纪律**：LLM 只引用 §17 预计算结果，有网格时查表选行，禁止自算
6. **无需 Token**：AKShare 免费

## 测试

```bash
AKSHARE_RATE_DELAY=0 .venv/bin/python -m pytest tests/ -v   # 51 tests, no network
```

覆盖：代码验证 / store 契约 / 中期+末期分红合并支付率 / §17 衍生指标全链路 / 新浪报表归一化 / 周线重采样 / 数据包分段解析 / 风险检测。

## License

MIT（继承原项目）
