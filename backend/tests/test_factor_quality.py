from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from backend.research.factor_quality import (
    analyze_implementation_quality,
    analyze_multi_factor_quality,
    orthogonalize_factor_panels,
)
from backend.services.factor_research import FactorResearchBody


def _panels() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2024-01-02", periods=6)
    codes = [f"{index:06d}" for index in range(1, 7)]
    factor = pd.DataFrame(
        [
            np.arange(1, 7, dtype=float) + shift
            for shift in range(len(dates))
        ],
        index=dates,
        columns=codes,
    )
    forward = factor / 100.0
    amount = pd.DataFrame(
        1_000_000.0,
        index=dates,
        columns=codes,
    )
    return factor, forward, amount


def test_implementation_quality_exposes_gross_net_turnover_and_capacity() -> None:
    factor, forward, amount = _panels()

    result = analyze_implementation_quality(
        factor,
        forward,
        amount=amount,
        quantiles=3,
        rebalance_interval=2,
        cost_scenarios_bps=[0.0, 10.0, 25.0],
        default_cost_bps=10.0,
        capacity_participation_rates=[0.01, 0.05],
        min_samples=6,
    )

    assert result["status"] == "available"
    assert result["assumptions"]["default_cost_bps"] == 10.0
    assert result["assumptions"]["cost_convention"] == (
        "one_way_turnover_times_bps"
    )
    assert result["turnover"]["series"][0]["long_short_turnover"] == 1.0
    assert result["gross"]["long_short"]["mean"] > (
        result["net_default"]["long_short"]["mean"]
    )
    zero_cost = result["cost_sensitivity"][0]
    assert zero_cost["long_short"]["mean"] == pytest.approx(
        result["gross"]["long_short"]["mean"]
    )
    assert result["capacity"]["status"] == "available"
    assert result["capacity"]["scenarios"]["0.05"]["mean"] is not None
    assert result["coverage"]["tradable"]["ratio"] == 1.0


def test_capacity_fails_closed_without_complete_amount_evidence() -> None:
    factor, forward, amount = _panels()
    missing = analyze_implementation_quality(
        factor,
        forward,
        amount=None,
        quantiles=3,
        rebalance_interval=1,
        cost_scenarios_bps=[10.0],
        default_cost_bps=10.0,
        capacity_participation_rates=[0.05],
        min_samples=6,
    )
    assert missing["capacity"] == {
        "status": "unavailable",
        "reason": "amount_field_missing",
        "amount_field": None,
        "available_rebalance_dates": 0,
        "total_rebalance_dates": 6,
        "scenarios": {
            "0.05": {"count": 0, "mean": None, "min": None, "max": None}
        },
        "daily": [
            {
                "date": date.strftime("%Y-%m-%d"),
                "status": "unavailable",
                "estimates": {},
            }
            for date in factor.index
        ],
    }

    amount.iloc[0, 0] = np.nan
    partial = analyze_implementation_quality(
        factor,
        forward,
        amount=amount,
        quantiles=3,
        rebalance_interval=1,
        cost_scenarios_bps=[10.0],
        default_cost_bps=10.0,
        capacity_participation_rates=[0.05],
        min_samples=6,
    )
    assert partial["capacity"]["status"] == "partial"
    assert partial["capacity"]["reason"] == "amount_incomplete"
    assert partial["capacity"]["available_rebalance_dates"] < 6


def test_multi_factor_alignment_orthogonalization_and_digest_are_deterministic() -> None:
    base, forward, _amount = _panels()
    inverse = -base
    inverse.iloc[0, 0] = np.nan
    factors = {"z_factor": inverse, "a_factor": base}

    first = analyze_multi_factor_quality(
        factors,
        forward,
        weights={"a_factor": 0.6, "z_factor": 0.4},
        quantiles=3,
        min_samples=5,
        orthogonalize=True,
    )
    second = analyze_multi_factor_quality(
        dict(reversed(list(factors.items()))),
        forward,
        weights={"z_factor": 0.4, "a_factor": 0.6},
        quantiles=3,
        min_samples=5,
        orthogonalize=True,
    )

    assert first == second
    assert first["correlation"]["alignment"] == (
        "same_date_and_code_pairwise_complete"
    )
    assert first["correlation"]["pearson"]["factors"] == [
        "a_factor",
        "z_factor",
    ]
    assert first["correlation"]["spearman"]["matrix"][0][1] == pytest.approx(
        -1.0
    )
    transform = first["orthogonalization"]
    assert transform["order"] == ["a_factor", "z_factor"]
    assert transform["order_rule"] == "lexical_factor_id_ascending"
    assert transform["fit_window"] == "request_start_to_end_only"
    assert len(transform["input_digest"]) == 64
    assert first["combination"]["constraints"]["sum"] == 1.0
    assert first["publication"]["automatic_publish"] is False


def test_orthogonalization_digest_changes_with_window_input() -> None:
    base, _forward, _amount = _panels()
    _, full = orthogonalize_factor_panels(
        {"a": base, "b": base * 2},
        min_samples=5,
    )
    _, shorter = orthogonalize_factor_panels(
        {"a": base.iloc[1:], "b": base.iloc[1:] * 2},
        min_samples=5,
    )

    assert full["input_digest"] != shorter["input_digest"]
    assert full["steps"][1]["regressed_on"] == ["a"]


def test_multi_factor_direct_api_rejects_unbounded_weights() -> None:
    base, forward, _amount = _panels()

    with pytest.raises(ValueError, match="权重和"):
        analyze_multi_factor_quality(
            {"a": base, "b": -base},
            forward,
            weights={"a": 0.8, "b": 0.8},
            quantiles=3,
            min_samples=5,
            orthogonalize=False,
        )


@pytest.mark.parametrize(
    "override",
    [
        {"related_factor_ids": ["momentum_20"]},
        {"related_factor_ids": ["unknown"]},
        {"cost_scenarios_bps": [0.0, 5.0]},
        {"capacity_participation_rates": [0.3]},
        {"cost_scenarios_bps": [0, True, 10]},
        {
            "related_factor_ids": ["short_reversal_5"],
            "combination_weights": {
                "momentum_20": 0.8,
                "short_reversal_5": 0.3,
            },
        },
    ],
)
def test_factor_research_request_rejects_unsafe_quality_parameters(
    override: dict[str, object],
) -> None:
    body = {
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    body.update(copy.deepcopy(override))

    with pytest.raises(ValueError):
        FactorResearchBody(**body)


def test_factor_research_request_accepts_bounded_explicit_combination() -> None:
    body = FactorResearchBody(
        start="2024-01-01",
        end="2024-12-31",
        related_factor_ids=["short_reversal_5"],
        combination_weights={
            "momentum_20": 0.7,
            "short_reversal_5": 0.3,
        },
    )

    assert sum(body.combination_weights.values()) == pytest.approx(1.0)
