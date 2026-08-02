"""依赖注入 —— FastAPI Depends 函数集.

提供:
- 数据库连接（多库切换）
- 当前用户解析（JWT）
- 权限检查
- 策略注册中心
- 任务队列
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import aiosqlite
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import settings

# ── Bearer Token 提取器 ─────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)

# ═══════════════════════════════════════════════════════════════════════════
# 数据库连接
# ═══════════════════════════════════════════════════════════════════════════

_DB_KEYS = {
    "users": "USERS_DB",
    "experiment": "EXPERIMENT_DB",
    "trading_sim": "TRADING_SIM_DB",
}


async def get_db(db_name: str) -> AsyncGenerator[aiosqlite.Connection, None]:
    """获取指定数据库的 aiosqlite 连接。

    Args:
        db_name: "users" | "experiment" | "trading_sim"

    Yields:
        aiosqlite.Connection（使用完毕自动关闭）
    """
    if db_name not in _DB_KEYS:
        raise ValueError(f"Unknown database: {db_name}. Must be one of {list(_DB_KEYS)}")

    db_path = settings.abs_path(getattr(settings, _DB_KEYS[db_name]))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# 当前用户
# ═══════════════════════════════════════════════════════════════════════════


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    """从 JWT Bearer Token 解析当前登录用户。

    Returns:
        user dict: {id, username, display_name, email, is_admin, is_active, permissions: [...]}

    Raises:
        HTTPException 401: 未提供 token 或 token 无效
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="请先登录")

    token = credentials.credentials
    try:
        from backend.auth.jwt_handler import decode_token
        payload = decode_token(token)
        if payload is None:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Token 类型无效")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 格式错误")

    # 查询用户信息
    async for conn in get_db("users"):
        try:
            cursor = await conn.execute(
                "SELECT id, username, display_name, email, is_admin, is_active FROM users WHERE id = ?",
                (int(user_id),),
            )
            row = await cursor.fetchone()
        except Exception:
            raise HTTPException(status_code=500, detail="数据库查询失败")

        if row is None:
            raise HTTPException(status_code=401, detail="用户不存在")

        if not row["is_active"]:
            raise HTTPException(status_code=403, detail="账户已禁用")

        # Stateful tokens are issued after the session-hardening migration.
        # Their server-side session must still be active.  Tokens created
        # before that migration deliberately keep their normal bounded JWT
        # lifetime for compatibility, but newly-issued tokens fail closed.
        session_id = payload.get("sid")
        if session_id:
            from backend.auth.sessions import session_is_active
            if not isinstance(session_id, str) or not await session_is_active(
                conn,
                user_id=int(user_id),
                session_id=session_id,
            ):
                raise HTTPException(status_code=401, detail="会话已撤销或已过期")

        # 查询用户权限
        perms: list[str] = []
        try:
            cursor = await conn.execute(
                "SELECT permission FROM user_permissions WHERE user_id = ?",
                (int(user_id),),
            )
            perm_rows = await cursor.fetchall()
            perms = [r["permission"] for r in perm_rows]
        except Exception:
            pass  # 权限查询失败不阻断登录

        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "is_admin": bool(row["is_admin"]),
            "is_active": bool(row["is_active"]),
            "permissions": perms,
            "jti": payload.get("jti", ""),
            "session_id": session_id if isinstance(session_id, str) else None,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 权限检查
# ═══════════════════════════════════════════════════════════════════════════


def require_permission(permission: str):
    """FastAPI Depends: 检查当前用户是否有指定权限。

    Usage:
        @router.get("/something")
        async def handler(user = Depends(require_permission("experiments:read"))):
            ...

    Args:
        permission: 权限字符串，如 "experiments:create"

    Returns:
        一个 Depends callable，通过则返回 user dict，否则 403
    """

    async def _check(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        # admin 拥有所有权限
        if user.get("is_admin"):
            return user

        if permission in user.get("permissions", []):
            return user

        raise HTTPException(
            status_code=403,
            detail=f"需要权限: {permission}",
        )

    return _check


# ═══════════════════════════════════════════════════════════════════════════
# 策略注册中心
# ═══════════════════════════════════════════════════════════════════════════


def get_strategy_registry():
    """返回全局策略注册表单例。"""
    from backend.strategies.registry import get_registry
    return get_registry()


# ═══════════════════════════════════════════════════════════════════════════
# 任务队列
# ═══════════════════════════════════════════════════════════════════════════


def get_job_broker():
    """返回全局任务队列 Broker 单例。"""
    from backend.jobs.broker import get_broker
    return get_broker()
