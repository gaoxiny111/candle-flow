"""Holdings management: scan watchlist / explicit symbols for add·reduce·exit."""

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.database import get_db
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse
from app.services.watchlist import parse_watchlist

router = APIRouter()


class HoldingsScanRequest(BaseModel):
    symbols: list[str] | None = Field(
        default=None,
        description="Explicit symbols; if omitted, use login watchlist or guest symbols",
    )
    guest_symbols: list[str] | None = Field(
        default=None,
        description="Guest (unauthenticated) local watchlist fallback",
    )


@router.get("/holdings/rules")
def holdings_rules():
    from app.services.fundamental_hold import HOLD_RULES, IRON_RULES

    return ApiResponse(
        data={
            "rules": HOLD_RULES,
            "iron_rules": IRON_RULES,
            "note": "对自选/持仓股票做蜡烛图动态跟踪：加仓、减仓止盈、清仓。",
        }
    )


@router.post("/holdings/scan")
def scan_holdings(body: HoldingsScanRequest, db: Session = Depends(get_db), user: UserConfig | None = Depends(get_optional_user)):
    """Scan holdings (watchlist or provided symbols) for position-management signals."""
    from app.services.fundamental_hold import hold_symbols

    symbols: list[str] = []
    if body.symbols:
        symbols = list(body.symbols)
    elif user is not None:
        symbols = parse_watchlist(user.watchlist)
    elif body.guest_symbols:
        symbols = list(body.guest_symbols)

    if not symbols:
        raise HTTPException(status_code=400, detail="请先在设置中添加持仓/关注股票，或传入 symbols")

    try:
        data = hold_symbols(db, symbols)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"持仓扫描失败：{e}") from e
    return ApiResponse(data=data)
