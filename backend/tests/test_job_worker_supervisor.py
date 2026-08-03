from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from starlette.responses import Response

import backend.jobs.worker as worker_module
from backend.main import health_check
from backend.jobs.broker import JobBroker


def test_job_worker_supervisor_restarts_with_a_finite_crash_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        calls = 0

        async def crashing_worker() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("worker boom")

        monkeypatch.setattr(worker_module, "job_worker", crashing_worker)
        with pytest.raises(RuntimeError, match="exhausted its restart budget"):
            await worker_module.supervise_job_worker(
                max_attempts=3,
                retry_base_seconds=0,
            )
        assert calls == 3

    asyncio.run(scenario())


def test_job_worker_supervisor_resets_budget_after_a_stable_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        calls = 0
        third_started = asyncio.Event()

        async def intermittently_crashing_worker() -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("worker boom")
            third_started.set()
            await asyncio.Event().wait()

        monotonic_values = iter((0.0, 0.1, 1.0, 3.0, 4.0))
        monkeypatch.setattr(
            worker_module,
            "job_worker",
            intermittently_crashing_worker,
        )
        task = asyncio.create_task(
            worker_module.supervise_job_worker(
                max_attempts=2,
                retry_base_seconds=0,
                stable_run_seconds=1,
                monotonic=lambda: next(monotonic_values),
            )
        )
        await asyncio.wait_for(third_started.wait(), timeout=1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_job_worker_supervisor_shutdown_cancels_active_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def active_worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(worker_module, "job_worker", active_worker)
        task = asyncio.create_task(
            worker_module.supervise_job_worker(
                max_attempts=3,
                retry_base_seconds=0,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(scenario())


def test_critical_background_task_health_reports_stopped_components() -> None:
    running = SimpleNamespace(done=lambda: False)
    stopped = SimpleNamespace(done=lambda: True)
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            critical_background_tasks={
                "job_worker": stopped,
                "paper_simulation_scheduler": running,
            }
        )
    )

    assert worker_module.stopped_critical_background_tasks(fake_app) == [
        "job_worker"
    ]


def test_health_endpoint_returns_503_after_critical_task_stops() -> None:
    async def scenario() -> None:
        fake_app = SimpleNamespace(
            state=SimpleNamespace(
                critical_background_tasks={
                    "job_worker": SimpleNamespace(done=lambda: True),
                }
            )
        )
        response = Response()
        payload = await health_check(
            SimpleNamespace(app=fake_app),  # type: ignore[arg-type]
            response,
        )

        assert response.status_code == 503
        assert payload["status"] == "degraded"
        assert payload["critical_processes"] == {
            "healthy": False,
            "stopped": ["job_worker"],
            "unhealthy": [],
            "job_worker_heartbeat": {
                "healthy": True,
                "online": False,
                "startup_grace": True,
                "standby": False,
                "heartbeat_at": None,
            },
        }

    asyncio.run(scenario())


def test_shutdown_cleans_all_tasks_and_broker_after_worker_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        active_stopped = asyncio.Event()

        async def failed_worker() -> None:
            raise RuntimeError("worker failed before shutdown")

        async def active_scheduler() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                active_stopped.set()

        class Broker:
            def __init__(self) -> None:
                self.shutdown_called = False

            async def shutdown(self) -> None:
                self.shutdown_called = True

        worker_task = asyncio.create_task(failed_worker())
        scheduler_task = asyncio.create_task(active_scheduler())
        await asyncio.sleep(0)
        assert worker_task.done()
        broker = Broker()

        await worker_module.shutdown_background_runtime(
            {
                "job_worker": worker_task,
                "paper_simulation_scheduler": scheduler_task,
            },
            broker,
        )

        assert active_stopped.is_set()
        assert scheduler_task.cancelled()
        assert broker.shutdown_called is True

    with caplog.at_level(logging.ERROR, logger="quant_platform"):
        asyncio.run(scenario())
    assert "job_worker exited with an error before shutdown" in caplog.text


def test_health_uses_in_memory_heartbeat_with_grace_and_standby() -> None:
    class Broker:
        def __init__(self, snapshot: dict) -> None:
            self.snapshot = snapshot

        def worker_health_snapshot(self) -> dict:
            return self.snapshot

    async def check(snapshot: dict, started_at: float) -> tuple[int, dict]:
        fake_app = SimpleNamespace(
            state=SimpleNamespace(
                critical_background_tasks={
                    "job_worker": SimpleNamespace(done=lambda: False),
                },
                critical_background_started_monotonic=started_at,
                job_broker=Broker(snapshot),
            )
        )
        response = Response()
        payload = await health_check(
            SimpleNamespace(app=fake_app),  # type: ignore[arg-type]
            response,
        )
        return response.status_code, payload

    offline_leader = {
        "online": False,
        "leader": True,
        "reasons": [],
        "heartbeat_at": "2026-07-30T00:00:00+00:00",
    }
    status, payload = asyncio.run(check(offline_leader, 0))
    assert status == 503
    assert payload["critical_processes"]["unhealthy"] == [
        "job_worker_heartbeat"
    ]

    # A just-started worker gets one lease-length grace period.
    status, payload = asyncio.run(check(offline_leader, time.monotonic()))
    assert status == 200
    assert payload["critical_processes"]["job_worker_heartbeat"]["startup_grace"]

    legal_standby = {
        "online": False,
        "leader": False,
        "reasons": ["scheduler_lease_held_by_other_process"],
        "heartbeat_at": None,
    }
    status, payload = asyncio.run(check(legal_standby, 0))
    assert status == 200
    assert payload["critical_processes"]["job_worker_heartbeat"]["standby"]


def test_health_returns_503_only_while_an_active_claim_lease_is_expired(
    tmp_path,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        broker.mark_worker_started()
        broker.set_scheduler_status(leader=True, reasons=[])
        job_uuid = await broker.submit_job(
            "backtest",
            {"experiment_id": 1},
            user_id=1,
        )
        assert await broker.claim_job(
            job_uuid,
            worker_id="host:5697:scheduler:secret-owner",
            lease_seconds=30,
        )
        fake_app = SimpleNamespace(
            state=SimpleNamespace(
                critical_background_tasks={
                    "job_worker": SimpleNamespace(done=lambda: False),
                },
                critical_background_started_monotonic=0,
                job_broker=broker,
            )
        )

        live_response = Response()
        live_payload = await health_check(
            SimpleNamespace(app=fake_app),  # type: ignore[arg-type]
            live_response,
        )
        assert live_response.status_code == 200
        assert live_payload["job_claim_leases"] == {
            "healthy": True,
            "expired_count": 0,
            "sample": [],
        }

        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET lease_expires_at='2000-01-01 00:00:00' "
                "WHERE job_uuid=?",
                (job_uuid,),
            )
            conn.commit()
        stale_response = Response()
        stale_payload = await health_check(
            SimpleNamespace(app=fake_app),  # type: ignore[arg-type]
            stale_response,
        )
        assert stale_response.status_code == 503
        assert stale_payload["status"] == "degraded"
        assert "expired_job_claims" in stale_payload["critical_processes"][
            "unhealthy"
        ]
        assert stale_payload["job_claim_leases"]["expired_count"] == 1
        serialized_snapshot = repr(stale_payload["job_claim_leases"])
        assert job_uuid not in serialized_snapshot
        assert "secret-owner" not in serialized_snapshot

        await broker.recover_expired_claims()
        recovered_response = Response()
        recovered_payload = await health_check(
            SimpleNamespace(app=fake_app),  # type: ignore[arg-type]
            recovered_response,
        )
        assert recovered_response.status_code == 200
        assert recovered_payload["job_claim_leases"]["healthy"] is True

    asyncio.run(scenario())
