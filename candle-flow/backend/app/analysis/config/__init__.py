"""Module weight & threshold defaults — aligned with Nison-style fundamental report."""

# 与对照报告一致：盈利/成长/偿债/现金流/估值；营运效率仅作展示不参与加权。
# 现金流权重约为其余维度的 2 倍（短板否决：避免高成长掩盖现金流致命缺陷）。
MODULE_WEIGHTS = {
    "profitability": 0.20,
    "growth": 0.16,
    "solvency": 0.16,
    "cashflow": 0.32,
    "valuation": 0.16,
}

RISK_THRESHOLD = 60  # below this, composite score is penalized
# 风险扣分上限（占综合分的比例）。
# 风险模块的扣分项（应收恶化、利润含金量低、存贷双高…）与 cashflow/solvency
# 模块高度重叠，若再无上限地整体相乘，等于同一批风险被扣三次并用乘法复利放大
# （risk=44 → ×0.44，直接把 32.8 分砍到 14.4，把"盈利偏弱+现金流紧张"的
# 标的打成 E 级危险股）。故限幅为最大 -25%，生存级风险仍由 compliance_veto 兜底。
RISK_MAX_PENALTY = 0.25

# 维度得分 < 此值视为 E 档，加权贡献再乘惩罚系数
E_GRADE_SCORE = 40
E_GRADE_PENALTY = 0.5

# 现金流一票否决：得分低于门槛则强制降一档评级，并展示红色提示
CASHFLOW_VETO_THRESHOLD = 40
CASHFLOW_VETO_MESSAGE = "现金流不合格，暂不具备价值投资条件"

THRESHOLDS = {
    "roe": {"excellent": (15, 100), "good": (10, 15), "neutral": (5, 10)},
    "roic": {"excellent": (12, 100), "good": (8, 12), "neutral": (4, 8)},
    "gross_margin": {"excellent": (50, 100), "good": (30, 50), "neutral": (15, 30)},
    "debt_ratio": {"excellent": (0, 40), "good": (40, 60), "neutral": (60, 75)},
    "cash_ratio": {"excellent": (1.0, 5.0), "good": (0.7, 1.0), "neutral": (0.4, 0.7)},
}

# 重资产/周期行业：周转率正常区间更低，不可按消费股标准打分。
# 注意：此处按行业名做子串匹配，必须覆盖同一行业在数据源里的实际写法，否则
# 重资产股会掉进轻资产口径被打 0 分。新潮能源（600777）行业名为「油气开采Ⅲ」，
# 既不含「石油」也不含「天然气」，曾因此按 0.3~1.2 的轻资产区间打分 →
# 周转率 0.207 → 0 分 E 级（营业收入/总资产明明属重资产油气开采）。
# 故补齐油气产业链与资源开采类的通用写法。
CAPITAL_HEAVY_INDUSTRY_KEYWORDS = (
    "煤炭", "石油", "天然气", "油气", "油服", "勘探", "开采", "采矿", "矿业",
    "冶炼", "有色", "钢铁", "电力", "公用", "交运", "港口",
    "航运", "机场", "铁路", "高速", "航空", "燃气", "水务", "供热",
    "银行", "保险", "地产", "建筑", "化工", "水泥",
)
