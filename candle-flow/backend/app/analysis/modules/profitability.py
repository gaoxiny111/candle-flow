from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level
from app.analysis.config.company_profiles import (
    business_model_of,
    distribution_benchmarks,
    get_company_profile,
)
from app.analysis.dividend_profile import DIVIDEND_ASSET_WACC_PCT, classify_dividend_asset
from app.analysis.growth_quality import HIGH_GROWTH_WACC_PCT, classify_high_growth_quality
from app.analysis.growth_profile import classify_growth_stock


def _score_vs_benchmark(value: float, benchmark: float) -> tuple[float, AnalysisLevel]:
    """同业相对评分：以行业中位数为锚。

    用于分销/贸易这类「低净利率、高周转」商业模式。绝对阈值（净利率 15%、
    ROIC 12%）是按制造业设定的，套到分销商身上会把整个行业判成不合格
    —— 实测 7 家 A 股电子元器件分销商 2026 中报，ROIC 三年均值全部落在
    1.5%~3.8%，无一达到 WACC≈10%。

    改用相对位置后仍保留区分度：行业最差者依旧低分。
    锚点换算：达到行业中位 ≈ 55 分（中性）；2 倍中位 → 85 分；
    0.3 倍中位以下 → 25 分。
    """
    if value is None or benchmark is None or abs(float(benchmark)) < 1e-9:
        return 50.0, score_to_level(50.0)
    ratio = float(value) / float(benchmark)
    if ratio >= 2.0:
        score = 85.0
    elif ratio >= 1.4:
        score = 75.0
    elif ratio >= 1.1:
        score = 65.0
    elif ratio >= 0.9:
        score = 55.0
    elif ratio >= 0.6:
        score = 45.0
    elif ratio >= 0.3:
        score = 35.0
    else:
        score = 25.0
    return score, score_to_level(score)


def _estimate_wacc_pct(
    debt_ratio: float | None,
    *,
    is_dividend_asset: bool = False,
    is_high_growth_quality: bool = False,
    interest_bearing_ratio: float | None = None,
    business_model: str = "",
) -> float:
    """与引擎 DCF 动态 WACC 对齐（百分比）；红利/高成长赛道用更低资本成本。

    资本结构口径优先用「有息负债率」：应付账款/预收等经营性负债不构成资本成本，
    用含应付的资产负债率会把分销/贸易类企业（应付占比天然很高）误判成
    高杠杆并抬到 12%，从而误杀 ROIC。
    """
    if is_dividend_asset:
        return DIVIDEND_ASSET_WACC_PCT
    if is_high_growth_quality:
        return HIGH_GROWTH_WACC_PCT
    dr = None
    if interest_bearing_ratio is not None:
        dr = float(interest_bearing_ratio)
    elif debt_ratio is not None:
        dr = float(debt_ratio)
    if dr is None:
        base = 10.0
    elif dr < 20:
        base = 7.0
    elif dr <= 30:
        base = 8.0
    elif dr > 60:
        base = 12.0
    else:
        base = 10.0
    if business_model == "distribution":
        # 轻资产高周转：资本成本主要来自权益端，且无风险利率已降至 2% 以下，
        # 不宜按含应付账款的高资产负债率给到 12%
        return min(base, 10.0)
    return base


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

        # 公司画像：周期/资源股识别（替代硬编码公司名）
        _profile = get_company_profile(
            kwargs.get("name") or "", kwargs.get("symbol") or ""
        )

        fd = financial_data.copy()
        annual_dates = kwargs.get("annual_dates") or list(fd.index)
        annual_period = ""
        if annual_dates:
            last = format_report_period(str(annual_dates[-1]))
            annual_period = last or str(annual_dates[-1])

        # ── 周期/资源股识别：调整评分权重 ─────────────────────────
        _cycle_prof_kw = ("化工", "有色", "煤炭", "钢铁", "化肥", "磷", "矿", "农化", "航运", "开采")
        _industry_str = str(kwargs.get("industry") or "")
        _name_str = str(kwargs.get("name") or "")
        # 商业模式（毛利率阈值、WACC 分层、ROIC 判定都要用），
        # 必须在毛利率打分之前算好，不能等到 ROIC 段才计算。
        biz_model = business_model_of(_name_str, str(kwargs.get("symbol") or ""), _industry_str)
        is_dist_biz = biz_model == "distribution"
        # 分销模式改用同业中位数作评分锚点（见 company_profiles.DISTRIBUTION_BENCHMARKS）
        _dist_bench: dict = distribution_benchmarks() if is_dist_biz else {}
        _is_cyclical_prof = any(k in _industry_str for k in _cycle_prof_kw) or _profile.get("cyclical", False)
        # 权重：周期股 ROIC/ROE 主导，毛利率权重下调
        _w_roe = 3.0 if not _is_cyclical_prof else 3.0
        _w_roic = 2.5 if not _is_cyclical_prof else 4.0
        _w_nm = 2.0 if not _is_cyclical_prof else 2.0
        _w_gm = 2.0 if not _is_cyclical_prof else 1.0

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
        # 分销/贸易：同业 ROE 中位仅 5.35%，套用制造业 12%/8%/5% 阈值会集体低分
        if is_dist_biz and _dist_bench.get("roe_pct"):
            roe_score, roe_level = _score_vs_benchmark(roe, float(_dist_bench["roe_pct"]))
        else:
            roe_score, roe_level = self._score_by_range(roe, (12, 100), (8, 12), (5, 8))
        roe_comment_suffix = (
            f"；分销/贸易口径：ROE 同业中位 {float(_dist_bench['roe_pct']):.2f}%，"
            f"按同业相对位置而非制造业绝对阈值评分"
            if is_dist_biz and _dist_bench.get("roe_pct")
            else ""
        )
        # 成长股/周期底部：ROE 标注异常低谷，降权 30%
        _roe_is_growth = classify_growth_stock(
            symbol=kwargs.get("symbol"),
            gross_margin_pct=kwargs.get("latest_gross_margin"),
            revenue_yoy=kwargs.get("revenue_yoy"),
            profit_yoy=kwargs.get("profit_yoy"),
            pe_ttm=kwargs.get("pe_ttm"),
        ).get("is_growth_stock")
        if _roe_is_growth and roe < 5:
            roe_score = round(roe_score * 0.7, 1)
            roe_level = score_to_level(roe_score)
            roe_comment_suffix = "；周期底部异常值，已降权评估"
        indicators.append(
            IndicatorResult(
                name="ROE(%)",
                value=round(roe, 2),
                score=roe_score,
                level=roe_level,
                trend=self._calc_trend(roe_series),
                weight=_w_roe,
                comment=f"杜邦: 净利率{net_margin:.1%} × 周转{asset_turnover:.2f} × 权益乘数{equity_multiplier:.2f}{roe_comment_suffix}",
                period=annual_period,
            )
        )

        # 毛利率：优先真实成本；亦可直接读已算好的 gross_margin 列
        gross_margin_val: float | None = None
        # 分销/贸易赚的是周转与渠道价差，毛利率天然落在个位数~低双位数。
        # 沿用制造业 (50/30/15) 阈值会把正常经营判成「竞争优势不足」并压垮盈利能力分
        # （深圳华强毛利率 7.7% 属分销正常区间，却按制造业口径只得低分）。
        if is_dist_biz:
            _gm_bands = ((12, 100), (8, 12), (4, 8))
            _gm_comment = "分销/贸易口径：薄利多销、以周转取胜，毛利率天然偏低，不按制造业标准扣分"
        else:
            _gm_bands = ((50, 100), (30, 50), (15, 30))
            _gm_comment = ""
        if "cogs" in fd.columns and fd["cogs"].notna().any():
            gm_series = ((fd["revenue"] - fd["cogs"]) / revenue * 100).dropna()
            if len(gm_series):
                gross_margin_val = float(gm_series.iloc[-1])
                gm_score, gm_level = self._score_by_range(gross_margin_val, *_gm_bands)
                indicators.append(
                    IndicatorResult(
                        name="毛利率(%)",
                        value=round(gross_margin_val, 2),
                        score=gm_score,
                        level=gm_level,
                        trend=self._calc_trend(gm_series),
                        weight=_w_gm,
                        period=annual_period,
                        comment=_gm_comment,
                    )
                )
        elif "gross_margin" in fd.columns and fd["gross_margin"].notna().any():
            gm_series = fd["gross_margin"].dropna()
            gross_margin_val = float(gm_series.iloc[-1])
            gm_score, gm_level = self._score_by_range(gross_margin_val, *_gm_bands)
            indicators.append(
                IndicatorResult(
                    name="毛利率(%)",
                    value=round(gross_margin_val, 2),
                    score=gm_score,
                    level=gm_level,
                    trend=self._calc_trend(gm_series),
                    weight=_w_gm,
                    period=annual_period,
                    comment=_gm_comment,
                )
            )
        # ── 成本管控能力修正：周期股原燃料涨价但毛利率逆势提升 ──
        if _is_cyclical_prof and gross_margin_val is not None:
            # 检测毛利率趋势
            _gm_trend_col = "gross_margin" if "gross_margin" in fd.columns else None
            if _gm_trend_col is None and "cogs" in fd.columns:
                _gm_trend_col = "_calc_gm"
                _rev_s = fd["revenue"].replace(0, pd.NA)
                fd["_calc_gm"] = ((fd["revenue"] - fd["cogs"]) / _rev_s * 100)
            if _gm_trend_col and fd[_gm_trend_col].dropna().notna().sum() >= 2:
                _gm_trend = self._calc_trend(fd[_gm_trend_col].dropna())
                if _gm_trend == "up" and gross_margin_val >= 15:
                    # 周期股毛利率逆势向上 → 成本管控能力强
                    indicators.append(
                        IndicatorResult(
                            name="成本管控能力",
                            value=round(gross_margin_val, 2),
                            score=82.0,
                            level=AnalysisLevel.EXCELLENT,
                            trend="up",
                            weight=1.5,
                            period=annual_period,
                            comment=(
                                f"周期股毛利率{gross_margin_val:.1f}%逆势向上，"
                                f"体现原材料成本管控/战略采购能力"
                            ),
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

        # 成长股/周期底部反转识别（用于 ROIC/ROE 降权）
        gs = classify_growth_stock(
            symbol=kwargs.get("symbol"),
            gross_margin_pct=float(profile_gm) if profile_gm is not None else None,
            revenue_yoy=kwargs.get("revenue_yoy"),
            profit_yoy=kwargs.get("profit_yoy"),
            profit_cagr_3y=None,  # 由 engine 传 metadata，模块内不算
            pe_ttm=kwargs.get("pe_ttm"),
            is_high_growth_quality=is_hgq,
            is_dividend_asset=is_div,
        )
        is_growth_stock = bool(gs.get("is_growth_stock"))
        debt_ratio = kwargs.get("debt_ratio")
        wacc_pct = _estimate_wacc_pct(
            float(debt_ratio) if debt_ratio is not None else None,
            is_dividend_asset=is_div,
            is_high_growth_quality=is_hgq,
            interest_bearing_ratio=kwargs.get("interest_bearing_ratio"),
            business_model=biz_model,
        )
        roic_below_wacc = False
        roic_value: float | None = None
        roic_normalized: float | None = None

        if "operating_profit" in fd.columns and (
            "total_assets" in fd.columns or "equity" in fd.columns
        ):
            invested = _invested_capital_series(fd)
            nopat = fd["operating_profit"] * 0.75
            roic_series = (nopat / invested * 100).dropna()
            roic = float(roic_series.iloc[-1]) if len(roic_series) else 0.0
            roic_value = round(roic, 2)
            # ── 归一化 ROIC：单期 ROIC 受周期位置/扩张节奏扰动（周期底部刚恢复、
            # 或分销类当年大量备货），用最近 3 年均值做判定锚更稳健 ──
            normalized_used = False
            roic_judge = roic
            if len(roic_series) >= 2 and (
                _is_cyclical_prof or is_dist_biz or _roe_is_growth
            ):
                roic_judge = float(roic_series.tail(3).mean())
                roic_normalized = round(roic_judge, 2)
                normalized_used = abs(roic_judge - roic) > 0.5
            op_est = False
            if "operating_profit_estimated" in fd.columns:
                try:
                    op_est = float(fd["operating_profit_estimated"].iloc[-1] or 0) >= 1
                except Exception:
                    op_est = False
            # 红利/公用事业：ROIC 绝对门槛略降；高成长赛道按扩张期标准
            # 归一化生效时用 3 年均值打分，避免单期扰动
            roic_for_score = roic_judge if roic_normalized is not None else roic
            if is_div:
                roic_score, roic_level = self._score_by_range(roic_for_score, (8, 100), (5, 8), (3, 5))
            elif is_hgq:
                roic_score, roic_level = self._score_by_range(roic_for_score, (10, 100), (7, 10), (4, 7))
            elif is_dist_biz:
                # 分销/贸易：绝对门槛（制造业 12%/8%/4%）对全行业都失效
                # —— 实测同业 ROIC 三年均值仅 1.5%~3.8%，无一达到 WACC≈10%。
                # 改用同业中位数为锚：达到中位即中性，行业最差者仍判低分。
                _bench_roic = float(_dist_bench.get("roic_3y_pct") or 0) or None
                if _bench_roic:
                    roic_score, roic_level = _score_vs_benchmark(roic_for_score, _bench_roic)
                else:
                    roic_score, roic_level = self._score_by_range(roic_for_score, (9, 100), (6, 9), (3, 6))
            else:
                roic_score, roic_level = self._score_by_range(roic_for_score, (12, 100), (8, 12), (4, 8))

            wacc_label = (
                "红利资产WACC"
                if is_div
                else ("高成长赛道WACC" if is_hgq else "WACC")
            )
            _roic_anchor_note = (
                f"（3年均值 {roic_judge:.1f}%）" if roic_normalized is not None else ""
            )
            # 分销模式的「价值创造」判定改用同业相对位置：
            # 原判据是固定 ROE>=8%，对分销业锚点过高（同业 ROE 中位仅 5.35%），
            # 会把行业中上水平也判成毁灭价值。改为 ROIC 与 ROE 都不低于同业中位。
            _bench_roe_v = float(_dist_bench.get("roe_pct") or 0) or None
            _bench_roic_v = float(_dist_bench.get("roic_3y_pct") or 0) or None
            _dist_healthy = bool(
                is_dist_biz
                and _bench_roe_v
                and _bench_roic_v
                and roe >= _bench_roe_v
                and roic_judge >= _bench_roic_v
            )
            if roic_judge < wacc_pct:
                if is_div or is_hgq or is_growth_stock or _dist_healthy:
                    roic_below_wacc = False
                    if _dist_healthy:
                        # 分销/贸易：全行业 ROIC 都低于 WACC（同业三年均值中位仅 3.3%），
                        # 只要 ROIC/ROE 不低于同业中位，就不构成价值毁灭
                        roic_comment = (
                            f"ROIC {roic:.1f}%{_roic_anchor_note} 低于 WACC≈{wacc_pct:.1f}%："
                            f"分销/贸易为低净利率、高周转商业模式，ROIC 天然偏低"
                            f"（同业三年均值中位仅 {_bench_roic_v:.1f}%）；"
                            f"本公司 ROIC 与 ROE({roe:.1f}%，同业中位 {_bench_roe_v:.1f}%) "
                            f"均不低于同业中位，不构成价值毁灭"
                        )
                        warnings.append(
                            f"分销模式资本回报：ROIC({roic:.1f}%)低于WACC(≈{wacc_pct:.1f}%)"
                            f"属行业共性（同业中位 {_bench_roic_v:.1f}%），"
                            f"本公司 ROE {roe:.1f}% 不低于同业中位；关注营运资本效率与毛利率稳定性"
                        )
                    elif is_div:
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
                    elif is_growth_stock and not is_hgq:
                        # 周期底部反转：ROIC<WACC 是底部特征，不作否决
                        roic_comment = (
                            f"ROIC {roic:.1f}% < WACC≈{wacc_pct:.0f}%："
                            f"周期底部/成长股特征，当前 ROE/ROIC 为异常低谷值，"
                            f"参考营收增速与拐点确认评估真实盈利能力"
                        )
                        warnings.append(
                            f"周期底部反转：ROIC({roic:.1f}%)低于WACC(≈{wacc_pct:.0f}%)，"
                            f"为低谷特征不作价值否决；关注拐点持续性"
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
                    # 需「最新期与归一化值同时低于 WACC」才判定价值毁灭，
                    # 避免单期周期低谷/扩张期被一票否决
                    roic_below_wacc = roic < wacc_pct
                    roic_comment = (
                        f"ROIC {roic:.1f}%{_roic_anchor_note} < WACC≈{wacc_pct:.0f}%："
                        f"投入资本回报低于资本成本，可能在毁灭股东价值"
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
                    roic_comment = (
                        f"ROIC {roic:.1f}%{_roic_anchor_note} ≥ WACC≈{wacc_pct:.0f}%："
                        f"创造经济利润"
                    )
            if roic_normalized is not None:
                roic_comment += (
                    f"；判定口径：3年均值 ROIC {roic_judge:.1f}%"
                    f"（单期 {roic:.1f}% 易受周期位置/扩张节奏扰动）"
                )
            if op_est:
                roic_comment += "（营业利润为估算，待利润表校准）"
            indicators.append(
                IndicatorResult(
                    name="ROIC(%)",
                    value=round(roic, 2),
                    score=roic_score,
                    level=roic_level,
                    trend=self._calc_trend(roic_series),
                    weight=_w_roic,
                    comment=roic_comment,
                    period=annual_period,
                )
            )

        nm_pct = net_margin * 100
        # 周期股净利率门槛略降：10%+ 已属中上
        # 分销/贸易：同业净利率中位仅 1.91%，制造业 15%/8%/3% 阈值必然全行业不及格
        if is_dist_biz and _dist_bench.get("net_margin_pct"):
            nm_score, nm_level = _score_vs_benchmark(nm_pct, float(_dist_bench["net_margin_pct"]))
            nm_comment = (
                f"分销/贸易口径：同业净利率中位 {float(_dist_bench['net_margin_pct']):.2f}%"
                f"（薄利多销、以周转取胜），按同业相对位置评分"
            )
        elif _is_cyclical_prof:
            nm_score, nm_level = self._score_by_range(nm_pct, (12, 100), (7, 12), (3, 7))
            nm_comment = None
        else:
            nm_score, nm_level = self._score_by_range(nm_pct, (15, 100), (8, 15), (3, 8))
            nm_comment = None
        indicators.append(
            IndicatorResult(
                name="净利率(%)",
                value=round(nm_pct, 2),
                score=nm_score,
                level=nm_level,
                trend=self._calc_trend((fd["net_profit"] / revenue * 100).dropna()),
                weight=_w_nm,
                comment=nm_comment,
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
                    score=90.0 if dy >= 5 else (88.0 if dy >= 4 else 78.0),
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
                "roic_3y_avg": roic_normalized,
                "roic_below_wacc": roic_below_wacc,
                "business_model": biz_model or None,
                "is_dividend_asset": is_div,
                "dividend_profile": div_profile,
                "is_high_growth_quality": is_hgq,
                "growth_quality": growth_profile,
                "gross_margin": round(gross_margin_val, 2) if gross_margin_val is not None else None,
            },
        )
