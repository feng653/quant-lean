"""Shared response contracts for the public REST API."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.api.timestamps import serialize_utc_timestamp

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    data: T
    detail: str | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    limit: int


class ExtensibleModel(BaseModel):
    """Typed stable fields while retaining versioned extension fields."""

    model_config = ConfigDict(extra="allow")


class IdJobResponse(BaseModel):
    experiment_id: int
    job_id: str


class ResearchRerunBody(BaseModel):
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    allow_environment_drift: bool = False


class ResearchRerunResponse(ExtensibleModel):
    experiment_id: int
    job_id: str
    source_experiment_id: int
    replay_mode: str
    environment_differences: list[dict[str, Any]] = Field(default_factory=list)


class ResearchManifestResponse(ExtensibleModel):
    experiment_id: int
    schema_version: str
    manifest: dict[str, Any]
    manifest_hash: str
    created_at: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("created_at", mode="before")
    @classmethod
    def serialize_created_at(cls, value: Any) -> str | None:
        return serialize_utc_timestamp(value)


class UserResponse(ExtensibleModel):
    id: int
    username: str
    display_name: str | None = None
    email: str | None = None
    is_admin: bool
    is_active: bool = True
    permissions: list[str] = Field(default_factory=list)


class AuthResponse(ExtensibleModel):
    user_id: int
    username: str
    display_name: str | None = None
    email: str | None = None
    is_admin: bool
    access_token: str
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str


class IdResponse(ExtensibleModel):
    deployment_id: int | None = None
    portfolio_id: int | None = None
    job_id: str | None = None
    revision: int | None = None


class ResearchRiskSummaryResponse(BaseModel):
    legacy: bool
    no_manifest: bool
    legacy_no_manifest: bool
    manifest_integrity_valid: bool
    non_point_in_time: bool
    current_constituents: bool
    survivorship_bias: bool
    invalid_market_data: bool
    warnings: list[str] = Field(default_factory=list)
    warning_severity: str = "high"
    trust_tier: str = "legacy_or_incomplete"
    live_eligible: bool = False
    pit_eligible: bool = False
    paper_eligible: bool = False
    legacy_read_only: bool = True
    eligibility_code: str = "legacy_manifest_missing"


class ExperimentResponse(ExtensibleModel):
    id: int
    user_id: int | None = None
    name: str | None = None
    strategy_id: str
    strategy_category: str | None = None
    is_starred: bool = False
    labels: list[str] = Field(default_factory=list)
    pool_preset: str | None = None
    pool_custom_codes: str | list[str] | None = None
    pool_industries: str | list[str] | None = None
    train_start: str | None = None
    train_end: str | None = None
    test_start: str | None = None
    test_end: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    mode: str | None = None
    data_access_policy: str = "allow_fetch"
    research_trust: dict[str, Any] | None = None
    status: str = "completed"
    progress_pct: int | float = 0
    progress_message: str | None = None
    error_log: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    source_experiment_id: int | None = None
    sharpe_ratio: float | None = None
    annual_return: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    research_risk_summary: ResearchRiskSummaryResponse | None = None

    @field_validator(
        "created_at",
        "started_at",
        "completed_at",
        mode="before",
    )
    @classmethod
    def serialize_timestamps(cls, value: Any) -> str | None:
        return serialize_utc_timestamp(value)


ExperimentSortKey = Literal[
    "created_at",
    "annual_return",
    "sharpe_ratio",
    "max_drawdown",
    "strategy_id",
    "status",
]
ExperimentSortOrder = Literal["asc", "desc"]


class ExperimentPage(Page[ExperimentResponse]):
    sort_by: ExperimentSortKey
    sort_order: ExperimentSortOrder


class ParameterPresetResponse(ExtensibleModel):
    id: int
    user_id: int
    name: str
    strategy_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    mode: str = "batch"
    pool_preset: str | None = None
    pool_custom_codes: list[str] = Field(default_factory=list)
    pool_industries: list[str] = Field(default_factory=list)
    source_experiment_id: int | None = None
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    labels: list[str] = Field(default_factory=list)
    is_default: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def serialize_timestamps(cls, value: Any) -> str | None:
        return serialize_utc_timestamp(value)


class MetricsResponse(ExtensibleModel):
    experiment_id: int
    cumulative_return: float | None = None
    annual_return: float | None = None
    annualized_return: float | None = None
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    total_trades: int | None = None


class EquityPointResponse(BaseModel):
    date: str
    equity: float
    benchmark: float | None = None
    daily_return: float | None = None
    drawdown: float | None = None


class TradeResponse(ExtensibleModel):
    id: int
    experiment_id: int
    date: str
    signal_date: str | None = None
    code: str
    action: str
    price: float
    shares: int
    amount: float
    cost: float
    signal_strategy: str | None = None
    signal_score: float | None = None


class DeploymentResponse(ExtensibleModel):
    id: int
    user_id: int | None = None
    strategy_id: str
    strategy_category: str
    display_name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    mode: str
    source_experiment_id: int | None = None
    source_model_artifact_id: int | None = None
    requires_retraining: bool | int = False
    retrain_frequency: str | None = None
    position_config: dict[str, Any] = Field(default_factory=dict)
    status: str
    status_tags: list[str] = Field(default_factory=list)
    user_notes: str | None = None
    research_risk_snapshot: dict[str, Any] | None = None
    research_risk_snapshot_hash: str | None = None
    research_generation_id: str | None = None
    research_source_id: str | None = None
    research_window_start: str | None = None
    research_window_end: str | None = None


class AllocationResponse(ExtensibleModel):
    deployment_id: int
    target_weight_bps: int
    min_weight_bps: int = 0
    max_weight_bps: int = 10_000
    locked: bool | int = False
    risk_budget_bps: int | None = None


class PortfolioResponse(ExtensibleModel):
    id: int
    user_id: int | None = None
    name: str
    total_capital: float
    rebalance_frequency: str
    allocations: list[AllocationResponse] = Field(default_factory=list)
    cash_balance: float | None = None
    current_revision: int | None = None
    status: str | None = None


class PositionResponse(ExtensibleModel):
    id: int | None = None
    portfolio_id: int | None = None
    deployment_id: int | None = None
    date: str
    code: str
    name: str | None = None
    deployment_name: str | None = None
    shares: int
    avg_cost: float
    close_price: float
    market_value: float
    unrealized_pnl: float
    weight_in_portfolio: float


class SignalResponse(ExtensibleModel):
    id: int | None = None
    deployment_id: int
    deployment_name: str | None = None
    date: str
    code: str
    action: str
    score: float
    weight: float
    confidence: float
    reasoning: str


class OrderResponse(ExtensibleModel):
    id: int
    deployment_id: int
    portfolio_id: int | None = None
    deployment_name: str | None = None
    date: str
    code: str
    action: str
    price: float
    shares: int
    amount: float
    cost: float
    order_type: str
    status: str
    reject_reason: str | None = None
    filled_at: str | None = None
    created_at: str | None = None


class PoolResponse(ExtensibleModel):
    id: str
    name: str = ""
    description: str = ""
    count: int = 0
    index_code: str | None = None
    declared_count: int | None = None
    availability: dict[str, Any] | None = None
    lineage: dict[str, Any] | None = None
    risk_warnings: list[str] = Field(default_factory=list)


class CacheInfoResponse(ExtensibleModel):
    pool_id: str
    exists: bool
    date_start: str | None = None
    date_end: str | None = None
    n_dates: int = 0
    n_stocks: int = 0
    file_size_mb: float = 0
    last_updated: str | None = None
    schema_version: int = 1
    fields: list[str] = Field(default_factory=list)


class DataUpdateStatusResponse(BaseModel):
    broker_status: dict[str, Any]
    governance_refresh_status: dict[str, Any] = Field(default_factory=dict)
    research_refresh_status: dict[str, Any] = Field(default_factory=dict)
    market_data_update_contract: dict[str, Any] = Field(default_factory=dict)
    research_data_contract: dict[str, Any] = Field(default_factory=dict)
    research_pools: list[dict[str, Any]] = Field(default_factory=list)
    pools_cache: list[CacheInfoResponse]
