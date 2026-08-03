"""simulation_backfill 任务处理器（原 main._execute_job simulation_backfill 分支）。"""

from __future__ import annotations

from typing import Any

from backend.jobs import broker as broker_module
from backend.services.simulation import run_simulation_backfill


async def handle(job: dict[str, Any]) -> None:
    """按历史交易日回放模拟盘，并周期性回报进度。"""
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})

    async def report_backfill_progress(progress: float) -> None:
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=progress,
            message="正在回放历史交易日",
            stage="simulation_backfill",
        )

    await broker.raise_if_cancelled(job_uuid)
    result = await run_simulation_backfill(
        int(job.get("user_id") or params.get("user_id")),
        str(params["start_date"]),
        str(params["end_date"]),
        report_backfill_progress,
        portfolio_id=params.get("portfolio_id"),
        restart=bool(params.get("restart", False)),
    )
    await broker.raise_if_cancelled(job_uuid)
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message="历史回放完成",
        stage="completed",
    )
