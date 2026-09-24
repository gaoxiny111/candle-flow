"""本地单测：cycle_price 周期品「产品价格拐点」预警（纯函数，**零外网依赖**）。

靶点（每条都对应一个真实踩过的坑）：
- **product / cost 必须分开**：原油是化学原料的**成本项**，油价跌是利好；
  若不分侧，会把卫星化学、宝丰能源这类「油价上涨受益」的票标反。
- **停更品种不得充现价**：实测 ZC0（动力煤）停在 2022-12-30，
  必须按 stale 剔除，否则会用四年前的煤价做判断。
- **行业名要归一化**：本库真实行业名带后缀（『特钢Ⅱ』『调味发酵品Ⅱ』）。
- **只读**：预警绝不改写 qv_score / composite_score。
- **不让主视图 500**：抓取失败必须降级为 na，异常不上抛。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.services import cycle_price as cp  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    cp.clear_cache()
    yield
    cp.clear_cache()


def _dates(n: int, end: date | None = None) -> list[str]:
    end = end or date.today()
    return [(end - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]


def _series(closes: list[float], *, end: date | None = None, stale_days: int = 0):
    """构造一段日线：**末尾**锚定在 ``end - stale_days``（stale_days=0 即最新）。"""
    end = end or date.today()
    last = end - timedelta(days=stale_days)
    return {"dates": _dates(len(closes), end=last), "closes": list(closes)}


def _patch(monkeypatch, mapping: dict[str, list[float] | None], *, stale_days: dict | None = None):
    """把 ``_fetch_raw`` 换成按 code 返回的假序列。``None`` 表示抓取失败。"""
    stale_days = stale_days or {}

    def fake(code: str, **_kw):
        if code not in mapping:
            return None
        closes = mapping[code]
        if closes is None:
            return None
        return _series(closes, stale_days=stale_days.get(code, 0))

    monkeypatch.setattr(cp, "_fetch_raw", fake)


def _flat(n=300, v=100.0):
    return [v] * n


# ── 拐点判定 ─────────────────────────────────────────────────
def test_trap_when_profit_surges_and_product_price_falls(monkeypatch):
    """报表暴增 + 产品价从高位明确回落 → 周期陷阱（融捷/天赐型：锂价 -37%）。"""
    # 前 200 天在 200 附近，最后 100 天跌到 126 → 回撤约 -37%
    prices = [200.0] * 200 + [126.0] * 100
    _patch(monkeypatch, {"LC0": prices})
    r = cp.assess("002192.SZ", "能源金属", 1076.14)
    assert r["grade"] == "trap", r
    assert r["mapped"] is True
    assert r["worst_product"]["code"] == "LC0"
    assert r["worst_product"]["drawdown_pct"] <= cp.FALLEN_DD_PCT


def test_no_trap_when_profit_not_surging(monkeypatch):
    """价格回落但报表没暴增 → 不算陷阱（成长质量本来就没虚高）。"""
    prices = [200.0] * 200 + [126.0] * 100
    _patch(monkeypatch, {"LC0": prices})
    r = cp.assess("002192.SZ", "能源金属", 10.0)
    assert r["grade"] == "normal", r


def test_peak_watch_when_price_still_at_high(monkeypatch):
    """报表暴增 + 产品价仍在区间高位（未回落）→ 景气高位预警（株冶型：铝锌仅 -5%）。"""
    prices = [100.0] * 100 + [160.0] * 200
    _patch(monkeypatch, {"AL0": prices, "ZN0": prices, "CU0": prices})
    r = cp.assess("600961.SH", "工业金属", 232.75)
    assert r["grade"] == "peak", r
    assert r["worst_product"]["pos_pct"] >= cp.PEAK_POS_PCT


# ── product / cost 必须分开（防标反）─────────────────────────
def test_cost_falling_is_not_a_warning(monkeypatch):
    """化学原料：原油（**成本项**）跌 -37%，产品（PP/EG/EB）未跌 → 不得报陷阱。

    这是最容易标反的一条：油价回落对煤制烯烃/乙烷裂解是**成本改善**。
    """
    _patch(
        monkeypatch,
        {
            "SC0": [200.0] * 200 + [126.0] * 100,   # 原油暴跌（成本项）
            "MA0": [200.0] * 200 + [126.0] * 100,   # 甲醇跌（成本项）
            "PP0": [100.0] * 250 + [101.0] * 50,    # 产品横盘
            "EG0": [100.0] * 250 + [101.0] * 50,
            "EB0": [100.0] * 250 + [102.0] * 50,
        },
    )
    r = cp.assess("002648.SZ", "化学原料", 126.94)
    assert r["grade"] != "trap", r
    # 成本项仍应回传（作为背景信息），但不参与预警
    assert any(c["code"] == "SC0" for c in r["costs"])
    assert all(c["code"] != "SC0" for c in r["products"])


def test_product_falling_in_chemical_does_trigger(monkeypatch):
    """同一行业：**产品**（聚丙烯）跌 -37% → 必须报陷阱（对照组）。"""
    _patch(
        monkeypatch,
        {
            "SC0": _flat(300, 100.0),
            "MA0": _flat(300, 100.0),
            "PP0": [200.0] * 200 + [126.0] * 100,
            "EG0": [200.0] * 200 + [126.0] * 100,
            "EB0": _flat(300, 100.0),
        },
    )
    r = cp.assess("002648.SZ", "化学原料", 126.94)
    assert r["grade"] == "trap", r


# ── 篮子共振：不许拿单一离群品种定档（防假归属）────────────────
def _seg(segments: dict[float, int]) -> list[float]:
    """按 {价位: 根数} 拼一段日线（总长 250 = 统计窗口全长）。"""
    out: list[float] = []
    for price, n in segments.items():
        out.extend([price] * n)
    return out


_HIGH = _seg({100.0: 40, 150.0: 210})    # pos=100, dd=0      → 触发「高位」
_LOW = _seg({150.0: 40, 100.0: 210})     # pos=0,   dd=-33.3  → 触发「已回落」
_MID = _seg({100.0: 40, 150.0: 30, 130.0: 180})  # pos=60, dd=-13.3 → 两侧都不触发


def test_basket_needs_majority_not_single_outlier(monkeypatch):
    """工业金属 5 个品种只有 1 个在高位 → **不得**报景气高位。

    旧实现取「回撤最大」的品种再判断它是否高位，会让单一品种代表整个篮子；
    现在要求过半数（5 → 3）共振。
    """
    _patch(monkeypatch, {
        "CU0": _HIGH, "AL0": _MID, "ZN0": _MID, "PB0": _MID, "SN0": _MID,
    })
    r = cp.assess("002532.SZ", "工业金属", 100.44)
    assert r["grade"] == "normal", r
    assert r["trigger"] is None


def test_trigger_reports_which_products_fired(monkeypatch):
    """过半数共振时才定档，且 **trigger 必须列出实际触发的品种**（可自证）。

    这是「天山铝业最弱品种被算成沪锡」那个假归属的回归靶点：结论要指向
    真正处高位的那些品种，而不是篮子里跌得最多的那一个。
    """
    _patch(monkeypatch, {
        "CU0": _HIGH, "AL0": _HIGH, "ZN0": _HIGH, "PB0": _MID, "SN0": _MID,
    })
    r = cp.assess("002532.SZ", "工业金属", 100.44)
    assert r["grade"] == "peak", r
    tg = r["trigger"]
    assert tg["kind"] == "position"
    assert (tg["breadth"], tg["total"], tg["need"]) == (3, 5, 3)
    codes = {p["code"] for p in tg["products"]}
    assert codes == {"CU0", "AL0", "ZN0"}
    assert "PB0" not in codes and "SN0" not in codes
    # 所有触发品种都确实在高位
    assert all(p["pos_pct"] >= cp.PEAK_POS_PCT for p in tg["products"])


def test_basket_majority_drawdown_triggers_trap(monkeypatch):
    """多数产品明确回落 + 利润暴增 → 报陷阱（对照组）。"""
    _patch(monkeypatch, {
        "CU0": _LOW, "AL0": _LOW, "ZN0": _LOW, "PB0": _MID, "SN0": _MID,
    })
    r = cp.assess("600111.SH", "工业金属", 120.0)
    assert r["grade"] == "trap", r
    assert r["trigger"]["kind"] == "drawdown"
    assert r["trigger"]["breadth"] == 3


# ── 中下游行业：商品是投入品，不得当产品报警 ─────────────────
@pytest.mark.parametrize(
    ("symbol", "industry", "code"),
    [
        ("002202.SZ", "风电设备", "CU0"),      # 整机厂买铜/钢
        ("002372.SZ", "装修建材", "V0"),       # 管材厂买 PVC（曾把伟星新材标反）
        ("600143.SH", "塑料", "PP0"),          # 制品厂买树脂
        ("002493.SZ", "化学纤维", "TA0"),      # 涤纶厂买 PTA
        ("000876.SZ", "饲料", "M0"),           # 饲料厂买豆粕
    ],
)
def test_input_commodity_crash_is_not_a_warning(monkeypatch, symbol, industry, code):
    """原料暴跌对中下游是**成本改善**，绝不能报成「产品价格回落」陷阱。"""
    _patch(monkeypatch, {code: _LOW, "SC0": _LOW, "RB0": _LOW, "AL0": _LOW,
                         "L0": _LOW, "EG0": _LOW, "C0": _LOW})
    r = cp.assess(symbol, industry, 300.0)
    assert r["grade"] == "na", r
    assert all(c["code"] != code for c in r["products"])
    assert any(c["code"] == code for c in r["costs"])
    assert any("产出物" in n for n in r["notes"])


# ── 无代表性期货的行业：宁判 na，不给假归属 ────────────────────
@pytest.mark.parametrize("industry", ["小金属", "金属新材料", "化学制品"])
def test_industries_without_representative_futures_are_unmapped(industry):
    """稀土/钨/钼（小金属）、氟化工等无对应期货 → 不判定，不得拿锡/镍/苯乙烯冒充。

    旧表把小金属映射到 (沪锡, 沪镍)，导致不产锡镍的北方稀土、厦门钨业被标成
    「锡/镍陷阱」——这是必须留痕的假归属。
    """
    assert cp.commodity_meta("600111.SH", industry) is None
    r = cp.assess("600111.SH", industry, 120.46)
    assert r["grade"] == "na" and r["mapped"] is False


def test_battery_lithium_fall_still_warns(monkeypatch):
    """电池：碳酸锂是锂电中游定价锚，跌 -33% + 利润暴增 → 仍须报陷阱（防漏杀）。"""
    _patch(monkeypatch, {"LC0": _LOW})
    r = cp.assess("002709.SZ", "电池", 967.91)
    assert r["grade"] == "trap", r
    assert r["trigger"]["products"][0]["code"] == "LC0"


def test_fiberglass_not_judged_by_glass_price(monkeypatch):
    """玻纤厂不得用「玻璃」价代理：三级行业把玻璃厂与玻纤厂并在一起，
    玻璃是旗滨/南玻的产出物，不是中国巨石的 → 个股补录置空，判 na。"""
    _patch(monkeypatch, {"FG0": _LOW, "SA0": _LOW})
    r = cp.assess("600176.SH", "玻璃玻纤", 73.87)
    assert r["grade"] == "na", r
    assert r["products"] == []
    # 同行业的玻璃厂仍按玻璃判定（不能被一起关掉）
    r2 = cp.assess("601636.SH", "玻璃玻纤", 73.87)
    assert r2["grade"] == "trap", r2
    assert r2["trigger"]["products"][0]["code"] == "FG0"



def test_stale_commodity_excluded_from_decision(monkeypatch):
    """停更品种（动力煤型）必须剔除，不得用旧价判定。"""
    # 序列本身暴跌，但最新数据是 400 天前 → stale
    old = [200.0] * 200 + [10.0] * 100
    _patch(monkeypatch, {"JM0": old, "J0": _flat(300, 100.0)}, stale_days={"JM0": 400})
    st = cp.price_state("JM0")
    assert st["stale"] is True and st["stale_days"] > cp._STALE_DAYS

    r = cp.assess("600000.SH", "焦炭", 300.0)
    # JM0 是成本项 → 本就无关；关键是 J0（产品）未跌 → 不报陷阱，且 notes 留痕
    assert r["grade"] != "trap", r


def test_all_products_stale_gives_na(monkeypatch):
    """产品品种全部停更 → 判 na，不能凭停更数据给结论。"""
    _patch(monkeypatch, {"JM0": [200.0] * 100 + [50.0] * 100}, stale_days={"JM0": 400})
    r = cp.assess("600000.SH", "煤炭开采", 300.0)
    assert r["grade"] == "na", r
    assert any("停更" in n for n in r["notes"])


def test_fetch_failure_degrades_to_na(monkeypatch):
    """抓取失败 → 降级 na，不得抛异常（预警不能把主视图打 500）。"""
    _patch(monkeypatch, {"LC0": None})
    r = cp.assess("002192.SZ", "能源金属", 100.0)
    assert r["grade"] == "na"
    assert r["mapped"] is True


def test_unmapped_industry_is_na():
    """未纳入监测表的行业 → na 且可见（不是静默放行）。"""
    r = cp.assess("600519.SH", "白酒Ⅱ", -1.95)
    assert r["grade"] == "na" and r["mapped"] is False


# ── 行业名归一化 / 个股补录 ──────────────────────────────────
def test_industry_normalization_strips_roman_suffix(monkeypatch):
    """本库真实三级名带 Ⅱ 后缀（『特钢Ⅱ』），映射必须能命中。"""
    _patch(monkeypatch, {"RB0": _flat(300, 100.0), "NI0": _flat(300, 100.0), "I0": _flat(300, 100.0)})
    assert cp._norm_industry("特钢Ⅱ") == "特钢"
    assert cp.commodity_meta("000001.SZ", "特钢Ⅱ") is not None
    assert cp.commodity_meta("000001.SZ", "调味发酵品Ⅱ") is None


def test_symbol_override_takes_priority(monkeypatch):
    """个股补录优先于行业映射（盐湖股份：行业=农化制品，但利润含碳酸锂）。"""
    _patch(monkeypatch, {"LC0": [200.0] * 200 + [126.0] * 100, "UR0": _flat(300, 100.0)})
    meta = cp.commodity_meta("000792.SZ", "农化制品")
    assert meta is not None and "LC0" in meta["product"]
    r = cp.assess("000792.SZ", "农化制品", 137.88)
    assert r["grade"] == "trap", r


# ── 只读：不改写任何分数 ─────────────────────────────────────
def test_attach_does_not_touch_scores(monkeypatch):
    """预警是只读增强：qv_score / composite_score 一字不改。"""
    _patch(monkeypatch, {"LC0": [200.0] * 200 + [126.0] * 100})
    items = [
        {
            "symbol": "002192.SZ", "name": "融捷股份", "industry": "能源金属",
            "profit_yoy_pct": 1076.14, "qv_score": 92.0, "composite_score": 78.4,
            "qv_reasons": [],
        },
        {
            "symbol": "600519.SH", "name": "贵州茅台", "industry": "白酒Ⅱ",
            "profit_yoy_pct": -1.95, "qv_score": 68.3, "composite_score": 91.0,
            "qv_reasons": [],
        },
    ]
    before = [(dict(i)) for i in items]
    n = cp.attach_cycle_price(items)
    assert n == 2
    for a, b in zip(items, before):
        assert a["qv_score"] == b["qv_score"]
        assert a["composite_score"] == b["composite_score"]
        assert a["qv_reasons"] == b["qv_reasons"]
    assert items[0]["cycle_price"]["grade"] == "trap"
    assert items[1]["cycle_price"]["grade"] == "na"


def test_attach_survives_total_fetch_failure(monkeypatch):
    """整体抓取炸掉也必须返回、且条目仍可读。"""
    def boom(code, **_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(cp, "_fetch_raw", boom)
    items = [{"symbol": "002192.SZ", "industry": "能源金属", "profit_yoy_pct": 100.0}]
    n = cp.attach_cycle_price(items)
    assert n == 1
    assert items[0]["cycle_price"]["grade"] in ("na",)


def test_commodity_board_shape(monkeypatch):
    """看板：停更品种进 stale 列表，不混进 most_fallen。"""
    _patch(monkeypatch, {c: _flat(300, 100.0) for c in cp.MONITORED})
    board = cp.commodity_board()
    assert board["count"] == len([c for c in cp.MONITORED])
    assert board["live_count"] + len(board["stale"]) == board["count"]
    assert all(not s["stale"] for s in board["most_fallen"])
    # ZC0（动力煤）不得出现在监测表里
    assert "ZC0" not in cp.MONITORED
