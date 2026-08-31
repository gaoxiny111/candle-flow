from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import symbols
from app.services import valuation as valuation_mod
from app.services.valuation import get_valuations, percentile_rank, _parse_baidu_chart, _parse_tencent


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(symbols.router, prefix="/api/v1")
    return TestClient(app)


TENCENT_SAMPLE = (
    'v_sh600519="1~贵州茅台~600519~1412.50~1400.00~1401.00~100~50~50~'
    + "~".join(["0"] * 21)
    + '~20260828161500~12.50~1.23~1415~1390~x~100~1~0.1~21.40~~1415~1390~0.5~17780.00~17780.00~8.60~1~1~1~-1";'
    'v_sz000001="51~平安银行~000001~11.20~11.26~11.10~100~50~50~'
    + "~".join(["0"] * 21)
    + '~20260828161424~-0.06~-0.50~11.3~11.0~x~100~1~0.1~5.10~~11.3~11.0~1.0~2260.00~2260.00~0.62~1~1~1~-1";'
)


def test_percentile_rank():
    series = [float(i) for i in range(1, 21)]
    assert percentile_rank(5, series) == 22.5
    assert percentile_rank(1, series) == 2.5
    assert percentile_rank(20, series) == 97.5
    assert percentile_rank(-1, series) is None
    assert percentile_rank(5, [1.0, 2.0]) is None


def test_parse_baidu_chart_b_share():
    payload = {
        "Result": [
            {
                "DisplayData": {
                    "resultData": {
                        "tplData": {
                            "result": {
                                "chartInfo": [
                                    {
                                        "body": [["2018-01-02", "12.0"]] * 15
                                        + [["2024-06-01", "9.0"]] * 10
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        ]
    }
    series = _parse_baidu_chart(payload)
    assert len(series) >= 20
    assert series[-1] == 9.0


def test_b_share_uses_baidu_history(monkeypatch):
    valuation_mod.clear_cache()
    eastmoney_calls: list[str] = []
    monkeypatch.setattr(
        valuation_mod,
        "_fetch_eastmoney_history",
        lambda code: eastmoney_calls.append(code) or ([], []),
    )
    monkeypatch.setattr(
        valuation_mod,
        "_fetch_baidu_series",
        lambda code, indicator: [8.0] * 20 + [12.0] * 20 if "市盈" in indicator else [1.0] * 20 + [2.0] * 20,
    )
    pe_s, pb_s = valuation_mod._fetch_valuation_history("900948.SH")
    assert eastmoney_calls == []
    assert len(pe_s) == 40
    assert len(pb_s) == 40
    assert percentile_rank(8.99, pe_s) is not None


def test_parse_tencent_sample():
    rows = _parse_tencent(TENCENT_SAMPLE, {"600519.SH", "000001.SZ"})
    maotai = rows["600519.SH"]
    assert maotai["name"] == "贵州茅台"
    assert maotai["price"] == 1412.5
    assert maotai["change_pct"] == 1.23
    assert maotai["pe_ttm"] == 21.4
    assert maotai["pb"] == 8.6
    assert maotai["market_cap"] == 17780.00 * 1e8
    assert rows["000001.SZ"]["pe_ttm"] == 5.1


def test_get_valuations_uses_tencent(monkeypatch):
    valuation_mod.clear_cache()

    class Resp:
        content = TENCENT_SAMPLE.encode("gbk")

    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    monkeypatch.setattr(valuation_mod, "_histories_for", lambda symbols, now, db=None: ({}, []))
    rows = get_valuations(["600519.SH", "000001.SZ", "RB0.FUT"])
    assert [r["symbol"] for r in rows] == ["600519.SH", "000001.SZ", "RB0.FUT"]
    assert rows[0]["pe_ttm"] == 21.4
    assert rows[1]["pb"] == 0.62
    assert rows[2]["pe_ttm"] is None
    assert rows[2]["price"] is None


def test_valuations_api_open():
    res = _client().get("/api/v1/symbols/valuations", params={"symbols": "600519.SH"})
    assert res.status_code != 403
    assert res.status_code != 401


def test_valuations_cache(monkeypatch):
    valuation_mod.clear_cache()
    calls = {"n": 0}

    def fake_fetch(symbols):
        calls["n"] += 1
        return {
            s: {
                "symbol": s,
                "name": "",
                "price": 1.0,
                "change_pct": 0.0,
                "pe_ttm": 10.0,
                "pe_dynamic": 10.0,
                "pb": 1.0,
                "market_cap": 1e9,
            }
            for s in symbols
        }

    monkeypatch.setattr(valuation_mod, "_fetch_quotes", fake_fetch)
    monkeypatch.setattr(
        valuation_mod,
        "_histories_for",
        lambda symbols, now, db=None: (
            {s: ([8.0] * 20 + [12.0] * 20, [0.8] * 20 + [1.2] * 20) for s in symbols},
            [],
        ),
    )
    first = get_valuations(["600519.SH"], now=1000.0)
    second = get_valuations(["600519.SH"], now=1100.0)
    assert first[0]["pe_ttm"] == 10.0
    assert first[0]["pe_percentile"] == 50.0
    assert second[0]["pe_ttm"] == 10.0
    assert calls["n"] == 1
    get_valuations(["600519.SH"], now=1000.0 + valuation_mod.CACHE_TTL_SEC + 1)
    assert calls["n"] == 2


def test_skip_invalid_and_dedupe(monkeypatch):
    valuation_mod.clear_cache()
    monkeypatch.setattr(valuation_mod, "_fetch_quotes", lambda symbols: {})
    monkeypatch.setattr(valuation_mod, "_histories_for", lambda symbols, now, db=None: ({}, []))
    rows = get_valuations(["not-a-stock", "600519.SH", "600519.sh", ""])
    assert [r["symbol"] for r in rows] == ["600519.SH"]


def _memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base
    import app.models  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_quotes_return_before_history(monkeypatch):
    valuation_mod.clear_cache()
    scheduled: list[str] = []
    monkeypatch.setattr(
        valuation_mod,
        "_fetch_quotes",
        lambda symbols: {
            "600519.SH": {
                "symbol": "600519.SH",
                "name": "贵州茅台",
                "price": 1412.5,
                "change_pct": 1.2,
                "pe_ttm": 21.4,
                "pe_dynamic": 21.4,
                "pb": 8.6,
                "market_cap": 1e12,
            }
        },
    )
    monkeypatch.setattr(valuation_mod, "_schedule_history", lambda symbols: scheduled.extend(symbols))
    db = _memory_db()
    rows = get_valuations(["600519.SH"], now=1_700_000_000.0, db=db)
    assert rows[0]["pe_ttm"] == 21.4
    assert rows[0]["pe_percentile"] is None
    assert rows[0]["percentiles_pending"] is True
    assert scheduled == ["600519.SH"]


def test_sqlite_history_used_without_network(monkeypatch):
    valuation_mod.clear_cache()
    db = _memory_db()
    valuation_mod.upsert_history(db, "600519.SH", [8.0] * 20 + [12.0] * 20, [0.8] * 20 + [1.2] * 20)
    monkeypatch.setattr(
        valuation_mod,
        "_fetch_quotes",
        lambda symbols: {
            "600519.SH": {
                "symbol": "600519.SH",
                "name": "贵州茅台",
                "price": 10.0,
                "change_pct": 0.0,
                "pe_ttm": 10.0,
                "pe_dynamic": 10.0,
                "pb": 1.0,
                "market_cap": 1e9,
            }
        },
    )
    monkeypatch.setattr(
        valuation_mod,
        "_schedule_history",
        lambda symbols: (_ for _ in ()).throw(AssertionError("should use sqlite")),
    )
    rows = get_valuations(["600519.SH"], now=1_700_000_000.0, db=db)
    assert rows[0]["pe_percentile"] == 50.0
    assert rows[0]["percentiles_pending"] is False
