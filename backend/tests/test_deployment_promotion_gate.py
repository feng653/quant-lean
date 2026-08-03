from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from backend.api import trading as trading_api
from backend.config import settings
from backend.data.cache import (
    DailyMarketDataQualityError,
    LegacyAdjustedCacheError,
)
from backend.data.generation_manifest import GenerationManifestError
from backend.db.init import init_databases
from backend.services import deployment_promotion, simulation
from backend.services.experiment_eligibility import (
    ExperimentEligibility,
    PaperRiskBindingError,
    verify_paper_risk_binding,
)
from backend.services.deployment_promotion import (
    DeploymentPromotionError,
    PROMOTION_BINDING_SCHEMA,
)
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    canonical_sha256,
)
from backend.services.research_workflow import (
    ResearchWorkflowService,
    WorkflowError,
)
from backend.strategies.base import (
    RetrainFrequency,
    StrategyCategory,
    StrategyMode,
)


class _Registry:
    metadata = SimpleNamespace(
        strategy_id="test_strategy",
        display_name="Test strategy",
        category=StrategyCategory.TECHNICAL,
        supported_modes=[StrategyMode.BATCH],
        requires_training=False,
        retrain_frequency=RetrainFrequency.NEVER,
    )
    def get_metadata(self, strategy_id: str):
        if strategy_id != "test_strategy":
            raise KeyError(strategy_id)
        return self.metadata

    def validate_params(self, strategy_id: str, params: dict):
        return strategy_id == "test_strategy" and params == {}, ""


@pytest.fixture(autouse=True)
def isolated_pit_execution_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Promotion tests supply synthetic prices behind an explicit PIT seam."""

    async def ready(**_kwargs) -> None:
        return None

    async def pit_eligible(*_args, **_kwargs) -> ExperimentEligibility:
        return ExperimentEligibility(True, "pit_manifest_verified")

    monkeypatch.setattr(simulation, "require_simulation_pit_readiness", ready)
    monkeypatch.setattr(
        "backend.services.experiment_eligibility.load_experiment_eligibility",
        pit_eligible,
    )


def _configure_databases(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(
        settings,
        "EXPERIMENT_DB",
        str(tmp_path / "experiment.db"),
    )
    monkeypatch.setattr(
        settings,
        "TRADING_SIM_DB",
        str(tmp_path / "trading.db"),
    )
    asyncio.run(init_databases())


def _binding(promotion_id: int = 5) -> dict:
    identity = {
        "schema_version": PROMOTION_BINDING_SCHEMA,
        "promotion_id": promotion_id,
        "promotion_version": 3,
        "report_id": 11,
        "report_hash": "a" * 64,
        "experiment_id": 1,
        "manifest_hash": "b" * 64,
        "model_artifact_id": None,
        "model_sha256": None,
        "model_evidence_hash": None,
    }
    return {**identity, "binding_hash": canonical_sha256(identity)}


def _seed_source_experiment(path) -> tuple[int, str]:
    params_hash = hashlib.md5(b"{}").hexdigest()
    with sqlite3.connect(path) as connection:
        experiment_id = connection.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category,
                 test_start, test_end, params, params_hash, status)
            VALUES (7, 'Source', 'test_strategy', 'technical',
                    '2025-01-01', '2025-12-31', '{}', ?, 'completed')
            """,
            (params_hash,),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO equity_curve (experiment_id, date, equity)
            VALUES (?, '2025-01-02', 1000000)
            """,
            (experiment_id,),
        )
        manifest = {
            "schema_version": RUN_MANIFEST_SCHEMA,
            "experiment": {
                "experiment_id": experiment_id,
                "strategy_id": "test_strategy",
            },
            "windows": {
                "test_start": "2025-01-01",
                "test_end": "2025-12-31",
            },
            "dataset": {"digest": "d" * 64},
            "research_risk_warnings": ["single_source_research"],
        }
        manifest_hash = canonical_sha256(manifest)
        connection.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (?, 7, ?, ?, ?, datetime('now'))
            """,
            (
                experiment_id,
                RUN_MANIFEST_SCHEMA,
                json.dumps(manifest),
                manifest_hash,
            ),
        )
        connection.commit()
    return int(experiment_id), params_hash


def _seed_approved_promotion(path) -> tuple[int, str]:
    experiment_id, params_hash = _seed_source_experiment(path)
    with sqlite3.connect(path) as connection:
        manifest_hash = connection.execute(
            "SELECT manifest_hash FROM research_run_manifests WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()[0]
    snapshot = {
        "evidence_scope": "strict_locked_test_final",
        "trials": [
            {
                "role": "locked_test",
                "experiment_id": experiment_id,
                "manifest": {"manifest_hash": manifest_hash},
            }
        ],
    }
    with sqlite3.connect(path) as connection:
        hypothesis_id = connection.execute(
            """
            INSERT INTO research_hypotheses
                (user_id, title, falsifiable_statement,
                 preregistered_metrics_json, risk_acceptance_json,
                 status, version, idempotency_key, created_at, updated_at)
            VALUES (7, 'Test', 'Test statement', '[]', '{}',
                    'submitted', 1, 'hypothesis', datetime('now'),
                    datetime('now'))
            """
        ).lastrowid
        group_id = connection.execute(
            """
            INSERT INTO research_experiment_groups
                (user_id, hypothesis_id, name, strategy_id,
                 selection_protocol_json, locked_protocol_json,
                 manifest_policy_json, status, version, idempotency_key,
                 created_at, updated_at)
            VALUES (7, ?, 'Group', 'test_strategy', '{}', '{}', '{}',
                    'active', 1, 'group', datetime('now'), datetime('now'))
            """,
            (hypothesis_id,),
        ).lastrowid
        report_id = connection.execute(
            """
            INSERT INTO research_reports
                (user_id, group_id, report_type, snapshot_json,
                 snapshot_hash, idempotency_key, created_at)
            VALUES (7, ?, 'final', ?, ?, 'report', datetime('now'))
            """,
            (group_id, json.dumps(snapshot), canonical_sha256(snapshot)),
        ).lastrowid
        promotion_id = connection.execute(
            """
            INSERT INTO research_promotions
                (user_id, group_id, report_id, status, rationale,
                 blockers_json, version, idempotency_key,
                 created_at, updated_at)
            VALUES (7, ?, ?, 'approved', 'approved', '[]', 3,
                    'promotion', datetime('now'), datetime('now'))
            """,
            (group_id, report_id),
        ).lastrowid
        connection.commit()
    return int(promotion_id), params_hash


async def _no_blockers(*_args, **_kwargs):
    return []


def test_active_creation_requires_source_experiment_not_approval(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: _Registry(),
    )
    with pytest.raises(HTTPException) as blocked:
        asyncio.run(
            trading_api.create_deployment(
                trading_api.CreateDeploymentBody(
                    strategy_id="test_strategy",
                ),
                {
                    "id": 7,
                    "is_admin": False,
                    "permissions": ["trading:deploy"],
                },
            )
        )
    assert blocked.value.status_code == 422
    assert blocked.value.detail["code"] == "paper_source_experiment_required"


def test_active_creation_persists_approved_binding(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    experiment_id, _ = _seed_source_experiment(tmp_path / "experiment.db")
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: _Registry(),
    )

    async def fake_resolve(**_kwargs):
        return _binding()

    monkeypatch.setattr(
        deployment_promotion,
        "resolve_deployment_promotion",
        fake_resolve,
    )
    result = asyncio.run(
        trading_api.create_deployment(
            trading_api.CreateDeploymentBody(
                strategy_id="test_strategy",
                source_experiment_id=experiment_id,
                research_promotion_id=5,
                status="active",
            ),
            {
                "id": 7,
                "is_admin": False,
                "permissions": ["trading:deploy"],
            },
        )
    )
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        assert connection.execute(
            """
            SELECT status, research_promotion_id, promotion_report_id,
                   promotion_manifest_hash, promotion_binding_hash
            FROM deployments WHERE id=?
            """,
            (result["data"]["deployment_id"],),
        ).fetchone() == (
            "active",
            5,
            11,
            "b" * 64,
            _binding()["binding_hash"],
        )


def test_unapproved_completed_experiment_deploys_with_immutable_warning(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    experiment_id, _ = _seed_source_experiment(tmp_path / "experiment.db")
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: _Registry(),
    )

    result = asyncio.run(
        trading_api.create_deployment(
            trading_api.CreateDeploymentBody(
                strategy_id="test_strategy",
                source_experiment_id=experiment_id,
                status="active",
            ),
            {"id": 7, "is_admin": False, "permissions": ["trading:deploy"]},
        )
    )["data"]
    snapshot = result["research_risk_snapshot"]

    assert snapshot["research_promotion_bound"] is False
    assert snapshot["live_eligible"] is False
    assert set(snapshot["warnings"]) >= {
        "manual_research_approval_missing",
        "paper_only_live_trading_not_eligible",
        "research_generation_id_missing",
    }
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        row = connection.execute(
            """
            SELECT research_risk_snapshot_hash, research_source_id,
                   research_window_start, research_window_end
            FROM deployments WHERE id=?
            """,
            (result["deployment_id"],),
        ).fetchone()
        assert row[0] == result["research_risk_snapshot_hash"]
        assert row[1:] == (None, "2025-01-01", "2025-12-31")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="research risk binding is immutable",
        ):
            connection.execute(
                "UPDATE deployments SET research_window_end='2099-01-01' WHERE id=?",
                (result["deployment_id"],),
            )


def test_paper_risk_binding_detects_out_of_band_tamper() -> None:
    snapshot = {
        "schema_version": "paper-deployment-research-risk/v1",
        "source_experiment_id": 8,
        "source_manifest_hash": "a" * 64,
        "warnings": ["single_source_research"],
        "paper_eligible": True,
        "live_eligible": False,
        "research_generation_id": "generation-1",
        "research_source_id": "tushare",
        "window": {"start": "2020-01-01", "end": "2025-12-31"},
    }
    deployment = {
        "source_experiment_id": 8,
        "research_risk_snapshot": json.dumps(snapshot, sort_keys=True),
        "research_risk_snapshot_hash": canonical_sha256(snapshot),
        "research_generation_id": "generation-1",
        "research_source_id": "tushare",
        "research_window_start": "2020-01-01",
        "research_window_end": "2025-12-31",
    }

    assert verify_paper_risk_binding(deployment) == snapshot
    deployment["research_source_id"] = "other"
    with pytest.raises(PaperRiskBindingError, match="binding changed"):
        verify_paper_risk_binding(deployment)


def test_promotion_binding_rejects_idor_and_revocation(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    promotion_id, params_hash = _seed_approved_promotion(
        tmp_path / "experiment.db"
    )
    monkeypatch.setattr(
        ResearchWorkflowService,
        "_promotion_blockers",
        _no_blockers,
    )
    service = ResearchWorkflowService(tmp_path / "experiment.db")
    verified = asyncio.run(
        service.verify_deployment_binding(
            promotion_id,
            owner_user_id=7,
            experiment_id=1,
            strategy_id="test_strategy",
            params_hash=params_hash,
            model_artifact_id=None,
        )
    )
    assert verified["promotion_id"] == promotion_id
    assert verified["manifest_hash"]

    with pytest.raises(WorkflowError) as idor:
        asyncio.run(
            service.verify_deployment_binding(
                promotion_id,
                owner_user_id=8,
                experiment_id=1,
                strategy_id="test_strategy",
                params_hash=params_hash,
                model_artifact_id=None,
            )
        )
    assert idor.value.code == "promotion_binding_not_found"

    with sqlite3.connect(tmp_path / "experiment.db") as connection:
        connection.execute(
            """
            UPDATE research_promotions
            SET status='revoked', version=version + 1
            WHERE id=?
            """,
            (promotion_id,),
        )
        connection.commit()
    with pytest.raises(WorkflowError) as revoked:
        asyncio.run(
            service.verify_deployment_binding(
                promotion_id,
                owner_user_id=7,
                experiment_id=1,
                strategy_id="test_strategy",
                params_hash=params_hash,
                model_artifact_id=None,
            )
        )
    assert revoked.value.code == "promotion_not_approved"


def test_promotion_binding_rejects_manifest_tamper(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    promotion_id, params_hash = _seed_approved_promotion(
        tmp_path / "experiment.db"
    )
    monkeypatch.setattr(
        ResearchWorkflowService,
        "_promotion_blockers",
        _no_blockers,
    )
    with sqlite3.connect(tmp_path / "experiment.db") as connection:
        connection.execute("DROP TRIGGER trg_research_manifest_no_update")
        connection.execute(
            """
            UPDATE research_run_manifests
            SET manifest_json='{}'
            WHERE experiment_id=1
            """
        )
        connection.commit()
    service = ResearchWorkflowService(tmp_path / "experiment.db")
    with pytest.raises(WorkflowError) as tampered:
        asyncio.run(
            service.verify_deployment_binding(
                promotion_id,
                owner_user_id=7,
                experiment_id=1,
                strategy_id="test_strategy",
                params_hash=params_hash,
                model_artifact_id=None,
            )
        )
    assert tampered.value.code == "promotion_manifest_changed"


def test_paused_source_deployment_can_activate_without_promotion(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    experiment_id, _ = _seed_source_experiment(tmp_path / "experiment.db")
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: _Registry(),
    )
    user = {"id": 7, "is_admin": False, "permissions": ["trading:deploy"]}
    created = asyncio.run(
        trading_api.create_deployment(
            trading_api.CreateDeploymentBody(
                strategy_id="test_strategy",
                source_experiment_id=experiment_id,
                status="paused",
            ),
            user,
        )
    )
    deployment_id = created["data"]["deployment_id"]
    unpromoted = asyncio.run(
        trading_api.update_deployment(
            deployment_id,
            trading_api.UpdateDeploymentBody(status="active"),
            user,
        )
    )
    assert unpromoted["data"]["updated"] is True

    async def fake_resolve(**_kwargs):
        return _binding()

    monkeypatch.setattr(
        deployment_promotion,
        "resolve_deployment_promotion",
        fake_resolve,
    )
    activated = asyncio.run(
        trading_api.update_deployment(
            deployment_id,
            trading_api.UpdateDeploymentBody(
                status="active",
                research_promotion_id=5,
            ),
            user,
        )
    )
    assert activated["data"]["updated"] is True
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        row = connection.execute(
            """
            SELECT status, research_promotion_id, promotion_binding_hash
            FROM deployments WHERE id=?
            """,
            (deployment_id,),
        ).fetchone()
        assert row == ("active", 5, _binding()["binding_hash"])
        with pytest.raises(
            sqlite3.IntegrityError,
            match="promotion binding is immutable",
        ):
            connection.execute(
                """
                UPDATE deployments
                SET promotion_report_hash='tampered'
                WHERE id=?
                """,
                (deployment_id,),
            )


def test_model_evidence_identity_change_is_rejected(monkeypatch) -> None:
    binding = _binding()
    deployment = {
        "user_id": 7,
        "strategy_id": "test_strategy",
        "params_hash": "params",
        "source_experiment_id": 1,
        "source_model_artifact_id": None,
        "research_promotion_id": 5,
        "promotion_version": 3,
        "promotion_report_id": 11,
        "promotion_report_hash": "a" * 64,
        "promotion_manifest_hash": "b" * 64,
        "promotion_model_artifact_id": None,
        "promotion_model_sha256": None,
        "promotion_evidence_hash": None,
        "promotion_binding_hash": binding["binding_hash"],
    }
    changed = {
        **binding,
        "model_sha256": "c" * 64,
        "model_evidence_hash": "d" * 64,
    }
    changed_without_hash = {
        key: value for key, value in changed.items() if key != "binding_hash"
    }
    changed["binding_hash"] = canonical_sha256(changed_without_hash)

    async def fake_resolve(**_kwargs):
        return changed

    monkeypatch.setattr(
        deployment_promotion,
        "resolve_deployment_promotion",
        fake_resolve,
    )
    with pytest.raises(
        DeploymentPromotionError,
        match="differs from its immutable binding",
    ):
        asyncio.run(
            deployment_promotion.verify_deployment_promotion(deployment)
        )


def test_unbound_legacy_deployment_still_rejects_missing_prices(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        deployment_id = connection.execute(
            """
            INSERT INTO deployments
                (user_id, strategy_id, strategy_category, display_name,
                 params, params_hash, mode, status, pool_preset)
            VALUES (7, 'test_strategy', 'technical', 'Unsafe',
                    '{}', 'params', 'batch', 'active', 'csi300')
            """
        ).lastrowid
        portfolio_id = connection.execute(
            """
            INSERT INTO portfolios
                (user_id, name, total_capital, allocations, status,
                 cash_balance, current_revision)
            VALUES (7, 'Paper', 100000, '[]', 'active', 100000, 1)
            """
        ).lastrowid
        connection.execute(
            """
            INSERT INTO portfolio_allocations
                (portfolio_id, deployment_id, target_weight_bps,
                 min_weight_bps, max_weight_bps, locked, revision)
            VALUES (?, ?, 10000, 0, 10000, 0, 1)
            """,
            (portfolio_id, deployment_id),
        )
        connection.commit()

    with pytest.raises(
        (
            FileNotFoundError,
            LegacyAdjustedCacheError,
            DailyMarketDataQualityError,
            GenerationManifestError,
        )
    ):
        asyncio.run(
            simulation.run_daily_simulation(
                7,
                "2026-07-24",
                portfolio_id=portfolio_id,
            )
        )
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM orders"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM simulation_runs"
        ).fetchone()[0] == "failed"


def test_revocation_before_commit_rolls_back_simulation(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    stored = _binding()
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        deployment_id = connection.execute(
            """
            INSERT INTO deployments
                (user_id, strategy_id, strategy_category, display_name,
                 params, params_hash, mode, status, pool_preset,
                 source_experiment_id, research_promotion_id,
                 promotion_version, promotion_report_id,
                 promotion_report_hash, promotion_manifest_hash,
                 promotion_binding_hash)
            VALUES (7, 'test_strategy', 'technical', 'Promoted',
                    '{}', 'params', 'batch', 'active', 'csi300',
                    1, 5, 3, 11, ?, ?, ?)
            """,
            (
                "a" * 64,
                "b" * 64,
                stored["binding_hash"],
            ),
        ).lastrowid
        portfolio_id = connection.execute(
            """
            INSERT INTO portfolios
                (user_id, name, total_capital, allocations, status,
                 cash_balance, current_revision)
            VALUES (7, 'Paper', 100000, '[]', 'active', 100000, 1)
            """
        ).lastrowid
        connection.execute(
            """
            INSERT INTO portfolio_allocations
                (portfolio_id, deployment_id, target_weight_bps,
                 min_weight_bps, max_weight_bps, locked, revision)
            VALUES (?, ?, 10000, 0, 10000, 0, 1)
            """,
            (portfolio_id, deployment_id),
        )
        connection.commit()

    panel = pd.DataFrame(
        {
            ("000001", "open"): [10.0, 11.0],
            ("000001", "close"): [10.5, 11.5],
        },
        index=pd.to_datetime(["2026-07-23", "2026-07-24"]),
    )
    panel.columns = pd.MultiIndex.from_tuples(panel.columns)

    async def fake_load_pivot(*_args, **_kwargs):
        return panel

    calls = 0

    async def revoke_between_checks(_deployment):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DeploymentPromotionError(
                "promotion_not_approved",
                "Research promotion was revoked before commit",
            )
        return stored

    class _NoSignalStrategy:
        def generate_batch_signals(
            self,
            _pivot,
            _params,
            _start_date,
            end_date,
        ):
            return {end_date: []}

    class _NoSignalRegistry:
        def create_strategy(self, _strategy_id):
            return _NoSignalStrategy()

    monkeypatch.setattr(simulation, "_load_pivot", fake_load_pivot)
    monkeypatch.setattr(simulation, "get_registry", _NoSignalRegistry)
    monkeypatch.setattr(
        simulation,
        "verify_deployment_promotion",
        revoke_between_checks,
    )
    with pytest.raises(
        DeploymentPromotionError,
        match="revoked before commit",
    ):
        asyncio.run(
            simulation.run_daily_simulation(
                7,
                "2026-07-24",
                portfolio_id=portfolio_id,
            )
        )
    assert calls == 2
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        for table in (
            "orders",
            "daily_signals",
            "position_snapshots",
            "nav_history",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM simulation_runs"
        ).fetchone()[0] == "failed"
