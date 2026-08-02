"""Durable scheduling and public evidence for trained-model lifecycles."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from backend.config import settings
from backend.dependencies import get_job_broker

logger = logging.getLogger("quant_platform.model_lifecycle")

_FREQUENCY_MONTHS = {"monthly": 1, "quarterly": 3}
_FREQUENCY_DAYS = {"daily": 1, "weekly": 7}
_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:\\|/)(?:[^ \n\r\t:]+[\\/])+[^ \n\r\t:]+"
)


def _parse_utc(value: object) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip().replace(" ", "T")
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def next_retrain_at(deployment: dict[str, Any]) -> datetime | None:
    """Return the next UTC due time from immutable strategy frequency."""
    frequency = str(deployment.get("retrain_frequency") or "never").lower()
    anchor = (
        _parse_utc(deployment.get("last_retrain_at"))
        or _parse_utc(deployment.get("deployed_at"))
        or _parse_utc(deployment.get("created_at"))
    )
    if anchor is None or frequency == "never":
        return None
    if frequency in _FREQUENCY_DAYS:
        due = anchor + timedelta(days=_FREQUENCY_DAYS[frequency])
    elif frequency in _FREQUENCY_MONTHS:
        due = _add_months(anchor, _FREQUENCY_MONTHS[frequency])
    else:
        return None
    last_attempt = _parse_utc(deployment.get("last_attempt_at"))
    if last_attempt is not None:
        retry_after = last_attempt + timedelta(
            hours=max(int(settings.MODEL_RETRAIN_FAILURE_RETRY_HOURS), 1)
        )
        due = max(due, retry_after)
    return due


def public_failure(error: object) -> dict[str, str] | None:
    """Return useful failure evidence without exposing host storage paths."""
    if not error:
        return None
    raw = str(error).strip()
    if not raw:
        return None
    kind, separator, message = raw.partition(":")
    safe_code = kind.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", safe_code):
        safe_code = "RetrainFailed"
    safe_message = _PATH_PATTERN.sub("[redacted-path]", message if separator else raw)
    return {
        "code": safe_code if separator else "RetrainFailed",
        "message": safe_message.strip()[:2000],
    }


async def enqueue_due_retrains(
    *,
    now: datetime | None = None,
) -> list[str]:
    """Submit one idempotent retrain job per due active deployment."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    async with aiosqlite.connect(
        str(settings.abs_path(settings.TRADING_SIM_DB))
    ) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            """
            SELECT d.id, d.user_id, d.display_name, d.retrain_frequency,
                   d.last_retrain_at, d.deployed_at, d.created_at,
                   (
                       SELECT MAX(COALESCE(a.completed_at, a.created_at))
                       FROM model_retrain_attempts a
                       WHERE a.deployment_id=d.id
                         AND a.status='failed'
                   ) AS last_attempt_at
            FROM deployments d
            WHERE d.status='active' AND d.requires_retraining=1
              AND d.retrain_frequency IN (
                  'daily', 'weekly', 'monthly', 'quarterly'
              )
            ORDER BY d.id
            """
        )
        deployments = [dict(row) for row in await cursor.fetchall()]

    broker = get_job_broker()
    recent_jobs, _ = await broker.query_jobs(
        include_all=True,
        include_system=True,
        job_type="retrain",
        page=1,
        page_size=10_000,
    )
    cooldown = timedelta(
        hours=max(int(settings.MODEL_RETRAIN_FAILURE_RETRY_HOURS), 1)
    )
    terminal_cooldown: dict[int, datetime] = {}
    for job in recent_jobs:
        if job.get("status") not in {"failed", "cancelled"}:
            continue
        try:
            resource_id = int(job.get("resource_id"))
        except (TypeError, ValueError):
            continue
        terminal_at = (
            _parse_utc(job.get("completed_at"))
            or _parse_utc(job.get("updated_at"))
            or _parse_utc(job.get("created_at"))
        )
        if terminal_at is not None:
            terminal_cooldown[resource_id] = max(
                terminal_cooldown.get(
                    resource_id,
                    datetime.min.replace(tzinfo=timezone.utc),
                ),
                terminal_at + cooldown,
            )
    submitted: list[str] = []
    for deployment in deployments:
        due = next_retrain_at(deployment)
        if due is None or due > current:
            continue
        deployment_id = int(deployment["id"])
        if terminal_cooldown.get(deployment_id, current) > current:
            continue
        job_uuid = await broker.submit_job(
            job_type="retrain",
            params={
                "deployment_id": deployment_id,
                "user_id": int(deployment["user_id"]),
                "source": "model_lifecycle_scheduler",
                "scheduled_for": due.isoformat(),
            },
            user_id=int(deployment["user_id"]),
            display_name=f"自动重训练 · {deployment.get('display_name') or deployment_id}",
            resource_type="deployment",
            resource_id=deployment_id,
            deduplicate_active=True,
        )
        submitted.append(job_uuid)
    return submitted


async def run_model_retrain_scheduler() -> None:
    """Periodically inspect deployment due dates and submit durable jobs."""
    if not settings.MODEL_RETRAIN_AUTO_RUN:
        logger.info("Model retrain scheduler disabled by configuration")
        return
    minutes = max(int(settings.MODEL_RETRAIN_SCAN_MINUTES), 1)

    async def scan() -> None:
        try:
            submitted = await enqueue_due_retrains()
            if submitted:
                logger.info("Queued %d due model retrain job(s)", len(submitted))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to enqueue due model retraining")

    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(
        scan,
        trigger=IntervalTrigger(minutes=minutes),
        id="model-retrain-due-scan",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=max(minutes * 60, 60),
    )
    scheduler.start()
    logger.info("Model retrain scheduler enabled: scan every %d minute(s)", minutes)
    await scan()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


def parse_json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
