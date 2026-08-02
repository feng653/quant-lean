from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from backend.data.factor_research_runs import FactorResearchRunStore
from backend.research.factor_stability import analyze_pre_registered_stability
from backend.services.factor_research import (
    FactorResearchBody,
    FactorResearchExecutionError,
    execute_factor_research,
)


@pytest.fixture(autouse=True)
def isolated_pit_factor_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ready(**kwargs: Any) -> SimpleNamespace:
        cache = kwargs["cache"]
        pool_id = str(kwargs["pool_id"])
        pivot, provenance = await cache.load_pivot_with_provenance(pool_id)
        return SimpleNamespace(
            pool_id=pool_id,
            market=SimpleNamespace(
                frame=pivot,
                source_provenance=provenance,
            ),
        )

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        ready,
    )


def _fixture(
    periods: int = 400,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, str]]]:
    dates = pd.bdate_range("2023-01-02", periods=periods)
    codes = [f"{index:06d}" for index in range(12)]
    factor = pd.DataFrame(
        [
            np.arange(len(codes), dtype=float) + np.sin(day / 17)
            for day in range(periods)
        ],
        index=dates,
        columns=codes,
    )
    close = pd.DataFrame(
        {
            code: (10 + rank) * np.power(1 + rank / 100_000, np.arange(periods))
            for rank, code in enumerate(codes, start=1)
        },
        index=dates,
    )
    prices = pd.concat({"close": close}, axis=1).swaplevel(axis=1).sort_index(axis=1)
    windows = [
        {
            "role": "train",
            "start": str(dates[0].date()),
            "end": str(dates[251].date()),
        },
        {
            "role": "validation",
            "start": str(dates[252].date()),
            "end": str(dates[314].date()),
        },
        {
            "role": "locked",
            "start": str(dates[315].date()),
            "end": str(dates[377].date()),
        },
    ]
    return factor, prices, windows


def _analyze(
    factor: pd.DataFrame,
    prices: pd.DataFrame,
    windows: list[dict[str, str]],
) -> dict:
    return analyze_pre_registered_stability(
        factor,
        prices,
        windows=windows,
        horizons=[1, 5, 20],
        primary_horizon=5,
        quantiles=5,
        winsor_method="mad",
        hypotheses_tested=20,
        correction="bonferroni",
        alpha=0.05,
    )


def test_stability_windows_are_isolated_and_json_safe() -> None:
    factor, prices, windows = _fixture()
    original = _analyze(factor, prices, windows)

    mutated = prices.copy(deep=True)
    mutated_factor = factor.copy(deep=True)
    locked_start = pd.Timestamp(windows[2]["start"])
    mutated.loc[locked_start:, pd.IndexSlice[:, "close"]] *= 1_000_000
    mutated_factor.loc[locked_start:] *= -1_000
    changed = _analyze(mutated_factor, mutated, windows)

    assert original["windows"][0] == changed["windows"][0]
    assert original["windows"][1] == changed["windows"][1]
    assert original["design"]["forward_return_policy"] == (
        "truncate_at_each_window_end_before_shift"
    )
    assert [item["sessions"] for item in original["windows"]] == [252, 63, 63]
    assert original["multiple_testing"]["adjusted_alpha"] == pytest.approx(0.0025)
    primary_test = original["windows"][0]["horizons"]["5"]["multiple_testing"]
    if primary_test["raw_approx_p_value"] is not None:
        assert primary_test["adjusted_p_value"] == pytest.approx(
            min(1.0, primary_test["raw_approx_p_value"] * 20)
        )
    json.dumps(original, allow_nan=False, sort_keys=True)


def test_stability_fails_closed_on_insufficient_trading_sessions() -> None:
    factor, prices, windows = _fixture()
    too_short = copy.deepcopy(windows)
    too_short[1]["end"] = too_short[1]["start"]

    with pytest.raises(ValueError, match="validation.*至少需要 63"):
        _analyze(factor, prices, too_short)


def test_stability_fails_closed_when_factor_has_no_evaluable_dates() -> None:
    factor, prices, windows = _fixture()
    constant = pd.DataFrame(
        1.0,
        index=factor.index,
        columns=factor.columns,
    )

    with pytest.raises(ValueError, match="train.*可评估 RankIC.*至少需要 126"):
        _analyze(constant, prices, windows)


def test_pre_registered_config_requires_order_locked_declaration_and_bounds() -> None:
    factor, _, windows = _fixture()
    request = {
        "start": str(factor.index.min().date()),
        "end": str(factor.index.max().date()),
        "stability": {
            "mode": "fixed_three_way",
            "train": {
                "start": windows[0]["start"],
                "end": windows[0]["end"],
            },
            "validation": {
                "start": windows[1]["start"],
                "end": windows[1]["end"],
            },
            "locked": {
                "start": windows[2]["start"],
                "end": windows[2]["end"],
            },
            "locked_declared": True,
            "hypotheses_tested": 12,
            "correction": "bonferroni",
            "alpha": 0.05,
        },
    }
    body = FactorResearchBody.model_validate(request)
    assert body.stability is not None
    assert body.stability.windows() == windows
    assert body.model_dump()["stability"]["locked_declared"] is True

    request["stability"]["locked_declared"] = False
    with pytest.raises(ValidationError, match="运行前必须声明锁定窗"):
        FactorResearchBody.model_validate(request)

    request["stability"]["locked_declared"] = True
    request["stability"]["validation"]["start"] = windows[0]["end"]
    with pytest.raises(ValidationError, match="严格有序且互不重叠"):
        FactorResearchBody.model_validate(request)


def test_stability_is_optional_for_legacy_request_compatibility() -> None:
    body = FactorResearchBody(
        start="2024-01-01",
        end="2024-12-31",
    )
    assert body.stability is None
    assert body.model_dump()["stability"] is None


def test_factor_research_rejects_trust_label_without_dual_validation() -> None:
    _, prices, _ = _fixture()
    provenance = {
        "all_batches_raw_cross_validated": True,
        "all_batches_adjusted_factor_validated": False,
    }

    class IncompleteCache:
        async def load_pivot_with_provenance(self, cache_key: str):
            assert cache_key == "csi300"
            return prices, provenance

        @staticmethod
        def _source_trust(value: dict) -> str:
            assert value is provenance
            return "public_cross_validated_research_only"

    with pytest.raises(
        FactorResearchExecutionError,
        match="来源证据不足",
    ) as captured:
        asyncio.run(
            execute_factor_research(
                FactorResearchBody(
                    start=str(prices.index.min().date()),
                    end=str(prices.index.max().date()),
                ),
                owner_user_id=7,
                cache=IncompleteCache(),  # type: ignore[arg-type]
            )
        )

    assert captured.value.code == "factor_cache_source_untrusted"


def test_completed_stability_is_saved_in_immutable_run_evidence(
    tmp_path,
    allowed_isolated_cpu_executor: object,
) -> None:
    factor, prices, windows = _fixture()
    amount = pd.DataFrame(
        1_000_000.0,
        index=prices.index,
        columns=factor.columns,
    )
    pivot = pd.concat({"close": prices.xs("close", axis=1, level=1), "amount": amount}, axis=1)
    pivot = pivot.swaplevel(axis=1).sort_index(axis=1)
    provenance = {
        "providers": ["audited-test-provider"],
        "evidence_levels": ["public_aggregator"],
        "adjustments": ["hfq"],
        "content_sha256": "c" * 64,
        "all_batches_raw_cross_validated": True,
        "all_batches_adjusted_factor_validated": True,
    }

    class TrustedCache:
        async def load_pivot_with_provenance(self, cache_key: str):
            assert cache_key.startswith("custom_")
            return pivot, provenance

        @staticmethod
        def _source_trust(value: dict) -> str:
            assert value is provenance
            return "public_cross_validated_research_only"

    store = FactorResearchRunStore(tmp_path / "runs.db")
    body = FactorResearchBody(
        factor_id="momentum_20",
        pool_preset="custom",
        pool_custom_codes=[
            str(code)
            for code in prices.columns.get_level_values(0).unique()
        ],
        start=str(prices.index.min().date()),
        end=str(prices.index.max().date()),
        horizons=[1, 5, 20],
        primary_horizon=5,
        quantiles=5,
        stability={
            "train": windows[0],
            "validation": windows[1],
            "locked": windows[2],
            "locked_declared": True,
            "hypotheses_tested": 6,
        },
    )
    result = asyncio.run(
        execute_factor_research(
            body,
            owner_user_id=7,
            cache=TrustedCache(),
            store=store,
        )
    )
    run_id = result["run"]["run_id"]
    stored = store.get(owner_user_id=7, run_id=run_id)

    assert stored is not None
    assert stored["request"]["stability"]["locked_declared"] is True
    assert result["execution"]["cpu_boundary"] == "spawn_process"
    assert result["execution"]["max_concurrent_processes"] == 1
    assert stored["result"]["stability"]["schema_version"] == "factor-stability/v1"
    assert stored["result"]["stability"]["windows"][2]["role"] == "locked"
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE factor_research_runs SET request_json = '{}' WHERE run_id = ?",
                (run_id,),
            )
