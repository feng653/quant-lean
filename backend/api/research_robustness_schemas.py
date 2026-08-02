"""Strict HTTP contracts for post-hoc experiment robustness diagnostics."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RobustnessQuery(BaseModel):
    """Bounded query parameters; unknown evidence-like inputs are forbidden."""

    model_config = ConfigDict(extra="forbid")

    seed: int = Field(default=0, ge=0, le=4_294_967_295)
    n_bootstrap: int = Field(default=1_000, ge=100, le=5_000)
    bootstrap_method: Literal["moving", "stationary"] = "moving"
    n_slices: int = Field(default=8, ge=4, le=20)
    max_combinations: int = Field(default=256, ge=1, le=512)

    @model_validator(mode="after")
    def validate_slices(self) -> "RobustnessQuery":
        if self.n_slices % 2:
            raise ValueError("n_slices must be even")
        return self


class ResearchRobustnessResponse(BaseModel):
    """Stable top-level fields with versioned diagnostic payloads."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["research-robustness-report/v1"]
    experiment_id: int
    analysis_role: Literal["post_hoc_diagnostic"]
    selection_eligible: Literal[False]
    promotion_eligible: Literal[False]
    evidence: dict[str, Any]
    request_parameters: dict[str, Any]
    candidate_context: dict[str, Any]
    diagnostics: dict[str, dict[str, Any]]
    assumptions: list[str]
    limitations: list[str]
    workflow_notice: str
