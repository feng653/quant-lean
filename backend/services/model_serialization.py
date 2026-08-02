"""Explicit, fail-closed serialization contracts for trained models.

Joblib and legacy ``torch.save`` files are executable serialization formats.
They may only be loaded after the artifact verifier has bound their bytes to an
immutable platform-produced manifest.  This module deliberately does *not*
attempt to make pickle safe: that is not possible for arbitrary pickles.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MODEL_SERIALIZATION_SCHEMA = "model-serialization/v1"
JOBLIB_PLATFORM_V1 = "joblib-platform-v1"
TORCH_STATE_DICT_V1 = "torch-state-dict-v1"
LEGACY_PLATFORM_JOBLIB_V0 = "legacy-platform-joblib-v0"

_CONTRACTS: dict[str, dict[str, Any]] = {
    JOBLIB_PLATFORM_V1: {
        "loader": "joblib",
        "executable_deserialization": True,
    },
    TORCH_STATE_DICT_V1: {
        "loader": "torch.weights_only",
        "executable_deserialization": False,
    },
}


class ModelSerializationError(ValueError):
    """A model's serialization contract is unsafe, absent, or mismatched."""


def contract_for_model(strategy: Any, model: Any) -> dict[str, Any]:
    """Describe exactly how a platform-produced model must be loaded.

    The two torch rank strategies save tensor-only state dictionaries; every
    other current trainable strategy persists a joblib object.  This is kept
    narrow on purpose: unknown formats are rejected rather than guessed.
    """

    metadata = getattr(strategy, "metadata", None)
    strategy_id = str(
        getattr(metadata(), "strategy_id", "") if callable(metadata) else ""
    )
    artifact_format = JOBLIB_PLATFORM_V1
    if strategy_id in {"lstm_rank_v1", "transformer_rank_v1"}:
        try:
            from backend.strategies.ml.runtime import import_optional_torch

            torch = import_optional_torch()
            if torch is not None and isinstance(model, torch.nn.Module):
                artifact_format = TORCH_STATE_DICT_V1
        except (ImportError, AttributeError):
            # No optional torch runtime means this is necessarily the sklearn
            # fallback saved through joblib.
            pass
    return {
        "schema_version": MODEL_SERIALIZATION_SCHEMA,
        "format": artifact_format,
        "loader": _CONTRACTS[artifact_format]["loader"],
        "platform_produced": True,
        "executable_deserialization": _CONTRACTS[artifact_format][
            "executable_deserialization"
        ],
    }


def _allowed_formats(strategy: Any) -> set[str]:
    metadata = getattr(strategy, "metadata", None)
    strategy_id = str(
        getattr(metadata(), "strategy_id", "") if callable(metadata) else ""
    )
    allowed = {JOBLIB_PLATFORM_V1, LEGACY_PLATFORM_JOBLIB_V0}
    if strategy_id in {"lstm_rank_v1", "transformer_rank_v1"}:
        allowed.add(TORCH_STATE_DICT_V1)
    return allowed


def validate_contract(
    value: object,
    *,
    strategy: Any,
    allow_legacy: bool = False,
) -> str:
    """Validate a signed contract before a strategy loader sees any bytes."""

    if value == LEGACY_PLATFORM_JOBLIB_V0 and allow_legacy:
        return LEGACY_PLATFORM_JOBLIB_V0
    if not isinstance(value, Mapping):
        raise ModelSerializationError("model serialization contract is missing")
    artifact_format = value.get("format")
    if artifact_format not in _CONTRACTS:
        raise ModelSerializationError("model serialization format is not allowed")
    expected = _CONTRACTS[str(artifact_format)]
    if (
        value.get("schema_version") != MODEL_SERIALIZATION_SCHEMA
        or value.get("loader") != expected["loader"]
        or value.get("platform_produced") is not True
        or value.get("executable_deserialization")
        is not expected["executable_deserialization"]
    ):
        raise ModelSerializationError("model serialization contract is invalid")
    if artifact_format not in _allowed_formats(strategy):
        raise ModelSerializationError("model serialization does not match strategy")
    return str(artifact_format)
