from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def _json_safe(obj: Any) -> Any:
    """把 numpy/pandas 标量转成原生类型，避免 Pydantic 序列化 500。"""
    if obj is None:
        return None
    # np.float64 是 float 子类，必须先于 isinstance(..., float) 处理
    mod = getattr(type(obj), "__module__", "") or ""
    item = getattr(obj, "item", None)
    if callable(item) and (mod.startswith("numpy") or mod.startswith("pandas")):
        try:
            return _json_safe(item())
        except (ValueError, TypeError):
            pass
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    try:
        if pd.isna(obj):
            return None
    except (ValueError, TypeError):
        pass
    return obj


def _module_to_dict(m: ModuleResult) -> dict:
    return {
        "module_name": m.module_name,
        "score": m.score,
        "level": m.level.value,
        "indicators": [asdict(i) | {"level": i.level.value} for i in m.indicators],
        "warnings": m.warnings,
        "metadata": _json_safe(m.metadata),
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
        # 无历史分位时，用绝对估值给信号（避免 PE 有值却 signal=「—」不参与打分）
        if pe is not None and "PE_TTM" in rel and rel["PE_TTM"].get("signal") in (None, "—"):
            p = float(pe)
            if 0 < p <= 12:
                rel["PE_TTM"]["signal"] = "低估"
            elif p <= 20:
                rel["PE_TTM"]["signal"] = "合理"
            elif p > 35:
                rel["PE_TTM"]["signal"] = "高估"
        if pb is not None and "PB" in rel and rel["PB"].get("signal") in (None, "—"):
            b = float(pb)
            if 0 < b <= 2.0:
                rel["PB"]["signal"] = "低估"
            elif b <= 3.5:
                rel["PB"]["signal"] = "合理"
            elif b > 6:
                rel["PB"]["signal"] = "高估"
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
            result["dcf"] = self._build_dcf(fin_df, market, meta)

        # 无相对估值信号时：仅用「可信」DCF 的安全边际粗估，避免默认 55 / 爆表估值污染分数
        dcf = result.get("dcf") or {}
        if not scores and dcf.get("is_reliable") and dcf.get("intrinsic_value_per_share"):
            price = market.get("price")
            iv = dcf["intrinsic_value_per_share"]
            if price and float(price) > 0:
                mos = (float(iv) - float(price)) / float(price)
                if mos > 0.3:
                    result["composite_valuation_score"] = 82.0
                elif mos > 0:
                    result["composite_valuation_score"] = 70.0
                else:
                    result["composite_valuation_score"] = 50.0

        return result

    @staticmethod
    def _growth_rate(meta: dict) -> float:
        raw = meta.get("profit_yoy")
        if raw is None:
            return 0.05
        growth = float(raw) / 100.0
        if growth >= 1:  # 已是小数却被当成百分数
            return 0.05
        return max(0.02, min(0.12, growth))

    @staticmethod
    def _dynamic_wacc(debt_ratio: float | None) -> float:
        """低负债现金奶牛略降 WACC；高杠杆抬升折现率。"""
        if debt_ratio is None:
            return 0.10
        dr = float(debt_ratio)
        if dr < 30:
            return 0.08
        if dr > 60:
            return 0.12
        return 0.10

    @staticmethod
    def _resolve_shares(fin_df: pd.DataFrame, market: dict, meta: dict) -> tuple[float, str]:
        """优先行情股本 → 市值/股价 → 净利润/EPS → 默认 10 亿（告警）。"""
        raw = market.get("total_shares")
        if raw is not None and float(raw) > 0:
            return float(raw), "market.total_shares"

        price = market.get("price")
        mcap = market.get("market_cap")
        if price and mcap and float(price) > 0 and float(mcap) > 0:
            shares = float(mcap) / float(price)
            if shares > 1e6:  # 排除明显单位错误
                return shares, "market_cap/price"

        if not fin_df.empty and "net_profit" in fin_df.columns and "eps" in fin_df.columns:
            last = fin_df.iloc[-1]
            np_ = last.get("net_profit")
            eps = last.get("eps")
            if pd.notna(np_) and pd.notna(eps) and abs(float(eps)) > 1e-9:
                shares = float(np_) / float(eps)
                if shares > 1e6:
                    return shares, "net_profit/eps"

        eps = meta.get("eps")
        if eps and not fin_df.empty and "net_profit" in fin_df.columns:
            np_ = fin_df["net_profit"].iloc[-1]
            if pd.notna(np_) and abs(float(eps)) > 1e-9:
                shares = float(np_) / float(eps)
                if shares > 1e6:
                    return shares, "meta.eps"

        logger.warning(
            "[%s] 无法获取有效股本，使用默认值 10亿股，估值结果可能失真",
            meta.get("symbol") or market.get("symbol") or "?",
        )
        return 1e9, "default_1e9"

    @staticmethod
    def _base_fcf(fin_df: pd.DataFrame, symbol: str) -> tuple[float | None, str]:
        """真实 FCF = OCF − |CapEx|；为负时降级 OCF×0.3，再不行跳过。"""
        ocf = float(fin_df.get("operating_cashflow", pd.Series([0])).iloc[-1] or 0)
        capex_raw = fin_df.get("capital_expenditure", pd.Series([0])).iloc[-1]
        capex = abs(float(capex_raw or 0))
        real_fcf = ocf - capex if ocf else 0.0

        if real_fcf > 0:
            return real_fcf, "ocf-capex"
        if ocf > 0:
            logger.info("[%s] 真实FCF为负/无效，降级使用 OCF*0.3 作为基期", symbol)
            return ocf * 0.3, "ocf*0.3"
        logger.warning("[%s] 无有效现金流数据，跳过 DCF 估值", symbol)
        return None, "skip"

    def _build_dcf(self, fin_df: pd.DataFrame, market: dict, meta: dict) -> dict[str, Any]:
        symbol = str(meta.get("symbol") or market.get("symbol") or "")
        base_fcf, fcf_src = self._base_fcf(fin_df, symbol)
        if base_fcf is None:
            return {
                "intrinsic_value_per_share": None,
                "note": "现金流数据缺失，跳过 DCF",
                "is_reliable": False,
                "fcf_source": fcf_src,
            }

        shares, shares_src = self._resolve_shares(fin_df, market, meta)
        growth = self._growth_rate(meta)
        wacc = self._dynamic_wacc(meta.get("debt_ratio"))
        dcf = DCFModel(wacc=wacc, terminal_growth=0.02).value(
            base_fcf=base_fcf,
            high_growth_rate=growth,
            transition_growth_rate=max(0.03, growth * 0.5),
            shares_outstanding=shares,
        )
        dcf["fcf_source"] = fcf_src
        dcf["shares_source"] = shares_src

        price = market.get("price")
        iv = dcf.get("intrinsic_value_per_share")
        if price and iv and float(price) > 0:
            dcf["margin_of_safety_pct"] = round((float(iv) - float(price)) / float(price) * 100, 1)
            # 偏离现价 3 倍以上视为不可信（常见原因：股本默认 10 亿）
            if float(iv) > float(price) * 3 or float(iv) < float(price) / 3:
                logger.warning(
                    "[%s] DCF估值异常！计算值: %.2f, 现价: %s, 股本来源: %s, FCF来源: %s",
                    symbol,
                    float(iv),
                    price,
                    shares_src,
                    fcf_src,
                )
                dcf["is_reliable"] = False
                dcf["note"] = (dcf.get("note") or "") + "估值偏离现价过大，已标记不可信"
            else:
                dcf["is_reliable"] = bool(shares_src != "default_1e9")
        else:
            # 无现价对照时：默认股本一律不可信
            dcf["is_reliable"] = bool(iv) and shares_src != "default_1e9"

        return _json_safe(dcf)

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
    return _json_safe(FundamentalEngine().run_full_analysis(symbol, db=db, **kwargs))
