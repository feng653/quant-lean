from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd
import pytest

from backend.services import maintenance
from backend.strategies.base import TrainingWindowContext


def test_retrain_fit_uses_registered_isolated_cpu_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run(task: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured["task"] = task
        captured["payload"] = payload
        return {
            "candidate_model": {"safe": True},
            "train_metrics": {"n_validation_samples": 1},
            "feature_importance": {},
        }

    monkeypatch.setattr(maintenance, "run_isolated_cpu", fake_run)
    context = TrainingWindowContext(
        train_start="2024-01-01",
        train_end="2024-06-30",
        validation_start="2024-07-01",
        validation_end="2024-07-31",
    )
    model, metrics, importance = asyncio.run(
        maintenance._run_isolated_retrain_fit(
            strategy_id="alpha158_xgb_v1",
            pivot=pd.DataFrame({"000001": [1.0, 2.0]}),
            params={"seed": 7},
            context=context,
        )
    )

    assert captured["task"] == "model_retrain_fit"
    assert captured["payload"]["strategy_id"] == "alpha158_xgb_v1"
    assert captured["payload"]["windows"]["validation_end"] == "2024-07-31"
    assert model == {"safe": True}
    assert metrics == {"n_validation_samples": 1}
    assert importance == {}


def test_retrain_fit_fails_closed_when_isolation_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise maintenance.IsolatedCpuError("isolated_cpu_crashed", "internal")

    monkeypatch.setattr(maintenance, "run_isolated_cpu", crash)
    context = TrainingWindowContext(
        train_start="2024-01-01",
        train_end="2024-06-30",
        validation_start="2024-07-01",
        validation_end="2024-07-31",
    )
    with pytest.raises(RuntimeError, match="isolated_cpu_crashed"):
        asyncio.run(
            maintenance._run_isolated_retrain_fit(
                strategy_id="alpha158_xgb_v1",
                pivot=pd.DataFrame({"000001": [1.0, 2.0]}),
                params={},
                context=context,
            )
        )
