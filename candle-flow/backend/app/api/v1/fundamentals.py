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
