"""Bounded operational telemetry helpers.

Operational labels are deliberately finite and never include users, filesystem
paths, credentials, or per-job identifiers. Research evidence remains in its
own immutable stores; this module only describes service health.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.config import settings
from backend.data.generation_manifest import (
    GenerationManifestError,
    GenerationManifestStore,
)

_KNOWN_POOLS = {"csi300", "csi500", "csi800", "csi1000", "all_a"}


def structured_log(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Emit one JSON log record with an explicit low-cardinality contract."""
    allowed = {
        "component",
        "job_type",
        "outcome",
        "stage",
        "reason",
        "attempt",
        "delay_seconds",
        "count",
        "actual",
        "threshold",
        "window_hours",
    }
    payload = {
        "schema_version": "operations-log/v1",
        "event": str(event)[:80],
        **{
            key: value
            for key, value in fields.items()
            if key in allowed
            and isinstance(value, (str, int, float, bool, type(None)))
        },
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def cache_quality_snapshot() -> dict[str, Any]:
    """Aggregate cache metadata without exposing cache keys or local paths."""
    daily_dir = settings.abs_path(settings.DATA_CACHE_DIR) / "daily"
    counts = {
        "total": 0,
        "research_ready": 0,
        "execution_ready": 0,
        "quality_ready": 0,
        "legacy_or_invalid": 0,
        "missing_quality": 0,
    }
    trust_counts = {
        "exchange_authoritative": 0,
        "licensed": 0,
        "public_cross_validated_research_only": 0,
        "public_single_source_research_only": 0,
        "unverified": 0,
        "other": 0,
    }
    if not daily_dir.is_dir():
        return {
            "schema_version": "cache-quality-summary/v1",
            "counts": counts,
            "source_trust": trust_counts,
        }
    manifests = daily_dir / "generation-manifests"
    store = GenerationManifestStore(
        daily_dir,
        required_artifacts={"pivot", "metadata"},
    )
    # A hard file bound prevents an accidental custom-cache explosion from
    # turning an admin status call into a denial of service.
    for path in sorted(manifests.glob("*.json"))[:500]:
        stem = path.stem
        # Preserve only aggregate totals. The test is used solely to recognise
        # a known label class, never returned as a cache identifier.
        _ = stem in _KNOWN_POOLS
        counts["total"] += 1
        try:
            view = store.load(stem)
            if view is None:
                raise GenerationManifestError("active generation is missing")
            payload = json.loads(
                view.artifacts["metadata"].read_text(encoding="utf-8")
            )
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            GenerationManifestError,
        ):
            counts["legacy_or_invalid"] += 1
            trust_counts["unverified"] += 1
            continue
        quality = payload.get("data_quality")
        if isinstance(quality, dict) and quality.get("ready") is True:
            counts["quality_ready"] += 1
        else:
            counts["missing_quality"] += 1
        if payload.get("ready_for_return_research") is True:
            counts["research_ready"] += 1
        if payload.get("ready_for_execution_simulation") is True:
            counts["execution_ready"] += 1
        if int(payload.get("schema_version") or 0) < 4:
            counts["legacy_or_invalid"] += 1
        trust = str(payload.get("source_trust") or "unverified")
        trust_counts[trust if trust in trust_counts else "other"] += 1
    return {
        "schema_version": "cache-quality-summary/v1",
        "counts": counts,
        "source_trust": trust_counts,
    }
