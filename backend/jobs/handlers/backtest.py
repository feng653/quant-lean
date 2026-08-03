"""backtest 任务处理器（原 main._execute_job backtest 分支）。"""

from __future__ import annotations

from typing import Any

from backend.execution.backtest_runner import run_experiment


async def handle(job: dict[str, Any]) -> None:
    """执行一次可复现回测并原子落库。"""
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})
    exp_id = params.get("experiment_id")
    if not exp_id:
        raise ValueError("backtest job missing experiment_id")
    await run_experiment(int(exp_id), job_uuid)
