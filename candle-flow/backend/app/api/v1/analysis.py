from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.analysis.engine import analyze_symbol_full
from app.database import get_db
from app.schemas.common import ApiResponse
from app.utils.symbol import SymbolError, normalize_symbol

router = APIRouter()


@router.get("/analysis/{symbol}")
def get_fundamental_analysis(symbol: str, db: Session = Depends(get_db)):
    """单票基本面深度分析（模块化评分 + 估值）。"""
    try:
        sym = normalize_symbol(symbol)
    except SymbolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        report = analyze_symbol_full(db, sym)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"分析失败: {e}") from e
    return ApiResponse(data=report)
