from __future__ import annotations

import asyncio

import aiosqlite

from backend.config import settings
from backend.services import simulation, simulation_scheduler


class _RecordingBroker:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    async def submit_job(self, **kwargs):
        self.submissions.append(kwargs)
        return f"job-{len(self.submissions)}"


def test_scheduler_binds_simulations_to_system_data_prerequisite(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "trading.db"

    async def prepare() -> None:
        async with aiosqlite.connect(db_path) as connection:
            await connection.execute(
                """
                CREATE TABLE portfolios (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            await connection.executemany(
                "INSERT INTO portfolios (id, user_id, status) VALUES (?, ?, ?)",
                [(11, 7, "active"), (12, 8, "active"), (13, 9, "paused")],
            )
            await connection.commit()

    asyncio.run(prepare())
    broker = _RecordingBroker()
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(db_path))
    monkeypatch.setattr(settings, "PAPER_SIMULATION_REFRESH_DATA", True)
    monkeypatch.setattr(settings, "PIT_AUTOMATION_ACTOR_USER_ID", 99)
    monkeypatch.setattr(simulation_scheduler, "get_job_broker", lambda: broker)

    async def isolated_pit_ready(**_kwargs) -> None:
        return None

    monkeypatch.setattr(
        simulation,
        "require_simulation_pit_readiness",
        isolated_pit_ready,
    )

    count = asyncio.run(simulation_scheduler.enqueue_daily_simulations("2026-07-28"))

    assert count == 2
    assert len(broker.submissions) == 3
    prerequisite = broker.submissions[0]
    assert prerequisite["job_type"] == "data_update"
    assert prerequisite["user_id"] is None

    for submission in broker.submissions[1:]:
        assert submission["job_type"] == "daily_simulation"
        assert submission["params"]["required_data_job_uuid"] == "job-1"
