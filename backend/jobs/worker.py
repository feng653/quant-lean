"""后台任务工作进程生命周期（原 main._job_worker / _supervise_job_worker /
_stopped_critical_background_tasks / _worker_heartbeat_health /
_shutdown_background_runtime）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from backend.config import settings
from backend.jobs import broker as broker_module
from backend.jobs.handlers import execute_job
from backend.jobs.scheduler import AdaptiveJobScheduler

logger = logging.getLogger("quant_platform")


async def job_worker() -> None:
    """Run the resource-aware, lease-protected local dispatcher."""
    scheduler = AdaptiveJobScheduler(
        broker_module.get_broker(),
        execute_job,
    )
    await scheduler.run()


async def supervise_job_worker(
    *,
    max_attempts: int = 3,
    retry_base_seconds: float = 1.0,
    stable_run_seconds: float = 60.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Restart an unexpectedly failed dispatcher without hiding a crash loop."""
    attempts = max(int(max_attempts), 1)
    consecutive_failures = 0
    while True:
        started_at = monotonic()
        try:
            await job_worker()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        else:
            failure = RuntimeError(
                "background job worker stopped unexpectedly without an error"
            )

        uptime = monotonic() - started_at
        if (
            consecutive_failures
            and uptime >= max(float(stable_run_seconds), 0.0)
        ):
            consecutive_failures = 0
        consecutive_failures += 1
        logger.exception(
            "Background job worker crashed (consecutive attempt %d/%d)",
            consecutive_failures,
            attempts,
            exc_info=(
                type(failure),
                failure,
                failure.__traceback__,
            ),
        )
        if consecutive_failures >= attempts:
            logger.critical(
                "Background job worker exhausted %d consecutive restart attempts",
                attempts,
            )
            raise RuntimeError(
                "background job worker exhausted its restart budget"
            ) from failure
        delay = max(float(retry_base_seconds), 0.0) * (
            2 ** (consecutive_failures - 1)
        )
        logger.warning("Restarting background job worker in %.2fs", delay)
        await asyncio.sleep(delay)


def stopped_critical_background_tasks(app: FastAPI) -> list[str]:
    """Return stable public component names without exposing task exceptions."""
    critical_tasks = getattr(app.state, "critical_background_tasks", {})
    return sorted(name for name, task in critical_tasks.items() if task.done())


def worker_heartbeat_health(
    app: FastAPI,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Assess in-memory heartbeat freshness without a database operation."""
    broker = getattr(app.state, "job_broker", None)
    started_monotonic = getattr(
        app.state,
        "critical_background_started_monotonic",
        None,
    )
    grace_seconds = max(
        min(float(settings.JOB_SCHEDULER_LEASE_SECONDS), 60.0),
        15.0,
    )
    startup_grace = (
        started_monotonic is None
        or monotonic() - float(started_monotonic) < grace_seconds
    )
    if broker is None:
        return {
            "healthy": startup_grace,
            "online": False,
            "startup_grace": startup_grace,
            "standby": False,
            "heartbeat_at": None,
        }

    snapshot = broker.worker_health_snapshot()
    reasons = list(snapshot.get("reasons") or [])
    standby = (
        snapshot.get("leader") is False
        and "scheduler_lease_held_by_other_process" in reasons
    )
    online = snapshot.get("online") is True
    return {
        "healthy": online or startup_grace or standby,
        "online": online,
        "startup_grace": startup_grace,
        "standby": standby,
        "heartbeat_at": snapshot.get("heartbeat_at"),
    }


async def shutdown_background_runtime(
    tasks: dict[str, asyncio.Task[None]],
    broker: Any,
) -> None:
    """Stop every task and always run durable broker cleanup."""
    for task in tasks.values():
        if not task.done():
            task.cancel()
    try:
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for (name, _), result in zip(tasks.items(), results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                logger.error(
                    "Background task %s exited with an error before shutdown",
                    name,
                    exc_info=(type(result), result, result.__traceback__),
                )
    finally:
        try:
            await broker.shutdown()
        except Exception:
            logger.exception("Broker shutdown error")
