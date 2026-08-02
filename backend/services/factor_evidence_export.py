"""Bounded, integrity-checked exports for completed factor research runs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import csv
from datetime import UTC, datetime
import hashlib
import io
import json
import sqlite3
import tempfile
from typing import Any, BinaryIO
import zipfile

from backend.data.factor_research_runs import (
    FactorResearchIntegrityError,
    FactorResearchPayloadTooLargeError,
    FactorResearchRunStore,
)
from backend.services.research_evidence_export import (
    csv_safe_cell,
    sanitize_export_value,
)

FACTOR_EVIDENCE_EXPORT_SCHEMA = "factor-research-evidence-export/v1"
JSON_MEDIA_TYPE = "application/json"
ZIP_MEDIA_TYPE = "application/zip"
MAX_PERSISTED_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_STREAM_CHUNK_SIZE = 64 * 1024
_SPOOL_MEMORY_LIMIT = 1024 * 1024
_STACK_KEY_PARTS = (
    "exception",
    "stack",
    "traceback",
    "error_log",
)


class FactorEvidenceExportError(RuntimeError):
    """Safe error raised before export response headers are sent."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sanitize(value: Any, *, key: str = "") -> Any:
    normalized = key.lower()
    if any(part in normalized for part in _STACK_KEY_PARTS):
        return "[REDACTED_ERROR_DETAIL]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return sanitize_export_value(value, key=key)


def _factor_definition_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    definition = result.get("factor")
    safe_definition = (
        _sanitize(definition)
        if isinstance(definition, Mapping)
        else {}
    )
    definition_sha256 = _canonical_sha256(safe_definition)
    return {
        "definition": safe_definition,
        "definition_sha256": definition_sha256,
        "version": f"sha256:{definition_sha256}",
    }


def prepare_factor_evidence(
    store: FactorResearchRunStore,
    run_id: str,
    user: Mapping[str, Any],
) -> dict[str, Any]:
    """Load one owned completed run and build a reproducible safe payload."""
    try:
        run = store.get_for_export(
            owner_user_id=int(user["id"]),
            run_id=run_id,
            max_payload_bytes=MAX_PERSISTED_PAYLOAD_BYTES,
        )
    except FactorResearchPayloadTooLargeError as exc:
        raise FactorEvidenceExportError(
            "factor_evidence_too_large",
            "因子研究证据超过导出大小限制",
            status_code=413,
        ) from exc
    except (FactorResearchIntegrityError, KeyError, TypeError, ValueError) as exc:
        raise FactorEvidenceExportError(
            "factor_evidence_integrity_failure",
            "因子研究证据完整性校验失败",
            status_code=409,
        ) from exc
    except sqlite3.Error as exc:
        raise FactorEvidenceExportError(
            "factor_evidence_database_unavailable",
            "因子研究证据数据库暂不可用",
            status_code=503,
        ) from exc
    if run is None:
        raise FactorEvidenceExportError(
            "factor_run_not_found",
            "研究运行不存在",
            status_code=404,
        )

    try:
        source_job_status = store.source_job_status(run.get("source_job_uuid"))
    except sqlite3.Error as exc:
        raise FactorEvidenceExportError(
            "factor_evidence_database_unavailable",
            "因子研究证据数据库暂不可用",
            status_code=503,
        ) from exc
    if source_job_status is not None and source_job_status != "completed":
        raise FactorEvidenceExportError(
            "factor_run_not_completed",
            "仅已完成因子研究运行可导出证据",
            status_code=409,
        )

    result = run.get("result")
    request = run.get("request")
    if not isinstance(result, Mapping) or not isinstance(request, Mapping):
        raise FactorEvidenceExportError(
            "factor_evidence_integrity_failure",
            "因子研究证据完整性校验失败",
            status_code=409,
        )
    if _canonical_sha256(request) != run["request_digest"]:
        raise FactorEvidenceExportError(
            "factor_evidence_integrity_failure",
            "因子研究请求摘要校验失败",
            status_code=409,
        )
    if _canonical_sha256(result) != run["result_digest"]:
        raise FactorEvidenceExportError(
            "factor_evidence_integrity_failure",
            "因子研究结果摘要校验失败",
            status_code=409,
        )

    safe_request = _sanitize(request)
    safe_result = _sanitize(result)
    factor_definition = _factor_definition_evidence(result)
    analysis = {
        key: value
        for key, value in safe_result.items()
        if key
        not in {
            "schema_version",
            "factor",
            "request",
            "dataset",
            "limitations",
            "run",
        }
    }
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    evidence: dict[str, Any] = {
        "schema_version": FACTOR_EVIDENCE_EXPORT_SCHEMA,
        "generated_at": generated_at,
        "status": "completed",
        "archived": run.get("archived_at") is not None,
        "archived_at": run.get("archived_at"),
        "run": {
            "run_id": run["run_id"],
            "factor_id": run["factor_id"],
            "schema_version": run["schema_version"],
            "created_at": run["created_at"],
            "source_job_uuid": run.get("source_job_uuid"),
            "request_digest": run["request_digest"],
            "dataset_digest": run["dataset_digest"],
            "result_digest": run["result_digest"],
            "run_digest": run["run_digest"],
        },
        "request": safe_request,
        "factor_definition": factor_definition,
        "dataset": safe_result.get("dataset", {}),
        "runtime_code": safe_result.get("runtime_code", {}),
        "analysis": analysis,
        "limitations": safe_result.get("limitations", []),
        "redactions_applied": safe_request != request or safe_result != result,
    }
    evidence_digest = _canonical_sha256(evidence)
    manifest_core = {
        "schema_version": "factor-research-reproducibility-manifest/v1",
        "canonicalization": (
            "quant-platform-canonical-json/v1: UTF-8, sorted keys, "
            "compact separators, finite numbers"
        ),
        "hash_algorithm": "SHA-256",
        "run_id": run["run_id"],
        "factor_definition_sha256": factor_definition["definition_sha256"],
        "request_sha256": run["request_digest"],
        "dataset_sha256": run["dataset_digest"],
        "result_sha256": run["result_digest"],
        "run_sha256": run["run_digest"],
        "export_evidence_sha256": evidence_digest,
        "verification": (
            "Recompute export_evidence_sha256 from the canonical top-level "
            "document after removing reproducibility_manifest."
        ),
    }
    evidence["reproducibility_manifest"] = {
        **manifest_core,
        "manifest_sha256": _canonical_sha256(manifest_core),
    }
    return evidence


async def stream_factor_json(
    evidence: Mapping[str, Any],
) -> AsyncIterator[bytes]:
    """Incrementally encode a bounded evidence document."""
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for item in encoder.iterencode(evidence):
        encoded = item.encode("utf-8")
        for offset in range(0, len(encoded), _STREAM_CHUNK_SIZE):
            yield encoded[offset : offset + _STREAM_CHUNK_SIZE]


def _write_row(writer: csv.writer, values: list[Any]) -> None:
    writer.writerow([csv_safe_cell(value) for value in values])


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return _canonical_json(value)
    return value


def _write_mapping(
    archive: zipfile.ZipFile,
    filename: str,
    payload: Mapping[str, Any] | None,
) -> None:
    with archive.open(filename, "w") as raw:
        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            writer = csv.writer(text)
            keys = list(payload) if payload else []
            _write_row(writer, keys)
            if payload:
                _write_row(writer, [_cell(payload[key]) for key in keys])


def _write_records(
    archive: zipfile.ZipFile,
    filename: str,
    columns: list[str],
    records: list[Mapping[str, Any]],
) -> None:
    with archive.open(filename, "w") as raw:
        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            writer = csv.writer(text)
            _write_row(writer, columns)
            for record in records:
                _write_row(writer, [_cell(record.get(key)) for key in columns])


def _ic_tables(
    analysis: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    ic = analysis.get("ic")
    if not isinstance(ic, Mapping):
        return summaries, series
    for horizon, payload in ic.items():
        if not isinstance(payload, Mapping):
            continue
        summary = payload.get("summary")
        if isinstance(summary, Mapping):
            for method, values in summary.items():
                if isinstance(values, Mapping):
                    summaries.append(
                        {
                            "horizon": horizon,
                            "method": method,
                            **values,
                        }
                    )
        points = payload.get("series")
        if isinstance(points, list):
            for point in points:
                if isinstance(point, Mapping):
                    series.append({"horizon": horizon, **point})
    return summaries, series


def _decay_records(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    decay = analysis.get("decay")
    if not isinstance(decay, Mapping) or not isinstance(decay.get("points"), list):
        return []
    return [
        dict(point)
        for point in decay["points"]
        if isinstance(point, Mapping)
    ]


def _quantile_records(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    quantile = analysis.get("quantile_returns")
    if not isinstance(quantile, Mapping):
        return []
    groups = quantile.get("mean_group_returns")
    records = (
        [
            {"group": group, "mean_forward_return": value}
            for group, value in groups.items()
        ]
        if isinstance(groups, Mapping)
        else []
    )
    records.append(
        {
            "group": "long_short",
            "mean_forward_return": quantile.get("long_short"),
            "monotonicity": quantile.get("monotonicity"),
        }
    )
    return records


def _mapping_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def build_factor_csv_zip(evidence: Mapping[str, Any]) -> BinaryIO:
    """Build a fixed-member ZIP on a bounded-memory spooled file."""
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_LIMIT)
    try:
        analysis = evidence.get("analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        ic_summary, ic_series = _ic_tables(analysis)
        with zipfile.ZipFile(
            spool,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=False,
        ) as archive:
            _write_mapping(
                archive,
                "run.csv",
                evidence.get("run")
                if isinstance(evidence.get("run"), Mapping)
                else {},
            )
            _write_mapping(
                archive,
                "request.csv",
                evidence.get("request")
                if isinstance(evidence.get("request"), Mapping)
                else {},
            )
            factor = evidence.get("factor_definition")
            factor = factor if isinstance(factor, Mapping) else {}
            definition = factor.get("definition")
            _write_mapping(
                archive,
                "factor_definition.csv",
                {
                    **(
                        dict(definition)
                        if isinstance(definition, Mapping)
                        else {}
                    ),
                    "definition_sha256": factor.get("definition_sha256"),
                    "version": factor.get("version"),
                },
            )
            _write_mapping(
                archive,
                "dataset_provenance.csv",
                evidence.get("dataset")
                if isinstance(evidence.get("dataset"), Mapping)
                else {},
            )
            _write_records(
                archive,
                "ic_summary.csv",
                [
                    "horizon",
                    "method",
                    "count",
                    "mean",
                    "std",
                    "icir",
                    "positive_ratio",
                    "t_stat",
                ],
                ic_summary,
            )
            _write_records(
                archive,
                "ic_series.csv",
                [
                    "horizon",
                    "date",
                    "pearson_ic",
                    "rank_ic",
                    "sample_count",
                ],
                ic_series,
            )
            _write_records(
                archive,
                "decay.csv",
                ["horizon", "rank_ic", "pearson_ic"],
                _decay_records(analysis),
            )
            _write_records(
                archive,
                "quantile_returns.csv",
                [
                    "group",
                    "mean_forward_return",
                    "monotonicity",
                ],
                _quantile_records(analysis),
            )
            quantile = analysis.get("quantile_returns")
            quantile = quantile if isinstance(quantile, Mapping) else {}
            _write_records(
                archive,
                "quantile_series.csv",
                [
                    "date",
                    "sample_count",
                    "group_returns",
                    "long_short_spread",
                ],
                _mapping_records(quantile.get("series")),
            )
            for key in ("cost", "costs", "capacity", "cost_capacity"):
                value = analysis.get(key)
                if isinstance(value, Mapping):
                    _write_mapping(
                        archive,
                        f"{key}.csv",
                        value,
                    )
            implementation = analysis.get("implementation")
            if isinstance(implementation, Mapping):
                _write_mapping(
                    archive,
                    "implementation_summary.csv",
                    {
                        key: value
                        for key, value in implementation.items()
                        if key not in {
                            "cost_sensitivity",
                            "turnover",
                            "capacity",
                        }
                    },
                )
                _write_records(
                    archive,
                    "implementation_cost_sensitivity.csv",
                    [
                        "cost_bps",
                        "mean_group_returns",
                        "long_short",
                    ],
                    _mapping_records(implementation.get("cost_sensitivity")),
                )
                turnover = implementation.get("turnover")
                turnover = turnover if isinstance(turnover, Mapping) else {}
                _write_records(
                    archive,
                    "implementation_turnover.csv",
                    [
                        "date",
                        "group_turnover",
                        "long_short_turnover",
                    ],
                    _mapping_records(turnover.get("series")),
                )
                capacity = implementation.get("capacity")
                capacity = capacity if isinstance(capacity, Mapping) else {}
                _write_records(
                    archive,
                    "implementation_capacity.csv",
                    ["date", "status", "estimates"],
                    _mapping_records(capacity.get("daily")),
                )
            neutralization = analysis.get("neutralization")
            if isinstance(neutralization, Mapping):
                primary = neutralization.get("primary_factor")
                primary = primary if isinstance(primary, Mapping) else {}
                _write_mapping(
                    archive,
                    "neutralization_summary.csv",
                    {
                        "schema_version": neutralization.get(
                            "schema_version"
                        ),
                        "mode": neutralization.get("mode"),
                        "status": neutralization.get("status"),
                        "fit_window": neutralization.get("fit_window"),
                        **(
                            dict(primary.get("summary"))
                            if isinstance(primary.get("summary"), Mapping)
                            else {}
                        ),
                    },
                )
                _write_records(
                    archive,
                    "neutralization_daily.csv",
                    [
                        "date",
                        "status",
                        "sample_count",
                        "candidate_count",
                        "coverage_ratio",
                        "dropped_by_reason",
                        "rank",
                        "feature_count",
                        "before",
                        "after",
                    ],
                    _mapping_records(primary.get("daily")),
                )
                archive.writestr(
                    "neutralization_inputs.json",
                    _canonical_json(neutralization.get("inputs", {})),
                )
            protocol_review = analysis.get("protocol_review")
            if isinstance(protocol_review, Mapping):
                _write_mapping(
                    archive,
                    "protocol_review.csv",
                    {
                        key: value
                        for key, value in protocol_review.items()
                        if key not in {"checks", "export_rules"}
                    },
                )
                _write_records(
                    archive,
                    "protocol_thresholds.csv",
                    [
                        "metric",
                        "operator",
                        "threshold",
                        "actual",
                        "passed",
                    ],
                    _mapping_records(protocol_review.get("checks")),
                )
                archive.writestr(
                    "protocol_export_rules.json",
                    _canonical_json(protocol_review.get("export_rules", {})),
                )
            limitations = evidence.get("limitations")
            _write_records(
                archive,
                "limitations.csv",
                ["index", "limitation"],
                [
                    {"index": index + 1, "limitation": value}
                    for index, value in enumerate(limitations)
                ]
                if isinstance(limitations, list)
                else [],
            )
            archive.writestr(
                "preprocessing.json",
                _canonical_json(analysis.get("preprocessing", {})),
            )
            archive.writestr(
                "analysis.json",
                _canonical_json(analysis),
            )
            archive.writestr(
                "reproducibility_manifest.json",
                _canonical_json(evidence["reproducibility_manifest"]),
            )
        if spool.tell() > MAX_ARCHIVE_BYTES:
            raise FactorEvidenceExportError(
                "factor_evidence_archive_too_large",
                "因子研究证据压缩包超过导出大小限制",
                status_code=413,
            )
        spool.seek(0)
        return spool
    except Exception:
        spool.close()
        raise


async def stream_binary_file(file: BinaryIO) -> AsyncIterator[bytes]:
    try:
        while chunk := file.read(_STREAM_CHUNK_SIZE):
            yield chunk
    finally:
        file.close()
