"""WebSocket 端点 —— 实时信号推送.

路径: /ws/realtime/{deployment_id}
推送部署的实时交易信号。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("quant_platform.ws.realtime")

# Each connection owns a queue. Sharing one queue between clients would make
# subscribers consume alternating messages instead of receiving a broadcast.
_realtime_queues: dict[int, set[asyncio.Queue]] = {}
_realtime_lock = asyncio.Lock()


async def publish_realtime_signal(deployment_id: int, data: dict[str, Any]) -> None:
    """向指定部署的实时信号订阅者推送消息。供 worker 调用。"""
    async with _realtime_lock:
        queues = tuple(_realtime_queues.get(deployment_id, ()))
    for queue in queues:
        await queue.put(data)


async def ws_endpoint(websocket: WebSocket, deployment_id: int) -> None:
    """WebSocket 实时信号推送 endpoint。

    建立连接后持续推送该部署策略产生的实时信号。

    消息格式:
        {
            "type": "signal",
            "deployment_id": 1,
            "timestamp": "2026-07-27T14:35:00",
            "signals": [
                {
                    "code": "000001.SZ",
                    "action": "BUY",
                    "score": 0.85,
                    "confidence": 0.92,
                    "reasoning": "MACD金叉 + 放量突破"
                }
            ]
        }

    特殊消息:
        {"type": "connected", "deployment_id": 1}
        {"type": "error", "deployment_id": 1, "message": "..."}
    """
    from backend.ws.auth import authenticate_websocket

    user = await authenticate_websocket(
        websocket,
        "trading:read",
        resource_db="trading_sim",
        resource_table="deployments",
        resource_id=deployment_id,
    )
    if user is None:
        return
    from backend.jobs.broker import get_broker

    logger.info("Realtime WS connected: deployment_id=%d", deployment_id)
    await get_broker().record_operational_event(
        "websocket_connected",
        "websocket",
        outcome="connected",
        stage="realtime",
    )

    # 每个连接使用独立队列，发布端会广播到所有队列。
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    async with _realtime_lock:
        _realtime_queues.setdefault(deployment_id, set()).add(queue)

    try:
        await websocket.send_json({
            "type": "connected",
            "deployment_id": deployment_id,
            "message": "实时信号通道已建立",
        })

        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                if msg is None:
                    break
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:
                    break
            except RuntimeError:
                break

    except WebSocketDisconnect:
        logger.info("Realtime WS disconnected: deployment_id=%d", deployment_id)
    except Exception:
        logger.exception("Realtime WS error: deployment_id=%d", deployment_id)
    finally:
        async with _realtime_lock:
            queues = _realtime_queues.get(deployment_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    _realtime_queues.pop(deployment_id, None)
        try:
            await websocket.close()
        except Exception:
            pass
        await get_broker().record_operational_event(
            "websocket_disconnected",
            "websocket",
            outcome="disconnected",
            stage="realtime",
        )
