from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, score_to_level
from app.analysis.config import CAPITAL_HEAVY_INDUSTRY_KEYWORDS

# ── 利润侵蚀预警阈值（同期口径，见 financials._ops_efficiency_from_sina）──
# 存货增速持续高于营收增速 → 积压加重，后续存货跌价与毛利侵蚀风险上升。
# 用「存货/营收比值同比变化率」而非存货绝对增速：后者在营收同步扩张时
# 会给出误报（备货型增长）。比值口径剔除了营收规模效应。
INV_REV_RATIO_YOY_WARN = 30.0  # 比值同比 +30% → 预警
INV_REV_RATIO_YOY_SEVERE = 60.0  # 比值同比 +60% → 严重
# 应收周转天数同比拉长 → 回款效率下降（账期放宽/下游压力）
AR_DAYS_YOY_WARN = 15.0
AR_DAYS_YOY_SEVERE = 30.0


class EfficiencyAnalyzer(BaseAnalyzer):
    """总资产周转率 + 应收周转天数变化率 + 存货/营收比值（利润侵蚀预警）。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        if financial_data.empty or "total_assets" not in financial_data:
            return ModuleResult("营运效率", 50, AnalysisLevel.NEUTRAL)

        industry = str(kwargs.get("industry") or "")
        heavy = any(k in industry for k in CAPITAL_HEAVY_INDUSTRY_KEYWORDS)

        fd = financial_data
        ta = fd["total_assets"].replace(0, pd.NA)
        rev = fd["revenue"]
        turnover = (rev / ta).dropna()
        if len(turnover):
            tv = float(turnover.iloc[-1])
            # 重资产：0.15~0.45 为中性偏好；轻资产仍用 0.3~1.2
            if heavy:
                score = self._linear_score(tv, 0.10, 0.50)
                comment = "重资产行业周转率天然偏低，已按行业标准评分"
            else:
                score = self._linear_score(tv, 0.3, 1.2)
                comment = ""
            # 地板抬高到 40，避免单项拖垮观感（本模块不参与综合加权）
            score = max(40.0, score) if heavy else score
            indicators.append(
                IndicatorResult(
                    name="总资产周转率",
                    value=round(tv, 3),
                    score=score,
                    level=score_to_level(score),
                    trend=self._calc_trend(turnover),
                    weight=2.5,
                    comment=comment,
                )
            )

        # ── 应收周转天数变化率 ────────────────────────────────────────────
        # 口径：financials._ops_efficiency_from_sina 用「同期对同期」（中报对去年
        # 中报）计算，避免跨期错配。这里只消费，不重算（同一判定不两处各算）。
        ops = kwargs.get("ops_efficiency") or {}
        ar_yoy = ops.get("ar_days_yoy_pct")
        if ar_yoy is not None:
            v = float(ar_yoy)
            days_latest = ops.get("ar_days_latest")
            days_txt = f"当期{days_latest}天，" if days_latest is not None else ""
            if v >= AR_DAYS_YOY_SEVERE:
                s, lv = 32.0, AnalysisLevel.POOR
                txt = f"{days_txt}同比拉长{v:.1f}%，回款效率显著恶化，账龄结构需重点核查"
            elif v >= AR_DAYS_YOY_WARN:
                s, lv = 50.0, AnalysisLevel.NEUTRAL
                txt = f"{days_txt}同比拉长{v:.1f}%，回款节奏放缓，关注下游结算周期变化"
            elif v <= -AR_DAYS_YOY_WARN:
                s, lv = 82.0, AnalysisLevel.GOOD
                txt = f"{days_txt}同比缩短{abs(v):.1f}%，回款效率改善"
            else:
                s, lv = 70.0, AnalysisLevel.NEUTRAL
                txt = f"{days_txt}同比{'拉长' if v > 0 else '缩短'}{abs(v):.1f}%，基本稳定"
            indicators.append(
                IndicatorResult(
                    name="应收周转天数变化率",
                    value=round(v, 1),
                    score=s,
                    level=lv,
                    trend="down" if v > 0 else "up",
                    weight=2.0,
                    comment=f"{txt}（同期口径 {ops.get('prev_period') or '—'} → {ops.get('period') or '—'}）",
                    period=str(ops.get("period") or ""),
                )
            )

        # ── 存货/营收比值 + 利润侵蚀预警 ──────────────────────────────────
        inv_ratio = ops.get("inv_to_rev_latest")
        inv_ratio_yoy = ops.get("inv_to_rev_yoy_pct")
        inv_yoy = ops.get("inv_yoy_pct")
        rev_yoy = ops.get("rev_yoy_pct")
        if inv_ratio is not None and inv_ratio_yoy is not None:
            v = float(inv_ratio_yoy)
            # 判据：存货/营收比值同期同比上升超过阈值 → 积压加重
            # 这是「存货增速持续高于营收增速」的可比口径实现（比值上升即
            # 存货增速 > 营收增速）。严重档额外要求营收未高增——营收本身
            # 大幅扩张时的比值上升多为主动备货，不宜按积压定性。
            rev_expanding = rev_yoy is not None and float(rev_yoy) >= 20
            if v >= INV_REV_RATIO_YOY_SEVERE and not rev_expanding:
                s, lv = 30.0, AnalysisLevel.POOR
                verdict = "存货积压加重，后续存货跌价与毛利侵蚀风险高"
            elif v >= INV_REV_RATIO_YOY_WARN:
                s, lv = 50.0, AnalysisLevel.NEUTRAL
                verdict = "存货占营收比重上升，需跟踪跌价准备计提与周转效率"
            else:
                s, lv = 75.0, AnalysisLevel.NEUTRAL
                verdict = "存货占营收比重稳定"
            parts = [f"存货/营收={float(inv_ratio):.3f}", f"比值同比{v:+.1f}%"]
            if inv_yoy is not None:
                parts.append(f"存货同比{float(inv_yoy):+.1f}%")
            if rev_yoy is not None:
                parts.append(f"营收同比{float(rev_yoy):+.1f}%")
            if rev_expanding and v >= INV_REV_RATIO_YOY_SEVERE:
                parts.append("营收高增，比值上升或为主动备货，暂不按积压定性")
            parts.append(verdict)
            indicators.append(
                IndicatorResult(
                    name="存货/营收比值",
                    value=round(float(inv_ratio), 3),
                    score=s,
                    level=lv,
                    trend="down" if v > 0 else "up",
                    weight=2.0,
                    comment="；".join(parts),
                    period=str(ops.get("period") or ""),
                )
            )
            # 利润侵蚀预警：只在「严重档且营收未高增」时给出，避免备货型误报
            if v >= INV_REV_RATIO_YOY_SEVERE and not rev_expanding:
                _inv_txt = (
                    f"，存货同比{float(inv_yoy):+.1f}%" if inv_yoy is not None else ""
                )
                _rev_txt = (
                    f"、营收同比{float(rev_yoy):+.1f}%" if rev_yoy is not None else ""
                )
                warnings.append(
                    f"利润侵蚀预警：存货/营收比值同比上升{v:.1f}%"
                    f"（存货/营收={float(inv_ratio):.3f}{_inv_txt}{_rev_txt}）；"
                    f"存货增速持续高于营收增速，需关注后续存货跌价准备计提对利润的侵蚀"
                )
        if ar_yoy is not None and float(ar_yoy) >= AR_DAYS_YOY_SEVERE:
            warnings.append(
                f"应收回款预警：应收周转天数同比拉长{float(ar_yoy):.1f}%，"
                f"账期放宽可能掩盖下游需求压力与坏账风险"
            )

        module_score = self._weighted_score(indicators) if indicators else 50.0
        return ModuleResult(
            module_name="营运效率",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={
                "capital_heavy": heavy,
                "ops_efficiency": ops or None,
                "inv_erosion_warning": bool(
                    inv_ratio_yoy is not None
                    and float(inv_ratio_yoy) >= INV_REV_RATIO_YOY_SEVERE
                    and not (rev_yoy is not None and float(rev_yoy) >= 20)
                ),
            },
        )
