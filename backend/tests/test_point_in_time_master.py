from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import point_in_time
from backend.data.point_in_time_master import (
    PointInTimeConflictError,
    PointInTimeIntegrityError,
    PointInTimeMasterStore,
    PointInTimeValidationError,
)
from backend.data.cache_readiness import inspect_cached_market_data
from backend.db.migrate import migrate_experiment
from backend.dependencies import get_current_user

FIXTURE = Path(__file__).parent / "fixtures" / "point_in_time_master_v1.json"


def test_display_resolution_allows_only_weekend_prior_activated_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "master.db")
    calls: list[str] = []

    def query(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["as_of"])
        if kwargs["as_of"] == "2024-01-05":
            return {
                "available": True,
                "reason": None,
                "records": [{"security_code": "000001"}],
                "source_batches": [],
            }
        return {
            "available": False,
            "reason": "effective_dated_history_missing",
            "records": [],
            "source_batches": [],
        }

    monkeypatch.setattr(store, "query_as_of", query)
    resolved = store.resolve_display_observation(
        domain="index_membership",
        scope_id="csi300",
        requested_as_of="2024-01-07",
    )

    assert calls == ["2024-01-07", "2024-01-05"]
    assert resolved["resolved_as_of"] == "2024-01-05"
    assert resolved["staleness_calendar_days"] == 2
    assert resolved["resolution"] == "weekend_prior_activated_observation"


def test_display_resolution_fails_closed_for_missing_weekday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "master.db")
    calls: list[str] = []

    def query(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["as_of"])
        return {
            "available": False,
            "reason": "effective_dated_history_missing",
            "records": [],
            "source_batches": [],
        }

    monkeypatch.setattr(store, "query_as_of", query)
    resolved = store.resolve_display_observation(
        domain="index_membership",
        scope_id="csi300",
        requested_as_of="2024-01-08",
    )

    assert calls == ["2024-01-08"]
    assert resolved["resolved_as_of"] is None
    assert "point_in_time_working_day_coverage_missing" in resolved["risk_warnings"]


def _documents() -> list[dict[str, Any]]:
    documents = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Generic master-store tests use a non-production scope.  Canonical CSI
    # scopes are deliberately reserved for the governed four-scope import.
    for document in documents:
        if document["domain"] == "index_membership":
            document["scope_id"] = "fixture_csi300"
    return documents


def _import(store: PointInTimeMasterStore, document: dict[str, Any]) -> dict[str, Any]:
    return store.import_batch(
        **document,
        imported_by_user_id=7,
    )


def test_effective_dated_fixture_supports_as_of_and_full_coverage(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    results = [_import(store, document) for document in _documents()]

    assert all(item["idempotent"] is False for item in results)
    assert all("/" not in item["batch_id"] for item in results)
    june = store.query_as_of(
        domain="index_membership",
        scope_id="fixture_csi300",
        as_of="2024-06-28",
    )
    july = store.query_as_of(
        domain="index_membership",
        scope_id="fixture_csi300",
        as_of="2024-07-02",
    )
    assert june["available"] is True
    assert [item["security_code"] for item in june["records"]] == ["000001"]
    assert [item["security_code"] for item in july["records"]] == ["600000"]

    coverage = store.inspect_research_coverage(
        pool_id="fixture_csi300",
        security_codes=["000001", "600000"],
        start="2024-01-01",
        end="2024-12-31",
    )
    assert coverage["ready"] is True
    assert coverage["universe"]["ready"] is True
    assert coverage["security_master"]["ready"] is True
    assert coverage["industry"]["neutralization_ready"] is True


def test_current_snapshot_never_claims_historical_coverage(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    document = _documents()[2]
    document.update(
        evidence_kind="current_snapshot",
        coverage_from="2024-12-31",
        coverage_to="2024-12-31",
        records=[
            {
                **record,
                "effective_from": "2024-12-31",
                "effective_to": "2024-12-31",
            }
            for record in document["records"]
        ],
    )
    _import(store, document)

    result = store.query_as_of(
        domain="industry",
        scope_id="cninfo_008001",
        as_of="2024-12-31",
        security_codes=["000001"],
    )
    assert result["available"] is False
    assert result["reason"] == ("current_snapshot_not_valid_for_historical_research")


def test_current_snapshot_rejects_inferred_historical_interval(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    document = _documents()[2]
    document["evidence_kind"] = "current_snapshot"

    with pytest.raises(
        PointInTimeValidationError,
        match="observation date only",
    ):
        _import(store, document)


def test_overlap_rejection_is_atomic_and_identical_import_is_idempotent(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    document = _documents()[0]
    first = _import(store, document)
    repeated = _import(store, document)
    assert repeated["batch_id"] == first["batch_id"]
    assert repeated["idempotent"] is True

    overlap = _documents()[0]
    overlap["source"] = {
        **overlap["source"],
        "version": "2024-correction",
        "content_sha256": "d" * 64,
    }
    overlap["records"] = [
        {
            **overlap["records"][0],
            "effective_from": "2024-06-01",
        }
    ]
    with pytest.raises(PointInTimeConflictError):
        _import(store, overlap)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pit_master_batches").fetchone()[0] == 1


def test_tampering_is_detected_even_if_database_trigger_is_bypassed(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    _import(store, _documents()[0])
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER pit_master_intervals_no_update")
        connection.execute(
            """
            UPDATE pit_master_intervals
            SET attributes_json = '{"exchange":"SSE"}'
            WHERE security_code = '000001'
            """
        )

    with pytest.raises(PointInTimeIntegrityError):
        store.query_as_of(
            domain="security",
            scope_id="cn_equity",
            as_of="2024-06-01",
        )


def test_path_like_source_and_scope_are_rejected_without_echoing_paths(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    document = _documents()[0]
    document["source"]["provider"] = "/Users/private/provider"
    with pytest.raises(PointInTimeValidationError) as exc_info:
        _import(store, document)
    assert "/Users/private" not in str(exc_info.value)


def test_migration_is_idempotent_for_existing_experiment_database(
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
                WHERE type='table' AND name LIKE 'pit_master_%'
                """
            )
        }
        version = connection.execute(
            """
            SELECT version FROM schema_migrations
            WHERE version='experiment-009-point-in-time-master'
            """
        ).fetchone()
    assert names == {
        "pit_master_batches",
        "pit_master_intervals",
        "pit_master_governed_activations",
    }
    assert version == ("experiment-009-point-in-time-master",)


def test_api_enforces_permissions_and_never_returns_storage_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "private" / "experiment.db")
    monkeypatch.setattr(
        point_in_time,
        "_store",
        lambda **_kwargs: store,
    )
    app = FastAPI()
    app.include_router(point_in_time.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read"],
    }
    with TestClient(app) as client:
        denied = client.post(
            "/api/data/point-in-time/imports",
            json=_documents()[0],
        )
        missing = client.get(
            "/api/data/point-in-time/as-of",
            params={
                "domain": "industry",
                "scope_id": "cninfo_008001",
                "date": "2024-01-01",
            },
        )
    assert denied.status_code == 403
    assert missing.status_code == 200
    assert missing.json()["data"]["available"] is False
    assert missing.json()["data"]["reason"] == ("point_in_time_store_uninitialized")
    assert str(tmp_path) not in missing.text


def test_legacy_store_without_governed_activation_table_fails_closed(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(
        tmp_path / "experiment.db",
        initialize=True,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE pit_master_governed_activations")

    result = store.query_as_of(
        domain="index_membership",
        scope_id="csi300",
        as_of="2024-01-01",
    )

    assert result["available"] is False
    assert result["reason"] == "point_in_time_store_uninitialized"


def test_legacy_store_without_supersession_table_remains_legacy(
    tmp_path: Path,
) -> None:
    """A read-only v1 database has no possible revision edges to resolve."""

    store = PointInTimeMasterStore(tmp_path / "experiment.db")
    _import(store, _documents()[0])
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE point_in_time_batch_supersessions")

    result = store.query_as_of(
        domain="security",
        scope_id="cn_equity",
        as_of="2024-06-01",
    )

    assert result["available"] is True
    # The compatibility path never upgrades legacy evidence to bitemporal
    # availability proof merely because the newer reader understands it.
    assert result["bitemporal_availability_verified"] is False


def test_genuine_pre_v2_readonly_schema_stays_legacy_and_fails_known_at_closed(
    tmp_path: Path,
) -> None:
    """Readers must support archived v1 tables without v2 columns.

    The store is deliberately opened with ``initialize=False`` after the
    downgrade. This proves the read path neither requires a writable migration
    nor treats absent fields as availability/revision evidence.
    """

    path = tmp_path / "legacy-v1.db"
    writer = PointInTimeMasterStore(path)
    _import(writer, _documents()[0])
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER pit_master_intervals_no_overlap")
        connection.execute("DROP INDEX uq_pit_master_interval_identity")
        connection.execute("DROP TABLE point_in_time_batch_supersessions")
        for table, columns in {
            "pit_master_batches": (
                "available_at",
                "ingested_at",
                "revision",
                "supersedes_batch_id",
            ),
            "pit_master_intervals": (
                "effective_at",
                "available_at",
                "ingested_at",
                "revision",
            ),
        }.items():
            for column in columns:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    reader = PointInTimeMasterStore(path, initialize=False)
    normal_read = reader.query_as_of(
        domain="security",
        scope_id="cn_equity",
        as_of="2024-06-01",
    )
    known_at_read = reader.query_as_of(
        domain="security",
        scope_id="cn_equity",
        as_of="2024-06-01",
        as_known_at="2024-06-02T00:00:00Z",
    )
    history = reader.query_effective_history(
        domain="security",
        scope_id="cn_equity",
        start="2024-06-01",
        end="2024-06-01",
    )

    assert normal_read["available"] is True
    assert normal_read["bitemporal_availability_verified"] is False
    assert history["available"] is True
    assert history["bitemporal_availability_verified"] is False
    # An as-known-at query requires real availability and ingestion proof;
    # missing legacy columns must never be silently promoted to that proof.
    assert known_at_read["available"] is False
    assert known_at_read["reason"] == "effective_dated_history_missing"
    with sqlite3.connect(path) as connection:
        batch_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(pit_master_batches)")
        }
    assert "available_at" not in batch_columns


def test_import_requires_trusted_admin_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "experiment.db")
    monkeypatch.setattr(
        point_in_time,
        "_store",
        lambda **_kwargs: store,
    )
    current_user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:update"],
    }
    app = FastAPI()
    app.include_router(point_in_time.router)
    app.dependency_overrides[get_current_user] = lambda: current_user
    with TestClient(app) as client:
        operator_response = client.post(
            "/api/data/point-in-time/imports",
            json=_documents()[0],
        )
        current_user = {
            "id": 1,
            "is_admin": True,
            "permissions": [],
        }
        admin_response = client.post(
            "/api/data/point-in-time/imports",
            json=_documents()[0],
        )

    assert operator_response.status_code == 403
    assert operator_response.json()["detail"] == "需要权限: admin:users"
    assert admin_response.status_code == 200
    assert admin_response.json()["data"]["record_count"] == 2
    with sqlite3.connect(store.path) as connection:
        imported_by = connection.execute(
            "SELECT imported_by_user_id FROM pit_master_batches"
        ).fetchone()
    assert imported_by == (1,)


def test_market_data_readiness_consumes_verified_point_in_time_contract(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "research.db")
    for document in _documents():
        _import(store, document)
    dates = pd.bdate_range("2024-01-01", "2024-12-31", name="date")
    columns = pd.MultiIndex.from_product(
        [
            ["000001", "600000"],
            ["open", "high", "low", "close", "volume"],
        ],
        names=["code", "field"],
    )
    frame = pd.DataFrame(10.0, index=dates, columns=columns)
    for code in ("000001", "600000"):
        frame[(code, "low")] = 9.0
        frame[(code, "high")] = 11.0
        frame[(code, "volume")] = 1_000_000.0

    class Cache:
        async def load_pivot_with_provenance(self, _cache_key: str):
            return frame, {
                "providers": ["provider_a", "provider_b"],
                "evidence_levels": ["public_aggregator"],
                "adjustments": ["hfq"],
                "frame_digest": "dv2|fixture|sha256:" + "a" * 64,
                "identity_consistent": True,
                "complete_code_coverage": True,
                "all_batches_cross_validated": True,
            }

    result = asyncio.run(
        inspect_cached_market_data(
            Cache(),  # type: ignore[arg-type]
            cache_key="fixture_csi300",
            pool_id="fixture_csi300",
            requested_codes=[],
            required_start="2024-01-01",
            required_end="2024-12-31",
            point_in_time_store=store,
        )
    )
    # This deliberately non-production fixture scope has no fixed-size index
    # contract, so its complete effective-dated history is PIT-ready.
    assert result.report["point_in_time"]["ready"] is True
    assert result.report["universe_point_in_time"] is True
    assert result.report["survivorship_bias_risk"] is False
    # PIT coverage alone cannot promote a single adjusted cache into the raw
    # execution + hfq research + corporate-action dual-ledger contract.
    assert result.report["ready_for_unbiased_tuning"] is False
    assert result.report["ready_for_real_tuning"] is False
    assert result.report["price_ledger"]["reason"] == "ledger_unavailable"
