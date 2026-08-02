from __future__ import annotations

from copy import deepcopy

from backend.data.provider_artifacts import canonical_sha256
from backend.data.sources.tushare_pit_backfill import FOUR_INDEX_CODES
from backend.services.tushare_research_trust import (
    KNOWN_LIMITATIONS,
    REQUIRED_MONTHS,
    assess_tushare_research_trust,
)


def _complete_report() -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": "tushare-pit-candidate-backfill/v4",
        "observed_at": "2026-08-02T08:00:00Z",
        "classification": "quarantine",
        "plan": {
            "first_month": "2016-01",
            "last_month": "2026-06",
            "four_index_codes": list(FOUR_INDEX_CODES),
        },
        "progress": {
            "complete": True,
            "all_sessions_reconciled": True,
        },
        "index_month_coverage": [
            {
                "index_code": index_code,
                "month": month,
                "status": "complete_monthly_snapshot_candidate",
                "manifest_sha256": canonical_sha256(
                    {"index_code": index_code, "month": month}
                ),
            }
            for index_code in FOUR_INDEX_CODES
            for month in REQUIRED_MONTHS
        ],
        "incomplete_index_months": [],
        "failures": [],
        "candidate_collection_valid": True,
        "checkpoint": {"sha256": "c" * 64},
        "production_pit_ready": False,
        "runtime_data_changed": False,
        "promotion": {"eligible": False},
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _assess(report: dict[str, object], **overrides: str) -> dict[str, object]:
    return assess_tushare_research_trust(
        report=report,
        report_object_sha256="d" * 64,
        required_start=overrides.get("required_start", "2016-01-01"),
        required_end=overrides.get("required_end", "2026-06-30"),
        purpose=overrides.get("purpose", "return_research"),
    )


def test_complete_504_month_contract_is_research_only() -> None:
    result = _assess(_complete_report())

    assert result["eligible"] is True
    assert result["blockers"] == []
    assert result["declared_coverage"]["required_index_month_count"] == 504
    assert result["claims"] == {
        "eligible_for_conditional_research": True,
        "eligible_for_rigorous_production_pit_research": False,
        "eligible_for_real_tuning": True,
        "eligible_for_promotion": False,
        "eligible_for_paper_trading": True,
        "eligible_for_live_trading": False,
        "dual_price_ledger_certified": False,
    }
    assert set(KNOWN_LIMITATIONS) <= set(result["known_limitations"])


def test_missing_month_or_changed_digest_fails_closed() -> None:
    report = _complete_report()
    coverage = report["index_month_coverage"]
    assert isinstance(coverage, list)
    coverage.pop()

    result = _assess(report)

    assert result["eligible"] is False
    assert set(result["blockers"]) >= {
        "report_content_digest_valid",
        "four_index_monthly_manifest_coverage_complete",
    }


def test_july_2026_remains_forbidden_but_tuning_is_warning_bound_research() -> None:
    july = _assess(_complete_report(), required_end="2026-07-31")
    tuning = _assess(_complete_report(), purpose="real_tuning")

    assert july["eligible"] is False
    assert "window_within_declared_coverage" in july["blockers"]
    assert tuning["eligible"] is True
    assert tuning["claims"]["eligible_for_real_tuning"] is True


def test_incomplete_session_collection_is_high_warning_not_hidden_blocker() -> None:
    report = _complete_report()
    report["candidate_collection_valid"] = False
    progress = report["progress"]
    assert isinstance(progress, dict)
    progress["complete"] = False
    progress["all_sessions_reconciled"] = False
    report["failures"] = [{"diagnostic": {"code": "ambiguous_session"}}]
    report["report_sha256"] = canonical_sha256(
        {key: value for key, value in report.items() if key != "report_sha256"}
    )

    result = _assess(report)

    assert result["eligible"] is True
    assert result["warning_severity"] == "high"
    assert set(result["warnings"]) >= {
        "collection_complete",
        "candidate_collection_valid",
        "all_sessions_reconciled",
        "no_candidate_failures",
    }


def test_candidate_cannot_claim_production_or_runtime_change() -> None:
    report = deepcopy(_complete_report())
    report["production_pit_ready"] = True
    report["runtime_data_changed"] = True
    report["report_sha256"] = canonical_sha256(
        {key: value for key, value in report.items() if key != "report_sha256"}
    )

    result = _assess(report)

    assert result["eligible"] is False
    assert set(result["blockers"]) >= {
        "production_claim_remains_false",
        "runtime_was_not_changed",
    }
