"""HTTP boundary for user-isolated factor research preregistration."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.data.factor_research_protocols import (
    PROTOCOL_SCHEMA,
    FactorProtocolError,
    FactorResearchProtocolStore,
)
from backend.dependencies import require_permission


router = APIRouter(
    prefix="/api/factor-research/protocols",
    tags=["Factor Research"],
)


class ProtocolData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
    version_policy: Literal[
        "latest_trusted_at_execution",
        "pinned_dataset_digest",
    ] = "latest_trusted_at_execution"
    expected_dataset_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_digest_policy(self) -> "ProtocolData":
        if (self.version_policy == "pinned_dataset_digest") != (
            self.expected_dataset_digest is not None
        ):
            raise ValueError("固定数据版本策略必须且只能提供 expected_dataset_digest")
        return self


class ProtocolWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_order(self) -> "ProtocolWindow":
        if self.start >= self.end:
            raise ValueError("协议开始日期必须早于结束日期")
        return self


class ProtocolImplementation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizons: list[int] = Field(min_length=1, max_length=12)
    primary_horizon: int = Field(ge=1, le=252)
    quantiles: int = Field(ge=2, le=10)
    rebalance_interval: int = Field(ge=1, le=252)
    default_cost_bps: float = Field(ge=0, le=100)
    cost_scenarios_bps: list[float] = Field(min_length=1, max_length=8)
    neutralization: Literal["none", "industry", "size", "industry+size"]

    @model_validator(mode="after")
    def validate_implementation(self) -> "ProtocolImplementation":
        if (
            len(set(self.horizons)) != len(self.horizons)
            or any(value < 1 or value > 252 for value in self.horizons)
            or self.primary_horizon not in self.horizons
        ):
            raise ValueError("协议研究周期必须唯一、在 1..252 内并包含主周期")
        if (
            len(set(self.cost_scenarios_bps)) != len(self.cost_scenarios_bps)
            or any(value < 0 or value > 100 for value in self.cost_scenarios_bps)
            or self.default_cost_bps not in self.cost_scenarios_bps
        ):
            raise ValueError("协议成本档位必须唯一、有效并包含默认成本")
        return self


class ProtocolThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank_ic_mean_min: float = Field(default=0.02, ge=-1, le=1)
    rank_ic_ir_min: float = Field(default=0.3, ge=-100, le=100)
    long_short_mean_min: float = Field(default=0, ge=-1, le=1)


class ProtocolExportRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_strategy_export: bool = True
    require_all_thresholds: bool = True
    require_dataset_consistency: bool = True
    minimum_evidence_runs: int = Field(default=1, ge=1, le=20)


class FactorProtocolPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["factor-research-protocol/v1"] = PROTOCOL_SCHEMA
    question: str = Field(min_length=8, max_length=1000)
    hypothesis: str = Field(min_length=8, max_length=2000)
    factor_ids: list[str] = Field(min_length=1, max_length=6)
    data: ProtocolData
    window: ProtocolWindow
    implementation: ProtocolImplementation
    thresholds: ProtocolThresholds = Field(default_factory=ProtocolThresholds)
    export_rules: ProtocolExportRules = Field(
        default_factory=ProtocolExportRules
    )

    @model_validator(mode="after")
    def validate_factors(self) -> "FactorProtocolPayload":
        if len(set(self.factor_ids)) != len(self.factor_ids):
            raise ValueError("协议因子不能重复")
        return self


class CreateProtocolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=80)
    payload: FactorProtocolPayload


class CreateProtocolVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_version: int = Field(ge=1)
    payload: FactorProtocolPayload


class LockProtocolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _store() -> FactorResearchProtocolStore:
    return FactorResearchProtocolStore()


def _http_error(exc: FactorProtocolError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("")
async def list_protocols(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    try:
        rows = await asyncio.to_thread(
            _store().list,
            owner_user_id=int(user["id"]),
        )
    except FactorProtocolError as exc:
        raise _http_error(exc) from exc
    return {"data": rows}


@router.post("", status_code=201)
async def create_protocol(
    body: CreateProtocolBody,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            _store().create,
            owner_user_id=int(user["id"]),
            name=body.name,
            payload=body.payload.model_dump(),
        )
    except FactorProtocolError as exc:
        raise _http_error(exc) from exc
    return {"data": result}


@router.post("/{protocol_id}/versions", status_code=201)
async def create_protocol_version(
    protocol_id: str,
    body: CreateProtocolVersionBody,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            _store().create_version,
            owner_user_id=int(user["id"]),
            protocol_id=protocol_id,
            expected_current_version=body.expected_current_version,
            payload=body.payload.model_dump(),
        )
    except FactorProtocolError as exc:
        raise _http_error(exc) from exc
    return {"data": result}


@router.post("/{protocol_id}/versions/{version}/lock")
async def lock_protocol(
    protocol_id: str,
    version: int,
    body: LockProtocolBody,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            _store().lock,
            owner_user_id=int(user["id"]),
            protocol_id=protocol_id,
            version=version,
            payload_digest=body.payload_digest,
        )
    except FactorProtocolError as exc:
        raise _http_error(exc) from exc
    return {"data": result}
