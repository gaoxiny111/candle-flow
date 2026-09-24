import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.core.exceptions import AppException
from app.database import init_db
from app.services.main_board_kline_sync import start_kline_sync_scheduler, stop_kline_sync_scheduler
from app.services.market_flow import start_market_flow_scheduler, stop_market_flow_scheduler
from app.services.pay_qr import PAY_DIR, ensure_pay_qrs


def _warmup_hot_endpoints() -> None:
    """启动后后台预热热点链路，消除重启后的冷启动空窗。

    两层：① 共振索引（内存态，重启即失；构建约 2 分钟，期间 resonance
    全部 409）；② technical-overlay 榜单（冷 SQLite 页缓存约 35s）。
    失败静默，不影响启动。
    """
    import threading
    import time

    def _run() -> None:
        time.sleep(8)  # 等 uvicorn 完成端口绑定与 K 线同步调度器启动
        db = None
        try:
            from app.database import SessionLocal
            from app.services import market_scan as msmod
            from app.services.market_scan import technical_overlay

            msmod.resonance_index_build()  # force=False：复用单票缓存与 TTL
            db = SessionLocal()
            technical_overlay(db, top=60, offset=0)
        except Exception:  # noqa: BLE001 - 预热失败只损失首访速度
            pass
        finally:
            if db is not None:
                db.close()

    threading.Thread(target=_run, daemon=True, name="hot-endpoint-warmup").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("data", exist_ok=True)
    init_db()
    ensure_pay_qrs()
    start_kline_sync_scheduler()
    start_market_flow_scheduler()
    _warmup_hot_endpoints()
    yield
    stop_market_flow_scheduler()
    stop_kline_sync_scheduler()


app = FastAPI(title="Candle Flow", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "message": exc.message, "data": None},
    )


app.include_router(api_router)

PAY_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/pay", StaticFiles(directory=str(PAY_DIR)), name="pay-qr")

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    assets = DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="frontend-assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("pay/") or full_path in {"docs", "redoc", "openapi.json"}:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (DIST / full_path).resolve()
        if DIST.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")
