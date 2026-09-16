"""
股利贴现模型（戈登固定增长模型）：V = D0·(1+g) / (r−g)。

适用于红利/高股息资产的内在价值锚（替代对分红不敏感的 FCFF/DCF）：
- D0：最近一个完整会计年度每股分红（中期+末期）
- g：长期股利永续增速
- r：股权要求回报率
"""

from __future__ import annotations

from typing import Any


class DDMModel:
    def __init__(self, required_return: float, terminal_growth: float):
        self.required_return = float(required_return)
        self.terminal_growth = float(terminal_growth)

    def value(self, d0: float) -> dict[str, Any]:
        r, g = self.required_return, self.terminal_growth
        if d0 is None or float(d0) <= 0 or r <= g:
            return {
                "intrinsic_value_per_share": None,
                "d0": d0,
                "growth": g,
                "required_return": r,
            }
        intrinsic = float(d0) * (1 + g) / (r - g)
        return {
            "intrinsic_value_per_share": round(intrinsic, 2),
            "d0": round(float(d0), 4),
            "growth": g,
            "required_return": r,
            "formula": "D0(1+g)/(r-g)",
        }
