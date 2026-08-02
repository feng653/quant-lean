"""WebSocket 端点 —— 系统通知推送.

路径: /ws/notifications
向客户端推送系统级通知（任务完成、错误告警、重训练结果等）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("quant_platform.ws.notifications")


# 连接管理器
class NotificationManager:
    """管理所有通知 WebSocket 连接。"""

    def __init__(self) -> None:
        self._connections: dict[int, list[WebSocket]] = {}  # user_id → [ws, ...]

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        if user_id not in self._connections:
            self._connections[user_id] = []
        self._connections[user_id].append(websocket)
        logger.info("Notification WS connected: user_id=%d", user_id)

    async def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        if user_id in self._connections:
            self._connections[user_id] = [
                ws for ws in self._connections[user_id] if ws is not websocket
            ]
            if not self._connections[user_id]:
                del self._connections[user_id]
        logger.info("Notification WS disconnected: user_id=%d", user_id)

    async def broadcast(self, user_id: int, message: dict[str, Any]) -> None:
        """向指定用户推送通知。"""
        sockets = self._connections.get(user_id, [])
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    async def broadcast_all(self, message: dict[str, Any]) -> None:
        """向所有连接用户推送通知。"""
        for user_sockets in self._connections.values():
            for ws in user_sockets:
                try:
                    await ws.send_json(message)
                except Exception:
                    pass


# 全局单例
_notification_manager: NotificationManager | None = None


def get_notification_manager() -> NotificationManager:
    global _notification_manager
    if _notification_manager is None:
        _notification_manager = NotificationManager()
    return _notification_manager


async def ws_endpoint(websocket: WebSocket) -> None:
    """WebSocket 系统通知 endpoint。

    消息格式:
        {
            "type": "notification",
            "level": "info|success|warning|error",
            "title": "任务完成",
            "message": "实验 #42 已执行完毕",
            "timestamp": "2026-07-27T15:30:00",
            "data": { ... }
        }
    """
    from backend.ws.auth import authenticate_websocket

    user = await authenticate_websocket(websocket, "experiments:read")
    if user is None:
        return
    from backend.jobs.broker import get_broker

    user_id = int(user["id"])

    manager = get_notification_manager()
    await manager.connect(user_id, websocket)
    await get_broker().record_operational_event(
        "websocket_connected",
        "websocket",
        outcome="connected",
        stage="notifications",
    )

    # 发送欢迎消息
    await websocket.send_json({
        "type": "connected",
        "message": "通知通道已建立",
    })

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Notification WS error: user_id=%d", user_id)
    finally:
        await manager.disconnect(user_id, websocket)
        await get_broker().record_operational_event(
            "websocket_disconnected",
            "websocket",
            outcome="disconnected",
            stage="notifications",
        )
