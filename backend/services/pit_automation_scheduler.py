"""Periodic submitter for the independent durable PIT update state machine."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from backend.config import settings
from backend.dependencies import get_job_broker
from backend.services.pit_durable_update import (
    PitAutomationIdentityError,
    PitDurableUpdateStore,
    require_automation_service_identity,
)

logger = logging.getLogger("quant_platform.pit_automation_scheduler")


def _cycle_key(now: datetime | None = None) -> str:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    minutes = max(int(settings.PIT_AUTOMATION_SCAN_MINUTES), 1)
    slot = (current.hour * 60 + current.minute) // minutes
    return f"scheduled:{current.date().isoformat()}:{slot}"


async def enqueue_pit_durable_update(*, idempotency_key: str | None = None) -> str:
    require_automation_service_identity()
    key = idempotency_key or _cycle_key()
    return await get_job_broker().submit_job(
        job_type="pit_durable_update",
        params={"idempotency_key": key, "source": "pit_automation_scheduler"},
        user_id=None,
        resource_type="pit_automation",
        resource_id=key,
        deduplicate_active=True,
    )


async def run_pit_automation_scheduler() -> None:
    if not settings.PIT_AUTOMATION_AUTO_RUN:
        logger.info("PIT durable update scheduler disabled by configuration")
        return
    interval = max(int(settings.PIT_AUTOMATION_SCAN_MINUTES), 1) * 60
    while True:
        try:
            # Identity is revalidated every cycle, including deactivation.
            for retry_key in PitDurableUpdateStore().due_retry_keys():
                await enqueue_pit_durable_update(idempotency_key=retry_key)
            await enqueue_pit_durable_update()
        except PitAutomationIdentityError:
            logger.error("PIT updater skipped: automation service identity is invalid")
        except Exception:
            logger.exception("Unable to enqueue PIT durable update")
        await asyncio.sleep(interval)
