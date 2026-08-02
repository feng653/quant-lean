"""Canonical, content-addressed versions for research datasets.

The public ``compute_data_version`` function remains string-returning for
existing callers.  New integrations can use ``compute_dataset_version`` to
retain the structured version metadata as well.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
import struct
from typing import Any, Mapping

import numpy as np
import pandas as pd
from pandas.api.types import CategoricalDtype


DATASET_VERSION_SCHEMA = "dataset-version/v2"
_PANDAS_HASH_KEY = "p0lineagehashkey"


def _canonical_value(value: Any) -> Any:
    """Convert supported configuration values into a tagged JSON value."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return {"$type": "integer", "value": str(int(value))}
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if np.isnan(number):
            return {"$type": "float", "value": "nan"}
        if np.isposinf(number):
            return {"$type": "float", "value": "+inf"}
        if np.isneginf(number):
            return {"$type": "float", "value": "-inf"}
        return {
            "$type": "float64",
            "value": struct.pack(">d", number).hex(),
        }
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return {"$type": "timestamp", "value": "NaT"}
        return {"$type": "timestamp", "value": value.isoformat()}
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            return {"$type": "datetime64", "value": "NaT"}
        return {
            "$type": "datetime64",
            "value": np.datetime_as_string(value, unit="ns", timezone="UTC"),
        }
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, (pd.Timedelta, np.timedelta64)):
        return {
            "$type": "timedelta-ns",
            "value": str(pd.Timedelta(value).value),
        }
    if isinstance(value, bytes):
        return {"$type": "bytes", "value": value.hex()}
    if isinstance(value, Path):
        return {"$type": "path", "value": value.as_posix()}
    if isinstance(value, Mapping):
        pairs = [
            [_canonical_value(key), _canonical_value(item)]
            for key, item in value.items()
        ]
        pairs.sort(
            key=lambda pair: json.dumps(
                pair[0],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {"$type": "mapping", "items": pairs}
    if isinstance(value, tuple):
        return {
            "$type": "tuple",
            "items": [_canonical_value(item) for item in value],
        }
    if isinstance(value, list):
        return {
            "$type": "list",
            "items": [_canonical_value(item) for item in value],
        }
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        items.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {"$type": "set", "items": items}
    if isinstance(value, np.generic):
        return _canonical_value(value.item())
    raise TypeError(
        f"Unsupported value in canonical dataset context: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON for a supported value."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    """Return a stable SHA-256 digest for configuration/lineage metadata."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _dtype_descriptor(dtype: Any) -> dict[str, Any]:
    if isinstance(dtype, CategoricalDtype):
        return {
            "name": "category",
            "ordered": bool(dtype.ordered),
            "categories": [
                _canonical_value(item) for item in dtype.categories.tolist()
            ],
        }
    return {"name": str(dtype)}


def _frame_content_digest(frame: pd.DataFrame) -> str:
    """Hash values in row/column order, with missing positions explicit."""
    digest = hashlib.sha256()
    try:
        row_hashes = pd.util.hash_pandas_object(
            frame,
            index=False,
            categorize=False,
            hash_key=_PANDAS_HASH_KEY,
        ).to_numpy(dtype="uint64", copy=False)
        digest.update(row_hashes.astype(">u8", copy=False).tobytes())
    except (TypeError, ValueError):
        # Rare object columns may contain unhashable structured values.  The
        # fallback is slower but remains canonical and refuses unstable reprs.
        for row in frame.itertuples(index=False, name=None):
            digest.update(canonical_json_bytes(tuple(row)))
            digest.update(b"\n")

    missing = frame.isna().to_numpy(dtype=np.uint8, copy=False)
    digest.update(b"\x00missing-bitmap\x00")
    digest.update(np.ascontiguousarray(missing).tobytes())
    return digest.hexdigest()


def _axis_metadata(axis: pd.Index) -> dict[str, Any]:
    names = list(axis.names) if isinstance(axis, pd.MultiIndex) else [axis.name]
    labels = axis.tolist()
    return {
        "class": f"{type(axis).__module__}.{type(axis).__qualname__}",
        "names": [_canonical_value(name) for name in names],
        "labels": [_canonical_value(label) for label in labels],
    }


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """Structured dataset fingerprint suitable for persistence and audit."""

    digest: str
    rows: int
    columns: int
    start: str
    end: str
    context_digest: str
    schema_version: str = DATASET_VERSION_SCHEMA

    def __str__(self) -> str:
        return (
            f"dv2|r{self.rows}|c{self.columns}|start{self.start}|end{self.end}"
            f"|sha256:{self.digest}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "digest": self.digest,
            "rows": self.rows,
            "columns": self.columns,
            "start": self.start,
            "end": self.end,
            "context_digest": self.context_digest,
            "version": str(self),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DatasetVersion":
        schema_version = str(payload.get("schema_version", DATASET_VERSION_SCHEMA))
        if schema_version != DATASET_VERSION_SCHEMA:
            raise ValueError(f"Unsupported dataset version schema: {schema_version}")
        version = cls(
            digest=str(payload["digest"]),
            rows=int(payload["rows"]),
            columns=int(payload["columns"]),
            start=str(payload.get("start", "")),
            end=str(payload.get("end", "")),
            context_digest=str(payload["context_digest"]),
            schema_version=schema_version,
        )
        stored = payload.get("version")
        if stored is not None and str(stored) != str(version):
            raise ValueError("Dataset version string does not match its fields")
        return version


def compute_dataset_version(
    pivot: pd.DataFrame,
    context: Mapping[str, Any] | None = None,
) -> DatasetVersion:
    """Compute a canonical fingerprint over data, schema, axes and context.

    ``context`` is where callers bind source identity and semantics such as
    adjustment mode, source options, cleaning version, and universe snapshot
    hash. Mapping key order is intentionally irrelevant; list order is not.
    """
    if pivot.empty:
        raise ValueError("Cannot compute version for empty pivot DataFrame")

    normalized_context = context or {}
    context_digest = canonical_digest(normalized_context)
    index_frame = pivot.index.to_frame(index=False)
    metadata = {
        "schema_version": DATASET_VERSION_SCHEMA,
        "shape": [len(pivot), len(pivot.columns)],
        "columns": _axis_metadata(pivot.columns),
        "column_dtypes": [
            _dtype_descriptor(dtype) for dtype in pivot.dtypes.tolist()
        ],
        "index": _axis_metadata(pivot.index),
        "index_dtypes": [
            _dtype_descriptor(dtype) for dtype in index_frame.dtypes.tolist()
        ],
        "context": normalized_context,
        "index_content_digest": _frame_content_digest(index_frame),
        "value_content_digest": _frame_content_digest(pivot),
    }
    digest = canonical_digest(metadata)
    start, end = extract_date_range(pivot)
    return DatasetVersion(
        digest=digest,
        rows=len(pivot),
        columns=len(pivot.columns),
        start=start,
        end=end,
        context_digest=context_digest,
    )


def compute_data_version(
    pivot: pd.DataFrame,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Backward-compatible string form of :func:`compute_dataset_version`."""
    return str(compute_dataset_version(pivot, context=context))


def version_matches(v1: str | DatasetVersion, v2: str | DatasetVersion) -> bool:
    """Compare two non-empty version identifiers exactly."""
    left = str(v1)
    right = str(v2)
    return left == right and len(left) > 0


def extract_date_range(pivot: pd.DataFrame) -> tuple[str, str]:
    """Return inclusive min/max index labels in the legacy date format."""
    if pivot.empty:
        return ("", "")
    idx = pivot.index
    if isinstance(idx, pd.DatetimeIndex):
        return (idx.min().strftime("%Y-%m-%d"), idx.max().strftime("%Y-%m-%d"))
    return (str(idx.min())[:10], str(idx.max())[:10])
