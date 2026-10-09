"""个股基本面：行业口径隔离 + 自适应模块权重。

合成前先定画像，避免银行/保险/券商被通用 FCF、制造业负债率拖死；
红利/成长/周期则切换估值与现金流权重。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.analysis.config import MODULE_WEIGHTS
from app.analysis.config.company_profiles import business_model_of, is_bank

# ── 权重表（五维之和 = 1.0；资产质量并入偿债维展示）────────────────
# 一般制造：现金流仍最重，但 28%（原 32%）；估值抬到 20%
WEIGHTS_GENERAL: dict[str, float] = {
    "profitability": 0.20,
    "growth": 0.16,
    "solvency": 0.16,
    "cashflow": 0.28,
    "valuation": 0.20,
}

# 银行/保险/券商：弱化通用现金流，偿债含「资本/资产质量」权重
WEIGHTS_FINANCIAL: dict[str, float] = {
    "profitability": 0.20,
    "growth": 0.16,
    "solvency": 0.26,  # 原偿债 16% + 资产质量口径 10%
    "cashflow": 0.14,
    "valuation": 0.24,
}

# 地产：经营现金流仍重要，但少用制造业 FCF 否决
WEIGHTS_PROPERTY: dict[str, float] = {
    "profitability": 0.18,
    "growth": 0.14,
    "solvency": 0.20,
    "cashflow": 0.20,
    "valuation": 0.28,
}

# 红利/价值：抬估值、现金流仍关键
WEIGHTS_DIVIDEND: dict[str, float] = {
    "profitability": 0.20,
    "growth": 0.14,
    "solvency": 0.16,
    "cashflow": 0.24,
    "valuation": 0.26,
}

# 成长：现金流保持较高，估值维持 16%
WEIGHTS_GROWTH: dict[str, float] = {
    "profitability": 0.20,
    "growth": 0.18,
    "solvency": 0.14,
    "cashflow": 0.28,
    "valuation": 0.20,
}

# 周期：估值与现金流均衡；周期风险放进估值折减，不另开权重
WEIGHTS_CYCLICAL: dict[str, float] = {
    "profitability": 0.18,
    "growth": 0.14,
    "solvency": 0.16,
    "cashflow": 0.22,
    "valuation": 0.30,
}

FINANCIAL_INDUSTRY_KEYS: tuple[str, ...] = (
    "银行",
    "保险",
    "证券",
    "多元金融",
    "非银金融",
    "券商",
)
PROPERTY_INDUSTRY_KEYS: tuple[str, ...] = ("房地产", "房地产开发", "房地产服务", "地产")
UTILITY_ROIC_EXEMPT_KEYS: tuple[str, ...] = (
    "公用事业",
    "电力",
    "水务",
    "燃气",
    "热力",
    "电信",
    "通信服务",
    "高速公路",
    "港口",
    "机场",
    "铁路",
)


@dataclass(frozen=True)
class SectorProfile:
    """合成层画像。"""

    kind: str  # bank|insurance|broker|property|dividend|growth|cyclical|general
    label: str
    weights: dict[str, float]
    # 关闭通用 FCF/分红 FCF 覆盖等制造业现金流口径
    disable_generic_fcf: bool = False
    # 禁用 ROIC<WACC 价值陷阱锁估值
    disable_roic_wacc_trap: bool = False
    # 现金流<40 时不降档（金融口径本就不适用）
    soft_cashflow_veto: bool = False
    # 估值侧不因现金流低分折减
    skip_valuation_cf_haircut: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "weights": dict(self.weights),
            "disable_generic_fcf": self.disable_generic_fcf,
            "disable_roic_wacc_trap": self.disable_roic_wacc_trap,
            "soft_cashflow_veto": self.soft_cashflow_veto,
            "skip_valuation_cf_haircut": self.skip_valuation_cf_haircut,
        }


def _ind_hit(industry: str, keys: tuple[str, ...]) -> bool:
    blob = str(industry or "")
    return any(k in blob for k in keys)


def _financial_subkind(industry: str, *, name: str = "", symbol: str = "") -> str | None:
    if is_bank(name, symbol, industry) or _ind_hit(industry, ("银行",)):
        return "bank"
    if _ind_hit(industry, ("保险",)):
        return "insurance"
    if _ind_hit(industry, ("证券", "券商", "多元金融", "非银金融")):
        return "broker"
    if _ind_hit(industry, FINANCIAL_INDUSTRY_KEYS):
        return "broker"
    return None


def resolve_sector_profile(
    *,
    industry: str = "",
    name: str = "",
    symbol: str = "",
    is_dividend_asset: bool = False,
    is_growth_stock: bool = False,
    is_cyclical: bool = False,
) -> SectorProfile:
    """按行业与资产属性解析画像；优先级：金融 > 地产 > 红利 > 成长 > 周期 > 一般。"""
    fin = _financial_subkind(industry, name=name, symbol=symbol)
    if fin == "bank":
        return SectorProfile(
            kind="bank",
            label="银行",
            weights=dict(WEIGHTS_FINANCIAL),
            disable_generic_fcf=True,
            disable_roic_wacc_trap=True,
            soft_cashflow_veto=True,
            skip_valuation_cf_haircut=True,
        )
    if fin == "insurance":
        return SectorProfile(
            kind="insurance",
            label="保险",
            weights=dict(WEIGHTS_FINANCIAL),
            disable_generic_fcf=True,
            disable_roic_wacc_trap=True,
            soft_cashflow_veto=True,
            skip_valuation_cf_haircut=True,
        )
    if fin == "broker":
        return SectorProfile(
            kind="broker",
            label="券商/非银",
            weights=dict(WEIGHTS_FINANCIAL),
            disable_generic_fcf=True,
            disable_roic_wacc_trap=True,
            soft_cashflow_veto=True,
            skip_valuation_cf_haircut=True,
        )
    if _ind_hit(industry, PROPERTY_INDUSTRY_KEYS):
        return SectorProfile(
            kind="property",
            label="地产",
            weights=dict(WEIGHTS_PROPERTY),
            disable_generic_fcf=True,
            disable_roic_wacc_trap=False,
            soft_cashflow_veto=False,
            skip_valuation_cf_haircut=False,
        )
    if _ind_hit(industry, UTILITY_ROIC_EXEMPT_KEYS):
        # 公用/电信：权重用一般或红利；强制豁免 ROIC 陷阱
        base = dict(WEIGHTS_DIVIDEND if is_dividend_asset else WEIGHTS_GENERAL)
        return SectorProfile(
            kind="utility",
            label="公用/电信",
            weights=base,
            disable_generic_fcf=False,
            disable_roic_wacc_trap=True,
            soft_cashflow_veto=False,
            skip_valuation_cf_haircut=False,
        )
    if is_dividend_asset:
        return SectorProfile(
            kind="dividend",
            label="红利/价值",
            weights=dict(WEIGHTS_DIVIDEND),
            disable_generic_fcf=False,
            disable_roic_wacc_trap=True,
            soft_cashflow_veto=False,
            skip_valuation_cf_haircut=False,
        )
    if is_growth_stock:
        return SectorProfile(
            kind="growth",
            label="成长",
            weights=dict(WEIGHTS_GROWTH),
            disable_generic_fcf=False,
            disable_roic_wacc_trap=False,
            soft_cashflow_veto=False,
            skip_valuation_cf_haircut=False,
        )
    if is_cyclical:
        return SectorProfile(
            kind="cyclical",
            label="周期",
            weights=dict(WEIGHTS_CYCLICAL),
            disable_generic_fcf=False,
            disable_roic_wacc_trap=False,
            soft_cashflow_veto=False,
            skip_valuation_cf_haircut=False,
        )
    # 分销商业模式仍用一般权重，但现金流模块自有保底
    _ = business_model_of(name, symbol, industry)
    return SectorProfile(
        kind="general",
        label="一般",
        weights=dict(WEIGHTS_GENERAL),
    )


def module_weights_for(profile: SectorProfile | None) -> dict[str, float]:
    if profile is None:
        return dict(MODULE_WEIGHTS)
    return dict(profile.weights)
