"""JWT signing and verification security invariants."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from backend.auth.jwt_handler import create_access_token, decode_token
from backend.config import settings
from backend.main import lifespan


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {
        "sub": "7",
        "username": "researcher",
        "jti": "test-jti",
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return claims


def test_access_token_round_trip_uses_hs256_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "JWT_SECRET", "s" * 64)
    token = create_access_token(7, "researcher", ["experiments:read"])

    payload = decode_token(token)

    assert payload is not None
    assert payload["sub"] == "7"
    assert payload["type"] == "access"
    assert payload["permissions"] == ["experiments:read"]


@pytest.mark.parametrize("missing_claim", ["sub", "jti", "type", "iat", "exp"])
def test_decode_rejects_tokens_missing_required_claims(
    missing_claim: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 64
    monkeypatch.setattr(settings, "JWT_SECRET", secret)
    claims = _claims()
    claims.pop(missing_claim)
    token = jwt.encode(claims, secret, algorithm="HS256")

    assert decode_token(token) is None


def test_decode_rejects_expired_or_wrong_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 64
    monkeypatch.setattr(settings, "JWT_SECRET", secret)
    expired = jwt.encode(
        _claims(exp=datetime.now(timezone.utc) - timedelta(seconds=1)),
        secret,
        algorithm="HS256",
    )
    wrong_algorithm = jwt.encode(_claims(), secret, algorithm="HS384")

    assert decode_token(expired) is None
    assert decode_token(wrong_algorithm) is None


def test_production_lifespan_rejects_short_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET", "too-short")

    async def start_application() -> None:
        async with lifespan(None):
            pytest.fail("production startup must fail before yielding")

    with pytest.raises(RuntimeError, match="32"):
        asyncio.run(start_application())
