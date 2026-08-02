from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

from backend.config import settings
from backend.main import _init_databases
from backend.services import simulation


def _configure_databases(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    asyncio.run(_init_databases())


def test_mixed_strict_and_research_deployments_validate_both_bindings(
    tmp_path, monkeypatch
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    experiment_db = str(tmp_path / "experiment.db")
    trading_db = str(tmp_path / "trading.db")
    with sqlite3.connect(experiment_db) as connection:
        experiment_id = connection.execute(
            """INSERT INTO experiments
            (user_id, name, strategy_id, strategy_category, pool_preset,
             test_start, test_end, params, params_hash, status)
            VALUES (1, 'research', 'ma_cross_v1', 'technical', 'csi300',
                    '2020-01-01', '2020-12-31', '{}', 'hash', 'completed')"""
        ).lastrowid
        connection.execute(
            """INSERT INTO research_run_manifests
            (experiment_id, user_id, schema_version, manifest_json,
             manifest_hash, created_at)
            VALUES (?, 1, 'research-run-manifest/v1', ?, ?, datetime('now'))""",
            (
                experiment_id,
                json.dumps(
                    {
                        "research_trust": {
                            "profile": "tushare_research_trusted"
                        }
                    }
                ),
                "b" * 64,
            ),
        )
    with sqlite3.connect(trading_db) as connection:
        portfolio_id = connection.execute(
            """INSERT INTO portfolios
            (user_id, name, total_capital, rebalance_frequency,
             allocations, status, cash_balance, current_revision)
            VALUES (1, 'mixed', 100000, 'daily', '[]', 'active', 100000, 1)"""
        ).lastrowid
        strict_id = connection.execute(
            """INSERT INTO deployments
            (user_id, strategy_id, strategy_category, display_name, params,
             params_hash, mode, status, pool_preset)
            VALUES (1, 'ma_cross_v1', 'technical', 'strict', '{}', 's',
                    'batch', 'active', 'csi300')"""
        ).lastrowid
        research_id = connection.execute(
            """INSERT INTO deployments
            (user_id, strategy_id, strategy_category, display_name, params,
             params_hash, mode, status, pool_preset, source_experiment_id,
             research_generation_id)
            VALUES (1, 'ma_cross_v1', 'technical', 'research', '{}', 'r',
                    'batch', 'active', 'csi300', ?, ?)""",
            (experiment_id, "a" * 64),
        ).lastrowid
        connection.executemany(
            """INSERT INTO portfolio_allocations
            (portfolio_id, deployment_id, target_weight_bps, min_weight_bps,
             max_weight_bps, locked, revision)
            VALUES (?, ?, 5000, 0, 10000, 0, 1)""",
            [(portfolio_id, strict_id), (portfolio_id, research_id)],
        )

    monkeypatch.setattr(
        "backend.services.experiment_eligibility.assess_experiment_eligibility",
        lambda **_kwargs: SimpleNamespace(eligible=True),
    )
    monkeypatch.setattr(
        "backend.services.experiment_eligibility.verify_paper_risk_binding",
        lambda _deployment: None,
    )
    calls: list[str] = []

    async def research_market(**_kwargs):
        calls.append("research")
        return {"report": {"warnings": []}}

    async def strict_runtime(**_kwargs):
        calls.append("strict")
        return object()

    monkeypatch.setattr(
        "backend.services.research_runtime.load_research_market",
        research_market,
    )
    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input", strict_runtime
    )

    result = asyncio.run(
        simulation.require_simulation_pit_readiness(
            user_id=1,
            start_date="2020-12-30",
            end_date="2020-12-30",
            portfolio_id=portfolio_id,
        )
    )

    assert result["runnable"] is True
    assert calls == ["research", "strict"]


def test_manual_all_scope_and_scheduler_scope_share_portfolio_date_identity(
    tmp_path, monkeypatch
) -> None:
    _configure_databases(tmp_path, monkeypatch)

    async def ready(**_kwargs):
        return {"warnings": []}

    monkeypatch.setattr(simulation, "require_simulation_pit_readiness", ready)
    trading_db = str(tmp_path / "trading.db")
    with sqlite3.connect(trading_db) as connection:
        portfolio_ids = [
            connection.execute(
                """INSERT INTO portfolios
                (user_id, name, total_capital, rebalance_frequency,
                 allocations, status, cash_balance, current_revision)
                VALUES (1, ?, 100000, 'daily', '[]', 'active', 100000, 1)""",
                (name,),
            ).lastrowid
            for name in ("one", "two")
        ]

    manual = asyncio.run(simulation.run_daily_simulation(1, "2026-07-24"))
    for portfolio_id in portfolio_ids:
        asyncio.run(
            simulation.run_daily_simulation(
                1, "2026-07-24", portfolio_id=portfolio_id
            )
        )

    assert len(manual["portfolio_runs"]) == 2
    with sqlite3.connect(trading_db) as connection:
        rows = connection.execute(
            """SELECT portfolio_id, idempotency_key, status
            FROM simulation_runs ORDER BY portfolio_id"""
        ).fetchall()
    assert rows == [
        (portfolio_id, f"1:{portfolio_id}:2026-07-24", "completed")
        for portfolio_id in portfolio_ids
    ]


def test_expired_legacy_all_scope_claim_is_recovered_without_side_effects(
    tmp_path, monkeypatch
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    monkeypatch.setattr(
        simulation,
        "require_simulation_pit_readiness",
        lambda **_kwargs: None,
    )
    trading_db = str(tmp_path / "trading.db")
    with sqlite3.connect(trading_db) as connection:
        connection.execute(
            """INSERT INTO simulation_runs
            (id, user_id, portfolio_id, trade_date, idempotency_key, status,
             claim_token, claim_expires_at)
            VALUES ('legacy', 1, NULL, '2026-07-24', '1:all:2026-07-24',
                    'running', 'dead-worker', '2020-01-01T00:00:00+00:00')"""
        )

    result = asyncio.run(simulation.run_daily_simulation(1, "2026-07-24"))

    assert result["portfolio_runs"] == []
    with sqlite3.connect(trading_db) as connection:
        assert connection.execute(
            "SELECT status, error FROM simulation_runs WHERE id='legacy'"
        ).fetchone() == ("failed", "legacy_scope_replaced_after_expiry")
