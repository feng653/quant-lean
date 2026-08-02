import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from backend.api.factor_research import FactorResearchBody
from backend.config import settings
from backend.data.factor_research_runs import FactorResearchRunStore
from backend.research.factor_catalog import FACTOR_CATALOG, build_factor_panel
from backend.strategies.factor import _configured_factor
from backend.strategies.factor._configured_factor import (
    ConfiguredFactorStrategy,
    export_factor_strategy,
    load_factor_strategy_definitions,
)
from backend.strategies.registry import StrategyRegistry


def _pivot() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=30)
    columns = pd.MultiIndex.from_product(
        [["000001", "000002"], ["close", "amount"]]
    )
    rows = []
    for index in range(len(dates)):
        rows.append(
            [
                10 + index,
                1_000_000 + index,
                20 + index * 2,
                2_000_000 + index,
            ]
        )
    return pd.DataFrame(rows, index=dates, columns=columns)


@pytest.mark.parametrize(
    "factor_id",
    [
        "momentum_20",
        "short_reversal_5",
        "low_volatility_20",
        "liquidity_20",
        "price_efficiency_20",
        "risk_adjusted_momentum_20",
    ],
)
def test_supported_factor_panels_are_date_by_code(factor_id: str) -> None:
    result = build_factor_panel(_pivot(), factor_id)
    assert list(result.columns) == ["000001", "000002"]
    assert len(result) == 30


def test_factor_request_rejects_primary_horizon_outside_decay_set() -> None:
    with pytest.raises(ValueError, match="primary_horizon"):
        FactorResearchBody(
            start="2024-01-01",
            end="2024-02-01",
            horizons=[1, 5],
            primary_horizon=20,
        )


def test_factor_request_rejects_boolean_horizon_and_non_iso_date() -> None:
    with pytest.raises(ValueError):
        FactorResearchBody(
            start="2024/01/01",
            end="2024-02-01",
        )
    with pytest.raises(ValueError):
        FactorResearchBody(
            start="2024-01-01",
            end="2024-02-01",
            horizons=[True, 5],
            primary_horizon=5,
        )


def test_factor_catalog_definitions_are_unique_complete_and_buildable() -> None:
    factor_ids = [str(item["factor_id"]) for item in FACTOR_CATALOG]

    assert len(factor_ids) == len(set(factor_ids))
    assert factor_ids
    for item in FACTOR_CATALOG:
        assert set(item) == {
            "factor_id",
            "name",
            "description",
            "direction",
            "lookback",
            "required_fields",
            "category",
            "parameters",
            "version",
            "definition_digest",
            "parameter_schema",
            "dependencies",
            "supersedes",
        }
        assert str(item["name"]).strip()
        assert str(item["description"]).strip()
        assert item["direction"] == "high"
        assert isinstance(item["lookback"], int)
        assert item["lookback"] > 0
        assert item["required_fields"]
        assert item["category"]
        assert isinstance(item["parameters"], dict)
        assert item["version"] == "1.0.0"
        assert len(str(item["definition_digest"])) == 64
        assert item["parameter_schema"]["additionalProperties"] is False
        assert isinstance(item["dependencies"], list)

        panel = build_factor_panel(_pivot(), str(item["factor_id"]))
        assert panel.index.equals(_pivot().index)
        assert list(panel.columns) == ["000001", "000002"]


class _CapturingRegistry:
    def __init__(self) -> None:
        self.strategy_classes: list[type[ConfiguredFactorStrategy]] = []

    def register_strategy_class(
        self,
        strategy_class: type[ConfiguredFactorStrategy],
    ) -> None:
        self.strategy_classes.append(strategy_class)


def _export_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], _CapturingRegistry]:
    definitions_path = tmp_path / "factor_strategies.json"
    registry = _CapturingRegistry()
    monkeypatch.setattr(
        _configured_factor,
        "_definitions_path",
        lambda: definitions_path,
    )
    from backend.strategies import registry as registry_module

    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    definition = export_factor_strategy(
        name="动量流动性组合",
        components=[
            {"factor_id": "momentum_20", "weight": 3.0},
            {"factor_id": "liquidity_20", "weight": 1.0},
        ],
        top_k_pct=0.25,
        owner_user_id=7,
    )
    return definition, registry


def test_exported_factor_definition_round_trips_and_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, _registry = _export_definition(tmp_path, monkeypatch)

    assert load_factor_strategy_definitions() == [definition]
    assert definition["strategy_id"].startswith("factor_combo_")
    assert len(definition["definition_sha256"]) == 64

    definitions_path = tmp_path / "factor_strategies.json"
    payload = json.loads(definitions_path.read_text(encoding="utf-8"))
    payload[0]["components"][0]["weight"] = 99.0
    definitions_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="integrity mismatch"):
        load_factor_strategy_definitions()


def test_exported_factor_definition_binds_research_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definitions_path = tmp_path / "factor_strategies.json"
    monkeypatch.setattr(
        _configured_factor,
        "_definitions_path",
        lambda: definitions_path,
    )
    from backend.strategies import registry as registry_module

    monkeypatch.setattr(
        registry_module,
        "get_registry",
        lambda: _CapturingRegistry(),
    )
    evidence = [
        {
            "run_id": "frun_" + "1" * 32,
            "factor_id": "momentum_20",
            "dataset_digest": "a" * 64,
            "result_digest": "b" * 64,
        }
    ]
    definition = export_factor_strategy(
        name="证据组合",
        components=[{"factor_id": "momentum_20", "weight": 1.0}],
        top_k_pct=0.1,
        owner_user_id=7,
        research_evidence=evidence,
    )

    assert definition["research_evidence"] == evidence
    assert load_factor_strategy_definitions() == [definition]


def test_export_registers_runnable_strategy_with_deterministic_top_k_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, registry = _export_definition(tmp_path, monkeypatch)

    assert len(registry.strategy_classes) == 1
    strategy_class = registry.strategy_classes[0]
    monkeypatch.setattr(StrategyRegistry, "_instance", None)
    runtime_registry = StrategyRegistry()
    runtime_registry.register_strategy_class(strategy_class)
    strategy = runtime_registry.create_strategy(definition["strategy_id"])
    second_instance = runtime_registry.create_strategy(definition["strategy_id"])
    assert strategy is not second_instance
    metadata = strategy.metadata()
    assert metadata.strategy_id == definition["strategy_id"]
    assert metadata.display_name == "动量流动性组合"
    assert metadata.requires_training is False
    assert metadata.params[0].default == pytest.approx(0.25)

    dates = pd.bdate_range("2024-01-01", periods=45)
    codes = ["000001", "000002", "000003", "000004"]
    columns = pd.MultiIndex.from_product([codes, ["close", "amount"]])
    values: list[list[float]] = []
    for day in range(len(dates)):
        row: list[float] = []
        for rank, _code in enumerate(codes, start=1):
            row.extend(
                [
                    10.0 * (1.0 + rank / 100.0) ** day,
                    float(rank * 1_000_000),
                ]
            )
        values.append(row)
    pivot = pd.DataFrame(values, index=dates, columns=columns)

    signals = strategy.generate_batch_signals(
        pivot,
        {"top_k_pct": 0.25},
        "2024-02-01",
        "2024-02-29",
    )

    assert signals
    assert all(len(items) == 1 for items in signals.values())
    assert {
        item.code
        for items in signals.values()
        for item in items
    } == {"000004"}
    assert all(
        item.action == "BUY" and np.isfinite(item.score)
        for items in signals.values()
        for item in items
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "  "}, "策略名称"),
        ({"top_k_pct": float("nan")}, "top_k_pct"),
        (
            {
                "components": [
                    {"factor_id": "momentum_20", "weight": 1.0},
                    {"factor_id": "momentum_20", "weight": 2.0},
                ]
            },
            "components",
        ),
        (
            {"components": [{"factor_id": "unknown", "weight": 1.0}]},
            "components",
        ),
        ({"owner_user_id": True}, "owner_user_id"),
    ],
)
def test_export_rejects_invalid_direct_call_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    message: str,
) -> None:
    definitions_path = tmp_path / "factor_strategies.json"
    monkeypatch.setattr(
        _configured_factor,
        "_definitions_path",
        lambda: definitions_path,
    )
    kwargs: dict[str, Any] = {
        "name": "有效策略",
        "components": [{"factor_id": "momentum_20", "weight": 1.0}],
        "top_k_pct": 0.1,
        "owner_user_id": 7,
    }
    kwargs.update(overrides)

    with pytest.raises(ValueError, match=message):
        export_factor_strategy(**kwargs)

    assert not definitions_path.exists()


def test_factor_run_store_is_immutable_user_isolated_and_archivable(
    tmp_path: Path,
) -> None:
    store = FactorResearchRunStore(tmp_path / "factor-runs.db")
    result = {
        "dataset": {"content_sha256": "a" * 64},
        "metric": 1.5,
    }
    created = store.create(
        owner_user_id=7,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20"},
        result=result,
    )

    assert store.get(owner_user_id=8, run_id=created["run_id"]) is None
    record = store.get(owner_user_id=7, run_id=created["run_id"])
    assert record is not None
    assert record["result"] == result
    assert len(record["result_digest"]) == 64
    assert record["created_at"].endswith("Z")
    assert store.list(owner_user_id=7)[0]["run_id"] == created["run_id"]

    assert store.archive(owner_user_id=8, run_id=created["run_id"]) is False
    assert store.archive(owner_user_id=7, run_id=created["run_id"]) is True
    assert store.list(owner_user_id=7) == []
    assert store.list(owner_user_id=7, include_archived=True)[0]["archived_at"]

    with sqlite3.connect(store.path) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="immutable",
    ):
        connection.execute(
            "UPDATE factor_research_runs SET result_json = ? WHERE run_id = ?",
            ("{}", created["run_id"]),
        )


def test_factor_run_store_is_idempotent_for_replayed_job(tmp_path) -> None:
    store = FactorResearchRunStore(tmp_path / "factor-runs.db")
    result = {
        "dataset": {"content_sha256": "a" * 64},
        "metric": 1.5,
    }

    first = store.create(
        owner_user_id=7,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20"},
        result=result,
        source_job_uuid="job-replayed-1",
    )
    second = store.create(
        owner_user_id=7,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20"},
        result=result,
        source_job_uuid="job-replayed-1",
    )

    assert second == first
    assert len(store.list(owner_user_id=7)) == 1
    record = store.get(owner_user_id=7, run_id=first["run_id"])
    assert record is not None
    assert record["source_job_uuid"] == "job-replayed-1"


def test_factor_run_store_query_is_filtered_and_stably_paginated(
    tmp_path: Path,
) -> None:
    store = FactorResearchRunStore(tmp_path / "factor-runs.db")
    for factor_id, horizon in [
        ("momentum_20", 20),
        ("short_reversal_5", 5),
        ("momentum_20", 5),
        ("low_volatility_20", 10),
    ]:
        store.create(
            owner_user_id=7,
            factor_id=factor_id,
            request={
                "factor_id": factor_id,
                "primary_horizon": horizon,
            },
            result={
                "dataset": {"content_sha256": "a" * 64},
                "factor": {"factor_id": factor_id},
            },
        )
    store.create(
        owner_user_id=8,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20", "primary_horizon": 1},
        result={
            "dataset": {"content_sha256": "b" * 64},
            "factor": {"factor_id": "momentum_20"},
        },
    )

    first_page, total = store.query(
        owner_user_id=7,
        factor_id="momentum_20",
        sort="horizon",
        page=1,
        page_size=1,
    )
    second_page, second_total = store.query(
        owner_user_id=7,
        factor_id="momentum_20",
        sort="horizon",
        page=2,
        page_size=1,
    )

    assert total == second_total == 2
    assert first_page[0]["request"]["primary_horizon"] == 5
    assert second_page[0]["request"]["primary_horizon"] == 20
    queried, queried_total = store.query(
        owner_user_id=7,
        query="REVERSAL",
        page=1,
        page_size=20,
    )
    assert queried_total == 1
    assert queried[0]["factor_id"] == "short_reversal_5"


def test_factor_run_store_defaults_to_managed_experiment_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_database = tmp_path / "managed-experiment.db"
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(managed_database))

    store = FactorResearchRunStore()

    assert store.path == managed_database
    assert managed_database.exists()
    assert not (tmp_path / "factor_research.db").exists()
    with sqlite3.connect(managed_database) as connection:
        table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'factor_research_runs'
            """
        ).fetchone()
    assert table == ("factor_research_runs",)


def test_export_rolls_back_definition_when_registry_rejects_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definitions_path = tmp_path / "factor_strategies.json"
    original = b"[]"
    definitions_path.write_bytes(original)
    monkeypatch.setattr(
        _configured_factor,
        "_definitions_path",
        lambda: definitions_path,
    )
    from backend.strategies import registry as registry_module

    class _RejectingRegistry:
        @staticmethod
        def register_strategy_class(
            strategy_class: type[ConfiguredFactorStrategy],
        ) -> None:
            del strategy_class
            raise ValueError("registry rejected")

    monkeypatch.setattr(
        registry_module,
        "get_registry",
        lambda: _RejectingRegistry(),
    )

    with pytest.raises(ValueError, match="registry rejected"):
        export_factor_strategy(
            name="回滚验证",
            components=[{"factor_id": "momentum_20", "weight": 1.0}],
            top_k_pct=0.1,
            owner_user_id=7,
        )

    assert definitions_path.read_bytes() == original
