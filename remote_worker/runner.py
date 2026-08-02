"""Validated training lifecycle for a trusted Windows worker."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import time
from typing import Any

from .client import RemoteTrainingHTTPClient
from .errors import (
    ArtifactError,
    DatasetValidationError,
    RemoteTrainingError,
    StrategyValidationError,
)
from .manifest import (
    REQUIRED_OHLCV_FIELDS,
    RESULT_SCHEMA_VERSION,
    RemoteTrainingManifest,
)


DEPENDENCY_NAMES = (
    "numpy",
    "pandas",
    "pyarrow",
    "joblib",
    "scikit-learn",
    "lightgbm",
    "xgboost",
    "torch",
)


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in DEPENDENCY_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def doctor_report() -> dict[str, Any]:
    """Return accelerator and runtime information without contacting a server."""
    torch_info: dict[str, Any] = {
        "installed": False,
        "version": None,
        "cuda_available": False,
        "cuda_version": None,
        "gpu_names": [],
    }
    try:
        import torch

        torch_info["installed"] = True
        torch_info["version"] = str(torch.__version__)
        torch_info["cuda_available"] = bool(torch.cuda.is_available())
        torch_info["cuda_version"] = getattr(torch.version, "cuda", None)
        if torch.cuda.is_available():
            torch_info["gpu_names"] = [
                torch.cuda.get_device_name(index)
                for index in range(torch.cuda.device_count())
            ]
    except (ImportError, RuntimeError):
        pass
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "torch": torch_info,
        "dependencies": _dependency_versions(),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strategy_source_path(strategy: Any) -> Path:
    source = inspect.getsourcefile(strategy.__class__)
    if source is None:
        raise StrategyValidationError("strategy source file cannot be resolved")
    path = Path(source).resolve()
    if not path.is_file():
        raise StrategyValidationError("strategy source file does not exist")
    return path


def _actual_model_device(model: Any) -> str:
    try:
        import torch

        if isinstance(model, torch.nn.Module):
            parameter = next(model.parameters(), None)
            return str(parameter.device) if parameter is not None else "cpu"
    except ImportError:
        pass
    return "cpu"


def _safe_error(exc: BaseException, token: str | None = None) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if token:
        message = message.replace(token, "[redacted]")
    return message[:2000]


class RemoteTrainingRunner:
    """Orchestrate one immutable remote-training task."""

    def __init__(
        self,
        transport: RemoteTrainingHTTPClient,
        output_dir: str | Path,
        *,
        device: str = "auto",
        project_root: str | Path | None = None,
        registry: Any | None = None,
    ) -> None:
        if device != "auto":
            raise ValueError(
                "only --device auto is supported; the strategy owns device selection"
            )
        self.transport = transport
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[1]
        )
        self.registry = registry
        self.device = device

    def _load_registry(self) -> Any:
        """Scan before any Parquet read to preserve Windows DLL load order."""
        if self.registry is None:
            from backend.strategies.registry import get_registry

            self.registry = get_registry()
        strategies_dir = self.project_root / "backend" / "strategies"
        self.registry.scan_directory(strategies_dir)
        return self.registry

    def _resolve_strategy(
        self,
        registry: Any,
        manifest: RemoteTrainingManifest,
    ) -> Any:
        try:
            strategy = registry.create_strategy(manifest.strategy.id)
        except KeyError as exc:
            raise StrategyValidationError(
                f"strategy is unavailable: {manifest.strategy.id}"
            ) from exc

        from backend.strategies.base import TrainableStrategy

        if not isinstance(strategy, TrainableStrategy):
            raise StrategyValidationError(
                f"strategy is not TrainableStrategy: {manifest.strategy.id}"
            )
        source_path = _strategy_source_path(strategy)
        if _sha256_file(source_path) != manifest.strategy.source_sha256:
            raise StrategyValidationError("strategy source SHA-256 mismatch")
        valid, error = strategy.validate_params(manifest.params)
        if not valid:
            raise StrategyValidationError(
                f"strategy parameters are invalid: {error}"
            )
        actual_horizon = strategy.label_horizon_days(manifest.params)
        if actual_horizon != manifest.training.label_horizon_days:
            raise StrategyValidationError(
                "strategy label horizon does not match the manifest"
            )
        return strategy

    @staticmethod
    def _validate_dataset(
        parquet_path: Path,
        manifest: RemoteTrainingManifest,
    ) -> Any:
        # Intentionally imported only after Registry.scan_directory().
        import pandas as pd

        try:
            pivot = pd.read_parquet(parquet_path)
        except Exception as exc:
            raise DatasetValidationError("cannot read Parquet dataset") from exc
        if pivot.empty:
            raise DatasetValidationError("Parquet dataset is empty")
        if not isinstance(pivot.index, pd.DatetimeIndex):
            raise DatasetValidationError("Parquet index must be DatetimeIndex")
        if pivot.index.tz is not None:
            raise DatasetValidationError("Parquet DatetimeIndex must be timezone-naive")
        if not pivot.index.is_monotonic_increasing or not pivot.index.is_unique:
            raise DatasetValidationError(
                "Parquet dates must be sorted and unique"
            )
        if not isinstance(pivot.columns, pd.MultiIndex) or pivot.columns.nlevels != 2:
            raise DatasetValidationError(
                "Parquet columns must be a two-level (code, field) MultiIndex"
            )
        if not pivot.columns.is_unique:
            raise DatasetValidationError("Parquet columns must be unique")
        if len(pivot) != manifest.dataset.rows:
            raise DatasetValidationError("Parquet row count mismatch")
        if len(pivot.columns) != manifest.dataset.columns:
            raise DatasetValidationError("Parquet column count mismatch")
        first_date = pivot.index[0].strftime("%Y-%m-%d")
        last_date = pivot.index[-1].strftime("%Y-%m-%d")
        if (
            first_date != manifest.dataset.date_start
            or last_date != manifest.dataset.date_end
        ):
            raise DatasetValidationError("Parquet date range mismatch")
        fields = {
            str(value).lower()
            for value in pivot.columns.get_level_values(-1)
        }
        if fields != set(manifest.dataset.fields):
            raise DatasetValidationError("Parquet field set mismatch")
        missing = REQUIRED_OHLCV_FIELDS.difference(fields)
        if missing:
            raise DatasetValidationError(
                "Parquet is missing fields: " + ", ".join(sorted(missing))
            )
        codes = {
            str(value).strip()
            for value in pivot.columns.get_level_values(0)
        }
        if not codes or "" in codes:
            raise DatasetValidationError("Parquet contains an invalid stock code")
        price_frames: dict[str, Any] = {}
        for field in REQUIRED_OHLCV_FIELDS:
            field_frame = pivot.xs(field, axis=1, level=-1)
            if not all(
                pd.api.types.is_numeric_dtype(dtype)
                for dtype in field_frame.dtypes
            ):
                raise DatasetValidationError(
                    f"Parquet field {field!r} must be numeric"
                )
            price_frames[field] = field_frame
        if (
            (price_frames["volume"] < 0).any(axis=None)
            or (price_frames["amount"] < 0).any(axis=None)
        ):
            raise DatasetValidationError(
                "Parquet volume and amount cannot be negative"
            )
        common_codes = sorted(
            set(price_frames["open"].columns)
            & set(price_frames["high"].columns)
            & set(price_frames["low"].columns)
            & set(price_frames["close"].columns)
        )
        if not common_codes:
            raise DatasetValidationError(
                "Parquet has no stock with complete OHLC columns"
            )
        open_frame = price_frames["open"].reindex(columns=common_codes)
        high_frame = price_frames["high"].reindex(columns=common_codes)
        low_frame = price_frames["low"].reindex(columns=common_codes)
        close_frame = price_frames["close"].reindex(columns=common_codes)
        invalid_high = (
            (high_frame < open_frame)
            | (high_frame < close_frame)
            | (high_frame < low_frame)
        )
        invalid_low = (
            (low_frame > open_frame)
            | (low_frame > close_frame)
            | (low_frame > high_frame)
        )
        if invalid_high.any(axis=None) or invalid_low.any(axis=None):
            raise DatasetValidationError("Parquet contains invalid OHLC bounds")

        train_start = pd.Timestamp(manifest.training.train_start)
        train_end = pd.Timestamp(manifest.training.train_end)
        if train_start < pivot.index[0] or train_end > pivot.index[-1]:
            raise DatasetValidationError(
                "training window is outside the Parquet date range"
            )
        if not ((pivot.index >= train_start) & (pivot.index <= train_end)).any():
            raise DatasetValidationError("training window contains no observations")
        return pivot

    def _bundle_paths(
        self,
        manifest: RemoteTrainingManifest,
    ) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if not self.output_dir.is_dir():
            raise ArtifactError("--output-dir is not a directory")
        final_dir = (self.output_dir / manifest.task_uuid).resolve()
        if final_dir.parent != self.output_dir:
            raise ArtifactError("unsafe bundle output path")
        if final_dir.exists():
            raise ArtifactError(
                f"bundle already exists for task {manifest.task_uuid}"
            )
        temp_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{manifest.task_uuid}.",
                dir=self.output_dir,
            )
        )
        return temp_dir, final_dir

    def _publish_bundle(
        self,
        temp_dir: Path,
        final_dir: Path,
    ) -> None:
        try:
            os.replace(temp_dir, final_dir)
        except OSError as exc:
            raise ArtifactError("cannot atomically publish model bundle") from exc

    def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        manifest: RemoteTrainingManifest | None = None
        temp_dir: Path | None = None
        started = time.monotonic()
        task_id = self.transport.task_id
        try:
            manifest = RemoteTrainingManifest.parse(
                self.transport.get_manifest(),
                expected_task_uuid=task_id,
            )

            # This must happen before download validation imports pandas/pyarrow.
            registry = self._load_registry()
            strategy = self._resolve_strategy(registry, manifest)

            temp_dir, final_dir = self._bundle_paths(manifest)
            dataset_path = temp_dir / "training.parquet"
            dataset_bytes = self.transport.download_dataset(
                manifest.dataset.url,
                dataset_path,
                manifest.dataset.sha256,
            )
            pivot = self._validate_dataset(dataset_path, manifest)

            runtime = doctor_report()
            base_report = {
                "schema_version": manifest.schema_version,
                "task_uuid": manifest.task_uuid,
                "experiment_id": manifest.experiment_id,
                "strategy_id": manifest.strategy.id,
                "params_sha256": manifest.params_sha256,
                "data_sha256": manifest.dataset.sha256,
                "dataset_bytes": dataset_bytes,
                "data_version": manifest.dataset.data_version,
                "device_requested": self.device,
                "runtime": runtime,
            }
            if dry_run:
                shutil.rmtree(temp_dir)
                temp_dir = None
                return {
                    **base_report,
                    "status": "validated",
                    "dry_run": True,
                }

            self.transport.start()
            self.transport.progress(
                0.1,
                "Preparing training features",
            )
            strategy.prepare(pivot, manifest.params)
            self.transport.progress(
                0.2,
                "Fitting model",
            )
            fitted = strategy.fit(
                pivot,
                manifest.params,
                manifest.training.train_start,
                manifest.training.train_end,
            )
            model = getattr(fitted, "model", fitted)
            if model is None:
                model = getattr(strategy, "_model", None)
            if model is None:
                raise ArtifactError("strategy.fit returned no model")
            metrics = strategy.last_train_metrics

            artifact_path = temp_dir / manifest.artifact.suggested_name
            strategy.save_model(model, str(artifact_path))
            if not artifact_path.is_file():
                raise ArtifactError("strategy.save_model did not create a file")
            artifact_size = artifact_path.stat().st_size
            if artifact_size <= 0:
                raise ArtifactError("model artifact is empty")
            if artifact_size > manifest.artifact.max_bytes:
                raise ArtifactError(
                    "model artifact exceeds artifact.max_bytes"
                )
            artifact_sha256 = _sha256_file(artifact_path)
            dataset_path.unlink(missing_ok=True)

            report = {
                **base_report,
                "schema_version": RESULT_SCHEMA_VERSION,
                "status": "completed",
                "progress": 1.0,
                "data_sha256": manifest.dataset.sha256,
                "device_actual": _actual_model_device(model),
                "training": asdict(manifest.training),
                "metrics": metrics,
                "artifact": {
                    "name": artifact_path.name,
                    "bytes": artifact_size,
                    "sha256": artifact_sha256,
                },
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            report_path = temp_dir / "report.json"
            report_path.write_text(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            self._publish_bundle(temp_dir, final_dir)
            temp_dir = None
            final_artifact = final_dir / artifact_path.name
            self.transport.complete(report, final_artifact)
            return {**report, "bundle_dir": str(final_dir)}
        except Exception as exc:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
            failure = {
                "task_uuid": task_id,
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": _safe_error(exc),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            try:
                self.transport.fail(failure["error"])
            except Exception:
                pass
            if isinstance(exc, RemoteTrainingError):
                raise
            raise RemoteTrainingError(_safe_error(exc)) from exc
