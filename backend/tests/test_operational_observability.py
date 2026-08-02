from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import Response

import backend.main as main_module
from backend.api import jobs as jobs_api
from backend.config import settings
from backend.jobs.broker import JobBroker
from backend.jobs.resources import (
    AdaptiveCapacityController,
    SystemLoadSnapshot,
)
from backend.strategies.ml import (
    alpha158_lgb,
    alpha158_rank_lgb,
    alpha158_xgb,
    transformer_rank,
)


class _Provider:
    def __init__(self, snapshot: SystemLoadSnapshot) -> None:
        self.snapshot = snapshot

    def sample(self) -> SystemLoadSnapshot:
        return self.snapshot


def _snapshot(**changes: float | int | None) -> SystemLoadSnapshot:
    values = {
        "cpu_count": 8,
        "load_1m": 2.0,
        "normalized_load": 0.25,
        "memory_total_mb": 8192.0,
        "memory_available_mb": 4096.0,
        "memory_used_ratio": 0.5,
        "swap_used_mb": 0.0,
        "source": "test",
        "disk_free_mb": 8192.0,
        "io_pressure": 0.1,
        "io_source": "test",
    }
    values.update(changes)
    return SystemLoadSnapshot(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"normalized_load": 1.2, "load_1m": 9.6}, "cpu_budget_exhausted"),
        (
            {"memory_available_mb": 512.0, "memory_used_ratio": 0.94},
            "memory_budget_exhausted",
        ),
        ({"disk_free_mb": 1024.0}, "io_budget_exhausted"),
        ({"io_pressure": 0.95}, "io_budget_exhausted"),
    ],
)
def test_critical_resource_pressure_pauses_new_heavy_jobs(
    changes: dict[str, float],
    reason: str,
) -> None:
    decision = AdaptiveCapacityController(
        _Provider(_snapshot(**changes))
    ).decide()

    assert decision.capacity == 1
    assert decision.pause_heavy is True
    assert decision.admission_mode == "pause_heavy"
    assert reason in decision.reasons


def test_pressure_pauses_heavy_job_but_allows_light_job(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        heavy = await broker.submit_job(
            "retrain",
            {"deployment_id": 3},
            user_id=1,
        )
        light = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 4,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001"],
            },
            user_id=1,
        )
        broker.set_scheduler_status(
            leader=True,
            pause_heavy=True,
            admission_mode="pause_heavy",
            reasons=["memory_budget_exhausted"],
        )

        selected = await broker.claim_next_job(worker_id="test-worker")
        assert selected is not None
        assert selected["job_uuid"] == light
        queued = await broker.get_job_status(heavy)
        assert queued is not None
        assert queued["status"] == "pending"
        assert queued["queue_reason"] == "resource_pressure_heavy_jobs_paused"

    asyncio.run(scenario())


def test_observability_survives_restart_and_uses_bounded_labels(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        db_path = str(tmp_path / "jobs.db")
        broker = JobBroker(db_path)
        job_uuid = await broker.submit_job(
            "factor_research",
            {"factor_id": "momentum_20"},
            user_id=99,
        )
        assert await broker.claim_job(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=0.5,
            stage="future_stage",
            message="half",
        )
        await broker.update_job_progress(
            job_uuid,
            progress=1,
            status="completed",
            stage="completed",
        )
        await broker.record_operational_event(
            "websocket_connected",
            "websocket",
            outcome="connected",
            stage="jobs",
        )
        broker.note_sqlite_contention()
        await broker.flush_operational_counters()

        restarted = JobBroker(db_path)
        payload = await restarted.get_observability(window_hours=24)
        assert payload["jobs"]["by_type"]["factor_research"]["completed"] == 1
        assert payload["jobs"]["by_type"]["factor_research"]["success_rate"] == 1
        assert payload["slo"]["schema_version"] == "operations-slo/v1"
        assert payload["slo"]["objectives"]["sqlite_contention_events"]["actual"] == 1
        encoded = str(payload)
        assert "future_stage" not in encoded
        assert "99" not in payload["labels"]["allowed"]

    monkeypatch.setattr(settings, "JOB_OBSERVABILITY_RETENTION_HOURS", 168)
    asyncio.run(scenario())


def test_slo_alerts_are_debounced_audited_and_cooled_down(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        db_path = str(tmp_path / "jobs.db")
        broker = JobBroker(db_path)
        await broker.record_operational_event(
            "sqlite_contention",
            "storage",
            value=6,
        )

        await broker.evaluate_slo_alerts(window_hours=24)
        first = await broker.get_observability(window_hours=24)
        state = first["slo"]["alerting"]["states"]["sqlite_contention_events"]
        assert state["status"] == "healthy"
        assert state["pending_status"] == "breaching"
        assert state["consecutive_observations"] == 1
        assert first["slo"]["alerting"]["recent"] == []

        await broker.evaluate_slo_alerts(window_hours=24)
        breached = await broker.get_observability(window_hours=24)
        assert breached["slo"]["alerting"]["states"][
            "sqlite_contention_events"
        ]["status"] == "breaching"
        assert breached["slo"]["alerting"]["recent"][0] == {
            "objective": "sqlite_contention_events",
            "transition": "breach",
            "actual": 6.0,
            "threshold": 5.0,
            "window_hours": 24,
            "notification_emitted": True,
            "created_at": breached["slo"]["alerting"]["recent"][0]["created_at"],
        }

        conn = sqlite3.connect(db_path)
        conn.execute(
            "DELETE FROM operational_events WHERE event_name='sqlite_contention'"
        )
        conn.commit()
        conn.close()
        await broker.evaluate_slo_alerts(window_hours=24)
        await broker.evaluate_slo_alerts(window_hours=24)
        recovered = await broker.get_observability(window_hours=24)
        assert recovered["slo"]["alerting"]["recent"][0]["transition"] == "recovery"
        assert recovered["slo"]["alerting"]["recent"][0][
            "notification_emitted"
        ] is True

        await broker.record_operational_event(
            "sqlite_contention",
            "storage",
            value=6,
        )
        await broker.evaluate_slo_alerts(window_hours=24)
        await broker.evaluate_slo_alerts(window_hours=24)
        cooled = await broker.get_observability(window_hours=24)
        assert cooled["slo"]["alerting"]["recent"][0]["transition"] == "breach"
        assert cooled["slo"]["alerting"]["recent"][0][
            "notification_emitted"
        ] is False
        assert len(cooled["slo"]["alerting"]["recent"]) == 3

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO slo_alert_events
                (objective, transition, actual, threshold, window_hours)
            VALUES ('/private/user/token', 'breach', 1, 0, 24)
            """
        )
        conn.commit()
        conn.close()
        bounded = await broker.get_observability(window_hours=24)
        assert "/private/user/token" not in str(bounded)

    monkeypatch.setattr(settings, "JOB_SLO_CONFIRMATIONS_REQUIRED", 2)
    monkeypatch.setattr(settings, "JOB_SLO_ALERT_COOLDOWN_SECONDS", 3600)
    caplog.set_level(logging.INFO, logger="quant_platform.jobs")
    asyncio.run(scenario())
    alert_logs = [
        record.message
        for record in caplog.records
        if '"event": "slo_alert"' in record.message
    ]
    assert len(alert_logs) == 2
    assert all("sqlite_contention_events" in message for message in alert_logs)
    assert all("token" not in message for message in alert_logs)


def test_observability_api_is_admin_only_and_read_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        monkeypatch.setattr(jobs_api, "get_job_broker", lambda: broker)
        monkeypatch.setattr(
            jobs_api,
            "cache_quality_snapshot",
            lambda: {
                "schema_version": "cache-quality-summary/v1",
                "counts": {"total": 0},
                "source_trust": {},
            },
        )

        with pytest.raises(HTTPException) as denied:
            await jobs_api.get_job_observability(
                window_hours=24,
                user={"id": 1, "is_admin": False},
            )
        assert denied.value.status_code == 403

        before = (tmp_path / "jobs.db").stat().st_size
        response = await jobs_api.get_job_observability(
            window_hours=24,
            user={"id": 1, "is_admin": True},
        )
        assert response["data"]["schema_version"] == "operations-observability/v1"
        assert response["data"]["cache_quality"]["counts"]["total"] == 0
        assert (tmp_path / "jobs.db").stat().st_size == before

    asyncio.run(scenario())


def test_ml_execution_budget_has_no_unbounded_worker_fanout() -> None:
    for module in (
        alpha158_lgb,
        alpha158_rank_lgb,
        alpha158_xgb,
        transformer_rank,
    ):
        source = inspect.getsource(module)
        assert "n_jobs=-1" not in source
        assert "JOB_CPU_THREAD_BUDGET" in source


def test_clean_ml_runtime_exit_has_no_resource_tracker_leak() -> None:
    script = """
import multiprocessing
from backend.config import settings
from backend.strategies.ml import alpha158_lgb, alpha158_rank_lgb
from backend.strategies.ml import alpha158_xgb, transformer_rank
assert settings.JOB_CPU_THREAD_BUDGET >= 1
assert multiprocessing.active_children() == []
print("clean-exit")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=settings.PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "clean-exit"
    assert "resource_tracker" not in result.stderr
    assert "leaked semaphore" not in result.stderr


def test_watchdog_health_exposes_budget_without_treating_pressure_as_crash() -> None:
    class _Broker:
        def worker_health_snapshot(self) -> dict[str, object]:
            return {
                "online": True,
                "leader": True,
                "heartbeat_at": "2026-07-31T00:00:00Z",
                "admission_mode": "pause_heavy",
                "pause_heavy": True,
                "desired_capacity": 1,
                "running_slots": 1,
                "reasons": ["memory_budget_exhausted"],
                "metrics": {"memory_available_mb": 600},
            }

    async def scenario() -> None:
        fake_app = SimpleNamespace(
            state=SimpleNamespace(
                critical_background_tasks={
                    "job_worker": SimpleNamespace(done=lambda: False),
                },
                critical_background_started_monotonic=0,
                job_broker=_Broker(),
            )
        )
        response = Response()
        payload = await main_module.health_check(
            SimpleNamespace(app=fake_app),  # type: ignore[arg-type]
            response,
        )
        assert response.status_code == 200
        assert payload["status"] == "ok"
        assert payload["resource_budget"] == {
            "admission_mode": "pause_heavy",
            "pause_heavy": True,
            "capacity": 1,
            "running_slots": 1,
            "reasons": ["memory_budget_exhausted"],
            "metrics": {"memory_available_mb": 600},
        }

    asyncio.run(scenario())


def test_sqlite_contention_telemetry_is_deferred_without_losing_api_control(
    tmp_path,
) -> None:
    async def scenario() -> None:
        db_path = str(tmp_path / "jobs.db")
        broker = JobBroker(db_path)
        blocker = sqlite3.connect(db_path, timeout=0)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            await asyncio.wait_for(
                broker.record_operational_event(
                    "websocket_connected",
                    "websocket",
                    outcome="connected",
                    stage="jobs",
                ),
                timeout=1,
            )
            assert broker._pending_sqlite_contention == 1
        finally:
            blocker.rollback()
            blocker.close()

        await broker.flush_operational_counters()
        payload = await broker.get_observability(window_hours=1)
        objective = payload["slo"]["objectives"]["sqlite_contention_events"]
        assert objective["actual"] == 1

    asyncio.run(scenario())
