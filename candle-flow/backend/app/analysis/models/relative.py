from __future__ import annotations

import pandas as pd


class RelativeValuation:
    """PE/PB/PS/EV 历史分位 + 行业对比。"""

    def analyze(
        self,
        current: dict,
        history: pd.DataFrame | None = None,
        industry: pd.DataFrame | None = None,
    ) -> dict:
        results: dict = {}
        for metric in ("PE_TTM", "PB", "PS", "EV_EBITDA"):
            cur_val = current.get(metric)
            if cur_val is None:
                continue
            cur_val = float(cur_val)
            percentile = None
            if history is not None and metric in history.columns:
                hist_series = history[metric].dropna()
                if len(hist_series):
                    percentile = round(float((hist_series < cur_val).mean() * 100), 1)

            ind_median = None
            ind_premium = None
            if industry is not None and metric in industry.columns:
                ind_median = float(industry[metric].median())
                if ind_median:
                    ind_premium = round((cur_val / ind_median - 1) * 100, 1)

            if percentile is not None:
                if percentile < 25:
                    signal = "低估"
                elif percentile > 75:
                    signal = "高估"
                else:
                    signal = "合理"
            else:
                signal = "—"

            results[metric] = {
                "current": cur_val,
                "percentile_5y": percentile,
                "industry_median": round(ind_median, 2) if ind_median else None,
                "industry_premium_pct": ind_premium,
                "signal": signal,
            }

        pe = current.get("PE_TTM")
        growth = current.get("profit_growth_rate")
        if pe and growth and float(growth) > 0:
            peg = float(pe) / float(growth)
            results["PEG"] = {
                "value": round(peg, 2),
                "signal": "低估" if peg < 0.8 else ("高估" if peg > 1.5 else "合理"),
            }

        return results
