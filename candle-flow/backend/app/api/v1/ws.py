import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.websocket import ws_manager

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    await ws_manager.connect(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            await ws_manager.handle_message(websocket, raw)
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
