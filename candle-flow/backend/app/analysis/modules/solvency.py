from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level

# 轻资产/制造类行业：资产负债率阈值更严
_TIGHT_DEBT_INDUSTRY_KEYS = ("电子", "通信", "半导体", "元件", "模组", "光模块", "消费电子", "计算机", "软件")


class SolvencyAnalyzer(BaseAnalyzer):
    """资产负债率、有息负债、流动/速动比。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        industry = str(kwargs.get("industry") or "")
        tight = any(k in industry for k in _TIGHT_DEBT_INDUSTRY_KEYS)
        bs = kwargs.get("balance_sheet") or {}
        bs_period = format_report_period(bs.get("report_date") or kwargs.get("latest_report"))

        debt_ratio = kwargs.get("debt_ratio")
        if debt_ratio is not None:
            dr = float(debt_ratio)
            if tight:
                score, level = self._score_by_range(dr, (0, 35), (35, 50), (50, 60))
            else:
                score, level = self._score_by_range(dr, (0, 40), (40, 55), (55, 70))
            if dr > 75:
                score, level = 25.0, AnalysisLevel.DANGER
                warnings.append(f"资产负债率 {dr:.1f}% 偏高，偿债压力较大")
            elif tight and dr >= 55:
                warnings.append(f"资产负债率 {dr:.1f}% 对{industry or '本行业'}偏高，需区分经营性负债与有息负债")

            comment_parts = []
            if kwargs.get("debt_ratio_estimated"):
                comment_parts.append("估算值，仅供参考")
            comment_parts.append("含应付等经营性负债；危险程度看有息负债分项")
            if tight:
                comment_parts.append("轻资产行业采用更严阈值")
            indicators.append(
                IndicatorResult(
                    name="资产负债率(%)",
                    value=round(dr, 2),
                    score=score,
                    level=level,
                    weight=3.0,
                    comment="；".join(comment_parts),
                    period=bs_period,
                )
            )

        ibd_ratio = kwargs.get("interest_bearing_ratio")
        if ibd_ratio is None and bs.get("interest_bearing_ratio") is not None:
            ibd_ratio = bs.get("interest_bearing_ratio")
        if ibd_ratio is not None:
            ibr = float(ibd_ratio)
            # 有息负债率：优秀 <25，良好 25–40，中性 40–55
            ib_score, ib_level = self._score_by_range(ibr, (0, 25), (25, 40), (40, 55))
            if ibr > 55:
                ib_score, ib_level = 28.0, AnalysisLevel.POOR
                warnings.append(f"近似有息负债率 {ibr:.1f}% 偏高，短期偿债与利息压力需关注")
            indicators.append(
                IndicatorResult(
                    name="有息负债率(%)",
                    value=round(ibr, 2),
                    score=ib_score,
                    level=ib_level,
                    weight=2.5,
                    comment="≈(总负债−应付−预收)/总资产；剔除经营性负债后的杠杆压力",
                    period=bs_period,
                )
            )

        current_ratio = kwargs.get("current_ratio")
        if current_ratio is None and bs.get("current_ratio") is not None:
            current_ratio = bs.get("current_ratio")
        if current_ratio is None and not financial_data.empty and "current_ratio" in financial_data.columns:
            current_ratio = float(financial_data["current_ratio"].iloc[-1])
        if current_ratio is not None:
            cr = float(current_ratio)
            score2 = self._linear_score(cr, 0.8, 2.5)
            indicators.append(
                IndicatorResult(
                    name="流动比率",
                    value=round(cr, 2),
                    score=score2,
                    level=score_to_level(score2),
                    weight=2.0,
                    comment="由货币资金+应收+存货 / 流动负债粗估，简表口径",
                    period=bs_period,
                )
            )

        quick_ratio = kwargs.get("quick_ratio")
        if quick_ratio is None and bs.get("quick_ratio") is not None:
            quick_ratio = bs.get("quick_ratio")
        if quick_ratio is not None:
            qr = float(quick_ratio)
            score3 = self._linear_score(qr, 0.5, 1.8)
            indicators.append(
                IndicatorResult(
                    name="速动比率",
                    value=round(qr, 2),
                    score=score3,
                    level=score_to_level(score3),
                    weight=1.5,
                    comment="(货币资金+应收)/流动负债粗估，剔除存货",
                    period=bs_period,
                )
            )

        if not indicators:
            return ModuleResult("偿债能力", 50, AnalysisLevel.NEUTRAL, warnings=["暂无资产负债数据"])

        module_score = self._weighted_score(indicators)
        return ModuleResult(
            module_name="偿债能力",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={"tight_debt_industry": tight, "balance_sheet": bs or None},
        )
