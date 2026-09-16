from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level
from app.analysis.dividend_profile import DIVIDEND_ASSET_WACC_PCT, classify_dividend_asset
from app.analysis.growth_quality import HIGH_GROWTH_WACC_PCT, classify_high_growth_quality


def _estimate_wacc_pct(
    debt_ratio: float | None,
    *,
    is_dividend_asset: bool = False,
    is_high_growth_quality: bool = False,
) -> float:
    """与引擎 DCF 动态 WACC 对齐（百分比）；红利/高成长赛道用更低资本成本。"""
    if is_dividend_asset:
        return DIVIDEND_ASSET_WACC_PCT
    if is_high_growth_quality:
        return HIGH_GROWTH_WACC_PCT
    if debt_ratio is None:
        return 10.0
    dr = float(debt_ratio)
    if dr < 20:
        return 7.0
    if dr <= 30:
        return 8.0
    if dr > 60:
        return 12.0
    return 10.0


def _invested_capital_series(fd: pd.DataFrame) -> pd.Series:
    """
    投入资本优先：权益 + 有息负债 − 货币资金；
    其次权益+有息债；再回退 总资产−流动负债。
    """
    equity = fd["equity"] if "equity" in fd.columns else pd.Series(pd.NA, index=fd.index)
    ibd = (
        pd.to_numeric(fd["interest_bearing_debt"], errors="coerce")
        if "interest_bearing_debt" in fd.columns
        else pd.Series(pd.NA, index=fd.index)
    )
    cash = (
        pd.to_numeric(fd["monetary_funds"], errors="coerce")
        if "monetary_funds" in fd.columns
        else pd.Series(0.0, index=fd.index)
    )
    invested = equity + ibd.fillna(0) - cash.fillna(0)
    # 权益缺失或结果非正时回退
    ta = fd["total_assets"] if "total_assets" in fd.columns else pd.Series(pd.NA, index=fd.index)
    cl = (
        pd.to_numeric(fd["current_liabilities"], errors="coerce")
        if "current_liabilities" in fd.columns
        else pd.Series(0.0, index=fd.index)
    )
    fallback = ta - cl.fillna(0)
    out = invested.where(invested.notna() & (invested > 0), fallback)
    return out.replace(0, pd.NA)


class ProfitabilityAnalyzer(BaseAnalyzer):
    """ROE / ROIC / 毛利率 / 净利率 + 杜邦分解。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        if financial_data.empty:
            return ModuleResult("盈利能力", 0, AnalysisLevel.DANGER, warnings=["暂无财务数据"])

        fd = financial_data.copy()
        annual_dates = kwargs.get("annual_dates") or list(fd.index)
        annual_period = ""
        if annual_dates:
            last = format_report_period(str(annual_dates[-1]))
            annual_period = last or str(annual_dates[-1])

        equity = fd["equity"].replace(0, pd.NA)
        revenue = fd["revenue"].replace(0, pd.NA)
        # 评分用年报 ROE（kwargs.latest_roe 已由 financials 设为年报末值）
        if "roe" in fd.columns and fd["roe"].notna().any():
            roe_series = fd["roe"].dropna()
        else:
            roe_series = (fd["net_profit"] / equity * 100).dropna()
        latest_roe = kwargs.get("latest_roe")
        roe = float(latest_roe) if latest_roe is not None else (
            float(roe_series.iloc[-1]) if len(roe_series) else 0.0
        )

        net_margin = float((fd["net_profit"] / revenue).iloc[-1]) if len(revenue.dropna()) else 0.0
        asset_turnover = float((fd["revenue"] / fd["total_assets"].replace(0, pd.NA)).iloc[-1]) if "total_assets" in fd else 0.0
        equity_multiplier = float((fd["total_assets"] / equity).iloc[-1]) if len(equity.dropna()) else 0.0

        # 周期/蓝筹 ROE：12%+ 已属优秀（对照神华等龙头报告）
        roe_score, roe_level = self._score_by_range(roe, (12, 100), (8, 12), (5, 8))
        indicators.append(
            IndicatorResult(
                name="ROE(%)",
                value=round(roe, 2),
                score=roe_score,
                level=roe_level,
                trend=self._calc_trend(roe_series),
                weight=3.0,
                comment=f"杜邦: 净利率{net_margin:.1%} × 周转{asset_turnover:.2f} × 权益乘数{equity_multiplier:.2f}",
                period=annual_period,
            )
        )

        # 毛利率：优先真实成本；亦可直接读已算好的 gross_margin 列
        gross_margin_val: float | None = None
        if "cogs" in fd.columns and fd["cogs"].notna().any():
            gm_series = ((fd["revenue"] - fd["cogs"]) / revenue * 100).dropna()
            if len(gm_series):
                gross_margin_val = float(gm_series.iloc[-1])
                gm_score, gm_level = self._score_by_range(gross_margin_val, (50, 100), (30, 50), (15, 30))
                indicators.append(
                    IndicatorResult(
                        name="毛利率(%)",
                        value=round(gross_margin_val, 2),
                        score=gm_score,
                        level=gm_level,
                        trend=self._calc_trend(gm_series),
                        weight=2.0,
                        period=annual_period,
                    )
                )
        elif "gross_margin" in fd.columns and fd["gross_margin"].notna().any():
            gm_series = fd["gross_margin"].dropna()
            gross_margin_val = float(gm_series.iloc[-1])
            gm_score, gm_level = self._score_by_range(gross_margin_val, (50, 100), (30, 50), (15, 30))
            indicators.append(
                IndicatorResult(
                    name="毛利率(%)",
                    value=round(gross_margin_val, 2),
                    score=gm_score,
                    level=gm_level,
                    trend=self._calc_trend(gm_series),
                    weight=2.0,
                    period=annual_period,
                )
            )

        profile_gm = kwargs.get("latest_gross_margin")
        if profile_gm is None:
            profile_gm = gross_margin_val
        growth_profile = classify_high_growth_quality(
            gross_margin_pct=float(profile_gm) if profile_gm is not None else None,
            profit_yoy_pct=kwargs.get("profit_yoy"),
        )
        is_hgq = bool(growth_profile.get("is_high_growth_quality"))

        div_profile = classify_dividend_asset(
            dividend_yield_pct=kwargs.get("dividend_yield"),
            pe_ttm=kwargs.get("pe_ttm"),
            payout_ratio_pct=kwargs.get("payout_ratio_pct"),
        )
        is_div = bool(div_profile.get("is_dividend_asset"))
        debt_ratio = kwargs.get("debt_ratio")
        wacc_pct = _estimate_wacc_pct(
            float(debt_ratio) if debt_ratio is not None else None,
            is_dividend_asset=is_div,
            is_high_growth_quality=is_hgq,
        )
        roic_below_wacc = False
        roic_value: float | None = None

        if "operating_profit" in fd.columns and (
            "total_assets" in fd.columns or "equity" in fd.columns
        ):
            invested = _invested_capital_series(fd)
            nopat = fd["operating_profit"] * 0.75
            roic_series = (nopat / invested * 100).dropna()
            roic = float(roic_series.iloc[-1]) if len(roic_series) else 0.0
            roic_value = round(roic, 2)
            op_est = False
            if "operating_profit_estimated" in fd.columns:
                try:
                    op_est = float(fd["operating_profit_estimated"].iloc[-1] or 0) >= 1
                except Exception:
                    op_est = False
            # 红利/公用事业：ROIC 绝对门槛略降；高成长赛道按扩张期标准
            if is_div:
                roic_score, roic_level = self._score_by_range(roic, (8, 100), (5, 8), (3, 5))
            elif is_hgq:
                roic_score, roic_level = self._score_by_range(roic, (10, 100), (7, 10), (4, 7))
            else:
                roic_score, roic_level = self._score_by_range(roic, (12, 100), (8, 12), (4, 8))

            wacc_label = (
                "红利资产WACC"
                if is_div
                else ("高成长赛道WACC" if is_hgq else "WACC")
            )
            if roic < wacc_pct:
                if is_div or is_hgq:
                    roic_below_wacc = False
                    if is_div:
                        dy = div_profile.get("dividend_yield_pct")
                        po = div_profile.get("payout_ratio_pct")
                        roic_comment = (
                            f"ROIC {roic:.1f}% vs {wacc_label}≈{wacc_pct:.1f}%："
                            f"高股息（股息率{dy}% / 估分红率{po}%）以股东现金回报衡量，"
                            f"不适用成长股 ROIC 对资本成本的一票否决框架"
                        )
                        warnings.append(
                            f"ROIC({roic:.1f}%)略低于红利口径WACC(≈{wacc_pct:.1f}%)，"
                            f"但高股息资产以分红回报为主，不按价值否决处理"
                        )
                    else:
                        gm = growth_profile.get("gross_margin_pct")
                        yoy = growth_profile.get("profit_yoy_pct")
                        roic_comment = (
                            f"ROIC {roic:.1f}% vs {wacc_label}≈{wacc_pct:.1f}%："
                            f"高毛利({gm}%)+高增长(净利同比{yoy}%)扩张期，"
                            f"资本成本按赛道下调，不按毁灭价值否决"
                        )
                        warnings.append(
                            f"高毛利高成长赛道：WACC 下调至≈{wacc_pct:.1f}%，"
                            f"ROIC({roic:.1f}%)不作价值毁灭一票否决"
                        )
                    roic_score = max(40.0, roic_score)
                    roic_level = score_to_level(roic_score)
                else:
                    roic_below_wacc = True
                    roic_comment = (
                        f"ROIC {roic:.1f}% < WACC≈{wacc_pct:.0f}%：投入资本回报低于资本成本，"
                        f"可能在毁灭股东价值"
                    )
                    if debt_ratio is not None and float(debt_ratio) >= 55:
                        roic_comment += "；叠加较高杠杆，风险更大"
                        warnings.append(
                            f"ROIC({roic:.1f}%)低于WACC(≈{wacc_pct:.0f}%)且资产负债率偏高，价值创造存疑"
                        )
                    else:
                        warnings.append(
                            f"ROIC({roic:.1f}%)低于估计WACC(≈{wacc_pct:.0f}%)，经济利润偏弱"
                        )
                    roic_score = max(15.0, roic_score - 10)
                    roic_level = score_to_level(roic_score)
            else:
                if is_div:
                    roic_comment = (
                        f"ROIC {roic:.1f}% ≥ {wacc_label}≈{wacc_pct:.1f}%："
                        f"覆盖资本成本，持续为股东创造现金回报"
                    )
                elif is_hgq:
                    roic_comment = (
                        f"ROIC {roic:.1f}% ≥ {wacc_label}≈{wacc_pct:.1f}%："
                        f"高毛利高成长赛道下覆盖资本成本，生意回报强"
                    )
                else:
                    roic_comment = f"ROIC {roic:.1f}% ≥ WACC≈{wacc_pct:.0f}%：创造经济利润"
            if op_est:
                roic_comment += "（营业利润为估算，待利润表校准）"
            indicators.append(
                IndicatorResult(
                    name="ROIC(%)",
                    value=round(roic, 2),
                    score=roic_score,
                    level=roic_level,
                    trend=self._calc_trend(roic_series),
                    weight=2.5,
                    comment=roic_comment,
                    period=annual_period,
                )
            )

        nm_pct = net_margin * 100
        # 煤炭等净利率 15%+ 已属优秀
        nm_score, nm_level = self._score_by_range(nm_pct, (15, 100), (8, 15), (3, 8))
        indicators.append(
            IndicatorResult(
                name="净利率(%)",
                value=round(nm_pct, 2),
                score=nm_score,
                level=nm_level,
                trend=self._calc_trend((fd["net_profit"] / revenue * 100).dropna()),
                weight=2.0,
                period=annual_period,
            )
        )

        if is_div and div_profile.get("dividend_yield_pct") is not None:
            dy = float(div_profile["dividend_yield_pct"])
            sp = div_profile.get("div_bond_spread_pct")
            indicators.append(
                IndicatorResult(
                    name="股息率(%)",
                    value=round(dy, 2),
                    score=90.0 if dy >= 5 else 78.0,
                    level=AnalysisLevel.EXCELLENT if dy >= 5 else AnalysisLevel.GOOD,
                    weight=2.0,
                    comment=(
                        f"红利资产：估分红率{div_profile.get('payout_ratio_pct')}%，"
                        f"相对十年国债利差约 {sp}pct"
                    ),
                    period="TTM",
                )
            )

        if roe > 30 and equity_multiplier > 4:
            warnings.append("ROE较高但杠杆倍数偏大，需关注债务风险")
        if gross_margin_val is not None and gross_margin_val < 10:
            warnings.append("毛利率过低，竞争优势可能不足")

        module_score = self._weighted_score(indicators)
        return ModuleResult(
            module_name="盈利能力",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={
                "dupont": {
                    "net_margin": net_margin,
                    "asset_turnover": asset_turnover,
                    "equity_multiplier": equity_multiplier,
                },
                "wacc_pct": wacc_pct,
                "roic": roic_value,
                "roic_below_wacc": roic_below_wacc,
                "is_dividend_asset": is_div,
                "dividend_profile": div_profile,
                "is_high_growth_quality": is_hgq,
                "growth_quality": growth_profile,
                "gross_margin": round(gross_margin_val, 2) if gross_margin_val is not None else None,
            },
        )
