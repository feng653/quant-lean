from __future__ import annotations

import asyncio
from datetime import datetime

from pydantic import SecretStr

from backend.services import research_data_scheduler as scheduler


def test_daily_refresh_is_source_date_deduplicated(monkeypatch) -> None:
    class Broker:
        calls: list[dict] = []
        latest = None

        async def submit_job(self, **kwargs):
            self.calls.append(kwargs)
            self.latest = {
                "job_uuid": "job-id",
                "status": "pending",
                "params": kwargs["params"],
            }
            return "job-id"

        async def get_latest_system_job(self, _job_type):
            return self.latest

    broker = Broker()
    monkeypatch.setattr(scheduler.settings, "TUSHARE_TOKEN", SecretStr("token"))
    monkeypatch.setattr(scheduler, "get_job_broker", lambda: broker)

    first = asyncio.run(
        scheduler.enqueue_daily_research_refresh(
            now=datetime.fromisoformat("2026-08-02T08:00:00+08:00")
        )
    )
    second = asyncio.run(
        scheduler.enqueue_daily_research_refresh(
            now=datetime.fromisoformat("2026-08-02T22:00:00+08:00")
        )
    )

    assert first == second == "job-id"
    assert len(broker.calls) == 1
    assert {call["resource_id"] for call in broker.calls} == {"tushare:2026-08-02"}
    assert all(call["deduplicate_existing"] is True for call in broker.calls)
    assert all(call["user_id"] is None for call in broker.calls)


def test_failed_daily_refresh_retries_after_cooldown_with_a_bound(monkeypatch) -> None:
    class Broker:
        calls: list[dict] = []
        latest = {
            "job_uuid": "failed-1",
            "status": "failed",
            "updated_at": "2026-08-02T08:00:00+08:00",
            "params": {
                "idempotency_key": "tushare:2026-08-02",
                "scheduler_attempt": 1,
            },
        }

        async def get_latest_system_job(self, _job_type):
            return self.latest

        async def submit_job(self, **kwargs):
            self.calls.append(kwargs)
            return "retry-2"

    broker = Broker()
    monkeypatch.setattr(scheduler.settings, "TUSHARE_TOKEN", SecretStr("token"))
    monkeypatch.setattr(scheduler, "get_job_broker", lambda: broker)

    during_cooldown = asyncio.run(
        scheduler.enqueue_daily_research_refresh(
            now=datetime.fromisoformat("2026-08-02T08:30:00+08:00")
        )
    )
    retry = asyncio.run(
        scheduler.enqueue_daily_research_refresh(
            now=datetime.fromisoformat("2026-08-02T10:00:00+08:00")
        )
    )

    assert during_cooldown == "failed-1"
    assert retry == "retry-2"
    assert broker.calls[0]["resource_id"] == "tushare:2026-08-02:attempt:2"
    assert broker.calls[0]["params"]["scheduler_attempt"] == 2


def test_failed_daily_refresh_treats_sqlite_naive_timestamp_as_utc(
    monkeypatch,
) -> None:
    class Broker:
        calls: list[dict] = []
        latest = {
            "job_uuid": "failed-utc",
            "status": "failed",
            # SQLite datetime('now') has no suffix but is UTC.  At 15:00 CST
            # this is only ten seconds old, not eight hours old.
            "updated_at": "2026-08-02 07:00:00",
            "params": {
                "idempotency_key": "tushare:2026-08-02",
                "scheduler_attempt": 1,
            },
        }

        async def get_latest_system_job(self, _job_type):
            return self.latest

        async def submit_job(self, **kwargs):
            self.calls.append(kwargs)
            return "unexpected-retry"

    broker = Broker()
    monkeypatch.setattr(scheduler.settings, "TUSHARE_TOKEN", SecretStr("token"))
    monkeypatch.setattr(scheduler, "get_job_broker", lambda: broker)

    result = asyncio.run(
        scheduler.enqueue_daily_research_refresh(
            now=datetime.fromisoformat("2026-08-02T15:00:10+08:00")
        )
    )

    assert result == "failed-utc"
    assert broker.calls == []


def test_daily_refresh_skips_without_token(monkeypatch) -> None:
    monkeypatch.setattr(scheduler.settings, "TUSHARE_TOKEN", SecretStr(""))
    assert asyncio.run(scheduler.enqueue_daily_research_refresh()) is None
