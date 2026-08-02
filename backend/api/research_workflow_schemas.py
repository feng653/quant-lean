"""Strict request contracts for the preregistered research workflow."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricThreshold(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: float


class RiskAcceptance(StrictModel):
    accepted_risks: list[str] = Field(default_factory=list, max_length=64)
    rationale: str = Field(min_length=8, max_length=4000)


class CreateHypothesisBody(StrictModel):
    title: str = Field(min_length=3, max_length=200)
    falsifiable_statement: str = Field(min_length=20, max_length=8000)
    preregistered_metrics: list[MetricThreshold] = Field(
        min_length=1,
        max_length=32,
    )
    risk_acceptance: RiskAcceptance
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class UpdateHypothesisBody(StrictModel):
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=3, max_length=200)
    falsifiable_statement: str = Field(min_length=20, max_length=8000)
    preregistered_metrics: list[MetricThreshold] = Field(
        min_length=1,
        max_length=32,
    )
    risk_acceptance: RiskAcceptance


class VersionBody(StrictModel):
    expected_version: int = Field(ge=1)


class WindowProtocol(StrictModel):
    start: date
    end: date

    @model_validator(mode="after")
    def validate_order(self) -> "WindowProtocol":
        if self.start >= self.end:
            raise ValueError("window start must be earlier than end")
        return self


class ManifestPolicy(StrictModel):
    required: bool = True
    schema_version: str = "research-run-manifest/v1"
    require_clean_git: bool = True


class CreateGroupBody(StrictModel):
    hypothesis_id: int = Field(ge=1)
    name: str = Field(min_length=3, max_length=200)
    strategy_id: str = Field(min_length=1, max_length=128)
    selection_protocol: WindowProtocol
    locked_protocol: WindowProtocol
    manifest_policy: ManifestPolicy = Field(default_factory=ManifestPolicy)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    @model_validator(mode="after")
    def validate_protocols(self) -> "CreateGroupBody":
        if self.selection_protocol.end >= self.locked_protocol.start:
            raise ValueError(
                "selection window must end before locked-test window starts"
            )
        return self


class GroupTransitionBody(VersionBody):
    target_status: Literal["active", "closed"]


class LinkTrialBody(StrictModel):
    experiment_id: int = Field(ge=1)
    role: Literal["selection", "locked_test"]
    expected_group_version: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class CreateReportBody(StrictModel):
    report_type: Literal["selection", "final"]
    expected_group_version: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class CreatePromotionBody(StrictModel):
    report_id: int = Field(ge=1)
    rationale: str = Field(min_length=8, max_length=4000)
    expected_group_version: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class PromotionTransitionBody(StrictModel):
    expected_version: int = Field(ge=1)
    target_status: Literal["reviewed", "approved", "rejected", "revoked"]
    rationale: str | None = Field(default=None, max_length=4000)


JsonObject = dict[str, Any]
