from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.database import get_db
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse
from app.services.membership import require_member
from app.services.stock_universe import lookup_name, refresh_universe, resolve_symbol, search_stocks
from app.services.valuation import get_valuations
from app.services.watchlist import MAX_WATCHLIST
from app.utils.symbol import SymbolError, normalize_symbol

router = APIRouter()


@router.get("/symbols/search")
def search_symbols(q: str = Query(..., min_length=1, max_length=32), db: Session = Depends(get_db)):
    items = search_stocks(db, q.strip(), limit=10)
    return ApiResponse(data=items)


@router.get("/symbols/resolve")
def resolve_query(q: str = Query(..., min_length=1, max_length=32), db: Session = Depends(get_db)):
    try:
        symbol = resolve_symbol(q.strip(), db)
        return ApiResponse(data={"symbol": symbol, "name": lookup_name(db, symbol)})
    except SymbolError as e:
        return ApiResponse(code=400101, message=str(e), data=None)


@router.get("/symbols/names")
def lookup_names(symbols: str = Query(""), db: Session = Depends(get_db)):
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in symbols.split(",")[:MAX_WATCHLIST]:
        item = raw.strip()
        if not item:
            continue
        try:
            symbol = normalize_symbol(item)
        except SymbolError:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        name = lookup_name(db, symbol)
        if not name:
            hits = search_stocks(db, symbol.split(".")[0], limit=8)
            name = next((h["name"] for h in hits if h["symbol"] == symbol), "")
        if name:
            out.append({"symbol": symbol, "name": name})
    return ApiResponse(data=out)


@router.get("/symbols/valuations")
def lookup_valuations(
    symbols: str = Query(""),
    db: Session = Depends(get_db),
    user: UserConfig | None = Depends(get_optional_user),
):
    require_member(user)
    items = get_valuations([s.strip() for s in symbols.split(",") if s.strip()], db=db)
    for item in items:
        if not item.get("name"):
            item["name"] = lookup_name(db, item["symbol"]) or ""
    return ApiResponse(data=items)


@router.post("/symbols/refresh")
def refresh_symbols(db: Session = Depends(get_db)):
    count = refresh_universe(db, force=True)
    return ApiResponse(data={"updated": count})
