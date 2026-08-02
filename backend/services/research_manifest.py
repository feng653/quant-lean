"""Immutable, canonical manifests for reproducible research runs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import re
from typing import Any, Mapping

import aiosqlite

from backend.config import settings
from backend.core.cost_model import CostModel
from backend.core.engine import ExecutionConstraints
from backend.data.lineage import UniverseSnapshot
from backend.data.market_quality import MarketDataQualitySnapshot
from backend.data.versioning import DatasetVersion
from backend.version import (
    observed_worktree_drift,
    runtime_code_identity,
    runtime_code_version,
)


RUN_MANIFEST_SCHEMA = "research-run-manifest/v1"
ARTIFACT_MANIFEST_SCHEMA = "research-artifact-manifest/v1"
SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "authorization",
)
KEY_PACKAGES = (
    "numpy",
    "pandas",
    "pyarrow",
    "scikit-learn",
    "lightgbm",
    "xgboost",
    "torch",
    "statsmodels",
)
THREAD_ENV_KEYS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)
_ABSOLUTE_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class ManifestError(RuntimeError):
    """Base error for reproducibility enforcement."""


def _enum_text(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


class ManifestConflictError(ManifestError):
    """An experiment already owns a different immutable manifest."""

    def __init__(self, differences: list[dict[str, Any]]) -> None:
        super().__init__("existing run manifest differs from current run")
        self.differences = differences


class ManifestDriftError(ManifestError):
    """Expected replay inputs differ before backtest execution."""

    def __init__(self, differences: list[dict[str, Any]]) -> None:
        super().__init__("exact replay input validation failed")
        self.differences = differences


class ManifestSecurityError(ManifestError):
    """A manifest candidate contains secret-like or local path data."""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically; reject NaN and non-JSON objects."""
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError("manifest must be canonical finite JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strategy_source_sha256(strategy: Any) -> str:
    try:
        source_path = Path(inspect.getfile(type(strategy))).resolve()
        return _file_sha256(source_path)
    except (OSError, TypeError):
        source = inspect.getsource(type(strategy)).encode("utf-8")
        return hashlib.sha256(source).hexdigest()


def capture_git_state() -> dict[str, Any]:
    """Return the code identity captured when the backend process started."""
    return runtime_code_identity()


def code_version(git_state: Mapping[str, Any]) -> str:
    return runtime_code_version(git_state)


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in KEY_PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _device_inventory() -> dict[str, Any]:
    cpu = {
        "architecture": platform.machine() or "unknown",
        "logical_cores": os.cpu_count(),
        "processor": platform.processor() or "unknown",
    }
    gpu: dict[str, Any] = {
        "backend": "none",
        "available": False,
        "devices": [],
    }
    try:
        import torch

        if torch.cuda.is_available():
            gpu = {
                "backend": "cuda",
                "available": True,
                "devices": [
                    str(torch.cuda.get_device_name(index))
                    for index in range(torch.cuda.device_count())
                ],
            }
        elif (
            hasattr(torch.backends, "mps")
            and torch.backends.mps.is_available()
        ):
            gpu = {
                "backend": "mps",
                "available": True,
                "devices": ["Apple MPS"],
            }
        else:
            gpu["backend"] = "torch-cpu"
    except Exception as exc:
        gpu = {
            "backend": "unavailable",
            "available": False,
            "devices": [],
            "error_type": type(exc).__name__,
        }
    return {"cpu": cpu, "gpu": gpu}


def capture_runtime_environment(
    strategy: Any,
    strategy_metadata: Any,
) -> dict[str, Any]:
    """Capture only portable facts; never persist paths or environment secrets."""
    requirements = settings.PROJECT_ROOT / "requirements.txt"
    dependency_file = {
        "name": "requirements.txt",
        "sha256": _file_sha256(requirements) if requirements.is_file() else None,
    }
    return {
        "git": capture_git_state(),
        "observed_worktree_drift": observed_worktree_drift(),
        "strategy": {
            "strategy_id": str(strategy_metadata.strategy_id),
            "version": str(strategy_metadata.version),
            "source_sha256": _strategy_source_sha256(strategy),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "dependencies": {
            "lock_file": dependency_file,
            "packages": _package_versions(),
        },
        "devices": _device_inventory(),
    }


def _assert_safe_manifest_value(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in SENSITIVE_KEY_PARTS):
                raise ManifestSecurityError(
                    f"secret-like manifest key is forbidden at {path}.{key_text}"
                )
            _assert_safe_manifest_value(item, f"{path}.{key_text}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_safe_manifest_value(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        if _ABSOLUTE_WINDOWS_PATH.match(value) or value.startswith(("/", "\\")):
            raise ManifestSecurityError(
                f"absolute local path is forbidden at {path}"
            )


def resolve_execution_payload(
    strategy: Any,
    params: Mapping[str, Any],
    strategy_metadata: Any | None = None,
) -> dict[str, Any]:
    """Resolve the settings that will be passed to ``BacktestEngine``."""
    nested = params.get("execution", {})
    if not isinstance(nested, Mapping):
        nested = {}
    cost_values = nested.get("cost_model", {})
    if not isinstance(cost_values, Mapping):
        cost_values = {}
    constraint_values = nested.get("execution_constraints", {})
    if not isinstance(constraint_values, Mapping):
        constraint_values = {}

    cost_model = CostModel(
        commission_rate=float(
            cost_values.get(
                "commission_rate",
                params.get("commission_rate", 0.0003),
            )
        ),
        slippage_rate=float(
            cost_values.get(
                "slippage_rate",
                params.get("slippage_rate", 0.001),
            )
        ),
        stamp_duty_rate=float(
            cost_values.get(
                "stamp_duty_rate",
                params.get("stamp_duty_rate", 0.001),
            )
        ),
        min_commission=float(
            cost_values.get(
                "min_commission",
                params.get("min_commission", 5.0),
            )
        ),
    )
    volume_participation = constraint_values.get(
        "volume_participation",
        params.get("volume_participation"),
    )
    constraints = ExecutionConstraints(
        volume_participation=(
            float(volume_participation)
            if volume_participation is not None
            else None
        ),
        lot_size=int(
            constraint_values.get(
                "lot_size",
                params.get("lot_size", 100),
            )
        ),
    )
    rebalance_mode = _enum_text(
        nested.get(
            "rebalance_mode",
            params.get(
                "rebalance_mode",
                getattr(
                    strategy,
                    "rebalance_mode",
                    getattr(
                        strategy_metadata,
                        "rebalance_mode",
                        "signal_driven",
                    ),
                ),
            ),
        )
    )
    portfolio_signal_mode = _enum_text(
        nested.get(
            "portfolio_signal_mode",
            params.get(
                "portfolio_signal_mode",
                getattr(
                    strategy,
                    "portfolio_signal_mode",
                    getattr(
                        strategy_metadata,
                        "portfolio_signal_mode",
                        "event_orders",
                    ),
                ),
            ),
        )
    )
    if rebalance_mode not in {"signal_driven", "monthly_liquidate_compat"}:
        raise ManifestError("unsupported rebalance_mode")
    if portfolio_signal_mode not in {"event_orders", "target_weights"}:
        raise ManifestError("unsupported portfolio_signal_mode")
    initial_capital = float(
        nested.get(
            "initial_capital",
            params.get("initial_capital", 1_000_000),
        )
    )
    max_positions = int(
        nested.get(
            "max_positions",
            params.get("max_positions", 20),
        )
    )
    if initial_capital <= 0 or max_positions <= 0:
        raise ManifestError("capital and max_positions must be positive")
    if any(
        value < 0
        for value in (
            cost_model.commission_rate,
            cost_model.slippage_rate,
            cost_model.stamp_duty_rate,
            cost_model.min_commission,
        )
    ):
        raise ManifestError("cost model values must be non-negative")
    return {
        "initial_capital": initial_capital,
        "max_positions": max_positions,
        "cost_model": {
            "commission_rate": cost_model.commission_rate,
            "slippage_rate": cost_model.slippage_rate,
            "stamp_duty_rate": cost_model.stamp_duty_rate,
            "min_commission": cost_model.min_commission,
        },
        "execution_constraints": asdict(constraints),
        "rebalance_mode": rebalance_mode,
        "portfolio_signal_mode": portfolio_signal_mode,
        "signal_timing": _enum_text(
            nested.get(
                "signal_timing",
                getattr(
                    strategy,
                    "signal_timing",
                    getattr(
                        strategy_metadata,
                        "signal_timing",
                        "signal_on_T_fill_next_session_open",
                    ),
                ),
            )
        ),
    }


def build_run_manifest(
    *,
    experiment: Mapping[str, Any],
    strategy: Any,
    strategy_metadata: Any,
    params: Mapping[str, Any],
    dataset_version: DatasetVersion,
    universe_snapshot: UniverseSnapshot,
    benchmark: Mapping[str, Any],
    market_data_quality: MarketDataQualitySnapshot,
    dataset_snapshot: Mapping[str, Any] | None = None,
    replay: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    execution: Mapping[str, Any] | None = None,
    research_trust: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic initial manifest after data is loaded."""
    runtime_environment = dict(
        environment
        if environment is not None
        else capture_runtime_environment(strategy, strategy_metadata)
    )
    params_payload = dict(params)
    params_hash = canonical_sha256(params_payload)
    random_seed = params_payload.get(
        "random_seed",
        params_payload.get("seed", 42),
    )
    thread_settings = {
        "model_n_jobs": params_payload.get("n_jobs"),
        "environment": {
            key: os.environ.get(key)
            for key in THREAD_ENV_KEYS
            if os.environ.get(key) is not None
        },
    }
    manifest = {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "experiment": {
            "experiment_id": int(experiment["id"]),
            "strategy_id": str(experiment["strategy_id"]),
            "mode": str(experiment.get("mode") or "batch"),
            "data_access_policy": str(
                experiment.get("data_access_policy") or "allow_fetch"
            ),
        },
        "strategy": dict(runtime_environment["strategy"]),
        "environment": runtime_environment,
        "parameters": {
            "canonical": params_payload,
            "sha256": params_hash,
        },
        "windows": {
            "train_start": experiment.get("train_start"),
            "train_end": experiment.get("train_end"),
            "test_start": experiment.get("test_start"),
            "test_end": experiment.get("test_end"),
            "data_start": dataset_version.start,
            "data_end": dataset_version.end,
        },
        "determinism": {
            "random_seed": random_seed,
            "thread_settings": thread_settings,
        },
        "execution": dict(
            execution
            if execution is not None
            else resolve_execution_payload(
                strategy,
                params_payload,
                strategy_metadata,
            )
        ),
        "benchmark": dict(benchmark),
        "dataset": {
            **dataset_version.to_dict(),
            **(
                {"snapshot": dict(dataset_snapshot)}
                if dataset_snapshot is not None
                else {}
            ),
        },
        "market_data_quality": market_data_quality.to_dict(),
        "universe": universe_snapshot.to_dict(),
        "research_risk_warnings": list(universe_snapshot.risk_warnings),
        "replay": dict(replay or {}),
    }
    if research_trust is not None:
        trust_payload = dict(research_trust)
        quality_source = manifest["market_data_quality"].get("source", {})
        if (
            trust_payload.get("schema_version") != "tushare-research-trust/v1"
            or trust_payload.get("profile") != "tushare_research_trusted"
            or trust_payload.get("eligible") is not True
            or not isinstance(quality_source, Mapping)
            or quality_source.get("provider") != "tushare"
        ):
            raise ManifestError("conditional research trust evidence is invalid")
        manifest["research_trust"] = trust_payload
        manifest["experiment"]["research_trust_profile"] = (
            "tushare_research_trusted"
        )
        limitations = trust_payload.get("known_limitations")
        if not isinstance(limitations, list) or not limitations:
            raise ManifestError("conditional research limitations are missing")
        manifest["research_risk_warnings"] = list(
            dict.fromkeys(
                [
                    *manifest["research_risk_warnings"],
                    *[str(item) for item in limitations if str(item).strip()],
                ]
            )
        )
    universe_payload = manifest["universe"]
    execution_payload = manifest["execution"]
    canonical_binding = execution_payload.get("canonical_price_binding")
    role_usage = (
        canonical_binding.get("price_role_usage")
        if isinstance(canonical_binding, Mapping)
        else None
    )
    expected_role_usage = {
        "signal_and_research_features": "research_adjusted",
        "execution_fills_and_valuation": "raw_execution",
        "mixed_role_fallback_allowed": False,
    }
    role_usage_verified = role_usage == expected_role_usage
    bitemporal_verified = bool(
        isinstance(canonical_binding, Mapping)
        and canonical_binding.get("bitemporal_availability_verified") is True
        and isinstance(canonical_binding.get("as_known_at"), str)
        and canonical_binding.get("as_known_at")
        == universe_payload.get("timeline_identity", {}).get("as_known_at")
        and universe_payload.get("timeline_identity", {}).get(
            "bitemporal_availability_verified"
        )
        is True
    )
    manifest["pit_runtime"] = {
        "schema_version": "pit-runtime-binding/v1",
        "verified": bool(
            manifest["experiment"]["data_access_policy"] == "cache_only"
            and universe_payload.get("point_in_time") is True
            and isinstance(universe_payload.get("timeline_identity"), Mapping)
            and isinstance(canonical_binding, Mapping)
        ),
        "network_accessed": False,
        "legacy_or_static_fallback_allowed": False,
        "timeline_hash": (
            universe_payload.get("timeline_identity", {}).get("timeline_hash")
            if isinstance(universe_payload.get("timeline_identity"), Mapping)
            else None
        ),
        "canonical_price_binding_id": (
            canonical_binding.get("binding_id")
            if isinstance(canonical_binding, Mapping)
            else None
        ),
        "canonical_price_binding_digest": (
            canonical_binding.get("binding_digest")
            if isinstance(canonical_binding, Mapping)
            else None
        ),
    }
    if isinstance(canonical_binding, Mapping) and (
        canonical_binding.get("bitemporal_availability_verified") is not None
        or canonical_binding.get("price_role_usage") is not None
    ):
        manifest["pit_runtime"].update(
            bitemporal_verified=bitemporal_verified,
            price_role_usage_verified=role_usage_verified,
            as_known_at=canonical_binding.get("as_known_at"),
            production_eligible=bool(
                bitemporal_verified and role_usage_verified
            ),
        )
    qa_attestation = execution_payload.get("qa_runtime_attestation")
    if isinstance(qa_attestation, Mapping):
        manifest["pit_runtime"]["qa_runtime_attestation"] = dict(
            qa_attestation
        )
        manifest["pit_runtime"]["production_eligible"] = False
    if research_trust is not None:
        manifest["pit_runtime"].update(
            verified=False,
            production_eligible=False,
            trust_tier="conditional_personal_research",
            paper_trading_eligible=True,
            live_trading_eligible=False,
        )
    _assert_safe_manifest_value(manifest)
    # Validate finite canonical JSON before any persistence.
    canonical_json_bytes(manifest)
    return manifest


def manifest_envelope(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "manifest": payload,
        "manifest_hash": canonical_sha256(payload),
    }


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flattened: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten(value[key], child))
        return flattened
    return {prefix: value}


def structured_differences(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    prefix: str = "",
) -> list[dict[str, Any]]:
    left = _flatten(expected, prefix)
    right = _flatten(actual, prefix)
    return [
        {
            "field": field,
            "expected": left.get(field),
            "actual": right.get(field),
        }
        for field in sorted(set(left) | set(right))
        if left.get(field) != right.get(field)
    ]


def compare_replay_environment(
    source_manifest: Mapping[str, Any],
    current_environment: Mapping[str, Any],
) -> list[dict[str, Any]]:
    expected = source_manifest.get("environment", {})
    differences = structured_differences(
        expected,
        current_environment,
        prefix="environment",
    )
    git = expected.get("git", {}) if isinstance(expected, Mapping) else {}
    if git.get("dirty"):
        differences.append(
            {
                "field": "environment.git.dirty_unverifiable",
                "expected": False,
                "actual": True,
            }
        )
    return differences


def expected_replay_differences(
    manifest: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compare data/universe hashes immediately before a replay backtest."""
    actual = {
        "source_manifest_hash": manifest.get("replay", {}).get(
            "source_manifest_hash"
        ),
        "dataset_digest": manifest.get("dataset", {}).get("digest"),
        "universe_snapshot_hash": manifest.get("universe", {}).get(
            "snapshot_hash"
        ),
        "benchmark_sha256": manifest.get("benchmark", {}).get("sha256"),
        "market_data_quality_sha256": manifest.get(
            "market_data_quality", {}
        ).get("content_sha256"),
    }
    relevant_expected = {
        "source_manifest_hash": expected.get("source_manifest_hash"),
        "dataset_digest": expected.get("dataset_digest"),
        "universe_snapshot_hash": expected.get("universe_snapshot_hash"),
        "benchmark_sha256": expected.get("benchmark_sha256"),
        "market_data_quality_sha256": expected.get(
            "market_data_quality_sha256"
        ),
    }
    if not expected.get("allow_environment_drift", False):
        actual["environment_sha256"] = canonical_sha256(
            manifest.get("environment", {})
        )
        relevant_expected["environment_sha256"] = expected.get(
            "environment_sha256"
        )
    return structured_differences(relevant_expected, actual, prefix="replay")


async def persist_initial_manifest(
    *,
    db_path: Path | str,
    experiment_id: int,
    user_id: int,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Insert once and atomically bind ``experiments.code_version``."""
    envelope = manifest_envelope(manifest)
    manifest_json = canonical_json_bytes(envelope["manifest"]).decode("utf-8")
    git_state = envelope["manifest"]["environment"]["git"]
    resolved_code_version = code_version(git_state)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    try:
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """
            SELECT r.manifest_json, r.manifest_hash, r.user_id,
                   e.code_version
            FROM research_run_manifests r
            JOIN experiments e ON e.id = r.experiment_id
            WHERE r.experiment_id=?
            """,
            (experiment_id,),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            current = json.loads(existing["manifest_json"])
            current_hash = canonical_sha256(current)
            integrity_differences: list[dict[str, Any]] = []
            if current_hash != existing["manifest_hash"]:
                integrity_differences.append(
                    {
                        "field": "manifest_hash",
                        "expected": existing["manifest_hash"],
                        "actual": current_hash,
                    }
                )
            if int(existing["user_id"]) != user_id:
                integrity_differences.append(
                    {
                        "field": "user_id",
                        "expected": int(existing["user_id"]),
                        "actual": user_id,
                    }
                )
            if existing["code_version"] != resolved_code_version:
                integrity_differences.append(
                    {
                        "field": "code_version",
                        "expected": existing["code_version"],
                        "actual": resolved_code_version,
                    }
                )
            if existing["manifest_hash"] != envelope["manifest_hash"]:
                integrity_differences.extend(
                    structured_differences(current, envelope["manifest"])
                )
            if integrity_differences:
                raise ManifestConflictError(integrity_differences)
            await connection.rollback()
            return {
                "manifest": current,
                "manifest_hash": existing["manifest_hash"],
                "code_version": resolved_code_version,
            }
        await connection.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                user_id,
                RUN_MANIFEST_SCHEMA,
                manifest_json,
                envelope["manifest_hash"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        cursor = await connection.execute(
            """
            UPDATE experiments SET code_version=?
            WHERE id=? AND user_id=?
            """,
            (resolved_code_version, experiment_id, user_id),
        )
        if cursor.rowcount != 1:
            raise ManifestError("experiment ownership changed before manifest write")
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise
    finally:
        await connection.close()
    return {
        **envelope,
        "code_version": resolved_code_version,
    }


async def load_run_manifest(
    db_path: Path | str,
    experiment_id: int,
) -> dict[str, Any] | None:
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    try:
        cursor = await connection.execute(
            """
            SELECT schema_version, manifest_json, manifest_hash, created_at
            FROM research_run_manifests WHERE experiment_id=?
            """,
            (experiment_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        manifest = json.loads(row["manifest_json"])
        actual_hash = canonical_sha256(manifest)
        if actual_hash != row["manifest_hash"]:
            raise ManifestConflictError(
                [{
                    "field": "manifest_hash",
                    "expected": row["manifest_hash"],
                    "actual": actual_hash,
                }]
            )
        cursor = await connection.execute(
            """
            SELECT artifact_kind, artifact_sha256, artifact_size,
                   metadata_json, created_at
            FROM research_artifact_manifests
            WHERE experiment_id=? ORDER BY id
            """,
            (experiment_id,),
        )
        artifacts = [
            {
                "artifact_kind": item["artifact_kind"],
                "sha256": item["artifact_sha256"],
                "size": item["artifact_size"],
                "metadata": json.loads(item["metadata_json"] or "{}"),
                "created_at": item["created_at"],
            }
            for item in await cursor.fetchall()
        ]
        return {
            "schema_version": row["schema_version"],
            "manifest": manifest,
            "manifest_hash": row["manifest_hash"],
            "created_at": row["created_at"],
            "artifacts": artifacts,
        }
    finally:
        await connection.close()


async def append_artifact_manifest(
    *,
    db_path: Path | str,
    experiment_id: int,
    run_manifest_hash: str,
    artifact_kind: str,
    artifact_path: Path | str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a hash-only supplement; never mutate the initial manifest."""
    path = Path(artifact_path)
    size = path.stat().st_size
    sha256 = await asyncio.to_thread(_file_sha256, path)
    safe_metadata = dict(metadata or {})
    _assert_safe_manifest_value(safe_metadata)
    metadata_json = canonical_json_bytes(safe_metadata).decode("utf-8")
    connection = await aiosqlite.connect(str(db_path))
    try:
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute(
            """
            INSERT OR IGNORE INTO research_artifact_manifests
                (experiment_id, run_manifest_hash, schema_version,
                 artifact_kind, artifact_sha256, artifact_size,
                 metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                run_manifest_hash,
                ARTIFACT_MANIFEST_SCHEMA,
                artifact_kind,
                sha256,
                size,
                metadata_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await connection.commit()
    finally:
        await connection.close()
    return {
        "artifact_kind": artifact_kind,
        "sha256": sha256,
        "size": size,
        "metadata": safe_metadata,
    }
