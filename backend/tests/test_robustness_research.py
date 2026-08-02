from __future__ import annotations

import json
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from backend.research.robustness import (
    block_bootstrap_performance,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
    multiple_testing_correction,
    parameter_stability_region,
    probabilistic_sharpe_ratio,
)


def _assert_json_safe(result: dict) -> None:
    assert {"method", "sample_count", "seed", "limitations"} <= set(result)
    assert isinstance(result["limitations"], list)
    json.dumps(result, allow_nan=False, sort_keys=True)


def test_block_bootstrap_is_reproducible_uses_blocks_and_matches_point_formula() -> None:
    returns = pd.Series(
        np.tile([0.01, 0.004, -0.003, 0.002], 63),
        index=pd.date_range("2024-01-01", periods=252),
    )
    original = returns.copy(deep=True)
    first = block_bootstrap_performance(
        returns,
        n_bootstrap=300,
        block_size=8,
        method="moving",
        seed=17,
    )
    second = block_bootstrap_performance(
        returns,
        n_bootstrap=300,
        block_size=8,
        method="moving",
        seed=17,
    )
    stationary = block_bootstrap_performance(
        returns,
        n_bootstrap=300,
        block_size=8,
        method="stationary",
        seed=17,
    )

    pdt.assert_series_equal(returns, original)
    assert first == second
    assert first["status"] == "ok"
    expected_annual = float(np.prod(1 + returns.to_numpy()) - 1)
    assert first["point_estimate"]["annualized_return"] == pytest.approx(
        expected_annual
    )
    assert first["assumptions"]["method"] == "moving"
    assert first["assumptions"]["block_size"] == 8
    assert stationary["status"] == "ok"
    assert stationary["confidence_intervals"] != first["confidence_intervals"]
    for metric in ("annualized_return", "sharpe_ratio", "max_drawdown"):
        interval = first["confidence_intervals"][metric]
        assert interval["lower"] <= interval["upper"]
        assert interval["valid_bootstrap_samples"] >= 240
    _assert_json_safe(first)
    _assert_json_safe(stationary)


@pytest.mark.parametrize(
    ("returns", "expected_status"),
    [
        ([0.01] * 30, "degenerate_input"),
        ([0.01, np.nan] + [0.0] * 28, "invalid_input"),
        ([0.01, np.inf] + [0.0] * 28, "invalid_input"),
        ([0.01, -0.01] * 5, "insufficient_samples"),
    ],
)
def test_bootstrap_fails_closed_for_unreliable_inputs(
    returns,
    expected_status,
) -> None:
    result = block_bootstrap_performance(
        returns,
        n_bootstrap=100,
        min_samples=20,
    )
    assert result["status"] == expected_status
    assert "point_estimate" not in result
    _assert_json_safe(result)


def test_probabilistic_sharpe_known_zero_benchmark_and_deflation() -> None:
    symmetric = np.tile([-0.01, 0.01], 60)
    psr_zero = probabilistic_sharpe_ratio(
        symmetric,
        benchmark_sharpe=0.0,
        periods_per_year=252,
    )
    assert psr_zero["status"] == "ok"
    assert psr_zero["observed_sharpe"] == pytest.approx(0.0, abs=1e-14)
    assert psr_zero["probabilistic_sharpe_ratio"] == pytest.approx(0.5)
    assert psr_zero["diagnostics"]["skewness"] == pytest.approx(0.0)
    assert psr_zero["diagnostics"]["kurtosis"] == pytest.approx(1.0)

    positive = np.tile([-0.006, 0.012, 0.004, 0.002], 63)
    ordinary = probabilistic_sharpe_ratio(positive)
    trial_sharpes = [-0.2, 0.0, 0.2, 0.4, 0.6]
    deflated = deflated_sharpe_ratio(positive, trial_sharpes)
    count = len(trial_sharpes)
    expected_maximum = np.mean(trial_sharpes) + np.std(
        trial_sharpes, ddof=1
    ) * (
        (1 - 0.5772156649015329)
        * NormalDist().inv_cdf(1 - 1 / count)
        + 0.5772156649015329
        * NormalDist().inv_cdf(1 - 1 / (count * math.e))
    )

    assert ordinary["status"] == "ok"
    assert deflated["status"] == "ok"
    assert deflated["expected_max_trial_sharpe"] == pytest.approx(expected_maximum)
    assert (
        deflated["deflated_sharpe_ratio"]
        < ordinary["probabilistic_sharpe_ratio"]
    )
    _assert_json_safe(psr_zero)
    _assert_json_safe(deflated)


def test_deflated_sharpe_rejects_constant_trial_distribution() -> None:
    returns = np.tile([-0.005, 0.01, 0.002], 40)
    result = deflated_sharpe_ratio(
        returns,
        [0.3, 0.3, 0.3],
    )
    assert result["status"] == "degenerate_input"
    assert "deflated_sharpe_ratio" not in result


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("bonferroni", {"a": 0.03, "b": 0.12, "c": 0.09}),
        ("holm", {"a": 0.03, "b": 0.06, "c": 0.06}),
        ("bh", {"a": 0.03, "b": 0.04, "c": 0.04}),
    ],
)
def test_multiple_testing_known_adjusted_pvalues(method, expected) -> None:
    result = multiple_testing_correction(
        {"b": 0.04, "a": 0.01, "c": 0.03},
        method=method,
        alpha=0.05,
    )
    actual = {
        item["name"]: item["adjusted_p_value"]
        for item in result["hypotheses"]
    }
    assert result["status"] == "ok"
    assert actual == pytest.approx(expected)
    assert [item["name"] for item in result["hypotheses"]] == ["a", "b", "c"]
    _assert_json_safe(result)


def test_multiple_testing_fails_closed_on_any_nonfinite_pvalue() -> None:
    result = multiple_testing_correction(
        {"valid": 0.01, "invalid": np.nan},
        method="bh",
    )
    assert result["status"] == "invalid_input"
    assert "hypotheses" not in result
    _assert_json_safe(result)


def test_cscv_pbo_is_capped_reproducible_disjoint_and_input_immutable() -> None:
    rng = np.random.default_rng(4)
    noise = rng.normal(0.0, 0.002, (4, 80))
    values = noise.copy()
    values[0, :40] += 0.008
    values[0, 40:] -= 0.008
    values[1, :40] -= 0.008
    values[1, 40:] += 0.008
    values[2] += 0.001
    frame = pd.DataFrame(
        values,
        index=["regime_a", "regime_b", "stable", "noise"],
    )
    original = frame.copy(deep=True)

    first = cscv_probability_of_backtest_overfitting(
        frame,
        n_slices=8,
        max_combinations=7,
        seed=9,
    )
    second = cscv_probability_of_backtest_overfitting(
        frame,
        n_slices=8,
        max_combinations=7,
        seed=9,
    )

    pdt.assert_frame_equal(frame, original)
    assert first == second
    assert first["status"] == "ok"
    assert first["total_possible_combinations"] == math.comb(8, 4)
    assert first["valid_combinations"] == 7
    assert first["deterministically_sampled"] is True
    assert 0 <= first["probability_of_backtest_overfitting"] <= 1
    for split in first["splits"]:
        assert split["overlap_count"] == 0
        assert set(split["in_sample_slices"]).isdisjoint(
            split["out_of_sample_slices"]
        )
    _assert_json_safe(first)


def test_cscv_fails_closed_for_constant_or_nonfinite_trials() -> None:
    constant = np.vstack([np.ones(40), np.arange(40)])
    nonfinite = np.vstack([np.arange(40), np.arange(40)]).astype(float)
    nonfinite[0, 2] = np.nan

    assert cscv_probability_of_backtest_overfitting(
        constant,
        n_slices=4,
    )["status"] == "degenerate_input"
    assert cscv_probability_of_backtest_overfitting(
        nonfinite,
        n_slices=4,
    )["status"] == "invalid_input"
    assert cscv_probability_of_backtest_overfitting(
        np.vstack([np.arange(40), np.arange(40)[::-1]]),
        n_slices=4,
        max_combinations=10_001,
    )["status"] == "invalid_input"


def test_parameter_plateau_distinguishes_stable_region_from_single_peak() -> None:
    rows = []
    for x in range(3):
        for y in range(3):
            rows.append(
                {
                    "x": x,
                    "y": y,
                    "plateau": 1.0 - 0.01 * (abs(x - 1) + abs(y - 1)),
                    "spike": 1.0 if (x, y) == (1, 1) else 0.0,
                }
            )
    frame = pd.DataFrame(rows)
    original = frame.copy(deep=True)
    plateau = parameter_stability_region(
        frame,
        ["x", "y"],
        metric_column="plateau",
        min_neighbors=4,
    )
    spike = parameter_stability_region(
        frame,
        ["x", "y"],
        metric_column="spike",
        min_neighbors=4,
    )

    pdt.assert_frame_equal(frame, original)
    assert plateau["best_parameters"] == {"x": 1.0, "y": 1.0}
    assert plateau["neighbor_count"] == 8
    assert plateau["is_stable"] is True
    assert spike["best_parameters"] == {"x": 1.0, "y": 1.0}
    assert spike["is_stable"] is False
    assert spike["plateau_score"] < plateau["plateau_score"]
    assert any("单点峰值" in warning for warning in spike["warnings"])
    _assert_json_safe(plateau)
    _assert_json_safe(spike)


def test_parameter_stability_warns_boundary_and_never_calls_isolated_peak_stable() -> None:
    boundary = parameter_stability_region(
        pd.DataFrame({"window": [10, 20, 30], "score": [0.1, 0.2, 0.4]}),
        ["window"],
    )
    isolated = parameter_stability_region(
        pd.DataFrame(
            {
                "x": [0, 1, 2, 2],
                "y": [0, 2, 1, 2],
                "score": [1.0, 0.1, 0.1, 0.1],
            }
        ),
        ["x", "y"],
    )

    assert boundary["boundary_optimum"] is True
    assert boundary["boundary_parameters"] == ["window"]
    assert any("边界" in warning for warning in boundary["warnings"])
    assert isolated["neighbors"] == []
    assert isolated["plateau_score"] == 0.0
    assert isolated["is_stable"] is False
    assert any("孤立最优点" in warning for warning in isolated["warnings"])


def test_parameter_stability_fails_closed_on_nan_or_duplicate_coordinates() -> None:
    nan_result = parameter_stability_region(
        pd.DataFrame({"x": [1.0, 2.0], "score": [0.1, np.nan]}),
        ["x"],
    )
    duplicate = parameter_stability_region(
        pd.DataFrame({"x": [1.0, 1.0], "score": [0.1, 0.2]}),
        ["x"],
    )
    constant = parameter_stability_region(
        pd.DataFrame({"x": [1.0, 2.0], "score": [0.1, 0.1]}),
        ["x"],
    )
    assert nan_result["status"] == "invalid_input"
    assert duplicate["status"] == "invalid_input"
    assert constant["status"] == "degenerate_input"
    _assert_json_safe(nan_result)
    _assert_json_safe(duplicate)
    _assert_json_safe(constant)
