"""retrain 任务处理器（原 _execute_job retrain 分支）。"""

from __future__ import annotations

from typing import Any

from backend.jobs import broker as broker_module
from backend.services.maintenance import retrain_deployment


async def handle(job: dict[str, Any]) -> None:
    """重新训练部署模型。"""
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})

    await broker.update_job_progress(
        job_uuid,
        progress=0.1,
        message="正在重新训练模型",
        stage="training",
    )
    await broker.raise_if_cancelled(job_uuid)
    result = await retrain_deployment(
        int(params["deployment_id"]),
        int(job.get("user_id") or params["user_id"]),
    )
    await broker.raise_if_cancelled(job_uuid)
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message="模型重训练完成",
        stage="completed",
    )
