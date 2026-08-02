from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backend.core.types import SignalItem
from backend.strategies.composite.research_weighted import (
    CompositeResearchWeightedStrategy,
)
from backend.strategies.registry import get_registry


def _ensure_registry() -> None:
    registry = get_registry()
    if not registry.list_all():
        registry.scan_directory(Path(__file__).resolve().parents[1] / "strategies")


def _params() -> dict[str, str]:
    return {
        "component_specs": json.dumps(
            [
                {
                    "strategy_id": "ma_cross_v1",
                    "params": {"fast_period": 5, "slow_period": 20},
                    "source_experiment_id": 11,
                    "source_manifest_hash": "a" * 64,
                },
                {
                    "strategy_id": "rsi_reversal_v1",
                    "params": {},
                    "source_experiment_id": 12,
                    "source_manifest_hash": "b" * 64,
                },
            ]
        ),
        "static_weights": "[0.75,0.25]",
    }


def test_manifest_bound_composite_accepts_only_safe_atomic_definitions() -> None:
    _ensure_registry()
    strategy = CompositeResearchWeightedStrategy()
    assert strategy.validate_params(_params()) == (True, "")

    nested = _params()
    specs = json.loads(nested["component_specs"])
    specs[0]["strategy_id"] = "composite_equal_v1"
    nested["component_specs"] = json.dumps(specs)
    valid, error = strategy.validate_params(nested)
    assert valid is False
    assert "非机器学习原子策略" in error

    injected = _params()
    specs = json.loads(injected["component_specs"])
    specs[0]["callable"] = "os.system"
    injected["component_specs"] = json.dumps(specs)
    valid, error = strategy.validate_params(injected)
    assert valid is False
    assert "未知字段" in error


def test_manifest_bound_composite_normalizes_static_weights(monkeypatch) -> None:
    _ensure_registry()
    strategy = CompositeResearchWeightedStrategy()

    class Child:
        def __init__(self, code: str) -> None:
            self.code = code

        def generate_batch_signals(self, *_args):
            return {
                "2024-01-02": [SignalItem(self.code, "BUY", 1.0, 1.0)]
            }

    monkeypatch.setattr(
        strategy,
        "_get_sub_strategy",
        lambda strategy_id: Child("000001" if strategy_id == "ma_cross_v1" else "000002"),
    )
    signals = strategy.generate_batch_signals(
        pd.DataFrame(),
        _params(),
        "2024-01-02",
        "2024-01-02",
    )
    by_code = {item.code: item for item in signals["2024-01-02"]}
    assert by_code["000001"].score == pytest.approx(0.75)
    assert by_code["000002"].score == pytest.approx(0.25)


def test_manifest_bound_composite_rejects_forged_source_pair() -> None:
    _ensure_registry()
    strategy = CompositeResearchWeightedStrategy()
    params = _params()
    specs = json.loads(params["component_specs"])
    specs[0].pop("source_manifest_hash")
    params["component_specs"] = json.dumps(specs)
    valid, error = strategy.validate_params(params)
    assert valid is False
    assert "同时提供" in error
