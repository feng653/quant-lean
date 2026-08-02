"""Cryptographically bound, explicitly non-production PIT E2E fixtures.

This module is a test harness boundary, not a source adapter.  It can only
complete readiness evidence when the whole service is running with
``ENVIRONMENT=test`` and every mutable path is contained by one external QA
root.  Production and development therefore retain the normal fail-closed PIT
gate even if a stale QA attestation happens to exist on disk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from backend.config import settings


QA_MARKER_SCHEMA = "pit-qa-environment/v1"
QA_ATTESTATION_SCHEMA = "pit-qa-runtime-attestation/v1"


class PitQaIsolationError(RuntimeError):
    """The QA fixture configuration could weaken a non-test runtime."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def qa_attestation_sha256(value: Mapping[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("attestation_sha256", None)
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _configured_qa_root() -> Path | None:
    if settings.ENVIRONMENT != "test":
        return None
    raw_root = str(settings.PIT_QA_FIXTURE_ROOT or "").strip()
    raw_attestation = str(settings.PIT_QA_ATTESTATION or "").strip()
    if not raw_root and not raw_attestation:
        return None
    if not raw_root or not raw_attestation:
        raise PitQaIsolationError(
            "PIT QA requires both PIT_QA_FIXTURE_ROOT and PIT_QA_ATTESTATION"
        )
    root = Path(raw_root).expanduser().resolve()
    if not root.is_absolute() or root == settings.PROJECT_ROOT.resolve():
        raise PitQaIsolationError("PIT QA root must be an isolated absolute path")
    production_data = (settings.PROJECT_ROOT / "data").resolve()
    if root == production_data or _inside(root, production_data):
        raise PitQaIsolationError("PIT QA root must not be inside production data/")

    mutable_paths = (
        settings.USERS_DB,
        settings.EXPERIMENT_DB,
        settings.TRADING_SIM_DB,
        settings.TRADING_LIVE_DB,
        settings.DATA_CACHE_DIR,
        settings.DATA_STAGING_DIR,
        settings.PIT_EVIDENCE_DIR,
        settings.PIT_EVIDENCE_DB,
        settings.MODEL_STORE_DIR,
        settings.RESEARCH_SNAPSHOT_DIR,
    )
    escaped = [
        str(settings.abs_path(item).resolve())
        for item in mutable_paths
        if not _inside(settings.abs_path(item), root)
    ]
    if escaped:
        raise PitQaIsolationError(
            "PIT QA mutable paths escaped the fixture root: " + ", ".join(escaped)
        )
    marker_path = root / ".pit-qa-only.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PitQaIsolationError("PIT QA isolation marker is missing or invalid") from exc
    if marker != {
        "schema_version": QA_MARKER_SCHEMA,
        "non_production": True,
    }:
        raise PitQaIsolationError("PIT QA isolation marker is not canonical")
    return root


def verified_qa_runtime_attestation(
    *,
    pool_id: str,
    required_start: str,
    required_end: str,
    timeline_identity: Mapping[str, Any] | None,
    runtime_price_binding: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a sanitized QA proof only for an exact isolated runtime bind."""

    root = _configured_qa_root()
    if root is None:
        return None
    attestation_path = Path(settings.PIT_QA_ATTESTATION).expanduser().resolve()
    if not _inside(attestation_path, root):
        raise PitQaIsolationError("PIT QA attestation escaped the fixture root")
    try:
        payload = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PitQaIsolationError("PIT QA attestation is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise PitQaIsolationError("PIT QA attestation must be a JSON object")
    if (
        payload.get("schema_version") != QA_ATTESTATION_SCHEMA
        or payload.get("non_production") is not True
        or payload.get("production_eligible") is not False
        or payload.get("attestation_sha256") != qa_attestation_sha256(payload)
    ):
        raise PitQaIsolationError("PIT QA attestation integrity check failed")
    timeline = dict(timeline_identity or {})
    binding = dict(runtime_price_binding or {})
    expected = {
        "pool_id": str(pool_id),
        "timeline_hash": str(timeline.get("timeline_hash") or ""),
        "binding_id": str(binding.get("binding_id") or ""),
        "binding_digest": str(binding.get("binding_digest") or ""),
    }
    if any(payload.get(key) != value or not value for key, value in expected.items()):
        raise PitQaIsolationError("PIT QA attestation does not match runtime evidence")
    if not (
        str(payload.get("coverage_from") or "") <= str(required_start)
        <= str(required_end) <= str(payload.get("coverage_to") or "")
    ):
        raise PitQaIsolationError("PIT QA attestation does not cover the request")
    benchmark_path = root / str(payload.get("benchmark_artifact") or "")
    if not _inside(benchmark_path, root) or not benchmark_path.is_file():
        raise PitQaIsolationError("PIT QA benchmark artifact is unavailable")
    benchmark_sha256 = hashlib.sha256(benchmark_path.read_bytes()).hexdigest()
    if benchmark_sha256 != payload.get("benchmark_artifact_sha256"):
        raise PitQaIsolationError("PIT QA benchmark artifact integrity mismatch")
    return {
        "schema_version": QA_ATTESTATION_SCHEMA,
        "attestation_sha256": payload["attestation_sha256"],
        "non_production": True,
        "production_eligible": False,
        "fixture_kind": "deterministic_isolated_pit_e2e",
        **expected,
    }
