from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, ModuleResult, score_to_level
from app.analysis.dividend_profile import classify_dividend_asset
from app.analysis.receivable_quality import ar_turnover_warning, is_quality_receivable_context


class RiskAnalyzer(BaseAnalyzer):
    """排雷：商誉、应收、存贷双高、现金流背离、ST/退市；观察级质押不按生存危机处理。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        warnings: list[str] = []
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
        ar_metrics = kwargs.get("ar_metrics")
        if ar_metrics:
            days_latest = ar_metrics.get("days_latest")
            days_5y = ar_metrics.get("days_5y_ago")
            days_delta = ar_metrics.get("days_delta_5y")
            notes_yoy = ar_metrics.get("notes_yoy_pct")
            parts: list[str] = []
            if days_latest is not None and days_5y is not None and days_delta is not None:
                parts.append(
                    f"应收账款周转天数从{float(days_5y):.1f}天升至{float(days_latest):.1f}天"
                    f"（五年+{float(days_delta):.1f}天）"
                )
            if notes_yoy is not None and float(notes_yoy) > 30:
                parts.append(f"应收票据同比+{float(notes_yoy):.0f}%")
            if parts:
                parts.append("反映下游付款节奏放缓")
                if bool(div_profile_early.get("is_dividend_asset")) or any(k in industry for k in ("煤炭", "焦炭", "煤业", "开采")):
                    risk_score -= 3
                else:
                    risk_score -= 6
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

        if cash > 1e9 and short_debt > 1e9 and short_to_cash >= 0.40:
            warnings.append("存贷双高，需关注资金真实性")
            risk_score -= 20
        elif cash > 5e9 and short_to_cash < 0.25 and (dr_f is None or dr_f < 45):
            if short_debt < 1e8 and ibd_f < 1e8:
                warnings.append(
                    "资金极度充裕，近乎零有息负债（预收/合同负债等经营性负债≠银行借款）"
                )
            elif is_div or ibd_f <= cash * 0.85:
                # 神华等：现金奶牛 + 低短债占比，负债多为经营性占用
                warnings.append(
                    "资金极度充裕，产业链话语权强（经营性负债为主，有息负债可控）"
                )
        elif is_div and cash > 5e9 and short_to_cash < 0.35:
            warnings.append(
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
            },
        )
