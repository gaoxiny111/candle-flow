"""Module weight & threshold defaults — aligned with Nison-style fundamental report."""

# 默认权重（一般制造）；实际合成按 sector_profile 切换。
# 现金流仍最重但不高于估值过多，避免口径失真时压垮综合分。
MODULE_WEIGHTS = {
    "profitability": 0.20,
    "growth": 0.16,
    "solvency": 0.16,
    "cashflow": 0.28,
    "valuation": 0.20,
}

RISK_THRESHOLD = 60  # below this, composite score is penalized
# 风险扣分上限：与偿债/现金流高度重叠，故限幅降至 12%；
# 合成层再按偿债/现金流是否已低分做去重折减。生存级仍由 compliance_veto 兜底。
RISK_MAX_PENALTY = 0.12

# 维度低分软折价（不再对 <40 一律 ×0.5，避免单维失真压垮综合分）
E_GRADE_SCORE = 40
E_GRADE_SOFT_PENALTY = 0.8  # score < 40
E_MID_SCORE = 55
E_MID_SOFT_PENALTY = 0.95  # 40 ≤ score < 55
# 兼容旧名：测试/外部若仍引用 E_GRADE_PENALTY，等同软折价
E_GRADE_PENALTY = E_GRADE_SOFT_PENALTY

# 现金流一票否决：得分低于门槛则强制降一档评级，并展示红色提示
# （否决维在合成时不再叠加软折价）
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
