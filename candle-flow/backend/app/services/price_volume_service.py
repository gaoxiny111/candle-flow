"""价量策略扫描服务：全市场「趋势确认 / 反转捕捉」信号扫描 + 有效性验证。

**只读**：不改写 ``composite_score`` / ``qv_score`` / 技术面得分，也不参与任何硬门槛。

## 口径（与 `app/core/price_volume.py` 一致，勿另写一套）

- 价格前复权；均量基线**不含当日**（``shift(1)``）。
- **T+1**：信号在 t 日收盘产生 → t+1 **开盘**成交；一字板封死不计入可成交样本。
- 回测/验证**必扣成本**：佣金（万 2.5、单笔最低 5 元）+ 印花税（卖出 0.05%）
  + 过户费（双向 0.001%）+ 滑点（单边 0.1%）。
- 基准用**同票全样本**同口径前瞻收益（不是指数）——回答的是「信号日是否真的
  比这只票随机一天更好」，避免把 beta 当成 alpha。
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from datetime import date, datetime, time as dtime
from typing import Any, Callable, Sequence

import numpy as np
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.bull_tactics import is_main_board, is_st_name
from app.core.candle import Candle
from app.core import price_volume as pvmod

logger = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, str], None]

CACHE_VERSION = 2
# 2 = 增加「ADX 环境分流 + 信号优先级仲裁」字段（arb / arb_verdict / arb_env / arb_signal）。
# 改索引结构必须 bump，否则旧缓存（无 arb 字段）会被当成新鲜数据继续返回。
SCAN_TTL_SEC = 600          # 扫描索引缓存 10 分钟
VALIDATE_TTL_SEC = 3600     # 验证结果缓存 1 小时
MAX_BARS = 320              # 扫描：每票读取的最大 K 线根数（够 250 日分位 + 60 日窗口）
VALIDATE_BARS = 760         # 验证：约 3 年
DEFAULT_LOOKBACK_DAYS = 10
MAX_TOP = 500
_SYMBOL_CHUNK = 400         # IN 子句分片（兼容旧 SQLite 的 999 变量上限）

# ── 交易成本（2026 现行口径）────────────────────────────────────────
COMMISSION_RATE = 0.00025   # 券商佣金 万 2.5（全佣，含规费）
COMMISSION_MIN = 5.0        # 单笔佣金不足 5 元按 5 元收
STAMP_TAX = 0.0005          # 印花税：**卖出单边** 0.05%
TRANSFER_FEE = 0.00001      # 过户费：买卖双向 0.001%
SLIPPAGE = 0.001            # 滑点：单边 0.1%（市价单保守值）
POSITION_CNY = 100_000.0    # 单笔名义本金（最低佣金的实际影响与本金相关）

_scan_cache: dict[str, Any] = {
    "ts": 0.0, "version": 0, "items": None, "stats": None, "coverage": None,
    "lookback_days": DEFAULT_LOOKBACK_DAYS,
}
_scan_lock: threading.Lock | None = None
_validate_cache: dict[str, Any] = {"ts": 0.0, "key": None, "payload": None}


def cost_rate() -> float:
    """一买一卖的**合计**成本率（含「单笔最低 5 元佣金」在小资金下的放大效应）。"""
    one_side = (
        max(COMMISSION_RATE * POSITION_CNY, COMMISSION_MIN) / POSITION_CNY
        + TRANSFER_FEE
        + SLIPPAGE
    )
    return one_side * 2 + STAMP_TAX


def cost_breakdown() -> dict[str, float]:
    return {
        "commission_rate": COMMISSION_RATE,
        "commission_min_cny": COMMISSION_MIN,
        "stamp_tax_sell": STAMP_TAX,
        "transfer_fee": TRANSFER_FEE,
        "slippage_one_side": SLIPPAGE,
        "position_cny": POSITION_CNY,
        "round_trip": round(cost_rate(), 6),
    }


def limit_pct(symbol: str, name: str | None = None) -> float:
    """涨跌停幅度：主板 10 / 创业板科创板 20 / 北交所 30 / ST 5。"""
    if name and is_st_name(name):
        return 5.0
    code = str(symbol).split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("4", "8", "920")):
        return 30.0
    return 10.0


# ── 数据读取 ────────────────────────────────────────────────────────

def _universe(db: Session, *, include_gem: bool = False) -> list[tuple[str, str]]:
    """主板（可选含创业板）宇宙，剔除 ST / 退市。"""
    rows = db.execute(
        text("SELECT symbol, name, market FROM stock_info WHERE market IN ('SH','SZ')")
    ).all()
    out: list[tuple[str, str]] = []
    for sym, name, _market in rows:
        sym = str(sym)
        name = str(name or "")
        if is_st_name(name):
            continue
        code = sym.split(".")[0]
        ok = is_main_board(sym)
        if not ok and include_gem and code.startswith(("300", "301")):
            ok = True
        if ok:
            out.append((sym, name))
    out.sort()
    return out


def _load_klines(
    db: Session,
    symbols: Sequence[str],
    max_bars: int,
    *,
    progress: ProgressCb | None = None,
) -> dict[str, list[Candle]]:
    """按 symbol 批量读取**最近 ``max_bars`` 根** K 线（SQLite 窗口函数，一次拉完）。"""
    sql = text(
        """
        SELECT symbol, date, open, high, low, close, volume FROM (
            SELECT symbol, date, open, high, low, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
            FROM kline_data WHERE symbol IN :syms
        ) WHERE rn <= :n ORDER BY symbol, date
        """
    ).bindparams(bindparam("syms", expanding=True))
    out: dict[str, list[Candle]] = {}
    total = len(symbols)
    for i in range(0, total, _SYMBOL_CHUNK):
        chunk = list(symbols[i: i + _SYMBOL_CHUNK])
        for sym, d, o, h, lo, c, v in db.execute(sql, {"syms": chunk, "n": max_bars}):
            try:
                day = date.fromisoformat(str(d)[:10])
            except ValueError:
                continue
            out.setdefault(str(sym), []).append(
                Candle(
                    open=float(o), high=float(h), low=float(lo), close=float(c),
                    volume=float(v), timestamp=datetime.combine(day, dtime(9, 30)),
                )
            )
        # 分片报进度：否则前端在读取阶段（十几秒）看不到任何变化，像是卡死
        if progress:
            progress(min(i + _SYMBOL_CHUNK, total), total, "load")
    return out


def _light_meta(db: Session) -> dict[str, dict[str, Any]]:
    """行业 / 市值 / 最新价（来自因子快照 payload，只读）。"""
    sql = text(
        """
        SELECT symbol,
               json_extract(payload, '$.industry')          AS industry,
               json_extract(payload, '$.market.market_cap') AS market_cap,
               json_extract(payload, '$.market.price')      AS price
        FROM factor_snapshots
        """
    )
    out: dict[str, dict[str, Any]] = {}
    for sym, industry, cap, price in db.execute(sql):
        out[str(sym)] = {
            "industry": (str(industry) if industry else None),
            "market_cap": (float(cap) if cap not in (None, "") else None),
            "price": (float(price) if price not in (None, "") else None),
        }
    return out


# ── 扫描索引 ────────────────────────────────────────────────────────

def index_age() -> float | None:
    if _scan_cache["items"] is None:
        return None
    return max(0.0, time.time() - float(_scan_cache["ts"]))


def _analyze_symbol(
    symbol: str, name: str, candles: list[Candle], *, lookback_days: int
) -> dict[str, Any]:
    res = pvmod.analyze(candles, lookback_days=lookback_days, limit_pct=limit_pct(symbol, name))
    res["symbol"] = symbol
    res["name"] = name
    res["signal_score"] = pvmod.signal_score(res["signals"])
    # 信号处理流水线：ADX 环境分流 → 优先级仲裁 → 唯一裁决（只读，不改任何分数）
    adx = (res.get("regime") or {}).get("adx")
    arb = pvmod.arbitrate(res["signals"], pvmod.env_of(adx), adx=adx)
    res["arb"] = arb
    res["arb_verdict"] = arb["verdict"]
    res["arb_env"] = arb["env"]
    res["arb_signal"] = (arb.get("final") or {}).get("key")
    res["arb_signal_name"] = (arb.get("final") or {}).get("name")
    return res


def scan_market(
    db: Session,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    include_gem: bool = False,
    force: bool = False,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """全市场价量信号扫描（进程内缓存 10 分钟；同参数（含回看天数）才复用）。"""
    global _scan_lock
    if _scan_lock is None:
        _scan_lock = threading.Lock()
    with _scan_lock:
        now = time.time()
        if (
            not force
            and _scan_cache["items"] is not None
            and int(_scan_cache["version"]) == CACHE_VERSION
            and int(_scan_cache["lookback_days"]) == int(lookback_days)
            and now - float(_scan_cache["ts"]) < SCAN_TTL_SEC
        ):
            if progress:
                progress(1, 1, "cache")
            return {"cached": True, **(_scan_cache["stats"] or {})}

        universe = _universe(db, include_gem=include_gem)
        if progress:
            progress(0, len(universe), "load")
        klines = _load_klines(db, [s for s, _ in universe], MAX_BARS, progress=progress)
        meta = _light_meta(db)

        items: list[dict[str, Any]] = []
        insufficient = 0
        no_signal = 0
        signal_counts: dict[str, int] = {k: 0 for k in pvmod.SIGNAL_META}
        category_counts = {"trend": 0, "reversal": 0}
        regime_counts = {"trend": 0, "range": 0, "neutral": 0, "unknown": 0}
        # 裁决口径统计：只覆盖「有信号的票」（no_signal 的票不在 items 里，等同 IGNORE）
        verdict_counts: dict[str, int] = {k: 0 for k in pvmod.VERDICT_ZH}
        env_counts: dict[str, int] = {k: 0 for k in pvmod.ENV_ZH}
        priority_counts: dict[str, int] = {k: 0 for k in pvmod.SIGNAL_META}
        latest_counts: dict[str, int] = {k: 0 for k in pvmod.SIGNAL_META}
        unmapped_meta = 0

        for idx, (sym, name) in enumerate(universe):
            bars = klines.get(sym) or []
            if len(bars) < pvmod.MIN_BARS:
                insufficient += 1
                if progress and idx % 50 == 0:
                    progress(idx, len(universe), "scan")
                continue
            try:
                res = _analyze_symbol(sym, name, bars, lookback_days=lookback_days)
            except Exception as exc:  # noqa: BLE001
                logger.warning("价量分析失败 %s: %s", sym, exc)
                insufficient += 1
                continue
            reg = (res.get("regime") or {}).get("state") or "unknown"
            regime_counts[reg] = regime_counts.get(reg, 0) + 1
            if not res["signals"]:
                no_signal += 1
            else:
                m = meta.get(sym) or {}
                if not m:
                    unmapped_meta += 1
                arb = res.get("arb") or {}
                verdict_counts[arb.get("verdict") or "IGNORE"] = (
                    verdict_counts.get(arb.get("verdict") or "IGNORE", 0) + 1
                )
                env_counts[arb.get("env") or "UNKNOWN"] = (
                    env_counts.get(arb.get("env") or "UNKNOWN", 0) + 1
                )
                fin = arb.get("final") or {}
                if fin.get("key"):
                    priority_counts[fin["key"]] = priority_counts.get(fin["key"], 0) + 1
                for s in res["signals"]:
                    signal_counts[s["key"]] = signal_counts.get(s["key"], 0) + 1
                for k in res["latest_signals"]:
                    latest_counts[k] = latest_counts.get(k, 0) + 1
                for cat in res["categories"]:
                    category_counts[cat] = category_counts.get(cat, 0) + 1
                res["industry"] = m.get("industry")
                cap = m.get("market_cap")
                res["market_cap_yi"] = round(cap / 1e8, 2) if cap else None
                res["limit_pct_used"] = limit_pct(sym, name)
                items.append(res)
            if progress and (idx % 50 == 0 or idx == len(universe) - 1):
                progress(idx + 1, len(universe), "scan")

        items.sort(key=lambda r: (-((r.get("signal_score") or 0)), r["symbol"]))
        stats = {
            "universe": len(universe),
            "items": len(items),
            "no_signal": no_signal,
            "insufficient": insufficient,
            "lookback_days": lookback_days,
            "signal_counts": signal_counts,
            "latest_signal_counts": latest_counts,
            "category_counts": category_counts,
            "regime_counts": regime_counts,
            "verdict_counts": verdict_counts,
            "env_counts": env_counts,
            "priority_counts": priority_counts,
            "cost": cost_breakdown(),
            "built_at": datetime.now().isoformat(timespec="seconds"),
        }
        _scan_cache.update(
            ts=time.time(), version=CACHE_VERSION, items=items, stats=stats,
            coverage={"unmapped_meta": unmapped_meta}, lookback_days=lookback_days,
        )
        logger.info("price-volume scan built: %s", {k: stats[k] for k in ("universe", "items", "no_signal")})
        return {"cached": False, **stats}


def latest_scan() -> dict[str, Any] | None:
    if _scan_cache["items"] is None:
        return None
    return dict(_scan_cache["stats"] or {})


def view(
    db: Session,
    *,
    category: str | None = None,
    signal: str | None = None,
    regime: str | None = None,
    verdict: str | None = None,
    env: str | None = None,
    exclude_avoid: bool = False,
    latest_only: bool = False,
    sort_by: str = "signal_score",
    top: int = 100,
    offset: int = 0,
    industry: str | None = None,
    keyword: str | None = None,
    min_market_cap_yi: float | None = None,
    exclude_st: bool = True,
) -> dict[str, Any]:
    """读层（毫秒级）：索引未构建时返回 ``{"empty": True, "reason": …}``。

    裁决筛选（``verdict`` / ``exclude_avoid``）作用于**信号处理流水线**的产出：
    ``SIGNAL`` = 保留信号（买入候选）、``AVOID`` = 规避（已从买入列表剔除）、
    ``IGNORE`` = 信号被环境屏蔽、``STANDBY`` = 无趋势空仓。
    """
    stats = latest_scan()
    if stats is None:
        return {
            "empty": True,
            "reason": "价量策略索引尚未构建，请先触发 POST /strategies/price-volume/build",
        }
    items: list[dict[str, Any]] = list(_scan_cache["items"] or [])
    if exclude_st:
        items = [r for r in items if not is_st_name(r.get("name") or "")]
    if category:
        items = [r for r in items if category in (r.get("categories") or [])]
    if signal:
        keys = {k.strip() for k in str(signal).split(",") if k.strip()}
        items = [
            r for r in items
            if keys & {s["key"] for s in (r.get("signals") or [])}
        ]
    if latest_only:
        items = [r for r in items if r.get("latest_signals")]
    if regime:
        items = [r for r in items if (r.get("regime") or {}).get("state") == regime]
    if env:
        wanted = {k.strip().upper() for k in str(env).split(",") if k.strip()}
        items = [r for r in items if str(r.get("arb_env") or "").upper() in wanted]
    if verdict:
        wanted = {k.strip().upper() for k in str(verdict).split(",") if k.strip()}
        # 兼容两种叫法：候选/保留 = SIGNAL，规避 = AVOID
        wanted = {("SIGNAL" if w in ("BUY", "CANDIDATE", "KEEP") else w) for w in wanted}
        items = [r for r in items if str(r.get("arb_verdict") or "").upper() in wanted]
    if exclude_avoid:
        items = [r for r in items if (r.get("arb_verdict") or "") != "AVOID"]
    if industry:
        items = [r for r in items if industry in str(r.get("industry") or "")]
    if keyword:
        kw = keyword.strip()
        items = [
            r for r in items
            if kw in str(r.get("name") or "") or kw in str(r.get("symbol") or "")
        ]
    if min_market_cap_yi is not None:
        items = [
            r for r in items
            if (r.get("market_cap_yi") or 0) >= float(min_market_cap_yi)
        ]

    key = sort_by or "signal_score"
    if key == "adx":
        items.sort(key=lambda r: -float((r.get("regime") or {}).get("adx") or -1))
    elif key == "verdict":
        # 买入候选在前（同档按优先级、再按触发新鲜度），规避/忽略/空仓依次垫后
        items.sort(key=lambda r: (
            pvmod.VERDICT_SORT_ORDER.get(str(r.get("arb_verdict") or "IGNORE"), 9),
            -int(((r.get("arb") or {}).get("priority") or 0)),
            (r.get("newest_bars_ago") if r.get("newest_bars_ago") is not None else 99),
            -float(r.get("signal_score") or 0),
        ))
    elif key == "market_cap":
        items.sort(key=lambda r: -float(r.get("market_cap_yi") or 0))
    elif key == "trend":
        items.sort(key=lambda r: (-int((r.get("counts") or {}).get("trend") or 0),
                                  -float(r.get("signal_score") or 0)))
    elif key == "reversal":
        items.sort(key=lambda r: (-int((r.get("counts") or {}).get("reversal") or 0),
                                  -float(r.get("signal_score") or 0)))
    elif key == "newest":
        items.sort(key=lambda r: (r.get("newest_bars_ago") if r.get("newest_bars_ago") is not None else 99,
                                  -float(r.get("signal_score") or 0)))
    else:
        items.sort(key=lambda r: (-float(r.get("signal_score") or 0), str(r.get("symbol"))))

    total = len(items)
    page = items[offset: offset + max(1, top)]
    return {
        "empty": False,
        "total": total,
        "offset": offset,
        "top": top,
        "items": page,
        "stats": stats,
        "index_age_sec": round(index_age() or 0.0, 1),
        "cost": cost_breakdown(),
        "notes": [
            "本视图**只读**：不改写 composite_score / qv_score，不参与任何硬门槛。",
            "signal_score 仅用于排序（越近的信号权重越高），不是收益预测、不是评分。",
            "信号方向：趋势类为看多；反转类含看多（地量止跌/价量恐慌）与看空（天量滞涨/价量过热）。",
            "★ **信号处理流水线（补丁一 + 补丁二）**：先按 ADX 判策略环境"
            f"（>{pvmod.ENV_TREND_ADX:g} 趋势市 → 只跑趋势类；<{pvmod.ENV_WEAK_ADX:g} 无趋势 → 空仓观望；"
            "其间为震荡市 → 只跑反转类），再按优先级仲裁取唯一结论"
            f"（风险类 ≥{pvmod.PRIORITY_AVOID} 直接判「规避」）。",
            "★ 刻意偏离伪代码：**风险类（看空）信号不受环境过滤** —— 否则趋势市会把「价量过热」"
            "这类风险提示一起屏蔽，等于给否决类判据开免检通道（伪代码自带的万科A例子也会推不出来）。",
            "裁决四态：保留信号（买入候选）/ 规避 / 忽略（被环境屏蔽）/ 空仓观望（ADX<20）。"
            "裁决只影响本视图的展示与筛选，**不进任何评分与门槛**。",
            "T+1：信号在收盘产生，次日开盘才能成交；`tradable.blocked` 标出次日一字板封死无法成交的情形。",
            "成交量单位「手」，且已剔除停更/污染 bar；前复权价格。",
        ],
    }


def single(db: Session, symbol: str) -> dict[str, Any]:
    """单票明细（不走缓存，实时算）。"""
    sym = symbol.strip().upper()
    row = db.execute(
        text("SELECT name FROM stock_info WHERE symbol = :s"), {"s": sym}
    ).first()
    name = str(row[0]) if row else ""
    bars = _load_klines(db, [sym], MAX_BARS).get(sym) or []
    if not bars:
        return {"symbol": sym, "name": name, "insufficient": True, "reason": "无 K 线数据"}
    res = _analyze_symbol(sym, name, bars, lookback_days=DEFAULT_LOOKBACK_DAYS)
    res["limit_pct_used"] = limit_pct(sym, name)
    meta = _light_meta(db).get(sym) or {}
    res["industry"] = meta.get("industry")
    cap = meta.get("market_cap")
    res["market_cap_yi"] = round(cap / 1e8, 2) if cap else None
    res["signal_score"] = pvmod.signal_score(res["signals"])
    res["price_history"] = [
        {"date": c.timestamp.strftime("%Y-%m-%d"), "close": round(c.close, 2),
         "volume": int(c.volume)}
        for c in bars[-60:]
    ]
    return res


# ── 有效性验证（事件研究：T+1 开盘成交 + 扣全成本 + 同票基准）────────

def _forward_stats(
    opens: np.ndarray, o_ok: np.ndarray, idxs: list[int], horizon: int,
) -> list[float]:
    """事件后 T+1 开盘→T+1+h 开盘的**毛收益**（不可成交的剔除，由调用方判定）。"""
    out: list[float] = []
    n = opens.size
    for i in idxs:
        e = i + 1
        x = i + 1 + horizon
        if e >= n or x >= n:
            continue
        if not o_ok[i]:
            continue
        base = opens[e]
        if not np.isfinite(base) or base <= 0:
            continue
        out.append(float(opens[x] / base - 1.0))
    return out


def _baseline(
    opens: np.ndarray, o_ok: np.ndarray, horizon: int, *, start: int, stop: int
) -> float | None:
    """同票「随机一天」的同口径前瞻收益均值（进场 = 次日开盘）。"""
    vals: list[float] = []
    n = opens.size
    for i in range(start, min(stop, n - horizon - 1)):
        if not o_ok[i]:
            continue
        e = i + 1
        x = i + 1 + horizon
        if x >= n or not np.isfinite(opens[e]) or opens[e] <= 0:
            continue
        vals.append(float(opens[x] / opens[e] - 1.0))
    if len(vals) < 30:
        return None
    return float(np.mean(vals))


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def validate(
    db: Session,
    *,
    horizons: Sequence[int] = (5, 10, 20),
    lookback_days: int = 400,
    include_gem: bool = False,
    symbol_limit: int | None = None,
    force: bool = False,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """事件研究：每条信号在历史上的前瞻收益（**已扣成本**）+ 同票基准对照。

    刻意不做的事：不做参数寻优（那是过拟合的温床）。这里只回答
    「信号发生后的收益，是否显著优于这只票的随机一天」，并按时段/环境拆开看稳定性。
    """
    global _validate_cache
    cache_key = (tuple(int(h) for h in horizons), int(lookback_days), bool(include_gem), symbol_limit)
    now = time.time()
    if (
        not force
        and _validate_cache["payload"] is not None
        and _validate_cache["key"] == cache_key
        and now - float(_validate_cache["ts"]) < VALIDATE_TTL_SEC
    ):
        if progress:
            progress(1, 1, "cache")
        return _validate_cache["payload"]

    universe = _universe(db, include_gem=include_gem)
    if symbol_limit:
        universe = universe[: int(symbol_limit)]
    klines = _load_klines(db, [s for s, _ in universe], VALIDATE_BARS)
    cost = cost_rate()

    # 聚合结构：key → horizon → 统计
    agg: dict[str, dict[int, dict[str, Any]]] = {
        k: {int(h): {"rets": [], "excess": [], "blocked": 0, "total": 0, "by_regime": {},
                     "by_year": {}, "no_base": 0, "symbols": set()}
            for h in horizons}
        for k in pvmod.SIGNAL_META
    }
    trades: list[dict[str, Any]] = []
    evaluated = 0

    for idx, (sym, name) in enumerate(universe):
        bars = klines.get(sym) or []
        if len(bars) < pvmod.MIN_BARS + max(horizons) + 5:
            if progress and idx % 50 == 0:
                progress(idx, len(universe), "validate")
            continue
        try:
            opens = np.array([float(c.open) for c in bars], dtype=float)
            o_ok = np.isfinite(opens) & (opens > 0)
            events = pvmod.signal_events(bars, lookback_days=lookback_days,
                                         limit_pct=limit_pct(sym, name))
            if not events:
                continue
            h_arr = np.array([float(c.high) for c in bars], dtype=float)
            l_arr = np.array([float(c.low) for c in bars], dtype=float)
            c_arr = np.array([float(c.close) for c in bars], dtype=float)
            adx_s, _p, _m = pvmod.adx_series(h_arr, l_arr, c_arr)
            start = pvmod.MIN_BARS - 1
            stop = len(bars) - max(horizons) - 2
            bases = {int(h): _baseline(opens, o_ok, int(h), start=start, stop=stop)
                     for h in horizons}
            evaluated += 1
            for key, idxs in events.items():
                meta = pvmod.SIGNAL_META.get(key)
                if meta is None:
                    continue
                direction = meta["direction"]
                cat = agg[key]
                for i in idxs:
                    if i < start or i > stop:
                        continue
                    # 可成交性：一字板封死则不计入收益（否则回测会凭空赚板）
                    code, _zh = pvmod.exec_block(
                        bars, i, direction=direction, limit_pct=limit_pct(sym, name)
                    )
                    blocked = code is not None
                    for h in horizons:
                        h = int(h)
                        bucket = cat[h]
                        bucket["total"] += 1
                        if blocked:
                            bucket["blocked"] += 1
                            continue
                        if i + 1 + h >= len(bars):
                            continue
                        entry = opens[i + 1]
                        if not np.isfinite(entry) or entry <= 0:
                            continue
                        gross = float(opens[i + 1 + h] / entry - 1.0)
                        net = gross - cost
                        bucket["rets"].append(net)
                        bucket["symbols"].add(sym)
                        base = bases.get(h)
                        if base is not None:
                            bucket["excess"].append(gross - base)
                        else:
                            bucket["no_base"] += 1
                        reg = pvmod.regime_of(
                            float(adx_s[i]) if np.isfinite(adx_s[i]) else None
                        )
                        rb = bucket["by_regime"].setdefault(
                            reg, {"n": 0, "net_sum": 0.0, "win": 0}
                        )
                        rb["n"] += 1
                        rb["net_sum"] += net
                        rb["win"] += 1 if net > 0 else 0
                        yr = bars[i].timestamp.strftime("%Y")
                        yb = bucket["by_year"].setdefault(yr, {"n": 0, "net_sum": 0.0, "win": 0})
                        yb["n"] += 1
                        yb["net_sum"] += net
                        yb["win"] += 1 if net > 0 else 0
                        if len(trades) < 400:
                            trades.append({
                                "symbol": sym, "name": name, "signal": key,
                                "signal_name": meta["name"], "direction": direction,
                                "date": bars[i].timestamp.strftime("%Y-%m-%d"),
                                "hold": h, "gross_pct": round(gross * 100, 2),
                                "net_pct": round(net * 100, 2),
                                "excess_pct": round((gross - base) * 100, 2) if base is not None else None,
                                "regime": reg,
                            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("价量验证失败 %s: %s", sym, exc)
        if progress and (idx % 25 == 0 or idx == len(universe) - 1):
            progress(idx + 1, len(universe), "validate")

    def _pack(bucket: dict[str, Any]) -> dict[str, Any]:
        rets: list[float] = bucket["rets"]
        exc: list[float] = bucket["excess"]
        return {
            "samples": len(rets),
            "symbols": len(bucket["symbols"]),
            "total_events": bucket["total"],
            "blocked": bucket["blocked"],
            "blocked_pct": round(bucket["blocked"] / bucket["total"] * 100, 1) if bucket["total"] else None,
            "net_mean_pct": round(float(np.mean(rets)) * 100, 2) if rets else None,
            "net_median_pct": round((_median(rets) or 0.0) * 100, 2) if rets else None,
            "win_rate_pct": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1) if rets else None,
            "excess_mean_pct": round(float(np.mean(exc)) * 100, 2) if exc else None,
            "excess_median_pct": round((_median(exc) or 0.0) * 100, 2) if exc else None,
            "excess_win_rate_pct": round(sum(1 for x in exc if x > 0) / len(exc) * 100, 1) if exc else None,
            "no_baseline": bucket["no_base"],
            "by_regime": {
                k: {
                    "n": v["n"],
                    "net_mean_pct": round(v["net_sum"] / v["n"] * 100, 2) if v["n"] else None,
                    "win_rate_pct": round(v["win"] / v["n"] * 100, 1) if v["n"] else None,
                }
                for k, v in sorted(bucket["by_regime"].items())
            },
            "by_year": {
                k: {
                    "n": v["n"],
                    "net_mean_pct": round(v["net_sum"] / v["n"] * 100, 2) if v["n"] else None,
                    "win_rate_pct": round(v["win"] / v["n"] * 100, 1) if v["n"] else None,
                }
                for k, v in sorted(bucket["by_year"].items())
            },
        }

    payload = {
        "horizons": [int(h) for h in horizons],
        "lookback_days": int(lookback_days),
        "symbols_evaluated": evaluated,
        "universe": len(universe),
        "cost": cost_breakdown(),
        "signals": {
            k: {str(h): _pack(agg[k][int(h)]) for h in horizons}
            for k in pvmod.SIGNAL_META
        },
        "trades_sample": trades[:120],
        "notes": [
            "口径：信号日收盘出信号 → **次日开盘**买入 → 持有 h 个交易日后**开盘**卖出。",
            f"已扣成本：一买一卖合计 {round(cost_rate() * 100, 3)}%（佣金万2.5/最低5元 + 印花税0.05%卖出 + 过户费0.001%双向 + 滑点0.1%单边）。",
            "一字板封死不计入收益样本（否则回测会白白赚到买不进的涨停）。",
            "excess = 信号后毛收益 − **同一只票**同口径随机一天的前瞻收益（同票基准，剔除了 beta/风格）。",
            "★ 样本独立性：状态型信号（价量共振 / PVT 上行）会在同一只票连续多日触发，"
            "样本高度重叠，`samples` 远大于有效样本量——判断显著性时应以 `symbols`（去重股票数）为准。",
            "by_regime/by_year 用于看稳定性：若超额只集中在某一两年或某一环境，应视为未验证。",
            "本模块不做参数寻优（Walk-Forward 只做分段稳定性观察），避免过拟合。",
        ],
    }
    _validate_cache = {"ts": time.time(), "key": cache_key, "payload": payload}
    return payload


def validate_age() -> float | None:
    if _validate_cache["payload"] is None:
        return None
    return max(0.0, time.time() - float(_validate_cache["ts"]))
