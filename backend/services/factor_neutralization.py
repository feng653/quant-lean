"""Fail-closed exposure inputs for factor neutralization."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from backend.data.point_in_time_master import PointInTimeMasterStore

NeutralizationMode = Literal["none", "industry", "size", "industry+size"]

NEUTRALIZATION_SCHEMA = "factor-neutralization/v1"
FIELD_PROVENANCE_SCHEMA = "point-in-time-field-provenance/v1"
INDUSTRY_SCOPE = "cninfo_008001"
SIZE_FIELDS = ("float_market_cap", "market_cap")
_TRUSTED_FIELD_LEVELS = {
    "public_cross_validated",
    "licensed",
    "licensed_vendor",
    "exchange_authoritative",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


class NeutralizationInputError(ValueError):
    """Exposure evidence is unavailable or incomplete."""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def inspect_size_capability(
    fields: Sequence[str],
    provenance: Mapping[str, Any] | None,
    *,
    requested_field: str = "auto",
) -> dict[str, Any]:
    """Inspect field-level PIT size provenance without inferring semantics."""

    available = [field for field in SIZE_FIELDS if field in set(fields)]
    selected = (
        next(iter(available), None)
        if requested_field == "auto"
        else requested_field
        if requested_field in available
        else None
    )
    field_evidence = (
        provenance.get("field_provenance")
        if isinstance(provenance, Mapping)
        and isinstance(provenance.get("field_provenance"), Mapping)
        else {}
    )
    evidence = (
        field_evidence.get(selected)
        if selected is not None and isinstance(field_evidence, Mapping)
        else None
    )
    reason: str | None = None
    if selected is None:
        reason = "point_in_time_size_field_missing"
    elif not isinstance(evidence, Mapping):
        reason = "point_in_time_size_provenance_missing"
    elif evidence.get("schema_version") != FIELD_PROVENANCE_SCHEMA:
        reason = "point_in_time_size_provenance_invalid"
    elif evidence.get("field") != selected:
        reason = "point_in_time_size_field_identity_mismatch"
    elif evidence.get("point_in_time") is not True:
        reason = "point_in_time_size_semantics_missing"
    elif evidence.get("effective_date_semantics") != "trading_date_close":
        reason = "point_in_time_size_semantics_missing"
    elif evidence.get("available_at") != "market_close":
        reason = "point_in_time_size_availability_invalid"
    elif evidence.get("observation_lag_sessions") != 0:
        reason = "point_in_time_size_availability_invalid"
    elif evidence.get("evidence_level") not in _TRUSTED_FIELD_LEVELS:
        reason = "point_in_time_size_evidence_insufficient"
    elif any(
        not _SAFE_SOURCE_ID.fullmatch(str(evidence.get(key) or ""))
        for key in ("provider", "dataset", "version")
    ):
        reason = "point_in_time_size_provenance_invalid"
    elif not _is_aware_timestamp(evidence.get("retrieved_at")):
        reason = "point_in_time_size_provenance_invalid"
    elif not _SHA256.fullmatch(str(evidence.get("content_sha256") or "")):
        reason = "point_in_time_size_provenance_invalid"
    return {
        "schema_version": "factor-neutralization-readiness/v1",
        "ready": reason is None,
        "reason": reason,
        "selected_field": selected,
        "available_fields": available,
        "required_provenance_schema": FIELD_PROVENANCE_SCHEMA,
        "evidence": (
            {
                key: evidence.get(key)
                for key in (
                    "schema_version",
                    "field",
                    "point_in_time",
                    "effective_date_semantics",
                    "available_at",
                    "observation_lag_sessions",
                    "evidence_level",
                    "provider",
                    "dataset",
                    "version",
                    "retrieved_at",
                    "content_sha256",
                )
                if key in evidence
            }
            if reason is None and isinstance(evidence, Mapping)
            else None
        ),
    }


def _is_aware_timestamp(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def extract_size_panel(
    frame: pd.DataFrame,
    *,
    dates: pd.DatetimeIndex,
    codes: Sequence[str],
    provenance: Mapping[str, Any] | None,
    requested_field: str = "auto",
    required_codes_by_date: Mapping[str, Sequence[str]] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    fields = (
        sorted({str(column[-1]) for column in frame.columns})
        if isinstance(frame.columns, pd.MultiIndex)
        else []
    )
    capability = inspect_size_capability(
        fields,
        provenance,
        requested_field=requested_field,
    )
    if not capability["ready"]:
        raise NeutralizationInputError(
            code=str(capability["reason"]),
            message="规模中性化缺少可信、点时可用的市值字段及来源证据",
        )
    field = str(capability["selected_field"])
    values: dict[str, pd.Series] = {}
    for code in codes:
        key = (str(code), field)
        if key not in frame.columns:
            raise NeutralizationInputError(
                code="point_in_time_size_coverage_missing",
                message="规模中性化市值字段未覆盖全部股票",
            )
        values[str(code)] = pd.to_numeric(
            frame[key],
            errors="coerce",
        )
    panel = pd.DataFrame(values, index=frame.index).reindex(
        index=dates,
        columns=list(codes),
    )
    required = pd.DataFrame(
        True,
        index=dates,
        columns=list(codes),
        dtype=bool,
    )
    if required_codes_by_date is not None:
        required.loc[:, :] = False
        for timestamp in dates:
            day = pd.Timestamp(timestamp).strftime("%Y-%m-%d")
            active = list(required_codes_by_date.get(day, ()))
            required.loc[timestamp, active] = True
    values_array = panel.to_numpy(dtype=float)
    finite_positive = np.isfinite(values_array) & (values_array > 0)
    if not finite_positive[required.to_numpy(dtype=bool)].all():
        raise NeutralizationInputError(
            code="point_in_time_size_coverage_missing",
            message="规模中性化市值字段存在缺失、非有限值或非正值",
        )
    panel = panel.where(required)
    required_observations = int(required.to_numpy(dtype=bool).sum())
    return panel, {
        "field": field,
        "coverage": {
            "trading_dates": len(dates),
            "security_codes": len(codes),
            "observations": required_observations,
            "coverage_ratio": 1.0,
        },
        "provenance": capability["evidence"],
    }


def load_industry_panel(
    store: PointInTimeMasterStore,
    *,
    dates: pd.DatetimeIndex,
    codes: Sequence[str],
    scope_id: str = INDUSTRY_SCOPE,
    required_codes_by_date: Mapping[str, Sequence[str]] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Query immutable PIT industry evidence independently for every date."""

    values: list[dict[str, str]] = []
    source_batches: dict[str, dict[str, Any]] = {}
    for timestamp in dates:
        day = pd.Timestamp(timestamp).strftime("%Y-%m-%d")
        required_codes = list(
            required_codes_by_date.get(day, ())
            if required_codes_by_date is not None
            else codes
        )
        if not required_codes:
            raise NeutralizationInputError(
                code="historical_membership_empty",
                message=f"行业中性化在交易日 {day} 没有有效点时成分",
            )
        result = store.query_as_of(
            domain="industry",
            scope_id=scope_id,
            as_of=day,
            security_codes=required_codes,
        )
        if not result.get("available"):
            reason = str(result.get("reason") or "industry_coverage_missing")
            raise NeutralizationInputError(
                code=reason,
                message=f"行业中性化在交易日 {day} 缺少完整点时行业覆盖",
            )
        row: dict[str, str] = {}
        for record in result.get("records") or []:
            if not isinstance(record, Mapping):
                continue
            code = str(record.get("security_code") or "")
            attributes = record.get("attributes")
            industry = (
                str(attributes.get("industry_code") or "").strip()
                if isinstance(attributes, Mapping)
                else ""
            )
            if code and industry:
                row[code] = industry
        missing = sorted(set(required_codes) - set(row))
        if missing:
            raise NeutralizationInputError(
                code="industry_effective_period_missing",
                message=f"行业中性化在交易日 {day} 缺少完整点时行业覆盖",
            )
        values.append(row)
        for batch in result.get("source_batches") or []:
            if isinstance(batch, Mapping) and batch.get("batch_id"):
                source_batches[str(batch["batch_id"])] = dict(batch)
    panel = pd.DataFrame(values, index=dates, columns=list(codes))
    required_observations = sum(
        len(required_codes_by_date.get(
            pd.Timestamp(timestamp).strftime("%Y-%m-%d"),
            (),
        ))
        for timestamp in dates
    ) if required_codes_by_date is not None else int(panel.size)
    return panel, {
        "scope_id": scope_id,
        "query_semantics": "one_verified_as_of_query_per_trading_date",
        "coverage": {
            "trading_dates": len(dates),
            "security_codes": len(codes),
            "observations": required_observations,
            "coverage_ratio": 1.0,
        },
        "source_batches": [
            source_batches[key] for key in sorted(source_batches)
        ],
    }
