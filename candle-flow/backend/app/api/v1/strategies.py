"""价量策略（趋势确认 / 反转捕捉）端点。

**只读**：不参与评分、不改写任何分数。全市场扫描走后台任务 + 进度轮询
（与共振索引同一套机制），读层毫秒级返回。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core import price_volume as pvmod
from app.database import get_db
from app.schemas.common import ApiResponse
from app.services import price_volume_service as pvsvc

router = APIRouter()


@router.get("/strategies/price-volume/meta")
def price_volume_meta():
    """信号目录 + 交易成本口径（前端标签与说明用，避免前端硬编码）。"""
    return ApiResponse(
        data={
            "signals": [
                {
                    "key": k,
                    "name": v["name"],
                    "category": v["category"],
                    "category_zh": pvmod.CATEGORY_ZH.get(v["category"], v["category"]),
                    "direction": v["direction"],
                    "direction_zh": pvmod.DIRECTION_ZH.get(v["direction"], v["direction"]),
                    # 优先级 = 信号仲裁器权重（≥5 为风险类，直接判「规避」）
                    "priority": pvmod.priority_of(k),
                }
                for k, v in pvmod.SIGNAL_META.items()
            ],
            "categories": [
                {"key": "trend", "name": pvmod.CATEGORY_ZH["trend"],
                 "desc": "顺势：价格与成交量同步走强，确认趋势有效性"},
                {"key": "reversal", "name": pvmod.CATEGORY_ZH["reversal"],
                 "desc": "逆势：价量偏离常态到极端，捕捉反转"},
            ],
            "regimes": [
                {"key": "trend", "name": pvmod.REGIME_ZH["trend"],
                 "desc": f"ADX ≥ {pvmod.ADX_TREND}，趋势策略更可信"},
                {"key": "range", "name": pvmod.REGIME_ZH["range"],
                 "desc": f"ADX ≤ {pvmod.ADX_RANGE}，反转策略更可信"},
                {"key": "neutral", "name": pvmod.REGIME_ZH["neutral"], "desc": "介于两者之间"},
                {"key": "unknown", "name": pvmod.REGIME_ZH["unknown"], "desc": "历史不足，无法判定"},
            ],
            # 补丁二：交易分流环境（与上面的 regimes 描述口径不同，勿混用）
            "envs": [
                {"key": "TREND", "name": pvmod.ENV_ZH["TREND"],
                 "desc": f"ADX > {pvmod.ENV_TREND_ADX}，只跑趋势类看多信号"},
                {"key": "RANGE", "name": pvmod.ENV_ZH["RANGE"],
                 "desc": f"{pvmod.ENV_WEAK_ADX} ≤ ADX ≤ {pvmod.ENV_TREND_ADX}，只跑反转类看多信号"},
                {"key": "WEAK", "name": pvmod.ENV_ZH["WEAK"],
                 "desc": f"ADX < {pvmod.ENV_WEAK_ADX}，无趋势 → 空仓观望，信号全部屏蔽"},
                {"key": "UNKNOWN", "name": pvmod.ENV_ZH["UNKNOWN"],
                 "desc": "ADX 缺失 → 不分流，仅按优先级仲裁"},
            ],
            "verdicts": [
                {"key": "SIGNAL", "name": pvmod.VERDICT_ZH["SIGNAL"],
                 "desc": "保留一条最高优先级信号（买入候选）"},
                {"key": "AVOID", "name": pvmod.VERDICT_ZH["AVOID"],
                 "desc": f"风险类信号命中（优先级 ≥ {pvmod.PRIORITY_AVOID}）→ 从买入列表剔除"},
                {"key": "IGNORE", "name": pvmod.VERDICT_ZH["IGNORE"],
                 "desc": "信号被环境分流屏蔽 → 本轮不参与"},
                {"key": "STANDBY", "name": pvmod.VERDICT_ZH["STANDBY"],
                 "desc": f"ADX < {pvmod.ENV_WEAK_ADX} 无趋势 → 空仓观望"},
            ],
            "priority_rule": {
                "risk": pvmod.PRIORITY_RISK,
                "trend": pvmod.PRIORITY_TREND,
                "reversal": pvmod.PRIORITY_REVERSAL,
                "avoid_at": pvmod.PRIORITY_AVOID,
            },
            "cost": pvsvc.cost_breakdown(),
            "params": {
                "breakout_n": pvmod.BREAKOUT_N,
                "breakout_vol_k": pvmod.BREAKOUT_VOL_K,
                "reso_product_min": pvmod.RESO_PRODUCT_MIN,
                "adx_period": pvmod.ADX_PERIOD,
                "adx_trend": pvmod.ADX_TREND,
                "adx_range": pvmod.ADX_RANGE,
                "pv_window": pvmod.PV_WINDOW,
                "pv_high_pct": pvmod.PV_HIGH_PCT,
                "min_bars": pvmod.MIN_BARS,
                "lookback_days": pvsvc.DEFAULT_LOOKBACK_DAYS,
            },
            "scoring_impact": "none",
        }
    )


@router.get("/strategies/price-volume")
def price_volume_view(
    category: Optional[str] = Query(default=None, description="trend / reversal，空为全部"),
    signal: Optional[str] = Query(default=None, description="信号 key，逗号分隔，如 vol_breakout,pv_resonance"),
    regime: Optional[str] = Query(default=None, description="ADX 环境：trend / range / neutral"),
    verdict: Optional[str] = Query(
        default=None,
        description="裁决：SIGNAL（保留信号/买入候选）/ AVOID（规避）/ IGNORE（被环境屏蔽）/ STANDBY（无趋势空仓），逗号分隔",
    ),
    env: Optional[str] = Query(
        default=None, description="策略环境：TREND（趋势市）/ RANGE（震荡市）/ WEAK（无趋势空仓）"
    ),
    exclude_avoid: bool = Query(default=False, description="剔除裁决为「规避」的标的（= 只看可买入集合）"),
    latest_only: bool = Query(default=False, description="只看当日仍在生效的信号"),
    sort_by: str = Query(default="signal_score",
                         description="signal_score / verdict / trend / reversal / newest / adx / market_cap"),
    top: int = Query(default=100, ge=1, le=pvsvc.MAX_TOP),
    offset: int = Query(default=0, ge=0),
    industry: Optional[str] = Query(default=None, description="行业名包含匹配"),
    keyword: Optional[str] = Query(default=None, description="名称/代码包含匹配"),
    min_market_cap_yi: Optional[float] = Query(default=None, description="总市值下限（亿元）"),
    exclude_st: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """读层：读最近一次扫描结果（未构建时返回 empty + reason，前端据此触发构建）。"""
    try:
        data = pvsvc.view(
            db,
            category=category or None,
            signal=signal or None,
            regime=regime or None,
            verdict=verdict or None,
            env=env or None,
            exclude_avoid=exclude_avoid,
            latest_only=latest_only,
            sort_by=sort_by,
            top=top,
            offset=offset,
            industry=industry or None,
            keyword=keyword or None,
            min_market_cap_yi=min_market_cap_yi,
            exclude_st=exclude_st,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"价量策略读取失败：{e}") from e
    return ApiResponse(data=data)


@router.post("/strategies/price-volume/build")
def price_volume_build(
    lookback_days: int = Query(default=pvsvc.DEFAULT_LOOKBACK_DAYS, ge=1, le=60,
                               description="信号回看天数（窗口内出现过即算）"),
    include_gem: bool = Query(default=False, description="是否纳入创业板"),
    force: bool = Query(default=False, description="忽略 10 分钟缓存强制重建"),
    db: Session = Depends(get_db),
):
    """后台重建全市场价量信号索引，返回 job_id。"""
    from app.services.job_progress import finish_job, get_job, run_in_background, update_job

    existing = get_job(kind="price_volume_scan")
    if existing and existing.get("status") == "running":
        raise HTTPException(status_code=409, detail="价量策略扫描正在进行中")

    if not force:
        age = pvsvc.index_age()
        stats = pvsvc.latest_scan()
        if age is not None and age < pvsvc.SCAN_TTL_SEC and stats and stats.get("lookback_days") == lookback_days:
            return ApiResponse(data={"status": "cached", "age_sec": round(age, 1), **stats})

    def _run(job_id: str) -> None:
        def progress(done: int, total: int, phase: str) -> None:
            label = {"load": "读取 K 线", "scan": "价量判定", "cache": "命中缓存"}.get(phase, phase)
            update_job(
                job_id, done=done, total=total, phase=phase,
                message=f"{label} {done}/{total}" if total else label,
            )

        # 后台线程无请求上下文，自己开一个 Session
        from app.database import SessionLocal

        session = SessionLocal()
        try:
            summary = pvsvc.scan_market(
                session, lookback_days=lookback_days, include_gem=include_gem,
                force=force, progress=progress,
            )
        except Exception as exc:  # noqa: BLE001
            finish_job(job_id, error=str(exc))
            return
        finally:
            session.close()
        finish_job(job_id, summary)

    job_id = run_in_background(
        "price_volume_scan", _run, phase="starting", message="价量策略扫描已启动"
    )
    return ApiResponse(data={"status": "started", "job_id": job_id, "progress": get_job(job_id)})


@router.get("/strategies/price-volume/progress")
def price_volume_progress(job_id: Optional[str] = Query(default=None)):
    from app.services.job_progress import get_job

    job = get_job(job_id, kind="price_volume_scan")
    if not job:
        return ApiResponse(data={"status": "empty"})
    return ApiResponse(data=job)


@router.get("/strategies/price-volume/validation")
def price_volume_validation(
    refresh: bool = Query(default=False, description="忽略 1 小时缓存重新计算（耗时约 1 分钟）"),
    horizons: str = Query(default="5,10,20", description="持有期（交易日），逗号分隔"),
    symbol_limit: Optional[int] = Query(default=None, ge=50, description="仅验证前 N 只（快速抽样）"),
    include_gem: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """信号有效性验证：事件研究（T+1 开盘成交、扣全成本、同票基准对照）。"""
    try:
        hs = tuple(int(x) for x in str(horizons).split(",") if x.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail="horizons 需为逗号分隔的整数") from e
    if not hs:
        raise HTTPException(status_code=400, detail="horizons 不能为空")
    try:
        data = pvsvc.validate(
            db, horizons=hs, include_gem=include_gem, symbol_limit=symbol_limit,
            force=refresh,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"价量验证失败：{e}") from e
    return ApiResponse(data=data)


@router.get("/strategies/price-volume/stock/{symbol}")
def price_volume_stock(symbol: str, db: Session = Depends(get_db)):
    """单票明细（实时计算，不走扫描缓存）。"""
    try:
        data = pvsvc.single(db, symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"单票价量判定失败：{e}") from e
    return ApiResponse(data=data)
