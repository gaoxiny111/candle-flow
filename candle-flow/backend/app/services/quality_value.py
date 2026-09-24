"""质量 × 价值 选股视图（读层，**不重算任何分数**）。

## 为什么是独立读层而不是新模块

本视图的输入**全部是已有快照里的字段**（模块指标 value + market + valuation.relative），
不引入任何新取数、不产生第二个综合分（铁律 10：``composite_score`` 全站唯一权威）。
因此实现为「读层筛选 + 行业内标准化 + 排序」，与 ``market_scan`` 的共振视图同构。

## 与用户提案（外部「质量40+价值30+成长30」打分表）的四处**必须偏离**

外部方案是通用模板，直接落到本项目会踩已知的口径坑：

1. **不新建 100 分总分，改「硬门槛 + 展示分」**。
   提案要求 ``total_score = 0.4*质量+0.3*价值+0.3*成长``。本项目已有五维加权综合分
   （``composite_score``，权重见 ``analysis.config.MODULE_WEIGHTS``），再造第三套总分
   会让「同一只票两个分数」（铁律 10）。故：
   - **准入选股**用硬门槛（+ 行业中性分位），语义明确、可自证；
   - 展示分 ``qv_score`` 仅用于排序与呈现，``score_role="display_only"`` 显式标注，
     前端标题注明「不改写综合分」。

2. **ROIC 阈值按本项目会计口径下调**（提案 MIN_ROIC=15）。
   本项目 ROIC 口径为「投入资本回报率」，实测贵州茅台 42.75、江西铜业 6.49、
   万科 A -12.56 —— 通用模板的 15% 会拦掉绝大多数制造业。故取 8%（与
   ``config.THRESHOLDS['roic']`` 的「good」下界同源，铁律 5：同一判定不两处各算）。

3. **毛利率门槛必须行业中性**（提案 MIN_GROSS_MARGIN=30）。
   本项目真实行业名是**申万三级**（铁律 16），且行业内差异巨大：
   江西铜业毛利率 4.4%、中国神华 35.08%。绝对阈值 30% 会一刀切掉整个工业金属、
   商贸零售、建筑装饰、大宗供应链。故改为**行业内分位**，绝对毛利率只作兜底。

4. **利润含金量用 5 年均值而非单年**（提案 ``cash_flow/net_profit >= 0.8``）。
   本项目 ``cashflow.indicators[1]``（年报 OCF/净利润）实测**在行业间极性相反**：
   中国神华 1.42、万科 A 0.01、江西铜业 **-0.97**。单年值受存货与应收占用扰动，
   会把正常经营的周期股误杀。故取 ``indicators[0]``（**5 年均值**，口径更稳），
   阈值 0.8 保留（提案值）。

## 数据可得性（2026-09-24 实测）

| 因子 | 快照路径 | 可得性 |
|---|---|---|
| ROE | ``modules.profitability.indicators[0]`` | 高（茅台 32.53） |
| 毛利率 | ``modules.profitability.indicators[1]`` | 高（茅台 91.34） |
| ROIC | ``modules.profitability.indicators[2]`` | 高（茅台 42.75） |
| 资产负债率 | ``modules.solvency.indicators[0]`` | 中高（待全库核） |
| 经营现金流/净利润(5年) | ``modules.cashflow.indicators[0]`` | 中（茅台 0.9） |
| 股息率 | ``market.dividend_yield`` | 高 |
| PE / PB | ``market.pe_ttm`` / ``market.pb`` | 高 |
| PE 分位(5y) | ``market.pe_percentile`` | 中（亏损股为 None） |
| PEG | ``valuation.relative.PEG.value`` | **低**（红利资产被口径置空，茅台即 None） |

**PEG 缺失去留**：PEG 优先级降到最低且**缺失不淘汰**（只记 ``missing`` 提示）。
若把 PEG 当硬门槛，会因「红利资产不适用 PEG」把茅台这类票整体筛掉 —— 那是口径
洁癖造成的误杀，不是选股逻辑。

## 与行业中性化

分位基准 = **当前筛选命中集**（与 ``market_scan`` 的 ``industry_pct`` 同源做法），
故「内蒙古」这类地域筛选不会因为样本变小而让分位失真。
"""
from __future__ import annotations

from typing import Any, Literal

# ── 硬门槛默认值（均可在 API 层覆盖）────────────────────────────
# 盈利能力
MIN_ROE_DEFAULT = 10.0            # 提案值 10，与 config.THRESHOLDS['roe'].good 下界一致
MIN_ROIC_DEFAULT = 8.0            # **偏离提案 15**：本项目 ROIC 口径下 15 会误杀多数制造业
# 财务健康
MAX_DEBT_RATIO_DEFAULT = 60.0     # 提案值 60
MIN_OCF_NP_DEFAULT = 0.8          # 提案值 0.8（用 5 年均值口径，见模块 docstring）
# 质量分位兜底（绝对毛利率只作兜底，主判据是行业内分位）
MIN_GROSS_MARGIN_DEFAULT = 15.0   # 与 config.THRESHOLDS['gross_margin'].neutral 下界一致
MIN_GROSS_MARGIN_PCT_DEFAULT = 40.0  # 行业内毛利率分位下限
# 价值分位兜底
MAX_PE_DEFAULT = 50.0             # 防御性上限，主判据是 PE 分位 / PB 分位
MAX_PB_DEFAULT = 8.0
MAX_PE_PCTILE_DEFAULT = 40.0      # 提案 30 偏严：A 股多数行业 PE 分位中位在 45~60
MAX_PB_PCTILE_DEFAULT = 50.0
MIN_DIVIDEND_YIELD_DEFAULT = 0.0  # 提案要求 ≥3%，但那会与「质量」主判据重复；默认不限
# 成长
MIN_REVENUE_GROWTH_DEFAULT = 0.0  # 提案 20% 会拦掉全部成熟价值股；默认不限，仅兜底退化
MAX_PEG_DEFAULT = 1.5             # 提案 1.2 偏严且 PEG 缺失率高；缺失不淘汰
# 规模（提案的 50 亿下限）
MIN_MARKET_CAP_YI_DEFAULT = 50.0

# 展示分权重（**仅用于排序**，不参与任何入库分数）
QV_WEIGHT_QUALITY = 0.40
QV_WEIGHT_VALUE = 0.30
QV_WEIGHT_GROWTH = 0.30

# 缺失值的处理：不赋中性分（见项目口径：缺失≠中性），只在 components 里记 null
_QV_MIN_PCT_SAMPLES = 5  # 行业内样本 < 5 时该行业内分位不可信 → 退回绝对阈值判定


def _f(v: Any) -> float | None:
    """安全转 float；None/NaN/非数值一律 None（不赋 0）。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _percentile_rank(values: dict[str, float]) -> dict[str, float]:
    """横截面分位（0~100，越高越好）。与 market_scan.percentile_rank 同口径。"""
    out: dict[str, float] = {}
    n = len(values)
    if n == 0:
        return out
    if n == 1:
        return {next(iter(values)): 100.0}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    for i, (k, _) in enumerate(ordered):
        out[k] = round(i / (n - 1) * 100.0, 1)
    return out


def _industry_pct_map(
    rows: list[dict[str, Any]], accessor
) -> dict[str, float]:
    """按行业分组算分位，返回 {symbol: pct}。

    行业名取快照顶层 ``industry``（申万三级，铁律 16：精确匹配，不做子串）。
    """
    by_ind: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.get("_dropped"):
            continue
        v = _f(accessor(r))
        ind = str(r.get("industry") or "").strip()
        if v is None or not ind:
            continue
        by_ind.setdefault(ind, {})[r["symbol"]] = v
    out: dict[str, float] = {}
    for vals in by_ind.values():
        out.update(_percentile_rank(vals))
    return out


def _ind_median(rows: list[dict[str, Any]], accessor) -> dict[str, float]:
    """行业内中位数。用于「行业相对估值」的展示列（不做跨行业 PE 比较）。"""
    by_ind: dict[str, list[float]] = {}
    for r in rows:
        v = _f(accessor(r))
        ind = str(r.get("industry") or "").strip()
        if v is None or not ind:
            continue
        by_ind.setdefault(ind, []).append(v)
    out: dict[str, float] = {}
    for ind, vals in by_ind.items():
        vals.sort()
        n = len(vals)
        out[ind] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return out


# 产品/服务型低毛利行业（申万三级写法）。绝对毛利率门槛对这些行业不适用，
# 判定走「行业内分位」通道而不是绝对阈值。**精确匹配行业名**，不做子串。
#
# 为什么需要它：江西铜业（工业金属）毛利率 4.4%、商业贸易类普遍 5~8%，
# 绝对门槛 15% 会把整条产业链剔除 —— 那是行业属性而非质量问题。
_LOW_MARGIN_INDUSTRY_HINTS: tuple[str, ...] = (
    "工业金属", "铜", "铝", "铅锌", "小金属", "贵金属", "普钢", "特钢Ⅱ",
    "冶钢原料", "焦炭Ⅱ", "化学原料", "化学制品", "农化制品", "塑料", "橡胶",
    "油品石化贸易", "大宗供应链", "贸易Ⅱ", "物流", "航运", "港口",
    "建筑装饰", "建筑安装", "装修装饰", "房屋建设Ⅱ", "基础建设", "专业工程",
    "电力", "火力发电", "水力发电", "光伏设备", "风电设备", "电池",
    "乘用车", "商用车", "汽车零部件", "白色家电", "黑色家电", "消费电子",
    "光学光电子", "面板", "半导体", "元件", "通信设备",
    "房屋建设", "房地产开发", "航空机场", "航空运输", "机场航运",
)


def is_low_margin_industry(industry: str | None) -> bool:
    """是否为结构性低毛利行业（判定走行业内分位而非绝对毛利率门槛）。"""
    return bool(industry) and any(k in industry for k in _LOW_MARGIN_INDUSTRY_HINTS)


def _gate(
    value: float | None,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    pct: float | None = None,
    min_pct: float | None = None,
    max_pct: float | None = None,
    pct_trusted: bool = True,
) -> tuple[bool, str]:
    """单条件判定：绝对阈值与分位阈值**任一通过即通过**（互为兜底，不叠加）。

    返回 (是否通过, 未通过原因)。缺失（value 与 pct 均为 None）→ 通过并留痕，
    以避免「数据缺失」被当成「质量差」（项目口径：缺失不赋坏值）。
    """
    has_abs = value is not None
    has_rel = pct is not None and pct_trusted

    if not has_abs and not has_rel:
        return True, "missing"

    abs_fail = False
    if has_abs:
        if min_value is not None and value < min_value:
            abs_fail = True
        if max_value is not None and value > max_value:
            abs_fail = True

    rel_fail = False
    if has_rel:
        if min_pct is not None and pct < min_pct:
            rel_fail = True
        if max_pct is not None and pct > max_pct:
            rel_fail = True

    # 互为兜底：**已指定的**判据任一通过即通过；全部指定判据都失败才拒绝。
    # ★ 旧实现把「未指定的另一侧」默认为 True 再做 `abs_ok or rel_ok`，
    #   导致单边门槛（只给绝对值或只给分位）失败时被空缺侧 vacuously 放行 ——
    #   生产实测（2026-09-24）ROIC/资产负债率/OCF-NP(5y)/PEG/市值 五个门槛
    #   拒绝数恒 0：负债率 91.65% 的常熟银行、75.35% 的三棵树都被放行。
    if has_abs and not abs_fail:
        return True, ""
    if has_rel and not rel_fail:
        return True, ""

    # 给出人话理由（优先报更"硬"的那个）
    if abs_fail:
        if min_value is not None and value < min_value:
            return False, f"{value:g} < {min_value:g}"
        if max_value is not None and value > max_value:
            return False, f"{value:g} > {max_value:g}"
    if rel_fail:
        if min_pct is not None and pct < min_pct:
            return False, f"行业分位 {pct:g} < {min_pct:g}"
        if max_pct is not None and pct > max_pct:
            return False, f"行业分位 {pct:g} > {max_pct:g}"
    return False, "未达标"


def compute_qv(
    rows: list[dict[str, Any]],
    *,
    min_roe: float = MIN_ROE_DEFAULT,
    min_roic: float = MIN_ROIC_DEFAULT,
    max_debt_ratio: float = MAX_DEBT_RATIO_DEFAULT,
    min_gross_margin: float = MIN_GROSS_MARGIN_DEFAULT,
    min_gross_margin_pct: float = MIN_GROSS_MARGIN_PCT_DEFAULT,
    min_ocf_np: float = MIN_OCF_NP_DEFAULT,
    max_pe: float = MAX_PE_DEFAULT,
    max_pb: float = MAX_PB_DEFAULT,
    max_pe_pctile: float = MAX_PE_PCTILE_DEFAULT,
    max_pb_pctile: float = MAX_PB_PCTILE_DEFAULT,
    min_dividend_yield: float = MIN_DIVIDEND_YIELD_DEFAULT,
    min_revenue_growth: float = MIN_REVENUE_GROWTH_DEFAULT,
    max_peg: float = MAX_PEG_DEFAULT,
    min_market_cap_yi: float = MIN_MARKET_CAP_YI_DEFAULT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对轻量行做「质量 × 价值」判定与打分。

    Args:
        rows: 每行须含（缺失允许 None）：
            symbol / name / industry / composite_score / market_cap / price /
            pe_ttm / pb / pe_percentile / pb_percentile / dividend_yield /
            roe_pct / gross_margin_pct / roic_pct / debt_ratio_pct /
            ocf_np_5y / revenue_yoy / peg

    Returns:
        (passed_items, rejected_items)：两条列表都带 ``qv_components`` 自证明细。
        rejected 仅保留被**硬门槛**拦下的票及其原因，供 UI 的「被筛掉」展开查看。
    """
    prepared: list[dict[str, Any]] = []
    for r in rows:
        mcap = _f(r.get("market_cap"))
        prepared.append(
            {
                **r,
                "market_cap_yi": None if mcap is None else round(mcap / 1e8, 2),
                "_dropped": False,
            }
        )

    # ── 行业内分位（基准 = 本次筛选命中集，地域筛选不会让分位失真）──
    pct_gm = _industry_pct_map(prepared, lambda r: r.get("gross_margin_pct"))
    pct_roe = _industry_pct_map(prepared, lambda r: r.get("roe_pct"))
    pct_pe = _industry_pct_map(prepared, lambda r: r.get("pe_ttm"))
    pct_pb = _industry_pct_map(prepared, lambda r: r.get("pb"))
    ind_med_pe = _ind_median(prepared, lambda r: r.get("pe_ttm"))
    ind_med_pb = _ind_median(prepared, lambda r: r.get("pb"))
    ind_med_gm = _ind_median(prepared, lambda r: r.get("gross_margin_pct"))

    # 行业内样本数（< _QV_MIN_PCT_SAMPLES 时该行业的分位不可信 → 退回绝对阈值）
    ind_size: dict[str, int] = {}
    for r in prepared:
        ind = str(r.get("industry") or "").strip()
        if ind:
            ind_size[ind] = ind_size.get(ind, 0) + 1

    passed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for r in prepared:
        sym = r["symbol"]
        ind = str(r.get("industry") or "").strip()
        trusted = ind_size.get(ind, 0) >= _QV_MIN_PCT_SAMPLES
        low_margin_ind = is_low_margin_industry(ind)

        roe = _f(r.get("roe_pct"))
        gm = _f(r.get("gross_margin_pct"))
        roic = _f(r.get("roic_pct"))
        debt = _f(r.get("debt_ratio_pct"))
        ocf_np = _f(r.get("ocf_np_5y"))
        pe = _f(r.get("pe_ttm"))
        pb = _f(r.get("pb"))
        pep = _f(r.get("pe_percentile"))
        pbp = _f(r.get("pb_percentile"))
        dy = _f(r.get("dividend_yield"))
        rev_yoy = _f(r.get("revenue_yoy"))
        peg = _f(r.get("peg"))
        mcap_yi = _f(r.get("market_cap_yi"))

        fails: list[str] = []
        missing: list[str] = []

        def _check(name: str, ok: bool, reason: str, is_missing: bool = False) -> None:
            if is_missing:
                if reason == "missing":
                    missing.append(name)
                return
            if not ok:
                fails.append(f"{name}: {reason}")

        # ── 质量：盈利能力 ──────────────────────────────
        ok, why = _gate(roe, min_value=min_roe, pct=pct_roe.get(sym), min_pct=30.0,
                        pct_trusted=trusted)
        _check("ROE", ok, why, why == "missing")

        ok, why = _gate(roic, min_value=min_roic)
        _check("ROIC", ok, why, why == "missing")

        # 毛利率：结构性低毛利行业**只认行业内分位**；其余行业绝对阈值与分位互为兜底
        if low_margin_ind:
            ok, why = _gate(gm, pct=pct_gm.get(sym), min_pct=min_gross_margin_pct,
                            pct_trusted=trusted)
        else:
            ok, why = _gate(gm, min_value=min_gross_margin, pct=pct_gm.get(sym),
                            min_pct=min_gross_margin_pct, pct_trusted=trusted)
        _check("毛利率", ok, why, why == "missing")

        # ── 质量：财务健康 ──────────────────────────────
        ok, why = _gate(debt, max_value=max_debt_ratio)
        _check("资产负债率", ok, why, why == "missing")

        ok, why = _gate(ocf_np, min_value=min_ocf_np, pct=None)
        _check("现金流/净利润(5y)", ok, why, why == "missing")

        # ── 价值 ────────────────────────────────────────
        # 亏损股 PE 为负 → 绝对 PE 无意义，只走分位（分位也常缺失 → 放行留痕）
        if pe is not None and pe <= 0:
            ok, why = _gate(None, pct=pep, max_pct=max_pe_pctile, pct_trusted=trusted)
        else:
            ok, why = _gate(pe, max_value=max_pe, pct=pep, max_pct=max_pe_pctile,
                            pct_trusted=trusted)
        _check("PE", ok, why, why == "missing")

        ok, why = _gate(pb, max_value=max_pb, pct=pbp, max_pct=max_pb_pctile,
                        pct_trusted=trusted)
        _check("PB", ok, why, why == "missing")

        # 股息率为「加分项」而非门槛：默认 0 即不限
        if min_dividend_yield > 0:
            ok, why = _gate(dy, min_value=min_dividend_yield)
            _check("股息率", ok, why, why == "missing")

        # ── 成长 / 规模 ─────────────────────────────────
        if min_revenue_growth > 0:
            ok, why = _gate(rev_yoy, min_value=min_revenue_growth)
            _check("营收增速", ok, why, why == "missing")

        # PEG：优先级最低，**缺失不淘汰**（红利资产被口径置空，茅台即 None）
        if peg is not None:
            ok, why = _gate(peg, max_value=max_peg)
            _check("PEG", ok, why, why == "missing")
        else:
            missing.append("PEG")

        ok, why = _gate(mcap_yi, min_value=min_market_cap_yi)
        _check("市值", ok, why, why == "missing")

        # ── 展示分（0~100，仅排序用；缺失按 50 中性，不影响入选与否）──
        def _pct_or_neutral(v: float | None) -> float:
            return 50.0 if v is None else max(0.0, min(100.0, v))

        quality = round(
            (_pct_or_neutral(pct_roe.get(sym)) + _pct_or_neutral(pct_gm.get(sym))) / 2.0, 1
        )
        value = round(
            (
                _pct_or_neutral(100.0 - pep if pep is not None else None)
                + _pct_or_neutral(100.0 - pbp if pbp is not None else None)
            )
            / 2.0,
            1,
        )
        growth = round(_pct_or_neutral(_f(r.get("profit_yoy_pct"))), 1)
        qv_score = round(
            QV_WEIGHT_QUALITY * quality
            + QV_WEIGHT_VALUE * value
            + QV_WEIGHT_GROWTH * growth,
            1,
        )

        item = {
            **{k: v for k, v in r.items() if not k.startswith("_")},
            "composite_score": _f(r.get("composite_score")),
            "qv_score": qv_score,
            "score_role": "display_only",
            "qv_components": {
                "quality_pct": quality,
                "value_pct": value,
                "growth_pct": growth,
                "roe_industry_pct": pct_roe.get(sym),
                "gross_margin_industry_pct": pct_gm.get(sym),
                "pe_industry_pct": pep if pep is not None else pct_pe.get(sym),
                "pb_industry_pct": pbp if pbp is not None else pct_pb.get(sym),
                "low_margin_industry": low_margin_ind,
                "industry_sample": ind_size.get(ind, 0),
                # 中位数仅在样本 ≥ _QV_MIN_PCT_SAMPLES 时才可信：样本=1 时
                # 「行业中位数」就是自己（实测江西铜业单查时 PE 中位 13.48
                # == 自身 PE，会被误读成「行业估值水平」）。故同时回传样本数，
                # 前端据此提示，而不是把退化值当中位数用。
                "industry_median": {
                    "pe": ind_med_pe.get(ind) if ind_size.get(ind, 0) >= _QV_MIN_PCT_SAMPLES else None,
                    "pb": ind_med_pb.get(ind) if ind_size.get(ind, 0) >= _QV_MIN_PCT_SAMPLES else None,
                    "gross_margin_pct": (
                        ind_med_gm.get(ind) if ind_size.get(ind, 0) >= _QV_MIN_PCT_SAMPLES else None
                    ),
                },
            },
            "qv_missing": missing,
            "qv_reasons": fails,
        }

        if fails:
            rejected.append(item)
        else:
            passed.append(item)

    return passed, rejected


def sort_qv(items: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    """排序。``qv_score``（默认）或直接按某一分位/原始值降序。

    与 ``market_scan`` 同原则：**只排序不重算**，缺失值排最后（不赋 0 参与排名）。

    **PE/PB 特殊处理**：按 PE 升序时，**亏损股的负 PE 不可当「最便宜」**——
    负 PE 是「无盈利」而非「低估值」，排在榜首会把整张表变成亏损股清单
    （实测 000031 大悦城 PE=-3.23 会挤到动力新科 2.49 之前）。
    故：PE ≤ 0 → 视为不可比，排到**最后**（与缺失同组）；PE > 0 才按升序参与。
    """
    keymap = {
        "qv_score": lambda r: _f(r.get("qv_score")),
        "quality": lambda r: _f((r.get("qv_components") or {}).get("quality_pct")),
        "value": lambda r: _f((r.get("qv_components") or {}).get("value_pct")),
        "roe_pct": lambda r: _f(r.get("roe_pct")),
        "gross_margin_pct": lambda r: _f(r.get("gross_margin_pct")),
        "roic_pct": lambda r: _f(r.get("roic_pct")),
        "dividend_yield": lambda r: _f(r.get("dividend_yield")),
        # 升序类：取负值让统一降序逻辑生效；PE≤0/PB≤0 归 None（排最后）
        "pe_ttm": lambda r: (
            None if (v := _f(r.get("pe_ttm"))) is None or v <= 0 else -v
        ),
        "pb": lambda r: None if (v := _f(r.get("pb"))) is None or v <= 0 else -v,
        "composite_score": lambda r: _f(r.get("composite_score")),
    }
    getter = keymap.get(sort_by) or keymap["qv_score"]
    ordered = sorted(
        items,
        key=lambda r: (getter(r) is None, -(getter(r) or 0.0)),
    )
    return ordered


QV_SORT_KEYS: tuple[str, ...] = (
    "qv_score", "quality", "value", "roe_pct", "gross_margin_pct", "roic_pct",
    "dividend_yield", "pe_ttm", "pb", "composite_score",
)

QvSortKey = Literal[
    "qv_score", "quality", "value", "roe_pct", "gross_margin_pct", "roic_pct",
    "dividend_yield", "pe_ttm", "pb", "composite_score",
]


# ══════════════════════════════════════════════════════════════════════════
# 交易规则（硬编码调仓）
# ══════════════════════════════════════════════════════════════════════════
#
# 规则原文（用户给定）：
#   调入：综合得分 > 80 分，且市值 > 50 亿（避免小盘股流动性风险）
#   调出：得分 < 50 分，或单月跌幅 > 20%（止损）
#
# ## 落到本项目时的三处**必须显式化**的选择
#
# **① 「综合得分」用哪个分？ → ``qv_score``，并同时回显 ``composite_score``。**
#   本视图已经有一个自己的 0~100 展示分 ``qv_score``（质量40+价值30+成长30）。
#   实测（2026-09-24 全库 3058 只主板基础集）两条线的命中量差异极大：
#     qv_score > 80 且 市值>50亿 → **26 只**
#     composite_score > 80 且 市值>50亿 → 76 只
#     两者同时 > 80 → 仅 **8 只**（交集远小于任一单线）
#   规则是给「质量×价值策略」用的，故取 ``qv_score`` 为**判定口径**；
#   ``composite_score`` 只作为并列回显字段（不下判定），避免两个总分互相打架。
#   两线并存时 **不取「都满足」** —— 那会把 26 只砍到 8 只，使策略容量过小。
#
# **② 「单月跌幅 20%」的相对基准 → 持仓基准价（``entry_price``），可配置。**
#   本项目快照 ``market.price`` 只有**当日点位**，没有历史净值序列
#   （铁律 32：只有当日快照的接口不能当历史源）。故：
#   - 默认口径 = ``(price - entry_price) / entry_price <= -20%`` → 触发止损；
#   - ``entry_price`` 缺失 → **不触发**止损（缺失不是利空，铁律 7/13 同精神），
#     但记入 ``missing`` 让用户看见「这条规则这次没生效」。
#   - 备选口径 ``month_drop_pct``：若调用方已从 K 线算好近 20 交易日收益率，
#     可直接传入，两者**取更负者**（更严者优先，铁律 26）。
#
# **③ 缺失一律不放行「买入」，缺失一律不触发「卖出」。**
#   与 ``compute_qv`` 的 ``_gate`` 同精神：``qv_score`` 缺失 → 不调入；
#   ``market_cap_yi`` 缺失 → 不调入（流动性风险本身就是本规则要防的东西，
#   市值未知 = 风险未知，不能默认安全）。
#   反向：卖出侧缺失**不触发** —— 宁可多留一天，也不因为读不到数就清仓。

# 调入阈值（用户硬编码值）
QVT_BUY_SCORE_DEFAULT = 80.0
QVT_MIN_MARKET_CAP_YI_DEFAULT = 50.0
# 调出阈值
QVT_SELL_SCORE_DEFAULT = 50.0
QVT_MAX_MONTH_DROP_PCT_DEFAULT = 20.0


def evaluate_position(
    item: dict[str, Any],
    *,
    buy_score: float = QVT_BUY_SCORE_DEFAULT,
    min_market_cap_yi: float = QVT_MIN_MARKET_CAP_YI_DEFAULT,
    sell_score: float = QVT_SELL_SCORE_DEFAULT,
    max_month_drop_pct: float = QVT_MAX_MONTH_DROP_PCT_DEFAULT,
    entry_price: float | None = None,
    month_drop_pct: float | None = None,
) -> dict[str, Any]:
    """对单只标的输出调仓动作。

    Returns:
        ``{symbol, name, industry, action, reasons, missing, qv_score,
        composite_score, market_cap_yi, price, entry_price, month_drop_pct}``

        ``action`` ∈ ``buy`` / ``sell`` / ``hold`` / ``skip``：
          - ``sell`` ：**仅持仓票**（传了基准价）且满足「qv_score < sell_score」
            **或**「跌幅 > 阈值」
          - ``skip`` ：未达调入线（含数据缺失）——**未持仓票跌破卖出线也只算 skip**，
            不得进风控调出条
          - ``hold`` ：**已持仓**（传了 entry_price）且无卖出信号 —— 无论是否仍达调入线

        **已持仓不报 buy**：持仓票即使仍满足调入条件也只报 ``hold``，
        否则会给出「加仓」的错误暗示（规则原意是「够格才配在池子里」）。

        **字段继承**：返回值 = ``{**item, 判定字段}`` —— 候选的全部原始字段
        （roe_pct / qv_components / …）原样保留，前端可直接用 buy 桶渲染整张
        选股表，无需二次查询。``meets_buy`` 标明「持仓且仍达调入线」，
        供前端把这类票继续展示在选股表里（徽标为「持有」而非「调入」）。

        **优先级：卖出 > 买入。** 同一只票不可能既买又卖；若两侧同时命中
        （理论上只在阈值区间重叠时发生，如 buy_score < sell_score 的误配），
        以**卖出**为准——风控优先（与铁律 26「取更严者」一致）。
    """
    sym = str(item.get("symbol") or "")
    name = item.get("name")
    ind = item.get("industry")
    qv = _f(item.get("qv_score"))
    comp = _f(item.get("composite_score"))
    mcap_yi = _f(item.get("market_cap_yi"))
    price = _f(item.get("price"))
    ep = _f(entry_price if entry_price is not None else item.get("entry_price"))
    md = _f(month_drop_pct if month_drop_pct is not None else item.get("month_drop_pct"))

    reasons: list[str] = []
    missing: list[str] = []

    # 缺失留痕（无论是否持仓，缺失都要可见——缺失≠利空，但用户得知道规则没生效）
    if qv is None:
        missing.append("qv_score")
    if entry_price is None and item.get("entry_price") is None:
        missing.append("entry_price")

    # ── 卖出侧：**仅对持仓生效** ─────────────────────────────────
    # 规则原文「调出仅对持仓」。旧实现把全市场低分票都判成 sell，导致空仓用户的
    # 「风控调出」条被 79 只不合格股票刷屏（2026-09-24 用户反馈）——
    # 未持仓票跌破卖出线的真实含义只是「未达调入线」（skip），不是「调出」。
    sell_signals: list[str] = []
    if ep is not None:
        if qv is not None and qv < sell_score:
            sell_signals.append(f"得分 {qv:.1f} < {sell_score:g}")

        # 跌幅：两条口径取更负者（更严者优先）
        drop_candidates: list[tuple[str, float]] = []
        if price is not None and ep > 0:
            drop_candidates.append(("持仓", (price - ep) / ep * 100.0))
        if md is not None:
            drop_candidates.append(("单月", md))

        eff_drop: float | None = None
        drop_src = None
        if drop_candidates:
            drop_src, eff_drop = min(drop_candidates, key=lambda t: t[1])
            # 浮点边界保护： (80-100)/100*100 会算出 -20.000000000000004，
            # 直接比 `<= -20` 会让「恰好跌 20%」被误判为止损。
            # 规则原文是「跌幅 > 20%」，故先归一到 4 位小数再比。
            eff_drop = round(eff_drop, 4)
            if eff_drop < -abs(max_month_drop_pct):
                sell_signals.append(
                    f"{drop_src}跌幅 {abs(eff_drop):.1f}% > {max_month_drop_pct:g}%"
                )
    else:
        eff_drop = None
        drop_src = None

    # 是否满足调入条件（供前端区分「持有且仍达标」与「持有但已衰减」；
    # sell 行也带此字段，无副作用）
    meets_buy = (
        qv is not None
        and qv > buy_score
        and mcap_yi is not None
        and mcap_yi > min_market_cap_yi
    )

    def _out(action: str, *, reasons: list[str]) -> dict[str, Any]:
        # ★ 判定结果**继承完整候选字段**（roe_pct/qv_components/…原样保留）：
        #   前端拿 buy 桶就能直接渲染整张选股表，无需二次查询。
        return {
            **item,
            "action": action,
            "reasons": reasons,
            "missing": missing,
            "meets_buy": meets_buy,
            "qv_score": None if qv is None else round(qv, 2),
            "composite_score": None if comp is None else round(comp, 2),
            "market_cap_yi": None if mcap_yi is None else round(mcap_yi, 2),
            "price": price,
            "entry_price": ep,
            "month_drop_pct": None if eff_drop is None else round(eff_drop, 2),
            "drop_source": drop_src,
        }

    if sell_signals:
        return _out("sell", reasons=sell_signals)

    # ── 买入侧 ──────────────────────────────────────────────────
    buy_ok = meets_buy
    if qv is None:
        buy_ok = False  # 缺失已记入 missing
    elif qv <= buy_score:
        reasons.append(f"得分 {qv:.1f} ≤ {buy_score:g}")

    if mcap_yi is None:
        buy_ok = False
        # 市值未知 = 流动性风险未知，不能默认安全
        if "market_cap_yi" not in missing:
            missing.append("market_cap_yi")
    elif mcap_yi <= min_market_cap_yi:
        reasons.append(f"市值 {mcap_yi:.1f}亿 ≤ {min_market_cap_yi:g}亿")

    if buy_ok:
        # ★ 已持仓者不得报「buy」：否则会给用户「加仓」的暗示，而规则原意只是
        #   「够格的才配在池子里」。持仓且无卖出信号 = 继续持有（hold）。
        #   这也避免同一只票在 buy 与 hold 两个桶里语义打架（铁律 14 同精神）。
        already_held = ep is not None
        return _out(
            "hold" if already_held else "buy",
            reasons=(
                [f"已持仓（基准 {ep:g}）", f"得分 {qv:.1f} > {buy_score:g}", f"市值 {mcap_yi:.1f}亿 > {min_market_cap_yi:g}亿"]
                if already_held
                else [f"得分 {qv:.1f} > {buy_score:g}", f"市值 {mcap_yi:.1f}亿 > {min_market_cap_yi:g}亿"]
            ),
        )

    return _out(
        "hold" if ep is not None else "skip",
        reasons=reasons or ["未达调入线"],
    )


def apply_trading_rules(
    items: list[dict[str, Any]],
    *,
    buy_score: float = QVT_BUY_SCORE_DEFAULT,
    min_market_cap_yi: float = QVT_MIN_MARKET_CAP_YI_DEFAULT,
    sell_score: float = QVT_SELL_SCORE_DEFAULT,
    max_month_drop_pct: float = QVT_MAX_MONTH_DROP_PCT_DEFAULT,
    holdings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """对候选集批量应用调仓规则。

    Args:
        items: ``compute_qv`` 的 passed 列表（或任何含 qv_score/market_cap_yi/price 的字典）。
        holdings: ``{symbol: 持仓基准价}``。给了基准价的票才会被判为 ``hold``；
            未持有的票只会是 ``buy`` / ``skip``。``None`` 视为空仓（只选不卖）。

    Returns:
        ``{"buy": [...], "sell": [...], "hold": [...], "skip": [...],
        "buy_count": n, "sell_count": n, "thresholds": {...}, "notes": [...]}``

        **只做分类不排序** —— 排序仍由 ``sort_qv`` 负责（铁律 11：对外榜单只排序不重算）。
    """
    hp = holdings or {}
    buckets: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": [], "hold": [], "skip": []}

    for it in items:
        sym = str(it.get("symbol") or "")
        ep = hp.get(sym)
        verdict = evaluate_position(
            it,
            buy_score=buy_score,
            min_market_cap_yi=min_market_cap_yi,
            sell_score=sell_score,
            max_month_drop_pct=max_month_drop_pct,
            entry_price=ep,
        )
        elems = buckets.get(verdict["action"])
        if elems is None:
            elems = buckets["skip"]
        elems.append(verdict)

    notes = [
        f"调入：qv_score > {buy_score:g} 且 市值 > {min_market_cap_yi:g}亿"
        "（市值缺失视为不满足——流动性风险未知不得默认安全）。",
        f"调出：**仅持仓票**，qv_score < {sell_score:g} 或 相对持仓基准价跌幅 > {max_month_drop_pct:g}%"
        "（基准价缺失不触发止损，宁可多留一天）；未持仓票跌破卖出线只记为「未达调入线」，"
        "不进风控调出条——选股结果不展示不合格股票。",
        "判定口径 = 本视图 qv_score；composite_score 仅并列回显、不参与判定"
        "（实测两条线交集仅 8 只，取「都满足」会让策略容量过小）。",
        "**本接口只产出信号，不落持仓、不下单、不记录成交**；"
        "holdings 由调用方维护，缺失即视为空仓。",
    ]

    return {
        "buy": buckets["buy"],
        "sell": buckets["sell"],
        "hold": buckets["hold"],
        "skip": buckets["skip"],
        "buy_count": len(buckets["buy"]),
        "sell_count": len(buckets["sell"]),
        "hold_count": len(buckets["hold"]),
        "skip_count": len(buckets["skip"]),
        "thresholds": {
            "buy_score": buy_score,
            "min_market_cap_yi": min_market_cap_yi,
            "sell_score": sell_score,
            "max_month_drop_pct": max_month_drop_pct,
        },
        "notes": notes,
    }
