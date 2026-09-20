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


def _payload(name: str, industry: str, *, mcap: float, dims: dict, rating: str = "B") -> str:
    return json.dumps(
        {
            "name": name,
            "industry": industry,
            "final_rating": rating,
            "risk_level_label": "中",
            "market": {"market_cap": mcap, "pb": 1.5, "dividend_yield": 2.0},
            "dim_scores": dims,
        },
        ensure_ascii=False,
    )


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
