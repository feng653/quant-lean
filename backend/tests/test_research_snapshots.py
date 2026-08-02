from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.data.research_snapshots import (
    ResearchSnapshotStore,
    SnapshotIntegrityError,
    clip_to_test_end,
)
from backend.data.versioning import compute_dataset_version


def _pivot() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=8, name="date")
    columns = pd.MultiIndex.from_product(
        [["000001", "000002"], ["open", "close"]],
        names=["code", "field"],
    )
    return pd.DataFrame(
        np.arange(len(dates) * len(columns), dtype=float).reshape(
            len(dates),
            len(columns),
        ),
        index=dates,
        columns=columns,
    )


def test_snapshot_is_content_addressed_idempotent_and_portable(
    tmp_path: Path,
) -> None:
    store = ResearchSnapshotStore(tmp_path / "snapshots")
    pivot = _pivot()

    first = store.save_pivot(pivot)
    second = store.save_pivot(pivot.copy())

    assert first == second
    assert first["key"] == first["file_sha256"]
    assert first["relative_key"] == (
        f"pivot/{first['key']}.parquet"
    )
    assert "\\" not in first["relative_key"]
    assert str(tmp_path) not in str(first)
    pd.testing.assert_frame_equal(
        store.load_pivot(first),
        pivot,
        check_freq=False,
    )
    assert len(list((tmp_path / "snapshots" / "pivot").glob("*.parquet"))) == 1


def test_concurrent_snapshot_writes_are_write_once(
    tmp_path: Path,
) -> None:
    store = ResearchSnapshotStore(tmp_path / "snapshots")
    pivot = _pivot()

    with ThreadPoolExecutor(max_workers=8) as executor:
        evidence = list(executor.map(lambda _: store.save_pivot(pivot), range(16)))

    assert all(item == evidence[0] for item in evidence)
    assert len(list((tmp_path / "snapshots" / "pivot").glob("*.parquet"))) == 1
    pd.testing.assert_frame_equal(
        store.load_pivot(evidence[0]),
        pivot,
        check_freq=False,
    )


def test_existing_corrupt_snapshot_is_never_overwritten(
    tmp_path: Path,
) -> None:
    store = ResearchSnapshotStore(tmp_path / "snapshots")
    pivot = _pivot()
    evidence = store.save_pivot(pivot)
    path = tmp_path / "snapshots" / evidence["relative_key"]
    path.write_bytes(b"tampered")

    with pytest.raises(SnapshotIntegrityError, match="size changed"):
        store.load_pivot(evidence)
    with pytest.raises(SnapshotIntegrityError, match="size changed"):
        store.save_pivot(pivot)
    assert path.read_bytes() == b"tampered"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "../" + "a" * 61),
        ("relative_key", "../outside.parquet"),
        ("file_sha256", "A" * 64),
        ("kind", "other"),
    ],
)
def test_snapshot_evidence_rejects_traversal_and_non_sha_keys(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store = ResearchSnapshotStore(tmp_path / "snapshots")
    evidence = store.save_pivot(_pivot())
    changed = deepcopy(evidence)
    changed[field] = value

    with pytest.raises(SnapshotIntegrityError):
        store.load_pivot(changed)


def test_benchmark_snapshot_round_trip_and_tamper_detection(
    tmp_path: Path,
) -> None:
    store = ResearchSnapshotStore(tmp_path / "snapshots")
    benchmark = pd.Series(
        [3100.5, 3112.0, 3098.25],
        index=pd.bdate_range("2024-01-02", periods=3, name="date"),
        name="close",
    )
    evidence = store.save_benchmark(benchmark)

    pd.testing.assert_series_equal(
        store.load_benchmark(evidence),
        benchmark,
        check_freq=False,
    )
    changed = deepcopy(evidence)
    changed["series"]["dtype"] = "int64"
    with pytest.raises(SnapshotIntegrityError, match="dtype changed"):
        store.load_benchmark(changed)


def test_benchmark_snapshot_normalizes_second_resolution_index(
    tmp_path: Path,
) -> None:
    store = ResearchSnapshotStore(tmp_path / "snapshots")
    index = pd.date_range("2025-01-01", periods=3, name="date").as_unit("s")
    benchmark = pd.Series([1.0, 1.1, 1.2], index=index, name="close")

    evidence = store.save_benchmark(benchmark)
    restored = store.load_benchmark(evidence)

    pd.testing.assert_series_equal(
        restored,
        benchmark.set_axis(index.as_unit("ms")),
        check_freq=False,
    )


def test_future_cache_rows_do_not_enter_research_input_or_hash() -> None:
    pivot = _pivot()
    test_end = pivot.index[4]
    selected = clip_to_test_end(pivot, test_end)
    extended = pd.concat(
        [
            pivot,
            pivot.iloc[-2:].set_axis(
                pd.bdate_range(
                    pivot.index[-1],
                    periods=3,
                    name=pivot.index.name,
                )[1:],
                axis=0,
            ),
        ]
    )

    clipped_extended = clip_to_test_end(extended, test_end)
    pd.testing.assert_frame_equal(selected, clipped_extended)
    assert selected.index.max() == test_end
    assert compute_dataset_version(selected) == compute_dataset_version(
        clipped_extended
    )
    assert selected.index.min() == pivot.index.min()
