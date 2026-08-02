from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import pandas as pd

from backend.api import data as data_api
from backend.api import experiments as experiments_api
from backend.strategies.base import StrategyCategory
from backend.strategies.registry import StrategyRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = (
    PROJECT_ROOT
    / "scripts"
    / "frontend_tuning"
    / "non_ml_tuning.v1.json"
)


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_tuning_grid_matches_every_non_ml_single_strategy_contract() -> None:
    config = _load_config()
    registry = StrategyRegistry()
    registry.scan_directory(str(PROJECT_ROOT / "backend" / "strategies"))
    non_ml_single = {
        metadata.strategy_id
        for metadata in registry.list_all()
        if not metadata.requires_training
        and metadata.category
        not in {StrategyCategory.ML, StrategyCategory.COMPOSITE}
    }
    configured = {item["strategy_id"] for item in config["strategies"]}
    assert configured == non_ml_single
    assert len(configured) == 11
    assert "composite_research_weighted_v1" not in configured

    selection_total = 0
    for strategy in config["strategies"]:
        metadata = registry.get_metadata(strategy["strategy_id"])
        assert metadata.requires_training is False
        assert metadata.category not in {
            StrategyCategory.ML,
            StrategyCategory.COMPOSITE,
        }
        assert metadata.version == strategy["expected_version"]
        fields = {field.name: field for field in metadata.params}
        assert set(strategy["grid"]) <= set(fields)
        values = list(strategy["grid"].values())
        selection_total += len(list(itertools.product(*values)))
        base_params = {field.name: field.default for field in metadata.params}
        for combination in itertools.product(*values):
            candidate = {
                **base_params,
                **dict(zip(strategy["grid"], combination, strict=True)),
            }
            valid, message = registry.validate_params(
                strategy["strategy_id"],
                candidate,
            )
            assert valid, (
                strategy["strategy_id"],
                candidate,
                message,
            )

    assert selection_total == 114
    assert config["expected"] == {
        "strategy_count": 11,
        "baseline_experiments": 11,
        "selection_experiments": 114,
        "locked_test_experiments": 11,
        "total_experiments": 136,
        "persistent_sweep_tabs": 11,
    }


def test_tuning_custom_cache_key_and_windows_are_preregistered() -> None:
    config = _load_config()
    codes = config["dataset"]["codes"]
    digest = hashlib.sha256(",".join(sorted(codes)).encode()).hexdigest()
    assert len(codes) == len(set(codes)) == 30
    assert config["dataset"]["cache_key"] == f"custom_{digest[:16]}"
    assert config["dataset"]["kind"] == "deterministic_synthetic"
    assert config["dataset"]["evidence_level"] == "declared"
    assert config["dataset"]["price_adjustment"] == "qfq"
    assert config["dataset"]["n_dates"] == 2412
    assert config["dataset"]["required_fields"] == [
        "amount",
        "close",
        "high",
        "low",
        "open",
        "volume",
    ]
    assert config["dataset"]["frame_digest"].endswith(
        "sha256:14693f29c18c9d6bdde63cbc3fac935d44af6d8fe143ac7159284f07080bb5e0"
    )
    assert config["windows"] == {
        "selection_start": "2023-07-31",
        "selection_end": "2023-12-29",
        "locked_test_start": "2024-01-02",
        "locked_test_end": "2024-03-29",
    }


def test_cache_status_is_read_only_and_sanitized(monkeypatch) -> None:
    codes = ["000001", "600000"]
    columns = pd.MultiIndex.from_product(
        [codes, ["open", "close", "high", "low", "volume", "amount"]]
    )
    frame = pd.DataFrame(
        1.0,
        index=pd.bdate_range("2024-01-02", "2024-01-05"),
        columns=columns,
    )

    class FakeCache:
        info_calls = 0
        load_calls = 0

        async def get_cache_info(self, pool_id: str) -> dict:
            self.info_calls += 1
            return {
                "pool_id": pool_id,
                "exists": True,
                "schema_version": 4,
                "source_trust": "declared",
                "source_provenance": {
                    "providers": ["local-synthetic-acceptance"],
                    "evidence_levels": ["declared"],
                    "frame_digest": "dv2|test|sha256:" + "a" * 64,
                    "identity_consistent": True,
                    "complete_code_coverage": True,
                },
                "price_adjustment": "qfq",
            }

        async def load_pivot(self, pool_id: str) -> pd.DataFrame:
            self.load_calls += 1
            return frame

    cache = FakeCache()
    monkeypatch.setattr(data_api._data_svc, "source", object())
    monkeypatch.setattr(data_api._data_svc, "cache", cache)
    result = __import__("asyncio").run(
        data_api.get_cache_status(
            pool_id="custom_1234",
            user={"id": 1},
        )
    )["data"]

    assert cache.info_calls == cache.load_calls == 1
    assert result["available"] is True
    assert result["runtime_readable"] is True
    assert result["source_trust"] == "declared"
    assert result["source_providers"] == ["local-synthetic-acceptance"]
    assert result["source_frame_digest"] == (
        "dv2|test|sha256:" + "a" * 64
    )
    assert result["source_identity_consistent"] is True
    assert result["source_complete_code_coverage"] is True
    assert result["n_stocks"] == 2
    assert result["fields"] == [
        "amount",
        "close",
        "high",
        "low",
        "open",
        "volume",
    ]
    assert "path" not in result
    assert "source_provenance" not in result


def test_cache_status_missing_cache_does_not_attempt_a_load(
    monkeypatch,
) -> None:
    class MissingCache:
        load_calls = 0

        async def get_cache_info(self, pool_id: str) -> dict:
            return {"pool_id": pool_id, "exists": False}

        async def load_pivot(self, pool_id: str) -> None:
            self.load_calls += 1
            raise AssertionError(pool_id)

    cache = MissingCache()
    monkeypatch.setattr(data_api._data_svc, "source", object())
    monkeypatch.setattr(data_api._data_svc, "cache", cache)
    result = __import__("asyncio").run(
        data_api.get_cache_status(
            pool_id="custom_missing",
            user={"id": 1},
        )
    )["data"]

    assert result["available"] is False
    assert result["error_code"] == "cache_missing"
    assert cache.load_calls == 0


def test_cache_status_hides_integrity_failure_details(monkeypatch) -> None:
    class InvalidCache:
        async def get_cache_info(self, pool_id: str) -> dict:
            return {
                "pool_id": pool_id,
                "exists": True,
                "schema_version": 3,
                "path": "/private/secret/cache.parquet",
            }

        async def load_pivot(self, pool_id: str) -> None:
            raise RuntimeError(
                f"invalid bytes at /private/secret/{pool_id}.parquet"
            )

    monkeypatch.setattr(data_api._data_svc, "source", object())
    monkeypatch.setattr(data_api._data_svc, "cache", InvalidCache())
    result = __import__("asyncio").run(
        data_api.get_cache_status(
            pool_id="custom_invalid",
            user={"id": 1},
        )
    )["data"]

    assert result["available"] is False
    assert result["runtime_readable"] is False
    assert result["error_code"] == "cache_not_runtime_readable"
    assert "/private/" not in json.dumps(result)


def test_cache_status_rejects_invalid_frame_structure(monkeypatch) -> None:
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "close", "high", "low", "volume", "amount"]]
    )
    frame = pd.DataFrame(
        1.0,
        index=pd.to_datetime(["2024-01-02", "2024-01-02"]),
        columns=columns,
    )

    class StructuralCache:
        async def get_cache_info(self, pool_id: str) -> dict:
            return {
                "pool_id": pool_id,
                "exists": True,
                "schema_version": "malformed",
                "fields": {"not": "a list"},
            }

        async def load_pivot(self, pool_id: str) -> pd.DataFrame:
            return frame

    monkeypatch.setattr(data_api._data_svc, "source", object())
    monkeypatch.setattr(data_api._data_svc, "cache", StructuralCache())
    result = __import__("asyncio").run(
        data_api.get_cache_status(
            pool_id="custom_structure",
            user={"id": 1},
        )
    )["data"]

    assert result["available"] is False
    assert result["error_code"] == "cache_structure_invalid"
    assert result["schema_version"] == 0
    assert result["fields"] == []


def test_submission_recovery_lookup_is_exact_and_owner_scoped(
    monkeypatch,
) -> None:
    class FakeCursor:
        def __init__(self, rows: list[dict]) -> None:
            self._rows = rows

        async def fetchall(self) -> list[dict]:
            return self._rows

    class FakeConnection:
        calls: list[tuple[str, tuple]] = []

        async def execute(self, query: str, params: tuple) -> FakeCursor:
            self.calls.append((query, params))
            if "FROM param_sweeps ps" in query:
                return FakeCursor([
                    {
                        "id": 7,
                        "name": "campaign-baseline-ma_cross_v1",
                        "strategy_id": "ma_cross_v1",
                        "sweep_config": '{"fast_period":[5,10,20]}',
                        "selection_start": "2023-07-31",
                        "selection_end": "2023-12-29",
                        "locked_test_start": "2024-01-02",
                        "locked_test_end": "2024-03-29",
                        "research_trust": "locked_test",
                        "total_experiments": 3,
                        "status": "running",
                        "created_at": "2026-07-30 00:00:01",
                        "promoted_experiment_id": None,
                        "promotion_source_experiment_id": None,
                        "member_run_spec": (
                            '{"data_access_policy":"cache_only"}'
                        ),
                        "source_experiment_id": 42,
                    }
                ])
            return FakeCursor([
                {
                    "id": 42,
                    "name": "campaign-baseline-ma_cross_v1",
                    "strategy_id": "ma_cross_v1",
                    "pool_preset": "custom",
                    "pool_custom_codes": '["000001","600000"]',
                    "pool_industries": "",
                    "train_start": None,
                    "train_end": None,
                    "test_start": "2023-07-31",
                    "test_end": "2023-12-29",
                    "params": '{"fast_period":10,"slow_period":60}',
                    "params_hash": "abc",
                    "mode": "batch",
                    "status": "running",
                    "created_at": "2026-07-30 00:00:00",
                    "source_experiment_id": None,
                    "run_spec": (
                        '{"data_access_policy":"cache_only"}'
                    ),
                }
            ])

    connection = FakeConnection()

    async def fake_get_db(name: str):
        assert name == "experiment"
        yield connection

    monkeypatch.setattr(experiments_api, "get_db", fake_get_db)
    result = __import__("asyncio").run(
        experiments_api.find_submission_recovery_candidates(
            name="campaign-baseline-ma_cross_v1",
            strategy_id="ma_cross_v1",
            user={"id": 9, "is_admin": True},
        )
    )["data"]

    assert len(result["experiments"]) == len(result["sweeps"]) == 1
    assert result["experiments"][0]["pool_custom_codes"] == [
        "000001",
        "600000",
    ]
    assert result["experiments"][0]["pool_industries"] == []
    assert result["experiments"][0]["data_access_policy"] == "cache_only"
    assert result["sweeps"][0]["sweep_config"] == {
        "fast_period": [5, 10, 20]
    }
    assert result["sweeps"][0]["data_access_policy"] == "cache_only"
    assert result["sweeps"][0]["source_experiment_id"] == 42
    assert "run_spec" not in result["experiments"][0]
    assert "member_run_spec" not in result["sweeps"][0]
    assert all(
        params == (9, "campaign-baseline-ma_cross_v1", "ma_cross_v1")
        for _, params in connection.calls
    )
