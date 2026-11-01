---
id: pdf-pipeline
type: module-design
title: 年报 PDF 下载与文本提取
status: draft
parent: guigui-architecture
---

# 年报 PDF 下载与文本提取

**代码**：`scripts/download_report.py`、`scripts/pdf_preprocessor.py`

## 职责

- **下载**（download_report.py）：搜索并下载年报 PDF 到标的目录
- **提取**（pdf_preprocessor.py）：关键词章节摘要（P2 受限资产 / P3 应收账龄 / P4 关联交易 / P6 或有负债 / P13 非经常性损益 / MDA / SUB）+ 全文分片（~120K 字符/片）+ manifest（页码与分片映射、空白页标记）；重跑自动清理本程序旧分片

## 边界

- **无 OCR**：扫描/乱码 PDF 返回失败并要求 OCR/外部来源补充，不得声称已解析
- 关键词命中 ≠ 整章阅读；生成全文 ≠ 已读全文（供 [qualitative-analysis](qualitative-analysis.md) 文本优先阅读，需要核表时才回看原 PDF）
- 独立工具：不依赖采集/估值层，仅 pdfplumber + 文本处理
