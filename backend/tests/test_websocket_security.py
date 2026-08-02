from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

import backend.ws.auth as websocket_auth
from backend.api.auth import _create_tokens
from backend.auth.jwt_handler import create_access_token
from backend.config import settings
from backend.core.log_redaction import SensitiveUrlFilter
from backend.ws.auth import authenticate_websocket
from backend.ws.jobs import ws_endpoint as jobs_ws
from backend.ws.notifications import ws_endpoint as notifications_ws
from backend.ws.realtime import (
    publish_realtime_signal,
    ws_endpoint as realtime_ws,
)
from backend.ws.training import (
    publish_training_progress,
    ws_endpoint as training_ws,
)


@pytest.fixture()
def isolated_users_db(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users_db = tmp_path / "users.db"
    with sqlite3.connect(users_db) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                is_admin INTEGER NOT NULL,
                is_active INTEGER NOT NULL
            );
            CREATE TABLE user_permissions (
                user_id INTEGER NOT NULL,
                permission TEXT NOT NULL
            );
            INSERT INTO users VALUES (7, 'researcher', 0, 1);
            INSERT INTO user_permissions VALUES (7, 'experiments:read');
            INSERT INTO user_permissions VALUES (7, 'trading:read');
            """
        )
    experiment_db = tmp_path / "experiment.db"
    with sqlite3.connect(experiment_db) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL
            );
            INSERT INTO experiments VALUES (101, 7);
            INSERT INTO experiments VALUES (102, 8);
            """
        )
    trading_db = tmp_path / "trading.db"
    with sqlite3.connect(trading_db) as conn:
        conn.executescript(
            """
            CREATE TABLE deployments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL
            );
            INSERT INTO deployments VALUES (201, 7);
            INSERT INTO deployments VALUES (202, 8);
            """
        )
    monkeypatch.setattr(settings, "USERS_DB", str(users_db))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(experiment_db))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(trading_db))


def _authentication_app(permission: str | None = None) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws")
    async def endpoint(websocket: WebSocket) -> None:
        user = await authenticate_websocket(websocket, permission)
        if user is not None:
            await websocket.send_json({"type": "ready", "user_id": user["id"]})

    return app


def test_peer_disconnect_before_authentication_is_not_closed_twice() -> None:
    class DisconnectingWebSocket:
        query_params: dict[str, str] = {}
        client_state = WebSocketState.CONNECTING

        def __init__(self) -> None:
            self.close_calls = 0

        async def accept(self) -> None:
            self.client_state = WebSocketState.CONNECTED

        async def receive_json(self) -> None:
            self.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(code=1006)

        async def close(self, *, code: int, reason: str) -> None:
            self.close_calls += 1
            raise AttributeError("transfer_data_task")

    websocket = DisconnectingWebSocket()

    result = asyncio.run(authenticate_websocket(websocket))  # type: ignore[arg-type]

    assert result is None
    assert websocket.close_calls == 0


def test_first_frame_authentication_preserves_rbac(isolated_users_db) -> None:
    token = create_access_token(7, "researcher", ["experiments:read"])
    with TestClient(_authentication_app("experiments:read")) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "authenticate", "token": token})
            assert websocket.receive_json() == {"type": "authenticated"}
            assert websocket.receive_json() == {"type": "ready", "user_id": 7}


def test_all_real_websocket_endpoints_accept_exactly_once(
    isolated_users_db,
) -> None:
    app = FastAPI()
    app.add_api_websocket_route("/ws/jobs", jobs_ws)
    app.add_api_websocket_route("/ws/notifications", notifications_ws)
    app.add_api_websocket_route("/ws/training/{experiment_id}", training_ws)
    app.add_api_websocket_route("/ws/realtime/{deployment_id}", realtime_ws)
    token = create_access_token(
        7,
        "researcher",
        ["experiments:read", "trading:read"],
    )
    authentication = {"type": "authenticate", "token": token}

    with TestClient(app) as client:
        with client.websocket_connect("/ws/jobs") as websocket:
            websocket.send_json(authentication)
            assert websocket.receive_json() == {"type": "authenticated"}
            assert websocket.receive_json() == {
                "type": "connected",
                "channel": "jobs",
            }
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"

        with client.websocket_connect("/ws/notifications") as websocket:
            websocket.send_json(authentication)
            assert websocket.receive_json() == {"type": "authenticated"}
            assert websocket.receive_json()["type"] == "connected"
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"

        with client.websocket_connect("/ws/training/101") as websocket:
            websocket.send_json(authentication)
            assert websocket.receive_json() == {"type": "authenticated"}
            connected = websocket.receive_json()
            assert connected["type"] == "connected"
            assert connected["experiment_id"] == 101
            client.portal.call(publish_training_progress, 101, None)
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()

        with client.websocket_connect("/ws/realtime/201") as websocket:
            websocket.send_json(authentication)
            assert websocket.receive_json() == {"type": "authenticated"}
            connected = websocket.receive_json()
            assert connected["type"] == "connected"
            assert connected["deployment_id"] == 201
            client.portal.call(publish_realtime_signal, 201, None)
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()


def test_query_string_credentials_are_rejected_before_subscription(
    isolated_users_db,
) -> None:
    token = create_access_token(7, "researcher", ["experiments:read"])
    with TestClient(_authentication_app()) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(f"/ws?token={token}"):
                pass
    assert error.value.code == 4401


def test_missing_first_frame_times_out_fail_closed(
    isolated_users_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(websocket_auth, "_AUTH_TIMEOUT_SECONDS", 0.01)
    with TestClient(_authentication_app()) as client:
        with client.websocket_connect("/ws") as websocket:
            with pytest.raises(WebSocketDisconnect) as error:
                websocket.receive_json()
    assert error.value.code == 4401


@pytest.mark.parametrize(
    "message",
    [
        {"type": "authenticate"},
        {"type": "wrong", "token": "not-used"},
        {"type": "authenticate", "token": "not-a-jwt"},
        ["authenticate", "not-a-jwt"],
    ],
)
def test_malformed_or_invalid_first_frame_fails_closed(
    isolated_users_db,
    message,
) -> None:
    with TestClient(_authentication_app()) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(message)
            with pytest.raises(WebSocketDisconnect) as error:
                websocket.receive_json()
    assert error.value.code == 4401


def test_permission_is_checked_before_authenticated_ack(
    isolated_users_db,
) -> None:
    token = create_access_token(7, "researcher", [])
    with TestClient(_authentication_app("trading:write")) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "authenticate", "token": token})
            with pytest.raises(WebSocketDisconnect) as error:
                websocket.receive_json()
    assert error.value.code == 4403


def test_refresh_token_cannot_authenticate_websocket(isolated_users_db) -> None:
    refresh_token = _create_tokens(7, "researcher")["refresh_token"]
    with TestClient(_authentication_app()) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {"type": "authenticate", "token": refresh_token},
            )
            with pytest.raises(WebSocketDisconnect) as error:
                websocket.receive_json()
    assert error.value.code == 4401


@pytest.mark.parametrize(
    ("resource_id", "expected_code"),
    [(101, None), (102, 4403), (999, 4403)],
)
def test_resource_ownership_is_checked_before_subscription(
    isolated_users_db,
    resource_id: int,
    expected_code: int | None,
) -> None:
    token = create_access_token(7, "researcher", ["experiments:read"])
    app = FastAPI()

    @app.websocket("/ws")
    async def endpoint(websocket: WebSocket) -> None:
        user = await authenticate_websocket(
            websocket,
            "experiments:read",
            resource_db="experiment",
            resource_table="experiments",
            resource_id=resource_id,
        )
        if user is not None:
            await websocket.send_json({"type": "ready"})

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "authenticate", "token": token})
            if expected_code is None:
                assert websocket.receive_json() == {"type": "authenticated"}
                assert websocket.receive_json() == {"type": "ready"}
            else:
                with pytest.raises(WebSocketDisconnect) as error:
                    websocket.receive_json()
                assert error.value.code == expected_code


def test_uvicorn_request_target_token_is_redacted() -> None:
    token = "header.payload.signature"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "192.0.2.10:1234",
            "WebSocket",
            f"/ws/jobs?token={token}&view=active",
            "1.1",
            403,
        ),
        None,
    )

    assert SensitiveUrlFilter().filter(record)
    rendered = record.getMessage()
    assert token not in rendered
    assert "/ws/jobs?token=<redacted>&view=active" in rendered


def test_lazy_url_token_logging_keeps_format_arguments_valid() -> None:
    token = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiI3IiwidHlwZSI6ImFjY2VzcyJ9."
        "abcdefghijklmnopqrstuvwxyz012345"
    )
    record = logging.LogRecord(
        "quant_platform",
        logging.WARNING,
        __file__,
        1,
        "Rejected request %s?token=%s",
        ("/ws/jobs", token),
        None,
    )

    assert SensitiveUrlFilter().filter(record)
    rendered = record.getMessage()
    assert token not in rendered
    assert rendered == "Rejected request /ws/jobs?token=<redacted-jwt>"
