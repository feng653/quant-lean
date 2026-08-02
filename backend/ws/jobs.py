"""Authenticated WebSocket stream for background-job state changes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("quant_platform.ws.jobs")


class JobConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}
        self._admin_connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, user_id: int, is_admin: bool, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.setdefault(user_id, set()).add(websocket)
            if is_admin:
                self._admin_connections.add(websocket)

    async def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            sockets = self._connections.get(user_id)
            if sockets is not None:
                sockets.discard(websocket)
                if not sockets:
                    self._connections.pop(user_id, None)
            self._admin_connections.discard(websocket)

    async def publish(self, user_id: int | None, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = set(self._admin_connections)
            if user_id is not None:
                targets.update(self._connections.get(int(user_id), set()))
        stale: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            for owner_id in tuple(self._connections):
                await self.disconnect(owner_id, websocket)


_manager = JobConnectionManager()


async def publish_job_change(user_id: int | None, payload: dict[str, Any]) -> None:
    await _manager.publish(user_id, payload)


async def ws_endpoint(websocket: WebSocket) -> None:
    from backend.ws.auth import authenticate_websocket

    user = await authenticate_websocket(websocket)
    if user is None:
        return
    from backend.jobs.broker import get_broker

    user_id = int(user["id"])
    await _manager.connect(user_id, bool(user.get("is_admin")), websocket)
    await get_broker().record_operational_event(
        "websocket_connected",
        "websocket",
        outcome="connected",
        stage="jobs",
    )
    try:
        await websocket.send_json({"type": "connected", "channel": "jobs"})
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Job WebSocket disconnected with error", exc_info=True)
    finally:
        await _manager.disconnect(user_id, websocket)
        await get_broker().record_operational_event(
            "websocket_disconnected",
            "websocket",
            outcome="disconnected",
            stage="jobs",
        )
