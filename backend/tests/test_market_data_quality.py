from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from backend.data.market_quality import (
    MARKET_DATA_QUALITY_SCHEMA,
    MarketDataQualityError,
    MarketDataQualitySnapshot,
    audit_market_data,
)


def _frame(
    *,
    dates: list[str] | None = None,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        dates or ["2024-01-02", "2024-01-03"],
        name="date",
    )
    frame = pd.DataFrame(
        {
            ("000001", "open"): [10.0, 10.5][: len(index)],
            ("000001", "high"): [11.0, 11.5][: len(index)],
            ("000001", "low"): [9.0, 10.0][: len(index)],
            ("000001", "close"): [10.5, 11.0][: len(index)],
            ("000001", "volume"): [1000.0, 1200.0][: len(index)],
        },
        index=index,
    )
    frame.columns = pd.MultiIndex.from_tuples(
        frame.columns,
        names=["code", "field"],
    )
    return frame


def _audit(frame: pd.DataFrame) -> MarketDataQualitySnapshot:
    return audit_market_data(
        frame,
        test_end="2024-01-03",
        source="akshare",
        price_adjustment="qfq",
    )


def test_clean_market_data_is_deterministic_and_json_safe() -> None:
    first = _audit(_frame())
    second = _audit(_frame())

    assert first.to_dict() == second.to_dict()
    assert first.payload["schema_version"] == MARKET_DATA_QUALITY_SCHEMA
    assert first.is_clean is True
    assert first.payload["fatal"] == []
    assert first.payload["date_range"] == {
        "start": "2024-01-02",
        "end": "2024-01-03",
        "row_count": 2,
    }
    assert MarketDataQualitySnapshot.from_dict(first.to_dict()) == first


def test_negative_qfq_prices_and_ohlc_relationships_are_fatal() -> None:
    frame = _frame()
    frame.loc[pd.Timestamp("2024-01-02"), ("000001", "open")] = -10.0
    frame.loc[pd.Timestamp("2024-01-03"), ("000001", "high")] = 9.5

    snapshot = _audit(frame)

    assert snapshot.is_clean is False
    assert "non_positive_ohlc" in snapshot.fatal_codes
    assert "ohlc_relationship_errors" in snapshot.fatal_codes
    assert snapshot.payload["statistics"]["non_positive_ohlc"]["count"] == 1
    assert (
        snapshot.payload["statistics"]["ohlc_relationship_errors"]["count"]
        == 2
    )


def test_nan_is_warning_but_inf_and_negative_volume_are_fatal() -> None:
    frame = _frame()
    frame.loc[pd.Timestamp("2024-01-02"), ("000001", "close")] = np.nan
    frame.loc[pd.Timestamp("2024-01-03"), ("000001", "open")] = np.inf
    frame.loc[pd.Timestamp("2024-01-03"), ("000001", "volume")] = -1.0

    snapshot = _audit(frame)

    assert {
        item["code"] for item in snapshot.payload["warnings"]
    } == {"missing_values_present"}
    assert "non_finite_values" in snapshot.fatal_codes
    assert "negative_volume" in snapshot.fatal_codes
    assert (
        snapshot.payload["statistics"]["missing_values"]["classification"]
        == "not_classified_by_listing_state"
    )
    assert any(
        "listing" in limitation.lower()
        for limitation in snapshot.payload["limitations"]
    )


def test_duplicate_and_unsorted_dates_are_fatal() -> None:
    duplicate = _frame(dates=["2024-01-02", "2024-01-02"])
    duplicated = _audit(duplicate)
    assert "duplicate_dates" in duplicated.fatal_codes

    unsorted = _frame(dates=["2024-01-03", "2024-01-02"])
    unordered = _audit(unsorted)
    assert "unsorted_dates" in unordered.fatal_codes


def test_future_values_are_not_scanned() -> None:
    frame = _frame(dates=["2024-01-03", "2024-01-04"])
    frame.loc[pd.Timestamp("2024-01-04"), ("000001", "open")] = -999.0

    snapshot = _audit(frame)

    assert "future_rows_present" in snapshot.fatal_codes
    assert "non_positive_ohlc" not in snapshot.fatal_codes
    assert snapshot.payload["date_range"]["end"] == "2024-01-03"
    assert snapshot.payload["axes"]["future_row_count"] == 1


def test_missing_field_and_code_coverage_are_fatal() -> None:
    frame = _frame().drop(columns=[("000001", "volume")])
    frame[("000002", "open")] = [20.0, 20.5]

    snapshot = _audit(frame)

    assert "missing_required_fields" in snapshot.fatal_codes
    assert "incomplete_code_field_coverage" in snapshot.fatal_codes
    assert snapshot.payload["coverage"][
        "codes_missing_required_fields_count"
    ] == 2


def test_quality_evidence_tampering_fails_closed() -> None:
    payload = _audit(_frame()).to_dict()
    tampered = deepcopy(payload)
    tampered["fatal"] = [
        {"code": "non_positive_ohlc", "count": 1}
    ]

    with pytest.raises(
        MarketDataQualityError,
        match="hash verification failed",
    ):
        MarketDataQualitySnapshot.from_dict(tampered)


def test_rehashed_internally_inconsistent_evidence_fails_closed() -> None:
    payload = _audit(_frame()).to_dict()
    payload["fatal"] = [
        {"code": "non_positive_ohlc", "count": 1}
    ]
    payload["is_clean"] = False
    unsigned = dict(payload)
    unsigned.pop("content_sha256")
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(
        MarketDataQualityError,
        match="conflicts with audited statistics",
    ):
        MarketDataQualitySnapshot.from_dict(payload)
