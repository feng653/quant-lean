"""任务类型分派注册表（原 main._execute_job）。

每个 job_type 一个 handler 模块（backend/jobs/handlers/<type>.py），
``execute_job`` 通过注册表分派到具体处理器。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from backend.jobs.handlers import (
    backtest,
    candidate_data_preflight,
    daily_simulation,
    data_update,
    factor_research,
    pit_durable_update,
    pit_governance_refresh,
    research_data_refresh,
    retrain,
    simulation_backfill,
)

JobHandler = Callable[[dict[str, Any]], Awaitable[None]]

HANDLERS: dict[str, JobHandler] = {
    "backtest": backtest.handle,
    "daily_simulation": daily_simulation.handle,
    "simulation_backfill": simulation_backfill.handle,
    "pit_durable_update": pit_durable_update.handle,
    "candidate_data_preflight": candidate_data_preflight.handle,
    "research_data_refresh": research_data_refresh.handle,
    "pit_governance_refresh": pit_governance_refresh.handle,
    "data_update": data_update.handle,
    "retrain": retrain.handle,
    "factor_research": factor_research.handle,
}


async def execute_job(job: dict[str, Any]) -> None:
    """执行一个已认领的任务（scheduler lease 边界内）。"""
    job_type = job.get("job_type")
    handler = HANDLERS.get(job_type)
    if handler is None:
        raise ValueError(f"unsupported job type: {job_type}")
    await handler(job)
