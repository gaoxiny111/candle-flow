from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.analysis.engine import analyze_symbol_full, analyze_symbols_batch
from app.database import get_db
from app.schemas.common import ApiResponse
from app.services.job_progress import finish_job, get_job, run_in_background, update_job
from app.utils.symbol import SymbolError, normalize_symbol

router = APIRouter()


class AnalysisBatchRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=50)


@router.post("/analysis/batch")
def batch_fundamental_analysis(body: AnalysisBatchRequest):
    """关注列表批量深度基本面（带缓存，后台可轮询 /analysis/batch/progress）。"""
    symbols = body.symbols or []
    if not symbols:
        return ApiResponse(data={"items": [], "count": 0, "cached": 0})

    existing = get_job(kind="analysis_batch")
    if existing and existing.get("status") == "running":
        return ApiResponse(data={"status": "already_running", "job_id": existing.get("job_id"), "progress": existing})

    def _run(job_id: str) -> None:
        def progress(done: int, total: int, phase: str) -> None:
            update_job(
                job_id,
                phase=phase,
                done=done,
                total=total,
                message=f"基本面分析 {done}/{total}",
            )

        try:
            update_job(job_id, phase="analysis", message="开始批量分析…", done=0, total=len(symbols))
            data = analyze_symbols_batch(symbols, progress=progress)
            finish_job(job_id, data)
        except Exception as exc:
            finish_job(job_id, error=str(exc))

    job_id = run_in_background("analysis_batch", _run, phase="starting", message="批量分析已启动")
    return ApiResponse(data={"status": "started", "job_id": job_id, "progress": get_job(job_id)})


@router.get("/analysis/batch/progress")
def analysis_batch_progress(job_id: Optional[str] = Query(None)):
    job = get_job(job_id, kind="analysis_batch")
    if not job:
        return ApiResponse(data={"status": "empty"})
    return ApiResponse(data=job)


@router.get("/analysis/{symbol}")
def get_fundamental_analysis(
    symbol: str,
    refresh: bool = Query(False, description="忽略缓存强制重算"),
    db: Session = Depends(get_db),
):
    """单票基本面深度分析（模块化评分 + 估值）。"""
    try:
        sym = normalize_symbol(symbol)
    except SymbolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        report = analyze_symbol_full(db, sym, use_cache=not refresh)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"分析失败: {e}") from e
    return ApiResponse(data=report)
