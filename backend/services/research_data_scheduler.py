"""Independent, idempotent scheduler for the personal research data store."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.config import settings
from backend.dependencies import get_job_broker


logger = logging.getLogger("quant_platform.research_data_scheduler")
_SOURCE_ID = "tushare"
_LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _daily_key(now: datetime | None = None) -> str:
    current = now or datetime.now(_LOCAL_ZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_LOCAL_ZONE)
    return f"{_SOURCE_ID}:{current.astimezone(_LOCAL_ZONE).date().isoformat()}"


async def enqueue_daily_research_refresh(
    *, now: datetime | None = None
) -> str | None:
    """Submit at most one research refresh for one provider/local date."""

    if not settings.TUSHARE_TOKEN.get_secret_value():
        return None
    key = _daily_key(now)
    broker = get_job_broker()
    attempt = 1
    resource_id = key
    system_lookup = getattr(broker, "get_latest_system_job", None)
    latest = (
        await system_lookup("research_data_refresh")
        if callable(system_lookup)
        else None
    )
    latest_params = latest.get("params") if isinstance(latest, dict) else None
    if (
        isinstance(latest_params, dict)
        and latest_params.get("idempotency_key") == key
    ):
        if latest.get("status") != "failed":
            return str(latest["job_uuid"])
        previous_attempt = int(latest_params.get("scheduler_attempt") or 1)
        maximum = max(int(settings.RESEARCH_DATA_REFRESH_DAILY_MAX_ATTEMPTS), 1)
        if previous_attempt >= maximum:
            return str(latest["job_uuid"])
        updated_at = latest.get("updated_at")
        if updated_at:
            try:
                observed = datetime.fromisoformat(
                    str(updated_at).replace("Z", "+00:00")
                )
                # SQLite datetime('now') values are UTC without an offset.
                # Treating them as Asia/Shanghai shortens a one-hour cooldown
                # by eight hours and caused immediate same-day retry loops.
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                current = now or datetime.now(timezone.utc)
                if current.tzinfo is None:
                    current = current.replace(tzinfo=_LOCAL_ZONE)
                age_seconds = (
                    current.astimezone(timezone.utc)
                    - observed.astimezone(timezone.utc)
                ).total_seconds()
                cooldown = max(
                    int(settings.RESEARCH_DATA_REFRESH_RETRY_COOLDOWN_MINUTES),
                    1,
                ) * 60
                if age_seconds < cooldown:
                    return str(latest["job_uuid"])
            except (TypeError, ValueError):
                return str(latest["job_uuid"])
        attempt = previous_attempt + 1
        resource_id = f"{key}:attempt:{attempt}"
    return await broker.submit_job(
        job_type="research_data_refresh",
        params={
            "source_id": _SOURCE_ID,
            "from_month": settings.RESEARCH_DATA_REFRESH_FROM_MONTH,
            "max_calls": max(int(settings.RESEARCH_DATA_REFRESH_MAX_CALLS), 1),
            "source": "research_data_scheduler",
            "idempotency_key": key,
            "scheduler_attempt": attempt,
        },
        user_id=None,
        resource_type="research_data_daily_refresh",
        resource_id=resource_id,
        deduplicate_existing=True,
    )


async def run_research_data_scheduler() -> None:
    """Continue pending history, then probe/import the latest complete month."""

    if not settings.RESEARCH_DATA_REFRESH_AUTO_RUN:
        logger.info("Research data refresh scheduler disabled by configuration")
        return
    interval = max(int(settings.RESEARCH_DATA_REFRESH_SCAN_MINUTES), 60) * 60
    while True:
        try:
            await enqueue_daily_research_refresh()
        except Exception:
            logger.exception("Unable to enqueue daily research data refresh")
        await asyncio.sleep(interval)
