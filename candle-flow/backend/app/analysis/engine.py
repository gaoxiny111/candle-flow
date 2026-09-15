from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any, Callable

import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.base import ModuleResult, score_to_level
from app.analysis.config import (
    CASHFLOW_VETO_MESSAGE,
    CASHFLOW_VETO_THRESHOLD,
    E_GRADE_PENALTY,
    E_GRADE_SCORE,
    MODULE_WEIGHTS,
    RISK_THRESHOLD,
)
from app.analysis.financials import build_financial_dataframe, industry_averages
from app.analysis.models.dcf import DCFModel
from app.analysis.models.relative import RelativeValuation
from app.analysis.models.comps import calculate_comparable_valuation
from app.analysis.modules.cashflow import CashflowAnalyzer
from app.analysis.modules.efficiency import EfficiencyAnalyzer
from app.analysis.modules.growth import GrowthAnalyzer
from app.analysis.modules.industry import IndustryAnalyzer
from app.analysis.modules.profitability import ProfitabilityAnalyzer
from app.analysis.modules.risk import RiskAnalyzer
from app.analysis.modules.solvency import SolvencyAnalyzer
from app.database import SessionLocal
from app.services.valuation import get_valuations
from app.services.major_risk_events import COMPLIANCE_VETO_MESSAGE, detect_major_risk_events
from app.utils.symbol import SymbolError, is_etf_symbol, normalize_symbol

logger = logging.getLogger(__name__)

ANALYSIS_CACHE_TTL_SEC = 6 * 3600
ANALYSIS_BATCH_WORKERS = 4
_analysis_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}
_analysis_cache_lock = threading.Lock()
ProgressCb = Callable[[int, int, str], None]


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
    meta = _json_safe(m.metadata) or {}
    if meta.get("insufficient_sample"):
        return {
            "module_name": m.module_name,
            "score": None,
            "level": "N/A",
            "indicators": [],
            "warnings": m.warnings or ["N/A（样本不足）"],
            "metadata": meta,
        }
    return {
        "module_name": m.module_name,
        "score": m.score,
        "level": m.level.value,
        "indicators": [asdict(i) | {"level": i.level.value} for i in m.indicators],
        "warnings": m.warnings,
        "metadata": meta,
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


RATING_ORDER = ("A", "A-", "B+", "B", "B-", "C", "D", "E")


def downgrade_rating(letter: str, steps: int = 1) -> str:
    """字母评级下调 steps 档（最低 E）。"""
    try:
        idx = RATING_ORDER.index(letter)
    except ValueError:
        return letter
    return RATING_ORDER[min(len(RATING_ORDER) - 1, idx + max(0, steps))]


def _penalized_module_score(score: float) -> float:
    """E 档（<40）维度贡献按系数打折，避免均分掩盖致命短板。"""
    if score < E_GRADE_SCORE:
        return score * E_GRADE_PENALTY
    return score


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
        if is_etf_symbol(sym):
            return skipped_etf_report(sym)

        market: dict[str, Any] = {}
        fin_df = pd.DataFrame()
        meta: dict[str, Any] = {}

        def _load_fin() -> tuple[pd.DataFrame, dict[str, Any]]:
            return build_financial_dataframe(sym)

        def _load_quotes() -> dict[str, Any]:
            try:
                vals = get_valuations([sym], db=None, include_history=True)
                return vals[0] if vals else {}
            except Exception:
                return {}

        def _load_risks() -> dict[str, Any]:
            try:
                return detect_major_risk_events(sym)
            except Exception as e:
                logger.warning("major risk scan failed for %s: %s", sym, e)
                return {"fatal": False, "events": [], "event_count": 0, "message": ""}

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_fin = pool.submit(_load_fin)
            f_mkt = pool.submit(_load_quotes)
            f_risk = pool.submit(_load_risks)
            fin_df, meta = f_fin.result()
            market = f_mkt.result() or {}
            major_risks = f_risk.result() or {}

        meta["symbol"] = sym

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
            "latest_report": meta.get("latest_report"),
            "annual_dates": meta.get("annual_dates") or meta.get("report_dates") or [],
            "balance_sheet": meta.get("balance_sheet") or {},
            "interest_bearing_ratio": meta.get("interest_bearing_ratio"),
            "current_ratio": meta.get("current_ratio"),
            "quick_ratio": meta.get("quick_ratio"),
            "symbol_roe": symbol_roe,
            "industry": meta.get("industry") or "",
            "name": meta.get("name") or market.get("name") or "",
            "audit_opinion": major_risks.get("audit_opinion_hint") or "标准无保留",
            "pledge_ratio": major_risks.get("pledge_ratio") or 0,
            "major_risk_events": major_risks.get("events") or [],
            "industry_avg": industry_averages(meta.get("industry", ""), meta.get("latest_report")),
            **kwargs,
        }

        module_results: dict[str, ModuleResult] = {}
        for name, analyzer in self.analyzers.items():
            module_results[name] = analyzer.analyze(fin_df, **ctx)

        cf = module_results.get("cashflow")
        valuation = self._run_valuation(
            fin_df,
            market,
            meta,
            db=db,
            cashflow_score=cf.score if cf is not None else None,
        )
        val_score = float(valuation.get("composite_valuation_score", 50.0))

        # 对照报告：盈利/成长/偿债/现金流/估值 加权；效率与行业仅展示。
        # E 档维度按 E_GRADE_PENALTY 打折后再加权。
        def _compose(v_score: float) -> float:
            composite = 0.0
            weight_sum = 0.0
            for name, w in self.weights.items():
                if name == "valuation":
                    composite += _penalized_module_score(v_score) * w
                    weight_sum += w
                    continue
                result = module_results.get(name)
                if result is None:
                    continue
                composite += _penalized_module_score(result.score) * w
                weight_sum += w
            if weight_sum > 0:
                composite /= weight_sum
            risk = module_results["risk"]
            if risk.score < RISK_THRESHOLD:
                composite *= risk.score / 100.0
            return round(max(0.0, min(100.0, composite)), 1)

        composite = _compose(val_score)
        letter = rating_label(composite)

        # 价值陷阱：E 档 / ROIC<WACC / ST·退市·连续亏损 → 估值分锁定 ≤28
        val_score, valuation, composite, letter = self._apply_value_trap_veto(
            val_score,
            valuation,
            composite,
            letter,
            module_results,
            name=str(meta.get("name") or market.get("name") or ""),
            compose_fn=_compose,
        )

        all_warnings: list[str] = []
        for r in module_results.values():
            all_warnings.extend(r.warnings)
        if valuation.get("value_trap_veto") and valuation.get("value_trap_message"):
            msg = str(valuation["value_trap_message"])
            if msg not in all_warnings:
                all_warnings.insert(0, msg)

        cashflow_veto = False
        if cf is not None and cf.score < CASHFLOW_VETO_THRESHOLD:
            cashflow_veto = True
            letter = downgrade_rating(letter, 1)
            if CASHFLOW_VETO_MESSAGE not in all_warnings:
                all_warnings.insert(0, CASHFLOW_VETO_MESSAGE)

        # 公告关键词重大风险：顶部红灯，强制 E，估值陷阱否决，财务分仅作参考
        compliance_veto = bool(major_risks.get("fatal"))
        if compliance_veto:
            letter = "E"
            composite = min(composite, 25.0)
            if COMPLIANCE_VETO_MESSAGE not in all_warnings:
                all_warnings.insert(0, COMPLIANCE_VETO_MESSAGE)
            for ev in major_risks.get("events") or []:
                line = f"【{ev.get('label')}】{ev.get('notice_date') or ''} {ev.get('title') or ''}".strip()
                if line and line not in all_warnings:
                    all_warnings.insert(1, line)
            valuation = dict(valuation)
            if val_score > 28.0:
                valuation["valuation_score_base"] = round(val_score, 1)
                valuation["composite_valuation_score"] = 28.0
                val_score = 28.0
            valuation["value_trap_veto"] = True
            valuation["value_trap_message"] = COMPLIANCE_VETO_MESSAGE
            base_r = valuation.get("valuation_rationale") or ""
            valuation["valuation_rationale"] = (
                (base_r + "；" if base_r else "")
                + "合规/生存风险一票否决，财务与相对估值仅作参考"
            )
            pr = major_risks.get("pledge_ratio")
            if pr is not None and module_results.get("risk") is not None:
                # 同步质押率到风险模块元数据（展示用）
                risk_dict = module_results["risk"]
                risk_dict.metadata["pledge_ratio"] = pr
                risk_dict.metadata["compliance_veto"] = True

        peer_sample_ok = bool(valuation.get("peer_sample_ok"))
        ind = module_results.get("industry")
        if ind is not None and ind.metadata.get("insufficient_sample"):
            peer_sample_ok = False
        show_pe_pct = market.get("pe_percentile") if peer_sample_ok else None
        show_pb_pct = market.get("pb_percentile") if peer_sample_ok else None

        return {
            "symbol": sym,
            "name": meta.get("name") or market.get("name") or "",
            "industry": meta.get("industry") or "",
            "report_dates": meta.get("report_dates") or [],
            "latest_report": meta.get("latest_report"),
            "composite_score": composite,
            "final_rating": letter,
            "final_rating_letter": letter,
            "cashflow_veto": cashflow_veto,
            "compliance_veto": compliance_veto,
            "major_risks": _json_safe(major_risks),
            "peer_sample_ok": peer_sample_ok,
            "modules": {k: _module_to_dict(v) for k, v in module_results.items()},
            "valuation": valuation,
            "market": {
                "price": market.get("price"),
                "pe_ttm": market.get("pe_ttm"),
                "pb": market.get("pb"),
                "pe_percentile": show_pe_pct,
                "pb_percentile": show_pb_pct,
                "pe_percentile_na": None if peer_sample_ok else "N/A（样本不足）",
                "market_cap": market.get("market_cap"),
                "dividend_yield": market.get("dividend_yield"),
            },
            "warnings": all_warnings,
            "summary": self._generate_summary(
                composite,
                letter,
                module_results,
                all_warnings,
                val_score,
                cashflow_veto=cashflow_veto,
                compliance_veto=compliance_veto,
            ),
        }

    def _run_valuation(
        self,
        fin_df: pd.DataFrame,
        market: dict,
        meta: dict,
        db: Session | None = None,
        cashflow_score: float | None = None,
    ) -> dict:
        result: dict[str, Any] = {}
        pe = market.get("pe_ttm")
        pb = market.get("pb")
        pe_pct = market.get("pe_percentile")
        pb_pct = market.get("pb_percentile")
        div = market.get("dividend_yield")

        growth_rate, growth_label = self._growth_for_peg(fin_df, meta)
        current = {
            "PE_TTM": pe,
            "PB": pb,
            "PS": None,
            "profit_growth_rate": growth_rate,
            "profit_growth_label": growth_label,
        }

        comps: dict[str, Any] = {}
        industry_frame = None
        try:
            comps = calculate_comparable_valuation(
                meta.get("symbol") or market.get("symbol") or "",
                market=market,
                meta=meta,
                fin_df=fin_df,
                db=db,
            )
            result["comps"] = comps
            # 喂给相对估值：行业 PE/PB 中位数（用可比公司均值近似）；样本不足则不喂
            if not comps.get("insufficient_sample") and (
                comps.get("avg_pe") is not None or comps.get("avg_pb") is not None
            ):
                row: dict[str, float] = {}
                if comps.get("avg_pe") is not None:
                    row["PE_TTM"] = float(comps["avg_pe"])
                if comps.get("avg_pb") is not None:
                    row["PB"] = float(comps["avg_pb"])
                industry_frame = pd.DataFrame([row])
        except Exception as e:
            logger.warning("comparable valuation failed: %s", e)
            result["comps"] = {
                "stock_code": meta.get("symbol") or "",
                "comparables": [],
                "avg_pe": None,
                "avg_pb": None,
                "valuation_range": {},
                "warning": f"可比估值计算失败: {e}",
                "peer_count": 0,
                "insufficient_sample": True,
            }
            comps = result["comps"]

        sample_ok = not bool(comps.get("insufficient_sample")) and int(comps.get("peer_count") or 0) >= 5
        result["peer_sample_ok"] = sample_ok

        rv = RelativeValuation()
        rel = rv.analyze(current, history=None, industry=industry_frame)
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
        # 图四：同业样本不足时 PE/PB 分位显示 N/A，不展示假精确分位
        if sample_ok:
            if pe_pct is not None and "PE_TTM" in rel:
                rel["PE_TTM"]["percentile_5y"] = pe_pct
                if pe is not None and 0 < float(pe) <= 12:
                    rel["PE_TTM"]["signal"] = "低估"
                elif pe_pct is not None and float(pe_pct) >= 75 and pe is not None and float(pe) > 20:
                    rel["PE_TTM"]["signal"] = "高估"
            if pb_pct is not None and "PB" in rel:
                rel["PB"]["percentile_5y"] = pb_pct
                if pb is not None and 0 < float(pb) <= 1.5:
                    rel["PB"]["signal"] = "低估"
        else:
            if "PE_TTM" in rel:
                rel["PE_TTM"]["percentile_5y"] = None
                rel["PE_TTM"]["percentile_na"] = "N/A（样本不足）"
            if "PB" in rel:
                rel["PB"]["percentile_5y"] = None
                rel["PB"]["percentile_na"] = "N/A（样本不足）"
        if div is not None:
            dy = float(div)
            rel["股息率"] = {
                "current": round(dy, 2),
                "signal": "低估" if dy >= 5 else ("合理" if dy >= 2 else "高估"),
                "percentile_5y": None,
            }
        # 可比公司信号优先补强相对估值（仅样本充足时）
        comps_signal = (comps or {}).get("signal") if sample_ok else None
        if comps_signal and pe is not None and "PE_TTM" in rel:
            if rel["PE_TTM"].get("signal") in (None, "—"):
                rel["PE_TTM"]["signal"] = comps_signal
        result["relative"] = rel

        scores: list[float] = []
        breakdown: list[dict[str, Any]] = []

        def _add_points(factor: str, points: float, detail: str) -> None:
            scores.append(points)
            breakdown.append({"factor": factor, "points": round(points, 1), "detail": detail})

        for key, item in rel.items():
            sig = item.get("signal")
            if sig == "低估":
                _add_points(key, 85, f"信号={sig}")
            elif sig == "合理":
                _add_points(key, 65, f"信号={sig}")
            elif sig in ("高估", "偏贵"):
                _add_points(key, 35, f"信号={sig}")
        # 绝对 PE 加分
        if pe is not None and 0 < float(pe) < 10:
            _add_points("绝对PE", 88, f"PE={float(pe):.1f}<10")
        elif pe is not None and 0 < float(pe) < 15:
            _add_points("绝对PE", 75, f"PE={float(pe):.1f}<15")
        if div is not None and float(div) >= 6:
            _add_points("高股息", 90, f"股息率={float(div):.1f}%")
        if comps_signal == "低估":
            _add_points("可比公司", 82, "相对可比低估")
        elif comps_signal == "高估":
            _add_points("可比公司", 40, "相对可比高估")

        base_score = sum(scores) / len(scores) if scores else 55.0
        rationale_parts: list[str] = []
        if breakdown:
            parts = [f"{b['factor']}{b['points']:.0f}" for b in breakdown]
            rationale_parts.append("构成：" + "、".join(parts) + f" → 均值 {base_score:.0f}")
        else:
            rationale_parts.append("暂无相对估值信号，采用默认中性分")

        haircut = 0.0
        if cashflow_score is not None and cashflow_score < 45:
            cheap_looking = any(
                (rel.get(k) or {}).get("signal") == "低估" for k in ("PE_TTM", "PB", "PEG")
            ) or base_score >= 70
            if cheap_looking or base_score >= 60:
                haircut = min(22.0, max(8.0, (45.0 - float(cashflow_score)) * 0.55))
                rationale_parts.append(
                    f"现金流质量仅 {cashflow_score:.0f} 分，估值合理性折减 {haircut:.0f} 分"
                    f"（账面估值偏便宜但造血不足）"
                )

        final_score = max(20.0, base_score - haircut)
        result["composite_valuation_score"] = final_score
        result["valuation_score_breakdown"] = breakdown
        result["valuation_score_base"] = round(base_score, 1)
        result["valuation_score_haircut"] = round(haircut, 1)
        result["valuation_rationale"] = "；".join(rationale_parts)

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
                result["valuation_rationale"] = (
                    f"无相对估值信号，按 DCF 安全边际 {mos * 100:.0f}% 给分 "
                    f"{result['composite_valuation_score']:.0f}"
                )

        return result

    @staticmethod
    def _apply_value_trap_veto(
        val_score: float,
        valuation: dict[str, Any],
        composite: float,
        letter: str,
        modules: dict[str, ModuleResult],
        *,
        name: str,
        compose_fn,
        cap: float = 28.0,
    ) -> tuple[float, dict[str, Any], float, str]:
        """
        价值陷阱一票否决：综合已是 E、ROIC<WACC、ST/退市/连续亏损时，
        相对估值（低 PB 等）不得给高分，锁定 ≤cap 并重算综合分。
        """
        reasons: list[str] = []
        prof = modules.get("profitability")
        risk = modules.get("risk")
        cf = modules.get("cashflow")

        if letter == "E":
            reasons.append("综合评级已为 E")
        if prof is not None and prof.metadata.get("roic_below_wacc"):
            reasons.append("ROIC低于WACC")
        if risk is not None and risk.metadata.get("delist_risk"):
            reasons.append("存在退市/ST风险标识")
        if risk is not None and int(risk.metadata.get("consecutive_loss_years") or 0) >= 2:
            reasons.append(f"净利润连续{int(risk.metadata['consecutive_loss_years'])}年亏损")
        if cf is not None and cf.metadata.get("paper_wealth"):
            reasons.append("亏损现金背离（纸面富贵）")
        name_u = (name or "").upper()
        if "ST" in name_u or "退" in (name or ""):
            if "存在退市/ST风险标识" not in reasons:
                reasons.append("证券简称含ST/退")

        if not reasons:
            return val_score, valuation, composite, letter

        if val_score <= cap:
            valuation = dict(valuation)
            valuation["value_trap_veto"] = True
            valuation["value_trap_message"] = (
                "基本面恶化，低估值为陷阱，不适用相对估值（"
                + "；".join(reasons)
                + "）"
            )
            # 仍写清 rationale，即使分数已低
            base_r = valuation.get("valuation_rationale") or ""
            valuation["valuation_rationale"] = (
                (base_r + "；" if base_r else "")
                + f"价值陷阱锁定 ≤{cap:.0f}："
                + "、".join(reasons)
            )
            return val_score, valuation, composite, letter

        valuation = dict(valuation)
        valuation["value_trap_veto"] = True
        valuation["valuation_score_base"] = round(val_score, 1)
        valuation["valuation_score_haircut"] = round(max(0.0, val_score - cap), 1)
        valuation["composite_valuation_score"] = cap
        msg = "基本面恶化，低估值为陷阱，不适用相对估值（" + "；".join(reasons) + "）"
        valuation["value_trap_message"] = msg
        base_r = valuation.get("valuation_rationale") or ""
        valuation["valuation_rationale"] = (
            (base_r + "；" if base_r else "")
            + f"价值陷阱一票否决，估值合理性由 {val_score:.0f} 锁定为 {cap:.0f}："
            + "、".join(reasons)
        )
        new_score = float(cap)
        new_composite = compose_fn(new_score)
        new_letter = rating_label(new_composite)
        return new_score, valuation, new_composite, new_letter

    @staticmethod
    def _growth_for_peg(fin_df: pd.DataFrame, meta: dict) -> tuple[float | None, str]:
        """PEG 优先用 3 年净利 CAGR；不足则回退最新净利同比。"""
        if fin_df is not None and not fin_df.empty and "net_profit" in fin_df.columns:
            clean = fin_df["net_profit"].dropna()
            if len(clean) >= 4:
                start, end = float(clean.iloc[-4]), float(clean.iloc[-1])
                if start > 0 and end > 0:
                    cagr = ((end / start) ** (1 / 3) - 1) * 100
                    if cagr > 0:
                        return round(cagr, 2), "3年净利CAGR"
            if len(clean) >= 2:
                start, end = float(clean.iloc[0]), float(clean.iloc[-1])
                span = max(len(clean) - 1, 1)
                if start > 0 and end > 0:
                    cagr = ((end / start) ** (1 / span) - 1) * 100
                    if cagr > 0:
                        return round(cagr, 2), f"{span}年净利CAGR"
        yoy = meta.get("profit_yoy")
        if yoy is not None and float(yoy) > 0:
            return round(float(yoy), 2), "最新净利同比"
        return None, "净利增速"
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
        cashflow_veto: bool = False,
        compliance_veto: bool = False,
    ) -> str:
        lines = [f"综合评分 {score:.1f} 分，评级 {letter}。"]
        if compliance_veto:
            lines.append(f"  ※ {COMPLIANCE_VETO_MESSAGE}")
        if cashflow_veto:
            lines.append(f"  ※ {CASHFLOW_VETO_MESSAGE}")
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


def skipped_etf_report(symbol: str) -> dict[str, Any]:
    """ETF 不跑个股财报模型：股息率 / 综合分 / 评级留空。"""
    return {
        "symbol": symbol,
        "name": "",
        "industry": "",
        "report_dates": [],
        "composite_score": None,
        "final_rating": None,
        "final_rating_letter": None,
        "cashflow_veto": False,
        "compliance_veto": False,
        "major_risks": {"fatal": False, "events": [], "event_count": 0},
        "modules": {},
        "valuation": {},
        "market": {
            "price": None,
            "pe_ttm": None,
            "pb": None,
            "pe_percentile": None,
            "pb_percentile": None,
            "market_cap": None,
            "dividend_yield": None,
        },
        "warnings": [],
        "summary": "ETF 不适用个股基本面评分，已跳过股息率、综合分与评级。",
        "skipped": True,
        "skip_reason": "etf",
    }


def analyze_symbol_full(
    db: Session,
    symbol: str,
    *,
    use_cache: bool = True,
    **kwargs,
) -> dict[str, Any]:
    sym = normalize_symbol(symbol)
    if use_cache:
        with _analysis_cache_lock:
            hit = _analysis_cache.get(sym)
            if hit and time.time() - hit[0] < ANALYSIS_CACHE_TTL_SEC:
                return _json_safe(hit[2])

    report = _json_safe(FundamentalEngine().run_full_analysis(sym, db=db, **kwargs))
    fingerprint = ",".join(str(x) for x in (report.get("report_dates") or [])[-3:])
    if use_cache and not report.get("skipped"):
        with _analysis_cache_lock:
            _analysis_cache[sym] = (time.time(), fingerprint, report)
    return report


def analyze_symbols_batch(
    symbols: list[str],
    *,
    use_cache: bool = True,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """批量深度基本面分析：共享财报预热 + 进程内缓存 + 有限并发。"""
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        try:
            sym = normalize_symbol(str(raw).strip())
        except SymbolError:
            continue
        key = sym.upper()
        if key in seen:
            continue
        seen.add(key)
        wanted.append(sym)
        if len(wanted) >= 50:
            break

    if not wanted:
        return {"items": [], "count": 0, "cached": 0}

    # 预热业绩快照，避免每票冷启动拉 yjbb
    try:
        from app.services.fundamental_screen import resolve_latest_report_frame, resolve_report_frames

        resolve_latest_report_frame()
        resolve_report_frames(5)
    except Exception as exc:
        logger.debug("analysis batch warmup skipped: %s", exc)

    items: list[dict[str, Any] | None] = [None] * len(wanted)
    cached = 0
    done = 0
    total = len(wanted)
    if progress:
        progress(0, total, "analysis")

    def _one(idx: int, sym: str) -> tuple[int, dict[str, Any], bool]:
        from_cache = False
        if use_cache:
            with _analysis_cache_lock:
                hit = _analysis_cache.get(sym)
                if hit and time.time() - hit[0] < ANALYSIS_CACHE_TTL_SEC:
                    return idx, hit[2], True
        db = SessionLocal()
        try:
            report = analyze_symbol_full(db, sym, use_cache=use_cache)
            return idx, report, from_cache
        finally:
            db.close()

    workers = min(ANALYSIS_BATCH_WORKERS, max(1, len(wanted)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, i, sym) for i, sym in enumerate(wanted)]
        for fut in as_completed(futures):
            idx, report, from_cache = fut.result()
            items[idx] = report
            if from_cache:
                cached += 1
            done += 1
            if progress:
                progress(done, total, "analysis")

    return {
        "items": [x for x in items if x is not None],
        "count": sum(1 for x in items if x is not None),
        "cached": cached,
        "requested": len(wanted),
    }
