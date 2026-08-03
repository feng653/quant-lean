"""Background maintenance services for data refresh and model retraining."""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso

import asyncio
import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite
import pandas as pd

from backend.config import settings
from backend.core.security_boundaries import sanitize_diagnostic
from backend.data.universe import POOL_NAME_ALIASES
from backend.services.model_artifacts import (
    RETRAIN_MANIFEST_SCHEMA,
    file_sha256,
)
from backend.services.model_serialization import contract_for_model
from backend.services.isolated_cpu import (
    IsolatedCpuError,
    IsolatedCpuTaskError,
    run_isolated_cpu,
)
from backend.services.research_manifest import canonical_sha256
from backend.services.walkforward import _split_fit_and_validation_windows
from backend.strategies.base import (
    RetrainFrequency,
    TrainedModel,
    TrainableStrategy,
    TrainingWindowContext,
)
from backend.strategies.registry import get_registry


@dataclass
class _RetrainAttemptState:
    train_window: tuple[str, str] | None = None
    validation_window: tuple[str, str] | None = None
    validation_metrics: dict[str, Any] | None = None
    candidate_path: Path | None = None
    candidate_metadata_path: Path | None = None
    final_model_path: Path | None = None
    final_metadata_path: Path | None = None
    moved_model: bool = False
    moved_metadata: bool = False
    retrain_manifest: dict[str, Any] | None = None
    retrain_manifest_hash: str | None = None
    model_sha256: str | None = None
    model_size: int | None = None
    model_version: int | None = None
    train_metrics: dict[str, Any] | None = None


class DataUpdateFailedError(RuntimeError):
    """A cache update had real errors and must not satisfy dependent jobs."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        errors = result.get("errors")
        count = len(errors) if isinstance(errors, list) else 1
        details: list[str] = []
        if isinstance(errors, list):
            for item in errors[:3]:
                if isinstance(item, dict):
                    details.append(
                        f"{item.get('pool_id', 'data_source')}:"
                        f"{item.get('error', 'unknown error')}"
                    )
                else:
                    details.append(str(item))
        suffix = f": {'; '.join(details)}" if details else ""
        super().__init__(
            f"market data update failed with {count} error(s){suffix}"
        )


DataUpdateProgress = Callable[[dict[str, Any]], Awaitable[None]]

_PIT_PACKAGE_ID = re.compile(r"^pitpkg_[0-9a-f]{32}$")
_PIT_AUTOMATION_ARTIFACT_LIMIT = 128 * 1024 * 1024


def _attest_quarantine_artifact(
    path_value: Any,
    *,
    evidence_root: Path,
    role: str,
) -> dict[str, Any]:
    """Bind an updater output to one regular file below the quarantine root."""

    path = Path(path_value)
    if path.is_symlink():
        raise ValueError(f"{role} must not be a symbolic link")
    resolved_root = evidence_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise ValueError(f"{role} escaped the PIT evidence root")
    size = resolved.stat().st_size
    if size < 1 or size > _PIT_AUTOMATION_ARTIFACT_LIMIT:
        raise ValueError(f"{role} has an invalid size")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "role": role,
        "sha256": digest.hexdigest(),
        "size_bytes": size,
    }


def _attest_automatic_collection(
    collected: Any,
    *,
    evidence_root: Path,
    requested_from: Any,
    observed_on: Any,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    package_id = str(getattr(collected, "package_id", ""))
    if not _PIT_PACKAGE_ID.fullmatch(package_id):
        raise ValueError("collector returned an invalid package identity")
    coverage_from = getattr(collected, "coverage_from", None)
    coverage_to = getattr(collected, "coverage_to", None)
    if not (
        hasattr(coverage_from, "isoformat")
        and hasattr(coverage_to, "isoformat")
        and requested_from <= coverage_from <= coverage_to <= observed_on
    ):
        raise ValueError("collector returned invalid or future coverage")
    artifacts = [
        _attest_quarantine_artifact(
            getattr(collected, attribute, None),
            evidence_root=evidence_root,
            role=role,
        )
        for attribute, role in (
            ("checkpoint_path", "checkpoint"),
            ("review_queue_path", "review_queue"),
            ("coverage_report_path", "coverage_report"),
        )
    ]
    return (
        package_id,
        coverage_from.isoformat(),
        coverage_to.isoformat(),
        artifacts,
    )


async def run_pit_governance_refresh(
    pool_id: str | None = None,
    *,
    progress: DataUpdateProgress | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Refresh official PIT governance evidence into quarantine only.

    This intentionally does *not* fetch prices or update the raw/research
    dual-price ledger.  Keeping it separate from :func:`run_data_update`
    prevents a successful governance collection from being presented as a
    successful market-data update.
    """

    from datetime import date

    from backend.data.pit_evidence_governance import PitEvidenceGovernance
    from backend.data.pit_runtime import PIT_RUNTIME_POOLS
    from backend.data.point_in_time_master import PointInTimeMasterStore
    from backend.data.sources.csindex_history import CsindexHistoryWorkflow
    from backend.data.sources.csindex_pit import CsindexOfficialCollector

    normalized_pool = (
        str(pool_id).strip().lower() if pool_id is not None else None
    )
    if normalized_pool is not None and normalized_pool not in PIT_RUNTIME_POOLS:
        result = {
            "schema_version": "pit-governance-refresh/v1",
            "status": "rejected",
            "errors": [
                {
                    "pool_id": normalized_pool,
                    "code": "point_in_time_pool_unsupported",
                    "error": (
                        "自动更新仅采集中证 PIT 治理证据；静态、自定义与 all_a "
                        "数据源已停用"
                    ),
                }
            ],
            "production_import_performed": False,
            "activation_performed": False,
        }
        raise DataUpdateFailedError(result)
    if isinstance(actor_user_id, bool) or not actor_user_id or actor_user_id < 1:
        result = {
            "schema_version": "pit-governance-refresh/v1",
            "status": "rejected",
            "errors": [
                {
                    "pool_id": normalized_pool or "all_governed_csi",
                    "code": "pit_update_actor_required",
                    "error": "PIT 治理采集必须绑定有效管理员身份",
                }
            ],
            "production_import_performed": False,
            "activation_performed": False,
        }
        raise DataUpdateFailedError(result)

    if progress is not None:
        await progress(
            {
                "overall_fraction": 0.01,
                "source_role": "pit_governance_collection",
                "provider": "csindex_official",
                "completed_codes": 0,
                "total_codes": 0,
                "reused_staging": True,
            }
        )
    evidence_root = settings.abs_path(settings.PIT_EVIDENCE_DIR)
    governance = PitEvidenceGovernance(
        root=evidence_root,
        database_path=settings.abs_path(settings.PIT_EVIDENCE_DB),
        master_store=PointInTimeMasterStore(),
    )
    workflow = CsindexHistoryWorkflow(
        workspace=evidence_root / "automatic" / "csindex_history",
        governance=governance,
        actor_user_id=actor_user_id,
        collector=CsindexOfficialCollector(),
    )
    requested_from = date(2015, 1, 1)
    observed_on = date.today()
    try:
        collected = await workflow.run(requested_from=requested_from)
    except Exception as exc:
        result = {
            "schema_version": "pit-governance-refresh/v1",
            "status": "failed",
            "errors": [
                {
                    "pool_id": normalized_pool or "all_governed_csi",
                    "code": "pit_evidence_collection_failed",
                    "error": sanitize_diagnostic(
                        f"{type(exc).__name__}: {exc}", max_length=500
                    ),
                }
            ],
            "production_import_performed": False,
            "activation_performed": False,
        }
        raise DataUpdateFailedError(result) from exc
    try:
        (
            package_id,
            coverage_from,
            coverage_to,
            artifacts,
        ) = _attest_automatic_collection(
            collected,
            evidence_root=evidence_root,
            requested_from=requested_from,
            observed_on=observed_on,
        )
    except (OSError, TypeError, ValueError) as exc:
        result = {
            "schema_version": "pit-governance-refresh/v1",
            "status": "failed",
            "errors": [
                {
                    "pool_id": normalized_pool or "all_governed_csi",
                    "code": "pit_collection_attestation_failed",
                    "error": sanitize_diagnostic(str(exc), max_length=500),
                }
            ],
            "production_import_performed": False,
            "activation_performed": False,
        }
        raise DataUpdateFailedError(result) from exc
    if progress is not None:
        await progress(
            {
                "overall_fraction": 1.0,
                "source_role": "pit_governance_collection",
                "provider": "csindex_official",
                "completed_codes": 0,
                "total_codes": 0,
                "reused_staging": True,
            }
        )
    return {
        "schema_version": "pit-governance-refresh/v1",
        "status": "pending_review",
        "requested_pool_hint": normalized_pool,
        "collected_scope": "all_governed_csi",
        "package_id": package_id,
        "proven_coverage_from": coverage_from,
        "proven_coverage_to": coverage_to,
        "artifacts": artifacts,
        "automatic_approval_permitted": False,
        "production_import_performed": False,
        "activation_performed": False,
        "runtime_data_changed": False,
        "errors": [],
    }


async def run_data_update(
    pool_id: str | None = None,
    *,
    progress: DataUpdateProgress | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Attempt a production market-data update, failing closed until ready.

    A production update has a materially stronger contract than collecting
    constituent evidence: it must resolve the *activated* PIT member union,
    fetch/validate raw execution data and its research-adjusted counterpart,
    and commit an exact dual-price binding.  No approved provider/backfill is
    configured for that operation yet.  Do not call governance collection from
    here: doing so used to leave a ``completed`` job with ``0/0`` stocks even
    though no market data changed.
    """

    normalized_pool = (
        str(pool_id).strip().lower() if pool_id is not None else None
    )
    if progress is not None:
        await progress(
            {
                "overall_fraction": 0.0,
                "source_role": "execution_binding",
                "provider": "not_configured",
                "completed_codes": 0,
                "total_codes": 0,
                "reused_staging": False,
            }
        )
    result = {
        "schema_version": "pit-market-data-update/v2",
        "status": "blocked",
        "requested_pool_hint": normalized_pool,
        "market_data_update": {
            "scope": "activated_pit_membership_union",
            "scope_resolved": False,
            "planned_codes": 0,
            "fetched_codes": 0,
            "validated_codes": 0,
            "raw_execution_committed": False,
            "research_adjusted_committed": False,
            "runtime_binding_committed": False,
        },
        "governance_refresh_performed": False,
        "production_import_performed": False,
        "activation_performed": False,
        "runtime_data_changed": False,
        "errors": [
            {
                "pool_id": normalized_pool or "all_governed_csi",
                "code": "pit_dual_price_update_not_authorized",
                "error": (
                    "未配置经许可且已验收的 PIT raw/复权双价格更新器；"
                    "本任务未采集行情、未复用治理暂存，也未变更运行时数据。"
                ),
                "required_contract": [
                    "activated_pit_membership_union",
                    "raw_execution_prices",
                    "research_adjusted_prices",
                    "corporate_actions_or_no_event_proof",
                    "exact_runtime_binding",
                ],
            }
        ],
    }
    raise DataUpdateFailedError(result)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _parse_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
            items = parsed if isinstance(parsed, list) else value.split(",")
        except json.JSONDecodeError:
            items = value.split(",")
    else:
        return []
    return sorted({str(item).strip() for item in items if str(item).strip()})


def _filter_pivot_codes(pivot: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pivot
    allowed = set(codes)
    if isinstance(pivot.columns, pd.MultiIndex):
        columns = [column for column in pivot.columns if str(column[0]) in allowed]
    else:
        columns = [column for column in pivot.columns if str(column) in allowed]
    return pivot.loc[:, columns]


async def _record_attempt_started(
    *,
    trading_db: str,
    attempt_id: str,
    deployment_id: int,
    expected_version: int,
) -> None:
    async with aiosqlite.connect(trading_db) as connection:
        await connection.execute(
            """
            INSERT INTO model_retrain_attempts
                (attempt_id, deployment_id, expected_model_version,
                 candidate_model_version, status, created_at)
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (
                attempt_id,
                deployment_id,
                expected_version,
                expected_version + 1,
                utc_now_iso(),
            ),
        )
        await connection.commit()


async def _record_attempt_failed(
    *,
    trading_db: str,
    attempt_id: str,
    state: _RetrainAttemptState,
    error: str,
) -> None:
    async with aiosqlite.connect(trading_db) as connection:
        await connection.execute(
            """
            UPDATE model_retrain_attempts
            SET status='failed', train_window_start=?, train_window_end=?,
                validation_window_start=?, validation_window_end=?,
                validation_metrics=?, retrain_manifest_json=?,
                retrain_manifest_hash=?, error=?, completed_at=?
            WHERE attempt_id=?
            """,
            (
                state.train_window[0] if state.train_window else None,
                state.train_window[1] if state.train_window else None,
                (
                    state.validation_window[0]
                    if state.validation_window
                    else None
                ),
                (
                    state.validation_window[1]
                    if state.validation_window
                    else None
                ),
                _json_dumps(state.validation_metrics or {}),
                (
                    _json_dumps(state.retrain_manifest)
                    if state.retrain_manifest
                    else None
                ),
                state.retrain_manifest_hash,
                error,
                utc_now_iso(),
                attempt_id,
            ),
        )
        await connection.commit()


async def _promotion_is_committed(
    *,
    trading_db: str,
    deployment_id: int,
    state: _RetrainAttemptState,
) -> bool:
    if (
        state.final_model_path is None
        or state.model_sha256 is None
        or state.model_size is None
        or state.model_version is None
    ):
        return False
    async with aiosqlite.connect(trading_db) as connection:
        cursor = await connection.execute(
            """
            SELECT 1
            FROM deployments d
            JOIN model_version_history mv
              ON mv.deployment_id=d.id
             AND mv.model_version=d.current_model_version
             AND mv.is_latest=1
            WHERE d.id=? AND d.current_model_version=?
              AND d.current_model_path=?
              AND d.current_model_sha256=?
              AND d.current_model_size=?
              AND mv.model_file_path=d.current_model_path
              AND mv.model_sha256=d.current_model_sha256
              AND mv.model_size=d.current_model_size
              AND mv.status='promoted'
            """,
            (
                deployment_id,
                state.model_version,
                str(state.final_model_path),
                state.model_sha256,
                state.model_size,
            ),
        )
        return await cursor.fetchone() is not None


def _promotion_result(
    deployment_id: int,
    state: _RetrainAttemptState,
) -> dict[str, Any]:
    return {
        "deployment_id": deployment_id,
        "model_version": state.model_version,
        "model_path": str(state.final_model_path),
        "model_sha256": state.model_sha256,
        "model_size": state.model_size,
        "train_metrics": state.train_metrics or {},
        "validation_metrics": state.validation_metrics or {},
    }


def _cleanup_candidate_files(state: _RetrainAttemptState) -> None:
    for path in (state.candidate_path, state.candidate_metadata_path):
        if path is not None:
            path.unlink(missing_ok=True)
    if state.moved_model and state.final_model_path is not None:
        state.final_model_path.unlink(missing_ok=True)
    if state.moved_metadata and state.final_metadata_path is not None:
        state.final_metadata_path.unlink(missing_ok=True)


def _candidate_windows(
    strategy: TrainableStrategy,
    pivot: pd.DataFrame,
    params: dict[str, Any],
    train_start: str,
) -> tuple[tuple[str, str], tuple[str, str], dict[str, Any]]:
    validation_months = int(params.get("validation_months", 1))
    if validation_months <= 0:
        raise ValueError(
            "automatic retraining requires validation_months >= 1"
        )
    label_horizon_days = strategy.label_horizon_days(params)
    if label_horizon_days <= 0:
        raise ValueError(
            "automatic retraining requires a positive label horizon"
        )
    effective_params = dict(params)
    # Champion promotion always includes a non-zero embargo. This upgrades
    # legacy deployments that explicitly stored zero without mutating them.
    embargo_days = max(1, int(params.get("embargo_days", 0)))
    effective_params["embargo_days"] = embargo_days
    window_mode = str(params.get("window_mode", "expanding")).lower()
    if window_mode not in {"expanding", "rolling"}:
        raise ValueError(
            "automatic retraining window_mode must be expanding or rolling"
        )
    rolling_train_months = int(params.get("rolling_train_months", 36))
    all_dates = pd.DatetimeIndex(pivot.index.unique()).sort_values()
    train_window, validation_window = _split_fit_and_validation_windows(
        all_dates,
        lower_bound=pd.Timestamp(train_start),
        upper_bound=all_dates[-1],
        validation_months=validation_months,
        label_horizon_days=label_horizon_days,
        embargo_days=embargo_days,
        window_mode=window_mode,
        rolling_train_months=rolling_train_months,
    )
    if validation_window is None:
        raise ValueError(
            "automatic retraining did not produce a validation window"
        )
    actual_months = (
        pd.Period(train_window[1], freq="M")
        - pd.Period(train_window[0], freq="M")
    ).n
    min_train_months = int(params.get("min_train_months", 12))
    if actual_months < min_train_months:
        raise ValueError(
            f"training window covers {actual_months} months; "
            f"min_train_months={min_train_months}"
        )
    return train_window, validation_window, effective_params


def _fit_candidate(
    strategy: TrainableStrategy,
    pivot: pd.DataFrame,
    params: dict[str, Any],
    context: TrainingWindowContext,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    strategy.prepare(pivot, params)
    fitted = strategy.fit_with_validation(pivot, params, context)
    if isinstance(fitted, TrainedModel):
        candidate_model = fitted.model
        metrics = {**fitted.train_metrics, **strategy.last_train_metrics}
        feature_importance = fitted.feature_importance or {}
    else:
        candidate_model = fitted
        metrics = strategy.last_train_metrics
        feature_importance = {}
    if candidate_model is None:
        raise RuntimeError("training returned no candidate model")
    validation_count = int(metrics.get("n_validation_samples") or 0)
    validation_rank_ic = metrics.get("validation_rank_ic")
    min_validation_rank_ic = float(params.get("min_validation_rank_ic", -1.0))
    if validation_count <= 0:
        raise RuntimeError(
            "validation gate rejected candidate: no evaluable samples"
        )
    if (
        validation_rank_ic is None
        or not math.isfinite(float(validation_rank_ic))
    ):
        raise RuntimeError(
            "validation gate rejected candidate: RankIC is unavailable"
        )
    if float(validation_rank_ic) < min_validation_rank_ic:
        raise RuntimeError(
            "validation gate rejected candidate: "
            f"RankIC={float(validation_rank_ic):.6f} < "
            f"min_validation_rank_ic={min_validation_rank_ic:.6f}"
        )
    return candidate_model, metrics, feature_importance


def _isolated_retrain_fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Registered worker task for one periodic model-fit attempt.

    This intentionally reconstructs the strategy in the child rather than
    pickling the parent instance.  The child owns all heavy native imports and
    training allocations; only the candidate model and finite evidence return
    through the bounded isolated-CPU protocol.
    """

    strategy_id = str(payload.get("strategy_id") or "")
    pivot = payload.get("pivot")
    params = payload.get("params")
    windows = payload.get("windows")
    if (
        not strategy_id
        or not isinstance(pivot, pd.DataFrame)
        or not isinstance(params, dict)
        or not isinstance(windows, dict)
    ):
        raise ValueError("isolated retrain payload is invalid")
    required = (
        "train_start",
        "train_end",
        "validation_start",
        "validation_end",
    )
    if any(not isinstance(windows.get(name), str) for name in required):
        raise ValueError("isolated retrain windows are invalid")
    strategy = get_registry().create_strategy(strategy_id)
    if not isinstance(strategy, TrainableStrategy):
        raise ValueError("isolated retrain strategy is not trainable")
    candidate_model, metrics, feature_importance = _fit_candidate(
        strategy,
        pivot,
        dict(params),
        TrainingWindowContext(
            train_start=windows["train_start"],
            train_end=windows["train_end"],
            validation_start=windows["validation_start"],
            validation_end=windows["validation_end"],
        ),
    )
    return {
        "candidate_model": candidate_model,
        "train_metrics": metrics,
        "feature_importance": feature_importance,
    }


async def _run_isolated_retrain_fit(
    *,
    strategy_id: str,
    pivot: pd.DataFrame,
    params: dict[str, Any],
    context: TrainingWindowContext,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Run retraining through the cancellable, bounded spawn boundary."""

    try:
        result = await run_isolated_cpu(
            "model_retrain_fit",
            {
                "strategy_id": strategy_id,
                "pivot": pivot,
                "params": dict(params),
                "windows": {
                    "train_start": context.train_start,
                    "train_end": context.train_end,
                    "validation_start": context.validation_start,
                    "validation_end": context.validation_end,
                },
            },
        )
    except IsolatedCpuTaskError as exc:
        raise RuntimeError(f"isolated retrain fit failed: {exc.message}") from exc
    except IsolatedCpuError as exc:
        raise RuntimeError(f"isolated retrain unavailable: {exc.code}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("isolated retrain returned an invalid result")
    metrics = result.get("train_metrics")
    feature_importance = result.get("feature_importance")
    if not isinstance(metrics, dict) or not isinstance(feature_importance, dict):
        raise RuntimeError("isolated retrain returned invalid evidence")
    if "candidate_model" not in result or result["candidate_model"] is None:
        raise RuntimeError("isolated retrain returned no candidate model")
    return result["candidate_model"], metrics, feature_importance


async def retrain_deployment(deployment_id: int, user_id: int) -> dict[str, Any]:
    """Train a candidate and atomically promote it after validation."""
    trading_db = str(settings.abs_path(settings.TRADING_SIM_DB))
    async with aiosqlite.connect(trading_db) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT * FROM deployments WHERE id=? AND user_id=?",
            (deployment_id, user_id),
        )
        row = await cursor.fetchone()
    if row is None:
        raise ValueError(f"deployment does not exist: {deployment_id}")
    deployment = dict(row)
    pool_id = deployment.get("pool_preset")
    if not pool_id and deployment.get("source_experiment_id"):
        async with aiosqlite.connect(
            str(settings.abs_path(settings.EXPERIMENT_DB))
        ) as connection:
            cursor = await connection.execute(
                "SELECT pool_preset, train_start FROM experiments "
                "WHERE id=? AND user_id=?",
                (deployment["source_experiment_id"], user_id),
            )
            source = await cursor.fetchone()
        if source is not None:
            pool_id = source[0]
            pit_start = str(source[1] or "2015-01-01")
        else:
            pit_start = "2015-01-01"
    else:
        pit_start = "2015-01-01"
    from backend.data.pit_runtime import require_pit_runtime_input

    await require_pit_runtime_input(
        pool_id=POOL_NAME_ALIASES.get(
            str(pool_id or "csi300"),
            str(pool_id or "csi300"),
        ),
        required_start=pit_start,
        required_end=pd.Timestamp.now().strftime("%Y-%m-%d"),
        purpose="tuning",
        require_benchmark=False,
    )
    expected_version = int(deployment.get("current_model_version") or 0)
    attempt_id = uuid.uuid4().hex
    state = _RetrainAttemptState()
    await _record_attempt_started(
        trading_db=trading_db,
        attempt_id=attempt_id,
        deployment_id=deployment_id,
        expected_version=expected_version,
    )
    try:
        return await _retrain_deployment_candidate(
            deployment=deployment,
            deployment_id=deployment_id,
            user_id=user_id,
            trading_db=trading_db,
            attempt_id=attempt_id,
            expected_version=expected_version,
            state=state,
        )
    except BaseException as exc:
        committed = await asyncio.shield(
            _promotion_is_committed(
                trading_db=trading_db,
                deployment_id=deployment_id,
                state=state,
            )
        )
        if committed:
            if isinstance(exc, asyncio.CancelledError):
                raise
            return _promotion_result(deployment_id, state)
        _cleanup_candidate_files(state)
        await asyncio.shield(
            _record_attempt_failed(
                trading_db=trading_db,
                attempt_id=attempt_id,
                state=state,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        raise


async def _retrain_deployment_candidate(
    *,
    deployment: dict[str, Any],
    deployment_id: int,
    user_id: int,
    trading_db: str,
    attempt_id: str,
    expected_version: int,
    state: _RetrainAttemptState,
) -> dict[str, Any]:
    pool_id = deployment.get("pool_preset")
    selected_codes = _parse_list(deployment.get("pool_custom_codes"))
    industries = _parse_list(deployment.get("pool_industries"))
    train_start = None
    if deployment.get("source_experiment_id"):
        async with aiosqlite.connect(
            str(settings.abs_path(settings.EXPERIMENT_DB))
        ) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """
                SELECT pool_preset, pool_custom_codes, pool_industries,
                       train_start, strategy_id, params_hash, status
                FROM experiments
                WHERE id=? AND user_id=?
                """,
                (deployment["source_experiment_id"], user_id),
            )
            source_experiment = await cursor.fetchone()
        if source_experiment:
            if (
                source_experiment["strategy_id"] != deployment["strategy_id"]
                or source_experiment["params_hash"] != deployment["params_hash"]
                or source_experiment["status"] != "completed"
            ):
                raise ValueError(
                    "source experiment strategy, parameters, or status "
                    "do not match the deployment"
                )
            pool_id = pool_id or source_experiment["pool_preset"]
            selected_codes = selected_codes or _parse_list(
                source_experiment["pool_custom_codes"]
            )
            industries = industries or _parse_list(
                source_experiment["pool_industries"]
            )
            train_start = source_experiment["train_start"]
        else:
            raise ValueError(
                "source experiment is missing or belongs to another owner"
            )

    pool_id = POOL_NAME_ALIASES.get(pool_id or "csi300", pool_id or "csi300")
    required_start = train_start or "2015-01-01"
    required_end = pd.Timestamp.now().strftime("%Y-%m-%d")
    from backend.data.pit_runtime import require_pit_runtime_input

    pit_input = await require_pit_runtime_input(
        pool_id=pool_id,
        required_start=required_start,
        required_end=required_end,
        purpose="tuning",
        requested_codes=selected_codes,
        require_benchmark=False,
    )
    pivot = pit_input.market.frame

    if pivot is None or pivot.empty:
        raise FileNotFoundError(f"pool {pool_id} has no usable market data")
    if not isinstance(pivot.index, pd.DatetimeIndex):
        pivot.index = pd.to_datetime(pivot.index)
    pivot.sort_index(inplace=True)

    if industries:
        raise ValueError(
            "periodic retraining with industry filters requires a hash-bound "
            "PIT industry timeline and is not yet enabled"
        )
    pivot = _filter_pivot_codes(pivot, selected_codes)
    if pivot.empty or len(pivot.columns) == 0:
        raise ValueError(
            "pool and industry filters leave no retraining securities"
        )

    latest = pivot.index.max()
    train_start = (
        pd.Timestamp(train_start).strftime("%Y-%m-%d")
        if train_start
        else (latest - pd.DateOffset(years=3)).strftime("%Y-%m-%d")
    )
    strategy = get_registry().create_strategy(deployment["strategy_id"])
    params = json.loads(deployment.get("params") or "{}")
    if not bool(deployment.get("requires_retraining")):
        raise ValueError("deployment is not configured for periodic retraining")
    metadata = strategy.metadata()
    if (
        not isinstance(strategy, TrainableStrategy)
        or metadata.retrain_frequency == RetrainFrequency.NEVER
    ):
        raise ValueError(
            f"strategy {deployment['strategy_id']} cannot use automatic retraining: "
            "it does not implement the periodic platform "
            "fit_with_validation contract; retrain it in a reviewed experiment "
            "and deploy the verified artifact"
        )

    train_window, validation_window, effective_params = _candidate_windows(
        strategy,
        pivot,
        params,
        train_start,
    )
    state.train_window = train_window
    state.validation_window = validation_window
    context = TrainingWindowContext(
        train_start=train_window[0],
        train_end=train_window[1],
        validation_start=validation_window[0],
        validation_end=validation_window[1],
    )
    candidate_model, train_metrics, feature_importance = await _run_isolated_retrain_fit(
        strategy_id=str(deployment["strategy_id"]),
        pivot=pivot,
        params=effective_params,
        context=context,
    )
    # The strategy instance stays in the parent only for its reviewed model
    # serialization contract; fitting itself happened in the child process.
    strategy._model = candidate_model
    validation_metrics = {
        key: value
        for key, value in train_metrics.items()
        if key.startswith("validation_") or key == "n_validation_samples"
    }
    state.validation_metrics = validation_metrics
    state.train_metrics = train_metrics

    next_version = expected_version + 1
    state.model_version = next_version
    model_root = settings.abs_path(settings.MODEL_STORE_DIR)
    model_dir = model_root / f"deployment_{deployment_id}"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_dir.chmod(0o700)
    state.candidate_path = model_dir / f".candidate-{attempt_id}.joblib"
    await asyncio.to_thread(
        strategy.save_model,
        candidate_model,
        str(state.candidate_path),
    )
    state.candidate_path.chmod(0o600)
    model_size = state.candidate_path.stat().st_size
    if model_size <= 0:
        raise RuntimeError("candidate model file is empty")
    model_sha256 = await asyncio.to_thread(file_sha256, state.candidate_path)
    state.model_sha256 = model_sha256
    state.model_size = model_size
    serialization = contract_for_model(strategy, candidate_model)
    state.retrain_manifest = {
        "schema_version": RETRAIN_MANIFEST_SCHEMA,
        "deployment": {
            "deployment_id": deployment_id,
            "owner_id": user_id,
            "strategy_id": deployment["strategy_id"],
            "params_hash": deployment["params_hash"],
            "model_version": next_version,
            "parent_model_version": expected_version,
            "parent_model_sha256": deployment.get("current_model_sha256"),
        },
        "parameters": {
            "canonical": params,
            "effective_embargo_days": effective_params["embargo_days"],
        },
        "windows": {
            "train_start": train_window[0],
            "train_end": train_window[1],
            "validation_start": validation_window[0],
            "validation_end": validation_window[1],
            "label_horizon_days": strategy.label_horizon_days(
                effective_params
            ),
        },
        "validation": validation_metrics,
        "dataset": {
            "data_version": deployment.get("data_version"),
            "pool_preset": pool_id,
            "pool_custom_codes": selected_codes,
            "pool_industries": industries,
            "observed_start": str(pivot.index.min().date()),
            "observed_end": str(pivot.index.max().date()),
            "rows": int(len(pivot)),
            "columns": int(len(pivot.columns)),
        },
        "artifact": {
            "sha256": model_sha256,
            "size": model_size,
            "serialization": serialization,
        },
    }
    state.retrain_manifest_hash = canonical_sha256(state.retrain_manifest)
    suffix = model_sha256[:12]
    unique_suffix = f"{suffix}_{attempt_id[:8]}"
    state.final_model_path = (
        model_dir / f"model_v{next_version}_{unique_suffix}.joblib"
    )
    state.final_metadata_path = (
        model_dir / f"model_v{next_version}_{unique_suffix}.json"
    )
    state.candidate_metadata_path = model_dir / f".candidate-{attempt_id}.json"
    state.candidate_metadata_path.write_text(
        _json_dumps(
            {
                "deployment_id": deployment_id,
                "model_version": next_version,
                "strategy_id": deployment["strategy_id"],
                "params_hash": deployment["params_hash"],
                "train_start": train_window[0],
                "train_end": train_window[1],
                "validation_start": validation_window[0],
                "validation_end": validation_window[1],
                "effective_embargo_days": effective_params["embargo_days"],
                "train_metrics": train_metrics,
                "validation_metrics": validation_metrics,
                "model_sha256": model_sha256,
                "model_size": model_size,
                "model_serialization": serialization,
                "retrain_manifest": state.retrain_manifest,
                "retrain_manifest_hash": state.retrain_manifest_hash,
                "pool_preset": pool_id,
                "pool_custom_codes": selected_codes,
                "pool_industries": industries,
            }
        ),
        encoding="utf-8",
    )
    state.candidate_metadata_path.chmod(0o600)

    async with aiosqlite.connect(trading_db) as connection:
        try:
            await connection.execute("PRAGMA busy_timeout=5000")
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """
                UPDATE deployments
                SET current_model_version=?, current_model_path=?,
                    current_model_sha256=?, current_model_size=?,
                    last_retrain_at=datetime('now')
                WHERE id=? AND user_id=?
                  AND COALESCE(current_model_version, 0)=?
                  AND strategy_id=? AND params_hash=?
                """,
                (
                    next_version,
                    str(state.final_model_path),
                    model_sha256,
                    model_size,
                    deployment_id,
                    user_id,
                    expected_version,
                    deployment["strategy_id"],
                    deployment["params_hash"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "concurrent model promotion conflict; champion was preserved"
                )
            if (
                state.final_model_path.exists()
                or state.final_metadata_path.exists()
            ):
                raise RuntimeError(
                    "candidate promotion target already exists; "
                    "champion was preserved"
                )
            os.replace(state.candidate_path, state.final_model_path)
            state.moved_model = True
            os.replace(
                state.candidate_metadata_path,
                state.final_metadata_path,
            )
            state.moved_metadata = True
            if os.name != "nt":
                state.final_model_path.chmod(0o400)
                state.final_metadata_path.chmod(0o400)
            await connection.execute(
                """
                UPDATE model_version_history
                SET is_latest=0
                WHERE deployment_id=?
                """,
                (deployment_id,),
            )
            await connection.execute(
                """
                INSERT INTO model_version_history
                    (deployment_id, model_version, model_file_path,
                     metadata_file_path, train_metrics, feature_importance,
                     train_window_start, train_window_end,
                     validation_window_start, validation_window_end,
                     validation_metrics, model_sha256, model_size,
                     strategy_id, params_hash, retrain_manifest_json,
                     retrain_manifest_hash, status, error, is_latest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'promoted', NULL, 1)
                """,
                (
                    deployment_id,
                    next_version,
                    str(state.final_model_path),
                    str(state.final_metadata_path),
                    _json_dumps(train_metrics),
                    _json_dumps(feature_importance),
                    train_window[0],
                    train_window[1],
                    validation_window[0],
                    validation_window[1],
                    _json_dumps(validation_metrics),
                    model_sha256,
                    model_size,
                    deployment["strategy_id"],
                    deployment["params_hash"],
                    _json_dumps(state.retrain_manifest),
                    state.retrain_manifest_hash,
                ),
            )
            cursor = await connection.execute(
                """
                UPDATE model_retrain_attempts
                SET status='promoted', train_window_start=?,
                    train_window_end=?, validation_window_start=?,
                    validation_window_end=?, validation_metrics=?,
                    model_sha256=?, model_size=?,
                    retrain_manifest_json=?, retrain_manifest_hash=?,
                    completed_at=?
                WHERE attempt_id=?
                """,
                (
                    train_window[0],
                    train_window[1],
                    validation_window[0],
                    validation_window[1],
                    _json_dumps(validation_metrics),
                    model_sha256,
                    model_size,
                    _json_dumps(state.retrain_manifest),
                    state.retrain_manifest_hash,
                    utc_now_iso(),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("retrain attempt record disappeared")
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise
    return _promotion_result(deployment_id, state)
