# output/ 输出目录说明

> 本目录存放各股票的分析产出。目录名格式：`{股票代码}_{公司名称}`。

## 输出约定

| 文件 | 是否生成 | 说明 |
|---|---|---|
| `*_分析报告.md` / `*_估值报告.md` / `qualitative_report.md` | ✅ 默认生成 | **最终交付物**，唯一入库文件（.md） |
| `data_pack_market.md` | ✅ 默认生成 | §1–§17 数据包（LLM 分析的数据底座，可再生） |
| `computed_metrics.md` / `valuation_computed.md` | ✅ 默认生成 | Python 确定性计算（审计与估值引用，可再生） |
| `pdf_sections*.md` / `*.json` | ✅ 有年报 PDF 时生成 | PDF 章节提取文本（可再生） |
| `*.html` | ⛔ **默认不生成** | 见下方说明 |

## HTML 输出策略

**默认不输出 HTML**，Markdown 已足够阅读。仅当用户**明确要求**网页版/打印版/仪表盘时，才用保留的转换脚本生成（纯 Python 转换，不消耗 LLM token）：

```bash
.venv/bin/python scripts/report_to_html.py --input <报告>.md --output <报告>.html --standalone
```

## 报告更新

对已有报告说"更新 XX 的报告"即可：Agent 会以旧报告为基线刷新数据，并在新报告中
增加「较上期变化」对照节（详见根目录 AGENTS.md 约定 #11）。
