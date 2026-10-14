"""常态 EPS / PE：剔爆款峰值，并列 2026E 代理区间。"""
from __future__ import annotations

import pandas as pd

from app.analysis.engine import FundamentalEngine


def test_estimate_eps_normal_excludes_blockbuster_peak():
    fin = pd.DataFrame(
        [
            {"report_date": "20231231", "eps": 0.12},
            {"report_date": "20241231", "eps": 0.15},
            {"report_date": "20251231", "eps": 0.57},  # 爆款年
        ]
    )
    meta = {"profit_yoy_extreme": True, "eps": 0.57}
    out = FundamentalEngine._estimate_eps_normal(fin, meta)
    assert out is not None
    assert out["eps_blockbuster_excluded"] is True
    # 常态应远低于峰值 0.57（约 0.25–0.32 量级）
    assert out["eps_normal"] < 0.40
    assert out["eps_normal_lo"] < out["eps_normal_hi"]
    assert 0.20 <= out["eps_normal_lo"] <= 0.30
    assert 0.28 <= out["eps_normal_hi"] <= 0.35


def test_sanitize_market_pe_adds_normal_band():
    fin = pd.DataFrame(
        [
            {"report_date": "20231231", "eps": 0.12},
            {"report_date": "20241231", "eps": 0.15},
            {"report_date": "20251231", "eps": 0.57},
        ]
    )
    market = {"price": 11.65, "pe_ttm": 20.5}
    meta = {"profit_yoy_extreme": True, "latest_report": "20251231", "eps": 0.57}
    FundamentalEngine._sanitize_market_pe(market, meta, fin)
    assert market["pe_ttm"] == 20.5  # 年报口径保留
    assert market["pe_normal"] is not None
    assert market["pe_normal"] > 30  # 常态 PE 明显高于 20.5
    assert market["pe_normal_lo"] is not None
    assert market["pe_normal_hi"] is not None
    assert "常态" in (market.get("pe_ttm_note") or "")
