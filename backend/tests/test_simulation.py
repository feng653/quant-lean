from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.config import settings
from backend.core.types import SignalItem
from backend.main import _init_databases
from backend.services import simulation
from backend.services.research_manifest import canonical_sha256
from backend.strategies.base import ParamField


class _AlwaysBuyStrategy:
    def generate_batch_signals(self, pivot, params, start_date, end_date):
        return {
            end_date: [
                SignalItem(
                    code="000001",
                    action="BUY",
                    score=1.0,
                    weight=1.0,
                )
            ]
        }


class _FakeRegistry:
    def create_strategy(self, strategy_id):
        assert strategy_id == "test_open"
        return _AlwaysBuyStrategy()


def test_simulation_lookback_uses_strategy_params_not_fixed_800_days(
    monkeypatch,
) -> None:
    registry = SimpleNamespace(
        get_metadata=lambda strategy_id: SimpleNamespace(
            params=[ParamField("slow_period", "int", 50)]
        )
    )
    monkeypatch.setattr(simulation, "get_registry", lambda: registry)

    sessions, warnings = simulation.derive_simulation_lookback(
        "ma_cross_v1",
        json.dumps({"slow_period": 120}),
    )

    assert sessions == 125
    assert warnings == ()


def test_unknown_strategy_lookback_is_warning_not_production_gate(
    monkeypatch,
) -> None:
    registry = SimpleNamespace(
        get_metadata=lambda strategy_id: (_ for _ in ()).throw(KeyError(strategy_id))
    )
    monkeypatch.setattr(simulation, "get_registry", lambda: registry)

    sessions, warnings = simulation.derive_simulation_lookback("unknown", {})

    assert sessions == 252
    assert set(warnings) == {
        "strategy_lookback_metadata_unavailable",
        "strategy_lookback_not_declared_using_252_sessions",
    }


@pytest.fixture(autouse=True)
def isolated_pit_simulation_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep execution-engine tests behind an explicit isolated PIT boundary."""

    async def ready(**_kwargs) -> None:
        return None

    monkeypatch.setattr(simulation, "require_simulation_pit_readiness", ready)


def test_deployed_model_artifact_is_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(tmp_path / "models"))
    asyncio.run(_init_databases())

    model_path = tmp_path / "models" / "experiment_1" / "model.joblib"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"verified-model")
    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
    params = {}
    params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
    params_hash = hashlib.md5(params_json.encode()).hexdigest()
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        experiment_id = connection.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category,
                 test_start, test_end, params, params_hash, status)
            VALUES (7, 'Source', 'test_model', 'ml',
                    '2025-01-01', '2025-12-31', ?, ?, 'completed')
            """,
            (params_json, params_hash),
        ).lastrowid
        manifest = {
            "schema_version": "research-run-manifest/v1",
            "experiment": {
                "experiment_id": experiment_id,
                "strategy_id": "test_model",
            },
            "strategy": {"class": "FakeModelStrategy"},
            "environment": {"python": {"version": "test"}},
            "parameters": {
                "canonical": params,
                "sha256": canonical_sha256(params),
            },
            "windows": {
                "train_start": "2024-01-01",
                "train_end": "2024-12-31",
                "test_start": "2025-01-01",
                "test_end": "2025-12-31",
            },
            "dataset": {
                "digest": "a" * 64,
                "context_digest": "b" * 64,
            },
            "universe": {"snapshot_hash": "c" * 64},
        }
        manifest_hash = canonical_sha256(manifest)
        connection.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (?, 7, 'research-run-manifest/v1', ?, ?, datetime('now'))
            """,
            (experiment_id, json.dumps(manifest), manifest_hash),
        )
        artifact_id = connection.execute(
            """
            INSERT INTO model_artifacts
                (experiment_id, strategy_id, model_file_path,
                  metadata_file_path, params_hash, artifact_sha256,
                  artifact_size, run_manifest_hash)
            VALUES (?, 'test_model', ?, 'metadata.json', ?, ?, ?, ?)
            """,
            (
                experiment_id,
                str(model_path),
                params_hash,
                model_sha256,
                model_path.stat().st_size,
                manifest_hash,
            ),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO research_artifact_manifests
                (experiment_id, run_manifest_hash, schema_version,
                 artifact_kind, artifact_sha256, artifact_size,
                 metadata_json, created_at)
            VALUES (?, ?, 'research-artifact-manifest/v1',
                    'trained_model', ?, ?, ?, datetime('now'))
            """,
            (
                experiment_id,
                manifest_hash,
                model_sha256,
                model_path.stat().st_size,
                json.dumps(
                    {"strategy_id": "test_model", "model_version": 1}
                ),
            ),
        )
        connection.commit()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="research artifact manifest is immutable",
        ):
            connection.execute(
                """
                UPDATE research_artifact_manifests
                SET artifact_size=artifact_size + 1
                WHERE experiment_id=?
                """,
                (experiment_id,),
            )
        connection.rollback()

    loaded_model = object()

    class FakeModelStrategy:
        _model = None

        def load_model(self, path):
            snapshot = Path(path)
            assert snapshot.read_bytes() == b"verified-model"
            assert snapshot.parent.name == ".verified-load"
            return loaded_model

    strategy = FakeModelStrategy()
    asyncio.run(
        simulation._load_deployed_model(
            strategy,
            {
                "source_model_artifact_id": artifact_id,
                "source_experiment_id": experiment_id,
                "strategy_id": "test_model",
                "user_id": 7,
                "params": params_json,
                "params_hash": params_hash,
            },
        )
    )
    assert strategy._model is loaded_model

    for invalid_deployment in (
        {
            "source_model_artifact_id": artifact_id,
            "source_experiment_id": experiment_id,
            "strategy_id": "test_model",
            "user_id": 8,
            "params": params_json,
            "params_hash": params_hash,
        },
        {
            "source_model_artifact_id": artifact_id,
            "source_experiment_id": experiment_id,
            "strategy_id": "other_model",
            "user_id": 7,
            "params": params_json,
            "params_hash": params_hash,
        },
        {
            "source_model_artifact_id": artifact_id,
            "source_experiment_id": experiment_id + 1,
            "strategy_id": "test_model",
            "user_id": 7,
            "params": params_json,
            "params_hash": params_hash,
        },
    ):
        with pytest.raises(ValueError, match="do not match"):
            asyncio.run(
                simulation._load_deployed_model(
                    FakeModelStrategy(),
                    invalid_deployment,
                )
            )


def test_daily_simulation_is_idempotent_and_fills_at_next_open(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path / "cache"))
    asyncio.run(_init_databases())

    panel = pd.DataFrame(
        {
            ("000001", "open"): [10.0, 11.0, 12.0],
            ("000001", "close"): [10.5, 11.5, 12.5],
        },
        index=pd.to_datetime(["2026-07-22", "2026-07-23", "2026-07-24"]),
    )
    panel.columns = pd.MultiIndex.from_tuples(panel.columns)

    async def fake_load_pivot(pool_id, requested_date, cache_by_pool):
        return panel

    monkeypatch.setattr(simulation, "_load_pivot", fake_load_pivot)
    monkeypatch.setattr(simulation, "get_registry", lambda: _FakeRegistry())

    async def fake_verify_promotion(deployment):
        return {"promotion_id": deployment["id"]}

    monkeypatch.setattr(
        simulation,
        "verify_deployment_promotion",
        fake_verify_promotion,
    )

    db_path = str(tmp_path / "trading.db")
    with sqlite3.connect(db_path) as conn:
        deployment_id = conn.execute(
            """
            INSERT INTO deployments
                (user_id, strategy_id, strategy_category, display_name,
                 params, params_hash, mode, status, pool_preset)
            VALUES (1, 'test_open', 'technical', 'Open contract',
                    '{}', 'hash', 'batch', 'active', 'csi300')
            """
        ).lastrowid
        portfolio_id = conn.execute(
            """
            INSERT INTO portfolios
                (user_id, name, total_capital, rebalance_frequency,
                 allocations, status, cash_balance, current_revision)
            VALUES (1, 'Paper', 100000, 'daily', '[]', 'active', 100000, 1)
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO portfolio_allocations
                (portfolio_id, deployment_id, target_weight_bps,
                 min_weight_bps, max_weight_bps, locked, revision)
            VALUES (?, ?, 10000, 0, 10000, 0, 1)
            """,
            (portfolio_id, deployment_id),
        )
        conn.commit()

    replay = asyncio.run(
        simulation.run_simulation_backfill(1, "2026-07-24", "2026-07-24")
    )
    assert replay["trading_days"] == 1
    first = replay["last_result"]
    second = asyncio.run(simulation.run_daily_simulation(1, "2026-07-24"))

    assert second == first
    with pytest.raises(ValueError, match="不能在已完成 .* 后回补"):
        asyncio.run(simulation.run_daily_simulation(1, "2026-07-23"))
    with pytest.raises(ValueError, match="不是部署 .* 的可执行交易日"):
        asyncio.run(simulation.run_daily_simulation(1, "2026-07-25"))
    with sqlite3.connect(db_path) as conn:
        order = conn.execute(
            """
            SELECT date, price, status, filled_at, order_intent_id
            FROM orders
            """
        ).fetchone()
        assert order[:4] == (
            "2026-07-24",
            12.0,
            "filled",
            "2026-07-24 09:30:00",
        )
        assert len(order[4]) == 64
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        nav = conn.execute(
            "SELECT date, total_equity FROM nav_history"
        ).fetchone()
        assert nav[0] == "2026-07-24"
        assert nav[1] > 0
        strategy_nav = conn.execute(
            """
            SELECT opening_equity, net_flow, cash_balance, market_value,
                   total_equity, daily_pnl, transaction_cost, turnover,
                   contribution_pnl
            FROM strategy_nav_history
            WHERE portfolio_id=? AND deployment_id=? AND date='2026-07-24'
            """,
            (portfolio_id, deployment_id),
        ).fetchone()
        assert strategy_nav is not None
        assert strategy_nav[4] == pytest.approx(strategy_nav[2] + strategy_nav[3])
        assert strategy_nav[5] == pytest.approx(
            strategy_nav[4] - strategy_nav[0] - strategy_nav[1]
        )
        assert strategy_nav[8] == pytest.approx(strategy_nav[5])
        assert strategy_nav[6] > 0
        assert strategy_nav[7] > 0
        assert strategy_nav[4] == pytest.approx(nav[1])

        control_portfolio_id = conn.execute(
            """
            INSERT INTO portfolios
                (user_id, name, total_capital, rebalance_frequency,
                 allocations, status, cash_balance, current_revision)
            VALUES (1, 'Control', 200000, 'daily', '[]', 'active', 200000, 1)
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO nav_history
                (portfolio_id, date, nav, daily_return, cumulative_return)
            VALUES (?, '2026-07-24', 200000, 0, 0)
            """,
            (control_portfolio_id,),
        )
        conn.commit()

    scoped_replay = asyncio.run(
        simulation.run_simulation_backfill(
            1,
            "2026-07-24",
            "2026-07-24",
            portfolio_id=portfolio_id,
            restart=True,
        )
    )
    assert scoped_replay["portfolio_id"] == portfolio_id
    assert scoped_replay["restarted"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nav_history WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM strategy_nav_history WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT nav FROM nav_history WHERE portfolio_id=?",
            (control_portfolio_id,),
        ).fetchone()[0] == 200000
        scoped_run = conn.execute(
            """
            SELECT portfolio_id FROM simulation_runs
            WHERE portfolio_id=? AND status='completed'
            """,
            (portfolio_id,),
        ).fetchone()
        assert scoped_run == (portfolio_id,)


def test_identical_running_simulation_cannot_be_claimed_twice(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    asyncio.run(_init_databases())

    db_path = str(tmp_path / "trading.db")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO simulation_runs
                (id, user_id, portfolio_id, trade_date, idempotency_key,
                 status, claim_token)
            VALUES ('owned-run', 1, NULL, '2026-07-24',
                    '1:all:2026-07-24', 'running', 'first-worker')
            """
        )
        connection.commit()

    with pytest.raises(
        simulation.SimulationRunInProgressError,
        match="already in progress",
    ):
        asyncio.run(simulation.run_daily_simulation(1, "2026-07-24"))

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT status, claim_token FROM simulation_runs
            WHERE id='owned-run'
            """
        ).fetchone() == ("running", "first-worker")
