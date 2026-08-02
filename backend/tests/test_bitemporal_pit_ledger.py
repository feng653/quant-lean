from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

import pytest

from backend.data.point_in_time_master import (
    PointInTimeMasterStore,
)
from backend.data.point_in_time_universe import PointInTimeUniverseTimeline
from backend.data.price_ledger import (
    PriceLedgerConflictError,
    PriceLedgerIntegrityError,
    PriceLedgerStore,
)


FIXTURE = Path(__file__).parent / "fixtures" / "bitemporal_pit_ledger_v2.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_pit_revision_is_append_only_and_future_revision_is_not_visible(
    tmp_path: Path,
) -> None:
    store = PointInTimeMasterStore(tmp_path / "pit.db")
    first_document = _fixture()["pit"]
    first = store.import_batch(**first_document, imported_by_user_id=7)
    second_document = copy.deepcopy(first_document)
    second_document["source"].update(
        version="r2",
        revision=2,
        supersedes_batch_id=first["batch_id"],
        content_sha256="d" * 64,
    )
    second_document["records"][0]["member_name"] = "corrected fixture"
    second = store.import_batch(**second_document, imported_by_user_id=7)

    before_revision = store.query_effective_history(
        domain="index_membership",
        scope_id="fixture_bitemporal_index",
        start="2024-01-02",
        end="2024-01-03",
        as_known_at=first["ingested_at"],
    )
    after_revision = store.query_effective_history(
        domain="index_membership",
        scope_id="fixture_bitemporal_index",
        start="2024-01-02",
        end="2024-01-03",
        as_known_at=second["ingested_at"],
    )
    assert before_revision["records"][0]["attributes"]["member_name"] == (
        "fixture only"
    )
    assert after_revision["records"][0]["attributes"]["member_name"] == (
        "corrected fixture"
    )
    assert before_revision["bitemporal_availability_verified"] is True

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_master_batches"
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE pit_master_batches SET revision=3 WHERE batch_id=?",
                (first["batch_id"],),
            )


def test_confirmed_no_event_conflicts_with_event_evidence(tmp_path: Path) -> None:
    store = PriceLedgerStore(tmp_path / "ledger.db", initialize=True)
    source = {
        "provider": "fixture_declared_provider",
        "dataset": "fixture_actions",
        "version": "r1",
        "adjustment": "corporate_action",
        "evidence_level": "declared",
        "retrieved_at": "2024-02-01T00:00:00Z",
        "content_sha256": "e" * 64,
    }
    no_event = store.import_corporate_action_evidence(
        scope_id="fixture_bitemporal_index",
        security_code="000001",
        evidence_kind="confirmed_no_event",
        effective_at="2024-01-01T00:00:00Z",
        effective_to="2024-01-31T23:59:59Z",
        available_at="2024-02-01T00:00:00Z",
        revision=1,
        supersedes_evidence_id=None,
        action_type=None,
        adjustment_multiplier=None,
        reference_id="fixture-no-event-january",
        source=source,
        imported_by_user_id=7,
    )
    assert no_event["evidence_id"].startswith("cae_")
    with pytest.raises(PriceLedgerConflictError) as caught:
        store.import_corporate_action_evidence(
            scope_id="fixture_bitemporal_index",
            security_code="000001",
            evidence_kind="event",
            effective_at="2024-01-15T00:00:00Z",
            effective_to=None,
            available_at="2024-02-01T00:00:00Z",
            revision=1,
            supersedes_evidence_id=None,
            action_type="cash_dividend",
            adjustment_multiplier=0.98,
            reference_id="fixture-conflicting-event",
            source=source,
            imported_by_user_id=7,
        )
    assert caught.value.evidence["code"] == "corporate_action_no_event_conflict"


def test_bitemporal_runtime_binding_validates_role_boundary_and_manifest_fields(
    tmp_path: Path,
) -> None:
    store = PriceLedgerStore(tmp_path / "ledger.db")
    price = _fixture()["price"]
    imported = store.import_batch(**price, imported_by_user_id=7)
    cutoff = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    timeline = PointInTimeUniverseTimeline(
        pool_id="fixture_bitemporal_index",
        dates=("2024-01-02", "2024-01-03"),
        members_by_date=(("000001",), ("000001",)),
        union_codes=("000001",),
        source_batches=(
            {
                "batch_id": "pit_" + "a" * 32,
                "batch_digest": "a" * 64,
                "coverage_from": "2024-01-02",
                "coverage_to": "2024-01-03",
            },
        ),
        timeline_hash="temporary",
        coverage_from="2024-01-02",
        coverage_to="2024-01-03",
        as_known_at=cutoff,
        bitemporal_availability_verified=True,
    )
    from backend.data.point_in_time_universe import _timeline_hash

    timeline = replace(
        timeline,
        timeline_hash=_timeline_hash(
            pool_id=timeline.pool_id,
            dates=timeline.dates,
            members_by_date=timeline.members_by_date,
        ),
    )
    binding = store.bind_runtime_scope(
        scope_id=timeline.pool_id,
        timeline_identity=timeline.identity(),
        trading_dates=timeline.dates,
        batch_ids=[imported["batch_id"]],
        status_source={
            "provider": "fixture_declared_provider",
            "dataset": "fixture_status",
            "version": "r1",
            "adjustment": "trading_status",
            "evidence_level": "declared",
            "retrieved_at": "2024-01-04T00:00:00Z",
            "content_sha256": "f" * 64,
        },
        suspension_observations=[],
        bound_by_user_id=7,
        as_known_at=cutoff,
    )
    validated = store.validate_runtime_binding(
        binding_id=binding["binding_id"],
        expected_scope_id=timeline.pool_id,
        expected_binding_digest=binding["binding_digest"],
    )
    assert validated["bitemporal_availability_verified"] is True
    assert validated["price_role_usage"] == {
        "signal_and_research_features": "research_adjusted",
        "execution_fills_and_valuation": "raw_execution",
        "mixed_role_fallback_allowed": False,
    }

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DROP TRIGGER price_ledger_runtime_bindings_no_update"
        )
        connection.execute(
            """
            UPDATE price_ledger_runtime_bindings
            SET price_role_usage_json='{}' WHERE binding_id=?
            """,
            (binding["binding_id"],),
        )
    with pytest.raises(PriceLedgerIntegrityError, match="role usage"):
        store.validate_runtime_binding(binding_id=binding["binding_id"])
