from __future__ import annotations

from backend.services.experiment_readiness import (
    READINESS_SCHEMA_VERSION,
    build_experiment_readiness_contract,
)


def test_incomplete_production_evidence_fails_closed_with_actionable_blockers() -> None:
    result = build_experiment_readiness_contract(
        price_purpose="return_research",
        market_report={
            "ready": True,
            "pool_id": "csi300",
            "issues": [],
            "universe_point_in_time": False,
            "canonical_runtime_price_bound": False,
            "authoritative_trading_calendar_bound": False,
            "point_in_time_benchmark_bound": False,
            "ready_for_unbiased_return_research": False,
            "point_in_time": {
                "universe": {"reason": "effective_dated_history_missing"}
            },
            "price_ledger": {"reason": "ledger_unavailable"},
        },
        benchmark_report={"ready": True, "issues": []},
    )

    assert result["schema_version"] == READINESS_SCHEMA_VERSION
    assert result["ready"] is False
    assert result["network_accessed"] is False
    assert result["writes_performed"] is False
    assert result["evidence"]["evidence_class"] == "incomplete"
    assert result["evidence"]["eligible_for_formal_experiment"] is False
    assert result["evidence"]["eligible_for_live_trading"] is False
    assert {item["code"] for item in result["blockers"]} >= {
        "effective_dated_history_missing",
        "ledger_unavailable",
        "pit_trading_calendar_binding_missing",
        "pit_benchmark_binding_missing",
        "purpose_evidence_incomplete:ready_for_unbiased_return_research",
    }


def test_verified_isolated_fixture_can_pass_without_becoming_production_evidence() -> None:
    result = build_experiment_readiness_contract(
        price_purpose="real_tuning",
        market_report={
            "ready": True,
            "pool_id": "csi300",
            "issues": [],
            "universe_point_in_time": True,
            "canonical_runtime_price_bound": True,
            "authoritative_trading_calendar_bound": True,
            "point_in_time_benchmark_bound": True,
            "ready_for_real_tuning": True,
            "point_in_time": {
                "universe": {"timeline": {"timeline_hash": "a" * 64}}
            },
            "price_ledger": {
                "binding_id": "plr_fixture",
                "binding_digest": "b" * 64,
            },
            "qa_runtime_attestation": {
                "attestation_sha256": "c" * 64,
                "non_production": True,
                "production_eligible": False,
                "binding_id": "plr_fixture",
                "binding_digest": "b" * 64,
            },
        },
        benchmark_report={"ready": True, "issues": []},
    )

    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["evidence"] == {
        "pool_id": "csi300",
        "timeline_hash": "a" * 64,
        "canonical_price_binding_id": "plr_fixture",
        "canonical_price_binding_digest": "b" * 64,
        "isolated_test_fixture": True,
        "evidence_class": "isolated_test_fixture",
        "trust_tier": "governed_production_pit",
        "known_limitations": [],
        "eligible_for_research_experiment": True,
        "eligible_for_formal_experiment": True,
        "eligible_for_real_tuning": True,
        "eligible_for_paper_trading": True,
        "eligible_for_live_trading": False,
        "qa_attestation_sha256": "c" * 64,
    }


def test_tushare_profile_allows_conditional_research_but_retains_production_blockers() -> None:
    result = build_experiment_readiness_contract(
        price_purpose="return_research",
        market_report={
            "ready": True,
            "pool_id": "csi300",
            "issues": [],
            "universe_point_in_time": False,
            "canonical_runtime_price_bound": False,
            "authoritative_trading_calendar_bound": False,
            "point_in_time_benchmark_bound": False,
            "ready_for_unbiased_return_research": False,
            "point_in_time": {"universe": {"reason": "effective_dated_history_missing"}},
            "price_ledger": {"reason": "ledger_unavailable"},
        },
        benchmark_report={"ready": True, "issues": []},
        research_trust={
            "profile": "tushare_research_trusted",
            "trust_tier": "conditional_personal_research",
            "eligible": True,
            "blockers": [],
            "known_limitations": ["historical_available_at_not_proven"],
            "claims": {
                "eligible_for_real_tuning": True,
                "eligible_for_paper_trading": True,
            },
        },
    )

    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["evidence"]["evidence_class"] == "tushare_research_trusted"
    assert result["evidence"]["eligible_for_research_experiment"] is True
    assert result["evidence"]["eligible_for_formal_experiment"] is False
    assert result["evidence"]["eligible_for_real_tuning"] is True
    assert result["evidence"]["eligible_for_paper_trading"] is True
    assert {item["code"] for item in result["production_blockers"]} >= {
        "effective_dated_history_missing",
        "ledger_unavailable",
        "pit_trading_calendar_binding_missing",
    }
    assert result["credibility_level"] == "high_warning"
    assert {item["code"] for item in result["warnings"]} >= {
        "historical_available_at_not_proven",
        "ledger_unavailable",
        "single_source_tushare_research",
        "manual_research_approval_not_required_for_paper",
    }


def test_tushare_missing_benchmark_is_warning_but_missing_prices_block() -> None:
    trust = {
        "profile": "tushare_research_trusted",
        "trust_tier": "conditional_personal_research",
        "eligible": True,
        "blockers": [],
        "known_limitations": ["historical_revision_not_proven"],
        "claims": {
            "eligible_for_real_tuning": True,
            "eligible_for_paper_trading": True,
        },
    }
    base_market = {
        "ready": True,
        "pool_id": "csi300",
        "issues": [],
        "universe_point_in_time": False,
        "canonical_runtime_price_bound": False,
        "authoritative_trading_calendar_bound": False,
        "point_in_time_benchmark_bound": False,
        "ready_for_unbiased_return_research": False,
        "point_in_time": {"universe": {"reason": "monthly_timeline_only"}},
        "price_ledger": {"reason": "dual_ledger_not_certified"},
    }

    warning_only = build_experiment_readiness_contract(
        price_purpose="return_research",
        market_report=base_market,
        benchmark_report={"ready": False, "issues": ["benchmark_cache_missing"]},
        research_trust=trust,
    )
    no_prices = build_experiment_readiness_contract(
        price_purpose="return_research",
        market_report={
            **base_market,
            "ready": False,
            "issues": ["daily_cache_missing"],
        },
        benchmark_report={"ready": False, "issues": ["benchmark_cache_missing"]},
        research_trust=trust,
    )

    assert warning_only["ready"] is True
    assert warning_only["technical_blockers"] == []
    assert "benchmark_cache_missing" in {
        item["code"] for item in warning_only["warnings"]
    }
    assert no_prices["ready"] is False
    assert "daily_cache_missing" in {
        item["code"] for item in no_prices["technical_blockers"]
    }
