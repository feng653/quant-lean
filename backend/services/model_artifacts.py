"""Fail-closed verification for model files before deserialization."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import aiosqlite

from backend.config import settings
from backend.services.research_manifest import (
    ARTIFACT_MANIFEST_SCHEMA,
    RUN_MANIFEST_SCHEMA,
    canonical_sha256,
)
from backend.services.model_serialization import (
    LEGACY_PLATFORM_JOBLIB_V0,
    ModelSerializationError,
    validate_contract,
)

MAX_MODEL_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
RETRAIN_MANIFEST_SCHEMA = "model-retrain-manifest/v1"


class ModelArtifactIntegrityError(ValueError):
    """A model artifact cannot be proven safe enough to deserialize."""


@dataclass(frozen=True)
class VerifiedModelArtifact:
    path: Path
    sha256: str
    size: int
    source: str
    model_version: int
    serialization: object


def _store_root() -> Path:
    return settings.abs_path(settings.MODEL_STORE_DIR).resolve(strict=False)


def _resolve_stored_path(raw_path: object) -> Path:
    if not raw_path or not str(raw_path).strip():
        raise ModelArtifactIntegrityError("model path is missing")
    candidate = Path(str(raw_path))
    if not candidate.is_absolute():
        candidate = settings.PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ModelArtifactIntegrityError("model file does not exist") from exc
    root = _store_root()
    if not resolved.is_relative_to(root):
        raise ModelArtifactIntegrityError(
            "model path is outside MODEL_STORE_DIR"
        )
    if not resolved.is_file():
        raise ModelArtifactIntegrityError("model path is not a regular file")
    return resolved


def _validated_sha256(value: object) -> str:
    digest = str(value or "").lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ModelArtifactIntegrityError(
            "model SHA-256 evidence is missing or invalid"
        )
    return digest


def _validated_size(value: object) -> int:
    if isinstance(value, bool):
        raise ModelArtifactIntegrityError("model size evidence is missing or invalid")
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise ModelArtifactIntegrityError(
            "model size evidence is missing or invalid"
        ) from exc
    if size <= 0:
        raise ModelArtifactIntegrityError(
            "model size evidence is missing or invalid"
        )
    return size


def _legacy_serialization(path: Path) -> str:
    """Compatibility for artifacts predating explicit contracts.

    This is deliberately limited to old platform-managed ``.joblib`` files
    that have already passed immutable provenance, version, size, and hash
    checks.  A naked file path never reaches this branch.
    """

    if path.suffix.lower() != ".joblib":
        raise ModelArtifactIntegrityError(
            "legacy model serialization is not eligible for compatibility"
        )
    return LEGACY_PLATFORM_JOBLIB_V0


def _require_serialization(
    value: object,
    *,
    path: Path,
) -> object:
    """Preserve a signed contract for later strategy-specific validation."""

    if value is None:
        return _legacy_serialization(path)
    if not isinstance(value, Mapping):
        raise ModelArtifactIntegrityError("model serialization contract is invalid")
    # Structural validation independent of a strategy.  Strategy matching is
    # repeated immediately before deserialization, where the actual loader is
    # available.
    artifact_format = value.get("format")
    if artifact_format not in {"joblib-platform-v1", "torch-state-dict-v1"}:
        raise ModelArtifactIntegrityError("model serialization format is not allowed")
    return dict(value)


def _validate_for_loader(strategy: Any, serialization: object) -> str:
    try:
        return validate_contract(
            serialization,
            strategy=strategy,
            allow_legacy=True,
        )
    except ModelSerializationError as exc:
        raise ModelArtifactIntegrityError(str(exc)) from exc


def file_sha256(path: Path) -> str:
    with path.open("rb") as model_file:
        return hashlib.file_digest(model_file, "sha256").hexdigest()


async def verify_model_file(
    raw_path: object,
    expected_sha256: object,
    expected_size: object,
) -> tuple[Path, str, int]:
    """Verify containment, existence, size, and digest before loading."""
    path = _resolve_stored_path(raw_path)
    digest = _validated_sha256(expected_sha256)
    size = _validated_size(expected_size)
    actual_size = path.stat().st_size
    if actual_size > MAX_MODEL_ARTIFACT_BYTES:
        raise ModelArtifactIntegrityError(
            "model file exceeds the configured size limit"
        )
    if actual_size != size:
        raise ModelArtifactIntegrityError(
            f"model size mismatch: expected={size}, actual={actual_size}"
        )
    actual_digest = await asyncio.to_thread(file_sha256, path)
    if not hmac.compare_digest(actual_digest, digest):
        raise ModelArtifactIntegrityError("model SHA-256 mismatch")
    return path, digest, size


def _create_verified_snapshot(artifact: VerifiedModelArtifact) -> Path:
    """Copy and re-verify the exact bytes that will be deserialized."""
    snapshot_dir = _store_root() / ".verified-load"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    descriptor, raw_snapshot = tempfile.mkstemp(
        prefix="model-",
        suffix=artifact.path.suffix,
        dir=snapshot_dir,
    )
    snapshot = Path(raw_snapshot)
    digest = hashlib.sha256()
    copied = 0
    try:
        with (
            artifact.path.open("rb") as source,
            open(descriptor, "wb", closefd=True) as destination,
        ):
            while chunk := source.read(1024 * 1024):
                copied += len(chunk)
                if copied > MAX_MODEL_ARTIFACT_BYTES:
                    raise ModelArtifactIntegrityError(
                        "model file exceeds the configured size limit"
                    )
                digest.update(chunk)
                destination.write(chunk)
        if copied != artifact.size:
            raise ModelArtifactIntegrityError(
                "model changed while creating the verified snapshot"
            )
        if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
            raise ModelArtifactIntegrityError(
                "model changed while creating the verified snapshot"
            )
        return snapshot
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise


def _deployment_identity(deployment: Mapping[str, Any]) -> tuple[int, str, str]:
    try:
        owner_id = int(deployment["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelArtifactIntegrityError("deployment owner is missing") from exc
    strategy_id = str(deployment.get("strategy_id") or "")
    params_hash = str(deployment.get("params_hash") or "")
    if not strategy_id or not params_hash:
        raise ModelArtifactIntegrityError(
            "deployment strategy or parameter identity is missing"
        )
    return owner_id, strategy_id, params_hash


async def verify_current_deployment_model(
    deployment: Mapping[str, Any],
) -> VerifiedModelArtifact | None:
    """Verify the promoted deployment champion against immutable history."""
    current_path = deployment.get("current_model_path")
    current_sha256 = deployment.get("current_model_sha256")
    current_size = deployment.get("current_model_size")
    if current_path is None and current_sha256 is None and current_size is None:
        return None
    if current_path is None or current_sha256 is None or current_size is None:
        raise ModelArtifactIntegrityError(
            "deployment current model evidence is incomplete"
        )
    normalized_current_sha256 = _validated_sha256(current_sha256)
    normalized_current_size = _validated_size(current_size)
    owner_id, strategy_id, params_hash = _deployment_identity(deployment)
    try:
        deployment_id = int(deployment["id"])
        model_version = int(deployment["current_model_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelArtifactIntegrityError(
            "deployment model version identity is missing"
        ) from exc

    async with aiosqlite.connect(
        str(settings.abs_path(settings.TRADING_SIM_DB))
    ) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            """
            SELECT mv.model_file_path, mv.model_sha256, mv.model_size,
                   mv.strategy_id, mv.params_hash, mv.status,
                   mv.retrain_manifest_json, mv.retrain_manifest_hash
            FROM model_version_history mv
            JOIN deployments d ON d.id=mv.deployment_id
            WHERE mv.deployment_id=? AND mv.model_version=?
              AND mv.is_latest=1
              AND d.user_id=? AND d.strategy_id=? AND d.params_hash=?
              AND d.current_model_version=mv.model_version
              AND d.current_model_path=mv.model_file_path
              AND d.current_model_sha256=mv.model_sha256
              AND d.current_model_size=mv.model_size
              AND d.current_model_path=?
              AND d.current_model_sha256=?
              AND d.current_model_size=?
            """,
            (
                deployment_id,
                model_version,
                owner_id,
                strategy_id,
                params_hash,
                str(current_path),
                normalized_current_sha256,
                normalized_current_size,
            ),
        )
        rows = await cursor.fetchall()
    if len(rows) != 1:
        raise ModelArtifactIntegrityError(
            "deployment current model has no unique promoted history record"
        )
    history = dict(rows[0])
    if history.get("status") != "promoted":
        raise ModelArtifactIntegrityError(
            "deployment current model history is not promoted"
        )
    if (
        history.get("strategy_id") != strategy_id
        or history.get("params_hash") != params_hash
    ):
        raise ModelArtifactIntegrityError(
            "deployment current model strategy or parameters do not match"
        )
    retrain_manifest = _parse_json_object(
        history.get("retrain_manifest_json"),
        field="retrain manifest",
    )
    retrain_manifest_hash = _validated_sha256(
        history.get("retrain_manifest_hash")
    )
    if (
        retrain_manifest.get("schema_version") != RETRAIN_MANIFEST_SCHEMA
        or not hmac.compare_digest(
            canonical_sha256(retrain_manifest),
            retrain_manifest_hash,
        )
    ):
        raise ModelArtifactIntegrityError(
            "deployment current model retrain manifest is invalid"
        )
    manifest_deployment = retrain_manifest.get("deployment") or {}
    manifest_artifact = retrain_manifest.get("artifact") or {}
    if (
        manifest_deployment.get("deployment_id") != deployment_id
        or manifest_deployment.get("owner_id") != owner_id
        or manifest_deployment.get("strategy_id") != strategy_id
        or manifest_deployment.get("params_hash") != params_hash
        or manifest_deployment.get("model_version") != model_version
        or manifest_artifact.get("sha256") != normalized_current_sha256
        or manifest_artifact.get("size") != normalized_current_size
        or not retrain_manifest.get("parameters")
        or not retrain_manifest.get("windows")
        or not retrain_manifest.get("validation")
        or not retrain_manifest.get("dataset")
    ):
        raise ModelArtifactIntegrityError(
            "deployment current model retrain manifest identity does not match"
        )
    path, digest, size = await verify_model_file(
        current_path,
        normalized_current_sha256,
        normalized_current_size,
    )
    history_path, history_digest, history_size = await verify_model_file(
        history["model_file_path"],
        history["model_sha256"],
        history["model_size"],
    )
    if (
        path != history_path
        or not hmac.compare_digest(digest, history_digest)
        or size != history_size
    ):
        raise ModelArtifactIntegrityError(
            "deployment current model does not match version history"
        )
    return VerifiedModelArtifact(
        path=path,
        sha256=digest,
        size=size,
        source="deployment",
        model_version=model_version,
        serialization=_require_serialization(
            manifest_artifact.get("serialization"), path=path
        ),
    )


def _parse_json_object(raw: object, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ModelArtifactIntegrityError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ModelArtifactIntegrityError(f"{field} is not a JSON object")
    return value


def _validate_run_manifest_structure(manifest: Mapping[str, Any]) -> None:
    required_objects = (
        "experiment",
        "strategy",
        "environment",
        "parameters",
        "windows",
        "dataset",
        "universe",
    )
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA:
        raise ModelArtifactIntegrityError(
            "source model RunManifest JSON schema is unsupported"
        )
    for field in required_objects:
        value = manifest.get(field)
        if not isinstance(value, dict) or not value:
            raise ModelArtifactIntegrityError(
                f"source model RunManifest is incomplete: {field}"
            )
    dataset = manifest["dataset"]
    if not dataset.get("digest") or not dataset.get("context_digest"):
        raise ModelArtifactIntegrityError(
            "source model RunManifest dataset identity is incomplete"
        )


async def verify_source_experiment_model(
    deployment: Mapping[str, Any],
) -> VerifiedModelArtifact | None:
    """Verify an experiment artifact and its RunManifest evidence."""
    artifact_id = deployment.get("source_model_artifact_id")
    if artifact_id is None:
        return None
    source_experiment_id = deployment.get("source_experiment_id")
    if source_experiment_id is None:
        raise ModelArtifactIntegrityError(
            "source model artifact is detached from an experiment"
        )
    owner_id, strategy_id, params_hash = _deployment_identity(deployment)
    async with aiosqlite.connect(
        str(settings.abs_path(settings.EXPERIMENT_DB))
    ) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            """
            SELECT ma.experiment_id, ma.strategy_id, ma.model_version,
                   ma.model_file_path, ma.params_hash,
                   ma.artifact_sha256, ma.artifact_size,
                   ma.run_manifest_hash,
                   e.user_id, e.strategy_id AS experiment_strategy_id,
                   e.params, e.params_hash AS experiment_params_hash,
                   e.status AS experiment_status,
                   rm.user_id AS manifest_user_id,
                   rm.schema_version AS run_schema_version,
                   rm.manifest_json, rm.manifest_hash,
                   am.schema_version AS artifact_schema_version,
                   am.metadata_json AS artifact_metadata_json
            FROM model_artifacts ma
            JOIN experiments e ON e.id=ma.experiment_id
            LEFT JOIN research_run_manifests rm
              ON rm.experiment_id=ma.experiment_id
             AND rm.manifest_hash=ma.run_manifest_hash
            LEFT JOIN research_artifact_manifests am
              ON am.experiment_id=ma.experiment_id
             AND am.run_manifest_hash=ma.run_manifest_hash
             AND am.artifact_kind='trained_model'
             AND am.artifact_sha256=ma.artifact_sha256
             AND am.artifact_size=ma.artifact_size
            WHERE ma.id=?
            """,
            (int(artifact_id),),
        )
        rows = await cursor.fetchall()
    if len(rows) != 1:
        raise ModelArtifactIntegrityError(
            "source model has no unique RunManifest artifact evidence"
        )
    evidence = dict(rows[0])
    expected_experiment_id = int(source_experiment_id)
    if (
        int(evidence["experiment_id"]) != expected_experiment_id
        or int(evidence["user_id"]) != owner_id
        or evidence["strategy_id"] != strategy_id
        or evidence["experiment_strategy_id"] != strategy_id
        or evidence["params_hash"] != params_hash
        or evidence["experiment_params_hash"] != params_hash
        or evidence["experiment_status"] != "completed"
    ):
        raise ModelArtifactIntegrityError(
            "source model owner, strategy, experiment, or parameters do not match"
        )
    try:
        manifest_owner_id = int(evidence["manifest_user_id"])
    except (TypeError, ValueError) as exc:
        raise ModelArtifactIntegrityError(
            "source model RunManifest evidence is missing or unsupported"
        ) from exc
    if (
        evidence["run_schema_version"] != RUN_MANIFEST_SCHEMA
        or evidence["artifact_schema_version"] != ARTIFACT_MANIFEST_SCHEMA
        or manifest_owner_id != owner_id
    ):
        raise ModelArtifactIntegrityError(
            "source model RunManifest evidence is missing or unsupported"
        )
    manifest = _parse_json_object(
        evidence["manifest_json"],
        field="RunManifest",
    )
    _validate_run_manifest_structure(manifest)
    manifest_hash = canonical_sha256(manifest)
    if (
        not hmac.compare_digest(manifest_hash, evidence["manifest_hash"])
        or not hmac.compare_digest(manifest_hash, evidence["run_manifest_hash"])
    ):
        raise ModelArtifactIntegrityError("source model RunManifest was tampered")
    experiment_identity = manifest.get("experiment") or {}
    manifest_parameters = manifest.get("parameters") or {}
    deployment_params = _parse_json_object(
        deployment.get("params") or "{}",
        field="deployment parameters",
    )
    experiment_params = _parse_json_object(
        evidence["params"] or "{}",
        field="experiment parameters",
    )
    if (
        experiment_identity.get("experiment_id") != expected_experiment_id
        or experiment_identity.get("strategy_id") != strategy_id
        or manifest_parameters.get("canonical") != experiment_params
        or deployment_params != experiment_params
        or manifest_parameters.get("sha256") != canonical_sha256(experiment_params)
    ):
        raise ModelArtifactIntegrityError(
            "source model RunManifest identity or parameters do not match"
        )
    artifact_metadata = _parse_json_object(
        evidence["artifact_metadata_json"],
        field="artifact metadata",
    )
    if (
        artifact_metadata.get("strategy_id") != strategy_id
        or int(artifact_metadata.get("model_version") or -1)
        != int(evidence["model_version"])
    ):
        raise ModelArtifactIntegrityError(
            "source model artifact metadata does not match"
        )
    path, digest, size = await verify_model_file(
        evidence["model_file_path"],
        evidence["artifact_sha256"],
        evidence["artifact_size"],
    )
    return VerifiedModelArtifact(
        path=path,
        sha256=digest,
        size=size,
        source="experiment",
        model_version=int(evidence["model_version"]),
        serialization=_require_serialization(
            (artifact_metadata.get("model") or {}).get("serialization"),
            path=path,
        ),
    )


async def load_verified_deployment_model(
    strategy: Any,
    deployment: Mapping[str, Any],
) -> VerifiedModelArtifact | None:
    """Prefer the promoted champion, then a verified source experiment."""
    verified = await verify_current_deployment_model(deployment)
    if verified is None:
        verified = await verify_source_experiment_model(deployment)
    if verified is None:
        if bool(deployment.get("requires_retraining")):
            raise ModelArtifactIntegrityError(
                "trainable deployment has no verified model artifact"
            )
        return None
    serialization = _validate_for_loader(strategy, verified.serialization)
    snapshot = await asyncio.to_thread(_create_verified_snapshot, verified)
    try:
        # Strategies use this private, short-lived capability only to select a
        # non-executable torch loader.  It is set after all provenance checks
        # and removed even when the loader rejects the artifact.
        strategy._verified_model_serialization = serialization
        model = await asyncio.to_thread(strategy.load_model, str(snapshot))
    finally:
        if hasattr(strategy, "_verified_model_serialization"):
            delattr(strategy, "_verified_model_serialization")
        snapshot.unlink(missing_ok=True)
    strategy._model = model
    strategy._verified_deployment_model = model
    return verified
