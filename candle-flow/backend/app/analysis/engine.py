from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.base import ModuleResult, score_to_level
from app.analysis.config import MODULE_WEIGHTS, RISK_THRESHOLD
from app.analysis.financials import build_financial_dataframe, industry_averages
from app.analysis.models.dcf import DCFModel
from app.analysis.models.relative import RelativeValuation
from app.analysis.modules.cashflow import CashflowAnalyzer
from app.analysis.modules.efficiency import EfficiencyAnalyzer
from app.analysis.modules.growth import GrowthAnalyzer
from app.analysis.modules.industry import IndustryAnalyzer
from app.analysis.modules.profitability import ProfitabilityAnalyzer
from app.analysis.modules.risk import RiskAnalyzer
from app.analysis.modules.solvency import SolvencyAnalyzer
from app.services.valuation import get_valuations
from app.utils.symbol import normalize_symbol


def _module_to_dict(m: ModuleResult) -> dict:
    return {
        "module_name": m.module_name,
        "score": m.score,
        "level": m.level.value,
        "indicators": [asdict(i) | {"level": i.level.value} for i in m.indicators],
        "warnings": m.warnings,
        "metadata": m.metadata,
    }


def rating_label(score: float) -> str:
    """字母评级，含 B+ 等细档（对齐对照报告）。"""
    if score >= 85:
        return "A"
    if score >= 80:
        return "A-"
    if score >= 74:
        return "B+"
    if score >= 70:
        return "B"
    if score >= 65:
        return "B-"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "E"


class FundamentalEngine:
    """基本面分析总引擎。"""

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = weights or dict(MODULE_WEIGHTS)
        self.analyzers = {
            "profitability": ProfitabilityAnalyzer(),
            "growth": GrowthAnalyzer(),
            "cashflow": CashflowAnalyzer(),
            "solvency": SolvencyAnalyzer(),
            "efficiency": EfficiencyAnalyzer(),
            "risk": RiskAnalyzer(),
            "industry": IndustryAnalyzer(),
        }

    def run_full_analysis(
        self,
        symbol: str,
        db: Session | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        sym = normalize_symbol(symbol)
        fin_df, meta = build_financial_dataframe(sym)
        market: dict[str, Any] = {}
        if db is not None:
            try:
                vals = get_valuations([sym], db=db)
                if vals:
                    market = vals[0]
            except Exception:
                pass

        symbol_roe = meta.get("latest_roe")
        if symbol_roe is None and not fin_df.empty and "roe" in fin_df.columns:
            symbol_roe = float(fin_df["roe"].iloc[-1])

        ctx = {
            "debt_ratio": meta.get("debt_ratio"),
            "debt_ratio_estimated": meta.get("debt_ratio_estimated", False),
            "revenue_yoy": meta.get("revenue_yoy"),
            "profit_yoy": meta.get("profit_yoy"),
            "ocf_per_share": meta.get("ocf_per_share"),
            "latest_roe": meta.get("latest_roe"),
            "symbol_roe": symbol_roe,
            "industry": meta.get("industry") or "",
            "industry_avg": industry_averages(meta.get("industry", ""), meta.get("latest_report")),
            **kwargs,
        }

        module_results: dict[str, ModuleResult] = {}
        for name, analyzer in self.analyzers.items():
            module_results[name] = analyzer.analyze(fin_df, **ctx)

        valuation = self._run_valuation(fin_df, market, meta)
        val_score = float(valuation.get("composite_valuation_score", 50.0))

        # 对照报告：盈利/成长/偿债/现金流/估值 加权；效率与行业仅展示
        composite = 0.0
        weight_sum = 0.0
        for name, w in self.weights.items():
            if name == "valuation":
                composite += val_score * w
                weight_sum += w
                continue
            result = module_results.get(name)
            if result is None:
                continue
            composite += result.score * w
            weight_sum += w
        if weight_sum > 0:
            composite /= weight_sum

        risk = module_results["risk"]
        if risk.score < RISK_THRESHOLD:
            composite *= risk.score / 100.0

        composite = round(max(0.0, min(100.0, composite)), 1)
        all_warnings: list[str] = []
        for r in module_results.values():
            all_warnings.extend(r.warnings)

        letter = rating_label(composite)
        return {
            "symbol": sym,
            "name": meta.get("name") or market.get("name") or "",
            "industry": meta.get("industry") or "",
            "report_dates": meta.get("report_dates") or [],
            "composite_score": composite,
            "final_rating": letter,
            "final_rating_letter": letter,
            "modules": {k: _module_to_dict(v) for k, v in module_results.items()},
            "valuation": valuation,
            "market": {
                "price": market.get("price"),
                "pe_ttm": market.get("pe_ttm"),
                "pb": market.get("pb"),
                "pe_percentile": market.get("pe_percentile"),
                "pb_percentile": market.get("pb_percentile"),
                "market_cap": market.get("market_cap"),
                "dividend_yield": market.get("dividend_yield"),
            },
            "warnings": all_warnings,
            "summary": self._generate_summary(composite, letter, module_results, all_warnings, val_score),
        }

    def _run_valuation(self, fin_df: pd.DataFrame, market: dict, meta: dict) -> dict:
        result: dict[str, Any] = {}
        pe = market.get("pe_ttm")
        pb = market.get("pb")
        pe_pct = market.get("pe_percentile")
        pb_pct = market.get("pb_percentile")
        div = market.get("dividend_yield")

        current = {
            "PE_TTM": pe,
            "PB": pb,
            "PS": None,
            "profit_growth_rate": meta.get("profit_yoy"),
        }
        rv = RelativeValuation()
        rel = rv.analyze(current, history=None, industry=None)
        if pe_pct is not None and "PE_TTM" in rel:
            rel["PE_TTM"]["percentile_5y"] = pe_pct
            # 绝对低估优先于历史分位：煤炭 PE<12 仍偏便宜
            if pe is not None and 0 < float(pe) <= 12:
                rel["PE_TTM"]["signal"] = "低估"
            elif pe_pct is not None and float(pe_pct) >= 75 and pe is not None and float(pe) > 20:
                rel["PE_TTM"]["signal"] = "高估"
        if pb_pct is not None and "PB" in rel:
            rel["PB"]["percentile_5y"] = pb_pct
            if pb is not None and 0 < float(pb) <= 1.5:
                rel["PB"]["signal"] = "低估"
        if div is not None:
            dy = float(div)
            rel["股息率"] = {
                "current": round(dy, 2),
                "signal": "低估" if dy >= 5 else ("合理" if dy >= 2 else "高估"),
                "percentile_5y": None,
            }
        result["relative"] = rel

        scores: list[float] = []
        for item in rel.values():
            sig = item.get("signal")
            if sig == "低估":
                scores.append(85)
            elif sig == "合理":
                scores.append(65)
            elif sig == "高估":
                scores.append(35)
        # 绝对 PE 加分
        if pe is not None and 0 < float(pe) < 10:
            scores.append(88)
        elif pe is not None and 0 < float(pe) < 15:
            scores.append(75)
        if div is not None and float(div) >= 6:
            scores.append(90)
        result["composite_valuation_score"] = sum(scores) / len(scores) if scores else 55.0

        if not fin_df.empty:
            ocf = float(fin_df.get("operating_cashflow", pd.Series([0])).iloc[-1] or 0)
            capex = float(fin_df.get("capital_expenditure", pd.Series([0])).iloc[-1] or 0)
            fcf = ocf - capex
            shares = market.get("total_shares") or 1e9
            growth = (meta.get("profit_yoy") or 5) / 100.0
            growth = max(0.02, min(0.12, growth if growth < 1 else 0.05))
            dcf = DCFModel(wacc=0.10, terminal_growth=0.02)
            result["dcf"] = dcf.value(
                base_fcf=max(fcf, ocf * 0.5) if ocf else 0,
                high_growth_rate=growth,
                transition_growth_rate=max(0.03, growth * 0.5),
                shares_outstanding=float(shares) if shares else 1e9,
            )
            price = market.get("price")
            iv = result["dcf"].get("intrinsic_value_per_share")
            if price and iv:
                result["dcf"]["margin_of_safety_pct"] = round((iv - float(price)) / float(price) * 100, 1)

        return result

    def _generate_summary(
        self,
        score: float,
        letter: str,
        modules: dict[str, ModuleResult],
        warnings: list[str],
        val_score: float,
    ) -> str:
        lines = [f"综合评分 {score:.1f} 分，评级 {letter}。"]
        for key in ("profitability", "growth", "cashflow", "solvency"):
            r = modules.get(key)
            if r:
                lines.append(f"  {r.module_name}: {r.score} 分 ({r.level.value})")
        lines.append(f"  估值合理性: {val_score:.1f} 分")
        if warnings:
            lines.append(f"\n共 {len(warnings)} 条风险提示：")
            for w in warnings[:5]:
                lines.append(f"  · {w}")
        return "\n".join(lines)


def analyze_symbol_full(db: Session, symbol: str, **kwargs) -> dict[str, Any]:
    return FundamentalEngine().run_full_analysis(symbol, db=db, **kwargs)
