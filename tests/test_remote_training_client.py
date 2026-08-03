from __future__ import annotations
from backend.core.hashing import file_sha256

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from backend.strategies.base import (
    ParamField,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    TrainableStrategy,
)
from remote_worker.errors import (
    DatasetValidationError,
    ManifestValidationError,
    StrategyValidationError,
)
from remote_worker.manifest import sha256_json
from remote_worker.client import RemoteTrainingHTTPClient
from remote_worker.runner import RemoteTrainingRunner


TASK_ID = "0123456789abcdef8123456789abcdef"
FIELDS = ("open", "high", "low", "close", "volume", "amount")


class FakeStrategy(TrainableStrategy):
    events: list[str] = []

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="fake_trainable_v1",
            display_name="Fake trainable",
            version="1.0.0",
            category=StrategyCategory.ML,
            description="Test-only deterministic trainable strategy.",
            requires_training=True,
            retrain_frequency=RetrainFrequency.NEVER,
            supported_modes=[StrategyMode.BATCH],
            params=[
                ParamField(
                    name="label_horizon_days",
                    type="int",
                    default=1,
                )
            ],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        return (
            (True, "")
            if params.get("label_horizon_days") == 1
            else (False, "bad horizon")
        )

    def prepare(self, pivot: pd.DataFrame, params: dict) -> None:
        self.events.append("prepare")
        assert not pivot.empty

    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        self.events.append("fit")
        model = {
            "strategy": self.metadata().strategy_id,
            "rows": len(pivot),
            "train_start": train_start,
            "train_end": train_end,
        }
        self._model = model
        self.record_train_metrics(
            n_samples=len(pivot),
            n_features=len(pivot.columns),
            model_type="fake",
        )
        return model

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        return {"000001": 1.0}

    def save_model(self, model: Any, path: str) -> None:
        self.events.append("save")
        Path(path).write_text(
            json.dumps(model, sort_keys=True),
            encoding="utf-8",
        )


class FakeRegistry:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.scanned = False

    def scan_directory(self, _path: Path) -> int:
        self.events.append("scan")
        self.scanned = True
        return 1

    def create_strategy(self, strategy_id: str) -> FakeStrategy:
        assert self.scanned, "Registry must be scanned before strategy creation"
        if strategy_id != "fake_trainable_v1":
            raise KeyError(strategy_id)
        FakeStrategy.events = self.events
        return FakeStrategy()


class FakeTransport:
    def __init__(
        self,
        manifest: dict[str, Any],
        parquet_path: Path,
        events: list[str],
    ) -> None:
        self.task_id = TASK_ID
        self.manifest = manifest
        self.parquet_path = parquet_path
        self.events = events
        self.started: list[dict[str, Any]] = []
        self.progressed: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.uploaded_artifact: bytes | None = None

    def get_manifest(self) -> dict[str, Any]:
        self.events.append("manifest")
        return self.manifest

    def download_dataset(
        self,
        _url: str,
        destination: Path,
        expected_sha256: str,
    ) -> int:
        self.events.append("download")
        payload = self.parquet_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise DatasetValidationError("Parquet SHA-256 mismatch")
        destination.write_bytes(payload)
        return len(payload)

    def start(self) -> None:
        self.events.append("start")
        self.started.append({})

    def progress(self, progress: float, message: str | None = None) -> None:
        self.events.append("progress")
        self.progressed.append(
            {"progress": progress, "message": message}
        )

    def complete(
        self,
        report: dict[str, Any],
        artifact_path: Path,
    ) -> None:
        self.events.append("complete")
        self.completed.append(report)
        self.uploaded_artifact = artifact_path.read_bytes()

    def fail(self, error: str) -> None:
        self.events.append("fail")
        self.failed.append({"error": error})


@pytest.fixture
def parquet_file(tmp_path: Path) -> Path:
    dates = pd.date_range("2024-01-01", periods=6, freq="D")
    columns = pd.MultiIndex.from_product(
        [["000001"], FIELDS],
        names=["code", "field"],
    )
    rows = [
        [10.0, 11.0, 9.0, 10.5, 1000.0, 10000.0],
        [10.5, 11.5, 10.0, 11.0, 1100.0, 11000.0],
        [11.0, 12.0, 10.5, 11.5, 1200.0, 12000.0],
        [11.5, 12.5, 11.0, 12.0, 1300.0, 13000.0],
        [12.0, 13.0, 11.5, 12.5, 1400.0, 14000.0],
        [12.5, 13.5, 12.0, 13.0, 1500.0, 15000.0],
    ]
    path = tmp_path / "training.parquet"
    pd.DataFrame(rows, index=dates, columns=columns).to_parquet(path)
    return path


def _manifest(parquet_path: Path) -> dict[str, Any]:
    params = {"label_horizon_days": 1}
    source_path = Path(__file__).resolve()
    return {
        "schema_version": "remote-training/v1",
        "task_uuid": TASK_ID,
        "experiment_id": 42,
        "strategy": {
            "id": "fake_trainable_v1",
            "source_sha256": file_sha256(source_path),
        },
        "params": params,
        "params_sha256": sha256_json(params),
        "training": {
            "train_start": "2024-01-02",
            "train_end": "2024-01-05",
            "label_horizon_days": 1,
        },
        "dataset": {
            "url": "data",
            "sha256": file_sha256(parquet_path),
            "data_version": "test-data-v1",
            "date_start": "2024-01-01",
            "date_end": "2024-01-06",
            "rows": 6,
            "columns": 6,
            "fields": list(FIELDS),
        },
        "artifact": {
            "max_bytes": 1024 * 1024,
            "suggested_name": "model_v1.joblib",
        },
    }


def _runner(
    tmp_path: Path,
    parquet_path: Path,
    manifest: dict[str, Any],
) -> tuple[RemoteTrainingRunner, FakeTransport, list[str]]:
    events: list[str] = []
    registry = FakeRegistry(events)
    transport = FakeTransport(manifest, parquet_path, events)
    runner = RemoteTrainingRunner(
        transport,  # type: ignore[arg-type]
        tmp_path / "output",
        registry=registry,
        project_root=tmp_path,
    )
    return runner, transport, events


def test_successful_training_is_atomic_and_uploads_artifact(
    tmp_path: Path,
    parquet_file: Path,
) -> None:
    runner, transport, events = _runner(
        tmp_path,
        parquet_file,
        _manifest(parquet_file),
    )

    report = runner.run()

    assert events.index("scan") < events.index("download")
    assert events.index("prepare") < events.index("fit") < events.index("save")
    assert events[-1] == "complete"
    assert len(transport.started) == 1
    assert len(transport.completed) == 1
    assert transport.failed == []
    assert transport.uploaded_artifact
    bundle_dir = Path(report["bundle_dir"])
    assert bundle_dir == tmp_path / "output" / TASK_ID
    assert (bundle_dir / "model_v1.joblib").read_bytes() == (
        transport.uploaded_artifact
    )
    saved_report = json.loads((bundle_dir / "report.json").read_text("utf-8"))
    assert saved_report["artifact"]["sha256"] == report["artifact"]["sha256"]
    assert list((tmp_path / "output").glob(f".{TASK_ID}.*")) == []


def test_dataset_hash_error_is_reported(
    tmp_path: Path,
    parquet_file: Path,
) -> None:
    manifest = _manifest(parquet_file)
    manifest["dataset"]["sha256"] = "0" * 64
    runner, transport, events = _runner(tmp_path, parquet_file, manifest)

    with pytest.raises(DatasetValidationError, match="SHA-256 mismatch"):
        runner.run()

    assert "fit" not in events
    assert len(transport.failed) == 1
    assert transport.completed == []


def test_strategy_source_mismatch_fails_before_dataset_read(
    tmp_path: Path,
    parquet_file: Path,
) -> None:
    manifest = _manifest(parquet_file)
    manifest["strategy"]["source_sha256"] = "f" * 64
    runner, transport, events = _runner(tmp_path, parquet_file, manifest)

    with pytest.raises(StrategyValidationError, match="source SHA-256 mismatch"):
        runner.run()

    assert "scan" in events
    assert "download" not in events
    assert len(transport.failed) == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda manifest: manifest.update(
                {"schema_version": "remote-training/v2"}
            ),
            "unsupported schema_version",
        ),
        (
            lambda manifest: manifest["artifact"].update(
                {"suggested_name": r"..\stolen.joblib"}
            ),
            "plain file name",
        ),
    ],
)
def test_invalid_manifest_schema_or_path_is_rejected_and_reported(
    tmp_path: Path,
    parquet_file: Path,
    mutate: Any,
    message: str,
) -> None:
    manifest = _manifest(parquet_file)
    mutate(manifest)
    runner, transport, events = _runner(tmp_path, parquet_file, manifest)

    with pytest.raises(ManifestValidationError, match=message):
        runner.run()

    assert "scan" not in events
    assert "download" not in events
    assert len(transport.failed) == 1


def test_dataset_shape_error_cleans_temporary_bundle_and_reports_failure(
    tmp_path: Path,
    parquet_file: Path,
) -> None:
    manifest = _manifest(parquet_file)
    manifest["dataset"]["rows"] = 99
    runner, transport, _events = _runner(tmp_path, parquet_file, manifest)

    with pytest.raises(DatasetValidationError, match="row count mismatch"):
        runner.run()

    output_dir = tmp_path / "output"
    assert not (output_dir / TASK_ID).exists()
    assert list(output_dir.iterdir()) == []
    assert len(transport.failed) == 1


def test_dry_run_validates_without_server_state_changes(
    tmp_path: Path,
    parquet_file: Path,
) -> None:
    runner, transport, events = _runner(
        tmp_path,
        parquet_file,
        _manifest(parquet_file),
    )

    report = runner.run(dry_run=True)

    assert report["status"] == "validated"
    assert events.index("scan") < events.index("download")
    assert "prepare" not in events
    assert transport.started == []
    assert transport.completed == []
    assert transport.failed == []
    assert not (tmp_path / "output" / TASK_ID).exists()


def test_http_client_uses_training_header_and_multipart_completion(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    artifact = tmp_path / "model.joblib"
    artifact.write_bytes(b"trusted-model")
    mock_http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://quant.example.com",
    )
    client = RemoteTrainingHTTPClient(
        "https://quant.example.com",
        TASK_ID,
        "one-time-secret",
        http_client=mock_http,
    )

    client.start()
    client.progress(0.5, "Halfway")
    client.complete({"task_uuid": TASK_ID}, artifact)
    client.fail("test")

    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "start",
        "progress",
        "complete",
        "fail",
    ]
    assert all(
        request.headers["X-Training-Token"] == "one-time-secret"
        for request in requests
    )
    complete_request = requests[2]
    assert requests[0].content == b""
    assert json.loads(requests[1].content) == {
        "progress": 0.5,
        "message": "Halfway",
    }
    assert complete_request.headers["content-type"].startswith(
        "multipart/form-data;"
    )
    body = complete_request.content
    assert b'name="report_json"' in body
    assert b'name="artifact"; filename="model.joblib"' in body
    assert b"trusted-model" in body
    assert b"one-time-secret" not in body
    assert json.loads(requests[3].content) == {"error": "test"}


def test_real_api_envelope_manifest_and_worker_contract_round_trip(
    tmp_path: Path,
    parquet_file: Path,
) -> None:
    params = {"label_horizon_days": 1}
    api_manifest = {
        "schema_version": "remote-training-bundle/v1",
        "task": {
            "task_uuid": TASK_ID,
            "experiment_id": 42,
            "single_window_only": True,
            "completes_walk_forward": False,
        },
        "strategy": {
            "strategy_id": "fake_trainable_v1",
            "version": "1.0.0",
            "retrain_frequency": "never",
            "source_sha256": file_sha256(Path(__file__).resolve()),
        },
        "params": params,
        "params_sha256": sha256_json(params),
        "windows": {
            "effective_train_start": "2024-01-02",
            "effective_train_end": "2024-01-05",
            "data_start": "2024-01-01",
            "data_end": "2024-01-06",
        },
        "data": {
            "sha256": file_sha256(parquet_file),
            "data_version": "api-snapshot-v1",
            "rows": 6,
            "columns": 6,
            "fields": list(FIELDS),
        },
        "max_artifact_bytes": 1024 * 1024,
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "bundle":
            return httpx.Response(200, json={"data": api_manifest})
        if endpoint == "data":
            return httpx.Response(
                200,
                content=parquet_file.read_bytes(),
                headers={
                    "content-type": "application/vnd.apache.parquet",
                },
            )
        return httpx.Response(200, json={"data": {"status": endpoint}})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    transport = RemoteTrainingHTTPClient(
        "https://quant.example.com",
        TASK_ID,
        "one-time-secret",
        http_client=mock_http,
    )
    events: list[str] = []
    runner = RemoteTrainingRunner(
        transport,
        tmp_path / "contract-output",
        registry=FakeRegistry(events),
        project_root=tmp_path,
    )

    report = runner.run()

    endpoints = [
        request.url.path.rsplit("/", 1)[-1]
        for request in requests
    ]
    assert endpoints == [
        "bundle",
        "data",
        "start",
        "progress",
        "progress",
        "complete",
    ]
    start_request = requests[2]
    assert start_request.content == b""
    for progress_request in requests[3:5]:
        assert set(json.loads(progress_request.content)) == {
            "progress",
            "message",
        }
    complete_body = requests[5].content
    assert b"remote-training-result/v1" in complete_body
    assert b'data_sha256' in complete_body
    assert b'dataset_sha256' not in complete_body
    assert report["schema_version"] == "remote-training-result/v1"
    assert report["task_uuid"] == TASK_ID
    assert report["experiment_id"] == 42
    assert report["strategy_id"] == "fake_trainable_v1"
    assert report["params_sha256"] == sha256_json(params)
    assert report["data_sha256"] == file_sha256(parquet_file)
    assert (Path(report["bundle_dir"]) / "model.joblib").is_file()
