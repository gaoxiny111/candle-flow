from __future__ import annotations


def dupont_decompose(net_margin: float, asset_turnover: float, equity_multiplier: float) -> dict:
    """杜邦三因子分解 ROE = 净利率 × 总资产周转 × 权益乘数。"""
    roe = net_margin * asset_turnover * equity_multiplier * 100
    return {
        "roe_pct": round(roe, 2),
        "net_margin": round(net_margin, 4),
        "asset_turnover": round(asset_turnover, 4),
        "equity_multiplier": round(equity_multiplier, 4),
    }
