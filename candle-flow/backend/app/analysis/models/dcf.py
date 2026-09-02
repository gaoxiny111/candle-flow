from __future__ import annotations


class DCFModel:
    """三阶段自由现金流折现。"""

    def __init__(
        self,
        high_growth_years: int = 5,
        transition_years: int = 5,
        terminal_growth: float = 0.02,
        wacc: float = 0.10,
    ):
        self.high_growth_years = high_growth_years
        self.transition_years = transition_years
        self.terminal_growth = terminal_growth
        self.wacc = wacc

    def value(
        self,
        base_fcf: float,
        high_growth_rate: float,
        transition_growth_rate: float,
        shares_outstanding: float,
    ) -> dict:
        if base_fcf <= 0 or shares_outstanding <= 0:
            return {
                "intrinsic_value_per_share": None,
                "total_pv": None,
                "note": "基期自由现金流或股本无效，跳过 DCF",
                "is_reliable": False,
            }

        # 限制增长率，避免乐观外推把估值打爆
        high_growth_rate = min(max(float(high_growth_rate), 0.0), 0.15)
        transition_growth_rate = min(max(float(transition_growth_rate), 0.0), 0.10)

        if self.wacc <= self.terminal_growth:
            return {
                "intrinsic_value_per_share": None,
                "note": "WACC 需大于永续增长率",
                "is_reliable": False,
            }

        total_pv = 0.0
        fcf = float(base_fcf)
        detail: list[dict] = []

        for year in range(1, self.high_growth_years + 1):
            fcf *= 1 + high_growth_rate
            pv = fcf / (1 + self.wacc) ** year
            total_pv += pv
            detail.append({"year": year, "fcf": fcf, "pv": pv, "phase": "高速增长"})

        for year in range(1, self.transition_years + 1):
            progress = year / self.transition_years
            growth = transition_growth_rate * (1 - progress) + self.terminal_growth * progress
            fcf *= 1 + growth
            actual_year = self.high_growth_years + year
            pv = fcf / (1 + self.wacc) ** actual_year
            total_pv += pv
            detail.append({"year": actual_year, "fcf": fcf, "pv": pv, "phase": "过渡"})

        terminal_year = self.high_growth_years + self.transition_years
        terminal_value = fcf * (1 + self.terminal_growth) / (self.wacc - self.terminal_growth)
        terminal_pv = terminal_value / (1 + self.wacc) ** terminal_year
        total_pv += terminal_pv

        intrinsic = total_pv / shares_outstanding
        return {
            "intrinsic_value_per_share": round(intrinsic, 2),
            "total_pv": round(total_pv, 0),
            "terminal_value": round(terminal_value, 0),
            "terminal_pv_ratio": round(terminal_pv / total_pv * 100, 1) if total_pv else None,
            "base_fcf": round(float(base_fcf), 2),
            "shares": float(shares_outstanding),
            "detail": detail[:8],
            "assumptions": {
                "wacc": self.wacc,
                "high_growth": high_growth_rate,
                "transition_growth": transition_growth_rate,
                "terminal_growth": self.terminal_growth,
            },
        }
