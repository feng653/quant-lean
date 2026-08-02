from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from backend.core.types import SignalItem
from backend.data.generation_manifest import GenerationManifestStore
from backend.research.validation_matrix import (
    STRATEGY_CONTRACTS,
    _canonical_signals,
    _default_params,
    audit_market_frame,
    build_validation_matrix,
    render_markdown,
    validate_strategy_on_dataset,
)
from backend.strategies.registry import StrategyRegistry


def _market_frame(*, periods: int = 380, codes: int = 6) -> pd.DataFrame:
    dates = pd.bdate_range("2022-01-03", periods=periods, name="date")
    rng = np.random.default_rng(20260728)
    values: dict[tuple[str, str], np.ndarray] = {}
    for position in range(codes):
        code = f"{position + 1:06d}.SZ"
        returns = rng.normal(0.0002 + position * 0.00002, 0.006, periods)
        close = (15.0 + position) * np.cumprod(1.0 + returns)
        open_ = close * (1.0 + rng.normal(0.0, 0.001, periods))
        high = np.maximum(open_, close) * 1.004
        low = np.minimum(open_, close) * 0.996
        volume = rng.integers(100_000, 2_000_000, periods).astype(float)
        values[(code, "open")] = open_
        values[(code, "high")] = high
        values[(code, "low")] = low
        values[(code, "close")] = close
        values[(code, "volume")] = volume
        values[(code, "amount")] = volume * close
    frame = pd.DataFrame(values, index=dates)
    frame.columns = pd.MultiIndex.from_tuples(
        frame.columns,
        names=["code", "field"],
    )
    return frame


def _write_cache(
    root: Path,
    frame: pd.DataFrame,
    *,
    pool_id: str = "custom",
    expected_codes: int | None = None,
) -> None:
    daily = root / "daily"
    daily.mkdir(parents=True)
    pivot = daily / f".{pool_id}.pivot.staged"
    metadata_path = daily / f".{pool_id}.metadata.staged"
    frame.to_parquet(pivot)
    metadata = {
        "date_start": str(frame.index.min().date()),
        "date_end": str(frame.index.max().date()),
        "source_kind": "cached_real",
    }
    metadata_path.write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    GenerationManifestStore(
        daily,
        required_artifacts={"pivot", "metadata"},
    ).publish_staged(
        pool_id,
        {"pivot": pivot, "metadata": metadata_path},
    )
    (root / f"pool_{pool_id}.json").write_text(
        json.dumps(
            {
                "count": expected_codes
                if expected_codes is not None
                else len(frame.columns.get_level_values(0).unique())
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def synthetic_matrix(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("research-matrix-cache")
    _write_cache(root, _market_frame())
    original_connect = socket.create_connection

    def reject_network(*args, **kwargs):
        raise AssertionError("validation matrix must remain offline")

    socket.create_connection = reject_network
    try:
        return build_validation_matrix(
            root,
            pool_ids=["custom"],
            source_kind="synthetic",
            point_in_time=True,
            max_rows=360,
            max_codes=6,
        )
    finally:
        socket.create_connection = original_connect


def test_registry_matrix_covers_all_strategies_without_promoting_synthetic(
    synthetic_matrix: dict,
) -> None:
    assert synthetic_matrix["offline_read_only"] is True
    assert synthetic_matrix["strategy_count"] == 22
    assert synthetic_matrix["matrix_row_count"] == 22
    assert synthetic_matrix["contract_coverage"] == {
        "covered": 22,
        "missing_strategy_ids": [],
        "orphan_contract_ids": [],
    }
    assert len(STRATEGY_CONTRACTS) == 22
    assert {
        row["readiness"] for row in synthetic_matrix["rows"]
    } <= {"blocked", "synthetic_only"}
    assert not any(
        row["deployable_research"] for row in synthetic_matrix["rows"]
    )
    assert all(
        "source_is_explicitly_synthetic" in row["readiness_reasons"]
        for row in synthetic_matrix["rows"]
    )
    json.dumps(synthetic_matrix, allow_nan=False)


def test_strategy_rows_include_t_plus_one_and_no_future_evidence(
    synthetic_matrix: dict,
) -> None:
    non_training = next(
        row
        for row in synthetic_matrix["rows"]
        if row["strategy_id"] == "short_reversal_v1"
    )
    assert non_training["signal_checks"]["t_plus_one_checked"] is True
    assert "cached session" in non_training["signal_checks"][
        "t_plus_one_evidence"
    ]
    assert non_training["no_future_status"] in {"passed", "failed"}
    assert "future_mutation=" in non_training["no_future_evidence"]
    assert "as_of_truncation=" in non_training["no_future_evidence"]

    training = next(
        row
        for row in synthetic_matrix["rows"]
        if row["strategy_id"] == "alpha158_xgb_v1"
    )
    assert training["signal_checks"]["t_plus_one_checked"] is False
    assert training["signal_checks"]["t_plus_one_evidence"].startswith(
        "not_applicable:"
    )
    assert training["no_future_status"] in {"passed", "failed"}
    assert "validation_window=" in training["no_future_evidence"]


def test_all_registered_rule_strategies_are_as_of_truncation_invariant() -> None:
    frame = _market_frame()
    dates = pd.DatetimeIndex(frame.index)
    start_date = dates[-180]
    decision_date = dates[-40]
    registry = StrategyRegistry()
    registry.scan_directory(Path("backend/strategies"))

    failures: list[str] = []
    for metadata in sorted(
        registry.list_all(),
        key=lambda item: item.strategy_id,
    ):
        if metadata.requires_training:
            continue
        params = _default_params(metadata)
        full = registry.create_strategy(
            metadata.strategy_id
        ).generate_batch_signals(
            frame,
            params,
            str(start_date.date()),
            str(decision_date.date()),
        )
        truncated = registry.create_strategy(
            metadata.strategy_id
        ).generate_batch_signals(
            frame.loc[:decision_date],
            params,
            str(start_date.date()),
            str(decision_date.date()),
        )
        if _canonical_signals(
            full,
            through=decision_date,
        ) != _canonical_signals(
            truncated,
            through=decision_date,
        ):
            failures.append(metadata.strategy_id)
    assert failures == []


def test_original_no_future_blockers_pass_both_invariants(
    synthetic_matrix: dict,
) -> None:
    targets = {
        "macd_signal_v1",
        "composite_equal_v1",
        "composite_momentum_v1",
        "composite_regime_v1",
        "composite_riskparity_v1",
    }
    rows = {
        row["strategy_id"]: row
        for row in synthetic_matrix["rows"]
        if row["strategy_id"] in targets
    }
    assert set(rows) == targets
    assert all(row["no_future_status"] == "passed" for row in rows.values())
    assert all(
        "future_mutation=passed" in row["no_future_evidence"]
        and "as_of_truncation=passed" in row["no_future_evidence"]
        for row in rows.values()
    )


def test_validator_remains_fail_closed_for_future_dependent_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _market_frame()
    audit = audit_market_frame(
        frame,
        pool_id="custom",
        metadata={"source_kind": "cached_real"},
        membership={"count": 6},
        source_kind="cached_real",
        point_in_time=True,
    )
    registry = StrategyRegistry()
    registry.scan_directory(Path("backend/strategies"))

    class FutureDependentStrategy:
        def validate_params(self, params: dict) -> tuple[bool, str]:
            return True, ""

        def generate_batch_signals(
            self,
            pivot: pd.DataFrame,
            params: dict,
            start_date: str,
            end_date: str,
        ) -> dict[str, list[SignalItem]]:
            del params, end_date
            code = str(pivot.columns[0][0])
            future_value = abs(float(pivot.iloc[-1, 0]))
            score = min(1.0, future_value / 1_000.0)
            return {
                start_date: [
                    SignalItem(code, "BUY", score=score, weight=score)
                ]
            }

    monkeypatch.setattr(
        registry,
        "create_strategy",
        lambda strategy_id: FutureDependentStrategy(),
    )
    row = validate_strategy_on_dataset(
        registry,
        "short_reversal_v1",
        frame,
        audit,
        max_rows=360,
        max_codes=6,
    )
    assert row["no_future_status"] == "failed"
    assert row["readiness"] == "blocked"
    assert row["deployable_research"] is False
    assert "future_mutation=failed" in row["no_future_evidence"]
    assert "as_of_truncation=failed" in row["no_future_evidence"]


def test_non_pit_index_cache_is_never_validated_or_deployable() -> None:
    frame = _market_frame()
    audit = audit_market_frame(
        frame,
        pool_id="csi500",
        metadata={"source_kind": "cached_real"},
        membership={"count": 500},
        source_kind="cached_real",
        point_in_time=False,
    )
    registry = StrategyRegistry()
    registry.scan_directory(Path("backend/strategies"))
    row = validate_strategy_on_dataset(
        registry,
        "short_reversal_v1",
        frame,
        audit,
        max_rows=360,
        max_codes=6,
    )
    assert row["readiness"] != "cached_real_validated"
    assert row["deployable_research"] is False
    assert "dataset_risk:non_point_in_time" in row["readiness_reasons"]
    assert "dataset_risk:survivorship_bias" in row["readiness_reasons"]


def test_cached_real_point_in_time_assertion_is_not_trusted() -> None:
    frame = _market_frame()
    audit = audit_market_frame(
        frame,
        pool_id="csi300",
        metadata={
            "source_kind": "cached_real",
            "point_in_time": True,
        },
        membership={"count": 6},
        source_kind="cached_real",
        point_in_time=True,
    )
    assert audit["point_in_time"] is False
    assert {"non_point_in_time", "survivorship_bias"} <= set(
        audit["risk_warnings"]
    )
    assert "unverified_point_in_time_claim" in {
        item["code"] for item in audit["warnings"]
    }


def test_market_quality_detects_structural_and_value_failures() -> None:
    base = _market_frame(periods=20, codes=2)
    broken = pd.concat([base.iloc[[0]], base.iloc[::-1]])
    broken.iloc[0, 0] = np.nan
    broken.iloc[1, broken.columns.get_loc(("000001.SZ", "close"))] = -1.0
    broken.iloc[2, broken.columns.get_loc(("000001.SZ", "volume"))] = -1.0
    broken.iloc[3, broken.columns.get_loc(("000001.SZ", "high"))] = 0.1
    broken = pd.concat([broken, broken.iloc[:, [0]]], axis=1)

    audit = audit_market_frame(
        broken,
        pool_id="custom",
        membership={"count": 3},
        source_kind="synthetic",
        point_in_time=True,
    )
    codes = {item["code"] for item in audit["issues"]}
    assert {
        "duplicate_dates",
        "duplicate_columns",
        "non_monotonic_dates",
        "nan_values",
        "negative_prices",
        "negative_volume_or_amount",
        "ohlcv_logic",
        "count_mismatch",
    } <= codes
    assert audit["quality_passed"] is False


def test_missing_cache_builds_complete_blocked_matrix(tmp_path: Path) -> None:
    report = build_validation_matrix(tmp_path, pool_ids=["missing"])
    assert report["matrix_row_count"] == 22
    assert {row["readiness"] for row in report["rows"]} == {"blocked"}
    assert all("cache_unavailable" in row["reasons"] for row in report["rows"])


def test_markdown_is_portable_and_exposes_deployment_evidence(
    synthetic_matrix: dict,
    tmp_path: Path,
) -> None:
    markdown = render_markdown(synthetic_matrix)
    assert "T+1 evidence" in markdown
    assert "No-future evidence" in markdown
    assert "Deployable" in markdown
    assert str(tmp_path) not in markdown


def test_cli_writes_only_requested_reports_from_missing_cache(
    tmp_path: Path,
) -> None:
    output = tmp_path / "reports" / "matrix.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/validate_research_matrix.py",
            "--cache-dir",
            str(tmp_path / "missing-cache"),
            "--pool",
            "missing",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    assert output.is_file()
    assert output.with_suffix(".md").is_file()
    assert str(tmp_path) not in completed.stdout
    assert sorted(
        path.name for path in output.parent.iterdir()
    ) == ["matrix.json", "matrix.md"]
