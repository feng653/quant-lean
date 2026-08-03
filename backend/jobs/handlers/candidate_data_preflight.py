"""candidate_data_preflight 任务处理器（原 _execute_job 分支）。"""

from __future__ import annotations

from typing import Any

from backend.jobs import broker as broker_module
from backend.services import candidate_preflight_scheduler as scheduler


async def handle(job: dict[str, Any]) -> None:
    """采集候选数据预检的隔离证据；失败时保留结构化诊断。

    通过模块属性访问 ``run_candidate_preflight_job``（而非 from-import 绑定），
    保持与旧 _execute_job 函数级 import 相同的可 patch 语义。
    """
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})

    await broker.update_job_progress(
        job_uuid,
        progress=0.05,
        message="候选数据预检正在采集隔离证据",
        stage="updating_data",
    )
    await broker.raise_if_cancelled(job_uuid)
    try:
        result = await scheduler.run_candidate_preflight_job(params)
    except scheduler.CandidatePreflightJobError as exc:
        await broker.update_job_progress(
            job_uuid,
            result=exc.public_result(),
            message="候选数据预检失败；隔离区与结构化诊断已保留",
            stage="candidate_preflight_failed",
        )
        raise
    await broker.raise_if_cancelled(job_uuid)
    deferred = result.get("preflight_outcome") == (
        "deferred_insufficient_coverage"
    )
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message=(
            "候选数据预检已延后：最新完整月覆盖不足；证据仅保留在隔离区"
            if deferred
            else "候选数据预检完成；证据仅保留在隔离区"
        ),
        stage="candidate_preflight_deferred" if deferred else "completed",
    )
