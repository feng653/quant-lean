from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from backend.research.capacity import (
    check_portfolio_constraints,
    cost_stress_scenarios,
    estimate_capacity_curve,
)


def _assert_json_safe(result: dict) -> None:
    assert {"method", "sample_count", "seed", "limitations"} <= set(result)
    assert isinstance(result["limitations"], list)
    json.dumps(result, allow_nan=False, sort_keys=True)


def test_cost_stress_is_monotone_echoes_assumptions_and_preserves_inputs() -> None:
    gross = pd.Series(
        np.tile([0.01, -0.004, 0.006, 0.002], 30),
        index=pd.date_range("2025-01-01", periods=120),
    )
    turnover = pd.Series(
        np.tile([0.2, 0.4, 0.3, 0.1], 30),
        index=gross.index,
    )
    gross_original = gross.copy(deep=True)
    turnover_original = turnover.copy(deep=True)
    scenarios = [
        {
            "name": "base",
            "commission_bps": 2,
            "spread_bps": 3,
            "slippage_bps": 5,
        },
        {
            "name": "stress",
            "commission_bps": 8,
            "spread_bps": 12,
            "slippage_bps": 20,
            "impact_bps": 10,
        },
    ]

    result = cost_stress_scenarios(gross, turnover, scenarios)

    pdt.assert_series_equal(gross, gross_original)
    pdt.assert_series_equal(turnover, turnover_original)
    assert result["status"] == "ok"
    base, stress = result["scenarios"]
    assert base["assumptions"]["total_cost_bps"] == 10
    assert stress["assumptions"]["total_cost_bps"] == 50
    assert (
        stress["metrics"]["annualized_return"]
        < base["metrics"]["annualized_return"]
        < result["gross_metrics"]["annualized_return"]
    )
    assert (
        stress["total_cost_return_drag"]
        > base["total_cost_return_drag"]
    )
    _assert_json_safe(result)


@pytest.mark.parametrize(
    ("gross", "expected"),
    [
        ([0.01] * 30, "degenerate_input"),
        ([0.01, np.nan] + [0.0] * 28, "invalid_input"),
        ([0.01, -0.01] * 5, "insufficient_samples"),
    ],
)
def test_cost_stress_fails_closed(gross, expected) -> None:
    result = cost_stress_scenarios(
        gross,
        0.2,
        min_samples=20,
    )
    assert result["status"] == expected
    assert "scenarios" not in result
    _assert_json_safe(result)


def test_capacity_curve_known_fill_participation_impact_and_partial_fill() -> None:
    result = estimate_capacity_curve(
        [1_000_000, 100_000, 500_000],
        average_daily_volume=1_000_000,
        one_way_turnover_rate=0.20,
        rebalances_per_year=12,
        execution_days=1,
        max_participation_rate=0.10,
        fixed_cost_bps=5.0,
        impact_coefficient_bps=10.0,
        impact_exponent=0.5,
        gross_annual_return=0.20,
    )

    assert result["status"] == "ok"
    assert result["full_fill_capacity"] == pytest.approx(500_000)
    assert [point["capital"] for point in result["curve"]] == [
        100_000,
        500_000,
        1_000_000,
    ]
    small, boundary, large = result["curve"]
    assert small["target_trade_notional"] == pytest.approx(20_000)
    assert small["fill_ratio"] == pytest.approx(1.0)
    assert small["adv_participation_rate"] == pytest.approx(0.02)
    assert small["impact_bps"] == pytest.approx(10 * np.sqrt(0.02))
    assert boundary["fill_ratio"] == pytest.approx(1.0)
    assert large["filled_notional"] == pytest.approx(100_000)
    assert large["unfilled_notional"] == pytest.approx(100_000)
    assert large["fill_ratio"] == pytest.approx(0.5)
    assert large["adv_participation_rate"] == pytest.approx(0.10)
    assert large["partially_filled"] is True
    assert (
        large["estimated_net_annual_return"]
        > small["estimated_net_annual_return"]
    )
    assert result["assumptions"]["impact_exponent"] == 0.5
    _assert_json_safe(result)


def test_capacity_curve_rejects_nonfinite_or_impossible_assumptions() -> None:
    assert estimate_capacity_curve(
        [100_000, np.inf],
        average_daily_volume=1_000_000,
        one_way_turnover_rate=0.2,
    )["status"] == "invalid_input"
    assert estimate_capacity_curve(
        [100_000],
        average_daily_volume=1_000_000,
        one_way_turnover_rate=0.2,
        max_participation_rate=1.5,
    )["status"] == "invalid_input"


def test_portfolio_constraints_report_all_breaches_and_known_hhi() -> None:
    result = check_portfolio_constraints(
        {"A": 0.6, "B": 0.3, "C": 0.1},
        {"A": "Bank", "B": "Bank", "C": "Tech"},
        turnover_rate=0.70,
        adv_participation={"A": 0.15, "B": 0.05, "C": 0.02},
        max_single_weight=0.50,
        max_industry_weight=0.70,
        max_gross_leverage=1.0,
        max_net_exposure=1.0,
        max_turnover_rate=0.50,
        max_adv_participation=0.10,
        max_concentration_hhi=0.40,
    )

    assert result["status"] == "ok"
    assert result["passed"] is False
    assert result["observations"]["concentration_hhi"] == pytest.approx(0.46)
    assert result["industry_exposure"] == pytest.approx(
        {"Bank": 0.9, "Tech": 0.1}
    )
    assert set(result["breaches"]) == {
        "single_weight",
        "industry_weight",
        "concentration_hhi",
        "turnover_rate",
        "adv_participation",
    }
    checks = {item["constraint"]: item for item in result["checks"]}
    assert checks["gross_leverage"]["passed"] is True
    assert checks["net_exposure"]["passed"] is True
    _assert_json_safe(result)


def test_portfolio_constraints_support_long_short_leverage() -> None:
    result = check_portfolio_constraints(
        {"long": 0.8, "short": -0.4},
        {"long": "Tech", "short": "Bank"},
        turnover_rate=0.2,
        adv_participation={"long": 0.04, "short": 0.05},
        max_single_weight=1.0,
        max_industry_weight=1.0,
        max_gross_leverage=1.0,
        max_net_exposure=0.5,
        max_concentration_hhi=1.0,
    )
    assert result["observations"]["gross_leverage"] == pytest.approx(1.2)
    assert result["observations"]["net_exposure"] == pytest.approx(0.4)
    assert "gross_leverage" in result["breaches"]
    assert "net_exposure" not in result["breaches"]


def test_portfolio_constraints_fail_closed_when_industry_or_liquidity_missing() -> None:
    missing_liquidity = check_portfolio_constraints(
        {"A": 1.0},
        {"A": "Tech"},
        turnover_rate=0.1,
        adv_participation=None,
    )
    missing_industry = check_portfolio_constraints(
        {"A": 1.0, "B": 0.0},
        {"A": "Tech"},
        turnover_rate=0.1,
        adv_participation={"A": 0.01, "B": 0.0},
    )
    zero_weight = check_portfolio_constraints(
        {"A": 0.0},
        {"A": "Tech"},
        turnover_rate=0.0,
        adv_participation={"A": 0.0},
    )

    assert missing_liquidity["status"] == "invalid_input"
    assert missing_industry["status"] == "invalid_input"
    assert zero_weight["status"] == "degenerate_input"
    assert missing_industry["missing_industries"] == ["B"]
    assert "passed" not in missing_industry
    _assert_json_safe(missing_liquidity)
    _assert_json_safe(missing_industry)
    _assert_json_safe(zero_weight)
