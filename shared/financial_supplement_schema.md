# 来源核对补充文件（v1）

可放在标的目录的`financial_supplement.json`（采集器自动加载），或显式传`--supplement <path>`。用于网络接口不能识别的归母现金、母公司表、审计意见与公告版本核对。**它不是独立分析审计PASS，不能为了得到买点而填零或确认同范围。**

顶层必需：`schema_version: 1`、`ts_code: "600900.SH"`、`amount_unit: "yuan"`。金额全部原始元，EPS为元/股；股数为实际股。

每条记录必需：
- `end_date`：YYYYMMDD报告期；
- `source`：原始年报/公告URL或本地文件；
- `locator`：页码/表名/公告位置；
- `review_status: "source_reconciled"`：已实际核对原文；
- `reconciliation_note`：合并范围、版本及各调整项核对说明。

可选数组：

| 字段 | 记录内容 |
|---|---|
| `statement_reconciliations` | `scope: "consolidated"`、`same_reporting_basis: true`；说明不同公告日期为何仍为同一重述/合并范围，不能仅凭日期相同推断 |
| `parent_statements` | `scope: "parent"`、`kind: "income_parent"`或`"balance_sheet_parent"`、`values`为内部字段名到原始金额的映射；见constants.py |
| `historical_shares` | `scope: "consolidated"`、`total_shares`为对应报告期实际股数，用于历史PS；不能把实收资本金额当股数（股票面值未必为1元） |
| `owner_cashflow` | 完整年度，`scope: "parent_attributable"`；字段见下；至少最近两个连续年度 |
| `fcff` | `scope: "consolidated"`、`fcff_yuan`、`interest_treatment`、`tax_treatment`、`working_capital_treatment`、`minority_treatment`；DCF还需完整现金、债务和少数股东数据 |

`owner_cashflow`每年必需明确给出：`ocf_yuan`、`capex_yuan`、`interest_paid_yuan`、`interest_classification: "operating"或"financing"`、`minority_claim_yuan`、`restricted_cash_increase_yuan`、`nonrecurring_net_inflow_yuan`。即便为零也须有核对依据。OCF和Capex须与同次快照相符；不能用补充文件悄悄改掉底表。

确定性计算：归母可用现金 = OCF − Capex − 融资分类的已付利息 − 少数股东现金主张 − 受限现金增加 − 非经常净流入。经营分类利息已在OCF中扣除，不重复扣减；Capex使用全部支出，不擅自加回“成长性开支”。少数股东现金主张不等于当年实付少数股东股息，必须核对归属和留存需求。

可选对象：
- `shares`：除上述来源字段外，提供`quote_date`（与同次行情日期完全相同）和`total_shares`（实际总股数）。同日市值/价格推算不能自动变成已核实股本。
- `audit`：除来源字段外提供`opinion`（明确意见类型）；这是年报审计意见，不是本分析的numeric_audit结果。

只有来源核对现金、股本及跨表版本均有效，决策证据才会给出正的目标价；现金结余非正仍无正买点。任何不完整/矛盾记录拒绝加载，不部分修改底表。来源证据须由后续独立numeric_audit复核。
