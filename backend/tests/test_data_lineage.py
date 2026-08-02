from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from backend.data.lineage import (
    COUNT_MISMATCH,
    DUPLICATE_CODES,
    EMPTY_CODES,
    MISSING_INDUSTRY_MAPPING,
    NON_POINT_IN_TIME,
    SURVIVORSHIP_BIAS,
    DataQualityReport,
    UniverseSnapshot,
    build_data_quality_report,
    build_universe_snapshot,
)
from backend.data.versioning import (
    DatasetVersion,
    compute_data_version,
    compute_dataset_version,
    version_matches,
)


def _frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(
        ["2024-01-02", "2024-01-03"],
        name="trade_date",
    )
    columns = pd.MultiIndex.from_tuples(
        [("000001", "close"), ("000002", "close")],
        names=["code", "field"],
    )
    return pd.DataFrame(
        [[10.0, np.nan], [10.5, 20.0]],
        index=index,
        columns=columns,
    )


def test_dataset_version_is_schema_axis_dtype_and_context_sensitive() -> None:
    base = _frame()
    context = {
        "source": "akshare",
        "source_config": {"endpoint": "eastmoney", "retries": 3},
        "adjustment": "qfq",
    }
    original = compute_dataset_version(base, context)

    reordered = base.loc[:, list(reversed(base.columns))]
    assert compute_dataset_version(reordered, context).digest != original.digest

    renamed_index = base.rename_axis("date")
    assert compute_dataset_version(renamed_index, context).digest != original.digest

    moved_index = base.copy()
    moved_index.index = moved_index.index + pd.Timedelta(days=1)
    assert compute_dataset_version(moved_index, context).digest != original.digest

    float32 = base.astype("float32")
    assert compute_dataset_version(float32, context).digest != original.digest

    changed_adjustment = {**context, "adjustment": "hfq"}
    assert (
        compute_dataset_version(base, changed_adjustment).digest
        != original.digest
    )

    changed_source = {**context, "source_config": {"endpoint": "sina", "retries": 3}}
    assert compute_dataset_version(base, changed_source).digest != original.digest


def test_dataset_version_tracks_nan_positions_not_only_non_nan_values() -> None:
    first = pd.DataFrame(
        [[1.0, np.nan], [np.nan, 2.0]],
        columns=["a", "b"],
    )
    second = pd.DataFrame(
        [[np.nan, 1.0], [2.0, np.nan]],
        columns=["a", "b"],
    )
    first_values = first.to_numpy().ravel()
    second_values = second.to_numpy().ravel()
    np.testing.assert_array_equal(
        np.sort(first_values[~np.isnan(first_values)]),
        np.sort(second_values[~np.isnan(second_values)]),
    )
    assert np.isnan(first_values).sum() == np.isnan(second_values).sum()
    assert compute_data_version(first) != compute_data_version(second)


def test_dataset_context_mapping_order_is_canonical() -> None:
    frame = _frame()
    left = {
        "source": "akshare",
        "config": {"adjust": "qfq", "timeout": 30},
    }
    right = {
        "config": {"timeout": 30, "adjust": "qfq"},
        "source": "akshare",
    }
    assert compute_data_version(frame, left) == compute_data_version(frame, right)


def test_dataset_version_is_stable_in_a_fresh_process() -> None:
    frame = _frame()
    expected = compute_data_version(
        frame,
        {"source": "akshare", "adjustment": "qfq"},
    )
    project_root = Path(__file__).resolve().parents[2]
    script = """
import numpy as np
import pandas as pd
from backend.data.versioning import compute_data_version
index = pd.DatetimeIndex(["2024-01-02", "2024-01-03"], name="trade_date")
columns = pd.MultiIndex.from_tuples(
    [("000001", "close"), ("000002", "close")],
    names=["code", "field"],
)
frame = pd.DataFrame(
    [[10.0, np.nan], [10.5, 20.0]],
    index=index,
    columns=columns,
)
print(compute_data_version(
    frame,
    {"source": "akshare", "adjustment": "qfq"},
))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == expected


def test_dataset_version_serialization_and_legacy_string_call() -> None:
    structured = compute_dataset_version(_frame(), {"adjustment": "qfq"})
    restored = DatasetVersion.from_dict(
        json.loads(json.dumps(structured.to_dict()))
    )

    assert restored == structured
    assert compute_data_version(_frame(), {"adjustment": "qfq"}) == str(structured)
    assert version_matches(restored, structured)
    assert str(structured).startswith("dv2|r2|c2|")

    tampered = structured.to_dict()
    tampered["rows"] = 99
    with pytest.raises(ValueError, match="does not match"):
        DatasetVersion.from_dict(tampered)


def test_empty_dataset_and_unstable_context_are_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        compute_dataset_version(pd.DataFrame())
    with pytest.raises(TypeError, match="Unsupported value"):
        compute_dataset_version(_frame(), {"source": object()})


def test_quality_report_captures_counts_duplicates_and_industry_gaps() -> None:
    report = build_data_quality_report(
        ["000001", "000002", "000001", "", None],
        expected_count=3,
        industry_map={"000001": "银行"},
    )

    assert report.requested_count == 5
    assert report.valid_requested_count == 3
    assert report.unique_count == 2
    assert report.duplicate_count == 1
    assert report.duplicate_codes == ("000001",)
    assert report.empty_code_count == 2
    assert report.count_difference == -1
    assert report.industry_mapped_count == 1
    assert report.missing_industry_codes == ("000002",)
    assert set(report.issue_codes) == {
        COUNT_MISMATCH,
        DUPLICATE_CODES,
        EMPTY_CODES,
        MISSING_INDUSTRY_MAPPING,
    }
    assert not report.is_clean


def test_current_constituents_for_history_raise_survivorship_risks() -> None:
    snapshot = build_universe_snapshot(
        "csi300",
        ["000002", "000001", "000001"],
        requested_as_of="2020-01-31",
        source_as_of="2026-07-28",
        point_in_time=False,
        industry_map={"000001": "银行", "000002": "电子"},
    )

    assert snapshot.codes == ("000001", "000002")
    assert snapshot.requested_count == 3
    assert snapshot.unique_count == 2
    assert NON_POINT_IN_TIME in snapshot.risk_warnings
    assert SURVIVORSHIP_BIAS in snapshot.risk_warnings
    assert DUPLICATE_CODES in snapshot.risk_warnings
    assert snapshot.snapshot_hash


def test_point_in_time_snapshot_rejects_future_source_date() -> None:
    with pytest.raises(ValueError, match="source_as_of"):
        build_universe_snapshot(
            "csi300",
            ["000001"],
            requested_as_of="2020-01-31",
            source_as_of="2026-07-28",
            point_in_time=True,
        )


def test_universe_snapshot_hash_is_canonical_and_tamper_evident() -> None:
    first = build_universe_snapshot(
        "custom",
        ["000002", "000001"],
        requested_as_of="2024-01-31",
        source_as_of="2024-01-31",
        point_in_time=True,
        timeline_identity={"timeline_hash": "a" * 64},
    )
    second = build_universe_snapshot(
        "custom",
        ["000001", "000002"],
        requested_as_of=pd.Timestamp("2024-01-31"),
        source_as_of=pd.Timestamp("2024-01-31"),
        point_in_time=True,
        timeline_identity={"timeline_hash": "a" * 64},
    )
    assert first.snapshot_hash == second.snapshot_hash

    restored = UniverseSnapshot.from_dict(
        json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
    )
    assert restored == first

    tampered = first.to_dict()
    tampered["codes"] = ["999999"]
    with pytest.raises(ValueError, match="verification"):
        UniverseSnapshot.from_dict(tampered)

    with pytest.raises(FrozenInstanceError):
        first.pool_id = "other"  # type: ignore[misc]


def test_quality_report_deserializes_older_minimal_payload() -> None:
    report = DataQualityReport.from_dict(
        {
            "requested_count": 2,
            "unique_count": 2,
            "duplicate_count": 0,
            "duplicate_codes": [],
        }
    )
    assert report.valid_requested_count == 2
    assert report.missing_industry_count == 0
    assert report.is_clean
