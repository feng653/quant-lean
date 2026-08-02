"""Fail-closed eligibility checks for persisted experiment reuse.

Legacy experiments remain visible as audit records, but no completed row is a
research/deployment candidate merely because it has metrics.  Reuse requires
an intact manifest whose PIT identities agree across every duplicated binding.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

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

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ExperimentEligibility:
    eligible: bool
    code: str
    warnings: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        payload = {
            "pit_eligible": self.eligible and self.code == "pit_manifest_verified",
            "paper_eligible": self.eligible,
            "legacy_read_only": not self.eligible,
            "eligibility_code": self.code,
            "live_eligible": False,
        }
        if self.warnings:
            payload["warnings"] = list(self.warnings)
            payload["warning_severity"] = "high"
        return payload


class PaperRiskBindingError(ValueError):
    """An immutable paper deployment warning snapshot was changed."""


def verify_paper_risk_binding(deployment: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recompute the canonical snapshot hash and duplicated query bindings.

    Legacy deployments without a snapshot remain readable.  New deployments
    carrying any snapshot field must pass the complete binding contract before
    a simulation may run.
    """

    raw = deployment.get("research_risk_snapshot")
    stored_hash = deployment.get("research_risk_snapshot_hash")
    bound_fields = (
        deployment.get("research_generation_id"),
        deployment.get("research_source_id"),
        deployment.get("research_window_start"),
        deployment.get("research_window_end"),
    )
    if raw is None and stored_hash is None and not any(bound_fields):
        return None
    if not isinstance(raw, (str, Mapping)) or not _sha256(stored_hash):
        raise PaperRiskBindingError("paper risk snapshot identity is incomplete")
    try:
        snapshot = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PaperRiskBindingError("paper risk snapshot is not valid JSON") from exc
    if not isinstance(snapshot, Mapping):
        raise PaperRiskBindingError("paper risk snapshot is not an object")
    try:
        actual_hash = canonical_sha256(snapshot)
    except (ManifestError, TypeError, ValueError) as exc:
        raise PaperRiskBindingError("paper risk snapshot is not canonical") from exc
    if not hmac.compare_digest(actual_hash, str(stored_hash)):
        raise PaperRiskBindingError("paper risk snapshot digest changed")
    window = _mapping(snapshot.get("window"))
    if (
        snapshot.get("schema_version") != "paper-deployment-research-risk/v1"
        or snapshot.get("source_experiment_id") != deployment.get("source_experiment_id")
        or snapshot.get("research_generation_id") != deployment.get("research_generation_id")
        or snapshot.get("research_source_id") != deployment.get("research_source_id")
        or window is None
        or window.get("start") != deployment.get("research_window_start")
        or window.get("end") != deployment.get("research_window_end")
        or not isinstance(snapshot.get("warnings"), list)
        or snapshot.get("paper_eligible") is not True
        or snapshot.get("live_eligible") is not False
    ):
        raise PaperRiskBindingError("paper risk snapshot binding changed")
    source_manifest_hash = snapshot.get("source_manifest_hash")
    if not _sha256(source_manifest_hash):
        raise PaperRiskBindingError("paper risk snapshot manifest identity is invalid")
    return dict(snapshot)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def assess_experiment_eligibility(
    *,
    experiment_id: int,
    strategy_id: str,
    manifest_json: str | None,
    manifest_hash: str | None,
    schema_version: str | None,
) -> ExperimentEligibility:
    """Prove one completed experiment is safe to reuse as a PIT candidate."""

    if not manifest_json:
        return ExperimentEligibility(False, "legacy_manifest_missing")
    try:
        manifest = json.loads(manifest_json)
        if not isinstance(manifest, Mapping):
            raise ValueError
        if (
            schema_version != RUN_MANIFEST_SCHEMA
            or manifest.get("schema_version") != RUN_MANIFEST_SCHEMA
            or not _sha256(manifest_hash)
            or not hmac.compare_digest(canonical_sha256(manifest), manifest_hash)
        ):
            raise ValueError
    except (json.JSONDecodeError, ManifestError, TypeError, ValueError):
        return ExperimentEligibility(False, "manifest_integrity_invalid")

    identity = _mapping(manifest.get("experiment"))
    if (
        identity is None
        or identity.get("experiment_id") != experiment_id
        or identity.get("strategy_id") != strategy_id
    ):
        return ExperimentEligibility(False, "manifest_identity_mismatch")
    if identity.get("data_access_policy") != "cache_only":
        return ExperimentEligibility(False, "legacy_data_policy")
    trust = _mapping(manifest.get("research_trust"))
    if (
        identity.get("research_trust_profile") == "tushare_research_trusted"
        or (trust is not None and trust.get("profile") == "tushare_research_trusted")
    ):
        claims = _mapping(trust.get("claims")) if trust is not None else None
        limitations = trust.get("known_limitations") if trust is not None else None
        evidence = _mapping(trust.get("evidence")) if trust is not None else None
        universe = _mapping(manifest.get("universe"))
        timeline = _mapping(universe.get("timeline_identity")) if universe else None
        source_batches = timeline.get("source_batches") if timeline else None
        dataset = _mapping(manifest.get("dataset"))
        benchmark = _mapping(manifest.get("benchmark"))
        runtime = _mapping(manifest.get("pit_runtime"))
        try:
            quality = MarketDataQualitySnapshot.from_dict(
                manifest.get("market_data_quality", {})
            )
        except MarketDataQualityError:
            quality = None
        report_digest = evidence.get("candidate_report_sha256") if evidence else None
        research_generation_id = (
            evidence.get("research_generation_id") if evidence else None
        )
        # A completed research run already proves that its exact immutable
        # dataset was computable.  For personal paper trading, strict PIT,
        # benchmark and promotion evidence are credibility warnings rather
        # than reuse blockers.  Structural identity, hashes and the declared
        # cache-only/source boundary remain technical invariants.
        if (
            trust is None
            or trust.get("schema_version") != "tushare-research-trust/v1"
            or not isinstance(limitations, list)
            or not limitations
            or claims is None
            or claims.get("eligible_for_live_trading") is not False
            or evidence is None
            or not _sha256(report_digest)
            or universe is None
            or dataset is None
            or not _sha256(dataset.get("digest"))
            or quality is None
            or not quality.is_clean
            or quality.payload.get("source", {}).get("provider") != "tushare"
            or runtime is None
            or runtime.get("verified") is not False
            or runtime.get("production_eligible") is not False
            or runtime.get("live_trading_eligible") is not False
            or runtime.get("network_accessed") is not False
        ):
            return ExperimentEligibility(
                False,
                "tushare_conditional_research_evidence_invalid",
            )
        warnings = {
            str(item)
            for item in [
                *limitations,
                *list(trust.get("warnings") or []),
                *list(trust.get("blockers") or []),
                *list(manifest.get("research_risk_warnings") or []),
            ]
            if str(item).strip()
        }
        if trust.get("eligible") is not True:
            warnings.add("tushare_research_trust_incomplete")
        if claims.get("eligible_for_paper_trading") is not True:
            warnings.add("paper_trading_claim_not_certified")
        if universe.get("point_in_time") is not True:
            warnings.add("point_in_time_universe_not_proven")
        if timeline is None or not _sha256(timeline.get("timeline_hash")):
            warnings.add("point_in_time_timeline_not_bound")
        timeline_binding_digest = (
            research_generation_id
            if _sha256(research_generation_id)
            else report_digest
        )
        if not isinstance(source_batches, list) or not any(
            isinstance(batch, Mapping)
            and batch.get("batch_digest") == timeline_binding_digest
            for batch in (source_batches or [])
        ):
            warnings.add("research_generation_not_bound_to_timeline")
        if (
            benchmark is None
            or benchmark.get("available") is not True
            or not _sha256(benchmark.get("sha256"))
        ):
            warnings.add("benchmark_unavailable")
        if runtime.get("paper_trading_eligible") is not True:
            warnings.add("paper_runtime_not_certified")
        warnings.update(
            {
                "single_source_tushare_research",
                "production_dual_price_ledger_not_certified",
                "manual_research_approval_missing_or_optional",
                "live_trading_not_eligible",
            }
        )
        return ExperimentEligibility(
            True,
            "tushare_research_paper_verified_with_warnings",
            tuple(sorted(warnings)),
        )

    universe = _mapping(manifest.get("universe"))
    timeline = _mapping(universe.get("timeline_identity")) if universe else None
    universe_quality = _mapping(universe.get("quality")) if universe else None
    if (
        universe is None
        or universe.get("point_in_time") is not True
        or timeline is None
        or not _sha256(timeline.get("timeline_hash"))
        or not isinstance(timeline.get("source_batches"), list)
        or not timeline.get("source_batches")
        or universe_quality is None
        or universe_quality.get("is_clean") is not True
    ):
        return ExperimentEligibility(False, "pit_universe_evidence_invalid")

    execution = _mapping(manifest.get("execution"))
    price_binding = (
        _mapping(execution.get("canonical_price_binding"))
        if execution
        else None
    )
    if (
        price_binding is None
        or not isinstance(price_binding.get("binding_id"), str)
        or not 1 <= len(price_binding["binding_id"]) <= 128
        or not _sha256(price_binding.get("binding_digest"))
    ):
        return ExperimentEligibility(False, "pit_price_binding_invalid")

    runtime = _mapping(manifest.get("pit_runtime"))
    if (
        runtime is None
        or runtime.get("schema_version") != "pit-runtime-binding/v1"
        or runtime.get("verified") is not True
        or runtime.get("network_accessed") is not False
        or runtime.get("legacy_or_static_fallback_allowed") is not False
        or runtime.get("timeline_hash") != timeline.get("timeline_hash")
        or runtime.get("canonical_price_binding_id")
        != price_binding.get("binding_id")
        or runtime.get("canonical_price_binding_digest")
        != price_binding.get("binding_digest")
    ):
        return ExperimentEligibility(False, "pit_runtime_binding_invalid")

    dataset = _mapping(manifest.get("dataset"))
    if dataset is None or not _sha256(dataset.get("digest")):
        return ExperimentEligibility(False, "pit_dataset_identity_invalid")
    try:
        quality = MarketDataQualitySnapshot.from_dict(
            manifest.get("market_data_quality", {})
        )
    except MarketDataQualityError:
        return ExperimentEligibility(False, "market_data_quality_invalid")
    if not quality.is_clean:
        return ExperimentEligibility(False, "market_data_quality_not_clean")

    warnings = {
        str(item)
        for item in manifest.get("research_risk_warnings", [])
        if isinstance(item, str)
    }
    if warnings & {NON_POINT_IN_TIME, SURVIVORSHIP_BIAS, "current_constituents"}:
        return ExperimentEligibility(False, "point_in_time_risk_warning")
    benchmark = _mapping(manifest.get("benchmark"))
    if (
        benchmark is None
        or benchmark.get("available") is not True
        or not _sha256(benchmark.get("sha256"))
    ):
        return ExperimentEligibility(False, "pit_benchmark_evidence_invalid")
    return ExperimentEligibility(True, "pit_manifest_verified")


async def load_experiment_eligibility(
    connection: Any,
    *,
    experiment_id: int,
    strategy_id: str,
) -> ExperimentEligibility:
    table_cursor = await connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='research_run_manifests'
        """
    )
    if await table_cursor.fetchone() is None:
        return ExperimentEligibility(False, "legacy_manifest_missing")
    cursor = await connection.execute(
        """
        SELECT schema_version, manifest_json, manifest_hash
        FROM research_run_manifests
        WHERE experiment_id=?
        """,
        (experiment_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return ExperimentEligibility(False, "legacy_manifest_missing")
    return assess_experiment_eligibility(
        experiment_id=experiment_id,
        strategy_id=strategy_id,
        schema_version=row["schema_version"],
        manifest_json=row["manifest_json"],
        manifest_hash=row["manifest_hash"],
    )
