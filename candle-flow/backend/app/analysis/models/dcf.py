from __future__ import annotations


class DCFModel:
    """三阶段自由现金流折现，支持三情景（乐观/中性/悲观）。"""

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
        return self._calc(base_fcf, high_growth_rate, transition_growth_rate, shares_outstanding, scenario="neutral")

    def value_three_scenarios(
        self,
        base_fcf: float,
        high_growth_rate: float,
        transition_growth_rate: float,
        shares_outstanding: float,
        *,
        optimistic_wacc_adj: float = -0.01,
        optimistic_growth_adj: float = 0.02,
        pessimistic_wacc_adj: float = 0.015,
        pessimistic_growth_adj: float = -0.02,
    ) -> dict:
        """三情景 DCF：返回乐观/中性/悲观结果，取中性为基准值。"""
        neutral = self._calc(base_fcf, high_growth_rate, transition_growth_rate, shares_outstanding, "neutral")
        if not neutral.get("intrinsic_value_per_share"):
            return neutral

        # 乐观：WACC 下调 + 增速上调
        opt_wacc = max(self.terminal_growth + 0.01, self.wacc + optimistic_wacc_adj)
        opt_growth = min(0.18, high_growth_rate + optimistic_growth_adj)
        opt_trans = min(0.12, transition_growth_rate + optimistic_growth_adj * 0.5)
        optimistic = self._calc(base_fcf, opt_growth, opt_trans, shares_outstanding, "optimistic", wacc_override=opt_wacc)

        # 悲观：WACC 上调 + 增速下调
        pes_wacc = self.wacc + pessimistic_wacc_adj
        pes_growth = max(0.0, high_growth_rate + pessimistic_growth_adj)
        pes_trans = max(0.0, transition_growth_rate + pessimistic_growth_adj * 0.5)
        pessimistic = self._calc(base_fcf, pes_growth, pes_trans, shares_outstanding, "pessimistic", wacc_override=pes_wacc)

        neutral_iv = neutral.get("intrinsic_value_per_share") or 0
        opt_iv = optimistic.get("intrinsic_value_per_share") or 0
        pes_iv = pessimistic.get("intrinsic_value_per_share") or 0

        return {
            **neutral,
            "scenario_optimistic": opt_iv,
            "scenario_neutral": neutral_iv,
            "scenario_pessimistic": pes_iv,
            "three_scenario": True,
        }

    def _calc(
        self,
        base_fcf: float,
        high_growth_rate: float,
        transition_growth_rate: float,
        shares_outstanding: float,
        scenario: str = "neutral",
        *,
        wacc_override: float | None = None,
    ) -> dict:
        wacc = wacc_override if wacc_override is not None else self.wacc
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

        if wacc <= self.terminal_growth:
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
            pv = fcf / (1 + wacc) ** year
            total_pv += pv
            detail.append({"year": year, "fcf": fcf, "pv": pv, "phase": "高速增长"})

        for year in range(1, self.transition_years + 1):
            progress = year / self.transition_years
            growth = transition_growth_rate * (1 - progress) + self.terminal_growth * progress
            fcf *= 1 + growth
            actual_year = self.high_growth_years + year
            pv = fcf / (1 + wacc) ** actual_year
            total_pv += pv
            detail.append({"year": actual_year, "fcf": fcf, "pv": pv, "phase": "过渡"})

        terminal_year = self.high_growth_years + self.transition_years
        terminal_value = fcf * (1 + self.terminal_growth) / (wacc - self.terminal_growth)
        terminal_pv = terminal_value / (1 + wacc) ** terminal_year
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
                "wacc": wacc,
                "high_growth": high_growth_rate,
                "transition_growth": transition_growth_rate,
                "terminal_growth": self.terminal_growth,
                "scenario": scenario,
            },
        }
