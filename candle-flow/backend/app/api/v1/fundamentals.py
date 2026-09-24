from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.common import ApiResponse
from app.services.fundamental_screen import (
    AUTO_THEME_TOP_N,
    ScreenThresholds,
    analyze_symbols,
    clear_pool,
    list_pool,
    pool_items_out,
    screen_fundamentals,
    themes_catalog,
)
from app.services.market_scan import DEFAULT_TOP, MAX_TOP

router = APIRouter()


class ScreenRequest(BaseModel):
    themes: list[str] | None = None
    auto_themes: bool = True
    top_themes: int = Field(default=AUTO_THEME_TOP_N, ge=1, le=6)
    pool_size: int = Field(default=20, ge=5, le=50)
    roe_min: float = 15.0
    growth_min: float = 15.0
    debt_max: float = 60.0
    pe_pct_max: float = 40.0
    pb_pct_max: float = 40.0
    peg_max: float = 1.5
    enrich_valuation: bool = True
    enrich_debt: bool = True


class AnalyzeRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=50)
    enrich_valuation: bool = True
    enrich_debt: bool = True


@router.get("/fundamentals/themes")
def get_themes():
    return ApiResponse(
        data={
            "themes": themes_catalog(),
            "defaults": [],
            "auto": True,
            "note": "景气赛道采用四维验证（盈利/供需代理/政策/资金）：盈利端一票否决，共振≥3 才入选。供需为板块量价代理，非乘联会/SMM 原始数据。",
        }
    )


@router.get("/fundamentals/pool")
def get_pool(db: Session = Depends(get_db)):
    rows = list_pool(db)
    run_id = rows[0].pool_run_id if rows else ""
    return ApiResponse(
        data={
            "pool_run_id": run_id,
            "count": len(rows),
            "items": pool_items_out(rows, db),
        }
    )


@router.delete("/fundamentals/pool")
def delete_pool(db: Session = Depends(get_db)):
    n = clear_pool(db)
    return ApiResponse(data={"cleared": n})


@router.post("/fundamentals/analyze")
def analyze_watchlist(body: AnalyzeRequest, db: Session = Depends(get_db)):
    """Watchlist fundamental snapshot (ROE / growth / debt / verdict)."""
    if not body.symbols:
        return ApiResponse(data={"report_dates": [], "items": []})
    try:
        data = analyze_symbols(
            db,
            body.symbols,
            enrich_valuation=body.enrich_valuation,
            enrich_debt=body.enrich_debt,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"基本面分析失败：{e}") from e
    return ApiResponse(data=data)


@router.post("/fundamentals/position")
def run_position(db: Session = Depends(get_db)):
    """Layer-2: PE percentile + weekly/monthly candles → bottom/mid/top zones."""
    from app.services.fundamental_position import position_pool

    rows = list_pool(db)
    if not rows:
        raise HTTPException(status_code=400, detail="请先运行第一层季度筛选生成候选池")
    try:
        data = position_pool(db)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"战略定位失败：{e}") from e
    return ApiResponse(data=data)


@router.post("/fundamentals/tactics")
def run_tactics(db: Session = Depends(get_db)):
    """Layer-3: daily pullback + confirm + stop for bottom-zone names."""
    from app.services.fundamental_tactics import tactics_pool

    rows = list_pool(db)
    if not rows:
        raise HTTPException(status_code=400, detail="请先运行第一层季度筛选生成候选池")
    try:
        data = tactics_pool(db, require_bottom=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"战术入场扫描失败：{e}") from e
    return ApiResponse(data=data)


@router.post("/fundamentals/hold")
def run_hold(db: Session = Depends(get_db)):
    """Layer-4: add / reduce / exit + market-regime weights."""
    from app.services.fundamental_hold import hold_pool

    rows = list_pool(db)
    if not rows:
        raise HTTPException(status_code=400, detail="请先运行第一层季度筛选生成候选池")
    try:
        data = hold_pool(db)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"持仓管理扫描失败：{e}") from e
    return ApiResponse(data=data)


@router.post("/fundamentals/screen")
def run_screen(body: ScreenRequest, db: Session = Depends(get_db)):
    th = ScreenThresholds(
        roe_min=body.roe_min,
        growth_min=body.growth_min,
        debt_max=body.debt_max,
        pe_pct_max=body.pe_pct_max,
        pb_pct_max=body.pb_pct_max,
        peg_max=body.peg_max,
        pool_size=body.pool_size,
    )
    try:
        run_id, rows, used_dates, theme_meta = screen_fundamentals(
            db,
            themes=body.themes,
            auto_themes=body.auto_themes if body.themes is None else False,
            top_themes=body.top_themes,
            thresholds=th,
            enrich_valuation=body.enrich_valuation,
            enrich_debt=body.enrich_debt,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"基本面筛选失败：{e}") from e
    saved = list_pool(db)
    selected = [t["theme"] for t in theme_meta if t.get("selected")] if theme_meta else []
    scanned = selected or [t["theme"] for t in theme_meta] if theme_meta else (body.themes or [])
    return ApiResponse(
        data={
            "pool_run_id": run_id,
            "count": len(saved),
            "scanned_themes": scanned,
            "theme_prosperity": theme_meta,
            "theme_scorecards": theme_meta,
            "auto_themes": body.themes is None or body.auto_themes,
            "report_dates": used_dates,
            "items": pool_items_out(saved, db),
        }
    )


@router.get("/fundamentals/market-coverage")
def get_market_coverage(db: Session = Depends(get_db)):
    """因子库覆盖率：已构建快照 / SH·SZ 股票总数。

    全市场排序只在已覆盖样本内成立，故单列一个接口让前端先看覆盖率。
    """
    from app.services.market_scan import market_coverage

    return ApiResponse(data=market_coverage(db))


@router.get("/fundamentals/market-scan")
def run_market_scan(
    top: int = Query(default=DEFAULT_TOP, ge=1, le=MAX_TOP),
    min_composite: float | None = Query(default=None),
    min_market_cap_yi: float | None = Query(default=None, description="总市值下限（亿元）"),
    exclude_st: bool = Query(default=True),
    industry: str | None = Query(default=None, description="行业名包含匹配"),
    sort_by: str = Query(
        default="composite_score",
        description="composite_score 或五个维度键：profitability/growth/cashflow/solvency/valuation",
    ),
    keyword: str | None = Query(default=None, description="名称/代码包含匹配，如「茅台」「600519」"),
    include_gem: bool = Query(default=False, description="是否纳入创业板/科创板（默认仅沪深主板）"),
    offset: int = Query(default=0, ge=0, description="分页起始下标（过滤排序后偏移）"),
    db: Session = Depends(get_db),
):
    """全市场基本面排序（只读因子库，沿用个股分析综合分，不重算分数）。"""
    from app.services.market_scan import scan_market

    try:
        data = scan_market(
            db,
            top=top,
            min_composite=min_composite,
            min_market_cap_yi=min_market_cap_yi,
            exclude_st=exclude_st,
            industry=industry,
            sort_by=sort_by,
            keyword=keyword,
            include_gem=include_gem,
            offset=offset,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"全市场扫描失败：{e}") from e
    return ApiResponse(data=data)


@router.get("/fundamentals/market-scan/quality-value")
def get_quality_value_view(
    top: int = Query(default=DEFAULT_TOP, ge=1, le=MAX_TOP),
    offset: int = Query(default=0, ge=0, description="分页起始下标（过滤排序后偏移）"),
    exclude_st: bool = Query(default=True),
    include_gem: bool = Query(default=False, description="是否纳入创业板/科创板"),
    industry: str | None = Query(default=None, description="行业名包含匹配"),
    keyword: str | None = Query(default=None, description="名称/代码包含匹配"),
    # ── 质量门槛 ────────────────────────────────────────────
    min_roe: float | None = Query(default=None, description="ROE 下限（%，默认 10）"),
    min_roic: float | None = Query(default=None, description="ROIC 下限（%，默认 8）"),
    min_gross_margin: float | None = Query(default=None, description="毛利率下限（%，默认 15，低毛利行业不适用）"),
    max_debt_ratio: float | None = Query(default=None, description="资产负债率上限（%，默认 60；金融股不适用）"),
    min_ocf_np: float | None = Query(default=None, description="经营现金流/净利润 5 年均值下限（默认 0.8）"),
    # ── 价值门槛 ────────────────────────────────────────────
    max_pe: float | None = Query(default=None, description="PE 上限（默认 50，兜底用）"),
    max_pb: float | None = Query(default=None, description="PB 上限（默认 8，兜底用）"),
    max_pe_pctile: float | None = Query(default=None, description="PE 历史分位上限（%，默认 40）"),
    max_pb_pctile: float | None = Query(default=None, description="PB 历史分位上限（%，默认 50）"),
    min_dividend_yield: float | None = Query(default=None, description="股息率下限（%，默认 0 = 不限）"),
    # ── 成长 / 规模 ─────────────────────────────────────────
    max_peg: float | None = Query(default=None, description="PEG 上限（默认 1.5；PEG 缺失不淘汰）"),
    min_market_cap_yi: float | None = Query(default=None, description="总市值下限（亿元，默认 50）"),
    sort_by: str = Query(default="qv_score", description="qv_score / quality / value / roe_pct / …"),
    with_cycle_price: bool = Query(
        default=True,
        description="是否附加周期品「产品价格拐点」预警（items[].cycle_price，只读，不改分）",
    ),
    db: Session = Depends(get_db),
):
    """「质量 × 价值」选股视图（读层，不重算任何分数）。

    质量（好公司）× 价值（好价格）双硬门槛 + 行业内标准化：
    - 质量：ROE ≥ 10、ROIC ≥ 8、毛利率行业中位以上、资产负债率 ≤ 60、
      经营现金流/净利润（**5 年均值**）≥ 0.8；
    - 价值：PE 或 PB **任一**处于行业内低分位，绝对 PE/PB 上限只作兜底。

    与通用「质量40+价值30+成长30」模板的四处**刻意偏离**（ROIC 阈值按本项目会计
    口径下调、毛利率改行业中性、现金流用 5 年均值而非单年、不新建第二总分）见
    ``app/services/quality_value.py`` 模块 docstring。

    ``qv_score`` 仅用于排序与展示（``score_role="display_only"``），
    ``composite_score`` 仍是全站唯一权威分。
    """
    from app.services.market_scan import quality_value_view

    try:
        data = quality_value_view(
            db,
            top=top,
            offset=offset,
            exclude_st=exclude_st,
            include_gem=include_gem,
            industry=industry,
            keyword=keyword,
            min_roe=min_roe,
            min_roic=min_roic,
            min_gross_margin=min_gross_margin,
            max_debt_ratio=max_debt_ratio,
            min_ocf_np=min_ocf_np,
            max_pe=max_pe,
            max_pb=max_pb,
            max_pe_pctile=max_pe_pctile,
            max_pb_pctile=max_pb_pctile,
            min_dividend_yield=min_dividend_yield,
            max_peg=max_peg,
            min_market_cap_yi=min_market_cap_yi,
            sort_by=sort_by,
            with_cycle_price=with_cycle_price,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"质量价值视图读取失败：{e}") from e
    return ApiResponse(data=data)


@router.get("/fundamentals/market-scan/quality-value/rebalance")
def get_quality_value_rebalance(
    buy_score: float = Query(default=80.0, description="调入得分下限（不满足则 skip/持有）"),
    min_market_cap_yi: float = Query(default=50.0, description="调入总市值下限（亿元）"),
    sell_score: float = Query(default=50.0, description="调出得分上限（低于则 sell）"),
    max_month_drop_pct: float = Query(default=20.0, description="止损跌幅（%，相对持仓基准价）"),
    holdings: str | None = Query(
        default=None,
        description="持仓基准价，格式 `600519.SH:1500,600036.SH:35`（可只传持仓票）",
    ),
    top: int = Query(
        default=MAX_TOP,
        ge=1,
        le=MAX_TOP,
        description="**展示**条数上限（判定不受此限制：全候选集都参与判定）",
    ),
    exclude_st: bool = Query(default=True),
    include_gem: bool = Query(default=False),
    industry: str | None = Query(default=None, description="行业名包含匹配"),
    keyword: str | None = Query(default=None, description="名称/代码包含匹配"),
    db: Session = Depends(get_db),
):
    """质量×价值策略的**硬编码调仓规则**（只产出信号，不落持仓、不下单）。

    规则（用户给定）：
    - **调入**：综合得分 > 80 分，且市值 > 50 亿（避免小盘股流动性风险）
    - **调出**：得分 < 50 分，或单月跌幅 > 20%（止损）

    落地时的三点显式化（理由与实测数据见 ``quality_value.py`` 的「交易规则」段）：

    1. 「综合得分」判定口径 = 本视图 ``qv_score``；``composite_score`` 仅并列回显。
       实测两条线命中量差 3 倍（26 只 vs 76 只），取「都满足」会只剩 8 只。
    2. 「单月跌幅」默认相对**持仓基准价**（本项目快照只有当日点位、无历史序列）；
       基准价缺失**不触发**止损（缺失不是利空），但会在 ``missing`` 里留痕。
       若已从 K 线算好近 20 日收益，可传 ``holdings`` 之外由前端补 ``month_drop_pct``。
    3. 缺失一律**不放行买入**（市值未知 = 流动性风险未知）；反向**不触发卖出**。

    返回 ``buy`` / ``sell`` / ``hold`` / ``skip`` 四桶 + 阈值回显。

    **判定覆盖全部候选**（`universe` 只），不受榜单分页上限截断 ——
    `top` 仅限制各桶回传的**明细**条数。否则排名靠后却已跌破卖出线的持仓票
    会永远收不到调出信号，止损规则静默失效。``truncated`` 标明是否发生过截断。
    """
    from app.services.market_scan import quality_value_view
    from app.services.quality_value import apply_trading_rules

    # 解析 holdings：`600519.SH:1500,600036.SH:35`
    hp: dict[str, float] = {}
    if holdings:
        for part in holdings.split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            sym, _, px = part.rpartition(":")
            try:
                hp[sym.strip()] = float(px)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail=f"holdings 格式错误：{part}（应为 代码:价格）"
                ) from None

    try:
        # ★ 判定必须覆盖**全部**候选，不能被榜单分页的 MAX_TOP 截断：
        #   否则排在第 500 名之后、但已跌破卖出线的持仓票会永远收不到调出信号
        #   —— 止损规则形同虚设，这是静默失效（没有任何报错）。
        #   故这里先把候选全量取出（top 探到上限之上的实际规模），
        #   判定完再按 `top` 截断各桶的**展示**条数。
        probe = quality_value_view(
            db,
            top=MAX_TOP,
            offset=0,
            exclude_st=exclude_st,
            include_gem=include_gem,
            industry=industry,
            keyword=keyword,
            min_market_cap_yi=None,
            sort_by="qv_score",
            with_cycle_price=True,
        )
        matched = int(probe.get("matched") or 0)
        all_items = list(probe.get("items") or [])
        # 分页把剩下的候选补全（每页上限 MAX_TOP）
        cursor = MAX_TOP
        while cursor < matched:
            page = quality_value_view(
                db,
                top=MAX_TOP,
                offset=cursor,
                exclude_st=exclude_st,
                include_gem=include_gem,
                industry=industry,
                keyword=keyword,
                min_market_cap_yi=None,
                sort_by="qv_score",
                with_cycle_price=True,
            )
            batch = list(page.get("items") or [])
            if not batch:
                break
            all_items.extend(batch)
            cursor += len(batch)

        rules = apply_trading_rules(
            all_items,
            buy_score=buy_score,
            min_market_cap_yi=min_market_cap_yi,
            sell_score=sell_score,
            max_month_drop_pct=max_month_drop_pct,
            holdings=hp,
        )
        # 展示截断：判定结果不截断，只截断回传的明细（避免响应体膨胀）
        for key in ("buy", "sell", "hold", "skip"):
            rules[key] = rules[key][:top]
        rules["evaluated"] = len(all_items)
        rules["truncated"] = {
            "buy": rules["buy_count"] > top,
            "sell": rules["sell_count"] > top,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"调仓规则计算失败：{e}") from e

    return ApiResponse(
        data={
            **rules,
            "universe": matched,
            "rejected_by_quality_gate": probe.get("rejected"),
            "holdings_parsed": len(hp),
            "filters": probe.get("filters"),
        }
    )


@router.get("/fundamentals/market-scan/cycle-price")
def get_cycle_price_board(
    force: bool = Query(default=False, description="跳过进程内缓存（默认 1 小时）"),
):
    """周期品「产品价格拐点」价格看板（**只读，不参与任何打分**）。

    监测 30+ 个期货主力连续（新浪），按「距区间高点回撤」升序给出最需要警惕的品种，
    供周期股「估值陷阱」人工判断。判定口径与边界（停更品种剔除、product/cost 之分）
    见 ``app/services/cycle_price.py`` 模块 docstring。

    本端点**不产出任何分数**，也不影响 ``qv_score`` / ``composite_score``。
    """
    from app.services.cycle_price import clear_cache, commodity_board

    try:
        if force:
            clear_cache()
        data = commodity_board()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"商品价格看板读取失败：{e}") from e
    return ApiResponse(data=data)


@router.get("/fundamentals/market-scan/technical-overlay")
def run_market_scan_technical_overlay(
    top: int = Query(default=60, ge=1, le=MAX_TOP, description="单页标的数（技术分析覆盖本页全部标的）"),
    min_composite: float | None = Query(default=None),
    min_market_cap_yi: float | None = Query(default=None, description="总市值下限（亿元）"),
    exclude_st: bool = Query(default=True),
    industry: str | None = Query(default=None, description="行业名包含匹配"),
    sort_by: str = Query(
        default="composite_score",
        description="composite_score 或五个维度键：profitability/growth/cashflow/solvency/valuation",
    ),
    force: bool = Query(default=False, description="跳过 10 分钟缓存"),
    keyword: str | None = Query(default=None, description="名称/代码包含匹配，如「茅台」「600519」"),
    include_gem: bool = Query(default=False, description="是否纳入创业板/科创板（默认仅沪深主板）"),
    offset: int = Query(default=0, ge=0, description="分页起始下标（与榜单请求保持一致）"),
    min_fund: float = Query(default=70.0, description="买入候选：基本面分下限"),
    min_tech: float = Query(default=70.0, description="买入候选：技术面分下限"),
    core_score: float = Query(default=85.0, description="核心持仓：基本面与技术面均需达到"),
    veto_score: float = Query(default=60.0, description="淘汰：任一低于该值"),
    db: Session = Depends(get_db),
):
    """榜单技术共振叠加：基本面榜单 × K线买点信号 × 形态共振（含周线多周期确认）。

    形态共振与买点信号口径与「主板战法 / 信号页」同源；本端点内部复用
    /fundamentals/market-scan 的榜单结果，不重算基本面分。
    **技术分析覆盖本页全部标的**（与榜单同一 offset/top，不按名次截断）；
    逐票结果带 10 分钟缓存，翻页只计算新出现的标的。

    双阈值决策（共振过滤法）：基本面 ≥min_fund 且 技术面 ≥min_tech → 买入候选；
    两者均 ≥core_score → 核心持仓；任一 <veto_score → 淘汰；其余为观察。
    标签只做分类，不改写任何分数。
    """
    from app.services.market_scan import technical_overlay

    try:
        data = technical_overlay(
            db,
            top=top,
            min_composite=min_composite,
            min_market_cap_yi=min_market_cap_yi,
            exclude_st=exclude_st,
            industry=industry,
            sort_by=sort_by,
            keyword=keyword,
            include_gem=include_gem,
            offset=offset,
            min_fund=min_fund,
            min_tech=min_tech,
            core_score=core_score,
            veto_score=veto_score,
            force=force,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"技术共振叠加失败：{e}") from e
    return ApiResponse(data=data)


@router.get("/fundamentals/market-scan/resonance")
def get_market_scan_resonance(
    top: int = Query(default=DEFAULT_TOP, ge=1, le=MAX_TOP),
    min_composite: float | None = Query(default=None),
    min_market_cap_yi: float | None = Query(default=None, description="总市值下限（亿元）"),
    exclude_st: bool = Query(default=True),
    industry: str | None = Query(default=None, description="行业名包含匹配"),
    keyword: str | None = Query(default=None, description="名称/代码包含匹配"),
    include_gem: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    min_fund: float = Query(default=70.0, description="买入候选：基本面分下限"),
    min_tech: float = Query(default=70.0, description="买入候选：技术面分下限"),
    core_score: float = Query(default=85.0, description="核心持仓：两者均需达到"),
    veto_score: float = Query(default=60.0, description="淘汰：任一低于该值"),
    verdict_filter: str = Query(
        default="", description="仅看某档：core / candidate_up / eliminated，空为全部"
    ),
    db: Session = Depends(get_db),
):
    """共振视图（基本面×技术面，按档位全局排序，读层毫秒级）。

    索引未构建时返回 ``{"empty": True, "reason": ...}``，前端据此触发
    POST /market-scan/resonance/build 后台构建并轮询进度。
    """
    from app.services.market_scan import resonance_view

    try:
        data = resonance_view(
            db,
            top=top,
            offset=offset,
            min_composite=min_composite,
            min_market_cap_yi=min_market_cap_yi,
            exclude_st=exclude_st,
            industry=industry,
            keyword=keyword,
            include_gem=include_gem,
            min_fund=min_fund,
            min_tech=min_tech,
            core_score=core_score,
            veto_score=veto_score,
            verdict_filter=verdict_filter,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        return ApiResponse(data={"empty": True, "reason": str(e)})
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"共振视图读取失败：{e}") from e
    return ApiResponse(data=data)


@router.post("/fundamentals/market-scan/resonance/build")
def build_market_scan_resonance(
    force: bool = Query(default=False, description="强制重建（忽略 10 分钟索引缓存）"),
    db: Session = Depends(get_db),
):
    """后台构建共振索引（对 fund≥淘汰线的全部标的算技术面），返回 job_id。

    全市场约 2000 只 × ~0.3s，首次需数分钟；索引 10 分钟内复用，
    重建时单票缓存仍有效的标的不会重算。
    """
    from app.services import market_scan as msmod
    from app.services.job_progress import finish_job, get_job, run_in_background, update_job

    existing = get_job(kind="resonance_index")
    if existing and existing.get("status") == "running":
        raise HTTPException(status_code=409, detail="共振索引构建正在进行中")

    if not force:
        age = msmod.resonance_index_age()
        if age is not None and age < msmod.RESO_TTL_SEC:
            return ApiResponse(
                data={"status": "cached", "age_sec": round(age, 1), **(msmod._reso_cache.get("stats") or {})}
            )

    def _run(job_id: str) -> None:
        def progress(done: int, total: int, phase: str) -> None:
            label = {"tech": "技术面分析"}.get(phase, phase)
            update_job(
                job_id,
                done=done,
                total=total,
                phase=phase,
                message=f"{label} {done}/{total}" if total else label,
            )

        try:
            summary = msmod.resonance_index_build(progress=progress, force=force)
        except Exception as exc:
            finish_job(job_id, error=str(exc))
            return
        finish_job(job_id, summary)

    job_id = run_in_background(
        "resonance_index", _run, phase="starting", message="共振索引构建已启动"
    )
    return ApiResponse(data={"status": "started", "job_id": job_id, "progress": get_job(job_id)})


@router.get("/fundamentals/market-scan/resonance/progress")
def market_scan_resonance_progress(job_id: str | None = Query(None)):
    from app.services.job_progress import get_job

    job = get_job(job_id, kind="resonance_index")
    if not job:
        return ApiResponse(data={"status": "empty"})
    return ApiResponse(data=job)


@router.get("/fundamentals/market-scan/market-regime")
def get_market_regime(
    force: bool = Query(default=False, description="跳过 10 分钟缓存"),
    db: Session = Depends(get_db),
):
    """大盘环境提示（沪深300 / 上证指数的均线排列 + 周线趋势 + 20 日涨跌）。

    **不参与打分**：本结果不进入技术面得分、不改写任何阈值
    （响应体 ``scoring_impact: "none"``）。它只是把「当前大盘处于什么状态」
    如实摆出来，是否据此调整技术面门槛由使用者决定。
    """
    from app.services.market_regime import market_regime

    try:
        data = market_regime(db, force=force)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"大盘环境读取失败：{e}") from e
    return ApiResponse(data=data)


@router.post("/fundamentals/factors/rebuild")
def rebuild_factors(
    force: bool = False,
    budget_sec: float | None = None,
    max_symbols: int | None = None,
    db: Session = Depends(get_db),
):
    """手动触发因子库重建。

    force=True 全量重建；budget_sec / max_symbols 用于可控增量补齐
    （全市场约 5400 只、单只 ≈13s，默认 1800s 预算约只能建 1100 只）。
    """
    from app.services.factor_db import build_all

    stats = build_all(force=force, budget_sec=budget_sec, max_symbols=max_symbols)
    return ApiResponse(data=stats)


@router.get("/fundamentals/factors/progress")
def factors_progress():
    """因子库口径重建进度（轻量轮询端点）。

    按当前 SCORING_VERSION 统计现存快照的已重判/待重判数量，
    重建运行时附 build_all 实时游标。前端因子库覆盖卡片的进度条
    据此「跑出来几条就显示几条」。
    """
    from app.services.factor_db import rebuild_progress

    return ApiResponse(data=rebuild_progress())
