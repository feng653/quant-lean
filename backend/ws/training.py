"""WebSocket 端点 —— 训练进度推送.

路径: /ws/training/{experiment_id}
消息格式: {type, experiment_id, progress, message, epoch, loss}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("quant_platform.ws.training")

# FIXED: reviewer issue #5 — 用 in-memory dict 管理 WebSocket 连接，替代不存在的 broker.subscribe_* 方法
# {experiment_id → asyncio.Queue}
_training_queues: dict[int, asyncio.Queue] = {}
_training_lock = asyncio.Lock()


async def publish_training_progress(experiment_id: int, data: dict[str, Any]) -> None:
    """向指定实验的训练进度订阅者推送消息。供 worker 调用。"""
    async with _training_lock:
        q = _training_queues.get(experiment_id)
    if q:
        await q.put(data)


async def ws_endpoint(websocket: WebSocket, experiment_id: int) -> None:
    """WebSocket 训练进度推送 endpoint。

    建立连接后持续向前端推送训练进度信息。
    前端可在实验中实时查看 epoch 进度和 loss 曲线。

    消息格式:
        {
            "type": "training_progress",
            "experiment_id": 1,
            "progress": 45.5,
            "message": "Epoch 10/100, loss=0.0523",
            "epoch": 10,
            "loss": 0.0523
        }

    特殊消息:
        {"type": "training_start", "experiment_id": 1, "message": "开始训练..."}
        {"type": "training_complete", "experiment_id": 1, "message": "训练完成", "metrics": {...}}
        {"type": "training_error", "experiment_id": 1, "message": "错误信息"}
    """
    from backend.ws.auth import authenticate_websocket

    user = await authenticate_websocket(
        websocket,
        "experiments:read",
        resource_db="experiment",
        resource_table="experiments",
        resource_id=experiment_id,
    )
    if user is None:
        return
    from backend.jobs.broker import get_broker

    logger.info("Training WS connected: experiment_id=%d", experiment_id)
    await get_broker().record_operational_event(
        "websocket_connected",
        "websocket",
        outcome="connected",
        stage="training",
    )

    # 创建该实验的队列
    async with _training_lock:
        if experiment_id not in _training_queues:
            _training_queues[experiment_id] = asyncio.Queue()
        queue = _training_queues[experiment_id]

    try:
        # 发送连接确认
        await websocket.send_json({
            "type": "connected",
            "experiment_id": experiment_id,
            "message": "训练进度通道已建立",
        })

        # 循环消费进度消息并推送
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                if msg is None:  # 结束信号
                    break
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                # 发送心跳保持连接
                try:
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:
                    break
            except RuntimeError:
                # queue 已关闭
                break

    except WebSocketDisconnect:
        logger.info("Training WS disconnected: experiment_id=%d", experiment_id)
    except Exception:
        logger.exception("Training WS error: experiment_id=%d", experiment_id)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        await get_broker().record_operational_event(
            "websocket_disconnected",
            "websocket",
            outcome="disconnected",
            stage="training",
        )
