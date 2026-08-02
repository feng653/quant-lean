"""Readiness and local validation APIs for future live broker adapters."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.dependencies import require_permission
from backend.execution.base import OrderRequest, OrderValidationResult
from backend.execution.registry import get_execution_registry
from backend.services.live_readiness import build_live_readiness_report

router = APIRouter(prefix="/api/execution", tags=["Execution"])


class ValidateOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    order: OrderRequest


@router.get("/live-readiness")
def get_live_readiness(
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """Return the machine-readable, fail-closed live certification gate."""
    report = build_live_readiness_report()
    return {"data": report.model_dump(mode="json")}


@router.get("/adapters/readiness")
def get_adapter_readiness(
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """Return side-effect-free adapter configuration and SDK readiness."""
    registry = get_execution_registry()
    return {
        "data": {
            "adapters": [
                {
                    "adapter_id": adapter.adapter_id,
                    "display_name": adapter.display_name,
                    "capabilities": adapter.capabilities.model_dump(mode="json"),
                    "health": adapter.health().model_dump(mode="json"),
                }
                for adapter in registry.list()
            ]
        }
    }


@router.post("/orders/validate")
def validate_order(
    body: ValidateOrderBody,
    user: dict[str, Any] = Depends(require_permission("trading:execute")),
) -> dict[str, Any]:
    """Validate an order locally without connecting to or calling a broker."""
    registry = get_execution_registry()
    try:
        adapter = registry.get(body.adapter_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error.args[0])) from error

    capabilities = adapter.capabilities
    health = adapter.health()
    errors = adapter.validate_order(body.order)
    capability_supported = (
        body.order.order_type in capabilities.supported_order_types
    )
    valid = not errors
    result = OrderValidationResult(
        adapter_id=adapter.adapter_id,
        valid=valid,
        capability_supported=capability_supported,
        adapter_ready=health.ready,
        submission_enabled=capabilities.live_order_submission,
        can_submit=(
            valid
            and capability_supported
            and health.ready
            and capabilities.live_order_submission
        ),
        errors=errors,
        warnings=(
            []
            if health.ready
            else ["适配器当前不可用于实盘提交；本接口未连接券商，也不会下单"]
        ),
        health=health,
    )
    return {"data": result.model_dump(mode="json")}
