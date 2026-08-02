"""Deterministic, fail-closed quality evidence for research market data."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


MARKET_DATA_QUALITY_SCHEMA = "market-data-quality/v1"
REQUIRED_MARKET_FIELDS = ("open", "high", "low", "close", "volume")
_REQUIRED_STATISTICS = {
    "missing_values",
    "non_finite_values",
    "non_numeric_values",
    "non_positive_ohlc",
    "negative_volume",
    "ohlc_relationship_errors",
}
_SAMPLE_LIMIT = 20


class MarketDataQualityError(ValueError):
    """Stored market-data quality evidence is incomplete or inconsistent."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MarketDataQualityError(
            "market-data quality evidence must be finite canonical JSON"
        ) from exc


def _content_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _issue(code: str, count: int, **details: Any) -> dict[str, Any]:
    return {"code": code, "count": int(count), **details}


def _normalise_date(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise MarketDataQualityError("audited_through must be a valid date")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp.normalize()


def _date_text(value: pd.Timestamp | None) -> str | None:
    return None if value is None else value.date().isoformat()


def _column_pairs(
    frame: pd.DataFrame,
) -> tuple[list[tuple[str, str]], bool]:
    if not isinstance(frame.columns, pd.MultiIndex):
        return [], False
    if frame.columns.nlevels < 2:
        return [], False
    return [
        (str(column[0]).strip(), str(column[-1]).strip().lower())
        for column in frame.columns
    ], True


def _numeric_field(
    frame: pd.DataFrame,
    pairs: Sequence[tuple[str, str]],
    field: str,
) -> tuple[pd.DataFrame, int, int, int]:
    positions = [index for index, pair in enumerate(pairs) if pair[1] == field]
    if not positions:
        return pd.DataFrame(index=frame.index), 0, 0, 0
    raw = frame.iloc[:, positions]
    numeric = raw.apply(pd.to_numeric, errors="coerce")
    raw_missing = raw.isna()
    missing_count = int(raw_missing.to_numpy(dtype=bool).sum())
    non_numeric_count = int(
        (numeric.isna() & ~raw_missing).to_numpy(dtype=bool).sum()
    )
    values = numeric.to_numpy(dtype="float64", copy=False)
    non_finite_count = int(np.isinf(values).sum())
    return numeric, missing_count, non_numeric_count, non_finite_count


def _pair_series(
    frame: pd.DataFrame,
    pairs: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], pd.Series]:
    positions: dict[tuple[str, str], list[int]] = {}
    for index, pair in enumerate(pairs):
        positions.setdefault(pair, []).append(index)
    return {
        pair: pd.to_numeric(frame.iloc[:, indexes[0]], errors="coerce")
        for pair, indexes in positions.items()
        if len(indexes) == 1
    }


@dataclass(frozen=True, slots=True)
class MarketDataQualitySnapshot:
    """Canonical audit result embedded in an immutable RunManifest."""

    payload: dict[str, Any]

    @property
    def is_clean(self) -> bool:
        return bool(self.payload["is_clean"])

    @property
    def fatal_codes(self) -> tuple[str, ...]:
        return tuple(item["code"] for item in self.payload["fatal"])

    @property
    def content_sha256(self) -> str:
        return str(self.payload["content_sha256"])

    def to_dict(self) -> dict[str, Any]:
        # Canonical round-trip also prevents returning mutable shared children.
        return json.loads(_canonical_bytes(self.payload))

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        verify_hash: bool = True,
    ) -> "MarketDataQualitySnapshot":
        if not isinstance(payload, Mapping):
            raise MarketDataQualityError(
                "market_data_quality must be an object"
            )
        candidate = dict(payload)
        if candidate.get("schema_version") != MARKET_DATA_QUALITY_SCHEMA:
            raise MarketDataQualityError(
                "unsupported market-data quality schema"
            )
        stored_hash = candidate.pop("content_sha256", None)
        if (
            not isinstance(stored_hash, str)
            or len(stored_hash) != 64
            or any(character not in "0123456789abcdef" for character in stored_hash)
        ):
            raise MarketDataQualityError(
                "market-data quality content hash is invalid"
            )
        if verify_hash and _content_sha256(candidate) != stored_hash:
            raise MarketDataQualityError(
                "market-data quality content hash verification failed"
            )
        candidate["content_sha256"] = stored_hash
        try:
            cls._validate(candidate)
        except MarketDataQualityError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataQualityError(
                "market-data quality evidence is malformed"
            ) from exc
        return cls(json.loads(_canonical_bytes(candidate)))

    @staticmethod
    def _validate(payload: Mapping[str, Any]) -> None:
        required_top_level = {
            "schema_version",
            "audited_through",
            "source",
            "date_range",
            "axes",
            "coverage",
            "statistics",
            "fatal",
            "warnings",
            "limitations",
            "is_clean",
            "content_sha256",
        }
        if set(payload) != required_top_level:
            raise MarketDataQualityError(
                "market-data quality evidence fields are incomplete"
            )
        _normalise_date(payload["audited_through"])
        source = payload["source"]
        if (
            not isinstance(source, Mapping)
            or not isinstance(source.get("provider"), str)
            or not source["provider"].strip()
            or not isinstance(source.get("price_adjustment"), str)
            or not source["price_adjustment"].strip()
        ):
            raise MarketDataQualityError(
                "market-data source and price adjustment are required"
            )
        provenance_sha256 = source.get("provenance_sha256")
        if provenance_sha256 is not None and (
            not isinstance(provenance_sha256, str)
            or len(provenance_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in provenance_sha256
            )
        ):
            raise MarketDataQualityError(
                "market-data source provenance hash is invalid"
            )
        axes = payload["axes"]
        required_axes = {
            "datetime_index",
            "index_unique",
            "index_monotonic_increasing",
            "columns_multiindex",
            "columns_unique",
            "invalid_date_count",
            "future_row_count",
        }
        if not isinstance(axes, Mapping) or set(axes) != required_axes:
            raise MarketDataQualityError("axis audit is incomplete")
        for key in (
            "datetime_index",
            "index_unique",
            "index_monotonic_increasing",
            "columns_multiindex",
            "columns_unique",
        ):
            if not isinstance(axes[key], bool):
                raise MarketDataQualityError(f"axis flag {key} is invalid")
        for key in ("invalid_date_count", "future_row_count"):
            if not isinstance(axes[key], int) or axes[key] < 0:
                raise MarketDataQualityError(f"axis count {key} is invalid")

        coverage = payload["coverage"]
        required_coverage = {
            "code_count",
            "codes",
            "required_fields",
            "observed_fields",
            "audited_fields",
            "missing_required_fields",
            "codes_missing_required_fields_count",
            "codes_missing_required_fields_sample",
            "normalized_column_duplicate_count",
        }
        if not isinstance(coverage, Mapping) or set(coverage) != required_coverage:
            raise MarketDataQualityError("field coverage audit is incomplete")
        if coverage["required_fields"] != list(REQUIRED_MARKET_FIELDS):
            raise MarketDataQualityError("required OHLCV fields are incomplete")
        if coverage["audited_fields"] != list(REQUIRED_MARKET_FIELDS):
            raise MarketDataQualityError("critical OHLCV fields were not audited")
        codes = coverage["codes"]
        if (
            not isinstance(codes, list)
            or codes != sorted(set(codes))
            or int(coverage["code_count"]) != len(codes)
        ):
            raise MarketDataQualityError("market-data code coverage is invalid")
        observed_fields = coverage["observed_fields"]
        if (
            not isinstance(observed_fields, list)
            or observed_fields != sorted(set(observed_fields))
        ):
            raise MarketDataQualityError("observed market fields are invalid")
        expected_missing = sorted(
            set(REQUIRED_MARKET_FIELDS) - set(observed_fields)
        )
        if coverage["missing_required_fields"] != expected_missing:
            raise MarketDataQualityError(
                "missing required field evidence is inconsistent"
            )
        missing_code_count = coverage[
            "codes_missing_required_fields_count"
        ]
        if not isinstance(missing_code_count, int) or missing_code_count < 0:
            raise MarketDataQualityError(
                "per-code field coverage count is invalid"
            )
        if not isinstance(
            coverage["codes_missing_required_fields_sample"],
            list,
        ):
            raise MarketDataQualityError(
                "per-code field coverage sample is invalid"
            )
        normalized_duplicate_count = coverage[
            "normalized_column_duplicate_count"
        ]
        if (
            not isinstance(normalized_duplicate_count, int)
            or normalized_duplicate_count < 0
        ):
            raise MarketDataQualityError(
                "normalized column duplicate count is invalid"
            )

        statistics = payload["statistics"]
        if not isinstance(statistics, Mapping) or set(statistics) != _REQUIRED_STATISTICS:
            raise MarketDataQualityError("market-data statistics are incomplete")
        for name, statistic in statistics.items():
            if not isinstance(statistic, Mapping):
                raise MarketDataQualityError(f"statistic {name} is invalid")
            count = statistic.get("count")
            if not isinstance(count, int) or count < 0:
                raise MarketDataQualityError(
                    f"statistic {name} count is invalid"
                )

        for severity in ("fatal", "warnings"):
            issues = payload[severity]
            if not isinstance(issues, list):
                raise MarketDataQualityError(f"{severity} must be a list")
            codes_seen: list[str] = []
            for item in issues:
                if (
                    not isinstance(item, Mapping)
                    or not isinstance(item.get("code"), str)
                    or not isinstance(item.get("count"), int)
                    or item["count"] < 0
                ):
                    raise MarketDataQualityError(
                        f"{severity} issue is invalid"
                    )
                codes_seen.append(item["code"])
            if codes_seen != sorted(set(codes_seen)):
                raise MarketDataQualityError(
                    f"{severity} issues must be unique and sorted"
                )
        if not isinstance(payload["limitations"], list) or not all(
            isinstance(item, str) and item
            for item in payload["limitations"]
        ):
            raise MarketDataQualityError("quality limitations are invalid")
        date_range = payload["date_range"]
        required_range = {"start", "end", "row_count"}
        if not isinstance(date_range, Mapping) or set(date_range) != required_range:
            raise MarketDataQualityError("audited date range is incomplete")
        if (
            not isinstance(date_range["row_count"], int)
            or date_range["row_count"] < 0
        ):
            raise MarketDataQualityError("audited row count is invalid")
        if date_range["row_count"] == 0:
            if date_range["start"] is not None or date_range["end"] is not None:
                raise MarketDataQualityError("empty audit has a date range")
        else:
            start = _normalise_date(date_range["start"])
            end = _normalise_date(date_range["end"])
            if start > end or end > _normalise_date(payload["audited_through"]):
                raise MarketDataQualityError(
                    "audited date range exceeds its declared cutoff"
                )
        expected_clean = not payload["fatal"]
        if payload["is_clean"] is not expected_clean:
            raise MarketDataQualityError("is_clean conflicts with fatal issues")
        fatal_codes = {item["code"] for item in payload["fatal"]}
        warning_codes = {item["code"] for item in payload["warnings"]}
        expected_fatal_conditions = {
            "empty_data": date_range["row_count"] == 0,
            "invalid_date_axis": (
                not axes["datetime_index"] or axes["invalid_date_count"] > 0
            ),
            "duplicate_dates": not axes["index_unique"],
            "unsorted_dates": not axes["index_monotonic_increasing"],
            "future_rows_present": axes["future_row_count"] > 0,
            "invalid_column_axis": not axes["columns_multiindex"],
            "duplicate_columns": not axes["columns_unique"],
            "duplicate_normalized_columns": normalized_duplicate_count > 0,
            "empty_code_coverage": int(coverage["code_count"]) == 0,
            "missing_required_fields": bool(expected_missing),
            "incomplete_code_field_coverage": missing_code_count > 0,
            "non_numeric_values": (
                statistics["non_numeric_values"]["count"] > 0
            ),
            "non_finite_values": (
                statistics["non_finite_values"]["count"] > 0
            ),
            "non_positive_ohlc": (
                statistics["non_positive_ohlc"]["count"] > 0
            ),
            "negative_volume": statistics["negative_volume"]["count"] > 0,
            "ohlc_relationship_errors": (
                statistics["ohlc_relationship_errors"]["count"] > 0
            ),
        }
        for code, present in expected_fatal_conditions.items():
            if present != (code in fatal_codes):
                raise MarketDataQualityError(
                    f"fatal issue {code} conflicts with audited statistics"
                )
        missing_warning = statistics["missing_values"]["count"] > 0
        if missing_warning != ("missing_values_present" in warning_codes):
            raise MarketDataQualityError(
                "missing-value warning conflicts with audited statistics"
            )


def audit_market_data(
    frame: pd.DataFrame,
    *,
    test_end: str | pd.Timestamp,
    source: str,
    price_adjustment: str,
    source_provenance_sha256: str | None = None,
) -> MarketDataQualitySnapshot:
    """Audit only observations on or before ``test_end``.

    Future row labels are counted as a fatal caller error, but their values are
    never inspected. Missing observations are reported without pretending to
    distinguish legitimate pre-listing gaps from data loss.
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("market data must be a pandas DataFrame")
    cutoff = _normalise_date(test_end)
    fatal: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    limitations = [
        (
            "Missing observations are counted but not classified because "
            "listing, suspension, and delisting state is not present."
        ),
        (
            "This audit validates OHLCV shape and arithmetic consistency; it "
            "does not certify point-in-time constituents or corporate actions."
        ),
        (
            "Source and adjustment metadata are caller-declared; per-symbol "
            "provider fallbacks and adjustment factors are not available."
        ),
    ]

    datetime_index = isinstance(frame.index, pd.DatetimeIndex)
    parsed_index = pd.to_datetime(frame.index, errors="coerce")
    invalid_dates = int(pd.isna(parsed_index).sum())
    valid_dates = pd.DatetimeIndex(parsed_index[~pd.isna(parsed_index)])
    if valid_dates.tz is not None:
        valid_dates = valid_dates.tz_convert(None)
    valid_positions = np.flatnonzero(~pd.isna(parsed_index))
    historical_mask = valid_dates.normalize() <= cutoff
    audited_positions = valid_positions[historical_mask]
    future_row_count = int((~historical_mask).sum())
    audited = frame.iloc[audited_positions].copy()
    audited.index = valid_dates[historical_mask]

    index_unique = bool(audited.index.is_unique)
    index_sorted = bool(audited.index.is_monotonic_increasing)
    columns_unique = bool(frame.columns.is_unique)
    pairs, columns_multiindex = _column_pairs(frame)
    if frame.empty or audited.empty:
        fatal.append(_issue("empty_data", 1))
    if not datetime_index or invalid_dates:
        fatal.append(
            _issue(
                "invalid_date_axis",
                invalid_dates + (0 if datetime_index else 1),
            )
        )
    if not index_unique:
        fatal.append(
            _issue("duplicate_dates", int(audited.index.duplicated().sum()))
        )
    if not index_sorted:
        fatal.append(_issue("unsorted_dates", 1))
    if future_row_count:
        fatal.append(_issue("future_rows_present", future_row_count))
    if not columns_multiindex:
        fatal.append(_issue("invalid_column_axis", 1))
    if not columns_unique:
        fatal.append(
            _issue("duplicate_columns", int(frame.columns.duplicated().sum()))
        )

    normalized_pair_duplicates = len(pairs) - len(set(pairs))
    if normalized_pair_duplicates:
        fatal.append(
            _issue("duplicate_normalized_columns", normalized_pair_duplicates)
        )
    codes = sorted({code for code, _ in pairs if code})
    observed_fields = sorted({field for _, field in pairs if field})
    missing_fields = sorted(set(REQUIRED_MARKET_FIELDS) - set(observed_fields))
    if not codes:
        fatal.append(_issue("empty_code_coverage", 1))
    if missing_fields:
        fatal.append(
            _issue(
                "missing_required_fields",
                len(missing_fields),
                fields=missing_fields,
            )
        )
    missing_by_code = [
        {
            "code": code,
            "missing_fields": sorted(
                set(REQUIRED_MARKET_FIELDS)
                - {field for pair_code, field in pairs if pair_code == code}
            ),
        }
        for code in codes
        if set(REQUIRED_MARKET_FIELDS)
        - {field for pair_code, field in pairs if pair_code == code}
    ]
    if missing_by_code:
        fatal.append(
            _issue(
                "incomplete_code_field_coverage",
                len(missing_by_code),
                sample=missing_by_code[:_SAMPLE_LIMIT],
            )
        )

    missing_by_field: dict[str, int] = {}
    non_numeric_by_field: dict[str, int] = {}
    non_finite_by_field: dict[str, int] = {}
    numeric_fields: dict[str, pd.DataFrame] = {}
    for field in REQUIRED_MARKET_FIELDS:
        numeric, missing, non_numeric, non_finite = _numeric_field(
            audited,
            pairs,
            field,
        )
        numeric_fields[field] = numeric
        missing_by_field[field] = missing
        non_numeric_by_field[field] = non_numeric
        non_finite_by_field[field] = non_finite
    missing_count = sum(missing_by_field.values())
    non_numeric_count = sum(non_numeric_by_field.values())
    non_finite_count = sum(non_finite_by_field.values())
    if missing_count:
        warnings.append(
            _issue(
                "missing_values_present",
                missing_count,
                by_field=missing_by_field,
            )
        )
    if non_numeric_count:
        fatal.append(
            _issue(
                "non_numeric_values",
                non_numeric_count,
                by_field=non_numeric_by_field,
            )
        )
    if non_finite_count:
        fatal.append(
            _issue(
                "non_finite_values",
                non_finite_count,
                by_field=non_finite_by_field,
            )
        )

    non_positive_by_field = {
        field: int(
            (
                numeric_fields[field].notna()
                & (numeric_fields[field] <= 0)
            ).to_numpy(dtype=bool).sum()
        )
        for field in ("open", "high", "low", "close")
    }
    non_positive_count = sum(non_positive_by_field.values())
    if non_positive_count:
        fatal.append(
            _issue(
                "non_positive_ohlc",
                non_positive_count,
                by_field=non_positive_by_field,
            )
        )
    volume = numeric_fields["volume"]
    negative_volume_count = int(
        (volume.notna() & (volume < 0)).to_numpy(dtype=bool).sum()
    )
    if negative_volume_count:
        fatal.append(_issue("negative_volume", negative_volume_count))

    relationship_counts = {
        "high_below_low": 0,
        "high_below_open": 0,
        "high_below_close": 0,
        "low_above_open": 0,
        "low_above_close": 0,
    }
    relationship_row_count = 0
    series = _pair_series(audited, pairs)
    for code in codes:
        required = {
            field: series.get((code, field))
            for field in ("open", "high", "low", "close")
        }
        if any(value is None for value in required.values()):
            continue
        values = pd.DataFrame(required)
        finite = pd.Series(
            np.isfinite(values.to_numpy(dtype="float64")).all(axis=1),
            index=values.index,
        )
        comparisons = {
            "high_below_low": values["high"] < values["low"],
            "high_below_open": values["high"] < values["open"],
            "high_below_close": values["high"] < values["close"],
            "low_above_open": values["low"] > values["open"],
            "low_above_close": values["low"] > values["close"],
        }
        broken = pd.Series(False, index=values.index)
        for name, mask in comparisons.items():
            valid_mask = finite & mask
            relationship_counts[name] += int(valid_mask.sum())
            broken |= valid_mask
        relationship_row_count += int(broken.sum())
    if relationship_row_count:
        fatal.append(
            _issue(
                "ohlc_relationship_errors",
                relationship_row_count,
                by_rule=relationship_counts,
            )
        )

    fatal.sort(key=lambda item: item["code"])
    warnings.sort(key=lambda item: item["code"])
    start = audited.index.min() if not audited.empty else None
    end = audited.index.max() if not audited.empty else None
    source_identity = {
        "provider": str(source).strip(),
        "price_adjustment": str(price_adjustment).strip(),
    }
    if source_provenance_sha256 is not None:
        source_identity["provenance_sha256"] = source_provenance_sha256
    body = {
        "schema_version": MARKET_DATA_QUALITY_SCHEMA,
        "audited_through": cutoff.date().isoformat(),
        "source": source_identity,
        "date_range": {
            "start": _date_text(start),
            "end": _date_text(end),
            "row_count": len(audited),
        },
        "axes": {
            "datetime_index": datetime_index,
            "index_unique": index_unique,
            "index_monotonic_increasing": index_sorted,
            "columns_multiindex": columns_multiindex,
            "columns_unique": columns_unique,
            "invalid_date_count": invalid_dates,
            "future_row_count": future_row_count,
        },
        "coverage": {
            "code_count": len(codes),
            "codes": codes,
            "required_fields": list(REQUIRED_MARKET_FIELDS),
            "observed_fields": observed_fields,
            "audited_fields": list(REQUIRED_MARKET_FIELDS),
            "missing_required_fields": missing_fields,
            "codes_missing_required_fields_count": len(missing_by_code),
            "codes_missing_required_fields_sample": missing_by_code[
                :_SAMPLE_LIMIT
            ],
            "normalized_column_duplicate_count": (
                normalized_pair_duplicates
            ),
        },
        "statistics": {
            "missing_values": {
                "count": missing_count,
                "by_field": missing_by_field,
                "classification": "not_classified_by_listing_state",
            },
            "non_finite_values": {
                "count": non_finite_count,
                "by_field": non_finite_by_field,
            },
            "non_numeric_values": {
                "count": non_numeric_count,
                "by_field": non_numeric_by_field,
            },
            "non_positive_ohlc": {
                "count": non_positive_count,
                "by_field": non_positive_by_field,
            },
            "negative_volume": {"count": negative_volume_count},
            "ohlc_relationship_errors": {
                "count": relationship_row_count,
                "by_rule": relationship_counts,
            },
        },
        "fatal": fatal,
        "warnings": warnings,
        "limitations": limitations,
        "is_clean": not fatal,
    }
    body["content_sha256"] = _content_sha256(body)
    return MarketDataQualitySnapshot.from_dict(body)
