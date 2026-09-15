from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level


class CashflowAnalyzer(BaseAnalyzer):
    """经营现金流含金量、自由现金流、资本开支压力、增长质量背离。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        if financial_data.empty:
            return ModuleResult("现金流质量", 0, AnalysisLevel.DANGER, warnings=["暂无现金流数据"])

        fd = financial_data
        annual_dates = kwargs.get("annual_dates") or list(fd.index)
        annual_period = format_report_period(str(annual_dates[-1])) if annual_dates else ""
        latest_period = format_report_period(kwargs.get("latest_report")) or annual_period

        ocf = fd.get("operating_cashflow", pd.Series(dtype=float))
        net_profit = fd["net_profit"].replace(0, pd.NA)
        capex = fd.get("capital_expenditure", pd.Series(0, index=fd.index))
        fcf = ocf - capex
        cash_ratio: float | None = None

        if len(ocf.dropna()) and len(net_profit.dropna()):
            cash_ratio_series = (ocf / net_profit).replace([np.inf, -np.inf], np.nan).dropna()
            cash_ratio = float(cash_ratio_series.iloc[-1]) if len(cash_ratio_series) else 0.0
            score, level = self._score_by_range(cash_ratio, (1.0, 5.0), (0.7, 1.0), (0.4, 0.7))
            indicators.append(
                IndicatorResult(
                    name="经营现金流/净利润",
                    value=round(cash_ratio, 2),
                    score=score,
                    level=level,
                    trend=self._calc_trend(cash_ratio_series),
                    weight=3.0,
                    comment=">1 利润含金量高，<0.5 警惕利润注水",
                    period=annual_period,
                )
            )

        if len(fcf.dropna()):
            fcf_latest = float(fcf.iloc[-1])
            fcf_positive = int((fcf > 0).sum())
            indicators.append(
                IndicatorResult(
                    name="自由现金流(元)",
                    value=round(fcf_latest, 0),
                    score=70 if fcf_latest > 0 else 30,
                    level=AnalysisLevel.GOOD if fcf_latest > 0 else AnalysisLevel.POOR,
                    trend=self._calc_trend(fcf),
                    weight=2.5,
                    comment=f"近{len(fcf)}期中有{fcf_positive}期为正",
                    period=annual_period,
                )
            )

        if len(ocf.dropna()) and ocf.iloc[-1] > 0:
            capex_ratio = float(capex.iloc[-1] / ocf.iloc[-1]) if ocf.iloc[-1] else 999.0
            cs, cl = self._score_by_range(capex_ratio, (0, 0.4), (0.4, 0.7), (0.7, 1.2))
            indicators.append(
                IndicatorResult(
                    name="资本支出/经营现金流",
                    value=round(capex_ratio, 2),
                    score=cs,
                    level=cl,
                    weight=2.0,
                    period=annual_period,
                )
            )

        ocf_ps = kwargs.get("ocf_per_share")
        if ocf_ps is not None:
            indicators.append(
                IndicatorResult(
                    name="每股经营现金流(元)",
                    value=round(float(ocf_ps), 3),
                    score=self._linear_score(float(ocf_ps), 0, 2),
                    level=AnalysisLevel.GOOD if float(ocf_ps) > 0.5 else AnalysisLevel.NEUTRAL,
                    weight=1.5,
                    period=latest_period,
                )
            )

        # 利润增速 vs 现金流质量背离
        yoy_profit = kwargs.get("profit_yoy")
        if yoy_profit is not None and cash_ratio is not None:
            yp = float(yoy_profit)
            if yp >= 15 and cash_ratio < 0.5:
                # 背离度：净利同比（小数）与现金流/净利 之差，越大越差
                gap = yp / 100.0 - cash_ratio
                div_score = max(10.0, min(55.0, 55.0 - gap * 35.0))
                indicators.append(
                    IndicatorResult(
                        name="增长质量背离度",
                        value=round(gap, 2),
                        score=round(div_score, 1),
                        level=score_to_level(div_score),
                        weight=2.5,
                        comment=(
                            f"净利同比+{yp:.1f}% 但经营现金流/净利={cash_ratio:.2f}，"
                            f"警惕赊销或存货堆积驱动的账面增长"
                        ),
                        period=latest_period,
                    )
                )
                warnings.append(
                    f"增长质量背离：净利高增(+{yp:.1f}%)与现金流含金量({cash_ratio:.2f})明显背离"
                )

        if "accounts_receivable" in fd.columns and "revenue" in fd.columns:
            ar = fd["accounts_receivable"].pct_change(fill_method=None).iloc[-1]
            rev = fd["revenue"].pct_change(fill_method=None).iloc[-1]
            if pd.notna(ar) and pd.notna(rev) and ar > rev * 2 and ar > 0.2:
                warnings.append("应收账款增速远超营收，可能存在虚增收入风险")

        if len(ocf.dropna()) >= 3 and (ocf.iloc[-3:] < 0).all():
            warnings.append("经营现金流连续3期为负，造血能力严重不足")

        module_score = self._weighted_score(indicators) if indicators else 0.0
        return ModuleResult(
            module_name="现金流质量",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
        )
