"""Attack-oriented tests for refresh rotation and revocable sessions."""

from __future__ import annotations

import sqlite3

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.auth import router as auth_router
from backend.auth.rate_limit import reset_auth_rate_limits_for_tests
from backend.config import settings


@pytest.fixture()
def auth_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    users_db = tmp_path / "users.db"
    password_hash = bcrypt.hashpw(b"correct-password", bcrypt.gensalt()).decode()
    with sqlite3.connect(users_db) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                email TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_login TEXT
            );
            CREATE TABLE user_permissions (
                user_id INTEGER NOT NULL,
                permission TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO users VALUES (1, 'alice', ?, 'Alice', NULL, 0, 1, NULL)",
            (password_hash,),
        )
    monkeypatch.setattr(settings, "USERS_DB", str(users_db))
    monkeypatch.setattr(settings, "JWT_SECRET", "test-session-secret-" + ("s" * 48))
    reset_auth_rate_limits_for_tests()
    app = FastAPI()
    app.include_router(auth_router)
    with TestClient(app) as client:
        yield client, users_db
    reset_auth_rate_limits_for_tests()


def _login(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct-password"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_refresh_rotation_replay_revokes_entire_device_session(auth_client) -> None:
    client, _ = auth_client
    first = _login(client)
    rotated = client.post(
        "/api/auth/refresh", json={"refresh_token": first["refresh_token"]},
    )
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()["data"]

    # Reusing the predecessor is a theft/replay signal.  It must revoke the
    # family, including access and refresh credentials issued by the rotation.
    replay = client.post(
        "/api/auth/refresh", json={"refresh_token": first["refresh_token"]},
    )
    assert replay.status_code == 401
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {second['access_token']}"},
    ).status_code == 401
    assert client.post(
        "/api/auth/refresh", json={"refresh_token": second["refresh_token"]},
    ).status_code == 401


def test_logout_revokes_access_and_database_never_contains_refresh_raw(auth_client) -> None:
    client, users_db = auth_client
    tokens = _login(client)
    logged_out = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={},
    )
    assert logged_out.status_code == 200, logged_out.text
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"},
    ).status_code == 401

    with sqlite3.connect(users_db) as conn:
        refresh_hash = conn.execute(
            "SELECT token_hash FROM auth_refresh_tokens"
        ).fetchone()[0]
    assert refresh_hash != tokens["refresh_token"]
    assert len(refresh_hash) == 64


def test_login_rate_limit_throttles_bad_passwords(auth_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = auth_client
    monkeypatch.setattr(settings, "AUTH_LOGIN_MAX_ATTEMPTS", 2)
    for _ in range(2):
        assert client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong-password"},
        ).status_code == 401
    throttled = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong-password"},
    )
    assert throttled.status_code == 429
    assert throttled.headers["retry-after"] == str(settings.AUTH_LOGIN_WINDOW_SECONDS)
