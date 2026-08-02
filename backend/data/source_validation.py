"""Integrity-checked source provenance and independent-feed validation.

The contracts in this module deliberately separate three questions:

* where a response actually came from;
* whether two independently fetched responses agree; and
* whether a feed is authoritative enough for a particular use.

Agreement between two public aggregation endpoints is useful anomaly evidence,
but it never upgrades either endpoint to exchange-licensed evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backend.data.versioning import compute_data_version

DAILY_FETCH_EVIDENCE_SCHEMA = "daily-fetch-evidence/v1"
CROSS_SOURCE_VALIDATION_SCHEMA = "cross-source-validation/v1"
CACHE_SOURCE_PROVENANCE_SCHEMA = "cache-source-provenance/v1"
ADJUSTMENT_FACTOR_VALIDATION_SCHEMA = "adjustment-factor-validation/v1"

EVIDENCE_LEVELS = {
    "declared",
    "public_aggregator",
    "licensed_vendor",
    "exchange_authoritative",
}
_EVIDENCE_RANK = {
    "declared": 0,
    "public_aggregator": 1,
    "licensed_vendor": 2,
    "exchange_authoritative": 3,
}
_SOURCE_POLICIES = {
    "tushare": {
        "adapter_id": "quant-platform/tushare-candidate-rest/v1",
        "upstream_id": "tushare-pro-vendor-market-data",
        "max_evidence_level": "public_aggregator",
    },
    "akshare:eastmoney": {
        "adapter_id": "quant-platform/akshare-eastmoney/v1",
        "upstream_id": "eastmoney-public-market-data",
        "max_evidence_level": "public_aggregator",
    },
    "akshare:sina": {
        "adapter_id": "quant-platform/akshare-sina/v1",
        "upstream_id": "sina-public-market-data",
        "max_evidence_level": "public_aggregator",
    },
    "akshare:tencent": {
        "adapter_id": "quant-platform/akshare-tencent/v1",
        "upstream_id": "tencent-public-market-data",
        "max_evidence_level": "public_aggregator",
    },
    "baostock:official": {
        "adapter_id": "quant-platform/baostock-raw-preclose-hfq/v1",
        "upstream_id": "baostock-public-market-data",
        "max_evidence_level": "public_aggregator",
    },
    "akshare:eastmoney-legacy-wrapper": {
        "adapter_id": "quant-platform/akshare-eastmoney/legacy",
        "upstream_id": "eastmoney-public-market-data",
        "max_evidence_level": "public_aggregator",
    },
}
_ADJUSTMENTS = {"raw", "qfq", "hfq", "unknown"}
_ADJUSTMENT_METHODS = {"baostock_raw_preclose_hfq_recurrence"}


class SourceEvidenceError(ValueError):
    """Source evidence is missing, inconsistent, or has been modified."""


class CrossSourceConflictError(RuntimeError):
    """Independent market-data observations failed the configured gate."""

    def __init__(
        self,
        message: str,
        *,
        evidence_summary: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence_summary = (
            dict(evidence_summary) if evidence_summary is not None else None
        )


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceEvidenceError(
            "source evidence must be finite canonical JSON"
        ) from exc


def _content_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalise_codes(codes: Sequence[Any]) -> list[str]:
    return sorted({str(code).strip() for code in codes if str(code).strip()})


def _frame_codes(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return []
    if not isinstance(frame.columns, pd.MultiIndex):
        return _normalise_codes(frame.columns)
    return _normalise_codes(frame.columns.get_level_values(0))


def _frame_digest(frame: pd.DataFrame, context: Mapping[str, Any]) -> str | None:
    if frame.empty:
        return None
    return compute_data_version(frame, context)


def _date_text(value: Any) -> str:
    return pd.Timestamp(value).normalize().strftime("%Y-%m-%d")


def _unsigned(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("content_sha256", None)
    return result


def _validate_hash(payload: Mapping[str, Any]) -> None:
    digest = payload.get("content_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise SourceEvidenceError("source evidence content hash is invalid")
    if _content_sha256(_unsigned(payload)) != digest:
        raise SourceEvidenceError("source evidence hash verification failed")


def _source_policy(provider: str) -> dict[str, str]:
    return dict(
        _SOURCE_POLICIES.get(
            provider,
            {
                "adapter_id": f"unregistered:{provider}",
                "upstream_id": f"unregistered:{provider}",
                "max_evidence_level": "declared",
            },
        )
    )


def validate_adjustment_factor_validation(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate bounded evidence for a raw-to-hfq factor transformation."""

    payload = dict(evidence)
    if payload.get("schema_version") != ADJUSTMENT_FACTOR_VALIDATION_SCHEMA:
        raise SourceEvidenceError(
            "unsupported adjustment factor validation schema"
        )
    if payload.get("method") not in _ADJUSTMENT_METHODS:
        raise SourceEvidenceError("adjustment factor method is not permitted")
    if (
        payload.get("input_adjustment") != "raw"
        or payload.get("output_adjustment") != "hfq"
    ):
        raise SourceEvidenceError(
            "adjustment factor input/output semantics are invalid"
        )
    for field in ("recurrence_validated", "factors_finite_positive"):
        if not isinstance(payload.get(field), bool):
            raise SourceEvidenceError(
                f"adjustment factor {field} must be boolean"
            )
    jump_count = payload.get("corporate_action_jump_count")
    if (
        isinstance(jump_count, bool)
        or not isinstance(jump_count, int)
        or jump_count < 0
    ):
        raise SourceEvidenceError(
            "adjustment factor corporate-action jump count is invalid"
        )
    examples = payload.get("corporate_action_examples")
    if not isinstance(examples, list) or len(examples) > 20:
        raise SourceEvidenceError(
            "adjustment factor corporate-action examples are invalid"
        )
    if len(examples) > jump_count:
        raise SourceEvidenceError(
            "adjustment factor examples exceed the recorded jump count"
        )
    for example in examples:
        if not isinstance(example, Mapping):
            raise SourceEvidenceError(
                "adjustment factor corporate-action example is malformed"
            )
        code = str(example.get("code", "")).strip()
        if not code or example.get("code") != code:
            raise SourceEvidenceError(
                "adjustment factor example code is invalid"
            )
        try:
            if example.get("date") != _date_text(example.get("date")):
                raise SourceEvidenceError(
                    "adjustment factor example date is invalid"
                )
            ratio = float(example.get("factor_ratio"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise SourceEvidenceError(
                "adjustment factor example is invalid"
            ) from exc
        if not math.isfinite(ratio) or ratio <= 0:
            raise SourceEvidenceError(
                "adjustment factor example ratio is invalid"
            )
    truncated = payload.get("evidence_truncated")
    if (
        not isinstance(truncated, bool)
        or truncated != (jump_count > len(examples))
    ):
        raise SourceEvidenceError(
            "adjustment factor truncation flag is inconsistent"
        )
    informational = payload.get("informational_hfq_cross_source")
    informational_status = payload.get("informational_hfq_status")
    if informational is not None and informational_status is not None:
        raise SourceEvidenceError(
            "adjustment factor informational evidence is ambiguous"
        )
    if informational is not None:
        validated = validate_cross_source_evidence(informational)
        if validated.get("adjustment") != "hfq":
            raise SourceEvidenceError(
                "informational adjusted-price evidence must use hfq"
            )
    if informational_status is not None and (
        not isinstance(informational_status, str)
        or not informational_status.strip()
        or len(informational_status) > 128
    ):
        raise SourceEvidenceError(
            "adjustment factor informational status is invalid"
        )
    return payload


def _raw_cross_validated(batch: Mapping[str, Any]) -> bool:
    cross_validation = batch.get("cross_validation")
    return bool(
        isinstance(cross_validation, Mapping)
        and cross_validation.get("adjustment") == "raw"
        and isinstance(cross_validation.get("summary"), Mapping)
        and cross_validation["summary"].get("acceptable") is True
    )


def _adjusted_factor_validated(batch: Mapping[str, Any]) -> bool:
    validation = batch.get("adjustment_validation")
    if not isinstance(validation, Mapping):
        return False
    validated = validate_adjustment_factor_validation(validation)
    return bool(
        batch.get("adjustment") == "hfq"
        and validated["input_adjustment"] == "raw"
        and validated["output_adjustment"] == "hfq"
        and validated["recurrence_validated"] is True
        and validated["factors_finite_positive"] is True
    )


@dataclass(frozen=True)
class DailyFetchResult:
    """One normalized panel plus immutable evidence for the network request."""

    frame: pd.DataFrame
    evidence: dict[str, Any]


async def fetch_daily_with_evidence(
    source: Any,
    codes: Sequence[str],
    start: str,
    end: str,
) -> DailyFetchResult:
    """Use a source-native evidence method or a conservative declared wrapper."""

    native = getattr(source, "fetch_daily_result", None)
    if callable(native):
        result = await native(list(codes), start, end)
        if not isinstance(result, DailyFetchResult):
            raise SourceEvidenceError(
                "fetch_daily_result must return DailyFetchResult"
            )
        return DailyFetchResult(
            frame=result.frame,
            evidence=validate_daily_fetch_evidence(
                result.evidence,
                frame=result.frame,
            ),
        )
    frame = await source.fetch_daily(list(codes), start, end)
    provider = type(source).__name__
    evidence = build_daily_fetch_evidence(
        frame,
        requested_codes=codes,
        start=start,
        end=end,
        provider=provider,
        endpoint=f"python:{type(source).__module__}.{type(source).__qualname__}",
        adjustment="qfq",
        evidence_level="declared",
    )
    return DailyFetchResult(frame=frame, evidence=evidence)


def build_daily_fetch_evidence(
    frame: pd.DataFrame,
    *,
    requested_codes: Sequence[str],
    start: str,
    end: str,
    provider: str,
    endpoint: str,
    adjustment: str,
    evidence_level: str,
    failed_codes: Mapping[str, str] | None = None,
    cross_validation: Mapping[str, Any] | None = None,
    transformations: Sequence[str] | None = None,
    adjustment_validation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic per-request source evidence.

    ``failed_codes`` is retained rather than silently discarded.  A caller can
    still cache partial observations, but downstream trust gates can see that
    the source did not cover the complete request.
    """

    provider = str(provider).strip()
    endpoint = str(endpoint).strip()
    adjustment = str(adjustment).strip().lower()
    evidence_level = str(evidence_level).strip().lower()
    if not provider or not endpoint:
        raise SourceEvidenceError("provider and endpoint are required")
    if adjustment not in _ADJUSTMENTS:
        raise SourceEvidenceError(f"unsupported adjustment: {adjustment}")
    if evidence_level not in EVIDENCE_LEVELS:
        raise SourceEvidenceError(
            f"unsupported evidence_level: {evidence_level}"
        )
    policy = _source_policy(provider)
    if _EVIDENCE_RANK[evidence_level] > _EVIDENCE_RANK[
        policy["max_evidence_level"]
    ]:
        raise SourceEvidenceError(
            f"provider {provider!r} cannot claim evidence level "
            f"{evidence_level!r}"
        )

    requested = _normalise_codes(requested_codes)
    observed = _frame_codes(frame)
    failures = {
        str(code).strip(): str(reason).strip()
        for code, reason in (failed_codes or {}).items()
        if str(code).strip()
    }
    missing = sorted(set(requested) - set(observed))
    for code in missing:
        failures.setdefault(code, "no_observations")
    unknown = sorted(set(observed) - set(requested))
    if unknown:
        raise SourceEvidenceError(
            f"response contains unrequested codes: {unknown[:5]}"
        )

    validation_payload: dict[str, Any] | None = None
    if cross_validation is not None:
        validation_payload = validate_cross_source_evidence(cross_validation)
    adjustment_validation_payload: dict[str, Any] | None = None
    if adjustment_validation is not None:
        adjustment_validation_payload = (
            validate_adjustment_factor_validation(adjustment_validation)
        )

    payload: dict[str, Any] = {
        "schema_version": DAILY_FETCH_EVIDENCE_SCHEMA,
        "provider": provider,
        "adapter_id": policy["adapter_id"],
        "upstream_id": policy["upstream_id"],
        "endpoint": endpoint,
        "adjustment": adjustment,
        "evidence_level": evidence_level,
        "request": {
            "start": _date_text(start),
            "end": _date_text(end),
            "codes": requested,
            "requested_code_count": len(requested),
        },
        "response": {
            "observed_codes": observed,
            "failed_codes": dict(sorted(failures.items())),
            "row_count": int(len(frame)),
            "column_count": int(len(frame.columns)),
            "frame_digest": _frame_digest(
                frame,
                {
                    "provider": provider,
                    "endpoint": endpoint,
                    "adjustment": adjustment,
                },
            ),
        },
        "cross_validation": validation_payload,
        "adjustment_validation": adjustment_validation_payload,
        "complete_code_coverage": not failures and observed == requested,
    }
    payload["raw_cross_validated"] = _raw_cross_validated(payload)
    payload["adjusted_factor_validated"] = _adjusted_factor_validated(payload)
    if transformations is not None:
        normalized_transformations = sorted(
            {
                str(item).strip()
                for item in transformations
                if str(item).strip()
            }
        )
        if not normalized_transformations:
            raise SourceEvidenceError(
                "declared transformations cannot be empty"
            )
        payload["transformations"] = normalized_transformations
    payload["content_sha256"] = _content_sha256(payload)
    return validate_daily_fetch_evidence(payload, frame=frame)


def validate_daily_fetch_evidence(
    evidence: Mapping[str, Any],
    *,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    payload = dict(evidence)
    if payload.get("schema_version") != DAILY_FETCH_EVIDENCE_SCHEMA:
        raise SourceEvidenceError("unsupported daily fetch evidence schema")
    _validate_hash(payload)
    if payload.get("evidence_level") not in EVIDENCE_LEVELS:
        raise SourceEvidenceError("daily fetch evidence level is invalid")
    provider = str(payload.get("provider", ""))
    policy = _source_policy(provider)
    if (
        payload.get("adapter_id") != policy["adapter_id"]
        or payload.get("upstream_id") != policy["upstream_id"]
        or _EVIDENCE_RANK[str(payload["evidence_level"])]
        > _EVIDENCE_RANK[policy["max_evidence_level"]]
    ):
        raise SourceEvidenceError(
            "daily fetch source identity or evidence level is not permitted"
        )
    if payload.get("adjustment") not in _ADJUSTMENTS:
        raise SourceEvidenceError("daily fetch adjustment is invalid")
    transformations = payload.get("transformations")
    if transformations is not None and (
        not isinstance(transformations, list)
        or not transformations
        or transformations
        != sorted(
            {
                str(item).strip()
                for item in transformations
                if str(item).strip()
            }
        )
    ):
        raise SourceEvidenceError(
            "daily fetch transformations are not canonical"
        )
    request = payload.get("request")
    response = payload.get("response")
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise SourceEvidenceError("daily fetch request/response is missing")
    requested = _normalise_codes(request.get("codes", []))
    if request.get("codes") != requested:
        raise SourceEvidenceError("daily fetch request codes are not canonical")
    if int(request.get("requested_code_count", -1)) != len(requested):
        raise SourceEvidenceError("daily fetch requested_code_count is invalid")
    observed = _normalise_codes(response.get("observed_codes", []))
    failures = response.get("failed_codes")
    if not isinstance(failures, Mapping):
        raise SourceEvidenceError("daily fetch failed_codes is invalid")
    expected_complete = (
        observed == requested
        and not failures
    )
    if bool(payload.get("complete_code_coverage")) != expected_complete:
        raise SourceEvidenceError(
            "complete_code_coverage conflicts with request evidence"
        )
    if payload.get("cross_validation") is not None:
        validate_cross_source_evidence(payload["cross_validation"])
    adjustment_validation = payload.get("adjustment_validation")
    if adjustment_validation is not None:
        validate_adjustment_factor_validation(adjustment_validation)
    expected_raw_cross_validated = _raw_cross_validated(payload)
    expected_adjusted_factor_validated = _adjusted_factor_validated(payload)
    if (
        "raw_cross_validated" in payload
        and payload.get("raw_cross_validated")
        is not expected_raw_cross_validated
    ):
        raise SourceEvidenceError(
            "daily fetch raw cross-validation flag is inconsistent"
        )
    if (
        "adjusted_factor_validated" in payload
        and payload.get("adjusted_factor_validated")
        is not expected_adjusted_factor_validated
    ):
        raise SourceEvidenceError(
            "daily fetch adjustment factor flag is inconsistent"
        )
    if frame is not None:
        if _frame_codes(frame) != observed:
            raise SourceEvidenceError(
                "daily fetch evidence does not cover the supplied frame"
            )
        expected_digest = _frame_digest(
            frame,
            {
                "provider": payload["provider"],
                "endpoint": payload["endpoint"],
                "adjustment": payload["adjustment"],
            },
        )
        if response.get("frame_digest") != expected_digest:
            raise SourceEvidenceError("daily fetch frame digest changed")
        if int(response.get("row_count", -1)) != len(frame):
            raise SourceEvidenceError("daily fetch row_count changed")
        if int(response.get("column_count", -1)) != len(frame.columns):
            raise SourceEvidenceError("daily fetch column_count changed")
    return payload


def _field_series(
    frame: pd.DataFrame,
    code: str,
    field: str,
) -> pd.Series | None:
    if not isinstance(frame.columns, pd.MultiIndex):
        return None
    candidates = {
        (str(column[0]).strip(), str(column[-1]).strip().lower()): column
        for column in frame.columns
    }
    column = candidates.get((code, field))
    if column is None:
        return None
    series = pd.to_numeric(frame[column], errors="coerce")
    series.index = pd.to_datetime(series.index, errors="coerce")
    return series[~series.index.isna()].sort_index()


def compare_independent_daily_frames(
    primary: pd.DataFrame,
    reference: pd.DataFrame,
    *,
    primary_provider: str,
    reference_provider: str,
    requested_codes: Sequence[str],
    adjustment: str,
    required_fields: Sequence[str] = (
        "open",
        "high",
        "low",
        "close",
        "volume",
    ),
    min_overlap_returns: int = 20,
    return_abs_tolerance: float = 0.005,
    max_conflict_ratio: float = 0.0,
    max_examples: int = 20,
) -> dict[str, Any]:
    """Compare independent adjusted feeds using daily returns.

    Absolute adjusted levels are intentionally not compared: providers may
    anchor qfq or hfq series to different dates.  Return comparison is
    invariant to a constant adjustment scale, while still detecting
    discontinuities and corporate-action disagreement.
    """

    primary_identity = _source_policy(primary_provider.strip())
    reference_identity = _source_policy(reference_provider.strip())
    if (
        primary_provider.strip() == reference_provider.strip()
        or primary_identity["upstream_id"] == reference_identity["upstream_id"]
    ):
        raise SourceEvidenceError(
            "cross-source validation requires different upstream identities"
        )
    if adjustment not in _ADJUSTMENTS - {"unknown"}:
        raise SourceEvidenceError("a known adjustment basis is required")
    if min_overlap_returns < 2:
        raise ValueError("min_overlap_returns must be at least 2")
    if not math.isfinite(return_abs_tolerance) or return_abs_tolerance < 0:
        raise ValueError("return_abs_tolerance must be finite and non-negative")
    if not 0 <= max_conflict_ratio <= 1:
        raise ValueError("max_conflict_ratio must be between 0 and 1")

    requested_codes = _normalise_codes(requested_codes)
    required_fields = sorted(
        {str(field).strip().lower() for field in required_fields}
    )
    if not requested_codes or "close" not in required_fields:
        raise ValueError("requested_codes and required close field are required")
    per_code: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []
    insufficient: list[str] = []
    unexpected_primary_codes = sorted(
        set(_frame_codes(primary)) - set(requested_codes)
    )
    unexpected_reference_codes = sorted(
        set(_frame_codes(reference)) - set(requested_codes)
    )
    total_compared = 0
    total_conflicts = 0
    primary_codes = set(_frame_codes(primary))
    reference_codes = set(_frame_codes(reference))

    for code in requested_codes:
        if code not in primary_codes or code not in reference_codes:
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": 0,
                    "conflicts": 0,
                    "status": (
                        "missing_primary_code"
                        if code not in primary_codes
                        else "missing_reference_code"
                    ),
                }
            )
            continue
        field_pairs = [
            (
                field,
                _field_series(primary, code, field),
                _field_series(reference, code, field),
            )
            for field in required_fields
        ]
        if any(first is None or second is None for _, first, second in field_pairs):
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": 0,
                    "conflicts": 0,
                    "status": "missing_required_fields",
                }
            )
            continue
        first_by_field = {
            field: first
            for field, first, _ in field_pairs
            if first is not None
        }
        second_by_field = {
            field: second
            for field, _, second in field_pairs
            if second is not None
        }
        first_close = first_by_field["close"]
        second_close = second_by_field["close"]
        first_observed = first_close.dropna().index
        second_observed = second_close.dropna().index
        partial_primary = any(
            not series.dropna().index.equals(first_observed)
            for series in first_by_field.values()
        )
        partial_reference = any(
            not series.dropna().index.equals(second_observed)
            for series in second_by_field.values()
        )
        if partial_primary or partial_reference:
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": 0,
                    "conflicts": 0,
                    "status": "non_finite_or_nan_values",
                }
            )
            continue
        shared_observed = first_observed.intersection(second_observed)
        primary_only_observed = first_observed.difference(second_observed)
        reference_only_observed = second_observed.difference(first_observed)
        if shared_observed.empty:
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": 0,
                    "conflicts": 0,
                    "primary_only_dates": len(primary_only_observed),
                    "reference_only_dates": len(reference_only_observed),
                    "status": "date_mask_mismatch",
                }
            )
            continue
        first_shared = shared_observed[0]
        last_shared = shared_observed[-1]
        interior_primary_only = primary_only_observed[
            (primary_only_observed >= first_shared)
            & (primary_only_observed <= last_shared)
        ]
        interior_reference_only = reference_only_observed[
            (reference_only_observed >= first_shared)
            & (reference_only_observed <= last_shared)
        ]
        if len(interior_primary_only) or len(interior_reference_only):
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": 0,
                    "conflicts": 0,
                    "primary_only_dates": len(primary_only_observed),
                    "reference_only_dates": len(reference_only_observed),
                    "interior_primary_only_dates": len(
                        interior_primary_only
                    ),
                    "interior_reference_only_dates": len(
                        interior_reference_only
                    ),
                    "status": "date_mask_mismatch",
                }
            )
            continue
        if any(
            not np.isfinite(
                series.loc[first_observed].to_numpy(dtype="float64")
            ).all()
            for series in first_by_field.values()
        ) or any(
            not np.isfinite(
                series.loc[second_observed].to_numpy(dtype="float64")
            ).all()
            for series in second_by_field.values()
        ):
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": 0,
                    "conflicts": 0,
                    "status": "non_finite_or_nan_values",
                }
            )
            continue
        # Providers occasionally expose an isolated observation before or
        # after the shared trading history (for example around a long
        # suspension). Such edge-only coverage is recorded but must not turn
        # thousands of identical shared returns into "insufficient". Missing
        # dates inside the shared span remain a fail-closed condition above.
        first = first_close.loc[shared_observed]
        second = second_close.loc[shared_observed]
        returns = pd.DataFrame(
            {
                "primary": first.pct_change(fill_method=None).iloc[1:],
                "reference": second.pct_change(fill_method=None).iloc[1:],
            }
        )
        if returns.isna().any().any() or not np.isfinite(
            returns.to_numpy(dtype="float64")
        ).all():
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": 0,
                    "conflicts": 0,
                    "status": "invalid_returns",
                }
            )
            continue
        overlap = int(len(returns))
        if overlap < min_overlap_returns:
            insufficient.append(code)
            per_code.append(
                {
                    "code": code,
                    "overlap_returns": overlap,
                    "conflicts": 0,
                    "status": "insufficient_overlap",
                }
            )
            continue
        return_delta = (returns["primary"] - returns["reference"]).abs()
        return_conflicts = return_delta > return_abs_tolerance

        # Compare each day's OHLC geometry, which is invariant to a provider's
        # constant adjusted-price anchor.  This catches high/low/open
        # corruption that close-only validation cannot see.
        comparison_index = returns.index
        geometry_conflicts = pd.Series(False, index=comparison_index)
        max_geometry_delta = 0.0
        for field in ("open", "high", "low"):
            if field not in first_by_field or field not in second_by_field:
                continue
            primary_shape = (
                first_by_field[field].loc[comparison_index]
                / first.loc[comparison_index]
            )
            reference_shape = (
                second_by_field[field].loc[comparison_index]
                / second.loc[comparison_index]
            )
            shape_delta = (primary_shape - reference_shape).abs()
            geometry_conflicts |= shape_delta > return_abs_tolerance
            max_geometry_delta = max(
                max_geometry_delta,
                float(shape_delta.max()),
            )

        # Volume units may differ by a constant factor (shares versus lots).
        # Validate zero masks exactly and deviations from the median log scale;
        # a per-day spike therefore remains a conflict.
        volume_conflicts = pd.Series(False, index=comparison_index)
        max_volume_scale_delta = 0.0
        if "volume" in first_by_field and "volume" in second_by_field:
            first_volume = first_by_field["volume"].loc[comparison_index]
            second_volume = second_by_field["volume"].loc[comparison_index]
            zero_mismatch = (first_volume == 0) ^ (second_volume == 0)
            volume_conflicts |= zero_mismatch
            positive = (first_volume > 0) & (second_volume > 0)
            if positive.any():
                log_scale = np.log(
                    first_volume.loc[positive] / second_volume.loc[positive]
                )
                centre = float(log_scale.median())
                scale_delta = (log_scale - centre).abs()
                volume_conflicts.loc[positive] |= (
                    scale_delta > return_abs_tolerance
                )
                max_volume_scale_delta = float(scale_delta.max())

        conflict_dates = set(return_delta[return_conflicts].index)
        conflict_dates.update(geometry_conflicts[geometry_conflicts].index)
        conflict_dates.update(volume_conflicts[volume_conflicts].index)
        conflicts = len(conflict_dates)
        total_compared += overlap
        total_conflicts += conflicts
        code_evidence = {
            "code": code,
            "overlap_returns": overlap,
            "conflicts": conflicts,
            "return_conflicts": int(return_conflicts.sum()),
            "geometry_conflicts": int(geometry_conflicts.sum()),
            "volume_conflicts": int(volume_conflicts.sum()),
            "max_abs_return_delta": float(return_delta.max()),
            "max_abs_geometry_delta": max_geometry_delta,
            "max_abs_volume_log_scale_delta": max_volume_scale_delta,
            "status": "conflict" if conflicts else "passed",
        }
        if len(primary_only_observed) or len(reference_only_observed):
            code_evidence.update(
                {
                    "primary_only_dates": len(primary_only_observed),
                    "reference_only_dates": len(reference_only_observed),
                    "edge_date_mask_difference": True,
                }
            )
        per_code.append(code_evidence)
        remaining = max_examples - len(examples)
        if remaining > 0 and conflicts:
            for date in sorted(conflict_dates)[:remaining]:
                examples.append(
                    {
                        "code": code,
                        "date": _date_text(date),
                        "primary_return": float(returns.loc[date, "primary"]),
                        "reference_return": float(returns.loc[date, "reference"]),
                        "abs_delta": float(return_delta.loc[date]),
                        "price_geometry_conflict": bool(
                            geometry_conflicts.get(date, False)
                        ),
                        "volume_scale_conflict": bool(
                            volume_conflicts.get(date, False)
                        ),
                    }
                )

    ratio = (
        float(total_conflicts / total_compared)
        if total_compared
        else 1.0
    )
    acceptable = (
        bool(requested_codes)
        and not insufficient
        and not unexpected_primary_codes
        and not unexpected_reference_codes
        and total_compared > 0
        and ratio <= max_conflict_ratio
    )
    payload: dict[str, Any] = {
        "schema_version": CROSS_SOURCE_VALIDATION_SCHEMA,
        "primary_provider": primary_provider.strip(),
        "reference_provider": reference_provider.strip(),
        "primary_upstream_id": primary_identity["upstream_id"],
        "reference_upstream_id": reference_identity["upstream_id"],
        "requested_codes": requested_codes,
        "required_fields": required_fields,
        "adjustment": adjustment,
        "policy": {
            "comparison": "scale_invariant_ohlcv",
            "min_overlap_returns": min_overlap_returns,
            "return_abs_tolerance": return_abs_tolerance,
            "max_conflict_ratio": max_conflict_ratio,
        },
        "summary": {
            "requested_code_count": len(requested_codes),
            "compared_return_count": total_compared,
            "conflict_count": total_conflicts,
            "conflict_ratio": ratio,
            "insufficient_codes": insufficient,
            "unexpected_primary_codes": unexpected_primary_codes,
            "unexpected_reference_codes": unexpected_reference_codes,
            "acceptable": acceptable,
        },
        "per_code": per_code,
        "conflict_examples": examples,
        "limitations": [
            "Agreement does not prove exchange accuracy or upgrade evidence level.",
            "Return comparison cannot validate absolute adjusted-price anchors.",
            "Corporate-action, suspension, listing-state and PIT universe evidence "
            "must be validated separately.",
        ],
    }
    payload["content_sha256"] = _content_sha256(payload)
    return validate_cross_source_evidence(payload)


def validate_cross_source_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(evidence)
    if payload.get("schema_version") != CROSS_SOURCE_VALIDATION_SCHEMA:
        raise SourceEvidenceError("unsupported cross-source validation schema")
    _validate_hash(payload)
    primary_policy = _source_policy(str(payload.get("primary_provider", "")))
    reference_policy = _source_policy(str(payload.get("reference_provider", "")))
    if (
        payload.get("primary_provider") == payload.get("reference_provider")
        or primary_policy["upstream_id"] == reference_policy["upstream_id"]
        or payload.get("primary_upstream_id") != primary_policy["upstream_id"]
        or payload.get("reference_upstream_id")
        != reference_policy["upstream_id"]
    ):
        raise SourceEvidenceError(
            "cross-source evidence does not bind independent upstreams"
        )
    summary = payload.get("summary")
    policy = payload.get("policy")
    per_code = payload.get("per_code")
    if (
        not isinstance(summary, Mapping)
        or not isinstance(policy, Mapping)
        or not isinstance(per_code, list)
    ):
        raise SourceEvidenceError("cross-source validation evidence is malformed")
    requested = _normalise_codes(payload.get("requested_codes", []))
    per_code_codes = [
        str(item.get("code", ""))
        for item in per_code
        if isinstance(item, Mapping)
    ]
    if (
        payload.get("requested_codes") != requested
        or len(per_code_codes) != len(set(per_code_codes))
        or sorted(per_code_codes) != requested
        or int(summary.get("requested_code_count", -1)) != len(requested)
    ):
        raise SourceEvidenceError(
            "cross-source requested/per-code coverage is inconsistent"
        )
    compared = int(summary.get("compared_return_count", -1))
    conflicts = int(summary.get("conflict_count", -1))
    expected_compared = sum(
        int(item.get("overlap_returns", 0))
        for item in per_code
        if isinstance(item, Mapping)
        and item.get("status") in {"passed", "conflict"}
    )
    expected_conflicts = sum(
        int(item.get("conflicts", 0))
        for item in per_code
        if isinstance(item, Mapping)
    )
    if compared != expected_compared or conflicts != expected_conflicts:
        raise SourceEvidenceError(
            "cross-source summary conflicts with per-code evidence"
        )
    ratio = float(conflicts / compared) if compared else 1.0
    if not math.isclose(
        float(summary.get("conflict_ratio", -1.0)),
        ratio,
        rel_tol=0,
        abs_tol=1e-15,
    ):
        raise SourceEvidenceError("cross-source conflict ratio is inconsistent")
    insufficient = summary.get("insufficient_codes")
    if not isinstance(insufficient, list):
        raise SourceEvidenceError("cross-source insufficient_codes is invalid")
    expected_insufficient = sorted(
        str(item["code"])
        for item in per_code
        if item.get("status") not in {"passed", "conflict"}
    )
    if sorted(insufficient) != expected_insufficient:
        raise SourceEvidenceError(
            "cross-source insufficient status is inconsistent"
        )
    unexpected_primary = summary.get("unexpected_primary_codes")
    unexpected_reference = summary.get("unexpected_reference_codes")
    if not isinstance(unexpected_primary, list) or not isinstance(
        unexpected_reference, list
    ):
        raise SourceEvidenceError("cross-source unexpected codes are invalid")
    expected_acceptable = (
        bool(per_code)
        and not insufficient
        and not unexpected_primary
        and not unexpected_reference
        and compared > 0
        and ratio <= float(policy["max_conflict_ratio"])
    )
    if bool(summary.get("acceptable")) != expected_acceptable:
        raise SourceEvidenceError(
            "cross-source acceptable flag is inconsistent"
        )
    return payload


def require_cross_source_acceptance(evidence: Mapping[str, Any]) -> None:
    payload = validate_cross_source_evidence(evidence)
    if not payload["summary"]["acceptable"]:
        summary = payload["summary"]
        public_evidence = summarize_cross_source_failure(payload)
        code_summary = ",".join(
            (
                f"{item['code']}"
                f"(r={item['return_conflicts']},"
                f"o={item['geometry_conflicts']},"
                f"v={item['volume_conflicts']})"
            )
            for item in public_evidence["conflicted_codes"]
        )
        if not code_summary:
            code_summary = "none"
        raise CrossSourceConflictError(
            "independent market-data validation failed: "
            f"{summary['conflict_count']} conflicts across "
            f"{summary['compared_return_count']} returns; "
            f"insufficient_codes={summary['insufficient_codes'][:10]}; "
            f"conflicted_codes={code_summary}",
            evidence_summary=public_evidence,
        )


def summarize_cross_source_failure(
    evidence: Mapping[str, Any],
    *,
    max_codes: int = 10,
    max_examples: int = 10,
) -> dict[str, Any]:
    """Return bounded, path-free failure evidence safe for durable job state."""

    if not 1 <= max_codes <= 20 or not 1 <= max_examples <= 20:
        raise ValueError("failure evidence limits must be between 1 and 20")
    payload = validate_cross_source_evidence(evidence)
    summary = payload["summary"]
    conflicted: list[dict[str, Any]] = []
    for item in payload["per_code"]:
        if item.get("status") != "conflict":
            continue
        conflicted.append(
            {
                "code": str(item["code"]),
                "conflicts": int(item.get("conflicts", 0)),
                "return_conflicts": int(item.get("return_conflicts", 0)),
                "geometry_conflicts": int(item.get("geometry_conflicts", 0)),
                "volume_conflicts": int(item.get("volume_conflicts", 0)),
                "max_abs_return_delta": float(
                    item.get("max_abs_return_delta", 0.0)
                ),
                "max_abs_geometry_delta": float(
                    item.get("max_abs_geometry_delta", 0.0)
                ),
                "max_abs_volume_log_scale_delta": float(
                    item.get("max_abs_volume_log_scale_delta", 0.0)
                ),
            }
        )
    examples: list[dict[str, Any]] = []
    for item in payload["conflict_examples"][:max_examples]:
        dimensions: list[str] = []
        if float(item.get("abs_delta", 0.0)) > float(
            payload["policy"]["return_abs_tolerance"]
        ):
            dimensions.append("return")
        if bool(item.get("price_geometry_conflict")):
            dimensions.append("ohlc_geometry")
        if bool(item.get("volume_scale_conflict")):
            dimensions.append("volume_scale")
        examples.append(
            {
                "code": str(item["code"]),
                "date": str(item["date"]),
                "dimensions": dimensions,
                "primary_return": float(item["primary_return"]),
                "reference_return": float(item["reference_return"]),
                "abs_return_delta": float(item["abs_delta"]),
            }
        )
    return {
        "schema_version": "cross-source-failure-summary/v1",
        "summary": {
            "requested_code_count": int(summary["requested_code_count"]),
            "compared_return_count": int(summary["compared_return_count"]),
            "conflict_count": int(summary["conflict_count"]),
            "insufficient_codes": [
                str(code) for code in summary["insufficient_codes"][:max_codes]
            ],
            "conflicted_code_count": len(conflicted),
            "evidence_truncated": (
                len(conflicted) > max_codes
                or len(payload["conflict_examples"]) > max_examples
            ),
        },
        "conflicted_codes": conflicted[:max_codes],
        "examples": examples,
    }


def build_cache_source_provenance(
    frame: pd.DataFrame,
    batches: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind all source batches and the final merged cache into one identity."""

    if frame.empty:
        raise SourceEvidenceError("cannot create provenance for empty cache")
    validated = [validate_daily_fetch_evidence(batch) for batch in batches]
    if not validated:
        raise SourceEvidenceError("at least one source batch is required")
    providers = sorted({str(batch["provider"]) for batch in validated})
    endpoints = sorted({str(batch["endpoint"]) for batch in validated})
    adjustments = sorted({str(batch["adjustment"]) for batch in validated})
    levels = sorted({str(batch["evidence_level"]) for batch in validated})
    all_cross_validated = all(
        batch.get("cross_validation") is not None
        and bool(batch["cross_validation"]["summary"]["acceptable"])
        for batch in validated
    )
    all_raw_cross_validated = all(
        _raw_cross_validated(batch) for batch in validated
    )
    all_adjusted_factor_validated = all(
        _adjusted_factor_validated(batch) for batch in validated
    )
    identity_consistent = (
        len(providers) == 1
        and len(adjustments) == 1
        and len(endpoints) == 1
    )
    payload: dict[str, Any] = {
        "schema_version": CACHE_SOURCE_PROVENANCE_SCHEMA,
        "providers": providers,
        "endpoints": endpoints,
        "adjustments": adjustments,
        "evidence_levels": levels,
        "identity_consistent": identity_consistent,
        "all_batches_cross_validated": all_cross_validated,
        "all_batches_raw_cross_validated": all_raw_cross_validated,
        "all_batches_adjusted_factor_validated": (
            all_adjusted_factor_validated
        ),
        "complete_code_coverage": all(
            bool(batch["complete_code_coverage"]) for batch in validated
        ),
        "frame_codes": _frame_codes(frame),
        "frame_digest": _frame_digest(
            frame,
            {
                "providers": providers,
                "endpoints": endpoints,
                "adjustments": adjustments,
            },
        ),
        "batches": validated,
        "limitations": [
            "Public aggregators remain research-only evidence even when "
            "independent feeds agree."
        ],
    }
    payload["content_sha256"] = _content_sha256(payload)
    return validate_cache_source_provenance(payload, frame=frame)


def validate_cache_source_provenance(
    evidence: Mapping[str, Any],
    *,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    payload = dict(evidence)
    if payload.get("schema_version") != CACHE_SOURCE_PROVENANCE_SCHEMA:
        raise SourceEvidenceError("unsupported cache source provenance schema")
    _validate_hash(payload)
    batches = payload.get("batches")
    if not isinstance(batches, list) or not batches:
        raise SourceEvidenceError("cache source batches are missing")
    validated = [validate_daily_fetch_evidence(batch) for batch in batches]
    providers = sorted({str(batch["provider"]) for batch in validated})
    endpoints = sorted({str(batch["endpoint"]) for batch in validated})
    adjustments = sorted({str(batch["adjustment"]) for batch in validated})
    levels = sorted({str(batch["evidence_level"]) for batch in validated})
    all_cross_validated = all(
        batch.get("cross_validation") is not None
        and bool(batch["cross_validation"]["summary"]["acceptable"])
        for batch in validated
    )
    all_raw_cross_validated = all(
        _raw_cross_validated(batch) for batch in validated
    )
    all_adjusted_factor_validated = all(
        _adjusted_factor_validated(batch) for batch in validated
    )
    complete_coverage = all(
        bool(batch["complete_code_coverage"]) for batch in validated
    )
    if payload.get("providers") != providers:
        raise SourceEvidenceError("cache providers conflict with batches")
    if payload.get("adjustments") != adjustments:
        raise SourceEvidenceError("cache adjustments conflict with batches")
    if payload.get("endpoints") != endpoints:
        raise SourceEvidenceError("cache endpoints conflict with batches")
    if payload.get("evidence_levels") != levels:
        raise SourceEvidenceError("cache evidence levels conflict with batches")
    if bool(payload.get("all_batches_cross_validated")) != all_cross_validated:
        raise SourceEvidenceError(
            "cache cross-validation flag conflicts with batches"
        )
    if (
        "all_batches_raw_cross_validated" in payload
        and payload.get("all_batches_raw_cross_validated")
        is not all_raw_cross_validated
    ):
        raise SourceEvidenceError(
            "cache raw cross-validation flag conflicts with batches"
        )
    if (
        "all_batches_adjusted_factor_validated" in payload
        and payload.get("all_batches_adjusted_factor_validated")
        is not all_adjusted_factor_validated
    ):
        raise SourceEvidenceError(
            "cache adjustment factor flag conflicts with batches"
        )
    if bool(payload.get("complete_code_coverage")) != complete_coverage:
        raise SourceEvidenceError(
            "cache coverage flag conflicts with batches"
        )
    if bool(payload.get("identity_consistent")) != (
        len(providers) == 1
        and len(adjustments) == 1
        and len(endpoints) == 1
    ):
        raise SourceEvidenceError("cache identity consistency is invalid")
    if frame is not None:
        if payload.get("frame_codes") != _frame_codes(frame):
            raise SourceEvidenceError("cache frame codes changed")
        expected = _frame_digest(
            frame,
            {
                "providers": payload["providers"],
                "endpoints": payload["endpoints"],
                "adjustments": payload["adjustments"],
            },
        )
        if payload.get("frame_digest") != expected:
            raise SourceEvidenceError("cache frame digest changed")
    return payload
