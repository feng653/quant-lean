from __future__ import annotations

import asyncio
import ctypes
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

import backend.jobs.broker as broker_module
import backend.jobs.scheduler as scheduler_module
from backend.config import settings
from backend.jobs.broker import (
    JobBroker,
    JobLeaseLostError,
    JobQueueFullError,
)
from backend.jobs.resources import (
    AdaptiveCapacityController,
    CapacityDecision,
    SystemLoadSnapshot,
)
from backend.jobs.scheduler import AdaptiveJobScheduler


class _FakeWindowsKernel32:
    def __init__(
        self,
        *,
        handle: int = 123,
        exit_code: int = 259,
        last_error: int = 0,
        exit_query_succeeds: bool = True,
    ) -> None:
        self.handle = handle
        self.exit_code = exit_code
        self.last_error = last_error
        self.exit_query_succeeds = exit_query_succeeds
        self.opened: list[tuple[int, bool, int]] = []
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        self.opened.append((access, inherit, pid))
        return self.handle

    def GetLastError(self) -> int:
        return self.last_error

    def GetExitCodeProcess(self, handle: int, exit_code_pointer: object) -> bool:
        assert handle == self.handle
        ctypes.cast(
            exit_code_pointer,
            ctypes.POINTER(ctypes.c_ulong),
        ).contents.value = self.exit_code
        return self.exit_query_succeeds

    def CloseHandle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True


def _snapshot(
    *,
    load: float | None = 0.2,
    available_mb: float | None = 4096,
    used_ratio: float | None = 0.5,
    swap_mb: float | None = 0,
    cpu_count: int = 8,
) -> SystemLoadSnapshot:
    return SystemLoadSnapshot(
        cpu_count=cpu_count,
        load_1m=None if load is None else load * cpu_count,
        normalized_load=load,
        memory_total_mb=8192,
        memory_available_mb=available_mb,
        memory_used_ratio=used_ratio,
        swap_used_mb=swap_mb,
        source="test",
    )


class _SequenceProvider:
    def __init__(self, snapshots: list[SystemLoadSnapshot]) -> None:
        self._snapshots: Iterator[SystemLoadSnapshot] = iter(snapshots)
        self._last = snapshots[-1]

    def sample(self) -> SystemLoadSnapshot:
        self._last = next(self._snapshots, self._last)
        return self._last


def _seed_running_sweep(
    broker: JobBroker,
    *,
    experiment_id: int = 1,
    sweep_id: int = 7,
) -> None:
    with broker._get_conn() as conn:
        conn.executescript(
            f"""
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                progress_pct REAL DEFAULT 0,
                progress_message TEXT,
                error_log TEXT,
                completed_at TEXT
            );
            CREATE TABLE param_sweeps (
                id INTEGER PRIMARY KEY,
                total_experiments INTEGER,
                completed_experiments INTEGER DEFAULT 0,
                status TEXT
            );
            CREATE TABLE sweep_experiments (
                sweep_id INTEGER,
                experiment_id INTEGER
            );
            INSERT INTO experiments (id, status)
            VALUES ({experiment_id}, 'running');
            INSERT INTO param_sweeps
                (id, total_experiments, completed_experiments, status)
            VALUES ({sweep_id}, 1, 0, 'running');
            INSERT INTO sweep_experiments (sweep_id, experiment_id)
            VALUES ({sweep_id}, {experiment_id});
            """
        )
        conn.commit()


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(259, True), (0, False)],
)
def test_windows_process_probe_queries_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    expected: bool,
) -> None:
    kernel32 = _FakeWindowsKernel32(exit_code=exit_code)
    monkeypatch.setattr(
        broker_module,
        "_load_windows_kernel32",
        lambda: kernel32,
    )

    assert broker_module._process_alive(4321, platform_name="win32") is expected
    assert kernel32.opened == [(0x1000, False, 4321)]
    assert kernel32.closed == [123]


@pytest.mark.parametrize(
    ("last_error", "expected"),
    [(5, True), (87, False), (1234, True)],
)
def test_windows_process_probe_handles_open_failures_conservatively(
    monkeypatch: pytest.MonkeyPatch,
    last_error: int,
    expected: bool,
) -> None:
    kernel32 = _FakeWindowsKernel32(handle=0, last_error=last_error)
    monkeypatch.setattr(
        broker_module,
        "_load_windows_kernel32",
        lambda: kernel32,
    )

    assert broker_module._process_alive(4321, platform_name="win32") is expected
    assert kernel32.closed == []


def test_windows_process_probe_closes_handle_after_exit_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeWindowsKernel32(exit_query_succeeds=False)
    monkeypatch.setattr(
        broker_module,
        "_load_windows_kernel32",
        lambda: kernel32,
    )

    assert broker_module._process_alive(4321, platform_name="win32")
    assert kernel32.closed == [123]


def test_windows_process_probe_never_calls_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeWindowsKernel32()
    monkeypatch.setattr(
        broker_module,
        "_load_windows_kernel32",
        lambda: kernel32,
    )

    def fail_if_called(pid: int, signal: int) -> None:
        raise AssertionError(f"destructive os.kill({pid}, {signal}) called")

    monkeypatch.setattr(broker_module.os, "kill", fail_if_called)
    assert broker_module._process_alive(4321, platform_name="win32")


def test_capacity_scales_up_only_after_stable_samples_and_drops_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "JOB_SCHEDULER_MAX_CONCURRENCY", 9)
    monkeypatch.setattr(settings, "JOB_SCHEDULER_SCALE_UP_SAMPLES", 2)
    controller = AdaptiveCapacityController(
        _SequenceProvider([_snapshot(), _snapshot(), _snapshot(load=0.95)])
    )

    warmup = controller.decide()
    scaled = controller.decide()
    pressured = controller.decide()

    assert warmup.capacity == 1
    assert warmup.reasons == ("scale_up_warmup",)
    assert scaled.capacity == 2
    assert scaled.configured_max == 2
    assert pressured.capacity == 1
    assert "cpu_load_high" in pressured.reasons


def test_capacity_fails_conservatively_when_memory_is_unavailable() -> None:
    controller = AdaptiveCapacityController(
        _SequenceProvider([_snapshot(available_mb=None, used_ratio=None)])
    )
    decision = controller.decide()
    assert decision.capacity == 1
    assert "memory_pressure_unavailable" in decision.reasons


def test_capacity_fails_conservatively_when_swap_is_unavailable() -> None:
    controller = AdaptiveCapacityController(
        _SequenceProvider([_snapshot(swap_mb=None)])
    )
    decision = controller.decide()
    assert decision.capacity == 1
    assert "swap_pressure_unavailable" in decision.reasons


def test_priority_dependency_and_sweep_fairness(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        sweep_one_a = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 1,
                "sweep_id": 10,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001", "000002"],
            },
            user_id=1,
        )
        sweep_one_b = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 2,
                "sweep_id": 10,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001", "000002"],
            },
            user_id=1,
        )
        sweep_two = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 3,
                "sweep_id": 11,
                "pool_preset": "custom",
                "pool_custom_codes": ["000003", "000004"],
            },
            user_id=1,
        )
        refresh = await broker.submit_job(
            "data_update",
            {"pool_id": "csi300", "source": "paper_scheduler"},
            user_id=None,
        )
        simulation = await broker.submit_job(
            "daily_simulation",
            {"user_id": 1, "required_data_job_uuid": refresh},
            user_id=1,
        )

        first = await broker.claim_next_job(worker_id="worker-1")
        assert first and first["job_uuid"] == refresh
        # The dependent simulation stays pending until refresh is completed.
        second = await broker.claim_next_job(worker_id="worker-2")
        assert second and second["job_uuid"] == sweep_one_a
        assert second["job_uuid"] != simulation

        await broker.update_job_progress(refresh, status="completed", progress=1)
        third = await broker.claim_next_job(worker_id="worker-3")
        assert third and third["job_uuid"] == simulation
        await broker.update_job_progress(simulation, status="completed", progress=1)

        # One sweep cannot occupy both slots; the other sweep advances first.
        fourth = await broker.claim_next_job(worker_id="worker-4")
        assert fourth and fourth["job_uuid"] == sweep_two
        assert (await broker.get_job_status(sweep_one_b))["status"] == "pending"

    asyncio.run(scenario())


def test_noncritical_priority_aging_prevents_interactive_starvation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_AGING_SECONDS", 1)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        retrain = await broker.submit_job(
            "retrain",
            {"deployment_id": 9, "user_id": 1},
            user_id=1,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET created_at=datetime('now', '-90 seconds') "
                "WHERE job_uuid=?",
                (retrain,),
            )
            conn.commit()
        interactive = await broker.submit_job(
            "backtest",
            {"experiment_id": 11, "pool_preset": "csi300"},
            user_id=1,
        )

        first = await broker.claim_next_job(worker_id="aged-worker")
        assert first and first["job_uuid"] == retrain
        jobs, _ = await broker.query_jobs(user_id=1, page_size=10)
        positions = {job["job_uuid"]: job["queue_position"] for job in jobs}
        assert positions[interactive] == 1

    asyncio.run(scenario())


def test_resource_mutex_and_exclusive_jobs(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        first_pool = await broker.submit_job(
            "data_update", {"pool_id": "csi300"}, user_id=1
        )
        same_pool = await broker.submit_job(
            "data_update", {"pool_id": "csi300"}, user_id=2
        )
        other_pool = await broker.submit_job(
            "data_update", {"pool_id": "csi500"}, user_id=3
        )
        same_pool_reader = await broker.submit_job(
            "backtest",
            {"experiment_id": 9, "pool_preset": "csi300"},
            user_id=4,
        )
        first = await broker.claim_next_job(worker_id="worker-1")
        second = await broker.claim_next_job(worker_id="worker-2")
        assert first and first["job_uuid"] == first_pool
        assert second is None
        assert (await broker.get_job_status(same_pool))["status"] == "pending"

        await broker.update_job_progress(first_pool, status="completed", progress=1)
        claimed_same_pool = await broker.claim_next_job(worker_id="worker-same")
        assert claimed_same_pool and claimed_same_pool["job_uuid"] == same_pool
        await broker.update_job_progress(same_pool, status="completed", progress=1)
        claimed_other_pool = await broker.claim_next_job(worker_id="worker-other")
        assert claimed_other_pool and claimed_other_pool["job_uuid"] == other_pool
        await broker.update_job_progress(other_pool, status="completed", progress=1)
        claimed_reader = await broker.claim_next_job(worker_id="worker-reader")
        assert claimed_reader and claimed_reader["job_uuid"] == same_pool_reader
        await broker.update_job_progress(
            same_pool_reader, status="completed", progress=1
        )
        backtest = await broker.submit_job(
            "backtest", {"experiment_id": 4}, user_id=1
        )
        backfill = await broker.submit_job(
            "simulation_backfill",
            {"user_id": 1, "start_date": "2026-01-01", "end_date": "2026-01-02"},
            user_id=1,
        )
        assert (await broker.claim_next_job(worker_id="worker-3"))["job_uuid"] == backtest
        assert await broker.claim_next_job(worker_id="worker-4") is None
        await broker.update_job_progress(backtest, status="completed", progress=1)
        assert (await broker.claim_next_job(worker_id="worker-5"))["job_uuid"] == backfill

    asyncio.run(scenario())


def test_backtest_dispatch_hydrates_only_missing_experiment_fields(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        with broker._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    pool_preset TEXT,
                    pool_custom_codes TEXT,
                    requires_training INTEGER
                );
                INSERT INTO experiments (
                    id, pool_preset, pool_custom_codes, requires_training
                )
                VALUES (17, 'csi500', NULL, 0);
                """
            )
            conn.commit()
        job_uuid = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 17,
                "pool_preset": "csi300",
            },
            user_id=1,
            resource_type="experiment",
            resource_id=17,
        )

        claimed = await broker.claim_next_job(worker_id="worker-1")

        assert claimed is not None
        assert claimed["job_uuid"] == job_uuid
        assert claimed["_dispatch_pool"] == "csi300"
        assert claimed["_dispatch_exclusive"] is True

    asyncio.run(scenario())


def test_missing_or_malformed_backtest_metadata_does_not_break_dispatch(
    tmp_path,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        with broker._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    pool_preset TEXT,
                    pool_custom_codes TEXT,
                    requires_training INTEGER
                );
                """
            )
            conn.commit()
        missing_experiment = await broker.submit_job(
            "backtest",
            {"experiment_id": 404},
            user_id=1,
            resource_type="experiment",
            resource_id=404,
        )
        malformed_params = await broker.submit_job(
            "backtest",
            {"experiment_id": 405},
            user_id=1,
            resource_type="experiment",
            resource_id=405,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET params='not-json' WHERE job_uuid=?",
                (malformed_params,),
            )
            conn.commit()

        first = await broker.claim_next_job(worker_id="worker-1")
        assert first is not None
        assert first["job_uuid"] == missing_experiment
        assert first["_dispatch_pool"] == "csi300"
        await broker.update_job_progress(
            missing_experiment, status="completed", progress=1
        )

        second = await broker.claim_next_job(worker_id="worker-2")
        assert second is not None
        assert second["job_uuid"] == malformed_params
        assert second["_dispatch_pool"] == "csi300"

    asyncio.run(scenario())


def test_heavy_backtests_run_alone_and_exclusive_queue_drains(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_AGING_SECONDS", 1)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        light_running = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 1,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001", "000002"],
            },
            user_id=1,
        )
        first = await broker.claim_next_job(worker_id="light-worker")
        assert first and first["job_uuid"] == light_running

        heavy = await broker.submit_job(
            "backtest",
            {"experiment_id": 2, "pool_preset": "csi300"},
            user_id=1,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET created_at=datetime('now', '-90 seconds') "
                "WHERE job_uuid=?",
                (heavy,),
            )
            conn.commit()
        later_light = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 3,
                "pool_preset": "custom",
                "pool_custom_codes": ["000003", "000004"],
            },
            user_id=1,
        )

        # The aged heavy job is at the front. Do not fill the second slot with
        # later light work; let the current light claim drain.
        assert await broker.claim_next_job(worker_id="drain-worker") is None
        assert (await broker.get_job_status(later_light))["status"] == "pending"
        await broker.update_job_progress(
            light_running, status="completed", progress=1
        )
        claimed_heavy = await broker.claim_next_job(worker_id="heavy-worker")
        assert claimed_heavy and claimed_heavy["job_uuid"] == heavy
        assert claimed_heavy["_dispatch_exclusive"] is True
        assert claimed_heavy["_dispatch_slots"] == 2
        assert await broker.claim_next_job(worker_id="blocked-worker") is None

    asyncio.run(scenario())


def test_factor_research_uses_two_exclusive_slots(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        factor_job = await broker.submit_job(
            "factor_research",
            {"factor_id": "momentum_20"},
            user_id=7,
        )
        claimed = await broker.claim_next_job(worker_id="factor-worker")

        assert claimed and claimed["job_uuid"] == factor_job
        assert claimed["_dispatch_exclusive"] is True
        assert claimed["_dispatch_slots"] == 2

        other = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 2,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001", "000002"],
            },
            user_id=7,
        )
        assert await broker.claim_next_job(worker_id="other-worker") is None
        assert (await broker.get_job_status(other))["status"] == "pending"

    asyncio.run(scenario())


def test_global_update_blocks_manual_simulation_cache_reader(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        global_update = await broker.submit_job(
            "data_update",
            {"pool_id": None},
            user_id=1,
        )

        first = await broker.claim_next_job(worker_id="update-worker")
        assert first and first["job_uuid"] == global_update
        simulation = await broker.submit_job(
            "daily_simulation",
            {"user_id": 1, "portfolio_id": 9},
            user_id=1,
        )
        assert await broker.claim_next_job(worker_id="simulation-worker") is None

        await broker.update_job_progress(global_update, status="completed", progress=1)
        second = await broker.claim_next_job(worker_id="simulation-worker")
        assert second and second["job_uuid"] == simulation

    asyncio.run(scenario())


def test_scheduler_leader_lease_and_stale_job_recovery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        assert await broker.acquire_scheduler_lease("owner-a", lease_seconds=30)
        assert not await broker.acquire_scheduler_lease("owner-b", lease_seconds=30)
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE job_scheduler_lease "
                "SET lease_expires_at='2000-01-01 00:00:00'"
            )
            conn.commit()
        # A live local PID cannot be stolen merely because an event-loop-bound
        # heartbeat was delayed by CPU work.
        assert not await broker.acquire_scheduler_lease(
            "owner-b", lease_seconds=30
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE job_scheduler_lease "
                "SET owner_process_start='old-process-generation', "
                "lease_expires_at=datetime('now', '+30 seconds')"
            )
            conn.commit()
        monkeypatch.setattr(
            broker_module,
            "_process_start_identity",
            lambda _: "reused-pid-generation",
        )
        # A live but reused local PID must not wedge the expired owner forever.
        assert await broker.acquire_scheduler_lease("owner-b", lease_seconds=30)
        await broker.release_scheduler_lease("owner-b")
        assert await broker.acquire_scheduler_lease("owner-a", lease_seconds=30)
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE job_scheduler_lease SET owner_pid=-1, "
                "lease_expires_at=datetime('now', '+30 seconds')"
            )
            conn.commit()
        # A confirmed-dead local PID permits immediate crash recovery.
        assert await broker.acquire_scheduler_lease("owner-b", lease_seconds=30)

        job_uuid = await broker.submit_job(
            "backtest",
            {"experiment_id": 1, "sweep_id": 7},
            user_id=1,
            resource_type="experiment",
            resource_id=1,
        )
        _seed_running_sweep(broker)
        assert await broker.claim_job(
            job_uuid, worker_id="owner-b:slot", lease_seconds=30
        )
        await broker.recover_pending_jobs()
        assert (await broker.get_job_status(job_uuid))["status"] == "running"

        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET lease_expires_at='2000-01-01 00:00:00' "
                "WHERE job_uuid=?",
                (job_uuid,),
            )
            conn.commit()
        await broker.recover_pending_jobs()
        recovered = await broker.get_job_status(job_uuid)
        assert recovered["status"] == "pending"
        assert recovered["worker_id"] is None
        with broker._get_conn() as conn:
            experiment = conn.execute(
                "SELECT status, progress_message FROM experiments WHERE id=1"
            ).fetchone()
            sweep = conn.execute(
                "SELECT status, completed_experiments "
                "FROM param_sweeps WHERE id=7"
            ).fetchone()
        assert tuple(experiment) == ("pending", "任务租约已过期，等待重新执行")
        assert tuple(sweep) == ("running", 0)

    asyncio.run(scenario())


def test_new_scheduler_periodically_recovers_claim_that_expires_after_start(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FixedController:
        def decide(self) -> CapacityDecision:
            return CapacityDecision(
                capacity=1,
                configured_max=1,
                degraded=False,
                reasons=(),
                metrics=_snapshot(),
            )

    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_POLL_SECONDS", 0.01)
        monkeypatch.setattr(settings, "JOB_SCHEDULER_SAMPLE_SECONDS", 0.01)
        monkeypatch.setattr(
            scheduler_module,
            "_EXPIRED_CLAIM_RECOVERY_INTERVAL_SECONDS",
            0.05,
        )
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_uuid = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 1,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001"],
            },
            user_id=1,
        )
        assert await broker.claim_job(
            job_uuid,
            worker_id="host:5697:scheduler:old:slot",
            lease_seconds=30,
        )
        old_claim = await broker.get_job_status(job_uuid)
        assert old_claim is not None
        old_generation = int(old_claim["lease_generation"])
        startup_recovery_complete = asyncio.Event()
        recover_pending_jobs = broker.recover_pending_jobs

        async def observe_startup_recovery() -> int:
            recovered = await recover_pending_jobs()
            startup_recovery_complete.set()
            return recovered

        monkeypatch.setattr(
            broker,
            "recover_pending_jobs",
            observe_startup_recovery,
        )

        executed: list[str] = []
        completed = asyncio.Event()

        async def executor(job: dict) -> None:
            executed.append(str(job["job_uuid"]))
            await broker.update_job_progress(
                str(job["job_uuid"]),
                status="completed",
                progress=1,
            )
            completed.set()

        scheduler = AdaptiveJobScheduler(
            broker,
            executor,
            controller=_FixedController(),  # type: ignore[arg-type]
        )
        task = asyncio.create_task(scheduler.run())
        try:
            # The replacement scheduler starts before the old claim expires.
            await asyncio.wait_for(startup_recovery_complete.wait(), timeout=2)
            assert (await broker.get_job_status(job_uuid))["status"] == "running"
            assert executed == []

            with broker._get_conn() as conn:
                conn.execute(
                    "UPDATE jobs SET lease_expires_at='2000-01-01 00:00:00' "
                    "WHERE job_uuid=?",
                    (job_uuid,),
                )
                conn.commit()
            assert broker.expired_claim_health_snapshot()["expired_count"] == 1

            await asyncio.wait_for(completed.wait(), timeout=2)
            recovered = await broker.get_job_status(job_uuid)
            assert recovered["status"] == "completed"
            assert int(recovered["lease_generation"]) == old_generation + 2
            assert executed == [job_uuid]
            with broker._get_conn() as conn:
                recovery_events = conn.execute(
                    """
                    SELECT COUNT(*) FROM job_events
                    WHERE job_uuid=? AND stage='lease_expired_recovered'
                    """,
                    (job_uuid,),
                ).fetchone()[0]
                running_events = conn.execute(
                    """
                    SELECT COUNT(*) FROM job_events
                    WHERE job_uuid=? AND status='running'
                    """,
                    (job_uuid,),
                ).fetchone()[0]
            assert recovery_events == 1
            assert running_events == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

    asyncio.run(scenario())


def test_expired_claim_recovery_is_bounded_and_preserves_live_leases(
    tmp_path,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        expired = [
            await broker.submit_job(
                "backtest",
                {"experiment_id": experiment_id},
                user_id=1,
            )
            for experiment_id in (1, 2)
        ]
        live = await broker.submit_job(
            "backtest",
            {"experiment_id": 3},
            user_id=1,
        )
        for index, job_uuid in enumerate((*expired, live), start=1):
            assert await broker.claim_job(
                job_uuid,
                worker_id=f"worker-{index}",
                lease_seconds=30,
            )
        expired_generation = int(
            (await broker.get_job_status(expired[0]))["lease_generation"]
        )
        with broker._get_conn() as conn:
            conn.executemany(
                "UPDATE jobs SET lease_expires_at='2000-01-01 00:00:00' "
                "WHERE job_uuid=?",
                [(job_uuid,) for job_uuid in expired],
            )
            conn.commit()

        first = await broker.recover_expired_claims(limit=1)
        assert first == {
            "recovered": 1,
            "running": 1,
            "cancel_requested": 0,
        }
        assert (await broker.get_job_status(expired[0]))["status"] == "pending"
        assert (await broker.get_job_status(expired[1]))["status"] == "running"
        assert (await broker.get_job_status(live))["status"] == "running"
        with broker.execution_claim(
            expired[0],
            "worker-1",
            expired_generation,
        ):
            with pytest.raises(JobLeaseLostError):
                await broker.update_job_progress(expired[0], progress=0.5)

        second = await broker.recover_expired_claims(limit=1)
        assert second["recovered"] == 1
        assert (await broker.get_job_status(expired[1]))["status"] == "pending"
        assert broker.expired_claim_health_snapshot()["healthy"] is True

    asyncio.run(scenario())


def test_lease_generation_fences_stale_worker_updates(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_uuid = await broker.submit_job(
            "backtest", {"experiment_id": 42}, user_id=1
        )
        job = await broker.claim_next_job(worker_id="worker-old")
        assert job is not None
        with broker._get_conn() as conn:
            conn.execute(
                """
                UPDATE jobs SET worker_id='worker-new',
                    lease_generation=lease_generation + 1
                WHERE job_uuid=?
                """,
                (job_uuid,),
            )
            conn.commit()
        with broker.execution_claim(
            job_uuid, "worker-old", int(job["lease_generation"])
        ):
            with pytest.raises(JobLeaseLostError):
                await broker.update_job_progress(job_uuid, progress=0.5)

    asyncio.run(scenario())


def test_queue_backpressure_reserves_critical_simulation_capacity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_MAX_PENDING_JOBS", 1)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        await broker.submit_job("backtest", {"experiment_id": 1}, user_id=1)
        with pytest.raises(JobQueueFullError):
            await broker.submit_job("backtest", {"experiment_id": 2}, user_id=1)
        critical = await broker.submit_job(
            "daily_simulation", {"user_id": 1}, user_id=1
        )
        assert critical

    asyncio.run(scenario())


def test_pending_sweep_cancel_and_retry_reconcile_all_state_in_one_database(
    tmp_path,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        with broker._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress_pct REAL DEFAULT 0,
                    progress_message TEXT,
                    error_log TEXT,
                    completed_at TEXT
                );
                CREATE TABLE param_sweeps (
                    id INTEGER PRIMARY KEY,
                    total_experiments INTEGER,
                    completed_experiments INTEGER DEFAULT 0,
                    status TEXT
                );
                CREATE TABLE sweep_experiments (
                    sweep_id INTEGER,
                    experiment_id INTEGER
                );
                INSERT INTO experiments (id, status) VALUES (42, 'pending');
                INSERT INTO param_sweeps
                    (id, total_experiments, completed_experiments, status)
                VALUES (7, 1, 0, 'running');
                INSERT INTO sweep_experiments (sweep_id, experiment_id)
                VALUES (7, 42);
                """
            )
            conn.commit()
        job_uuid = await broker.submit_job(
            "backtest",
            {"experiment_id": 42, "sweep_id": 7},
            user_id=1,
            resource_type="experiment",
            resource_id=42,
        )
        assert await broker.request_cancel(job_uuid) == "cancelled"
        with broker._get_conn() as conn:
            experiment = conn.execute(
                "SELECT status, progress_pct, progress_message, completed_at "
                "FROM experiments WHERE id=42"
            ).fetchone()
            sweep = conn.execute(
                "SELECT status, completed_experiments FROM param_sweeps WHERE id=7"
            ).fetchone()
        assert tuple(experiment)[:3] == (
            "cancelled",
            100,
            "任务在排队时取消",
        )
        assert experiment["completed_at"] is not None
        assert tuple(sweep) == ("completed", 1)

        retry_uuid = await broker.retry_job(job_uuid, user_id=1)
        assert retry_uuid is not None
        with broker._get_conn() as conn:
            experiment = conn.execute(
                "SELECT status, progress_pct, progress_message, completed_at "
                "FROM experiments WHERE id=42"
            ).fetchone()
            sweep = conn.execute(
                "SELECT status, completed_experiments FROM param_sweeps WHERE id=7"
            ).fetchone()
        assert tuple(experiment) == ("pending", 0, "重试已排队", None)
        assert tuple(sweep) == ("running", 0)

    asyncio.run(scenario())


def test_scheduler_executes_two_compatible_jobs_and_reports_slots(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FixedController:
        def decide(self) -> CapacityDecision:
            return CapacityDecision(
                capacity=2,
                configured_max=2,
                degraded=False,
                reasons=(),
                metrics=_snapshot(),
            )

    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_POLL_SECONDS", 0.01)
        monkeypatch.setattr(settings, "JOB_SCHEDULER_SAMPLE_SECONDS", 0.01)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_ids = [
            await broker.submit_job(
                "backtest",
                {
                    "experiment_id": experiment_id,
                    "pool_preset": "custom",
                    "pool_custom_codes": ["000001", "000002"],
                },
                user_id=1,
            )
            for experiment_id in (1, 2)
        ]
        started: list[str] = []
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def executor(job: dict) -> None:
            started.append(str(job["job_uuid"]))
            if len(started) == 2:
                both_started.set()
            await release.wait()
            await broker.update_job_progress(
                str(job["job_uuid"]), status="completed", progress=1
            )

        scheduler = AdaptiveJobScheduler(
            broker,
            executor,
            controller=_FixedController(),  # type: ignore[arg-type]
        )
        task = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(both_started.wait(), timeout=2)
        summary = await broker.get_summary(user_id=1)
        assert summary["worker"]["capacity"] == 2
        assert summary["worker"]["running_slots"] == 2
        release.set()
        for _ in range(100):
            statuses = [
                (await broker.get_job_status(job_id))["status"] for job_id in job_ids
            ]
            if statuses == ["completed", "completed"]:
                break
            await asyncio.sleep(0.01)
        assert statuses == ["completed", "completed"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_scheduler_survives_sqlite_writer_contention_and_claims_once_released(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FixedController:
        def decide(self) -> CapacityDecision:
            return CapacityDecision(
                capacity=1,
                configured_max=2,
                degraded=False,
                reasons=(),
                metrics=_snapshot(),
            )

    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_POLL_SECONDS", 0.01)
        monkeypatch.setattr(settings, "JOB_SCHEDULER_SAMPLE_SECONDS", 0.01)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_uuid = await broker.submit_job(
            "backtest",
            {
                "experiment_id": 1,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001"],
            },
            user_id=1,
        )
        claim_started = asyncio.Event()
        allow_claim = asyncio.Event()
        claim_next_job = broker.claim_next_job

        async def pause_before_claim(
            *, worker_id: str, lease_seconds: int
        ) -> dict | None:
            claim_started.set()
            await allow_claim.wait()
            return await claim_next_job(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

        monkeypatch.setattr(broker, "claim_next_job", pause_before_claim)
        executed: list[str] = []
        execution_started = asyncio.Event()

        async def executor(job: dict) -> None:
            executed.append(str(job["job_uuid"]))
            execution_started.set()
            await broker.update_job_progress(
                str(job["job_uuid"]),
                status="completed",
                progress=1,
            )

        scheduler = AdaptiveJobScheduler(
            broker,
            executor,
            controller=_FixedController(),  # type: ignore[arg-type]
        )
        task = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(claim_started.wait(), timeout=2)

        blocker = sqlite3.connect(str(tmp_path / "jobs.db"), timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        allow_claim.set()
        try:
            for _ in range(100):
                if scheduler._contention_failures:
                    break
                await asyncio.sleep(0.01)
            assert scheduler._contention_failures == 1
            assert not task.done()
            summary = await broker.get_summary(user_id=1)
            assert summary["worker"]["online"] is True
            assert summary["worker"]["degraded"] is True
            assert "sqlite_writer_contention" in summary["worker"]["reasons"]
            assert (await broker.get_job_status(job_uuid))["status"] == "pending"
        finally:
            blocker.rollback()
            blocker.close()

        await asyncio.wait_for(execution_started.wait(), timeout=2)
        for _ in range(100):
            if (await broker.get_job_status(job_uuid))["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert (await broker.get_job_status(job_uuid))["status"] == "completed"
        assert executed == [job_uuid]
        with broker._get_conn() as conn:
            running_events = conn.execute(
                "SELECT COUNT(*) FROM job_events "
                "WHERE job_uuid=? AND status='running'",
                (job_uuid,),
            ).fetchone()[0]
        assert running_events == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_scheduler_does_not_retry_non_contention_operational_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        await broker.submit_job(
            "backtest",
            {"experiment_id": 1},
            user_id=1,
        )
        calls = 0

        async def fail_claim(*, worker_id: str, lease_seconds: int) -> None:
            nonlocal calls
            del worker_id, lease_seconds
            calls += 1
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(broker, "claim_next_job", fail_claim)
        scheduler = AdaptiveJobScheduler(broker, lambda _: asyncio.sleep(0))
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            await asyncio.wait_for(scheduler.run(), timeout=2)
        assert calls == 1
        assert scheduler._contention_failures == 0

    asyncio.run(scenario())


def test_scheduler_stays_alive_while_recovering_mixed_pending_jobs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FixedController:
        def decide(self) -> CapacityDecision:
            return CapacityDecision(
                capacity=2,
                configured_max=2,
                degraded=False,
                reasons=(),
                metrics=_snapshot(),
            )

    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_POLL_SECONDS", 0.01)
        monkeypatch.setattr(settings, "JOB_SCHEDULER_SAMPLE_SECONDS", 0.01)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        with broker._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    pool_preset TEXT,
                    pool_custom_codes TEXT,
                    requires_training INTEGER
                );
                INSERT INTO experiments (
                    id, pool_preset, pool_custom_codes, requires_training
                )
                VALUES (17, 'csi300', NULL, 0);
                """
            )
            conn.commit()
        backtest = await broker.submit_job(
            "backtest",
            {"experiment_id": 17, "pool_preset": "csi300"},
            user_id=1,
            resource_type="experiment",
            resource_id=17,
        )
        data_update = await broker.submit_job(
            "data_update",
            {"pool_id": None, "source": "paper_scheduler"},
            user_id=None,
        )
        simulation = await broker.submit_job(
            "daily_simulation",
            {
                "user_id": 1,
                "portfolio_id": 1,
                "required_data_job_uuid": data_update,
            },
            user_id=1,
        )
        malformed = await broker.submit_job(
            "backtest",
            {"experiment_id": 404},
            user_id=1,
            resource_type="experiment",
            resource_id=404,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET params='not-json' WHERE job_uuid=?",
                (malformed,),
            )
            conn.commit()
        job_ids = (data_update, simulation, backtest, malformed)
        released_claims: set[str] = set()
        all_claims_released = asyncio.Event()
        release_claims = broker.release_claims

        async def observe_released_claims(
            claims: list[tuple[str, str, int]], *, reason: str
        ) -> int:
            released = await release_claims(claims, reason=reason)
            released_claims.update(claim[0] for claim in claims)
            if released_claims.issuperset(job_ids):
                all_claims_released.set()
            return released

        monkeypatch.setattr(broker, "release_claims", observe_released_claims)
        executed: list[str] = []

        async def executor(job: dict) -> None:
            job_uuid = str(job["job_uuid"])
            executed.append(job_uuid)
            if not isinstance(job["params"], dict):
                raise ValueError("malformed job params")
            await broker.update_job_progress(
                job_uuid,
                status="completed",
                progress=1,
            )

        scheduler = AdaptiveJobScheduler(
            broker,
            executor,
            controller=_FixedController(),  # type: ignore[arg-type]
        )
        task = asyncio.create_task(scheduler.run())
        try:
            # A terminal database status is committed before the scheduler
            # collects the child task and releases its claim. Synchronise on
            # that complete lifecycle so cancellation cannot race claim cleanup.
            await asyncio.wait_for(all_claims_released.wait(), timeout=5)
            statuses = [
                (await broker.get_job_status(job_id))["status"]
                for job_id in job_ids
            ]

            assert statuses == ["completed", "completed", "completed", "failed"]
            assert executed == list(job_ids)
            assert scheduler._running == {}
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

    asyncio.run(scenario())


def test_scheduler_shutdown_preserves_cancel_requested_terminal_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FixedController:
        def decide(self) -> CapacityDecision:
            return CapacityDecision(
                capacity=1,
                configured_max=2,
                degraded=True,
                reasons=("memory_available_low",),
                metrics=_snapshot(available_mb=512, used_ratio=0.94),
            )

    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_POLL_SECONDS", 0.01)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_uuid = await broker.submit_job(
            "backtest",
            {"experiment_id": 1, "sweep_id": 7},
            user_id=1,
            resource_type="experiment",
            resource_id=1,
        )
        _seed_running_sweep(broker)
        started = asyncio.Event()
        hold = asyncio.Event()

        async def executor(_: dict) -> None:
            started.set()
            await hold.wait()

        scheduler = AdaptiveJobScheduler(
            broker,
            executor,
            controller=_FixedController(),  # type: ignore[arg-type]
        )
        task = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await broker.request_cancel(job_uuid) == "cancel_requested"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        job = await broker.get_job_status(job_uuid)
        assert job["status"] == "cancelled"
        assert job["progress_message"] == "服务关闭时完成取消"
        with broker._get_conn() as conn:
            experiment = conn.execute(
                "SELECT status FROM experiments WHERE id=1"
            ).fetchone()
            sweep = conn.execute(
                "SELECT status, completed_experiments "
                "FROM param_sweeps WHERE id=7"
            ).fetchone()
        assert experiment["status"] == "cancelled"
        assert tuple(sweep) == ("completed", 1)

    asyncio.run(scenario())


def test_scheduler_shutdown_is_not_masked_by_sqlite_writer_contention(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(settings, "JOB_SCHEDULER_POLL_SECONDS", 0.01)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        await broker.submit_job(
            "backtest",
            {
                "experiment_id": 1,
                "pool_preset": "custom",
                "pool_custom_codes": ["000001"],
            },
            user_id=1,
        )
        started = asyncio.Event()

        async def executor(_: dict) -> None:
            started.set()
            await asyncio.Event().wait()

        scheduler = AdaptiveJobScheduler(broker, executor)
        task = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(started.wait(), timeout=2)
        blocker = sqlite3.connect(str(tmp_path / "jobs.db"), timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            summary = await broker.get_summary(user_id=1)
            assert summary["worker"]["online"] is False
        finally:
            blocker.rollback()
            blocker.close()

    asyncio.run(scenario())


def test_scheduler_requeues_claim_when_executor_is_independently_cancelled(
    tmp_path,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_uuid = await broker.submit_job(
            "backtest",
            {"experiment_id": 1, "sweep_id": 7},
            user_id=1,
            resource_type="experiment",
            resource_id=1,
        )
        _seed_running_sweep(broker)

        async def executor(_: dict) -> None:
            raise asyncio.CancelledError

        scheduler = AdaptiveJobScheduler(broker, executor)
        await scheduler._start_available_jobs(1)
        claim_task = next(iter(scheduler._running.values())).task
        with pytest.raises(asyncio.CancelledError):
            await claim_task
        await scheduler._collect_finished()

        job = await broker.get_job_status(job_uuid)
        assert job["status"] == "pending"
        assert job["worker_id"] is None
        assert job["progress_message"] == "执行协程提前结束，等待重新执行"
        with broker._get_conn() as conn:
            experiment = conn.execute(
                "SELECT status, progress_pct, progress_message "
                "FROM experiments WHERE id=1"
            ).fetchone()
            sweep = conn.execute(
                "SELECT status, completed_experiments "
                "FROM param_sweeps WHERE id=7"
            ).fetchone()
        assert tuple(experiment) == (
            "pending",
            0,
            "执行协程提前结束，等待重新执行",
        )
        assert tuple(sweep) == ("running", 0)

    asyncio.run(scenario())


def test_finished_claim_cleanup_honors_sqlite_contention_backoff(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_uuid = await broker.submit_job(
            "backtest",
            {"experiment_id": 1},
            user_id=1,
        )

        async def executor(_: dict) -> None:
            raise asyncio.CancelledError

        scheduler = AdaptiveJobScheduler(broker, executor)
        await scheduler._start_available_jobs(1)
        claim_task = scheduler._running[job_uuid].task
        with pytest.raises(asyncio.CancelledError):
            await claim_task

        calls = 0

        async def locked_release(
            claims: list[tuple[str, str, int]], *, reason: str
        ) -> int:
            nonlocal calls
            del claims, reason
            calls += 1
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(broker, "release_claims", locked_release)
        assert await scheduler._collect_finished() is True
        assert calls == 1
        # A second loop before retry_at must not enter SQLite again.
        assert await scheduler._collect_finished() is True
        assert calls == 1

    asyncio.run(scenario())


def test_scheduler_restart_reuses_process_lease_owner(
    tmp_path,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        first = AdaptiveJobScheduler(broker, lambda _: asyncio.sleep(0))
        second = AdaptiveJobScheduler(broker, lambda _: asyncio.sleep(0))
        assert first._owner_id == second._owner_id
        assert await broker.acquire_scheduler_lease(
            first._owner_id, lease_seconds=30
        )
        # A supervisor restart in the same live process can take over its own
        # lease even if prior shutdown cleanup was blocked.
        assert await broker.acquire_scheduler_lease(
            second._owner_id, lease_seconds=30
        )

    asyncio.run(scenario())


def test_scheduler_releases_leader_lease_when_recovery_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))

        async def fail_recovery() -> int:
            raise RuntimeError("recovery failed")

        monkeypatch.setattr(broker, "recover_pending_jobs", fail_recovery)
        scheduler = AdaptiveJobScheduler(broker, lambda _: asyncio.sleep(0))
        with pytest.raises(RuntimeError, match="recovery failed"):
            await scheduler.run()
        with broker._get_conn() as conn:
            assert conn.execute(
                "SELECT owner_id FROM job_scheduler_lease"
            ).fetchone() is None
        assert (await broker.get_summary(user_id=1))["worker"]["online"] is False

    asyncio.run(scenario())


def test_legacy_schema_migration_adds_dispatch_and_lease_columns(tmp_path) -> None:
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                job_type TEXT NOT NULL,
                job_uuid TEXT UNIQUE NOT NULL
            )
            """
        )
    broker = JobBroker(str(db_path))
    with broker._get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert {
        "priority",
        "queue_group",
        "worker_id",
        "lease_generation",
        "lease_expires_at",
    } <= columns
    with broker._get_conn() as conn:
        lease_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(job_scheduler_lease)")
        }
    assert "owner_process_start" in lease_columns


def test_job_schema_migration_is_serialized_across_starting_processes(
    tmp_path,
) -> None:
    db_path = str(tmp_path / "jobs.db")
    with ThreadPoolExecutor(max_workers=4) as executor:
        brokers = list(executor.map(lambda _: JobBroker(db_path), range(4)))
    assert len(brokers) == 4
    with brokers[0]._get_conn() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='job_scheduler_lease'"
        ).fetchone()


def test_claim_and_leader_lease_are_atomic_across_broker_instances(tmp_path) -> None:
    db_path = str(tmp_path / "jobs.db")
    brokers = [JobBroker(db_path), JobBroker(db_path)]
    job_uuid = asyncio.run(
        brokers[0].submit_job("backtest", {"experiment_id": 1}, user_id=1)
    )

    claim_barrier = threading.Barrier(2)

    def claim(index: int) -> dict | None:
        claim_barrier.wait()
        return asyncio.run(
            brokers[index].claim_next_job(worker_id=f"worker-{index}")
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    winners = [item for item in claims if item is not None]
    assert len(winners) == 1
    assert winners[0]["job_uuid"] == job_uuid
    assert asyncio.run(brokers[0].get_job_status(job_uuid))["lease_generation"] == 1

    asyncio.run(
        brokers[0].update_job_progress(job_uuid, status="failed", progress=1)
    )
    retry_barrier = threading.Barrier(2)

    def retry(index: int) -> str | None:
        retry_barrier.wait()
        return asyncio.run(brokers[index].retry_job(job_uuid, user_id=1))

    with ThreadPoolExecutor(max_workers=2) as executor:
        retries = list(executor.map(retry, range(2)))
    assert sum(item is not None for item in retries) == 1
    with brokers[0]._get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE parent_job_uuid=?",
            (job_uuid,),
        ).fetchone()[0] == 1

    leader_barrier = threading.Barrier(2)

    def acquire(index: int) -> bool:
        leader_barrier.wait()
        return asyncio.run(
            brokers[index].acquire_scheduler_lease(
                f"owner-{index}",
                lease_seconds=30,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        acquired = list(executor.map(acquire, range(2)))
    assert sorted(acquired) == [False, True]
