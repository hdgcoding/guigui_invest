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

## 选股器（两级筛选）

**Tier 1 — 批量过滤**（全市场快照，5 次批量接口，~1 分钟）：

| 数据 | 来源 |
|---|---|
| 价格/名称/成交额 | `stock_zh_a_spot`（新浪全市场） |
| EPS/BPS/ROE/毛利率/行业 | `stock_yjbb_em`（东财业绩报表，最新年报期） |
| TTM 每股股息 + 总股本 | `stock_fhps_em`（东财分红送配，近 5 个报告期聚合） |
| 上市日期 | `stock_info_sh/sz_name_code`（交易所） |

过滤：排除 ST/PT/退市/银行（可配）→ 上市 ≥3 年 → 市值 ≥5 亿 → 日成交额 ≥0.1 亿（换手率代理）→ 0<PB≤10 → 双通道 PE（主通道 0<PE≤50 + 股息率>0；观察通道亏损股按市值前 50）→ 排名 `0.4×dv + 0.3×(1/PE) + 0.3×(1/PB)`（归一化）取前 150。

**Tier 2 — 深度分析**（逐只，复用 AKShareClient 采集器 + TTL 缓存）：
- 硬否决：质押比 >70%（审计意见 AKShare 不可得，跳过）
- 财务质量：ROE ≥8%、毛利率 ≥15%、资产负债率 ≤70%（新浪指标限流时自动从三大报表推导）
- 因子 2：M（3 年支付率）/ AA / 穿透率 R / 门槛 II（max(3.5%, Rf+2%)）
- 因子 4：EV/EBITDA / FCF 收益率 / 现金调整 PE / 商誉占比
- 底价 5 法均值：净流动资产/BVPS/10 年低点/股息隐含/悲观 FCF
- 综合评分：ROE 20% + FCF 收益率 20% + 穿透率 R 25% + EV/EBITDA(逆) 15% + 底价溢价(逆) 20%

> 注：Tier 1 的 PE 为静态口径（最新年报 EPS），与原版 PE_TTM 略有差异（文档化近似）。

## PDF-first 深度分析（GLM 文本优先）

```bash
# 1. 下载年报（或由 /download-annual-report 命令完成）
# 2. 提取文本：章节摘要 + 全文分片
.venv/bin/python scripts/pdf_preprocessor.py --pdf <年报.pdf> \
  --output output/{code}_{company}/pdf_sections.json \
  --text-output output/{code}_{company}/pdf_sections_text.md --full-text
```

产出（伊利 2025 年报 259 页实测，7/7 章节命中）：
- `pdf_sections_text.md` — 7 章节摘要（P2 治理/P3 会计政策/P4 应收/P6 合并报表/P13 风险/MDA/SUB）
- `pdf_sections_text_fulltext_part1..N.md` — 全文分片（~120K 字符/片），Agent 按需读取

`/business-analysis` 命令已内置该流程：先读章节摘要 → 需要时按优先级读全文分片 → 提取附注生成 `data_pack_report.md`。

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

# 选股器（两级筛选）
.venv/bin/python scripts/screener_core.py --tier1-only              # Tier 1 快速筛选（~1分钟）
.venv/bin/python scripts/screener_core.py --tier2-limit 30          # 完整流程（限制 Tier 2 数量）
.venv/bin/python scripts/screener_core.py --min-roe 10 --max-pe 30  # 自定义阈值
.venv/bin/python scripts/screener_core.py --csv output/s.csv --html output/s.html  # 导出
.venv/bin/python scripts/screener_core.py --cache-refresh           # 刷新缓存

# 年报 PDF 文本提取（PDF-first 深度分析, GLM 文本优先）
.venv/bin/python scripts/pdf_preprocessor.py \
  --pdf output/{code}_{company}/*年报*.pdf \
  --output output/{code}_{company}/pdf_sections.json \
  --text-output output/{code}_{company}/pdf_sections_text.md --full-text

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
│   ├── screener_core.py         # 选股器（两级筛选 Tier 1 + Tier 2）
│   ├── screener_config.py       # 选股器阈值/权重配置
│   ├── quality_control.py       # 杠杆1: 确定性预计算 CM§1-5
│   ├── report_consistency.py    # 杠杆6: 跨段数值冲突扫描
│   ├── pdf_preprocessor.py      # 年报 PDF 章节提取 + 文本导出（--text-output/--full-text）
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
AKSHARE_RATE_DELAY=0 .venv/bin/python -m pytest tests/ -v   # 70 tests, no network
```

覆盖：代码验证 / store 契约 / 中期+末期分红合并支付率 / §17 衍生指标全链路 / 新浪报表归一化 / 周线重采样 / 数据包分段解析 / 风险检测 / 选股器 Tier 1 过滤与排名 / 综合评分 / PDF 文本导出。

## License

MIT（继承原项目）
