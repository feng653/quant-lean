"""Validation and typed access for ``remote-training/v1`` manifests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import PurePath, PurePosixPath, PureWindowsPath
import re
from typing import Any
from urllib.parse import urlsplit

from .errors import ManifestValidationError


SCHEMA_VERSION = "remote-training/v1"
SERVER_SCHEMA_VERSION = "remote-training-bundle/v1"
RESULT_SCHEMA_VERSION = "remote-training-result/v1"
REQUIRED_OHLCV_FIELDS = frozenset(
    {"open", "high", "low", "close", "volume", "amount"}
)
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_STRATEGY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TASK_UUID = re.compile(r"^[0-9a-f]{32}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical representation used by manifest hashes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{name} must be a JSON object")
    return value


def _text(value: Any, name: str, *, max_length: int = 2048) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ManifestValidationError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ManifestValidationError(f"{name} contains a NUL byte")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _sha256(value: Any, name: str) -> str:
    digest = _text(value, name, max_length=64).lower()
    if not _HEX_256.fullmatch(digest):
        raise ManifestValidationError(f"{name} must be a lowercase SHA-256 digest")
    return digest


def _date(value: Any, name: str) -> date:
    raw = _text(value, name, max_length=10)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ManifestValidationError(f"{name} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise ManifestValidationError(f"{name} must use YYYY-MM-DD")
    return parsed


def validate_task_uuid(value: str, *, name: str = "task-id") -> str:
    raw = _text(value, name, max_length=32)
    if not _TASK_UUID.fullmatch(raw):
        raise ManifestValidationError(
            f"{name} must be 32 lowercase hexadecimal characters"
        )
    return raw


def normalize_server_manifest(
    payload: Any,
    *,
    expected_task_uuid: str,
) -> dict[str, Any]:
    """Convert the server bundle schema to the worker's internal v1 shape."""
    root = _mapping(payload, "bundle")
    schema = _text(
        root.get("schema_version"),
        "schema_version",
        max_length=64,
    )
    if schema != SERVER_SCHEMA_VERSION:
        raise ManifestValidationError(
            f"unsupported server schema_version: {schema!r}"
        )
    task = _mapping(root.get("task"), "task")
    strategy = _mapping(root.get("strategy"), "strategy")
    windows = _mapping(root.get("windows"), "windows")
    data = _mapping(root.get("data"), "data")
    params = _mapping(root.get("params"), "params")
    task_uuid = validate_task_uuid(
        task.get("task_uuid"),
        name="task.task_uuid",
    )
    if task_uuid != validate_task_uuid(expected_task_uuid):
        raise ManifestValidationError(
            "task.task_uuid does not match --task-id"
        )
    label_horizon = windows.get(
        "label_tail_rows",
        params.get("label_horizon_days", 21),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "task_uuid": task_uuid,
        "experiment_id": task.get("experiment_id"),
        "strategy": {
            "id": strategy.get("strategy_id"),
            "source_sha256": strategy.get("source_sha256"),
        },
        "params": params,
        "params_sha256": root.get("params_sha256"),
        "training": {
            "train_start": windows.get("effective_train_start"),
            "train_end": windows.get("effective_train_end"),
            "label_horizon_days": label_horizon,
        },
        "dataset": {
            "url": "data",
            "sha256": data.get("sha256"),
            "data_version": data.get("data_version"),
            "date_start": windows.get("data_start"),
            "date_end": windows.get("data_end"),
            "rows": data.get("rows"),
            "columns": data.get("columns"),
            "fields": data.get("fields"),
        },
        "artifact": {
            "max_bytes": root.get("max_artifact_bytes"),
            "suggested_name": "model.joblib",
        },
    }


def validate_suggested_name(value: Any) -> str:
    name = _text(value, "artifact.suggested_name", max_length=128)
    if (
        PurePath(name).name != name
        or PurePosixPath(name).name != name
        or PureWindowsPath(name).name != name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise ManifestValidationError(
            "artifact.suggested_name must be a plain file name"
        )
    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED or name.endswith((" ", ".")):
        raise ManifestValidationError(
            "artifact.suggested_name is not valid on Windows"
        )
    return name


@dataclass(frozen=True)
class StrategyManifest:
    id: str
    source_sha256: str


@dataclass(frozen=True)
class TrainingManifest:
    train_start: str
    train_end: str
    label_horizon_days: int


@dataclass(frozen=True)
class DatasetManifest:
    url: str
    sha256: str
    data_version: str
    date_start: str
    date_end: str
    rows: int
    columns: int
    fields: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactManifest:
    max_bytes: int
    suggested_name: str


@dataclass(frozen=True)
class RemoteTrainingManifest:
    schema_version: str
    task_uuid: str
    experiment_id: int
    strategy: StrategyManifest
    params: dict[str, Any]
    params_sha256: str
    training: TrainingManifest
    dataset: DatasetManifest
    artifact: ArtifactManifest

    @classmethod
    def parse(
        cls,
        payload: Any,
        *,
        expected_task_uuid: str,
    ) -> "RemoteTrainingManifest":
        root = _mapping(payload, "manifest")
        schema_version = _text(
            root.get("schema_version"), "schema_version", max_length=64
        )
        if schema_version != SCHEMA_VERSION:
            raise ManifestValidationError(
                f"unsupported schema_version: {schema_version!r}"
            )

        task_uuid = validate_task_uuid(
            root.get("task_uuid"), name="task_uuid"
        )
        expected = validate_task_uuid(expected_task_uuid)
        if task_uuid != expected:
            raise ManifestValidationError("task_uuid does not match --task-id")

        strategy_obj = _mapping(root.get("strategy"), "strategy")
        strategy_id = _text(
            strategy_obj.get("id"), "strategy.id", max_length=128
        )
        if not _STRATEGY_ID.fullmatch(strategy_id):
            raise ManifestValidationError("strategy.id contains unsafe characters")

        params = _mapping(root.get("params"), "params")
        try:
            computed_params_hash = sha256_json(params)
        except (TypeError, ValueError) as exc:
            raise ManifestValidationError(
                "params must contain finite JSON values"
            ) from exc
        params_hash = _sha256(root.get("params_sha256"), "params_sha256")
        if computed_params_hash != params_hash:
            raise ManifestValidationError("params_sha256 does not match params")

        training_obj = _mapping(root.get("training"), "training")
        train_start = _date(
            training_obj.get("train_start"), "training.train_start"
        )
        train_end = _date(training_obj.get("train_end"), "training.train_end")
        if train_start >= train_end:
            raise ManifestValidationError(
                "training.train_start must be earlier than training.train_end"
            )

        dataset_obj = _mapping(root.get("dataset"), "dataset")
        date_start = _date(
            dataset_obj.get("date_start"), "dataset.date_start"
        )
        date_end = _date(dataset_obj.get("date_end"), "dataset.date_end")
        if date_start > train_start or date_end < train_end:
            raise ManifestValidationError(
                "training window must be contained in the dataset window"
            )
        fields_value = dataset_obj.get("fields")
        if (
            not isinstance(fields_value, list)
            or not fields_value
            or any(not isinstance(item, str) or not item for item in fields_value)
        ):
            raise ManifestValidationError(
                "dataset.fields must be a non-empty string array"
            )
        fields = tuple(str(item).lower() for item in fields_value)
        if len(set(fields)) != len(fields):
            raise ManifestValidationError("dataset.fields contains duplicates")
        missing = REQUIRED_OHLCV_FIELDS.difference(fields)
        if missing:
            raise ManifestValidationError(
                "dataset.fields is missing OHLCV fields: "
                + ", ".join(sorted(missing))
            )

        dataset_url = _text(
            dataset_obj.get("url"), "dataset.url", max_length=2048
        )
        parsed_url = urlsplit(dataset_url)
        if parsed_url.scheme not in {"", "http", "https"}:
            raise ManifestValidationError(
                "dataset.url must be HTTP(S) or a relative URL"
            )
        if parsed_url.username or parsed_url.password or parsed_url.fragment:
            raise ManifestValidationError("dataset.url contains unsafe URL parts")

        artifact_obj = _mapping(root.get("artifact"), "artifact")
        return cls(
            schema_version=schema_version,
            task_uuid=task_uuid,
            experiment_id=_integer(
                root.get("experiment_id"), "experiment_id", minimum=1
            ),
            strategy=StrategyManifest(
                id=strategy_id,
                source_sha256=_sha256(
                    strategy_obj.get("source_sha256"),
                    "strategy.source_sha256",
                ),
            ),
            params=dict(params),
            params_sha256=params_hash,
            training=TrainingManifest(
                train_start=train_start.isoformat(),
                train_end=train_end.isoformat(),
                label_horizon_days=_integer(
                    training_obj.get("label_horizon_days"),
                    "training.label_horizon_days",
                ),
            ),
            dataset=DatasetManifest(
                url=dataset_url,
                sha256=_sha256(
                    dataset_obj.get("sha256"), "dataset.sha256"
                ),
                data_version=_text(
                    dataset_obj.get("data_version"),
                    "dataset.data_version",
                    max_length=512,
                ),
                date_start=date_start.isoformat(),
                date_end=date_end.isoformat(),
                rows=_integer(dataset_obj.get("rows"), "dataset.rows", minimum=1),
                columns=_integer(
                    dataset_obj.get("columns"),
                    "dataset.columns",
                    minimum=1,
                ),
                fields=fields,
            ),
            artifact=ArtifactManifest(
                max_bytes=_integer(
                    artifact_obj.get("max_bytes"),
                    "artifact.max_bytes",
                    minimum=1,
                ),
                suggested_name=validate_suggested_name(
                    artifact_obj.get("suggested_name")
                ),
            ),
        )
