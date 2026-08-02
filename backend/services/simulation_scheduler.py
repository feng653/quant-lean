"""In-process scheduler for the daily paper-trading cycle.

The application already has a durable job broker.  This module deliberately
only enqueues one simulation job per active portfolio owner; the simulation
service remains responsible for idempotency and execution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import aiosqlite

from backend.config import settings
from backend.dependencies import get_job_broker

logger = logging.getLogger("quant_platform.paper_scheduler")


def _scheduled_time() -> time:
    try:
        hour, minute = (int(part) for part in settings.PAPER_SIMULATION_RUN_TIME.split(":", 1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError("PAPER_SIMULATION_RUN_TIME must use HH:MM") from exc


async def _scheduler_cycle_exists(trade_date: str) -> bool:
    """Avoid replaying the whole EOD cycle whenever the API process restarts."""
    jobs = await get_job_broker().list_jobs()
    return any(
        job.get("job_type") == "data_update"
        and (job.get("params") or {}).get("source") == "paper_scheduler"
        and (job.get("params") or {}).get("date") == trade_date
        for job in jobs
    )


async def enqueue_daily_simulations(trade_date: str) -> int:
    """Queue one EOD run per active portfolio."""
    async with aiosqlite.connect(str(settings.abs_path(settings.TRADING_SIM_DB))) as conn:
        cursor = await conn.execute(
            "SELECT user_id, id FROM portfolios WHERE status = 'active' ORDER BY user_id, id"
        )
        portfolios = [(int(row[0]), int(row[1])) for row in await cursor.fetchall()]

    broker = get_job_broker()
    required_data_job_uuid: str | None = None
    if (
        portfolios
        and settings.PAPER_SIMULATION_REFRESH_DATA
        and settings.PIT_AUTOMATION_ACTOR_USER_ID > 0
    ):
        # This is a system prerequisite, not a user-owned maintenance job.
        # Every dependent simulation rechecks its terminal status before any
        # portfolio side effect, so FIFO ordering alone is never the safety
        # mechanism.
        required_data_job_uuid = await broker.submit_job(
            job_type="data_update",
            params={
                "pool_id": None,
                "source": "paper_scheduler",
                "date": trade_date,
                "actor_user_id": settings.PIT_AUTOMATION_ACTOR_USER_ID,
            },
            user_id=None,
            resource_type="data_pool",
            resource_id="all_governed_csi",
            deduplicate_active=True,
        )
    queued = 0
    for user_id, portfolio_id in portfolios:
        from backend.data.pit_runtime import PitRuntimeDataError
        from backend.services.simulation import require_simulation_pit_readiness

        try:
            await require_simulation_pit_readiness(
                user_id=user_id,
                start_date=trade_date,
                end_date=trade_date,
                portfolio_id=portfolio_id,
            )
        except PitRuntimeDataError as exc:
            logger.warning(
                "Skipped PIT-unready paper portfolio %s for %s: %s",
                portfolio_id,
                trade_date,
                exc.code,
            )
            continue
        await broker.submit_job(
            job_type="daily_simulation",
            params={
                "user_id": user_id,
                "date": trade_date,
                "portfolio_id": portfolio_id,
                "scheduled_date": trade_date,
                "source": "scheduler",
                "required_data_job_uuid": required_data_job_uuid,
            },
            user_id=user_id,
        )
        queued += 1
    return queued


async def run_paper_simulation_scheduler() -> None:
    """Submit daily EOD simulations at the configured local time.

    Weekends are skipped. Exchange holidays are harmless: the simulation
    service uses the latest downloaded CSI500 trading date, so a scheduler run
    before today's data arrives is idempotently deferred to the latest snapshot.
    """
    if not settings.PAPER_SIMULATION_AUTO_RUN:
        logger.info("Paper simulation scheduler disabled by configuration")
        return

    zone = ZoneInfo(settings.PAPER_SIMULATION_TIMEZONE)
    trigger = _scheduled_time()
    last_enqueued_date: str | None = None
    logger.info(
        "Paper simulation scheduler enabled: weekdays at %s %s",
        settings.PAPER_SIMULATION_RUN_TIME,
        settings.PAPER_SIMULATION_TIMEZONE,
    )

    while True:
        now = datetime.now(zone)
        trade_date = now.date().isoformat()
        if (
            now.weekday() < 5
            and now.time() >= trigger
            and last_enqueued_date != trade_date
        ):
            try:
                if await _scheduler_cycle_exists(trade_date):
                    last_enqueued_date = trade_date
                    await asyncio.sleep(30)
                    continue
                count = await enqueue_daily_simulations(trade_date)
                last_enqueued_date = trade_date
                logger.info("Queued daily simulations for %d portfolio owners on %s", count, trade_date)
            except Exception:
                logger.exception("Failed to enqueue daily paper simulations")
        await asyncio.sleep(30)
