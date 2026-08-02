from backend.services.allocations import (
    build_rebalance_preview,
    canonicalize_allocations,
)


def test_canonical_allocations_preserve_explicit_cash():
    allocations, validation = canonicalize_allocations(
        [
            {
                "deployment_id": 1,
                "target_weight_bps": 6_000,
                "min_weight_bps": 2_000,
                "max_weight_bps": 8_000,
                "locked": False,
            },
            {
                "deployment_id": 2,
                "target_weight_bps": 2_500,
                "min_weight_bps": 0,
                "max_weight_bps": 5_000,
                "locked": True,
            },
        ],
        total_capital=1_000_000,
    )

    assert validation["valid"] is True
    assert validation["strategy_weight_bps"] == 8_500
    assert validation["cash_weight_bps"] == 1_500
    assert allocations[0]["capital"] == 600_000


def test_duplicate_deployment_and_bound_violations_are_rejected():
    _, validation = canonicalize_allocations(
        [
            {
                "deployment_id": 7,
                "target_weight_bps": 6_000,
                "min_weight_bps": 7_000,
                "max_weight_bps": 8_000,
            },
            {
                "deployment_id": 7,
                "target_weight_bps": 5_000,
            },
            {
                "deployment_id": 8,
                "target_weight_bps": 5_000,
            },
        ],
        total_capital=100_000,
    )

    assert validation["valid"] is False
    assert any("重复" in message for message in validation["errors"])
    assert any("超出" in message for message in validation["errors"])
    assert any("10000bp" in message for message in validation["errors"])


def test_rebalance_preview_reports_direction_turnover_and_cost():
    preview = build_rebalance_preview(
        allocations=[
            {
                "deployment_id": 1,
                "target_weight_bps": 7_000,
                "min_weight_bps": 0,
                "max_weight_bps": 10_000,
                "locked": False,
                "capital": 700_000,
            },
            {
                "deployment_id": 2,
                "target_weight_bps": 2_000,
                "min_weight_bps": 0,
                "max_weight_bps": 10_000,
                "locked": False,
                "capital": 200_000,
            },
        ],
        current_market_values={1: 500_000, 2: 400_000},
        total_capital=1_000_000,
    )

    assert [row["direction"] for row in preview["rows"]] == ["BUY", "SELL"]
    assert preview["one_way_turnover"] == 200_000
    assert preview["turnover_rate"] == 0.2
    assert preview["estimated_cost"] > 0
