"""daily_simulation 任务处理器（原 main._execute_job daily_simulation 分支）。"""

from __future__ import annotations

from typing import Any

from backend.jobs import broker as broker_module
from backend.services.simulation import run_daily_simulation


async def handle(job: dict[str, Any]) -> None:
    """执行模拟盘日结，并在依赖数据任务完成前等待。"""
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})
    required_data_job_uuid = params.get("required_data_job_uuid")
    if required_data_job_uuid:
        await broker.require_completed_job(
            str(required_data_job_uuid),
            expected_type="data_update",
        )
    await broker.raise_if_cancelled(job_uuid)
    result = await run_daily_simulation(
        int(job.get("user_id") or params.get("user_id")),
        params.get("date"),
        portfolio_id=params.get("portfolio_id"),
    )
    await broker.raise_if_cancelled(job_uuid)
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message="模拟盘日结完成",
        stage="completed",
    )
