"""Background-job query, cancellation, and retry API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth.permissions import has_permission
from backend.core.security_boundaries import sanitize_diagnostic
from backend.dependencies import get_current_user, get_job_broker
from backend.jobs.broker import (
    VALID_STATUSES,
    JobQueueFullError,
    sanitize_job_payload,
)
from backend.jobs.observability import cache_quality_snapshot

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])

RETRY_PERMISSIONS = {
    "backtest": "experiments:create",
    "daily_simulation": "trading:execute",
    "simulation_backfill": "trading:execute",
    "data_update": "data:update",
    "retrain": "trading:deploy",
    "factor_research": "data:read",
}


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result["params"] = sanitize_job_payload(result.get("params") or {})
    result["result"] = sanitize_job_payload(result.get("result"))
    result["error"] = (
        sanitize_diagnostic(result.get("error"), max_length=16_384)
        if result.get("error") is not None
        else None
    )
    result["progress_message"] = (
        sanitize_diagnostic(result.get("progress_message"), max_length=500)
        if result.get("progress_message") is not None
        else None
    )
    return result


def _ensure_access(job: dict[str, Any], user: dict[str, Any]) -> None:
    if user.get("is_admin"):
        return
    if job.get("user_id") is None or int(job["user_id"]) != int(user["id"]):
        raise HTTPException(status_code=403, detail="无权访问此任务")


def _ensure_retry_permission(job: dict[str, Any], user: dict[str, Any]) -> None:
    """Re-authorize a retry against the user's current database permissions."""
    required = RETRY_PERMISSIONS.get(str(job.get("job_type")))
    if required is None:
        raise HTTPException(status_code=403, detail="此任务类型不允许通过 API 重试")
    if not has_permission(user, required):
        raise HTTPException(status_code=403, detail=f"重试任务需要当前权限: {required}")


@router.get("/summary")
async def get_job_summary(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return counts and worker health for the current user's visible jobs."""
    broker = get_job_broker()
    try:
        summary = await broker.get_summary(
            user_id=int(user["id"]),
            include_all=bool(user.get("is_admin")),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="获取任务汇总失败") from exc
    return {"data": summary}


@router.get("/observability")
async def get_job_observability(
    window_hours: int = Query(24, ge=1, le=168),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return authenticated, read-only, aggregate service SLO telemetry."""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可查看服务可观测性")
    broker = get_job_broker()
    try:
        payload, cache_quality = await asyncio.gather(
            broker.get_observability(window_hours=window_hours),
            asyncio.to_thread(cache_quality_snapshot),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="获取服务可观测性失败") from exc
    payload["cache_quality"] = cache_quality
    return {"data": payload}


@router.post("/observability/alerts/{delivery_id}/acknowledge")
async def acknowledge_job_observability_alert(
    delivery_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Acknowledge one externally delivered SLO breach as an administrator.

    The opaque ``delivery_id`` is included in the signed webhook body.  It is
    intentionally not included in aggregate observability responses.
    """
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可确认服务告警")
    broker = get_job_broker()
    try:
        acknowledged = await broker.acknowledge_slo_alert_delivery(delivery_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="确认服务告警失败") from exc
    if not acknowledged:
        raise HTTPException(status_code=404, detail="未找到可确认的已投递越界告警")
    return {"data": {"acknowledged": True}}


@router.get("/")
async def list_jobs(
    status: str | None = Query(None),
    job_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    mine: bool = Query(False),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List visible jobs with SQL-side filtering and pagination."""
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"无效任务状态: {status}")
    broker = get_job_broker()
    try:
        jobs, total = await broker.query_jobs(
            user_id=int(user["id"]),
            include_all=bool(user.get("is_admin")) and not mine,
            include_system=bool(user.get("is_admin")) and not mine,
            status=status,
            job_type=job_type,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="获取任务列表失败") from exc
    return {
        "data": {
            "items": [_public_job(job) for job in jobs],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    }


@router.get("/{job_id}")
async def get_job_detail(
    job_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    broker = get_job_broker()
    try:
        job = await broker.get_job_status(job_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="获取任务详情失败") from exc
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    _ensure_access(job, user)
    events = await broker.list_job_events(job_id)
    payload = _public_job(job)
    payload["events"] = sanitize_job_payload(events)
    return {"data": payload}


@router.delete("/{job_id}")
async def cancel_job(
    job_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    broker = get_job_broker()
    job = await broker.get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    _ensure_access(job, user)
    if job.get("status") not in ("pending", "running", "cancel_requested"):
        raise HTTPException(status_code=400, detail="只能取消排队中或运行中的任务")
    try:
        next_status = await broker.request_cancel(job_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="取消任务失败") from exc
    if next_status is None:
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    return {"data": {"job_id": job_id, "status": next_status, "cancelled": True}}


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    broker = get_job_broker()
    job = await broker.get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    _ensure_access(job, user)
    _ensure_retry_permission(job, user)
    if job.get("status") not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail="只有失败或已取消的任务可以重试")
    try:
        new_job_id = await broker.retry_job(job_id, user_id=int(user["id"]))
    except JobQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="重试任务失败") from exc
    if new_job_id is None:
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    return {"data": {"job_id": new_job_id, "retry_of": job_id}}
