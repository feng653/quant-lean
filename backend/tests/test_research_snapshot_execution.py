from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from backend.config import settings
from backend.data.cache import DataCache
from backend.db.init import init_databases
from backend.execution.backtest_runner import run_experiment
from backend.services.research_manifest import load_run_manifest


class _Broker:
    async def update_job_progress(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def raise_if_cancelled(self, _job_uuid: str) -> None:
        return None

    async def is_cancel_requested(self, _job_uuid: str) -> bool:
        return False


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    experiment_db = tmp_path / "experiment.db"
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(experiment_db))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path / "empty-cache"))
    monkeypatch.setattr(
        settings,
        "RESEARCH_SNAPSHOT_DIR",
        str(tmp_path / "research-snapshots"),
    )
    asyncio.run(init_databases())
    broker = _Broker()
    monkeypatch.setattr("backend.jobs.broker.get_broker", lambda: broker)
    return experiment_db


def _insert_experiment(
    database: Path,
    *,
    pool_preset: str,
    run_spec: dict[str, Any],
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO experiments
                (id, user_id, name, strategy_id, strategy_category,
                 pool_preset, test_start, test_end, params, params_hash,
                 mode, requires_training, retrain_frequency, status, run_spec)
            VALUES (1, 1, 'PIT rejection probe', 'ma_cross_v1', 'technical',
                    ?, '2024-01-02', '2024-03-29', '{}', 'fixture',
                    'batch', 0, 'never', 'pending', ?)
            """,
            (pool_preset, json.dumps(run_spec, sort_keys=True)),
        )
        connection.commit()


def _forbid_legacy_data_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*_args: Any, **_kwargs: Any):
        pytest.fail("PIT rejection must happen before legacy cache/network access")

    monkeypatch.setattr(DataCache, "get_or_fetch", forbidden)
    monkeypatch.setattr(DataCache, "get_or_fetch_custom", forbidden)
    monkeypatch.setattr(DataCache, "get_or_fetch_index", forbidden)

    def forbidden_source():
        pytest.fail("PIT rejection must not construct a public data source")

    monkeypatch.setattr(
        "backend.data.sources.validated.build_public_research_source",
        forbidden_source,
    )


def _failure(database: Path) -> tuple[str, str, int]:
    with sqlite3.connect(database) as connection:
        status, error_log = connection.execute(
            "SELECT status, error_log FROM experiments WHERE id=1"
        ).fetchone()
        equity_count = connection.execute(
            "SELECT COUNT(*) FROM equity_curve WHERE experiment_id=1"
        ).fetchone()[0]
    return str(status), str(error_log), int(equity_count)


def test_worker_rejects_runtime_network_fallback_before_source_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _configure(tmp_path, monkeypatch)
    _insert_experiment(
        database,
        pool_preset="csi300",
        run_spec={"data_access_policy": "allow_fetch"},
    )
    _forbid_legacy_data_paths(monkeypatch)

    asyncio.run(run_experiment(1, "reject-network"))

    status, error_log, equity_count = _failure(database)
    assert status == "failed"
    assert "pit_cache_only_required" in error_log
    assert equity_count == 0
    assert asyncio.run(load_run_manifest(database, 1)) is None


def test_worker_rejects_custom_pool_before_cache_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _configure(tmp_path, monkeypatch)
    _insert_experiment(
        database,
        pool_preset="custom",
        run_spec={"data_access_policy": "cache_only"},
    )
    _forbid_legacy_data_paths(monkeypatch)

    asyncio.run(run_experiment(1, "reject-custom"))

    status, error_log, equity_count = _failure(database)
    assert status == "failed"
    assert "point_in_time_pool_unsupported" in error_log
    assert equity_count == 0


def test_worker_rejects_legacy_snapshot_without_pit_runtime_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _configure(tmp_path, monkeypatch)
    _insert_experiment(
        database,
        pool_preset="csi300",
        run_spec={
            "data_access_policy": "cache_only",
            "research_replay": {
                "dataset_snapshot": {
                    "relative_key": "must-not-be-opened.parquet",
                },
                "universe": {"point_in_time": False},
            },
        },
    )
    _forbid_legacy_data_paths(monkeypatch)

    asyncio.run(run_experiment(1, "reject-legacy-replay"))

    status, error_log, equity_count = _failure(database)
    assert status == "failed"
    assert "pit_replay_evidence_missing" in error_log
    assert "must-not-be-opened" not in error_log
    assert equity_count == 0


def test_worker_without_activated_pit_evidence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _configure(tmp_path, monkeypatch)
    _insert_experiment(
        database,
        pool_preset="csi300",
        run_spec={"data_access_policy": "cache_only"},
    )
    _forbid_legacy_data_paths(monkeypatch)

    asyncio.run(run_experiment(1, "reject-missing-evidence"))

    status, error_log, equity_count = _failure(database)
    assert status == "failed"
    assert (
        "price_cache_unavailable" in error_log
        or "effective_dated_history_missing" in error_log
    )
    assert equity_count == 0
