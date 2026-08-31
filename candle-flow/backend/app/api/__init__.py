from fastapi import APIRouter

from app.api.v1 import (
    bull_tactics,
    flow,
    fundamentals,
    holdings,
    kline,
    patterns,
    pay,
    risk,
    signals,
    symbols,
    system,
    ws,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(symbols.router, tags=["symbols"])
api_router.include_router(kline.router, tags=["kline"])
api_router.include_router(flow.router, tags=["flow"])
api_router.include_router(bull_tactics.router, tags=["bull-tactics"])
api_router.include_router(fundamentals.router, tags=["fundamentals"])
api_router.include_router(holdings.router, tags=["holdings"])
api_router.include_router(patterns.router, tags=["patterns"])
api_router.include_router(signals.router, tags=["signals"])
api_router.include_router(risk.router, tags=["risk"])
api_router.include_router(system.router, tags=["system"])
api_router.include_router(pay.router, tags=["pay"])
api_router.include_router(ws.router, tags=["websocket"])
