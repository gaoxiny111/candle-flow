from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.core.bull_tactics import TACTIC_NAMES
from app.database import get_db
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse
from app.services.bull_tactics_daily import load_daily_report, run_daily_bull_tactics_scan
from app.services.bull_tactics_service import TACTIC_RULES, BullTacticsService
from app.services.stock_universe import resolve_symbol
from app.services.watchlist import parse_watchlist
from app.utils.symbol import SymbolError

router = APIRouter()


def _parse_tactic(tactic: Optional[str]) -> list[str] | None:
    name = (tactic or "").strip()
    if not name:
        return None
    if name not in TACTIC_NAMES:
        raise HTTPException(status_code=400, detail=f"无效战法，可选：{'、'.join(TACTIC_NAMES)}")
    return [name]


@router.get("/bull-tactics/rules")
def bull_tactics_rules():
    return ApiResponse(
        data={
            "tactics": [
                {"id": name, "name": name, "rule": TACTIC_RULES[name]}
                for name in TACTIC_NAMES
            ],
            "universe": "沪深主板（600/601/603/605、000/001/002/003），排除 ST",
            "schedule": "工作日 16:35（Asia/Shanghai）增量同步未更新 K 线后自动扫描；K 线未达新鲜度门槛时不生成今日列表",
        }
    )


@router.get("/bull-tactics/daily")
def get_bull_tactics_daily(
    trade_date: Optional[str] = Query(None, description="YYYY-MM-DD，默认最新日报"),
):
    """读取收盘后自动扫描生成的当日战法列表。"""
    report = load_daily_report(trade_date)
    if not report:
        return ApiResponse(
            data={
                "status": "empty",
                "trade_date": trade_date,
                "count": 0,
                "items": [],
                "message": "暂无日报，可手动触发运行或等待收盘后自动任务",
            }
        )
    return ApiResponse(data=report)


@router.post("/bull-tactics/daily/run")
def run_bull_tactics_daily(
    recent_bars: int = Query(7, ge=3, le=30),
    refresh_universe: bool = Query(False),
    sync_klines: bool = Query(True, description="生成前增量同步未更新的主板 K 线"),
):
    """手动触发一次收盘战法扫描（与定时任务相同逻辑；默认先增量同步 K 线）。"""
    data = run_daily_bull_tactics_scan(
        recent_bars=recent_bars,
        refresh_list=refresh_universe,
        sync_klines=sync_klines,
    )
    if data.get("status") == "already_running":
        raise HTTPException(status_code=409, detail="扫描正在进行中，请稍后再试")
    return ApiResponse(data=data)


@router.get("/bull-tactics/scan/{symbol}")
def scan_bull_tactics_symbol(
    symbol: str,
    recent_bars: int = Query(30, ge=5, le=120),
    tactic: Optional[str] = Query(None, description="黑马跨栏 / N字反包 / 牛股三绝，留空则扫全部"),
    db: Session = Depends(get_db),
):
    try:
        symbol = resolve_symbol(symbol, db)
    except SymbolError as e:
        return ApiResponse(code=400101, message=str(e), data=None)
    tactics = _parse_tactic(tactic)
    row = BullTacticsService(db).scan_symbol(symbol, recent_bars=recent_bars, tactics=tactics)
    if not row:
        return ApiResponse(data={"symbol": symbol, "name": "", "hits": [], "eligible": False, "tactic": tactic})
    return ApiResponse(data={**row, "eligible": True, "tactic": tactic or None})


@router.post("/bull-tactics/scan/watchlist")
def scan_bull_tactics_watchlist(
    recent_bars: int = Query(30, ge=5, le=120),
    tactic: Optional[str] = Query(None),
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
    tactics = _parse_tactic(tactic)
    data = BullTacticsService(db).scan_symbols(watch, recent_bars=recent_bars, tactics=tactics)
    return ApiResponse(data=data)


@router.post("/bull-tactics/scan/market")
def scan_bull_tactics_market(
    recent_bars: int = Query(30, ge=5, le=120),
    tactic: Optional[str] = Query(None),
    refresh_universe: bool = Query(True, description="扫描前刷新股票列表"),
    db: Session = Depends(get_db),
):
    tactics = _parse_tactic(tactic)
    data = BullTacticsService(db).scan_market(
        recent_bars=recent_bars,
        refresh_list=refresh_universe,
        tactics=tactics,
    )
    if data.get("error"):
        raise HTTPException(status_code=503, detail=data["error"])
    return ApiResponse(data=data)
