"""Module weight & threshold defaults — aligned with Nison-style fundamental report."""

# 与对照报告一致：盈利/成长/偿债/现金流/估值；营运效率仅作展示不参与加权
MODULE_WEIGHTS = {
    "profitability": 0.25,
    "growth": 0.20,
    "solvency": 0.20,
    "cashflow": 0.20,
    "valuation": 0.15,
}

RISK_THRESHOLD = 60  # below this, composite score is penalized

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
