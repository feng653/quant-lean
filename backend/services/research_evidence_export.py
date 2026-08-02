"""Safe, bounded-memory export of completed experiment research evidence."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, AsyncIterator, BinaryIO, Mapping
import zipfile

import aiosqlite

from backend.api.timestamps import serialize_utc_timestamp
from backend.services.research_manifest import (
    ManifestError,
    canonical_sha256,
)
from backend.services.research_risk import research_risk_summary


RESEARCH_EVIDENCE_SCHEMA = "research-evidence-export/v1"
JSON_MEDIA_TYPE = "application/json"
ZIP_MEDIA_TYPE = "application/zip"
_FETCH_BATCH_SIZE = 500
_STREAM_CHUNK_SIZE = 64 * 1024
_SPOOL_MEMORY_LIMIT = 1024 * 1024
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "refresh_token",
    "secret",
    "token",
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")

_EXPERIMENT_EXPORT_FIELDS = (
    "id",
    "name",
    "strategy_id",
    "strategy_category",
    "labels",
    "pool_preset",
    "pool_custom_codes",
    "pool_industries",
    "train_start",
    "train_end",
    "test_start",
    "test_end",
    "params",
    "params_hash",
    "mode",
    "requires_training",
    "retrain_frequency",
    "data_version",
    "code_version",
    "run_spec",
    "status",
    "source_experiment_id",
    "created_at",
    "started_at",
    "completed_at",
)
_UTC_FIELDS = frozenset(
    {"created_at", "started_at", "completed_at", "updated_at", "generated_at"}
)
_INTERNAL_METRIC_FIELDS = frozenset({"id", "experiment_id", "created_at"})
_INTERNAL_TRADE_FIELDS = frozenset({"id", "experiment_id"})


class ResearchEvidenceExportError(RuntimeError):
    """A safe API error raised before response headers are sent."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_absolute_local_path(value: str) -> bool:
    stripped = value.strip()
    return (
        stripped.startswith(("/", "\\\\", "file://"))
        or bool(_WINDOWS_ABSOLUTE_PATH.match(stripped))
    )


def sanitize_export_value(value: Any, *, key: str = "") -> Any:
    """Return finite JSON data without secrets or local absolute paths."""
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return serialize_utc_timestamp(value)
    if isinstance(value, str):
        if _is_absolute_local_path(value):
            return "[REDACTED_LOCAL_PATH]"
        return value
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_export_value(
                item_value,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_export_value(item) for item in value]
    return str(value)


def csv_safe_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula execution while preserving numbers."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        value = json.dumps(
            sanitize_export_value(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
    if value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    if value and value[0].isspace() and value.lstrip().startswith(
        _CSV_FORMULA_PREFIXES
    ):
        return "'" + value
    return value


def _loads_json_field(value: Any, *, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _row_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        sanitize_export_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def _open_readonly(db_path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(
        f"{db_path.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA query_only=ON")
    return connection


async def prepare_research_evidence(
    db_path: Path,
    experiment_id: int,
    user: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate access and collect bounded export metadata."""
    if not db_path.is_file():
        raise ResearchEvidenceExportError(
            "experiment_database_unavailable",
            "实验数据库不可用",
            status_code=503,
        )
    try:
        connection = await _open_readonly(db_path)
    except aiosqlite.Error as exc:
        raise ResearchEvidenceExportError(
            "experiment_database_unavailable",
            "实验数据库不可用",
            status_code=503,
        ) from exc
    try:
        cursor = await connection.execute(
            "SELECT * FROM experiments WHERE id=?",
            (experiment_id,),
        )
        experiment_row = await cursor.fetchone()
        if (
            experiment_row is None
            or (
                not user.get("is_admin")
                and int(experiment_row["user_id"]) != int(user["id"])
            )
        ):
            raise ResearchEvidenceExportError(
                "experiment_not_found",
                "实验不存在",
                status_code=404,
            )
        if experiment_row["status"] != "completed":
            raise ResearchEvidenceExportError(
                "experiment_not_completed",
                "仅已完成实验可导出研究证据",
                status_code=409,
            )

        raw_experiment = _row_dict(experiment_row)
        experiment: dict[str, Any] = {}
        for field in _EXPERIMENT_EXPORT_FIELDS:
            if field not in raw_experiment:
                continue
            value = raw_experiment[field]
            if field in {
                "labels",
                "params",
                "pool_custom_codes",
                "pool_industries",
                "run_spec",
            }:
                value = _loads_json_field(
                    value,
                    default=(
                        {}
                        if field in {"params", "run_spec"}
                        else []
                    ),
                )
            if field in _UTC_FIELDS and value is not None:
                value = serialize_utc_timestamp(value)
            experiment[field] = sanitize_export_value(value, key=field)
        run_spec = experiment.get("run_spec")
        experiment["data_access_policy"] = (
            str(run_spec.get("data_access_policy", "allow_fetch"))
            if isinstance(run_spec, Mapping)
            else "allow_fetch"
        )

        cursor = await connection.execute(
            "SELECT * FROM experiment_metrics WHERE experiment_id=?",
            (experiment_id,),
        )
        metrics_row = await cursor.fetchone()
        metrics = (
            {
                key: sanitize_export_value(metrics_row[key], key=key)
                for key in metrics_row.keys()
                if key not in _INTERNAL_METRIC_FIELDS
            }
            if metrics_row is not None
            else None
        )

        cursor = await connection.execute(
            """
            SELECT schema_version, manifest_json, manifest_hash, created_at
            FROM research_run_manifests WHERE experiment_id=?
            """,
            (experiment_id,),
        )
        manifest_row = await cursor.fetchone()
        research_manifest: dict[str, Any] | None = None
        manifest_json: str | None = None
        manifest_hash: str | None = None
        manifest_schema: str | None = None
        if manifest_row is not None:
            manifest_json = str(manifest_row["manifest_json"])
            manifest_hash = str(manifest_row["manifest_hash"])
            manifest_schema = str(manifest_row["schema_version"])
            try:
                manifest = json.loads(manifest_json)
                actual_hash = canonical_sha256(manifest)
            except (json.JSONDecodeError, ManifestError, TypeError) as exc:
                raise ResearchEvidenceExportError(
                    "manifest_integrity_failure",
                    "研究清单完整性校验失败",
                    status_code=409,
                ) from exc
            if actual_hash != manifest_hash:
                raise ResearchEvidenceExportError(
                    "manifest_integrity_failure",
                    "研究清单完整性校验失败",
                    status_code=409,
                )
            safe_manifest = sanitize_export_value(manifest)
            if safe_manifest != manifest:
                raise ResearchEvidenceExportError(
                    "manifest_contains_unsafe_data",
                    "研究清单含不可导出的敏感字段或本机路径",
                    status_code=409,
                )
            cursor = await connection.execute(
                """
                SELECT artifact_kind, artifact_sha256, artifact_size,
                       metadata_json, created_at
                FROM research_artifact_manifests
                WHERE experiment_id=? ORDER BY id
                """,
                (experiment_id,),
            )
            artifacts: list[dict[str, Any]] = []
            for item in await cursor.fetchall():
                metadata = _loads_json_field(
                    item["metadata_json"],
                    default={},
                )
                artifacts.append(
                    sanitize_export_value(
                        {
                            "artifact_kind": item["artifact_kind"],
                            "sha256": item["artifact_sha256"],
                            "size_bytes": item["artifact_size"],
                            "metadata": metadata,
                            "created_at": serialize_utc_timestamp(
                                item["created_at"]
                            ),
                        }
                    )
                )
            research_manifest = {
                "schema_version": manifest_schema,
                "manifest": manifest,
                "manifest_hash": manifest_hash,
                "created_at": serialize_utc_timestamp(
                    manifest_row["created_at"]
                ),
                "artifacts": artifacts,
            }

        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM equity_curve WHERE experiment_id=?",
            (experiment_id,),
        )
        equity_count = int((await cursor.fetchone())["count"])
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM trade_log WHERE experiment_id=?",
            (experiment_id,),
        )
        trade_count = int((await cursor.fetchone())["count"])
    except ResearchEvidenceExportError:
        raise
    except (aiosqlite.Error, KeyError, TypeError, ValueError) as exc:
        raise ResearchEvidenceExportError(
            "research_evidence_unavailable",
            "研究证据暂不可导出",
            status_code=503,
        ) from exc
    finally:
        await connection.close()

    risk = research_risk_summary(
        manifest_json=manifest_json,
        manifest_hash=manifest_hash,
        schema_version=manifest_schema,
    )
    warnings: list[str] = []
    if metrics is None:
        warnings.append("metrics_missing")
    if research_manifest is None:
        warnings.append("immutable_manifest_missing")
    if equity_count == 0:
        warnings.append("equity_curve_missing")
    data_lineage: dict[str, Any] = {}
    if research_manifest is not None:
        manifest = research_manifest["manifest"]
        for key in (
            "dataset",
            "universe",
            "market_data_quality",
            "benchmark",
            "research_risk_warnings",
        ):
            if key in manifest:
                data_lineage[key] = manifest[key]

    return {
        "schema_version": RESEARCH_EVIDENCE_SCHEMA,
        "generated_at": serialize_utc_timestamp(
            datetime.now(timezone.utc)
        ),
        "experiment": experiment,
        "metrics": metrics,
        "research_manifest": research_manifest,
        "data_lineage": data_lineage,
        "risk_summary": {
            **risk,
            "evidence_warnings": sorted(warnings),
        },
        "evidence_completeness": {
            "metrics_present": metrics is not None,
            "immutable_manifest_present": research_manifest is not None,
            "equity_points": equity_count,
            "trades": trade_count,
        },
    }


async def stream_json_evidence(
    db_path: Path,
    experiment_id: int,
    context: Mapping[str, Any],
) -> AsyncIterator[bytes]:
    """Stream the two unbounded result arrays in finite database batches."""
    fixed = dict(context)
    fixed.pop("equity_curve", None)
    fixed.pop("trades", None)
    prefix = _json_bytes(fixed)
    yield prefix[:-1] + b',"equity_curve":['
    connection = await _open_readonly(db_path)
    try:
        cursor = await connection.execute(
            """
            SELECT date, equity, benchmark, daily_return, drawdown
            FROM equity_curve WHERE experiment_id=? ORDER BY date, id
            """,
            (experiment_id,),
        )
        first = True
        while rows := await cursor.fetchmany(_FETCH_BATCH_SIZE):
            for row in rows:
                if not first:
                    yield b","
                yield _json_bytes(_row_dict(row))
                first = False
        yield b'],"trades":['
        cursor = await connection.execute(
            """
            SELECT * FROM trade_log
            WHERE experiment_id=? ORDER BY date, id
            """,
            (experiment_id,),
        )
        first = True
        while rows := await cursor.fetchmany(_FETCH_BATCH_SIZE):
            for row in rows:
                trade = {
                    key: row[key]
                    for key in row.keys()
                    if key not in _INTERNAL_TRADE_FIELDS
                }
                if not first:
                    yield b","
                yield _json_bytes(trade)
                first = False
        yield b"]}"
    finally:
        await connection.close()


def _write_csv_row(writer: csv.writer, values: list[Any]) -> None:
    writer.writerow([csv_safe_cell(value) for value in values])


def _write_mapping_csv(
    archive: zipfile.ZipFile,
    filename: str,
    payload: Mapping[str, Any] | None,
) -> None:
    with archive.open(filename, "w") as raw:
        with io.TextIOWrapper(
            raw,
            encoding="utf-8-sig",
            newline="",
        ) as text:
            writer = csv.writer(text)
            keys = list(payload.keys()) if payload else []
            _write_csv_row(writer, keys)
            if payload:
                values = [
                    (
                        json.dumps(
                            value,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                        )
                        if isinstance(value, (dict, list))
                        else value
                    )
                    for value in payload.values()
                ]
                _write_csv_row(writer, values)


async def build_csv_zip_evidence(
    db_path: Path,
    experiment_id: int,
    context: Mapping[str, Any],
) -> BinaryIO:
    """Build a ZIP on a spooled file; memory use stays bounded."""
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_LIMIT)
    try:
        with zipfile.ZipFile(
            spool,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            metadata = {
                key: value
                for key, value in context.items()
                if key
                not in {
                    "experiment",
                    "metrics",
                    "research_manifest",
                    "data_lineage",
                    "risk_summary",
                    "evidence_completeness",
                }
            }
            _write_mapping_csv(archive, "metadata.csv", metadata)
            _write_mapping_csv(
                archive,
                "experiment.csv",
                context["experiment"],
            )
            _write_mapping_csv(archive, "metrics.csv", context["metrics"])
            _write_mapping_csv(
                archive,
                "risk_summary.csv",
                context["risk_summary"],
            )
            _write_mapping_csv(
                archive,
                "evidence_completeness.csv",
                context["evidence_completeness"],
            )
            archive.writestr(
                "research_manifest.json",
                _json_bytes(context["research_manifest"]),
            )
            archive.writestr(
                "data_lineage.json",
                _json_bytes(context["data_lineage"]),
            )

            connection = await _open_readonly(db_path)
            try:
                with archive.open("equity_curve.csv", "w") as raw:
                    with io.TextIOWrapper(
                        raw,
                        encoding="utf-8-sig",
                        newline="",
                    ) as text:
                        writer = csv.writer(text)
                        columns = [
                            "date",
                            "equity",
                            "benchmark",
                            "daily_return",
                            "drawdown",
                        ]
                        _write_csv_row(writer, columns)
                        cursor = await connection.execute(
                            """
                            SELECT date, equity, benchmark, daily_return,
                                   drawdown
                            FROM equity_curve
                            WHERE experiment_id=? ORDER BY date, id
                            """,
                            (experiment_id,),
                        )
                        while rows := await cursor.fetchmany(_FETCH_BATCH_SIZE):
                            for row in rows:
                                _write_csv_row(
                                    writer,
                                    [row[column] for column in columns],
                                )

                cursor = await connection.execute(
                    "SELECT * FROM trade_log WHERE experiment_id=? LIMIT 0",
                    (experiment_id,),
                )
                trade_columns = [
                    item[0]
                    for item in cursor.description or []
                    if item[0] not in _INTERNAL_TRADE_FIELDS
                ]
                with archive.open("trades.csv", "w") as raw:
                    with io.TextIOWrapper(
                        raw,
                        encoding="utf-8-sig",
                        newline="",
                    ) as text:
                        writer = csv.writer(text)
                        _write_csv_row(writer, trade_columns)
                        cursor = await connection.execute(
                            """
                            SELECT * FROM trade_log
                            WHERE experiment_id=? ORDER BY date, id
                            """,
                            (experiment_id,),
                        )
                        while rows := await cursor.fetchmany(_FETCH_BATCH_SIZE):
                            for row in rows:
                                _write_csv_row(
                                    writer,
                                    [
                                        row[column]
                                        if not isinstance(row[column], str)
                                        else sanitize_export_value(
                                            row[column],
                                            key=column,
                                        )
                                        for column in trade_columns
                                    ],
                                )
            finally:
                await connection.close()
        spool.seek(0)
        return spool
    except Exception:
        spool.close()
        raise


async def stream_binary_file(handle: BinaryIO) -> AsyncIterator[bytes]:
    try:
        while chunk := handle.read(_STREAM_CHUNK_SIZE):
            yield chunk
    finally:
        handle.close()
