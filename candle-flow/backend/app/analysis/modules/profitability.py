from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, score_to_level


class ProfitabilityAnalyzer(BaseAnalyzer):
    """ROE / ROIC / 毛利率 / 净利率 + 杜邦分解。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        if financial_data.empty:
            return ModuleResult("盈利能力", 0, AnalysisLevel.DANGER, warnings=["暂无财务数据"])

        fd = financial_data.copy()
        equity = fd["equity"].replace(0, pd.NA)
        revenue = fd["revenue"].replace(0, pd.NA)
        roe_series = (fd["net_profit"] / equity * 100).dropna()
        # 优先用最新报告期 ROE（可中报）；否则用年报序列末值
        latest_roe = kwargs.get("latest_roe")
        roe = float(latest_roe) if latest_roe is not None else (
            float(roe_series.iloc[-1]) if len(roe_series) else 0.0
        )

        net_margin = float((fd["net_profit"] / revenue).iloc[-1]) if len(revenue.dropna()) else 0.0
        asset_turnover = float((fd["revenue"] / fd["total_assets"].replace(0, pd.NA)).iloc[-1]) if "total_assets" in fd else 0.0
        equity_multiplier = float((fd["total_assets"] / equity).iloc[-1]) if len(equity.dropna()) else 0.0

        # 周期股 ROE 10% 附近视为良好（对照煤炭行业中上水平）
        roe_score, roe_level = self._score_by_range(roe, (14, 100), (9, 14), (5, 9))
        indicators.append(
            IndicatorResult(
                name="ROE(%)",
                value=round(roe, 2),
                score=roe_score,
                level=roe_level,
                trend=self._calc_trend(roe_series),
                weight=3.0,
                comment=f"杜邦: 净利率{net_margin:.1%} × 周转{asset_turnover:.2f} × 权益乘数{equity_multiplier:.2f}",
            )
        )

        if "operating_profit" in fd.columns and "total_assets" in fd.columns:
            invested = (
                fd["total_assets"]
                - fd.get("current_liabilities", pd.Series(0, index=fd.index))
                - fd.get("non_interest_liabilities", pd.Series(0, index=fd.index))
            ).replace(0, pd.NA)
            nopat = fd["operating_profit"] * 0.75
            roic_series = (nopat / invested * 100).dropna()
            roic = float(roic_series.iloc[-1]) if len(roic_series) else 0.0
            roic_score, roic_level = self._score_by_range(roic, (12, 100), (8, 12), (4, 8))
            indicators.append(
                IndicatorResult(
                    name="ROIC(%)",
                    value=round(roic, 2),
                    score=roic_score,
                    level=roic_level,
                    trend=self._calc_trend(roic_series),
                    weight=2.5,
                )
            )

        if "cogs" in fd.columns:
            gm_series = ((fd["revenue"] - fd["cogs"]) / revenue * 100).dropna()
            gross_margin = float(gm_series.iloc[-1]) if len(gm_series) else 0.0
            gm_score, gm_level = self._score_by_range(gross_margin, (50, 100), (30, 50), (15, 30))
            indicators.append(
                IndicatorResult(
                    name="毛利率(%)",
                    value=round(gross_margin, 2),
                    score=gm_score,
                    level=gm_level,
                    trend=self._calc_trend(gm_series),
                    weight=2.0,
                )
            )

        nm_pct = net_margin * 100
        nm_score, nm_level = self._score_by_range(nm_pct, (20, 100), (10, 20), (3, 10))
        indicators.append(
            IndicatorResult(
                name="净利率(%)",
                value=round(nm_pct, 2),
                score=nm_score,
                level=nm_level,
                trend=self._calc_trend((fd["net_profit"] / revenue * 100).dropna()),
                weight=2.0,
            )
        )

        if roe > 30 and equity_multiplier > 4:
            warnings.append("ROE较高但杠杆倍数偏大，需关注债务风险")
        if indicators and indicators[-1].value < 10 and "毛利率" in [i.name for i in indicators]:
            gm = next(i.value for i in indicators if i.name.startswith("毛利率"))
            if gm < 10:
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
                }
            },
        )
