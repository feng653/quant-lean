"""Small process-local authentication throttles.

This deliberately does not pretend to be a distributed rate limiter.  It is a
bounded, lock-protected brake for password guessing on the single-host service;
the reverse proxy/firewall remains responsible for network flood protection.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request

from backend.config import settings

_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def _client_key(request: Request, subject: str = "") -> str:
    host = request.client.host if request.client else "unknown"
    # Do not retain raw usernames in memory keys or any future diagnostics.
    subject_digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
    return f"{host}:{subject_digest}"


async def enforce_auth_rate_limit(
    request: Request,
    *,
    bucket: str,
    limit: int,
    window_seconds: int,
    subject: str = "",
) -> None:
    """Raise a generic 429 before processing an authentication attempt."""
    now = time.monotonic()
    key = f"{bucket}:{_client_key(request, subject)}"
    with _lock:
        entries = _attempts[key]
        cutoff = now - max(window_seconds, 1)
        while entries and entries[0] <= cutoff:
            entries.popleft()
        if len(entries) >= max(limit, 1):
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后再试",
                headers={"Retry-After": str(max(window_seconds, 1))},
            )
        entries.append(now)


async def limit_login(request: Request, username: str) -> None:
    await enforce_auth_rate_limit(
        request,
        bucket="login",
        limit=settings.AUTH_LOGIN_MAX_ATTEMPTS,
        window_seconds=settings.AUTH_LOGIN_WINDOW_SECONDS,
        subject=username.casefold(),
    )


async def limit_refresh(request: Request) -> None:
    await enforce_auth_rate_limit(
        request,
        bucket="refresh",
        limit=settings.AUTH_REFRESH_MAX_ATTEMPTS,
        window_seconds=settings.AUTH_REFRESH_WINDOW_SECONDS,
    )


async def limit_sensitive(request: Request, user_id: int) -> None:
    await enforce_auth_rate_limit(
        request,
        bucket="sensitive",
        limit=settings.AUTH_SENSITIVE_MAX_ATTEMPTS,
        window_seconds=settings.AUTH_SENSITIVE_WINDOW_SECONDS,
        subject=str(user_id),
    )


def reset_auth_rate_limits_for_tests() -> None:
    with _lock:
        _attempts.clear()
