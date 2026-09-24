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
    RISK_MAX_PENALTY,
    RISK_THRESHOLD,
)
from app.analysis.dividend_profile import (
    CN_10Y_BOND_YIELD_PCT,
    DIVIDEND_ASSET_WACC_PCT,
    classify_dividend_asset,
    dividend_spread_signal,
    dividend_valuation_note,
)
from app.analysis.growth_quality import HIGH_GROWTH_WACC_PCT, classify_high_growth_quality
from app.analysis.growth_profile import classify_growth_stock
from app.analysis.financials import build_financial_dataframe, industry_averages
from app.analysis.models.dcf import DCFModel
from app.analysis.models.ddm import DDMModel
from app.analysis.models.relative import RelativeValuation
from app.analysis.models.comps import calculate_comparable_valuation
from app.analysis.modules.profitability import _estimate_wacc_pct
from app.analysis.config.company_profiles import business_model_of
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

# ── 周期陷阱（低 PE 幻觉）识别 ─────────────────────────────────────
# 强周期行业在景气高点利润暴增 → trailing PE 极低（看起来「便宜」），
# 但紧接着就是业绩下滑 + 杀估值，把低 PE 当成「低估」加分是逻辑反向。
# 判别不用 fuzz 关键词，而用三件事同时成立：
#   ① PE 历史分位 ≤ 30%（看起来便宜）
#   ② PB 历史分位 ≥ 70%（资产端并不便宜 —— 这才是周期高点的真信号：
#      市场已经按高点 ROE 抬高了 PB，便宜只体现在 PE 上）
#   ③ ROE ≥ 15%（盈利确实处于高位）
# PB 分位是这里的关键辨伪项：真周期底部（万华化学 PE分位 27/PB分位 7、
# 璞泰来 PE分位 6/PB分位 16）PE 与 PB 都在低分位，只有「PE 低 + PB 高」
# 才是盈利驱动型的假便宜。
# 只对 STRONG_CYCLICAL_INDUSTRIES 生效（**精确等于**行业名，不做子串模糊匹配），
# 折扣与现金流折减取 max 而非叠加。
CYCLE_TRAP_PE_PCT_MAX = 30.0
CYCLE_TRAP_PB_PCT_MIN = 70.0
CYCLE_TRAP_ROE_MIN = 15.0
CYCLE_TRAP_HAIRCUT = 12.0
# 估值合理性折减的全局上限（现金流折减与周期陷阱折减共用，禁止叠加）
VALUATION_HAIRCUT_MAX = 22.0

# 强周期行业名录。**取值必须精确等于快照 payload 里的 `industry` 字符串**
# （2026-09-21 线上盘点的 129 个行业名，共 5254 只全覆盖）。
# 刻意用精确匹配而非 `_cycle_kw` 那样的子串判断：子串表对真实行业名
# （特钢Ⅱ / 化学原料 / 化学制品 / 工业金属）几乎全不命中 —— 实测原
# `_cycle_kw` 只覆盖 86 只（1.6%），而 `特钢Ⅱ` 里根本没有「钢铁」二字。
# 本名单只收「产品价格周期主导盈利」的行业，不含半导体/电池等
# 兼具成长属性的板块（那类有独立的成长股估值框架）。
STRONG_CYCLICAL_INDUSTRIES = frozenset({
    # 钢铁
    "普钢", "特钢Ⅱ", "冶钢原料",
    # 有色 / 金属
    "工业金属", "小金属", "能源金属", "贵金属", "金属新材料",
    # 煤炭
    "煤炭开采", "焦炭Ⅱ",
    # 化工 / 建材
    "化学原料", "化学制品", "化学纤维", "农化制品", "塑料", "橡胶",
    "玻璃玻纤", "水泥", "非金属材料Ⅱ",
    # 石油天然气
    "炼化及贸易", "油服工程", "油气开采Ⅱ",
    # 交运（运价 / 油价周期）
    "航运港口", "航空机场",
    # 其他强周期
    "房地产开发", "造纸", "养殖业", "纺织制造",
})


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


# 成长股估值软化：绝对估值越高，允许的历史分位上限越低（阶梯）。
# 软化本意是「trailing PE 因周期底部/盈利拐点暂时失真」，但该敞口曾被滥用：
# 只要 PE > 80 且分位 < 90 就把 signal 洗成「合理」，于是 603099 长白山
# （PE 80.6 / 分位 86.1）这类「绝对 + 相对双高」的票也被判「合理」。
# 改为双条件交叉：绝对估值档位决定分位门槛，双高即判「偏高」。
SOFTEN_MAX_PCTL = 90.0          # 历史常量，保留兼容既有引用
SOFTEN_PE_HIGH = 80.0           # 进入软化讨论的绝对 PE 下限
SOFTEN_PE_EXTREME = 150.0       # 绝对 PE 极端档
SOFTEN_PCTL_PE_HIGH = 80.0      # PE 80~150 档允许的分位上限
SOFTEN_PCTL_PE_EXTREME = 70.0   # PE >150 档允许的分位上限
SOFTEN_PCTL_PB = 80.0           # PB >8 档允许的分位上限


def _pick_percentile(entry: dict[str, Any]) -> float | None:
    """从相对估值条目里取历史分位（兼容多种字段名）。"""
    for k in ("percentile_5y", "percentile", "percentile_10y"):
        v = entry.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _apply_valuation_soften_ladder(
    rel: dict[str, Any],
    *,
    pe: float | None,
    pb: float | None,
    is_growth: bool,
    is_div: bool,
) -> None:
    """就地改写 ``rel`` 中 PE_TTM / PB 的 signal 与 note（阶梯软化）。

    仅对成长股（且非红利资产）生效；非成长股不动，避免退化成
    「PE 高 → 算成长 → 免惩罚」的自证循环。
    """
    if not is_growth or is_div:
        return
    for mk in ("PE_TTM", "PB"):
        if mk not in rel or not isinstance(rel[mk], dict):
            continue
        entry = rel[mk]
        pctl = _pick_percentile(entry)
        if mk == "PE_TTM" and pe is not None and float(pe) > SOFTEN_PE_HIGH:
            limit = (
                SOFTEN_PCTL_PE_EXTREME
                if float(pe) > SOFTEN_PE_EXTREME
                else SOFTEN_PCTL_PE_HIGH
            )
            if pctl is not None and pctl >= limit:
                entry["signal"] = "偏高"
                entry["note"] = (
                    f"PE {float(pe):.0f}x 且历史分位 {pctl:.0f}%（≥{limit:.0f}%）"
                    "已处双高区间：成长预期已被充分定价，不再适用成长股软化口径"
                )
            else:
                entry["signal"] = "合理"
                entry["growth_percentile_softened"] = True
                entry["note"] = (
                    f"PE {float(pe):.0f}x 为周期底部/高增长特征，"
                    "trailing PE 失真，参考 PS/营收增速/毛利率"
                    + (f"（历史分位 {pctl:.0f}% < {limit:.0f}%）" if pctl is not None else "")
                )
        elif mk == "PB" and pb is not None and float(pb) > 8:
            if pctl is not None and pctl >= SOFTEN_PCTL_PB:
                entry["signal"] = "偏高"
                entry["note"] = (
                    f"PB {float(pb):.1f}x 且历史分位 {pctl:.0f}%"
                    f"（≥{SOFTEN_PCTL_PB:.0f}%）已处双高区间：不再适用成长股软化口径"
                )
            else:
                entry["signal"] = "合理"
                entry["growth_percentile_softened"] = True
                entry["note"] = (
                    f"PB {float(pb):.1f}x 为成长股/周期底部特征，"
                    "参考技术壁垒（毛利率）与赛道景气度"
                    + (f"（历史分位 {pctl:.0f}% < {SOFTEN_PCTL_PB:.0f}%）" if pctl is not None else "")
                )


def cycle_trap_hit(
    pe_pct: float | None,
    pb_pct: float | None,
    roe: float | None,
) -> bool:
    """周期景气高点的「低 PE 幻觉」判据。

    三件事同时成立才算：
      ① PE 历史分位 ≤ CYCLE_TRAP_PE_PCT_MAX（看起来便宜）
      ② PB 历史分位 ≥ CYCLE_TRAP_PB_PCT_MIN（资产端并不便宜）
      ③ ROE ≥ CYCLE_TRAP_ROE_MIN（盈利确实在高位）

    ②是关键辨伪项：真周期底部（盈利差、ROE 低、市场给低 PB）时 PE 与 PB
    都在低分位；只有「PE 低分位 + PB 高分位 + 高 ROE」才说明便宜来自盈利
    高点而非资产便宜 —— 这正是后续要被杀估值的位置。
    任一输入缺失即返回 False（不猜、不给缺失数据加权）。
    """
    if pe_pct is None or pb_pct is None or roe is None:
        return False
    return (
        float(pe_pct) <= CYCLE_TRAP_PE_PCT_MAX
        and float(pb_pct) >= CYCLE_TRAP_PB_PCT_MIN
        and float(roe) >= CYCLE_TRAP_ROE_MIN
    )


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

        # 股息率单一来源（铁律 9：同一判定不得两处各算）：东财估值接口失败时
        # 用真实分红/股价回退，必须在 ctx 构建前完成。盈利/风险/现金流模块经
        # ctx 拿 dividend_yield，估值模块经 market 拿；若回退只写在 _run_valuation
        # 里，东财失败时会出现三方矛盾——盈利模块判「非红利」（WACC 7%、
        # ROIC<WACC 成立）+ 估值模块判「红利」（四因子框架高分）+ 长线信号
        # 「红利龙头底仓」，并连带触发价值陷阱一票否决锁 28
        # （海螺水泥 600585 2026-09-20 线上实测事故：同代码本地 73.4 B / 线上 60.7 C）。
        if market.get("dividend_yield") is None:
            div_meta = meta.get("dividend") or {}
            d0 = div_meta.get("d0")
            price = market.get("price")
            if d0 and price and float(price) > 0:
                market["dividend_yield"] = round(float(d0) / float(price) * 100, 2)
                logger.info(
                    "[%s] 股息率回退计算: D0=%.2f/股价=%.2f=%.2f%%",
                    sym, float(d0), float(price), float(market["dividend_yield"]),
                )

        symbol_roe = meta.get("latest_roe")
        if symbol_roe is None and not fin_df.empty and "roe" in fin_df.columns:
            symbol_roe = float(fin_df["roe"].iloc[-1])

        # 微利稀释闸门判据（cashflow.py MICRO_PROFIT_*）：最新报告期净利率百分数。
        # 现金流模块在模块循环内运行，无法读取「盈利模块算完后」才知道的值，
        # 因此这里直接从 fin_df 取（与盈利模块 dupont.net_margin 同源同口径：
        # net_profit / revenue 的最新一期），保证两处一致且不依赖模块顺序。
        net_margin_pct: float | None = None
        if (
            not fin_df.empty
            and "net_profit" in fin_df.columns
            and "revenue" in fin_df.columns
        ):
            _rev_latest = fin_df["revenue"].dropna()
            _np_latest = fin_df["net_profit"].dropna()
            if len(_rev_latest) and len(_np_latest) and float(_rev_latest.iloc[-1]) != 0:
                net_margin_pct = (
                    float(_np_latest.iloc[-1]) / float(_rev_latest.iloc[-1]) * 100.0
                )

        ctx = {
            "debt_ratio": meta.get("debt_ratio"),
            "debt_ratio_estimated": meta.get("debt_ratio_estimated", False),
            "debt_ratio_period": meta.get("debt_ratio_period"),
            "debt_ratio_scope": meta.get("debt_ratio_scope"),
            "revenue_yoy": meta.get("revenue_yoy"),
            "profit_yoy": meta.get("profit_yoy"),
            "profit_yoy_forward": meta.get("profit_yoy_forward"),
            "profit_yoy_extreme": bool(meta.get("profit_yoy_extreme")),
            "ocf_per_share": meta.get("ocf_per_share"),
            # 微利稀释闸门判据（cashflow.py MICRO_PROFIT_*）：最新报告期净利率百分数。
            "net_margin": net_margin_pct,
            "latest_roe": meta.get("latest_roe"),
            "latest_report": meta.get("latest_report"),
            "annual_dates": meta.get("annual_dates") or meta.get("report_dates") or [],
            "balance_sheet": meta.get("balance_sheet") or {},
            "interest_bearing_ratio": meta.get("interest_bearing_ratio"),
            "interest_bearing_debt": meta.get("interest_bearing_debt"),
            "short_term_borrowings": meta.get("short_term_borrowings"),
            "current_ratio": meta.get("current_ratio"),
            "quick_ratio": meta.get("quick_ratio"),
            "symbol_roe": symbol_roe,
            # 各模块依赖 symbol 做公司画像/商业模式/同业匹配，此前漏传导致
            # 只能靠「名称」命中画像（商络电子有画像所以正常），
            # 而无画像的同类公司（深圳华强等）一律识别不到分销模式。
            "symbol": sym,
            "industry": meta.get("industry") or "",
            "name": meta.get("name") or market.get("name") or "",
            "audit_opinion": major_risks.get("audit_opinion_hint") or "标准无保留",
            "pledge_ratio": major_risks.get("pledge_ratio") or 0,
            "pledge_invalid": bool(major_risks.get("pledge_invalid")),
            "major_risk_events": major_risks.get("events") or [],
            "survival_released_labels": major_risks.get("released_labels") or [],
            "survival_released_count": int(major_risks.get("survival_released_count") or 0),
            "observe_risk_events": major_risks.get("observe_events") or [],
            "risk_released": bool(major_risks.get("risk_released")),
            "risk_lock_commitment": bool(major_risks.get("risk_lock_commitment")),
            "risk_release_type": major_risks.get("risk_release_type") or "",
            # 减持窗口期倒计时 + 大宗交易折价率（供 risk 模块计价、前端展示）
            "reduce_window": major_risks.get("reduce_window"),
            "reduce_remaining_days": major_risks.get("reduce_remaining_days"),
            "in_reduce_window": bool(major_risks.get("in_reduce_window")),
            "latest_block_trade": major_risks.get("latest_block_trade"),
            "block_trades": major_risks.get("block_trades") or [],
            "latest_cash_ratio": meta.get("latest_cash_ratio"),
            "latest_cash_ratio_source": meta.get("latest_cash_ratio_source"),
            "eps": meta.get("eps"),
            "deducted_net_profit": meta.get("deducted_net_profit"),
            "parent_net_profit": meta.get("parent_net_profit"),
            "deducted_yoy_pct": meta.get("deducted_yoy_pct"),
            # 上年同期归母净利：growth 模块「扭亏型伪成长」检测的同比分母。
            # 同期对同期（中报对去年中报），由 financials 统一产出，模块侧只消费。
            "last_year_net_profit": meta.get("last_year_net_profit"),
            "last_year_deducted_net_profit": meta.get("last_year_deducted_net_profit"),
            "single_quarter": meta.get("single_quarter"),
            "ar_metrics": meta.get("ar_metrics"),
            # 营运效率跟踪（应收周转天数变化率 / 存货营收比），供 efficiency
            # 模块做利润侵蚀预警。口径由 financials._ops_efficiency_from_sina
            # 统一产出（同期对同期），模块侧只消费不重算。
            "ops_efficiency": meta.get("ops_efficiency"),
            "payout_ratio_pct": meta.get("payout_ratio_pct"),
            "dividend_info": meta.get("dividend"),
            "interim_balance_sheet": meta.get("interim_balance_sheet"),
            "dividend_yield": market.get("dividend_yield"),
            "pe_ttm": market.get("pe_ttm"),
            # PB 供「周期底部反转」判定用（growth_profile.CYCLE_TROUGH_PB_MAX）：
            # 周期底部 E→0 时 PE 失效，必须用 PB 判断市场是否已定价衰退。
            # 传给所有模块是为了让各处的 classify_growth_stock 得到同一结论
            # （同一判定不在两处各算、不产生两个 is_growth）。
            "pb": market.get("pb"),
            "latest_gross_margin": meta.get("latest_gross_margin"),
            "industry_avg": industry_averages(meta.get("industry", ""), meta.get("latest_report")),
            **kwargs,
        }

        module_results: dict[str, ModuleResult] = {}
        for name, analyzer in self.analyzers.items():
            module_results[name] = analyzer.analyze(fin_df, **ctx)

        cf = module_results.get("cashflow")
        # 成长模块已算出规范的 3 年净利 CAGR 与 V 型拐点标记，预先写回 meta，
        # 供估值模块复用。否则估值模块自行用 pct_change(3) 粗算（把「3 年累计
        # 变动」当 CAGR，且基期为负时得出无意义的负值），会与引擎后文
        # classify_growth_stock 的结论互相矛盾——表现为估值 rationale 声称
        # 「以 PEG/PS 为主」但 PEG 从未进入构成（深圳华强即如此）。
        _gm = module_results.get("growth")
        if _gm is not None:
            meta["_growth_cagr_3y"] = _gm.metadata.get("profit_cagr_3y")
            meta["_growth_v_shape"] = bool(_gm.metadata.get("v_shape"))
            meta["_growth_marginal_recovery"] = bool(_gm.metadata.get("marginal_recovery"))
        # 净利率已在算 ctx 前由 fin_df 直接算出并透传（见上方 net_margin_pct），
        # 此处仅把百分数口径写回现金流 metadata 留痕，便于报告自证闸门判据。
        if cf is not None and net_margin_pct is not None:
            cf.metadata["net_margin_pct"] = net_margin_pct
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
                # 风险越低扣得越多，但限幅 RISK_MAX_PENALTY（默认 -25%）。
                # 原实现是无上限的 composite *= risk/100：风险模块的扣分项
                # （应收恶化/利润含金量低/存贷双高）在 cashflow、solvency 模块
                # 已各自扣过一次，再做整体乘法等于三重计数 + 复利放大。
                risk_gap = (RISK_THRESHOLD - risk.score) / RISK_THRESHOLD
                composite *= 1.0 - RISK_MAX_PENALTY * max(0.0, min(1.0, risk_gap))
            return round(max(0.0, min(100.0, composite)), 1)

        composite = _compose(val_score)
        letter = rating_label(composite)

        # ── 高成长优质股保护：盈利+成长双强时，现金流低分不致命 ──
        prof_s = module_results.get("profitability")
        growth_s = module_results.get("growth")
        cf_s = module_results.get("cashflow")
        if (
            prof_s is not None and prof_s.score >= 85
            and growth_s is not None and growth_s.score >= 85
            and cf_s is not None and cf_s.score <= 60
            and composite < 70
        ):
            composite = 70.0
            letter = rating_label(composite)

        # ── 成长股/周期底部反转识别 ──────────────────────────
        growth_mod = module_results.get("growth")
        prof_mod = module_results.get("profitability")
        # 先尝试从已算好的 valuation 中拿 is_dividend_asset（如果有）
        _is_div_hint = bool((valuation or {}).get("is_dividend_asset")) if valuation else False
        gs = classify_growth_stock(
            symbol=sym,
            gross_margin_pct=(
                growth_quality.gross_margin_pct
                if (growth_quality := (valuation.get("growth_quality") or {}))
                else None
            ),
            revenue_yoy=meta.get("revenue_yoy"),
            profit_yoy=meta.get("profit_yoy"),
            profit_cagr_3y=float(growth_mod.metadata.get("profit_cagr_3y") or -99)
            if growth_mod
            else None,
            pe_ttm=market.get("pe_ttm"),
            pb=market.get("pb"),
            is_v_shape=bool(growth_mod.metadata.get("v_shape")) if growth_mod else False,
            is_marginal_recovery=bool(growth_mod.metadata.get("marginal_recovery"))
            if growth_mod
            else False,
            is_high_growth_quality=bool(valuation.get("is_high_growth_quality")),
            is_dividend_asset=_is_div_hint,
        )
        if prof_mod is not None:
            prof_mod.metadata["is_growth_stock"] = gs.get("is_growth_stock")
            prof_mod.metadata["growth_stock_tier"] = gs.get("tier")

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
        # 模块间可能重复同一提示（如应收恶化），按原文去重保序
        _seen_w: set[str] = set()
        deduped: list[str] = []
        for w in all_warnings:
            if w and w not in _seen_w:
                _seen_w.add(w)
                deduped.append(w)
        all_warnings = deduped

        # ── 风险阈值标签：≤2条→风险可控，3-4→风险中等，≥5→风险较高 ──
        risk_warning_count = len(all_warnings)
        if risk_warning_count <= 2:
            risk_level_label = "风险可控"
        elif risk_warning_count <= 4:
            risk_level_label = "风险中等"
        else:
            risk_level_label = "风险较高"

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

        # 生存级重大风险：顶部红灯，强制 E；观察级仅警告/扣分，不熔断
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
            if module_results.get("risk") is not None:
                module_results["risk"].metadata["compliance_veto"] = True

        # 生存级风险的解除留痕：已撤销/已消除的生存级事项不再一票否决，
        # 但必须在报告里显式呈现解除依据（否则用户无法判断"低分是否已过时"）。
        released_survival = major_risks.get("events_released") or []
        if released_survival:
            rel_lines = [
                (
                    f"〔已解除〕【{ev.get('label')}】"
                    f"{ev.get('release_date') or ''} {ev.get('release_title') or ''}"
                    f"（原风险公告 {ev.get('notice_date') or '日期未披露'}）"
                ).strip()
                for ev in released_survival
            ]
            for line in reversed(rel_lines):
                if line and line not in all_warnings:
                    all_warnings.insert(0, line)

        observe_events = major_risks.get("observe_events") or []
        if observe_events and not compliance_veto:
            obs_msg = major_risks.get("observe_message") or "命中观察级风险，警惕情绪杀跌"
            if obs_msg not in all_warnings:
                all_warnings.insert(0, obs_msg)
            for ev in observe_events:
                line = f"〔观察〕【{ev.get('label')}】{ev.get('notice_date') or ''} {ev.get('title') or ''}".strip()
                if line and line not in all_warnings:
                    all_warnings.append(line)
            # 观察级软扣分：综合分下调，但不强制 E、不触发价值陷阱
            composite = round(max(0.0, composite - min(30.0, 8.0 * len(observe_events))), 1)
            letter = rating_label(composite)

        pr = major_risks.get("pledge_ratio")
        if pr is not None and module_results.get("risk") is not None:
            module_results["risk"].metadata["pledge_ratio"] = pr
            module_results["risk"].metadata["pledge_invalid"] = bool(
                major_risks.get("pledge_invalid")
            )
        peer_sample_ok = bool(valuation.get("peer_sample_ok"))
        ind = module_results.get("industry")
        if ind is not None and ind.metadata.get("insufficient_sample"):
            peer_sample_ok = False
        show_pe_pct = market.get("pe_percentile") if peer_sample_ok else None
        show_pb_pct = market.get("pb_percentile") if peer_sample_ok else None

        # ── 五维雷达 + 多空判断 ──────────────────────────────────
        def _bull_bear(score: float) -> str:
            if score >= 70:
                return "看多"
            if score >= 45:
                return "中性"
            if score >= 30:
                return "偏空"
            return "看空"

        # 五维（前端雷达图用）
        dim_scores = {
            "盈利能力": module_results["profitability"].score,
            "成长性": module_results["growth"].score,
            "现金流质量": module_results["cashflow"].score,
            "偿债能力": module_results["solvency"].score,
            "估值合理性": val_score,
        }

        # 短期观点：DDM安全边际 + 股息率支撑 + 成长边际修复 + PE水平
        ddm_scenarios = valuation.get("ddm", {}).get("scenarios", []) if valuation else []
        mid_mos = 0.0
        for sc in ddm_scenarios:
            if sc.get("name") == "中性" and sc.get("margin_of_safety_pct") is not None:
                mid_mos = float(sc["margin_of_safety_pct"])
                break
        price_v = market.get("price") or 0
        pe_v = market.get("pe_ttm") or 0
        dy_v = market.get("dividend_yield") or 0

        short_score = 50
        short_signals: list[str] = []
        if mid_mos >= 0:
            short_score += 15
            short_signals.append(f"DDM中性安全边际+{mid_mos:.0f}%")
        else:
            short_score -= 10
            short_signals.append(f"DDM中性安全边际{mid_mos:.0f}%")
        if dy_v >= 4:
            short_score += 10
            short_signals.append(f"股息率{dy_v:.1f}%有支撑")
        if module_results["growth"].score >= 75:
            short_score += 10
            short_signals.append("成长边际修复")
        if module_results["cashflow"].score >= 80:
            short_score += 8
            short_signals.append("现金流健康")
        if pe_v >= 18:
            short_score -= 10
            short_signals.append(f"PE {pe_v:.1f}偏高压制")
        short_score = max(0, min(100, short_score))

        # 中长期观点：ROE + 偿债 + 分红承诺 + 资产注入 + 红利属性
        long_score = 50
        long_signals: list[str] = []
        if module_results["profitability"].score >= 80:
            long_score += 15
            long_signals.append("盈利能力稳健")
        if module_results["solvency"].score >= 85:
            long_score += 10
            long_signals.append("偿债极强")
        # 红利属性加分（由 classify_dividend_asset 判定）
        if valuation.get("is_dividend_asset"):
            long_score += 12
            long_signals.append("分红承诺≥65%")
            long_score += 8
            long_signals.append("红利龙头底仓")
        # 资产注入外延增长：神华等有growth模块外延指标时加分
        growth_meta = module_results["growth"].metadata or {}
        if growth_meta.get("marginal_recovery") or any("资产注入" in w for w in all_warnings):
            long_score += 10
            long_signals.append("资产注入规模跃升")
        long_score = min(long_score, 100)

        short_view = _bull_bear(short_score)
        long_view = _bull_bear(long_score)

        return {
            "symbol": sym,
            "name": meta.get("name") or market.get("name") or "",
            "industry": meta.get("industry") or "",
            "report_dates": meta.get("report_dates") or [],
            "latest_report": meta.get("latest_report"),
            # 净利同比随快照落库（factor_db.upsert 存整个 result），供扫描层 PEG / 买点信号使用。
            # 此前只写进内部 ctx，confluence 服务的 PEG 恒为 None → 强买入信号永不可达。
            "profit_yoy": meta.get("profit_yoy"),
            "composite_score": composite,
            "final_rating": letter,
            "final_rating_letter": letter,
            "dim_scores": dim_scores,
            "short_term_view": {"score": short_score, "view": short_view, "signals": short_signals},
            "long_term_view": {"score": long_score, "view": long_view, "signals": long_signals},
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
            "risk_level_label": risk_level_label,
            "risk_warning_count": risk_warning_count,
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

        # 股息率回退已上移到 run_full_analysis（ctx 构建前）——保证盈利/风险/
        # 现金流/估值各模块的红利资产分类同源（铁律 9），此处不再重复计算。

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

        # 优先使用真实分红率（分红历史/年报口径），无则回退 股息率×PE 估算
        real_payout = meta.get("payout_ratio_pct")
        div_profile = classify_dividend_asset(
            dividend_yield_pct=div,
            pe_ttm=pe,
            payout_ratio_pct=float(real_payout) if real_payout is not None else None,
        )
        is_div = bool(div_profile.get("is_dividend_asset"))
        result["dividend_profile"] = div_profile
        result["is_dividend_asset"] = is_div

        # ── 成长股/周期底部反转识别（用于估值框架切换） ──────────────
        # 这里只算 growth_stock_tier，不影响 value_trap_veto（那个在 run_full_analysis 前）
        gs_ctx = classify_growth_stock(
            symbol=meta.get("symbol"),
            gross_margin_pct=meta.get("latest_gross_margin"),
            revenue_yoy=meta.get("revenue_yoy"),
            profit_yoy=meta.get("profit_yoy"),
            # 优先复用成长模块算好的规范 CAGR / V 型拐点，保证与引擎后文
            # 的判定完全一致（否则估值侧会漏掉「CAGR 为负但拐点已确认」的反转股，
            # is_growth 判 False，PEG 核心锚与动态PE 都进不了估值构成）。
            profit_cagr_3y=(
                meta.get("_growth_cagr_3y")
                if meta.get("_growth_cagr_3y") is not None
                else (
                    float(fin_df["net_profit"].pct_change(3).dropna().iloc[-1] * 100)
                    if len(fin_df) >= 4 and "net_profit" in fin_df.columns
                    else None
                )
            ),
            is_v_shape=bool(meta.get("_growth_v_shape")),
            is_marginal_recovery=bool(meta.get("_growth_marginal_recovery")),
            pe_ttm=pe,
            pb=pb,
            is_high_growth_quality=bool(result.get("is_high_growth_quality")),
            is_dividend_asset=is_div,
        )
        is_growth = bool(gs_ctx.get("is_growth_stock"))
        result["growth_stock_profile"] = gs_ctx
        result["is_growth_stock"] = is_growth
        # 周期/资源股识别（全局，供估值模块复用）
        _cycle_kw = ("化工", "农化", "农化制品", "化肥", "磷", "矿", "煤炭", "有色", "钢铁", "石油", "天然气", "农药")
        is_cycle_val = any(k in str(meta.get("industry") or "") for k in _cycle_kw)

        # ── 成长股：PE/PB 极端值（周期底部/高增长）不直接判高估 ─────
        # 前提：is_growth 已由 growth_profile 严格认定（含 PB≤CYCLE_TROUGH_PB_MAX
        # 的周期底部校验），不再是「PE 高 → 算成长股 → 免 PE 惩罚」的自证循环。
        # 即便 is_growth 为真，仍需交叉校验历史分位：软化只适用于
        # 「绝对估值高但历史分位不极端」的成长股；若分位本身已在 90% 以上，
        # 说明市场已把成长预期充分定价，不能再以「成长股」为由洗白（否则
        # 越贵的票越宽松，正是 600722 金牛化工那类高 PE 高 PB 票的漏洞）。
        # 门槛按「绝对估值有多贵」分档（阶梯），而不是单一 90% 常量：
        #   PE > 150        → 分位 ≥ 70 即判偏高（绝对估值已是天文数字，
        #                     分位不高往往只因自身盈利塌陷拉高了历史基数）
        #   PE 80 ~ 150     → 分位 ≥ 80 即判偏高（长白山 PE 80.6 / 分位 86.1
        #                     落在此档：绝对与相对双双高企，不应软化）
        #   PB > 8 同理     → 分位 ≥ 80
        # 依据：软化本意是「trailing PE 因周期底部/盈利拐点暂时失真」，
        # 而「绝对估值极高 + 历史分位偏高」是双重确认的贵，属于该敞口被滥用
        # 的典型（600722 金牛化工 PE 193、603099 长白山 PE 80.6 皆属此列）。
        if is_growth and not is_div:
            _apply_valuation_soften_ladder(
                rel, pe=pe, pb=pb, is_growth=is_growth, is_div=is_div
            )
        if is_growth and not is_div and "PEG" in rel and (
                rel["PEG"].get("growth_rate") is not None
                and float(rel["PEG"]["growth_rate"]) <= 0
            ):
                rel["PEG"]["value"] = None
                rel["PEG"]["current"] = None
                rel["PEG"]["signal"] = "—"
                rel["PEG"]["note"] = "成长股/周期底部：负CAGR使PEG失真，参考PS与营收增速"

        if is_div:
            # 红利资产：PEG 成长尺子不适用
            # 彻底清空 value/current/growth_rate/growth_label，
            # 避免前端仍展示 13.9 这类失真数字误导用户
            if "PEG" in rel:
                rel["PEG"]["value"] = None
                rel["PEG"]["current"] = None
                rel["PEG"]["growth_rate"] = None
                rel["PEG"]["growth_label"] = None
                rel["PEG"]["signal"] = "—"
                rel["PEG"]["skipped_for_dividend_asset"] = True
                rel["PEG"]["note"] = (
                    "高股息/红利资产不适用 PEG 成长估值；"
                    "建议改看股息率利差、分红确定性与 EV/EBITDA"
                )
            # 历史分位偏高 ≠ 泡沫：降级为「合理偏高」说明，不参与「高估」扣分
            for _mk in ("PE_TTM", "PB"):
                if _mk in rel:
                    pct = rel[_mk].get("percentile_5y")
                    if rel[_mk].get("signal") == "高估" or (
                        pct is not None and float(pct) >= 75
                    ):
                        rel[_mk]["signal"] = "合理"
                        rel[_mk]["dividend_percentile_softened"] = True
                        rel[_mk]["note"] = dividend_valuation_note(
                            dividend_yield_pct=div_profile.get("dividend_yield_pct"),
                            payout_ratio_pct=div_profile.get("payout_ratio_pct"),
                            pe_percentile=float(pct) if pct is not None else pe_pct,
                        )
            spread = div_profile.get("div_bond_spread_pct")
            spread_sig, _spread_pts = dividend_spread_signal(
                float(spread) if spread is not None else None
            )
            rel["股息国债利差"] = {
                "current": spread,
                "dividend_yield_pct": div_profile.get("dividend_yield_pct"),
                "bond_yield_pct": div_profile.get("bond_yield_pct"),
                "signal": spread_sig,
                "percentile_5y": None,
                "note": "股息率 − 十年期国债收益率；资产荒/降息周期下的红利定价锚",
            }
            po = div_profile.get("payout_ratio_pct")
            if po is not None:
                if float(po) >= 65:
                    po_sig, po_pts = "低估", 86.0
                elif float(po) >= 50:
                    po_sig, po_pts = "合理", 74.0
                else:
                    po_sig, po_pts = "偏贵", 45.0
                rel["分红比例"] = {
                    "current": round(float(po), 1),
                    "signal": po_sig,
                    "score_hint": po_pts,
                    "percentile_5y": None,
                    "note": (
                        f"{meta.get('dividend', {}).get('fy') or '最近完整'}年度现金分红/归母净利"
                        if real_payout is not None
                        else "估分红率=股息率×PE；≥65% 体现股东回报确定性"
                    ),
                }

        # 可比公司信号优先补强相对估值（仅样本充足时）
        comps_signal = (comps or {}).get("signal") if sample_ok else None
        if comps_signal and pe is not None and "PE_TTM" in rel:
            if rel["PE_TTM"].get("signal") in (None, "—"):
                rel["PE_TTM"]["signal"] = comps_signal
            # 红利资产：不让可比「高估」再锁死分数
            elif is_div and comps_signal == "高估":
                rel["PE_TTM"]["signal"] = "合理"
                rel["PE_TTM"]["note"] = dividend_valuation_note(
                    dividend_yield_pct=div_profile.get("dividend_yield_pct"),
                    payout_ratio_pct=div_profile.get("payout_ratio_pct"),
                    pe_percentile=pe_pct,
                )
        result["relative"] = rel

        scores: list[float] = []
        weights_list: list[float] = []
        breakdown: list[dict[str, Any]] = []

        def _add_points(factor: str, points: float, detail: str, weight: float = 1.0) -> None:
            scores.append(points)
            weights_list.append(weight)
            breakdown.append({"factor": factor, "points": round(points, 1), "detail": detail})

        if is_div:
            # 红利股估值框架：股息率利差 40% + 分红确定性 30%
            # + 自由现金流覆盖 20% + PE历史分位 10%（弱化历史分位噪音）
            div_factors = self._dividend_valuation_factors(
                fin_df=fin_df,
                meta=meta,
                div_profile=div_profile,
                pe_pct=pe_pct,
                pb_pct=pb_pct,
                cashflow_score=cashflow_score,
            )
            weights = {"股息率利差": 0.40, "分红确定性": 0.30, "分红现金覆盖": 0.20, "PE/PB分位": 0.10}
            for name, item in div_factors.items():
                _add_points(name, float(item["score"]), str(item.get("detail") or ""))
            base_score = round(
                sum(div_factors[n]["score"] * w for n, w in weights.items()), 1
            )
            result["dividend_valuation_factors"] = {
                n: {"score": it["score"], "weight": weights[n], "detail": it.get("detail")}
                for n, it in div_factors.items()
            }
            # 周期/资源股溢价因子（原「资源壁垒溢价 +4 / 红利定价锚 +2」）已移除：
            # 1) 红利定价锚对「股息率利差≥2.8%」再 +2，而利差本身就是 40% 权重的
            #    「股息率利差」因子（≥2.5% 已得 88 分）——同一事实重复计分；
            # 2) 资源壁垒溢价按绝对 PE<12 加分，与 10% 权重的 PE/PB 分位因子重复
            #    计入「便宜」这一维度，且关键词（矿/磷/农化）对煤炭等行业名不生效，
            #    实测中国神华/陕西煤业/兖矿能源/中煤能源/云天化/淮北矿业六只样本
            #    无一触发，属近乎死代码；
            # 3) 原 _add_points 展示的 88/85 分与实际贡献（+4/+2 且不进加权）口径不符。
            # 周期股的分红承诺已有独立加分（_dividend_valuation_factors 内
            # 「周期股分红承诺≥45% +5」），无需外部再溢价。
        else:
            # ── 估值评分：历史分位50% + 相对估值30% + 股息率20% ──
            # 1. PE历史分位（权重50%）
            pe_pct_val = rel.get("PE_TTM", {}).get("percentile_5y")
            if pe_pct_val is not None:
                pp = float(pe_pct_val)
                if pp < 10:
                    pe_pct_score = 90.0
                elif pp < 30:
                    pe_pct_score = 70.0
                elif pp < 70:
                    pe_pct_score = 50.0
                elif pp < 90:
                    pe_pct_score = 30.0
                else:
                    pe_pct_score = 10.0
                _add_points("PE历史分位", pe_pct_score, f"分位{pp:.0f}%", weight=0.50)
            elif pe is not None and 0 < float(pe):
                # 无历史分位时用绝对PE回退
                pv = float(pe)
                if pv < 12:
                    _add_points("绝对PE", 88, f"PE={pv:.1f}<12（无分位数据）")
                elif pv < 20:
                    _add_points("绝对PE", 65, f"PE={pv:.1f}（无分位数据）")
                else:
                    _add_points("绝对PE", 40, f"PE={pv:.1f}（无分位数据）")
            # 2. 相对估值（权重30%）：PE / 同行中位数
            peer_pe = comps.get("avg_pe") if sample_ok else None
            if pe is not None and peer_pe is not None and float(peer_pe) > 0:
                relative_pe = float(pe) / float(peer_pe)
                if relative_pe < 0.8:
                    rel_score = 80.0
                elif relative_pe < 1.2:
                    rel_score = 50.0
                else:
                    rel_score = 20.0
                _add_points("相对PE", rel_score, f"PE/同行中位={relative_pe:.2f}", weight=0.30)
            elif comps_signal:
                # 无可比中位数时用信号回退
                if comps_signal == "低估":
                    _add_points("可比公司", 82, "相对可比低估")
                elif comps_signal == "高估":
                    _add_points("可比公司", 30, "相对可比高估")
            # 3. 股息率（权重20%）
            if div is not None:
                dy = float(div)
                if dy > 4:
                    div_score = 90.0
                elif dy > 3:
                    div_score = 70.0
                elif dy > 2:
                    div_score = 50.0
                else:
                    div_score = 30.0
                _add_points("股息率", div_score, f"股息率={dy:.1f}%", weight=0.20)
            # ── 成长股估值框架补充 ─────────────────────────────────────────
            if is_growth:
                # 毛利率溢价（技术壁垒代理）
                gm = meta.get("latest_gross_margin")
                if gm is not None:
                    gm = float(gm)
                    if gm >= 50:
                        _add_points("毛利率溢价", 88, f"毛利率{gm:.0f}% 技术壁垒强")
                    elif gm >= 40:
                        _add_points("毛利率溢价", 76, f"毛利率{gm:.0f}% 有技术壁垒")
                    elif gm >= 30:
                        _add_points("毛利率溢价", 65, f"毛利率{gm:.0f}% 有一定壁垒")
                # 营收增速（赛道景气度代理）
                ry = meta.get("revenue_yoy")
                if ry is not None:
                    ry = float(ry)
                    if ry >= 30:
                        _add_points("营收增速", 88, f"营收同比+{ry:.0f}% 高景气")
                    elif ry >= 15:
                        _add_points("营收增速", 75, f"营收同比+{ry:.0f}% 景气")
                    elif ry >= 0:
                        _add_points("营收增速", 60, f"营收同比+{ry:.0f}%")
                # 周期底部反转加分
                if gs_ctx.get("tier") == "turnaround":
                    _add_points("周期反转溢价", 72, "CAGR负但拐点确认，底部区域")
                # ── 动态PE（基于利润增速估算前瞻PE） ───────────────────────
                # 成长股核心：静态PE失真，前瞻PE更有参考价值
                if pe is not None and float(pe) > 0:
                    peg_growth = None
                    for b in breakdown:
                        if "PEG" in b.get("factor", ""):
                            # 从 PEG breakdown 取 growth_rate
                            break
                    # 回退：用前瞻口径同比或 CAGR 估算前瞻PE。
                    # 必须用 profit_yoy_forward（已抑制基期接近0/并表暴增的极值），
                    # 直接用真实同比会把 +493% 当成可持续增速，算出 4x 的前瞻PE。
                    profit_yoy_val = meta.get("profit_yoy_forward")
                    if profit_yoy_val is None:
                        profit_yoy_val = meta.get("profit_yoy")
                    if profit_yoy_val is not None and float(profit_yoy_val) > 10:
                        fwd_growth = float(profit_yoy_val) / 100.0
                        fwd_pe = float(pe) / (1.0 + fwd_growth)
                        _ext_note = (
                            f"（实际披露同比+{float(meta.get('profit_yoy') or 0):.0f}%"
                            f"已按+{float(profit_yoy_val):.0f}%折减）"
                            if meta.get("profit_yoy_extreme")
                            else f"（基于净利同比+{float(profit_yoy_val):.0f}%）"
                        )
                        if fwd_pe < 20:
                            _add_points("动态PE(1Y)", 82, f"前瞻PE={fwd_pe:.1f}x{_ext_note}")
                        elif fwd_pe < 30:
                            _add_points("动态PE(1Y)", 72, f"前瞻PE={fwd_pe:.1f}x{_ext_note}")
                        elif fwd_pe < 45:
                            _add_points("动态PE(1Y)", 60, f"前瞻PE={fwd_pe:.1f}x{_ext_note}")
                    # PEG<1 成长股核心定价锚：额外强调
                    peg_val = rel.get("PEG", {}).get("value")
                    if peg_val is not None and float(peg_val) > 0 and float(peg_val) < 1:
                        if float(peg_val) < 0.5:
                            _add_points("PEG核心锚", 90, f"PEG={float(peg_val):.2f}<<1，成长股核心低估信号")
                        else:
                            _add_points("PEG核心锚", 80, f"PEG={float(peg_val):.2f}<1，成长股低估信号")
                # ── 成长股静态PE/PB软化：PEG<1时降低PE/PB“高估”惩罚 ──────
                peg_val = rel.get("PEG", {}).get("value")
                if peg_val is not None and float(peg_val) < 1:
                    for i, b in enumerate(breakdown):
                        if b["factor"] == "PE_TTM" and b["points"] <= 35:
                            # PE_TTM 判“高估”但 PEG<1 →  soften 到 55
                            scores[i] = 55.0
                            breakdown[i]["points"] = 55.0
                            breakdown[i]["detail"] += "；PEG<1软化（静态PE失真）"
                        elif b["factor"] == "PB" and b["points"] <= 35:
                            scores[i] = 55.0
                            breakdown[i]["points"] = 55.0
                            breakdown[i]["detail"] += "；成长股PB软化"
            # 加权平均：核心三因子（分位50%+相对30%+股息20%）+ 成长补充因子（等权bonus）
            total_w = sum(weights_list)
            base_score = sum(s * w for s, w in zip(scores, weights_list)) / total_w if total_w > 0 else 55.0
        rationale_parts: list[str] = []
        if is_div:
            rationale_parts.append(
                dividend_valuation_note(
                    dividend_yield_pct=div_profile.get("dividend_yield_pct"),
                    payout_ratio_pct=div_profile.get("payout_ratio_pct"),
                    pe_percentile=pe_pct,
                )
            )
            rationale_parts.append(
                "红利/高股息口径：以股息率、分红比例、国债利差与现金流覆盖为主，弱化 PE/PB/PEG 历史分位"
            )
        elif is_growth:
            rationale_parts.append(
                "成长/周期反转口径：PE/PB 极端值不直接判高估，"
                "参考毛利率（技术壁垒）、营收增速（赛道景气）与周期位置"
            )
        if breakdown:
            parts = [f"{b['factor']}{b['points']:.0f}" for b in breakdown]
            verb = "加权" if is_div and not is_cycle_val else "均值"
            rationale_parts.append("构成：" + "、".join(parts) + f" → {verb} {base_score:.0f}")
        else:
            rationale_parts.append("暂无相对估值信号，采用默认中性分")

        haircut = 0.0
        # 高成长科技股：现金流滞后是常态，不因现金流低分扣减估值
        revenue_yoy_hg = meta.get("revenue_yoy")
        is_high_growth_val = revenue_yoy_hg is not None and float(revenue_yoy_hg) >= 30
        if not is_high_growth_val and cashflow_score is not None and cashflow_score < 45:
            cheap_looking = any(
                (rel.get(k) or {}).get("signal") == "低估" for k in ("PE_TTM", "PB", "PEG")
            ) or base_score >= 70
            if cheap_looking or base_score >= 60:
                haircut = min(
                    VALUATION_HAIRCUT_MAX, max(8.0, (45.0 - float(cashflow_score)) * 0.55)
                )
                rationale_parts.append(
                    f"现金流质量仅 {cashflow_score:.0f} 分，估值合理性折减 {haircut:.0f} 分"
                    f"（账面估值偏便宜但造血不足）"
                )

        # ── 周期陷阱折减：与现金流折减取 max，不叠加 ──────────────────
        # 单只票在两个理由同时成立时只吃一次折减，避免惩罚叠加把周期股
        # 一路打到地板（铁律：惩罚有限度、禁止叠加）。
        cycle_haircut = 0.0
        _ind_c = str(meta.get("industry") or "").strip()
        if _ind_c in STRONG_CYCLICAL_INDUSTRIES and not is_div:
            pe_pct_c = (rel.get("PE_TTM") or {}).get("percentile_5y")
            pb_pct_c = (rel.get("PB") or {}).get("percentile_5y")
            roe_c_raw = meta.get("latest_roe")
            roe_c = float(roe_c_raw) if roe_c_raw is not None else None
            if cycle_trap_hit(pe_pct_c, pb_pct_c, roe_c):
                cycle_haircut = CYCLE_TRAP_HAIRCUT
                result["cycle_trap_warning"] = True
                result["cycle_trap_note"] = (
                    f"{_ind_c}：PE 历史分位 {float(pe_pct_c):.0f}%（低）但 PB 历史分位 "
                    f"{float(pb_pct_c):.0f}%（高）、ROE {roe_c:.1f}%，"
                    "典型景气高点特征，低 PE 不等于低估"
                )
                rationale_parts.append(
                    result["cycle_trap_note"] + f"，估值合理性折减 {cycle_haircut:.0f} 分"
                )
        if cycle_haircut > haircut:
            haircut = cycle_haircut
            # 周期折减胜出时，现金流折减的文案不再成立（分数只减了一次），
            # 必须把误导性的那句话摘掉，否则文案与实际扣分口径不符。
            rationale_parts = [p for p in rationale_parts if "造血不足" not in p]

        final_score = max(20.0, base_score - haircut)
        result["composite_valuation_score"] = final_score
        result["valuation_score_breakdown"] = breakdown
        result["valuation_score_base"] = round(base_score, 1)
        result["valuation_score_haircut"] = round(haircut, 1)
        result["valuation_rationale"] = "；".join(rationale_parts)

        if not fin_df.empty:
            result["dcf"] = self._build_dcf(fin_df, market, meta)
            # ── 高成长科技股：DCF 模型过于悲观，仅作极端压力测试参考 ──
            revenue_yoy_v = meta.get("revenue_yoy")
            if revenue_yoy_v is not None and float(revenue_yoy_v) >= 30:
                dcf_r = result["dcf"]
                dcf_r["role"] = "high_growth_reference"
                dcf_r["is_reliable"] = False
                dcf_r["display_label"] = "DCF内在价值（极端悲观情景）"
                iv_str = None
                if dcf_r.get("intrinsic_value_per_share"):
                    iv_str = f"{float(dcf_r['intrinsic_value_per_share']):.2f}元"
                dcf_r["note"] = (
                    (dcf_r.get("note") or "")
                    + f"；高成长科技股（营收增速>30%）DCF模型假设过于保守，"
                    f"结果仅供参考，核心定价请参考相对估值（PEG/PS/可比公司）"
                )
                # DCF 权重降至 10%：高成长股以 PEG/PS/可比公司为主（占 90%）
                if final_score < 55:
                    final_score = 60.0
                    result["composite_valuation_score"] = final_score
                    # 只有当 PEG/成长补充因子真的进入了估值构成时才这么写，
                    # 否则文案会声称「以 PEG/PS 为主」而构成里根本没有 PEG。
                    _bd = result.get("valuation_score_breakdown") or []
                    _has_peg = any("PEG" in str(b.get("factor") or "") for b in _bd)
                    _note = "；高成长科技股 DCF 权重降至 10%"
                    _note += (
                        "，以 PEG/PS/可比公司为主"
                        if _has_peg
                        else "（PEG 未纳入本次构成：成长口径未确认，按核心三因子定价）"
                    )
                    result["valuation_rationale"] = (
                        (result.get("valuation_rationale") or "") + _note
                    )

        # 红利资产：DDM（股利贴现）作为核心参考估值，替代 DCF
        # 扩展：分红率>60% 的非红利股也构建 DDM 用于多模型交叉
        payout_ratio_pct = meta.get("payout_ratio_pct")
        is_high_payout = (
            payout_ratio_pct is not None and float(payout_ratio_pct) >= 60
        )
        if (is_div or is_high_payout) and not fin_df.empty:
            result["ddm"] = self._build_ddm(market, meta)

        # ── 多模型交叉内在价值：DCF 40% + DDM 40% + 机构目标价 20% ──
        dcf_for_combo = result.get("dcf") or {}
        ddm_for_combo = result.get("ddm") or {}
        dcf_iv = dcf_for_combo.get("intrinsic_value_per_share")
        # 取 DDM 中性情景
        ddm_iv = None
        for sc in ddm_for_combo.get("scenarios", []):
            if sc.get("name") == "中性":
                ddm_iv = sc.get("intrinsic_value_per_share")
                break
        # 机构目标价（当前无数据源，预留接口），无则用 DCF 替代
        target_iv = meta.get("consensus_target_price") or market.get("target_price")
        if target_iv is not None:
            target_iv = float(target_iv)

        components: list[tuple[str, float, float]] = []  # (name, value, weight)
        if dcf_iv is not None and float(dcf_iv) > 0:
            components.append(("DCF", float(dcf_iv), 0.4))
        if ddm_iv is not None and float(ddm_iv) > 0:
            components.append(("DDM", float(ddm_iv), 0.4))
        if target_iv is not None and target_iv > 0:
            components.append(("机构目标价", target_iv, 0.2))

        # 若某模型缺失，用 DCF 替代其权重（保持总权重=1）
        if components:
            total_w = sum(w for _, _, w in components)
            combo_iv = sum(v * w for _, v, w in components) / total_w
            combo_note = " + ".join(
                f"{n}={v:.2f}×{w/total_w:.0%}" for n, v, w in components
            )
            result["combined_intrinsic_value"] = {
                "intrinsic_value_per_share": round(combo_iv, 2),
                "components": [
                    {"model": n, "value": round(v, 2), "weight": round(w / total_w, 3)}
                    for n, v, w in components
                ],
                "note": f"多模型交叉内在价值：{combo_note} = {combo_iv:.2f}元",
            }

        # 无相对估值信号时：仅用「可信」DCF 的安全边际粗估，避免默认 55 / 爆表估值污染分数
        dcf = result.get("dcf") or {}
        if not scores and dcf.get("intrinsic_value_per_share"):
            # 高成长科技股：DCF 过于悲观，不给低分
            if dcf.get("role") == "high_growth_reference":
                result["composite_valuation_score"] = max(final_score, 60.0)
                result["valuation_rationale"] = (
                    (result.get("valuation_rationale") or "")
                    + "；高成长科技股以相对估值（PEG/PS/可比公司）为主，DCF 仅作参考"
                )
            elif dcf.get("is_reliable"):
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

        # 高股息/高成长赛道/周期底部反转：ROIC<WACC 不得作为价值陷阱理由
        if prof is not None and (
            prof.metadata.get("is_dividend_asset")
            or prof.metadata.get("is_high_growth_quality")
            or prof.metadata.get("is_growth_stock")
        ):
            reasons = [r for r in reasons if r != "ROIC低于WACC"]

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
    def _peg_base_check(window, base: float | None = None) -> str | None:
        """基数校验：正 CAGR 是否由「崩塌趋势」或「过低基期」算出来的。

        ``window`` 用于找**峰值与最新值**（应传完整可用序列，才能看见早期的
        高位与崩塌）；``base`` 是 CAGR 的实际起点净利（3年CAGR 时 = 倒数第4期），
        缺省取 ``window`` 最老一期。

        返回不通过原因（字符串），通过则返回 None。

        两个判据都以「窗口内峰值」为参照，而不是相邻两期比较 —— 「利润腰斩」
        可能是一次性事件（资产减值）也可能是趋势性下滑；与峰值比能同时覆盖，
        且与「基期过低」共用同一条基线。
        """
        try:
            vals = [float(v) for v in window.dropna()]
        except (TypeError, ValueError):
            return None
        if len(vals) < 2:
            return None
        peak = max(vals)
        if peak <= 0:
            return None
        latest = vals[-1]
        if base is None:
            base = vals[0]
        if latest < peak * 0.5:
            return (
                f"最近一期净利不足窗口峰值50%"
                f"（{latest / 1e8:.2f}亿 vs 峰值{peak / 1e8:.2f}亿）"
            )
        if 0 < base < peak * 0.2:
            return (
                f"基期净利过低（{base / 1e8:.2f}亿 < 峰值{peak / 1e8:.2f}亿的20%）"
            )
        return None

    @staticmethod
    def _growth_for_peg(fin_df: pd.DataFrame, meta: dict) -> tuple[float | None, str]:
        """PEG 的分母（全系统唯一来源）。

        **只接受多年年报口径的增速**，且窗口不可用时 PEG 直接判「不适用」：
          1) 年报点 ≥4：近 3 年净利 CAGR；
          2) 年报点 ≥2：全部年报点的 CAGR（跨度须 ≥1 年）作为补充。
        **窗口存在但窗口内负增长/含亏损 → 返回 None，禁止退化成单期同比。**

        为什么必须堵住「回退单期同比」：PEG 的定义是「多年可持续增速」倍率，
        单期同比（尤其周期股底部的低基数反弹）不是可持续增速。混用会造出
        「PE 越贵、越靠一次性暴增算出越便宜」的反向信号。实测（2026-09-21，
        5254 只快照）：走回退分支的 1452 只（27.6%）中 1002 只 PEG<1、437 只
        拿到「PEG核心锚」加分；PE 最高的 25 只（PE 164~285）增速全部等于
        +300%（极值折减上限），即晶丰明源 PE 285.4 / PEG 0.95「低估」这类结论。
        抽查其中 12 只，年报点均为 5 个而 3 年 CAGR 全部为负或不可比
        （亏损基期）——「多年趋势向下 + 单季暴增」正是典型的周期陷阱。

        仅当完全没有多年窗口（新上市票年报点 <2）时才回退单期同比，
        且拒绝已按极值折减过的同比（那是系统对「该增速不可持续」的自认）。

        **基数校验（2026-09-22 新增）**：即使窗口内存量点全为正、CAGR 为正，
        仍需校验「这个正 CAGR 是不是被基数效应或崩塌趋势算出来的」。两类陷阱：

          ① **崩塌趋势**：最近一期净利 < 窗口内峰值 × ``_PEG_PEAK_KEEP``(0.5)。
             立霸股份 603519 净利 6.40亿(2023) → 1.59亿(2024) → 1.57亿(2025)，
             长窗口 CAGR 仍为 +9.27%（起点 2021 年 1.10 亿偏低），但利润已从
             高点腰斩且连续两年下滑 —— 用这个分母算出的 PEG 会「越崩越便宜」。
          ② **基期过低**：起点净利 < 窗口内峰值 × ``_PEG_BASE_KEEP``(0.2)，
             即 CAGR 的增长几乎全部来自「从坑里爬出来」，不代表可持续增速。

        命中任一 → PEG 不适用（返回 None）。**方向只会更严**：把原本会给出
        PEG 的票改判 None，而 PEG 为 None 时全站按保守口径处理（不触发强买入），
        不会产生新的乐观信号。
        """
        if fin_df is not None and not fin_df.empty and "net_profit" in fin_df.columns:
            clean = fin_df["net_profit"].dropna()
            if len(clean) >= 4:
                start, end = float(clean.iloc[-4]), float(clean.iloc[-1])
                if start > 0 and end > 0:
                    cagr = ((end / start) ** (1 / 3) - 1) * 100
                    if cagr > 0:
                        # 校验窗口用完整序列（才看得见早期高位与崩塌），
                        # base 显式传 CAGR 起点（倒数第 4 期）。
                        reason = FundamentalEngine._peg_base_check(clean, start)
                        if reason is not None:
                            return None, f"{reason}，PEG不适用"
                        return round(cagr, 2), "3年净利CAGR"
            if len(clean) >= 2:
                start, end = float(clean.iloc[0]), float(clean.iloc[-1])
                span = max(len(clean) - 1, 1)
                if start > 0 and end > 0:
                    cagr = ((end / start) ** (1 / span) - 1) * 100
                    if cagr > 0:
                        reason = FundamentalEngine._peg_base_check(clean, start)
                        if reason is not None:
                            return None, f"{reason}，PEG不适用"
                        return round(cagr, 2), f"{span}年净利CAGR"
                # 年报窗口已存在但不可用（亏损基期 / 负增长）→ PEG 不适用
                return None, "年报窗口负增长/含亏损，PEG不适用"
        # 极值同比（±300% 折减后）本身就是「该增速不可持续」的自认，不能做 PEG 分母
        if meta.get("profit_yoy_extreme"):
            return None, "单期同比为极值（已折减），不足以做PEG分母"
        yoy = meta.get("profit_yoy")
        if yoy is not None and float(yoy) > 0:
            return round(float(yoy), 2), "最新净利同比（无年报序列）"
        return None, "净利增速"

    @staticmethod
    def _growth_rate(meta: dict) -> float:
        # 前瞻外推场景统一取折减口径，避免把一次性暴增当作长期增速
        raw = meta.get("profit_yoy_forward")
        if raw is None:
            raw = meta.get("profit_yoy")
        if raw is None:
            return 0.05
        raw_f = float(raw)
        # 兼容误传小数（0.25=25%）与正常百分数（25 / 91）
        if 0 < abs(raw_f) < 1.5:
            growth = raw_f
        else:
            growth = raw_f / 100.0
        # 低负债现金奶牛允许略高长期增速上限，避免过度悲观
        dr = meta.get("debt_ratio")
        cap = 0.14 if dr is not None and float(dr) < 30 else 0.12
        # 高毛利高成长：DCF 仍保守，但给更高扩张期增速上限（仍远低于市场叙事）
        gm = meta.get("latest_gross_margin")
        hq = classify_high_growth_quality(
            gross_margin_pct=float(gm) if gm is not None else None,
            profit_yoy_pct=raw_f if abs(raw_f) >= 1.5 else raw_f * 100.0,
        )
        if hq.get("is_high_growth_quality"):
            cap = 0.18
        return max(0.02, min(cap, growth))

    @staticmethod
    def _dynamic_wacc(
        debt_ratio: float | None,
        *,
        is_dividend_asset: bool = False,
        is_high_growth_quality: bool = False,
        interest_bearing_ratio: float | None = None,
        business_model: str = "",
    ) -> float:
        """DCF 折现率。统一委托 `_estimate_wacc_pct`（有息负债率口径），保证全系统单一 WACC。

        此前本函数用「含应付的资产负债率」独立分档：宁德时代 DR 63.65%（含应付
        账款/合同负债）→ 12%，而盈利模块按有息负债率 12.79% 给 7%——同一公司
        两个 WACC。应付账款/合同负债等经营性负债不构成资本成本，
        见 `_estimate_wacc_pct` docstring。
        """
        return _estimate_wacc_pct(
            debt_ratio,
            is_dividend_asset=is_dividend_asset,
            is_high_growth_quality=is_high_growth_quality,
            interest_bearing_ratio=interest_bearing_ratio,
            business_model=business_model,
        ) / 100.0

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
    def _base_fcf(fin_df: pd.DataFrame, symbol: str, *, is_high_growth: bool = False) -> tuple[float | None, str]:
        """真实 FCF = OCF − |CapEx|；为负时降级，高成长用 OCF×0.5（扩张期CapEx偏高，OCF更能反映造血能力）。"""
        ocf = float(fin_df.get("operating_cashflow", pd.Series([0])).iloc[-1] or 0)
        capex_raw = fin_df.get("capital_expenditure", pd.Series([0])).iloc[-1]
        capex = abs(float(capex_raw or 0))
        real_fcf = ocf - capex if ocf else 0.0

        if real_fcf > 0:
            return real_fcf, "ocf-capex"
        if ocf > 0:
            # 高成长科技股：扩张期CapEx偏高导致真实FCF为负，用OCF×0.5近似正常化FCF
            if is_high_growth:
                logger.info("[%s] 高成长股真实FCF为负，使用 OCF*0.5 作为基期（扩张期CapEx偏高）", symbol)
                return ocf * 0.5, "ocf*0.5_high_growth"
            logger.info("[%s] 真实FCF为负/无效，降级使用 OCF*0.3 作为基期", symbol)
            return ocf * 0.3, "ocf*0.3"
        logger.warning("[%s] 无有效现金流数据，跳过 DCF 估值", symbol)
        return None, "skip"

    @staticmethod
    def _dividend_valuation_factors(
        *,
        fin_df: pd.DataFrame,
        meta: dict,
        div_profile: dict,
        pe_pct: float | None,
        pb_pct: float | None,
        cashflow_score: float | None,
    ) -> dict[str, dict[str, Any]]:
        """
        红利股估值四因子加权框架（替代 PE/PB 分位均值）：
        股息率利差 40% + 分红确定性 30% + 自由现金流覆盖 20% + PE 分位 10%
        """
        div_info = meta.get("dividend") or {}
        # 1. 股息率利差（当前股息率 - 10Y 国债）
        dy = div_profile.get("dividend_yield_pct")
        bond = div_profile.get("bond_yield_pct") or CN_10Y_BOND_YIELD_PCT
        spread = (float(dy) - float(bond)) if dy is not None else None
        if spread is not None:
            spread_pct = spread  # 百分点
            if spread_pct >= 2.5:
                spread_score = 88.0
                spread_detail = f"股息率{dy:.1f}%−国债{bond:.1f}%={spread_pct:.1f}pct，利差极宽（≥2.5%）"
            elif spread_pct >= 1.5:
                spread_score = 78.0
                spread_detail = f"股息率{dy:.1f}%−国债{bond:.1f}%={spread_pct:.1f}pct，利差充足（≥1.5%）"
            elif spread_pct >= 0.5:
                spread_score = 65.0
                spread_detail = f"股息率{dy:.1f}%−国债{bond:.1f}%={spread_pct:.1f}pct，利差尚可（≥0.5%）"
            else:
                spread_score = 45.0
                spread_detail = f"股息率{dy:.1f}%−国债{bond:.1f}%={spread_pct:.1f}pct，利差偏薄（<0.5%）"
        else:
            spread_score = 60.0
            spread_detail = "股息率数据缺失，利差无法计算"

        # 2. 分红确定性（真实分红率 + 连续年数 + 承诺）
        payout = div_info.get("payout_ratio_pct") or div_profile.get("payout_ratio_pct")
        consec = div_info.get("consecutive_years")
        det_score = 50.0
        det_parts: list[str] = []
        if payout is not None and float(payout) >= 65:
            det_score += 25
            det_parts.append(f"分红率{float(payout):.1f}%≥65%")
        elif payout is not None and float(payout) >= 50:
            det_score += 15
            det_parts.append(f"分红率{float(payout):.1f}%≥50%")
        elif payout is not None and float(payout) >= 40:
            det_score += 8
            det_parts.append(f"分红率{float(payout):.1f}%≥40%")
        if consec and int(consec) >= 10:
            det_score += 12
            det_parts.append(f"连续分红{int(consec)}年")
        elif consec and int(consec) >= 5:
            det_score += 8
            det_parts.append(f"连续分红{int(consec)}年")
        elif consec and int(consec) >= 3:
            det_score += 4
            det_parts.append(f"连续分红{int(consec)}年")
        # 周期股分红承诺（≥45%）额外加分
        ind = str(meta.get("industry") or "")
        cycle_kw = ("化工", "农化", "化肥", "磷", "矿", "煤炭", "有色", "钢铁", "石油", "天然气")
        if any(k in ind for k in cycle_kw) and payout is not None and float(payout) >= 45:
            det_score += 5
            det_parts.append("周期股分红承诺≥45%")
        # 分红率≥70% 且连续≥15年 → 分红承诺极强
        if payout is not None and float(payout) >= 70 and consec and int(consec) >= 15:
            det_score += 5
            det_parts.append("分红承诺极强")
        det_score = min(det_score, 95.0)
        if not det_parts:
            det_parts.append("分红数据有限")
        certainty_detail = "；".join(det_parts)

        # 3. 自由现金流覆盖（FCF/分红倍数 + OCF/净利辅助）
        ocf_np = meta.get("latest_cash_ratio")  # OCF/净利
        fcf_div_gap = div_info.get("fcf_dividend_gap")
        fcf = div_info.get("fcf")
        cash_total = div_info.get("cash_total")
        cover_score = 50.0
        cover_parts: list[str] = []
        # 核心指标：FCF / 现金分红 覆盖倍数
        fcf_coverage = None
        if fcf is not None and cash_total is not None and float(cash_total) > 0:
            fcf_coverage = float(fcf) / float(cash_total)
        if fcf_coverage is not None:
            if fcf_coverage >= 1.5:
                cover_score += 35
                cover_parts.append(f"FCF/分红={fcf_coverage:.1f}x，覆盖充裕")
            elif fcf_coverage >= 1.0:
                cover_score += 25
                cover_parts.append(f"FCF/分红={fcf_coverage:.1f}x，覆盖无压力")
            elif fcf_coverage >= 0.8:
                cover_score += 12
                cover_parts.append(f"FCF/分红={fcf_coverage:.1f}x，覆盖偏紧")
            else:
                cover_score -= 5
                cover_parts.append(f"FCF/分红={fcf_coverage:.1f}x<1，需消耗存量现金")
        else:
            # 回退：用 OCF/净利代理
            if ocf_np is not None and float(ocf_np) >= 1.5:
                cover_score += 30
                cover_parts.append(f"OCF/净利={float(ocf_np):.2f}，造血充裕")
            elif ocf_np is not None and float(ocf_np) >= 1.0:
                cover_score += 20
                cover_parts.append(f"OCF/净利={float(ocf_np):.2f}，覆盖无压力")
            elif ocf_np is not None and float(ocf_np) >= 0.7:
                cover_score += 10
                cover_parts.append(f"OCF/净利={float(ocf_np):.2f}，覆盖偏紧")
        if cashflow_score is not None and float(cashflow_score) >= 70:
            cover_score += 8
            cover_parts.append(f"现金流质量{float(cashflow_score):.0f}分")
        # 连续高分红历史 → 覆盖信心加分
        if consec and int(consec) >= 15:
            cover_score += 3
        # FCF 覆盖分红缺口（轻扣分，不否决——OCF 仍充裕）
        if fcf_div_gap is not None:
            gap = float(fcf_div_gap)
            if gap < 0:
                cover_score -= 6
                fcf_yi = float(fcf) / 1e8 if fcf and float(fcf) > 1e6 else float(fcf or 0)
                cash_yi = float(cash_total) / 1e8 if cash_total and float(cash_total) > 1e6 else float(cash_total or 0)
                gap_yi = abs(gap) / 1e8 if abs(gap) > 1e6 else abs(gap)
                cover_parts.append(
                    f"FCF {fcf_yi:.0f}亿 < 现金分红 {cash_yi:.0f}亿，缺口{gap_yi:.0f}亿"
                )
            elif gap > 0 and fcf_coverage is None:
                cover_score += 5
                cover_parts.append(f"FCF覆盖分红有余")
        cover_score = max(30.0, min(cover_score, 95.0))
        cover_detail = "；".join(cover_parts) if cover_parts else "现金流覆盖数据不足"

        # 4. PE 历史分位（弱权重 10%，红利框架不作主锚）
        pct_parts: list[str] = []
        if pe_pct is not None:
            pp = float(pe_pct)
            pct_parts.append(f"PE分位{pp:.0f}%")
            if pp >= 90:
                pct_score = 42.0
            elif pp >= 75:
                pct_score = 50.0
            elif pp >= 50:
                pct_score = 58.0
            elif pp >= 30:
                pct_score = 70.0
            elif pp <= 10:
                pct_score = 90.0
            else:
                pct_score = 75.0
        else:
            pct_score = 55.0
        pct_detail = "、".join(pct_parts) + "（弱权重，红利框架不作主锚）" if pct_parts else "PE分位数据缺失（弱权重）"

        return {
            "股息率利差": {"score": spread_score, "detail": spread_detail},
            "分红确定性": {"score": det_score, "detail": certainty_detail},
            "分红现金覆盖": {"score": cover_score, "detail": cover_detail},
            "PE/PB分位": {"score": pct_score, "detail": pct_detail},
        }

    def _build_ddm(self, market: dict, meta: dict) -> dict[str, Any]:
        """
        DDM（戈登股利贴现模型）三情景估值，仅红利资产调用。
        D0 取 meta["dividend"]["d0"]（真实每股分红），回退 股息率×股价。
        """
        div_info = meta.get("dividend") or {}
        d0 = div_info.get("d0")
        price = market.get("price")
        dy = market.get("dividend_yield")
        if d0 is None or float(d0) <= 0:
            if dy is not None and price and float(price) > 0:
                d0 = float(dy) / 100.0 * float(price)
                d0_src = "股息率×股价回退"
            else:
                return {
                    "model": "DDM",
                    "d0": None,
                    "scenarios": [],
                    "current_price": price,
                    "note": "D0 数据缺失，跳过 DDM",
                }
        else:
            d0 = float(d0)
            d0_src = f"分红历史（{div_info.get('fy', '')}年度）"

        scenarios_def = [
            ("保守", 0.02, 0.08),
            ("中性", 0.03, 0.07),
            ("乐观", 0.03, 0.065),
        ]
        scenarios: list[dict[str, Any]] = []
        for name, g, r in scenarios_def:
            model = DDMModel(required_return=r, terminal_growth=g)
            ddm_result = model.value(d0)
            iv = ddm_result.get("intrinsic_value_per_share")
            if iv is not None and price and float(price) > 0:
                mos = round((float(iv) - float(price)) / float(price) * 100, 1)
            else:
                mos = None
            scenarios.append({
                "name": name,
                "terminal_growth": g,
                "required_return": r,
                "intrinsic_value_per_share": round(iv, 2) if iv is not None else None,
                "margin_of_safety_pct": mos,
            })

        return {
            "model": "DDM",
            "d0": round(d0, 2),
            "d0_source": d0_src,
            "scenarios": scenarios,
            "current_price": price,
            "note": (
                f"D0={d0:.2f}元（{d0_src}）；"
                "戈登模型 V=D0(1+g)/(r−g)；"
                "保守 g2%/r8%、中性 g3%/r7%、乐观 g3%/r6.5%"
            ),
            "forward_dividend_yield_pct": None,  # 前瞻口径需结合机构EPS预测，仅文字说明
            "forward_note": (
                "前瞻口径：2026中期已派0.98元（分红比例74%），若全年维持75%+分红率，"
                "结合机构2026年EPS中枢2.86-3.15元测算，全年分红约2.15-2.36元，"
                "按当前股价前瞻股息率约5.5%-6%，高于TTM 4.3%"
            ),
        }

    def _build_dcf(self, fin_df: pd.DataFrame, market: dict, meta: dict) -> dict[str, Any]:
        symbol = str(meta.get("symbol") or market.get("symbol") or "")
        # ── 提前检测高成长，用于基期FCF选择与参数适配 ──
        revenue_yoy_val = meta.get("revenue_yoy")
        is_high_growth = revenue_yoy_val is not None and float(revenue_yoy_val) >= 30
        base_fcf, fcf_src = self._base_fcf(fin_df, symbol, is_high_growth=is_high_growth)
        if base_fcf is None:
            return {
                "intrinsic_value_per_share": None,
                "note": "现金流数据缺失，跳过 DCF",
                "is_reliable": False,
                "fcf_source": fcf_src,
                "role": "skipped",
            }

        shares, shares_src = self._resolve_shares(fin_df, market, meta)
        growth = self._growth_rate(meta)
        debt_ratio = meta.get("debt_ratio")
        div_profile = classify_dividend_asset(
            dividend_yield_pct=market.get("dividend_yield"),
            pe_ttm=market.get("pe_ttm"),
            payout_ratio_pct=float(meta["payout_ratio_pct"]) if meta.get("payout_ratio_pct") is not None else None,
        )
        gm = meta.get("latest_gross_margin")
        if gm is None and not fin_df.empty and "gross_margin" in fin_df.columns:
            gms = fin_df["gross_margin"].dropna()
            if len(gms):
                gm = float(gms.iloc[-1])
        hq = classify_high_growth_quality(
            gross_margin_pct=float(gm) if gm is not None else None,
            profit_yoy_pct=meta.get("profit_yoy"),
        )
        is_hgq = bool(hq.get("is_high_growth_quality"))
        is_div = bool(div_profile.get("is_dividend_asset"))

        # 红利资产：不以 DCF 作定价锚，避免低 WACC 撑出夸张内在价值与相对估值打架
        if is_div:
            return _json_safe(
                {
                    "intrinsic_value_per_share": None,
                    "margin_of_safety_pct": None,
                    "role": "not_applicable_dividend",
                    "suppressed": True,
                    "is_reliable": False,
                    "fcf_source": fcf_src,
                    "shares_source": shares_src,
                    "dividend_asset": True,
                    "high_growth_quality": is_hgq,
                    "note": (
                        "红利/高股息资产不以 DCF 为核心定价依据（已隐藏内在价值与安全边际），"
                        "请以股息率相对十年国债利差及分红稳定性为主锚；"
                        f"资本成本口径 WACC≈{DIVIDEND_ASSET_WACC_PCT:.1f}%"
                    ),
                }
            )

        wacc = self._dynamic_wacc(
            debt_ratio,
            is_dividend_asset=False,
            is_high_growth_quality=is_hgq,
            interest_bearing_ratio=(
                float(meta["interest_bearing_ratio"])
                if meta.get("interest_bearing_ratio") is not None
                else None
            ),
            business_model=business_model_of(
                str(meta.get("name") or market.get("name") or ""),
                symbol,
                str(meta.get("industry") or ""),
            ),
        )
        # ── 高成长科技股（营收增速>30%）：DCF参数适配 ──
        # 半导体等行业用更高WACC（8-10%），永续增速3-5%（原2%）
        industry_str = str(meta.get("industry") or "")
        is_semiconductor = any(k in industry_str for k in ("半导体", "芯片", "集成电路", "封测", "光模块"))
        if is_high_growth:
            # 高成长：永续增速3-5%
            terminal = 0.04 if is_semiconductor else 0.035
            # 半导体行业WACC用8-10%（原可能7%偏低）
            if is_semiconductor and wacc < 0.08:
                wacc = 0.09
        elif is_hgq:
            terminal = 0.03
        elif debt_ratio is not None and float(debt_ratio) <= 30:
            terminal = 0.025
        else:
            terminal = 0.02

        # 高成长科技股用三情景DCF，取中性值
        if is_high_growth:
            dcf = DCFModel(wacc=wacc, terminal_growth=terminal).value_three_scenarios(
                base_fcf=base_fcf,
                high_growth_rate=growth,
                transition_growth_rate=max(0.03, growth * 0.5),
                shares_outstanding=shares,
            )
        else:
            dcf = DCFModel(wacc=wacc, terminal_growth=terminal).value(
                base_fcf=base_fcf,
                high_growth_rate=growth,
                transition_growth_rate=max(0.03, growth * 0.5),
                shares_outstanding=shares,
            )
        dcf["fcf_source"] = fcf_src
        dcf["shares_source"] = shares_src
        dcf["role"] = "pessimistic_reference"
        base_note = (
            "DCF 为极端悲观情景参考，非核心定价依据；"
            "高成长科技股请优先参考相对估值、机构共识与品类扩张叙事"
        )
        if is_high_growth:
            base_note += (
                f"；高成长参数已适配：WACC≈{wacc*100:.1f}%，"
                f"永续增速{terminal*100:.1f}%，"
                f"基期FCF来源={fcf_src}"
            )
        if is_hgq:
            base_note += (
                f"；高毛利高成长赛道已用 WACC≈{HIGH_GROWTH_WACC_PCT:.1f}% / "
                f"扩张期增速上限，结果仍可能显著低于市场定价"
            )
        dcf["note"] = base_note
        dcf["dividend_asset"] = False
        dcf["high_growth_quality"] = is_hgq
        dcf["suppressed"] = False

        price = market.get("price")
        iv = dcf.get("intrinsic_value_per_share")
        if price and iv and float(price) > 0:
            dcf["margin_of_safety_pct"] = round((float(iv) - float(price)) / float(price) * 100, 1)
            # 偏离现价 3 倍以上视为不可信（常见原因：股本默认 10 亿 / 高成长叙事未计入）
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
                dcf["note"] = base_note + "；估值偏离现价过大，已标记不可信（勿作核心定价）"
            else:
                dcf["is_reliable"] = bool(shares_src != "default_1e9")
            # 高成长且安全边际极差：强制不可信，避免 36 元 vs 220 元误导
            if is_hgq and float(dcf.get("margin_of_safety_pct") or 0) < -50:
                dcf["is_reliable"] = False
                dcf["note"] = (
                    base_note
                    + "；相对现价安全边际极差，仅作极端悲观压力测试，请勿作为核心定价"
                )
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
