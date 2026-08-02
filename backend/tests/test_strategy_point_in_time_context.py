from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.core.types import SignalDict
from backend.strategies.base import (
    StrategyMetadata,
    StrategyProtocol,
    TrainableStrategy,
)
from backend.strategies.factor._configured_factor import (
    make_factor_strategy_class,
)
from backend.strategies.registry import StrategyRegistry
from backend.strategies.research_context import (
    StrategyResearchContext,
    StrategyResearchContextError,
    activate_research_context,
    validate_strategy_research_context,
)


def _pivot(*, future_variant: float) -> pd.DataFrame:
    dates = pd.bdate_range("2022-01-03", periods=340, name="date")
    active = [f"{index:06d}" for index in range(12)]
    codes = [*active, "999999"]
    fields = ["open", "high", "low", "close", "volume", "amount"]
    columns = pd.MultiIndex.from_product(
        [codes, fields], names=["code", "field"]
    )
    frame = pd.DataFrame(index=dates, columns=columns, dtype=float)
    steps = np.arange(len(dates), dtype=float)
    for rank, code in enumerate(active, start=1):
        close = 10.0 + rank + steps * (0.002 + rank / 100_000)
        close += np.sin(steps / (7.0 + rank)) * 0.3
        frame[(code, "close")] = close
        frame[(code, "open")] = close * 0.999
        frame[(code, "high")] = close * 1.01
        frame[(code, "low")] = close * 0.99
        frame[(code, "volume")] = 1_000_000.0 + rank * 100
        frame[(code, "amount")] = frame[(code, "volume")] * close
    rogue = 20.0 + future_variant * steps + np.sin(steps) * 5.0
    rogue = np.maximum(rogue, 1.0)
    frame[("999999", "close")] = rogue
    frame[("999999", "open")] = rogue * 0.999
    frame[("999999", "high")] = rogue * 1.01
    frame[("999999", "low")] = rogue * 0.99
    frame[("999999", "volume")] = 9_999_999.0 * abs(future_variant)
    frame[("999999", "amount")] = (
        frame[("999999", "volume")] * rogue
    )
    return frame


def _context(frame: pd.DataFrame) -> StrategyResearchContext:
    members = tuple(f"{index:06d}" for index in range(12))
    return StrategyResearchContext.point_in_time_universe(
        dates=[day.strftime("%Y-%m-%d") for day in frame.index],
        members_by_date=[members for _day in frame.index],
        timeline_hash="a" * 64,
        price_role="adjusted_research_compatibility_not_raw_execution",
    )


def _params(metadata: StrategyMetadata) -> dict:
    return {field.name: field.default for field in metadata.params}


def _signal_identity(signals: SignalDict) -> dict[str, list[tuple]]:
    return {
        day: [
            (
                item.code,
                item.action,
                round(float(item.score), 12),
                round(float(item.weight), 12),
            )
            for item in items
        ]
        for day, items in sorted(signals.items())
    }


def _configured_factor() -> StrategyProtocol:
    strategy_type = make_factor_strategy_class(
        {
            "schema_version": "factor-combination-strategy/v1",
            "strategy_id": "factor_combo_0123456789ab",
            "name": "PIT contract fixture",
            "version": "1.0.0",
            "components": [
                {"factor_id": "momentum_20", "weight": 1.0}
            ],
            "top_k_pct": 0.25,
            "legacy_unbound": True,
            "research_evidence": [],
        }
    )
    return strategy_type()


def test_all_23_strategy_contracts_are_explicit_and_future_member_isolated() -> None:
    registry = StrategyRegistry()
    registry.scan_directory(Path("backend/strategies"))
    strategies = [
        registry.create_strategy(item.strategy_id)
        for item in registry.list_all()
    ]
    strategies.append(_configured_factor())
    assert len(strategies) == 23

    first = _pivot(future_variant=0.25)
    second = _pivot(future_variant=-0.04)
    context = _context(first)
    start = first.index[280].strftime("%Y-%m-%d")
    end = first.index[-1].strftime("%Y-%m-%d")

    for strategy in strategies:
        metadata = strategy.metadata()
        if metadata.requires_training:
            for candidate_context in (None, context):
                with pytest.raises(StrategyResearchContextError) as exc_info:
                    validate_strategy_research_context(
                        requires_training=True,
                        trainable_protocol=isinstance(
                            strategy,
                            TrainableStrategy,
                        ),
                        context=candidate_context,
                        point_in_time_capability=(
                            strategy.point_in_time_context_capability
                        ),
                    )
                assert exc_info.value.reason in {
                    "ml_point_in_time_universe_not_available",
                    "ml_point_in_time_label_eligibility_not_supported",
                }
            continue

        assert strategy.point_in_time_context_capability is not None
        validate_strategy_research_context(
            requires_training=False,
            trainable_protocol=False,
            context=context,
            point_in_time_capability=(
                strategy.point_in_time_context_capability
            ),
        )
        with activate_research_context(context):
            first_signals = strategy.generate_batch_signals(
                first, _params(metadata), start, end
            )
        second_strategy = (
            _configured_factor()
            if metadata.strategy_id.startswith("factor_combo_")
            else registry.create_strategy(metadata.strategy_id)
        )
        with activate_research_context(context):
            second_signals = second_strategy.generate_batch_signals(
                second, _params(metadata), start, end
            )
        assert _signal_identity(first_signals) == _signal_identity(
            second_signals
        ), metadata.strategy_id
        assert all(
            item.code != "999999"
            for items in first_signals.values()
            for item in items
        )


def test_undeclared_strategy_cannot_silently_run_on_point_in_time_data() -> None:
    frame = _pivot(future_variant=0.1)
    with pytest.raises(StrategyResearchContextError) as exc_info:
        validate_strategy_research_context(
            requires_training=False,
            trainable_protocol=False,
            context=_context(frame),
            point_in_time_capability=None,
        )
    assert exc_info.value.reason == (
        "strategy_point_in_time_context_not_supported"
    )


def test_self_managed_trainer_cannot_bypass_with_custom_or_all_a() -> None:
    for _pool_id in ("custom", "all_a"):
        with pytest.raises(StrategyResearchContextError) as exc_info:
            validate_strategy_research_context(
                requires_training=True,
                trainable_protocol=False,
                context=None,
                point_in_time_capability=None,
            )
        assert exc_info.value.reason == (
            "ml_point_in_time_universe_not_available"
        )
