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

# 重资产/周期行业：周转率正常区间更低，不可按消费股标准打分
CAPITAL_HEAVY_INDUSTRY_KEYWORDS = (
    "煤炭", "石油", "天然气", "有色", "钢铁", "电力", "公用", "交运", "港口",
    "航运", "机场", "银行", "保险", "地产", "建筑", "化工", "水泥",
)
