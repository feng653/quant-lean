"""RBAC 中间件 —— 权限检查装饰器与辅助函数.

架构文档第6节：多用户权限系统（RBAC）
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from fastapi import HTTPException



def require_permission(permission: str):
    """装饰器版本：检查当前用户是否有指定权限。

    用法（推荐使用 dependencies.py 中的 Depends 版本）:
        @require_permission("experiments:create")
        async def handler(...): ...

    此装饰器适用于非 FastAPI 路由函数，或需要更细粒度控制的场景。

    Args:
        permission: 权限字符串，如 "experiments:create"

    Returns:
        装饰器
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 尝试从 kwargs 中获取 current_user（由 FastAPI Depends 注入）
            user = kwargs.get("current_user")
            if user is None:
                # 没有用户时尝试从请求上下文获取
                raise HTTPException(status_code=401, detail="未认证")

            # admin 拥有所有权限
            if user.get("is_admin"):
                return await func(*args, **kwargs)

            user_perms: list[str] = user.get("permissions", [])
            if permission in user_perms:
                return await func(*args, **kwargs)

            raise HTTPException(status_code=403, detail=f"需要权限: {permission}")

        return wrapper

    return decorator


async def check_user_permission(
    user: dict[str, Any],
    permission: str,
) -> bool:
    """同步检查用户权限（非装饰器版本）。

    Args:
        user: 用户 dict（来自 get_current_user）
        permission: 权限字符串

    Returns:
        True 表示有权限
    """
    if user.get("is_admin"):
        return True
    return permission in user.get("permissions", [])


async def get_user_permissions(
    user_id: int,
) -> list[str]:
    """从数据库加载用户权限列表。

    Args:
        user_id: 用户ID

    Returns:
        权限字符串列表
    """
    from backend.dependencies import get_db

    async for conn in get_db("users"):
        import aiosqlite
        try:
            cursor = await conn.execute(
                "SELECT permission FROM user_permissions WHERE user_id = ?",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [r["permission"] for r in rows]
        except aiosqlite.Error:
            return []
