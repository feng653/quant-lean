from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest

from backend.data.cache import DataCache
from backend.data.source_validation import (
    SourceEvidenceError,
    build_cache_source_provenance,
    build_daily_fetch_evidence,
    compare_independent_daily_frames,
    validate_adjustment_factor_validation,
)


def _frame(multiplier: float = 1.0) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-02", periods=35, name="date")
    close = pd.Series(
        [10.0 * multiplier * 1.01**position for position in range(len(index))],
        index=index,
    )
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    frame = pd.DataFrame(index=index, columns=columns, dtype=float)
    frame[("000001", "open")] = close * 0.99
    frame[("000001", "high")] = close * 1.01
    frame[("000001", "low")] = close * 0.98
    frame[("000001", "close")] = close
    frame[("000001", "volume")] = 1000.0
    return frame


def _cross_validation(adjustment: str) -> dict:
    return compare_independent_daily_frames(
        _frame(),
        _frame(100.0),
        primary_provider="baostock:official",
        reference_provider="akshare:sina",
        requested_codes=["000001"],
        adjustment=adjustment,
        min_overlap_returns=20,
    )


def _adjustment_validation() -> dict:
    return {
        "schema_version": "adjustment-factor-validation/v1",
        "method": "baostock_raw_preclose_hfq_recurrence",
        "input_adjustment": "raw",
        "output_adjustment": "hfq",
        "recurrence_validated": True,
        "factors_finite_positive": True,
        "corporate_action_jump_count": 1,
        "corporate_action_examples": [
            {
                "code": "000001",
                "date": "2024-01-15",
                "factor_ratio": 1.25,
            }
        ],
        "evidence_truncated": False,
    }


def _daily_evidence(
    *,
    cross_adjustment: str = "raw",
    adjustment_validation: dict | None = None,
) -> dict:
    frame = _frame()
    return build_daily_fetch_evidence(
        frame,
        requested_codes=["000001"],
        start="2024-01-02",
        end="2024-02-19",
        provider="baostock:official",
        endpoint="baostock:query_history_k_data_plus",
        adjustment="hfq",
        evidence_level="public_aggregator",
        cross_validation=_cross_validation(cross_adjustment),
        adjustment_validation=adjustment_validation,
    )


def test_hfq_research_provenance_requires_raw_cross_and_factor_validation() -> None:
    evidence = _daily_evidence(
        adjustment_validation=_adjustment_validation(),
    )
    provenance = build_cache_source_provenance(_frame(), [evidence])

    assert evidence["raw_cross_validated"] is True
    assert evidence["adjusted_factor_validated"] is True
    assert provenance["all_batches_raw_cross_validated"] is True
    assert provenance["all_batches_adjusted_factor_validated"] is True
    assert (
        DataCache._source_trust(provenance)
        == "public_cross_validated_research_only"
    )


def test_hfq_cross_source_agreement_does_not_substitute_for_raw_validation() -> None:
    evidence = _daily_evidence(
        cross_adjustment="hfq",
        adjustment_validation=_adjustment_validation(),
    )
    provenance = build_cache_source_provenance(_frame(), [evidence])

    assert provenance["all_batches_cross_validated"] is True
    assert provenance["all_batches_raw_cross_validated"] is False
    assert provenance["all_batches_adjusted_factor_validated"] is True
    assert (
        DataCache._source_trust(provenance)
        == "public_single_source_research_only"
    )


def test_raw_cross_validation_without_factor_evidence_is_not_research_grade() -> None:
    evidence = _daily_evidence()
    provenance = build_cache_source_provenance(_frame(), [evidence])

    assert provenance["all_batches_raw_cross_validated"] is True
    assert provenance["all_batches_adjusted_factor_validated"] is False
    assert (
        DataCache._source_trust(provenance)
        == "public_single_source_research_only"
    )


def test_adjustment_validation_rejects_unbounded_or_nonpositive_evidence() -> None:
    invalid = deepcopy(_adjustment_validation())
    invalid["corporate_action_jump_count"] = 0
    with pytest.raises(SourceEvidenceError, match="exceed"):
        validate_adjustment_factor_validation(invalid)

    invalid = deepcopy(_adjustment_validation())
    invalid["corporate_action_examples"][0]["factor_ratio"] = 0.0
    with pytest.raises(SourceEvidenceError, match="ratio"):
        validate_adjustment_factor_validation(invalid)


def test_informational_hfq_comparison_is_validated_but_not_a_trust_gate() -> None:
    adjustment = _adjustment_validation()
    informational = _cross_validation("hfq")
    assert informational["summary"]["acceptable"] is True
    adjustment["informational_hfq_cross_source"] = informational

    validated = validate_adjustment_factor_validation(adjustment)

    assert validated["informational_hfq_cross_source"]["adjustment"] == "hfq"
