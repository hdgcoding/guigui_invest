# AGENTS.md — guigui_invest（龟龟投资框架 · AKShare + OpenCode/GLM 版）

AI 辅助的 A 股基本面分析系统。混合架构：**Python 脚本完成确定性数据采集与估值计算，LLM（GLM，通过 OpenCode 运行）按提示词驱动定性分析与多因子评估**。

> 本项目复刻自 [Turtle Investment Framework](https://github.com/terancejiang/Turtle_investment_framework)（MIT），两项核心替换：
> 1. 数据源 Tushare → **AKShare**（免费、无需 Token）
> 2. Agent 运行时 Claude Code → **OpenCode + GLM**（提示词资产保留原有 §1–§17 数据包契约）

## 快速开始

```bash
bash init.sh                # 创建 .venv + 安装依赖 + 跑测试
```

在 OpenCode 中使用命令：

```
/business-analysis 600887   # 独立商业分析（定性 6 维度 → qualitative_report.md）
/valuation 600887           # 独立估值分析（依赖 business-analysis 产出）
/turtle-analysis 600887     # 完整龟龟策略（定性 + 定量四因子 + 估值）
```

## 目录结构

```
guigui_invest/
├── .opencode/command/          # OpenCode slash 命令（入口）
├── shared/qualitative/         # 共享定性分析模块（D1–D6 六维度）
│   ├── coordinator.md          #   /business-analysis 入口提示词
│   ├── qualitative_assessment.md #  PDF-first 定性分析主提示词
│   ├── data_collection.md      #   WebSearch 补充采集指令
│   ├── agents/                 #   审计 agent（numeric_audit 等）
│   └── references/             #   输出 schema / 判断锚点 / 行业指标
├── strategies/
│   ├── turtle/                 # 龟龟策略专属（四因子）
│   │   ├── coordinator.md      #   /turtle-analysis 入口
│   │   ├── phase3_quantitative.md # 穿透回报率 11 步精算
│   │   ├── phase3_valuation.md #   估值 + 报告组装
│   │   └── references/         #   因子接口 / 税表 / 判断锚点
│   └── valuation/              # 独立估值模块
├── scripts/
│   ├── akshare_collector.py    # 数据采集门面（§1–§17 数据包）
│   ├── akshare_modules/        # 采集模块化实现
│   ├── valuation_engine.py     # 估值引擎（DCF/DDM/PE Band/PEG/PS）
│   ├── screener_core.py        # 选股器核心（两级筛选，复用 AKShareClient）
│   ├── screener_config.py      # 选股器阈值/权重配置
│   ├── pdf_preprocessor.py     # 年报 PDF 章节提取 + --full-text 全文分片（GLM 文本优先）
│   ├── report_to_html.py       # MD → HTML 仪表盘
│   ├── cache_utils.py          # Parquet TTL 缓存
│   ├── config.py               # 代码验证 / 环境配置（无 Token）
│   └── format_utils.py         # 数字/表格格式化
├── tests/                      # pytest 测试套件（Mock 数据，无需网络）
└── output/                     # 输出目录（gitignored）
```

## 分析流水线

```
用户输入 (股票代码)
   │
   ▼
Phase 1A  Python: akshare_collector.py → data_pack_market.md（§1–§17）
   │
   ▼
Phase 1B  Agent: WebSearch 补充 §7–§10（治理/行业/竞争/子公司）
   │
   ▼
Phase 2   Python: pdf_preprocessor.py → pdf_sections_text.md（章节摘要）
          + pdf_sections_text_fulltext_part*.md（全文分片，GLM 文本优先阅读）
   │
   ▼
Phase 3   LLM: 定性（D1–D6）→ 定量（四因子/穿透回报率）→ 估值
   │
   ▼
output/{code}_{company}/  报告 .md + .html
```

## 数据包契约（§1–§17）

`data_pack_market.md` 是所有提示词的共同数据底座，段号**不可变动**：

| 段号 | 内容 | 来源（AKShare） |
|------|------|----------------|
| §1 | 基本信息（价格/市值/PE/PB/上市日期） | stock_zh_a_hist + stock_zh_valuation_baidu + stock_info_* |
| §2 | 行情数据（52 周范围） | stock_zh_a_hist(weekly) |
| §3/§3P | 合并/母公司利润表（5 年） | stock_financial_report_sina（母公司表缺失时降级标注） |
| §4/§4P | 合并/母公司资产负债表（5 年） | 同上 |
| §5 | 合并现金流量表 + FCF | 同上（FCF 计算） |
| §6 | 分红历史 | stock_history_dividend_detail |
| §7–§10 | 治理/行业/竞争/子公司占位 | Agent WebSearch 补充 |
| §11 | 周线数据（10 年摘要） | stock_zh_a_hist(weekly, 10y) |
| §12 | 财务指标（ROE/毛利率等） | stock_financial_analysis_indicator |
| §13 | 风险警告 | 自动检测 + Agent 补充 |
| §14 | 无风险利率（10 年国债） | bond_zh_us_rate |
| §15 | 股份回购 | stock_repurchase_em（按代码过滤） |
| §16 | 股权质押 | stock_gpzy_individual_pledge_ratio_detail_em |
| §17 | 衍生指标预计算 | Python 计算（M/R/G 网格/底价） |

## 关键约定

1. **金额单位**：数据包内所有金额统一为**百万元**（原始元 ÷ 1e6）；正文报告用亿元。
2. **输出目录**：`output/{code}_{company}/`，如 `output/600887_伊利股份/`。
3. **Python 执行**：优先 `.venv/bin/python`，回退 `python3`。所有脚本从项目根目录运行。
4. **无需 Token**：AKShare 免费。若个别接口被代理拦截（如东财 push2），脚本会降级标注"数据缺失"，**不要中断流程**。
5. **数据时效**：财报类缓存 7 天，行情类缓存 24 小时（`output/.collector_cache/`）。财报季 4/8/10 月用 `--cache-refresh` 刷新。
6. **数值纪律**：LLM 分析时**只引用 §17 预计算结果，不自行做数学运算**；有网格时查表选行。
7. **写作风格**：结论先行、lead-with-numbers、禁止模糊量化词（详见 shared/qualitative/references/writing_style_rules.md）。
8. **选股器口径**：Tier 1 的 PE 为静态口径（最新年报 EPS）；新浪财务指标限流时自动从三大报表推导 ROE/毛利率/负债率。
9. **PDF-first**：定性分析优先用 `pdf_preprocessor --text-output --full-text` 产出的文本（摘要 → 按需读全文分片），扫描件降级 WebSearch。
10. **产出入库范围**：`output/` 仅最终报告（`*报告*.md/html`、`qualitative_report.md`）提交 Git；中间产物（数据包/PDF提取/估值计算/缓存）可再生，不入库。
11. **报告更新协议**：用户说"更新 XX 的报告"时——① 读旧报告（分析日期/结论/关键指标）作为基线；② 重新采集数据（数据包 >7 天则全量，否则 `--refresh-market`）；③ 重跑估值引擎与 §17；④ 新报告**必须增加「较上期变化」对照节**（价格 / 穿透回报率 R / 底价溢价 / 评级变动及归因），正文其余结构保持不变；⑤ Git 历史天然提供报告版本追溯。

## 开发规范

- 提交格式：`feat(scope): description` / `fix(scope): description` / `test(scope): description`
- scope 取值：`infrastructure`, `phase1a_akshare`, `phase1b_websearch`, `phase2a_pdf`, `phase3_analysis`, `valuation`, `screener`
- 同步编写测试（tests/ 使用 Mock 数据，不需要网络）
- 提示词资产（shared/ strategies/ 下的 .md）修改需保持 § 段号契约与输出 schema 兼容
