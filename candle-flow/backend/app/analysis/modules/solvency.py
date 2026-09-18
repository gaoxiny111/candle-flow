from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level
from app.analysis.config.company_profiles import (
    bank_benchmarks,
    business_model_of,
    distribution_benchmarks,
)

# 轻资产/制造类行业：资产负债率阈值更严
_TIGHT_DEBT_INDUSTRY_KEYS = ("电子", "通信", "半导体", "元件", "模组", "光模块", "消费电子", "计算机", "软件")


class SolvencyAnalyzer(BaseAnalyzer):
    """资产负债率、有息负债、流动/速动比；现金奶牛按产业链话语权修正。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        industry = str(kwargs.get("industry") or "")
        _biz_model = business_model_of(
            str(kwargs.get("name") or ""),
            str(kwargs.get("symbol") or ""),
            industry,
        )
        _is_dist = _biz_model == "distribution"
        _dist_bench: dict = distribution_benchmarks() if _is_dist else {}
        # 银行（存款类金融机构）：负债以客户存款为主，资产负债率天然 90%+，
        # 流动/速动比率无制造业含义，有息负债率因「利息支出型负债」口径
        # （短借+长借+应付债券）不含吸收存款而系统性偏低。三项均不可用，
        # 故银行改用「权益/总资产（资本缓冲）」相对同业中位评分。
        _is_bank = _biz_model == "bank"
        _bank_bench: dict = bank_benchmarks() if _is_bank else {}
        # 分销/贸易不套用「电子/轻资产」严阈值：其高负债率主要来自应付账款等
        # 经营性负债（上游账期），而非有息负债。实测同业中位 61.42%，
        # 用 35/50/60 的轻资产严阈值会把行业常态判成偿债压力大。
        tight = (not _is_dist) and any(k in industry for k in _TIGHT_DEBT_INDUSTRY_KEYS)
        bs = kwargs.get("balance_sheet") or {}
        bs_period = format_report_period(bs.get("report_date") or kwargs.get("latest_report"))

        debt_ratio = kwargs.get("debt_ratio")
        if debt_ratio is not None:
            dr = float(debt_ratio)
            _bench_dr = float(_dist_bench.get("debt_ratio_pct") or 0) or None
            _bank_eq_med = float(_bank_bench.get("equity_ratio_pct") or 0) or None
            _bank_eq = 100.0 - dr
            _bank_diff = (_bank_eq - _bank_eq_med) if _bank_eq_med else 0.0
            if _is_bank and _bank_eq_med:
                # 银行：以资本缓冲（权益/总资产）相对同业中位为锚。达到中位
                # ≈58 分（中性），缓冲每厚/薄 1pct 加减 5 分；上限 76——
                # 只看到杠杆、看不到资产质量，单靠本维度不足以给 A。
                score = max(35.0, min(76.0, 58.0 + _bank_diff * 5.0))
                level = score_to_level(score)
            elif _is_dist and _bench_dr:
                # 分销/贸易：以同业中位为锚（比率越低越好），不高于中位即中性
                _dr_ratio = dr / _bench_dr
                if _dr_ratio <= 0.85:
                    score = 75.0
                elif _dr_ratio <= 1.0:
                    score = 60.0
                elif _dr_ratio <= 1.15:
                    score = 50.0
                elif _dr_ratio <= 1.3:
                    score = 40.0
                else:
                    score = 28.0
                level = score_to_level(score)
            elif tight:
                score, level = self._score_by_range(dr, (0, 35), (35, 50), (50, 60))
            else:
                score, level = self._score_by_range(dr, (0, 40), (40, 55), (55, 70))
            if dr > 75 and not _is_bank and not (_is_dist and _bench_dr):
                score, level = 25.0, AnalysisLevel.DANGER
                warnings.append(f"资产负债率 {dr:.1f}% 偏高，偿债压力较大")
            elif _is_bank and _bank_eq_med and _bank_diff < -0.5:
                warnings.append(
                    f"资本缓冲（权益/总资产 {_bank_eq:.2f}%）低于银行同业中位 "
                    f"{_bank_eq_med:.2f}%，杠杆在业内偏高"
                )
            elif _is_dist and _bench_dr and dr > _bench_dr:
                warnings.append(
                    f"资产负债率 {dr:.1f}% 高于分销同业中位 {_bench_dr:.1f}%，"
                    f"需区分经营性负债与有息负债"
                )
            elif tight and dr >= 55:
                warnings.append(f"资产负债率 {dr:.1f}% 对{industry or '本行业'}偏高，需区分经营性负债与有息负债")

            comment_parts = []
            if kwargs.get("debt_ratio_estimated"):
                comment_parts.append("估算值，仅供参考")
            if _is_bank:
                comment_parts.append(
                    f"银行口径：负债以客户存款为主（权益/总资产 {_bank_eq:.2f}%），"
                    f"不套用制造业负债率阈值；按资本缓冲相对同业中位 "
                    f"{_bank_eq_med:.2f}% 评估"
                    if _bank_eq_med
                    else "银行口径：负债以客户存款为主，不套用制造业负债率阈值"
                )
            else:
                comment_parts.append("含应付等经营性负债；危险程度看有息负债分项")
                if _is_dist and _bench_dr:
                    comment_parts.append(
                        f"分销/贸易口径：负债以应付账款等经营性负债为主，"
                        f"按同业中位 {_bench_dr:.1f}% 相对评估"
                    )
                elif tight:
                    comment_parts.append("轻资产行业采用更严阈值")
            indicators.append(
                IndicatorResult(
                    name="资产负债率(%)",
                    value=round(dr, 2),
                    score=score,
                    level=level,
                    weight=3.0,
                    comment="；".join(comment_parts),
                    period=bs_period,
                )
            )

        ibd_ratio = kwargs.get("interest_bearing_ratio")
        if ibd_ratio is None and bs.get("interest_bearing_ratio") is not None:
            ibd_ratio = bs.get("interest_bearing_ratio")
        # 银行不评有息负债率：该口径（短借+长借+应付债券）不含吸收存款，
        # 对银行系统性偏低（20 家样本在 0.44%~16.8% 之间乱跳，与风险无关），
        # 计入反而会把招行这类银行抬到 90 分（偏宽松）。
        if ibd_ratio is not None and not _is_bank:
            ibr = float(ibd_ratio)
            # 有息负债率：优秀 <25，良好 25–40，中性 40–55
            ib_score, ib_level = self._score_by_range(ibr, (0, 25), (25, 40), (40, 55))
            if ibr > 55:
                ib_score, ib_level = 28.0, AnalysisLevel.POOR
                warnings.append(f"近似有息负债率 {ibr:.1f}% 偏高，短期偿债与利息压力需关注")
            indicators.append(
                IndicatorResult(
                    name="有息负债率(%)",
                    value=round(ibr, 2),
                    score=ib_score,
                    level=ib_level,
                    weight=2.5,
                    comment="≈有息负债/总资产；剔除经营性负债后的杠杆压力",
                    period=bs_period,
                )
            )

        op_liab = float(bs.get("operating_liabilities") or 0)
        short_b = float(
            bs.get("short_term_borrowings")
            if bs.get("short_term_borrowings") is not None
            else (kwargs.get("short_term_borrowings") or 0)
        )
        ta = float(bs.get("total_assets") or 0)
        # 产业链话语权：经营性负债主导 + 有息负债可控 + 杠杆不高
        supply_chain_power = (
            op_liab > 1e9
            and op_liab >= max(short_b, 1.0) * 1.5
            and (ibd_ratio is None or float(ibd_ratio) < 20)
            and (debt_ratio is None or float(debt_ratio) < 45)
        )
        # 周期/资源股：存货（矿石/化肥等）变现强，速动比率口径失真
        cycle_industries = ("煤炭", "焦炭", "有色", "钢铁", "化工", "航运", "港口", "开采", "化肥", "磷", "矿", "石油", "天然气", "农化", "农药")
        industry_str = str(kwargs.get("industry") or "")
        is_cycle_asset = any(k in industry_str for k in cycle_industries)
        # 有息负债率 < 30% 且经营负债主导 → 也视为产业链强势
        if not supply_chain_power and is_cycle_asset and ibd_ratio is not None:
            if float(ibd_ratio) < 30 and op_liab > 1e9:
                supply_chain_power = True
        # 周期股补充：速动比率口径对重资产资源股失真（存货变现强）
        cycle_supplement = is_cycle_asset and ibd_ratio is not None and float(ibd_ratio) < 35

        current_ratio = kwargs.get("current_ratio")
        if current_ratio is None and bs.get("current_ratio") is not None:
            current_ratio = bs.get("current_ratio")
        if current_ratio is None and not financial_data.empty and "current_ratio" in financial_data.columns:
            current_ratio = float(financial_data["current_ratio"].iloc[-1])
        # 流动比率恰为 0 只能是「科目未映射」而非真实经营状态（持续经营企业
        # 不可能没有流动资产），按缺失处理，否则会被打成 0 分最差档。
        if current_ratio is not None and float(current_ratio) <= 0:
            current_ratio = None

        # 现金奶牛：分母仅用短期借款（剔除应付/预收等经营性负债）重算流动/速动
        ca_proxy = None
        if ta > 0 and current_ratio is not None and (op_liab + short_b) > 0:
            # 反推流动资产粗值：CR × (op+short)
            ca_proxy = float(current_ratio) * (op_liab + short_b)
        adj_cr = None
        adj_qr = None
        if supply_chain_power and ca_proxy is not None and short_b > 1e6:
            adj_cr = ca_proxy / short_b
            # 速动粗估：按原速动/流动比例缩放，缺省按 0.9
            qr0 = kwargs.get("quick_ratio")
            if qr0 is None:
                qr0 = bs.get("quick_ratio")
            if qr0 is not None and current_ratio and float(current_ratio) > 0:
                adj_qr = adj_cr * (float(qr0) / float(current_ratio))
            else:
                adj_qr = adj_cr * 0.9

        # 银行不评流动/速动比率：存款类机构没有「流动资产/流动负债」的对应
        # 科目划分（数据源对银行的货币资金/应收/存货均未映射），该口径不适用。
        if current_ratio is not None and not _is_bank:
            cr = float(current_ratio)
            score_cr = self._linear_score(cr, 0.8, 2.5)
            cr_comment = "由货币资金+应收+存货 / 流动负债粗估，简表口径"
            cr_weight = 2.0
            if supply_chain_power:
                # 传统流动比率对经营性负债型龙头易失真：降权 + 用剔除经营负债后的口径评分
                cr_weight = 1.0
                if adj_cr is not None:
                    score_cr = max(score_cr, self._linear_score(min(adj_cr, 5.0), 0.8, 2.5))
                    cr_comment = (
                        f"账面流动比率 {cr:.2f}（含经营性负债）；"
                        f"剔除应付/预收后约 {adj_cr:.2f}，反映产业链资金占用优势"
                    )
                else:
                    score_cr = max(score_cr, 72.0)
                    cr_comment = (
                        f"账面流动比率 {cr:.2f}；现金奶牛/产业链强势下经营性负债≠偿债风险，已降权"
                    )
            elif cycle_supplement:
                # 周期股补充：存货变现强，流动比率口径同样失真
                cr_weight = 1.2
                score_cr = max(score_cr, 60.0)
                cr_comment = (
                    f"账面流动比率 {cr:.2f}；周期股存货变现强，传统制造业口径已降权"
                )
            indicators.append(
                IndicatorResult(
                    name="流动比率",
                    value=round(cr, 2),
                    score=score_cr,
                    level=score_to_level(score_cr),
                    weight=cr_weight,
                    comment=cr_comment,
                    period=bs_period,
                )
            )

        quick_ratio = kwargs.get("quick_ratio")
        if quick_ratio is None and bs.get("quick_ratio") is not None:
            quick_ratio = bs.get("quick_ratio")
        if quick_ratio is not None and float(quick_ratio) <= 0:
            quick_ratio = None
        if quick_ratio is not None and not _is_bank:
            qr = float(quick_ratio)
            score_qr = self._linear_score(qr, 0.5, 1.8)
            qr_comment = "(货币资金+应收)/流动负债粗估，剔除存货"
            qr_weight = 1.5
            if supply_chain_power:
                qr_weight = 0.8
                if adj_qr is not None:
                    score_qr = max(score_qr, self._linear_score(min(adj_qr, 4.0), 0.5, 1.8))
                    qr_comment = (
                        f"账面速动比率 {qr:.2f}；剔除经营性负债后约 {adj_qr:.2f}"
                    )
                else:
                    score_qr = max(score_qr, 70.0)
                    qr_comment = f"账面速动比率 {qr:.2f}；产业链强势龙头已降权传统速动口径"
            elif cycle_supplement:
                # 周期/资源股：存货（矿石/化肥）变现能力强，速动比率口径失真
                qr_weight = 0.6
                score_qr = max(score_qr, 65.0)
                qr_comment = (
                    f"账面速动比率 {qr:.2f}；周期/资源股存货变现强，"
                    "传统制造业速动口径失真，已降权并提底分"
                )
            indicators.append(
                IndicatorResult(
                    name="速动比率",
                    value=round(qr, 2),
                    score=score_qr,
                    level=score_to_level(score_qr),
                    weight=qr_weight,
                    comment=qr_comment,
                    period=bs_period,
                )
            )

        if supply_chain_power:
            # 有息负债已低时再抬高有息分项权重感：单独加分项
            if ibd_ratio is not None:
                for ind in indicators:
                    if ind.name == "有息负债率(%)":
                        ind.weight = 3.2
            power_score = 90.0 if (ibd_ratio is not None and float(ibd_ratio) < 12) else 84.0
            indicators.append(
                IndicatorResult(
                    name="产业链话语权",
                    value=round(op_liab / 1e8, 2) if op_liab else 0.0,
                    score=power_score,
                    level=AnalysisLevel.EXCELLENT if power_score >= 85 else AnalysisLevel.GOOD,
                    weight=2.5,
                    comment=(
                        "经营性负债为主、有息负债可控：上下游资金占用优势，"
                        "偿债评分侧重有息负债与现金覆盖而非账面流动比率"
                    ),
                    period=bs_period,
                )
            )

        if _is_bank:
            warnings.append(
                "银行偿债评分仅基于杠杆水平（权益/总资产相对同业中位）；"
                "数据源不提供不良贷款率/拨备覆盖率/资本充足率，未纳入资产质量判断"
            )

        if not indicators:
            return ModuleResult("偿债能力", 50, AnalysisLevel.NEUTRAL, warnings=["暂无资产负债数据"])

        module_score = self._weighted_score(indicators)
        return ModuleResult(
            module_name="偿债能力",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={
                "tight_debt_industry": tight,
                "business_model": _biz_model or None,
                "bank_leverage_only": bool(_is_bank),
                "asset_quality_metrics_available": not _is_bank,
                "balance_sheet": bs or None,
                "supply_chain_power": supply_chain_power,
                "adjusted_current_ratio": round(adj_cr, 2) if adj_cr is not None else None,
                "adjusted_quick_ratio": round(adj_qr, 2) if adj_qr is not None else None,
            },
        )
