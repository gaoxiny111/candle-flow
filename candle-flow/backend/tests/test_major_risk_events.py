"""重大风险事件：公告标题关键词扫描。"""

from app.services.major_risk_events import (
    COMPLIANCE_VETO_MESSAGE,
    clear_major_risk_cache,
    detect_major_risk_events,
    scan_notice_titles,
)


def test_scan_notice_titles_hits_pre_reorg_audit_freeze():
    notices = [
        {
            "title": "ST龙元:龙元建设关于法院决定对公司进行预重整的公告",
            "notice_date": "2026-09-10",
            "url": "",
        },
        {
            "title": "龙元建设董事会及审计委员会关于带有持续经营重大不确定性段落的无保留意见审计报告涉及事项的专项说明",
            "notice_date": "2026-04-30",
            "url": "",
        },
        {
            "title": "龙元建设关于控股股东部分股份被司法冻结的公告",
            "notice_date": "2026-06-09",
            "url": "",
        },
        {
            "title": "某公司关于召开董事会的通知",
            "notice_date": "2026-01-01",
            "url": "",
        },
    ]
    hits = scan_notice_titles(notices)
    ids = {h.rule_id for h in hits}
    assert "pre_reorg" in ids
    assert "audit_nonstd" in ids
    assert "share_freeze" in ids
    assert len(hits) == 3


def test_detect_major_risk_events_fatal_from_notices(monkeypatch):
    clear_major_risk_cache()

    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [
            {
                "title": "ST龙元:关于法院决定对公司进行预重整的公告",
                "notice_date": "2026-09-10",
                "url": "https://example.com",
            }
        ],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: None,
    )

    result = detect_major_risk_events("600491.SH")
    assert result["fatal"] is True
    assert result["event_count"] >= 1
    assert result["message"] == COMPLIANCE_VETO_MESSAGE
    assert any(e["rule_id"] == "pre_reorg" for e in result["events"])


def test_detect_major_risk_events_pledge_threshold(monkeypatch):
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: 0.9978,
    )
    result = detect_major_risk_events("600491.SH")
    assert result["fatal"] is True
    assert any(e["rule_id"] == "pledge_crisis" for e in result["events"])


def test_compliance_veto_forces_rating_e(monkeypatch):
    """命中重大风险时，无视财务打分强制 E + compliance_veto。"""
    import pandas as pd

    from app.analysis.engine import FundamentalEngine

    engine = FundamentalEngine()
    dates = ["20231231", "20241231", "20251231"]
    df = pd.DataFrame(
        {
            "revenue": [1e9, 1.1e9, 1.2e9],
            "net_profit": [1e8, 1.1e8, 1.2e8],
            "operating_cashflow": [1.2e8, 1.3e8, 1.4e8],
            "equity": [2e9, 2.1e9, 2.2e9],
            "total_assets": [3e9, 3.1e9, 3.2e9],
            "total_liabilities": [1e9, 1e9, 1e9],
            "goodwill": [0, 0, 0],
            "accounts_receivable": [1e8, 1e8, 1e8],
            "monetary_funds": [5e8, 5e8, 5e8],
            "short_term_borrowings": [0, 0, 0],
            "roe": [12.0, 13.0, 14.0],
            "roic": [10.0, 11.0, 12.0],
            "gross_margin": [30.0, 31.0, 32.0],
        },
        index=dates,
    )

    def fake_build(*_a, **_k):
        return df, {
            "name": "优质测试",
            "industry": "软件",
            "report_dates": dates,
            "annual_dates": dates,
            "latest_report": dates[-1],
            "latest_roe": 14.0,
            "debt_ratio": 0.33,
            "revenue_yoy": 0.09,
            "profit_yoy": 0.09,
            "ocf_per_share": 0.5,
            "balance_sheet": {},
        }

    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", fake_build)
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: [
            {
                "symbol": "600491.SH",
                "name": "优质测试",
                "price": 10.0,
                "pe_ttm": 8.0,
                "pb": 1.2,
                "pe_percentile": 20.0,
                "pb_percentile": 20.0,
                "market_cap": 1e10,
                "dividend_yield": 2.0,
                "total_shares": 1e9,
            }
        ],
    )
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "600491.SH",
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "peer_count": 0,
            "insufficient_sample": True,
        },
    )
    monkeypatch.setattr(
        "app.analysis.engine.detect_major_risk_events",
        lambda *_a, **_k: {
            "fatal": True,
            "events": [
                {
                    "rule_id": "pre_reorg",
                    "label": "法院预重整/破产重整",
                    "keyword": "预重整",
                    "title": "关于法院决定对公司进行预重整的公告",
                    "notice_date": "2026-09-10",
                    "url": "",
                    "source": "notice",
                }
            ],
            "event_count": 1,
            "message": COMPLIANCE_VETO_MESSAGE,
            "audit_opinion_hint": None,
            "labels": ["法院预重整/破产重整"],
            "pledge_ratio": None,
        },
    )

    report = engine.run_full_analysis("600491.SH", db=None)
    assert report["compliance_veto"] is True
    assert report["final_rating"] == "E"
    assert report["composite_score"] <= 25
    assert report["major_risks"]["fatal"] is True
