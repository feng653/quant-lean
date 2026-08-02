from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any

import aiosqlite
from fastapi import HTTPException
import pandas as pd
import pytest

from backend.api import research as research_api
from backend.api.schemas import ResearchRerunBody
from backend.config import settings
from backend.data.lineage import build_universe_snapshot
from backend.data.market_quality import audit_market_data
from backend.data.versioning import DatasetVersion
from backend.db.migrate import migrate_experiment
from backend.services import research_manifest as manifest_service
from backend import version as runtime_version
from backend.services.experiment_eligibility import (
    assess_experiment_eligibility,
)
from backend.services.research_manifest import (
    ManifestConflictError,
    ManifestSecurityError,
    build_run_manifest,
    canonical_sha256,
    expected_replay_differences,
    load_run_manifest,
    persist_initial_manifest,
    resolve_execution_payload,
)
from backend.strategies.base import PortfolioSignalMode


class _Strategy:
    portfolio_signal_mode = "target_weights"


@pytest.fixture(autouse=True)
def isolated_pit_manifest_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manifest tests isolate replay semantics from production PIT storage."""

    async def ready(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(pool_id=kwargs.get("pool_id", "csi300"))

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        ready,
    )


class _Registry:
    def create_strategy(self, strategy_id: str) -> _Strategy:
        if strategy_id != "test_strategy":
            raise KeyError(strategy_id)
        return _Strategy()

    def get_metadata(self, strategy_id: str) -> SimpleNamespace:
        self.create_strategy(strategy_id)
        return SimpleNamespace(
            strategy_id=strategy_id,
            version="1.2.3",
        )


class _Broker:
    def __init__(self, delay: float = 0) -> None:
        self.calls = 0
        self.delay = delay

    async def submit_job(self, **_: Any) -> str:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return "job-exact-1"


def _environment(*, sha: str = "a" * 40, dirty: bool = False) -> dict[str, Any]:
    return {
        "git": {
            "sha": sha,
            "dirty": dirty,
            "tracked_dirty": dirty,
            "untracked_runtime_file_count": 0,
        },
        "strategy": {
            "strategy_id": "test_strategy",
            "version": "1.2.3",
            "source_sha256": "b" * 64,
        },
        "python": {"version": "3.11.9", "implementation": "CPython"},
        "platform": {"system": "Windows", "release": "11", "machine": "AMD64"},
        "dependencies": {
            "lock_file": {"name": "requirements.txt", "sha256": "c" * 64},
            "packages": {"pandas": "2.2.3"},
        },
        "devices": {
            "cpu": {
                "architecture": "AMD64",
                "logical_cores": 8,
                "processor": "test-cpu",
            },
            "gpu": {"backend": "none", "available": False, "devices": []},
        },
    }


def _manifest(
    *,
    experiment_id: int = 1,
    params: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    with_snapshots: bool = False,
    data_access_policy: str = "cache_only",
    bitemporal: bool = False,
    research_trust: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strategy = _Strategy()
    resolved_params = params or {"max_positions": 12, "seed": 7}
    dataset = DatasetVersion(
        digest="d" * 64,
        rows=100,
        columns=10,
        start="2020-01-02",
        end="2024-12-31",
        context_digest="e" * 64,
    )
    universe = build_universe_snapshot(
        "csi300",
        ["000001", "000002"],
        requested_as_of="2024-01-02",
        source_as_of="2024-01-02",
        point_in_time=True,
        timeline_identity={
            "schema_version": "point-in-time-timeline-identity/v1",
            "timeline_hash": "a" * 64,
            "source_batches": (
                [
                    {
                        "batch_id": "tushare-research-fixture",
                        "batch_digest": research_trust["evidence"][
                            "candidate_report_sha256"
                        ],
                    }
                ]
                if research_trust is not None
                else ["fixture-batch"]
            ),
            **(
                {
                    "as_known_at": "2025-01-01T00:00:00Z",
                    "bitemporal_availability_verified": True,
                }
                if bitemporal
                else {}
            ),
        },
    )
    execution = resolve_execution_payload(strategy, resolved_params)
    execution["canonical_price_binding"] = {
        "binding_id": "bind_" + "b" * 32,
        "binding_digest": "c" * 64,
        **(
            {
                "as_known_at": "2025-01-01T00:00:00Z",
                "bitemporal_availability_verified": True,
                "price_role_usage": {
                    "signal_and_research_features": "research_adjusted",
                    "execution_fills_and_valuation": "raw_execution",
                    "mixed_role_fallback_allowed": False,
                },
            }
            if bitemporal
            else {}
        ),
    }
    dataset_snapshot = (
        _snapshot_evidence("pivot", "1" * 64)
        if with_snapshots
        else None
    )
    benchmark = {
        "code": "000300",
        "available": True,
        "sha256": "f" * 64,
        "fetch_start": "2023-12-20",
        "fetch_end": "2024-12-31",
    }
    if with_snapshots:
        benchmark["snapshot"] = _snapshot_evidence(
            "benchmark",
            "2" * 64,
            series=True,
        )
    quality_frame = pd.DataFrame(
        {
            ("000001", "open"): [10.0],
            ("000001", "high"): [11.0],
            ("000001", "low"): [9.0],
            ("000001", "close"): [10.5],
            ("000001", "volume"): [1000.0],
        },
        index=pd.DatetimeIndex(["2024-12-31"], name="date"),
    )
    quality_frame.columns = pd.MultiIndex.from_tuples(
        quality_frame.columns,
        names=["code", "field"],
    )
    return build_run_manifest(
        experiment={
            "id": experiment_id,
            "strategy_id": "test_strategy",
            "mode": "batch",
            "data_access_policy": data_access_policy,
            "train_start": "2020-01-02",
            "train_end": "2023-12-29",
            "test_start": "2024-01-02",
            "test_end": "2024-12-31",
        },
        strategy=strategy,
        strategy_metadata=_Registry().get_metadata("test_strategy"),
        params=resolved_params,
        dataset_version=dataset,
        universe_snapshot=universe,
        benchmark=benchmark,
        market_data_quality=audit_market_data(
            quality_frame,
            test_end="2024-12-31",
            source="tushare" if research_trust is not None else "akshare",
            price_adjustment="qfq",
        ),
        dataset_snapshot=dataset_snapshot,
        environment=environment or _environment(),
        execution=execution,
        research_trust=research_trust,
    )


def _snapshot_evidence(
    kind: str,
    key: str,
    *,
    series: bool = False,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema_version": "research-data-snapshot/v1",
        "kind": kind,
        "key": key,
        "relative_key": f"{kind}/{key}.parquet",
        "file_sha256": key,
        "size_bytes": 123,
        "format": "parquet",
        "schema": {
            "schema_version": "research-parquet-schema/v1",
            "rows": 10,
            "columns": 2,
            "sha256": "3" * 64,
        },
    }
    if series:
        evidence["series"] = {"name": "close", "dtype": "float64"}
    return evidence


def _initialize_database(path: Path) -> None:
    async def initialize() -> None:
        async with aiosqlite.connect(str(path)) as connection:
            await connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT,
                    strategy_id TEXT NOT NULL,
                    strategy_category TEXT NOT NULL,
                    pool_preset TEXT,
                    pool_custom_codes TEXT,
                    pool_industries TEXT,
                    train_start TEXT,
                    train_end TEXT,
                    test_start TEXT NOT NULL,
                    test_end TEXT NOT NULL,
                    params TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    mode TEXT DEFAULT 'batch',
                    requires_training INTEGER DEFAULT 0,
                    retrain_frequency TEXT,
                    status TEXT DEFAULT 'pending',
                    error_log TEXT,
                    progress_pct REAL DEFAULT 0,
                    progress_message TEXT
                );
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                """
            )
            await migrate_experiment(connection)
            await connection.commit()

    asyncio.run(initialize())


def _insert_experiment(
    path: Path,
    *,
    experiment_id: int = 1,
    user_id: int = 7,
    status: str = "completed",
    data_access_policy: str | None = None,
) -> None:
    params = {"max_positions": 12, "seed": 7}
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO experiments
                (id, user_id, name, strategy_id, strategy_category,
                 pool_preset, train_start, train_end, test_start, test_end,
                 params, params_hash, mode, requires_training,
                 retrain_frequency, status, run_spec)
            VALUES (?, ?, 'source', 'test_strategy', 'ml', 'csi300',
                    '2020-01-02', '2023-12-29', '2024-01-02', '2024-12-31',
                    ?, ?, 'batch', 1, 'never', ?, ?)
            """,
            (
                experiment_id,
                user_id,
                json.dumps(params),
                canonical_sha256(params),
                status,
                (
                    json.dumps(
                        {"data_access_policy": data_access_policy},
                        sort_keys=True,
                    )
                    if data_access_policy is not None
                    else None
                ),
            ),
        )


def test_tushare_conditional_trust_is_manifested_and_never_promotable() -> None:
    trust = {
        "schema_version": "tushare-research-trust/v1",
        "profile": "tushare_research_trusted",
        "trust_tier": "conditional_personal_research",
        "eligible": True,
        "warning_severity": "high",
        "evidence": {"candidate_report_sha256": "d" * 64},
        "claims": {
            "eligible_for_paper_trading": True,
            "eligible_for_live_trading": False,
        },
        "known_limitations": [
            "historical_available_at_not_proven",
            "production_dual_price_ledger_not_authorized",
        ],
    }
    manifest = _manifest(research_trust=trust)

    assert manifest["research_trust"] == trust
    assert manifest["pit_runtime"]["verified"] is False
    assert manifest["pit_runtime"]["production_eligible"] is False
    assert manifest["pit_runtime"]["paper_trading_eligible"] is True
    assert "historical_available_at_not_proven" in manifest["research_risk_warnings"]
    eligibility = assess_experiment_eligibility(
        experiment_id=1,
        strategy_id="test_strategy",
        manifest_json=json.dumps(manifest, sort_keys=True),
        manifest_hash=canonical_sha256(manifest),
        schema_version=manifest["schema_version"],
    )
    assert eligibility.eligible is True
    assert eligibility.code == "tushare_research_paper_verified_with_warnings"
    assert set(eligibility.warnings) >= {
        "historical_available_at_not_proven",
        "production_dual_price_ledger_not_certified",
        "manual_research_approval_missing_or_optional",
        "live_trading_not_eligible",
    }


def test_canonical_manifest_and_actual_execution_semantics() -> None:
    first = _manifest(params={"seed": 7, "max_positions": 12})
    second = _manifest(params={"max_positions": 12, "seed": 7})

    assert canonical_sha256(first) == canonical_sha256(second)
    assert first["strategy"]["source_sha256"] == "b" * 64
    assert first["parameters"]["sha256"] == canonical_sha256(
        {"seed": 7, "max_positions": 12}
    )
    assert first["windows"]["train_start"] == "2020-01-02"
    assert first["determinism"]["random_seed"] == 7
    assert first["execution"]["portfolio_signal_mode"] == "target_weights"
    assert first["execution"]["signal_timing"].endswith("next_session_open")
    assert first["dataset"]["digest"] == "d" * 64
    assert first["universe"]["snapshot_hash"]
    assert first["research_risk_warnings"] == []
    assert first["pit_runtime"]["verified"] is True


def test_bitemporal_binding_and_price_roles_are_bound_into_manifest() -> None:
    manifest = _manifest(bitemporal=True)

    assert manifest["pit_runtime"]["bitemporal_verified"] is True
    assert manifest["pit_runtime"]["price_role_usage_verified"] is True
    assert manifest["pit_runtime"]["production_eligible"] is True
    assert manifest["execution"]["canonical_price_binding"][
        "price_role_usage"
    ]["signal_and_research_features"] == "research_adjusted"
    assert manifest["execution"]["canonical_price_binding"][
        "price_role_usage"
    ]["execution_fills_and_valuation"] == "raw_execution"


def test_only_intact_pit_manifest_is_reusable() -> None:
    manifest = _manifest()
    eligible = assess_experiment_eligibility(
        experiment_id=1,
        strategy_id="test_strategy",
        manifest_json=json.dumps(manifest),
        manifest_hash=canonical_sha256(manifest),
        schema_version=manifest["schema_version"],
    )
    assert eligible.eligible is True
    assert eligible.code == "pit_manifest_verified"

    tampered = deepcopy(manifest)
    tampered["pit_runtime"]["canonical_price_binding_digest"] = "f" * 64
    cross_binding = assess_experiment_eligibility(
        experiment_id=1,
        strategy_id="test_strategy",
        manifest_json=json.dumps(tampered),
        manifest_hash=canonical_sha256(tampered),
        schema_version=tampered["schema_version"],
    )
    assert cross_binding.eligible is False
    assert cross_binding.code == "pit_runtime_binding_invalid"

    legacy = _manifest(data_access_policy="allow_fetch")
    legacy_result = assess_experiment_eligibility(
        experiment_id=1,
        strategy_id="test_strategy",
        manifest_json=json.dumps(legacy),
        manifest_hash=canonical_sha256(legacy),
        schema_version=legacy["schema_version"],
    )
    assert legacy_result.eligible is False
    assert legacy_result.code == "legacy_data_policy"


def test_manifest_hash_and_identity_are_rechecked_for_candidate_reuse() -> None:
    manifest = _manifest()
    bad_hash = assess_experiment_eligibility(
        experiment_id=1,
        strategy_id="test_strategy",
        manifest_json=json.dumps(manifest),
        manifest_hash="0" * 64,
        schema_version=manifest["schema_version"],
    )
    assert bad_hash.code == "manifest_integrity_invalid"
    wrong_identity = assess_experiment_eligibility(
        experiment_id=2,
        strategy_id="test_strategy",
        manifest_json=json.dumps(manifest),
        manifest_hash=canonical_sha256(manifest),
        schema_version=manifest["schema_version"],
    )
    assert wrong_identity.code == "manifest_identity_mismatch"


def test_manifest_records_portable_snapshot_evidence() -> None:
    manifest = _manifest(with_snapshots=True)

    assert manifest["dataset"]["snapshot"]["relative_key"].startswith(
        "pivot/"
    )
    assert manifest["benchmark"]["snapshot"]["relative_key"].startswith(
        "benchmark/"
    )
    assert "C:\\" not in json.dumps(manifest)
    assert "/tmp/" not in json.dumps(manifest)


def test_execution_overrides_are_resolved_once_for_engine_and_manifest() -> None:
    payload = resolve_execution_payload(
        _Strategy(),
        {
            "max_positions": 8,
            "execution": {
                "initial_capital": 2_000_000,
                "portfolio_signal_mode": "event_orders",
                "cost_model": {"commission_rate": 0.0001},
                "execution_constraints": {
                    "lot_size": 200,
                    "volume_participation": 0.1,
                },
            },
        },
    )

    assert payload["initial_capital"] == 2_000_000
    assert payload["max_positions"] == 8
    assert payload["portfolio_signal_mode"] == "event_orders"
    assert payload["cost_model"]["commission_rate"] == 0.0001
    assert payload["execution_constraints"] == {
        "volume_participation": 0.1,
        "lot_size": 200,
    }


def test_execution_payload_accepts_strategy_signal_mode_enum() -> None:
    strategy = _Strategy()
    strategy.portfolio_signal_mode = PortfolioSignalMode.TARGET_WEIGHTS

    payload = resolve_execution_payload(strategy, {})

    assert payload["portfolio_signal_mode"] == "target_weights"


@pytest.mark.parametrize(
    "params",
    [
        {"api_token": "must-not-persist"},
        {"model_file": r"C:\private\model.bin"},
        {"model_file": "/private/model.bin"},
    ],
)
def test_manifest_rejects_secrets_and_absolute_paths(
    params: dict[str, Any],
) -> None:
    with pytest.raises(ManifestSecurityError):
        _manifest(params=params)


def test_git_dirty_ignores_untracked_docs_but_not_runtime_sources(
    monkeypatch,
) -> None:
    untracked = ["机器学习功能升级.md", "策略.md"]

    def fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return "a" * 40
        if args[0] == "status":
            return ""
        if args[0] == "ls-files":
            return "\n".join(untracked)
        raise AssertionError(args)

    monkeypatch.setattr(runtime_version, "_run_git", fake_git)
    state = runtime_version._capture_worktree_state()
    assert state["dirty"] is False
    assert state["untracked_runtime_file_count"] == 0

    untracked.append("backend/new_strategy.py")
    state = runtime_version._capture_worktree_state()
    assert state["dirty"] is True
    assert state["untracked_runtime_file_count"] == 1


def test_manifest_keeps_process_start_identity_after_head_changes(
    monkeypatch,
) -> None:
    runtime_identity = runtime_version.runtime_code_identity()

    def changed_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return "f" * 40
        if args[0] == "status":
            return " M backend/main.py"
        if args[0] == "ls-files":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(runtime_version, "_run_git", changed_git)
    environment = manifest_service.capture_runtime_environment(
        _Strategy(),
        _Registry().get_metadata("test_strategy"),
    )
    manifest = _manifest(environment=environment)

    assert manifest["environment"]["git"] == runtime_identity
    assert manifest["environment"]["git"]["sha"] != "f" * 40
    assert manifest["environment"]["observed_worktree_drift"]["detected"] is True
    assert (
        manifest_service.code_version(manifest["environment"]["git"])
        == runtime_version.runtime_code_version()
    )


def test_manifest_persistence_is_atomic_immutable_and_tamper_evident(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "experiment.db"
    _initialize_database(db_path)
    _insert_experiment(db_path)
    manifest = _manifest()

    first = asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=manifest,
        )
    )
    second = asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=manifest,
        )
    )
    assert first["manifest_hash"] == second["manifest_hash"]
    with sqlite3.connect(db_path) as connection:
        code_version = connection.execute(
            "SELECT code_version FROM experiments WHERE id=1"
        ).fetchone()[0]
        assert code_version == "a" * 40
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE research_run_manifests
                SET manifest_hash='tampered' WHERE experiment_id=1
                """
            )

        connection.execute("DROP TRIGGER trg_research_manifest_no_update")
        connection.execute(
            """
            UPDATE research_run_manifests
            SET manifest_json='{}' WHERE experiment_id=1
            """
        )
        connection.commit()

    with pytest.raises(ManifestConflictError):
        asyncio.run(load_run_manifest(db_path, 1))
    with pytest.raises(ManifestConflictError):
        asyncio.run(
            persist_initial_manifest(
                db_path=db_path,
                experiment_id=1,
                user_id=7,
                manifest=manifest,
            )
        )


def test_same_experiment_rejects_a_different_manifest(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _initialize_database(db_path)
    _insert_experiment(db_path)
    original = _manifest()
    asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=original,
        )
    )
    changed = deepcopy(original)
    changed["parameters"]["canonical"]["seed"] = 8
    changed["parameters"]["sha256"] = canonical_sha256(
        changed["parameters"]["canonical"]
    )

    with pytest.raises(ManifestConflictError) as conflict:
        asyncio.run(
            persist_initial_manifest(
                db_path=db_path,
                experiment_id=1,
                user_id=7,
                manifest=changed,
            )
        )
    assert any(
        item["field"] == "parameters.canonical.seed"
        for item in conflict.value.differences
    )


def test_replay_input_hashes_fail_closed_before_backtest() -> None:
    manifest = _manifest()
    manifest["replay"] = {"source_manifest_hash": "source"}
    expected = {
        "source_manifest_hash": "source",
        "dataset_digest": "changed",
        "universe_snapshot_hash": manifest["universe"]["snapshot_hash"],
        "benchmark_sha256": manifest["benchmark"]["sha256"],
        "market_data_quality_sha256": manifest["market_data_quality"][
            "content_sha256"
        ],
        "environment_sha256": canonical_sha256(manifest["environment"]),
        "allow_environment_drift": False,
    }

    differences = expected_replay_differences(manifest, expected)
    assert differences == [{
        "field": "replay.dataset_digest",
        "expected": "changed",
        "actual": "d" * 64,
    }]


def test_manifest_api_enforces_owner_and_admin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "experiment.db"
    _initialize_database(db_path)
    _insert_experiment(db_path)
    asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=_manifest(),
        )
    )
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(db_path))

    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(
            research_api.get_research_manifest(
                1,
                {"id": 8, "is_admin": False},
            )
        )
    assert forbidden.value.status_code == 403
    response = asyncio.run(
        research_api.get_research_manifest(
            1,
            {"id": 99, "is_admin": True},
        )
    )
    assert response["data"]["manifest_hash"]


def test_rerun_rejects_environment_drift_and_is_idempotent_when_allowed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "experiment.db"
    _initialize_database(db_path)
    _insert_experiment(db_path)
    asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=_manifest(),
        )
    )
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(db_path))
    monkeypatch.setattr(
        research_api,
        "capture_runtime_environment",
        lambda *_: _environment(sha="9" * 40),
    )
    user = {"id": 7, "is_admin": False}
    registry = _Registry()
    broker = _Broker()

    with pytest.raises(HTTPException) as drift:
        asyncio.run(
            research_api.rerun_research_experiment(
                1,
                ResearchRerunBody(idempotency_key="exact-key-001"),
                user,
                registry,
                broker,
            )
        )
    assert drift.value.status_code == 409
    assert drift.value.detail["code"] == "environment_drift"

    body = ResearchRerunBody(
        idempotency_key="drift-key-001",
        allow_environment_drift=True,
    )
    first = asyncio.run(
        research_api.rerun_research_experiment(
            1,
            body,
            user,
            registry,
            broker,
        )
    )
    second = asyncio.run(
        research_api.rerun_research_experiment(
            1,
            body,
            user,
            registry,
            broker,
        )
    )
    assert first == second
    assert first["data"]["replay_mode"] == "environment_drift_allowed"
    assert broker.calls == 1
    with sqlite3.connect(db_path) as connection:
        clone_count = connection.execute(
            "SELECT COUNT(*) FROM experiments WHERE source_experiment_id=1"
        ).fetchone()[0]
        drift_json, run_spec = connection.execute(
            """
            SELECT r.environment_drift_json, e.run_spec
            FROM research_rerun_requests r
            JOIN experiments e ON e.id=r.new_experiment_id
            WHERE r.idempotency_key='drift-key-001'
            """
        ).fetchone()
    assert clone_count == 1
    assert json.loads(drift_json)
    legacy_replay = json.loads(run_spec)["research_replay"]
    assert legacy_replay["allow_environment_drift"] is True
    assert "dataset_snapshot" not in legacy_replay


def test_rerun_rejects_legacy_manifest_without_quality_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "experiment.db"
    _initialize_database(db_path)
    _insert_experiment(db_path)
    legacy_manifest = _manifest()
    legacy_manifest.pop("market_data_quality")
    asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=legacy_manifest,
        )
    )
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(db_path))

    with pytest.raises(HTTPException) as blocked:
        asyncio.run(
            research_api.rerun_research_experiment(
                1,
                ResearchRerunBody(idempotency_key="legacy-key-001"),
                {"id": 7, "is_admin": False},
                _Registry(),
                _Broker(),
            )
        )

    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == (
        "legacy_experiment_rerun_forbidden"
    )
    assert blocked.value.detail["eligibility_code"] == (
        "market_data_quality_invalid"
    )


def test_rerun_carries_verified_snapshot_and_universe_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "experiment.db"
    _initialize_database(db_path)
    _insert_experiment(db_path, data_access_policy="cache_only")
    source_manifest = _manifest(
        with_snapshots=True,
        data_access_policy="cache_only",
    )
    asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=source_manifest,
        )
    )
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(db_path))
    monkeypatch.setattr(
        research_api,
        "capture_runtime_environment",
        lambda *_: _environment(),
    )

    asyncio.run(
        research_api.rerun_research_experiment(
            1,
            ResearchRerunBody(idempotency_key="snapshot-key-001"),
            {"id": 7, "is_admin": False},
            _Registry(),
            _Broker(),
        )
    )

    with sqlite3.connect(db_path) as connection:
        raw_spec = connection.execute(
            """
            SELECT run_spec FROM experiments
            WHERE source_experiment_id=1
            """
        ).fetchone()[0]
    persisted_spec = json.loads(raw_spec)
    assert persisted_spec["data_access_policy"] == "cache_only"
    replay = persisted_spec["research_replay"]
    assert replay["dataset_snapshot"] == source_manifest["dataset"]["snapshot"]
    assert replay["benchmark"] == source_manifest["benchmark"]
    assert replay["universe"] == source_manifest["universe"]
    assert replay["market_data_quality"] == (
        source_manifest["market_data_quality"]
    )
    assert replay["market_data_quality_sha256"] == (
        source_manifest["market_data_quality"]["content_sha256"]
    )


def test_concurrent_idempotency_key_dispatches_only_one_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "experiment.db"
    _initialize_database(db_path)
    _insert_experiment(db_path)
    asyncio.run(
        persist_initial_manifest(
            db_path=db_path,
            experiment_id=1,
            user_id=7,
            manifest=_manifest(),
        )
    )
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(db_path))
    monkeypatch.setattr(
        research_api,
        "capture_runtime_environment",
        lambda *_: _environment(),
    )
    broker = _Broker(delay=0.15)
    body = ResearchRerunBody(idempotency_key="concurrent-key-001")
    user = {"id": 7, "is_admin": False}

    async def scenario() -> list[dict[str, Any]]:
        return await asyncio.gather(
            research_api.rerun_research_experiment(
                1,
                body,
                user,
                _Registry(),
                broker,
            ),
            research_api.rerun_research_experiment(
                1,
                body,
                user,
                _Registry(),
                broker,
            ),
        )

    first, second = asyncio.run(scenario())
    assert first == second
    assert broker.calls == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM experiments WHERE source_experiment_id=1"
        ).fetchone()[0] == 1
