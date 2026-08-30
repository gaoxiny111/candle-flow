import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Set

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)
        await self.send_to(websocket, "connected", {"session_id": id(websocket)})

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)

    async def send_to(self, websocket: WebSocket, event: str, data: Dict[str, Any]):
        msg = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await websocket.send_text(json.dumps(msg))

    async def broadcast(self, event: str, data: Dict[str, Any]):
        dead = []
        for ws in self.active:
            try:
                await self.send_to(ws, event, data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)

    async def handle_message(self, websocket: WebSocket, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_to(websocket, "error", {"code": 400, "message": "invalid json"})
            return
        event = payload.get("event")
        if event == "ping":
            await self.send_to(websocket, "pong", {})
        else:
            await self.send_to(websocket, "error", {"code": 400, "message": f"unknown event: {event}"})


ws_manager = WebSocketManager()
