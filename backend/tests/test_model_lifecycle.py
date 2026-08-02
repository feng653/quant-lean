from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiosqlite

from backend.config import settings
from backend.services import model_lifecycle


class _RecordingBroker:
    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.recent_jobs: list[dict] = []

    async def submit_job(self, **kwargs):
        self.submissions.append(kwargs)
        return f"job-{kwargs['params']['deployment_id']}"

    async def query_jobs(self, **kwargs):
        return self.recent_jobs, len(self.recent_jobs)


def test_next_retrain_at_uses_calendar_months_and_utc() -> None:
    due = model_lifecycle.next_retrain_at(
        {
            "retrain_frequency": "monthly",
            "last_retrain_at": "2026-01-31 08:00:00",
        }
    )
    assert due == datetime(2026, 2, 28, 8, 0, tzinfo=timezone.utc)
    quarterly = model_lifecycle.next_retrain_at(
        {
            "retrain_frequency": "quarterly",
            "deployed_at": "2026-01-31T08:00:00Z",
        }
    )
    assert quarterly == datetime(2026, 4, 30, 8, 0, tzinfo=timezone.utc)


def test_public_failure_redacts_storage_path() -> None:
    failure = model_lifecycle.public_failure(
        "FileNotFoundError: /Users/researcher/project/data/models/model.joblib missing"
    )
    assert failure == {
        "code": "FileNotFoundError",
        "message": "[redacted-path] missing",
    }
    assert "researcher" not in failure["message"]


def test_due_scheduler_only_queues_active_due_deployments(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "trading.db"

    async def prepare() -> None:
        async with aiosqlite.connect(database) as connection:
            await connection.execute(
                """
                CREATE TABLE deployments (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    display_name TEXT,
                    status TEXT NOT NULL,
                    requires_retraining INTEGER NOT NULL,
                    retrain_frequency TEXT,
                    last_retrain_at TEXT,
                    deployed_at TEXT,
                    created_at TEXT
                )
                """
            )
            await connection.execute(
                """
                CREATE TABLE model_retrain_attempts (
                    deployment_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            await connection.executemany(
                """
                INSERT INTO deployments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        1,
                        7,
                        "到期模型",
                        "active",
                        1,
                        "monthly",
                        "2026-06-01T00:00:00Z",
                        None,
                        "2026-01-01T00:00:00Z",
                    ),
                    (
                        2,
                        7,
                        "未到期模型",
                        "active",
                        1,
                        "monthly",
                        "2026-07-15T00:00:00Z",
                        None,
                        "2026-01-01T00:00:00Z",
                    ),
                    (
                        3,
                        8,
                        "暂停模型",
                        "paused",
                        1,
                        "daily",
                        "2026-01-01T00:00:00Z",
                        None,
                        "2026-01-01T00:00:00Z",
                    ),
                ],
            )
            await connection.commit()

    asyncio.run(prepare())
    broker = _RecordingBroker()
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(database))
    monkeypatch.setattr(model_lifecycle, "get_job_broker", lambda: broker)

    submitted = asyncio.run(
        model_lifecycle.enqueue_due_retrains(
            now=datetime(2026, 7, 31, tzinfo=timezone.utc)
        )
    )

    assert submitted == ["job-1"]
    assert broker.submissions[0]["resource_type"] == "deployment"
    assert broker.submissions[0]["resource_id"] == 1
    assert broker.submissions[0]["deduplicate_active"] is True

    broker.submissions.clear()
    broker.recent_jobs = [
        {
            "status": "cancelled",
            "resource_id": "1",
            "completed_at": "2026-07-31T00:00:00Z",
        }
    ]
    submitted = asyncio.run(
        model_lifecycle.enqueue_due_retrains(
            now=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)
        )
    )
    assert submitted == []
    assert broker.submissions == []
