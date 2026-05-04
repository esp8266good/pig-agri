import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()  # 無 prefix：HTTP 和 WS 路徑都完整寫


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, camera_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(camera_id, set()).add(ws)

    async def disconnect(self, camera_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.get(camera_id, set()).discard(ws)

    async def broadcast(self, camera_id: str, msg: dict) -> None:
        connections = set(self._connections.get(camera_id, set()))
        if not connections:
            return
        data = json.dumps(msg)
        dead: set[WebSocket] = set()
        for ws in connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._connections.get(camera_id, set()).difference_update(dead)


ws_manager = ConnectionManager()


@router.websocket("/ws/tracking/{camera_id}")
async def ws_tracking(ws: WebSocket, camera_id: str) -> None:
    await ws_manager.connect(camera_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(camera_id, ws)


@router.get("/tracking/{camera_id}")
async def get_tracking(
    camera_id: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    object_id: Optional[int] = None,
):
    return {"status": "not implemented"}
