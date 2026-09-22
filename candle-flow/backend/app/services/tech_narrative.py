"""技术面叙述分析：由系统自有指标生成成文的技术面分析报告。

口径声明：
- 全部技术指标由本地 K 线计算（MA/MACD/RSI/KDJ/BOLL/量能/形态），与信号页、
  共振榜单同源（PatternEngine + evaluate_confluence），不含外部观点；
- 资金面段落来自东方财富成交结构口径（主力=超大单+大单，非同花顺 DDX）+ 交易所
  融资融券明细 + 龙虎榜，仅作展示，**不参与打分与筛选**；
- 北向资金个股日频持股自 2024-08 起停止披露 → 不含北向数据；
- 本报告为技术面口径，与 composite_score（基本面）互相独立，不合成总分。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.core.confluence import (
    _bollinger,
    _px,
    evaluate_confluence,
    macd_at,
    rsi_at,
    _sma,
)
from app.core.ma_cross import ma_cross_kind
from app.core.oscillators import stochastic_at
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.core.timeframe import weekly_trend_at
from app.core.western_levels import polarity_levels
from app.services.kline_service import KlineService
from app.services.stock_fund_flow import build_fund_flow, _fmt_money
from app.services.stock_universe import lookup_name

KLINE_LIMIT = 180
MIN_BARS = 60
PATTERN_RECENT_BARS = 10


def _pct(v: float) -> str:
    return f"{v * 100:+.2f}%"


def _fmt_date(k) -> str:
    return str(k.date)[:10]


def _ma_alignment(ma5, ma10, ma20) -> str:
    if ma5 is None or ma10 is None or ma20 is None:
        return "未知"
    if ma5 > ma10 > ma20:
        return "多头排列"
    if ma5 < ma10 < ma20:
        return "空头排列"
    return "纠缠"


def _rsi_zone(rsi: float) -> tuple[str, str]:
    if rsi < 20:
        return "极度超卖", "短期可能有技术性反弹需求，但超卖不等于反转，需量能与阳线确认"
    if rsi < 30:
        return "超卖", "下跌动能阶段性释放，反转仍需右侧确认"
    if rsi < 45:
        return "偏弱", "空头占优，反弹以修复性质对待"
    if rsi <= 55:
        return "中性", "多空平衡，方向待选择"
    if rsi <= 70:
        return "偏强", "多头占优，注意追高风险"
    return "超买", "短期涨幅过快，谨防回撤"


def _macd_state(closes: list[float], n: int) -> dict[str, Any]:
    m = macd_at(closes, n)
    if not m:
        return {"available": False}
    dif, dea, hist, prev_hist = m
    # 找最近一次金叉/死叉（近 10 日内）
    cross = ""
    for back in range(0, 11):
        idx = n - back
        if idx < 35:
            break
        cur = macd_at(closes, idx)
        prev = macd_at(closes, idx - 1)
        if not cur or not prev:
            continue
        cur_diff = cur[0] - cur[1]
        prev_diff = prev[0] - prev[1]
        when = "当日" if back == 0 else f"{back} 日前"
        if prev_diff < 0 <= cur_diff:
            cross = f"{when}金叉"
            break
        if prev_diff > 0 >= cur_diff:
            cross = f"{when}死叉"
            break
    side = "零轴上方" if dif > 0 else "零轴下方"
    if hist > prev_hist:
        bar_note = "柱体收敛/抬升"
    elif hist < prev_hist:
        bar_note = "柱体扩张/回落"
    else:
        bar_note = "柱体持平"
    return {
        "available": True,
        "dif": dif,
        "dea": dea,
        "hist": hist,
        "state": ("金叉状态，DIF 在 DEA 上方" if dif >= dea else "死叉状态，DIF 在 DEA 下方") + f"（{side}）",
        "cross": cross,
        "bar_note": bar_note,
    }


def _boll_position(close: float, ma: float | None, upper: float | None, lower: float | None) -> str:
    if ma is None or upper is None or lower is None:
        return "未知"
    if close >= upper * 0.995:
        return "触及上轨"
    if close <= lower * 1.005:
        return "触及下轨"
    if close >= ma:
        return "中轨上方"
    return "中轨下方"


def _volume_state(klines: Sequence, n: int) -> dict[str, Any]:
    vols = [float(k.volume or 0) for k in klines]
    if n < 20 or not any(vols):
        return {"available": False}
    vol5 = sum(vols[n - 4 : n + 1]) / 5
    vol20_prev = sum(vols[n - 24 : n - 4]) / 20 if n >= 25 else sum(vols[max(0, n - 19) : n - 4]) / max(n - 4 - max(0, n - 19), 1)
    if vol20_prev <= 0:
        return {"available": False}
    ratio = vol5 / vol20_prev
    if ratio >= 1.3:
        label = "明显放量"
    elif ratio >= 1.1:
        label = "温和放量"
    elif ratio <= 0.7:
        label = "明显缩量"
    elif ratio <= 0.9:
        label = "温和缩量"
    else:
        label = "量能平稳"
    return {"available": True, "vol5": vol5, "vol20_prev": vol20_prev, "ratio": ratio, "label": label}


def _recent_patterns(klines: Sequence, n: int) -> list[dict[str, Any]]:
    candles = kline_to_candles(klines)
    results = PatternEngine(min_score=60.0).scan(candles)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in results:
        if r.candle_index < n - PATTERN_RECENT_BARS + 1 or r.candle_index > n:
            continue
        key = (r.pattern_name, r.direction, _fmt_date(klines[r.candle_index]))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "date": _fmt_date(klines[r.candle_index]),
                "direction": r.direction,
                "name": r.pattern_name,
                "score": round(float(r.score), 1),
            }
        )
    return sorted(out, key=lambda x: x["date"], reverse=True)


def _confluence_note(klines: Sequence, n: int, patterns: list[dict[str, Any]]) -> str:
    """对最近一根 K 线上的形态跑共振守卫，与信号页同源，提示逆趋势形态。

    ``pattern_name`` 必须传入（否则左侧超跌形态防守会整体跳过），
    ``decision_index`` 用最新一根（``n``）：个股页与榜单同为「今天要不要买」
    的决策口径，形态发生在更早的 K 线上也按今天的价与量复核。
    """
    if not patterns:
        return ""
    latest_date = patterns[0]["date"]
    target = None
    for i in range(n, max(n - PATTERN_RECENT_BARS, 19) - 1, -1):
        if _fmt_date(klines[i]) == latest_date:
            target = i
            break
    if target is None:
        return ""
    conf = evaluate_confluence(
        klines, target, "bullish", patterns[0].get("name"), decision_index=n
    )
    notes = [sc.message for sc in conf.soft_conflict_items if sc.kind == "structure_flaw"]
    if not notes:
        return ""
    return notes[0]


def _money(v: float | None) -> str:
    """金额（自动转万元/亿元，用于已由文字表达方向的场合，不带符号）。"""
    if v is None:
        return "—"
    return _fmt_money(abs(float(v))).lstrip("+")


def _flow_main_text(main: dict[str, Any]) -> str:
    net5 = main.get("net_5d")
    if net5 is None:
        return ""
    direction = "净流入" if net5 > 0 else "净流出"
    as_of = main.get("as_of") or ""
    prefix = f"截至 {as_of}（资金面最新披露日），" if as_of else ""
    parts = [f"{prefix}近 5 日主力资金（超大单+大单，东财口径）累计{direction} {_money(net5)}"]
    ratio = main.get("ratio_5d_avg")
    if ratio is not None:
        parts.append(f"近 5 日主力净占比均值 {ratio:+.2f}%")
    streaks = main.get("streak_days") or 0
    if streaks >= 2:
        parts.append(f"已连续 {streaks} 日主力{'净流入' if main.get('streak_dir') == 'in' else '净流出'}")
    inflow_days = main.get("inflow_days_5d")
    if inflow_days is not None:
        parts.append(f"近 5 日中 {inflow_days} 日为净流入")
    parts.append(
        f"其中超大单 {_fmt_money(main.get('xlarge_5d'))}、大单 {_fmt_money(main.get('large_5d'))}（近 5 日合计）"
    )
    prev5 = main.get("net_5d_prev")
    if prev5 is not None and (net5 or 0) != 0:
        if prev5 * net5 < 0:
            parts.append("资金方向较前 5 日已反转")
        elif abs(net5) < abs(prev5):
            parts.append(f"规模较前 5 日（{_money(prev5)}）收窄")
        else:
            parts.append(f"规模较前 5 日（{_money(prev5)}）放大")
    return "；".join(parts) + "。"


def _flow_margin_text(margin: dict[str, Any]) -> str:
    balance = margin.get("balance")
    if balance is None:
        return ""
    mdate = margin.get("date") or ""
    parts = [f"融资余额 {_money(balance)}" + (f"（截至 {mdate}）" if mdate else "")]
    chg5 = margin.get("balance_chg_5d")
    if chg5 is not None:
        parts.append(f"较 5 个交易日前{'增加' if chg5 > 0 else '减少'} {_money(chg5)}")
    net10 = margin.get("net_10d")
    if net10 is not None:
        if net10 > 0:
            parts.append(f"近 10 日融资净买入 {_money(net10)}（杠杆资金偏多）")
        else:
            parts.append(f"近 10 日融资净偿还 {_money(net10)}（杠杆资金偏谨慎）")
    short_vol = margin.get("short_volume")
    if short_vol is not None:
        seg = f"融券余量 {short_vol / 1e4:.2f} 万股"
        short_chg = margin.get("short_chg_5d")
        if short_chg is not None:
            seg += f"，较 5 日前{'增加' if short_chg > 0 else '减少'} {abs(short_chg) / 1e4:.2f} 万股"
        parts.append(seg)
    return "；".join(parts) + "。"


def _flow_lhb_text(lhb: dict[str, Any] | None) -> str:
    if not lhb:
        return "近半年未登上龙虎榜，无席位资金异动记录。"
    net = lhb.get("net_buy")
    net_txt = f"，龙虎榜净买入 {_fmt_money(net)}" if net is not None else ""
    days = lhb.get("days_ago")
    days_txt = f"，距今 {days} 天" if days is not None else ""
    return (
        f"最近一次登上龙虎榜为 {lhb.get('date')}（{lhb.get('reason') or '上榜原因未披露'}）"
        f"{net_txt}{days_txt}。"
    )


def _flow_stance(main: dict[str, Any] | None, margin: dict[str, Any] | None, align: str) -> str:
    """资金面倾向（叙述标签，不产生分数）。"""
    main_sign = None
    margin_sign = None
    if main and main.get("net_5d") is not None:
        main_sign = 1 if main["net_5d"] > 0 else -1
    if margin and margin.get("net_10d") is not None:
        margin_sign = 1 if margin["net_10d"] > 0 else -1
    if main_sign is None and margin_sign is None:
        return ""
    if main_sign == margin_sign == 1:
        base = "偏多：主力资金与杠杆资金同向流入"
    elif main_sign == margin_sign == -1:
        base = "偏空：主力资金净流出、杠杆资金净偿还，资金面不支撑反弹"
    elif main_sign is not None and margin_sign is not None:
        base = "分歧：主力资金与杠杆资金方向不一致，资金面信号不明确"
    elif main_sign == 1:
        base = "中性偏多：主力资金净流入（杠杆资金数据暂缺）"
    elif main_sign == -1:
        base = "中性偏空：主力资金净流出（杠杆资金数据暂缺）"
    elif margin_sign == 1:
        base = "中性偏多：杠杆资金净买入（主力资金数据暂缺）"
    else:
        base = "中性偏空：杠杆资金净偿还（主力资金数据暂缺）"
    if align == "空头排列" and (main_sign == -1 or margin_sign == -1):
        base += "；与均线空头排列互相印证，反弹缺乏资金推动"
    elif align == "多头排列" and main_sign == 1:
        base += "；与均线多头排列互相印证，趋势有资金承接"
    return base


def _build_fund_flow_section(symbol: str, align: str, db: Any = None) -> dict[str, Any] | None:
    """资金面段落。任意子段缺失都不阻断报告生成。"""
    try:
        ff = build_fund_flow(symbol, db)
    except Exception:  # noqa: BLE001
        return None
    if not ff.get("ok"):
        return None
    main = ff.get("main")
    margin = ff.get("margin")
    lhb = ff.get("lhb")
    texts = {
        "main": _flow_main_text(main) if main else "",
        "margin": _flow_margin_text(margin) if margin else "",
        "lhb": _flow_lhb_text(lhb),
        "stance": _flow_stance(main, margin, align),
    }
    return {
        "as_of": ff.get("as_of"),
        "source": "eastmoney",
        "updated_at": ff.get("updated_at"),
        "main": main,
        "margin": margin,
        "lhb": lhb,
        "texts": texts,
    }


def build_tech_narrative(db, symbol_raw: str) -> dict[str, Any]:
    from app.services.stock_universe import resolve_symbol
    from app.utils.symbol import SymbolError

    try:
        symbol = resolve_symbol(symbol_raw, db)
    except SymbolError as e:
        return {"ok": False, "reason": str(e)}

    name = lookup_name(db, symbol) or ""
    klines, _meta = KlineService(db).get_recent_klines(symbol, limit=KLINE_LIMIT)
    if len(klines) < MIN_BARS:
        return {"ok": False, "reason": f"K 线不足 {MIN_BARS} 根，无法生成技术面分析", "symbol": symbol}

    n = len(klines) - 1
    closes = [float(k.close) for k in klines]
    close = closes[n]
    ma5 = _sma(closes, 5, n)
    ma10 = _sma(closes, 10, n)
    ma20 = _sma(closes, 20, n)
    ma60 = _sma(closes, 60, n)
    align = _ma_alignment(ma5, ma10, ma20)
    chg5 = (close - closes[n - 5]) / closes[n - 5] if n >= 5 and closes[n - 5] else None
    chg20 = (close - closes[n - 20]) / closes[n - 20] if n >= 20 and closes[n - 20] else None
    weekly = weekly_trend_at(klines, n)

    lookback = klines[max(0, n - 19) : n + 1]
    low20 = min(float(k.low) for k in lookback)
    high20 = max(float(k.high) for k in lookback)

    macd = _macd_state(closes, n)
    rsi = rsi_at(closes, n)
    stoch = stochastic_at(klines, n)
    bb = _bollinger(closes, n)
    boll_ma, boll_up, boll_low = (bb[0], bb[1], bb[2]) if bb else (None, None, None)
    boll_pos = _boll_position(close, boll_ma, boll_up, boll_low)
    vol = _volume_state(klines, n)

    patterns = _recent_patterns(klines, n)
    guard_note = _confluence_note(klines, n, patterns)
    fund_flow = _build_fund_flow_section(symbol, align, db)

    # ── 支撑与压力 ────────────────────────────────────────────
    supports: list[dict[str, Any]] = []
    resistances: list[dict[str, Any]] = []

    def _add(bucket: list, price: float | None, source: str, note: str) -> None:
        if price and price > 0:
            bucket.append({"price": round(price, 3), "source": source, "note": note})

    if low20 > 0:
        _add(supports, low20, "近期低点", f"近 20 日最低 {_px(low20)}，跌破则趋势进一步走坏")
    _add(supports, boll_low, "布林下轨", f"布林下轨 {_px(boll_low)}")
    for role, price, detail in polarity_levels(klines, n):
        if abs(close - price) / max(price, 1e-9) <= 0.08:
            if role == "support" and price < close:
                _add(supports, price, "极性支撑", detail)
            elif role == "resistance" and price > close:
                _add(resistances, price, "极性阻力", detail)

    for label, ma in (("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA60", ma60)):
        if ma is None:
            continue
        if ma > close:
            _add(resistances, ma, label, f"{label} {_px(ma)} 压制")
        else:
            _add(supports, ma, label, f"{label} {_px(ma)} 支撑")
    _add(resistances, high20, "近期高点", f"近 20 日最高 {_px(high20)}")

    supports = sorted({round(s["price"], 3): s for s in supports}.values(), key=lambda x: -x["price"])
    resistances = sorted({round(r["price"], 3): r for r in resistances}.values(), key=lambda x: x["price"])
    near_supports = [s for s in supports if s["price"] <= close]
    near_resistances = [r for r in resistances if r["price"] >= close]

    # ── 综合判断 ──────────────────────────────────────────────
    trend_text = {
        "多头排列": "均线多头排列，短中期趋势向上",
        "空头排列": "均线空头排列，短中期趋势向下",
        "纠缠": "均线纠缠，趋势方向不明，以震荡对待",
    }[align]
    if weekly == "up":
        trend_text += "；周线波段向上"
    elif weekly == "down":
        trend_text += "；周线波段向下"
    else:
        trend_text += "；周线横向"

    rsi_text = ""
    if rsi is not None:
        zone, note = _rsi_zone(rsi)
        rsi_text = f"RSI(14)={rsi:.1f}，{zone}：{note}"

    r1 = near_resistances[0]["price"] if near_resistances else None
    r2 = near_resistances[1]["price"] if len(near_resistances) > 1 else None
    s1 = near_supports[0]["price"] if near_supports else None

    short_term_parts: list[str] = []
    if rsi is not None and rsi < 30:
        short_term_parts.append("RSI 进入超卖区，存在技术性反弹需求")
    if macd.get("available") and macd["dif"] < macd["dea"] and macd["hist"] < 0:
        short_term_parts.append("MACD 仍在死叉状态且柱体为负，空头动能未确认衰竭")
    if vol.get("available") and vol["ratio"] <= 0.8:
        short_term_parts.append("近期量能萎缩，反弹缺乏量能配合时更可能是下跌中继")
    if r1:
        short_term_parts.append(f"反弹第一目标看 {_px(r1)}" + (f"，第二目标 {_px(r2)}" if r2 else ""))
    if not short_term_parts:
        short_term_parts.append("指标中性，短期以区间震荡对待")

    gates: list[str] = []
    if align == "空头排列" or (ma20 and close < ma20):
        gates.append(f"放量收盘站回 MA20（{_px(ma20) if ma20 else '—'}）")
    if macd.get("available") and macd["dif"] < macd["dea"]:
        gates.append("MACD 零轴下方金叉或底背离")
    _ff_main = (fund_flow or {}).get("main")
    if _ff_main and _ff_main.get("net_5d") is not None:
        if _ff_main["net_5d"] > 0:
            gates.append(f"主力资金维持净流入（当前近 5 日 {_fmt_money(_ff_main['net_5d'])}）")
        else:
            gates.append(f"主力资金转为净流入并连续 3 日以上（当前近 5 日 {_fmt_money(_ff_main['net_5d'])}）")
    else:
        gates.append("主力级资金连续回流（资金面数据源当前不可用，需自行观察成交量持续性）")
    mid_term = f"{trend_text}。"
    if align == "空头排列" or (ma20 and close < ma20):
        mid_term += "中期趋势偏空，需等待以下信号逐一确认后再考虑介入：" + "；".join(gates[:2])
        if len(gates) > 2:
            mid_term += "；" + gates[2]
    elif align == "多头排列":
        mid_term += "中期趋势向好，回踩 MA10/MA20 缩量不破可视为健康回调。"
    else:
        mid_term += "中期方向不明，等待区间突破选择方向。"

    actions: list[str] = []
    if align == "空头排列" or (ma20 and close < ma20):
        if r1 and r2:
            actions.append(f"持仓者：反弹至 {_px(r1)}~{_px(r2)} 区域遇阻缩量，可考虑减仓降低风险")
        elif r1:
            actions.append(f"持仓者：反弹至 {_px(r1)} 附近遇阻，可考虑减仓降低风险")
        actions.append("空仓者：当前不建议抄底，等待底部形态确认（双底/头肩底）或放量突破关键压力位后再介入")
    elif align == "多头排列":
        actions.append("持仓者：趋势向上，可继续持有，跌破 MA20 再评估离场")
        actions.append("空仓者：回踩 MA10/MA20 缩量企稳时可分批介入，避免追高")
    else:
        actions.append("持仓者：区间震荡对待，接近区间上沿减仓、下沿不追卖")
        actions.append("空仓者：等待方向选择，突破区间并放量确认后再跟进")
    if s1:
        actions.append(f"止损参考：近期低点 {_px(s1)}，收盘跌破则果断离场/减仓")
    if guard_note:
        actions.append(f"形态提示：{guard_note}")
    _ff_texts = (fund_flow or {}).get("texts") or {}
    if _ff_texts.get("stance"):
        actions.append(f"资金面：{_ff_texts['stance']}")

    return {
        "ok": True,
        "symbol": symbol,
        "name": name,
        "as_of": _fmt_date(klines[n]),
        "close": round(close, 3),
        "trend": {
            "align": align,
            "weekly": weekly,
            "chg5": chg5,
            "chg20": chg20,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "ma60": ma60,
            "close": close,
            "summary": trend_text,
        },
        "patterns": patterns,
        "guard_note": guard_note,
        "indicators": {
            "macd": macd,
            "rsi": rsi,
            "rsi_text": rsi_text,
            "stoch": ({"k": stoch[0], "d": stoch[1]} if stoch else None),
            "boll": {"position": boll_pos, "mid": boll_ma, "upper": boll_up, "lower": boll_low},
            "volume": vol,
        },
        "levels": {"supports": supports[:4], "resistances": resistances[:4]},
        "fund_flow": fund_flow,
        "verdict": {
            "short_term": "；".join(short_term_parts) + "。",
            "mid_term": mid_term,
            "actions": actions,
        },
        "note": (
            "口径：技术指标由本系统 K 线数据计算，与信号页/共振榜单同源；资金面为东方财富成交结构口径"
            "（主力=超大单+大单，非同花顺 DDX，属成交结构统计而非持仓变动）+ 交易所融资融券明细（T+1）+ 龙虎榜，"
            "仅作展示、不参与打分与筛选；北向资金个股日频持股自 2024-08 起停止披露，故不含北向。"
            "技术面分析不构成投资建议。"
        ),
    }
