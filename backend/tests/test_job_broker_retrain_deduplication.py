from __future__ import annotations

import asyncio

from backend.jobs.broker import JobBroker


def test_active_retrain_submission_is_deduplicated(tmp_path) -> None:
    broker = JobBroker(str(tmp_path / "jobs.db"))

    async def submit_twice() -> tuple[str, str]:
        first = await broker.submit_job(
            "retrain",
            {"deployment_id": 42, "user_id": 7},
            7,
            resource_type="deployment",
            resource_id=42,
            deduplicate_active=True,
        )
        second = await broker.submit_job(
            "retrain",
            {"deployment_id": 42, "user_id": 7},
            7,
            resource_type="deployment",
            resource_id=42,
            deduplicate_active=True,
        )
        return first, second

    first, second = asyncio.run(submit_twice())
    assert second == first
    jobs = asyncio.run(broker.list_jobs())
    assert len(jobs) == 1
