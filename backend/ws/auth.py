"""Authentication and ownership checks shared by WebSocket endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from backend.auth.jwt_handler import decode_token
from backend.auth.sessions import session_is_active
from backend.config import settings

_AUTH_TIMEOUT_SECONDS = 5.0
_MAX_TOKEN_LENGTH = 4096
_FORBIDDEN_QUERY_CREDENTIALS = frozenset({"token", "access_token", "authorization"})


async def _close_authentication(
    websocket: WebSocket,
    code: int,
    reason: str,
) -> None:
    # ``receive_json`` marks the peer side disconnected before raising
    # ``WebSocketDisconnect``.  Sending another close frame after that point is
    # both unnecessary and unsafe: some Uvicorn/websockets teardown paths no
    # longer have a ``transfer_data_task`` to close.
    if websocket.client_state is WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        # The peer may disappear while an authentication failure is handled.
        pass


async def authenticate_websocket(
    websocket: WebSocket,
    permission: str | None = None,
    *,
    resource_db: str | None = None,
    resource_table: str | None = None,
    resource_id: int | None = None,
) -> dict[str, Any] | None:
    """Authenticate the first WebSocket data frame before subscribing it.

    Browser WebSocket APIs cannot set an ``Authorization`` header.  The client
    therefore sends ``{"type": "authenticate", "token": "..."}`` as its
    first frame.  Long-lived bearer credentials are deliberately rejected in
    the URL so reverse-proxy and Uvicorn request-target logs cannot capture
    them.
    """
    if any(
        key.casefold() in _FORBIDDEN_QUERY_CREDENTIALS
        for key in websocket.query_params
    ):
        await _close_authentication(
            websocket,
            4401,
            "credentials in query strings are forbidden",
        )
        return None

    await websocket.accept()
    try:
        message = await asyncio.wait_for(
            websocket.receive_json(),
            timeout=_AUTH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _close_authentication(websocket, 4401, "authentication timeout")
        return None
    except (ValueError, RuntimeError, WebSocketDisconnect):
        await _close_authentication(websocket, 4401, "authentication required")
        return None

    token = message.get("token") if isinstance(message, dict) else None
    if (
        not isinstance(message, dict)
        or message.get("type") != "authenticate"
        or not isinstance(token, str)
        or not token
        or len(token) > _MAX_TOKEN_LENGTH
    ):
        await _close_authentication(websocket, 4401, "authentication required")
        return None

    payload = decode_token(token)
    if not payload or payload.get("type") != "access" or not payload.get("sub"):
        await _close_authentication(websocket, 4401, "authentication required")
        return None

    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        await _close_authentication(websocket, 4401, "authentication required")
        return None

    async with aiosqlite.connect(str(settings.abs_path(settings.USERS_DB))) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, username, is_admin, is_active FROM users WHERE id=?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None or not row["is_active"]:
            await _close_authentication(websocket, 4401, "invalid user")
            return None
        session_id = payload.get("sid")
        if session_id and (
            not isinstance(session_id, str)
            or not await session_is_active(
                conn,
                user_id=user_id,
                session_id=session_id,
            )
        ):
            await _close_authentication(websocket, 4401, "session revoked")
            return None
        is_admin = bool(row["is_admin"])
        if not is_admin and permission:
            cursor = await conn.execute(
                "SELECT 1 FROM user_permissions WHERE user_id=? AND permission=?",
                (user_id, permission),
            )
            if await cursor.fetchone() is None:
                await _close_authentication(websocket, 4403, "permission denied")
                return None

    if (
        resource_db
        and resource_table
        and resource_id is not None
        and not is_admin
    ):
        db_path = {
            "experiment": settings.EXPERIMENT_DB,
            "trading_sim": settings.TRADING_SIM_DB,
        }[resource_db]
        async with aiosqlite.connect(str(settings.abs_path(db_path))) as conn:
            cursor = await conn.execute(
                f"SELECT user_id FROM {resource_table} WHERE id=?",
                (resource_id,),
            )
            owner = await cursor.fetchone()
            if owner is None or int(owner[0]) != user_id:
                await _close_authentication(
                    websocket,
                    4403,
                    "resource access denied",
                )
                return None

    await websocket.send_json({"type": "authenticated"})
    return {
        "id": user_id,
        "username": row["username"],
        "is_admin": is_admin,
    }
