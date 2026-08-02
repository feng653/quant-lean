"""管理 API — 用户管理与权限分配（仅 admin 可访问）."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.dependencies import (
    get_db,
    require_permission,
)
from backend.auth.rate_limit import limit_sensitive


async def _admin_sensitive_rate_guard(
    request: Request,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> None:
    """Throttle every management-plane route after JWT/RBAC authentication."""
    await limit_sensitive(request, int(user["id"]))


router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"],
    dependencies=[Depends(_admin_sensitive_rate_guard)],
)


# ── Pydantic 请求体 ──────────────────────────────────────────────────────────

class CreateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=72)
    display_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    is_admin: bool = False

    @field_validator("password")
    @classmethod
    def validate_bcrypt_length(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("密码的 UTF-8 编码不能超过 72 字节")
        return value


class UpdatePermissionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permissions: list[str]


class UpdateUserStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool


# ── 权限列表 ──────────────────────────────────────────────────────────────────

_ALL_PERMISSIONS = [
    {"key": "experiments:read", "name": "查看实验", "group": "实验"},
    {"key": "experiments:create", "name": "创建实验", "group": "实验"},
    {"key": "experiments:delete", "name": "删除实验", "group": "实验"},
    {"key": "experiments:sweep", "name": "参数扫描", "group": "实验"},
    {"key": "experiments:promote", "name": "审批研究晋升", "group": "实验"},
    {"key": "trading:read", "name": "查看交易", "group": "交易"},
    {"key": "trading:deploy", "name": "部署策略", "group": "交易"},
    {"key": "trading:execute", "name": "执行模拟", "group": "交易"},
    {"key": "trading:rebalance", "name": "再平衡", "group": "交易"},
    {"key": "data:read", "name": "查看数据", "group": "数据"},
    {"key": "data:update", "name": "更新数据", "group": "数据"},
    {"key": "strategies:read", "name": "查看策略", "group": "策略"},
    {"key": "strategies:scan", "name": "扫描策略", "group": "策略"},
    {"key": "ai:use", "name": "使用AI", "group": "AI"},
    {"key": "admin:users", "name": "管理用户", "group": "管理"},
]

# ═══════════════════════════════════════════════════════════════════════════
# GET /users — 用户列表
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/users")
async def list_users(
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """获取用户列表（仅 admin）。"""
    try:
        async for conn in get_db("users"):
            cursor = await conn.execute(
                """
                SELECT u.id, u.username, u.display_name, u.email,
                       u.is_admin, u.is_active, u.created_at, u.last_login
                FROM users u
                ORDER BY u.created_at DESC
                """
            )
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询用户列表失败: {e}")

    permissions_by_user: dict[int, list[str]] = {}
    try:
        async for conn in get_db("users"):
            cursor = await conn.execute(
                "SELECT user_id, permission FROM user_permissions ORDER BY permission"
            )
            for permission_row in await cursor.fetchall():
                permissions_by_user.setdefault(permission_row["user_id"], []).append(
                    permission_row["permission"]
                )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"查询用户权限失败: {exc}")

    users: list[dict[str, Any]] = []
    for row in rows:
        user_permissions = permissions_by_user.get(row["id"], [])
        perm_count = len(user_permissions)
        role = "管理员" if row["is_admin"] else ("操作员" if perm_count > 5 else "只读")
        users.append({
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "is_admin": bool(row["is_admin"]),
            "is_active": bool(row["is_active"]),
            "role": role,
            "permission_count": perm_count,
            "permissions": user_permissions,
            "created_at": row["created_at"],
            "last_login": row["last_login"],
        })

    return {"data": users}


# ═══════════════════════════════════════════════════════════════════════════
# POST /users — 创建用户
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/users")
async def create_user(
    body: CreateUserBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """创建新用户。"""
    # FIXED: bcrypt compatibility — use bcrypt lib directly
    import bcrypt as _bcrypt

    password_hash = _bcrypt.hashpw(body.password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")

    try:
        async for conn in get_db("users"):
            # 检查用户名唯一
            cursor = await conn.execute("SELECT id FROM users WHERE username = ?", (body.username,))
            if await cursor.fetchone():
                raise HTTPException(status_code=409, detail=f"用户名已存在: {body.username}")

            cursor = await conn.execute(
                """
                INSERT INTO users (username, password_hash, display_name, email, is_admin, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (body.username, password_hash, body.display_name, body.email, 1 if body.is_admin else 0),
            )
            await conn.commit()
            new_id = cursor.lastrowid

            # 如果非 admin，默认给只读权限
            if not body.is_admin:
                viewer_perms = [
                    "experiments:read", "trading:read", "data:read", "strategies:read"
                ]
                for perm in viewer_perms:
                    await conn.execute(
                        "INSERT INTO user_permissions (user_id, permission, granted_by) VALUES (?, ?, ?)",
                        (new_id, perm, user["id"]),
                    )
                await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建用户失败: {e}")

    return {"data": {"user_id": new_id, "username": body.username}}


# ═══════════════════════════════════════════════════════════════════════════
# PUT /users/{id}/permissions — 更新权限
# ═══════════════════════════════════════════════════════════════════════════

@router.put("/users/{target_user_id}/permissions")
async def update_user_permissions(
    target_user_id: int,
    body: UpdatePermissionsBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """更新用户权限（全量替换）。"""
    # 验证权限值
    valid_perms = {p["key"] for p in _ALL_PERMISSIONS}
    requested_permissions = sorted(set(body.permissions))
    invalid = [p for p in requested_permissions if p not in valid_perms]
    if invalid:
        raise HTTPException(status_code=400, detail=f"无效的权限: {invalid}")

    try:
        async for conn in get_db("users"):
            # 检查用户存在
            cursor = await conn.execute("SELECT id, is_admin FROM users WHERE id = ?", (target_user_id,))
            target = await cursor.fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail=f"用户不存在: {target_user_id}")

            # admin 用户不需要手动设置权限
            if target["is_admin"]:
                return {"data": {"user_id": target_user_id, "note": "admin 用户自动拥有所有权限"}}

            # 全量替换权限
            await conn.execute("DELETE FROM user_permissions WHERE user_id = ?", (target_user_id,))
            for perm in requested_permissions:
                await conn.execute(
                    "INSERT INTO user_permissions (user_id, permission, granted_by) VALUES (?, ?, ?)",
                    (target_user_id, perm, user["id"]),
                )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新权限失败: {e}")

    return {
        "data": {
            "user_id": target_user_id,
            "permissions": requested_permissions,
            "updated": True,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# PUT /users/{id}/status — 启用/停用用户
# ═══════════════════════════════════════════════════════════════════════════

@router.put("/users/{target_user_id}/status")
async def update_user_status(
    target_user_id: int,
    body: UpdateUserStatusBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """启用或停用账号；不能修改当前管理员自己的状态。"""
    if target_user_id == user["id"]:
        raise HTTPException(status_code=400, detail="不能修改自己的启用状态")
    try:
        async for conn in get_db("users"):
            cursor = await conn.execute(
                "UPDATE users SET is_active=? WHERE id=?",
                (1 if body.is_active else 0, target_user_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"用户不存在: {target_user_id}")
            if not body.is_active:
                await conn.execute(
                    "DELETE FROM user_sessions WHERE user_id=?",
                    (target_user_id,),
                )
                # New stateful browser sessions are separate from the legacy
                # table above.  Keep this additive migration lazy-safe for
                # databases created before auth session hardening.
                from backend.auth.sessions import revoke_session
                await revoke_session(
                    conn,
                    user_id=target_user_id,
                    reason="admin_account_disabled",
                )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"更新用户状态失败: {exc}")
    return {
        "data": {
            "user_id": target_user_id,
            "is_active": body.is_active,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# DELETE /users/{id} — 撤权并停用用户（保留历史审计记录）
# ═══════════════════════════════════════════════════════════════════════════

@router.delete("/users/{target_user_id}")
async def delete_user(
    target_user_id: int,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """撤销用户权限并停用账号，保留跨库实验和交易审计记录。"""
    if target_user_id == user["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己")

    try:
        async for conn in get_db("users"):
            cursor = await conn.execute("SELECT id FROM users WHERE id = ?", (target_user_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"用户不存在: {target_user_id}")

            await conn.execute(
                "UPDATE users SET is_active=0 WHERE id=?",
                (target_user_id,),
            )
            await conn.execute(
                "DELETE FROM user_permissions WHERE user_id=?",
                (target_user_id,),
            )
            await conn.execute(
                "DELETE FROM user_sessions WHERE user_id=?",
                (target_user_id,),
            )
            from backend.auth.sessions import revoke_session
            await revoke_session(
                conn,
                user_id=target_user_id,
                reason="admin_account_deactivated",
            )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除用户失败: {e}")

    return {"data": {"deactivated": True, "user_id": target_user_id}}


# ═══════════════════════════════════════════════════════════════════════════
# GET /permissions — 可用权限列表
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/permissions")
async def list_permissions(
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """获取系统中所有可用权限列表。"""
    return {"data": _ALL_PERMISSIONS}
