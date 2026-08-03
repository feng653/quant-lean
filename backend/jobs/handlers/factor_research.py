"""factor_research 任务处理器（原 _execute_job factor_research 分支）。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from backend.jobs import broker as broker_module
from backend.services import factor_research


async def handle(job: dict[str, Any]) -> None:
    """执行因子研究并保存不可变证据；参数错误以结构化结果失败。

    通过模块属性访问 ``execute_factor_research``（而非 from-import 绑定），
    保持与旧 _execute_job 函数级 import 相同的可 patch 语义。
    """
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})
    owner_user_id = job.get("user_id")

    async def report_factor_progress(
        progress: float,
        message: str,
        stage: str,
    ) -> None:
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=progress,
            message=message,
            stage=stage,
        )

    try:
        body = factor_research.FactorResearchBody.model_validate(params)
        result = await factor_research.execute_factor_research(
            body,
            owner_user_id=int(owner_user_id or 0),
            progress=report_factor_progress,
            source_job_uuid=job_uuid,
        )
        await broker.raise_if_cancelled(job_uuid)
    except ValidationError:
        error = factor_research.FactorResearchExecutionError(
            code="factor_research_request_invalid",
            message="因子研究任务参数无效，无法安全执行",
        )
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="failed",
            result=error.public_result(),
            error=error.message,
            message=error.message,
            stage="failed",
        )
        return
    except factor_research.FactorResearchExecutionError as exc:
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="failed",
            result=exc.public_result(),
            error=exc.message,
            message=exc.message,
            stage="failed",
        )
        return
    run = result["run"]
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result={
            "run_id": run["run_id"],
            "dataset_digest": run["dataset_digest"],
            "result_digest": run["result_digest"],
        },
        message="因子研究完成并保存不可变证据",
        stage="completed",
    )
