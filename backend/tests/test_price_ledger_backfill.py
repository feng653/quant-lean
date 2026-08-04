from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
import pytest

from backend.data.point_in_time_master import (
    IMPORT_SCHEMA_VERSION as PIT_IMPORT_SCHEMA_VERSION,
    PointInTimeMasterStore,
    _authorize_governed_import,
    _digest as pit_digest,
)
from backend.data.point_in_time_universe import resolve_point_in_time_universe
from backend.data.cache_readiness import inspect_cached_market_data
from backend.data.price_ledger import PriceLedgerStore
from backend.data.price_ledger_backfill import (
    BACKFILL_PLAN_SCHEMA,
    BackfillBudget,
    BackfillPlan,
    PriceLedgerBackfillError,
    PriceLedgerBackfillService,
)
from backend.data.sources.baostock_source import BaoStockLedgerFetchResult
from backend.data.source_validation import build_daily_fetch_evidence


DATES = ("2024-01-02", "2024-01-03", "2024-01-04")
CODES = ("000001", "600000")


def _import_membership(
    store: PointInTimeMasterStore,
    scope_id: str,
) -> None:
    del scope_id
    package_id = "pitpkg_" + "a" * 32
    package_sha256 = "b" * 64
    receipts = []
    for governed_scope in ("csi300", "csi500", "csi800", "csi1000"):
        document = {
            "schema_version": PIT_IMPORT_SCHEMA_VERSION,
            "domain": "index_membership",
            "scope_id": governed_scope,
            "evidence_kind": "effective_dated_history",
            "coverage_from": "2024-01-01",
            "coverage_to": "2024-12-31",
            "source": {
                "provider": "csindex_official",
                "dataset": "effective-membership-test-fixture",
                "version": "2024-v1",
                "evidence_level": "index_provider_authoritative",
                "retrieved_at": "2025-01-02T00:00:00Z",
                "content_sha256": (
                    f"{('a' if governed_scope == 'csi300' else 'b') * 63}"
                    f"{len(receipts)}"
                ),
            },
            "records": [
                {
                    "security_code": code,
                    "effective_from": "2024-01-01",
                    "effective_to": "2024-12-31",
                    "member_name": code,
                }
                for code in CODES
            ],
        }
        result = store.import_batch(
            **document,
            imported_by_user_id=1,
            _governed_authorization=_authorize_governed_import(
                package_id=package_id,
                package_sha256=package_sha256,
                document_sha256=pit_digest(document),
            ),
        )
        receipts.append(
            {
                "scope_id": governed_scope,
                "batch_id": result["batch_id"],
                "batch_digest": result["batch_digest"],
            }
        )
    store.activate_governed_csi_package(
        package_id=package_id,
        package_sha256=package_sha256,
        receipts=receipts,
    )


def _plan(store: PointInTimeMasterStore) -> BackfillPlan:
    timelines = [
        resolve_point_in_time_universe(
            store,
            pool_id=scope,
            trading_dates=DATES,
            expected_count=2,
        ).identity()
        for scope in ("csi300", "csi800")
    ]
    return BackfillPlan.from_mapping(
        {
            "schema_version": BACKFILL_PLAN_SCHEMA,
            "timelines": timelines,
            "trading_dates": list(DATES),
            "source_version": "baostock-fixture-20240104",
        }
    )


def test_checkpoint_write_is_portable_without_fchmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.data import price_ledger_backfill

    monkeypatch.delattr(price_ledger_backfill.os, "fchmod", raising=False)
    path = tmp_path / "checkpoint.json"

    price_ledger_backfill._atomic_write_json(path, {"status": "portable"})

    assert path.read_text(encoding="utf-8") == '{"status":"portable"}\n'


class FakeBaoStockSource:
    def __init__(self, *, omit_status: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.omit_status = omit_status

    async def fetch_ledger_daily_result(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        progress=None,
    ) -> BaoStockLedgerFetchResult:
        del progress
        normalized = tuple(sorted(codes))
        self.calls.append(normalized)
        frames: list[pd.DataFrame] = []
        statuses: list[dict[str, str]] = []
        for code in normalized:
            traded_dates = [DATES[0], DATES[2]]
            values = pd.DataFrame(
                {
                    (code, "open"): [10.0, 9.0],
                    (code, "high"): [10.5, 9.7],
                    (code, "low"): [9.8, 8.9],
                    (code, "close"): [10.0, 9.5],
                    (code, "preclose"): [9.9, 9.0],
                    (code, "volume"): [1000.0, 1200.0],
                    (code, "amount"): [10000.0, 11400.0],
                },
                index=pd.DatetimeIndex(traded_dates, name="date"),
            )
            values.columns = pd.MultiIndex.from_tuples(
                values.columns,
                names=["code", "field"],
            )
            frames.append(values)
            statuses.extend(
                [
                    {
                        "security_code": code,
                        "date": DATES[0],
                        "status": "traded",
                    },
                    {
                        "security_code": code,
                        "date": DATES[2],
                        "status": "traded",
                    },
                ]
            )
            if not self.omit_status:
                statuses.append(
                    {
                        "security_code": code,
                        "date": DATES[1],
                        "status": "suspended",
                    }
                )
        frame = pd.concat(frames, axis=1).sort_index(axis=1)
        evidence = build_daily_fetch_evidence(
            frame,
            requested_codes=normalized,
            start=start,
            end=end,
            provider="baostock:official",
            endpoint="baostock.fixture/raw+preclose",
            adjustment="raw",
            evidence_level="public_aggregator",
            transformations=["retain:raw_preclose"],
        )
        return BaoStockLedgerFetchResult(
            frame=frame,
            status_rows=tuple(statuses),
            evidence=evidence,
        )


def test_backfill_resumes_and_reuses_one_canonical_price_set_across_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "experiment.db"
    pit_store = PointInTimeMasterStore(database)
    for scope in ("csi300", "csi800"):
        _import_membership(pit_store, scope)
    plan = _plan(pit_store)
    source = FakeBaoStockSource()
    service = PriceLedgerBackfillService(
        ledger_store=PriceLedgerStore(database),
        pit_store=pit_store,
        source=source,  # type: ignore[arg-type]
    )
    checkpoint = tmp_path / "checkpoint.json"
    budget = BackfillBudget(chunk_size=1, rate_limit_seconds=0)

    with pytest.raises(PriceLedgerBackfillError, match="interrupted"):
        asyncio.run(
            service.run(
                plan,
                checkpoint_path=checkpoint,
                imported_by_user_id=7,
                budget=budget,
                stop_after_chunks=1,
            )
        )
    report = asyncio.run(
        service.run(
            plan,
            checkpoint_path=checkpoint,
            imported_by_user_id=7,
            budget=budget,
        )
    )

    assert source.calls == [("000001",), ("600000",)]
    assert report["status"] == "completed"
    assert len(report["batch_ids"]) == 2
    assert len(report["bindings"]) == 2
    assert all(item["unresolved_gap_count"] == 0 for item in report["gap_reports"])

    store = PriceLedgerStore(database)
    for scope in ("csi300", "csi800"):
        timeline = resolve_point_in_time_universe(
            pit_store,
            pool_id=scope,
            trading_dates=DATES,
            expected_count=2,
        )
        bound = store.load_bound_runtime_prices(
            scope_id=scope,
            timeline_identity=timeline.identity(),
            trading_dates=DATES,
        )
        assert bound is not None
        assert bound.binding["runtime_price_roles_separated"] is True
        assert pd.isna(
            bound.raw_execution.loc[
                pd.Timestamp(DATES[1]),
                ("000001", "open"),
            ]
        )
        assert bound.research_adjusted.loc[
            pd.Timestamp(DATES[2]),
            ("000001", "open"),
        ] == pytest.approx(10.0)
        readiness = store.inspect_bound_runtime_readiness(
            scope_id=scope,
            timeline_identity=timeline.identity(),
            trading_dates=DATES,
        )
        assert readiness["canonical_runtime_price_bound"] is True
        # Explicit BaoStock suspensions are useful gap evidence but remain a
        # low-grade declaration, so they can never unlock an unbiased label.
        assert readiness["trading_status_authoritative"] is False
        assert readiness["ready_for_unbiased_return_research"] is False
        assert readiness["ready_for_unbiased_research"] is False
        assert readiness["ready_for_real_tuning"] is False
        assert (
            "corporate_action_authoritative_evidence_missing"
            in readiness["limitations"]
        )

    import sqlite3

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM price_ledger_prices"
        ).fetchone()[0] == 8
        assert connection.execute(
            "SELECT COUNT(*) FROM price_ledger_runtime_bindings"
        ).fetchone()[0] == 2

    from backend.data.universe import PRESET_POOLS

    monkeypatch.setitem(PRESET_POOLS["csi300"], "expected_count", 2)
    csi300_timeline = resolve_point_in_time_universe(
        pit_store,
        pool_id="csi300",
        trading_dates=DATES,
        expected_count=2,
    )
    csi300_bound = store.load_bound_runtime_prices(
        scope_id="csi300",
        timeline_identity=csi300_timeline.identity(),
        trading_dates=DATES,
    )
    assert csi300_bound is not None

    class Cache:
        async def load_pivot_with_provenance(self, _cache_key: str):
            before = csi300_bound.research_adjusted.iloc[[0]].copy()
            before.index = pd.DatetimeIndex(["2024-01-01"], name="date")
            after = csi300_bound.research_adjusted.iloc[[-1]].copy()
            after.index = pd.DatetimeIndex(["2024-01-05"], name="date")
            wider_legacy_cache = pd.concat(
                [
                    before,
                    csi300_bound.research_adjusted,
                    after,
                ]
            ).sort_index()
            return wider_legacy_cache * 99, {
                "providers": ["legacy"],
                "evidence_levels": ["declared"],
                "adjustments": ["hfq"],
            }

    inspected = asyncio.run(
        inspect_cached_market_data(
            Cache(),  # type: ignore[arg-type]
            cache_key="csi300",
            pool_id="csi300",
            requested_codes=CODES,
            required_start=DATES[0],
            required_end=DATES[-1],
            point_in_time_store=pit_store,
            price_ledger_store=store,
        )
    )
    assert inspected.report["canonical_runtime_price_bound"] is True
    assert inspected.runtime_price_binding is not None
    assert inspected.raw_execution_frame is not None
    pd.testing.assert_frame_equal(
        inspected.frame,
        csi300_bound.research_adjusted,
    )


def test_backfill_fails_closed_on_unclassified_member_gap(
    tmp_path: Path,
) -> None:
    database = tmp_path / "experiment.db"
    pit_store = PointInTimeMasterStore(database)
    for scope in ("csi300", "csi800"):
        _import_membership(pit_store, scope)
    service = PriceLedgerBackfillService(
        ledger_store=PriceLedgerStore(database),
        pit_store=pit_store,
        source=FakeBaoStockSource(omit_status=True),  # type: ignore[arg-type]
    )

    with pytest.raises(PriceLedgerBackfillError, match="unresolved PIT gaps"):
        asyncio.run(
            service.run(
                _plan(pit_store),
                checkpoint_path=tmp_path / "blocked.json",
                imported_by_user_id=7,
                budget=BackfillBudget(
                    chunk_size=2,
                    rate_limit_seconds=0,
                ),
            )
        )

    import sqlite3

    with sqlite3.connect(database) as connection:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='price_ledger_batches'
            """
        ).fetchone()
        assert table is None


def test_actual_backfill_is_blocked_when_local_pit_master_is_empty(
    tmp_path: Path,
) -> None:
    populated = PointInTimeMasterStore(tmp_path / "populated.db")
    for scope in ("csi300", "csi800"):
        _import_membership(populated, scope)
    plan = _plan(populated)
    empty = PointInTimeMasterStore(tmp_path / "empty.db")
    service = PriceLedgerBackfillService(
        ledger_store=PriceLedgerStore(empty.path),
        pit_store=empty,
        source=FakeBaoStockSource(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        PriceLedgerBackfillError,
        match="production PIT timeline is not ready",
    ):
        asyncio.run(
            service.run(
                plan,
                checkpoint_path=tmp_path / "unused.json",
                imported_by_user_id=7,
                dry_run=True,
            )
        )


def _research_grade_document(scope_id: str) -> dict:
    """A 2-code x 3-day dual ledger with constant hfq factor (no action
    changes to explain) and research-grade raw/hfq evidence levels."""
    import copy

    rows = []
    for code in CODES:
        for day in DATES:
            rows.append(
                {
                    "security_code": code,
                    "date": day,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.0,
                    "volume": 1000.0,
                }
            )
    research_rows = copy.deepcopy(rows)
    for item in research_rows:
        for field in ("open", "high", "low", "close"):
            item[field] = round(item[field] * 1.05, 6)
    source = {
        "provider": "research-feed",
        "dataset": "cn-equity-daily",
        "version": "2024.01.04",
        "adjustment": "raw",
        "evidence_level": "public_cross_validated",
        "retrieved_at": "2024-01-05T00:00:00Z",
        "content_sha256": "c" * 64,
    }
    research_source = dict(source)
    research_source["adjustment"] = "hfq"
    return {
        "schema_version": "dual-price-ledger-import/v1",
        "scope_id": scope_id,
        "coverage_from": DATES[0],
        "coverage_to": DATES[-1],
        "raw_source": source,
        "research_source": research_source,
        "corporate_action_source": None,
        "raw_prices": rows,
        "research_prices": research_rows,
        "corporate_actions": [],
    }


def test_paper_simulation_readiness_becomes_true_with_bound_research_grade_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.8.4 acceptance: the paper (L2) readiness check can be True with only
    research-grade data once the exact runtime binding exists; real tuning (L3)
    stays hard-locked."""
    from backend.data.universe import PRESET_POOLS
    from backend.data.price_ledger import (
        _authorize_production_release,
        _digest,
    )

    monkeypatch.setitem(PRESET_POOLS["csi300"], "expected_count", 2)
    database = tmp_path / "experiment.db"
    pit_store = PointInTimeMasterStore(database)
    _import_membership(pit_store, "csi300")
    store = PriceLedgerStore(database)
    timeline = resolve_point_in_time_universe(
        pit_store,
        pool_id="csi300",
        trading_dates=DATES,
        expected_count=2,
    )
    document = _research_grade_document("csi300")
    submitted = {
        "schema_version": document["schema_version"],
        "scope_id": document["scope_id"],
        "coverage_from": document["coverage_from"],
        "coverage_to": document["coverage_to"],
        "raw_source": dict(document["raw_source"]),
        "research_source": dict(document["research_source"]),
        "corporate_action_source": None,
        "raw_prices": [dict(item) for item in document["raw_prices"]],
        "research_prices": [
            dict(item) for item in document["research_prices"]
        ],
        "corporate_actions": [],
        "revision": None,
        "supersedes_batch_id": None,
    }
    imported = store.import_batch(
        **document,
        imported_by_user_id=7,
        _production_release_authorization=_authorize_production_release(
            operation="import_batch",
            plan_sha256="a" * 64,
            manifest_sha256="b" * 64,
            document_sha256=_digest(submitted),
        ),
    )
    store.bind_runtime_scope(
        scope_id="csi300",
        timeline_identity=timeline.identity(),
        trading_dates=DATES,
        batch_ids=[imported["batch_id"]],
        status_source={
            "provider": "fixture-declared-status",
            "dataset": "fixture-status",
            "version": "r1",
            "adjustment": "trading_status",
            "evidence_level": "declared",
            "retrieved_at": "2024-01-05T00:00:00Z",
            "content_sha256": "f" * 64,
        },
        suspension_observations=[],
        bound_by_user_id=7,
    )
    bound = store.load_bound_runtime_prices(
        scope_id="csi300",
        timeline_identity=timeline.identity(),
        trading_dates=DATES,
    )
    assert bound is not None

    class Cache:
        async def load_pivot_with_provenance(self, _cache_key: str):
            return bound.research_adjusted, {
                "providers": ["research-feed"],
                "evidence_levels": ["public_cross_validated"],
                "adjustments": ["hfq"],
                "frame_digest": "dv2|fixture|sha256:" + "a" * 64,
                "identity_consistent": True,
                "complete_code_coverage": True,
                "all_batches_cross_validated": True,
                "all_batches_raw_cross_validated": True,
                "all_batches_adjusted_factor_validated": True,
            }

    inspected = asyncio.run(
        inspect_cached_market_data(
            Cache(),  # type: ignore[arg-type]
            cache_key="csi300",
            pool_id="csi300",
            requested_codes=CODES,
            required_start=DATES[0],
            required_end=DATES[-1],
            point_in_time_store=pit_store,
            price_ledger_store=store,
        )
    )
    assert inspected.report["canonical_runtime_price_bound"] is True
    assert (
        inspected.report["price_ledger"]["ready_for_execution_simulation"]
        is True
    )
    assert inspected.report["ready_for_execution_simulation"] is True
    assert inspected.report["ready_for_real_tuning"] is False
    assert (
        inspected.report["price_ledger"]["roles"]["raw_execution"]["trusted"]
        is True
    )
