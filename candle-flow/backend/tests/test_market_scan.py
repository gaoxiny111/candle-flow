"""全市场排序读层回归测试。

背景：`factor_snapshots` 此前**只写不读**（除自身模块外零引用），
即「全市场基本面打分排序」一直缺输出层。本轮补上读层，需保证：

1. 排序沿用既有 `composite_score`，不新造第二套权重（同一判定单一来源）；
2. 分位是展示列、不参与排序；缺失值不得被赋 50 分中性值；
3. 覆盖率必须自证（已覆盖 / SH·SZ 总数），不得默认宣称已覆盖全市场。
"""

from __future__ import annotations

import json

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
def _reset_overlay_caches():
    """叠加层有两级进程内缓存（请求级 + 单票级），测试间必须隔离。"""
    ms._overlay_cache.update({"ts": 0.0, "key": None, "payload": None})
    ms._overlay_item_cache.clear()
    ms._reso_cache.update({"ts": 0.0, "items": None, "coverage": None, "stats": None})
    yield
    ms._overlay_cache.update({"ts": 0.0, "key": None, "payload": None})
    ms._overlay_item_cache.clear()
    ms._reso_cache.update({"ts": 0.0, "items": None, "coverage": None, "stats": None})


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
                payload=_payload(
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
    for sym in ("600000.SH", "000001.SZ", "300750.SZ"):
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
    """已有快照但 payload 缺必需字段时，非 force 增量构建也必须重建。

    否则给 run_full_analysis 新增字段（如 profit_yoy）在存量行上永不回填。
    判定看键是否存在：值为 null 的合法缺失不应触发反复重建。
    """
    from app.services import factor_db as mod

    engine = create_engine(
        f"sqlite:///{tmp_path / 'guard.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    setup = Session()
    for sym in ("600000.SH", "000001.SZ", "300750.SZ"):
        setup.add(StockInfo(symbol=sym, code=sym[:6], name="测试", market=sym[-2:]))
    # 600000：旧 payload 缺 profit_yoy → 应纳入重建
    setup.add(
        FactorSnapshot(
            symbol="600000.SH",
            payload=json.dumps({"composite_score": 50.0}),
            composite_score=50.0,
        )
    )
    # 000001：已含 profit_yoy 且值为 null（合法空值）→ 不应重建
    setup.add(
        FactorSnapshot(
            symbol="000001.SZ",
            payload=json.dumps({"composite_score": 51.0, "profit_yoy": None}),
            composite_score=51.0,
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
    # 600000（缺键）+ 300750（无快照）→ 2 只；000001 已有键 → 跳过
    assert stats["built"] == 2
    assert sorted(built_symbols) == ["300750.SZ", "600000.SH"]
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
                            lambda sym, name, score, pe, py: {"symbol": sym, "name": name, **tech[sym]})
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

    def fake_one(sym, name, score, pe, py):
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

    def flaky(sym, name, score, pe, py):
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
            lambda sym, name, score, pe, py: {"symbol": sym, "name": name, "kline_bars": 0,
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
    row = ms._overlay_one("600999.SH", "无K线", 80.0, 10.0, None)
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
        lambda self, job: (
            {"pattern_name": "启明星", "pattern_score": 88.0,
             "confluence_effective": 3.2, "combined_score": 107.2}, "hit"),
    )
    assert ms._overlay_one("600001.SH", "甲", 88.0, 10.0, 20.0)["tech_score"] == 100

    # 无达标形态共振 → None（不可评估），绝不赋 0 或中性分
    monkeypatch.setattr(MarketConfluenceService, "_scan_job", lambda self, job: (None, "ok"))
    assert ms._overlay_one("600002.SH", "乙", 88.0, 10.0, 20.0)["tech_score"] is None

    # 未达 100 时原样保留（不重标定量纲）
    assert ms._tech_score(83.4) == 83 and ms._tech_score(None) is None


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
            lambda sym, name, score, pe, py: {
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


def _fake_overlay_one(sym, name, score, pe, py):
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

