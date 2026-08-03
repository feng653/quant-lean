"""data_update 任务处理器（原 _execute_job data_update 分支）。"""

from __future__ import annotations

from typing import Any

from backend.jobs import broker as broker_module
from backend.services.maintenance import (
    DataUpdateFailedError,
    run_data_update,
)


async def handle(job: dict[str, Any]) -> None:
    """核验 PIT 双价格账本更新条件并采集证据。"""
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})

    await broker.update_job_progress(
        job_uuid,
        progress=0.1,
        message="正在核验 PIT 双价格账本更新条件",
        stage="updating_data",
    )
    await broker.raise_if_cancelled(job_uuid)
    reported_progress = 0.1

    async def report_market_data_progress(event: dict[str, Any]) -> None:
        nonlocal reported_progress
        await broker.raise_if_cancelled(job_uuid)
        completed = max(0, int(event.get("completed_codes", 0)))
        total = max(0, int(event.get("total_codes", 0)))
        source_role = str(event.get("source_role", "validation"))
        provider = str(event.get("provider", "unknown"))
        reused = bool(event.get("reused_staging", False))
        overall = max(
            0.0,
            min(float(event.get("overall_fraction", 0.0)), 1.0),
        )
        reported_progress = max(reported_progress, 0.1 + 0.8 * overall)
        role_label = {
            "primary": "主源",
            "reference": "复核源",
            "adjusted_reference": "复权差异观察源",
            "validation": "双源核验",
            "execution_binding": "双价格账本门禁",
        }.get(source_role, source_role)
        reuse_label = "（已恢复安全暂存）" if reused else ""
        await broker.update_job_progress(
            job_uuid,
            progress=reported_progress,
            result={
                "market_data_progress": {
                    "source_role": source_role,
                    "provider": provider,
                    "completed_codes": completed,
                    "total_codes": total,
                    "reused_staging": reused,
                }
            },
            message=(
                f"{role_label} {provider}{reuse_label}："
                f"{completed}/{total} 只股票"
            ),
            stage=(
                "market_data_execution_binding"
                if source_role == "execution_binding"
                else f"market_data_{source_role}"
            ),
        )

    try:
        result = await run_data_update(
            params.get("pool_id"),
            progress=report_market_data_progress,
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
            message="PIT 行情/双价格账本更新已阻断；运行时数据未变更",
            stage="market_data_failed_validation",
        )
        raise
    await broker.raise_if_cancelled(job_uuid)
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message="PIT 证据采集完成，等待独立复核与激活",
        stage="completed",
    )
