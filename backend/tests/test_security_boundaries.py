from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api import experiments as experiments_api
from backend.api import trading as trading_api
from backend.config import settings
from backend.db.init import init_databases
from backend.strategies.base import (
    RetrainFrequency,
    StrategyCategory,
    StrategyMode,
)
from backend.services.experiment_eligibility import ExperimentEligibility


class _Registry:
    def __init__(
        self,
        *,
        requires_training: bool = False,
        retrain_frequency: RetrainFrequency = RetrainFrequency.NEVER,
    ) -> None:
        self.metadata = SimpleNamespace(
            strategy_id="guarded_strategy",
            display_name="Guarded",
            category=(
                StrategyCategory.ML
                if requires_training
                else StrategyCategory.TECHNICAL
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=requires_training,
            retrain_frequency=retrain_frequency,
        )
    def get_metadata(self, strategy_id: str):
        assert strategy_id == "guarded_strategy"
        return self.metadata

    def validate_params(self, strategy_id: str, params: dict):
        assert strategy_id == "guarded_strategy"
        assert isinstance(params, dict)
        return True, ""


@pytest.fixture(autouse=True)
def isolated_pit_submission_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue atomicity tests use an explicit isolated PIT-ready boundary."""

    async def ready(**kwargs) -> SimpleNamespace:
        return SimpleNamespace(pool_id=kwargs.get("pool_id", "csi300"))

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        ready,
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


def test_deployment_rejects_detached_artifact_and_frequency_mismatch(
    monkeypatch,
) -> None:
    registry = _Registry(
        requires_training=True,
        retrain_frequency=RetrainFrequency.MONTHLY,
    )
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: registry,
    )
    user = {"id": 7, "is_admin": False, "permissions": ["trading:deploy"]}

    detached = trading_api.CreateDeploymentBody(
        strategy_id="guarded_strategy",
        source_model_artifact_id=99,
        retrain_frequency="monthly",
    )
    with pytest.raises(HTTPException) as detached_error:
        asyncio.run(trading_api.create_deployment(detached, user))
    assert detached_error.value.status_code == 422
    assert "来源实验" in str(detached_error.value.detail)

    mismatched = trading_api.CreateDeploymentBody(
        strategy_id="guarded_strategy",
        retrain_frequency="weekly",
    )
    with pytest.raises(HTTPException) as frequency_error:
        asyncio.run(trading_api.create_deployment(mismatched, user))
    assert frequency_error.value.status_code == 422
    assert "expected=monthly" in str(frequency_error.value.detail)


def test_deployment_persists_metadata_derived_lifecycle(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    registry = _Registry(
        requires_training=True,
        retrain_frequency=RetrainFrequency.MONTHLY,
    )
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: registry,
    )

    result = asyncio.run(
        trading_api.create_deployment(
            trading_api.CreateDeploymentBody(
                strategy_id="guarded_strategy",
                status="paused",
            ),
            {"id": 7, "is_admin": False, "permissions": ["trading:deploy"]},
        )
    )
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        row = connection.execute(
            """
            SELECT requires_retraining, retrain_frequency
            FROM deployments WHERE id=?
            """,
            (result["data"]["deployment_id"],),
        ).fetchone()
    assert row == (1, "monthly")


def test_deployment_rejects_parameters_that_do_not_match_source_model(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)

    async def pit_eligible(*_args, **_kwargs) -> ExperimentEligibility:
        return ExperimentEligibility(True, "pit_manifest_verified")

    monkeypatch.setattr(
        "backend.services.experiment_eligibility.load_experiment_eligibility",
        pit_eligible,
    )
    registry = _Registry(
        requires_training=True,
        retrain_frequency=RetrainFrequency.MONTHLY,
    )
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: registry,
    )
    source_params = {"lookback": 10}
    source_json = json.dumps(
        source_params,
        ensure_ascii=False,
        sort_keys=True,
    )
    source_hash = hashlib.md5(source_json.encode()).hexdigest()
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        experiment_id = connection.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category,
                 test_start, test_end, params, params_hash, status)
            VALUES (7, 'Source', 'guarded_strategy', 'ml',
                    '2025-01-01', '2025-12-31', ?, ?, 'completed')
            """,
            (source_json, source_hash),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO equity_curve (experiment_id, date, equity)
            VALUES (?, '2025-01-02', 1000000)
            """,
            (experiment_id,),
        )
        connection.execute(
            """
            INSERT INTO model_artifacts
                (experiment_id, strategy_id, model_file_path,
                 metadata_file_path, params_hash)
            VALUES (?, 'guarded_strategy', 'model.joblib',
                    'model.json', ?)
            """,
            (experiment_id, source_hash),
        )
        connection.commit()

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            trading_api.create_deployment(
                trading_api.CreateDeploymentBody(
                    strategy_id="guarded_strategy",
                    source_experiment_id=experiment_id,
                    params={"lookback": 20},
                    status="paused",
                ),
                {
                    "id": 7,
                    "is_admin": False,
                    "permissions": ["trading:deploy"],
                },
            )
        )
    assert error.value.status_code == 422
    assert "must match the source model" in str(error.value.detail)

    with pytest.raises(HTTPException) as legacy_error:
        asyncio.run(
            trading_api.create_deployment(
                trading_api.CreateDeploymentBody(
                    strategy_id="guarded_strategy",
                    source_experiment_id=experiment_id,
                    params=source_params,
                    status="paused",
                ),
                {
                    "id": 7,
                    "is_admin": False,
                    "permissions": ["trading:deploy"],
                },
            )
        )
    assert legacy_error.value.status_code == 422
    assert "source model artifact verification failed" in str(
        legacy_error.value.detail
    )


def test_deployment_rejects_completed_legacy_experiment(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    registry = _Registry()
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: registry,
    )
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        experiment_id = connection.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category,
                 test_start, test_end, params, params_hash, status)
            VALUES (7, 'Legacy source', 'guarded_strategy', 'technical',
                    '2025-01-01', '2025-12-31', '{}', ?, 'completed')
            """,
            (hashlib.md5(b"{}").hexdigest(),),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO equity_curve (experiment_id, date, equity)
            VALUES (?, '2025-01-02', 1000000)
            """,
            (experiment_id,),
        )
        connection.commit()

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            trading_api.create_deployment(
                trading_api.CreateDeploymentBody(
                    strategy_id="guarded_strategy",
                    source_experiment_id=experiment_id,
                    status="paused",
                ),
                {
                    "id": 7,
                    "is_admin": False,
                    "permissions": ["trading:deploy"],
                },
            )
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == (
        "legacy_experiment_deployment_forbidden"
    )


def test_sweep_rejects_cartesian_product_before_materializing(
    monkeypatch,
) -> None:
    registry = _Registry()
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: registry,
    )
    body = experiments_api.SweepBody(
        strategy_id="guarded_strategy",
        param_grid={f"p{index}": list(range(10)) for index in range(10)},
        test_start="2025-01-01",
        test_end="2025-12-31",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            experiments_api.create_param_sweep(
                body,
                {"id": 7, "is_admin": False},
            )
        )
    assert error.value.status_code == 422
    assert "最多生成 100 个实验" in str(error.value.detail)


@pytest.mark.parametrize(
    ("param_grid", "expected"),
    [
        ({f"p{index}": [1] for index in range(11)}, "最多包含 10 个参数"),
        ({"p0": list(range(101))}, "最多包含 100 个候选值"),
    ],
)
def test_sweep_limits_dimensions_and_values(
    monkeypatch,
    param_grid,
    expected,
) -> None:
    registry = _Registry()
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: registry,
    )
    body = experiments_api.SweepBody(
        strategy_id="guarded_strategy",
        param_grid=param_grid,
        test_start="2025-01-01",
        test_end="2025-12-31",
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            experiments_api.create_param_sweep(
                body,
                {"id": 7, "is_admin": False},
            )
        )
    assert expected in str(error.value.detail)


@pytest.mark.parametrize(
    ("successful_submissions", "expected_sweep", "expected_children"),
    [
        (1, ("running", 1), ["pending", "failed"]),
        (0, ("failed", 2), ["failed", "failed"]),
    ],
)
def test_sweep_submission_failures_reach_consistent_states(
    tmp_path,
    monkeypatch,
    successful_submissions,
    expected_sweep,
    expected_children,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    registry = _Registry()
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: registry,
    )

    class PartialBroker:
        calls = 0

        async def submit_job(self, **kwargs):
            self.calls += 1
            if self.calls > successful_submissions:
                raise RuntimeError("queue unavailable")
            return f"job-{kwargs['params']['experiment_id']}"

    monkeypatch.setattr(
        experiments_api,
        "get_job_broker",
        lambda: PartialBroker(),
    )
    result = asyncio.run(
        experiments_api.create_param_sweep(
            experiments_api.SweepBody(
                strategy_id="guarded_strategy",
                param_grid={"lookback": [10, 20]},
                test_start="2025-01-01",
                test_end="2025-12-31",
            ),
            {"id": 7, "is_admin": False},
        )
    )["data"]

    assert len(result["job_ids"]) == successful_submissions
    assert len(result["failed_experiment_ids"]) == 2 - successful_submissions
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        sweep = connection.execute(
            """
            SELECT status, completed_experiments
            FROM param_sweeps WHERE id=?
            """,
            (result["sweep_id"],),
        ).fetchone()
        children = connection.execute(
            """
            SELECT id, status, error_log
            FROM experiments WHERE id IN (?, ?) ORDER BY id
            """,
            tuple(result["experiment_ids"]),
        ).fetchall()
    assert sweep == expected_sweep
    assert [row[1] for row in children] == expected_children
    for row in children[successful_submissions:]:
        assert row[2].startswith("任务提交失败: RuntimeError")
