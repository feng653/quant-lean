"""research_data_refresh 任务处理器（原 _execute_job 分支）。"""

from __future__ import annotations

import logging
from typing import Any

from backend.jobs import broker as broker_module
from backend.services.research_data_refresh import (
    ResearchDataRefreshError,
    run_research_data_refresh_responsive,
)

logger = logging.getLogger("quant_platform")


async def handle(job: dict[str, Any]) -> None:
    """有界研究数据刷新；容量不足时自动安排续跑。"""
    broker = broker_module.get_broker()
    job_uuid = str(job["job_uuid"])
    params = job.get("params", {})

    await broker.update_job_progress(
        job_uuid,
        progress=0.02,
        message="正在准备有界研究数据刷新",
        stage="provider_collection",
    )

    async def report_research_refresh(event: dict[str, Any]) -> None:
        update = {
            "progress": max(
                0.02, min(float(event["overall_fraction"]), 0.99)
            ),
            "message": str(event["message"]),
            "stage": str(event["stage"]),
        }
        if event.get("cancellable") is False:
            # Enter the explicit atomic commit point first. request_cancel
            # rejects later cancellation while this stage is visible; an
            # earlier request remains detectable before file activation.
            await broker.update_job_progress(job_uuid, **update)
            await broker.raise_if_cancelled(job_uuid)
        else:
            await broker.raise_if_cancelled(job_uuid)
            await broker.update_job_progress(job_uuid, **update)

    try:
        result = await run_research_data_refresh_responsive(
            source_id=str(params.get("source_id") or ""),
            from_month=str(params.get("from_month") or "2016-01"),
            to_month=(
                str(params["to_month"])
                if params.get("to_month") is not None
                else None
            ),
            max_calls=int(params.get("max_calls") or 16),
            retry_optional_failures=not bool(params.get("continuation_of")),
            progress=report_research_refresh,
        )
    except ResearchDataRefreshError as exc:
        await broker.update_job_progress(
            job_uuid,
            result=exc.result,
            message=str(exc),
            stage="research_import",
        )
        raise
    if result.get("activation_committed") is not True:
        await broker.raise_if_cancelled(job_uuid)
    if result.get("continuation_required") is True:
        try:
            raw_user_id = int(params.get("user_id") or 0)
            continuation_job_id = await broker.submit_job(
                job_type="research_data_refresh",
                params={
                    **params,
                    "max_calls": 128,
                    "continuation_of": job_uuid,
                },
                user_id=raw_user_id if raw_user_id > 0 else None,
                resource_type="research_data_source",
                resource_id=str(params.get("source_id") or "tushare"),
                deduplicate_active=False,
            )
            result["continuation_job_id"] = continuation_job_id
            result["continuation_scheduled"] = True
        except Exception as exc:
            logger.warning(
                "Unable to schedule research refresh continuation: %s",
                type(exc).__name__,
            )
            result["continuation_scheduled"] = False
    await broker.update_job_progress(
        job_uuid,
        progress=1.0,
        status="completed",
        result=result,
        message=(
            "研究数据本批次完成并已自动安排续跑；风险与跨源差异保留为告警"
            if result.get("continuation_scheduled")
            else "研究数据刷新完成；风险与跨源差异已保留为告警"
        ),
        stage="completed",
    )
