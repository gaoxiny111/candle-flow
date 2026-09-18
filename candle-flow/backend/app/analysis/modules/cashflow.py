from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level
from app.analysis.config.company_profiles import distribution_benchmarks, is_distribution


def _fmt_yi(value: float) -> str:
    """
    金额（已是「亿」单位）的自适应精度显示。

    小额金额不能用 %.0f：0.45 亿会被舍成「0亿」，让「现金分红 0亿」这种
    与事实不符的文案出现在报告里。10 亿以下保留 1 位小数，否则取整。
    """
    v = float(value or 0)
    return f"{v:.1f}" if abs(v) < 10 else f"{v:.0f}"


class CashflowAnalyzer(BaseAnalyzer):
    """经营现金流含金量、自由现金流、资本开支压力、增长质量背离。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        if financial_data.empty:
            return ModuleResult("现金流质量", 0, AnalysisLevel.DANGER, warnings=["暂无现金流数据"])

        # 分销/贸易商业模式：低净利率、高周转，上游预付 + 下游长账期，
        # 扩张期经营现金流为负属行业常态，不能直接套用制造业阈值。
        is_dist = is_distribution(
            str(kwargs.get("name") or ""),
            str(kwargs.get("symbol") or ""),
            str(kwargs.get("industry") or ""),
        ) or str(kwargs.get("cashflow_profile") or "") == "distribution"
        rev_yoy_v = kwargs.get("revenue_yoy")
        dist_expanding = (
            is_dist and rev_yoy_v is not None and float(rev_yoy_v) >= 15
        )

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
        # 5年趋势值：均值与标准差
        cash_ratio_5y_avg: float | None = None
        cash_ratio_5y_std: float | None = None
        if len(ocf.dropna()) and len(net_profit.dropna()):
            cash_ratio_series = (ocf / net_profit).replace([np.inf, -np.inf], np.nan).dropna()
            annual_cash_ratio = float(cash_ratio_series.iloc[-1]) if len(cash_ratio_series) else None
            # 取最近5期（含年报+可能的季报）计算均值与标准差
            if len(cash_ratio_series) >= 2:
                _recent = cash_ratio_series.tail(5)
                cash_ratio_5y_avg = float(_recent.mean())
                cash_ratio_5y_std = float(_recent.std(ddof=0)) if len(_recent) >= 2 else 0.0

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
            score_ratio = cash_ratio  # 默认用当期值，后续按趋势逻辑覆盖
            cr_label = "经营现金流/净利润(当期)"
            # 必须先初始化：下方「年报对照」「指标权重」等处在分支外读取它，
            # 而 profit_for_sign < 0 分支不会进入均值判断逻辑，
            # 否则亏损股（如万科A）会直接抛 NameError 导致整个分析中断。
            is_anomaly = False
            if profit_for_sign < 0:
                score, level = 35.0, AnalysisLevel.POOR
                cr_comment = "净利为负时该比率失真，不按利润含金量解读"
            else:
                # ── 优先用5年均值评分，避免单期扰动（如所得税集中支付） ──
                # 判断当期是否偏离均值超过2个标准差
                if (cash_ratio_5y_avg is not None and cash_ratio_5y_std is not None
                        and cash_ratio_5y_std > 1e-6):
                    if abs(cash_ratio - cash_ratio_5y_avg) > 2 * cash_ratio_5y_std:
                        is_anomaly = True

                if is_anomaly and cash_ratio_5y_avg is not None:
                    # 当期偏离大：判断是否为短期扰动（所得税/汇兑/备货占用）
                    annual_ok = annual_cash_ratio is not None and float(annual_cash_ratio) >= 0.7
                    yoy_p_v = kwargs.get("profit_yoy")
                    profit_growing = yoy_p_v is not None and float(yoy_p_v) >= 15
                    # 短期扰动：年报健康 + (利润高增 或 当期值非崩盘性低)
                    is_temporary = annual_ok and (profit_growing or cash_ratio > 0.3)
                    if is_temporary:
                        # 短期扰动（所得税集中支付/战略备货/汇兑），不降级，按5年均值评分
                        score_ratio = cash_ratio_5y_avg
                        # 按均值打分 → 标签与展示值也必须切到均值口径
                        cr_label = "经营现金流/净利润(5年均值)"
                        reason = "利润高增+战略备货" if profit_growing else "所得税/汇兑等"
                        cr_comment = (
                            f"当期经营现金流/净利={cash_ratio:.2f}偏离5年均值{cash_ratio_5y_avg:.2f}"
                            f"（{cash_ratio_5y_std:.2f}σ），但年报健康，"
                            f"疑为{reason}短期扰动，按5年均值评分"
                        )
                    else:
                        # 结构性恶化，按当期值评分
                        score_ratio = cash_ratio
                        cr_label = "经营现金流/净利润(当期)"
                        reason2 = "年报不健康" if not annual_ok else "利润无增长"
                        cr_comment = (
                            f"当期经营现金流/净利={cash_ratio:.2f}显著偏离5年均值{cash_ratio_5y_avg:.2f}"
                            f"且{reason2}，疑似结构性恶化"
                        )
                elif cash_ratio_5y_avg is not None:
                    # 当期正常，按5年均值评分
                    score_ratio = cash_ratio_5y_avg
                    cr_label = "经营现金流/净利润(5年均值)"
                    cr_comment = (
                        f"5年均值{score_ratio:.2f}（当期{cash_ratio:.2f}），"
                        f"避免单期扰动，按趋势值评分"
                    )
                else:
                    score_ratio = cash_ratio
                    cr_comment = ">1 利润含金量高，<0.5 警惕利润注水"

                # 评分标准：>0.8=A档, 0.5-0.8=B档, 0.3-0.5=C档, <0.3=D档
                score, level = self._score_by_range(score_ratio, (0.8, 5.0), (0.5, 0.8), (0.3, 0.5))
                if use_latest and cash_ratio < 0.4 and not is_anomaly:
                    # ── 年报修正锚：上年年报健康时，中报/季报低比值可能是阶段性现象 ──
                    # 战略备货（存货↑）+ 订单饱满（应收↑）导致现金流占用，≠利润注水
                    #
                    # 本分支一律按「当期值」定性与打分，因此名称与展示值必须同步切到当期。
                    # 否则会出现 name=「(5年均值)」、value=1.16（五年均值）、score=25（当期口径）
                    # 这种名/值/分三者互相矛盾的报告（深圳华强即如此）。
                    cr_label = "经营现金流/净利润(当期)"
                    score_ratio = cash_ratio
                    annual_ok = annual_cash_ratio is not None and float(annual_cash_ratio) >= 0.7
                    yoy_p_v = kwargs.get("profit_yoy")
                    profit_growing = yoy_p_v is not None and float(yoy_p_v) >= 15
                    if annual_ok and profit_growing:
                        # 年报健康 + 利润高增 → 阶段性现金流占用，非结构性恶化
                        score = 50.0
                        level = AnalysisLevel.NEUTRAL
                        cr_comment = (
                            f"当期({period_for_cr})经营现金流/净利仅 {cash_ratio:.1%}，"
                            f"但上年年报为 {float(annual_cash_ratio):.2f}（健康）；"
                            f"利润高增(+{float(yoy_p_v):.0f}%)背景下，"
                            f"现金流占用多为战略备货/订单扩张所致，非利润注水"
                        )
                        warnings.append(
                            f"当期现金流/净利 {cash_ratio:.1%} 偏低（年报 {float(annual_cash_ratio):.2f} 健康），"
                            f"关注存货/应收占用是否为战略备货型（订单饱满）而非赊销堆积型"
                        )
                    else:
                        score = min(score, 25.0)
                        level = AnalysisLevel.DANGER
                        # 年报对照锚的措辞必须与事实一致：进入本分支只说明
                        # 「年报健康 + 利润高增」不同时成立。若 annual_ok 为假，
                        # 上年年报本身也不健康（深圳华强 2025 年报 OCF/净利 -2.14），
                        # 此时再写「勿被上年年报高含金量掩盖」即自相矛盾。
                        if annual_cash_ratio is None:
                            _annual_txt = "且无可对照的健康年报锚"
                        elif annual_ok:
                            _annual_txt = (
                                f"上年年报 {float(annual_cash_ratio):.2f} 虽健康，"
                                f"但本期利润未高增，现金流回落不宜按扩张占用豁免"
                            )
                        else:
                            _annual_txt = (
                                f"上年年报 {float(annual_cash_ratio):.2f} 同样不健康，"
                                f"已连续两期利润含金量偏低"
                            )
                        cr_comment = (
                            f"当期({period_for_cr})经营现金流/净利仅 {cash_ratio:.1%}，"
                            f"显著弱于健康阈值；{_annual_txt}"
                        )
                        warnings.append(
                            f"当期现金流恶化：经营现金流/净利润={cash_ratio:.1%}（{period_for_cr}），"
                            f"利润含金量偏低"
                        )
            # ── 商业模式差异化：分销/贸易类扩张期现金流为负属行业常态 ──
            if is_dist:
                _relaxed = self._score_by_range(
                    score_ratio, (0.5, 5.0), (0.2, 0.5), (0.05, 0.2)
                )[0]
                if dist_expanding:
                    score = max(score, _relaxed, 52.0)
                    level = score_to_level(score)
                    cr_comment += (
                        f"；分销模式（低净利率高周转，上游预付/下游长账期）下，"
                        f"营收{float(rev_yoy_v):+.1f}% 扩张期经营现金流为负属行业常态，"
                        f"已按分销口径下移阈值"
                    )
                else:
                    score = max(score, min(_relaxed, 55.0))
                    level = score_to_level(score)
                    cr_comment += "；已按分销/贸易商业模式下移阈值"
            if kwargs.get("latest_cash_ratio_source") and use_latest:
                cr_comment += f"；口径：{kwargs['latest_cash_ratio_source']}"
            indicators.append(
                IndicatorResult(
                    name=cr_label,
                    value=round(score_ratio, 2),
                    score=score,
                    level=level,
                    trend=self._calc_trend(cash_ratio_series) if len(cash_ratio_series) else "flat",
                    weight=3.5 if use_latest and cash_ratio < 0.4 and not is_anomaly else 3.0,
                    comment=cr_comment,
                    period=period_for_cr,
                )
            )
            # ── 现金流改善趋势：连续环比改善说明阶段性占用正在修复 ──
            # 高成长扩张期企业（如半导体）现金流滞后是常态，关键看趋势方向
            ocf_trend_boost = 0.0
            if len(ocf.dropna()) >= 3:
                ocf_vals = ocf.dropna().values
                qoq_changes = []
                for i in range(1, len(ocf_vals)):
                    prev_v = float(ocf_vals[i - 1])
                    curr_v = float(ocf_vals[i])
                    if abs(prev_v) > 1e-6:
                        qoq_changes.append((curr_v - prev_v) / abs(prev_v))
                    elif curr_v > 0:
                        qoq_changes.append(1.0)  # 从负转正视为改善
                    else:
                        qoq_changes.append(0.0)
                # 检查最近2-3期是否连续改善
                recent = qoq_changes[-3:] if len(qoq_changes) >= 3 else qoq_changes[-2:] if len(qoq_changes) >= 2 else []
                consecutive_improve = 0
                for ch in reversed(recent):
                    if ch > 0:
                        consecutive_improve += 1
                    else:
                        break
                if consecutive_improve >= 2:
                    ocf_trend_boost = 12.0 if consecutive_improve >= 3 else 8.0
                    trend_label = "连续3期" if consecutive_improve >= 3 else "连续2期"
                    indicators.append(
                        IndicatorResult(
                            name="现金流改善趋势",
                            value=consecutive_improve,
                            score=80.0 if consecutive_improve >= 3 else 72.0,
                            level=AnalysisLevel.GOOD,
                            trend="up",
                            weight=1.5,
                            comment=f"经营现金流{trend_label}环比改善，扩张期现金流占用正在修复",
                            period=latest_period,
                        )
                    )
            if use_latest and annual_cash_ratio is not None:
                _bench_ocf = (
                    float(distribution_benchmarks().get("ocf_to_profit") or 0)
                    if is_dist
                    else None
                )
                if is_dist and _bench_ocf and annual_cash_ratio < 0:
                    # 分销/贸易：同业经营现金流/净利中位为负（实测 -2.25），
                    # 此处仅作年报对照，与当期同口径按同业中位相对评估，
                    # 不套用制造业 (1.0/0.7/0.4) 绝对阈值
                    a_score = 55.0 if annual_cash_ratio >= _bench_ocf else 40.0
                    a_level = score_to_level(a_score)
                else:
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

        # FCF 覆盖分红：动态判断 - 货币资金充裕时降为「观察项」而非核心风险
        div_info = kwargs.get("dividend_info") or {}
        if div_info and div_info.get("fcf_dividend_gap") is not None:
            gap = float(div_info["fcf_dividend_gap"])
            fcf_amt = div_info.get("fcf")
            cash_div = div_info.get("cash_total")
            # 统一转亿显示：必须按绝对值判断量级，否则负值（FCF<0）不换算，
            # 会输出「自由现金流 -1119194300亿」这类量级错误
            fcf_yi = float(fcf_amt) / 1e8 if abs(float(fcf_amt or 0)) > 1e6 else float(fcf_amt or 0)
            cash_yi = float(cash_div) / 1e8 if abs(float(cash_div or 0)) > 1e6 else float(cash_div or 0)
            gap_yi = abs(gap) / 1e8 if abs(gap) > 1e6 else abs(gap)
            # 从 fd 年报序列取货币资金（用于动态判断短期分红可持续性）
            if "monetary_funds" in fd.columns:
                mf_series = fd["monetary_funds"].dropna()
                cash_reserve = float(mf_series.iloc[-1]) if len(mf_series) else 0.0
            else:
                cash_reserve = 0.0
            cash_reserve_yi = cash_reserve / 1e8 if cash_reserve > 1e6 else 0.0
            if gap < 0 and fcf_amt is not None and cash_div is not None:
                # ── 综合货币资金覆盖倍数 + 经营现金流强度 ──
                # 而非单纯看 FCF/分红比值，避免对现金奶牛+高利润含金量的误判
                ocf_np_ratio = kwargs.get("latest_cash_ratio")
                if ocf_np_ratio is None and annual_cash_ratio is not None:
                    ocf_np_ratio = annual_cash_ratio
                ocf_strong = (
                    ocf_np_ratio is not None
                    and float(ocf_np_ratio) >= 1.5
                )
                cash_multi = (
                    cash_reserve_yi / max(gap_yi, 1)
                    if cash_reserve_yi > 0 and gap_yi > 0
                    else 0.0
                )
                if cash_reserve_yi > 0 and cash_reserve_yi >= gap_yi * 2:
                    # 货币资金≥2倍缺口 → 短期可持续性无虞
                    cover_score = 75.0
                    cover_comment = (
                        f"自由现金流 {_fmt_yi(fcf_yi)}亿低于现金分红 {_fmt_yi(cash_yi)}亿，"
                        f"缺口约{_fmt_yi(gap_yi)}亿；但账面货币资金 {_fmt_yi(cash_reserve_yi)}亿 "
                        f"覆盖缺口{cash_multi:.1f}倍，短期可持续性无虞"
                    )
                elif cash_reserve_yi > 0 and cash_reserve_yi >= gap_yi:
                    # 货币资金可覆盖缺口但缓冲有限
                    cover_score = 70.0
                    cover_comment = (
                        f"自由现金流 {_fmt_yi(fcf_yi)}亿低于现金分红 {_fmt_yi(cash_yi)}亿，"
                        f"缺口约{_fmt_yi(gap_yi)}亿；账面货币资金 {_fmt_yi(cash_reserve_yi)}亿 "
                        f"可覆盖但缓冲有限，跟踪经营现金流修复"
                    )
                else:
                    # 货币资金不足以覆盖缺口
                    cover_score = 60.0
                    cover_comment = (
                        f"自由现金流 {_fmt_yi(fcf_yi)}亿低于现金分红 {_fmt_yi(cash_yi)}亿，"
                        f"缺口约{_fmt_yi(gap_yi)}亿，账面货币资金 {_fmt_yi(cash_reserve_yi)}亿 "
                        f"覆盖不足，高分红可持续性核心风险点"
                    )
                    warnings.append(cover_comment)
                # ── OCF强度高 + 货币资金充裕 → 综合加分 ──
                # 经营现金流/净利≥1.5（利润含金量高）+ 货币资金≥3倍缺口
                # → 分红可持续性远超FCF/分红比值所反映的
                if ocf_strong and cash_multi >= 3.0:
                    boost = 8.0
                    cover_score = min(95.0, cover_score + boost)
                    cover_comment += (
                        f"；经营现金流/净利{float(ocf_np_ratio):.2f}（利润含金量高）"
                        f"+货币资金覆盖{cash_multi:.1f}倍，综合加分+{boost:.0f}"
                    )
                elif ocf_strong and cash_multi >= 2.0:
                    boost = 5.0
                    cover_score = min(95.0, cover_score + boost)
                    cover_comment += (
                        f"；经营现金流/净利{float(ocf_np_ratio):.2f}（利润含金量高）"
                        f"+货币资金覆盖{cash_multi:.1f}倍，综合加分+{boost:.0f}"
                    )
            elif gap >= 0 and fcf_amt is not None and cash_div is not None:
                cover_score = 85.0
                cover_comment = f"自由现金流 {_fmt_yi(fcf_yi)}亿覆盖现金分红 {_fmt_yi(cash_yi)}亿有余"
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
        # ── 区分"暂时滞后"（应收/存货增长合理）vs"结构恶化"（存货增速>营收2倍持续2期） ──
        yoy_profit = kwargs.get("profit_yoy")
        yoy_rev_val = kwargs.get("revenue_yoy")
        # 检测应收/存货增长 vs 营收增长，判断现金流占用性质
        is_temporary_lag = False
        is_structural_deterioration = False
        if "accounts_receivable" in fd.columns and "revenue" in fd.columns and len(fd) >= 2:
            ar_chg = fd["accounts_receivable"].pct_change(fill_method=None).dropna()
            rev_chg = fd["revenue"].pct_change(fill_method=None).dropna()
            if len(ar_chg) and len(rev_chg):
                ar_g = float(ar_chg.iloc[-1])
                rev_g = float(rev_chg.iloc[-1])
                if pd.notna(ar_g) and pd.notna(rev_g) and rev_g > 0:
                    # 应收增速 < 营收2倍 → 应收增长合理，现金流占用是暂时的
                    if ar_g < rev_g * 2:
                        is_temporary_lag = True
        if "inventory" in fd.columns and "revenue" in fd.columns and len(fd) >= 3:
            inv_chg = fd["inventory"].pct_change(fill_method=None).dropna()
            rev_chg_inv = fd["revenue"].pct_change(fill_method=None).dropna()
            if len(inv_chg) >= 2 and len(rev_chg_inv) >= 2:
                # 检查最近2期存货增速是否都 > 营收2倍
                inv_recent = inv_chg.iloc[-2:]
                rev_recent = rev_chg_inv.iloc[-2:]
                structural_count = 0
                for iv, rv in zip(inv_recent, rev_recent):
                    if pd.notna(iv) and pd.notna(rv) and rv > 0 and iv > rv * 2:
                        structural_count += 1
                if structural_count >= 2:
                    is_structural_deterioration = True
                    is_temporary_lag = False  # 结构恶化优先级更高

        # ── 应收+存货双低：营运资本增长合理，现金流评分保底 ──
        healthy_wc = False
        order_backed_inv = False  # 订单备货型存货：存货增速<营收×0.7 + 合同负债正增长
        _wc_note_parts: list[str] = []  # 评分依据注释
        if "accounts_receivable" in fd.columns and "inventory" in fd.columns and "revenue" in fd.columns and len(fd) >= 2:
            ar_chg_wc = fd["accounts_receivable"].pct_change(fill_method=None).dropna()
            inv_chg_wc = fd["inventory"].pct_change(fill_method=None).dropna()
            rev_chg_wc = fd["revenue"].pct_change(fill_method=None).dropna()
            if len(ar_chg_wc) and len(inv_chg_wc) and len(rev_chg_wc):
                ar_g = float(ar_chg_wc.iloc[-1])
                inv_g = float(inv_chg_wc.iloc[-1])
                rev_g = float(rev_chg_wc.iloc[-1])
                if pd.notna(ar_g) and pd.notna(inv_g) and pd.notna(rev_g) and rev_g > 0:
                    if ar_g < rev_g and inv_g < rev_g * 1.5:
                        healthy_wc = True
                        _wc_note_parts.append(
                            f"应收增速{ar_g*100:.1f}%、存货增速{inv_g*100:.1f}%"
                            f"均低于营收增速{rev_g*100:.1f}%"
                        )
                    # ── 订单备货型存货检测 ──
                    if inv_g < rev_g * 0.7 and "advance_receipts" in fd.columns:
                        adv_series = fd["advance_receipts"].dropna()
                        if len(adv_series) >= 2:
                            adv_latest = float(adv_series.iloc[-1])
                            adv_prev = float(adv_series.iloc[-2])
                            if adv_latest > 0 and adv_prev > 0 and adv_latest > adv_prev:
                                order_backed_inv = True
                                _wc_note_parts.append(
                                    f"合同负债/预收同比增{(adv_latest/adv_prev-1)*100:.1f}%，"
                                    f"存货为订单备货而非积压"
                                )

        if yoy_profit is not None and cash_ratio is not None and len(profit_clean) and float(profit_clean.iloc[-1]) > 0:
            yp = float(yoy_profit)
            if yp >= 15 and cash_ratio < 0.5:
                gap = yp / 100.0 - cash_ratio
                # ── 年报修正锚：上年年报健康时，背离多为阶段性，降低惩罚 ──
                annual_healthy = (
                    use_latest and annual_cash_ratio is not None
                    and float(annual_cash_ratio) >= 0.7
                )
                # ── 暂时滞后型：应收/存货增长合理，现金流占用是扩张所致 ──
                if is_dist and dist_expanding and not is_structural_deterioration:
                    # 分销/贸易扩张期：现金流占用是商业模式使然，评分轻扣且设下限，
                    # 关注点转为「营运资本质量 + 是否随营收收敛」而非单期正负
                    div_score = max(45.0, min(72.0, 72.0 - gap * 4.0))
                    div_comment = (
                        f"净利同比+{yp:.1f}% 但经营现金流/净利={cash_ratio:.2f}；"
                        f"分销/贸易模式（上游预付、下游长账期）在营收扩张期必然占用营运资本，"
                        f"不按制造业含金量口径扣分，重点跟踪应收账龄与存货周转是否随规模收敛"
                    )
                elif is_temporary_lag and not is_structural_deterioration:
                    # 订单备货型：合同负债增长确认存货为订单驱动，进一步轻扣
                    if order_backed_inv:
                        div_score = max(62.0, min(80.0, 80.0 - gap * 8.0))
                        div_comment = (
                            f"净利同比+{yp:.1f}% 但经营现金流/净利={cash_ratio:.2f}；"
                            f"应收/存货增速均低于营收增速，合同负债增长确认订单备货型，"
                            f"属扩张期正常波动，评分轻扣"
                        )
                    else:
                        div_score = max(55.0, min(75.0, 75.0 - gap * 10.0))
                        div_comment = (
                            f"净利同比+{yp:.1f}% 但经营现金流/净利={cash_ratio:.2f}；"
                            f"应收增速未超营收2倍，属扩张期暂时滞后而非利润注水，"
                            f"叠加现金流环比改善趋势，评分轻扣"
                        )
                elif is_structural_deterioration:
                    # 结构恶化：存货增速持续超营收2倍，现金流评分压至40以下
                    div_score = max(10.0, min(38.0, 38.0 - gap * 20.0))
                    div_comment = (
                        f"净利同比+{yp:.1f}% 但经营现金流/净利={cash_ratio:.2f}；"
                        f"存货增速连续2期超营收2倍，疑似结构性堆积，"
                        f"现金流质量严重恶化"
                    )
                    warnings.append("存货增速连续2期超营收2倍，疑似结构性堆积，警惕存货减值风险")
                elif annual_healthy:
                    # 年报健康 + 当期低 = 阶段性占用，背离度打折
                    div_score = max(40.0, min(65.0, 65.0 - gap * 20.0))
                    div_comment = (
                        f"净利同比+{yp:.1f}% 但当期经营现金流/净利={cash_ratio:.2f}，"
                        f"上年年报 {float(annual_cash_ratio):.2f} 健康；"
                        f"背离多为战略备货/订单扩张所致，非结构性恶化"
                    )
                else:
                    div_score = max(10.0, min(55.0, 55.0 - gap * 35.0))
                    div_comment = (
                        f"净利同比+{yp:.1f}% 但经营现金流/净利={cash_ratio:.2f}，"
                        f"警惕赊销或存货堆积驱动的账面增长"
                    )
                indicators.append(
                    IndicatorResult(
                        name="增长质量背离度",
                        value=round(gap, 2),
                        score=round(div_score, 1),
                        level=score_to_level(div_score),
                        weight=2.5 if not annual_healthy and not is_temporary_lag else 1.5,
                        comment=div_comment,
                        period=latest_period,
                    )
                )
                if not annual_healthy and not is_temporary_lag and not is_structural_deterioration:
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
                    msg, _ = ar_turnover_warning(
                        quality_context=quality_ar, severe_wc_spike=True, distribution=is_dist
                    )
                    warnings.append(msg)
                elif rev_g < 0 and ar_g > rev_g:
                    msg, _ = ar_turnover_warning(
                        quality_context=quality_ar, distribution=is_dist
                    )
                    warnings.append(msg)
                elif rev_g >= 0 and ar_g > rev_g + 0.10:
                    msg, _ = ar_turnover_warning(
                        quality_context=quality_ar, distribution=is_dist
                    )
                    warnings.append(msg)

        if len(ocf.dropna()) >= 3 and (ocf.iloc[-3:] < 0).all():
            warnings.append("经营现金流连续3期为负，造血能力严重不足")

        # ── OCF 环比大幅改善（>100%）额外加分，>200% 加分更多 ──
        ocf_rebound_boost = 0.0
        if len(ocf.dropna()) >= 2:
            ocf_vals_rb = ocf.dropna().values
            prev_ocf = float(ocf_vals_rb[-2])
            curr_ocf = float(ocf_vals_rb[-1])
            if abs(prev_ocf) > 1e-6:
                ocf_qoq = (curr_ocf - prev_ocf) / abs(prev_ocf)
                if ocf_qoq > 3.0:
                    ocf_rebound_boost = 10.0
                    indicators.append(
                        IndicatorResult(
                            name="经营现金流环比大幅改善",
                            value=round(ocf_qoq * 100, 1),
                            score=95.0,
                            level=AnalysisLevel.GOOD,
                            trend="up",
                            weight=1.0,
                            comment=f"最新期经营现金流环比改善{ocf_qoq*100:.0f}%（>300%），造血能力强劲修复",
                            period=latest_period,
                        )
                    )
                elif ocf_qoq > 2.0:
                    ocf_rebound_boost = 8.0
                    indicators.append(
                        IndicatorResult(
                            name="经营现金流环比大幅改善",
                            value=round(ocf_qoq * 100, 1),
                            score=95.0,
                            level=AnalysisLevel.GOOD,
                            trend="up",
                            weight=1.0,
                            comment=f"最新期经营现金流环比改善{ocf_qoq*100:.0f}%（>200%），造血能力强劲修复",
                            period=latest_period,
                        )
                    )
                elif ocf_qoq > 1.0:
                    ocf_rebound_boost = 5.0
                    indicators.append(
                        IndicatorResult(
                            name="经营现金流环比大幅改善",
                            value=round(ocf_qoq * 100, 1),
                            score=90.0,
                            level=AnalysisLevel.GOOD,
                            trend="up",
                            weight=1.0,
                            comment=f"最新期经营现金流环比改善{ocf_qoq*100:.0f}%（>100%），造血能力显著修复",
                            period=latest_period,
                        )
                    )

        # ── 分销/贸易模式专用观察项：营运资本质量与现金流收敛性 ──
        if is_dist:
            ar_m = kwargs.get("ar_metrics") or {}
            days_latest = ar_m.get("days_latest")
            days_delta = ar_m.get("days_delta_5y")
            _obs_parts = [
                "分销模式核心不是单期经营现金流正负，而是①应收账龄结构 "
                "②存货周转与跌价风险 ③现金流是否随营收规模收敛"
            ]
            if days_latest is not None:
                _obs_parts.append(f"应收周转天数 {float(days_latest):.0f} 天")
                if days_delta is not None:
                    _obs_parts.append(
                        f"较5年前 {float(days_delta):+.0f} 天"
                        f"（{'账期拉长，占用加剧' if float(days_delta) > 30 else '基本稳定'}）"
                    )
            indicators.append(
                IndicatorResult(
                    name="分销模式观察项",
                    value=round(float(days_latest), 1) if days_latest is not None else 0.0,
                    score=62.0 if (days_delta is None or float(days_delta) <= 30) else 48.0,
                    level=AnalysisLevel.NEUTRAL
                    if (days_delta is None or float(days_delta) <= 30)
                    else AnalysisLevel.POOR,
                    trend="down" if (days_delta is not None and float(days_delta) > 30) else "flat",
                    weight=1.5,
                    comment="；".join(_obs_parts),
                    period=ar_m.get("period") or latest_period,
                )
            )
            if days_delta is not None and float(days_delta) > 30:
                warnings.append(
                    f"分销模式应收质量：周转天数较5年前拉长{float(days_delta):.0f}天，"
                    f"账期放宽可能掩盖下游需求压力与坏账风险"
                )

        module_score = self._weighted_score(indicators) if indicators else 0.0
        # OCF 环比大幅改善额外加分
        if ocf_rebound_boost > 0:
            module_score += ocf_rebound_boost
        # 下限保护前的真实加权分：必须留痕，否则「模块分为何停在 52.0 不动」
        # 在报告里无法解释（原始加权可能远低于下限，指标级再加分也纹丝不动）。
        raw_module_score = round(module_score, 1)
        floor_applied: str | None = None
        # ── 应收+存货双低 → 现金流评分保底 55-60（仅调分，不作为风险提示） ──
        if healthy_wc and module_score < 55.0:
            module_score = 58.0
            floor_applied = "healthy_working_capital"
        # ── 分销/贸易扩张期保底：商业模式决定的负现金流不应拉低到 D/E 档 ──
        if dist_expanding and module_score < 52.0:
            module_score = 52.0
            floor_applied = "distribution_expanding"
            warnings.append(
                f"分销/贸易扩张期经营现金流为负属行业常态，现金流评分已按商业模式下限保护"
                f"（原始加权 {raw_module_score:.1f} 分 → 52 分）；仍需跟踪应收账龄与存货跌价"
            )

        return ModuleResult(
            module_name="现金流质量",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={
                "paper_wealth": bool(paper_wealth),
                "consecutive_loss_years": int(consec_loss),
                "healthy_working_capital": healthy_wc,
                "order_backed_inventory": order_backed_inv,
                "business_model": "distribution" if is_dist else None,
                "distribution_expanding": bool(dist_expanding),
                "raw_weighted_score": raw_module_score,
                "floor_applied": floor_applied,
                "scoring_note": "；".join(_wc_note_parts) if _wc_note_parts else None,
            },
        )
