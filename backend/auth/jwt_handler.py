"""JWT 认证工具 —— 令牌创建、解码、用户提取."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt

from backend.config import settings

# ═══════════════════════════════════════════════════════════════════════════
# Token 操作
# ═══════════════════════════════════════════════════════════════════════════


def create_access_token(
    user_id: int,
    username: str,
    permissions: list[str] | None = None,
    *,
    session_id: str | None = None,
) -> str:
    """创建 JWT 访问令牌。

    Args:
        user_id: 用户数据库 ID。
        username: 用户名。
        permissions: 用户权限列表（持久化到 token 中）。

    Returns:
        JWT 字符串。
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "jti": uuid.uuid4().hex,  # 唯一 token ID（用于撤销）
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
        "permissions": permissions or [],
    }
    # ``sid`` binds newly issued access tokens to a revocable device session.
    # It remains optional so an upgrade does not instantly invalidate the
    # bounded lifetime of already-issued access tokens.
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """解码 JWT 令牌。

    Args:
        token: JWT 字符串。

    Returns:
        解码后的 payload 字典；解码失败返回 None。
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=["HS256"],
            options={"require": ["sub", "jti", "type", "iat", "exp"]},
        )
        return payload
    except jwt.InvalidTokenError:
        return None


def get_current_user(token: str) -> Optional[dict[str, Any]]:
    """从 JWT 令牌中提取当前用户信息。

    Args:
        token: JWT 字符串（不含 'Bearer ' 前缀）。

    Returns:
        包含 user_id, username, permissions 的字典；无效则返回 None。
    """
    payload = decode_token(token)
    if payload is None:
        return None

    # 检查是否过期
    exp = payload.get("exp")
    if exp is None:
        return None
    exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
    if exp_dt < datetime.now(timezone.utc):
        return None

    return {
        "user_id": int(payload["sub"]),
        "username": payload.get("username", ""),
        "permissions": payload.get("permissions", []),
        "jti": payload.get("jti", ""),
    }
