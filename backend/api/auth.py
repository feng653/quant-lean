"""认证 API — 用户注册、登录、Token 刷新."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
import bcrypt as _bcrypt
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config import settings
from backend.dependencies import get_current_user, get_db
from backend.api.schemas import (
    ApiResponse,
    AuthResponse,
    TokenResponse,
    UserResponse,
)
from backend.auth.rate_limit import limit_login, limit_refresh, limit_sensitive
from backend.auth.sessions import (
    issue_new_session,
    list_sessions,
    revoke_session,
    rotate_refresh_token,
)

router = APIRouter(prefix="/api/auth", tags=["Auth"])


# ── Pydantic 请求体 ──────────────────────────────────────────────────────────

class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=72)
    display_name: str | None = Field(default=None, max_length=100)
    email: str | None = None

    @field_validator("password")
    @classmethod
    def validate_bcrypt_length(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("密码的 UTF-8 编码不能超过 72 字节")
        return value


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str


class LogoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_sessions: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════

# FIXED: bcrypt compatibility — use bcrypt lib directly instead of passlib
def _hash_password(password: str) -> str:
    """使用 bcrypt 进行密码哈希。"""
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


# FIXED: reviewer issue #1 — JWT 创建调用签名匹配 create_access_token(user_id, username, permissions)
def _create_tokens(user_id: int, username: str, permissions: list[str] | None = None) -> dict[str, str]:
    """生成 access token 和 refresh token。"""
    from backend.auth.jwt_handler import create_access_token

    access_token = create_access_token(
        user_id=user_id,
        username=username,
        permissions=permissions,
    )
    # Refresh token: 内嵌 type="refresh" 标记
    import jwt
    from uuid import uuid4
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    refresh_payload = {
        "sub": str(user_id),
        "username": username,
        "type": "refresh",
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES * 7),
    }
    refresh_token = jwt.encode(refresh_payload, settings.JWT_SECRET, algorithm="HS256")
    return {"access_token": access_token, "refresh_token": refresh_token}


# ═══════════════════════════════════════════════════════════════════════════
# POST /register
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/register", response_model=ApiResponse[AuthResponse])
async def register(
    body: RegisterBody,
    request: Request,
    bootstrap_token: str | None = Header(None, alias="X-Bootstrap-Token"),
) -> dict[str, Any]:
    """注册新用户；开发环境首位用户为管理员，生产环境还需引导令牌。"""
    password_hash = _hash_password(body.password)

    try:
        async for conn in get_db("users"):
            # 检查用户名唯一
            cursor = await conn.execute("SELECT id FROM users WHERE username = ?", (body.username,))
            if await cursor.fetchone():
                raise HTTPException(status_code=409, detail=f"用户名已存在: {body.username}")

            # 检查是否是首位用户
            cursor = await conn.execute("SELECT COUNT(*) as cnt FROM users")
            count_row = await cursor.fetchone()
            is_first = (count_row["cnt"] == 0) if count_row else True
            is_bootstrap_admin = is_first and (
                settings.ENVIRONMENT != "production"
                or (
                    bool(settings.BOOTSTRAP_ADMIN_TOKEN)
                    and secrets.compare_digest(
                        bootstrap_token or "",
                        settings.BOOTSTRAP_ADMIN_TOKEN,
                    )
                )
            )

            cursor = await conn.execute(
                """
                INSERT INTO users (username, password_hash, display_name, email, is_admin, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    body.username,
                    password_hash,
                    body.display_name,
                    body.email,
                    1 if is_bootstrap_admin else 0,
                ),
            )
            await conn.commit()
            user_id = cursor.lastrowid

            # 非 admin 用户默认给予只读权限
            if not is_bootstrap_admin:
                viewer_perms = [
                    "experiments:read", "trading:read", "data:read", "strategies:read"
                ]
                for perm in viewer_perms:
                    await conn.execute(
                        "INSERT INTO user_permissions (user_id, permission, granted_by) VALUES (?, ?, ?)",
                        (user_id, perm, user_id),
                    )
                await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"注册失败: {e}")

    try:
        async for conn in get_db("users"):
            tokens = await issue_new_session(conn, user_id=user_id, username=body.username)
            await conn.commit()
    except Exception:
        # The account exists at this point, but never return stateless tokens
        # when creation of their revocation state has failed.
        raise HTTPException(status_code=503, detail="会话初始化失败，请稍后登录")

    return {
        "data": {
            "user_id": user_id,
            "username": body.username,
            "is_admin": is_bootstrap_admin,
            **tokens,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# POST /login
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/login", response_model=ApiResponse[AuthResponse])
async def login(body: LoginBody, request: Request) -> dict[str, Any]:
    """用户登录。"""
    await limit_login(request, body.username)
    try:
        async for conn in get_db("users"):
            cursor = await conn.execute(
                "SELECT id, username, password_hash, display_name, email, is_admin, is_active FROM users WHERE username = ?",
                (body.username,),
            )
            row = await cursor.fetchone()

            if row is None:
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            # FIXED: bcrypt compatibility — 使用 _bcrypt.checkpw()
            if not _bcrypt.checkpw(body.password.encode("utf-8"), row["password_hash"].encode("utf-8")):
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            if not row["is_active"]:
                raise HTTPException(status_code=403, detail="账户已禁用，请联系管理员")

            # 更新最后登录时间
            await conn.execute(
                "UPDATE users SET last_login = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            tokens = await issue_new_session(conn, user_id=row["id"], username=row["username"])
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"登录失败: {e}")

    return {
        "data": {
            "user_id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "is_admin": bool(row["is_admin"]),
            **tokens,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# POST /refresh
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/refresh", response_model=ApiResponse[TokenResponse])
async def refresh_token(body: RefreshBody, request: Request) -> dict[str, Any]:
    """刷新 access token。"""
    await limit_refresh(request)
    try:
        from backend.auth.jwt_handler import decode_token

        payload = decode_token(body.refresh_token)
        if payload is None or payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="无效的 refresh token")

        user_id = int(payload.get("sub"))
        async for conn in get_db("users"):
            cursor = await conn.execute(
                "SELECT username, is_active FROM users WHERE id=?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None or not row["is_active"]:
                raise HTTPException(status_code=401, detail="用户不存在或已停用")
            tokens = await rotate_refresh_token(
                conn,
                refresh_token=body.refresh_token,
                payload=payload,
                username=row["username"],
            )
            if tokens is None:
                raise HTTPException(status_code=401, detail="Token 无效或已过期")
        return {"data": tokens}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")


# ═══════════════════════════════════════════════════════════════════════════
# GET /me
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/me", response_model=ApiResponse[UserResponse])
async def get_me(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取当前登录用户信息。"""
    # Dependency-only JWT/session metadata must not be echoed by the generic
    # extensible response model.  Session IDs are exposed only in the explicit
    # device-session list, where ownership is already checked.
    return {
        "data": {
            key: user[key]
            for key in (
                "id", "username", "display_name", "email", "is_admin",
                "is_active", "permissions",
            )
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# Revocable device sessions
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/sessions")
async def get_sessions(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List the caller's sessions without exposing token material."""
    await limit_sensitive(request, int(user["id"]))
    async for conn in get_db("users"):
        sessions = await list_sessions(conn, user_id=int(user["id"]))
    current = user.get("session_id")
    return {
        "data": [
            {**session, "current": session["session_id"] == current}
            for session in sessions
        ]
    }


@router.post("/logout")
async def logout(
    request: Request,
    body: LogoutBody,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Revoke the current device session, or every session on request."""
    await limit_sensitive(request, int(user["id"]))
    session_id = None if body.all_sessions else user.get("session_id")
    if not body.all_sessions and not isinstance(session_id, str):
        # A pre-migration token has no server-side device record.  Revoking all
        # sessions is still available and avoids pretending this token did.
        raise HTTPException(status_code=409, detail="旧会话请使用 all_sessions 退出")
    async for conn in get_db("users"):
        revoked = await revoke_session(
            conn,
            user_id=int(user["id"]),
            session_id=session_id,
            reason="user_logout_all" if body.all_sessions else "user_logout",
        )
        await conn.commit()
    return {"data": {"revoked_sessions": revoked, "all_sessions": body.all_sessions}}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Revoke one listed device session owned by the caller."""
    if len(session_id) != 32 or any(char not in "0123456789abcdef" for char in session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    await limit_sensitive(request, int(user["id"]))
    async for conn in get_db("users"):
        revoked = await revoke_session(
            conn,
            user_id=int(user["id"]),
            session_id=session_id,
            reason="user_session_revoke",
        )
        await conn.commit()
    if not revoked:
        raise HTTPException(status_code=404, detail="会话不存在或已撤销")
    return {"data": {"revoked": True, "session_id": session_id}}
