from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.api import experiments as experiments_api
from backend.config import settings
from backend.db.migrate import migrate_experiment
from backend.db.init import init_databases
from backend.jobs.broker import JobBroker
from backend.services import research_runtime
from backend.services.experiment_eligibility import ExperimentEligibility
from backend.strategies.base import (
    RetrainFrequency,
    StrategyCategory,
    StrategyMode,
)


class _Registry:
    metadata = SimpleNamespace(
        strategy_id="locked_protocol_strategy",
        display_name="Locked protocol",
        category=StrategyCategory.TECHNICAL,
        supported_modes=[StrategyMode.BATCH],
        requires_training=False,
        retrain_frequency=RetrainFrequency.NEVER,
        params=[],
    )

    def get_metadata(self, strategy_id: str):
        assert strategy_id == self.metadata.strategy_id
        return self.metadata

    def validate_params(self, strategy_id: str, params: dict):
        assert strategy_id == self.metadata.strategy_id
        assert isinstance(params, dict)
        return True, ""


class _RecordingBroker:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit_job(self, **kwargs):
        self.calls.append(kwargs)
        return f"job-{len(self.calls)}"


def _configure(tmp_path, monkeypatch) -> _RecordingBroker:
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
    monkeypatch.setattr(
        "backend.dependencies.get_strategy_registry",
        lambda: _Registry(),
    )

    async def isolated_pit_fixture(**_kwargs):
        return SimpleNamespace(pool_id="csi300")

    # These tests exercise sweep locking/persistence.  Their data dependency
    # is an explicit in-memory PIT fixture; production readiness is covered by
    # test_pit_only_runtime.py and is never bypassed by an application flag.
    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        isolated_pit_fixture,
    )

    async def isolated_experiment_eligibility(
        *_args, **_kwargs
    ) -> ExperimentEligibility:
        return ExperimentEligibility(True, "pit_manifest_verified")

    monkeypatch.setattr(
        experiments_api,
        "load_experiment_eligibility",
        isolated_experiment_eligibility,
    )
    broker = _RecordingBroker()
    monkeypatch.setattr(experiments_api, "get_job_broker", lambda: broker)
    return broker


def _strict_body(*, values: list[int] | None = None):
    return experiments_api.SweepBody(
        strategy_id="locked_protocol_strategy",
        name="Strict sweep",
        param_grid={"lookback": values or [10, 20]},
        pool_preset="csi300",
        validation_start="2024-01-01",
        validation_end="2024-06-30",
        locked_test_start="2024-07-01",
        locked_test_end="2024-12-31",
        data_access_policy="cache_only",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "validation_start": "2024-01-01",
            "validation_end": "2024-07-01",
            "locked_test_start": "2024-07-01",
            "locked_test_end": "2024-12-31",
        },
        {
            "validation_start": "2024-01-01",
            "validation_end": "2024-06-30",
            "locked_test_start": "2024-07-01",
        },
    ],
)
def test_sweep_rejects_overlap_and_half_locked_window(payload) -> None:
    with pytest.raises(ValidationError):
        experiments_api.SweepBody(
            strategy_id="locked_protocol_strategy",
            param_grid={"lookback": [10]},
            **payload,
        )


def test_data_access_policy_is_strict_and_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        experiments_api.CreateExperimentBody(
            name="invalid-policy",
            strategy_id="locked_protocol_strategy",
            pool_preset="csi300",
            test_start="2024-01-01",
            test_end="2024-06-30",
            data_access_policy="offline",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        experiments_api.SweepBody(
            strategy_id="locked_protocol_strategy",
            param_grid={"lookback": [10]},
            test_start="2024-01-01",
            test_end="2024-06-30",
            data_access_policy="cache_only",
            unexpected_policy_override=True,  # type: ignore[call-arg]
        )


def test_strict_sweep_never_leaks_locked_window_to_trials(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _configure(tmp_path, monkeypatch)
    result = asyncio.run(
        experiments_api.create_param_sweep(
            _strict_body(),
            {"id": 7, "is_admin": False},
        )
    )["data"]

    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        sweep = connection.execute(
            """
            SELECT selection_start, selection_end, locked_test_start,
                   locked_test_end, research_trust
            FROM param_sweeps WHERE id=?
            """,
            (result["sweep_id"],),
        ).fetchone()
        trials = connection.execute(
            """
            SELECT test_start, test_end, run_spec
            FROM experiments
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            tuple(result["experiment_ids"]),
        ).fetchall()

    assert sweep == (
        "2024-01-01",
        "2024-06-30",
        "2024-07-01",
        "2024-12-31",
        "locked_test",
    )
    assert result["research_trust"] == "locked_test"
    assert result["data_access_policy"] == "cache_only"
    assert all(row[:2] == ("2024-01-01", "2024-06-30") for row in trials)
    for _, _, run_spec_json in trials:
        run_spec = json.loads(run_spec_json)
        assert run_spec["test_start"] == "2024-01-01"
        assert run_spec["test_end"] == "2024-06-30"
        assert run_spec["data_access_policy"] == "cache_only"
        assert "locked_test_start" not in run_spec
        assert "locked_test_end" not in run_spec
        assert "locked_test" not in run_spec_json
        assert "2024-12-31" not in run_spec_json
    assert all(
        "locked_test_start" not in call["params"]
        and "locked_test_end" not in call["params"]
        for call in broker.calls
    )


def test_tushare_selection_and_locked_test_bind_their_own_runtime_windows(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    generation_id = "a" * 64
    dataset_digest = "b" * 64
    calls: list[dict] = []

    def trust_for(end: str) -> dict:
        timeline = {"timeline_hash": ("c" if end == "2024-06-30" else "d") * 64}
        return {
            "schema_version": "tushare-research-trust/v1",
            "profile": "tushare_research_trusted",
            "eligible": True,
            "warnings": [],
            "known_limitations": [],
            "evidence": {"research_generation_id": generation_id},
            "runtime_binding": {
                "generation_id": generation_id,
                "runtime_dataset_digest": dataset_digest,
                "actual_window": {"start": "2023-01-01", "end": end},
                "timeline_identity": timeline,
            },
        }

    async def bind(**kwargs):
        calls.append(dict(kwargs))
        if kwargs["test_end"] == "2024-07-01":
            raise AssertionError("selection preflight must not read locked data")
        return trust_for(str(kwargs["test_end"]))

    monkeypatch.setattr(experiments_api, "_require_pit_submission", bind)
    body = experiments_api.SweepBody(
        strategy_id="locked_protocol_strategy",
        name="Tushare bound sweep",
        param_grid={"lookback": [10]},
        pool_preset="csi300",
        validation_start="2024-01-01",
        validation_end="2024-06-30",
        locked_test_start="2024-07-01",
        locked_test_end="2024-12-31",
        data_access_policy="cache_only",
        research_trust_profile="tushare_research_trusted",
    )
    created = asyncio.run(
        experiments_api.create_param_sweep(
            body,
            {"id": 7, "is_admin": False},
        )
    )["data"]
    selection_id = created["experiment_ids"][0]
    with sqlite3.connect(tmp_path / "experiment.db") as connection:
        selection_spec = json.loads(
            connection.execute(
                "SELECT run_spec FROM experiments WHERE id=?",
                (selection_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE experiments SET status='completed' WHERE id=?",
            (selection_id,),
        )

    promoted = asyncio.run(
        experiments_api.promote_sweep_experiment(
            created["sweep_id"],
            experiments_api.PromoteSweepBody(experiment_id=selection_id),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    with sqlite3.connect(tmp_path / "experiment.db") as connection:
        locked_spec = json.loads(
            connection.execute(
                "SELECT run_spec FROM experiments WHERE id=?",
                (promoted["experiment_id"],),
            ).fetchone()[0]
        )

    selection_trust = selection_spec["research_trust"]
    locked_trust = locked_spec["research_trust"]
    assert selection_spec["test_end"] == "2024-06-30"
    assert selection_trust["runtime_binding"]["actual_window"]["end"] == (
        "2024-06-30"
    )
    assert locked_spec["test_end"] == "2024-12-31"
    assert locked_trust["runtime_binding"]["actual_window"]["end"] == (
        "2024-12-31"
    )
    assert locked_spec["research_trust_profile"] == "tushare_research_trusted"
    assert calls[0].get("research_generation_id") is None
    assert calls[0]["test_end"] == "2024-06-30"
    assert calls[1]["research_generation_id"] == generation_id
    assert calls[1]["test_end"] == "2024-12-31"

    def market_for(trust: dict) -> dict:
        binding = trust["runtime_binding"]
        return {
            "source_provenance": {"content_sha256": dataset_digest},
            "report": {
                "generation_id": generation_id,
                "date_start": binding["actual_window"]["start"],
                "date_end": binding["actual_window"]["end"],
                "timeline_identity": binding["timeline_identity"],
            },
        }

    research_runtime.verify_research_runtime_binding(
        selection_trust["runtime_binding"], market_for(selection_trust)
    )
    research_runtime.verify_research_runtime_binding(
        locked_trust["runtime_binding"], market_for(locked_trust)
    )
    with pytest.raises(
        research_runtime.ResearchRuntimeError,
        match="worker 实际派生语义不一致",
    ):
        research_runtime.verify_research_runtime_binding(
            selection_trust["runtime_binding"], market_for(locked_trust)
        )


def test_experiment_persists_and_returns_cache_only_policy(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _configure(tmp_path, monkeypatch)
    created = asyncio.run(
        experiments_api.create_experiment(
            experiments_api.CreateExperimentBody(
                name="cache-only",
                strategy_id="locked_protocol_strategy",
                pool_preset="csi300",
                test_start="2024-01-01",
                test_end="2024-06-30",
                data_access_policy="cache_only",
            ),
            {"id": 7, "is_admin": False},
        )
    )["data"]

    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        run_spec = json.loads(
            connection.execute(
                "SELECT run_spec FROM experiments WHERE id=?",
                (created["experiment_id"],),
            ).fetchone()[0]
        )
    detail = asyncio.run(
        experiments_api.get_experiment_detail(
            created["experiment_id"],
            {"id": 7, "is_admin": False},
        )
    )["data"]

    assert run_spec["data_access_policy"] == "cache_only"
    assert detail["data_access_policy"] == "cache_only"
    assert "run_spec" not in detail
    assert broker.calls[0]["params"]["data_access_policy"] == "cache_only"


def test_tushare_industry_selection_is_persisted_with_warning(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(tmp_path, monkeypatch)

    async def warning_only_submission(**_kwargs):
        return {
            "schema_version": "tushare-research-trust/v1",
            "profile": "tushare_research_trusted",
            "eligible": True,
            "warnings": ["single_source_tushare_research"],
        }

    monkeypatch.setattr(
        experiments_api,
        "_require_pit_submission",
        warning_only_submission,
    )
    created = asyncio.run(
        experiments_api.create_experiment(
            experiments_api.CreateExperimentBody(
                name="industry-warning-only",
                strategy_id="locked_protocol_strategy",
                pool_preset="csi300",
                pool_industries=["银行"],
                test_start="2024-01-01",
                test_end="2024-06-30",
                research_trust_profile="tushare_research_trusted",
            ),
            {"id": 7, "is_admin": False},
        )
    )["data"]

    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        industries, run_spec_json = connection.execute(
            "SELECT pool_industries, run_spec FROM experiments WHERE id=?",
            (created["experiment_id"],),
        ).fetchone()
    run_spec = json.loads(run_spec_json)

    assert json.loads(industries) == ["银行"]
    assert set(run_spec["research_trust"]["warnings"]) >= {
        "single_source_tushare_research",
        "industry_filter_uses_current_classification",
        "historical_industry_neutralization_not_proven",
    }


def test_sweep_inherits_baseline_policy_and_rejects_override(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    baseline = asyncio.run(
        experiments_api.create_experiment(
            experiments_api.CreateExperimentBody(
                name="cache-only-baseline",
                strategy_id="locked_protocol_strategy",
                pool_preset="csi300",
                test_start="2024-01-01",
                test_end="2024-06-30",
                data_access_policy="cache_only",
            ),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    inherited = experiments_api.SweepBody(
        strategy_id="locked_protocol_strategy",
        param_grid={"lookback": [10]},
        test_start="2024-01-01",
        test_end="2024-06-30",
        source_experiment_id=baseline["experiment_id"],
    )
    sweep = asyncio.run(
        experiments_api.create_param_sweep(
            inherited,
            {"id": 7, "is_admin": False},
        )
    )["data"]

    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        child_spec, child_source = connection.execute(
            """
            SELECT run_spec, source_experiment_id
            FROM experiments WHERE id=?
            """,
            (sweep["experiment_ids"][0],),
        ).fetchone()
    assert sweep["data_access_policy"] == "cache_only"
    assert json.loads(child_spec)["data_access_policy"] == "cache_only"
    assert child_source == baseline["experiment_id"]

    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(
            experiments_api.create_param_sweep(
                experiments_api.SweepBody(
                    strategy_id="locked_protocol_strategy",
                    param_grid={"lookback": [10]},
                    test_start="2024-01-01",
                    test_end="2024-06-30",
                    data_access_policy="allow_fetch",
                    source_experiment_id=baseline["experiment_id"],
                ),
                {"id": 7, "is_admin": False},
            )
        )
    assert mismatch.value.status_code == 422


def test_sweep_results_name_metrics_as_selection_metrics(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    result = asyncio.run(
        experiments_api.create_param_sweep(
            _strict_body(values=[10]),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    experiment_id = result["experiment_ids"][0]
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        connection.execute(
            "UPDATE experiments SET status='completed' WHERE id=?",
            (experiment_id,),
        )
        connection.execute(
            """
            INSERT INTO experiment_metrics
                (experiment_id, sharpe_ratio, annual_return,
                 max_drawdown, win_rate)
            VALUES (?, 1.5, 0.2, -0.1, 0.6)
            """,
            (experiment_id,),
        )
        connection.commit()

    item = asyncio.run(
        experiments_api.get_sweep_result(
            result["sweep_id"],
            {"id": 7, "is_admin": False},
        )
    )["data"]["experiments"][0]
    assert item["selection_metrics"]["sharpe_ratio"] == 1.5
    assert "sharpe_ratio" not in {
        key for key in item if key != "selection_metrics"
    }
    assert "best_test_metrics" not in item
    assert "final_metrics" not in item
    sweep = asyncio.run(
        experiments_api.get_sweep_result(
            result["sweep_id"],
            {"id": 7, "is_admin": False},
        )
    )["data"]["sweep"]
    assert sweep["data_access_policy"] == "cache_only"


def test_sweep_repairs_only_transient_sqlite_failures_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    created = asyncio.run(
        experiments_api.create_param_sweep(
            _strict_body(),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    repairable_id, strategy_failure_id = created["experiment_ids"]
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        connection.execute(
            """
            UPDATE experiments
            SET status='failed', error_log='OperationalError: database is locked'
            WHERE id=?
            """,
            (repairable_id,),
        )
        connection.execute(
            """
            UPDATE experiments
            SET status='failed', error_log='ValueError: invalid strategy signal'
            WHERE id=?
            """,
            (strategy_failure_id,),
        )
        connection.commit()

    before = asyncio.run(
        experiments_api.get_sweep_result(
            created["sweep_id"],
            {"id": 7, "is_admin": False},
        )
    )["data"]
    assert before["repairable_experiment_ids"] == [repairable_id]
    assert before["experiments"][0]["repairable"] is True

    broker = JobBroker(str(tmp_path / "experiment.db"))
    monkeypatch.setattr(experiments_api, "get_job_broker", lambda: broker)
    repaired = asyncio.run(
        experiments_api.repair_param_sweep(
            created["sweep_id"],
            {"id": 7, "is_admin": False},
        )
    )["data"]
    assert repaired["repaired_experiment_ids"] == [repairable_id]
    assert len(repaired["job_ids"]) == 1

    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        statuses = connection.execute(
            "SELECT id, status, error_log FROM experiments WHERE id IN (?, ?) ORDER BY id",
            (repairable_id, strategy_failure_id),
        ).fetchall()
        queued = connection.execute(
            """
            SELECT resource_id, status FROM jobs
            WHERE job_uuid=?
            """,
            (repaired["job_ids"][0],),
        ).fetchone()
    assert statuses == [
        (repairable_id, "pending", None),
        (strategy_failure_id, "failed", "ValueError: invalid strategy signal"),
    ]
    assert queued == (str(repairable_id), "pending")


def test_sweep_repair_replaces_member_with_immutable_manifest(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    created = asyncio.run(
        experiments_api.create_param_sweep(
            _strict_body(values=[10]),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    original_id = created["experiment_ids"][0]
    broker = JobBroker(str(tmp_path / "experiment.db"))
    prior_job = asyncio.run(
        broker.submit_job(
            "backtest",
            {"experiment_id": original_id, "sweep_id": created["sweep_id"]},
            user_id=7,
            resource_type="experiment",
            resource_id=original_id,
        )
    )
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status='failed', error='OperationalError: database is locked'
            WHERE job_uuid=?
            """,
            (prior_job,),
        )
        connection.execute(
            """
            UPDATE experiments
            SET status='failed',
                error_log='ManifestConflictError: existing run manifest differs'
            WHERE id=?
            """,
            (original_id,),
        )
        connection.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (?, 7, 'research-run-manifest/v1', '{}', 'immutable', '2026-01-01T00:00:00Z')
            """,
            (original_id,),
        )
        connection.commit()

    result = asyncio.run(
        experiments_api.get_sweep_result(
            created["sweep_id"],
            {"id": 7, "is_admin": False},
        )
    )["data"]
    assert result["repairable_experiment_ids"] == [original_id]
    assert result["experiments"][0]["repair_mode"] == "replace"

    monkeypatch.setattr(experiments_api, "get_job_broker", lambda: broker)
    repaired = asyncio.run(
        experiments_api.repair_param_sweep(
            created["sweep_id"],
            {"id": 7, "is_admin": False},
        )
    )["data"]
    replacement_id = repaired["replacement_experiment_ids"][str(original_id)]
    assert replacement_id != original_id

    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        original = connection.execute(
            "SELECT status, error_log FROM experiments WHERE id=?",
            (original_id,),
        ).fetchone()
        replacement = connection.execute(
            """
            SELECT status, error_log, code_version, data_version
            FROM experiments WHERE id=?
            """,
            (replacement_id,),
        ).fetchone()
        membership = connection.execute(
            "SELECT experiment_id FROM sweep_experiments WHERE sweep_id=?",
            (created["sweep_id"],),
        ).fetchone()
        manifest_count = connection.execute(
            "SELECT COUNT(*) FROM research_run_manifests WHERE experiment_id=?",
            (original_id,),
        ).fetchone()[0]
    assert original == (
        "failed",
        "ManifestConflictError: existing run manifest differs",
    )
    assert replacement == ("pending", None, None, None)
    assert membership == (replacement_id,)
    assert manifest_count == 1


def test_promotion_enforces_security_membership_completion_and_idempotency(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _configure(tmp_path, monkeypatch)
    first = asyncio.run(
        experiments_api.create_param_sweep(
            _strict_body(),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    second = asyncio.run(
        experiments_api.create_param_sweep(
            _strict_body(values=[30]),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    completed_member, incomplete_member = first["experiment_ids"]
    nonmember = second["experiment_ids"][0]
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        connection.execute(
            "UPDATE experiments SET status='completed' WHERE id IN (?, ?)",
            (completed_member, nonmember),
        )
        connection.commit()

    with pytest.raises(HTTPException) as cross_user:
        asyncio.run(
            experiments_api.promote_sweep_experiment(
                first["sweep_id"],
                experiments_api.PromoteSweepBody(
                    experiment_id=completed_member
                ),
                {"id": 8, "is_admin": False},
            )
        )
    assert cross_user.value.status_code == 404

    with pytest.raises(HTTPException) as outside_sweep:
        asyncio.run(
            experiments_api.promote_sweep_experiment(
                first["sweep_id"],
                experiments_api.PromoteSweepBody(experiment_id=nonmember),
                {"id": 7, "is_admin": False},
            )
        )
    assert outside_sweep.value.status_code == 422

    with pytest.raises(HTTPException) as incomplete:
        asyncio.run(
            experiments_api.promote_sweep_experiment(
                first["sweep_id"],
                experiments_api.PromoteSweepBody(
                    experiment_id=incomplete_member
                ),
                {"id": 7, "is_admin": False},
            )
        )
    assert incomplete.value.status_code == 409

    broker.calls.clear()
    request = experiments_api.PromoteSweepBody(
        experiment_id=completed_member
    )
    async def promote_twice():
        return await asyncio.gather(
            experiments_api.promote_sweep_experiment(
                first["sweep_id"],
                request,
                {"id": 7, "is_admin": False},
            ),
            experiments_api.promote_sweep_experiment(
                first["sweep_id"],
                request,
                {"id": 7, "is_admin": False},
            ),
        )

    promoted, repeated = asyncio.run(promote_twice())
    promoted = promoted["data"]
    repeated = repeated["data"]

    assert promoted["experiment_id"] == repeated["experiment_id"]
    assert sorted((promoted["created"], repeated["created"])) == [False, True]
    assert len(broker.calls) == 1
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        locked = connection.execute(
            """
            SELECT source_experiment_id, strategy_id, pool_preset,
                   test_start, test_end, params, run_spec
            FROM experiments WHERE id=?
            """,
            (promoted["experiment_id"],),
        ).fetchone()
        promotion_count = connection.execute(
            """
            SELECT COUNT(*) FROM experiments
            WHERE source_experiment_id=?
              AND test_start='2024-07-01'
              AND test_end='2024-12-31'
            """,
            (completed_member,),
        ).fetchone()[0]
    assert locked[:5] == (
        completed_member,
        "locked_protocol_strategy",
        "csi300",
        "2024-07-01",
        "2024-12-31",
    )
    assert json.loads(locked[5]) == {"lookback": 10}
    assert json.loads(locked[6])["data_access_policy"] == "cache_only"
    assert broker.calls[0]["params"]["data_access_policy"] == "cache_only"
    assert promotion_count == 1


def test_legacy_sweep_is_explicitly_marked_unlocked(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    result = asyncio.run(
        experiments_api.create_param_sweep(
            experiments_api.SweepBody(
                strategy_id="locked_protocol_strategy",
                param_grid={"lookback": [10]},
                test_start="2024-01-01",
                test_end="2024-06-30",
            ),
            {"id": 7, "is_admin": False},
        )
    )["data"]
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        row = connection.execute(
            """
            SELECT selection_start, selection_end, locked_test_start,
                   locked_test_end, research_trust
            FROM param_sweeps WHERE id=?
            """,
            (result["sweep_id"],),
        ).fetchone()
    assert row == (
        "2024-01-01",
        "2024-06-30",
        None,
        None,
        "legacy_unlocked",
    )
    assert result["research_trust"] == "legacy_unlocked"


def test_locked_sweep_migration_is_idempotent_under_concurrent_startup(
    tmp_path,
) -> None:
    async def scenario() -> set[str]:
        db_path = tmp_path / "concurrent-migration.db"
        async with aiosqlite.connect(db_path) as connection:
            await connection.executescript(
                """
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                CREATE TABLE experiments (id INTEGER PRIMARY KEY);
                """
            )
            await connection.commit()
        async with (
            aiosqlite.connect(db_path, isolation_level=None) as first,
            aiosqlite.connect(db_path, isolation_level=None) as second,
        ):
            await asyncio.gather(
                migrate_experiment(first),
                migrate_experiment(second),
            )
            cursor = await first.execute("PRAGMA table_info(param_sweeps)")
            return {row[1] for row in await cursor.fetchall()}

    assert {
        "selection_start",
        "selection_end",
        "locked_test_start",
        "locked_test_end",
        "research_trust",
        "promoted_experiment_id",
        "promotion_source_experiment_id",
        "promoted_at",
    } <= asyncio.run(scenario())


def test_migration_backfills_legacy_selection_window(tmp_path) -> None:
    async def scenario() -> tuple:
        db_path = tmp_path / "legacy-sweep.db"
        async with aiosqlite.connect(db_path) as connection:
            await connection.executescript(
                """
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    test_start TEXT NOT NULL,
                    test_end TEXT NOT NULL
                );
                CREATE TABLE param_sweeps (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    strategy_id TEXT NOT NULL,
                    name TEXT,
                    sweep_config TEXT NOT NULL
                );
                CREATE TABLE sweep_experiments (
                    sweep_id INTEGER NOT NULL,
                    experiment_id INTEGER NOT NULL,
                    param_combo TEXT NOT NULL
                );
                INSERT INTO experiments
                    (id, test_start, test_end)
                VALUES (11, '2023-01-01', '2023-06-30');
                INSERT INTO param_sweeps
                    (id, user_id, strategy_id, sweep_config)
                VALUES (3, 7, 'legacy', '{}');
                INSERT INTO sweep_experiments
                    (sweep_id, experiment_id, param_combo)
                VALUES (3, 11, '{}');
                """
            )
            await migrate_experiment(connection)
            await migrate_experiment(connection)
            cursor = await connection.execute(
                """
                SELECT selection_start, selection_end, research_trust
                FROM param_sweeps WHERE id=3
                """
            )
            row = await cursor.fetchone()
            await connection.commit()
            return tuple(row)

    assert asyncio.run(scenario()) == (
        "2023-01-01",
        "2023-06-30",
        "legacy_unlocked",
    )
