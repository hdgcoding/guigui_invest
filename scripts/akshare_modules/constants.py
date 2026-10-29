"""guigui_invest - AKShare field mappings.

Maps Chinese column names (Sina financial statements / Baidu valuation) to the
project-internal Tushare-style column names. The internal contract MUST stay
identical to the original Turtle framework so that derived_metrics.py and
valuation_engine.py remain portable.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Sina income statement (stock_financial_report_sina, symbol="利润表")
#   internal_name -> sina column name
# ---------------------------------------------------------------------------
SINA_INCOME_MAP = {
    "revenue": "营业总收入",
    "oper_cost": "营业成本",
    "biz_tax_surch": "营业税金及附加",
    "sell_exp": "销售费用",
    "admin_exp": "管理费用",
    "rd_exp": "研发费用",
    "finance_exp": "财务费用",
    "assets_impair_loss": "资产减值损失",
    "credit_impair_loss": "信用减值损失",
    "fv_value_chg_gain": "公允价值变动收益",
    "invest_income": "投资收益",
    "asset_disp_income": "资产处置收益",
    "oth_income": "其他收益",
    "operate_profit": "营业利润",
    "non_oper_income": "营业外收入",
    "non_oper_exp": "营业外支出",
    "total_profit": "利润总额",
    "income_tax": "所得税费用",
    "n_income": "净利润",
    "n_income_attr_p": "归属于母公司所有者的净利润",
    "minority_gain": "少数股东损益",
    "basic_eps": "基本每股收益",
    "diluted_eps": "稀释每股收益",
}

# ---------------------------------------------------------------------------
# Sina balance sheet (symbol="资产负债表")
# ---------------------------------------------------------------------------
SINA_BALANCE_MAP = {
    # Assets
    "money_cap": "货币资金",
    "trad_asset": "交易性金融资产",
    "notes_receiv": "应收票据",
    "accounts_receiv": "应收账款",
    "oth_receiv": "其他应收款",
    "prepayment": "预付款项",
    "inventories": "存货",
    "oth_cur_assets": "其他流动资产",
    "total_cur_assets": "流动资产合计",
    "lt_eqt_invest": "长期股权投资",
    "fix_assets": "固定资产净额",
    "cip": "在建工程合计",
    "intang_assets": "无形资产",
    "goodwill": "商誉",
    "defer_tax_assets": "递延所得税资产",
    "total_nca": "非流动资产合计",
    "total_assets": "资产总计",
    # Liabilities
    "st_borr": "短期借款",
    "notes_payable": "应付票据",
    "acct_payable": "应付账款",
    "adv_receipts": "预收款项",
    "contract_liab": "合同负债",
    "payroll_payable": "应付职工薪酬",
    "taxes_payable": "应交税费",
    "oth_payable": "其他应付款",
    "non_cur_liab_due_1y": "一年内到期的非流动负债",
    "oth_cur_liab": "其他流动负债",
    "total_cur_liab": "流动负债合计",
    "lt_borr": "长期借款",
    "bond_payable": "应付债券",
    "lt_payable": "长期应付款",
    "lease_liab": "租赁负债",
    "provisions": "预计非流动负债",
    "defer_tax_liab": "递延所得税负债",
    "total_ncl": "非流动负债合计",
    "total_liab": "负债合计",
    # Equity
    "total_share_capital": "实收资本(或股本)",
    "capital_reserve": "资本公积",
    "surplus_reserve": "盈余公积",
    "undistributed_profit": "未分配利润",
    "total_hldr_eqy_exc_min_int": "归属于母公司股东权益合计",
    "minority_int": "少数股东权益",
    "total_holders_eq": "所有者权益(或股东权益)合计",
}

# ---------------------------------------------------------------------------
# Sina cash flow statement (symbol="现金流量表")
# ---------------------------------------------------------------------------
SINA_CASHFLOW_MAP = {
    # Operating
    "c_recp_sale_goods": "销售商品、提供劳务收到的现金",
    "tax_rend_recd": "收到的税费返还",
    "recp_other_oper": "收到的其他与经营活动有关的现金",
    "c_paid_goods_svr": "购买商品、接受劳务支付的现金",
    "c_pay_to_staff": "支付给职工以及为职工支付的现金",
    "c_paid_for_taxes": "支付的各项税费",
    "pay_other_oper": "支付的其他与经营活动有关的现金",
    "n_cashflow_act": "经营活动产生的现金流量净额",
    # Investing
    "n_recp_disp_fiolta": "处置固定资产、无形资产和其他长期资产所收回的现金净额",
    "c_recpr_invest_income": "取得投资收益收到的现金",
    "c_pay_acq_const_fiolta": "购建固定资产、无形资产和其他长期资产所支付的现金",
    "n_cashflow_inv_act": "投资活动产生的现金流量净额",
    # Financing
    "c_pay_dist_dpcp_int_exp": "分配股利、利润或偿付利息所支付的现金",
    "n_cash_flows_fnc_act": "筹资活动产生的现金流量净额",
}

# ---------------------------------------------------------------------------
# Sina financial analysis indicator (stock_financial_analysis_indicator)
#   internal (Tushare fina_indicator names) -> sina indicator column names
# ---------------------------------------------------------------------------
SINA_FINA_INDICATOR_MAP = {
    "roe": "净资产收益率(%)",           # 摊薄 ROE
    "roe_waa": "加权净资产收益率(%)",
    "grossprofit_margin": "销售毛利率(%)",
    "netprofit_margin": "销售净利率(%)",
    "debt_to_assets": "资产负债率(%)",
    "current_ratio": "流动比率",
    "quick_ratio": "速动比率",
    "assets_turn": "总资产周转率(次)",
    "revenue_yoy": "主营业务收入增长率(%)",
    "netprofit_yoy": "净利润增长率(%)",
    "bps": "每股净资产_调整前(元)",
    "ocfps": "每股经营性现金流(元)",
    "eps": "摊薄每股收益(元)",
    "payout_ratio": "股息发放率(%)",
}

# Sina statement meta columns to carry over
SINA_META_COLS = ["报告日", "类型", "公告日期", "数据源"]

# TTL categories (mirrors original collector):
#   financial statements & indicators -> 7 days
#   market / price series -> 24 hours
CACHE_TTL_CATEGORY = {
    "income": "financial",
    "balancesheet": "financial",
    "cashflow": "financial",
    "dividend": "financial",
    "fina_indicator": "financial",
    "basic_name": "financial",
    "weekly": "market",
    "daily_hist": "market",
    "valuation": "market",
    "risk_free_rate": "market",
}
CACHE_TTL_HOURS = {"financial": 168, "market": 24}

# Never cached (live-ish endpoints)
NEVER_CACHE = {"repurchase", "pledge_detail"}
