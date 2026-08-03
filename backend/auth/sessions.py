"""Server-side state for revocable browser/device sessions.

Refresh JWTs are bearer credentials, so their database representation is a
SHA-256 proof only.  A refresh JWT may be consumed once.  Reusing it is a
strong theft signal and revokes the entire session family before returning a
generic 401 response.  No function in this module logs or returns a token.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import aiosqlite
import jwt

from backend.auth.jwt_handler import create_access_token
from backend.config import settings


_AUTH_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    family_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active
    ON auth_sessions(user_id, revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_family
    ON auth_sessions(family_id);

CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    token_jti TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (session_id) REFERENCES auth_sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_auth_refresh_session
    ON auth_refresh_tokens(session_id, used_at, revoked_at);
CREATE INDEX IF NOT EXISTS idx_auth_refresh_family
    ON auth_refresh_tokens(family_id, used_at, revoked_at);
"""


async def ensure_auth_session_schema(conn: aiosqlite.Connection) -> None:
    """Apply the additive auth migration for both new and existing users DBs."""
    await conn.executescript(_AUTH_SESSION_SCHEMA)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_refresh_token(
    *,
    user_id: int,
    username: str,
    session_id: str,
    family_id: str,
    now: datetime,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "type": "refresh",
        "jti": uuid4().hex,
        "sid": session_id,
        "family_id": family_id,
        "iat": now,
        "exp": now + timedelta(days=settings.AUTH_SESSION_MAX_DAYS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256"), payload


async def issue_new_session(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    username: str,
    permissions: list[str] | None = None,
) -> dict[str, str]:
    """Create one server-side session and its first one-time refresh token.

    The caller owns the transaction boundary.  This is deliberately one
    session per interactive login, which makes device-level revoke meaningful.
    """
    await ensure_auth_session_schema(conn)
    now = utc_now()
    session_id = uuid4().hex
    family_id = uuid4().hex
    expires_at = now + timedelta(days=settings.AUTH_SESSION_MAX_DAYS)
    refresh_token, payload = _new_refresh_token(
        user_id=user_id,
        username=username,
        session_id=session_id,
        family_id=family_id,
        now=now,
    )
    await conn.execute(
        """INSERT INTO auth_sessions
        (session_id, user_id, family_id, created_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, user_id, family_id, _timestamp(now), _timestamp(now), _timestamp(expires_at)),
    )
    await conn.execute(
        """INSERT INTO auth_refresh_tokens
        (token_jti, token_hash, session_id, family_id, user_id, issued_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            str(payload["jti"]), _token_hash(refresh_token), session_id, family_id,
            user_id, _timestamp(now), _timestamp(expires_at),
        ),
    )
    return {
        "access_token": create_access_token(
            user_id, username, permissions, session_id=session_id,
        ),
        "refresh_token": refresh_token,
    }


async def rotate_refresh_token(
    conn: aiosqlite.Connection,
    *,
    refresh_token: str,
    payload: dict[str, Any],
    username: str,
    permissions: list[str] | None = None,
) -> dict[str, str] | None:
    """Consume a refresh token and issue its replacement atomically.

    ``None`` includes malformed state, stale sessions, and replay.  Replay
    revokes the whole family/session before it returns, preventing a stolen
    predecessor from racing a legitimate browser indefinitely.
    """
    try:
        user_id = int(payload["sub"])
        token_jti = str(payload["jti"])
        session_id = str(payload["sid"])
        family_id = str(payload["family_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all((token_jti, session_id, family_id)):
        return None

    await ensure_auth_session_schema(conn)
    now = utc_now()
    now_text = _timestamp(now)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            """SELECT s.user_id, s.family_id, s.revoked_at, s.expires_at,
                      t.token_hash, t.used_at, t.revoked_at AS token_revoked_at
               FROM auth_sessions AS s
               LEFT JOIN auth_refresh_tokens AS t ON t.token_jti=?
               WHERE s.session_id=?""",
            (token_jti, session_id),
        )
        row = await cursor.fetchone()
        valid = bool(
            row
            and int(row["user_id"]) == user_id
            and secrets.compare_digest(str(row["family_id"]), family_id)
            and secrets.compare_digest(str(row["token_hash"] or ""), _token_hash(refresh_token))
            and row["used_at"] is None
            and row["token_revoked_at"] is None
            and row["revoked_at"] is None
            and str(row["expires_at"]) > now_text
        )
        if not valid:
            # The session id/family originate from a signed token.  Revoking
            # only when the pair matches avoids turning arbitrary junk into a
            # logout primitive for another account.
            await conn.execute(
                """UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at, ?),
                   revoke_reason=COALESCE(revoke_reason, 'refresh_replay_or_invalid')
                   WHERE session_id=? AND user_id=? AND family_id=?""",
                (now_text, session_id, user_id, family_id),
            )
            await conn.execute(
                """UPDATE auth_refresh_tokens SET revoked_at=COALESCE(revoked_at, ?)
                   WHERE user_id=? AND family_id=?""",
                (now_text, user_id, family_id),
            )
            await conn.commit()
            return None

        cursor = await conn.execute(
            """UPDATE auth_refresh_tokens SET used_at=?
               WHERE token_jti=? AND used_at IS NULL AND revoked_at IS NULL""",
            (now_text, token_jti),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            return None
        next_refresh, next_payload = _new_refresh_token(
            user_id=user_id,
            username=username,
            session_id=session_id,
            family_id=family_id,
            now=now,
        )
        # PyJWT serializes datetime claims in the encoded token but does not
        # mutate our payload dictionary, so retain the source-of-truth window
        # here instead of assuming ``exp`` is already an integer.
        next_exp = now + timedelta(days=settings.AUTH_SESSION_MAX_DAYS)
        await conn.execute(
            """INSERT INTO auth_refresh_tokens
            (token_jti, token_hash, session_id, family_id, user_id, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(next_payload["jti"]), _token_hash(next_refresh), session_id,
                family_id, user_id, now_text, _timestamp(next_exp),
            ),
        )
        await conn.execute(
            "UPDATE auth_sessions SET last_seen_at=? WHERE session_id=?",
            (now_text, session_id),
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {
        "access_token": create_access_token(
            user_id, username, permissions, session_id=session_id,
        ),
        "refresh_token": next_refresh,
    }


async def session_is_active(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    session_id: str,
) -> bool:
    """Fail closed for a stateful token when its session state is unavailable."""
    try:
        cursor = await conn.execute(
            """SELECT 1 FROM auth_sessions
               WHERE session_id=? AND user_id=? AND revoked_at IS NULL
               AND expires_at > ?""",
            (session_id, user_id, _timestamp(utc_now())),
        )
        return await cursor.fetchone() is not None
    except aiosqlite.Error:
        return False


async def revoke_session(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    session_id: str | None = None,
    reason: str = "user_logout",
) -> int:
    """Revoke one device session, or all sessions when ``session_id`` is None."""
    await ensure_auth_session_schema(conn)
    now_text = _timestamp(utc_now())
    where = "user_id=?" + (" AND session_id=?" if session_id else "")
    params: tuple[object, ...] = (user_id, session_id) if session_id else (user_id,)
    cursor = await conn.execute(
        f"UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at, ?), "
        f"revoke_reason=COALESCE(revoke_reason, ?) WHERE {where} AND revoked_at IS NULL",
        (now_text, reason, *params),
    )
    if session_id:
        await conn.execute(
            "UPDATE auth_refresh_tokens SET revoked_at=COALESCE(revoked_at, ?) "
            "WHERE user_id=? AND session_id=?",
            (now_text, user_id, session_id),
        )
    else:
        await conn.execute(
            "UPDATE auth_refresh_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE user_id=?",
            (now_text, user_id),
        )
    return max(cursor.rowcount, 0)


async def list_sessions(conn: aiosqlite.Connection, *, user_id: int) -> list[dict[str, Any]]:
    await ensure_auth_session_schema(conn)
    cursor = await conn.execute(
        """SELECT session_id, created_at, last_seen_at, expires_at, revoked_at
           FROM auth_sessions WHERE user_id=? ORDER BY last_seen_at DESC""",
        (user_id,),
    )
    return [dict(row) for row in await cursor.fetchall()]
