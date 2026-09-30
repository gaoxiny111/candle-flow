"""内在价值（Intrinsic Value）：先分类 → 再选模型 → 最后交叉验证。

定位（重要，先说清楚）
----------------------
本模块产出的是**展示用参考锚**，与 ``composite_valuation_score``（估值分，
最终进入 composite_score 加权）**严格分离**：它不参与任何评分、不构成第二个
总分。把这里的数字接进评分的任何改动都必须重走口径版本流程（bump
``SCORING_VERSION`` 并全库重建）。

三层结构
--------
1. **分类** ``classify_intrinsic_style``：周期 > 红利 > 成长 > 价值。
   目的就是「DCF 只给现金流可预测的票用」：红利走股息锚、周期走正常化利润 +
   PB-ROE 锚、成长/价值走 DCF。避免所有股票都被 DCF 定价 —— 周期股在景气高点
   的 FCF 被外推，正是最典型的「峰值 DCF 高估」。

   **强周期行业排他（2026-09-29 定案）**：强周期行业一律走周期锚，与股息率高低无关。
   原实现把「|净利同比| > 30%」当成周期**身份**的**必要条件** —— 于是「强周期行业 +
   当年盈利处中枢」的票（中远海控 601919 −23.5%、神华 +4.1%、宝钢 −6.3%）整批漏进
   股息锚。实测全库 723 只强周期行业里有 90 只被判红利，恰好是股息锚最不可靠的场景
   （高股息来自峰值利润、现金流不可持续）。现改为：**行业命中即强制周期**
   （``matched = cyclical_industry``）；盈利波动只用于标记周期**位置**
   （``cyclical_volatile``），不再决定归类。评分侧同源拦截见
   ``dividend_profile.classify_dividend_asset``（唯一实现，5 处调用点共用）。
2. **选模型** ``intrinsic_value_dividend`` / ``intrinsic_value_cyclical`` /
   ``intrinsic_value_dcf`` / ``intrinsic_value_ddm``。
3. **交叉验证** ``compute_intrinsic_value``：≥2 个模型才算「交叉验证」。
   全部可信 → 取**中位数**；只要含一个被标 ``reliable=False`` 的模型 → 取
   **min（保守侧）**，note 里写明「含高成长 DCF 等不可信模型」。只剩 1 个模型
   时明确标注「单模型，未交叉」；一个都没有则返回 None + 原因，绝不用默认值兜底。

   不可信模型**不被剔除出池**：整条剔出去会让高成长票退化成「只剩 DDM 的
   单模型」，而按原值参与中位数又会用无法外推的值抬高结果 —— 取 min 两头都避开。

单位约定（与项目其余模块一致，**全部用百分数**）
----------------------------------------------
``dividend_yield`` / ``payout_ratio_pct`` / ``profit_yoy`` / ``roe_ttm`` /
``pb_percentile`` 一律是百分数（4.2 表示 4.2%）；只有 ``dividend_per_share`` /
``eps_ttm`` / ``bvps`` / ``price`` 是「元/股」。不要混入 0.042 这种小数写法 ——
本项目历史上多次因 0.04 vs 4.0 的混用产生静默失效。

权威判定优先（铁律 9/10：同一判定不两处各算）
-------------------------------------------
``classify_intrinsic_style`` 接受 ``is_dividend_asset`` / ``is_growth_stock``
两个外部判定：**引擎传入时优先采信**（全系统只有一份红利定义、一份成长定义），
不传时才回退到本模块自带的原始阈值，从而保证本模块可独立运行与单测。
周期行业名单同理走参数注入，缺省时才惰性读取 engine 的
``STRONG_CYCLICAL_INDUSTRIES``（那是唯一来源，不得在此复制第二份）。
"""

from __future__ import annotations

import statistics
from typing import Any

STYLE_CYCLICAL = "cyclical"
STYLE_DIVIDEND = "dividend"
STYLE_GROWTH = "growth"
STYLE_VALUE = "value"

# 分类优先级（一处可调）。
# 注意：这里刻意采用「周期 > 红利 > 成长 > 价值」（2026-09-29 经确认保留，不回调
# 为「红利 > 周期」），而 ``cyclical_profile.classify_cyclical_stock`` 的文档注释
# 写的是「红利 > 周期 > 成长」、估值打分侧也是红利框架优先。两者只在
# 「既是强周期行业、当年盈利又大幅波动、同时还是红利资产」这一交集上分歧；
# 该交集正是股息锚最不可靠的场景：强周期行业在景气高点的股息率往往极高，
# 但高分红来自**峰值利润**、现金流不可持续，按红利优先会被高股息率「骗」出
# 虚高的内在价值。周期优先可先把它们按正常化利润 + PB-ROE 处理，股息锚至多
# 作为辅助参考（见 ``compute_intrinsic_value`` 周期分支的兜底注释）。
# 2026-09-29 定案：该优先级此前只在「|净利同比| > 30%」时才真正生效，导致强周期行业
# 里盈利平稳的票整批漏进红利分支；现已**取消该闸** —— 行业命中即强制走周期框架
# （``matched = cyclical_industry``），评分侧由 ``dividend_profile`` 的同源拦截保持一致。
STYLE_PRECEDENCE: tuple[str, ...] = (
    STYLE_CYCLICAL,
    STYLE_DIVIDEND,
    STYLE_GROWTH,
    STYLE_VALUE,
)

# ── 分类阈值 ────────────────────────────────────────────────────────────
# **不再是周期身份的闸门**（2026-09-29 取消）：行业 ∈ STRONG_CYCLICAL_INDUSTRIES
# 即强制走周期框架，本阈值只用于给周期**位置**打标记（盈利大幅波动 / 处中枢）。
# 历史教训：把它当必要条件时，中远海控（同比 −23.5%）、神华（+4.1%）、
# 宝钢（−6.3%）这类「强周期行业 + 当年盈利处中枢」的票整批漏进股息锚。
CYCLICAL_YOY_ABS_MIN_PCT = 30.0
DIVIDEND_YIELD_MIN_PCT = 4.0      # 红利：股息率 ≥ 4%
DIVIDEND_PAYOUT_MIN_PCT = 30.0    # 且分红率 ≥ 30%
GROWTH_YOY_MIN_PCT = 20.0         # 成长：净利同比 > 20%
GROWTH_SCORE_MIN = 60.0           # 且成长模块分 ≥ 60
GROWTH_EXCLUDE_YIELD_PCT = 4.0    # 排除「高股息 + PE<15」的低估值票被成长分支吃掉
GROWTH_EXCLUDE_PE_MAX = 15.0

# ── 模型参数 ────────────────────────────────────────────────────────────
CYCLICAL_NORMAL_PE = 10.0         # 周期正常化 PE 锚（跨周期中性利润 × 10）
CYCLICAL_PB_ROE_MULT = 15.0       # 合理 PB = ROE(小数) × 15，即 ROE 10% → PB 1.5
CYCLICAL_PB_FLOOR = 1.0           # 合理 PB 下限
CYCLICAL_PB_PCTL_HIGH = 70.0      # PB 历史分位 > 70% → 折价 20%
CYCLICAL_PB_PCTL_EXTREME = 85.0   # PB 历史分位 > 85% → 折价 40%
CYCLICAL_DISCOUNT_HIGH = 0.8
CYCLICAL_DISCOUNT_EXTREME = 0.6

DIVIDEND_ANCHOR_ALPHA = 0.02      # 股息锚定：要求回报率 = 国债 + 2% 风险溢价
DDM_ALPHA = 0.03                  # DDM：要求回报率 = 国债 + 3%
DDM_PAYOUT_MIN_PCT = 20.0         # 分红率过低 → 分红不稳定，DDM 不适用
DDM_PAYOUT_MAX_PCT = 90.0         # 分红率过高 → 不可持续
DDM_DEFAULT_GROWTH = 0.02         # 股利永续增速缺省
DCF_FCF_TO_EPS = 0.85             # 简化：FCF ≈ EPS × 85%（仅独立运行时使用）
DCF_DEFAULT_WACC = 0.09
DCF_DEFAULT_TERMINAL_GROWTH = 0.02
DCF_HIGH_GROWTH_CAP = 0.30
DCF_TRANSITION_CAP = 0.10
DCF_HIGH_YEARS = 3
DCF_MID_YEARS = 2


# ── 小工具 ──────────────────────────────────────────────────────────────
def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _engine_cyclical_industries() -> frozenset[str]:
    """周期行业名单的**唯一来源**在 engine（申万三级精确匹配）。

    惰性导入以避免 models ← engine 的循环依赖；engine 调用本模块时会
    显式传入该集合，因此这条回退路径只服务于独立运行/单测。
    """
    from app.analysis.engine import STRONG_CYCLICAL_INDUSTRIES

    return STRONG_CYCLICAL_INDUSTRIES


# ── 1. 分类 ─────────────────────────────────────────────────────────────
def classify_intrinsic_style_detail(
    row: dict[str, Any],
    *,
    is_dividend_asset: bool | None = None,
    is_growth_stock: bool | None = None,
    cyclical_industries: frozenset[str] | None = None,
) -> dict[str, Any]:
    """返回 ``{"style", "reason", "matched"}``；三态缺失一律留痕不猜。

    ``is_dividend_asset`` / ``is_growth_stock`` 为 None 时才用本模块的原始
    阈值（保证独立可用）；引擎传入时以引擎的权威判定为准（铁律 10）。
    """
    industry = str(row.get("industry") or "").strip()
    dy = _f(row.get("dividend_yield"))
    payout = _f(row.get("payout_ratio_pct"))
    if payout is None:
        payout = _f(row.get("payout_ratio"))
    profit_yoy = _f(row.get("profit_yoy"))
    if profit_yoy is None:
        profit_yoy = _f(row.get("net_profit_growth_yoy"))
    pe = _f(row.get("pe_ttm"))
    growth_score = _f(row.get("growth_score"))

    industries = (
        cyclical_industries if cyclical_industries is not None
        else _engine_cyclical_industries()
    )

    # 红利命中（三态：权威判定优先，None 时才用本模块阈值；False 表示明确不是）。
    # 在周期闸之前先算出来，供「周期锁」使用 —— 否则强周期红利股会先漏到②。
    _div_hit = is_dividend_asset is True or (
        is_dividend_asset is None
        and dy is not None
        and dy >= DIVIDEND_YIELD_MIN_PCT
        and payout is not None
        and payout >= DIVIDEND_PAYOUT_MIN_PCT
        and (pe is None or pe > 0)
    )

    # ① 周期优先：行业命中（**精确等于**申万三级行业名）→ 强制走周期框架。
    # 2026-09-29 定案：取消原「|净利同比| > 30%」硬闸。该闸曾被当作周期身份的
    # **必要条件**，于是「强周期行业 + 当年盈利处中枢」的票（中远海控 −23.5%、
    # 神华 +4.1%、宝钢 −6.3%）整批漏进股息锚 —— 恰是股息锚最不可靠的场景。
    # 利润波动幅度是**周期位置**的输入（高点/低点），不是「是不是周期股」的判据。
    # 名单与评分侧同源（engine.STRONG_CYCLICAL_INDUSTRIES），评分侧的同类拦截在
    # ``dividend_profile.classify_dividend_asset``（唯一实现），两层口径已对齐。
    if industry and industry in industries:
        _volatile = (
            profit_yoy is not None
            and abs(profit_yoy) > CYCLICAL_YOY_ABS_MIN_PCT
        )
        _lock_txt = (
            "；该行业同时被判为红利资产，但周期锁生效：股息锚仅作对照，不替代周期锚"
            if _div_hit
            else ""
        )
        if profit_yoy is None:
            # 三态留痕：周期位置无法判定（不按「波动为 0」免检），但仍强制归周期。
            return {
                "style": STYLE_CYCLICAL,
                "matched": "cyclical_industry_yoy_missing",
                "partial": True,
                "reason": (
                    f"强周期行业「{industry}」（申万三级精确命中）→ 强制走周期框架："
                    "正常化利润 + PB-ROE 双锚，禁用峰值 DCF。"
                    "净利同比缺失 → 周期位置未判定（已留痕），不改变周期归类"
                    + _lock_txt
                ),
            }
        _pos_txt = (
            f"净利同比 {profit_yoy:.1f}%（|同比|>{CYCLICAL_YOY_ABS_MIN_PCT:.0f}%）"
            "→ 盈利大幅波动，周期位置待判"
            if _volatile
            else f"净利同比 {profit_yoy:.1f}%（|同比|未过 "
            f"{CYCLICAL_YOY_ABS_MIN_PCT:.0f}%，盈利处中枢）"
            "→ 仅用于周期位置判断，不影响周期归类"
        )
        return {
            "style": STYLE_CYCLICAL,
            "matched": "cyclical_volatile" if _volatile else "cyclical_industry",
            "reason": (
                f"强周期行业「{industry}」（申万三级精确命中）→ 强制走周期框架："
                "正常化利润 + PB-ROE 双锚，禁用峰值 DCF。" + _pos_txt + _lock_txt
            ),
        }

    # ② 红利：权威判定优先
    if is_dividend_asset is True:
        return {
            "style": STYLE_DIVIDEND,
            "matched": "dividend_authoritative",
            "reason": "红利资产（引擎 classify_dividend_asset 判定）→ 走股息锚定价值，不用 DCF",
        }
    if is_dividend_asset is None and _div_hit:
        return {
            "style": STYLE_DIVIDEND,
            "matched": "dividend_threshold",
            "reason": (
                f"股息率 {dy:.1f}%≥{DIVIDEND_YIELD_MIN_PCT:.0f}% 且分红率 "
                f"{payout:.1f}%≥{DIVIDEND_PAYOUT_MIN_PCT:.0f}% → 股息锚定价值"
            ),
        }

    # ③ 成长：权威判定优先
    if is_growth_stock is True:
        return {
            "style": STYLE_GROWTH,
            "matched": "growth_authoritative",
            "reason": "成长股（引擎 classify_growth_stock 判定）→ 走 DCF，可叠加 DDM 交叉",
        }
    if is_growth_stock is None and profit_yoy is not None and profit_yoy > GROWTH_YOY_MIN_PCT:
        blocked_by_yield = (
            dy is not None and dy >= GROWTH_EXCLUDE_YIELD_PCT
            and pe is not None and 0 < pe < GROWTH_EXCLUDE_PE_MAX
        )
        if growth_score is not None and growth_score >= GROWTH_SCORE_MIN and not blocked_by_yield:
            return {
                "style": STYLE_GROWTH,
                "matched": "growth_threshold",
                "reason": (
                    f"净利同比 {profit_yoy:.1f}%>{GROWTH_YOY_MIN_PCT:.0f}% 且成长分 "
                    f"{growth_score:.0f}≥{GROWTH_SCORE_MIN:.0f} → 走 DCF"
                ),
            }
        if blocked_by_yield:
            return {
                "style": STYLE_VALUE,
                "matched": "growth_blocked_by_dividend",
                "reason": (
                    f"净利同比 {profit_yoy:.1f}% 但股息率 {dy:.1f}% 且 PE {pe:.1f}<"
                    f"{GROWTH_EXCLUDE_PE_MAX:.0f}（低估值高股息），不按成长股定价"
                ),
            }

    return {
        "style": STYLE_VALUE,
        "matched": "value_default",
        "reason": "未命中周期/红利/成长条件 → 按价值/普通股走 DCF，可叠加 DDM 交叉",
    }


def classify_intrinsic_style(
    row: dict[str, Any],
    *,
    is_dividend_asset: bool | None = None,
    is_growth_stock: bool | None = None,
    cyclical_industries: frozenset[str] | None = None,
) -> str:
    """分类入口，返回 ``cyclical`` / ``dividend`` / ``growth`` / ``value``。"""
    return classify_intrinsic_style_detail(
        row,
        is_dividend_asset=is_dividend_asset,
        is_growth_stock=is_growth_stock,
        cyclical_industries=cyclical_industries,
    )["style"]


# ── 2. 四类资产的内在价值模型 ────────────────────────────────────────────
def intrinsic_value_dividend(
    row: dict[str, Any],
    *,
    rf_pct: float | None = None,
    alpha: float = DIVIDEND_ANCHOR_ALPHA,
) -> tuple[float | None, str]:
    """红利资产：股息锚定价值（零增长资本化），不用 DCF。

    ``V = DPS / (国债收益率 + 风险溢价)``。``rf_pct`` 缺省取
    ``CN_10Y_BOND_YIELD_PCT``（全系统同一个国债口径，铁律 10）。
    """
    from app.analysis.dividend_profile import CN_10Y_BOND_YIELD_PCT

    rf = float(CN_10Y_BOND_YIELD_PCT if rf_pct is None else rf_pct)
    dps = _f(row.get("dividend_per_share"))
    dy = _f(row.get("dividend_yield"))
    if dps is None or dps <= 0 or dy is None or dy <= 0:
        return None, "分红数据不足，无法计算股息锚定价值"
    cost = rf / 100.0 + float(alpha)
    if cost <= 0:
        return None, "要求回报率非正，无法计算股息锚定价值"
    value = dps / cost
    spread = dy - rf
    return value, (
        f"红利锚定价值：每股股息 {dps:.2f} 元 ÷ (国债 {rf:.1f}% + 风险溢价 "
        f"{float(alpha) * 100:.1f}%) = {value:.2f} 元；当前股息率 {dy:.1f}%，"
        f"相对国债利差 {spread:.1f}pct（零增长资本化，不含股利成长）"
    )


def cyclical_anchors(row: dict[str, Any]) -> dict[str, Any]:
    """周期双锚明细（唯一实现，``intrinsic_value_cyclical`` 与组装层共用）。

    - 正常化 PE 锚：跨周期中性利润对应的 EPS × 正常化 PE
    - PB-ROE 锚：合理 PB = ROE × 15（下限 1.0），乘 BVPS
    - 取两者保守值，再按 PB 历史分位折价（周期位置修正）
    """
    eps_norm = _f(row.get("eps_normalized"))
    eps_src = "跨周期中性利润/股本"
    if eps_norm is None:
        eps_norm = _f(row.get("eps_5y_median"))
        eps_src = "近 5 年 EPS 中位数"
    if eps_norm is None:
        eps_norm = _f(row.get("eps_ttm"))
        eps_src = "最新 EPS（正常化数据缺失，回退）"

    bvps = _f(row.get("bvps"))
    roe = _f(row.get("roe_ttm"))
    pb_pctl = _f(row.get("pb_percentile"))

    v_pe: float | None = None
    if eps_norm is not None and eps_norm > 0:
        v_pe = eps_norm * CYCLICAL_NORMAL_PE

    pb_target: float | None = None
    if roe is not None and roe > 0:
        pb_target = max(CYCLICAL_PB_FLOOR, roe / 100.0 * CYCLICAL_PB_ROE_MULT)
    elif roe is not None:
        pb_target = CYCLICAL_PB_FLOOR  # ROE ≤ 0：只给净资产底线，不给溢价
    v_pb: float | None = None
    if bvps is not None and bvps > 0 and pb_target is not None:
        v_pb = bvps * pb_target

    vals = [v for v in (v_pe, v_pb) if v is not None]
    intrinsic: float | None = None
    discount = 1.0
    discount_label = ""
    if vals:
        intrinsic = min(vals)
        # 周期位置修正：先判极端档，否则 >70 的 0.8 会吃掉 >85 的 0.6（原式分支顺序有误）
        if pb_pctl is not None:
            if pb_pctl > CYCLICAL_PB_PCTL_EXTREME:
                discount = CYCLICAL_DISCOUNT_EXTREME
                discount_label = (
                    f"PB 历史分位 {pb_pctl:.0f}%>"
                    f"{CYCLICAL_PB_PCTL_EXTREME:.0f}%，周期高位折价 "
                    f"{(1 - discount) * 100:.0f}%"
                )
            elif pb_pctl > CYCLICAL_PB_PCTL_HIGH:
                discount = CYCLICAL_DISCOUNT_HIGH
                discount_label = (
                    f"PB 历史分位 {pb_pctl:.0f}%>"
                    f"{CYCLICAL_PB_PCTL_HIGH:.0f}%，周期偏高位折价 "
                    f"{(1 - discount) * 100:.0f}%"
                )
        intrinsic *= discount

    return {
        "eps_normalized": eps_norm,
        "eps_source": eps_src,
        "normal_pe": CYCLICAL_NORMAL_PE,
        "value_normalized_pe": v_pe,
        "bvps": bvps,
        "roe_ttm_pct": roe,
        "pb_target": pb_target,
        "value_pb_roe": v_pb,
        "anchor_used": (
            "normalized_pe" if v_pe is not None and (v_pb is None or v_pe <= v_pb)
            else ("pb_roe" if v_pb is not None else None)
        ),
        "pb_percentile": pb_pctl,
        "discount": discount,
        "discount_label": discount_label,
        "intrinsic_value_per_share": intrinsic,
    }


def intrinsic_value_cyclical(row: dict[str, Any]) -> tuple[float | None, str]:
    """周期资产：正常化 PE + PB-ROE 双锚取保守值，按 PB 分位折价；禁用峰值 DCF。"""
    a = cyclical_anchors(row)
    iv = a["intrinsic_value_per_share"]
    if iv is None:
        return None, "周期估值数据不足（正常化 EPS 与 BVPS 均不可得）"

    parts: list[str] = []
    if a["value_normalized_pe"] is not None:
        parts.append(
            f"正常化 EPS {a['eps_normalized']:.2f} 元（{a['eps_source']}）×"
            f"{a['normal_pe']:.0f}PE = {a['value_normalized_pe']:.2f} 元"
        )
    if a["value_pb_roe"] is not None:
        parts.append(
            f"PB 锚 合理PB {a['pb_target']:.2f}×BVPS {a['bvps']:.2f} = "
            f"{a['value_pb_roe']:.2f} 元"
        )
    parts.append(f"取保守值 {min(v for v in (a['value_normalized_pe'], a['value_pb_roe']) if v is not None):.2f} 元")
    if a["discount_label"]:
        parts.append(a["discount_label"])
    parts.append(f"周期内在价值≈{iv:.2f} 元")
    return iv, "周期估值（禁用峰值 DCF）：" + "；".join(parts)


def intrinsic_value_dcf(row: dict[str, Any]) -> tuple[float | None, str]:
    """成长/价值资产：三阶段 DCF（简化口径，仅供独立运行；引擎优先用真实 FCF 版）。"""
    eps = _f(row.get("eps_ttm"))
    if eps is None or eps <= 0:
        return None, "EPS 为负或为零，DCF 不适用"

    g_raw = _f(row.get("profit_yoy"))
    g1 = min(max((g_raw or 15.0) / 100.0, 0.0), DCF_HIGH_GROWTH_CAP)
    g2 = min(g1 * 0.5, DCF_TRANSITION_CAP)
    wacc = _f(row.get("wacc"))
    wacc = DCF_DEFAULT_WACC if wacc is None else wacc
    tg = _f(row.get("terminal_growth"))
    tg = DCF_DEFAULT_TERMINAL_GROWTH if tg is None else tg
    if wacc <= tg:
        return None, "WACC 需大于永续增长率"

    fcf = eps * DCF_FCF_TO_EPS
    pv = 0.0
    for y in range(1, DCF_HIGH_YEARS + 1):
        fcf *= 1 + g1
        pv += fcf / (1 + wacc) ** y
    for y in range(1, DCF_MID_YEARS + 1):
        fcf *= 1 + g2
        pv += fcf / (1 + wacc) ** (DCF_HIGH_YEARS + y)
    terminal = fcf * (1 + tg) / (wacc - tg)
    pv += terminal / (1 + wacc) ** (DCF_HIGH_YEARS + DCF_MID_YEARS)

    return pv, (
        f"DCF 三阶段：高增 {g1 * 100:.0f}%×{DCF_HIGH_YEARS}年 + 过渡 "
        f"{g2 * 100:.0f}%×{DCF_MID_YEARS}年 + 永续 {tg * 100:.0f}%，"
        f"WACC={wacc * 100:.1f}%，FCF≈EPS×{DCF_FCF_TO_EPS:.0%}，内在价值≈{pv:.2f} 元"
    )


def ddm_payout_allows(payout_pct: float | None) -> bool:
    """DDM 适用性闸门（**唯一实现**）：分红率必须落在 [20%, 90%]。

    三态语义：``payout_pct is None`` → 不可得 → 返回 False（不判、不给结论），
    调用方须在 note 里体现「分红率不可得」而非当成「不适用」。
    过低（<20%）说明分红不稳定，过高（>90%）不具备可持续性。
    """
    p = _f(payout_pct)
    if p is None:
        return False
    return DDM_PAYOUT_MIN_PCT <= p <= DDM_PAYOUT_MAX_PCT


def intrinsic_value_ddm(
    row: dict[str, Any],
    *,
    rf_pct: float | None = None,
    alpha: float = DDM_ALPHA,
) -> tuple[float | None, str | None]:
    """DDM（戈登）补充模型：仅用于分红稳定且分红率合理的票。"""
    from app.analysis.dividend_profile import CN_10Y_BOND_YIELD_PCT

    rf = float(CN_10Y_BOND_YIELD_PCT if rf_pct is None else rf_pct)
    dps = _f(row.get("dividend_per_share"))
    payout = _f(row.get("payout_ratio_pct"))
    if dps is None or dps <= 0:
        return None, None
    # 分红率闸门（含「不可得 → 不判」的三态语义）
    if not ddm_payout_allows(payout):
        return None, None
    r = rf / 100.0 + float(alpha)
    g = _f(row.get("dividend_growth"))
    g = DDM_DEFAULT_GROWTH if g is None else g
    if r <= g:
        return None, None
    value = dps * (1 + g) / (r - g)
    return value, (
        f"DDM：DPS {dps:.2f} 元，r={r * 100:.1f}%（国债 {rf:.1f}% + "
        f"{float(alpha) * 100:.1f}%），g={g * 100:.1f}%，价值≈{value:.2f} 元"
    )


# ── 3. 交叉验证与最终输出 ────────────────────────────────────────────────
def compute_intrinsic_value(
    row: dict[str, Any],
    *,
    dcf: dict[str, Any] | None = None,
    ddm: dict[str, Any] | None = None,
    is_dividend_asset: bool | None = None,
    is_growth_stock: bool | None = None,
    cyclical_industries: frozenset[str] | None = None,
) -> dict[str, Any]:
    """按 style 选模型 → 交叉验证 → 安全边际。

    ``dcf`` / ``ddm`` 允许调用方注入更权威的模型结果
    （形如 ``{"value": float|None, "note": str, "reliable": bool}``）：
    引擎会传自己那套「真实 FCF + 动态 WACC + 三情景」的 DCF，
    避免同一份 DCF 在本仓算两遍（铁律 10）。

    交叉规则（2026-09-29 修订：不可信模型「入池但取保守侧」）：
      - ≥2 个模型 → ``cross_model=True``：
          * 全部可信 → 取**中位数**，``cross_basis="median"``
          * 含任一 ``reliable=False`` → 取 **min（保守锚）**，
            ``cross_basis="conservative"``、``conservative=True``，note 写明
            「含高成长 DCF 等不可信模型」
      - 只有 1 个 → ``cross_model=False``，note 明确写「单模型，未交叉」
      - 0 个 → 返回 None + 原因（不兜底、不给默认值）

    为什么不可信模型不剔除：高成长股 DCF 被引擎标 ``is_reliable=False``
    （增速无法外推）是合理的风控，但不可靠 ≠ 不能入池 —— 整条剔出去会让这类
    票只剩 DDM 单模型（丢掉交叉验证），按原值入中位数又会用无法外推的高值
    抬高结果。取 min 则同时避免「丢交叉」和「被高值拉高」。

    DDM 候选（无论注入还是自算）一律受 ``ddm_payout_allows`` 分红率闸门约束。
    """
    detail = classify_intrinsic_style_detail(
        row,
        is_dividend_asset=is_dividend_asset,
        is_growth_stock=is_growth_stock,
        cyclical_industries=cyclical_industries,
    )
    style = detail["style"]
    price = _f(row.get("price"))

    models: dict[str, dict[str, Any]] = {}
    # 辅助参考（display_only，**不入 models、不参与交叉验证**）：目前只有周期股
    # 「命中周期但同时也是红利资产」时记下的股息锚，仅用于对照说明。
    auxiliary: dict[str, Any] = {}

    def _put(name: str, value: Any, note: str | None, reliable: bool = True) -> None:
        v = _f(value)
        if v is None or v <= 0:
            return
        models[name] = {"value": v, "note": note, "reliable": reliable}

    def _resolve_dcf() -> None:
        """DCF 候选：注入优先（引擎那套真实 FCF 版），否则按简化式自算。"""
        if dcf is not None and _f(dcf.get("value")) is not None:
            _put("dcf", dcf.get("value"), dcf.get("note"), bool(dcf.get("reliable", True)))
            return
        v, n = intrinsic_value_dcf(row)
        _put("dcf", v, n)

    def _resolve_ddm() -> None:
        """DDM 候选：分红率闸门统一走 ddm_payout_allows（唯一实现，注入值同受闸门约束）。"""
        if not ddm_payout_allows(row.get("payout_ratio_pct")):
            return
        if ddm is not None and _f(ddm.get("value")) is not None:
            _put("ddm", ddm.get("value"), ddm.get("note"), bool(ddm.get("reliable", True)))
            return
        v, n = intrinsic_value_ddm(row)
        _put("ddm", v, n)

    if style == STYLE_DIVIDEND:
        v, n = intrinsic_value_dividend(row)
        _put("dividend_anchor", v, n)
        _resolve_ddm()

    elif style == STYLE_CYCLICAL:
        anchors = cyclical_anchors(row)
        v, n = intrinsic_value_cyclical(row)
        _put("cyclical_anchor", v, n)
        if "cyclical_anchor" in models:
            models["cyclical_anchor"]["anchors"] = anchors
        # ── 兜底注释（2026-09-29 确认保留「周期 > 红利」优先级的落地说明）──
        # 若该股同时满足红利条件，股息锚仅作为辅助参考，不替代周期锚。
        # 周期股不用 DCF 定价（峰值 FCF 外推会高估），同理也不该用股息锚定价：
        # 景气高点的股息来自峰值利润、现金流不可持续，把它当永续年金做零增长
        # 资本化，算出的「内在价值」会虚高。故此处只把股息锚记进 auxiliary
        # 供人工对照 —— 它不入 models、不参与交叉验证、不影响任何分数。
        _aux_v, _aux_n = intrinsic_value_dividend(row)
        if _aux_v is not None:
            auxiliary["dividend_anchor"] = {
                "value": round(_aux_v, 2),
                "note": _aux_n,
                "reliable": False,
                "reason": (
                    "周期股不用股息锚定价：景气高点的高股息来自峰值利润、"
                    "现金流不可持续（仅作对照参考）"
                ),
            }

    else:  # growth / value
        _resolve_dcf()
        _resolve_ddm()

    base = {
        "style": style,
        "style_reason": detail["reason"],
        "style_matched": detail["matched"],
        "current_price": price,
        "display_only": True,
    }

    if not models:
        return {
            **base,
            "intrinsic_value_per_share": None,
            "cross_model": False,
            "cross_basis": None,
            "conservative": False,
            "model_count": 0,
            "models": {},
            "unreliable_models": [],
            "auxiliary": auxiliary,
            "margin_of_safety_pct": None,
            "note": "数据不足，无法计算内在价值",
        }

    # 2026-09-29 修订：不可信模型不再被剔除出池，改为「入池但取保守侧」。
    # 不可靠 ≠ 不能入池：整条剔出去会让高成长票退化成「只剩 DDM 的单模型」，
    # 而按原值参与中位数又会用无法外推的高值抬高结果 —— 取 min 两头都避开。
    vals = [m["value"] for m in models.values()]
    unreliable = [k for k, m in models.items() if not m["reliable"]]
    cross = len(models) >= 2

    if cross:
        if unreliable:
            final = min(vals)          # 保守锚：宁可低估，不可被不可信的高值拉高
            cross_basis = "conservative"
            _unrel_label = "、".join(
                "高成长 DCF" if k == "dcf" else k for k in unreliable
            )
            note = (
                f"交叉验证（含不可信模型 {_unrel_label}，取保守值 {final:.2f} 元；"
                f"参与：{'+'.join(models)}）"
            )
        else:
            final = statistics.median(vals)
            cross_basis = "median"
            note = "交叉验证（" + "+".join(models) + f" 中位数 {final:.2f} 元）"
    else:
        only = next(iter(models))
        final = vals[0]
        cross_basis = None
        note = f"单模型参考，未交叉（仅 {only}）"
        if unreliable:
            note += "；该模型被标记不可信，结果仅供参考"

    margin = (final / price - 1) * 100 if price and price > 0 else None
    return {
        **base,
        "intrinsic_value_per_share": round(final, 2),
        "cross_model": cross,
        "cross_basis": cross_basis,
        "conservative": cross_basis == "conservative",
        "model_count": len(models),
        "models": {
            k: {
                "value": round(m["value"], 2),
                "note": m["note"],
                "reliable": m["reliable"],
                **({"anchors": m["anchors"]} if "anchors" in m else {}),
            }
            for k, m in models.items()
        },
        "unreliable_models": unreliable,
        "auxiliary": auxiliary,
        "margin_of_safety_pct": round(margin, 1) if margin is not None else None,
        "note": note,
    }
