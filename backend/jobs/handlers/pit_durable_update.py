"""pit_durable_update 任务处理器（原 main._execute_job pit_durable_update 分支）。"""

from __future__ import annotations

from typing import Any

from backend.jobs import broker as broker_module
from backend.services.pit_durable_update import run_configured_pit_update


async def handle(job: dict[str, Any]) -> None:
    """推进 PIT durable update 状态机。"""
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})

    await broker.update_job_progress(
        job_uuid,
        progress=0.05,
        message="PIT durable update state machine running",
        stage="updating_data",
    )
    await broker.raise_if_cancelled(job_uuid)
    result = await run_configured_pit_update(str(params["idempotency_key"]))
    await broker.raise_if_cancelled(job_uuid)
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message="PIT durable update checkpoint committed",
        stage="completed",
    )
