from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from backend.jobs.broker import JobBroker, JobLeaseLostError
from backend.services import research_data_refresh as refresh


def test_responsive_refresh_keeps_server_event_loop_schedulable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []

    async def blocking_refresh(**_kwargs):
        worker_threads.append(threading.get_ident())
        # Represents collector reconciliation/materialization code that does
        # not yield even though it lives inside an async function.
        time.sleep(0.18)
        return {"status": "completed"}

    monkeypatch.setattr(refresh, "run_research_data_refresh", blocking_refresh)

    async def scenario() -> tuple[dict, int]:
        ticks = 0
        finished = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal ticks
            while not finished.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(heartbeat())
        try:
            result = await refresh.run_research_data_refresh_responsive(
                source_id="tushare",
                from_month="2016-01",
                to_month="2016-01",
                max_calls=1,
            )
        finally:
            finished.set()
            await ticker
        return result, ticks

    result, ticks = asyncio.run(scenario())

    assert result == {"status": "completed"}
    assert ticks >= 8
    assert worker_threads and worker_threads[0] != caller_thread


def test_responsive_refresh_cancellation_stops_before_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_activation: list[bool] = []

    async def blocking_refresh(**kwargs):
        callback = kwargs["blocking_progress"]
        callback(
            {
                "overall_fraction": 0.5,
                "stage": "provider_collection",
                "message": "checkpoint persisted",
            }
        )
        time.sleep(0.08)
        callback(
            {
                "overall_fraction": 0.99,
                "stage": "research_import_activate",
                "message": "activation boundary",
            }
        )
        reached_activation.append(True)
        return {"status": "completed"}

    monkeypatch.setattr(refresh, "run_research_data_refresh", blocking_refresh)

    async def scenario() -> None:
        task = asyncio.create_task(
            refresh.run_research_data_refresh_responsive(
                source_id="tushare",
                from_month="2016-01",
                to_month="2016-01",
                max_calls=1,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert reached_activation == []


def test_responsive_refresh_outer_cancel_after_commit_waits_for_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_finished = threading.Event()

    async def blocking_refresh(**kwargs):
        kwargs["blocking_progress"](
            {
                "overall_fraction": 0.99,
                "stage": "research_import_activate",
                "message": "activation pointer commit started",
                "cancellable": False,
            }
        )
        time.sleep(0.08)
        worker_finished.set()
        return {"status": "completed", "activation_committed": True}

    commit_visible = asyncio.Event()

    async def progress(event):
        if event.get("cancellable") is False:
            commit_visible.set()

    monkeypatch.setattr(refresh, "run_research_data_refresh", blocking_refresh)

    async def scenario() -> dict:
        task = asyncio.create_task(
            refresh.run_research_data_refresh_responsive(
                source_id="tushare",
                from_month="2016-01",
                to_month="2016-01",
                max_calls=1,
                progress=progress,
            )
        )
        await asyncio.wait_for(commit_visible.wait(), timeout=1)
        task.cancel()
        # Scheduler shutdown can issue cancel more than once.  Once the
        # activation pointer commit has started, both requests must be
        # suppressed until the executor worker returns; an application timeout
        # cannot safely terminate that thread.
        asyncio.get_running_loop().call_later(0.01, task.cancel)
        return await task

    result = asyncio.run(scenario())

    assert result == {"status": "completed", "activation_committed": True}
    assert worker_finished.is_set()


def test_responsive_refresh_cancel_has_no_late_progress_invalid_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocking_refresh(**kwargs):
        kwargs["blocking_progress"](
            {
                "overall_fraction": 0.5,
                "stage": "provider_collection",
                "message": "slow progress",
            }
        )
        return {"status": "completed"}

    async def slow_progress(_event):
        await asyncio.sleep(1)

    monkeypatch.setattr(refresh, "run_research_data_refresh", blocking_refresh)

    async def scenario() -> list[dict]:
        loop = asyncio.get_running_loop()
        loop_errors: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            task = asyncio.create_task(
                refresh.run_research_data_refresh_responsive(
                    source_id="tushare",
                    from_month="2016-01",
                    to_month="2016-01",
                    max_calls=1,
                    progress=slow_progress,
                )
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            return loop_errors
        finally:
            loop.set_exception_handler(previous)

    assert asyncio.run(scenario()) == []


def test_responsive_refresh_preserves_job_claim_context_for_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = contextvars.ContextVar("fixture_claim", default="missing")
    observed: list[str] = []

    async def blocking_refresh(**kwargs):
        kwargs["blocking_progress"](
            {
                "overall_fraction": 0.5,
                "stage": "provider_collection",
                "message": "progress",
            }
        )
        return {"status": "completed"}

    async def progress(_event):
        observed.append(claim.get())

    monkeypatch.setattr(refresh, "run_research_data_refresh", blocking_refresh)

    async def scenario() -> None:
        token = claim.set("lease-generation-7")
        try:
            await refresh.run_research_data_refresh_responsive(
                source_id="tushare",
                from_month="2016-01",
                to_month="2016-01",
                max_calls=1,
                progress=progress,
            )
        finally:
            claim.reset(token)

    asyncio.run(scenario())
    assert observed == ["lease-generation-7"]


def test_responsive_refresh_progress_honors_real_broker_lease_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocking_refresh(**kwargs):
        kwargs["blocking_progress"](
            {
                "overall_fraction": 0.5,
                "stage": "provider_collection",
                "message": "progress",
            }
        )
        return {"status": "completed"}

    monkeypatch.setattr(refresh, "run_research_data_refresh", blocking_refresh)

    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        job_uuid = await broker.submit_job(
            "research_data_refresh",
            {"source_id": "tushare"},
            user_id=1,
        )
        worker_id = "original-worker"
        assert await broker.claim_job(job_uuid, worker_id=worker_id) is True
        claimed = await broker.get_job_status(job_uuid)
        generation = int(claimed["lease_generation"])
        with broker.execution_claim(job_uuid, worker_id, generation):
            with broker._get_conn() as connection:  # noqa: SLF001
                connection.execute(
                    "UPDATE jobs SET worker_id=?, lease_generation=? WHERE job_uuid=?",
                    ("replacement-worker", generation + 1, job_uuid),
                )
                connection.commit()

            async def progress(event):
                await broker.update_job_progress(
                    job_uuid,
                    progress=float(event["overall_fraction"]),
                    stage=str(event["stage"]),
                )

            with pytest.raises(JobLeaseLostError):
                await refresh.run_research_data_refresh_responsive(
                    source_id="tushare",
                    from_month="2016-01",
                    to_month="2016-01",
                    max_calls=1,
                    progress=progress,
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("before_generation", "market_available", "previous_sessions", "collected_sessions", "pending", "expected_publish"),
    [
        (None, False, 0, 0, 900, False),
        (None, False, 0, 1, 899, True),
        ("old", True, 100, 351, 549, False),
        ("old", True, 100, 352, 548, True),
        ("old", True, 100, 101, 0, True),
    ],
)
def test_refresh_publishes_only_usable_and_throttled_market_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_generation: str | None,
    market_available: bool,
    previous_sessions: int,
    collected_sessions: int,
    pending: int,
    expected_publish: bool,
) -> None:
    collection = {
        "run_id": "a" * 32,
        "stored_report_sha256": "b" * 64,
        "classification": "quarantine",
        "failures": {},
        "optional_failures": [],
        "progress": {
            "planned_tasks": 1_000,
            "completed_tasks": 1_000 - pending,
            "pending_tasks": pending,
            "calls_this_invocation": 128,
            "completed_this_invocation": 128,
            "reconciled_session_count": collected_sessions,
            "complete": pending == 0,
        },
    }

    class FakeCollector:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self):
            return collection

    class FakeStore:
        import_calls = 0

        def __init__(self, _root=None) -> None:
            self.generation = before_generation

        def status(self):
            return {
                "available": self.generation is not None,
                "generation_id": self.generation,
                "market": {"available": market_available},
                "coverage": {"reconciled_session_count": previous_sessions},
            }

        def import_tushare_reconciled_history(self, *_args, **_kwargs):
            type(self).import_calls += 1
            self.generation = "new"
            return self.status()

        def conflict_report(self):
            return {"status": "insufficient_sources", "comparisons": []}

    monkeypatch.setattr(refresh.settings, "TUSHARE_TOKEN", SecretStr("test-token"))
    monkeypatch.setattr(refresh, "TusharePitBackfillCollector", FakeCollector)
    monkeypatch.setattr(refresh, "ResearchDataStore", FakeStore)

    result = asyncio.run(
        refresh.run_research_data_refresh(
            source_id="tushare",
            from_month="2016-01",
            to_month="2026-06",
            max_calls=128,
            evidence_root=tmp_path / "evidence",
            research_root=tmp_path / "research",
        )
    )

    assert FakeStore.import_calls == int(expected_publish)
    assert result["collection"] == {
        "run_id": "a" * 32,
        "completed_tasks": 1_000 - pending,
        "planned_tasks": 1_000,
        "pending_tasks": pending,
        "calls_this_invocation": 128,
        "completed_this_invocation": 128,
        "reconciled_session_count": collected_sessions,
        "complete": pending == 0,
        "failures": {},
        "optional_failures": [],
        "classification": "quarantine",
    }
    if collected_sessions == 0:
        assert result["import_warning"] == "reconciled_market_sessions_not_yet_available"


def test_optional_stall_gets_one_automatic_retry_without_a_busy_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = {
        "run_id": "a" * 32,
        "stored_report_sha256": "b" * 64,
        "classification": "quarantine",
        "failures": [],
        "optional_failures": [],
        "progress": {
            "planned_tasks": 10,
            "completed_tasks": 9,
            "pending_tasks": 1,
            "calls_this_invocation": 1,
            "completed_this_invocation": 0,
            "reconciled_session_count": 0,
            "complete": False,
        },
    }

    class FakeCollector:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self):
            return collection

    class FakeStore:
        def __init__(self, _root=None) -> None:
            pass

        def status(self):
            return {
                "available": False,
                "generation_id": None,
                "market": {"available": False},
                "coverage": {},
            }

        def conflict_report(self):
            return {"status": "insufficient_sources", "comparisons": []}

    monkeypatch.setattr(refresh.settings, "TUSHARE_TOKEN", SecretStr("test-token"))
    monkeypatch.setattr(refresh, "TusharePitBackfillCollector", FakeCollector)
    monkeypatch.setattr(refresh, "ResearchDataStore", FakeStore)

    outcomes: list[bool] = []
    for attempt in (1, 2):
        collection["optional_failures"] = [{
            "task": {"dataset": "daily_basic"},
            "diagnostic": {"code": "provider_service_unavailable", "retryable": True},
            "attempt_count": attempt,
        }]
        result = asyncio.run(
            refresh.run_research_data_refresh(
                source_id="tushare",
                from_month="2016-01",
                to_month="2026-06",
                max_calls=1,
                evidence_root=tmp_path / "evidence",
                research_root=tmp_path / "research",
            )
        )
        outcomes.append(result["continuation_required"])

    assert outcomes == [True, False]


def test_committed_activation_keeps_final_progress_non_cancellable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = {
        "run_id": "a" * 32,
        "stored_report_sha256": "b" * 64,
        "classification": "quarantine",
        "failures": {},
        "optional_failures": [],
        "progress": {
            "planned_tasks": 1,
            "completed_tasks": 1,
            "pending_tasks": 0,
            "calls_this_invocation": 1,
            "completed_this_invocation": 1,
            "reconciled_session_count": 1,
            "complete": True,
        },
    }

    class FakeCollector:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self):
            return collection

    class FakeStore:
        def __init__(self, _root=None) -> None:
            self.generation = None

        def status(self):
            return {
                "generation_id": self.generation,
                "market": {"available": self.generation is not None},
                "coverage": {},
            }

        def import_tushare_reconciled_history(self, *_args, **_kwargs):
            self.generation = "new"
            return {**self.status(), "activation_committed": True}

        def conflict_report(self):
            return {"status": "insufficient_sources", "comparisons": []}

    events: list[dict] = []

    async def progress(event):
        events.append(event)

    monkeypatch.setattr(refresh.settings, "TUSHARE_TOKEN", SecretStr("test-token"))
    monkeypatch.setattr(refresh, "TusharePitBackfillCollector", FakeCollector)
    monkeypatch.setattr(refresh, "ResearchDataStore", FakeStore)

    result = asyncio.run(
        refresh.run_research_data_refresh(
            source_id="tushare",
            from_month="2016-01",
            to_month="2016-01",
            max_calls=1,
            progress=progress,
            evidence_root=tmp_path / "evidence",
            research_root=tmp_path / "research",
        )
    )

    assert result["activation_committed"] is True
    assert events[-1]["stage"] == "research_import_activate"
    assert events[-1]["cancellable"] is False


def test_temporary_session_reconciliation_gap_does_not_stop_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = {
        "run_id": "a" * 32,
        "stored_report_sha256": "b" * 64,
        "classification": "quarantine",
        "failures": [
            {
                "code": "historical_member_session_coverage_invalid",
                "retryable": False,
                "trade_date": "20160104",
                "blockers": [{"code": "000001.SZ", "reason": "not_collected_yet"}],
            }
        ],
        "optional_failures": [],
        "progress": {
            "planned_tasks": 2_000,
            "completed_tasks": 283,
            "pending_tasks": 1_717,
            "calls_this_invocation": 128,
            "completed_this_invocation": 128,
            "reconciled_session_count": 1,
            "complete": False,
        },
    }

    class FakeCollector:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self):
            return collection

    class FakeStore:
        def __init__(self, _root=None) -> None:
            self.generation = None

        def status(self):
            return {
                "available": self.generation is not None,
                "generation_id": self.generation,
                "market": {"available": self.generation is not None},
                "coverage": {},
            }

        def import_tushare_reconciled_history(self, *_args, **_kwargs):
            self.generation = "new"
            return self.status()

        def conflict_report(self):
            return {"status": "insufficient_sources", "comparisons": []}

    monkeypatch.setattr(refresh.settings, "TUSHARE_TOKEN", SecretStr("test-token"))
    monkeypatch.setattr(refresh, "TusharePitBackfillCollector", FakeCollector)
    monkeypatch.setattr(refresh, "ResearchDataStore", FakeStore)

    result = asyncio.run(
        refresh.run_research_data_refresh(
            source_id="tushare",
            from_month="2016-01",
            to_month="2026-06",
            max_calls=128,
            evidence_root=tmp_path / "evidence",
            research_root=tmp_path / "research",
        )
    )

    assert result["continuation_required"] is True
    assert result["continuation_blockers"] == []

    collection["progress"]["completed_this_invocation"] = 0
    stalled = asyncio.run(
        refresh.run_research_data_refresh(
            source_id="tushare",
            from_month="2016-01",
            to_month="2026-06",
            max_calls=128,
            evidence_root=tmp_path / "evidence",
            research_root=tmp_path / "research",
        )
    )
    assert stalled["continuation_required"] is False

    collection["progress"]["completed_this_invocation"] = 1
    collection["failures"].append(
        {
            "task": {"dataset": "daily"},
            "diagnostic": {
                "code": "provider_contract_invalid",
                "retryable": False,
            },
        }
    )
    blocked = asyncio.run(
        refresh.run_research_data_refresh(
            source_id="tushare",
            from_month="2016-01",
            to_month="2026-06",
            max_calls=128,
            evidence_root=tmp_path / "evidence",
            research_root=tmp_path / "research",
        )
    )
    assert blocked["continuation_required"] is False
    assert refresh._collection_failure_code(  # noqa: SLF001
        blocked["continuation_blockers"][0]
    ) == "provider_contract_invalid"
