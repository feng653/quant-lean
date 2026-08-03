from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.db.migrate import migrate_experiment
from backend.core.hashing import file_sha256
from backend.services.remote_training import (
    MAX_REPORT_JSON_BYTES,
    RESULT_SCHEMA_VERSION,
    RemoteTrainingError,
    RemoteTrainingService,
    _persist_snapshot,
)
from backend.strategies.base import (
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    TrainableStrategy,
)


class _TrainingStrategy(TrainableStrategy):
    frequency = RetrainFrequency.NEVER

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="remote_test_strategy",
            display_name="Remote Test",
            version="1.0",
            category=StrategyCategory.ML,
            description="test",
            requires_training=True,
            retrain_frequency=cls.frequency,
        )

    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        return object()

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        return {}


class _Registry:
    def create_strategy(self, strategy_id: str) -> _TrainingStrategy:
        if strategy_id != "remote_test_strategy":
            raise KeyError(strategy_id)
        return _TrainingStrategy()

    def get_metadata(self, strategy_id: str) -> StrategyMetadata:
        return self.create_strategy(strategy_id).metadata()

    def validate_params(
        self,
        strategy_id: str,
        params: dict,
    ) -> tuple[bool, str]:
        self.create_strategy(strategy_id)
        return True, ""


class _Upload:
    def __init__(self, payload: bytes, filename: str | None = "model.bin") -> None:
        self.payload = payload
        self.filename = filename
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.payload):
            return b""
        if size < 0:
            size = len(self.payload)
        result = self.payload[self.offset : self.offset + size]
        self.offset += len(result)
        return result


def _init_db(path: Path) -> None:
    async def initialize() -> None:
        connection = await __import__("aiosqlite").connect(str(path))
        try:
            await connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    strategy_id TEXT NOT NULL,
                    pool_preset TEXT,
                    pool_custom_codes TEXT,
                    pool_industries TEXT,
                    train_start TEXT,
                    train_end TEXT,
                    test_start TEXT NOT NULL,
                    test_end TEXT NOT NULL,
                    params TEXT NOT NULL
                );
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                """
            )
            await migrate_experiment(connection)
            await connection.commit()
        finally:
            await connection.close()

    asyncio.run(initialize())


def _insert_experiment(
    path: Path,
    *,
    experiment_id: int = 1,
    user_id: int = 7,
    train_start: str | None = "2024-01-02",
    train_end: str | None = "2024-03-01",
    params: dict[str, Any] | None = None,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO experiments
                (id, user_id, strategy_id, pool_preset, train_start, train_end,
                 test_start, test_end, params)
            VALUES (?, ?, 'remote_test_strategy', 'csi300', ?, ?,
                    '2024-04-01', '2024-06-28', ?)
            """,
            (
                experiment_id,
                user_id,
                train_start,
                train_end,
                json.dumps(params or {"label_horizon_days": 5}),
            ),
        )


async def _snapshot_builder(
    experiment: dict[str, Any],
    strategy: TrainableStrategy,
    params: dict[str, Any],
    train_start: str,
    train_end: str,
    task_dir: Path,
):
    del experiment
    dates = pd.bdate_range("2022-01-03", "2024-05-31")
    columns = pd.MultiIndex.from_product(
        [["000001", "000002"], ["open", "close", "volume"]]
    )
    values = [
        [float(row * len(columns) + column + 1) for column in range(len(columns))]
        for row in range(len(dates))
    ]
    pivot = pd.DataFrame(values, index=dates, columns=columns)
    return _persist_snapshot(
        pivot,
        strategy=strategy,
        params=params,
        train_start=train_start,
        train_end=train_end,
        task_dir=task_dir,
    )


def _service(
    tmp_path: Path,
    *,
    now=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
    max_artifact_bytes: int = 1024,
) -> RemoteTrainingService:
    return RemoteTrainingService(
        db_path=tmp_path / "experiment.db",
        storage_root=tmp_path / "remote",
        snapshot_builder=_snapshot_builder,
        now=now,
        max_artifact_bytes=max_artifact_bytes,
    )


async def _create(service: RemoteTrainingService) -> tuple[dict[str, Any], str]:
    return await service.create_task(
        experiment_id=1,
        user_id=7,
        registry=_Registry(),
    )


def _valid_report(task: dict[str, Any]) -> str:
    return json.dumps(
        {
            "schema_version": RESULT_SCHEMA_VERSION,
            "task_uuid": task["task_uuid"],
            "experiment_id": task["experiment_id"],
            "strategy_id": task["strategy_id"],
            "params_sha256": task["params_hash"],
            "data_sha256": task["data_sha256"],
            "metrics": {"loss": 0.1},
        }
    )


def test_token_is_hashed_once_and_expiry_is_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(db_path)
    current = [datetime(2026, 7, 28, tzinfo=timezone.utc)]
    service = _service(tmp_path, now=lambda: current[0])

    task, token = asyncio.run(_create(service))
    assert token
    assert "token_hash" not in task
    with sqlite3.connect(db_path) as connection:
        stored_hash, expires_at = connection.execute(
            """
            SELECT token_hash, token_expires_at
            FROM remote_training_tasks WHERE task_uuid=?
            """,
            (task["task_uuid"],),
        ).fetchone()
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest()
    assert stored_hash != token
    assert datetime.fromisoformat(expires_at) > current[0]

    manifest = asyncio.run(
        service.worker_manifest(task_uuid=task["task_uuid"], token=token)
    )
    assert manifest["task"]["task_uuid"] == task["task_uuid"]
    with pytest.raises(RemoteTrainingError, match="令牌无效") as invalid:
        asyncio.run(
            service.worker_manifest(task_uuid=task["task_uuid"], token="bad")
        )
    assert invalid.value.status_code == 401

    current[0] += timedelta(hours=25)
    with pytest.raises(RemoteTrainingError, match="已过期") as expired:
        asyncio.run(
            service.worker_manifest(task_uuid=task["task_uuid"], token=token)
        )
    assert expired.value.status_code == 401


def test_task_and_experiment_ownership_are_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(db_path)
    service = _service(tmp_path)
    task, _ = asyncio.run(_create(service))

    with pytest.raises(RemoteTrainingError) as hidden:
        asyncio.run(service.get_task(task_uuid=task["task_uuid"], user_id=8))
    assert hidden.value.status_code == 404
    assert asyncio.run(service.list_tasks(user_id=8)) == []
    with pytest.raises(RemoteTrainingError) as foreign_experiment:
        asyncio.run(
            service.create_task(
                experiment_id=1,
                user_id=8,
                registry=_Registry(),
            )
        )
    assert foreign_experiment.value.status_code == 404


def test_snapshot_manifest_hash_and_tamper_detection(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(db_path)
    service = _service(tmp_path)
    task, token = asyncio.run(_create(service))

    path = asyncio.run(
        service.worker_data_path(task_uuid=task["task_uuid"], token=token)
    )
    manifest = asyncio.run(
        service.worker_manifest(task_uuid=task["task_uuid"], token=token)
    )
    assert manifest["data"]["sha256"] == file_sha256(path)
    assert manifest["windows"]["lookback_rows"] == 252
    assert manifest["windows"]["label_tail_rows"] == 5
    frame = pd.read_parquet(path)
    assert len(frame) == manifest["data"]["rows"]
    assert frame.index.min().strftime("%Y-%m-%d") < task["train_start"]
    assert frame.index.max().strftime("%Y-%m-%d") > task["train_end"]

    with path.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(RemoteTrainingError, match="校验失败") as tampered:
        asyncio.run(
            service.worker_data_path(task_uuid=task["task_uuid"], token=token)
        )
    assert tampered.value.status_code == 409


def test_state_machine_completion_and_terminal_protection(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(db_path)
    service = _service(tmp_path)
    task, token = asyncio.run(_create(service))
    task_uuid = task["task_uuid"]

    started = asyncio.run(service.worker_start(task_uuid=task_uuid, token=token))
    assert started["status"] == "running"
    progressed = asyncio.run(
        service.worker_progress(
            task_uuid=task_uuid,
            token=token,
            progress=0.6,
            message="epoch 6",
        )
    )
    assert progressed["progress"] == 0.6
    completed = asyncio.run(
        service.worker_complete(
            task_uuid=task_uuid,
            token=token,
            report_json=_valid_report(task),
            artifact=_Upload(b"opaque-model-bytes"),
        )
    )
    assert completed["status"] == "completed"
    assert completed["artifact_sha256"] == hashlib.sha256(
        b"opaque-model-bytes"
    ).hexdigest()
    assert "artifact_path" not in completed

    with pytest.raises(RemoteTrainingError, match="令牌无效") as overwrite:
        asyncio.run(
            service.worker_fail(
                task_uuid=task_uuid,
                token=token,
                error="late failure",
            )
        )
    assert overwrite.value.status_code == 401
    with pytest.raises(RemoteTrainingError, match="令牌无效") as download:
        asyncio.run(service.worker_manifest(task_uuid=task_uuid, token=token))
    assert download.value.status_code == 401
    with pytest.raises(RemoteTrainingError) as cancel:
        asyncio.run(service.cancel_task(task_uuid=task_uuid, user_id=7))
    assert cancel.value.status_code == 409


def test_concurrent_completion_preserves_winning_artifact(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(db_path)
    service = _service(tmp_path)
    task, token = asyncio.run(_create(service))
    task_uuid = task["task_uuid"]
    asyncio.run(service.worker_start(task_uuid=task_uuid, token=token))

    async def compete() -> list[object]:
        first = service.worker_complete(
            task_uuid=task_uuid,
            token=token,
            report_json=_valid_report(task),
            artifact=_Upload(b"first-model"),
        )
        second = service.worker_complete(
            task_uuid=task_uuid,
            token=token,
            report_json=_valid_report(task),
            artifact=_Upload(b"second-model"),
        )
        return await asyncio.gather(first, second, return_exceptions=True)

    outcomes = asyncio.run(compete())
    completed = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, dict) and outcome.get("status") == "completed"
    ]
    rejected = [
        outcome for outcome in outcomes if isinstance(outcome, RemoteTrainingError)
    ]
    assert len(completed) == 1
    assert len(rejected) == 1
    assert rejected[0].status_code in {401, 409}

    with sqlite3.connect(db_path) as connection:
        artifact_path, artifact_sha256 = connection.execute(
            """
            SELECT artifact_path, artifact_sha256
            FROM remote_training_tasks WHERE task_uuid=?
            """,
            (task_uuid,),
        ).fetchone()
    committed_path = Path(artifact_path)
    assert committed_path.is_file()
    assert file_sha256(committed_path) == artifact_sha256
    assert list(committed_path.parent.glob("artifact-*.bin")) == [committed_path]


@pytest.mark.parametrize(
    ("upload", "report_mutation", "expected_status"),
    [
        (_Upload(b"12345"), None, 413),
        (_Upload(b""), None, 422),
        (_Upload(b"ok"), {"strategy_id": "wrong"}, 422),
        (_Upload(b"ok", filename=None), None, 422),
    ],
)
def test_oversized_and_invalid_uploads_are_rejected(
    tmp_path: Path,
    upload: _Upload,
    report_mutation: dict[str, Any] | None,
    expected_status: int,
) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(db_path)
    service = _service(tmp_path, max_artifact_bytes=4)
    task, token = asyncio.run(_create(service))
    asyncio.run(service.worker_start(task_uuid=task["task_uuid"], token=token))
    report = json.loads(_valid_report(task))
    report.update(report_mutation or {})

    with pytest.raises(RemoteTrainingError) as rejected:
        asyncio.run(
            service.worker_complete(
                task_uuid=task["task_uuid"],
                token=token,
                report_json=json.dumps(report),
                artifact=upload,
            )
        )
    assert rejected.value.status_code == expected_status
    persisted = asyncio.run(
        service.get_task(task_uuid=task["task_uuid"], user_id=7)
    )
    assert persisted["status"] == "running"
    assert persisted["artifact_sha256"] is None


def test_periodic_strategy_gets_one_derived_window_only(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(
        db_path,
        train_start=None,
        train_end=None,
        params={
            "label_horizon_days": 5,
            "window_mode": "rolling",
            "rolling_train_months": 6,
        },
    )
    service = _service(tmp_path)
    original_frequency = _TrainingStrategy.frequency
    _TrainingStrategy.frequency = RetrainFrequency.MONTHLY
    try:
        task, token = asyncio.run(_create(service))
    finally:
        _TrainingStrategy.frequency = original_frequency

    manifest = asyncio.run(
        service.worker_manifest(task_uuid=task["task_uuid"], token=token)
    )
    assert manifest["task"]["single_window_only"] is True
    assert manifest["task"]["completes_walk_forward"] is False
    assert manifest["strategy"]["retrain_frequency"] == "monthly"
    assert manifest["windows"]["requested_train_end"] == "2024-03-31"


@pytest.mark.parametrize(
    ("report_json", "expected_status"),
    [
        (" " * (MAX_REPORT_JSON_BYTES + 1), 413),
        (
            json.dumps(
                {
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "metrics": {"loss": float("nan")},
                }
            ),
            422,
        ),
    ],
    ids=["oversized", "non-finite"],
)
def test_report_size_and_non_finite_numbers_are_rejected(
    tmp_path: Path,
    report_json: str,
    expected_status: int,
) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(db_path)
    service = _service(tmp_path)
    task, token = asyncio.run(_create(service))
    asyncio.run(service.worker_start(task_uuid=task["task_uuid"], token=token))

    with pytest.raises(RemoteTrainingError) as rejected:
        asyncio.run(
            service.worker_complete(
                task_uuid=task["task_uuid"],
                token=token,
                report_json=report_json,
                artifact=_Upload(b"model"),
            )
        )
    assert rejected.value.status_code == expected_status
    persisted = asyncio.run(
        service.get_task(task_uuid=task["task_uuid"], user_id=7)
    )
    assert persisted["status"] == "running"


def test_non_finite_params_are_rejected_before_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "experiment.db"
    _init_db(db_path)
    _insert_experiment(
        db_path,
        params={"label_horizon_days": 5, "learning_rate": float("inf")},
    )
    service = _service(tmp_path)

    with pytest.raises(RemoteTrainingError, match="规范 JSON") as rejected:
        asyncio.run(_create(service))
    assert rejected.value.status_code == 422
    assert list((tmp_path / "remote").glob("*")) == []
