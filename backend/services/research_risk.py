"""Conservative experiment risk summaries derived from immutable manifests."""

from __future__ import annotations

import json
from typing import Any, Mapping

from backend.data.lineage import NON_POINT_IN_TIME, SURVIVORSHIP_BIAS
from backend.data.market_quality import (
    MarketDataQualityError,
    MarketDataQualitySnapshot,
)
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    ManifestError,
    canonical_sha256,
)


_CURRENT_INDEX_POOLS = {"csi300", "csi500", "csi800", "csi1000"}


def _unsafe_legacy_summary(reason: str) -> dict[str, Any]:
    return {
        "legacy": True,
        "no_manifest": reason == "manifest_missing",
        "legacy_no_manifest": reason == "manifest_missing",
        "manifest_integrity_valid": False,
        "non_point_in_time": True,
        "current_constituents": True,
        "survivorship_bias": True,
        "invalid_market_data": True,
        "warnings": sorted({reason, "live_trading_not_certified"}),
        "live_eligible": False,
    }


def research_risk_summary(
    *,
    manifest_json: str | None,
    manifest_hash: str | None,
    schema_version: str | None,
) -> dict[str, Any]:
    """Return no-false-safe flags for an experiment list/detail response."""
    if not manifest_json:
        return _unsafe_legacy_summary("manifest_missing")
    try:
        manifest = json.loads(manifest_json)
        if not isinstance(manifest, Mapping):
            raise ValueError("manifest must be an object")
        if (
            schema_version != RUN_MANIFEST_SCHEMA
            or manifest.get("schema_version") != RUN_MANIFEST_SCHEMA
            or canonical_sha256(manifest) != manifest_hash
        ):
            raise ValueError("manifest integrity verification failed")
    except (json.JSONDecodeError, ManifestError, TypeError, ValueError):
        return _unsafe_legacy_summary("manifest_integrity_failure")

    warnings = {
        str(item)
        for item in manifest.get("research_risk_warnings", [])
        if isinstance(item, str) and item
    }
    universe = manifest.get("universe")
    if not isinstance(universe, Mapping):
        return _unsafe_legacy_summary("universe_evidence_invalid")
    point_in_time = universe.get("point_in_time") is True
    pool_id = str(universe.get("pool_id") or "").lower()
    non_point_in_time = not point_in_time or NON_POINT_IN_TIME in warnings
    current_constituents = (
        pool_id in _CURRENT_INDEX_POOLS and non_point_in_time
    )
    survivorship_bias = (
        SURVIVORSHIP_BIAS in warnings or current_constituents
    )

    invalid_market_data = True
    legacy_manifest = "market_data_quality" not in manifest
    quality_warnings: set[str] = set()
    try:
        quality = MarketDataQualitySnapshot.from_dict(
            manifest.get("market_data_quality", {})
        )
    except MarketDataQualityError:
        quality_warnings.add("market_data_quality_evidence_invalid")
        if legacy_manifest:
            quality_warnings.add(
                "legacy_manifest_missing_market_data_quality"
            )
    else:
        invalid_market_data = not quality.is_clean
        quality_warnings.update(
            str(item["code"]) for item in quality.payload["warnings"]
        )
        quality_warnings.update(
            str(item["code"]) for item in quality.payload["fatal"]
        )
    warnings.update(quality_warnings)
    if current_constituents:
        warnings.add("current_constituents")
    if non_point_in_time:
        warnings.add(NON_POINT_IN_TIME)
    if survivorship_bias:
        warnings.add(SURVIVORSHIP_BIAS)
    warnings.add("live_trading_not_certified")
    universe_quality = universe.get("quality")
    dataset = manifest.get("dataset")
    git = manifest.get("environment", {}).get("git", {})
    execution = manifest.get("execution")
    research_trust = manifest.get("research_trust")
    conditional_tushare = bool(
        isinstance(research_trust, Mapping)
        and research_trust.get("profile") == "tushare_research_trusted"
        and research_trust.get("eligible") is True
    )
    if conditional_tushare:
        warnings.update(
            str(item)
            for item in research_trust.get("warnings", [])
            if isinstance(item, str) and item
        )
    platform_live_certified = False
    live_eligible = all(
        (
            point_in_time,
            not current_constituents,
            not survivorship_bias,
            not invalid_market_data,
            isinstance(universe_quality, Mapping)
            and universe_quality.get("is_clean") is True,
            isinstance(dataset, Mapping) and bool(dataset.get("digest")),
            isinstance(git, Mapping) and git.get("dirty") is False,
            isinstance(execution, Mapping) and bool(execution),
            platform_live_certified,
        )
    )
    return {
        "legacy": legacy_manifest,
        "no_manifest": False,
        "legacy_no_manifest": False,
        "manifest_integrity_valid": True,
        "non_point_in_time": non_point_in_time,
        "current_constituents": current_constituents,
        "survivorship_bias": survivorship_bias,
        "invalid_market_data": invalid_market_data,
        "warnings": sorted(warnings),
        "warning_severity": "high" if conditional_tushare else (
            "high" if warnings else "none"
        ),
        "trust_tier": (
            "conditional_personal_research"
            if conditional_tushare
            else "governed_production_pit"
        ),
        "paper_eligible": bool(
            conditional_tushare
            and isinstance(research_trust.get("claims"), Mapping)
            and research_trust["claims"].get("eligible_for_paper_trading") is True
        ),
        # Research evidence is not a broker/live-readiness certificate.
        "live_eligible": live_eligible,
    }
