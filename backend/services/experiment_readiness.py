"""Side-effect-free readiness contract for formal experiment submission.

The cache inspector contains detailed engineering diagnostics.  This module
turns those diagnostics into a small, stable contract shared by the API and
browser: every formal gate is explicit, every failure has a machine code, and
an isolated QA attestation is visibly non-production.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal


READINESS_SCHEMA_VERSION = "experiment-readiness/v4"
PricePurpose = Literal[
    "compatibility_research",
    "return_research",
    "real_tuning",
    "execution_simulation",
]

_PURPOSE_GATE = {
    "compatibility_research": "ready_for_unbiased_return_research",
    "return_research": "ready_for_unbiased_return_research",
    "real_tuning": "ready_for_real_tuning",
    "execution_simulation": "ready_for_execution_simulation",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _reason(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else fallback


def build_experiment_readiness_contract(
    *,
    price_purpose: PricePurpose,
    market_report: Mapping[str, Any],
    benchmark_report: Mapping[str, Any],
    research_trust: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, sanitized readiness result without any I/O."""

    point_in_time = _mapping(market_report.get("point_in_time"))
    universe = _mapping(point_in_time.get("universe"))
    price_ledger = _mapping(market_report.get("price_ledger"))
    qa = _mapping(market_report.get("qa_runtime_attestation"))
    timeline = _mapping(universe.get("timeline"))
    gate_field = _PURPOSE_GATE[price_purpose]

    strict_checks = [
        {
            "code": "market_cache_covered",
            "passed": market_report.get("ready") is True,
            "source": "market_data",
        },
        {
            "code": "benchmark_cache_covered",
            "passed": benchmark_report.get("ready") is True,
            "source": "benchmark",
        },
        {
            "code": "point_in_time_universe_bound",
            "passed": market_report.get("universe_point_in_time") is True,
            "source": "market_data.point_in_time.universe",
        },
        {
            "code": "canonical_dual_price_runtime_bound",
            "passed": market_report.get("canonical_runtime_price_bound") is True,
            "source": "market_data.price_ledger",
        },
        {
            "code": "authoritative_trading_calendar_bound",
            "passed": market_report.get("authoritative_trading_calendar_bound") is True,
            "source": "market_data",
        },
        {
            "code": "point_in_time_benchmark_bound",
            "passed": market_report.get("point_in_time_benchmark_bound") is True,
            "source": "market_data",
        },
        {
            "code": f"purpose_gate:{gate_field}",
            "passed": market_report.get(gate_field) is True,
            "source": "market_data",
        },
    ]

    production_blockers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(code: Any, source: str) -> None:
        normalized = _reason(code, "readiness_evidence_incomplete")
        identity = (normalized, source)
        if identity not in seen:
            seen.add(identity)
            production_blockers.append({"code": normalized, "source": source})

    if market_report.get("ready") is not True:
        issues = market_report.get("issues")
        if isinstance(issues, list) and issues:
            for issue in issues:
                add(issue, "market_data")
        else:
            add("market_data_not_ready", "market_data")
    if benchmark_report.get("ready") is not True:
        issues = benchmark_report.get("issues")
        if isinstance(issues, list) and issues:
            for issue in issues:
                add(issue, "benchmark")
        else:
            add("benchmark_data_not_ready", "benchmark")
    if market_report.get("universe_point_in_time") is not True:
        add(
            universe.get("reason") or "point_in_time_universe_missing",
            "market_data.point_in_time.universe",
        )
    if market_report.get("canonical_runtime_price_bound") is not True:
        add(
            price_ledger.get("reason") or "canonical_runtime_binding_missing",
            "market_data.price_ledger",
        )
    if market_report.get("authoritative_trading_calendar_bound") is not True:
        add("pit_trading_calendar_binding_missing", "market_data")
    if market_report.get("point_in_time_benchmark_bound") is not True:
        add("pit_benchmark_binding_missing", "market_data")
    if market_report.get(gate_field) is not True:
        add(f"purpose_evidence_incomplete:{gate_field}", "market_data")

    fixture_verified = bool(
        qa.get("non_production") is True
        and qa.get("production_eligible") is False
        and qa.get("attestation_sha256")
    )
    trust = _mapping(research_trust)
    tushare_research = trust.get("profile") == "tushare_research_trusted"
    if tushare_research:
        checks = [
            strict_checks[0],
            {
                "code": "tushare_research_trust_contract",
                "passed": trust.get("eligible") is True,
                "source": "tushare_candidate_evidence",
            },
        ]
        blockers = [
            {"code": str(code), "source": "tushare_candidate_evidence"}
            for code in trust.get("blockers", [])
            if str(code).strip()
        ]
        if market_report.get("ready") is not True:
            blockers.extend(
                item
                for item in production_blockers
                if item["source"] == "market_data"
            )
        ready = all(bool(check["passed"]) for check in checks)
        evidence_class = "tushare_research_trusted" if ready else "incomplete"
    else:
        checks = strict_checks
        blockers = production_blockers
        ready = all(bool(check["passed"]) for check in checks)
        evidence_class = (
            "isolated_test_fixture"
            if fixture_verified
            else "governed_runtime"
            if ready
            else "incomplete"
        )
    warnings: list[dict[str, str]] = []
    if tushare_research:
        warning_seen: set[tuple[str, str]] = set()

        def warn(code: Any, source: str) -> None:
            normalized = _reason(code, "research_data_warning")
            identity = (normalized, source)
            if identity not in warning_seen:
                warning_seen.add(identity)
                warnings.append({"code": normalized, "source": source})

        for item in production_blockers:
            if item not in blockers:
                warn(item["code"], item["source"])
        for code in trust.get("warnings", []):
            warn(code, "tushare_candidate_evidence")
        for code in trust.get("known_limitations", []):
            warn(code, "tushare_candidate_evidence")
        warn("single_source_tushare_research", "market_data")
        warn("manual_research_approval_not_required_for_paper", "research_workflow")
        warn("live_trading_not_eligible", "live_trading")

    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "ready": ready,
        "requested_purpose": price_purpose,
        "effective_gate": gate_field,
        "network_accessed": False,
        "writes_performed": False,
        "legacy_or_static_fallback_allowed": False,
        "checks": checks,
        "blockers": blockers,
        "technical_blockers": blockers,
        "warnings": warnings,
        "credibility_level": (
            "high_warning" if ready and warnings else "verified" if ready else "not_runnable"
        ),
        "production_blockers": production_blockers,
        "research_trust": dict(trust) if tushare_research else None,
        "evidence": {
            "pool_id": market_report.get("pool_id"),
            "timeline_hash": timeline.get("timeline_hash"),
            "canonical_price_binding_id": (
                price_ledger.get("binding_id") or qa.get("binding_id")
            ),
            "canonical_price_binding_digest": (
                price_ledger.get("binding_digest") or qa.get("binding_digest")
            ),
            "isolated_test_fixture": fixture_verified,
            "evidence_class": evidence_class,
            "trust_tier": (
                trust.get("trust_tier") if tushare_research else "governed_production_pit"
            ),
            "known_limitations": (
                list(trust.get("known_limitations", [])) if tushare_research else []
            ),
            "eligible_for_research_experiment": ready,
            "eligible_for_formal_experiment": ready and not tushare_research,
            "eligible_for_real_tuning": bool(
                ready
                and (
                    not tushare_research
                    or _mapping(trust.get("claims")).get("eligible_for_real_tuning")
                    is True
                )
            ),
            "eligible_for_paper_trading": bool(
                ready
                and (
                    not tushare_research
                    or _mapping(trust.get("claims")).get(
                        "eligible_for_paper_trading"
                    )
                    is True
                )
            ),
            # This endpoint is not a broker/live deployment certificate.
            "eligible_for_live_trading": False,
            "qa_attestation_sha256": (
                qa.get("attestation_sha256") if fixture_verified else None
            ),
        },
    }
