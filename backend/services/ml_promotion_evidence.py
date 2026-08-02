"""Immutable training and validation evidence for research promotion.

This module deliberately verifies model bytes without deserializing them.  A
completed backtest is not sufficient evidence for promoting a trainable
strategy: the exact model, RunManifest, parameters, windows, and validation
decision must all agree with an immutable artifact supplement.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import hashlib
import json
import math
import os
from typing import Any

import aiosqlite

from backend.services.model_artifacts import (
    ModelArtifactIntegrityError,
    verify_model_file,
)
from backend.services.model_serialization import (
    JOBLIB_PLATFORM_V1,
    MODEL_SERIALIZATION_SCHEMA,
)
from backend.services.research_manifest import (
    ARTIFACT_MANIFEST_SCHEMA,
    RUN_MANIFEST_SCHEMA,
    canonical_json_bytes,
    canonical_sha256,
)
from backend.strategies.base import (
    DEFAULT_VALIDATION_RANK_IC,
    MIN_VALIDATION_CROSS_SECTION_SIZE,
    MIN_VALIDATION_EFFECTIVE_DATES,
    MIN_VALIDATION_RANK_IC,
    TrainableStrategy,
)


ML_PROMOTION_EVIDENCE_SCHEMA = "trained-model-promotion-evidence/v2"
PLATFORM_TRAINABLE_CONTRACT = "platform-trainable-strategy/v1"
LEGACY_TRAINING_CONTRACT = "legacy-self-managed-training"
TRAINED_MODEL_ARTIFACT_KIND = "trained_model"
VALIDATION_METRIC_NAMES = (
    "validation_ic",
    "validation_ic_std",
    "validation_icir",
    "validation_rank_ic",
    "validation_rank_ic_std",
    "validation_rank_icir",
    "validation_loss",
    "validation_score",
)
MODEL_IMPLEMENTATIONS: dict[str, dict[str, tuple[str, str, bool]]] = {
    "alpha158_lgb_v1": {
        "LightGBM": ("lightgbm_regressor", "lightgbm", False),
    },
    "alpha158_xgb_v1": {
        "XGBoost": ("xgboost_regressor", "xgboost", False),
    },
    "alpha158_rank_lgb_v1": {
        "LightGBM LambdaRank": (
            "lightgbm_lambdarank",
            "lightgbm",
            False,
        ),
    },
    "lstm_rank_v1": {
        "LSTM (PyTorch)": ("lstm", "pytorch", False),
        "MLP (sklearn fallback)": ("mlp_classifier", "sklearn", True),
    },
    "transformer_rank_v1": {
        "Transformer (PyTorch)": ("transformer", "pytorch", False),
        "RandomForest (sklearn fallback)": (
            "random_forest_classifier",
            "sklearn",
            True,
        ),
    },
}
EXPLICIT_IMPLEMENTATION_STRATEGIES = {
    "lstm_rank_v1",
    "transformer_rank_v1",
}


class MLPromotionEvidenceError(ValueError):
    """A structured fail-closed model promotion validation error."""

    def __init__(
        self,
        code: str,
        field: str,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message
        self.expected = expected
        self.actual = actual


def _error(
    code: str,
    field: str,
    message: str,
    *,
    expected: Any = None,
    actual: Any = None,
) -> MLPromotionEvidenceError:
    return MLPromotionEvidenceError(
        code,
        field,
        message,
        expected=expected,
        actual=actual,
    )


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _resolve_model_implementation(
    strategy_id: str,
    train_metrics: Mapping[str, Any],
) -> tuple[str, str, bool]:
    model_type = train_metrics.get("model_type")
    if not isinstance(model_type, str) or not model_type:
        return "unverified", "unverified", True
    resolved = MODEL_IMPLEMENTATIONS.get(strategy_id, {}).get(
        model_type,
        ("unverified", "unverified", True),
    )
    if strategy_id in EXPLICIT_IMPLEMENTATION_STRATEGIES:
        implementation, backend, fallback_used = resolved
        if (
            train_metrics.get("model_implementation") != implementation
            or train_metrics.get("model_backend") != backend
            or train_metrics.get("fallback_used") is not fallback_used
        ):
            return "unverified", "unverified", True
    return resolved


def finite_json_value(value: Any) -> Any:
    """Return a JSON-safe value without inventing replacements for NaN/Inf."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(key): finite_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [finite_json_value(item) for item in value]
    return str(value)


def legacy_params_hash(params: Mapping[str, Any]) -> str:
    """Match the experiment table's historical MD5 identity field."""
    raw = json.dumps(
        dict(params),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _metadata_file_sha256(path: str) -> tuple[str, int]:
    with open(path, "rb") as metadata_file:
        digest = hashlib.file_digest(metadata_file, "sha256").hexdigest()
    return digest, int(os.path.getsize(path))


def build_model_promotion_evidence(
    *,
    experiment: Mapping[str, Any],
    strategy: Any,
    strategy_metadata: Any,
    params: Mapping[str, Any],
    walkforward_result: Any,
    model_version: int,
    model_sha256: str,
    model_size: int,
    metadata_file_path: str,
    run_manifest_hash: str,
    training_telemetry: Mapping[str, Any],
    model_serialization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the immutable supplement written beside one trained model."""
    canonical_params = dict(params)
    safe_telemetry = finite_json_value(dict(training_telemetry))
    telemetry_hash = canonical_sha256(safe_telemetry)
    metadata_sha256, metadata_size = _metadata_file_sha256(metadata_file_path)
    serialization = dict(
        model_serialization
        or {
            "schema_version": MODEL_SERIALIZATION_SCHEMA,
            "format": JOBLIB_PLATFORM_V1,
            "loader": "joblib",
            "platform_produced": True,
            "executable_deserialization": True,
        }
    )
    platform_contract = isinstance(strategy, TrainableStrategy)
    retrained_cycles = (
        [cycle for cycle in walkforward_result.cycles if cycle.retrained]
        if walkforward_result is not None
        else []
    )
    failed_cycles = (
        [cycle for cycle in walkforward_result.cycles if cycle.error]
        if walkforward_result is not None
        else []
    )
    latest_cycle = retrained_cycles[-1] if retrained_cycles else None
    latest_cycle_payload = (
        finite_json_value(vars(latest_cycle))
        if latest_cycle is not None
        else None
    )
    latest_train_metrics = (
        dict(latest_cycle.train_metrics)
        if latest_cycle is not None
        else {}
    )
    model_type = latest_train_metrics.get("model_type")
    model_implementation, model_backend, implementation_fallback = (
        _resolve_model_implementation(
            str(experiment["strategy_id"]),
            latest_train_metrics,
        )
    )
    latest_metrics = (
        dict(latest_cycle.validation_metrics)
        if latest_cycle is not None
        else {}
    )
    finite_metrics = {
        name: number
        for name in VALIDATION_METRIC_NAMES
        if (number := _finite_number(latest_metrics.get(name))) is not None
    }
    validation_samples = (
        int(latest_cycle.n_validation_samples or 0)
        if latest_cycle is not None
        else 0
    )
    validation_dates = int(latest_metrics.get("n_validation_dates") or 0)
    minimum_cross_section = int(
        latest_metrics.get("min_validation_cross_section_size") or 0
    )
    train_samples = (
        int(latest_cycle.n_train_samples or 0)
        if latest_cycle is not None
        else 0
    )
    threshold = _finite_number(
        params.get(
            "min_validation_rank_ic",
            DEFAULT_VALIDATION_RANK_IC,
        )
    )
    rank_ic = finite_metrics.get("validation_rank_ic")
    gate_passed = (
        threshold is not None
        and threshold >= MIN_VALIDATION_RANK_IC
        and rank_ic is not None
        and float(rank_ic) >= float(threshold)
    )
    validation_passed = (
        platform_contract
        and latest_cycle is not None
        and not failed_cycles
        and validation_samples > 0
        and validation_dates >= MIN_VALIDATION_EFFECTIVE_DATES
        and minimum_cross_section >= MIN_VALIDATION_CROSS_SECTION_SIZE
        and validation_samples
        >= validation_dates * minimum_cross_section
        and bool(finite_metrics)
        and gate_passed
        and latest_cycle.validation_start is not None
        and latest_cycle.validation_end is not None
        and latest_cycle.pred_date is not None
        and not implementation_fallback
    )
    overall_status = (
        "passed"
        if validation_passed
        else ("rejected" if platform_contract else "unverified_legacy")
    )
    training_mode = (
        "train_once"
        if str(strategy_metadata.retrain_frequency.value) == "never"
        else "periodic"
    )
    evidence = {
        "schema_version": ML_PROMOTION_EVIDENCE_SCHEMA,
        # Compatibility identity for the existing deployment integrity reader.
        "strategy_id": str(experiment["strategy_id"]),
        "model_version": int(model_version),
        "identity": {
            "experiment_id": int(experiment["id"]),
            "owner_user_id": int(experiment["user_id"]),
            "strategy_id": str(experiment["strategy_id"]),
            "model_version": int(model_version),
        },
        "parameters": {
            "canonical": canonical_params,
            "sha256": canonical_sha256(canonical_params),
            "experiment_params_hash": str(experiment["params_hash"]),
        },
        "model": {
            "artifact_kind": TRAINED_MODEL_ARTIFACT_KIND,
            "sha256": model_sha256,
            "size": int(model_size),
            "metadata_sha256": metadata_sha256,
            "metadata_size": metadata_size,
            "run_manifest_hash": run_manifest_hash,
            "serialization": serialization,
        },
        "training": {
            "contract": (
                PLATFORM_TRAINABLE_CONTRACT
                if platform_contract
                else LEGACY_TRAINING_CONTRACT
            ),
            "mode": training_mode,
            "retrain_frequency": str(strategy_metadata.retrain_frequency.value),
            "status": overall_status,
            "train_samples": train_samples,
            "model_type": model_type,
            "model_implementation": model_implementation,
            "backend": model_backend,
            "implementation_status": (
                "fallback"
                if implementation_fallback
                and model_implementation != "unverified"
                else (
                    "unverified"
                    if model_implementation == "unverified"
                    else "native"
                )
            ),
            "telemetry_sha256": telemetry_hash,
            "latest_cycle_sha256": (
                canonical_sha256(latest_cycle_payload)
                if latest_cycle_payload is not None
                else None
            ),
            "attempt_count": (
                len(walkforward_result.cycles)
                if walkforward_result is not None
                else 0
            ),
            "retrain_count": len(retrained_cycles),
            "failed_attempt_count": len(failed_cycles),
            "fallback_used": bool(failed_cycles or implementation_fallback),
        },
        "windows": {
            "train_start": (
                latest_cycle.train_start if latest_cycle is not None else None
            ),
            "train_end": (
                latest_cycle.train_end if latest_cycle is not None else None
            ),
            "validation_start": (
                latest_cycle.validation_start if latest_cycle is not None else None
            ),
            "validation_end": (
                latest_cycle.validation_end if latest_cycle is not None else None
            ),
            "prediction_start": (
                latest_cycle.pred_date if latest_cycle is not None else None
            ),
            "prediction_month": (
                latest_cycle.pred_month if latest_cycle is not None else None
            ),
            "test_start": str(experiment["test_start"]),
            "test_end": str(experiment["test_end"]),
            "label_horizon_days": (
                int(latest_cycle.label_horizon_days)
                if latest_cycle is not None
                else None
            ),
            "embargo_days": (
                int(latest_cycle.embargo_days)
                if latest_cycle is not None
                else None
            ),
        },
        "validation": {
            "status": "passed" if validation_passed else "rejected",
            "samples": validation_samples,
            "effective_dates": validation_dates,
            "minimum_cross_section_size": minimum_cross_section,
            "metrics": finite_metrics,
            "ic": finite_metrics.get("validation_ic"),
            "rank_ic": rank_ic,
            "loss": finite_metrics.get("validation_loss"),
            "gate": {
                "metric": "validation_rank_ic",
                "operator": "gte",
                "threshold": threshold,
                "actual": rank_ic,
                "passed": gate_passed,
            },
            "evidence_gate": {
                "minimum_effective_dates": MIN_VALIDATION_EFFECTIVE_DATES,
                "actual_effective_dates": validation_dates,
                "minimum_cross_section_size": (
                    MIN_VALIDATION_CROSS_SECTION_SIZE
                ),
                "actual_minimum_cross_section_size": minimum_cross_section,
                "minimum_samples": (
                    validation_dates * minimum_cross_section
                ),
                "actual_samples": validation_samples,
                "passed": (
                    validation_dates >= MIN_VALIDATION_EFFECTIVE_DATES
                    and minimum_cross_section
                    >= MIN_VALIDATION_CROSS_SECTION_SIZE
                    and validation_samples
                    >= validation_dates * minimum_cross_section
                ),
            },
        },
    }
    # The canonical encoder rejects any accidentally retained NaN/Inf.
    canonical_json_bytes(evidence)
    return evidence


def _json_object(raw: Any, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise _error(
            "ml_evidence_invalid",
            field,
            f"{field} is not valid JSON",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "ml_evidence_invalid",
            field,
            f"{field} must be a JSON object",
        )
    return value


def _require_equal(
    *,
    field: str,
    actual: Any,
    expected: Any,
    code: str = "ml_evidence_identity_mismatch",
) -> None:
    if actual != expected:
        raise _error(
            code,
            field,
            "Immutable training evidence does not match the live experiment",
            expected=expected,
            actual=actual,
        )


def _validate_manifest(
    raw_manifest: Any,
    stored_hash: Any,
    *,
    experiment: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _json_object(raw_manifest, field="run_manifest.manifest_json")
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA:
        raise _error(
            "ml_run_manifest_invalid",
            "run_manifest.schema_version",
            "A supported immutable RunManifest is required",
        )
    actual_hash = canonical_sha256(manifest)
    _require_equal(
        field="run_manifest.manifest_hash",
        actual=actual_hash,
        expected=str(stored_hash),
        code="ml_run_manifest_invalid",
    )
    identity = manifest.get("experiment") or {}
    parameters = manifest.get("parameters") or {}
    windows = manifest.get("windows") or {}
    _require_equal(
        field="run_manifest.experiment.experiment_id",
        actual=identity.get("experiment_id"),
        expected=int(experiment["id"]),
    )
    _require_equal(
        field="run_manifest.experiment.strategy_id",
        actual=identity.get("strategy_id"),
        expected=str(experiment["strategy_id"]),
    )
    _require_equal(
        field="run_manifest.parameters.canonical",
        actual=parameters.get("canonical"),
        expected=dict(params),
    )
    _require_equal(
        field="run_manifest.parameters.sha256",
        actual=parameters.get("sha256"),
        expected=canonical_sha256(params),
    )
    for name in ("test_start", "test_end"):
        _require_equal(
            field=f"run_manifest.windows.{name}",
            actual=windows.get(name),
            expected=experiment[name],
        )
    return manifest


def _validate_supplement(
    evidence: Mapping[str, Any],
    *,
    experiment: Mapping[str, Any],
    params: Mapping[str, Any],
    artifact: Mapping[str, Any],
    run_manifest_hash: str,
) -> None:
    if evidence.get("schema_version") != ML_PROMOTION_EVIDENCE_SCHEMA:
        raise _error(
            "ml_evidence_legacy_or_missing",
            "artifact.metadata.schema_version",
            "Immutable training/validation evidence v1 is required",
        )
    identity = evidence.get("identity") or {}
    parameters = evidence.get("parameters") or {}
    model = evidence.get("model") or {}
    training = evidence.get("training") or {}
    windows = evidence.get("windows") or {}
    validation = evidence.get("validation") or {}
    expected_identity = {
        "experiment_id": int(experiment["id"]),
        "owner_user_id": int(experiment["user_id"]),
        "strategy_id": str(experiment["strategy_id"]),
        "model_version": int(artifact["model_version"]),
    }
    for name, expected in expected_identity.items():
        _require_equal(
            field=f"artifact.metadata.identity.{name}",
            actual=identity.get(name),
            expected=expected,
        )
    _require_equal(
        field="artifact.metadata.parameters.canonical",
        actual=parameters.get("canonical"),
        expected=dict(params),
    )
    _require_equal(
        field="artifact.metadata.parameters.sha256",
        actual=parameters.get("sha256"),
        expected=canonical_sha256(params),
    )
    _require_equal(
        field="artifact.metadata.parameters.experiment_params_hash",
        actual=parameters.get("experiment_params_hash"),
        expected=str(experiment["params_hash"]),
    )
    expected_model = {
        "artifact_kind": TRAINED_MODEL_ARTIFACT_KIND,
        "sha256": str(artifact["artifact_sha256"]),
        "size": int(artifact["artifact_size"]),
        "run_manifest_hash": run_manifest_hash,
    }
    for name, expected in expected_model.items():
        _require_equal(
            field=f"artifact.metadata.model.{name}",
            actual=model.get(name),
            expected=expected,
        )
    try:
        artifact_train_samples = int(artifact["train_samples"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(
            "ml_artifact_unverified_legacy",
            "model_artifact.train_samples",
            "Verified positive training sample evidence is required",
        ) from exc
    _require_equal(
        field="artifact.metadata.training.train_samples",
        actual=training.get("train_samples"),
        expected=artifact_train_samples,
    )
    frequency = str(experiment["retrain_frequency"])
    expected_mode = "train_once" if frequency == "never" else "periodic"
    _require_equal(
        field="artifact.metadata.training.mode",
        actual=training.get("mode"),
        expected=expected_mode,
    )
    _require_equal(
        field="artifact.metadata.training.retrain_frequency",
        actual=training.get("retrain_frequency"),
        expected=frequency,
    )
    if training.get("contract") != PLATFORM_TRAINABLE_CONTRACT:
        raise _error(
            "ml_contract_noncompliant",
            "artifact.metadata.training.contract",
            "Strategy does not use the platform TrainableStrategy validation contract",
            expected=PLATFORM_TRAINABLE_CONTRACT,
            actual=training.get("contract"),
        )
    if (
        training.get("implementation_status") == "fallback"
        or training.get("fallback_used") is True
    ):
        raise _error(
            "ml_model_fallback_disallowed",
            "artifact.metadata.training.fallback_used",
            "Fallback model implementations or fallback training cycles cannot be promoted",
            expected=False,
            actual=training.get("fallback_used"),
        )
    if (
        training.get("implementation_status") != "native"
        or training.get("model_implementation") in {None, "", "unverified"}
        or training.get("backend") in {None, "", "unverified"}
    ):
        raise _error(
            "ml_model_implementation_unverified",
            "artifact.metadata.training.model_implementation",
            "A recognized native model implementation and backend are required",
            actual={
                "model_type": training.get("model_type"),
                "model_implementation": training.get("model_implementation"),
                "backend": training.get("backend"),
            },
        )
    if training.get("status") != "passed":
        raise _error(
            "ml_training_not_accepted",
            "artifact.metadata.training.status",
            "Failed, rejected, fallback, or legacy training cannot be promoted",
            expected="passed",
            actual=training.get("status"),
        )
    if (
        int(training.get("attempt_count") or 0) <= 0
        or int(training.get("retrain_count") or 0) <= 0
        or int(training.get("train_samples") or 0) <= 0
        or int(training.get("failed_attempt_count") or 0) != 0
        or training.get("fallback_used") is not False
    ):
        raise _error(
            "ml_training_attempt_failed",
            "artifact.metadata.training",
            "All training attempts must be successful without fallback",
        )
    for name in (
        "train_start",
        "train_end",
        "validation_start",
        "validation_end",
        "prediction_start",
    ):
        if not isinstance(windows.get(name), str) or not windows[name]:
            raise _error(
                "ml_validation_window_missing",
                f"artifact.metadata.windows.{name}",
                "Complete train and validation windows are required",
            )
    for name in ("train_start", "train_end"):
        _require_equal(
            field=f"artifact.metadata.windows.{name}",
            actual=windows.get(name),
            expected=artifact[name.replace("train_", "train_window_")],
        )
    for name in ("test_start", "test_end"):
        _require_equal(
            field=f"artifact.metadata.windows.{name}",
            actual=windows.get(name),
            expected=experiment[name],
        )
    try:
        parsed_windows = {
            name: date.fromisoformat(str(windows[name]))
            for name in (
                "train_start",
                "train_end",
                "validation_start",
                "validation_end",
                "prediction_start",
                "test_start",
                "test_end",
            )
        }
        valid_temporal_order = (
            parsed_windows["train_start"] <= parsed_windows["train_end"]
            < parsed_windows["validation_start"]
            <= parsed_windows["validation_end"]
            < parsed_windows["prediction_start"]
            and parsed_windows["test_start"]
            <= parsed_windows["prediction_start"]
            <= parsed_windows["test_end"]
        )
        horizon = int(windows["label_horizon_days"])
        embargo = int(windows["embargo_days"])
    except (KeyError, TypeError, ValueError):
        valid_temporal_order = False
        horizon = -1
        embargo = -1
    if not valid_temporal_order or horizon < 0 or embargo < 0:
        raise _error(
            "ml_validation_window_invalid",
            "artifact.metadata.windows",
            "Training, purged validation, and test windows are invalid",
        )
    if validation.get("status") != "passed":
        raise _error(
            "ml_validation_gate_failed",
            "artifact.metadata.validation.status",
            "Validation evidence must have passed",
            expected="passed",
            actual=validation.get("status"),
        )
    try:
        validation_samples = int(validation.get("samples"))
    except (TypeError, ValueError):
        validation_samples = 0
    metrics = validation.get("metrics")
    finite_metrics = (
        {
            name: value
            for name, value in metrics.items()
            if name in VALIDATION_METRIC_NAMES
            and _finite_number(value) is not None
        }
        if isinstance(metrics, Mapping)
        else {}
    )
    gate = validation.get("gate") or {}
    evidence_gate = validation.get("evidence_gate") or {}
    threshold = _finite_number(gate.get("threshold"))
    actual = _finite_number(gate.get("actual"))
    expected_threshold = _finite_number(
        params.get(
            "min_validation_rank_ic",
            DEFAULT_VALIDATION_RANK_IC,
        )
    )
    gate_valid = (
        gate.get("metric") == "validation_rank_ic"
        and gate.get("operator") == "gte"
        and gate.get("passed") is True
        and threshold is not None
        and threshold >= MIN_VALIDATION_RANK_IC
        and expected_threshold is not None
        and float(threshold) == float(expected_threshold)
        and actual is not None
        and float(actual) >= float(threshold)
        and finite_metrics.get("validation_rank_ic") == actual
        and validation.get("rank_ic") == actual
    )
    try:
        effective_dates = int(validation.get("effective_dates"))
        minimum_cross_section = int(
            validation.get("minimum_cross_section_size")
        )
        evidence_gate_valid = (
            evidence_gate.get("passed") is True
            and int(evidence_gate.get("minimum_effective_dates"))
            == MIN_VALIDATION_EFFECTIVE_DATES
            and int(evidence_gate.get("actual_effective_dates"))
            == effective_dates
            and int(evidence_gate.get("minimum_cross_section_size"))
            == MIN_VALIDATION_CROSS_SECTION_SIZE
            and int(
                evidence_gate.get("actual_minimum_cross_section_size")
            )
            == minimum_cross_section
            and int(evidence_gate.get("minimum_samples"))
            == effective_dates * minimum_cross_section
            and int(evidence_gate.get("actual_samples"))
            == validation_samples
            and effective_dates >= MIN_VALIDATION_EFFECTIVE_DATES
            and minimum_cross_section
            >= MIN_VALIDATION_CROSS_SECTION_SIZE
            and validation_samples
            >= effective_dates * minimum_cross_section
        )
    except (TypeError, ValueError):
        evidence_gate_valid = False
    for name in ("label_horizon_days", "embargo_days"):
        if name in params:
            try:
                parameter_window_matches = int(windows[name]) == int(params[name])
            except (KeyError, TypeError, ValueError):
                parameter_window_matches = False
            if not parameter_window_matches:
                raise _error(
                    "ml_validation_window_invalid",
                    f"artifact.metadata.windows.{name}",
                    "Validation purge settings do not match canonical parameters",
                )
    if (
        validation_samples <= 0
        or not finite_metrics
        or not gate_valid
        or not evidence_gate_valid
    ):
        raise _error(
            "ml_validation_gate_failed",
            "artifact.metadata.validation",
            "Finite preregistered validation metrics and a passing gate are required",
        )


def _validate_training_telemetry_binding(
    evidence: Mapping[str, Any],
    telemetry: Mapping[str, Any],
) -> None:
    training = evidence.get("training") or {}
    windows = evidence.get("windows") or {}
    validation = evidence.get("validation") or {}
    cycles = telemetry.get("cycles")
    if not isinstance(cycles, list) or not cycles:
        raise _error(
            "ml_training_telemetry_tampered",
            "model_artifact.train_metrics.cycles",
            "Immutable per-cycle training telemetry is required",
        )
    mapped_cycles = [cycle for cycle in cycles if isinstance(cycle, Mapping)]
    if len(mapped_cycles) != len(cycles):
        raise _error(
            "ml_training_telemetry_tampered",
            "model_artifact.train_metrics.cycles",
            "Training cycle telemetry is malformed",
        )
    retrained_cycles = [
        cycle for cycle in mapped_cycles if cycle.get("retrained") is True
    ]
    failed_cycles = [cycle for cycle in mapped_cycles if cycle.get("error")]
    if not retrained_cycles:
        raise _error(
            "ml_training_telemetry_tampered",
            "model_artifact.train_metrics.cycles",
            "No accepted retrained cycle is bound to the model",
        )
    latest_cycle = retrained_cycles[-1]
    latest_train_metrics = latest_cycle.get("train_metrics")
    if not isinstance(latest_train_metrics, Mapping):
        raise _error(
            "ml_training_telemetry_tampered",
            "model_artifact.train_metrics.cycles.train_metrics",
            "Latest cycle model implementation telemetry is missing",
        )
    model_type = latest_train_metrics.get("model_type")
    strategy_id = str((evidence.get("identity") or {}).get("strategy_id") or "")
    model_implementation, backend, implementation_fallback = (
        _resolve_model_implementation(strategy_id, latest_train_metrics)
    )
    implementation_status = (
        "fallback"
        if implementation_fallback and model_implementation != "unverified"
        else (
            "unverified"
            if model_implementation == "unverified"
            else "native"
        )
    )
    expected_counts = {
        "attempt_count": len(mapped_cycles),
        "retrain_count": len(retrained_cycles),
        "failed_attempt_count": len(failed_cycles),
        "fallback_used": bool(failed_cycles or implementation_fallback),
        "latest_cycle_sha256": canonical_sha256(latest_cycle),
        "model_type": model_type,
        "model_implementation": model_implementation,
        "backend": backend,
        "implementation_status": implementation_status,
    }
    for name, expected in expected_counts.items():
        _require_equal(
            field=f"artifact.metadata.training.{name}",
            actual=training.get(name),
            expected=expected,
            code="ml_training_telemetry_tampered",
        )
    cycle_window_fields = {
        "train_start": "train_start",
        "train_end": "train_end",
        "validation_start": "validation_start",
        "validation_end": "validation_end",
        "prediction_start": "pred_date",
        "prediction_month": "pred_month",
        "label_horizon_days": "label_horizon_days",
        "embargo_days": "embargo_days",
    }
    for evidence_name, cycle_name in cycle_window_fields.items():
        _require_equal(
            field=f"artifact.metadata.windows.{evidence_name}",
            actual=windows.get(evidence_name),
            expected=latest_cycle.get(cycle_name),
            code="ml_training_telemetry_tampered",
        )
    _require_equal(
        field="artifact.metadata.validation.samples",
        actual=validation.get("samples"),
        expected=latest_cycle.get("n_validation_samples"),
        code="ml_training_telemetry_tampered",
    )
    cycle_validation = latest_cycle.get("validation_metrics")
    if not isinstance(cycle_validation, Mapping):
        raise _error(
            "ml_training_telemetry_tampered",
            "model_artifact.train_metrics.cycles.validation_metrics",
            "Latest cycle validation telemetry is missing",
        )
    for evidence_name, cycle_name in (
        ("effective_dates", "n_validation_dates"),
        (
            "minimum_cross_section_size",
            "min_validation_cross_section_size",
        ),
    ):
        _require_equal(
            field=f"artifact.metadata.validation.{evidence_name}",
            actual=validation.get(evidence_name),
            expected=cycle_validation.get(cycle_name),
            code="ml_training_telemetry_tampered",
        )
    for name, actual in (validation.get("metrics") or {}).items():
        _require_equal(
            field=f"artifact.metadata.validation.metrics.{name}",
            actual=actual,
            expected=cycle_validation.get(name),
            code="ml_training_telemetry_tampered",
        )


async def verify_experiment_model_promotion_evidence(
    connection: aiosqlite.Connection,
    experiment: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one unique latest model and immutable validation supplement."""
    experiment = dict(experiment)
    experiment_id = int(experiment["id"])
    cursor = await connection.execute(
        """
        SELECT *
        FROM model_artifacts
        WHERE experiment_id=? AND is_latest=1
        ORDER BY id
        """,
        (experiment_id,),
    )
    artifact_rows = await cursor.fetchall()
    if not artifact_rows:
        raise _error(
            "ml_artifact_missing",
            "experiment.model_artifacts",
            "Trainable promotion requires a verified model artifact",
        )
    if len(artifact_rows) != 1:
        raise _error(
            "ml_artifact_latest_not_unique",
            "experiment.model_artifacts.is_latest",
            "Exactly one latest model artifact is required",
            expected=1,
            actual=len(artifact_rows),
        )
    artifact = dict(artifact_rows[0])
    params = _json_object(experiment["params"], field="experiment.params")
    _require_equal(
        field="experiment.params_hash",
        actual=str(experiment["params_hash"]),
        expected=legacy_params_hash(params),
    )
    cursor = await connection.execute(
        """
        SELECT *
        FROM research_run_manifests
        WHERE experiment_id=?
        """,
        (experiment_id,),
    )
    manifest_rows = await cursor.fetchall()
    if len(manifest_rows) != 1:
        raise _error(
            "ml_run_manifest_invalid",
            "experiment.run_manifest",
            "Exactly one immutable RunManifest is required",
            expected=1,
            actual=len(manifest_rows),
        )
    manifest_row = dict(manifest_rows[0])
    _require_equal(
        field="run_manifest.user_id",
        actual=int(manifest_row["user_id"]),
        expected=int(experiment["user_id"]),
    )
    if manifest_row["schema_version"] != RUN_MANIFEST_SCHEMA:
        raise _error(
            "ml_run_manifest_invalid",
            "run_manifest.schema_version",
            "A supported immutable RunManifest is required",
        )
    _validate_manifest(
        manifest_row["manifest_json"],
        manifest_row["manifest_hash"],
        experiment=experiment,
        params=params,
    )
    run_manifest_hash = str(manifest_row["manifest_hash"])
    expected_artifact_identity = {
        "experiment_id": experiment_id,
        "strategy_id": str(experiment["strategy_id"]),
        "params_hash": str(experiment["params_hash"]),
        "run_manifest_hash": run_manifest_hash,
    }
    for name, expected in expected_artifact_identity.items():
        _require_equal(
            field=f"model_artifact.{name}",
            actual=artifact.get(name),
            expected=expected,
        )
    if (
        not isinstance(artifact.get("artifact_sha256"), str)
        or not isinstance(artifact.get("artifact_size"), int)
    ):
        raise _error(
            "ml_artifact_unverified_legacy",
            "model_artifact.integrity",
            "Legacy model artifacts without hash and size cannot be promoted",
        )
    cursor = await connection.execute(
        """
        SELECT *
        FROM research_artifact_manifests
        WHERE experiment_id=? AND run_manifest_hash=?
          AND artifact_kind=? AND artifact_sha256=? AND artifact_size=?
        ORDER BY id
        """,
        (
            experiment_id,
            run_manifest_hash,
            TRAINED_MODEL_ARTIFACT_KIND,
            artifact["artifact_sha256"],
            artifact["artifact_size"],
        ),
    )
    supplement_rows = await cursor.fetchall()
    if len(supplement_rows) != 1:
        raise _error(
            "ml_evidence_legacy_or_missing",
            "model_artifact.supplement",
            "Exactly one immutable training/validation supplement is required",
            expected=1,
            actual=len(supplement_rows),
        )
    supplement_row = dict(supplement_rows[0])
    if supplement_row["schema_version"] != ARTIFACT_MANIFEST_SCHEMA:
        raise _error(
            "ml_evidence_legacy_or_missing",
            "model_artifact.supplement.schema_version",
            "Artifact manifest schema is unsupported",
        )
    evidence = _json_object(
        supplement_row["metadata_json"],
        field="model_artifact.supplement.metadata_json",
    )
    _validate_supplement(
        evidence,
        experiment=experiment,
        params=params,
        artifact=artifact,
        run_manifest_hash=run_manifest_hash,
    )
    telemetry = _json_object(
        artifact.get("train_metrics"),
        field="model_artifact.train_metrics",
    )
    try:
        telemetry_hash = canonical_sha256(telemetry)
    except (TypeError, ValueError) as exc:
        raise _error(
            "ml_training_telemetry_tampered",
            "model_artifact.train_metrics",
            "Training telemetry is not finite canonical JSON",
        ) from exc
    _require_equal(
        field="model_artifact.train_metrics",
        actual=telemetry_hash,
        expected=(evidence.get("training") or {}).get("telemetry_sha256"),
        code="ml_training_telemetry_tampered",
    )
    _validate_training_telemetry_binding(evidence, telemetry)
    try:
        await verify_model_file(
            artifact["model_file_path"],
            artifact["artifact_sha256"],
            artifact["artifact_size"],
        )
        metadata_path, metadata_sha256, metadata_size = await verify_model_file(
            artifact["metadata_file_path"],
            (evidence.get("model") or {}).get("metadata_sha256"),
            (evidence.get("model") or {}).get("metadata_size"),
        )
    except ModelArtifactIntegrityError as exc:
        raise _error(
            "ml_artifact_integrity_failed",
            "model_artifact.file",
            "Model or metadata artifact integrity verification failed",
            actual=str(exc),
        ) from exc
    try:
        metadata_payload = _json_object(
            metadata_path.read_text(encoding="utf-8"),
            field="model_artifact.metadata_file",
        )
        _require_equal(
            field="model_artifact.metadata_file.experiment_id",
            actual=metadata_payload.get("experiment_id"),
            expected=experiment_id,
        )
        _require_equal(
            field="model_artifact.metadata_file.strategy_id",
            actual=metadata_payload.get("strategy_id"),
            expected=str(experiment["strategy_id"]),
        )
        _require_equal(
            field="model_artifact.metadata_file.params",
            actual=metadata_payload.get("params"),
            expected=params,
        )
        metadata_training = metadata_payload.get("training")
        if not isinstance(metadata_training, Mapping):
            raise _error(
                "ml_training_telemetry_tampered",
                "model_artifact.metadata_file.training",
                "Model metadata training telemetry is missing",
            )
        _require_equal(
            field="model_artifact.metadata_file.training",
            actual=canonical_sha256(metadata_training),
            expected=(evidence.get("training") or {}).get("telemetry_sha256"),
            code="ml_training_telemetry_tampered",
        )
    except OSError as exc:
        raise _error(
            "ml_artifact_integrity_failed",
            "model_artifact.metadata_file",
            "Verified model metadata could not be read",
            actual=type(exc).__name__,
        ) from exc
    return {
        "schema_version": ML_PROMOTION_EVIDENCE_SCHEMA,
        "experiment_id": experiment_id,
        "strategy_id": str(experiment["strategy_id"]),
        "model_version": int(artifact["model_version"]),
        "model_sha256": str(artifact["artifact_sha256"]),
        "model_size": int(artifact["artifact_size"]),
        "metadata_sha256": metadata_sha256,
        "metadata_size": metadata_size,
        "run_manifest_hash": run_manifest_hash,
        "validation": evidence["validation"],
    }
