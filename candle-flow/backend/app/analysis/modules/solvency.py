from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level

# 轻资产/制造类行业：资产负债率阈值更严
_TIGHT_DEBT_INDUSTRY_KEYS = ("电子", "通信", "半导体", "元件", "模组", "光模块", "消费电子", "计算机", "软件")


class SolvencyAnalyzer(BaseAnalyzer):
    """资产负债率、有息负债、流动/速动比；现金奶牛按产业链话语权修正。"""

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
                    comment="≈有息负债/总资产；剔除经营性负债后的杠杆压力",
                    period=bs_period,
                )
            )

        op_liab = float(bs.get("operating_liabilities") or 0)
        short_b = float(
            bs.get("short_term_borrowings")
            if bs.get("short_term_borrowings") is not None
            else (kwargs.get("short_term_borrowings") or 0)
        )
        ta = float(bs.get("total_assets") or 0)
        # 产业链话语权：经营性负债主导 + 有息负债可控 + 杠杆不高
        supply_chain_power = (
            op_liab > 1e9
            and op_liab >= max(short_b, 1.0) * 1.5
            and (ibd_ratio is None or float(ibd_ratio) < 20)
            and (debt_ratio is None or float(debt_ratio) < 45)
        )

        current_ratio = kwargs.get("current_ratio")
        if current_ratio is None and bs.get("current_ratio") is not None:
            current_ratio = bs.get("current_ratio")
        if current_ratio is None and not financial_data.empty and "current_ratio" in financial_data.columns:
            current_ratio = float(financial_data["current_ratio"].iloc[-1])

        # 现金奶牛：分母仅用短期借款（剔除应付/预收等经营性负债）重算流动/速动
        ca_proxy = None
        if ta > 0 and current_ratio is not None and (op_liab + short_b) > 0:
            # 反推流动资产粗值：CR × (op+short)
            ca_proxy = float(current_ratio) * (op_liab + short_b)
        adj_cr = None
        adj_qr = None
        if supply_chain_power and ca_proxy is not None and short_b > 1e6:
            adj_cr = ca_proxy / short_b
            # 速动粗估：按原速动/流动比例缩放，缺省按 0.9
            qr0 = kwargs.get("quick_ratio")
            if qr0 is None:
                qr0 = bs.get("quick_ratio")
            if qr0 is not None and current_ratio and float(current_ratio) > 0:
                adj_qr = adj_cr * (float(qr0) / float(current_ratio))
            else:
                adj_qr = adj_cr * 0.9

        if current_ratio is not None:
            cr = float(current_ratio)
            score_cr = self._linear_score(cr, 0.8, 2.5)
            cr_comment = "由货币资金+应收+存货 / 流动负债粗估，简表口径"
            cr_weight = 2.0
            if supply_chain_power:
                # 传统流动比率对经营性负债型龙头易失真：降权 + 用剔除经营负债后的口径评分
                cr_weight = 1.0
                if adj_cr is not None:
                    score_cr = max(score_cr, self._linear_score(min(adj_cr, 5.0), 0.8, 2.5))
                    cr_comment = (
                        f"账面流动比率 {cr:.2f}（含经营性负债）；"
                        f"剔除应付/预收后约 {adj_cr:.2f}，反映产业链资金占用优势"
                    )
                else:
                    score_cr = max(score_cr, 72.0)
                    cr_comment = (
                        f"账面流动比率 {cr:.2f}；现金奶牛/产业链强势下经营性负债≠偿债风险，已降权"
                    )
            indicators.append(
                IndicatorResult(
                    name="流动比率",
                    value=round(cr, 2),
                    score=score_cr,
                    level=score_to_level(score_cr),
                    weight=cr_weight,
                    comment=cr_comment,
                    period=bs_period,
                )
            )

        quick_ratio = kwargs.get("quick_ratio")
        if quick_ratio is None and bs.get("quick_ratio") is not None:
            quick_ratio = bs.get("quick_ratio")
        if quick_ratio is not None:
            qr = float(quick_ratio)
            score_qr = self._linear_score(qr, 0.5, 1.8)
            qr_comment = "(货币资金+应收)/流动负债粗估，剔除存货"
            qr_weight = 1.5
            if supply_chain_power:
                qr_weight = 0.8
                if adj_qr is not None:
                    score_qr = max(score_qr, self._linear_score(min(adj_qr, 4.0), 0.5, 1.8))
                    qr_comment = (
                        f"账面速动比率 {qr:.2f}；剔除经营性负债后约 {adj_qr:.2f}"
                    )
                else:
                    score_qr = max(score_qr, 70.0)
                    qr_comment = f"账面速动比率 {qr:.2f}；产业链强势龙头已降权传统速动口径"
            indicators.append(
                IndicatorResult(
                    name="速动比率",
                    value=round(qr, 2),
                    score=score_qr,
                    level=score_to_level(score_qr),
                    weight=qr_weight,
                    comment=qr_comment,
                    period=bs_period,
                )
            )

        if supply_chain_power:
            # 有息负债已低时再抬高有息分项权重感：单独加分项
            if ibd_ratio is not None:
                for ind in indicators:
                    if ind.name == "有息负债率(%)":
                        ind.weight = 3.2
            power_score = 90.0 if (ibd_ratio is not None and float(ibd_ratio) < 12) else 84.0
            indicators.append(
                IndicatorResult(
                    name="产业链话语权",
                    value=round(op_liab / 1e8, 2) if op_liab else 0.0,
                    score=power_score,
                    level=AnalysisLevel.EXCELLENT if power_score >= 85 else AnalysisLevel.GOOD,
                    weight=2.5,
                    comment=(
                        "经营性负债为主、有息负债可控：上下游资金占用优势，"
                        "偿债评分侧重有息负债与现金覆盖而非账面流动比率"
                    ),
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
            metadata={
                "tight_debt_industry": tight,
                "balance_sheet": bs or None,
                "supply_chain_power": supply_chain_power,
                "adjusted_current_ratio": round(adj_cr, 2) if adj_cr is not None else None,
                "adjusted_quick_ratio": round(adj_qr, 2) if adj_qr is not None else None,
            },
        )
