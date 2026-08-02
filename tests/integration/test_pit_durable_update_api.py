from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import point_in_time
from backend.config import settings
from backend.dependencies import get_current_user
from backend.services import pit_automation_scheduler


class _Broker:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    async def submit_job(self, **kwargs):
        self.submissions.append(kwargs)
        return "pit-job-1"


def _seed_service_user(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                is_admin INTEGER NOT NULL,
                is_active INTEGER NOT NULL
            );
            CREATE TABLE user_permissions (user_id INTEGER, permission TEXT);
            INSERT INTO users VALUES (41, 'pit_automation', 0, 1);
            INSERT INTO user_permissions VALUES (41, 'data:update');
            """
        )


def test_api_queues_independent_service_job_and_observes_state(tmp_path, monkeypatch) -> None:
    users_db = tmp_path / "users.db"
    _seed_service_user(users_db)
    monkeypatch.setattr(settings, "USERS_DB", str(users_db))
    monkeypatch.setattr(settings, "PIT_EVIDENCE_DB", str(tmp_path / "pit.db"))
    monkeypatch.setattr(settings, "PIT_AUTOMATION_SERVICE_USER_ID", 41)
    monkeypatch.setattr(settings, "PIT_AUTOMATION_SERVICE_USERNAME", "pit_automation")
    broker = _Broker()
    monkeypatch.setattr(pit_automation_scheduler, "get_job_broker", lambda: broker)

    app = FastAPI()
    app.include_router(point_in_time.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read", "data:update"],
    }
    with TestClient(app) as client:
        queued = client.post(
            "/api/data/point-in-time/automation/runs",
            json={"idempotency_key": "manual:integration"},
        )
        observed = client.get("/api/data/point-in-time/automation/runs")

    assert queued.status_code == 200
    assert observed.status_code == 200
    assert broker.submissions == [
        {
            "job_type": "pit_durable_update",
            "params": {
                "idempotency_key": "manual:integration",
                "source": "pit_automation_scheduler",
            },
            "user_id": None,
            "resource_type": "pit_automation",
            "resource_id": "manual:integration",
            "deduplicate_active": True,
        }
    ]
    assert "portfolio_id" not in broker.submissions[0]["params"]
    assert observed.json()["data"]["runs"] == []


def test_api_does_not_enqueue_without_valid_service_actor(tmp_path, monkeypatch) -> None:
    users_db = tmp_path / "users.db"
    _seed_service_user(users_db)
    monkeypatch.setattr(settings, "USERS_DB", str(users_db))
    monkeypatch.setattr(settings, "PIT_AUTOMATION_SERVICE_USER_ID", 41)
    monkeypatch.setattr(settings, "PIT_AUTOMATION_SERVICE_USERNAME", "wrong-name")
    broker = _Broker()
    monkeypatch.setattr(pit_automation_scheduler, "get_job_broker", lambda: broker)

    app = FastAPI()
    app.include_router(point_in_time.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:update"],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/data/point-in-time/automation/runs",
            json={"idempotency_key": "manual:no-actor"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "pit_automation_actor_invalid"
    assert broker.submissions == []
