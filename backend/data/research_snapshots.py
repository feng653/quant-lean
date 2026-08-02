"""Immutable content-addressed Parquet snapshots for exact research replay."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Literal, Mapping

import pandas as pd


SNAPSHOT_SCHEMA = "research-data-snapshot/v1"
PARQUET_SCHEMA = "research-parquet-schema/v1"
_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"pivot", "benchmark"}
_CHUNK_SIZE = 1024 * 1024


class SnapshotError(RuntimeError):
    """Base error for immutable research snapshot operations."""


class SnapshotIntegrityError(SnapshotError):
    """Snapshot evidence or stored bytes failed closed verification."""


def clip_to_test_end(
    values: pd.DataFrame | pd.Series,
    test_end: str | pd.Timestamp,
) -> pd.DataFrame | pd.Series:
    """Return sorted research input with all post-test rows removed."""
    result = values.copy()
    if not isinstance(result.index, pd.DatetimeIndex):
        try:
            result.index = pd.to_datetime(result.index)
        except (TypeError, ValueError) as exc:
            raise SnapshotError("research input index must contain dates") from exc
    result = result.sort_index()
    return result.loc[result.index <= pd.Timestamp(test_end)].copy()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _axis_names(axis: pd.Index) -> list[str | None]:
    names = axis.names if isinstance(axis, pd.MultiIndex) else [axis.name]
    return [None if name is None else str(name) for name in names]


def _labels_digest(axis: pd.Index) -> str:
    hashed = pd.util.hash_pandas_object(axis, index=False).values.tobytes()
    return hashlib.sha256(hashed).hexdigest()


def _frame_schema(frame: pd.DataFrame) -> dict[str, Any]:
    index_frame = frame.index.to_frame(index=False)
    schema = {
        "schema_version": PARQUET_SCHEMA,
        "rows": int(len(frame)),
        "columns": int(len(frame.columns)),
        "index": {
            "type": f"{type(frame.index).__module__}.{type(frame.index).__qualname__}",
            "levels": int(frame.index.nlevels),
            "names": _axis_names(frame.index),
            "dtypes": [str(dtype) for dtype in index_frame.dtypes],
            "labels_sha256": _labels_digest(frame.index),
        },
        "column_axis": {
            "type": (
                f"{type(frame.columns).__module__}."
                f"{type(frame.columns).__qualname__}"
            ),
            "levels": int(frame.columns.nlevels),
            "names": _axis_names(frame.columns),
            "labels_sha256": _labels_digest(frame.columns),
        },
        "dtypes": [str(dtype) for dtype in frame.dtypes],
    }
    return {
        **schema,
        "sha256": hashlib.sha256(_canonical_bytes(schema)).hexdigest(),
    }


def _validated_key(value: Any) -> str:
    key = str(value)
    if _KEY_PATTERN.fullmatch(key) is None:
        raise SnapshotIntegrityError("snapshot key must be a lowercase sha256")
    return key


def _validated_kind(value: Any) -> Literal["pivot", "benchmark"]:
    kind = str(value)
    if kind not in _KINDS:
        raise SnapshotIntegrityError("unsupported research snapshot kind")
    return kind  # type: ignore[return-value]


class ResearchSnapshotStore:
    """Write-once snapshot store addressed by the exact Parquet file hash."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def save_pivot(self, pivot: pd.DataFrame) -> dict[str, Any]:
        if pivot.empty:
            raise SnapshotError("cannot snapshot an empty pivot")
        return self._save_frame(pivot, kind="pivot")

    def load_pivot(self, evidence: Mapping[str, Any]) -> pd.DataFrame:
        return self._load_frame(evidence, expected_kind="pivot")

    def save_benchmark(self, benchmark: pd.Series) -> dict[str, Any]:
        if benchmark.empty:
            raise SnapshotError("cannot snapshot an empty benchmark")
        frame = benchmark.to_frame(name="__benchmark_value__")
        evidence = self._save_frame(frame, kind="benchmark")
        return {
            **evidence,
            "series": {
                "name": None if benchmark.name is None else str(benchmark.name),
                "dtype": str(benchmark.dtype),
            },
        }

    def load_benchmark(self, evidence: Mapping[str, Any]) -> pd.Series:
        frame = self._load_frame(evidence, expected_kind="benchmark")
        if list(frame.columns) != ["__benchmark_value__"]:
            raise SnapshotIntegrityError("benchmark snapshot column is invalid")
        series_evidence = evidence.get("series")
        if not isinstance(series_evidence, Mapping):
            raise SnapshotIntegrityError("benchmark series evidence is missing")
        series = frame["__benchmark_value__"].copy()
        expected_dtype = str(series_evidence.get("dtype", ""))
        if str(series.dtype) != expected_dtype:
            raise SnapshotIntegrityError("benchmark snapshot dtype changed")
        raw_name = series_evidence.get("name")
        series.name = None if raw_name is None else str(raw_name)
        return series

    def verify(self, evidence: Mapping[str, Any]) -> None:
        kind = _validated_kind(evidence.get("kind"))
        self._load_frame(evidence, expected_kind=kind)

    def _save_frame(
        self,
        frame: pd.DataFrame,
        *,
        kind: Literal["pivot", "benchmark"],
    ) -> dict[str, Any]:
        stored_frame = frame.copy()
        if (
            isinstance(stored_frame.index, pd.DatetimeIndex)
            and stored_frame.index.unit == "s"
        ):
            # PyArrow widens second-resolution timestamps to milliseconds when
            # reading Parquet. Normalize first so a valid round trip is not
            # mistaken for snapshot corruption.
            stored_frame.index = stored_frame.index.as_unit("ms")
        kind_dir = self.root / kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        temp_path = kind_dir / f".snapshot-{secrets.token_hex(16)}.tmp"
        try:
            stored_frame.to_parquet(
                temp_path,
                compression="snappy",
                index=True,
            )
            file_hash = _sha256_file(temp_path)
            size_bytes = temp_path.stat().st_size
            target = kind_dir / f"{file_hash}.parquet"
            try:
                os.link(temp_path, target)
            except FileExistsError:
                self._verify_file(target, file_hash, size_bytes)
            evidence = {
                "schema_version": SNAPSHOT_SCHEMA,
                "kind": kind,
                "key": file_hash,
                "relative_key": f"{kind}/{file_hash}.parquet",
                "file_sha256": file_hash,
                "size_bytes": size_bytes,
                "format": "parquet",
                "schema": _frame_schema(stored_frame),
            }
            self._load_frame(evidence, expected_kind=kind)
            return evidence
        finally:
            temp_path.unlink(missing_ok=True)

    def _load_frame(
        self,
        evidence: Mapping[str, Any],
        *,
        expected_kind: Literal["pivot", "benchmark"],
    ) -> pd.DataFrame:
        if str(evidence.get("schema_version")) != SNAPSHOT_SCHEMA:
            raise SnapshotIntegrityError("unsupported research snapshot schema")
        kind = _validated_kind(evidence.get("kind"))
        if kind != expected_kind:
            raise SnapshotIntegrityError("research snapshot kind mismatch")
        key = _validated_key(evidence.get("key"))
        file_hash = _validated_key(evidence.get("file_sha256"))
        if file_hash != key:
            raise SnapshotIntegrityError("snapshot key does not match file hash")
        relative_key = f"{kind}/{key}.parquet"
        if evidence.get("relative_key") != relative_key:
            raise SnapshotIntegrityError("snapshot relative key is invalid")
        if evidence.get("format") != "parquet":
            raise SnapshotIntegrityError("snapshot format is invalid")
        try:
            size_bytes = int(evidence["size_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotIntegrityError("snapshot size evidence is invalid") from exc
        if size_bytes <= 0:
            raise SnapshotIntegrityError("snapshot size evidence is invalid")

        path = self.root / kind / f"{key}.parquet"
        if path.is_symlink():
            raise SnapshotIntegrityError("snapshot symlinks are forbidden")
        expected_parent = (self.root / kind).resolve()
        if path.parent.resolve() != expected_parent:
            raise SnapshotIntegrityError("snapshot path escaped storage root")
        self._verify_file(path, file_hash, size_bytes)
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:
            raise SnapshotIntegrityError("snapshot parquet cannot be loaded") from exc
        expected_schema = evidence.get("schema")
        if not isinstance(expected_schema, Mapping):
            raise SnapshotIntegrityError("snapshot schema evidence is missing")
        if _frame_schema(frame) != dict(expected_schema):
            raise SnapshotIntegrityError("snapshot parquet schema changed")
        return frame

    @staticmethod
    def _verify_file(path: Path, file_hash: str, size_bytes: int) -> None:
        try:
            stat = path.stat()
        except OSError as exc:
            raise SnapshotIntegrityError("snapshot file is missing") from exc
        if stat.st_size != size_bytes:
            raise SnapshotIntegrityError("snapshot file size changed")
        if _sha256_file(path) != file_hash:
            raise SnapshotIntegrityError("snapshot file hash changed")
