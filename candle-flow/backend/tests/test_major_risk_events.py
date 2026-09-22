"""重大风险事件：公告扫描、质押率归一、生存/观察分级。"""

import pytest

from app.services.major_risk_events import (
    COMPLIANCE_VETO_MESSAGE,
    clear_major_risk_cache,
    detect_major_risk_events,
    normalize_pledge_hold_ratio,
    scan_notice_titles,
)


def test_normalize_pledge_hold_ratio_percent_not_decimal():
    """东财 1.5 / 3.45 是百分数，绝不能当成 150%。"""
    assert normalize_pledge_hold_ratio(1.5) == pytest.approx(0.015)
    assert normalize_pledge_hold_ratio(3.45) == pytest.approx(0.0345)
    assert normalize_pledge_hold_ratio(99.78) == pytest.approx(0.9978)
    assert normalize_pledge_hold_ratio(150) is None  # >100% 脏数据
    assert normalize_pledge_hold_ratio(100) == pytest.approx(1.0)

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
            "title": "*ST华闻:关于华闻传媒投资集团股份有限公司2025年度财务报告非标准审计意见的专项说明",
            "notice_date": "2026-04-30",
            "url": "",
        },
        {
            "title": "某公司关于召开董事会的通知",
            "notice_date": "2026-01-01",
            "url": "",
        },
    ]
    hits = scan_notice_titles(notices)
    by_id = {h.rule_id: h for h in hits}
    assert "pre_reorg" in by_id and by_id["pre_reorg"].severity == "survival"
    assert "audit_nonstd" in by_id and by_id["audit_nonstd"].severity == "survival"
    assert "share_freeze" in by_id and by_id["share_freeze"].severity == "observe"
    assert any("非标准审计" in h.keyword or "持续经营" in h.keyword for h in hits if h.rule_id == "audit_nonstd")


def test_growth_deducted_profit_veto():
    """归母净利高增但扣非仍为负 → 成长性强制 D，提示利润幻增。"""
    import pandas as pd

    from app.analysis.modules.growth import GrowthAnalyzer

    rows = [
        {"revenue": 2e8, "net_profit": -8e7},
        {"revenue": 1.8e8, "net_profit": -5e7},
        {"revenue": 1.5e8, "net_profit": -3e7},
    ]
    fd = pd.DataFrame(rows, index=["20231231", "20241231", "20251231"])
    result = GrowthAnalyzer().analyze(
        fd,
        revenue_yoy=13.39,
        profit_yoy=176.28,
        latest_report="20260630",
        annual_dates=["20231231", "20241231", "20251231"],
        parent_net_profit=58_281_533.91,
        deducted_net_profit=-55_227_195.37,
    )
    assert result.score <= 40
    assert result.level.value == "D"
    assert result.metadata.get("profit_illusion") is True
    assert any("非经常性损益" in w for w in result.warnings)
    assert not result.metadata.get("v_shape")


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
        lambda *_a, **_k: {"ratio": None, "invalid": False, "source": "", "holder": ""},
    )

    result = detect_major_risk_events("600491.SH")
    assert result["fatal"] is True
    assert result["event_count"] >= 1
    assert result["message"] == COMPLIANCE_VETO_MESSAGE
    assert any(e["rule_id"] == "pre_reorg" for e in result["events"])


def test_high_pledge_is_observe_not_fatal(monkeypatch):
    """高质押仅为观察级，不得一票否决。"""
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: {
            "ratio": 0.85,
            "invalid": False,
            "source": "control_pf_hold_ratio",
            "holder": "测试控股",
        },
    )
    result = detect_major_risk_events("300308.SZ")
    assert result["fatal"] is False
    assert result["event_count"] == 0
    assert result["observe_count"] >= 1
    assert any(e["rule_id"] == "pledge_high" for e in result["observe_events"])
    assert result["pledge_ratio"] == 0.85


def test_low_pledge_no_observe(monkeypatch):
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: {
            "ratio": 0.0165,
            "invalid": False,
            "source": "control_pf_hold_ratio",
            "holder": "山东中际投资控股有限公司",
        },
    )
    result = detect_major_risk_events("300308.SZ")
    assert result["fatal"] is False
    assert result["observe_count"] == 0
    assert abs(result["pledge_ratio"] - 0.0165) < 1e-6


def test_share_freeze_observe_not_fatal(monkeypatch):
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [
            {
                "title": "关于控股股东部分股份被司法冻结的公告",
                "notice_date": "2026-06-09",
                "url": "",
            }
        ],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: {"ratio": None, "invalid": False, "source": "", "holder": ""},
    )
    result = detect_major_risk_events("300308.SZ")
    assert result["fatal"] is False
    assert any(e["rule_id"] == "share_freeze" for e in result["observe_events"])


def test_compliance_veto_forces_rating_e(monkeypatch):
    """命中生存级风险时，无视财务打分强制 E + compliance_veto。"""
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
                    "severity": "survival",
                }
            ],
            "observe_events": [],
            "event_count": 1,
            "observe_count": 0,
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


def test_observe_pledge_does_not_force_e(monkeypatch):
    import pandas as pd

    from app.analysis.engine import FundamentalEngine

    engine = FundamentalEngine()
    dates = ["20231231", "20241231", "20251231"]
    df = pd.DataFrame(
        {
            "revenue": [1e9, 1.2e9, 1.5e9],
            "net_profit": [2e8, 3e8, 5e8],
            "operating_cashflow": [2.2e8, 3.1e8, 5.2e8],
            "equity": [2e9, 2.5e9, 3e9],
            "total_assets": [4e9, 5e9, 6e9],
            "goodwill": [0, 0, 0],
            "accounts_receivable": [1e8, 1e8, 1e8],
            "monetary_funds": [1e9, 1e9, 1e9],
            "short_term_borrowings": [0, 0, 0],
            "roe": [15.0, 18.0, 22.0],
            "roic": [12.0, 14.0, 16.0],
            "gross_margin": [40.0, 42.0, 45.0],
        },
        index=dates,
    )

    monkeypatch.setattr(
        "app.analysis.engine.build_financial_dataframe",
        lambda *_a, **_k: (
            df,
            {
                "name": "中际旭创",
                "industry": "通信设备",
                "report_dates": dates,
                "annual_dates": dates,
                "latest_report": "20260630",
                "latest_roe": 22.0,
                "debt_ratio": 30.0,
                "revenue_yoy": 50.0,
                "profit_yoy": 80.0,
                "ocf_per_share": 0.5,
                "eps": 3.8,
                "latest_cash_ratio": 0.132,
                "balance_sheet": {},
            },
        ),
    )
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: [
            {
                "symbol": "300308.SZ",
                "name": "中际旭创",
                "price": 100.0,
                "pe_ttm": 20.0,
                "pb": 5.0,
                "pe_percentile": 40.0,
                "pb_percentile": 40.0,
                "market_cap": 1e11,
                "dividend_yield": 0.5,
                "total_shares": 1e9,
            }
        ],
    )
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "300308.SZ",
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
            "fatal": False,
            "events": [],
            "observe_events": [
                {
                    "rule_id": "pledge_high",
                    "label": "大股东高比例质押（观察）",
                    "severity": "observe",
                    "title": "测试观察质押",
                    "notice_date": "",
                    "url": "",
                    "source": "pledge",
                    "keyword": "85%",
                }
            ],
            "event_count": 0,
            "observe_count": 1,
            "message": "",
            "observe_message": "命中观察级风险，警惕情绪杀跌；不等于公司生存危机",
            "pledge_ratio": 0.0165,
            "pledge_invalid": False,
            "labels": [],
            "observe_labels": ["大股东高比例质押（观察）"],
        },
    )

    report = engine.run_full_analysis("300308.SZ", db=None)
    assert report["compliance_veto"] is False
    assert report["final_rating"] != "E"
    assert report["major_risks"]["fatal"] is False
    # 不应因观察级质押触发价值陷阱文案
    assert "生存" not in (report.get("valuation", {}).get("value_trap_message") or "")
    cf = report["modules"]["cashflow"]
    cr = next(i for i in cf["indicators"] if i["name"].startswith("经营现金流/净利润"))
    # 5年均值评分：年报OCF/NP健康(>0.7) + 利润高增(+80%) → 短期扰动，按均值评分
    assert cr["value"] > 0.8  # 5年均值在1.0左右，而非当期0.13
    assert "短期扰动" in (cr.get("comment") or "")


# ── 风险释放：减持计划到期/届满 ────────────────────────────────────
# RISK_RELEASE_KEYWORDS 是枚举式的，实务标题写法无法穷举。格力电器
# 2026-06-22「关于大股东减持计划期限届满暨减持结果的公告」因关键词只列了
# 「减持计划届满」（少「期限」两字）而未被识别为释放，导致减持已到期结束
# 仍按新增观察级风险扣 8 分。以下测试锁定「减持 + 届满/到期」组合释放规则。


def _release_scan(title: str):
    from app.services.major_risk_events import scan_risk_release

    return scan_risk_release([{"title": title, "notice_date": "2026-06-22"}])


@pytest.mark.parametrize(
    "title",
    (
        "格力电器:关于大股东减持计划期限届满暨减持结果的公告",  # 真实漏判案例
        "XX:关于股东减持计划届满的公告",
        "XX:关于减持计划期限届满的公告",
        "XX:减持期限届满暨减持结果公告",
        "XX:关于股东减持计划时间届满的公告",
        "XX:关于减持计划实施完毕的公告",
        "XX:关于股东承诺不减持股份的公告",
    ),
)
def test_reduce_plan_expiry_is_release_signal(title):
    assert _release_scan(title)["released"] is True


@pytest.mark.parametrize(
    "title",
    (
        "XX:关于大股东拟减持股份的公告",  # 新增减持风险，不得释放
        "XX:关于股东减持计划的预披露公告",
        "XX:关于限售期届满的提示性公告",  # 无减持语义，不得因「届满」二字释放
        "XX:关于控股股东部分股份被司法冻结的公告",
    ),
)
def test_non_release_titles_not_released(title):
    assert _release_scan(title)["released"] is False


def test_reduce_hold_observe_event_released_by_plan_expiry(monkeypatch):
    """端到端：减持计划期限届满 → 该观察事件不计入 observe_count。"""
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [
            {
                "title": "格力电器:关于大股东减持计划期限届满暨减持结果的公告",
                "notice_date": "2026-06-22",
                "url": "",
            }
        ],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: {"ratio": None, "invalid": False, "source": "", "holder": ""},
    )
    result = detect_major_risk_events("000651.SZ")
    assert result["risk_released"] is True
    assert result["observe_count"] == 0
    assert any(e["rule_id"] == "reduce_hold" for e in result["observe_events_released"])


def test_reduce_hold_still_counts_when_only_plan_announced(monkeypatch):
    """反例：仅「拟减持」公告时，观察事件仍应计入并扣分。"""
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [
            {"title": "XX:关于大股东拟减持股份的公告", "notice_date": "2026-06-22", "url": ""}
        ],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: {"ratio": None, "invalid": False, "source": "", "holder": ""},
    )
    result = detect_major_risk_events("000651.SZ")
    assert result["observe_count"] == 1
    assert any(e["rule_id"] == "reduce_hold" for e in result["observe_events"])


# ── 生存级风险的解除通道（新潮能源 600777 案例，2026-09-21） ──────────────
# 旧逻辑对生存级事件没有释放路径：一旦命中「可能被终止上市」「无法表示意见」，
# 即使公司已摘帽也永远强制 E 级、估值锁 28 分，与事实相反。

_600777_NOTICES = [
    {"title": "新潮能源:2026年半年度业绩暨现金分红说明会投资者关系活动记录表",
     "notice_date": "2026-09-07", "url": ""},
    {"title": "*ST新潮:山东新潮能源股份有限公司关于撤销退市风险警示及其他风险警示暨停牌的公告",
     "notice_date": "2026-06-19", "url": ""},
    {"title": "*ST新潮:山东新潮能源股份有限公司关于申请撤销公司股票退市风险警示和其他风险警示的公告",
     "notice_date": "2026-04-24", "url": ""},
    {"title": "*ST新潮:山东新潮能源股份有限公司关于2024年度内部控制和财务报表审计报告"
              "无法表示意见涉及事项影响已消除的专项说明",
     "notice_date": "2026-04-24", "url": ""},
    {"title": "*ST新潮:山东新潮能源股份有限公司关于公司股票可能被终止上市的第六次风险提示公告",
     "notice_date": "2026-04-18", "url": ""},
]


def _patch(monkeypatch, notices):
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices", lambda *_a, **_k: notices
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: {"ratio": None, "invalid": False, "source": "", "holder": ""},
    )


def test_survival_release_clears_veto_after_st_removal(monkeypatch):
    """已正式撤销退市风险警示且简称去 ST → 生存级事件不再一票否决。"""
    _patch(monkeypatch, _600777_NOTICES)
    result = detect_major_risk_events("600777.SH")
    assert result["fatal"] is False
    assert result["event_count"] == 0
    assert result["message"] == ""
    assert set(result["released_labels"]) == {"非标审计/持续经营不确定性", "面值/重大违法退市风险"}
    assert result["current_name_hint"] == "新潮能源"
    assert result["name_has_st"] is False
    assert result["audit_opinion_hint"] is None  # 非标提示不得再传给风险模块扣 30 分


def test_application_stage_is_not_release(monkeypatch):
    """仅「申请撤销」阶段（尚未获准）不得视为解除。"""
    notices = [n for n in _600777_NOTICES if "暨停牌的公告" not in n["title"]]
    notices = [
        n for n in notices if "进展公告" not in n["title"]
    ]
    notices.insert(
        1,
        {
            "title": "*ST新潮:山东新潮能源股份有限公司关于申请撤销公司股票退市风险警示"
                     "及其他风险警示的进展公告",
            "notice_date": "2026-05-20",
            "url": "",
        },
    )
    _patch(monkeypatch, notices)
    result = detect_major_risk_events("600777.SH")
    assert result["fatal"] is True
    assert "面值/重大违法退市风险" in result["labels"]


def test_still_st_name_blocks_face_delist_release(monkeypatch):
    """撤销公告已出但简称仍带 ST（口径冲突时保守处理）→ 仍一票否决。"""
    notices = [
        {"title": "*ST某公司:关于撤销退市风险警示的公告", "notice_date": "2026-06-19", "url": ""},
        {"title": "*ST某公司:关于公司股票可能被终止上市的第三次风险提示公告",
         "notice_date": "2026-04-18", "url": ""},
    ]
    _patch(monkeypatch, notices)
    result = detect_major_risk_events("000001.SZ")
    assert result["fatal"] is True
    assert result["name_has_st"] is True


def test_release_rule_covers_audit_and_reorg_but_not_clean_reports():
    """解除语义白名单：非标已消除/重整执行完毕可解除；常规报告不误判。"""
    from app.services.major_risk_events import _survival_release_kind

    assert _survival_release_kind("X:关于审计报告无法表示意见涉及事项影响已消除的专项说明") == "audit_nonstd"
    assert _survival_release_kind("X:关于重整计划执行完毕的公告") == "pre_reorg"
    assert _survival_release_kind("X:2026年半年度审计报告") is None
    assert _survival_release_kind("X:关于变更签字会计师的公告") is None
    assert _survival_release_kind("X:2025年年度审计报告带强调事项段的无保留意见专项说明") is None


# ── 第八轮：中文语序导致的减持漏判（立霸股份 603519 案例）──────────────
# 真实公告标题「关于控股股东一致行动人减持股份计划公告」（2026-08-25）
# 含「减持股份计划」，但既不含「减持计划」（非连续）也不含「股份减持」
# （顺序相反）→ 旧版 4 个连续子串关键词零命中。全库同族写法实测漏判 10/15。
@pytest.mark.parametrize(
    "title",
    (
        "立霸股份:关于控股股东一致行动人减持股份计划公告",
        "XX:关于股东减持股份的预披露公告",
        "XX:关于控股股东减持股份计划的公告",
        "XX:关于持股5%以上股东减持股份计划公告",
        "XX:关于董事减持股份计划的公告",
        "XX:关于股东集中竞价减持股份计划公告",
        "XX:关于股东减持股份进展公告",
        "XX:关于股东减持股份计划时间过半的公告",
        "XX:关于控股股东一致行动人减持公司股份计划公告",
        "XX:关于公司股东减持股份计划公告",
    ),
)
def test_reduce_hold_semantic_match_covers_word_order_variants(title):
    """减持标题的语序变体必须全部命中（语义共现，非连续子串）。"""
    from app.services.major_risk_events import scan_notice_titles

    hits = scan_notice_titles([{"title": title, "notice_date": "2026-08-25", "url": ""}])
    assert any(h.rule_id == "reduce_hold" for h in hits), title


@pytest.mark.parametrize(
    "title",
    (
        "XX:关于股东减持计划实施完毕的公告",
        "格力电器:关于大股东减持计划期限届满暨减持结果的公告",
    ),
)
def test_reduce_hold_expiry_titles_go_to_release_not_risk(title):
    """「计划已结束」类标题不得记为新增减持风险，应走释放通道。"""
    from app.services.major_risk_events import scan_notice_titles, scan_risk_release

    n = [{"title": title, "notice_date": "2026-06-22", "url": ""}]
    assert not any(h.rule_id == "reduce_hold" for h in scan_notice_titles(n))
    assert scan_risk_release(n)["released"] is True


def test_release_only_cancels_older_reduce_events(monkeypatch):
    """释放公告只释放早于它的事件；更晚的新减持仍计入观察扣分。"""
    clear_major_risk_cache()
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_stock_notices",
        lambda *_a, **_k: [
            # 先有旧减持，随后计划届满释放
            {"title": "X:关于股东减持股份计划的公告", "notice_date": "2026-03-01", "url": ""},
            {"title": "X:关于股东减持计划期限届满暨减持结果的公告",
             "notice_date": "2026-06-22", "url": ""},
            # 释放之后又出现**新的**减持计划 → 不得被旧释放洗白
            {"title": "X:关于控股股东一致行动人减持股份计划公告",
             "notice_date": "2026-08-25", "url": ""},
        ],
    )
    monkeypatch.setattr(
        "app.services.major_risk_events.fetch_controller_pledge_ratio",
        lambda *_a, **_k: {"ratio": None, "invalid": False, "source": "", "holder": ""},
    )
    result = detect_major_risk_events("603519.SH")
    active = [e for e in result["observe_events"] if e.get("rule_id") == "reduce_hold"]
    # 2026-08-25 的新减持仍在观察级（未被 2026-06-22 的释放抵消）
    assert any(e.get("notice_date") == "2026-08-25" for e in active)
    assert result["observe_count"] >= 1
