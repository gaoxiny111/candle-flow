"""全市场排序读层回归测试。

背景：`factor_snapshots` 此前**只写不读**（除自身模块外零引用），
即「全市场基本面打分排序」一直缺输出层。本轮补上读层，需保证：

1. 排序沿用既有 `composite_score`，不新造第二套权重（同一判定单一来源）；
2. 分位是展示列、不参与排序；缺失值不得被赋 50 分中性值；
3. 覆盖率必须自证（已覆盖 / SH·SZ 总数），不得默认宣称已覆盖全市场。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.factor_snapshot import FactorSnapshot
from app.models.stock import StockInfo
from app.services import market_scan as ms


def _payload(
    name: str,
    industry: str,
    *,
    mcap: float,
    dims: dict,
    rating: str = "B",
    price: float | None = None,
) -> str:
    return json.dumps(
        {
            "name": name,
            "industry": industry,
            "final_rating": rating,
            "risk_level_label": "中",
            "market": {
                "market_cap": mcap,
                "price": price,
                "pb": 1.5,
                "dividend_yield": 2.0,
            },
            "dim_scores": dims,
        },
        ensure_ascii=False,
    )


@pytest.fixture(autouse=True)
def _reset_overlay_caches(monkeypatch):
    """叠加层有两级进程内缓存（请求级 + 单票级），测试间必须隔离。

    行情覆盖层也必须隔离：默认置为「行情源不可用」，否则读层会去真实抓腾讯
    快照，把 fixture 里的 price 缩放掉（测试变成依赖外网 + 时间）。
    需要行情覆盖的用例自会用 monkeypatch 覆盖 ``_market_snapshot``。
    """
    ms._overlay_cache.update({"ts": 0.0, "key": None, "payload": None})
    ms._overlay_item_cache.clear()
    ms._reso_cache.update({"ts": 0.0, "items": None, "coverage": None, "stats": None})
    ms.invalidate_light_cache()
    ms._BASE_COUNTERS.update(hits=0, misses=0)
    ms._QUOTE_LAST["data"] = {"applied": False, "reason": "never_ran"}
    monkeypatch.setattr(
        ms,
        "_market_snapshot",
        lambda: {"ok": False, "items": {}, "errors": ["test_stub"]},
    )
    yield
    ms._overlay_cache.update({"ts": 0.0, "key": None, "payload": None})
    ms._overlay_item_cache.clear()
    ms._reso_cache.update({"ts": 0.0, "items": None, "coverage": None, "stats": None})
    ms.invalidate_light_cache()
    ms._BASE_COUNTERS.update(hits=0, misses=0)


def _make_db(snapshots, universe):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for sym, name, payload in snapshots:
        db.add(
            FactorSnapshot(
                symbol=sym,
                composite_score=payload["comp"],
                pe_ttm=10.0,
                payload=payload.get("raw")
                or _payload(
                    name,
                    payload.get("ind", "煤炭开采"),
                    mcap=payload.get("mcap", 5e10),
                    dims=payload.get("dims", {}),
                    price=payload.get("price"),
                ),
            )
        )
    for sym in universe:
        db.add(StockInfo(symbol=sym, code=sym[:6], name="占位", market=sym[-2:]))
    db.commit()
    return db


def test_scan_reuses_composite_score_and_discloses_coverage():
    """排序必须用既有 composite_score；覆盖率与分位基准必须自证。"""
    db = _make_db(
        [
            ("600001.SH", "高分公司", {"comp": 88.0, "ind": "酿酒", "mcap": 2e11}),
            ("600002.SH", "中分公司", {"comp": 70.0, "ind": "酿酒", "mcap": 5e10}),
            ("600003.SH", "ST退市风险", {"comp": 95.0, "ind": "酿酒"}),
            ("600004.SH", "低分公司", {"comp": 40.0, "ind": "煤炭开采"}),
        ],
        # 股票池 6 只，只有 4 只有快照 → 覆盖率必须显式给出
        ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH", "600006.SH"],
    )
    try:
        data = ms.scan_market(db, top=10)
        symbols = [i["symbol"] for i in data["items"]]
        # ST 被剔除；其余按综合分降序
        assert "600003.SH" not in symbols
        assert symbols == ["600001.SH", "600002.SH", "600004.SH"]
        # 综合分原样透传，未被扫描层改写
        assert data["items"][0]["composite_score"] == 88.0
        assert data["items"][0]["final_rating"] == "B"
        # 覆盖率自证：股票池 6 只、已构建 4 只 → 缺口必须显式暴露
        cov = data["coverage"]
        assert cov["covered"] == 4
        assert cov["universe"] == 6
        assert cov["remaining"] == 2
        assert cov["coverage_pct"] == 66.7
        assert cov["complete"] is False
        assert cov["latest_built_at"] is not None
        # 分位基准 = 全部已覆盖样本（含被过滤掉的 ST），不是过滤后的 3 只
        assert data["percentile_base"] == 4
        # 4 只样本 [40,70,88,95]：88 → 平均秩 2.5/4 = 62.5；40 → 0.5/4 = 12.5
        assert data["items"][0]["market_pct"] == 62.5
        assert data["items"][-1]["market_pct"] == 12.5
    finally:
        db.close()


def test_quote_overlay_scales_display_fields_not_scores(monkeypatch):
    """行情覆盖：股价/市值/PE/PB 按现价缩放、股息率反缩放；分数与分位纹丝不动。"""
    db = _make_db(
        [
            ("600001.SH", "甲", {"comp": 88.0, "ind": "酿酒", "mcap": 1e11, "price": 100.0}),
        ],
        ["600001.SH"],
    )
    # 腾讯快照现价 111.12 vs 快照构建时价 100.00 → ratio 1.1112
    monkeypatch.setattr(
        ms,
        "_market_snapshot",
        lambda: {
            "ok": True,
            "items": {
                "600001.SH": {
                    "price": 111.12,
                    "prev_close": 100.0,
                    "date": "20260928",
                    "ts": "2026-09-28 15:00:00",
                }
            },
            "source": "tencent",
            "ts": "2026-09-28 15:00:00",
            "date": "20260928",
            "errors": [],
        },
    )
    try:
        data = ms.scan_market(db, top=10)
        it = data["items"][0]
        assert it["price"] == 111.12
        # PE 由快照载荷 10.0 → 10.0 * 1.1112
        assert it["pe_ttm"] == pytest.approx(11.112, abs=1e-3)
        assert it["pb"] == pytest.approx(1.5 * 1.1112, abs=1e-3)
        assert it["market_cap_yi"] == pytest.approx(1e11 * 1.1112 / 1e8, abs=1e-2)
        assert it["dividend_yield"] == pytest.approx(2.0 / 1.1112, abs=1e-3)
        # 分数与分位锚定报告期，绝不被行情改写
        assert it["composite_score"] == 88.0
        assert it["market_pct"] == 50.0
        # 自证字段
        ov = data["quote_overlay"]
        assert ov["applied"] is True
        assert ov["applied_count"] == 1
        assert ov["as_of"] == "2026-09-28 15:00:00"
    finally:
        db.close()


def test_quote_overlay_skipped_when_source_down():
    """行情源不可用 → 整体跳过并留痕，展示退回收盘价，榜单不得挂掉。"""
    db = _make_db(
        [("600001.SH", "甲", {"comp": 88.0, "ind": "酿酒", "mcap": 1e11, "price": 100.0})],
        ["600001.SH"],
    )
    try:
        data = ms.scan_market(db, top=10)  # autouse fixture 已把快照置为不可用
        assert data["items"][0]["price"] == 100.0
        ov = data["quote_overlay"]
        assert ov["applied"] is False
        assert ov["reason"] == "snapshot_unavailable"
        # notes 必须提示未生效，不得让用户以为看到的是最新价
        assert any("行情覆盖未生效" in n for n in data["notes"])
    finally:
        db.close()


def test_quote_overlay_survives_snapshot_exception(monkeypatch):
    """快照抛异常也必须吞掉（覆盖层失败不得影响榜单可用性）。"""
    def _boom():
        raise RuntimeError("network exploded")

    monkeypatch.setattr(ms, "_market_snapshot", _boom)
    db = _make_db(
        [("600001.SH", "甲", {"comp": 88.0, "ind": "酿酒", "mcap": 1e11, "price": 100.0})],
        ["600001.SH"],
    )
    try:
        data = ms.scan_market(db, top=10)
        assert data["items"][0]["price"] == 100.0
        assert data["quote_overlay"]["reason"] == "snapshot_error"
    finally:
        db.close()


def test_quote_overlay_skips_bad_price(monkeypatch):
    """停牌/异常价（≤0）单条跳过，保留快照价。"""
    monkeypatch.setattr(
        ms,
        "_market_snapshot",
        lambda: {
            "ok": True,
            "items": {"600001.SH": {"price": 0.0}, "600002.SH": {"price": 5.0}},
            "source": "tencent",
            "ts": "2026-09-28 15:00:00",
        },
    )
    db = _make_db(
        [
            ("600001.SH", "停牌", {"comp": 88.0, "ind": "酿酒", "mcap": 1e11, "price": 100.0}),
            ("600002.SH", "正常", {"comp": 70.0, "ind": "酿酒", "mcap": 1e11, "price": 2.5}),
        ],
        ["600001.SH", "600002.SH"],
    )
    try:
        data = ms.scan_market(db, top=10)
        by = {i["symbol"]: i for i in data["items"]}
        assert by["600001.SH"]["price"] == 100.0  # 异常价：保留
        assert by["600002.SH"]["price"] == 5.0  # 正常：覆盖
        ov = data["quote_overlay"]
        assert ov["applied_count"] == 1
        assert ov["missed_bad_price"] == 1
    finally:
        db.close()


def test_market_coverage_uses_main_board_universe_and_flags_disclosure(monkeypatch):
    """coverage 的 universe 必须与 build_all 同源（主板），并自证披露窗口。"""
    db = _make_db(
        [("600001.SH", "甲", {"comp": 88.0, "ind": "酿酒", "mcap": 1e11})],
        # 3 只主板 + 2 只双创 → universe 只算主板
        ["600001.SH", "600002.SH", "600003.SH", "300001.SZ", "688001.SH"],
    )
    try:
        cov = ms.market_coverage(db)
        assert cov["covered"] == 1
        assert cov["universe"] == 3
        assert cov["remaining"] == 2
        assert cov["excluded_non_main"] == 2  # 双创显式暴露，但不计入缺口
        assert isinstance(cov["disclosure_window"], bool)
    finally:
        db.close()


def test_scan_filters_and_industry_percentile():
    db = _make_db(
        [
            ("600001.SH", "酿酒甲", {"comp": 80.0, "ind": "酿酒", "mcap": 1e11}),
            ("600002.SH", "酿酒乙", {"comp": 60.0, "ind": "酿酒", "mcap": 3e9}),
            ("600011.SH", "煤炭甲", {"comp": 90.0, "ind": "煤炭开采", "mcap": 1e11}),
            ("600012.SH", "煤炭乙", {"comp": 50.0, "ind": "煤炭开采", "mcap": 1e11}),
            ("600021.SH", "小市值", {"comp": 99.0, "ind": "酿酒", "mcap": 1e9}),
        ],
        ["600001.SH"],
    )
    try:
        # 行业过滤 + 市值下限（亿元）→ 只剩酿酒甲
        data = ms.scan_market(db, top=10, industry="酿酒", min_market_cap_yi=50)
        assert [i["symbol"] for i in data["items"]] == ["600001.SH"]
        # 行业内分位在「酿酒」组内计算（组内 80/60/99 → 80 取平均秩 1.5/3 = 50.0）；
        # 被市值过滤的 600021 仍计入分位基准，故分位不是过滤后重排
        assert data["items"][0]["industry_pct"] == 50.0
        # 综合分下限
        data2 = ms.scan_market(db, top=10, min_composite=85)
        assert [i["symbol"] for i in data2["items"]] == ["600021.SH", "600011.SH"]
    finally:
        db.close()


def test_scan_keyword_and_gem_default_excluded():
    """keyword 名称/代码搜索；创业板/科创板默认剔除、include_gem=True 才纳入。"""
    db = _make_db(
        [
            ("600519.SH", "贵州茅台", {"comp": 90.0, "ind": "酿酒"}),
            ("300750.SZ", "宁德时代", {"comp": 85.0, "ind": "电池"}),
            ("688002.SH", "睿创微纳", {"comp": 80.0, "ind": "电子"}),
            ("000001.SZ", "平安银行", {"comp": 60.0, "ind": "银行"}),
        ],
        ["600519.SH", "300750.SZ", "688002.SH", "000001.SZ"],
    )
    try:
        # 默认：创业板/科创板不在榜
        data = ms.scan_market(db, top=10)
        syms = [i["symbol"] for i in data["items"]]
        assert syms == ["600519.SH", "000001.SZ"]
        assert data["filters"]["include_gem"] is False

        # 勾选纳入后回到综合分排序
        data2 = ms.scan_market(db, top=10, include_gem=True)
        assert [i["symbol"] for i in data2["items"]] == [
            "600519.SH", "300750.SZ", "688002.SH", "000001.SZ",
        ]

        # keyword 命中名称（大小写不敏感、子串匹配）
        data3 = ms.scan_market(db, top=10, keyword="茅台", include_gem=True)
        assert [i["symbol"] for i in data3["items"]] == ["600519.SH"]
        # keyword 命中代码前缀
        data4 = ms.scan_market(db, top=10, keyword="688", include_gem=True)
        assert [i["symbol"] for i in data4["items"]] == ["688002.SH"]
        # keyword 无命中 → 空榜但结构自证
        data5 = ms.scan_market(db, top=10, keyword="不存在")
        assert data5["items"] == [] and data5["matched"] == 0
        assert data5["filters"]["keyword"] == "不存在"
    finally:
        db.close()


def test_dim_sort_is_display_only_and_missing_not_neutralised():
    """按维度排序只改展示顺序；缺失维度不赋 50 分、排在末尾。"""
    db = _make_db(
        [
            ("600001.SH", "现金流强", {"comp": 60.0, "dims": {"现金流质量": 92.0}}),
            ("600002.SH", "现金流弱", {"comp": 85.0, "dims": {"现金流质量": 30.0}}),
            ("600003.SH", "无该维度", {"comp": 90.0, "dims": {}}),
        ],
        ["600001.SH"],
    )
    try:
        data = ms.scan_market(db, top=10, sort_by="cashflow")
        items = data["items"]
        assert [i["symbol"] for i in items] == ["600001.SH", "600002.SH", "600003.SH"]
        # 缺失值保持 None，未被塞成 50 分中性值
        assert items[2]["dim_scores"]["现金流质量"] is None
        # 综合分不受维度排序影响
        assert [i["composite_score"] for i in items] == [60.0, 85.0, 90.0]
        # 中文维度名亦可
        assert ms.scan_market(db, top=1, sort_by="现金流质量")["items"][0]["symbol"] == "600001.SH"
        with pytest.raises(ValueError):
            ms.scan_market(db, top=1, sort_by="不存在的维度")
    finally:
        db.close()


def test_load_covered_falls_back_when_json_extract_unavailable(monkeypatch):
    """SQLite JSON1 不可用时回退 Python 解析，结果口径不变。"""
    db = _make_db(
        [("600001.SH", "白酒甲", {"comp": 77.0, "ind": "酿酒", "dims": {"盈利能力": 88.0}})],
        ["600001.SH"],
    )
    try:
        monkeypatch.setattr(ms, "_LIGHT_SQL", ms.text("SELECT * FROM __no_such_table__"))
        rows, fallback = ms.load_covered(db)
        assert fallback is True
        assert len(rows) == 1
        assert rows[0]["name"] == "白酒甲"
        assert rows[0]["industry"] == "酿酒"
        assert rows[0]["dim_profitability"] == 88.0
        data = ms.scan_market(db, top=5)
        assert data["items"][0]["symbol"] == "600001.SH"
        assert any("回退路径" in n for n in data["notes"])
    finally:
        db.close()


def test_build_all_caps_batch_and_reports_coverage(monkeypatch, tmp_path):
    """build_all 支持 max_symbols / budget_sec，并回传覆盖率。"""
    from app.services import factor_db as mod

    # 用文件库 + check_same_thread=False：build_all 走线程池，内存库会跨线程报错
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scan.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    setup = Session()
    # 股票池只含主板：_get_all_symbols 已改为只扫主板（剔除创业板 300/301、
    # 科创板 688/689），与读层榜单默认 include_gem=False 对齐。
    for sym in ("600000.SH", "000001.SZ", "600519.SH"):
        setup.add(StockInfo(symbol=sym, code=sym[:6], name="测试", market=sym[-2:]))
    setup.commit()
    setup.close()

    monkeypatch.setattr(mod, "SessionLocal", Session)

    def _fake_upsert(sym: str, report: dict) -> None:
        db = Session()
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == sym).first()
        if row is None:
            row = FactorSnapshot(symbol=sym)
            db.add(row)
        row.payload = json.dumps(report)
        row.composite_score = report.get("composite_score")
        db.commit()
        db.close()

    monkeypatch.setattr(mod, "upsert", _fake_upsert)
    import app.analysis.engine as eng

    monkeypatch.setattr(
        eng,
        "analyze_symbol_full",
        lambda db, symbol, use_cache=True, **k: {"composite_score": 60.0, "market": {}},
    )

    stats = mod.build_all(budget_sec=120, max_symbols=2)
    assert stats["built"] == 2
    assert stats["failed"] == 0
    assert stats["budget_sec"] == 120
    assert stats["truncated"] is False
    # 3 只股票池，只建 2 只 → 覆盖率显式暴露缺口
    assert stats["coverage"]["universe"] == 3
    assert stats["coverage"]["covered"] == 2
    assert stats["coverage"]["remaining"] == 1
    assert stats["coverage"]["coverage_pct"] == 66.7


def test_build_all_schema_guard_rebuilds_snapshots_missing_new_keys(monkeypatch, tmp_path):
    """已有快照但 payload 缺必需字段（或口径版本落后）时，非 force 增量构建也必须重建。

    否则给 run_full_analysis 新增字段（如 profit_yoy）在存量行上永不回填；
    同理，改动打分口径后若不重建，榜单会长期停留在旧分数（详情页实时重算、
    榜单读快照 → 同一只票两个分数）。判定看键是否存在：值为 null 的合法缺失、
    以及口径版本一致的空值，不应触发反复重建。
    """
    from app.services import factor_db as mod

    engine = create_engine(
        f"sqlite:///{tmp_path / 'guard.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    setup = Session()
    for sym in ("600000.SH", "000001.SZ", "600519.SH", "000002.SZ"):
        setup.add(StockInfo(symbol=sym, code=sym[:6], name="测试", market=sym[-2:]))
    # 600000：旧 payload 缺 profit_yoy → 应纳入重建
    setup.add(
        FactorSnapshot(
            symbol="600000.SH",
            payload=json.dumps({"composite_score": 50.0}),
            composite_score=50.0,
        )
    )
    # 000001：已含 profit_yoy 且值为 null（合法空值）+ 当前口径版本 → 不应重建
    setup.add(
        FactorSnapshot(
            symbol="000001.SZ",
            payload=json.dumps(
                {
                    "composite_score": 51.0,
                    "profit_yoy": None,
                    "scoring_version": mod.SCORING_VERSION,
                }
            ),
            composite_score=51.0,
        )
    )
    # 000002：字段齐全但口径版本落后（打分口径已改动）→ 应重建
    setup.add(
        FactorSnapshot(
            symbol="000002.SZ",
            payload=json.dumps(
                {
                    "composite_score": 52.0,
                    "profit_yoy": 3.0,
                    "scoring_version": "1970.01.01.0",
                }
            ),
            composite_score=52.0,
        )
    )
    setup.commit()
    setup.close()

    monkeypatch.setattr(mod, "SessionLocal", Session)
    # 固定非披露期，避免 1-4/7-8/10 月全量重建导致断言不稳定
    monkeypatch.setattr(mod, "_is_in_disclosure_window", lambda *a, **k: False)

    built_symbols: list[str] = []

    def _fake_upsert(sym: str, report: dict) -> None:
        built_symbols.append(sym)
        db = Session()
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == sym).first()
        if row is None:
            row = FactorSnapshot(symbol=sym)
            db.add(row)
        row.payload = json.dumps(report)
        row.composite_score = report.get("composite_score")
        db.commit()
        db.close()

    monkeypatch.setattr(mod, "upsert", _fake_upsert)
    import app.analysis.engine as eng

    monkeypatch.setattr(
        eng,
        "analyze_symbol_full",
        lambda db, symbol, use_cache=True, **k: {
            "composite_score": 60.0,
            "market": {},
            "profit_yoy": 12.5,
        },
    )

    stats = mod.build_all(budget_sec=120)
    # 600000（缺键）+ 000002（口径版本落后）+ 600519（无快照）→ 3 只；
    # 000001 字段齐全且口径版本一致 → 跳过
    assert stats["built"] == 3
    assert sorted(built_symbols) == ["000002.SZ", "600000.SH", "600519.SH"]
    assert stats["skipped"] == 1


def test_outdated_snapshot_symbols_tolerates_broken_payload(monkeypatch, tmp_path):
    """payload 非法 JSON 时按「需重建」处理，不抛异常。"""
    from app.services import factor_db as mod

    engine = create_engine(
        f"sqlite:///{tmp_path / 'broken.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    setup = Session()
    setup.add(FactorSnapshot(symbol="600001.SH", payload="{not json", composite_score=1.0))
    setup.commit()
    setup.close()

    monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(mod, "_REQUIRED_SNAPSHOT_KEYS", ("profit_yoy",))
    # 伪造 SQL 触发异常，验证 Python 回退解析路径
    orig_text = mod.text
    monkeypatch.setattr(
        mod, "text", lambda sql: orig_text("SELECT * FROM __no_such_table__")
    )
    assert mod._outdated_snapshot_symbols() == {"600001.SH"}


# ── 技术共振叠加层（三层漏斗第二、三层）─────────────────────

def test_technical_overlay_merges_and_aggregates(monkeypatch):
    """overlay 必须复用 scan_market 榜单口径，聚合信号计数与行业共振，且缓存生效。"""
    db = _make_db(
        [
            ("600001.SH", "甲", {"comp": 88.0, "ind": "农化制品"}),
            ("600002.SH", "乙", {"comp": 86.0, "ind": "农化制品"}),
            ("600003.SH", "丙", {"comp": 84.0, "ind": "白酒"}),
        ],
        ["600001.SH", "600002.SH", "600003.SH"],
    )
    try:
        tech = {
            "600001.SH": {"buy_signal": "strong_buy", "buy_label": "强买入",
                          "buy_reasons": ["x"], "kline_bars": 90,
                          "pattern_name": "启明星", "pattern_score": 88.0,
                          "confluence_effective": 3.2, "combined_score": 107.2},
            "600002.SH": {"buy_signal": "strong_buy", "buy_label": "强买入",
                          "buy_reasons": ["x"], "kline_bars": 90,
                          "pattern_name": None},
            "600003.SH": {"buy_signal": "neutral", "buy_label": "中性",
                          "buy_reasons": [], "kline_bars": 90,
                          "pattern_name": None},
        }
        monkeypatch.setattr(ms, "_overlay_one",
                            lambda sym, name, score, peg, rw=None: {"symbol": sym, "name": name, **tech[sym]})
        ms._overlay_cache.update({"ts": 0.0, "key": None, "payload": None})
        out = ms.technical_overlay(db, top=3)
        assert out["count"] == 3
        # 综合分必须来自榜单（第一层口径），未被技术层改写
        assert [it["composite_score"] for it in out["items"]] == [88.0, 86.0, 84.0]
        assert out["signal_counts"] == {"strong_buy": 2, "neutral": 1}
        # 行业共振：农化 2 只信号 → 列出；白酒 1 只 → 不列
        assert out["industry_confluence"] == [
            {"industry": "农化制品", "total": 2, "strong_buy": 2, "signaled": 2}
        ]
        assert out["stats"]["pattern_hits"] == 1
        # 旧快照无 profit_yoy → PEG 缺失必须自证，不得虚构
        assert out["stats"]["peg_available"] == 0
        assert "不触发强买入" in out["stats"]["peg_note"]
        # 二次调用走缓存
        monkeypatch.setattr(ms, "_overlay_one",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache miss")))
        cached = ms.technical_overlay(db, top=3)
        assert cached["cached"] is True
    finally:
        db.close()


def test_scan_pagination_slices_after_filter_and_sort_and_carries_price():
    """分页必须在「过滤 + 排序后」的命中集合上偏移；股价原样透传。"""
    db = _make_db(
        [
            ("600001.SH", "一", {"comp": 90.0, "price": 12.35}),
            ("600002.SH", "二", {"comp": 80.0, "price": 3.836}),
            ("600003.SH", "三", {"comp": 70.0, "price": 105.5}),
            ("600004.SH", "四", {"comp": 60.0, "price": None}),
            ("600005.SH", "五", {"comp": 50.0, "price": 8.0}),
        ],
        ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"],
    )
    try:
        p1 = ms.scan_market(db, top=2, offset=0)
        assert p1["matched"] == 5 and p1["count"] == 2
        assert [i["symbol"] for i in p1["items"]] == ["600001.SH", "600002.SH"]
        assert (p1["offset"], p1["limit"], p1["has_more"]) == (0, 2, True)
        assert p1["items"][0]["price"] == 12.35
        assert p1["items"][1]["price"] == 3.836

        p2 = ms.scan_market(db, top=2, offset=2)
        assert [i["symbol"] for i in p2["items"]] == ["600003.SH", "600004.SH"]
        assert (p2["offset"], p2["has_more"]) == (2, True)
        # 缺价必须为 None，不得填 0 或前值
        assert p2["items"][1]["price"] is None

        p3 = ms.scan_market(db, top=2, offset=4)
        assert [i["symbol"] for i in p3["items"]] == ["600005.SH"]
        assert (p3["offset"], p3["has_more"]) == (4, False)

        # 越界 offset 返回空页但 matched 仍如实自证
        p4 = ms.scan_market(db, top=2, offset=99)
        assert p4["items"] == [] and p4["matched"] == 5 and p4["has_more"] is False

        # 偏移必须发生在「过滤 + 排序之后」：先按综合分≥75 过滤掉后 3 名，
        # 再在同一命中集合上偏移 1 → 首行应是原第 2 名，而不是原第 1 名。
        p5 = ms.scan_market(db, top=2, offset=1, min_composite=75.0)
        assert p5["matched"] == 2
        assert [i["symbol"] for i in p5["items"]] == ["600002.SH"]
        assert p5["has_more"] is False
    finally:
        db.close()


def test_overlay_covers_whole_page_not_first_n(monkeypatch):
    """技术分析必须覆盖本页全部标的（不截断），并按单票缓存跳过已算过的票。"""
    db = _make_db(
        [(f"60000{i}.SH", f"票{i}", {"comp": 90.0 - i}) for i in range(1, 9)],
        [f"60000{i}.SH" for i in range(1, 9)],
    )
    calls: list[str] = []

    def fake_one(sym, name, score, peg, rw=None):
        calls.append(sym)
        return {"symbol": sym, "name": name, "kline_bars": 90,
                "buy_signal": "neutral", "buy_label": "中性", "buy_reasons": [],
                "pattern_name": None}

    monkeypatch.setattr(ms, "_overlay_one", fake_one)
    try:
        # 第一页 5 只：全部都要有技术结论，不能截断
        out = ms.technical_overlay(db, top=5, offset=0)
        assert out["stats"]["analyzed"] == 5 and out["stats"]["computed"] == 5
        assert len(calls) == 5

        # 第二页 offset=4：与第一页重叠 1 只（600005），只应新算 600006~600008
        calls.clear()
        out2 = ms.technical_overlay(db, top=5, offset=4)
        assert out2["stats"]["analyzed"] == 4
        assert out2["offset"] == 4
        assert sorted(calls) == ["600006.SH", "600007.SH", "600008.SH"]
        assert out2["stats"]["computed"] == 3
        # 综合分仍来自榜单第一层，未被技术层改写
        assert [it["composite_score"] for it in out2["items"]] == [85.0, 84.0, 83.0, 82.0]

        # 单页上限与榜单单页一致（500），不再是 120
        assert ms.OVERLAY_PAGE_LIMIT == ms.MAX_TOP
    finally:
        db.close()


def test_overlay_retries_failed_symbol_and_never_caches_failure(monkeypatch):
    """瞬时失败（如 SQLite 写锁竞争）必须串行重试且不进缓存；重试成功即正常参与统计。"""
    db = _make_db(
        [
            ("600001.SH", "甲", {"comp": 90.0}),
            ("600002.SH", "乙", {"comp": 80.0}),
        ],
        ["600001.SH", "600002.SH"],
    )
    calls: list[str] = []
    fail_once = {"600002.SH"}

    def flaky(sym, name, score, peg, rw=None):
        calls.append(sym)
        if sym in fail_once:
            fail_once.discard(sym)
            return {"symbol": sym, "name": name, "kline_bars": 0,
                    "buy_signal": "insufficient_data", "buy_label": "分析失败",
                    "buy_reasons": [], "pattern_name": None, "failed": True}
        return {"symbol": sym, "name": name, "kline_bars": 90,
                "buy_signal": "neutral", "buy_label": "中性", "buy_reasons": [],
                "pattern_name": None}

    monkeypatch.setattr(ms, "_overlay_one", flaky)
    try:
        out = ms.technical_overlay(db, top=2)
        # 600002 首次失败 → 串行重试成功
        assert out["stats"]["retried"] == 1
        assert out["stats"]["failed"] == 0
        assert calls.count("600002.SH") == 2
        # 失败未被缓存：结果里是重试后的正常结论
        assert out["items"][1]["buy_label"] == "中性"
        assert "600002.SH" in ms._overlay_item_cache

        # 永久失败的票：不进缓存，下次请求仍会重算（而不是固化 10 分钟）
        monkeypatch.setattr(ms, "_overlay_item_cache", {})
        monkeypatch.setattr(
            ms, "_overlay_one",
            lambda sym, name, score, peg, rw=None: {"symbol": sym, "name": name, "kline_bars": 0,
                                              "buy_signal": "insufficient_data",
                                              "buy_label": "分析失败", "buy_reasons": [],
                                              "pattern_name": None, "failed": True},
        )
        # force=True 绕过请求级缓存，直接验证单票级缓存未被污染
        out2 = ms.technical_overlay(db, top=2, force=True)
        assert out2["stats"]["failed"] == 2
        assert out2["stats"]["retried"] == 2
        assert ms._overlay_item_cache == {}
        assert "不写入缓存" in "".join(out2["notes"])
    finally:
        db.close()


def test_overlay_one_without_klines_is_insufficient(monkeypatch):
    """无K线（如非主板同步范围）必须如实标注 insufficient_data，不得编造信号。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    mem = sessionmaker(bind=engine)
    monkeypatch.setattr(ms, "SessionLocal", mem)
    row = ms._overlay_one("600999.SH", "无K线", 80.0, None, None)
    assert row["buy_signal"] == "insufficient_data"
    assert row["kline_bars"] == 0


def test_strong_buy_requires_peg():
    """强买入四条件缺 PEG 不可达：净利同比缺失时保守降级为中性，不虚构信号。"""
    from types import SimpleNamespace

    from app.services.market_confluence_service import _detect_buy_signal

    bars = []
    for i in range(70):
        close = 10.0 if i < 60 else 11.0  # 后 10 根突破 → 价格 > MA20
        vol = 13.0 if i >= 65 else 10.0   # 近 5 日放量 30%
        bars.append(SimpleNamespace(close=close, volume=vol, date=f"2026-{i:04d}"))
    full = _detect_buy_signal(bars, 85.0, 0.5, 10.0)   # PEG=0.5 → 四条件齐
    assert full["signal"] == "strong_buy"
    missing = _detect_buy_signal(bars, 85.0, None, 10.0)  # PEG 缺失
    assert missing["signal"] == "neutral"


# ── 双阈值漏斗（共振过滤法）─────────────────────────────────

def test_verdict_two_threshold_funnel():
    """淘汰优先于核心/候选；技术面缺失（不可评估）归「观察」而非按最差淘汰。"""
    v = ms._verdict
    kw = dict(min_fund=70.0, min_tech=70.0, core=85.0, veto=60.0)

    assert v(88.0, 90, **kw)[0] == "core"
    assert v(85.0, 85, **kw)[0] == "core"          # 边界含等号
    assert v(84.9, 95, **kw)[0] == "candidate"     # 差 0.1 即退为候选
    assert v(72.0, 88, **kw)[0] == "candidate"
    assert v(88.0, 72, **kw)[0] == "candidate"
    # 淘汰优先：基本面再好，技术面 <60 也淘汰；反之亦然
    assert v(95.0, 59, **kw)[0] == "eliminated"
    assert v(59.0, 95, **kw)[0] == "eliminated"
    # 技术面不可评估（无达标形态共振）→ 观察，不当作 0 分淘汰
    lvl, why = v(95.0, None, **kw)
    assert lvl == "watch"
    assert any("不可评估" in w for w in why)
    # 基本面够、技术面在 [veto, min_tech) → 观察
    assert v(95.0, 65, **kw)[0] == "watch"
    # 阈值可被覆盖（外部方案换成 80/80/90/50 时口径随之改变）
    assert v(72.0, 72.0, min_fund=80.0, min_tech=80.0, core=90.0, veto=50.0)[0] == "watch"


def test_tech_score_derives_from_combined_and_missing_is_none(monkeypatch):
    """技术面得分必须由信号页共振组合分派生（只截断不重标定），无共振为 None。"""
    from types import SimpleNamespace

    from app.services.kline_service import KlineService
    from app.services.market_confluence_service import MarketConfluenceService

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ms, "SessionLocal", sessionmaker(bind=engine))
    bars = [
        SimpleNamespace(close=10.0, volume=10.0, date=f"2026-01-{i:02d}")
        for i in range(1, 61)
    ]
    monkeypatch.setattr(KlineService, "get_recent_klines", lambda self, sym, limit=90: (bars, {}))

    # 有达标形态共振：组合分 107.2 → 截断到 100
    monkeypatch.setattr(
        MarketConfluenceService, "_scan_job",
        lambda self, job, diag=None: (
            {"pattern_name": "启明星", "pattern_score": 88.0,
             "confluence_effective": 3.2, "combined_score": 107.2}, "hit"),
    )
    assert ms._overlay_one("600001.SH", "甲", 88.0, 1.2, None)["tech_score"] == 100

    # 无达标形态共振 → None（不可评估），绝不赋 0 或中性分
    monkeypatch.setattr(
        MarketConfluenceService, "_scan_job", lambda self, job, diag=None: (None, "ok")
    )
    assert ms._overlay_one("600002.SH", "乙", 88.0, 1.2, None)["tech_score"] is None

    # 未达 100 时原样保留（不重标定量纲）
    assert ms._tech_score(83.4) == 83 and ms._tech_score(None) is None

    # 被「左侧超跌形态防守」否决 → 技术面同样为 None，但必须带回否决原因，
    # 与「本来就没有形态」区分（否则用户会以为形态逻辑坏了）。
    def _blocked(self, job, diag=None):
        if diag is not None:
            diag["left_side_blocked"] = (
                "左侧超跌形态（平底锅底部）：收盘 10.00 在 MA60 12.00 下方，"
                "量比 0.87 < 1.2（无放量确认）"
            )
        return None, "left_side_blocked"

    monkeypatch.setattr(MarketConfluenceService, "_scan_job", _blocked)
    blocked_case = ms._overlay_one("600003.SH", "丙", 88.0, 1.2, None)
    assert blocked_case["tech_score"] is None
    assert "左侧超跌形态" in (blocked_case["tech_blocker"] or "")
    v, reasons = ms._verdict(
        88.0, None, min_fund=70, min_tech=70, core=85, veto=60,
        tech_blocker=blocked_case["tech_blocker"],
    )
    assert v == "watch"
    assert any("技术面已否决" in r for r in reasons)


def test_overlay_labels_verdicts_and_keeps_scores_untouched(monkeypatch):
    """决策标签只做分类：不改写综合分；阈值可覆盖并自证；换阈值不重算技术面。"""
    db = _make_db(
        [
            ("600001.SH", "核心甲", {"comp": 90.0}),
            ("600002.SH", "候选乙", {"comp": 75.0}),
            ("600003.SH", "淘汰丙", {"comp": 55.0}),
            ("600004.SH", "观察丁", {"comp": 92.0}),
        ],
        ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
    )
    tech_score = {"600001.SH": 90, "600002.SH": 72, "600003.SH": 88, "600004.SH": None}
    try:
        monkeypatch.setattr(
            ms, "_overlay_one",
            lambda sym, name, score, peg, rw=None: {
                "symbol": sym, "name": name, "kline_bars": 90,
                "buy_signal": "neutral", "buy_label": "中性", "buy_reasons": [],
                "pattern_name": None, "tech_score": tech_score[sym],
            },
        )
        out = ms.technical_overlay(db, top=4)
        assert {it["symbol"]: it["verdict"] for it in out["items"]} == {
            "600001.SH": "core",        # 90 / 90
            "600002.SH": "candidate",   # 75 / 72
            "600003.SH": "eliminated",  # 基本面 55 < 60
            "600004.SH": "watch",       # 基本面 92 但技术面不可评估
        }
        assert out["verdict_counts"] == {"core": 1, "candidate": 1, "watch": 1, "eliminated": 1}
        assert out["verdict_thresholds"] == {
            "min_fund": 70.0, "min_tech": 70.0, "core": 85.0, "veto": 60.0,
        }
        # 综合分来自榜单第一层，未被决策层改写（无第二个综合分字段）
        assert {it["symbol"]: it["composite_score"] for it in out["items"]} == {
            "600001.SH": 90.0, "600002.SH": 75.0, "600003.SH": 55.0, "600004.SH": 92.0,
        }
        assert "不产生第二个综合分" in "".join(out["notes"])

        # 覆盖阈值：候选线提到 80 → 「候选乙」降为观察，且不重算技术面（命中单票缓存）
        out2 = ms.technical_overlay(db, top=4, min_fund=80.0, min_tech=80.0)
        assert {it["symbol"]: it["verdict"] for it in out2["items"]}["600002.SH"] == "watch"
        assert out2["verdict_thresholds"]["min_fund"] == 80.0
        assert out2["stats"]["computed"] == 0
    finally:
        db.close()


# ── 共振视图（索引构建 + 档位排序读层）────────────────────────


_RESO_TECH = {
    "600001.SH": 90,   # core（90/90）
    "600002.SH": 72,   # candidate（75/72）
    "600004.SH": None, # watch（基本面 92 但无形态共振）
    "600005.SH": 50,   # eliminated（技术面 <60）
    "300001.SZ": 95,   # gem，默认剔除；纳入后 core
}


def _fake_overlay_one(sym, name, score, peg, rw=None):
    return {
        "symbol": sym, "name": name, "kline_bars": 90,
        "buy_signal": "neutral", "buy_label": "中性", "buy_reasons": [],
        "pattern_name": None, "tech_score": _RESO_TECH.get(sym),
    }


def _make_reso_db():
    return _make_db(
        [
            ("600001.SH", "核心甲", {"comp": 90.0}),
            ("600002.SH", "候选乙", {"comp": 75.0}),
            ("600003.SH", "淘汰丙", {"comp": 55.0}),  # fund<60，不进技术面计算池
            ("600004.SH", "观察丁", {"comp": 92.0}),
            ("600005.SH", "淘汰戊", {"comp": 88.0}),
            ("300001.SZ", "创业己", {"comp": 95.0, "ind": "半导体"}),
        ],
        ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH", "300001.SZ"],
    )


def test_resonance_read_requires_index():
    """索引未构建必须显式报错（路由层转 empty 提示），不得静默返回空榜。"""
    db = _make_reso_db()
    try:
        with pytest.raises(RuntimeError):
            ms.resonance_view(db, top=10)
    finally:
        db.close()


def test_resonance_build_and_verdict_ordered_read(monkeypatch):
    """共振索引只给 fund≥淘汰线的票算技术面；读层按档位全局排序，不按综合分。"""
    db = _make_reso_db()
    try:
        monkeypatch.setattr(ms, "SessionLocal", sessionmaker(bind=db.get_bind()))
        monkeypatch.setattr(ms, "_overlay_one", _fake_overlay_one)
        summary = ms.resonance_index_build()
        # 计算池 = comp≥60 的 5 只（600003 comp=55 不进池，规则直接淘汰）
        assert summary["tech_needed"] == 5
        assert summary["tech_computed"] == 5

        # 二次构建命中索引缓存
        assert ms.resonance_index_build()["cached"] is True

        out = ms.resonance_view(db, top=10)
        # 默认剔除 gem：300001.SZ 不在榜
        assert [it["symbol"] for it in out["items"]] == [
            "600001.SH",   # core
            "600002.SH",   # candidate
            "600004.SH",   # watch（技术面缺失）
            "600005.SH",   # eliminated（技术 50）
            "600003.SH",   # eliminated（基本面 55，无技术面）
        ]
        assert out["verdict_counts"] == {
            "core": 1, "candidate": 1, "watch": 1, "eliminated": 2,
        }
        assert out["matched"] == 5 and out["has_more"] is False
        # 综合分未被改写（无第二综合分）
        assert out["items"][0]["composite_score"] == 90.0
        assert "不再按基本面综合分排序" in "".join(out["notes"])

        # 服务端 verdict_filter：仅核心持仓
        only_core = ms.resonance_view(db, top=10, verdict_filter="core")
        assert only_core["matched"] == 1
        assert only_core["items"][0]["symbol"] == "600001.SH"
        assert only_core["verdict_counts"]["core"] == 1  # 计数仍基于全部命中

        # candidate_up = 核心 + 候选
        up = ms.resonance_view(db, top=10, verdict_filter="candidate_up")
        assert {it["symbol"] for it in up["items"]} == {"600001.SH", "600002.SH"}

        # 纳入 gem → 创业己进入 core，且排最前（同档内技术分降序 95>90）
        gem = ms.resonance_view(db, top=10, include_gem=True)
        assert gem["verdict_counts"]["core"] == 2
        assert gem["items"][0]["symbol"] == "300001.SZ"

        # 关键词过滤在索引读层生效（不触发重算）
        kw = ms.resonance_view(db, top=10, keyword="候选")
        assert [it["symbol"] for it in kw["items"]] == ["600002.SH"]

        # 分页切片在档位排序之后
        p2 = ms.resonance_view(db, top=2, offset=2)
        assert [it["symbol"] for it in p2["items"]] == ["600004.SH", "600005.SH"]
        assert p2["has_more"] is True
    finally:
        db.close()


def test_resonance_rejects_unknown_verdict_filter():
    db = _make_reso_db()
    try:
        with pytest.raises(ValueError):
            ms.resonance_view(db, top=10, verdict_filter="nonsense")
    finally:
        db.close()



# ── 分类准入门槛（候选池熔断，只封档位不改分）─────────────────────────────
# 背景：600722 金牛化工 PE 193.5 / 每股经营现金流 0.078 / ROE 3.92%，
# 却因偿债 93.9 + 现金流 75.8 把综合分拉到 71.5 → 进「买入候选」。
# 病根是「估值极高 却盈利极弱」的矛盾组合被加权平均掩盖，不是单指标超标。
#
# 关键设计约束（实测 2026-09-21，5254 只）：
#   单一绝对阈值全部被否决——
#     周转率<0.5 命中 55.8%、ROE<5% 命中 54.2%、PE分位>85% 命中 18.7%；
#     「PE分位≥90 且 PB分位≥90」甚至拦掉中国神华(87.2/A)、生益科技、深南电路。
#   反证：PE分位 95.6% 拦中国神华，却放走 PE分位 89.8% 的金牛化工。
# 因此改为「分类适用 + 矛盾组合」：四类资产各用各的门槛，不新造综合分。


def test_admission_gate_blocks_jinniu_chemical_style_mismatch():
    """金牛化工式「高估值 + 弱盈利」矛盾组合必须被拦。"""
    gate = ms.classify_admission_gate(
        roe=3.92, asset_turnover=0.316, pe_percentile=89.8
    )
    assert gate is not None
    label, why = gate
    assert label == "传统价值型"
    assert "背离" in why
    assert "ROE" in why


def test_admission_gate_spares_quality_high_percentile():
    """高分位但盈利强的优质股不得被误杀（分位高 ≠ 泡沫）。"""
    # 中国神华：PE 分位 95.6 但 ROE 12.76
    assert ms.classify_admission_gate(
        roe=12.76, asset_turnover=0.6, pe_percentile=95.6
    ) is None
    # 三环集团：PE 分位 97.8 但 ROE 12.62
    assert ms.classify_admission_gate(
        roe=12.62, asset_turnover=0.5, pe_percentile=97.8
    ) is None
    # 生益科技/深南电路：成长型不设 PE 上限
    assert ms.classify_admission_gate(
        is_growth_stock=True, revenue_yoy=25.0, gross_margin=22.0
    ) is None


def test_admission_gate_dividend_branch_skips_roe_and_turnover():
    """红利型不看 ROE/周转率（重资产低周转是行业属性），只看分红能力。"""
    # 大秦铁路：ROE 3.64 / 周转 0.367（若按传统价值型会被「矛盾组合」命中），
    # 但股息 4.72% + payout 75% → 走红利分支必须放行。
    assert ms.classify_admission_gate(
        is_dividend_asset=True, dividend_yield=4.72, payout_ratio=75.0
    ) is None
    # 红利型但分红不达标 → 拦
    gate = ms.classify_admission_gate(
        is_dividend_asset=True, dividend_yield=2.0, payout_ratio=30.0
    )
    assert gate is not None and gate[0] == "红利型"


def test_admission_gate_missing_fields_pass_through():
    """关键字段缺失一律放行——不猜、不因缺数据而改变判定。"""
    assert ms.classify_admission_gate() is None
    assert ms.classify_admission_gate(roe=None, pe_percentile=None) is None
    # 成长型两者都缺 → 放行
    assert ms.classify_admission_gate(is_growth_stock=True) is None


def test_admission_gate_cyclical_traps_at_earnings_peak():
    """周期型：景气高点（cycle_trap）熔断。"""
    gate = ms.classify_admission_gate(is_strong_cyclical=True, cycle_trap=True)
    assert gate is not None and gate[0] == "周期型"


def test_admission_gate_cyclical_is_not_a_free_pass():
    """周期型**不是免检通道**：高估值分位 + 弱盈利同样拦截。

    历史缺陷：该分支只判 ``cycle_trap`` 而该参数恒为 False，导致 894 只强周期股
    全部无条件放行，混入 143 只「PE分位≥80 且 ROE<6」，与金牛化工同病。
    """
    # 泸天化式：PE分位 99.3 / ROE 0.5 → 拦
    gate = ms.classify_admission_gate(
        is_strong_cyclical=True,
        pe_percentile=99.3,
        roe=0.5,
    )
    assert gate is not None and gate[0] == "周期型"
    assert "背离" in gate[1]
    # 招商蛇口式：PE分位 100.0 / ROE 0.73 → 拦
    assert ms.classify_admission_gate(
        is_strong_cyclical=True, pe_percentile=100.0, roe=0.73
    ) is not None
    # 峰值周期股不误杀：高 ROE 拿高估值分位是常态（中国神华式）
    assert ms.classify_admission_gate(
        is_strong_cyclical=True, pe_percentile=95.6, roe=12.76
    ) is None
    # 真·底部反转不误杀：谷底 ROE 为负但估值在低位，正是该买时点
    assert ms.classify_admission_gate(
        is_strong_cyclical=True, pe_percentile=12.0, roe=-3.0
    ) is None
    # 缺字段放行（不猜）
    assert ms.classify_admission_gate(
        is_strong_cyclical=True, pe_percentile=None, roe=1.0
    ) is None
    assert ms.classify_admission_gate(
        is_strong_cyclical=True, pe_percentile=99.0, roe=None
    ) is None


def test_admission_gate_financial_exempt_from_turnover_floor():
    """金融业结构性豁免周转率门槛：银行/券商周转 0.02~0.08 是业务模型，非低效。

    实测 42 家银行 / 49 家券商 / 20 家多元金融周转率 <0.15，若不豁免全部误杀
    （宁波银行 0.02、华泰证券 0.033、华夏银行 0.019）。
    """
    # 宁波银行式：周转 0.02 但银行身份 → 不被周转兜底拦（ROE 正常）
    assert ms.classify_admission_gate(
        asset_turnover=0.02, roe=11.0, pe_percentile=60.0, industry="银行Ⅱ"
    ) is None
    # 同一周转率但不是金融 → 仍拦（真僵尸资产）
    gate = ms.classify_admission_gate(
        asset_turnover=0.02, roe=11.0, pe_percentile=60.0, industry="化学原料"
    )
    assert gate is not None and "周转率" in gate[1]
    # 券商同理豁免
    assert ms.classify_admission_gate(
        asset_turnover=0.033, roe=8.0, pe_percentile=50.0, industry="证券Ⅱ"
    ) is None
    # 豁免不等于免检：金融股照样过「估值↔盈利矛盾」这一关
    gate2 = ms.classify_admission_gate(
        asset_turnover=0.02, roe=0.7, pe_percentile=100.0, industry="银行Ⅱ"
    )
    assert gate2 is not None and "背离" in gate2[1]


def test_verdict_profile_gate_caps_to_watch_without_score_rewrite():
    """闸门命中只封顶「观察」，且理由带类型标签；fund 已低于淘汰线仍报淘汰。"""
    v, reasons = ms._verdict(
        71.5, 100,
        min_fund=80, min_tech=80, core=85, veto=60,
        profile_gate=("传统价值型", "估值与盈利背离：…"),
    )
    assert v == "watch"
    assert any("分类准入未通过" in r for r in reasons)
    # 更差的票（fund < veto）仍报淘汰，不被闸门理由覆盖
    v2, _ = ms._verdict(
        45.0, 100,
        min_fund=80, min_tech=80, core=85, veto=60,
        profile_gate=("传统价值型", "…"),
    )
    assert v2 == "eliminated"


def test_verdict_no_gate_keeps_original_behaviour():
    """不传 profile_gate 时行为与既有完全一致（历史调用方不受影响）。"""
    assert ms._verdict(
        90.0, 90, min_fund=80, min_tech=80, core=85, veto=60
    )[0] == "core"
    assert ms._verdict(
        82.0, 82, min_fund=80, min_tech=80, core=85, veto=60
    )[0] == "candidate"
    assert ms._verdict(
        70.0, 82, min_fund=80, min_tech=80, core=85, veto=60
    )[0] == "watch"


def test_admission_gate_growth_branch_handles_partial_none():
    """成长型分支任一字段为 None 时不得抛异常。

    回归锁：首版格式化文案直接 `float(revenue_yoy)`，当营收为 None 而毛利率
    有值（或反之）且均不达标时会抛 TypeError，**导致共振索引构建整体失败**
    （线上表现为 /technical-overlay 恒 503、progress 报
    「float() argument must be ... not 'NoneType'」）。
    """
    # 营收 None + 毛利率有值且不达标
    gate = ms.classify_admission_gate(
        is_growth_stock=True, revenue_yoy=None, gross_margin=20.0
    )
    assert gate is not None and gate[0] == "成长型"
    assert "缺失" in gate[1]
    # 营收有值且不达标 + 毛利率 None
    gate2 = ms.classify_admission_gate(
        is_growth_stock=True, revenue_yoy=10.0, gross_margin=None
    )
    assert gate2 is not None and gate2[0] == "成长型"
    assert "缺失" in gate2[1]
    # 两者都有值且不达标
    gate3 = ms.classify_admission_gate(
        is_growth_stock=True, revenue_yoy=10.0, gross_margin=20.0
    )
    assert gate3 is not None


def test_base_items_gate_survives_missing_new_fields():
    """旧快照缺新增分类字段时 _base_items 不得崩溃（回退路径同样安全）。"""
    row = {
        "symbol": "600722.SH",
        "composite_score": 71.5,
        "pe_ttm": 193.5,
        "name": "金牛化工",
        "industry": "化学原料",
        "final_rating": "B",
        "risk_level_label": "风险可控",
        "market_cap": 1.09e10,
        "price": 16.02,
        "pb": 8.49,
        "dividend_yield": 0.47,
        "dim_profitability": 54.7,
        "dim_growth": 76.7,
        "dim_cashflow": 75.8,
        "dim_solvency": 93.9,
        "dim_valuation": 56.0,
        # 关键：分类字段全缺（模拟 09-20 旧快照）
    }
    items = ms._base_items([row])
    assert len(items) == 1
    # 字段全缺 → 传统价值型分支但三个判据都缺 → 放行（不猜）
    assert items[0]["profile_gate"] is None


# ── 轻量行缓存（2026-09-28：杜绝每请求全表 json_extract）─────────────
# 背景：_LIGHT_SQL 要对全表跑 25 个 json_extract，线上实测 0.335s/次
# （3218 只），而翻页 / 换筛选 / 切视图每次都重跑一遍。
# 失效用**内容指纹**（= 已覆盖条数 + max(built_at)）而非固定 TTL：
# 盘后 build_all 期间指纹持续变化 → 自动失效，不会把旧口径当新鲜数据。
def test_light_rows_cache_hits_and_returns_isolated_copies():
    """第二次读取命中缓存；且返回浅拷贝 —— 调用方就地写字段不得污染缓存。"""
    db = _make_db(
        [("600001.SH", "甲公司", {"comp": 80.0, "ind": "酿酒"})],
        ["600001.SH"],
    )
    try:
        rows1, fb1 = ms.load_covered(db)
        assert fb1 is False
        assert len(rows1) == 1
        assert ms.light_cache_stats()["misses"] == 1

        # 副本可被下游就地改写（技术叠加列就是这么写的）
        rows1[0]["name"] = "被下游改过的名字"

        rows2, _ = ms.load_covered(db)
        assert ms.light_cache_stats()["hits"] == 1
        assert rows2[0]["name"] == "甲公司"  # 缓存未被污染
        assert rows2[0]["symbol"] == "600001.SH"
    finally:
        db.close()


def test_light_rows_cache_invalidated_when_snapshot_added():
    """新增快照 → 条数变化 → 指纹变化 → 缓存失效（立刻看到新票）。"""
    db = _make_db(
        [("600001.SH", "甲公司", {"comp": 80.0, "ind": "酿酒"})],
        ["600001.SH", "600002.SH"],
    )
    try:
        assert len(ms.load_covered(db)[0]) == 1
        fp_before = ms.light_cache_stats()["fingerprint"]

        db.add(
            FactorSnapshot(
                symbol="600002.SH",
                composite_score=70.0,
                pe_ttm=10.0,
                payload=_payload("乙公司", "酿酒", mcap=5e10, dims={}),
            )
        )
        db.commit()

        rows, _ = ms.load_covered(db)
        assert len(rows) == 2
        fp_after = ms.light_cache_stats()["fingerprint"]
        assert fp_after != fp_before
        assert fp_after[0] == 2
    finally:
        db.close()


def test_light_rows_cache_invalidated_on_rebuild_same_count():
    """**重建同一批票**（条数不变、built_at 前移）也必须失效 —— 这是盘后
    build_all 的常态；若只按条数判断，就会把重建前的旧口径当新鲜数据返回。"""
    db = _make_db([("600001.SH", "甲公司", {"comp": 80.0})], ["600001.SH"])
    try:
        assert len(ms.load_covered(db)[0]) == 1
        fp_before = ms.light_cache_stats()["fingerprint"]

        snap = db.query(FactorSnapshot).one()
        snap.built_at = datetime.now(timezone.utc) + timedelta(seconds=7)
        db.commit()

        rows, _ = ms.load_covered(db)
        assert len(rows) == 1  # 条数没变
        fp_after = ms.light_cache_stats()["fingerprint"]
        assert fp_after != fp_before  # 但指纹必须变 → 缓存已失效并重建
    finally:
        db.close()


# ── 风格标签（**两维叠加**，2026-09-29 二次修订）：读层展示口径，不改分数 ──
# 维度 A（价值 / 成长，二选一）+ 维度 B（周期 / 红利 / 蓝筹，命中即加）；
# 红利命中时价值被吸收（红利 = 价值 + 股息 ≥4% + 分红稳定）。
# 展示顺序：价值 → 成长 → 周期 → 红利 → 蓝筹。
# 这里锁定四件事：① 周期 + 红利叠加（不能停在「周期」）；② 券商 = 价值 + 周期；
# ③ 收租型（港口 / 写字楼）从周期摘出改判红利；④ 服务端 style 筛选与 style_counts 同口径。


def _style_payload(
    *,
    industry: str,
    name: str = "风格测试",
    pb: float = 1.5,
    dy: float = 2.0,
    payout: float | None = None,
    roe: float | None = None,
    profit_yoy: float | None = None,
    mcap_yi: float = 50.0,
    growth_score: float | None = None,
    profit_score: float | None = None,
) -> str:
    """构造风格判定用快照 payload。

    注意：`pe_ttm` **不在 payload 里**（读层从 `FactorSnapshot.pe_ttm` 列取），
    故传参时 pe 由 `_style_db` 的元组给出。
    """
    return json.dumps(
        {
            "name": name,
            "industry": industry,
            "final_rating": "B",
            "market": {"market_cap": mcap_yi * 1e8, "price": 10.0, "pb": pb, "dividend_yield": dy},
            "valuation": (
                {"dividend_profile": {"payout_ratio_pct": payout}} if payout is not None else {}
            ),
            "modules": {
                "profitability": {"indicators": ([{"value": roe}] if roe is not None else [])}
            },
            # 两维叠加要用维度分：成长分 / 盈利质量分（_LIGHT_SQL 从 dim_scores 读）
            "dim_scores": {"成长性": growth_score, "盈利能力": profit_score},
            "profit_yoy": profit_yoy,
        },
        ensure_ascii=False,
    )


def _style_db(rows):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for sym, payload, pe in rows:
        db.add(FactorSnapshot(symbol=sym, composite_score=80.0, pe_ttm=pe, payload=payload))
        db.add(StockInfo(symbol=sym, code=sym[:6], name="占位", market=sym[-2:]))
    db.commit()
    return db


def _cls(
    industry: str,
    *,
    name: str = "风格测试",
    symbol: str = "",
    pe: float | None = 10.0,
    pb: float | None = 1.5,
    dy: float | None = 2.0,
    payout: float | None = None,
    roe: float | None = None,
    mcap: float | None = 50.0,
    growth: float | None = None,
    profit: float | None = None,
    ind_med_pe: float | None = None,
) -> tuple[list[str], str]:
    """直接调用 classify_style（真实签名：维度分 + 收租白名单 symbol）。"""
    return ms.classify_style(
        industry=industry,
        pe_ttm=pe,
        pb=pb,
        dividend_yield=dy,
        payout_ratio_pct=payout,
        roe_pct=roe,
        market_cap_yi=mcap,
        growth_score=growth,
        profit_score=profit,
        symbol=symbol,
        name=name,
        industry_median_pe=ind_med_pe,
    )


def _tags(*args, **kwargs) -> list[str]:
    return _cls(*args, **kwargs)[0]


def test_style_keys_two_axis_definition():
    """两维叠加的键集合与阈值：价值回归主键、防御股删除（用户 2026-09-29 二次口径）。"""
    # STYLE_KEYS 顺序即展示顺序：价值 → 成长 → 周期 → 红利 → 蓝筹 →（其他）。
    # 该顺序对应用户口径示例「价值+周期」「周期+红利」「成长+红利」「价值+蓝筹」。
    assert ms.STYLE_KEYS == ("value", "growth", "cyclical", "dividend", "blue_chip", "other")
    assert "defensive" not in ms.STYLE_KEYS
    assert not hasattr(ms, "DEFENSIVE_INDUSTRIES")
    # 赛道白名单 / 旧「高股息兜底线」常量已删：成长改由维度分判定、红利阈值统一。
    assert not hasattr(ms, "GROWTH_TRACK_INDUSTRIES")
    assert not hasattr(ms, "DIVIDEND_HIGH_YIELD")
    # 阈值（用户口径：PB<2、PE<15 或低于行业中位、盈利质量≥70、成长分≥75、息≥3.95%）。
    assert ms.VALUE_MAX_PB == 2.0
    assert ms.VALUE_MAX_PE == 15.0
    assert ms.VALUE_MIN_PROFIT_SCORE == 70.0
    assert ms.GROWTH_MIN_SCORE == 75.0
    assert ms.DIVIDEND_MIN_YIELD == 3.95
    assert ms.BLUE_CHIP_MIN_MCAP_YI == 1000.0
    # 保险Ⅱ 已从红利行业移除（定价核心是 PB 资产折价 → 中国人寿走价值分支）。
    assert "保险Ⅱ" not in ms.DIVIDEND_INDUSTRIES
    # 收租型地产白名单：中国国贸（写字楼收租）显式登记，否则会落进房地产开发周期闸。
    assert "600007.SH" in ms.RENTAL_SYMBOLS


def test_classify_style_cyclical_plus_dividend_superimpose():
    """① 核心：周期行业 + 股息率 ≥3.95% 必须输出「周期+红利」，不能停在「周期」。"""
    # 天山铝业 工业金属 息 4.65%（改前被误判红利股，改后 周期+红利）
    assert _tags("工业金属", pe=7.8635, pb=1.7675, dy=4.6501, payout=52.9, roe=17.0,
                 mcap=543.41, growth=86.9, profit=83.6) == ["cyclical", "dividend"]
    # 芭田股份 农化制品 息 7.01%
    assert _tags("农化制品", pe=9.57, pb=2.78, dy=7.01, payout=77.7, roe=25.93,
                 mcap=101.26, growth=85.4, profit=88.0) == ["cyclical", "dividend"]
    # 鄂尔多斯 冶钢原料 息 5.51%
    assert _tags("冶钢原料", pe=12.7591, pb=1.6608, dy=5.5069, payout=82.9, roe=11.06,
                 mcap=356.0, growth=74.3, profit=77.4) == ["cyclical", "dividend"]
    # 新和成 化学制品 息 3.9679%（刚好越过 3.95% 属性线 —— 阈值取 3.95 而非 4.0 的原因）
    assert _tags("化学制品", pe=10.7986, pb=2.2156, dy=3.9679, payout=45.4, roe=21.87,
                 mcap=774.19, growth=84.8, profit=85.3) == ["cyclical", "dividend"]
    # 锦江航运 航运港口 息 6.41%：是航运（不含「港」）→ 周期 + 红利
    assert _tags("航运港口", name="锦江航运", pe=11.249, pb=1.7688, dy=6.4102,
                 payout=70.0, roe=16.63, mcap=163.96, growth=64.3,
                 profit=88.0) == ["cyclical", "dividend"]
    # 招商南油 息 0.61% → 只周期（属性轴只加命中项）
    assert _tags("航运港口", name="招商南油", pe=13.4083, pb=1.71, dy=0.6072,
                 payout=9.6, roe=11.55, mcap=208.28, growth=82.6, profit=82.0) == ["cyclical"]
    # 云铝股份 息 2.75% < 3.95% → 只周期
    assert _tags("工业金属", pe=8.0411, pb=2.309, dy=2.745, payout=40.0, roe=19.7,
                 mcap=882.25, growth=85.1, profit=79.5) == ["cyclical"]
    # 周期行业的利润暴增不判成长（防「周期顶当成长」）
    assert "growth" not in _tags("化学原料", pe=8.0, pb=1.2, dy=1.0, payout=20.0,
                                 roe=15.0, mcap=100.0, growth=99.0, profit=95.0)


def test_classify_style_broker_is_value_plus_cyclical():
    """② 券商走「价值+周期」：低 PB 给价值、行业（行情β）给周期，两个都加。"""
    # 长江证券 PB 1.20 / PE 8.56（改前被误判成长股）
    assert _tags("证券Ⅱ", pe=8.5585, pb=1.197, dy=3.7594, payout=44.9, roe=10.02,
                 mcap=440.75, growth=90.4, profit=84.0) == ["value", "cyclical"]
    # 中国银河 PB 1.05
    assert _tags("证券Ⅱ", pe=9.1556, pb=1.0458, dy=3.0187, payout=30.6, roe=9.84,
                 mcap=1266.2, growth=86.4, profit=84.0) == ["value", "cyclical"]
    # 国金证券 息 0.99%（无红利证据）仍是 价值+周期
    assert _tags("证券Ⅱ", pe=11.9939, pb=0.8215, dy=0.9933, payout=13.0, roe=6.58,
                 mcap=297.24, growth=89.8, profit=76.0) == ["value", "cyclical"]
    # 中信证券：千亿 + ROE ≥10 → 价值 + 周期 + 蓝筹（属性轴可叠两项）
    assert _tags("证券Ⅱ", pe=10.2001, pb=1.3677, dy=3.1458, payout=34.5, roe=10.59,
                 mcap=4046.69, growth=87.7, profit=84.0) == ["value", "cyclical", "blue_chip"]
    # 券商判价值**只认 PB**：PB ≥2 即便 PE 很「低」也不给价值（东兴证券 PB 2.05）
    assert _tags("证券Ⅱ", pe=18.29, pb=2.05, dy=1.0, payout=20.0, roe=8.0,
                 mcap=400.0, growth=88.0, profit=90.0) == ["cyclical"]
    # 券商不判成长（哪怕成长分 95 —— 那是行情 β，不是成长）
    assert "growth" not in _tags("证券Ⅱ", pe=8.0, pb=3.0, dy=1.0, payout=20.0,
                                 roe=8.0, mcap=100.0, growth=95.0, profit=90.0)


def test_classify_style_rental_pulled_out_of_cyclical_to_dividend():
    """③ 收租型资产（港口 / 写字楼）从周期闸摘出、改判红利；航运仍留周期。"""
    # 唐山港：航运港口 + 简称含「港」→ 收租 → 红利（不是一个孤零零的「周期」）
    assert _tags("航运港口", name="唐山港", pe=12.7387, pb=1.2655, dy=4.3887,
                 payout=59.3, roe=9.5, mcap=270.22, growth=80.2, profit=84.4) == ["dividend"]
    # 中国国贸：房地产开发 + 白名单兜底 → 红利（原型只按简称判，漏掉了它）
    assert _tags("房地产开发", name="中国国贸", symbol="600007.SH", pe=16.8619,
                 pb=2.182, dy=5.3103, payout=89.6, roe=12.64, mcap=202.87,
                 growth=66.7, profit=90.0) == ["dividend"]
    # 上港集团 息 3.59%：收租 → 红利（股息率不足 4% 但收租类走行业证据路径）
    assert _tags("航运港口", name="上港集团", pe=12.0, pb=1.2, dy=3.59, payout=45.0,
                 roe=9.0, mcap=1200.0, growth=60.0, profit=80.0) == ["dividend"]
    # 反例：中远海控 / 宁波海运 是航运（不含「港」）→ 仍周期
    for nm, dy, payout in (("中远海控", 6.19, 50.0), ("宁波海运", 0.59, 8.0)):
        tags = _tags("航运港口", name=nm, pe=13.0, pb=1.5, dy=dy, payout=payout,
                     roe=12.0, mcap=300.0, growth=70.0, profit=80.0)
        assert "cyclical" in tags, f"{nm} 应仍带周期"
        assert "value" not in tags or "cyclical" in tags
    # 白名单是唯一入口：未登记的房地产开发标的仍走周期闸
    assert _tags("房地产开发", name="万科A", symbol="000002.SZ", pe=9.0, pb=1.2,
                 dy=1.0, payout=20.0, roe=5.0, mcap=900.0, growth=60.0,
                 profit=70.0) == ["cyclical"]


def test_classify_style_growth_hard_filtered_by_score():
    """成长硬过滤：成长分 <75 一律不得进成长分支（天有为 47.2 = 全表最低）。"""
    # 天有为 成长分 47.2：改前错标成长；现因「廉价 + 盈利质量 + 息 4.91%」→ 红利（价值被吸收）
    assert _tags("汽车零部件", pe=11.839, pb=1.1975, dy=4.9096, payout=43.4, roe=16.23,
                 mcap=81.42, growth=47.2, profit=87.0) == ["dividend"]
    # 星宇股份 成长分 71.3（用户判定「下半区」）→ 不判成长；息 2.88% → 只价值
    assert _tags("汽车零部件", pe=12.5061, pb=1.7518, dy=2.877, payout=35.2, roe=15.1,
                 mcap=198.4, growth=71.3, profit=79.5) == ["value"]
    # 珀莱雅 成长分 86 → 成长（PB 3.24 不便宜，故不会被价值截胡）
    assert _tags("化妆品", pe=11.5473, pb=3.2386, dy=3.6727, payout=52.9, roe=25.8,
                 mcap=215.57, growth=86.0, profit=86.8) == ["growth"]
    # 焦点科技 成长分 73.5 < 75 → 不判成长；息 5.18% → 红利
    assert _tags("互联网电商", pe=22.1205, pb=4.0572, dy=5.1822, payout=81.9, roe=20.6,
                 mcap=103.75, growth=73.5, profit=90.0) == ["dividend"]
    # 成长分缺失 → 不判成长（缺失不猜，铁律 8）
    assert "growth" not in _tags("软件开发", pe=40.0, pb=6.0, dy=0.5, payout=10.0,
                                 growth=None, profit=95.0)
    # 价值优先于成长：便宜 + 质量达标时，即使成长分 90 也走价值（二选一）
    assert _tags("通用设备", pe=10.0, pb=1.2, dy=1.0, payout=20.0, roe=15.0,
                 mcap=100.0, growth=90.0, profit=85.0) == ["value"]


def test_classify_style_dividend_absorbs_value():
    """层级：红利 = 价值 + 股息 ≥3.95% + 分红稳定 → 命中红利时不再显示价值。"""
    # 便宜 + 质量 + 息 5.2% → 只留「红利」（价值被吸收，避免标签泛滥）
    assert _tags("银行Ⅱ", pe=6.0, pb=0.7, dy=5.2, payout=30.0, roe=11.0,
                 mcap=800.0, growth=40.0, profit=80.0) == ["dividend"]
    # 便宜、但息在 2%~4% 且非红利行业 → 只价值（不叠红利）
    assert _tags("汽车零部件", pe=12.0, pb=1.2, dy=2.0, payout=20.0, roe=15.0,
                 mcap=60.0, growth=60.0, profit=80.0) == ["value"]
    # 非红利无质量（盈利质量分 <70）→ 一条都不沾
    assert _tags("汽车零部件", pe=12.0, pb=1.2, dy=2.0, payout=20.0, roe=15.0,
                 mcap=60.0, growth=60.0, profit=65.0) == []


def test_classify_style_value_needs_cheap_and_quality():
    """价值判据：PB <2 且（PE <15 或低于行业中位）且盈利质量分 ≥70；周期只认 PB。"""
    # PB ≥2 → 不便宜
    assert "value" not in _tags("专用设备", pe=10.0, pb=2.5, dy=1.0, payout=10.0,
                                mcap=100.0, growth=50.0, profit=90.0)
    # PE 19 且无行业中位基准 → PE 不达标
    assert "value" not in _tags("专用设备", pe=19.0, pb=1.2, dy=1.0, payout=10.0,
                                mcap=100.0, growth=50.0, profit=90.0)
    # PE 19 < 行业中位 54.7 → 视为便宜
    assert "value" in _tags("软件开发", pe=19.0, pb=1.6, dy=1.0, payout=10.0,
                            mcap=100.0, growth=50.0, profit=90.0, ind_med_pe=54.7)
    # 盈利质量分 <70 / 缺失 → 不判价值
    for p in (65.0, None):
        assert "value" not in _tags("专用设备", pe=10.0, pb=1.2, dy=1.0, payout=10.0,
                                    mcap=100.0, growth=50.0, profit=p)
    # 商品周期股不判价值：低 PE 是周期顶特征（PB 1.0 / PE 6 也只给周期）
    assert _tags("工业金属", pe=6.0, pb=1.0, dy=1.0, payout=10.0, roe=20.0,
                 mcap=500.0, growth=90.0, profit=95.0) == ["cyclical"]


def test_classify_style_blue_chip_threshold():
    """蓝筹门槛：市值 ≥1000 亿 且 ROE ≥10% 且 PE>0；可与任意风格并存。"""
    base = dict(pe=25.0, pb=5.0, dy=1.0, payout=10.0, mcap=1500.0, growth=50.0, profit=60.0)
    assert _tags("专用设备", roe=15.0, **base) == ["blue_chip"]
    # 400 亿（改前判蓝筹的量级）→ 不再算蓝筹
    assert _tags("专用设备", roe=15.0, **{**base, "mcap": 400.0}) == []
    # ROE 不足 / 亏损（PE ≤0）→ 不判蓝筹
    for roe, pe in ((8.0, 25.0), (15.0, -3.0)):
        assert _tags("专用设备", roe=roe, **{**base, "pe": pe}) == []
    # 全缺失 → 空（= 其他），不赋中性值
    assert _tags("", pe=None, pb=None, dy=None, payout=None, roe=None, mcap=None,
                 growth=None, profit=None) == []


# ── 两维叠加靶点表：**真实快照数值**（2026-09-29 线上 dump）→ 期望标签列表 ──
# 判据：① 用户点名的每一只；② 周期+红利 / 价值+周期 / 红利（收租）三类叠加样式；
# ③ 改前判对的对照标的零回归。全部数值取自 `/api` 同源的 factor_snapshots。
@pytest.mark.parametrize(
    "name,industry,pe,pb,dy,payout,roe,mcap,growth,profit,symbol,expect",
    [
        # ── ① 周期 + 红利叠加（用户首要修复项）────────────────────────
        ("天山铝业", "工业金属", 7.8635, 1.7675, 4.6501, 52.9, 17.0, 543.41, 86.9, 83.6, "002532.SZ", ["cyclical", "dividend"]),
        ("芭田股份", "农化制品", 9.57, 2.78, 7.01, 77.7, 25.93, 101.26, 85.4, 88.0, "002170.SZ", ["cyclical", "dividend"]),
        ("鄂尔多斯", "冶钢原料", 12.7591, 1.6608, 5.5069, 82.9, 11.06, 356.0, 74.3, 77.4, "600295.SH", ["cyclical", "dividend"]),
        ("新和成", "化学制品", 10.7986, 2.2156, 3.9679, 45.4, 21.87, 774.19, 84.8, 85.3, "002001.SZ", ["cyclical", "dividend"]),
        ("锦江航运", "航运港口", 11.249, 1.7688, 6.4102, 70.0, 16.63, 163.96, 64.3, 88.0, "601083.SH", ["cyclical", "dividend"]),
        ("招商南油", "航运港口", 13.4083, 1.71, 0.6072, 9.6, 11.55, 208.28, 82.6, 82.0, "601975.SH", ["cyclical"]),
        # ── ③ 收租型：从周期摘出改判红利 ───────────────────────────────
        ("唐山港", "航运港口", 12.7387, 1.2655, 4.3887, 59.3, 9.5, 270.22, 80.2, 84.4, "601000.SH", ["dividend"]),
        ("中国国贸", "房地产开发", 16.8619, 2.182, 5.3103, 89.6, 12.64, 202.87, 66.7, 90.0, "600007.SH", ["dividend"]),
        # ── ② 券商：价值 + 周期 ────────────────────────────────────────
        ("长江证券", "证券Ⅱ", 8.5585, 1.197, 3.7594, 44.9, 10.02, 440.75, 90.4, 84.0, "000783.SZ", ["value", "cyclical"]),
        ("中国银河", "证券Ⅱ", 9.1556, 1.0458, 3.0187, 30.6, 9.84, 1266.2, 86.4, 84.0, "601881.SH", ["value", "cyclical"]),
        ("国泰海通", "证券Ⅱ", 9.1134, 0.8896, 2.9798, 31.7, 9.78, 2946.59, 89.8, 84.0, "601211.SH", ["value", "cyclical"]),
        ("国金证券", "证券Ⅱ", 11.9939, 0.8215, 0.9933, 13.0, 6.58, 297.24, 89.8, 76.0, "600109.SH", ["value", "cyclical"]),
        ("国元证券", "证券Ⅱ", 11.498, 0.8077, 2.507, 32.4, 6.45, 313.32, 85.5, 71.0, "000728.SZ", ["value", "cyclical"]),
        ("中信证券", "证券Ⅱ", 10.2001, 1.3677, 3.1458, 34.5, 10.59, 4046.69, 87.7, 84.0, "600030.SH", ["value", "cyclical", "blue_chip"]),
        # ── ⑤ 明确错配：成长分低 / 高价高息 / 保险 ─────────────────────
        ("天有为", "汽车零部件", 11.839, 1.1975, 4.9096, 43.4, 16.23, 81.42, 47.2, 87.0, "603202.SH", ["dividend"]),
        ("星宇股份", "汽车零部件", 12.5061, 1.7518, 2.877, 35.2, 15.1, 198.4, 71.3, 79.5, "601799.SH", ["value"]),
        ("中国人寿", "保险Ⅱ", 4.1984, 1.5673, 2.3267, 15.7, 27.81, 10387.27, 90.4, 90.0, "601628.SH", ["value", "blue_chip"]),
        # ── ② 无标签高股息：补红利（用户点名的 19 只抽样）─────────────
        ("弘亚数控", "专用设备", 18.0921, 2.7164, 4.7851, 88.7, 14.81, 80.15, 76.6, 87.0, "002833.SZ", ["growth", "dividend"]),
        ("小商品城", "一般零售", 13.2489, 2.6919, 4.6089, 65.2, 17.53, 594.96, 89.8, 87.0, "600415.SH", ["growth", "dividend"]),
        ("美亚光电", "专用设备", 18.6049, 5.2818, 4.5395, 85.9, 26.05, 136.04, 74.1, 89.7, "002690.SZ", ["dividend"]),
        ("康尼机电", "轨交设备Ⅱ", 14.4235, 1.3722, 4.892, 73.6, 8.78, 52.85, 74.6, 80.5, "603111.SH", ["dividend"]),
        ("理工能科", "软件开发", 19.3332, 1.6103, 5.1127, 105.6, 7.4, 43.58, 74.4, 77.6, "002322.SZ", ["dividend"]),
        ("S佳通", "汽车零部件", 20.0358, 3.9099, 4.5856, 98.3, 16.59, 42.26, 79.6, 77.5, "600182.SH", ["growth", "dividend"]),
        # ── 成长赛道（维度分 ≥75 且不便宜）─────────────────────────────
        ("东鹏饮料", "饮料乳品", 16.5236, 4.162, 4.5256, 62.2, 51.61, 810.99, 89.8, 87.0, "605499.SH", ["growth", "dividend"]),
        ("甘源食品", "休闲食品", 16.1701, 2.1037, 4.3531, 71.2, 12.08, 34.58, 81.2, 84.4, "002991.SZ", ["growth", "dividend"]),
        ("珀莱雅", "化妆品", 11.5473, 3.2386, 3.6727, 52.9, 25.8, 215.57, 86.0, 86.8, "603605.SH", ["growth"]),
        ("焦点科技", "互联网电商", 22.1205, 4.0572, 5.1822, 81.9, 20.6, 103.75, 73.5, 90.0, "002315.SZ", ["dividend"]),
        ("热威股份", "家电零部件Ⅱ", 24.3854, 3.8826, 3.5438, 87.2, 15.33, 79.48, 78.1, 86.8, "603075.SH", ["growth"]),
        # ── 改前判对、必须零回归的对照 ────────────────────────────────
        ("巨人网络", "游戏Ⅱ", 13.5987, 2.773, 1.5247, 37.1, 12.45, 424.59, 89.6, 90.0, "002558.SZ", ["growth"]),
        ("恺英网络", "游戏Ⅱ", 14.2261, 3.2302, 0.6283, 11.2, 23.13, 338.84, 89.6, 90.0, "002517.SZ", ["growth"]),
        ("吉比特", "游戏Ⅱ", 10.8792, 4.566, 5.7908, 78.7, 33.27, 243.68, 85.3, 90.0, "603444.SH", ["growth", "dividend"]),
        # 2026-09-29：煤炭红利豁免（COAL_DIVIDEND_INDUSTRIES）已废除 → 煤炭回归周期轴。
        # 风格层「维度 B 命中即加」允许周期与红利并存（高股息是事实），故煤炭股
        # 现在是「周期 (+ 红利) (+ 蓝筹)」；评分侧则统一走周期框架（非红利）。
        # 陕西煤业息 3.71% < 3.95% 门槛、且煤炭已移出 DIVIDEND_INDUSTRIES
        # → 不再有红利标签（此前靠「豁免 + 弱周期红利行业」路径拿到）。
        ("中国神华", "煤炭开采", 18.9253, 2.2702, 4.2668, 79.1, 12.76, 10215.72, 78.5, 88.4, "601088.SH", ["cyclical", "dividend", "blue_chip"]),
        ("陕西煤业", "煤炭开采", 12.1229, 2.4501, 3.7142, 54.8, 17.88, 2473.2, 79.2, 83.5, "601225.SH", ["cyclical", "blue_chip"]),
        ("电投能源", "煤炭开采", 12.0022, 1.6266, 4.7313, 53.8, 14.94, 859.46, 85.5, 87.7, "002128.SZ", ["cyclical", "dividend"]),
        ("中国海油", "油气开采Ⅱ", 11.4834, 1.8583, 3.4258, 44.6, 15.64, 15884.51, 82.6, 90.0, "600938.SH", ["cyclical", "blue_chip"]),
        ("盐湖股份", "农化制品", 10.2303, 2.5777, 0.0894, 0.9, 22.3, 1232.41, 80.6, 90.0, "000792.SZ", ["cyclical", "blue_chip"]),
        ("云铝股份", "工业金属", 8.0411, 2.309, 2.745, 40.0, 19.7, 882.25, 85.1, 79.5, "000807.SZ", ["cyclical"]),
        ("焦作万方", "工业金属", 6.9275, 1.5148, 3.1688, 36.2, 16.0, 122.32, 86.9, 82.6, "000612.SZ", ["cyclical"]),
        ("赤峰黄金", "贵金属", 19.4955, 5.0421, 0.836, 19.7, 27.14, 722.91, 89.1, 90.0, "600988.SH", ["cyclical"]),
        ("贵州茅台", "白酒Ⅱ", 18.9718, 6.1498, 4.2109, 79.0, 32.53, 15447.01, 68.3, 89.7, "600519.SH", ["dividend", "blue_chip"]),
        ("分众传媒", "广告营销", 19.8276, 5.0891, 6.1932, 166.7, 18.28, 675.89, 74.9, 90.0, "002027.SZ", ["dividend"]),
        ("中国平安", "保险Ⅱ", 5.9547, 0.9428, 5.156, 36.3, 14.0, 9486.59, 85.1, 86.4, "601318.SH", ["dividend", "blue_chip"]),
    ],
)
def test_style_labels_match_real_snapshot_table(
    name, industry, pe, pb, dy, payout, roe, mcap, growth, profit, symbol, expect
):
    """用户点名的三类系统性问题 → 逐票锁定期望标签（真实快照数值，两维叠加）。"""
    tags = _tags(industry, name=name, symbol=symbol, pe=pe, pb=pb, dy=dy, payout=payout,
                 roe=roe, mcap=mcap, growth=growth, profit=profit)
    assert tags == expect, f"{name}({industry}) → {tags}，期望 {expect}"


@pytest.mark.parametrize(
    "name,industry,dy,payout,expect_dividend",
    [
        # 真高股息：分红被利润覆盖、股息率在合理区间 → 红利
        ("分众传媒", "广告营销", 6.193, 166.7, True),
        ("吉比特", "游戏Ⅱ", 5.77, 78.7, True),
        ("承德露露", "饮料乳品", 6.2915, 60.0, True),
        # 伪影：股息率 >12% 多半是股价崩了 / 一次性特别分红
        ("山子高科", "汽车零部件", 25.9257, 832.0, False),
        ("皓宸医疗", "医疗服务", 39.2178, None, False),
        ("东方雨虹", "装修建材", 14.7387, 3394.0, False),
        ("*ST起步", "服装家纺", 13.3776, 132.0, False),
        ("中公教育", "教育", 13.0459, None, False),
        # 伪影：分红率 >200%（分红远超利润）
        ("文科股份", "基础建设", 26.7873, 289.0, False),
        ("建元信托", "多元金融", 21.2798, 429.0, False),
        ("汇洁股份", "服装家纺", 14.9488, 823.0, False),
        # 🔴 分红率缺失：无法验证「分红是否被利润覆盖」→ 不判红利（铁律 8，缺失不猜）
        ("某高息无分红率票", "家居用品", 8.5, None, False),
        ("某高息无分红率票2", "广告营销", 4.5, None, False),
    ],
)
def test_high_yield_fallback_rejects_artifacts(name, industry, dy, payout, expect_dividend):
    """高股息兜底的双向边界：挡「股价崩了的一次性高息」，且分红率缺失不猜。"""
    tags = _tags(industry, pe=15.0, pb=1.5, dy=dy, payout=payout, roe=8.0,
                 mcap=100.0, growth=60.0, profit=80.0)
    assert ("dividend" in tags) is expect_dividend, f"{name} 息{dy}% 分红{payout}% → {tags}"


def test_dividend_evidence_shared_by_both_paths():
    """分红证据是唯一实现：行业路径与高股息兜底共用，边界一致（铁律 10）。

    语义为「或」（两条证据任一成立即算，但每条自身都有上下界）：
    红利行业里 5% 股息率本身就是有效证据，不必再看分红率是否被覆盖。
    """
    assert ms.dividend_evidence(6.0, 50.0) == "分红率 50%"
    assert ms.dividend_evidence(1.5, 250.0) is None          # 分红率超上限 且 股息率不足
    assert ms.dividend_evidence(6.0, 20.0) == "股息率 6.0%"  # 分红率不足但股息率够
    assert ms.dividend_evidence(15.0, 20.0) is None          # 股息率超上限（股价崩了）
    assert ms.dividend_evidence(None, None) is None          # 缺失不猜
    # 红利行业 + 双证据都越界 → 不判红利（行业路径同样受双向边界约束）
    assert "dividend" not in _tags("电力", pe=15.0, pb=2.0, dy=14.0, payout=900.0,
                                   roe=12.0, mcap=100.0, growth=40.0, profit=80.0)


def test_style_matches_is_tag_membership_and_other_means_no_tag():
    """style_matches 唯一实现：任一标签命中即算；「其他」= 标签列表为空。"""
    two = {"style_tags": ["cyclical", "dividend"]}
    assert ms.style_matches(two, "cyclical") and ms.style_matches(two, "dividend")
    assert not ms.style_matches(two, "value") and not ms.style_matches(two, "other")
    none_ = {"style_tags": []}
    assert ms.style_matches(none_, "other")
    for k in ("value", "growth", "cyclical", "dividend", "blue_chip"):
        assert not ms.style_matches(none_, k)
    # 旧快照没有 style_tags 键 → 视为无标签（不炸）
    assert ms.style_matches({}, "other")


def test_scan_market_style_tags_labels_filter_and_counts():
    """style_tags / style_label / style_reason 透出 + 服务端筛选与 style_counts 同源。"""
    db = _style_db(
        [
            # 周期 + 红利（天山铝业量级）
            ("002532.SZ", _style_payload(industry="工业金属", pb=1.77, dy=4.65,
                                         payout=52.9, roe=17.0, mcap_yi=543.4,
                                         growth_score=86.9, profit_score=83.6), 7.86),
            # 价值 + 周期（长江证券量级）
            ("000783.SZ", _style_payload(industry="证券Ⅱ", pb=1.20, dy=3.76,
                                         payout=44.9, roe=10.02, mcap_yi=440.8,
                                         growth_score=90.4, profit_score=84.0), 8.56),
            # 红利（收租，中国国贸白名单）
            ("600007.SH", _style_payload(industry="房地产开发", name="中国国贸", 
                                         pb=2.18, dy=5.31, payout=89.6, roe=12.64,
                                         mcap_yi=202.9, growth_score=66.7,
                                         profit_score=90.0), 16.86),
            # 其他（无任何标签）
            ("000004.SZ", _style_payload(industry="综合Ⅱ", pb=6.0, dy=0.8,
                                         payout=10.0, roe=5.0, mcap_yi=30.0,
                                         growth_score=50.0, profit_score=50.0), 80.0),
        ]
    )
    try:
        data = ms.scan_market(db, top=10)
        items = {it["symbol"]: it for it in data["items"]}
        assert data["matched"] == 4
        counts = data["style_counts"]
        # 两维叠加：sum(counts) ≥ 命中总数（多标签条目被每个标签各计一次）
        assert counts["cyclical"] == 2 and counts["dividend"] == 2
        assert counts["value"] == 1 and counts["other"] == 1
        assert sum(counts.values()) == 6 > 4

        # 标签列表 + 中文组合名 + 依据
        assert items["002532.SZ"]["style_tags"] == ["cyclical", "dividend"]
        assert items["002532.SZ"]["style"] == "cyclical"
        assert items["002532.SZ"]["style_label"] == "周期+红利"
        assert "强周期" in items["002532.SZ"]["style_reason"]
        assert items["000783.SZ"]["style_label"] == "价值+周期"
        assert items["600007.SH"]["style_tags"] == ["dividend"]
        assert items["600007.SH"]["style_label"] == "红利股"
        assert items["000004.SZ"]["style_tags"] == []
        assert items["000004.SZ"]["style"] == "other"
        assert items["000004.SZ"]["style_label"] == "其他"

        # 服务端筛选：命中数 == chip 计数（口径同源）
        for k in ("value", "growth", "cyclical", "dividend", "blue_chip", "other"):
            assert ms.scan_market(db, top=50, style=k)["matched"] == counts.get(k, 0)
        div = ms.scan_market(db, top=10, style="dividend")
        assert [it["symbol"] for it in div["items"]] == ["002532.SZ", "600007.SH"]
        # style_counts 不随 style 过滤缩水
        assert div["style_counts"] == counts
        assert div["filters"]["style"] == "dividend"

        # 分页口径：style 筛选后 offset 在命中集内偏移
        paged = ms.scan_market(db, top=1, offset=1, style="dividend")
        assert paged["matched"] == 2
        assert [it["symbol"] for it in paged["items"]] == ["600007.SH"]

        # 「价值」不再是已删风格（旧链接 value 现在合法并命中券商）
        val = ms.scan_market(db, top=10, style="value")
        assert [it["symbol"] for it in val["items"]] == ["000783.SZ"]
        with pytest.raises(ValueError):
            ms.scan_market(db, top=5, style="不存在的风格")
        # 「防御股」已删除 → 拒绝（避免前端旧链接静默命中空集）
        with pytest.raises(ValueError):
            ms.scan_market(db, top=5, style="defensive")
    finally:
        db.close()


def test_style_counts_include_blue_chip_alongside_main_tag():
    """蓝筹作为属性轴标签与主标签并存计入 style_counts（千亿周期红利股三处各计一次）。

    2026-09-29：煤炭红利豁免废除后，中国神华同时带周期/红利/蓝筹三个标签。
    """
    db = _style_db(
        [
            # 千亿周期红利龙头（中国神华量级）→ 周期 + 红利 + 蓝筹
            ("601088.SH", _style_payload(industry="煤炭开采", pb=2.27, dy=4.27,
                                         payout=79.1, roe=12.76, mcap_yi=10215.7,
                                         growth_score=78.5, profit_score=88.4), 18.93),
            ("000002.SZ", _style_payload(industry="综合Ⅱ", pb=6.0, dy=0.8,
                                         payout=10.0, roe=5.0, mcap_yi=30.0,
                                         growth_score=50.0, profit_score=50.0), 80.0),
        ]
    )
    try:
        data = ms.scan_market(db, top=10)
        counts = data["style_counts"]
        assert data["matched"] == 2
        assert (
            counts["cyclical"] == 1
            and counts["dividend"] == 1
            and counts["blue_chip"] == 1
            and counts["other"] == 1
        )
        bc = ms.scan_market(db, top=10, style="blue_chip")
        assert bc["matched"] == counts["blue_chip"] == 1
        assert [it["symbol"] for it in bc["items"]] == ["601088.SH"]
        items = {it["symbol"]: it for it in data["items"]}
        assert items["601088.SH"]["style_label"] == "周期+红利+蓝筹"
    finally:
        db.close()


def test_style_anchors_to_snapshot_when_quote_overlay_moves_yield(monkeypatch):
    """风格判据锚定**快照原值**：日内行情覆盖把股息率压到阈值下也不改标签。

    真实案例：新和成 快照股息率 3.96% ≥ 3.95%，但盘中价上行（10.0 → 25.33）把
    展示用日股息率压到 1.56% —— 若用覆盖后的值判定，「周期+红利」会在盘中反复翻转，
    而「风格」是结构性属性（用户报的股息率也是快照值）。展示字段仍用行情值。
    """
    db = _style_db(
        [
            ("002001.SZ", _style_payload(industry="化学制品", name="新和成", pb=2.22, dy=3.96,
                                         payout=45.4, roe=21.87, mcap_yi=774.19,
                                         growth_score=84.8, profit_score=85.3), 10.80),
        ]
    )
    monkeypatch.setattr(
        ms,
        "_market_snapshot",
        lambda: {
            "ok": True,
            "ts": "2026-09-29",
            "source": "test",
            "items": {"002001.SZ": {"price": 25.33}},
        },
    )
    try:
        rows, _ = ms.load_covered(db)
        # 覆盖前原值已留档（供风格判据使用）
        assert rows[0]["_snap"]["dividend_yield"] == 3.96
        it = ms._base_items(rows)[0]
        # 展示字段用行情覆盖后的日股息率与市值
        assert it["dividend_yield"] == round(3.96 / (25.33 / 10.0), 4)
        assert it["market_cap_yi"] > 1000  # 覆盖后被放大
        # 风格标签仍按快照判定 → 周期 + 红利（不被日内价压掉）
        assert it["style_tags"] == ["cyclical", "dividend"]
        assert "股息率 3.96%" in it["style_reason"]
        # 蓝筹也必须按快照市值（774 亿 < 1000 亿）→ 覆盖到 1961 亿也不给蓝筹
        assert "blue_chip" not in it["style_tags"]
    finally:
        db.close()






# ── 高股息筛选「真高股息防御型」v4（2026-09-30 用户框架，替换 v3 板块名单口径）──
# 判据：① 剔ST ∧ 市值≥30亿；② 动态股息率（周期股近三年均息≥5% / 非周期 TTM≥3.5%）；
#      ③ 分红档案新鲜度（档案最新完整会计年度 ≥ 最近应已披露完毕的年度，随日期滚动）；
#      ④ 连续分红≥3年 ∧ 支付率30%~90%；⑤ 财务底线软组合（偿债分≥40 ∨ OCF覆盖≥1.0）；
#      ⑥ 估值拥挤度熔断（PE/PB 分位取高者 ≥95 剔除）。
# ②③④ 三维依赖 stock_dividend_profile 分红档案表
# （div_years / dps_3y_avg / div_latest_fy），故测试要走「快照 + 档案」两条数据源，
# 不能只塞 payload。


def _hd_payload(
    *,
    name: str = "高分红",
    industry: str = "电力",
    dy: float | None = 4.5,
    payout: float | None = 50.0,
    solv: float | None = 70.0,
    pe_pct: float | None = 45.0,
    pb_pct: float | None = 40.0,
    price: float | None = 10.0,
    mcap: float | None = 2e10,
    pledge: float | None = None,
    in_rw: bool = False,
    dual: bool | None = None,
    roe: float | None = None,
    debt: float | None = None,
    cash: float | None = None,
    ibd: float | None = None,
    ta: float | None = None,
    ibd_explicit: bool | None = None,
) -> str:
    """高股息判据所需 payload。字段路径与线上一致，缺一判据就「缺数据」。

    roe / debt / cash/ibd/ta / dual 保留是**故意**的：v4 不含这些维度，
    用例据此证明它们即使处在危险区间也不再影响命中（撤除不是「忘了读」）。
    """
    market: dict = {
        "market_cap": mcap,
        "price": price,
        "pb": 1.5,
        "dividend_yield": dy,
        "pe_percentile": pe_pct,
        "pb_percentile": pb_pct,
    }
    solvency_meta: dict = {}
    if dual is not None:
        solvency_meta["deposit_loan_dual_high"] = dual
    bs: dict = {}
    if cash is not None:
        bs["monetary_funds"] = cash
    if ibd is not None:
        bs["interest_bearing_debt"] = ibd
    if ta is not None:
        bs["total_assets"] = ta
    if ibd_explicit is not None:
        bs["interest_bearing_explicit"] = ibd_explicit
    if bs:
        solvency_meta["balance_sheet"] = bs
    indicators: list[dict] = []
    if debt is not None:
        indicators.append({"name": "资产负债率(%)", "value": debt})
    return json.dumps(
        {
            "name": name,
            "industry": industry,
            "final_rating": "A",
            "risk_level_label": "低",
            "market": market,
            "dim_scores": {} if solv is None else {"偿债能力": solv},
            "valuation": {"dividend_profile": {"payout_ratio_pct": payout}},
            "modules": {
                "profitability": {"indicators": [{"name": "ROE(%)", "value": roe}]},
                "solvency": {"indicators": indicators, "metadata": solvency_meta},
            },
            "major_risks": {"pledge_ratio": pledge, "in_reduce_window": in_rw},
        },
        ensure_ascii=False,
    )


def _prof(years: int = 8, dps3: float = 0.5, latest_fy: int | None = None) -> dict:
    """分红档案行（div_years / dps_3y_avg / div_latest_fy 的载体）。

    ``latest_fy`` 默认取 ``ms.hd_required_fy()``（**刻意不写死年份**）：判据③「档案
    新鲜度闸门」随日期滚动，写死会让整数十条用例在跨年后集体腐烂 —— 与 K 线类
    测试必须相对当前日期生成同一个道理（见 skill 的「测试腐烂」教训）。
    """
    fy = ms.hd_required_fy() if latest_fy is None else latest_fy
    return {
        "consecutive_years": years,
        "latest_fy": fy,
        "dps_latest_fy": dps3,
        "dps_3y_avg": dps3,
        "dps_series": json.dumps(
            {str(fy): dps3, str(fy - 1): dps3, str(fy - 2): dps3}
        ),
        "source": "test",
    }


def _hd_db(*snapshots, profiles: dict[str, dict] | None = None,
           pes: dict[str, float | None] | None = None):
    """按 (symbol, payload_str) 建库，并写入分红档案。

    ``pes``：覆盖 ``FactorSnapshot.pe_ttm`` **列**（默认 10.0，见 ``_make_db``）。
    为什么必须能控制它：v5 判据⑧（隐含分红率 = TTM 息 × PE）与⑨（PE<0 → 观察池）
    读的都是这一列（线上由 ``upsert`` 从 report["market"]["pe_ttm"] 写入），
    不覆盖的话 ⑨ 的「PE<0」分支在测试里永远走不到 = 死代码。
    """
    from app.models.dividend_profile import StockDividendProfile

    db = _make_db(
        [(sym, "占位名", {"comp": 80.0, "ind": "电力", "raw": raw}) for sym, raw in snapshots],
        [sym for sym, _ in snapshots],
    )
    for sym, kw in (profiles or {}).items():
        db.add(StockDividendProfile(symbol=sym, **kw))
    for sym, val in (pes or {}).items():
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == sym).one()
        row.pe_ttm = val
    db.commit()
    return db


def _hd_item_of(db, symbol: str) -> dict:
    """取某只标的高股息条目的完整读层输出（留痕 + hd_pool/hd_review/台账）。"""
    items = ms._base_items(ms.load_covered(db)[0])
    for it in items:
        if it["symbol"] == symbol:
            return it
    raise AssertionError(f"{symbol} 不在条目中")


def _hd_detail_of(db, symbol: str) -> list[str]:
    """取某只标的的高股息留痕（用于断言「卡在哪一维」）。"""
    return _hd_item_of(db, symbol)["hd_detail"]


def test_high_dividend_constants_lock_v4_thresholds():
    """锁 v4 框架阈值，并在测试层锁住「v3 维度常量不得复活」。"""
    assert ms.HD_MIN_MCAP_YI == 30.0
    assert ms.HD_CYCLICAL_YIELD_3Y_MIN == 5.0
    assert ms.HD_MIN_YIELD == 3.5
    assert ms.HD_MIN_DIV_YEARS == 3
    assert (ms.HD_PAYOUT_MIN, ms.HD_PAYOUT_MAX) == (30.0, 90.0)
    assert ms.HD_SOLVENCY_FLOOR == 40.0
    assert ms.HD_OCF_DIV_COVER_MIN == 1.0
    assert ms.HD_CROWD_PERCENTILE == 95.0
    assert ms.HD_PROFILE_DISCLOSURE_MONTH == 5
    assert (ms.HD_SCORE_W_YIELD, ms.HD_SCORE_W_SOLVENCY, ms.HD_SCORE_W_PAYOUT) == (0.4, 0.3, 0.3)
    # v3 的六个维度常量必须已撤除（复活 = 口径回退）
    for dead in ("HD_MIN_DY", "HD_MIN_ROE", "HD_MAX_DEBT", "HD_PLEDGE_MAX", "HD_MAX_PB",
                 "HD_MAX_PE_PCT", "HD_FINANCE_IND_PREFIXES"):
        assert not hasattr(ms, dead), f"{dead} 不应存在（v4 已撤除该维度）"
    # 周期名册必须是引擎既有强周期名册的**子集**（防两处各算 / 名字漂移）
    from app.analysis.engine import STRONG_CYCLICAL_INDUSTRIES

    assert ms.HD_CYCLICAL_INDUSTRIES <= STRONG_CYCLICAL_INDUSTRIES
    assert "特钢Ⅱ" in ms.HD_CYCLICAL_INDUSTRIES
    assert "有色金属" not in ms.HD_CYCLICAL_INDUSTRIES  # 一级行业，库里不存在该名
    assert {"工业金属", "小金属", "贵金属"} <= ms.HD_CYCLICAL_INDUSTRIES


def test_high_dividend_cyclical_uses_three_year_avg_not_ttm():
    """动态门槛：周期股看近三年均息，非周期看 TTM（同一组数字，行业不同结论不同）。

    v5 起多了 ⑦「当前 TTM ≥4.0%」这道**一律适用**的下限，故本用例把 TTM 设在
    4.5%（先过 ⑦）来隔离出判据② 的差异：三年均息 4.0% < 5% 的周期股被拒，
    而同数字的非周期股按 TTM 4.5% ≥ 3.5% 通过。
    """
    db = _hd_db(
        # 周期（煤炭开采）：TTM 4.5% 过 ⑦，但三年均息 4.0% < 5% → 判据② 拒
        ("601001.SH", _hd_payload(industry="煤炭开采", dy=4.5)),
        # 同一组数字但行业非周期 → TTM 4.5% ≥ 3.5% → 命中
        ("601002.SH", _hd_payload(industry="电力", dy=4.5)),
        profiles={"601001.SH": _prof(dps3=0.4), "601002.SH": _prof(dps3=0.4)},
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert [i["symbol"] for i in data["items"]] == ["601002.SH"]
        hit = data["items"][0]
        assert any("股息率4.50%✓" in d for d in hit["hd_detail"])
        drop = _hd_detail_of(db, "601001.SH")
        assert any("周期·三年均息4.00%✗" in d for d in drop)
        # 该票 ⑦ 是过的（卡点是②），避免把两种口径混为一谈
        assert any("当前息4.50%✓" in d for d in drop)
    finally:
        db.close()


def test_high_dividend_v5_thresholds_locked():
    """锁 v5 三条阈值（改口径必须同步改测试，防静默漂移）。"""
    assert ms.HD_MIN_YIELD_FLOOR == 4.0
    assert ms.HD_IMPLIED_PAYOUT_MAX == 150.0
    assert ms.HD_IMPLIED_PAYOUT_REVIEW == 100.0
    # v4 的两道门槛仍在（⑦⑧⑨ 是**叠加**，不是替换）
    assert (ms.HD_MIN_YIELD, ms.HD_CYCLICAL_YIELD_3Y_MIN) == (3.5, 5.0)


def test_high_dividend_v5_ttm_floor_beats_high_three_year_avg():
    """⑦ 当前 TTM 股息率 <4.0% 一律移出主池 —— **含周期股**（靶点：冀中能源）。

    冀中能源型：三年均息 11% 过得了判据②，但当年 TTM 只有 2.29% → ⑦ 才是卡点。
    这条正是用户 2026-09-30 要的语义：「防御型要求**当下**也有息」。
    """
    db = _hd_db(
        ("601011.SH", _hd_payload(industry="煤炭开采", dy=2.29)),
        ("601012.SH", _hd_payload(industry="煤炭开采", dy=4.5)),
        profiles={"601011.SH": _prof(dps3=1.1), "601012.SH": _prof(dps3=1.1)},
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert [i["symbol"] for i in data["items"]] == ["601012.SH"]
        drop = _hd_detail_of(db, "601011.SH")
        assert any("周期·三年均息11.00%✓" in d for d in drop)  # ② 过了
        assert any("当前息2.29%✗" in d for d in drop)  # ⑦ 卡住
        # ⑦ 是必判维度：缺「当前息」同样不入选（缺失不免检）
        assert not any("✓" in d and "当前息" in d for d in drop)
    finally:
        db.close()


def test_high_dividend_v5_implied_payout_bounds():
    """⑧ 隐含分红率（= TTM 息 × PE）：>150% 剔除；100~150% 入池但标黄；≤100% 正常。

    pe_ttm 列固定 10.0（见 _make_db）→ 隐含分红率 = dy × 10：
    dy 4.5 → 45%（正常）/ dy 12 → 120%（标黄）/ dy 16 → 160%（剔除）。
    """
    db = _hd_db(
        ("600101.SH", _hd_payload(dy=4.5)),
        ("600102.SH", _hd_payload(dy=12.0)),
        ("600103.SH", _hd_payload(dy=16.0)),
        profiles={s: _prof() for s in ("600101.SH", "600102.SH", "600103.SH")},
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert sorted(i["symbol"] for i in data["items"]) == ["600101.SH", "600102.SH"]
        assert data["hd_review_count"] == 1
        flagged = next(i for i in data["items"] if i["symbol"] == "600102.SH")
        assert flagged["hd_review"] is True
        assert flagged["hd_pool"] == "main"
        assert flagged["hd_implied_payout_pct"] == pytest.approx(120.0)
        assert any("隐含分红率120.0%⚠待复核" in d for d in flagged["hd_detail"])
        # 台账（hd_metrics）必须吐出判据实际吃进去的数字
        assert flagged["hd_metrics"]["implied_payout_pct"] == pytest.approx(120.0)
        assert flagged["hd_metrics"]["yield_ttm_snap_pct"] == pytest.approx(12.0)
        normal = next(i for i in data["items"] if i["symbol"] == "600101.SH")
        assert normal["hd_review"] is False
        dropped = _hd_item_of(db, "600103.SH")
        assert dropped["hd_pool"] is None
        assert any("隐含分红率160.0%✗" in d for d in dropped["hd_detail"])
    finally:
        db.close()


def test_high_dividend_v5_implied_payout_missing_is_undetermined():
    """⑧ 缺失 = **未判定**（熔断类语义）：PE 缺失时不剔除、不标黄，但必须留痕。

    与判据⑥ 估值拥挤度同语义 —— 熔断类判据缺失不能当「有问题」剔除，
    也不能当「没问题」静默通过（要有「未判定」的痕）。
    """
    db = _hd_db(
        ("600401.SH", _hd_payload()),
        profiles={"600401.SH": _prof()},
        pes={"600401.SH": None},
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert [i["symbol"] for i in data["items"]] == ["600401.SH"]
        assert data["hd_review_count"] == 0
        assert data["items"][0]["hd_implied_payout_pct"] is None
        assert any("隐含分红率未判定" in d for d in data["items"][0]["hd_detail"])
    finally:
        db.close()


def test_high_dividend_v5_loss_making_goes_to_watch_pool():
    """⑨ 亏损分红（PE<0）单列观察池，不计主池；且**优先于⑦**（靶点：万和电气）。

    万和电气型：TTM 息 3.58% < 4.0% 本会被 ⑦ 当「息不够」剔除，但 PE=−53 是亏损分红，
    用户要的是「单列观察」而非剔除（亏损 ≠ 骗息）→ ⑨ 必须先判。
    """
    db = _hd_db(
        ("600201.SH", _hd_payload()),  # 正常 → 主池
        ("600202.SH", _hd_payload(dy=4.36)),  # 北大荒型：TTM 过 ⑦，但 PE<0 → 观察池
        ("600203.SH", _hd_payload(dy=3.58)),  # 万和电气型：TTM<4 且 PE<0 → 仍进观察池
        profiles={s: _prof() for s in ("600201.SH", "600202.SH", "600203.SH")},
        pes={"600202.SH": -63.29, "600203.SH": -53.05},
    )
    try:
        main = ms.scan_market(db, top=10, high_dividend=True)
        assert [i["symbol"] for i in main["items"]] == ["600201.SH"]
        assert main["hd_watch_count"] == 2
        watch_item = _hd_item_of(db, "600203.SH")
        assert watch_item["hd_pool"] == "watch"
        assert watch_item["high_dividend"] is False
        assert any("亏损分红·观察池" in d for d in watch_item["hd_detail"])
        # ⑨ 优先 ⇒ 不得出现 ⑦ 的「当前息✗」判词（否则说明顺序写反了）
        assert not any("当前息3.58%✗" in d for d in watch_item["hd_detail"])
        # hd_watch=true 单独取观察池
        watch = ms.scan_market(db, top=10, hd_watch=True)
        assert sorted(i["symbol"] for i in watch["items"]) == ["600202.SH", "600203.SH"]
        assert all(i["hd_pool"] == "watch" for i in watch["items"])
        # 观察池不参与池内分位归一（hd_score 只对主池有意义 → 该键不存在）
        assert all(i.get("hd_score") is None for i in watch["items"])
    finally:
        db.close()


def test_hd_watch_is_exclusive_with_high_dividend():
    """观察池与主池互斥：同时传两个 = 调用方意图不明，显式失败而非静默返回空集。"""
    db = _hd_db(("600301.SH", _hd_payload()), profiles={"600301.SH": _prof()})
    try:
        with pytest.raises(ValueError):
            ms.scan_market(db, top=5, high_dividend=True, hd_watch=True)
        # 单独用各自都正常
        assert ms.scan_market(db, top=5, high_dividend=True)["matched"] == 1
        assert ms.scan_market(db, top=5, hd_watch=True)["matched"] == 0
    finally:
        db.close()


def test_high_dividend_continuity_and_payout_bounds():
    """判据③：连续分红<3年 / 支付率出界 都拒绝，且留痕指出具体哪一维。"""
    db = _hd_db(
        ("601101.SH", _hd_payload(payout=50.0)),
        ("601102.SH", _hd_payload()),  # 连续 2 年
        ("601103.SH", _hd_payload(payout=95.0)),  # 支付率 >90
        ("601104.SH", _hd_payload(payout=25.0)),  # 支付率 <30
        profiles={
            "601101.SH": _prof(years=8),
            "601102.SH": _prof(years=2),
            "601103.SH": _prof(years=8),
            "601104.SH": _prof(years=8),
        },
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert [i["symbol"] for i in data["items"]] == ["601101.SH"]
        assert any("连续分红2年✗" in d for d in _hd_detail_of(db, "601102.SH"))
        assert any("支付率95.0%✗" in d for d in _hd_detail_of(db, "601103.SH"))
        assert any("支付率25.0%✗" in d for d in _hd_detail_of(db, "601104.SH"))
    finally:
        db.close()


def test_high_dividend_missing_data_never_qualifies():
    """缺失不免检：无分红档案 / 缺三年均息 / 缺市值 都不得入选。"""
    db = _hd_db(
        ("601201.SH", _hd_payload()),  # 无档案 → 分红档案缺（判据③ 闸门先命中）
        ("601202.SH", _hd_payload(industry="煤炭开采", dy=6.0)),  # 周期但无档案 → 三年均息缺
        ("601203.SH", _hd_payload(mcap=None)),  # 市值缺
        profiles={"601202.SH": _prof(dps3=None), "601203.SH": _prof()},
    )
    try:
        assert ms.scan_market(db, top=10, high_dividend=True)["matched"] == 0
        assert any("分红档案缺" in d for d in _hd_detail_of(db, "601201.SH"))
        assert any("周期·三年均息缺" in d for d in _hd_detail_of(db, "601202.SH"))
        assert any("市值缺" in d for d in _hd_detail_of(db, "601203.SH"))
    finally:
        db.close()


def test_high_dividend_financial_floor_is_soft_combination():
    """判据④ 软组合：偿债分达标 ∨ 现金流覆盖分红达标 —— 两条都差才否。"""
    db = _hd_db(
        # 偿债 35（低于 40）但 OCF 覆盖 1.5（≥1.0）→ 过关
        ("601301.SH", _hd_payload(solv=35.0, payout=50.0)),
        # 偿债 35 且覆盖 0.5（=1.0/(0.5)… 见下方 _payload OCF 注入）→ 否
        ("601302.SH", _hd_payload(solv=35.0, payout=50.0)),
        # 偿债 55 达标（覆盖再差也过关）
        ("601303.SH", _hd_payload(solv=55.0, payout=50.0)),
        profiles={"601301.SH": _prof(), "601302.SH": _prof(), "601303.SH": _prof()},
    )
    # OCF/净利：301 给 0.75（0.75÷0.5=1.5 覆盖），302 给 0.25（0.25÷0.5=0.5 不覆盖），
    # 303 给 0.25（不覆盖但偿债达标）
    for sym, val in (("601301.SH", 0.75), ("601302.SH", 0.25), ("601303.SH", 0.25)):
        r = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == sym).one()
        p = json.loads(r.payload)
        p["modules"]["cashflow"] = {"indicators": [{"name": "经营现金流/净利润", "value": val}]}
        r.payload = json.dumps(p, ensure_ascii=False)
    db.commit()
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert sorted(i["symbol"] for i in data["items"]) == ["601301.SH", "601303.SH"]
        assert any("偿债35且现金流覆盖分红0.50✗" in d for d in _hd_detail_of(db, "601302.SH"))
    finally:
        db.close()


def test_high_dividend_valuation_crowding_circuit_breaker():
    """判据⑤ 熔断：PE/PB 分位取**较高者**；两者都缺 = 未判定（不熔断）。"""
    db = _hd_db(
        ("601401.SH", _hd_payload(pe_pct=96.0, pb_pct=20.0)),  # PE 极高 → 熔断
        ("601402.SH", _hd_payload(pe_pct=20.0, pb_pct=97.0)),  # PB 极高 → 熔断
        ("601403.SH", _hd_payload(pe_pct=None, pb_pct=None)),  # 都缺 → 未判定放行
        ("601404.SH", _hd_payload(pe_pct=94.9, pb_pct=50.0)),  # 恰好线下 → 放行
        profiles={f"60140{i}.SH": _prof() for i in range(1, 5)},
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert sorted(i["symbol"] for i in data["items"]) == ["601403.SH", "601404.SH"]
        assert any("估值拥挤96%分位✗" in d for d in _hd_detail_of(db, "601401.SH"))
        assert any("估值拥挤97%分位✗" in d for d in _hd_detail_of(db, "601402.SH"))
        assert any("估值分位未判定" in d for d in _hd_detail_of(db, "601403.SH"))
    finally:
        db.close()


def test_high_dividend_st_and_mcap_gate():
    """判据①：ST/退市 与 市值<30亿 一律出局（即使用户关掉全局 exclude_st）。"""
    db = _hd_db(
        ("601501.SH", _hd_payload(name="ST某某")),
        ("601502.SH", _hd_payload(mcap=2.5e9)),  # 25 亿
        profiles={"601501.SH": _prof(), "601502.SH": _prof()},
    )
    try:
        assert ms.scan_market(db, top=10, high_dividend=True, exclude_st=False)["matched"] == 0
        assert any("ST✗" in d for d in _hd_detail_of(db, "601501.SH"))
        assert any("市值25亿✗" in d for d in _hd_detail_of(db, "601502.SH"))
    finally:
        db.close()


def test_high_dividend_v3_dimensions_removed_and_hints_remain():
    """v4 撤除的六维不得参与判定；风险信息改为 hd_detail 提示项。"""
    db = _hd_db(
        (
            "601601.SH",
            _hd_payload(
                roe=1.0,  # ROE 极低
                debt=95.0,  # 负债率极高
                pledge=0.68,  # 大股东质押 68%
                in_rw=True,  # 减持窗口
                dual=True,  # 存贷双高（原标记）
                cash=3.2e9,
                ibd=2.4e9,
                ta=1.0e10,
                ibd_explicit=True,
            ),
        ),
        profiles={"601601.SH": _prof()},
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert [i["symbol"] for i in data["items"]] == ["601601.SH"]
        detail = data["items"][0]["hd_detail"]
        hint = [d for d in detail if d.startswith("提示·不参与判定")]
        assert hint, detail
        assert "存贷双高" in hint[0] and "质押68%" in hint[0] and "减持窗口" in hint[0]
    finally:
        db.close()


def test_high_dividend_score_is_pool_percentile_normalized():
    """综合评分：三维池内分位归一后按 0.4/0.3/0.3 加权（消掉原式量纲失衡）。"""
    db = _hd_db(
        # ①(v5) 起当前息下限 4.0%（锚定值）→ 池内最低息也得 ≥4.0，故 3.6 改为 4.2
        ("601701.SH", _hd_payload(dy=4.2, solv=45.0, payout=45.0)),
        ("601702.SH", _hd_payload(dy=5.0, solv=60.0, payout=60.0)),
        ("601703.SH", _hd_payload(dy=6.5, solv=90.0, payout=80.0)),
        ("601704.SH", _hd_payload(dy=1.0)),  # 不达标，不进池
        profiles={f"60170{i}.SH": _prof() for i in range(1, 5)},
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        items = {i["symbol"]: i for i in data["items"]}
        assert set(items) == {"601701.SH", "601702.SH", "601703.SH"}
        assert data["high_dividend_count"] == 3
        assert data["filters"]["high_dividend"] is True
        # 分位（3 只，平均秩）：16.7 / 50.0 / 83.3
        # A = .4*16.7 + .3*16.7 + .3*50.0 ；B = .4*50 + .3*50 + .3*83.3；C = .4*83.3 + .3*83.3 + .3*16.7
        assert items["601701.SH"]["hd_score"] == pytest.approx(26.69, abs=0.01)
        assert items["601702.SH"]["hd_score"] == pytest.approx(59.99, abs=0.01)
        assert items["601703.SH"]["hd_score"] == pytest.approx(63.32, abs=0.01)
        # 默认排序 = **股息率降序**（「仅高股息」自带主排序，2026-09-30 二次裁决）。
        # 本例三只的股息率恰好与 hd_score 同序，故顺序断言不变；**排序键必须换**。
        assert [i["symbol"] for i in data["items"]] == ["601703.SH", "601702.SH", "601701.SH"]
        assert data["sort_by"] == "dividend_yield"
        # 显式要求按评分排仍然被尊重（默认值只决定「不指定时」的行为）
        by_score = ms.scan_market(db, top=10, high_dividend=True, sort_by="hd_score")
        assert by_score["sort_by"] == "hd_score"
        # 池外标的不带分（缺失不赋中性值）
        out_of_pool = [
            i
            for i in ms._base_items(ms.load_covered(db)[0])
            if i["symbol"] == "601704.SH"
        ][0]
        assert "hd_score" not in out_of_pool
    finally:
        db.close()


def test_high_dividend_explicit_sort_still_respected_and_hd_score_implies_filter():
    """显式排序优先；sort_by=hd_score 隐含「仅高股息」过滤。"""
    db = _hd_db(
        ("601801.SH", _hd_payload(dy=4.2, solv=45.0)),
        ("601802.SH", _hd_payload(dy=6.5, solv=90.0)),
        ("601803.SH", _hd_payload(dy=1.0)),  # 出局
        profiles={f"60180{i}.SH": _prof() for i in range(1, 4)},
    )
    try:
        asc = ms.scan_market(
            db, top=10, high_dividend=True, sort_by="dividend_yield", sort_order="asc"
        )
        assert [i["symbol"] for i in asc["items"]] == ["601801.SH", "601802.SH"]
        assert asc["sort_by"] == "dividend_yield"

        implied = ms.scan_market(db, top=10, sort_by="hd_score")
        assert implied["filters"]["high_dividend"] is True
        assert [i["symbol"] for i in implied["items"]] == ["601802.SH", "601801.SH"]
        assert any("隐含「仅高股息」过滤" in n for n in implied["notes"])
    finally:
        db.close()


def test_high_dividend_uses_snapshot_price_not_intraday_overlay():
    """判据锚定快照价：行情覆盖后展示价变了，三年均息与命中结论都不翻转。"""
    db = _hd_db(("601901.SH", _hd_payload(dy=4.5)), profiles={"601901.SH": _prof(dps3=0.5)})
    rows, _ = ms.load_covered(db)
    for r in rows:
        r["_snap"] = {
            "price": 10.0,
            "dividend_yield": r.get("dividend_yield"),
            "pe_ttm": r.get("pe_ttm"),
            "pb": r.get("pb"),
            "market_cap": r.get("market_cap"),
        }
        r["price"] = 20.0  # 日内翻倍：展示价 20，快照价 10
        r["dividend_yield"] = 2.25  # 行情覆盖同步缩放展示股息率（2.25% 已跌破 3.5% 线）
    try:
        it = ms._base_items(rows)[0]
        assert it["price"] == 20.0  # 展示值是覆盖后的
        assert it["yield_3y_avg"] == pytest.approx(5.0)  # 判据按快照价 10 → 0.5/10
        assert it["high_dividend"] is True
        # 台账把两个口径**分开**吐：判据 4.5%（快照） vs 展示 2.25%（行情后）——
        # 这正是线上「展示股息率 <3.5% 却在池内」那几只的成因，不是过滤失效。
        assert it["hd_metrics"]["yield_used_pct"] == pytest.approx(4.5)
        assert it["hd_metrics"]["yield_display_pct"] == pytest.approx(2.25)
        assert it["hd_metrics"]["yield_basis"] == "ttm"
    finally:
        db.close()


def test_high_dividend_stale_profile_disqualified():
    """判据③ 档案新鲜度：分红记录停更 → 不入选，且连续性不再标 ✓（金科股份型）。

    为什么单列一条：这是首版上线验收后才补的补丁（2026-09-30）。停更标的的展示
    股息率是「股价崩塌放大了冻结的历史分红」，实测能顶到名单最前（金科股份
    latest_fy=2020 却显示 36.58%、正邦科技 24.14%、万科A 17.26%）—— 与「防御型」
    立意正相反，必须由回归测试钉住，否则以后一次「放宽」就会放回来。
    """
    req = ms.hd_required_fy()
    db = _hd_db(
        # 展示息/支付率/市值/连续 10 年全部达标，唯独档案停在 req-4 年 → 出局
        ("602001.SH", _hd_payload(dy=4.5)),
        # 档案恰好达标 → 命中（证明闸门是「≥」而非「>」）
        ("602002.SH", _hd_payload(dy=4.5)),
        profiles={
            "602001.SH": _prof(years=10, latest_fy=req - 4),
            "602002.SH": _prof(years=10, latest_fy=req),
        },
    )
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        assert [i["symbol"] for i in data["items"]] == ["602002.SH"]
        stale = _hd_detail_of(db, "602001.SH")
        assert any(f"档案停更FY{req - 4}✗" in d for d in stale), stale
        # 过期口径下累积的连续年限不可采信，否则留痕自相矛盾
        assert any("连续分红不采信" in d for d in stale), stale
        assert not any("连续分红10年✓" in d for d in stale), stale
        assert any(f"档案FY{req}✓" in d for d in _hd_detail_of(db, "602002.SH"))
    finally:
        db.close()


def test_high_dividend_missing_profile_fy_disqualified():
    """档案行存在但缺 latest_fy（残缺/旧库）→ 按缺数据不入选，不得默认通过。"""
    from app.models.dividend_profile import StockDividendProfile

    db = _hd_db(("602101.SH", _hd_payload(dy=4.5)), profiles={"602101.SH": _prof()})
    try:
        r = (
            db.query(StockDividendProfile)
            .filter(StockDividendProfile.symbol == "602101.SH")
            .one()
        )
        r.latest_fy = None
        db.commit()
        assert ms.scan_market(db, top=10, high_dividend=True)["matched"] == 0
        assert any("档案年度缺" in d for d in _hd_detail_of(db, "602101.SH"))
    finally:
        db.close()


def test_hd_required_fy_rolls_with_date():
    """新鲜度阈值随日期滚动：5 月起要求上一完整年度，1~4 月放宽一年。

    为什么不写死年份：年报与分红方案集中在 3~6 月披露。若一律要求 today.year-1，
    则每年 1~4 月整张名单会被误判「档案过期」而清空（FY N 的方案尚未披露）。
    """
    from datetime import date as _date

    assert ms.HD_PROFILE_DISCLOSURE_MONTH == 5
    assert ms.hd_required_fy(_date(2026, 9, 30)) == 2025  # 披露完毕 → 要上一年度
    assert ms.hd_required_fy(_date(2026, 5, 1)) == 2025  # 边界月（含 5 月）
    assert ms.hd_required_fy(_date(2026, 4, 30)) == 2024  # 披露窗口内 → 放宽一年
    assert ms.hd_required_fy(_date(2027, 1, 15)) == 2025  # 年初不得误清空
    # 不传参 = 今天，结论须与显式传今天一致
    assert ms.hd_required_fy() == ms.hd_required_fy(_date.today())


def test_high_dividend_metrics_ledger_exposes_criterion_inputs():
    """hd_metrics 台账：六条判据的实际取值结构化透出，**落选票也带**。

    为什么必须单测：判据⑤⑥ 的输入（偿债分 / PE·PB 历史分位 / 支付率）原先只存在
    于中文留痕文案里，接口不给结构化值；外部想复核这份名单，结构化的「分位」只剩
    列表里那一列（= ``market_pct`` 横截面综合分分位），于是把「估值分位」误算成
    综合分分位。2026-09-30 实测：体检表报「个股估值分位 ≥95% 共 44 只」，真身是
    ``market_pct ≥95`` 的 44 只，而判据⑥的取值池内上限只有 94（≥95 的全被熔断在
    池外）。台账是这份名单唯一能自证的途径，故用回归测试钉住字段与口径。
    """
    db = _hd_db(
        ("603001.SH", _hd_payload(dy=4.5, solv=52.0, payout=55.0)),
        # 池外票：估值分位 97 → 熔断。台账要能直接读出是 crowd_pct 挡的。
        ("603002.SH", _hd_payload(dy=4.5, pe_pct=97.0)),
        profiles={f"60300{i}.SH": _prof() for i in (1, 2)},
    )
    # 注入 OCF/净利 0.825 → 覆盖 = 0.825 ÷ 0.55(支付率小数口径) = 1.5
    _r = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == "603001.SH").one()
    _p = json.loads(_r.payload)
    _p["modules"]["cashflow"] = {"indicators": [{"name": "经营现金流/净利润", "value": 0.825}]}
    _r.payload = json.dumps(_p, ensure_ascii=False)
    db.commit()
    try:
        data = ms.scan_market(db, top=10, high_dividend=True)
        hit = {i["symbol"]: i for i in data["items"]}["603001.SH"]
        m = hit["hd_metrics"]
        assert m["solvency"] == pytest.approx(52.0)
        assert m["payout_pct"] == pytest.approx(55.0)
        assert m["ocf_div_cover"] == pytest.approx(1.5)
        assert m["crowd_pct"] == pytest.approx(45.0)  # max(PE 45, PB 40)
        assert m["pe_percentile"] == pytest.approx(45.0)
        assert m["pb_percentile"] == pytest.approx(40.0)
        assert m["yield_basis"] == "ttm"
        assert m["yield_used_pct"] == pytest.approx(4.5)
        assert m["mcap_yi"] == pytest.approx(200.0)
        assert m["div_latest_fy"] == ms.hd_required_fy()
        # 落选票同样带台账：读 crowd_pct 即知熔断原因，不必解析中文留痕
        out = [
            i
            for i in ms._base_items(ms.load_covered(db)[0])
            if i["symbol"] == "603002.SH"
        ][0]
        assert out["high_dividend"] is False
        assert out["hd_metrics"]["crowd_pct"] == pytest.approx(97.0)
        assert any("估值拥挤97%分位✗" in d for d in out["hd_detail"])
    finally:
        db.close()


def test_high_dividend_default_sort_is_yield_desc_not_score():
    """判据排序口径：不指定排序时按**股息率降序**，与 hd_score 序刻意区分开。

    为什么要区分：默认若与评分序相同（或共用一维），这条断言就退化成摆设。
    本例构造出**两个序不同**的池（三只全部过 v5 ⑦ 的 4% 下限）：
      股息率降序 = X(6.0) → Z(4.2) → Y(4.0)
      评分降序   = Y（偿债 95 拔高）→ Z → X（支付率 85 离 60 最远）
    用户看到的必须是前者。
    """
    db = _hd_db(
        ("603101.SH", _hd_payload(dy=6.0, solv=40.0, payout=85.0)),
        ("603102.SH", _hd_payload(dy=4.0, solv=95.0, payout=60.0)),
        ("603103.SH", _hd_payload(dy=4.2, solv=40.0, payout=60.0)),
        profiles={f"60310{i}.SH": _prof() for i in (1, 2, 3)},
    )
    try:
        default = ms.scan_market(db, top=10, high_dividend=True)
        assert default["sort_by"] == "dividend_yield"
        assert [i["symbol"] for i in default["items"]] == [
            "603101.SH",
            "603103.SH",
            "603102.SH",
        ]
        by_score = ms.scan_market(db, top=10, high_dividend=True, sort_by="hd_score")
        assert [i["symbol"] for i in by_score["items"]] == [
            "603102.SH",
            "603103.SH",
            "603101.SH",
        ]
        assert by_score["items"][0]["hd_score"] > by_score["items"][1]["hd_score"]
    finally:
        db.close()
