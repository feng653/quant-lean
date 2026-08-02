from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pickle
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
import aiosqlite

from backend.config import settings
from backend.db.migrate import migrate_trading
from backend.main import _init_databases
from backend.services import maintenance
from backend.services import model_artifacts
from backend.services.model_artifacts import (
    ModelArtifactIntegrityError,
    load_verified_deployment_model,
    verify_model_file,
)
from backend.services.model_serialization import (
    JOBLIB_PLATFORM_V1,
    ModelSerializationError,
    contract_for_model,
    validate_contract,
)
from backend.services.walkforward import run_walk_forward
from backend.strategies.base import (
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    TrainableStrategy,
    TrainingWindowContext,
)
from backend.strategies.factor.alphamaster_gbr import AlphaMasterGBRStrategy


class _CandidateStrategy(TrainableStrategy):
    rank_ic = 0.4
    barrier: threading.Barrier | None = None
    contexts: list[TrainingWindowContext] = []
    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="candidate_strategy",
            display_name="Candidate",
            version="1",
            category=StrategyCategory.ML,
            description="Test candidate lifecycle",
            requires_training=True,
            retrain_frequency=RetrainFrequency.MONTHLY,
        )

    def label_horizon_days(self, params: dict) -> int:
        return 2

    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        raise AssertionError("maintenance must use fit_with_validation")

    def fit_with_validation(
        self,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> Any:
        type(self).contexts.append(context)
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        self._model = {"candidate": True}
        self.record_train_metrics(
            n_samples=100,
            n_features=2,
            n_validation_samples=100,
            n_validation_candidate_dates=5,
            n_validation_dates=5,
            min_validation_cross_section_size=20,
            validation_ic=self.rank_ic,
            validation_ic_std=0.1,
            validation_icir=self.rank_ic / 0.1,
            validation_rank_ic=self.rank_ic,
            validation_rank_ic_std=0.1,
            validation_rank_icir=self.rank_ic / 0.1,
            validation_loss=0.1,
            validation_score=0.2,
        )
        return self._model

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        return {"000001": 1.0}

    def save_model(self, model: Any, path: str) -> None:
        Path(path).write_bytes(b"candidate-model")


@pytest.fixture(autouse=True)
def isolated_pit_retrain_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Artifact tests replace production PIT storage with an explicit seam."""

    async def ready(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            pool_id=kwargs.get("pool_id", "csi300"),
            market=SimpleNamespace(frame=pd.DataFrame()),
        )

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        ready,
    )


class _Registry:
    def create_strategy(self, strategy_id: str) -> _CandidateStrategy:
        assert strategy_id == "candidate_strategy"
        return _CandidateStrategy()


def _configure_databases(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(tmp_path / "models"))
    asyncio.run(_init_databases())


def _params(*, min_validation_rank_ic: float = 0.02) -> dict[str, Any]:
    return {
        "validation_months": 1,
        "embargo_days": 0,
        "min_train_months": 6,
        "rolling_train_months": 24,
        "window_mode": "expanding",
        "min_validation_rank_ic": min_validation_rank_ic,
    }


def _insert_deployment(
    tmp_path: Path,
    *,
    params: dict[str, Any],
    current_model: bytes | None = None,
    current_version: int = 1,
) -> tuple[int, str, Path | None]:
    params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
    params_hash = hashlib.md5(params_json.encode()).hexdigest()
    current_path = None
    current_sha256 = None
    current_size = None
    if current_model is not None:
        current_path = (
            tmp_path
            / "models"
            / "deployment_seed"
            / f"model_v{current_version}.joblib"
        )
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_bytes(current_model)
        current_sha256 = hashlib.sha256(current_model).hexdigest()
        current_size = len(current_model)
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        deployment_id = connection.execute(
            """
            INSERT INTO deployments
                (user_id, strategy_id, strategy_category, display_name,
                 params, params_hash, mode, requires_retraining,
                 retrain_frequency, current_model_version,
                 current_model_path, current_model_sha256,
                 current_model_size, status, pool_preset)
            VALUES (7, 'candidate_strategy', 'ml', 'Candidate',
                    ?, ?, 'batch', 1, 'monthly', ?, ?, ?, ?,
                    'active', 'csi300')
            """,
            (
                params_json,
                params_hash,
                current_version,
                str(current_path) if current_path else None,
                current_sha256,
                current_size,
            ),
        ).lastrowid
        if current_path is not None:
            retrain_manifest = {
                "schema_version": "model-retrain-manifest/v1",
                "deployment": {
                    "deployment_id": deployment_id,
                    "owner_id": 7,
                    "strategy_id": "candidate_strategy",
                    "params_hash": params_hash,
                    "model_version": current_version,
                },
                "artifact": {
                    "sha256": current_sha256,
                    "size": current_size,
                },
                "parameters": {"canonical": params},
                "windows": {"train_start": "2023-01-01"},
                "validation": {"validation_rank_ic": 0.4},
                "dataset": {"data_version": "test"},
            }
            retrain_manifest_hash = model_artifacts.canonical_sha256(
                retrain_manifest
            )
            connection.execute(
                """
                INSERT INTO model_version_history
                    (deployment_id, model_version, model_file_path,
                     metadata_file_path, model_sha256, model_size,
                     strategy_id, params_hash, retrain_manifest_json,
                     retrain_manifest_hash, status, is_latest)
                VALUES (?, ?, ?, 'seed.json', ?, ?,
                        'candidate_strategy', ?, ?, ?, 'promoted', 1)
                """,
                (
                    deployment_id,
                    current_version,
                    str(current_path),
                    current_sha256,
                    current_size,
                    params_hash,
                    json.dumps(retrain_manifest, sort_keys=True),
                    retrain_manifest_hash,
                ),
            )
        connection.commit()
    return deployment_id, params_hash, current_path


def _market_data() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-02", "2024-12-31")
    return pd.DataFrame(
        {
            "000001": range(len(dates)),
            "000002": range(len(dates), len(dates) * 2),
        },
        index=dates,
    )


def _install_retrain_fakes(monkeypatch, panel: pd.DataFrame) -> None:
    async def isolated_pit(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            pool_id=kwargs.get("pool_id", "csi300"),
            market=SimpleNamespace(frame=panel.copy()),
        )

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        isolated_pit,
    )
    monkeypatch.setattr(maintenance, "get_registry", lambda: _Registry())

    async def local_isolated_fit(
        *,
        strategy_id: str,
        pivot: pd.DataFrame,
        params: dict[str, Any],
        context: TrainingWindowContext,
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        assert strategy_id == "candidate_strategy"
        return await asyncio.to_thread(
            maintenance._fit_candidate,
            _CandidateStrategy(),
            pivot,
            params,
            context,
        )

    monkeypatch.setattr(
        maintenance,
        "_run_isolated_retrain_fit",
        local_isolated_fit,
    )


def test_model_file_rejects_path_escape_and_same_size_tampering(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(tmp_path / "models"))
    outside = tmp_path / "outside.joblib"
    outside.write_bytes(b"outside")
    outside_hash = hashlib.sha256(outside.read_bytes()).hexdigest()
    with pytest.raises(
        ModelArtifactIntegrityError,
        match="outside MODEL_STORE_DIR",
    ):
        asyncio.run(
            verify_model_file(outside, outside_hash, outside.stat().st_size)
        )

    inside = tmp_path / "models" / "model.joblib"
    inside.parent.mkdir()
    inside.write_bytes(b"trusted")
    trusted_hash = hashlib.sha256(inside.read_bytes()).hexdigest()
    inside.write_bytes(b"tamper!")
    with pytest.raises(ModelArtifactIntegrityError, match="SHA-256 mismatch"):
        asyncio.run(verify_model_file(inside, trusted_hash, len(b"trusted")))

    monkeypatch.setattr(model_artifacts, "MAX_MODEL_ARTIFACT_BYTES", 4)
    with pytest.raises(ModelArtifactIntegrityError, match="size limit"):
        asyncio.run(
            verify_model_file(
                inside,
                hashlib.sha256(inside.read_bytes()).hexdigest(),
                inside.stat().st_size,
            )
        )


def test_current_deployment_model_is_preferred_and_identity_bound(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    deployment_id, params_hash, current_path = _insert_deployment(
        tmp_path,
        params=_params(),
        current_model=b"current-champion",
        current_version=4,
    )
    assert current_path is not None
    loaded: list[str] = []

    class Loader:
        _model = None

        def load_model(self, path):
            loaded.append(Path(path).read_bytes().decode())
            return "champion"

    deployment = {
        "id": deployment_id,
        "user_id": 7,
        "strategy_id": "candidate_strategy",
        "params": json.dumps(_params(), sort_keys=True),
        "params_hash": params_hash,
        "current_model_version": 4,
        "current_model_path": str(current_path),
        "current_model_sha256": hashlib.sha256(
            b"current-champion"
        ).hexdigest(),
        "current_model_size": len(b"current-champion"),
        # This invalid fallback must never be consulted.
        "source_experiment_id": 999,
        "source_model_artifact_id": 999,
        "requires_retraining": 1,
    }
    loader = Loader()
    verified = asyncio.run(
        load_verified_deployment_model(loader, deployment)
    )
    assert verified is not None
    assert verified.source == "deployment"
    assert loader._model == "champion"
    assert loaded == ["current-champion"]
    deployment["user_id"] = 8
    with pytest.raises(
        ModelArtifactIntegrityError,
        match="no unique promoted history",
    ):
        asyncio.run(load_verified_deployment_model(Loader(), deployment))
    deployment["user_id"] = 7
    deployment["params_hash"] = "different"
    with pytest.raises(
        ModelArtifactIntegrityError,
        match="no unique promoted history",
    ):
        asyncio.run(load_verified_deployment_model(Loader(), deployment))
    assert loaded == ["current-champion"]


def test_verified_periodic_model_is_reused_without_inline_retraining() -> None:
    strategy = _CandidateStrategy()
    strategy._model = {"verified": True}
    strategy._verified_deployment_model = strategy._model
    _CandidateStrategy.contexts = []
    result = run_walk_forward(
        strategy,
        _market_data(),
        {
            **_params(),
        },
        "2024-11-01",
        "2024-12-31",
    )
    assert result.last_model == {"verified": True}
    assert _CandidateStrategy.contexts == []
    assert not any(cycle.retrained for cycle in result.cycles)


def test_model_replacement_between_hash_and_load_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    deployment_id, params_hash, current_path = _insert_deployment(
        tmp_path,
        params=_params(),
        current_model=b"current-champion",
        current_version=4,
    )
    assert current_path is not None
    original_file_sha256 = model_artifacts.file_sha256
    hash_calls = 0

    def replace_after_hash(path):
        nonlocal hash_calls
        hash_calls += 1
        digest = original_file_sha256(path)
        if hash_calls == 2:
            Path(path).write_bytes(b"changed-champion")
        return digest

    monkeypatch.setattr(
        model_artifacts,
        "file_sha256",
        replace_after_hash,
    )

    class Loader:
        _model = None

        def load_model(self, path):
            raise AssertionError("raced artifact must not be deserialized")

    with pytest.raises(
        ModelArtifactIntegrityError,
        match="changed while creating",
    ):
        asyncio.run(
            load_verified_deployment_model(
                Loader(),
                {
                    "id": deployment_id,
                    "user_id": 7,
                    "strategy_id": "candidate_strategy",
                    "params": json.dumps(_params(), sort_keys=True),
                    "params_hash": params_hash,
                    "current_model_version": 4,
                    "current_model_path": str(current_path),
                    "current_model_sha256": hashlib.sha256(
                        b"current-champion"
                    ).hexdigest(),
                    "current_model_size": len(b"current-champion"),
                    "requires_retraining": 1,
                },
            )
        )


def test_untrusted_executable_pickle_contract_is_rejected_before_loader(
    tmp_path,
    monkeypatch,
) -> None:
    """A malicious pickle must not execute merely because it has a digest."""

    _configure_databases(tmp_path, monkeypatch)
    marker = tmp_path / "pwned"

    class _Malicious:
        def __reduce__(self):
            return (os.system, (f"touch {marker}",))

    payload = pickle.dumps(_Malicious())
    deployment_id, params_hash, current_path = _insert_deployment(
        tmp_path,
        params=_params(),
        current_model=payload,
        current_version=4,
    )
    assert current_path is not None
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        row = connection.execute(
            "SELECT retrain_manifest_json FROM model_version_history "
            "WHERE deployment_id=?",
            (deployment_id,),
        ).fetchone()
        manifest = json.loads(row[0])
        manifest["artifact"]["serialization"] = {
            "schema_version": "model-serialization/v1",
            "format": "pickle-untrusted-v1",
            "loader": "pickle",
            "platform_produced": False,
            "executable_deserialization": True,
        }
        connection.execute(
            "UPDATE model_version_history SET retrain_manifest_json=?, "
            "retrain_manifest_hash=? WHERE deployment_id=?",
            (
                json.dumps(manifest, sort_keys=True),
                model_artifacts.canonical_sha256(manifest),
                deployment_id,
            ),
        )
        connection.commit()

    class RefusingLoader:
        _model = None

        def load_model(self, path):
            raise AssertionError("untrusted pickle reached a loader")

    with pytest.raises(
        ModelArtifactIntegrityError,
        match="serialization format is not allowed",
    ):
        asyncio.run(
            load_verified_deployment_model(
                RefusingLoader(),
                {
                    "id": deployment_id,
                    "user_id": 7,
                    "strategy_id": "candidate_strategy",
                    "params": json.dumps(_params(), sort_keys=True),
                    "params_hash": params_hash,
                    "current_model_version": 4,
                    "current_model_path": str(current_path),
                    "current_model_sha256": hashlib.sha256(payload).hexdigest(),
                    "current_model_size": len(payload),
                    "requires_retraining": 1,
                },
            )
        )
    assert not marker.exists()


def test_serialization_contract_is_strategy_bound() -> None:
    strategy = _CandidateStrategy()
    contract = contract_for_model(strategy, {"candidate": True})
    assert contract["format"] == JOBLIB_PLATFORM_V1
    assert validate_contract(contract, strategy=strategy) == JOBLIB_PLATFORM_V1
    contract["format"] = "torch-state-dict-v1"
    contract["loader"] = "torch.weights_only"
    contract["executable_deserialization"] = False
    with pytest.raises(ModelSerializationError, match="does not match strategy"):
        validate_contract(contract, strategy=strategy)


def test_user_reuse_parameter_cannot_skip_training() -> None:
    strategy = _CandidateStrategy()
    _CandidateStrategy.contexts = []
    _CandidateStrategy.rank_ic = 0.4
    result = run_walk_forward(
        strategy,
        _market_data(),
        {
            **_params(),
            "_reuse_loaded_model": True,
        },
        "2024-12-02",
        "2024-12-31",
    )
    assert result.last_model == {"candidate": True}
    assert len(_CandidateStrategy.contexts) == 1
    assert result.cycles[0].retrained is True


def test_prepare_cannot_replace_verified_deployment_model() -> None:
    verified_model = {"verified": True}

    class ReplacingPrepareStrategy(_CandidateStrategy):
        def prepare(self, pivot, params):
            self._model = {"unverified": True}

        def predict_scores(self, model, pivot, params, as_of_date):
            assert model is verified_model
            return {"000001": 1.0}

    strategy = ReplacingPrepareStrategy()
    strategy._model = verified_model
    strategy._verified_deployment_model = verified_model
    result = run_walk_forward(
        strategy,
        _market_data(),
        _params(),
        "2024-12-02",
        "2024-12-31",
    )
    assert result.last_model is verified_model
    assert strategy._model is verified_model


def test_legacy_source_model_without_hash_evidence_fails_before_load(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    params_json = json.dumps({}, sort_keys=True)
    params_hash = hashlib.md5(params_json.encode()).hexdigest()
    model_path = tmp_path / "models" / "legacy.joblib"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"legacy")
    with sqlite3.connect(str(tmp_path / "experiment.db")) as connection:
        experiment_id = connection.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category,
                 test_start, test_end, params, params_hash, status)
            VALUES (7, 'Legacy', 'candidate_strategy', 'ml',
                    '2024-01-01', '2024-12-31', ?, ?, 'completed')
            """,
            (params_json, params_hash),
        ).lastrowid
        artifact_id = connection.execute(
            """
            INSERT INTO model_artifacts
                (experiment_id, strategy_id, model_file_path,
                 metadata_file_path, params_hash)
            VALUES (?, 'candidate_strategy', ?, 'legacy.json', ?)
            """,
            (experiment_id, str(model_path), params_hash),
        ).lastrowid
        connection.commit()

    class RefusingLoader:
        _model = None

        def load_model(self, path):
            raise AssertionError("unverified legacy artifact must not load")

    with pytest.raises(
        ModelArtifactIntegrityError,
        match="RunManifest evidence",
    ):
        asyncio.run(
            load_verified_deployment_model(
                RefusingLoader(),
                {
                    "user_id": 7,
                    "strategy_id": "candidate_strategy",
                    "params": params_json,
                    "params_hash": params_hash,
                    "source_experiment_id": experiment_id,
                    "source_model_artifact_id": artifact_id,
                    "requires_retraining": 1,
                },
            )
        )


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema_version": "research-run-manifest/v0"},
        {
            "schema_version": "research-run-manifest/v1",
            "experiment": {"experiment_id": 1},
            "strategy": {"class": "Model"},
            "environment": {"python": "test"},
            "parameters": {"canonical": {}},
            "windows": {"train_start": "2024-01-01"},
            "dataset": {},
            "universe": {"snapshot": "test"},
        },
    ],
)
def test_incomplete_run_manifest_is_rejected(manifest) -> None:
    with pytest.raises(
        ModelArtifactIntegrityError,
        match="schema|incomplete",
    ):
        model_artifacts._validate_run_manifest_structure(manifest)


def test_retrain_promotes_validated_candidate_with_purge_and_embargo(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    panel = _market_data()
    _install_retrain_fakes(monkeypatch, panel)
    _CandidateStrategy.contexts = []
    _CandidateStrategy.rank_ic = 0.4
    _CandidateStrategy.barrier = None
    deployment_id, _, _ = _insert_deployment(tmp_path, params=_params())

    result = asyncio.run(maintenance.retrain_deployment(deployment_id, 7))
    assert result["model_version"] == 2
    promoted_path = Path(result["model_path"])
    assert promoted_path.is_file()
    assert result["model_sha256"] == hashlib.sha256(
        promoted_path.read_bytes()
    ).hexdigest()
    context = _CandidateStrategy.contexts[-1]
    train_end_position = panel.index.get_loc(pd.Timestamp(context.train_end))
    validation_start_position = panel.index.get_loc(
        pd.Timestamp(context.validation_start)
    )
    assert validation_start_position - train_end_position - 1 == 3

    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        deployment = connection.execute(
            """
            SELECT current_model_version, current_model_path,
                   current_model_sha256, current_model_size
            FROM deployments WHERE id=?
            """,
            (deployment_id,),
        ).fetchone()
        history = connection.execute(
            """
            SELECT model_version, model_sha256, model_size,
                   validation_window_start, validation_window_end,
                   validation_metrics, status, is_latest
            FROM model_version_history WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchone()
        attempt = connection.execute(
            """
            SELECT status, validation_metrics
            FROM model_retrain_attempts WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchone()
    assert deployment == (
        2,
        str(promoted_path),
        result["model_sha256"],
        promoted_path.stat().st_size,
    )
    assert history[:3] == (
        2,
        result["model_sha256"],
        promoted_path.stat().st_size,
    )
    assert history[3:5] == (
        context.validation_start,
        context.validation_end,
    )
    assert json.loads(history[5])["validation_rank_ic"] == 0.4
    assert history[6:] == ("promoted", 1)
    assert attempt[0] == "promoted"
    assert json.loads(attempt[1])["n_validation_samples"] == 100

    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        connection.row_factory = sqlite3.Row
        deployment_row = dict(
            connection.execute(
                "SELECT * FROM deployments WHERE id=?",
                (deployment_id,),
            ).fetchone()
        )

    class Loader:
        _model = None

        def load_model(self, path):
            assert Path(path).read_bytes() == b"candidate-model"
            return "promoted-candidate"

    loader = Loader()
    verified = asyncio.run(
        load_verified_deployment_model(loader, deployment_row)
    )
    assert verified is not None and verified.source == "deployment"
    assert loader._model == "promoted-candidate"


def test_failed_validation_preserves_champion_and_records_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    _install_retrain_fakes(monkeypatch, _market_data())
    _CandidateStrategy.contexts = []
    _CandidateStrategy.rank_ic = -0.5
    _CandidateStrategy.barrier = None
    deployment_id, _, champion_path = _insert_deployment(
        tmp_path,
        params=_params(min_validation_rank_ic=0.02),
        current_model=b"old-champion",
        current_version=4,
    )

    with pytest.raises(RuntimeError, match="validation gate rejected"):
        asyncio.run(maintenance.retrain_deployment(deployment_id, 7))
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        deployment = connection.execute(
            """
            SELECT current_model_version, current_model_path
            FROM deployments WHERE id=?
            """,
            (deployment_id,),
        ).fetchone()
        history = connection.execute(
            """
            SELECT model_version, is_latest
            FROM model_version_history WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchall()
        attempt = connection.execute(
            """
            SELECT status, error FROM model_retrain_attempts
            WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchone()
    assert deployment == (4, str(champion_path))
    assert history == [(4, 1)]
    assert attempt[0] == "failed"
    assert "validation gate rejected" in attempt[1]
    assert champion_path is not None and champion_path.read_bytes() == b"old-champion"


def test_concurrent_retrain_uses_compare_and_swap(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    _install_retrain_fakes(monkeypatch, _market_data())
    _CandidateStrategy.contexts = []
    _CandidateStrategy.rank_ic = 0.4
    _CandidateStrategy.barrier = threading.Barrier(2)
    deployment_id, _, _ = _insert_deployment(tmp_path, params=_params())

    async def run_both():
        return await asyncio.gather(
            maintenance.retrain_deployment(deployment_id, 7),
            maintenance.retrain_deployment(deployment_id, 7),
            return_exceptions=True,
        )

    outcomes = asyncio.run(run_both())
    successes = [item for item in outcomes if isinstance(item, dict)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == len(failures) == 1
    assert "concurrent model promotion conflict" in str(failures[0])
    promoted_path = Path(successes[0]["model_path"])
    assert promoted_path.is_file()

    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        deployment = connection.execute(
            """
            SELECT current_model_version, current_model_path
            FROM deployments WHERE id=?
            """,
            (deployment_id,),
        ).fetchone()
        history = connection.execute(
            """
            SELECT model_version, is_latest
            FROM model_version_history WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchall()
        attempts = connection.execute(
            """
            SELECT status FROM model_retrain_attempts
            WHERE deployment_id=? ORDER BY status
            """,
            (deployment_id,),
        ).fetchall()
    assert deployment == (2, str(promoted_path))
    assert history == [(2, 1)]
    assert attempts == [("failed",), ("promoted",)]
    _CandidateStrategy.barrier = None


def test_alphamaster_automatic_retrain_fails_closed_with_guidance(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    panel = _market_data()

    class AlphaMasterRegistry:
        def create_strategy(self, strategy_id):
            assert strategy_id == "alphamaster_gbr_v1"
            return AlphaMasterGBRStrategy()

    async def isolated_pit(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            pool_id=kwargs.get("pool_id", "csi300"),
            market=SimpleNamespace(frame=panel.copy()),
        )

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        isolated_pit,
    )
    monkeypatch.setattr(
        maintenance,
        "get_registry",
        lambda: AlphaMasterRegistry(),
    )
    deployment_id, _, _ = _insert_deployment(tmp_path, params=_params())
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        connection.execute(
            """
            UPDATE deployments
            SET strategy_id='alphamaster_gbr_v1'
            WHERE id=?
            """,
            (deployment_id,),
        )
        connection.commit()

    with pytest.raises(
        ValueError,
        match="does not implement the periodic platform",
    ):
        asyncio.run(maintenance.retrain_deployment(deployment_id, 7))
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        attempt = connection.execute(
            """
            SELECT status, error FROM model_retrain_attempts
            WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchone()
    assert attempt[0] == "failed"
    assert "reviewed experiment" in attempt[1]


def test_retrain_missing_source_experiment_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_databases(tmp_path, monkeypatch)
    deployment_id, _, _ = _insert_deployment(tmp_path, params=_params())
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        connection.execute(
            """
            UPDATE deployments SET source_experiment_id=999
            WHERE id=?
            """,
            (deployment_id,),
        )
        connection.commit()

    with pytest.raises(ValueError, match="source experiment is missing"):
        asyncio.run(maintenance.retrain_deployment(deployment_id, 7))
    with sqlite3.connect(str(tmp_path / "trading.db")) as connection:
        deployment = connection.execute(
            """
            SELECT current_model_version, current_model_path
            FROM deployments WHERE id=?
            """,
            (deployment_id,),
        ).fetchone()
        history_count = connection.execute(
            """
            SELECT COUNT(*) FROM model_version_history
            WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchone()[0]
        attempt = connection.execute(
            """
            SELECT status, error FROM model_retrain_attempts
            WHERE deployment_id=?
            """,
            (deployment_id,),
        ).fetchone()
    assert deployment == (1, None)
    assert history_count == 0
    assert attempt[0] == "failed"
    assert "source experiment is missing" in attempt[1]


def test_trading_model_integrity_migration_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "legacy-trading.db"
    with sqlite3.connect(str(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE deployments (
                id INTEGER PRIMARY KEY,
                current_model_version INTEGER
            );
            CREATE TABLE portfolios (id INTEGER PRIMARY KEY);
            CREATE TABLE daily_signals (
                deployment_id INTEGER, date TEXT, code TEXT
            );
            CREATE TABLE position_snapshots (
                portfolio_id INTEGER, deployment_id INTEGER,
                date TEXT, code TEXT
            );
            CREATE TABLE orders (id INTEGER PRIMARY KEY);
            CREATE TABLE nav_history (portfolio_id INTEGER, date TEXT);
            CREATE TABLE model_version_history (
                id INTEGER PRIMARY KEY,
                deployment_id INTEGER,
                model_version INTEGER,
                model_file_path TEXT,
                metadata_file_path TEXT,
                is_latest INTEGER
            );
            INSERT INTO deployments(id, current_model_version) VALUES (1, 1);
            INSERT INTO model_version_history
                (id, deployment_id, model_version, model_file_path,
                 metadata_file_path, is_latest)
            VALUES (1, 1, 1, 'legacy.joblib', 'legacy.json', 1);
            """
        )

    async def migrate_twice() -> None:
        async def migrate_once() -> None:
            async with aiosqlite.connect(str(db_path)) as connection:
                await migrate_trading(connection)
                await connection.commit()

        await asyncio.gather(migrate_once(), migrate_once())
        async with aiosqlite.connect(str(db_path)) as connection:
            await migrate_trading(connection)
            await connection.commit()

    asyncio.run(migrate_twice())
    with sqlite3.connect(str(db_path)) as connection:
        deployment_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(deployments)"
            ).fetchall()
        }
        history_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(model_version_history)"
            ).fetchall()
        }
        attempt_count = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='table' AND name='model_retrain_attempts'
            """
        ).fetchone()[0]
        marker_count = connection.execute(
            """
            SELECT COUNT(*) FROM schema_migrations
            WHERE version='trading-005-model-artifact-integrity'
            """
        ).fetchone()[0]
        legacy = connection.execute(
            """
            SELECT status, retrain_manifest_hash
            FROM model_version_history WHERE id=1
            """
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO model_version_history
                    (deployment_id, model_version, model_file_path,
                     metadata_file_path, is_latest)
                VALUES (1, 2, 'second.joblib', 'second.json', 1)
                """
            )
        connection.rollback()
    assert {"current_model_sha256", "current_model_size"} <= deployment_columns
    assert {
        "model_sha256",
        "model_size",
        "validation_metrics",
        "strategy_id",
        "params_hash",
        "status",
    } <= history_columns
    assert attempt_count == marker_count == 1
    assert legacy == ("unverified_legacy", None)
