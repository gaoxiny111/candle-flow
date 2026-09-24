"""周期品「产品价格拐点」预警（读层增强，**只读不写任何分数**）。

## 为什么需要它

周期股在盈利**顶峰**时 PE 往往**最低**——报表的利润增速锚定最新报告期（滞后），
而产品价格是高频领先量。当「产品价格已从高位明显回落」而「报表利润却仍在暴增」时，
就构成最典型的**周期陷阱**：估值看着最便宜，实际是最贵的时候。

本模块只把这条背离关系**显式化并逐票留痕**，供人工判断。

## 与评分链的关系（重要）

**只读不写**：不参与 ``qv_score`` / ``composite_score``、不参与任何硬门槛、
不否决也不抬分。三条理由：

1. **映射是行业级的，会漏判多元化公司**。实测：盐湖股份行业被归为「农化制品」
   （钾肥），但其利润有相当部分来自碳酸锂 —— 行业级映射看不到锂敞口。
   用这种覆盖度去做否决 = 「数据源覆盖不足却拿去过滤」（铁律 12 精神）。
2. **产品与原料必须分开**。化学原料的原油是**成本项**（原油跌 = 利好），
   而聚丙烯/乙二醇是**产品**（跌 = 利空）。若不分 product/cost，会把
   卫星化学、宝丰能源这类「油价上涨受益」的票**标反**。
3. 预警的价值在「提醒人去看」，不在「替人决定」。

## 数据源与已知边界

- 期货主力连续：``ak.futures_zh_daily_sina``（新浪），代码命名复用
  ``app.utils.symbol.FUTURE_NAMES``，不新建第二套（铁律 5）。
- **时效性校验必须有**：实测 ``ZC0``（动力煤连续）最新数据停在 2022-12-30
  （合约事实上停更）。故对最新交易日晚于 ``_STALE_DAYS`` 的品种一律标记
  ``stale`` 并**不参与判定**，绝不用四年前的煤价当现价。
- 30+ 个品种并行抓取实测约 2.8s（8 线程），进程内缓存 ``_TTL_SECONDS`` 后
  命中即零成本。缓存只在进程内，重启即清（与共振索引同性质）。

## 判定口径

单品种（近 ``_WINDOW`` 个交易日）：
- ``pos_pct`` = (close - lo) / (hi - lo) × 100 —— 在区间中的位置
- ``drawdown_pct`` = (close - hi) / hi × 100 —— 距区间高点回撤（≤0）
- ``trend`` = ma20 > ma60 ? up : down

组合判定（只看 **product**，cost 仅作背景）。行业篮子有多个品种时，**要求过半数
共振**（``need = ceil(n/2)``），不靠单一离群品种定档 —— 否则 5 个金属的篮子会让
每个工业金属股都顶着「回撤最大的那个品种」的名头，产生假归属：

- ``trap`` 周期陷阱预警：``profit_yoy ≥ 50`` **且** 回撤 ≥15% 的产品数 ``≥ need``
  → 报表仍在暴增，但产品价格已明确回落（滞后风险）
- ``peak`` 景气高位预警：``profit_yoy ≥ 50`` **且** 区间位置 ≥70 的产品数 ``≥ need``
  → 利润暴增且价格仍处高位（未见回落，但已在顶部区域）
- ``normal`` 有映射且无上述组合
- ``na`` 无映射 / 无产出物期货 / 数据缺失 / 全部停更

``trigger`` 字段给出**实际触发的那批品种**与宽度（``breadth`` / ``need``），
使结论可自证；``worst_product`` 保留为「回撤最大的产品」，仅作参考。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ── 阈值（集中定义，便于调参）───────────────────────────────────
PROFIT_SURGE_PCT = 50.0     # 利润增速达到该值 → 视为「报表暴增」
FALLEN_DD_PCT = -15.0       # 距区间高点回撤达到该值 → 「价格已明确回落」
PEAK_POS_PCT = 70.0         # 区间位置高于该值 → 「价格仍处高位」

_WINDOW = 250               # 统计窗口（交易日，约 1 年）
_MA_FAST = 20
_MA_SLOW = 60
_STALE_DAYS = 15            # 最新交易日晚于该天数 → 视为停更，不参与判定
_TTL_SECONDS = 3600.0       # 进程内缓存有效期
_MAX_WORKERS = 8

# ── 监测品种（只列有连续行情且与 A 股主业相关的）─────────────────
# 注意：ZC0（动力煤连续）**故意不在表内** —— 实测停更于 2022-12-30。
MONITORED: dict[str, str] = {
    "LC0": "碳酸锂",
    "SI0": "工业硅",
    "CU0": "沪铜",
    "AL0": "沪铝",
    "ZN0": "沪锌",
    "PB0": "沪铅",
    "SN0": "沪锡",
    "NI0": "沪镍",
    "AU0": "沪金",
    "AG0": "沪银",
    "RB0": "螺纹钢",
    "HC0": "热卷",
    "I0": "铁矿石",
    "J0": "焦炭",
    "JM0": "焦煤",
    "SC0": "原油",
    "MA0": "甲醇",
    "TA0": "PTA",
    "EG0": "乙二醇",
    "EB0": "苯乙烯",
    "PP0": "聚丙烯",
    "L0": "塑料",
    "V0": "PVC",
    "FG0": "玻璃",
    "SA0": "纯碱",
    "UR0": "尿素",
    "SP0": "纸浆",
    "LH0": "生猪",
    "CF0": "郑棉",
    "SR0": "白糖",
    "M0": "豆粕",
    "C0": "玉米",
    "RU0": "橡胶",
}

# ── 申万三级行业 → 产品 / 原料 ──────────────────────────────────
# ``product``：公司**卖出**的东西，价格下跌 = 利空（**唯一**产生预警的一侧）
# ``cost``   ：公司**买入**的东西，价格下跌 = 成本改善（只作背景，不产生预警）
#
# **收录判据（务必遵守，否则会标反）**：只有当该商品确实是这个行业的**产出物**
# 时才写进 ``product``。中下游加工/制造行业（塑料制品、化学纤维、管材、轮胎、
# 饲料、纺织、风电设备）买到的是**树脂/PTA/橡胶/豆粕/棉花/铜**——这些是成本，
# 不是产品；把它们当产品会把「原料降价利好」整体标成「产品跌价利空」。
#
# 反过来，**行业产出没有期货品种**时不要硬找代理（锡/镍不是稀土的产出物，
# 苯乙烯不是氟化工的产出物）——按「数据源无 → 不判定」处理（铁律 12），
# 宁可判 na 留痕，也不要给一个看起来像结论的假归属。
#
# 行业名按申万三级精确匹配；带「Ⅱ/Ⅲ」后缀的由 ``_norm_industry`` 归一化。
INDUSTRY_COMMODITY: dict[str, dict[str, tuple[str, ...]]] = {
    # ── 有色：金属即产出物 ──
    "能源金属": {"product": ("LC0", "NI0"), "cost": ()},
    "工业金属": {"product": ("CU0", "AL0", "ZN0", "PB0", "SN0"), "cost": ()},
    "贵金属": {"product": ("AU0", "AG0"), "cost": ()},
    # 小金属（稀土/钨/钼/锑）、金属新材料：**无代表性期货品种**。
    # 曾用 (SN0, NI0) 代理 → 把不产锡/镍的北方稀土、厦门钨业标成「锡/镍陷阱」，
    # 属虚假归属，已移除（改为 na）。
    # ── 新能源 ──
    # 电池：碳酸锂是锂电中游（正极/电解液）产品定价的锚，其下跌对应售价与
    # 库存的明确利空；工业硅/镍是投入品 → 放 cost。
    "电池": {"product": ("LC0",), "cost": ("NI0", "SI0")},
    # 光伏设备 / 风电设备：产出是组件/硅片/整机（无期货），工业硅/铜/铝/钢
    # 都是**投入品** → 只作 cost，不给预警（此前把铜价高位误报成风电「景气高位」）。
    "光伏设备": {"product": (), "cost": ("SI0", "CU0", "AL0")},
    "风电设备": {"product": (), "cost": ("CU0", "RB0")},
    # ── 化工：产品与原料务必分开（原油/煤/甲醇是成本项）──
    "化学原料": {"product": ("PP0", "EG0", "EB0"), "cost": ("SC0", "MA0")},
    # 化学纤维：涤纶/锦纶厂**买** PTA、乙二醇做丝 → 是成本，不是产品。
    "化学纤维": {"product": (), "cost": ("TA0", "EG0", "SC0")},
    # 塑料：制品厂**买**树脂 → 是成本。
    "塑料": {"product": (), "cost": ("PP0", "L0", "V0", "SC0")},
    # 橡胶：轮胎厂**买**橡胶（三级以橡胶制品为主）→ 是成本。
    "橡胶": {"product": (), "cost": ("RU0",)},
    # 化学制品：内部差异过大（涂料/民爆/氟化工/添加剂），且均无对应期货，
    # 任何代理都会产生假归属（实测把氟化工的永和股份标成「苯乙烯陷阱」）→ 移除。
    "农化制品": {"product": ("UR0",), "cost": ("SC0",)},
    # ── 建材 ──
    # 玻璃是玻璃厂的产出物；纯碱是它的**成本**（此前把纯碱跌算进产品侧）。
    "玻璃玻纤": {"product": ("FG0",), "cost": ("SA0",)},
    # 装修建材：管材/型材厂**买** PVC、PP → 是成本（此前把 PVC 跌 -23.7% 当成
    # 伟星新材「产品价格回落」，方向完全相反）。
    "装修建材": {"product": (), "cost": ("V0", "PP0", "SC0")},
    # ── 钢铁 / 煤炭 ──
    "普钢": {"product": ("RB0", "HC0"), "cost": ("I0", "JM0", "J0")},
    "特钢": {"product": ("RB0",), "cost": ("NI0", "I0")},
    "冶钢原料": {"product": ("I0",), "cost": ()},
    "焦炭": {"product": ("J0",), "cost": ("JM0",)},
    "煤炭开采": {"product": ("JM0", "J0"), "cost": ()},
    # ── 石化 / 油气 ──
    # 炼化及贸易：**原油是投入品**（此前把油价回落标成炼化产品利空，方向相反）；
    # 产出是成品油与化工品，用聚丙烯/PTA/乙二醇代理。
    "炼化及贸易": {"product": ("PP0", "TA0", "EG0"), "cost": ("SC0",)},
    "油气开采": {"product": ("SC0",), "cost": ()},
    # 油服工程：本身不产油，但收入随油价景气走 → 油价作为**景气代理**保留。
    "油服工程": {"product": ("SC0",), "cost": ()},
    # ── 造纸 / 纺服 / 农业 ──
    # 造纸厂**买**纸浆 → 成本。
    "造纸": {"product": (), "cost": ("SP0",)},
    # 纺织厂**买**棉花 → 成本。
    "纺织制造": {"product": (), "cost": ("CF0",)},
    "养殖业": {"product": ("LH0",), "cost": ("M0", "C0")},
    # 饲料厂**买**豆粕/玉米 → 成本。
    "饲料": {"product": (), "cost": ("M0", "C0")},
    "农产品加工": {"product": ("SR0", "C0"), "cost": ()},
    "种植业": {"product": ("C0", "SR0"), "cost": ()},
    # 渔业**买**豆粕做饲料 → 成本。
    "渔业": {"product": (), "cost": ("M0",)},
}

# ── 个股级补录（行业级映射漏判多元化公司时的显式修正）────────────
# 只补「行业归类看不到、但利润来源明确」的案例，每条都必须写清理由，
# 便于日后复核。不要用它来替代行业映射。
SYMBOL_COMMODITY_OVERRIDE: dict[str, dict[str, tuple[str, ...]]] = {
    # 盐湖股份：行业归「农化制品」（钾肥），但利润有相当部分来自碳酸锂
    # （蓝科锂业），行业级映射看不到锂敞口 → 个股级补录碳酸锂。
    "000792.SZ": {"product": ("LC0",), "cost": ()},
    # 玻纤：申万三级「玻璃玻纤」把玻璃厂（旗滨/南玻）与玻纤厂（巨石/中材）
    # 并在一起，但**玻纤没有期货品种** —— 拿玻璃价格代理会把玻纤股标成
    # 「玻璃价格回落陷阱」（实测中国巨石 yoy=73.87 被判 trap）。
    # 玻璃价格不是它们的产出物 → 显式置空，判 na（铁律 12：数据源无则不判定）。
    "600176.SH": {"product": (), "cost": ()},   # 中国巨石（玻纤）
    "002080.SZ": {"product": (), "cost": ()},   # 中材科技（玻纤/叶片）
}

_ROMAN_SUFFIX = ("Ⅱ", "Ⅲ", "Ⅰ", "II", "III", "I")

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def _norm_industry(ind: Any) -> str:
    """归一化行业名：去空白 + 去尾部罗马数字后缀（『特钢Ⅱ』→『特钢』）。"""
    s = str(ind or "").strip()
    for suf in _ROMAN_SUFFIX:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    return s


def _f(v: Any) -> float | None:
    """安全转 float；None/NaN/非数值一律 None（不赋 0）。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _fetch_raw(code: str) -> dict[str, Any] | None:
    """抓单个品种的日线收盘序列（不缓存，由 ``price_state`` 负责缓存）。"""
    import akshare as ak

    try:
        df = ak.futures_zh_daily_sina(symbol=code)
    except Exception as e:  # noqa: BLE001
        logger.warning("cycle_price 抓取失败 %s: %s", code, e)
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    closes = [_f(v) for v in df["close"].tolist()]
    dates = [str(v) for v in df["date"].tolist()] if "date" in df.columns else []
    pairs = [(d, c) for d, c in zip(dates, closes) if c is not None]
    if not pairs:
        return None
    return {"dates": [p[0] for p in pairs], "closes": [p[1] for p in pairs]}


def price_state(code: str, *, now: date | None = None) -> dict[str, Any] | None:
    """单品种价格状态（带进程内缓存）。

    Returns:
        ``{code, name, date, close, stale, stale_days, pos_pct, drawdown_pct,
        high, low, ma20, ma60, trend, ret_20_pct, ret_60_pct, window}``
        或 ``None``（无数据）。
    """
    today = now or date.today()
    with _CACHE_LOCK:
        hit = _CACHE.get(code)
        if hit is not None and time.time() - hit[0] < _TTL_SECONDS:
            return hit[1] or None

    # 兜底：抓取层即使抛出非预期异常（akshare 返回结构变化等）也只降级为无数据，
    # 绝不让预警把主视图打 500。
    try:
        raw = _fetch_raw(code)
    except Exception as e:  # noqa: BLE001
        logger.warning("cycle_price 抓取异常 %s: %s", code, e)
        raw = None
    if raw is None:
        with _CACHE_LOCK:
            _CACHE[code] = (time.time(), None)
        return None

    # 唯一一次算清，之后只读缓存
    last_date = raw["dates"][-1] if raw["dates"] else ""
    stale_days: int | None = None
    stale = False
    try:
        d = datetime.strptime(last_date[:10], "%Y-%m-%d").date()
        stale_days = (today - d).days
        stale = stale_days > _STALE_DAYS
    except (ValueError, TypeError):
        stale = True  # 日期读不出来 → 不敢用

    closes = raw["closes"][-_WINDOW:]
    close = closes[-1]
    hi = max(closes)
    lo = min(closes)
    pos_pct = 50.0 if hi == lo else round((close - lo) / (hi - lo) * 100.0, 1)
    drawdown_pct = 0.0 if hi == 0 else round((close - hi) / hi * 100.0, 2)
    ma20 = _mean(closes[-_MA_FAST:]) if len(closes) >= _MA_FAST else None
    ma60 = _mean(closes[-_MA_SLOW:]) if len(closes) >= _MA_SLOW else None
    trend = None
    if ma20 is not None and ma60 is not None:
        trend = "up" if ma20 > ma60 else "down"

    def _ret(n: int) -> float | None:
        if len(closes) <= n:
            return None
        base = closes[-n - 1]
        return None if base == 0 else round((close - base) / base * 100.0, 2)

    out = {
        "code": code,
        "name": MONITORED.get(code, code),
        "date": last_date,
        "close": close,
        "window": len(closes),
        "high": hi,
        "low": lo,
        "pos_pct": pos_pct,
        "drawdown_pct": drawdown_pct,
        "ma20": None if ma20 is None else round(ma20, 2),
        "ma60": None if ma60 is None else round(ma60, 2),
        "trend": trend,
        "ret_20_pct": _ret(20),
        "ret_60_pct": _ret(60),
        "stale": stale,
        "stale_days": stale_days,
    }
    with _CACHE_LOCK:
        _CACHE[code] = (time.time(), out)
    return out


def clear_cache() -> None:
    """清空进程内价格缓存（供 ``force=true`` 刷新用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def prefetch(codes: list[str] | tuple[str, ...]) -> None:
    """并行预热缓存（未缓存的才抓）。给读层用，避免逐票串行抓取。"""
    todo: list[str] = []
    with _CACHE_LOCK:
        now = time.time()
        for c in dict.fromkeys(codes):
            hit = _CACHE.get(c)
            if hit is None or now - hit[0] >= _TTL_SECONDS:
                todo.append(c)
    if not todo:
        return
    if len(todo) == 1:
        price_state(todo[0])
        return
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(todo))) as ex:
        list(ex.map(price_state, todo))


def commodity_meta(symbol: str, industry: str) -> dict[str, tuple[str, ...]] | None:
    """取某票的监测品种（个股补录优先于行业映射）。"""
    ov = SYMBOL_COMMODITY_OVERRIDE.get(str(symbol or "").strip().upper())
    if ov is not None:
        return ov
    return INDUSTRY_COMMODITY.get(_norm_industry(industry))


def _brief(s: dict[str, Any]) -> dict[str, Any]:
    """触发品种的紧凑摘要（给 ``trigger`` 用，避免把整段日线塞进 payload）。"""
    return {
        "code": s["code"],
        "name": s["name"],
        "close": s["close"],
        "pos_pct": s["pos_pct"],
        "drawdown_pct": s["drawdown_pct"],
        "trend": s["trend"],
    }


def assess(symbol: str, industry: str, profit_yoy_pct: Any) -> dict[str, Any]:
    """单票周期品价格预警（**只读，不改写任何分数**）。"""
    yoy = _f(profit_yoy_pct)
    meta = commodity_meta(symbol, industry)
    if meta is None:
        return {
            "grade": "na",
            "label": "无对应商品",
            "industry": industry,
            "industry_normalized": _norm_industry(industry),
            "mapped": False,
            "profit_yoy_pct": yoy,
            "products": [],
            "costs": [],
            "worst_product": None,
            "trigger": None,
            "notes": ["该申万三级行业未纳入商品监测表（行业级映射覆盖不到）。"],
        }

    prods = [s for c in meta.get("product", ()) if (s := price_state(c)) is not None]
    costs = [s for c in meta.get("cost", ()) if (s := price_state(c)) is not None]
    live = [s for s in prods if not s["stale"]]
    stale = [s for s in prods if s["stale"]]

    notes: list[str] = []
    declared_products = meta.get("product", ())
    if not declared_products:
        notes.append(
            "该行业的产出物没有对应期货品种（映射里只有成本项）→ 不判定，"
            "避免用原料价格冒充产品价格。"
        )
    elif not prods:
        notes.append("产品品种无数据，无法判定。")
    if stale:
        notes.append(
            "已剔除停更品种：" + "、".join(f"{s['name']}({s['date']})" for s in stale)
        )
    if len(live) > 1:
        notes.append(
            f"行业篮子共 {len(live)} 个品种，需过半数（{max(1, math.ceil(len(live) / 2))} 个）"
            "共振才定档，避免单一离群品种产生假归属。"
        )

    n = len(live)
    need = max(1, math.ceil(n / 2)) if n else 0
    fired_dd = sorted(
        [s for s in live if s["drawdown_pct"] <= FALLEN_DD_PCT],
        key=lambda s: s["drawdown_pct"],
    )
    fired_pos = sorted(
        [s for s in live if s["pos_pct"] >= PEAK_POS_PCT],
        key=lambda s: -s["pos_pct"],
    )

    grade = "normal"
    label = "无预警"
    trigger: dict[str, Any] | None = None
    if yoy is not None and yoy >= PROFIT_SURGE_PCT and n:
        if len(fired_dd) >= need:
            grade = "trap"
            label = (
                f"周期陷阱预警：报表利润暴增，{len(fired_dd)}/{n} 个产品价格已明确回落"
            )
            trigger = {
                "kind": "drawdown",
                "breadth": len(fired_dd),
                "total": n,
                "need": need,
                "products": [_brief(s) for s in fired_dd],
            }
        elif len(fired_pos) >= need:
            grade = "peak"
            label = (
                f"景气高位预警：利润暴增且 {len(fired_pos)}/{n} 个产品价格仍处高位"
            )
            trigger = {
                "kind": "position",
                "breadth": len(fired_pos),
                "total": n,
                "need": need,
                "products": [_brief(s) for s in fired_pos],
            }

    if grade == "normal":
        if not declared_products:
            grade, label = "na", "产出物无对应期货"
        elif not live:
            grade, label = "na", "产品价格数据缺失"

    worst = min(live, key=lambda s: s["drawdown_pct"]) if live else None

    return {
        "grade": grade,
        "label": label,
        "industry": industry,
        "industry_normalized": _norm_industry(industry),
        "mapped": True,
        "profit_yoy_pct": yoy,
        "worst_product": worst,
        "trigger": trigger,
        "products": prods,
        "costs": costs,
        "notes": notes,
    }


_CYCLE_PRICE_KEY = "cycle_price"


def attach_cycle_price(items: list[dict[str, Any]]) -> int:
    """给读层条目就地补 ``cycle_price`` 字段（返回被判定的条目数）。

    先并行预热缓存，再逐票判定 —— 避免逐票串行抓取把一次请求拖到几十秒。
    任何异常都不上抛：预警是**增强信息**，绝不能因为它让主视图 500。
    """
    if not items:
        return 0
    # 预热失败**不能**阻断附加：失败时逐票会各自降级为 na，而不是整体没有预警字段。
    try:
        codes: list[str] = []
        for it in items:
            meta = commodity_meta(it.get("symbol"), it.get("industry"))
            if meta:
                codes.extend(meta.get("product", ()))
                codes.extend(meta.get("cost", ()))
        prefetch(codes)
    except Exception as e:  # noqa: BLE001
        logger.warning("cycle_price 预热失败（逐票降级为 na）: %s", e)

    n = 0
    for it in items:
        try:
            it[_CYCLE_PRICE_KEY] = assess(
                it.get("symbol"), it.get("industry"), it.get("profit_yoy_pct")
            )
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("cycle_price 判定失败 %s: %s", it.get("symbol"), e)
            it[_CYCLE_PRICE_KEY] = {
                "grade": "na", "label": "判定异常", "mapped": False,
                "products": [], "costs": [], "worst_product": None,
                "trigger": None, "notes": [f"{type(e).__name__}"],
            }
    return n


def grade_of(item: dict[str, Any]) -> str:
    """便捷取档位（给排序/筛选用）。"""
    cp = item.get(_CYCLE_PRICE_KEY) or {}
    return str(cp.get("grade") or "na")


def commodity_board(*, now: date | None = None) -> dict[str, Any]:
    """全部监测品种的价格看板（供预警面板/端点直接消费）。"""
    codes = list(MONITORED.keys())
    prefetch(codes)
    states: list[dict[str, Any]] = []
    for c in codes:
        st = price_state(c, now=now)
        if st is not None:
            states.append(st)
    live = [s for s in states if not s["stale"]]
    stale = [s for s in states if s["stale"]]

    # 按「距高点回撤」升序（跌得最多的排前面 = 最需要警惕）
    live.sort(key=lambda s: s["drawdown_pct"])
    high_pos = sorted(live, key=lambda s: -s["pos_pct"])

    declared = sorted(set(MONITORED) - {s["code"] for s in states})
    return {
        "count": len(states),
        "live_count": len(live),
        "stale": [
            {"code": s["code"], "name": s["name"], "date": s["date"], "stale_days": s["stale_days"]}
            for s in stale
        ],
        "unavailable": declared,
        "most_fallen": live[:10],
        "highest_position": high_pos[:10],
        "items": states,
        "thresholds": {
            "profit_surge_pct": PROFIT_SURGE_PCT,
            "fallen_dd_pct": FALLEN_DD_PCT,
            "peak_pos_pct": PEAK_POS_PCT,
            "window": _WINDOW,
            "stale_days": _STALE_DAYS,
            "ttl_seconds": _TTL_SECONDS,
        },
        "notes": [
            "价格 = 期货主力连续收盘（新浪），窗口 %d 交易日。" % _WINDOW,
            "pos_pct = 区间位置（0=区间最低，100=区间最高）；"
            "drawdown_pct = 距区间高点回撤（≤0）。",
            "本看板**只供展示与预警**，不参与任何打分与硬门槛（理由见模块 docstring）。",
            "停更品种（如 ZC0 动力煤，2022-12 后无数据）一律剔除，不用旧价充现价。",
        ],
    }


def coverage_report(industries: list[str]) -> dict[str, Any]:
    """映射覆盖率自检：哪些行业能判定、哪些只能看成本、哪些完全没映射。

    分三桶的理由：**「能拿到数据」不等于「能下结论」**。像风电设备、塑料、
    装修建材这类只映射到成本项的行业，数据是有的但产品无现货期货，
    必须与「完全没映射」区分开，否则排查「为什么这票没预警」时会看错方向。
    """
    judged: dict[str, int] = {}
    cost_only: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for raw in industries:
        ind = str(raw or "").strip() or "<空>"
        norm = _norm_industry(ind)
        meta = INDUSTRY_COMMODITY.get(norm)
        if meta is None:
            unmapped[ind] = unmapped.get(ind, 0) + 1
        elif meta.get("product"):
            judged[ind] = judged.get(ind, 0) + 1
        else:
            cost_only[ind] = cost_only.get(ind, 0) + 1
    return {
        "judged_industries": len(judged),
        "cost_only_industries": len(cost_only),
        "unmapped_industries": len(unmapped),
        "judged_detail": dict(sorted(judged.items(), key=lambda kv: -kv[1])),
        "cost_only_detail": dict(sorted(cost_only.items(), key=lambda kv: -kv[1])),
        "unmapped_detail": dict(sorted(unmapped.items(), key=lambda kv: -kv[1])),
    }
