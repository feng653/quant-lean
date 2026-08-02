"""Adaptive in-process dispatcher for durable SQLite jobs.

This module deliberately controls asyncio concurrency only. CPU-heavy pandas
or model code remains in the API process and may block the event loop; process
isolation is a separate architecture change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from backend.config import settings
from backend.jobs.broker import (
    JobBroker,
    JobCancelledError,
    JobLeaseLostError,
)
from backend.jobs.resources import AdaptiveCapacityController, CapacityDecision
from backend.jobs.observability import structured_log

logger = logging.getLogger("quant_platform.jobs.scheduler")
JobExecutor = Callable[[dict[str, Any]], Awaitable[None]]

_CONTENTION_BACKOFF_BASE_SECONDS = 0.25
_CONTENTION_BACKOFF_MAX_SECONDS = 5.0
_STATE_RECONCILIATION_INTERVAL_SECONDS = 60.0
_EXPIRED_CLAIM_RECOVERY_INTERVAL_SECONDS = 5.0
_EXPIRED_CLAIM_RECOVERY_LIMIT = 100
_PROCESS_OWNER_PID = os.getpid()
_PROCESS_OWNER_TOKEN = uuid.uuid4().hex[:12]


def _scheduler_owner_id() -> str:
    """Keep lease identity stable across supervisor restarts in one process."""
    global _PROCESS_OWNER_PID, _PROCESS_OWNER_TOKEN
    current_pid = os.getpid()
    if current_pid != _PROCESS_OWNER_PID:
        _PROCESS_OWNER_PID = current_pid
        _PROCESS_OWNER_TOKEN = uuid.uuid4().hex[:12]
    return (
        f"{socket.gethostname()}:{current_pid}:scheduler:{_PROCESS_OWNER_TOKEN}"
    )


def _is_sqlite_contention(exc: sqlite3.OperationalError) -> bool:
    """Recognise only transient writer contention, not arbitrary DB failures."""
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


@dataclass
class RunningClaim:
    job_uuid: str
    worker_id: str
    lease_generation: int
    slots: int
    exclusive: bool
    task: asyncio.Task[None]

    def lease_tuple(self) -> tuple[str, str, int]:
        return self.job_uuid, self.worker_id, self.lease_generation


class AdaptiveJobScheduler:
    """Own one SQLite leader lease and fill up to the resource-safe capacity."""

    def __init__(
        self,
        broker: JobBroker,
        executor: JobExecutor,
        *,
        controller: AdaptiveCapacityController | None = None,
    ) -> None:
        self._broker = broker
        self._executor = executor
        self._controller = controller or AdaptiveCapacityController()
        self._owner_id = _scheduler_owner_id()
        self._running: dict[str, RunningClaim] = {}
        self._last_decision: CapacityDecision | None = None
        self._last_admission_mode: str | None = None
        self._contention_failures = 0
        self._contention_retry_at = 0.0

    def _defer_for_contention(self, operation: str) -> float:
        self._contention_failures += 1
        self._broker.note_sqlite_contention()
        delay = min(
            _CONTENTION_BACKOFF_BASE_SECONDS
            * (2 ** min(self._contention_failures - 1, 8)),
            _CONTENTION_BACKOFF_MAX_SECONDS,
        )
        self._contention_retry_at = time.monotonic() + delay
        logger.warning(
            "SQLite writer contention while %s; retrying in %.2fs (attempt %d)",
            operation,
            delay,
            self._contention_failures,
        )
        structured_log(
            logger,
            logging.WARNING,
            "sqlite_contention",
            component="job_scheduler",
            reason="writer_contention",
            attempt=self._contention_failures,
            delay_seconds=round(delay, 3),
        )
        return delay

    def _publish_status(
        self,
        decision: CapacityDecision,
        *,
        leader: bool,
        extra_reasons: list[str] | None = None,
    ) -> None:
        reasons = list(decision.reasons)
        if extra_reasons:
            reasons.extend(reason for reason in extra_reasons if reason not in reasons)
        running_slots = sum(claim.slots for claim in self._running.values())
        if any(claim.exclusive for claim in self._running.values()):
            running_slots = max(running_slots, decision.capacity)
        if decision.admission_mode != self._last_admission_mode:
            structured_log(
                logger,
                logging.WARNING if decision.pause_heavy else logging.INFO,
                "resource_admission_changed",
                component="job_scheduler",
                outcome=decision.admission_mode,
                reason=(reasons[0] if reasons else "healthy"),
            )
            self._last_admission_mode = decision.admission_mode
        self._broker.set_scheduler_status(
            desired_capacity=decision.capacity if leader else 0,
            configured_max=decision.configured_max,
            running_slots=running_slots,
            degraded=decision.degraded or bool(extra_reasons),
            reasons=reasons,
            metrics=decision.metrics.public_dict(),
            execution_mode="hybrid_spawn_factor_research",
            leader=leader,
            pause_heavy=decision.pause_heavy,
            admission_mode=decision.admission_mode,
            budgets={
                "cpu": {
                    "scale_up_max": float(
                        settings.JOB_SCHEDULER_CPU_LOAD_LIMIT
                    ),
                    "heavy_pause_at": float(
                        settings.JOB_SCHEDULER_CRITICAL_CPU_LOAD
                    ),
                },
                "memory": {
                    "scale_up_min_available_mb": int(
                        settings.JOB_SCHEDULER_MIN_AVAILABLE_MEMORY_MB
                    ),
                    "heavy_pause_min_available_mb": int(
                        settings.JOB_SCHEDULER_CRITICAL_AVAILABLE_MEMORY_MB
                    ),
                    "heavy_pause_used_ratio": float(
                        settings.JOB_SCHEDULER_CRITICAL_MEMORY_USED
                    ),
                },
                "io": {
                    "min_disk_free_mb": int(
                        settings.JOB_SCHEDULER_MIN_DISK_FREE_MB
                    ),
                    "max_pressure": float(
                        settings.JOB_SCHEDULER_MAX_IO_PRESSURE
                    ),
                },
                "cpu_threads_per_heavy_job": max(
                    int(settings.JOB_CPU_THREAD_BUDGET), 1
                ),
            },
        )

    async def _execute_claim(
        self,
        job: dict[str, Any],
        worker_id: str,
        lease_generation: int,
    ) -> None:
        job_uuid = str(job["job_uuid"])
        with self._broker.execution_claim(
            job_uuid, worker_id, lease_generation
        ):
            try:
                await self._executor(job)
            except asyncio.CancelledError:
                raise
            except JobCancelledError:
                logger.info("Job %s cancelled", job_uuid)
            except JobLeaseLostError:
                logger.error("Worker lost lease for job %s; stopping stale updates", job_uuid)
            except Exception as exc:
                logger.exception("Job %s failed in scheduler", job_uuid)
                try:
                    await self._broker.update_job_progress(
                        job_uuid,
                        progress=1.0,
                        status="failed",
                        error=str(exc),
                        message="任务执行失败",
                        stage="failed",
                    )
                except JobLeaseLostError:
                    logger.error("Unable to record failure after lease loss: %s", job_uuid)

    async def _start_available_jobs(self, capacity: int) -> None:
        while sum(claim.slots for claim in self._running.values()) < capacity:
            worker_id = (
                f"{self._owner_id}:slot:{uuid.uuid4().hex[:8]}"
            )
            job = await self._broker.claim_next_job(
                worker_id=worker_id,
                lease_seconds=int(settings.JOB_SCHEDULER_LEASE_SECONDS),
            )
            if job is None:
                break
            job_uuid = str(job["job_uuid"])
            generation = int(job["lease_generation"])
            task = asyncio.create_task(
                self._execute_claim(job, worker_id, generation),
                name=f"job-{job_uuid[:12]}",
            )
            self._running[job_uuid] = RunningClaim(
                job_uuid=job_uuid,
                worker_id=worker_id,
                lease_generation=generation,
                slots=min(
                    max(int(job.get("_dispatch_slots") or 1), 1),
                    max(capacity, 1),
                ),
                exclusive=bool(job.get("_dispatch_exclusive")),
                task=task,
            )

    async def _collect_finished(self) -> bool:
        finished = [
            job_uuid
            for job_uuid, claim in self._running.items()
            if claim.task.done()
        ]
        if not finished:
            return False
        if time.monotonic() < self._contention_retry_at:
            return True
        for job_uuid in finished:
            claim = self._running[job_uuid]
            try:
                await claim.task
            except asyncio.CancelledError:
                # Awaiting an independently cancelled child also raises
                # CancelledError. Do not, however, swallow cancellation of the
                # scheduler task itself or application shutdown can hang.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                # _execute_claim contains the failure boundary. This is a final
                # guard so one unexpected task exception never kills dispatch.
                logger.exception("Unhandled task boundary for job %s", job_uuid)
            # Executors normally commit a terminal status themselves. If one
            # returns early or is independently cancelled, immediately
            # relinquish its still-active claim. Keep the finished claim in
            # memory when SQLite is busy so the next scheduler loop can retry
            # instead of silently losing ownership bookkeeping.
            try:
                await self._broker.release_claims(
                    [claim.lease_tuple()],
                    reason="执行协程提前结束，等待重新执行",
                )
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_contention(exc):
                    raise
                self._defer_for_contention("releasing a finished job claim")
                continue
            self._running.pop(job_uuid, None)
        return any(claim.task.done() for claim in self._running.values())

    async def run(self) -> None:
        lease_seconds = max(int(settings.JOB_SCHEDULER_LEASE_SECONDS), 10)
        while True:
            try:
                lease_acquired = await self._broker.acquire_scheduler_lease(
                    self._owner_id, lease_seconds=lease_seconds
                )
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_contention(exc):
                    raise
                delay = self._defer_for_contention(
                    "acquiring the scheduler lease"
                )
                decision = self._controller.decide()
                self._publish_status(
                    decision,
                    leader=False,
                    extra_reasons=["sqlite_writer_contention"],
                )
                await asyncio.sleep(delay)
                continue
            break
        while not lease_acquired:
            decision = self._controller.decide()
            self._publish_status(
                decision,
                leader=False,
                extra_reasons=["scheduler_lease_held_by_other_process"],
            )
            logger.warning(
                "Job scheduler waiting: another process owns the lease"
            )
            await asyncio.sleep(
                min(
                    max(float(settings.JOB_SCHEDULER_POLL_SECONDS), 0.25),
                    5.0,
                )
            )
            try:
                lease_acquired = await self._broker.acquire_scheduler_lease(
                    self._owner_id, lease_seconds=lease_seconds
                )
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_contention(exc):
                    raise
                delay = self._defer_for_contention(
                    "reacquiring the scheduler lease"
                )
                self._publish_status(
                    decision,
                    leader=False,
                    extra_reasons=["sqlite_writer_contention"],
                )
                await asyncio.sleep(delay)

        self._broker.mark_worker_started()
        try:
            while True:
                try:
                    recovered = await self._broker.recover_pending_jobs()
                except sqlite3.OperationalError as exc:
                    if not _is_sqlite_contention(exc):
                        raise
                    delay = self._defer_for_contention(
                        "recovering pending jobs"
                    )
                    decision = self._controller.decide()
                    self._publish_status(
                        decision,
                        leader=True,
                        extra_reasons=["sqlite_writer_contention"],
                    )
                    await asyncio.sleep(delay)
                    continue
                break
            self._contention_failures = 0
            self._contention_retry_at = 0.0
            logger.info(
                "Adaptive job scheduler started as %s (recovered %d jobs)",
                self._owner_id,
                recovered,
            )
            await self._broker.record_operational_event(
                "scheduler_restart",
                "scheduler",
            )
            next_sample_at = 0.0
            next_heartbeat_at = 0.0
            next_reconciliation_at = (
                time.monotonic() + _STATE_RECONCILIATION_INTERVAL_SECONDS
            )
            next_expired_claim_recovery_at = (
                time.monotonic() + _EXPIRED_CLAIM_RECOVERY_INTERVAL_SECONDS
            )
            next_slo_evaluation_at = 0.0
            while True:
                # asyncio.wait_for can consume a cancellation when its inner
                # event completes in the same loop turn. Honour the retained
                # cancellation count before dispatching or waiting again.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError
                cleanup_deferred = await self._collect_finished()
                now = time.monotonic()
                if self._last_decision is None or now >= next_sample_at:
                    self._last_decision = self._controller.decide()
                    next_sample_at = now + max(
                        float(settings.JOB_SCHEDULER_SAMPLE_SECONDS), 0.5
                    )
                    # Publish admission before trying to claim: otherwise the
                    # first loop after a pressure spike could start one heavy
                    # job using the previous sample's policy.
                    self._publish_status(
                        self._last_decision,
                        leader=True,
                    )
                if cleanup_deferred:
                    self._publish_status(
                        self._last_decision,
                        leader=True,
                        extra_reasons=["sqlite_writer_contention"],
                    )
                    await asyncio.sleep(
                        max(self._contention_retry_at - time.monotonic(), 0.01)
                    )
                    continue
                if now >= next_heartbeat_at:
                    try:
                        lease_ok = await self._broker.renew_scheduler_lease(
                            self._owner_id, lease_seconds=lease_seconds
                        )
                        if not lease_ok:
                            self._publish_status(
                                self._last_decision,
                                leader=False,
                                extra_reasons=["scheduler_lease_lost"],
                            )
                            raise RuntimeError("scheduler leader lease lost")
                        await self._broker.heartbeat_claims(
                            [
                                claim.lease_tuple()
                                for claim in self._running.values()
                            ],
                            lease_seconds=lease_seconds,
                        )
                    except sqlite3.OperationalError as exc:
                        if not _is_sqlite_contention(exc):
                            raise
                        delay = self._defer_for_contention(
                            "renewing scheduler leases"
                        )
                        next_heartbeat_at = time.monotonic() + delay
                    else:
                        self._broker.mark_worker_heartbeat()
                        await self._broker.flush_operational_counters()
                        next_heartbeat_at = now + max(lease_seconds / 3, 5)

                contention_reasons: list[str] = []
                if now >= next_expired_claim_recovery_at:
                    try:
                        await self._broker.recover_expired_claims(
                            limit=_EXPIRED_CLAIM_RECOVERY_LIMIT,
                            source="periodic",
                        )
                    except sqlite3.OperationalError as exc:
                        if not _is_sqlite_contention(exc):
                            logger.exception("Expired job claim recovery failed")
                        else:
                            self._defer_for_contention(
                                "recovering expired job claims"
                            )
                            contention_reasons.append(
                                "sqlite_writer_contention"
                            )
                    except Exception:
                        logger.exception("Expired job claim recovery failed")
                    finally:
                        next_expired_claim_recovery_at = (
                            now + _EXPIRED_CLAIM_RECOVERY_INTERVAL_SECONDS
                        )
                if now >= next_reconciliation_at:
                    try:
                        await self._broker.reconcile_terminal_backtest_jobs()
                    except sqlite3.OperationalError as exc:
                        if not _is_sqlite_contention(exc):
                            logger.exception(
                                "Terminal job state reconciliation failed"
                            )
                        else:
                            self._defer_for_contention(
                                "reconciling terminal job state"
                            )
                            contention_reasons.append(
                                "sqlite_writer_contention"
                            )
                    except Exception:
                        logger.exception(
                            "Terminal job state reconciliation failed"
                        )
                    finally:
                        next_reconciliation_at = (
                            now + _STATE_RECONCILIATION_INTERVAL_SECONDS
                        )
                if now >= next_slo_evaluation_at:
                    try:
                        await self._broker.evaluate_slo_alerts()
                    except sqlite3.OperationalError as exc:
                        if not _is_sqlite_contention(exc):
                            logger.exception("SLO alert evaluation failed")
                        else:
                            self._defer_for_contention(
                                "evaluating SLO alerts"
                            )
                            contention_reasons.append(
                                "sqlite_writer_contention"
                            )
                    except Exception:
                        logger.exception("SLO alert evaluation failed")
                    finally:
                        next_slo_evaluation_at = now + max(
                            float(settings.JOB_SLO_EVALUATION_SECONDS),
                            5.0,
                        )
                if now >= self._contention_retry_at:
                    try:
                        await self._start_available_jobs(
                            self._last_decision.capacity
                        )
                    except sqlite3.OperationalError as exc:
                        if not _is_sqlite_contention(exc):
                            raise
                        self._defer_for_contention("dispatching jobs")
                        contention_reasons.append("sqlite_writer_contention")
                    else:
                        self._contention_failures = 0
                        self._contention_retry_at = 0.0
                else:
                    contention_reasons.append("sqlite_writer_contention")
                self._publish_status(
                    self._last_decision,
                    leader=True,
                    extra_reasons=contention_reasons,
                )

                if self._running:
                    await asyncio.wait(
                        [claim.task for claim in self._running.values()],
                        timeout=min(
                            max(float(settings.JOB_SCHEDULER_POLL_SECONDS), 0.1),
                            5.0,
                        ),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await self._broker.wait_for_work(
                        max(float(settings.JOB_SCHEDULER_POLL_SECONDS), 0.1)
                    )
        finally:
            claims = list(self._running.values())
            for claim in claims:
                claim.task.cancel()
            await asyncio.gather(
                *(claim.task for claim in claims),
                return_exceptions=True,
            )
            try:
                await self._broker.release_claims(
                    [claim.lease_tuple() for claim in claims],
                    reason="服务关闭，等待重新执行",
                )
            except Exception:
                logger.exception(
                    "Unable to release active job claims during scheduler shutdown"
                )
            self._running.clear()
            try:
                await self._broker.release_scheduler_lease(self._owner_id)
            except Exception:
                logger.exception(
                    "Unable to release scheduler lease during shutdown"
                )
            finally:
                try:
                    self._broker.mark_worker_stopped()
                finally:
                    logger.info("Adaptive job scheduler stopped")
