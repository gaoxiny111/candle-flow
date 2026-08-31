from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.core.bull_tactics import TACTIC_NAMES
from app.database import get_db
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse
from app.services.bull_tactics_service import TACTIC_RULES, BullTacticsService
from app.services.stock_universe import resolve_symbol
from app.services.watchlist import parse_watchlist
from app.utils.symbol import SymbolError

router = APIRouter()


@router.get("/bull-tactics/rules")
def bull_tactics_rules():
    return ApiResponse(
        data={
            "tactics": [
                {"id": name, "name": name, "rule": TACTIC_RULES[name]}
                for name in TACTIC_NAMES
            ],
            "universe": "沪深主板（600/601/603/605、000/001/002/003），排除 ST",
        }
    )


@router.get("/bull-tactics/scan/{symbol}")
def scan_bull_tactics_symbol(
    symbol: str,
    recent_bars: int = Query(30, ge=5, le=120),
    db: Session = Depends(get_db),
):
    try:
        symbol = resolve_symbol(symbol, db)
    except SymbolError as e:
        return ApiResponse(code=400101, message=str(e), data=None)
    row = BullTacticsService(db).scan_symbol(symbol, recent_bars=recent_bars)
    if not row:
        return ApiResponse(data={"symbol": symbol, "name": "", "hits": [], "eligible": False})
    return ApiResponse(data={**row, "eligible": True})


@router.post("/bull-tactics/scan/watchlist")
def scan_bull_tactics_watchlist(
    recent_bars: int = Query(30, ge=5, le=120),
    symbols: Optional[str] = None,
    db: Session = Depends(get_db),
    user: UserConfig | None = Depends(get_optional_user),
):
    watch: list[str] = []
    if user:
        watch = parse_watchlist(user.watchlist)
    if symbols:
        watch = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not watch:
        raise HTTPException(status_code=400, detail="还没有关注股票，请先添加自选")
    data = BullTacticsService(db).scan_symbols(watch, recent_bars=recent_bars)
    return ApiResponse(data=data)


@router.post("/bull-tactics/scan/market")
def scan_bull_tactics_market(
    recent_bars: int = Query(30, ge=5, le=120),
    refresh_universe: bool = Query(True, description="扫描前刷新股票列表"),
    db: Session = Depends(get_db),
):
    data = BullTacticsService(db).scan_market(
        recent_bars=recent_bars,
        refresh_list=refresh_universe,
    )
    if data.get("error"):
        raise HTTPException(status_code=503, detail=data["error"])
    return ApiResponse(data=data)
