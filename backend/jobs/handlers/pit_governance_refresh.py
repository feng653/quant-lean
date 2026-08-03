"""pit_governance_refresh 任务处理器（原 _execute_job 分支）。"""

from __future__ import annotations

from typing import Any

from backend.jobs import broker as broker_module
from backend.services.maintenance import (
    DataUpdateFailedError,
    run_pit_governance_refresh,
)


async def handle(job: dict[str, Any]) -> None:
    """刷新官方 PIT 治理证据；不更新行情或双价格账本。"""
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})

    await broker.update_job_progress(
        job_uuid,
        progress=0.05,
        message="正在刷新官方 PIT 治理证据（不更新行情或双价格账本）",
        stage="pit_governance_collection",
    )
    await broker.raise_if_cancelled(job_uuid)
    try:
        result = await run_pit_governance_refresh(
            params.get("pool_id"),
            actor_user_id=int(
                job.get("user_id")
                or params.get("user_id")
                or params.get("actor_user_id")
                or 0
            ),
        )
    except DataUpdateFailedError as exc:
        await broker.update_job_progress(
            job_uuid,
            result=exc.result,
            message="PIT 治理证据刷新失败；未触发行情更新",
            stage="pit_governance_collection",
        )
        raise
    await broker.raise_if_cancelled(job_uuid)
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message="PIT 治理证据已写入隔离区，等待独立复核；行情未更新",
        stage="completed",
    )
