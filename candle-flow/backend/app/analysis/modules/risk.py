from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, ModuleResult, score_to_level
from app.analysis.dividend_profile import classify_dividend_asset
from app.analysis.receivable_quality import ar_turnover_warning, is_quality_receivable_context


class RiskAnalyzer(BaseAnalyzer):
    """排雷：商誉、应收、存贷双高、现金流背离、ST/退市；观察级质押不按生存危机处理。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        warnings: list[str] = []
        highlights: list[str] = []  # 优势/亮点，不参与风险扣分
        risk_score = 100.0
        if financial_data.empty:
            return ModuleResult("风险预警", 50, AnalysisLevel.NEUTRAL, warnings=["数据不足，风险评分中性"])

        latest = financial_data.iloc[-1]
        prev = financial_data.iloc[-2] if len(financial_data) > 1 else latest

        name = str(kwargs.get("name") or "")
        industry = str(kwargs.get("industry") or "")
        delist_risk = False
        if "ST" in name.upper() or "退" in name:
            warnings.append(f"名称含风险标识（{name}），存在退市/重整风险")
            risk_score -= 35
            delist_risk = True

        goodwill = float(latest.get("goodwill", 0) or 0)
        equity = float(latest.get("equity", 1) or 1)
        if equity > 0 and goodwill / equity > 0.3:
            warnings.append(f"商誉占净资产 {goodwill / equity:.0%}，减值风险较高")
            risk_score -= 25

        ocf_early = float(latest.get("operating_cashflow", 0) or 0)
        profit_early = float(latest.get("net_profit", 0) or 0)
        div_profile_early = classify_dividend_asset(
            dividend_yield_pct=kwargs.get("dividend_yield"),
            pe_ttm=kwargs.get("pe_ttm"),
            payout_ratio_pct=kwargs.get("payout_ratio_pct"),
        )
        quality_ar = is_quality_receivable_context(
            name=name,
            industry=industry,
            ocf=ocf_early,
            net_profit=profit_early,
            is_dividend_asset=bool(div_profile_early.get("is_dividend_asset")),
            cash_ratio=kwargs.get("latest_cash_ratio"),
        )

        ar0 = float(prev.get("accounts_receivable", 0) or 0)
        ar1 = float(latest.get("accounts_receivable", 0) or 0)
        rev0 = float(prev.get("revenue", 0) or 0)
        rev1 = float(latest.get("revenue", 0) or 0)
        if ar0 > 0 and rev0 > 0:
            ar_growth = ar1 / ar0 - 1
            rev_growth = rev1 / rev0 - 1
            if rev_growth >= 0.20 and ar_growth > rev_growth * 2 and ar_growth > 0.2:
                msg, deduct = ar_turnover_warning(quality_context=quality_ar, severe_wc_spike=True)
                warnings.append(msg)
                risk_score -= deduct
            elif rev_growth < 0 and ar_growth > rev_growth:
                msg, deduct = ar_turnover_warning(quality_context=quality_ar)
                warnings.append(msg)
                risk_score -= deduct
            elif rev_growth >= 0 and ar_growth > rev_growth + 0.10:
                msg, deduct = ar_turnover_warning(quality_context=quality_ar)
                warnings.append(msg)
                risk_score -= deduct

        # 应收账款周转天数量化（新浪审计口径，神华等周期股轻扣分）
        # ── 先检查是否已有应收周转警告（来自 ar 列分析），避免重复 ──
        ar_already_warned = any("应收" in w and ("周转" in w or "回款" in w) for w in warnings)
        ar_metrics = kwargs.get("ar_metrics")
        if ar_metrics:
            days_latest = ar_metrics.get("days_latest")
            days_5y = ar_metrics.get("days_5y_ago")
            days_delta = ar_metrics.get("days_delta_5y")
            notes_yoy = ar_metrics.get("notes_yoy_pct")
            parts: list[str] = []
            ar_is_warning = True
            if days_latest is not None and days_5y is not None and days_delta is not None:
                delta_f = float(days_delta)
                if delta_f > 0:
                    parts.append(
                        f"应收账款周转天数从{float(days_5y):.1f}天升至{float(days_latest):.1f}天"
                        f"（五年+{delta_f:.1f}天）"
                    )
                elif delta_f < -2:
                    # 天数下降 ≥2天：是改善，不应扣分
                    highlights.append(
                        f"应收账款周转天数从{float(days_5y):.1f}天降至{float(days_latest):.1f}天"
                        f"（五年{delta_f:.1f}天，回款效率改善）"
                    )
                    ar_is_warning = False
                else:
                    parts.append(
                        f"应收账款周转天数{float(days_latest):.1f}天（五年{delta_f:+.1f}天）"
                    )
            if notes_yoy is not None and float(notes_yoy) > 30:
                parts.append(f"应收票据同比+{float(notes_yoy):.0f}%")
            if parts:
                if ar_is_warning:
                    parts.append("反映下游付款节奏放缓")
                # 神华等煤炭龙头：前五大客户多为五大发电集团（央企），坏账风险可控
                if "神华" in name or "601088" in str(kwargs.get("symbol") or ""):
                    parts.append("客户信用质量高（五大发电集团央企为主），坏账风险可控")
                if bool(div_profile_early.get("is_dividend_asset")) or any(k in industry for k in ("煤炭", "焦炭", "煤业", "开采")):
                    risk_score -= 3
                elif ar_is_warning:
                    risk_score -= 6
                # ── 合并应收周转警告：若已有来自 ar 列分析的同类警告，合并为一条 ──
                if ar_already_warned:
                    # 找到已有警告并追加周转天数信息
                    for i, w in enumerate(warnings):
                        if "应收" in w and ("周转" in w or "回款" in w):
                            # 将天数信息追加到已有警告
                            days_info = parts[0] if parts else ""
                            if days_info and days_info not in w:
                                warnings[i] = w.rstrip("。") + f"；{days_info}"
                            break
                else:
                    warnings.append("，".join(parts))

        cash = float(latest.get("monetary_funds", 0) or 0)
        short_debt = float(latest.get("short_term_borrowings", 0) or kwargs.get("short_term_borrowings") or 0)
        ibd = kwargs.get("interest_bearing_debt")
        if ibd is None:
            ibd = latest.get("interest_bearing_debt")
        try:
            ibd_f = float(ibd) if ibd is not None else short_debt
        except (TypeError, ValueError):
            ibd_f = short_debt
        debt_ratio = kwargs.get("debt_ratio")
        try:
            dr_f = float(debt_ratio) if debt_ratio is not None else None
        except (TypeError, ValueError):
            dr_f = None

        # 存贷双高：真短债相对现金比例过高才算（神华等短债≪现金≠双高）
        short_to_cash = (short_debt / cash) if cash > 0 else 0.0
        div_profile = div_profile_early
        is_div = bool(div_profile.get("is_dividend_asset"))
        # 周期/资源股（磷化工/农化/矿等）现金+短债双高是行业特性（运营资金需求大）
        cycle_industries_risk = ("煤炭", "焦炭", "化工", "农化", "化肥", "磷", "矿", "有色", "钢铁", "航运")
        is_cycle_risk = any(k in industry for k in cycle_industries_risk)

        if cash > 1e9 and short_debt > 1e9 and short_to_cash >= 0.40:
            if is_cycle_risk:
                # 周期/资源股：双高是行业特性（大量运营资金+票据贴现），不扣分
                warnings.append("存贷双高（周期/资源股行业特性：运营资金需求大+票据贴现所致）")
                risk_score -= 2  # 轻扣而非重扣20
            else:
                warnings.append("存贷双高，需关注资金真实性")
                risk_score -= 20
        elif cash > 5e9 and short_to_cash < 0.25 and (dr_f is None or dr_f < 45):
            if short_debt < 1e8 and ibd_f < 1e8:
                highlights.append(
                    "资金极度充裕，近乎零有息负债（预收/合同负债等经营性负债≠银行借款）"
                )
            elif is_div or ibd_f <= cash * 0.85:
                # 神华等：现金奶牛 + 低短债占比，负债多为经营性占用
                highlights.append(
                    "资金极度充裕，产业链话语权强（经营性负债为主，有息负债可控）"
                )
        elif is_div and cash > 5e9 and short_to_cash < 0.35:
            highlights.append(
                "高股息现金奶牛：资金充裕、分红能力强，短期借款相对现金可控"
            )

        # 白酒/高端消费：宏观与政策风险为观察项（轻扣分）
        if any(k in industry for k in ("白酒", "酿酒", "白酒制造")) or "茅台" in name:
            warnings.append(
                "宏观与政策风险（观察）：关注高端消费景气度、消费税改革及禁酒/公务消费政策对估值的影响"
            )
            risk_score -= 5
        if any(k in industry for k in ("煤炭", "焦炭", "煤业", "开采")) or "神华" in name:
            warnings.append(
                "行业风险（观察）：关注煤炭价格周期波动、长协煤履约率及分红政策稳定性"
            )
            risk_score -= 3
            if is_div:
                warnings.append(
                    "红利资产观察：高股息可持续性依赖经营现金流与分红承诺兑现"
                )
            # 资产注入并表（神华2026重大变量）
            if "神华" in name or "601088" in str(kwargs.get("symbol") or ""):
                warnings.append(
                    "资产注入并表（观察）：交易对价约1336亿（30%股份+70%现金），"
                    "注入资产2026-2028业绩承诺净利29.6/45.5/66.4亿；"
                    "关注商誉减值风险及整合协同效应"
                )

        # ── 光模块/光通信行业特有风险 ─────────────────────────────
        _optical_kw = ("光模块", "光通信", "光电子", "光芯片", "光子")
        is_optical = (
            any(k in industry for k in _optical_kw)
            or any(k in name for k in ("中际旭创", "新易盛", "天孚通信", "光迅科技"))
            or "300308" in str(kwargs.get("symbol") or "")
        )
        if is_optical:
            # CPO技术替代风险
            warnings.append(
                "CPO技术替代风险：若共封装光学(CPO)提前规模商用，"
                "可能压缩可插拔光模块需求窗口，关注技术路线切换节奏"
            )
            risk_score -= 4
            # 供应链依赖风险
            warnings.append(
                "供应链集中风险：DSP芯片依赖博通/Marvell（全球份额>90%），"
                "EML激光器依赖Lumentum等，地缘政治或供应瓶颈可能影响交付"
            )
            risk_score -= 3
            # 客户集中风险
            warnings.append(
                "客户集中风险：前五大客户贡献超70%营收，"
                "北美云厂商资本开支波动直接影响业绩确定性"
            )
            risk_score -= 3
            # 毛利率下行风险
            gm_val = kwargs.get("latest_gross_margin")
            if gm_val is not None and float(gm_val) >= 40:
                warnings.append(
                    f"毛利率下行风险：当前毛利率{float(gm_val):.0f}%处于高位，"
                    "竞争加剧后可能向制造业正常水平(15-20%)回落，"
                    "关注产品降价节奏与成本控制能力"
                )
                risk_score -= 3

        # ── 磷化工/化肥行业特有风险 ─────────────────────────────
        _phosphate_kw = ("磷", "化肥", "硫化工")
        _is_phosphate = (
            any(k in industry for k in _phosphate_kw)
            or "云天化" in name
            or "600096" in str(kwargs.get("symbol") or "")
        )
        if _is_phosphate:
            # 磷酸铁锂产能过剩风险
            warnings.append(
                "磷酸铁锂产能过剩风险：行业低端产能过剩、高端紧俏，"
                "需关注公司高端产线客户导入进度及产品差异化竞争力"
            )
            risk_score -= 3
            # 硫磺价格波动风险
            warnings.append(
                "硫磺价格波动风险：硫磺为磷肥核心原料，"
                "采购价虽低于市场价但持续高位仍压缩磷肥毛利"
            )
            risk_score -= 3
            # 镇雄磷矿注入进度风险
            if "云天化" in name or "600096" in str(kwargs.get("symbol") or ""):
                warnings.append(
                    "镇雄磷矿注入进度风险：碗厂磷矿(24.38亿吨)2026年12月开工，"
                    "建设期约5年，取得采矿证后3年内注入，时点存在不确定性"
                )
                risk_score -= 2
            # 出口政策变动风险
            warnings.append(
                "出口政策变动风险：化肥出口法检政策调整可能影响出口量及盈利"
            )
            risk_score -= 2

        ocf = ocf_early
        profit = profit_early
        if profit > 0 and ocf < profit * 0.3:
            warnings.append("利润含金量低，经营现金流远低于净利润")
            risk_score -= 15
        elif profit < 0 and ocf > 0:
            warnings.append("亏损下经营现金为正，警惕营运负债挤现（纸面富贵）")
            risk_score -= 15

        profit_series = financial_data["net_profit"].dropna() if "net_profit" in financial_data else pd.Series(dtype=float)
        consec_loss = 0
        for v in reversed(list(profit_series)):
            if float(v) < 0:
                consec_loss += 1
            else:
                break
        if consec_loss >= 2:
            warnings.append(f"净利润连续{consec_loss}年为负，基本面持续恶化")
            risk_score -= 20

        # 质押：脏数据不参与；高质押仅观察扣分，不按退市危机
        if kwargs.get("pledge_invalid"):
            warnings.append("质押率数据异常（>100%），已忽略，请人工复核字段")
        else:
            pledge_ratio = float(kwargs.get("pledge_ratio") or 0)
            if pledge_ratio > 1.0:
                warnings.append(f"质押率读数异常 {pledge_ratio:.0%}，已忽略")
            elif pledge_ratio >= 0.8:
                warnings.append(
                    f"大股东质押率约 {pledge_ratio:.0%}（观察级：多为个人融资安排，≠公司生存危机）"
                )
                risk_score -= 15
            elif pledge_ratio >= 0.5:
                warnings.append(
                    f"大股东质押率约 {pledge_ratio:.0%}（观察级，警惕情绪杀跌）"
                )
                risk_score -= 8

        audit = kwargs.get("audit_opinion", "标准无保留")
        if audit and audit != "标准无保留":
            warnings.append(f"审计意见：{audit}")
            risk_score -= 30

        major_events = kwargs.get("major_risk_events") or []
        if major_events:
            labels = sorted({str(e.get("label") or "") for e in major_events if e.get("label")})
            if labels:
                warnings.append("生存级重大风险：" + "、".join(labels))
            risk_score = min(risk_score, 15.0)
            delist_risk = True

        observe_events = kwargs.get("observe_risk_events") or []
        if observe_events:
            labels = sorted({str(e.get("label") or "") for e in observe_events if e.get("label")})
            if labels:
                warnings.append("观察级风险：" + "、".join(labels) + "（扣分但不否决）")
            risk_score -= min(30.0, 12.0 * len(observe_events))

        risk_score = max(0.0, risk_score)
        return ModuleResult(
            module_name="风险预警",
            score=round(risk_score, 1),
            level=score_to_level(risk_score),
            warnings=warnings,
            metadata={
                "delist_risk": delist_risk,
                "consecutive_loss_years": int(consec_loss),
                "major_risk_count": len(major_events),
                "observe_risk_count": len(observe_events),
                "highlights": highlights,
            },
        )
