from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, ModuleResult, score_to_level


class RiskAnalyzer(BaseAnalyzer):
    """排雷：商誉、应收、存贷双高、现金流背离、ST/退市。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        warnings: list[str] = []
        risk_score = 100.0
        if financial_data.empty:
            return ModuleResult("风险预警", 50, AnalysisLevel.NEUTRAL, warnings=["数据不足，风险评分中性"])

        latest = financial_data.iloc[-1]
        prev = financial_data.iloc[-2] if len(financial_data) > 1 else latest

        name = str(kwargs.get("name") or "")
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

        ar0 = float(prev.get("accounts_receivable", 0) or 0)
        ar1 = float(latest.get("accounts_receivable", 0) or 0)
        rev0 = float(prev.get("revenue", 0) or 0)
        rev1 = float(latest.get("revenue", 0) or 0)
        if ar0 > 0 and rev0 > 0:
            ar_growth = ar1 / ar0 - 1
            rev_growth = rev1 / rev0 - 1
            if rev_growth < 0 and ar_growth > rev_growth:
                warnings.append("应收账款周转恶化，回款极其困难")
                risk_score -= 15
            elif rev_growth > 0 and ar_growth > rev_growth + 0.3 and ar_growth > rev_growth * 2:
                warnings.append("应收账款增速远超营收，可能存在虚增收入风险")
                risk_score -= 15

        cash = float(latest.get("monetary_funds", 0) or 0)
        short_debt = float(latest.get("short_term_borrowings", 0) or 0)
        if cash > 1e9 and short_debt > 1e9:
            warnings.append("存贷双高，需关注资金真实性")
            risk_score -= 20

        ocf = float(latest.get("operating_cashflow", 0) or 0)
        profit = float(latest.get("net_profit", 0) or 0)
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

        pledge_ratio = float(kwargs.get("pledge_ratio") or 0)
        if pledge_ratio > 0.5:
            warnings.append(f"大股东质押率 {pledge_ratio:.0%}，平仓风险")
            risk_score -= 20

        audit = kwargs.get("audit_opinion", "标准无保留")
        if audit and audit != "标准无保留":
            warnings.append(f"审计意见：{audit}")
            risk_score -= 30

        major_events = kwargs.get("major_risk_events") or []
        if major_events:
            labels = sorted({str(e.get("label") or "") for e in major_events if e.get("label")})
            if labels:
                warnings.append("重大风险事件：" + "、".join(labels))
            risk_score = min(risk_score, 15.0)
            delist_risk = True

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
            },
        )
