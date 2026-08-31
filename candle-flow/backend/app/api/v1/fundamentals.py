from fastapi import APIRouter, Depends, HTTPException
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
