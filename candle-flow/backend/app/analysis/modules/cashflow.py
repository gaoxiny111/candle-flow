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
        use_latest = False

        # 连续亏损 + 经营/自由现金流为正 → 「纸面富贵」（常靠拖欠应付挤出现金）
        profit_clean = fd["net_profit"].dropna()
        consec_loss = 0
        for v in reversed(list(profit_clean)):
            if float(v) < 0:
                consec_loss += 1
            else:
                break
        ocf_latest = float(ocf.dropna().iloc[-1]) if len(ocf.dropna()) else None
        fcf_latest = float(fcf.dropna().iloc[-1]) if len(fcf.dropna()) else None
        paper_wealth = consec_loss >= 2 and (
            (ocf_latest is not None and ocf_latest > 0) or (fcf_latest is not None and fcf_latest > 0)
        )
        # 单期亏损但当期 OCF/FCF 为正，也视为异常背离（稍轻）
        single_loss_ocf = (
            not paper_wealth
            and len(profit_clean)
            and float(profit_clean.iloc[-1]) < 0
            and ocf_latest is not None
            and ocf_latest > 0
        )

        cash_ratio_series = pd.Series(dtype=float)
        annual_cash_ratio: float | None = None
        if len(ocf.dropna()) and len(net_profit.dropna()):
            cash_ratio_series = (ocf / net_profit).replace([np.inf, -np.inf], np.nan).dropna()
            annual_cash_ratio = float(cash_ratio_series.iloc[-1]) if len(cash_ratio_series) else None

        latest_cr = kwargs.get("latest_cash_ratio")
        use_latest = bool(
            latest_cr is not None
            and latest_period
            and (not annual_period or latest_period != annual_period)
        )
        if use_latest or annual_cash_ratio is not None or latest_cr is not None:
            if use_latest:
                cash_ratio = float(latest_cr)  # type: ignore[arg-type]
                period_for_cr = latest_period
            elif latest_cr is not None and annual_cash_ratio is None:
                cash_ratio = float(latest_cr)
                period_for_cr = latest_period or annual_period
                use_latest = True
            else:
                cash_ratio = float(annual_cash_ratio or 0.0)
                period_for_cr = annual_period

            profit_for_sign = float(profit_clean.iloc[-1]) if len(profit_clean) else 0.0
            if use_latest and kwargs.get("eps") is not None and float(kwargs.get("eps") or 0) < 0:
                profit_for_sign = -1.0
            if profit_for_sign < 0:
                score, level = 35.0, AnalysisLevel.POOR
                cr_comment = "净利为负时该比率失真，不按利润含金量解读"
            else:
                score, level = self._score_by_range(cash_ratio, (1.0, 5.0), (0.7, 1.0), (0.4, 0.7))
                cr_comment = ">1 利润含金量高，<0.5 警惕利润注水"
                if use_latest and cash_ratio < 0.4:
                    score = min(score, 25.0)
                    level = AnalysisLevel.DANGER
                    cr_comment = (
                        f"当期({period_for_cr})经营现金流/净利仅 {cash_ratio:.1%}，"
                        f"显著弱于健康阈值；勿被上年年报高含金量掩盖"
                    )
                    warnings.append(
                        f"当期现金流恶化：经营现金流/净利润={cash_ratio:.1%}（{period_for_cr}），"
                        f"利润含金量偏低"
                    )
            indicators.append(
                IndicatorResult(
                    name="经营现金流/净利润",
                    value=round(cash_ratio, 2),
                    score=score,
                    level=level,
                    trend=self._calc_trend(cash_ratio_series) if len(cash_ratio_series) else "flat",
                    weight=3.5 if use_latest and cash_ratio < 0.4 else 3.0,
                    comment=cr_comment,
                    period=period_for_cr,
                )
            )
            if use_latest and annual_cash_ratio is not None:
                a_score, a_level = self._score_by_range(
                    annual_cash_ratio, (1.0, 5.0), (0.7, 1.0), (0.4, 0.7)
                )
                indicators.append(
                    IndicatorResult(
                        name="年报经营现金流/净利润",
                        value=round(annual_cash_ratio, 2),
                        score=a_score,
                        level=a_level,
                        weight=1.0,
                        comment="对照上年年报口径，当期以最新报告期为准",
                        period=annual_period,
                    )
                )

        if len(fcf.dropna()):
            fcf_val = float(fcf.iloc[-1])
            fcf_positive = int((fcf > 0).sum())
            # FCF/净利比率：衡量自由现金流对利润的覆盖质量
            fcf_np_ratio: float | None = None
            np_latest = float(net_profit.dropna().iloc[-1]) if len(net_profit.dropna()) else None
            if np_latest and abs(np_latest) > 1e-6:
                fcf_np_ratio = fcf_val / np_latest
            if paper_wealth and fcf_val > 0:
                fcf_score, fcf_level = 28.0, AnalysisLevel.POOR
                fcf_comment = (
                    f"连续{consec_loss}年亏损却自由现金流为正，警惕应付账款挤现/"
                    f"纸面富贵（近{len(fcf)}期中{fcf_positive}期为正）"
                )
            elif single_loss_ocf and fcf_val > 0:
                fcf_score, fcf_level = 40.0, AnalysisLevel.POOR
                fcf_comment = f"当期亏损但自由现金流为正，需排查营运负债变动（近{len(fcf)}期中{fcf_positive}期为正）"
            else:
                if fcf_val > 0:
                    # 基础 70 分，FCF/净利比率微调
                    if fcf_np_ratio is not None:
                        if fcf_np_ratio >= 0.8:
                            fcf_score = 85.0
                        elif fcf_np_ratio >= 0.6:
                            fcf_score = 80.0
                        elif fcf_np_ratio >= 0.4:
                            fcf_score = 78.0
                        elif fcf_np_ratio >= 0.2:
                            fcf_score = 72.0
                        else:
                            fcf_score = 68.0
                    else:
                        fcf_score = 70.0
                else:
                    fcf_score = 30.0
                fcf_level = AnalysisLevel.GOOD if fcf_val > 0 else AnalysisLevel.POOR
                fcf_comment = f"近{len(fcf)}期中有{fcf_positive}期为正"
                if fcf_np_ratio is not None and fcf_val > 0:
                    fcf_comment += f"，FCF/净利={fcf_np_ratio:.1%}"
            indicators.append(
                IndicatorResult(
                    name="自由现金流(元)",
                    value=round(fcf_val, 0),
                    score=fcf_score,
                    level=fcf_level,
                    trend=self._calc_trend(fcf),
                    weight=2.5,
                    comment=fcf_comment,
                    period=annual_period,
                )
            )

        # FCF 覆盖分红：红利资产核心风险点
        div_info = kwargs.get("dividend_info") or {}
        if div_info and div_info.get("fcf_dividend_gap") is not None:
            gap = float(div_info["fcf_dividend_gap"])
            fcf_amt = div_info.get("fcf")
            cash_div = div_info.get("cash_total")
            # 统一转亿显示
            fcf_yi = float(fcf_amt) / 1e8 if fcf_amt and float(fcf_amt) > 1e6 else float(fcf_amt or 0)
            cash_yi = float(cash_div) / 1e8 if cash_div and float(cash_div) > 1e6 else float(cash_div or 0)
            gap_yi = abs(gap) / 1e8 if abs(gap) > 1e6 else abs(gap)
            if gap < 0 and fcf_amt is not None and cash_div is not None:
                cover_score = 65.0
                cover_comment = (
                    f"自由现金流 {fcf_yi:.0f}亿低于现金分红 {cash_yi:.0f}亿，"
                    f"缺口约{gap_yi:.0f}亿，需消耗存量货币资金，"
                    f"高分红可持续性核心风险点"
                )
                warnings.append(cover_comment)
            elif gap >= 0 and fcf_amt is not None and cash_div is not None:
                cover_score = 85.0
                cover_comment = f"自由现金流 {fcf_yi:.0f}亿覆盖现金分红 {cash_yi:.0f}亿有余"
            else:
                cover_score = 70.0
                cover_comment = "分红数据不完整，覆盖率粗估"
            indicators.append(
                IndicatorResult(
                    name="FCF覆盖分红",
                    value=round(gap, 1),
                    score=cover_score,
                    level=score_to_level(cover_score),
                    weight=2.0,
                    comment=cover_comment,
                    period=annual_period,
                )
            )

        if len(ocf.dropna()) and ocf.iloc[-1] > 0:
            capex_ratio = float(capex.iloc[-1] / ocf.iloc[-1]) if ocf.iloc[-1] else 999.0
            if paper_wealth or single_loss_ocf:
                # 亏损下的低资本开支/高 OCF 不给「优秀」分
                cs, cl = 40.0, AnalysisLevel.POOR
                capex_comment = "亏损背景下经营现金多为营运负债驱动，资本开支比不代表造血健康"
            else:
                cs, cl = self._score_by_range(capex_ratio, (0, 0.4), (0.4, 0.7), (0.7, 1.2))
                capex_comment = ""
            indicators.append(
                IndicatorResult(
                    name="资本支出/经营现金流",
                    value=round(capex_ratio, 2),
                    score=cs,
                    level=cl,
                    weight=2.0,
                    comment=capex_comment,
                    period=annual_period,
                )
            )

        ocf_ps = kwargs.get("ocf_per_share")
        if ocf_ps is not None:
            ps = float(ocf_ps)
            if paper_wealth and ps > 0:
                ps_score, ps_level = 35.0, AnalysisLevel.POOR
            else:
                ps_score = self._linear_score(ps, 0, 2)
                ps_level = AnalysisLevel.GOOD if ps > 0.5 else AnalysisLevel.NEUTRAL
            indicators.append(
                IndicatorResult(
                    name="每股经营现金流(元)",
                    value=round(ps, 3),
                    score=ps_score,
                    level=ps_level,
                    weight=1.5,
                    period=latest_period,
                )
            )

        if paper_wealth:
            indicators.append(
                IndicatorResult(
                    name="亏损现金背离",
                    value=float(consec_loss),
                    score=20.0,
                    level=AnalysisLevel.DANGER,
                    weight=3.0,
                    comment="净利润连续为负但经营/自由现金流为正：异常背离，疑似拖欠供应商挤现",
                    period=annual_period,
                )
            )
            warnings.append(
                f"纸面富贵风险：连续{consec_loss}年亏损却出现正经营/自由现金流，"
                f"常见于应付账款等营运负债挤现，不宜按健康造血解读"
            )
        elif single_loss_ocf:
            warnings.append("当期净利为负但经营现金流为正，存在亏损现金背离，请结合应付账款变动核实")

        # 利润增速 vs 现金流质量背离
        yoy_profit = kwargs.get("profit_yoy")
        if yoy_profit is not None and cash_ratio is not None and len(profit_clean) and float(profit_clean.iloc[-1]) > 0:
            yp = float(yoy_profit)
            if yp >= 15 and cash_ratio < 0.5:
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
            if pd.notna(ar) and pd.notna(rev):
                from app.analysis.dividend_profile import classify_dividend_asset
                from app.analysis.receivable_quality import (
                    ar_turnover_warning,
                    is_quality_receivable_context,
                )

                ar_g, rev_g = float(ar), float(rev)
                last_ocf = float(ocf.iloc[-1]) if len(ocf) and pd.notna(ocf.iloc[-1]) else None
                last_np = float(fd["net_profit"].iloc[-1]) if pd.notna(fd["net_profit"].iloc[-1]) else None
                div_p = classify_dividend_asset(
                    dividend_yield_pct=kwargs.get("dividend_yield"),
                    pe_ttm=kwargs.get("pe_ttm"),
                )
                quality_ar = is_quality_receivable_context(
                    name=str(kwargs.get("name") or ""),
                    industry=str(kwargs.get("industry") or ""),
                    ocf=last_ocf,
                    net_profit=last_np,
                    is_dividend_asset=bool(div_p.get("is_dividend_asset")),
                    cash_ratio=kwargs.get("latest_cash_ratio") or cash_ratio,
                )
                # 营收高增且应收增速≥营收2倍 → 营运资本占用（非财务造假指控）
                if rev_g >= 0.20 and ar_g > rev_g * 2 and ar_g > 0.2:
                    msg, _ = ar_turnover_warning(quality_context=quality_ar, severe_wc_spike=True)
                    warnings.append(msg)
                elif rev_g < 0 and ar_g > rev_g:
                    msg, _ = ar_turnover_warning(quality_context=quality_ar)
                    warnings.append(msg)
                elif rev_g >= 0 and ar_g > rev_g + 0.10:
                    msg, _ = ar_turnover_warning(quality_context=quality_ar)
                    warnings.append(msg)

        if len(ocf.dropna()) >= 3 and (ocf.iloc[-3:] < 0).all():
            warnings.append("经营现金流连续3期为负，造血能力严重不足")

        module_score = self._weighted_score(indicators) if indicators else 0.0
        return ModuleResult(
            module_name="现金流质量",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={
                "paper_wealth": bool(paper_wealth),
                "consecutive_loss_years": int(consec_loss),
            },
        )
