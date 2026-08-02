from __future__ import annotations

import asyncio
import math
import sqlite3

import pytest
from fastapi import HTTPException

from backend.api.jobs import _ensure_retry_permission
from backend.core.security_boundaries import UnsafePayloadError
from backend.jobs.broker import (
    JobBroker,
    JobCancelledError,
    JobQueueFullError,
    sanitize_job_payload,
)


def test_research_activation_commit_point_rejects_late_cancellation(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        committed = await broker.submit_job(
            "research_data_refresh",
            {"source_id": "tushare"},
            user_id=1,
        )
        assert await broker.claim_job(committed, worker_id="worker-a") is True
        await broker.update_job_progress(
            committed,
            progress=0.99,
            stage="research_import_activate",
        )
        assert await broker.request_cancel(committed) is None
        assert (await broker.get_job_status(committed))["status"] == "running"

        early = await broker.submit_job(
            "research_data_refresh",
            {"source_id": "tushare", "scheduler_attempt": 2},
            user_id=1,
        )
        assert await broker.claim_job(early, worker_id="worker-b") is True
        assert await broker.request_cancel(early) == "cancel_requested"
        await broker.update_job_progress(
            early,
            progress=0.99,
            stage="research_import_activate",
        )
        with pytest.raises(JobCancelledError):
            await broker.raise_if_cancelled(early)
        assert (await broker.get_job_status(early))["status"] == "cancelled"

    asyncio.run(scenario())


def test_legacy_job_table_is_migrated(tmp_path):
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                job_type TEXT NOT NULL,
                params TEXT,
                status TEXT,
                progress REAL,
                user_id INTEGER,
                job_uuid TEXT UNIQUE NOT NULL,
                created_at TEXT
            )
            """
        )
    broker = JobBroker(str(db_path))
    with broker._get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert {"display_name", "resource_type", "progress_message", "updated_at"} <= columns


def test_job_lifecycle_pagination_and_retry(tmp_path):
    async def scenario():
        broker = JobBroker(str(tmp_path / "jobs.db"))
        first = await broker.submit_job(
            "backtest",
            {"experiment_id": 42},
            user_id=7,
            display_name="测试回测",
        )
        await broker.submit_job("data_update", {"pool_id": "csi500"}, user_id=8)

        jobs, total = await broker.query_jobs(user_id=7, page=1, page_size=10)
        assert total == 1
        assert jobs[0]["job_uuid"] == first
        assert jobs[0]["resource_type"] == "experiment"
        assert jobs[0]["resource_id"] == "42"
        runtime_evidence = jobs[0]["runtime_code_identity"]
        assert runtime_evidence["identity"]["sha"]
        assert runtime_evidence["code_version"]
        assert "observed_worktree_drift" in runtime_evidence
        # Queue position reflects dispatch priority, not insertion order.
        assert jobs[0]["queue_position"] == 2

        assert await broker.claim_job(first) is True
        assert await broker.request_cancel(first) == "cancel_requested"
        await broker.update_job_progress(first, progress=1, status="completed")
        cancelled = await broker.get_job_status(first)
        assert cancelled["status"] == "cancelled"

        retried = await broker.retry_job(first, user_id=7)
        assert retried is not None
        retry = await broker.get_job_status(retried)
        assert retry["parent_job_uuid"] == first
        assert retry["attempt"] == 2
        assert retry["runtime_code_identity"]["identity"]["sha"]

        events = await broker.list_job_events(first)
        assert any(event["status"] == "cancel_requested" for event in events)
        assert any(event["status"] == "cancelled" for event in events)

    asyncio.run(scenario())


def test_job_summary_and_sensitive_params(tmp_path):
    async def scenario():
        broker = JobBroker(str(tmp_path / "jobs.db"))
        await broker.submit_job("data_update", {}, user_id=3)
        summary = await broker.get_summary(user_id=3)
        assert summary["active"] == 1
        assert summary["counts"]["pending"] == 1
        assert summary["worker"]["online"] is False
        broker.mark_worker_started()
        assert (await broker.get_summary(user_id=3))["worker"]["online"] is True

    asyncio.run(scenario())
    assert sanitize_job_payload({"api_token": "secret", "nested": {"password": "x"}}) == {
        "api_token": "***",
        "nested": {"password": "***"},
    }


def test_public_job_payload_redacts_paths_and_inline_credentials() -> None:
    payload = sanitize_job_payload(
        {
            "checkpoint": "/Users/example/private/checkpoint.json",
            "message": (
                "failed at /Users/example/project/worker.py "
                "Authorization=Bearer abc.def.ghi"
            ),
        }
    )
    assert payload["checkpoint"] == "<internal-path>"
    assert "/Users/example" not in payload["message"]
    assert "abc.def.ghi" not in payload["message"]


def test_submission_rejects_secret_nonfinite_and_deep_params(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        with pytest.raises(UnsafePayloadError) as secret:
            await broker.submit_job(
                "backtest",
                {"experiment_id": 1, "api_token": "must-not-persist"},
                user_id=7,
            )
        assert secret.value.code == "payload_secret_key_forbidden"
        with pytest.raises(UnsafePayloadError) as nonfinite:
            await broker.submit_job(
                "backtest",
                {"experiment_id": 1, "score": math.nan},
                user_id=7,
            )
        assert nonfinite.value.code == "payload_non_finite_number"
        nested: dict[str, object] = {}
        cursor = nested
        for _ in range(14):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        with pytest.raises(UnsafePayloadError) as deep:
            await broker.submit_job("backtest", nested, user_id=7)
        assert deep.value.code == "payload_depth_limit_exceeded"

    asyncio.run(scenario())


def test_progress_storage_is_bounded_sanitized_and_monotonic(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_id = await broker.submit_job(
            "backtest", {"experiment_id": 1}, user_id=7
        )
        assert await broker.claim_job(job_id) is True
        await broker.update_job_progress(
            job_id,
            status="failed",
            error=(
                "Traceback /Users/example/project/main.py "
                "password=plain-text-secret"
            ),
            message="failed /Users/example/project/main.py",
            stage="failed",
        )
        row = await broker.get_job_status(job_id)
        assert "/Users/example" not in row["error"]
        assert "plain-text-secret" not in row["error"]
        assert "/Users/example" not in row["progress_message"]

        running = await broker.submit_job(
            "backtest", {"experiment_id": 2}, user_id=7
        )
        assert await broker.claim_job(running) is True
        with pytest.raises(UnsafePayloadError) as transition:
            await broker.update_job_progress(running, status="pending")
        assert transition.value.code == "job_status_transition_invalid"
        with pytest.raises(UnsafePayloadError) as bad_progress:
            await broker.update_job_progress(running, progress=math.inf)
        assert bad_progress.value.code == "job_progress_non_finite"

    asyncio.run(scenario())


def test_per_user_active_limit_is_atomic(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        monkeypatch.setattr(
            "backend.jobs.broker.settings.JOB_SCHEDULER_MAX_ACTIVE_PER_USER",
            2,
        )
        await broker.submit_job(
            "backtest", {"experiment_id": 1}, user_id=7
        )
        await broker.submit_job(
            "backtest", {"experiment_id": 2}, user_id=7
        )
        with pytest.raises(JobQueueFullError):
            await broker.submit_job(
                "backtest", {"experiment_id": 3}, user_id=7
            )
        # Another account retains its own capacity.
        await broker.submit_job(
            "backtest", {"experiment_id": 4}, user_id=8
        )
        jobs, total = await broker.query_jobs(
            include_all=True, include_system=True, page_size=20
        )
        assert total == 3
        assert {job["user_id"] for job in jobs} == {7, 8}

    asyncio.run(scenario())


def test_batch_submission_is_all_or_nothing_and_wakes_after_commit(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        broker = JobBroker(str(tmp_path / "jobs.db"))
        monkeypatch.setattr(
            "backend.jobs.broker.settings.JOB_SCHEDULER_MAX_PENDING_JOBS",
            1,
        )
        submissions = [
            {
                "job_type": "backtest",
                "params": {"experiment_id": experiment_id, "sweep_id": 8},
                "user_id": 7,
            }
            for experiment_id in (11, 12)
        ]
        with pytest.raises(Exception, match="剩余容量不足"):
            await broker.submit_jobs_batch(submissions)
        jobs, total = await broker.query_jobs(
            include_all=True,
            include_system=True,
            page_size=20,
        )
        assert jobs == []
        assert total == 0
        assert broker._wake_event.is_set() is False

        monkeypatch.setattr(
            "backend.jobs.broker.settings.JOB_SCHEDULER_MAX_PENDING_JOBS",
            10,
        )
        job_ids = await broker.submit_jobs_batch(submissions)
        assert len(job_ids) == 2
        assert broker._wake_event.is_set() is True

    asyncio.run(scenario())


def test_progress_update_retries_only_sqlite_busy(tmp_path, monkeypatch):
    async def scenario():
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_id = await broker.submit_job("backtest", {"experiment_id": 42})
        original = broker._update_job_progress_once
        attempts = 0

        async def flaky(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise sqlite3.OperationalError("database is locked")
            return await original(*args, **kwargs)

        monkeypatch.setattr(broker, "_update_job_progress_once", flaky)
        monkeypatch.setattr("backend.jobs.broker.asyncio.sleep", _no_sleep)
        await broker.update_job_progress(job_id, progress=0.5)
        assert attempts == 3
        assert (await broker.get_job_status(job_id))["progress"] == 0.5

        async def defect(*_args, **_kwargs):
            raise sqlite3.OperationalError("no such column: broken")

        monkeypatch.setattr(broker, "_update_job_progress_once", defect)
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await broker.update_job_progress(job_id, progress=0.6)

    async def _no_sleep(_delay):
        return None

    asyncio.run(scenario())


def test_pending_sweep_job_cancellation_updates_experiment_and_summary(
    tmp_path,
):
    async def scenario():
        broker = JobBroker(str(tmp_path / "jobs.db"))
        with broker._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress_message TEXT,
                    completed_at TEXT
                );
                CREATE TABLE param_sweeps (
                    id INTEGER PRIMARY KEY,
                    total_experiments INTEGER NOT NULL,
                    completed_experiments INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL
                );
                CREATE TABLE sweep_experiments (
                    sweep_id INTEGER NOT NULL,
                    experiment_id INTEGER NOT NULL
                );
                INSERT INTO experiments (id, status)
                VALUES (41, 'pending'), (42, 'pending');
                INSERT INTO param_sweeps
                    (id, total_experiments, completed_experiments, status)
                VALUES (7, 2, 0, 'pending');
                INSERT INTO sweep_experiments (sweep_id, experiment_id)
                VALUES (7, 41), (7, 42);
                """
            )

        first = await broker.submit_job(
            "backtest",
            {"experiment_id": 41, "sweep_id": 7},
            user_id=7,
        )
        second = await broker.submit_job(
            "backtest",
            {"experiment_id": 42, "sweep_id": 7},
            user_id=7,
        )

        assert await broker.request_cancel(first) == "cancelled"
        with broker._get_conn() as conn:
            assert conn.execute(
                "SELECT status FROM experiments WHERE id=41"
            ).fetchone()["status"] == "cancelled"
            partial = conn.execute(
                """
                SELECT status, completed_experiments
                FROM param_sweeps WHERE id=7
                """
            ).fetchone()
            assert (partial["status"], partial["completed_experiments"]) == (
                "running",
                1,
            )

        assert await broker.request_cancel(second) == "cancelled"
        with broker._get_conn() as conn:
            terminal = conn.execute(
                """
                SELECT status, completed_experiments
                FROM param_sweeps WHERE id=7
                """
            ).fetchone()
            assert (terminal["status"], terminal["completed_experiments"]) == (
                "completed",
                2,
            )

    asyncio.run(scenario())


def test_retry_is_single_claim_under_concurrency(tmp_path):
    async def scenario():
        broker = JobBroker(str(tmp_path / "jobs.db"))
        source = await broker.submit_job(
            "backtest",
            {"experiment_id": 42},
            user_id=7,
        )
        assert await broker.claim_job(source) is True
        await broker.update_job_progress(source, progress=1, status="failed")

        results = await asyncio.gather(
            broker.retry_job(source, user_id=7),
            broker.retry_job(source, user_id=7),
        )
        created = [item for item in results if item is not None]
        assert len(created) == 1

        with broker._get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE parent_job_uuid = ?",
                (source,),
            ).fetchone()[0]
        assert count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("job_type", "permission"),
    [
        ("backtest", "experiments:create"),
        ("daily_simulation", "trading:execute"),
        ("simulation_backfill", "trading:execute"),
        ("data_update", "data:update"),
        ("retrain", "trading:deploy"),
    ],
)
def test_retry_requires_current_job_type_permission(job_type, permission):
    job = {"job_type": job_type}
    _ensure_retry_permission(
        job,
        {"id": 7, "is_admin": False, "permissions": [permission]},
    )

    with pytest.raises(HTTPException) as denied:
        _ensure_retry_permission(
            job,
            {"id": 7, "is_admin": False, "permissions": []},
        )
    assert denied.value.status_code == 403
    assert permission in str(denied.value.detail)


def test_unknown_job_type_cannot_be_retried_via_api():
    with pytest.raises(HTTPException) as denied:
        _ensure_retry_permission(
            {"job_type": "future_live_order"},
            {"id": 7, "is_admin": True, "permissions": []},
        )
    assert denied.value.status_code == 403


def test_completed_job_dependency_fails_closed(tmp_path):
    async def scenario():
        broker = JobBroker(str(tmp_path / "jobs.db"))
        dependency = await broker.submit_job("data_update", {}, user_id=None)

        with pytest.raises(RuntimeError, match="expected completed"):
            await broker.require_completed_job(
                dependency,
                expected_type="data_update",
            )

        assert await broker.claim_job(dependency) is True
        await broker.update_job_progress(
            dependency,
            progress=1,
            status="completed",
            result={"updated": True},
        )
        completed = await broker.require_completed_job(
            dependency,
            expected_type="data_update",
        )
        assert completed["result"] == {"updated": True}

        with pytest.raises(RuntimeError, match="expected backtest"):
            await broker.require_completed_job(
                dependency,
                expected_type="backtest",
            )

    asyncio.run(scenario())


def test_recovery_reconciles_cancelled_backtest_experiment(tmp_path):
    async def scenario():
        db_path = tmp_path / "jobs.db"
        broker = JobBroker(str(db_path))
        with broker._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress_message TEXT,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO experiments (id, status, progress_message)
                VALUES (42, 'running', '训练中')
                """
            )
            conn.commit()

        job_uuid = await broker.submit_job(
            "backtest",
            {"experiment_id": 42},
            user_id=7,
        )
        assert await broker.claim_job(job_uuid) is True
        assert await broker.request_cancel(job_uuid) == "cancel_requested"

        # A cancellation request must not steal a still-live execution lease.
        assert await broker.recover_pending_jobs() == 0
        assert (await broker.get_job_status(job_uuid))["status"] == (
            "cancel_requested"
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET lease_expires_at='2000-01-01 00:00:00' "
                "WHERE job_uuid=?",
                (job_uuid,),
            )
            conn.commit()
        assert await broker.recover_pending_jobs() == 0
        recovered = await broker.get_job_status(job_uuid)
        assert recovered is not None
        assert recovered["status"] == "cancelled"

        with broker._get_conn() as conn:
            experiment = conn.execute(
                """
                SELECT status, progress_message, completed_at
                FROM experiments WHERE id = 42
                """
            ).fetchone()
        assert experiment["status"] == "cancelled"
        assert experiment["progress_message"] == "任务租约已过期，已完成取消"
        assert experiment["completed_at"] is not None

        # A historical cancelled parent must not cancel a later retry of the
        # same experiment during every subsequent service startup.
        with broker._get_conn() as conn:
            conn.execute(
                """
                UPDATE experiments
                SET status = 'running', progress_message = 'retry running'
                WHERE id = 42
                """
            )
            conn.commit()
        assert await broker.recover_pending_jobs() == 0
        with broker._get_conn() as conn:
            retried_experiment = conn.execute(
                "SELECT status FROM experiments WHERE id = 42"
            ).fetchone()
        assert retried_experiment["status"] == "running"

    asyncio.run(scenario())
