from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.core.cost_model import CostModel
from backend.core.engine import BacktestEngine
from backend.core.types import SignalItem
from backend.data.point_in_time_master import (
    IMPORT_SCHEMA_VERSION,
    PointInTimeMasterStore,
)
from backend.data.point_in_time_universe import (
    PointInTimeUniverseError,
    eligibility_panel,
    filter_timeline_codes,
    mask_market_data_to_timeline,
    origin_date_label_eligibility,
    require_point_in_time_training_eligibility,
    resolve_point_in_time_universe,
    timeline_from_identity,
    select_market_data_for_timeline,
    validate_signals_against_timeline,
)
from backend.research.factor_catalog import build_factor_panel
from backend.services.factor_research import (
    FactorResearchBody,
    _compute_factor_research,
)


def _import_membership(
    store: PointInTimeMasterStore,
    records: list[dict[str, str]],
    *,
    start: str = "2024-01-01",
    end: str = "2024-12-31",
    source_digest: str = "a" * 64,
) -> None:
    store.import_batch(
        schema_version=IMPORT_SCHEMA_VERSION,
        domain="index_membership",
        scope_id="fixture_index",
        evidence_kind="effective_dated_history",
        coverage_from=start,
        coverage_to=end,
        source={
            "provider": "fixture_index",
            "dataset": "historical_membership",
            "version": "2024",
            "evidence_level": "licensed",
            "retrieved_at": "2025-01-02T00:00:00Z",
            "content_sha256": source_digest,
        },
        records=records,
        imported_by_user_id=1,
    )


def _frame(codes: list[str]) -> pd.DataFrame:
    dates = pd.DatetimeIndex(
        ["2024-06-28", "2024-07-01", "2024-07-02"],
        name="date",
    )
    columns = pd.MultiIndex.from_product(
        [
            codes,
            ["open", "high", "low", "close", "volume", "amount"],
        ],
        names=["code", "field"],
    )
    return pd.DataFrame(10.0, index=dates, columns=columns)


def test_entrant_exit_boundary_masks_all_security_fields(tmp_path: Path) -> None:
    store = PointInTimeMasterStore(tmp_path / "pit.db")
    _import_membership(
        store,
        [
            {
                "security_code": "000001",
                "effective_from": "2024-01-01",
                "effective_to": "2024-06-30",
                "member_name": "exit",
            },
            {
                "security_code": "600000",
                "effective_from": "2024-07-01",
                "effective_to": "2024-12-31",
                "member_name": "entrant",
            },
        ],
    )
    frame = _frame(["000001", "600000", "999999"])

    timeline = resolve_point_in_time_universe(
        store,
        pool_id="fixture_index",
        trading_dates=frame.index,
        expected_count=1,
    )
    masked = mask_market_data_to_timeline(frame, timeline)

    assert timeline.members_on("2024-06-28") == ("000001",)
    assert timeline.members_on("2024-07-01") == ("600000",)
    assert set(masked.columns.get_level_values("code")) == {
        "000001",
        "600000",
    }
    assert masked.loc["2024-06-28", "600000"].isna().all()
    assert masked.loc["2024-07-01", "000001"].isna().all()
    assert masked.loc["2024-07-01", ("600000", "amount")] == 10.0
    replay = timeline_from_identity(
        timeline.identity(),
        trading_dates=frame.index,
    )
    assert replay.timeline_hash == timeline.timeline_hash
    assert replay.members_by_date == timeline.members_by_date


def test_code_filtered_timeline_identity_round_trips_exactly(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    _import_membership(
        store,
        [
            {
                "security_code": "000001",
                "effective_from": "2024-01-01",
                "effective_to": "2024-12-31",
                "member_name": "first",
            },
            {
                "security_code": "600000",
                "effective_from": "2024-01-01",
                "effective_to": "2024-12-31",
                "member_name": "second",
            },
        ],
    )
    dates = pd.DatetimeIndex(["2024-06-28", "2024-07-01"])
    timeline = resolve_point_in_time_universe(
        store,
        pool_id="fixture_index",
        trading_dates=dates,
        expected_count=2,
    )

    filtered = filter_timeline_codes(timeline, ["600000"])
    replay = timeline_from_identity(
        filtered.identity(),
        trading_dates=dates,
    )

    assert filtered.code_filter == ("600000",)
    assert replay.identity() == filtered.identity()


def test_future_membership_change_does_not_change_early_semantic_hash(
    tmp_path: Path,
) -> None:
    early = {
        "security_code": "000001",
        "effective_from": "2024-01-01",
        "effective_to": "2024-06-30",
        "member_name": "same-early-member",
    }
    first = PointInTimeMasterStore(tmp_path / "first.db")
    second = PointInTimeMasterStore(tmp_path / "second.db")
    _import_membership(
        first,
        [
            early,
            {
                "security_code": "600000",
                "effective_from": "2024-07-01",
                "effective_to": "2024-12-31",
                "member_name": "future-a",
            },
        ],
        source_digest="a" * 64,
    )
    _import_membership(
        second,
        [
            early,
            {
                "security_code": "000002",
                "effective_from": "2024-07-01",
                "effective_to": "2024-12-31",
                "member_name": "future-b",
            },
        ],
        source_digest="b" * 64,
    )
    dates = pd.DatetimeIndex(["2024-03-01", "2024-06-28"])

    first_timeline = resolve_point_in_time_universe(
        first,
        pool_id="fixture_index",
        trading_dates=dates,
        expected_count=1,
    )
    second_timeline = resolve_point_in_time_universe(
        second,
        pool_id="fixture_index",
        trading_dates=dates,
        expected_count=1,
    )

    assert first_timeline.timeline_hash == second_timeline.timeline_hash
    assert (
        first_timeline.source_batches[0]["batch_digest"]
        != second_timeline.source_batches[0]["batch_digest"]
    )


def test_forward_label_uses_only_origin_date_membership() -> None:
    dates = pd.bdate_range("2024-01-02", periods=5)
    eligibility = pd.DataFrame(
        {
            "stays": [True, True, True, True, True],
            "exits_before_h2": [True, True, False, False, False],
            "exits_at_endpoint": [True, True, True, False, False],
        },
        index=dates,
    )

    origin = origin_date_label_eligibility(eligibility)

    # A later committee decision cannot remove a sample selected at t. The
    # forward-return builder still requires a finite t+h research price.
    assert bool(origin.loc[dates[0], "exits_before_h2"]) is True
    assert bool(origin.loc[dates[1], "exits_before_h2"]) is True
    assert bool(origin.loc[dates[2], "exits_before_h2"]) is False
    assert bool(origin.loc[dates[0], "exits_at_endpoint"]) is True
    assert bool(origin.loc[dates[-1], "stays"]) is True


def test_factor_horizon_labels_keep_origin_member_that_later_exits() -> None:
    dates = pd.bdate_range("2024-01-02", periods=35, name="date")
    codes = [f"{index:06d}" for index in range(12)]
    columns = pd.MultiIndex.from_product(
        [codes, ["close", "amount"]],
        names=["code", "field"],
    )
    frame = pd.DataFrame(index=dates, columns=columns, dtype=float)
    for rank, code in enumerate(codes, start=1):
        frame[(code, "close")] = [
            (10.0 + rank) * (1.0 + rank / 10_000) ** day
            for day in range(len(dates))
        ]
        frame[(code, "amount")] = 1_000_000.0 + rank
    eligibility = pd.DataFrame(
        True,
        index=dates,
        columns=codes,
        dtype=bool,
    )
    origin_position = 21
    eligibility.loc[dates[origin_position + 2] :, codes[0]] = False
    body = FactorResearchBody(
        factor_id="momentum_20",
        pool_preset="custom",
        pool_custom_codes=codes,
        start=dates[20].strftime("%Y-%m-%d"),
        end=dates[-1].strftime("%Y-%m-%d"),
        horizons=[1, 5],
        primary_horizon=5,
        quantiles=2,
        default_cost_bps=10,
        cost_scenarios_bps=[0, 10],
    )

    result = _compute_factor_research(
        {
            "body": body.model_dump(),
            "research_input": frame,
            "industries": None,
            "market_caps": None,
            "exposure_inputs": {"industry": None, "size": None},
            "eligibility": eligibility,
        }
    )
    origin = dates[origin_position].strftime("%Y-%m-%d")
    one_day = {
        item["date"]: item
        for item in result["ic"]["1"]["series"]
    }[origin]
    five_day = {
        item["date"]: item
        for item in result["ic"]["5"]["series"]
    }[origin]

    assert one_day["sample_count"] == len(codes)
    assert five_day["sample_count"] == len(codes)


def test_empty_or_sparse_membership_fails_closed(tmp_path: Path) -> None:
    store = PointInTimeMasterStore(tmp_path / "pit.db")
    _import_membership(
        store,
        [
            {
                "security_code": "000001",
                "effective_from": "2024-01-01",
                "effective_to": "2024-06-30",
                "member_name": "only",
            },
        ],
    )

    with pytest.raises(PointInTimeUniverseError) as empty:
        resolve_point_in_time_universe(
            store,
            pool_id="fixture_index",
            trading_dates=["2024-06-28", "2024-07-01"],
            expected_count=1,
        )
    assert empty.value.reason == "historical_membership_empty"

    with pytest.raises(PointInTimeUniverseError) as sparse:
        resolve_point_in_time_universe(
            store,
            pool_id="fixture_index",
            trading_dates=["2024-06-28"],
            expected_count=2,
        )
    assert sparse.value.reason == "historical_membership_count_mismatch"


def test_missing_historical_price_columns_and_ineligible_signals_rejected(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "pit.db")
    _import_membership(
        store,
        [
            {
                "security_code": "000001",
                "effective_from": "2024-01-01",
                "effective_to": "2024-06-30",
                "member_name": "exit",
            },
            {
                "security_code": "600000",
                "effective_from": "2024-07-01",
                "effective_to": "2024-12-31",
                "member_name": "entrant",
            },
        ],
    )
    timeline = resolve_point_in_time_universe(
        store,
        pool_id="fixture_index",
        trading_dates=_frame(["000001"]).index,
        expected_count=1,
    )

    with pytest.raises(PointInTimeUniverseError) as missing:
        mask_market_data_to_timeline(_frame(["000001"]), timeline)
    assert missing.value.reason == "membership_price_coverage_missing"

    with pytest.raises(PointInTimeUniverseError) as ineligible:
        validate_signals_against_timeline(
            {
                "2024-07-01": [
                    SimpleNamespace(code="000001", action="BUY")
                ]
            },
            timeline,
        )
    assert ineligible.value.reason == (
        "signal_security_not_point_in_time_eligible"
    )


def test_execution_uses_complete_research_prices_to_exit_removed_holding() -> None:
    dates = pd.DatetimeIndex(
        ["2024-06-27", "2024-06-28", "2024-07-01"],
        name="date",
    )
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    pivot = pd.DataFrame(10.0, index=dates, columns=columns)
    pivot[("000001", "volume")] = 1_000_000.0
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=CostModel(
            commission_rate=0,
            slippage_rate=0,
            stamp_duty_rate=0,
            min_commission=0,
        ),
        start_date="2024-06-28",
        end_date="2024-07-01",
        max_positions=1,
        eligible_codes_by_date={
            "2024-06-27": {"000001"},
            "2024-06-28": {"000001"},
            "2024-07-01": set(),
        },
    ).run(
        {
            "2024-06-27": [
                SignalItem(
                    code="000001",
                    action="BUY",
                    score=1.0,
                    weight=1.0,
                )
            ]
        },
        pivot,
        strategy_id="fixture",
    )

    assert [(trade.date, trade.action) for trade in result.trade_log] == [
        ("2024-06-28", "BUY"),
        ("2024-07-01", "SELL"),
    ]
    assert result.trade_log[-1].signal_strategy == (
        "point_in_time_universe_exit"
    )
    assert result.final_equity == pytest.approx(100_000)


def test_raw_close_auction_exit_interface_is_reserved_but_fail_closed() -> None:
    with pytest.raises(ValueError) as exc_info:
        BacktestEngine(
            initial_capital=100_000,
            cost_model=CostModel(),
            start_date="2024-06-28",
            end_date="2024-07-01",
            eligible_codes_by_date={
                "2024-06-28": {"000001"},
                "2024-07-01": set(),
            },
            membership_exit_policy="raw_effective_close_auction",
        )
    assert str(exc_info.value) == (
        "raw_close_auction_execution_not_supported"
    )


def test_factor_lookback_is_preserved_but_cross_section_uses_same_eligibility(
    tmp_path: Path,
) -> None:
    dates = pd.bdate_range("2024-01-02", periods=30, name="date")
    transition = dates[-1].strftime("%Y-%m-%d")
    prior = dates[-2].strftime("%Y-%m-%d")
    store = PointInTimeMasterStore(tmp_path / "pit.db")
    _import_membership(
        store,
        [
            {
                "security_code": "000001",
                "effective_from": "2024-01-01",
                "effective_to": prior,
                "member_name": "exit",
            },
            {
                "security_code": "600000",
                "effective_from": transition,
                "effective_to": "2024-12-31",
                "member_name": "entrant",
            },
        ],
    )
    columns = pd.MultiIndex.from_product(
        [
            ["000001", "600000"],
            ["open", "high", "low", "close", "volume"],
        ],
        names=["code", "field"],
    )
    frame = pd.DataFrame(10.0, index=dates, columns=columns)
    frame[("600000", "close")] = range(10, 40)
    frame[("600000", "open")] = frame[("600000", "close")]
    timeline = resolve_point_in_time_universe(
        store,
        pool_id="fixture_index",
        trading_dates=dates,
        expected_count=1,
    )
    feature_tape = select_market_data_for_timeline(frame, timeline)
    raw_factor = build_factor_panel(feature_tape, "momentum_20")
    eligible_factor = raw_factor.where(eligibility_panel(timeline))

    # Entry-day momentum uses observable pre-entry prices, but the entrant is
    # absent from every earlier research cross-section and order set.
    assert pd.notna(raw_factor.loc[dates[-1], "600000"])
    assert pd.notna(eligible_factor.loc[dates[-1], "600000"])
    assert eligible_factor.loc[dates[-2], "600000"] != eligible_factor.loc[
        dates[-2], "600000"
    ]
    validate_signals_against_timeline(
        {
            transition: [
                SignalItem(
                    code="600000",
                    action="BUY",
                    score=1.0,
                )
            ]
        },
        timeline,
    )
    with pytest.raises(PointInTimeUniverseError) as unsupported_training:
        require_point_in_time_training_eligibility(
            trainable=True,
            timeline=timeline,
        )
    assert unsupported_training.value.reason == (
        "ml_point_in_time_label_eligibility_not_supported"
    )
    with pytest.raises(PointInTimeUniverseError) as static_training:
        require_point_in_time_training_eligibility(
            trainable=True,
            timeline=None,
        )
    assert static_training.value.reason == (
        "ml_point_in_time_universe_not_available"
    )
