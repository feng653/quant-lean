from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import price_ledger
from backend.data.price_ledger import (
    PriceLedgerConflictError,
    PriceLedgerIntegrityError,
    PriceLedgerStore,
    PriceLedgerValidationError,
    _authorize_production_release,
    _digest,
    strict_unbiased_readiness,
)
from backend.data.price_cache_audit import audit_legacy_price_caches
from backend.data.cache_readiness import inspect_cached_market_data
from backend.data.point_in_time_universe import (
    PointInTimeUniverseTimeline,
    _timeline_hash,
)
from backend.db.migrate import migrate_experiment
from backend.dependencies import get_current_user

FIXTURE = Path(__file__).parent / "fixtures" / "dual_price_ledger_v1.json"


def _document() -> dict[str, Any]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for field in (
        "raw_source",
        "research_source",
        "corporate_action_source",
    ):
        source = document.get(field)
        if source is not None:
            source["evidence_level"] = "declared"
    return document


def _privileged_document() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _research_grade_document() -> dict[str, Any]:
    """A dual ledger whose raw/hfq/action sources are research-grade only."""
    document = _document()
    for field in (
        "raw_source",
        "research_source",
        "corporate_action_source",
    ):
        document[field]["evidence_level"] = "public_cross_validated"
    return document


def _declared_document() -> dict[str, Any]:
    document = _document()
    document["raw_source"]["evidence_level"] = "declared"
    document["research_source"]["evidence_level"] = "declared"
    document["corporate_action_source"] = None
    document["corporate_actions"] = []
    return document


def _governed_import(
    store: PriceLedgerStore,
    document: dict[str, Any],
) -> dict[str, Any]:
    """Import non-declared (research-grade) evidence with a release capability.

    The store-level governance contract is intentionally unavailable over the
    HTTP API; this helper models the exact-document capability that the
    production materializer would issue after artifact review.
    """
    submitted_document = {
        "schema_version": document["schema_version"],
        "scope_id": document["scope_id"],
        "coverage_from": document["coverage_from"],
        "coverage_to": document["coverage_to"],
        "raw_source": dict(document["raw_source"]),
        "research_source": dict(document["research_source"]),
        "corporate_action_source": (
            dict(document["corporate_action_source"])
            if document.get("corporate_action_source") is not None
            else None
        ),
        "raw_prices": [dict(item) for item in document["raw_prices"]],
        "research_prices": [
            dict(item) for item in document["research_prices"]
        ],
        "corporate_actions": [
            dict(item) for item in document["corporate_actions"]
        ],
        "revision": document.get("revision"),
        "supersedes_batch_id": document.get("supersedes_batch_id"),
    }
    authorization = _authorize_production_release(
        operation="import_batch",
        plan_sha256="a" * 64,
        manifest_sha256="b" * 64,
        document_sha256=_digest(submitted_document),
    )
    return store.import_batch(
        **document,
        imported_by_user_id=7,
        _production_release_authorization=authorization,
    )


def _bound_timeline(
    *,
    pool_id: str = "csi300",
    dates: tuple[str, ...] = ("2024-01-02", "2024-01-03"),
) -> PointInTimeUniverseTimeline:
    """A minimal valid PIT timeline whose identity round-trips verification."""
    members_by_date = tuple(("000001",) for _ in dates)
    timeline = PointInTimeUniverseTimeline(
        pool_id=pool_id,
        dates=dates,
        members_by_date=members_by_date,
        union_codes=("000001",),
        source_batches=(
            {
                "batch_id": "pit_" + "a" * 32,
                "batch_digest": "a" * 64,
                "coverage_from": dates[0],
                "coverage_to": dates[-1],
            },
        ),
        timeline_hash="temporary",
        coverage_from=dates[0],
        coverage_to=dates[-1],
    )
    from dataclasses import replace

    return replace(
        timeline,
        timeline_hash=_timeline_hash(
            pool_id=pool_id,
            dates=dates,
            members_by_date=members_by_date,
        ),
    )


def _bind_runtime(
    store: PriceLedgerStore,
    document: dict[str, Any],
    *,
    timeline: PointInTimeUniverseTimeline,
) -> dict[str, Any]:
    return store.bind_runtime_scope(
        scope_id=timeline.pool_id,
        timeline_identity=timeline.identity(),
        trading_dates=timeline.dates,
        batch_ids=[_governed_import(store, document)["batch_id"]],
        status_source={
            "provider": "fixture-declared-status",
            "dataset": "fixture-status",
            "version": "r1",
            "adjustment": "trading_status",
            "evidence_level": "declared",
            "retrieved_at": "2024-01-04T00:00:00Z",
            "content_sha256": "f" * 64,
        },
        suspension_observations=[],
        bound_by_user_id=7,
    )


def _import(
    store: PriceLedgerStore,
    document: dict[str, Any],
) -> dict[str, Any]:
    return store.import_batch(
        **document,
        imported_by_user_id=7,
    )


def test_fixture_builds_separate_verified_price_roles(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    result = _import(store, _document())

    assert result["idempotent"] is False
    assert result["audit"]["factor_change_count"] == 1
    assert result["audit"]["unexplained_factor_change_count"] == 1
    readiness = store.inspect_readiness(
        scope_id="csi300",
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )
    assert readiness["dual_ledger_complete"] is True
    assert readiness["ready_for_return_research"] is False
    assert readiness["ready_for_adjusted_price_return_research"] is False
    assert readiness["ready_for_unbiased_return_research"] is False
    assert readiness["ready_for_execution_simulation"] is False
    assert readiness["ready_for_real_tuning"] is False
    assert readiness["corporate_action_authoritative"] is False

    raw = store.query_prices(
        scope_id="csi300",
        role="raw_execution",
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )
    research = store.query_prices(
        scope_id="csi300",
        role="research_adjusted",
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )
    assert raw["adjustment"] == "raw"
    assert raw["rows"][1]["open"] == 10.5
    assert research["adjustment"] == "hfq"
    assert research["rows"][1]["open"] == 11.55
    assert all(
        item["price_role"] == "raw_execution" for item in raw["rows"]
    )
    assert all("path" not in json.dumps(item) for item in raw["rows"])


def test_genuine_pre_v2_price_ledger_reads_as_legacy(
    tmp_path: Path,
) -> None:
    """An archived ledger without v2 columns remains readable, not upgraded."""

    path = tmp_path / "legacy-price-ledger.db"
    writer = PriceLedgerStore(path)
    _import(writer, _document())
    with sqlite3.connect(path) as connection:
        for table, columns in {
            "price_ledger_batches": (
                "available_at",
                "ingested_at",
                "revision",
                "supersedes_batch_id",
            ),
            "price_ledger_prices": (
                "effective_at",
                "available_at",
                "ingested_at",
                "revision",
            ),
        }.items():
            for column in columns:
                connection.execute(
                    f"ALTER TABLE {table} DROP COLUMN {column}"
                )

    reader = PriceLedgerStore(path, initialize=False)
    prices = reader.query_prices(
        scope_id="csi300",
        role="raw_execution",
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )
    readiness = reader.inspect_readiness(
        scope_id="csi300",
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )

    assert len(prices["rows"]) == 2
    assert readiness["ledger_available"] is True
    assert readiness["ready_for_unbiased_return_research"] is False
    with sqlite3.connect(path) as connection:
        batch_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(price_ledger_batches)"
            )
        }
    assert "available_at" not in batch_columns


def test_low_grade_daily_status_alone_blocks_strict_unbiased_readiness() -> None:
    assert strict_unbiased_readiness(
        exact_pit_binding=True,
        member_session_complete=True,
        bitemporal_availability_verified=True,
        trading_status_authoritative=False,
        corporate_action_validated=True,
        trusted_research_ledger=True,
        trusted_execution_ledger=True,
        adjustment_changes_explained=True,
    ) is False


def test_effective_time_without_as_known_time_blocks_unbiased_readiness() -> None:
    assert strict_unbiased_readiness(
        exact_pit_binding=True,
        member_session_complete=True,
        bitemporal_availability_verified=False,
        trading_status_authoritative=True,
        corporate_action_validated=True,
        trusted_research_ledger=True,
        trusted_execution_ledger=True,
        adjustment_changes_explained=True,
    ) is False


def test_identical_import_is_idempotent_and_identity_conflict_is_atomic(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    first = _import(store, _document())
    repeated = _import(store, _document())
    assert repeated["batch_id"] == first["batch_id"]
    assert repeated["idempotent"] is True

    conflict = _document()
    conflict["raw_source"]["content_sha256"] = "d" * 64
    conflict["raw_prices"][0]["close"] = 10.4
    conflict["research_prices"][0]["close"] = 10.4
    with pytest.raises(PriceLedgerConflictError):
        _import(store, conflict)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM price_ledger_batches"
        ).fetchone()[0] == 1


def test_same_canonical_prices_are_reused_across_scopes(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _import(store, _document())
    second = _document()
    second["scope_id"] = "csi800"

    result = _import(store, second)

    assert result["idempotent"] is False
    assert result["canonical_rows_reused"] == 4
    audit = store.audit_cross_scope_consistency(
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )
    assert audit["ready"] is True
    assert audit["checked_scope_count"] == 2
    readiness = store.inspect_readiness(
        scope_id="csi800",
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )
    assert readiness["canonical_price_consistency"] is True
    assert readiness["canonical_evidence_sha256"] == audit[
        "canonical_evidence_sha256"
    ]


def test_cross_scope_absolute_and_return_conflicts_are_structured_and_atomic(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _import(store, _document())
    conflict = _document()
    conflict["scope_id"] = "csi800"
    for price_key in ("raw_prices", "research_prices"):
        for field in ("open", "high", "low", "close"):
            conflict[price_key][1][field] *= 1.01

    with pytest.raises(PriceLedgerConflictError) as caught:
        _import(store, conflict)

    evidence = caught.value.evidence
    assert evidence["code"] == "cross_scope_price_conflict"
    classifications = {
        classification
        for item in evidence["conflicts"]
        for classification in item["classifications"]
    }
    assert "absolute_price_conflict" in classifications
    assert "return_conflict" in classifications
    assert all("path" not in json.dumps(item) for item in evidence["conflicts"])
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM price_ledger_batches"
        ).fetchone()[0] == 1


def test_hfq_constant_anchor_conflict_is_not_mislabeled_as_return_conflict(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _import(store, _document())
    conflict = _document()
    conflict["scope_id"] = "csi800"
    for item in conflict["research_prices"]:
        for field in ("open", "high", "low", "close"):
            item[field] *= 2

    with pytest.raises(PriceLedgerConflictError) as caught:
        _import(store, conflict)

    hfq = [
        item
        for item in caught.value.evidence["conflicts"]
        if item["adjustment"] == "hfq"
    ]
    assert hfq
    assert all(
        "hfq_constant_anchor_conflict" in item["classifications"]
        for item in hfq
    )
    assert all(
        "return_conflict" not in item["classifications"] for item in hfq
    )


def test_different_source_dataset_is_isolated_across_scopes(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _import(store, _document())
    second = _document()
    second["scope_id"] = "csi800"
    second["raw_source"]["dataset"] = "independent-raw"
    second["research_source"]["dataset"] = "independent-hfq"
    for price_key in ("raw_prices", "research_prices"):
        for item in second[price_key]:
            for field in ("open", "high", "low", "close"):
                item[field] *= 1.01

    result = _import(store, second)

    assert result["canonical_rows_reused"] == 0
    audit = store.audit_cross_scope_consistency(
        start="2024-01-02",
        end="2024-01-03",
    )
    assert audit["ready"] is True
    assert audit["conflict_identity_count"] == 0


def test_cross_scope_report_decision_is_not_truncated() -> None:
    rows = []
    for index in range(101):
        code = f"{index:06d}"
        for scope, close in (("csi300", 10.0), ("csi800", 11.0)):
            rows.append(
                {
                    "scope_id": scope,
                    "security_code": code,
                    "date": "2024-01-02",
                    "source": {
                        "provider": "provider",
                        "dataset": "dataset",
                        "version": "v1",
                    },
                    "adjustment": "raw",
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 100.0,
                }
            )

    report = PriceLedgerStore._build_cross_scope_report(
        rows,
        start="2024-01-02",
        end="2024-01-02",
        limit=1,
    )

    assert report["ready"] is False
    assert report["conflict_identity_count"] == 101
    assert report["truncated"] is True
    assert len(report["conflicts"]) == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value.update(
                schema_version="dual-price-ledger-import/v0"
            ),
            "unsupported dual-price import schema",
        ),
        (
            lambda value: value["research_source"].update(adjustment="qfq"),
            "research_source.adjustment must be hfq",
        ),
        (
            lambda value: value["raw_source"].update(adjustment="hfq"),
            "raw_source.adjustment must be raw",
        ),
    ],
)
def test_legacy_or_mixed_adjustment_import_contract_is_rejected(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    document = _document()
    mutation(document)
    with pytest.raises(PriceLedgerValidationError, match=message):
        _import(PriceLedgerStore(tmp_path / "experiment.db"), document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["research_prices"][1].update(open=11.7),
            "inconsistent adjustment factors",
        ),
        (
            lambda value: value["raw_prices"][0].update(low=12.0),
            "OHLC relationship",
        ),
        (
            lambda value: value["raw_prices"][0].update(close=0),
            "must be positive",
        ),
        (
            lambda value: value.update(coverage_from="2024-01-01"),
            "coverage boundaries",
        ),
    ],
)
def test_range_field_and_implied_factor_quality_gates(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    document = _document()
    mutation(document)
    with pytest.raises(PriceLedgerValidationError, match=message):
        _import(PriceLedgerStore(tmp_path / "experiment.db"), document)


def test_unexplained_normal_change_is_limited_but_abnormal_jump_is_rejected(
    tmp_path: Path,
) -> None:
    limited = _document()
    limited["corporate_action_source"] = None
    limited["corporate_actions"] = []
    store = PriceLedgerStore(tmp_path / "limited.db")
    _import(store, limited)
    readiness = store.inspect_readiness(
        scope_id="csi300",
        start="2024-01-02",
        end="2024-01-03",
    )
    assert readiness["ready_for_return_research"] is False
    assert readiness["ready_for_execution_simulation"] is False
    assert readiness["ready_for_real_tuning"] is False
    assert "corporate_action_authoritative_evidence_missing" in (
        readiness["limitations"]
    )
    assert "adjustment_factor_changes_unexplained" in readiness["limitations"]

    abnormal = _document()
    abnormal["corporate_action_source"] = None
    abnormal["corporate_actions"] = []
    for field in ("open", "high", "low", "close"):
        abnormal["research_prices"][1][field] *= 2
    with pytest.raises(
        PriceLedgerValidationError,
        match="abnormal adjustment-factor jump",
    ):
        _import(PriceLedgerStore(tmp_path / "abnormal.db"), abnormal)


def test_authoritative_action_without_numeric_multiplier_cannot_explain_jump(
    tmp_path: Path,
) -> None:
    document = _document()
    document["corporate_actions"][0]["adjustment_multiplier"] = None
    for field in ("open", "high", "low", "close"):
        document["research_prices"][1][field] *= 2

    with pytest.raises(
        PriceLedgerValidationError,
        match="abnormal adjustment-factor jump",
    ):
        _import(PriceLedgerStore(tmp_path / "empty-multiplier.db"), document)


@pytest.mark.parametrize(
    "evidence_level",
    ["public_cross_validated", "licensed", "exchange_authoritative"],
)
def test_non_declared_source_without_governance_receipt_is_rejected(
    tmp_path: Path,
    evidence_level: str,
) -> None:
    document = _document()
    document["raw_source"]["evidence_level"] = evidence_level
    with pytest.raises(
        PriceLedgerValidationError,
        match="managed artifact governance",
    ):
        PriceLedgerStore(tmp_path / "ungoverned.db").import_batch(
            **document,
            imported_by_user_id=7,
        )


def test_non_declared_no_action_coverage_cannot_bypass_governance(
    tmp_path: Path,
) -> None:
    document = _privileged_document()
    document["corporate_actions"] = []
    document["research_prices"][1] = copy.deepcopy(
        document["raw_prices"][1]
    )
    with pytest.raises(
        PriceLedgerValidationError,
        match="managed artifact governance",
    ):
        _import(PriceLedgerStore(tmp_path / "stable.db"), document)


def test_overlapping_source_versions_are_retained_but_fail_closed_as_ambiguous(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "ambiguous.db")
    _import(store, _document())
    second = _document()
    for source_name in (
        "raw_source",
        "research_source",
        "corporate_action_source",
    ):
        second[source_name]["provider"] += "-revision"
        second[source_name]["version"] = "2024.01.03-r2"
        second[source_name]["content_sha256"] = "d" * 64
    _import(store, second)

    readiness = store.inspect_readiness(
        scope_id="csi300",
        start="2024-01-02",
        end="2024-01-03",
    )
    assert readiness["ledger_available"] is True
    assert readiness["dual_ledger_complete"] is False
    assert readiness["ambiguous_roles"] == [
        "raw_execution",
        "research_adjusted",
    ]
    assert "dual_ledger_price_identity_ambiguous" in (
        readiness["limitations"]
    )
    assert readiness["ready_for_execution_simulation"] is False


def test_tampering_is_detected_even_if_immutability_trigger_is_bypassed(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _import(store, _document())
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER price_ledger_prices_no_update")
        connection.execute(
            """
            UPDATE price_ledger_prices
            SET close = 999
            WHERE price_role='raw_execution' AND trading_date='2024-01-02'
            """
        )
    with pytest.raises(PriceLedgerIntegrityError):
        store.inspect_readiness(
            scope_id="csi300",
            start="2024-01-02",
            end="2024-01-03",
        )


def test_missing_store_is_explicitly_not_a_legacy_upgrade(
    tmp_path: Path,
) -> None:
    readiness = PriceLedgerStore(
        tmp_path / "missing.db"
    ).inspect_readiness(
        scope_id="csi300",
        start="2024-01-02",
        end="2024-01-03",
    )
    assert readiness["ledger_available"] is False
    assert readiness["reason"] == "ledger_unavailable"
    assert readiness["dual_ledger_complete"] is False
    assert readiness["ready_for_real_tuning"] is False


def test_cache_readiness_blocks_execution_until_runtime_uses_ledger(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _import(store, _document())
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    frame = pd.DataFrame(
        [
            [10.0, 11.0, 9.0, 10.5, 1000.0],
            [11.55, 13.2, 11.0, 12.1, 1200.0],
        ],
        index=pd.DatetimeIndex(
            ["2024-01-02", "2024-01-03"],
            name="date",
        ),
        columns=columns,
    )

    class Cache:
        async def load_pivot_with_provenance(self, _cache_key: str):
            return frame, {
                "providers": ["validated-research-feed"],
                "evidence_levels": ["public_aggregator"],
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
            requested_codes=["000001"],
            required_start="2024-01-02",
            required_end="2024-01-03",
            price_ledger_store=store,
        )
    )
    assert inspected.report["ready_for_return_research"] is True
    assert inspected.report["ready_for_execution_simulation"] is False
    assert inspected.report["price_ledger"]["dual_ledger_complete"] is True
    assert inspected.report["canonical_runtime_price_bound"] is False
    assert (
        "runtime_parquet_not_bound_to_canonical_price_evidence"
        in inspected.report["research_limitations"]
    )
    # Point-in-time universe evidence is a separate mandatory real-tuning gate.
    assert inspected.report["ready_for_real_tuning"] is False


def test_paper_execution_accepts_research_grade_raw_on_bound_path(
    tmp_path: Path,
) -> None:
    """L2 paper trading gate: research-grade raw unlocks simulation readiness.

    v0.8.4 relaxes the paper-execution data tier from licensed-only
    (price_ledger._EXECUTION_LEVELS) to research-grade raw evidence.  Live
    (L3) remains hard-locked: ready_for_real_tuning stays false.
    """
    store = PriceLedgerStore(tmp_path / "experiment.db")
    timeline = _bound_timeline()
    _bind_runtime(store, _research_grade_document(), timeline=timeline)
    readiness = store.inspect_bound_runtime_readiness(
        scope_id="csi300",
        timeline_identity=timeline.identity(),
        trading_dates=timeline.dates,
    )
    assert readiness["canonical_runtime_price_bound"] is True
    assert readiness["roles"]["raw_execution"]["trusted"] is True
    assert readiness["ready_for_execution_simulation"] is True
    # L3 stays locked: no licensed execution ledger, no corporate-action
    # position application, so real tuning remains closed.
    assert readiness["ready_for_real_tuning"] is False
    assert "raw_execution_source_evidence_insufficient" not in (
        readiness["limitations"]
    )


def test_paper_execution_accepts_licensed_raw_on_bound_path(
    tmp_path: Path,
) -> None:
    """A licensed raw source also satisfies the paper gate (superset tier)."""
    store = PriceLedgerStore(tmp_path / "experiment.db")
    timeline = _bound_timeline()
    _bind_runtime(store, _privileged_document(), timeline=timeline)
    readiness = store.inspect_bound_runtime_readiness(
        scope_id="csi300",
        timeline_identity=timeline.identity(),
        trading_dates=timeline.dates,
    )
    assert readiness["ready_for_execution_simulation"] is True
    assert readiness["ready_for_real_tuning"] is False


def test_declared_raw_still_blocks_paper_execution_on_bound_path(
    tmp_path: Path,
) -> None:
    """declared test data never unlocks the paper gate."""
    store = PriceLedgerStore(tmp_path / "experiment.db")
    timeline = _bound_timeline()
    _bind_runtime(store, _document(), timeline=timeline)
    readiness = store.inspect_bound_runtime_readiness(
        scope_id="csi300",
        timeline_identity=timeline.identity(),
        trading_dates=timeline.dates,
    )
    assert readiness["ready_for_execution_simulation"] is False
    assert readiness["ready_for_real_tuning"] is False
    assert "raw_execution_source_evidence_insufficient" in (
        readiness["limitations"]
    )


def test_range_readiness_reports_research_grade_raw_without_certifying_execution(
    tmp_path: Path,
) -> None:
    """A non-bound range query exposes paper-grade raw trust but never certifies
    execution; only the exact runtime-binding path can do that."""
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _governed_import(store, _research_grade_document())
    readiness = store.inspect_readiness(
        scope_id="csi300",
        start="2024-01-02",
        end="2024-01-03",
        security_codes=["000001"],
    )
    assert readiness["dual_ledger_complete"] is True
    assert readiness["roles"]["raw_execution"]["trusted"] is True
    assert readiness["ready_for_execution_simulation"] is False
    assert "generic_readiness_not_execution_certification" in (
        readiness["limitations"]
    )


def test_strict_unbiased_keeps_licensed_execution_ledger_requirement() -> None:
    """Research-grade raw relaxes paper, not the strict unbiased research gate."""
    assert strict_unbiased_readiness(
        exact_pit_binding=True,
        member_session_complete=True,
        bitemporal_availability_verified=True,
        trading_status_authoritative=True,
        corporate_action_validated=True,
        trusted_research_ledger=True,
        trusted_execution_ledger=False,
        adjustment_changes_explained=True,
    ) is False


def test_migration_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "experiment.db"

    async def scenario() -> None:
        async with aiosqlite.connect(path) as connection:
            await connection.executescript(
                """
                CREATE TABLE experiments (id INTEGER PRIMARY KEY);
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                """
            )
            await migrate_experiment(connection)
            await migrate_experiment(connection)
            await connection.commit()

    asyncio.run(scenario())
    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE 'price_ledger_%'
                """
            )
        }
        version = connection.execute(
            """
            SELECT version FROM schema_migrations
            WHERE version='experiment-010-dual-price-ledger'
            """
        ).fetchone()
    assert names == {
        "price_ledger_batches",
        "price_ledger_prices",
        "price_ledger_adjustment_factors",
        "price_ledger_corporate_actions",
        "price_ledger_runtime_bindings",
        "price_ledger_batch_governance",
    }
    assert version == ("experiment-010-dual-price-ledger",)


def test_api_enforces_admin_import_and_returns_sanitized_business_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PriceLedgerStore(tmp_path / "private" / "experiment.db")
    monkeypatch.setattr(price_ledger, "_store", lambda **_kwargs: store)
    app = FastAPI()
    app.include_router(price_ledger.router)
    current_user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read", "data:update"],
    }
    app.dependency_overrides[get_current_user] = lambda: current_user

    with TestClient(app) as client:
        denied = client.post(
            "/api/data/price-ledger/imports",
            json=_document(),
        )
        contract = client.get("/api/data/price-ledger/import-contract")
        audit_denied = client.get(
            "/api/data/price-ledger/cross-scope-audit",
            params={"start": "2024-01-02", "end": "2024-01-03"},
        )
    assert denied.status_code == 403
    assert audit_denied.status_code == 403
    assert contract.status_code == 200
    assert contract.json()["data"]["administrator_permission"] == "admin:users"

    current_user["is_admin"] = True
    with TestClient(app) as client:
        governed = client.post(
            "/api/data/price-ledger/imports",
            json=_privileged_document(),
        )
        imported = client.post(
            "/api/data/price-ledger/imports",
            json=_declared_document(),
        )
        readiness = client.get(
            "/api/data/price-ledger/readiness",
            params={
                "scope_id": "csi300",
                "start": "2024-01-02",
                "end": "2024-01-03",
            },
        )
        prices = client.get(
            "/api/data/price-ledger/prices",
            params={
                "scope_id": "csi300",
                "role": "raw_execution",
                "start": "2024-01-02",
                "end": "2024-01-03",
            },
        )
    assert governed.status_code == 409
    assert (
        governed.json()["detail"]["code"]
        == "price_evidence_governance_required"
    )
    assert imported.status_code == 200, imported.text
    assert readiness.status_code == 200
    assert (
        readiness.json()["data"]["ready_for_unbiased_return_research"]
        is False
    )
    assert readiness.json()["data"]["ready_for_unbiased_research"] is False
    assert prices.status_code == 200
    rendered = json.dumps(
        {
            "import": imported.json(),
            "readiness": readiness.json(),
            "prices": prices.json(),
        }
    )
    assert str(tmp_path) not in rendered
    assert "raw_execution" in rendered


def test_api_rejects_path_like_scope_without_echoing_private_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PriceLedgerStore(tmp_path / "private" / "experiment.db")
    monkeypatch.setattr(price_ledger, "_store", lambda **_kwargs: store)
    app = FastAPI()
    app.include_router(price_ledger.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": True,
        "permissions": [],
    }
    document = copy.deepcopy(_declared_document())
    document["scope_id"] = "/Users/private"
    with TestClient(app) as client:
        response = client.post(
            "/api/data/price-ledger/imports",
            json=document,
        )
    assert response.status_code == 422
    assert "/Users/private" not in response.text


def _legacy_frame(closes: list[float]) -> pd.DataFrame:
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    rows = [
        [close, close * 1.1, close * 0.9, close, 1000.0]
        for close in closes
    ]
    return pd.DataFrame(
        rows,
        index=pd.DatetimeIndex(
            ["2024-01-02", "2024-01-03", "2024-01-04"],
            name="date",
        ),
        columns=columns,
    )


def test_legacy_cache_audit_separates_anchor_and_return_conflicts() -> None:
    class Cache:
        frames = {
            "csi500": _legacy_frame([10.0, 11.0, 12.0]),
            "csi800": _legacy_frame([20.0, 22.0, 25.0]),
        }

        async def load_legacy_pivot_for_audit(self, scope_id: str):
            return self.frames[scope_id]

        async def get_cache_info(self, scope_id: str):
            return {
                "pool_id": scope_id,
                "schema_version": 3,
                "price_adjustment": "qfq",
                "source_provenance": None,
            }

    report = asyncio.run(
        audit_legacy_price_caches(
            Cache(),  # type: ignore[arg-type]
            start="2024-01-02",
            end="2024-01-04",
            scope_ids=["csi500", "csi800"],
        )
    )

    assert report["conflict_identity_count"] == 3
    assert report["return_conflict_count"] == 1
    assert report["ready_for_unbiased_research"] is False
    by_date = {item["date"]: item for item in report["conflicts"]}
    assert "qfq_constant_anchor_conflict" in by_date["2024-01-02"][
        "classifications"
    ]
    assert "return_conflict" not in by_date["2024-01-02"][
        "classifications"
    ]
    assert "return_conflict" in by_date["2024-01-04"][
        "classifications"
    ]
    assert "legacy_schema3_cache_present" in report["limitations"]
    assert all("path" not in json.dumps(item) for item in report["conflicts"])


def test_legacy_cache_audit_does_not_merge_different_verified_sources() -> None:
    class Cache:
        async def load_legacy_pivot_for_audit(self, scope_id: str):
            return (
                _legacy_frame([10.0, 11.0, 12.0])
                if scope_id == "csi300"
                else _legacy_frame([20.0, 22.0, 25.0])
            )

        async def get_cache_info(self, scope_id: str):
            provider = "feed-a" if scope_id == "csi300" else "feed-b"
            return {
                "pool_id": scope_id,
                "schema_version": 4,
                "price_adjustment": "hfq",
                "source_provenance": {
                    "schema_version": "cache-source-provenance/v1",
                    "providers": [provider],
                    "endpoints": ["daily"],
                },
            }

    report = asyncio.run(
        audit_legacy_price_caches(
            Cache(),  # type: ignore[arg-type]
            start="2024-01-02",
            end="2024-01-04",
            scope_ids=["csi300", "csi800"],
        )
    )

    assert report["conflict_identity_count"] == 0
    assert report["isolated_source_pairs"] == [["csi300", "csi800"]]


def test_clean_verified_legacy_return_cache_never_claims_unbiased() -> None:
    class Cache:
        async def load_legacy_pivot_for_audit(self, _scope_id: str):
            return _legacy_frame([10.0, 11.0, 12.0])

        async def get_cache_info(self, scope_id: str):
            return {
                "pool_id": scope_id,
                "schema_version": 4,
                "price_adjustment": "hfq",
                "source_provenance": {
                    "schema_version": "cache-source-provenance/v1",
                    "providers": ["same-verified-feed"],
                    "endpoints": ["daily"],
                },
            }

    report = asyncio.run(
        audit_legacy_price_caches(
            Cache(),  # type: ignore[arg-type]
            start="2024-01-02",
            end="2024-01-04",
            scope_ids=["csi300", "csi800"],
        )
    )

    assert report["conflict_identity_count"] == 0
    assert report["descriptive_return_consistency"] is True
    assert report["ready_for_unbiased_return_research"] is False
    assert report["ready_for_unbiased_research"] is False
    assert (
        "legacy_cache_cannot_establish_unbiased_readiness"
        in report["limitations"]
    )


def test_legacy_cache_audit_reports_mixed_adjustment_without_false_merge() -> None:
    class Cache:
        async def load_legacy_pivot_for_audit(self, scope_id: str):
            return (
                _legacy_frame([10.0, 11.0, 12.0])
                if scope_id == "csi300"
                else _legacy_frame([20.0, 22.0, 25.0])
            )

        async def get_cache_info(self, scope_id: str):
            return {
                "pool_id": scope_id,
                "schema_version": 3,
                "price_adjustment": (
                    "hfq" if scope_id == "csi300" else "qfq"
                ),
                "source_provenance": None,
            }

    report = asyncio.run(
        audit_legacy_price_caches(
            Cache(),  # type: ignore[arg-type]
            start="2024-01-02",
            end="2024-01-04",
            scope_ids=["csi300", "csi800"],
        )
    )

    assert report["conflict_identity_count"] == 0
    assert report["mixed_adjustment_identity_count"] == 3
    assert report["mixed_adjustment_return_conflict_count"] == 1
    assert report["mixed_adjustment_pairs"] == [["csi300", "csi800"]]
    assert all(
        item["classifications"][0]
        == "mixed_adjustment_absolute_prices_not_comparable"
        for item in report["mixed_adjustment_examples"]
    )


def test_api_returns_structured_cross_scope_conflict_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PriceLedgerStore(tmp_path / "experiment.db")
    _import(store, _declared_document())
    monkeypatch.setattr(price_ledger, "_store", lambda **_kwargs: store)
    app = FastAPI()
    app.include_router(price_ledger.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": True,
        "permissions": [],
    }
    conflict = _declared_document()
    conflict["scope_id"] = "csi800"
    for item in conflict["research_prices"]:
        for field in ("open", "high", "low", "close"):
            item[field] *= 2

    with TestClient(app) as client:
        response = client.post(
            "/api/data/price-ledger/imports",
            json=conflict,
        )
        audit = client.get(
            "/api/data/price-ledger/cross-scope-audit",
            params={
                "start": "2024-01-02",
                "end": "2024-01-03",
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["evidence"]["code"] == "cross_scope_price_conflict"
    assert audit.status_code == 200
    assert audit.json()["data"]["ready"] is True
