from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import aiosqlite
import pytest

from backend.config import settings
from backend.db.migrate import migrate_experiment
from backend.services.ml_promotion_evidence import (
    LEGACY_TRAINING_CONTRACT,
    ML_PROMOTION_EVIDENCE_SCHEMA,
    MLPromotionEvidenceError,
    build_model_promotion_evidence,
    legacy_params_hash,
    verify_experiment_model_promotion_evidence,
)
from backend.services.research_manifest import (
    ARTIFACT_MANIFEST_SCHEMA,
    ManifestError,
    RUN_MANIFEST_SCHEMA,
    canonical_json_bytes,
    canonical_sha256,
)
from backend.services.research_workflow import ResearchWorkflowService
from backend.services.walkforward import TrainCycle, WalkForwardResult
from backend.strategies.factor.alphamaster_gbr import AlphaMasterGBRStrategy
from backend.strategies.ml.alpha158_lgb import Alpha158LGBStrategy
from backend.strategies.ml.alpha158_rank_lgb import Alpha158RankLGBStrategy
from backend.strategies.ml.alpha158_xgb import Alpha158XGBStrategy
from backend.strategies.ml.lstm_rank import LSTMRankStrategy
from backend.strategies.ml.transformer_rank import TransformerRankStrategy


COMPLIANT_STRATEGIES = (
    Alpha158LGBStrategy,
    Alpha158XGBStrategy,
    Alpha158RankLGBStrategy,
    LSTMRankStrategy,
    TransformerRankStrategy,
)
NATIVE_MODEL_TYPES = {
    "alpha158_lgb_v1": "LightGBM",
    "alpha158_xgb_v1": "XGBoost",
    "alpha158_rank_lgb_v1": "LightGBM LambdaRank",
    "lstm_rank_v1": "LSTM (PyTorch)",
    "transformer_rank_v1": "Transformer (PyTorch)",
}
MODEL_TYPE_STATES = {
    "LSTM (PyTorch)": ("lstm", "pytorch", False),
    "MLP (sklearn fallback)": ("mlp_classifier", "sklearn", True),
    "Transformer (PyTorch)": ("transformer", "pytorch", False),
    "RandomForest (sklearn fallback)": (
        "random_forest_classifier",
        "sklearn",
        True,
    ),
}


def _initialize(path: Path) -> None:
    async def scenario() -> None:
        async with aiosqlite.connect(str(path)) as connection:
            await connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    strategy_id TEXT NOT NULL,
                    params TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    train_start TEXT,
                    train_end TEXT,
                    test_start TEXT NOT NULL,
                    test_end TEXT NOT NULL,
                    requires_training INTEGER NOT NULL,
                    retrain_frequency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_spec TEXT
                );
                CREATE TABLE model_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER NOT NULL,
                    strategy_id TEXT NOT NULL,
                    model_version INTEGER NOT NULL,
                    model_file_path TEXT NOT NULL,
                    metadata_file_path TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    train_window_start TEXT,
                    train_window_end TEXT,
                    feature_count INTEGER,
                    train_samples INTEGER,
                    train_metrics TEXT,
                    feature_importance TEXT,
                    artifact_sha256 TEXT,
                    artifact_size INTEGER,
                    run_manifest_hash TEXT,
                    is_latest INTEGER DEFAULT 1
                );
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                CREATE TABLE deployments (id INTEGER PRIMARY KEY);
                CREATE TABLE orders (id INTEGER PRIMARY KEY);
                """
            )
            await migrate_experiment(connection)
            await connection.commit()

    asyncio.run(scenario())


def _manifest(
    *,
    experiment_id: int,
    strategy_id: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "experiment": {
            "experiment_id": experiment_id,
            "strategy_id": strategy_id,
            "mode": "batch",
        },
        "strategy": {"strategy_id": strategy_id, "version": "1.0"},
        "environment": {"git": {"sha": "a" * 40, "dirty": False}},
        "parameters": {
            "canonical": params,
            "sha256": canonical_sha256(params),
        },
        "windows": {
            "train_start": "2020-01-01",
            "train_end": "2020-12-31",
            "test_start": "2022-01-01",
            "test_end": "2024-12-31",
            "data_start": "2020-01-01",
            "data_end": "2024-12-31",
        },
        "dataset": {
            "digest": "d" * 64,
            "context_digest": "c" * 64,
            "rows": 100,
            "columns": 10,
        },
        "universe": {
            "snapshot_hash": "u" * 64,
            "point_in_time": True,
        },
    }


def _walkforward(
    *,
    samples: int = 100,
    rank_ic: float = 0.2,
    failed: bool = False,
    model_type: str = "LightGBM",
) -> WalkForwardResult:
    train_metrics: dict[str, Any] = {
        "n_samples": 500,
        "model_type": model_type,
    }
    implementation_state = MODEL_TYPE_STATES.get(model_type)
    if implementation_state is not None:
        implementation, backend, fallback_used = implementation_state
        train_metrics.update(
            {
                "model_implementation": implementation,
                "model_backend": backend,
                "fallback_used": fallback_used,
            }
        )
    cycle = TrainCycle(
        pred_month="2022-01",
        pred_date="2022-01-04",
        train_start="2020-01-01",
        train_end="2020-12-31",
        validation_start="2021-01-04",
        validation_end="2021-12-31",
        retrained=True,
        label_horizon_days=21,
        embargo_days=2,
        validation_months=12,
        n_train_samples=500,
        n_validation_samples=samples,
        n_train_features=158,
        train_metrics=train_metrics,
        validation_metrics={
            "n_validation_samples": samples,
            "n_validation_candidate_dates": 5,
            "n_validation_dates": 5,
            "min_validation_cross_section_size": 20,
            "validation_ic": 0.1,
            "validation_ic_std": 0.05,
            "validation_icir": 2.0,
            "validation_rank_ic": rank_ic,
            "validation_rank_ic_std": 0.05,
            "validation_rank_icir": rank_ic / 0.05,
            "validation_loss": 0.3,
        },
        error=("RuntimeError: rejected candidate" if failed else None),
    )
    return WalkForwardResult(
        signals={},
        cycles=[cycle],
        last_model=object(),
        last_window=(cycle.train_start, cycle.train_end),
        last_validation_window=(cycle.validation_start, cycle.validation_end),
        elapsed_seconds=1.0,
    )


def _seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    strategy: Any | None = None,
    experiment_id: int = 1,
    evidence_mutator: Any | None = None,
    include_supplement: bool = True,
    duplicate_latest: bool = False,
    walkforward: WalkForwardResult | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    strategy = strategy or Alpha158LGBStrategy()
    metadata = strategy.metadata()
    params = {
        "validation_months": 12,
        "min_validation_rank_ic": 0.02,
        "embargo_days": 2,
        "label_horizon_days": 21,
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / f"experiment-{experiment_id}.db"
    _initialize(db_path)
    model_root = tmp_path / "models"
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(model_root))
    artifact_dir = model_root / f"experiment_{experiment_id}"
    artifact_dir.mkdir(parents=True)
    model_path = artifact_dir / "model_v1.joblib"
    metadata_path = artifact_dir / "model_v1.json"
    model_path.write_bytes(f"model-{experiment_id}".encode())
    wf_result = walkforward or _walkforward(
        model_type=NATIVE_MODEL_TYPES[metadata.strategy_id]
    )
    telemetry = {
        "cycles": [asdict(cycle) for cycle in wf_result.cycles],
        "status": "completed",
    }
    metadata_path.write_text(
        json.dumps(
            {
                "experiment_id": experiment_id,
                "strategy_id": metadata.strategy_id,
                "params": params,
                "training": telemetry,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
    manifest = _manifest(
        experiment_id=experiment_id,
        strategy_id=metadata.strategy_id,
        params=params,
    )
    manifest_hash = canonical_sha256(manifest)
    experiment = {
        "id": experiment_id,
        "user_id": 7,
        "strategy_id": metadata.strategy_id,
        "params": json.dumps(params, ensure_ascii=False),
        "params_hash": legacy_params_hash(params),
        "train_start": "2020-01-01",
        "train_end": "2020-12-31",
        "test_start": "2022-01-01",
        "test_end": "2024-12-31",
        "requires_training": 1,
        "retrain_frequency": metadata.retrain_frequency.value,
        "status": "completed",
    }
    evidence = build_model_promotion_evidence(
        experiment=experiment,
        strategy=strategy,
        strategy_metadata=metadata,
        params=params,
        walkforward_result=wf_result,
        model_version=1,
        model_sha256=model_sha256,
        model_size=model_path.stat().st_size,
        metadata_file_path=str(metadata_path),
        run_manifest_hash=manifest_hash,
        training_telemetry=telemetry,
    )
    if evidence_mutator is not None:
        evidence_mutator(evidence)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO experiments
                (id, user_id, strategy_id, params, params_hash,
                 train_start, train_end, test_start, test_end,
                 requires_training, retrain_frequency, status, run_spec)
            VALUES (:id, :user_id, :strategy_id, :params, :params_hash,
                    :train_start, :train_end, :test_start, :test_end,
                    :requires_training, :retrain_frequency, :status, '{}')
            """,
            experiment,
        )
        connection.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                experiment_id,
                experiment["user_id"],
                RUN_MANIFEST_SCHEMA,
                canonical_json_bytes(manifest).decode(),
                manifest_hash,
            ),
        )
        artifact_values = (
            experiment_id,
            metadata.strategy_id,
            str(model_path),
            str(metadata_path),
            experiment["params_hash"],
            json.dumps(telemetry, sort_keys=True),
            model_sha256,
            model_path.stat().st_size,
            manifest_hash,
        )
        connection.execute(
            """
            INSERT INTO model_artifacts
                (experiment_id, strategy_id, model_version,
                 model_file_path, metadata_file_path, params_hash,
                 train_window_start, train_window_end, train_samples,
                 train_metrics, artifact_sha256, artifact_size,
                 run_manifest_hash, is_latest)
            VALUES (?, ?, 1, ?, ?, ?, '2020-01-01', '2020-12-31', 500,
                    ?, ?, ?, ?, 1)
            """,
            artifact_values,
        )
        if duplicate_latest:
            connection.execute(
                """
                INSERT INTO model_artifacts
                    (experiment_id, strategy_id, model_version,
                     model_file_path, metadata_file_path, params_hash,
                     train_metrics, artifact_sha256, artifact_size,
                     run_manifest_hash, is_latest)
                VALUES (?, ?, 2, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                artifact_values,
            )
        if include_supplement:
            try:
                metadata_json = canonical_json_bytes(evidence).decode()
            except (ValueError, ManifestError):
                # Deliberately seed malformed legacy JSON for fail-closed tests.
                metadata_json = json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=True,
                )
            connection.execute(
                """
                INSERT INTO research_artifact_manifests
                    (experiment_id, run_manifest_hash, schema_version,
                     artifact_kind, artifact_sha256, artifact_size,
                     metadata_json, created_at)
                VALUES (?, ?, ?, 'trained_model', ?, ?, ?, datetime('now'))
                """,
                (
                    experiment_id,
                    manifest_hash,
                    ARTIFACT_MANIFEST_SCHEMA,
                    model_sha256,
                    model_path.stat().st_size,
                    metadata_json,
                ),
            )
    return db_path, experiment, evidence


async def _verify(path: Path) -> dict[str, Any]:
    async with aiosqlite.connect(str(path)) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute("SELECT * FROM experiments WHERE id=1")
        experiment = await cursor.fetchone()
        assert experiment is not None
        return await verify_experiment_model_promotion_evidence(
            connection,
            experiment,
        )


@pytest.mark.parametrize("strategy_class", COMPLIANT_STRATEGIES)
def test_five_platform_trainable_strategies_have_verifiable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy_class: type,
) -> None:
    path, _experiment, evidence = _seed(
        tmp_path,
        monkeypatch,
        strategy=strategy_class(),
    )

    verified = asyncio.run(_verify(path))

    assert verified["strategy_id"] == strategy_class().metadata().strategy_id
    assert verified["schema_version"] == ML_PROMOTION_EVIDENCE_SCHEMA
    assert evidence["validation"]["samples"] > 0
    assert evidence["validation"]["gate"]["passed"] is True
    assert evidence["training"]["implementation_status"] == "native"
    assert evidence["training"]["fallback_used"] is False

    async def promotion_blockers() -> list[dict[str, Any]]:
        async with aiosqlite.connect(str(path)) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute(
                "SELECT * FROM experiments WHERE id=1"
            )).fetchone()
            return await ResearchWorkflowService._ml_promotion_blockers(
                connection,
                row,
            )

    assert asyncio.run(promotion_blockers()) == []


@pytest.mark.parametrize(
    ("strategy_class", "fallback_model_type"),
    [
        (LSTMRankStrategy, "MLP (sklearn fallback)"),
        (
            TransformerRankStrategy,
            "RandomForest (sklearn fallback)",
        ),
    ],
)
def test_neural_sklearn_fallback_is_blocked_despite_finite_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy_class: type,
    fallback_model_type: str,
) -> None:
    path, _experiment, evidence = _seed(
        tmp_path,
        monkeypatch,
        strategy=strategy_class(),
        walkforward=_walkforward(model_type=fallback_model_type),
    )

    assert evidence["validation"]["samples"] == 100
    assert evidence["validation"]["metrics"]["validation_rank_ic"] == 0.2
    assert evidence["validation"]["gate"]["passed"] is True
    assert evidence["training"]["backend"] == "sklearn"
    assert evidence["training"]["implementation_status"] == "fallback"
    assert evidence["training"]["fallback_used"] is True
    with pytest.raises(MLPromotionEvidenceError) as caught:
        asyncio.run(_verify(path))
    assert caught.value.code == "ml_model_fallback_disallowed"


@pytest.mark.parametrize(
    ("strategy_class", "mutator", "expected_code"),
    [
        (
            LSTMRankStrategy,
            lambda evidence: evidence["training"].update(
                {"backend": "sklearn"}
            ),
            "ml_training_telemetry_tampered",
        ),
        (
            TransformerRankStrategy,
            lambda evidence: evidence["training"].update(
                {"fallback_used": True}
            ),
            "ml_model_fallback_disallowed",
        ),
    ],
)
def test_model_backend_and_fallback_tampering_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy_class: type,
    mutator: Any,
    expected_code: str,
) -> None:
    path, _experiment, _evidence = _seed(
        tmp_path,
        monkeypatch,
        strategy=strategy_class(),
        evidence_mutator=mutator,
    )

    with pytest.raises(MLPromotionEvidenceError) as caught:
        asyncio.run(_verify(path))
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("strategy_class", "model_type", "field", "tampered_value"),
    [
        (
            LSTMRankStrategy,
            "LSTM (PyTorch)",
            "model_backend",
            "sklearn",
        ),
        (
            TransformerRankStrategy,
            "Transformer (PyTorch)",
            "fallback_used",
            True,
        ),
    ],
)
def test_inconsistent_explicit_strategy_backend_state_is_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy_class: type,
    model_type: str,
    field: str,
    tampered_value: Any,
) -> None:
    walkforward = _walkforward(model_type=model_type)
    walkforward.cycles[0].train_metrics[field] = tampered_value
    path, _experiment, evidence = _seed(
        tmp_path,
        monkeypatch,
        strategy=strategy_class(),
        walkforward=walkforward,
    )

    assert evidence["training"]["model_implementation"] == "unverified"
    assert evidence["training"]["backend"] == "unverified"
    assert evidence["training"]["fallback_used"] is True
    with pytest.raises(MLPromotionEvidenceError) as caught:
        asyncio.run(_verify(path))
    assert caught.value.code == "ml_model_fallback_disallowed"


def test_nontraining_strategy_does_not_require_model_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nontraining.db"
    _initialize(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO experiments
                (id, user_id, strategy_id, params, params_hash,
                 test_start, test_end, requires_training,
                 retrain_frequency, status)
            VALUES (1, 7, 'ma_cross_v1', '{}', ?, '2024-01-01',
                    '2024-12-31', 0, 'never', 'completed')
            """,
            (legacy_params_hash({}),),
        )

    async def scenario() -> list[dict[str, Any]]:
        async with aiosqlite.connect(str(path)) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute(
                "SELECT * FROM experiments WHERE id=1"
            )).fetchone()
            return await ResearchWorkflowService._ml_promotion_blockers(
                connection,
                row,
            )

    assert asyncio.run(scenario()) == []


def test_alphamaster_is_explicitly_contract_noncompliant(tmp_path: Path) -> None:
    path = tmp_path / "alphamaster.db"
    _initialize(path)
    strategy_id = AlphaMasterGBRStrategy().metadata().strategy_id
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO experiments
                (id, user_id, strategy_id, params, params_hash,
                 test_start, test_end, requires_training,
                 retrain_frequency, status)
            VALUES (1, 7, ?, '{}', ?, '2024-01-01',
                    '2024-12-31', 1, 'monthly', 'completed')
            """,
            (strategy_id, legacy_params_hash({})),
        )

    async def scenario() -> list[dict[str, Any]]:
        async with aiosqlite.connect(str(path)) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute(
                "SELECT * FROM experiments WHERE id=1"
            )).fetchone()
            return await ResearchWorkflowService._ml_promotion_blockers(
                connection,
                row,
            )

    blockers = asyncio.run(scenario())
    assert [item["code"] for item in blockers] == ["ml_contract_noncompliant"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            "owner",
            "ml_evidence_identity_mismatch",
        ),
        (
            "params",
            "ml_evidence_identity_mismatch",
        ),
        (
            "run_hash",
            "ml_evidence_identity_mismatch",
        ),
        (
            "mutable_metrics",
            "ml_training_telemetry_tampered",
        ),
        (
            "model_file",
            "ml_artifact_integrity_failed",
        ),
    ],
)
def test_live_identity_or_mutable_metrics_tampering_blocks_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_code: str,
) -> None:
    path, _experiment, _evidence = _seed(tmp_path, monkeypatch)
    with sqlite3.connect(path) as connection:
        if mutation == "owner":
            connection.execute("UPDATE experiments SET user_id=8 WHERE id=1")
        elif mutation == "params":
            connection.execute(
                "UPDATE experiments SET params='{\"changed\":true}' WHERE id=1"
            )
        elif mutation == "run_hash":
            connection.execute(
                "UPDATE model_artifacts SET run_manifest_hash=? WHERE experiment_id=1",
                ("f" * 64,),
            )
        elif mutation == "model_file":
            model_path = Path(connection.execute(
                "SELECT model_file_path FROM model_artifacts WHERE experiment_id=1"
            ).fetchone()[0])
            model_path.write_bytes(b"tampered-model")
        else:
            connection.execute(
                "UPDATE model_artifacts SET train_metrics='{\"changed\":true}' "
                "WHERE experiment_id=1"
            )

    with pytest.raises(MLPromotionEvidenceError) as caught:
        asyncio.run(_verify(path))
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda evidence: evidence["validation"].update(
                {"samples": 0}
            ),
            "ml_validation_gate_failed",
        ),
        (
            lambda evidence: evidence["validation"]["metrics"].update(
                {"validation_rank_ic": float("nan")}
            ),
            "ml_validation_gate_failed",
        ),
        (
            lambda evidence: evidence["validation"]["gate"].update(
                {"passed": False}
            ),
            "ml_validation_gate_failed",
        ),
        (
            lambda evidence: evidence["validation"]["gate"].update(
                {"threshold": -99.0, "passed": True}
            ),
            "ml_validation_gate_failed",
        ),
        (
            lambda evidence: evidence["validation"].update(
                {"effective_dates": 4}
            ),
            "ml_validation_gate_failed",
        ),
        (
            lambda evidence: evidence["validation"].update(
                {"minimum_cross_section_size": 19}
            ),
            "ml_validation_gate_failed",
        ),
        (
            lambda evidence: evidence["training"].update(
                {
                    "status": "rejected",
                    "failed_attempt_count": 1,
                    "fallback_used": True,
                }
            ),
            "ml_model_fallback_disallowed",
        ),
        (
            lambda evidence: evidence["training"].update(
                {
                    "contract": LEGACY_TRAINING_CONTRACT,
                    "status": "unverified_legacy",
                }
            ),
            "ml_contract_noncompliant",
        ),
    ],
)
def test_invalid_validation_and_training_statuses_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator: Any,
    expected_code: str,
) -> None:
    path, _experiment, _evidence = _seed(
        tmp_path,
        monkeypatch,
        evidence_mutator=mutator,
    )

    with pytest.raises(MLPromotionEvidenceError) as caught:
        asyncio.run(_verify(path))
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda evidence: evidence["windows"].update(
                {"validation_end": evidence["windows"]["prediction_start"]}
            ),
            "ml_validation_window_invalid",
        ),
        (
            lambda evidence: evidence["windows"].update(
                {
                    "validation_end": "2021-12-01",
                    "prediction_start": "2021-12-31",
                }
            ),
            "ml_validation_window_invalid",
        ),
        (
            lambda evidence: evidence["windows"].update(
                {"prediction_start": "2025-01-02"}
            ),
            "ml_validation_window_invalid",
        ),
        (
            lambda evidence: evidence["windows"].pop("prediction_start"),
            "ml_validation_window_missing",
        ),
        (
            lambda evidence: evidence["windows"].update(
                {"prediction_start": "2022-01-05"}
            ),
            "ml_training_telemetry_tampered",
        ),
    ],
)
def test_prediction_window_is_strictly_bound_to_latest_training_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator: Any,
    expected_code: str,
) -> None:
    path, _experiment, evidence = _seed(
        tmp_path,
        monkeypatch,
        evidence_mutator=mutator,
    )

    with pytest.raises(MLPromotionEvidenceError) as caught:
        asyncio.run(_verify(path))
    assert caught.value.code == expected_code
    assert evidence["windows"].get("prediction_month") == "2022-01"


def test_legacy_missing_and_duplicate_latest_artifacts_are_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path, _experiment, _evidence = _seed(
        tmp_path / "missing",
        monkeypatch,
        include_supplement=False,
    )
    with pytest.raises(MLPromotionEvidenceError) as missing:
        asyncio.run(_verify(missing_path))
    assert missing.value.code == "ml_evidence_legacy_or_missing"

    duplicate_path, _experiment, _evidence = _seed(
        tmp_path / "duplicate",
        monkeypatch,
        duplicate_latest=True,
    )
    with pytest.raises(MLPromotionEvidenceError) as duplicate:
        asyncio.run(_verify(duplicate_path))
    assert duplicate.value.code == "ml_artifact_latest_not_unique"


def test_artifact_supplement_is_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _experiment, _evidence = _seed(tmp_path, monkeypatch)

    with sqlite3.connect(path) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="research artifact manifest is immutable",
    ):
        connection.execute(
            "UPDATE research_artifact_manifests SET metadata_json='{}' WHERE id=1"
        )


def test_exact_rerun_evidence_is_bound_to_new_experiment_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, _source, source_evidence = _seed(
        tmp_path / "source",
        monkeypatch,
        experiment_id=1,
    )
    rerun_path, _rerun, rerun_evidence = _seed(
        tmp_path / "rerun",
        monkeypatch,
        experiment_id=2,
    )

    assert source_evidence["identity"]["experiment_id"] == 1
    assert rerun_evidence["identity"]["experiment_id"] == 2
    assert (
        source_evidence["model"]["run_manifest_hash"]
        != rerun_evidence["model"]["run_manifest_hash"]
    )

    async def verify_rerun() -> dict[str, Any]:
        async with aiosqlite.connect(str(rerun_path)) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute(
                "SELECT * FROM experiments WHERE id=2"
            )).fetchone()
            return await verify_experiment_model_promotion_evidence(
                connection,
                row,
            )

    assert asyncio.run(verify_rerun())["experiment_id"] == 2

    def reuse_source_evidence(evidence: dict[str, Any]) -> None:
        evidence.clear()
        evidence.update(source_evidence)

    reused_path, _rerun, _reused = _seed(
        tmp_path / "reused-source-row",
        monkeypatch,
        experiment_id=2,
        evidence_mutator=reuse_source_evidence,
    )

    async def verify_reused_source() -> dict[str, Any]:
        async with aiosqlite.connect(str(reused_path)) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute(
                "SELECT * FROM experiments WHERE id=2"
            )).fetchone()
            return await verify_experiment_model_promotion_evidence(
                connection,
                row,
            )

    with pytest.raises(MLPromotionEvidenceError) as caught:
        asyncio.run(verify_reused_source())
    assert caught.value.code == "ml_evidence_identity_mismatch"


def test_structured_workflow_blocker_never_raises_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _experiment, _evidence = _seed(
        tmp_path,
        monkeypatch,
        include_supplement=False,
    )

    async def scenario() -> list[dict[str, Any]]:
        async with aiosqlite.connect(str(path)) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute(
                "SELECT * FROM experiments WHERE id=1"
            )).fetchone()
            return await ResearchWorkflowService._ml_promotion_blockers(
                connection,
                row,
            )

    blockers = asyncio.run(scenario())
    assert blockers[0]["code"] == "ml_evidence_legacy_or_missing"
