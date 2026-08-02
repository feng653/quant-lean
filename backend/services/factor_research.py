"""Trusted factor-research computation shared by HTTP and durable jobs.

The immutable run store is written only after every computation and integrity
digest succeeds.  A queued/running job therefore never masquerades as research
evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from backend.config import settings
from backend.data.cache import DataCache
from backend.data.factor_governance import FactorGovernanceStore
from backend.data.factor_research_protocols import (
    FactorProtocolError,
    FactorResearchProtocolStore,
    evaluate_protocol,
)
from backend.data.factor_research_runs import FactorResearchRunStore
from backend.data.universe import POOL_NAME_ALIASES, PRESET_POOLS
from backend.data.versioning import compute_dataset_version
from backend.research.factor_analysis import (
    analyze_factor_decay,
    analyze_quantile_returns,
    calculate_ic,
    compute_forward_returns,
    cross_sectional_preprocess,
    neutralize_factor_exposures,
)
from backend.research.factor_catalog import FACTOR_CATALOG, build_factor_panel
from backend.research.factor_quality import (
    analyze_implementation_quality,
    analyze_multi_factor_quality,
)
from backend.research.factor_stability import analyze_pre_registered_stability
from backend.services.factor_neutralization import (
    INDUSTRY_SCOPE,
    NeutralizationInputError,
    NeutralizationMode,
    extract_size_panel,
    load_industry_panel,
)
from backend.services.isolated_cpu import (
    IsolatedCpuError,
    IsolatedCpuTaskError,
    run_isolated_cpu,
)
from backend.version import runtime_code_evidence

FactorProgress = Callable[[float, str, str], Awaitable[None]]

_FACTOR_IDS = {str(item["factor_id"]) for item in FACTOR_CATALOG}
_SAFE_CACHE_KEY = re.compile(
    r"(?:csi(?:300|500|800|1000)|all_a|custom_[0-9a-f]{16})"
)
RESEARCH_TRUST = {
    "public_cross_validated_research_only",
    "licensed",
    "exchange_authoritative",
}


class FactorResearchWindow(BaseModel):
    """One explicitly pre-registered, inclusive research window."""

    start: str
    end: str

    @model_validator(mode="after")
    def validate_dates(self) -> "FactorResearchWindow":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.start) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", self.end
        ):
            raise ValueError("样本外窗口日期必须使用 YYYY-MM-DD")
        try:
            start = pd.Timestamp(self.start)
            end = pd.Timestamp(self.end)
        except (TypeError, ValueError) as exc:
            raise ValueError("样本外窗口必须是有效日期") from exc
        if start > end:
            raise ValueError("样本外窗口开始日期不能晚于结束日期")
        return self


class FactorStabilityConfig(BaseModel):
    """Immutable fixed train/validation/locked evaluation declaration."""

    mode: Literal["fixed_three_way"] = "fixed_three_way"
    train: FactorResearchWindow
    validation: FactorResearchWindow
    locked: FactorResearchWindow
    locked_declared: bool
    hypotheses_tested: StrictInt = Field(default=1, ge=1, le=10_000)
    correction: Literal["bonferroni"] = "bonferroni"
    alpha: float = Field(default=0.05, gt=0, lt=1)

    @model_validator(mode="after")
    def validate_pre_registration(self) -> "FactorStabilityConfig":
        if not self.locked_declared:
            raise ValueError("运行前必须声明锁定窗，运行后配置不可修改")
        boundaries = [
            pd.Timestamp(self.train.start),
            pd.Timestamp(self.train.end),
            pd.Timestamp(self.validation.start),
            pd.Timestamp(self.validation.end),
            pd.Timestamp(self.locked.start),
            pd.Timestamp(self.locked.end),
        ]
        if not all(left < right for left, right in zip(boundaries, boundaries[1:])):
            raise ValueError("train、validation、locked 必须严格有序且互不重叠")
        if isinstance(self.alpha, bool) or not math.isfinite(self.alpha):
            raise ValueError("alpha 必须是有限数字")
        return self

    def windows(self) -> list[dict[str, str]]:
        return [
            {"role": "train", **self.train.model_dump()},
            {"role": "validation", **self.validation.model_dump()},
            {"role": "locked", **self.locked.model_dump()},
        ]


class FactorProtocolReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: str = Field(pattern=r"^fproto_[0-9a-f]{32}$")
    version: StrictInt = Field(ge=1)
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FactorResearchBody(BaseModel):
    """Validated, JSON-serializable factor research request."""

    factor_id: str = "momentum_20"
    pool_preset: str = "csi300"
    pool_custom_codes: list[str] = Field(default_factory=list, max_length=500)
    start: str
    end: str
    horizons: list[StrictInt] = Field(default_factory=lambda: [1, 5, 20])
    primary_horizon: StrictInt = 5
    quantiles: int = Field(default=5, ge=2, le=10)
    winsor_method: Literal["mad", "quantile", "none"] = "mad"
    related_factor_ids: list[str] = Field(default_factory=list, max_length=5)
    rebalance_interval: StrictInt = Field(default=5, ge=1, le=252)
    default_cost_bps: float = Field(default=10.0, ge=0, le=100)
    cost_scenarios_bps: list[float] = Field(
        default_factory=lambda: [0.0, 5.0, 10.0, 20.0],
        min_length=1,
        max_length=8,
    )
    capacity_participation_rates: list[float] = Field(
        default_factory=lambda: [0.01, 0.05, 0.1],
        min_length=1,
        max_length=5,
    )
    orthogonalize: bool = True
    combination_weights: dict[str, float] = Field(default_factory=dict)
    stability: FactorStabilityConfig | None = None
    neutralization: NeutralizationMode = "none"
    industry_scope: str = INDUSTRY_SCOPE
    size_field: Literal["auto", "float_market_cap", "market_cap"] = "auto"
    protocol: FactorProtocolReference | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_numeric_inputs(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        numeric_fields = (
            "default_cost_bps",
            "cost_scenarios_bps",
            "capacity_participation_rates",
            "combination_weights",
        )
        for field in numeric_fields:
            raw = value.get(field)
            candidates = (
                raw.values()
                if isinstance(raw, dict)
                else raw
                if isinstance(raw, list)
                else [raw]
            )
            if any(isinstance(item, bool) for item in candidates):
                raise ValueError(f"{field} 不能包含布尔值")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> "FactorResearchBody":
        if self.factor_id not in _FACTOR_IDS:
            raise ValueError("factor_id 不在受支持目录中")
        if not _SAFE_CACHE_KEY.fullmatch(
            POOL_NAME_ALIASES.get(self.pool_preset, self.pool_preset)
        ) and self.pool_preset != "custom":
            raise ValueError("pool_preset 不是受支持的安全缓存标识")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.start) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", self.end
        ):
            raise ValueError("start/end 必须使用 YYYY-MM-DD")
        try:
            start = pd.Timestamp(self.start)
            end = pd.Timestamp(self.end)
        except (TypeError, ValueError) as exc:
            raise ValueError("start/end 必须是有效日期") from exc
        if start >= end:
            raise ValueError("start 必须早于 end")
        if end - start > pd.Timedelta(days=3653):
            raise ValueError("单次研究窗口不能超过 10 年")
        if (
            not self.horizons
            or len(self.horizons) > 12
            or len(set(self.horizons)) != len(self.horizons)
            or any(
                isinstance(value, bool) or value <= 0 or value > 252
                for value in self.horizons
            )
        ):
            raise ValueError("horizons 必须是至多 12 个不重复的 1..252 正整数")
        if self.primary_horizon not in self.horizons:
            raise ValueError("primary_horizon 必须包含在 horizons 中")
        if self.pool_preset == "custom" and not self.pool_custom_codes:
            raise ValueError("自定义股票池必须提供股票代码")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", self.industry_scope)
            or ".." in self.industry_scope
        ):
            raise ValueError("industry_scope 必须是安全的不透明标识")
        if any(
            not re.fullmatch(r"\d{6}", str(code).strip())
            for code in self.pool_custom_codes
        ):
            raise ValueError("股票代码必须为 6 位数字")
        if (
            len(set(self.related_factor_ids)) != len(self.related_factor_ids)
            or self.factor_id in self.related_factor_ids
            or any(item not in _FACTOR_IDS for item in self.related_factor_ids)
        ):
            raise ValueError("related_factor_ids 包含未知、重复或主因子")
        if isinstance(self.default_cost_bps, bool) or not math.isfinite(
            self.default_cost_bps
        ):
            raise ValueError("default_cost_bps 必须是有限数字")
        if (
            any(
                isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or value > 100
                for value in self.cost_scenarios_bps
            )
            or len(set(self.cost_scenarios_bps)) != len(self.cost_scenarios_bps)
            or not any(
                math.isclose(
                    self.default_cost_bps,
                    value,
                    abs_tol=1e-9,
                )
                for value in self.cost_scenarios_bps
            )
        ):
            raise ValueError(
                "cost_scenarios_bps 必须是不重复的 0..100 费率并包含默认费率"
            )
        if (
            any(
                isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
                or value > 0.25
                for value in self.capacity_participation_rates
            )
            or len(set(self.capacity_participation_rates))
            != len(self.capacity_participation_rates)
        ):
            raise ValueError(
                "capacity_participation_rates 必须是不重复的 (0, 0.25] 数字"
            )
        selected_factors = [self.factor_id, *self.related_factor_ids]
        if self.combination_weights:
            if set(self.combination_weights) != set(selected_factors):
                raise ValueError("combination_weights 必须完整覆盖所选因子")
            if any(
                isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or value > 1
                for value in self.combination_weights.values()
            ) or not math.isclose(
                sum(self.combination_weights.values()),
                1.0,
                abs_tol=1e-9,
            ):
                raise ValueError("combination_weights 必须有界且权重和为 1")
        if self.stability is not None:
            if pd.Timestamp(self.stability.train.start) < start:
                raise ValueError("train 窗口不能早于研究开始日期")
            if pd.Timestamp(self.stability.locked.end) > end:
                raise ValueError("locked 窗口不能晚于研究结束日期")
        return self


class FactorResearchExecutionError(RuntimeError):
    """Safe structured failure suitable for an API or persisted job."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 422,
        cache_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.cache_key = cache_key

    def public_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error_code": self.code,
            "message": self.message,
        }
        if self.cache_key is not None:
            result["cache_key"] = self.cache_key
            result["action"] = "refresh_in_data_center"
        return result


def factor_cache_key(body: FactorResearchBody) -> tuple[str, list[str]]:
    pool_id = POOL_NAME_ALIASES.get(body.pool_preset, body.pool_preset)
    codes = sorted(
        {
            str(code).strip()
            for code in body.pool_custom_codes
            if str(code).strip()
        }
    )
    if pool_id == "custom":
        pool_id = "custom_" + hashlib.sha256(
            ",".join(codes).encode("utf-8")
        ).hexdigest()[:16]
    return pool_id, codes


def _filter_codes(pivot: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pivot
    allowed = set(codes)
    columns = [column for column in pivot.columns if str(column[0]) in allowed]
    return pivot.loc[:, columns]


def _prepare_research_input(
    pivot: pd.DataFrame,
    codes: list[str],
    body: FactorResearchBody,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    pivot = _filter_codes(pivot, codes)
    if pivot.empty:
        raise FactorResearchExecutionError(
            code="factor_codes_missing",
            message="股票代码与缓存没有交集",
        )
    pivot.index = pd.DatetimeIndex(pd.to_datetime(pivot.index))
    pivot = pivot.sort_index()
    selected_ids = {body.factor_id, *body.related_factor_ids}
    factor_definitions = [
        item for item in FACTOR_CATALOG if item["factor_id"] in selected_ids
    ]
    factor_definition = next(
        item for item in factor_definitions if item["factor_id"] == body.factor_id
    )
    available_fields = (
        {str(column[-1]) for column in pivot.columns}
        if isinstance(pivot.columns, pd.MultiIndex)
        else {"close"}
    )
    required_fields = {
        str(field)
        for definition in factor_definitions
        for field in definition.get("required_fields") or ["close"]
    }
    missing_fields = sorted(required_fields - available_fields)
    if missing_fields:
        raise FactorResearchExecutionError(
            code="factor_fields_missing",
            message="因子所需行情字段不可用: " + ",".join(missing_fields),
        )
    start_position = max(
        0,
        int(pivot.index.searchsorted(pd.Timestamp(body.start)))
        - max(int(item["lookback"]) for item in factor_definitions),
    )
    return (
        pivot.iloc[start_position:].loc[: pd.Timestamp(body.end)],
        factor_definition,
    )


async def _load_verified_cache(
    cache: DataCache,
    cache_key: str,
) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
    """Keep the cache lock in this loop while offloading blocking parquet I/O.

    DataCache's verified-load facade is async for its lock, but the parquet
    read and quality scan beneath it are synchronous pandas work.  Calling the
    unlocked facade in a short-lived worker loop preserves the same lock
    boundary without blocking the API/scheduler event loop.
    """

    if type(cache) is not DataCache:
        # Test adapters and alternate cache implementations own their async
        # contract and may not expose DataCache internals.
        return await cache.load_pivot_with_provenance(cache_key)

    async with cache._pool_lock(cache_key):
        return await asyncio.to_thread(
            lambda: asyncio.run(cache._load_verified_pivot_unlocked(cache_key))
        )


async def _report(
    progress: FactorProgress | None,
    value: float,
    message: str,
    stage: str,
) -> None:
    if progress is not None:
        await progress(value, message, stage)


def _compute_factor_research(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure, pickle-safe factor computation executed by the spawn worker."""

    body = FactorResearchBody.model_validate(payload["body"])
    research_input = payload["research_input"]
    industries = payload.get("industries")
    market_caps = payload.get("market_caps")
    eligibility = payload.get("eligibility")
    exposure_inputs = payload["exposure_inputs"]
    if not isinstance(research_input, pd.DataFrame):
        raise TypeError("factor research input must be a DataFrame")
    if industries is not None and not isinstance(industries, pd.DataFrame):
        raise TypeError("industry input must be a DataFrame")
    if market_caps is not None and not isinstance(market_caps, pd.DataFrame):
        raise TypeError("market-cap input must be a DataFrame")
    if eligibility is not None and not isinstance(eligibility, pd.DataFrame):
        raise TypeError("factor eligibility input must be a DataFrame")

    selected_factor_ids = sorted({body.factor_id, *body.related_factor_ids})
    raw_factors = {}
    for factor_id in selected_factor_ids:
        values = build_factor_panel(research_input, factor_id).loc[
            pd.Timestamp(body.start) : pd.Timestamp(body.end)
        ]
        if eligibility is not None:
            values = values.where(
                eligibility.reindex(
                    index=values.index,
                    columns=values.columns,
                    fill_value=False,
                )
            )
        raw_factors[factor_id] = values
    neutralization_by_factor: dict[str, dict[str, Any]] = {}
    if body.neutralization != "none":
        for factor_id in selected_factor_ids:
            neutralized = neutralize_factor_exposures(
                raw_factors[factor_id],
                mode=body.neutralization,
                industries=industries,
                market_caps=market_caps,
                min_samples=body.quantiles * 2,
            )
            residuals = neutralized.pop("residuals")
            raw_factors[factor_id] = pd.DataFrame(
                residuals["values"],
                index=pd.DatetimeIndex(pd.to_datetime(residuals["dates"])),
                columns=[str(item) for item in residuals["codes"]],
                dtype=float,
            )
            neutralization_by_factor[factor_id] = neutralized
    factor = raw_factors[body.factor_id]
    if factor.empty or factor.shape[1] < body.quantiles * 2:
        raise ValueError("研究窗口或横截面样本不足，至少需要分组数两倍的股票")
    processed = cross_sectional_preprocess(
        factor,
        winsor_method=body.winsor_method,
        min_samples=body.quantiles * 2,
    )
    forward = compute_forward_returns(
        research_input,
        horizons=body.horizons,
        evaluation_end=body.end,
    )
    by_horizon = forward["horizons"]
    if eligibility is not None:
        from backend.data.point_in_time_universe import (
            origin_date_label_eligibility,
        )

        label_eligibility = origin_date_label_eligibility(eligibility)

        for horizon, values in list(by_horizon.items()):
            panel = pd.DataFrame(
                values["values"],
                index=pd.DatetimeIndex(pd.to_datetime(values["dates"])),
                columns=[str(code) for code in values["codes"]],
                dtype=float,
            )
            by_horizon[horizon] = panel.where(
                label_eligibility.reindex(
                    index=panel.index,
                    columns=panel.columns,
                    fill_value=False,
                )
            )
    min_samples = body.quantiles * 2
    ic = {
        str(horizon): calculate_ic(
            processed["values"],
            by_horizon[str(horizon)],
            min_samples=min_samples,
        )
        for horizon in sorted(body.horizons)
    }
    decay = analyze_factor_decay(
        processed["values"],
        by_horizon,
        min_samples=min_samples,
    )
    quantiles = analyze_quantile_returns(
        processed["values"],
        by_horizon[str(body.primary_horizon)],
        quantiles=body.quantiles,
        min_samples=min_samples,
    )
    processed_factors: dict[str, pd.DataFrame | dict[str, Any]] = {
        body.factor_id: processed["values"]
    }
    for factor_id in selected_factor_ids:
        if factor_id == body.factor_id:
            continue
        processed_factors[factor_id] = cross_sectional_preprocess(
            raw_factors[factor_id],
            winsor_method=body.winsor_method,
            min_samples=min_samples,
        )["values"]
    amount: pd.DataFrame | None = None
    if isinstance(research_input.columns, pd.MultiIndex):
        amount_values = {
            str(code): pd.to_numeric(
                research_input[(code, "amount")],
                errors="coerce",
            )
            for code in sorted(
                {str(column[0]) for column in research_input.columns}
            )
            if (code, "amount") in research_input.columns
        }
        if amount_values:
            amount = pd.DataFrame(
                amount_values,
                index=research_input.index,
            ).loc[pd.Timestamp(body.start) : pd.Timestamp(body.end)]
    implementation = analyze_implementation_quality(
        processed["values"],
        by_horizon[str(body.primary_horizon)],
        amount=amount,
        quantiles=body.quantiles,
        rebalance_interval=body.rebalance_interval,
        cost_scenarios_bps=body.cost_scenarios_bps,
        default_cost_bps=body.default_cost_bps,
        capacity_participation_rates=body.capacity_participation_rates,
        min_samples=min_samples,
    )
    implementation["assumptions"]["return_horizon_sessions"] = int(
        body.primary_horizon
    )
    weights = (
        body.combination_weights
        if body.combination_weights
        else {
            factor_id: 1.0 / len(selected_factor_ids)
            for factor_id in selected_factor_ids
        }
    )
    multi_factor = analyze_multi_factor_quality(
        processed_factors,
        by_horizon[str(body.primary_horizon)],
        weights=weights,
        quantiles=body.quantiles,
        min_samples=min_samples,
        orthogonalize=body.orthogonalize,
    )
    stability = None
    if body.stability is not None:
        stability_windows = body.stability.windows()
        if body.neutralization == "none":
            stability_factor = pd.concat(
                [
                    build_factor_panel(
                        research_input.loc[: pd.Timestamp(window["end"])],
                        body.factor_id,
                    ).loc[
                        pd.Timestamp(window["start"]) : pd.Timestamp(
                            window["end"]
                        )
                    ]
                    for window in stability_windows
                ],
                axis=0,
            )
        else:
            stability_factor = pd.concat(
                [
                    raw_factors[body.factor_id].loc[
                        pd.Timestamp(window["start"]) : pd.Timestamp(
                            window["end"]
                        )
                    ]
                    for window in stability_windows
                ],
                axis=0,
            )
        if eligibility is not None:
            stability_factor = stability_factor.where(
                eligibility.reindex(
                    index=stability_factor.index,
                    columns=stability_factor.columns,
                    fill_value=False,
                )
            )
        stability = analyze_pre_registered_stability(
            stability_factor,
            research_input,
            windows=stability_windows,
            horizons=list(body.horizons),
            primary_horizon=int(body.primary_horizon),
            quantiles=body.quantiles,
            winsor_method=body.winsor_method,
            hypotheses_tested=int(body.stability.hypotheses_tested),
            correction=body.stability.correction,
            alpha=body.stability.alpha,
            eligibility=eligibility,
        )
    neutralization_result: dict[str, Any] = {
        "schema_version": "factor-neutralization/v1",
        "mode": body.neutralization,
        "status": (
            "not_requested" if body.neutralization == "none" else "completed"
        ),
        "fit_window": (
            "not_applicable"
            if body.neutralization == "none"
            else "same_trading_date_only"
        ),
        "inputs": exposure_inputs,
        "primary_factor": neutralization_by_factor.get(body.factor_id),
        "factor_summaries": {
            factor_id: evidence["summary"]
            for factor_id, evidence in neutralization_by_factor.items()
        },
    }
    return {
        "processed": {
            "config": processed["config"],
            "diagnostics": processed["diagnostics"],
        },
        "ic": ic,
        "decay": decay,
        "quantiles": quantiles,
        "implementation": implementation,
        "multi_factor": multi_factor,
        "stability": stability,
        "neutralization": neutralization_result,
    }


async def execute_factor_research(
    body: FactorResearchBody,
    *,
    owner_user_id: int,
    progress: FactorProgress | None = None,
    cache: DataCache | None = None,
    store: FactorResearchRunStore | None = None,
    source_job_uuid: str | None = None,
    point_in_time_store: Any | None = None,
) -> dict[str, Any]:
    """Compute and persist one completed factor research run.

    Cancellation is cooperative around I/O stages.  The pure CPU section runs
    in a fresh, credential-free spawn process; cancellation, timeout or a crash
    terminates that process before any evidence can be persisted.
    """

    if isinstance(owner_user_id, bool) or owner_user_id <= 0:
        raise FactorResearchExecutionError(
            code="factor_research_owner_invalid",
            message="因子研究任务缺少有效用户身份",
            status_code=403,
        )
    protocol: dict[str, Any] | None = None
    if body.protocol is not None:
        try:
            protocol = await asyncio.to_thread(
                FactorResearchProtocolStore().require_locked,
                owner_user_id=owner_user_id,
                reference=body.protocol.model_dump(),
                request=body.model_dump(),
            )
        except FactorProtocolError as exc:
            raise FactorResearchExecutionError(
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
            ) from exc
    published_factor_ids = {
        str(item["factor_id"])
        for item in await asyncio.to_thread(
            FactorGovernanceStore().list_catalog,
            include_deprecated=False,
        )
        if item.get("current")
    }
    requested_factor_ids = {body.factor_id, *body.related_factor_ids}
    if not requested_factor_ids <= published_factor_ids:
        raise FactorResearchExecutionError(
            code="factor_definition_not_published",
            message="所选因子版本已弃用或尚未发布，不能创建新的研究证据",
            status_code=409,
        )
    await _report(progress, 0.05, "正在验证可信行情缓存", "loading_data")
    resolved_cache = cache or DataCache()
    cache_key, codes = factor_cache_key(body)
    try:
        from backend.data.pit_runtime import require_pit_runtime_input

        pit_input = await require_pit_runtime_input(
            pool_id=cache_key,
            required_start=body.start,
            required_end=body.end,
            purpose="research",
            requested_codes=codes,
            cache=resolved_cache,
            point_in_time_store=point_in_time_store,
            require_benchmark=False,
        )
        pivot = pit_input.market.frame
        provenance = pit_input.market.source_provenance
    except Exception as exc:
        from backend.data.pit_runtime import PitRuntimeDataError

        if isinstance(exc, PitRuntimeDataError):
            raise FactorResearchExecutionError(
                code=exc.code,
                message=(
                    "PIT 运行证据不完整；请在数据治理流程完成审核、"
                    "激活与精确绑定后重试"
                ),
                status_code=409,
                cache_key=cache_key,
            ) from exc
        raise
    if pivot is None:
        raise FactorResearchExecutionError(
            code="factor_cache_integrity_invalid",
            message="PIT 运行门禁未返回可验证的研究数据",
            status_code=409,
            cache_key=cache_key,
        )
    if pivot is None or pivot.empty:
        raise FactorResearchExecutionError(
            code="factor_cache_missing",
            message="所选股票池没有可信本地缓存，请先在数据中心更新数据",
            cache_key=cache_key,
        )
    assert provenance is not None
    source_trust = resolved_cache._source_trust(provenance)
    source_validation_ready = bool(
        provenance.get("all_batches_raw_cross_validated") is True
        and provenance.get("all_batches_adjusted_factor_validated") is True
    )
    if source_trust not in RESEARCH_TRUST or not source_validation_ready:
        raise FactorResearchExecutionError(
            code="factor_cache_source_untrusted",
            message="所选缓存的来源证据不足，不能用于生成因子研究结论",
            cache_key=cache_key,
        )
    research_input, factor_definition = await asyncio.to_thread(
        _prepare_research_input,
        pivot,
        codes,
        body,
    )
    point_in_time_timeline = None
    if cache_key in {"csi300", "csi500", "csi800", "csi1000"}:
        from backend.data.point_in_time_master import PointInTimeMasterStore
        from backend.data.point_in_time_universe import (
            PointInTimeUniverseError,
            eligibility_panel,
            resolve_point_in_time_universe,
            select_market_data_for_timeline,
        )

        try:
            point_in_time_timeline = await asyncio.to_thread(
                resolve_point_in_time_universe,
                point_in_time_store or PointInTimeMasterStore(),
                pool_id=cache_key,
                trading_dates=research_input.index,
                expected_count=PRESET_POOLS[cache_key]["expected_count"],
            )
            research_input = await asyncio.to_thread(
                select_market_data_for_timeline,
                research_input,
                point_in_time_timeline,
            )
            factor_eligibility = eligibility_panel(
                point_in_time_timeline
            )
        except PointInTimeUniverseError as exc:
            raise FactorResearchExecutionError(
                code=exc.reason,
                message=(
                    "所选预设股票池缺少完整可复核的点时成分时间线，"
                    "不能生成可信因子研究证据"
                ),
                status_code=409,
                cache_key=cache_key,
            ) from exc
    elif cache_key == "all_a":
        raise FactorResearchExecutionError(
            code="point_in_time_universe_unsupported",
            message="全 A 股池尚未建立点时上市状态时间线，不能生成可信因子研究证据",
            status_code=409,
            cache_key=cache_key,
        )
    else:
        factor_eligibility = None
    exposure_dates = pd.DatetimeIndex(
        research_input.loc[
            pd.Timestamp(body.start) : pd.Timestamp(body.end)
        ].index
    )
    exposure_codes = sorted(
        {str(column[0]) for column in research_input.columns}
        if isinstance(research_input.columns, pd.MultiIndex)
        else {str(column) for column in research_input.columns}
    )
    industries: pd.DataFrame | None = None
    market_caps: pd.DataFrame | None = None
    exposure_inputs: dict[str, Any] = {
        "industry": None,
        "size": None,
    }
    required_codes_by_date = (
        {
            day: members
            for day, members in zip(
                point_in_time_timeline.dates,
                point_in_time_timeline.members_by_date,
            )
        }
        if point_in_time_timeline is not None
        else None
    )
    try:
        if body.neutralization in {"industry", "industry+size"}:
            from backend.data.point_in_time_master import PointInTimeMasterStore

            industries, exposure_inputs["industry"] = await asyncio.to_thread(
                load_industry_panel,
                point_in_time_store or PointInTimeMasterStore(),
                dates=exposure_dates,
                codes=exposure_codes,
                scope_id=body.industry_scope,
                required_codes_by_date=required_codes_by_date,
            )
        if body.neutralization in {"size", "industry+size"}:
            market_caps, exposure_inputs["size"] = await asyncio.to_thread(
                extract_size_panel,
                research_input,
                dates=exposure_dates,
                codes=exposure_codes,
                provenance=provenance,
                requested_field=body.size_field,
                required_codes_by_date=required_codes_by_date,
            )
    except NeutralizationInputError as exc:
        raise FactorResearchExecutionError(
            code=exc.code,
            message=exc.message,
            status_code=422,
        ) from exc
    await _report(progress, 0.2, "正在计算因子截面与前瞻收益", "computing")

    try:
        computation = await run_isolated_cpu(
            "factor_research_compute",
            {
                "body": body.model_dump(),
                "research_input": research_input,
                "industries": industries,
                "market_caps": market_caps,
                "exposure_inputs": exposure_inputs,
                "eligibility": factor_eligibility,
            },
        )
        processed = computation["processed"]
        ic = computation["ic"]
        decay = computation["decay"]
        quantiles = computation["quantiles"]
        implementation = computation["implementation"]
        multi_factor = computation["multi_factor"]
        stability = computation["stability"]
        neutralization = computation["neutralization"]
    except IsolatedCpuTaskError as exc:
        if exc.original_type != "ValueError":
            raise FactorResearchExecutionError(
                code="factor_research_compute_failed",
                message="因子研究隔离计算失败，未保存任何研究证据",
                status_code=500,
            ) from exc
        raise FactorResearchExecutionError(
            code="factor_research_sample_invalid",
            message=(
                exc.message
                if exc.message
                == "研究窗口或横截面样本不足，至少需要分组数两倍的股票"
                else "因子研究样本无法完成安全计算"
            ),
        ) from exc
    except IsolatedCpuError as exc:
        raise FactorResearchExecutionError(
            code=exc.code,
            message=exc.message,
            status_code=503,
        ) from exc
    await _report(progress, 0.75, "正在生成数据与结果摘要", "digesting")
    version = await asyncio.to_thread(
        compute_dataset_version,
        research_input,
        context={
            "factor_id": body.factor_id,
            "start": body.start,
            "end": body.end,
            "horizons": body.horizons,
            "source_provenance_sha256": (provenance or {}).get("content_sha256"),
            "neutralization": body.neutralization,
            "neutralization_inputs": exposure_inputs,
            "point_in_time_timeline_hash": (
                point_in_time_timeline.timeline_hash
                if point_in_time_timeline is not None
                else None
            ),
        },
    )
    result: dict[str, Any] = {
        "schema_version": "factor-research/v4",
        "factor": factor_definition,
        "request": body.model_dump(),
        "dataset": {
            "cache_key": cache_key,
            "rows": len(research_input),
            "codes": len({str(column[0]) for column in research_input.columns}),
            "date_start": str(research_input.index.min().date()),
            "date_end": str(research_input.index.max().date()),
            "content_sha256": version.digest,
            "source_provenance": {
                "providers": (provenance or {}).get("providers", []),
                "adjustments": (provenance or {}).get("adjustments", []),
                "evidence_levels": (provenance or {}).get("evidence_levels", []),
                "content_sha256": (provenance or {}).get("content_sha256"),
                "source_trust": source_trust,
            },
            "universe": (
                point_in_time_timeline.identity()
                if point_in_time_timeline is not None
                else {
                    "point_in_time": False,
                    "reason": "custom_static_universe",
                }
            ),
        },
        "preprocessing": {
            "config": processed["config"],
            "diagnostics": processed["diagnostics"],
            "forward_label_eligibility": {
                "policy": (
                    "origin_date_membership_fixed_horizon_security_return"
                    if point_in_time_timeline is not None
                    else "static_declared_universe"
                ),
                "horizons": sorted(int(item) for item in body.horizons),
            },
        },
        "ic": ic,
        "decay": decay,
        "quantile_returns": quantiles,
        "implementation": implementation,
        "multi_factor": multi_factor,
        "stability": stability,
        "neutralization": neutralization,
        "execution": {
            "cpu_boundary": "spawn_process",
            "max_concurrent_processes": 1,
            "thread_budget": max(int(settings.JOB_CPU_THREAD_BUDGET), 1),
        },
        "runtime_code": runtime_code_evidence(),
        "limitations": [
            "结果只基于当前本地缓存，不代表未来收益。",
            (
                "预设股票池的因子横截面与前瞻标签已按每个交易日的"
                "不可变点时成分约束；固定期收益样本只按起点成员资格筛选，"
                "不会读取未来调样决定。"
                if point_in_time_timeline is not None
                else "自定义股票池是用户声明的静态集合，不代表历史指数成分。"
            ),
            (
                "固定期因子标签使用证券自身的复权研究收益；证券在持有期内退池时，"
                "该标签不模拟指数调样收盘竞价或原始价退出。"
                if point_in_time_timeline is not None
                else "静态集合标签不证明历史可投资范围。"
            ),
            (
                "本次未请求行业或规模中性化。"
                if body.neutralization == "none"
                else "中性化仅使用同一交易日可得的点时暴露，未跨日期拟合。"
            ),
            "CPU 计算在独立子进程中运行；取消、超时或崩溃后不会保存半成品研究证据。",
        ],
    }
    if protocol is not None:
        expected_dataset_digest = protocol["payload"]["data"].get(
            "expected_dataset_digest"
        )
        if (
            expected_dataset_digest is not None
            and expected_dataset_digest != version.digest
        ):
            raise FactorResearchExecutionError(
                code="protocol_dataset_version_mismatch",
                message="实际研究数据摘要与锁定协议不一致，结果未保存",
                status_code=409,
            )
        result["protocol_review"] = evaluate_protocol(protocol, result)
    await _report(progress, 0.9, "正在保存不可变研究证据", "persisting")
    try:
        run = await asyncio.to_thread(
            (store or FactorResearchRunStore()).create,
            owner_user_id=owner_user_id,
            factor_id=body.factor_id,
            request=body.model_dump(),
            result=result,
            source_job_uuid=source_job_uuid,
        )
    except Exception as exc:
        raise FactorResearchExecutionError(
            code="factor_research_persistence_failed",
            message="保存不可变研究证据失败",
            status_code=500,
        ) from exc
    result["run"] = run
    return result
