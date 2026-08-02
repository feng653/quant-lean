from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.data.generation_manifest import GenerationManifestStore
from backend.data.production_pit_release import (
    APPROVED_ARTIFACT_SCHEMA,
    ARTIFACT_PAYLOAD_SCHEMA,
    READINESS_SCHEMA,
    RELEASE_BUNDLE_SCHEMA,
    ApprovedProviderArtifactStore,
    AtomicPitReleaseRegistry,
    ProductionPitReleaseOrchestrator,
    ProductionPitReleasePolicy,
    ReleaseActivationBlocked,
)
from backend.data.production_pit_materializer import (
    ProductionPitMaterializationError,
    ProductionPitReleaseMaterializer,
    ProductionPitRuntimeReader,
)


START = "2020-01-02"
END = "2020-01-03"
POOLS = ("csi300", "csi500", "csi800", "csi1000")
CODES = {
    "csi300": "000001",
    "csi500": "000002",
    "csi800": "000003",
    "csi1000": "000004",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _temporal(day: str) -> dict[str, Any]:
    return {
        "effective_at": f"{day}T00:00:00Z",
        "available_at": f"{day}T01:00:00Z",
        "ingested_at": f"{day}T02:00:00Z",
        "revision": 1,
    }


def _interval(code: str, **extra: Any) -> dict[str, Any]:
    return {
        "security_code": code,
        "effective_from": START,
        "effective_to": END,
        **_temporal(START),
        **extra,
    }


def _write_artifact(
    root: Path,
    private_key: Ed25519PrivateKey,
    *,
    kind: str,
    scope: str,
    rows: list[dict[str, Any]],
) -> str:
    payload = {"schema_version": ARTIFACT_PAYLOAD_SCHEMA, "rows": rows}
    payload_bytes = _canonical(payload)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    payload_path = root / "artifacts" / "sha256" / payload_sha256[:2] / f"{payload_sha256}.json"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(payload_bytes)
    licence_receipt = b"fixture local research retention licence"
    licence_receipt_sha256 = hashlib.sha256(licence_receipt).hexdigest()
    licence_path = (
        root
        / "licence-receipts"
        / "sha256"
        / licence_receipt_sha256[:2]
        / licence_receipt_sha256
    )
    licence_path.parent.mkdir(parents=True, exist_ok=True)
    licence_path.write_bytes(licence_receipt)
    manifest: dict[str, Any] = {
        "schema_version": APPROVED_ARTIFACT_SCHEMA,
        "classification": "approved",
        "provider": "fixture_provider",
        "dataset": kind,
        "provider_version": "fixture-v1",
        "artifact_kind": kind,
        "evidence_level": "licensed",
        "scope_id": scope,
        "coverage_from": START,
        "coverage_to": END,
        "payload_sha256": payload_sha256,
        "size_bytes": len(payload_bytes),
        "row_count": len(rows),
        "licence_scope": "local_research_retention",
        "licence_receipt_sha256": licence_receipt_sha256,
        "staged_by": "fixture_collector",
        "approval": {
            "key_id": "fixture_approval_key",
            "reviewer_id": "fixture_reviewer",
            "approved_at": "2020-01-04T00:00:00Z",
        },
    }
    manifest["approval"]["signature"] = base64.b64encode(
        private_key.sign(_canonical(manifest))
    ).decode()
    manifest_bytes = _canonical(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_path = (
        root
        / "manifests"
        / "sha256"
        / manifest_sha256[:2]
        / f"{manifest_sha256}.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)
    return manifest_sha256


def _fixture(
    tmp_path: Path,
    *,
    mutate: tuple[str, str, Any] | None = None,
) -> tuple[ProductionPitReleaseOrchestrator, dict[str, Any]]:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    rows: dict[tuple[str, str], list[dict[str, Any]]] = {
        ("trading_calendar", "cn_equity"): [
            {"trading_date": day, **_temporal(day)} for day in (START, END)
        ],
        ("security_master", "all_a"): [
            _interval(
                code,
                listing_status="listed",
                name=f"fixture-{code}",
                exchange="szse",
            )
            for code in CODES.values()
        ],
        ("industry", "cninfo_008001"): [
            _interval(code, industry_code="A01", industry_name="fixture")
            for code in CODES.values()
        ],
        ("market_status", "all_a"): [
            {
                "security_code": code,
                "trading_date": day,
                "status": "tradable",
                **_temporal(day),
            }
            for code in CODES.values()
            for day in (START, END)
        ],
        ("dual_price_ledger", "all_a"): [
            {
                "security_code": code,
                "trading_date": day,
                "raw": {
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 1000,
                },
                "research_adjusted": {
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 1000,
                },
                "amount": 10_000,
                "adjustment_factor": 1,
                **_temporal(day),
            }
            for code in CODES.values()
            for day in (START, END)
        ],
        ("corporate_action_evidence", "all_a"): [
            _interval(code, evidence_kind="confirmed_no_event")
            for code in CODES.values()
        ],
        **{
            ("index_membership", pool): [_interval(code)]
            for pool, code in CODES.items()
        },
    }
    if mutate is not None:
        kind, scope, callback = mutate
        callback(rows[(kind, scope)])
    references = [
        _write_artifact(
            tmp_path / "approved",
            private_key,
            kind=kind,
            scope=scope,
            rows=artifact_rows,
        )
        for (kind, scope), artifact_rows in rows.items()
    ]
    store = ApprovedProviderArtifactStore(
        tmp_path / "approved",
        trusted_approval_keys={"fixture_approval_key": public_key},
    )
    policy = ProductionPitReleasePolicy(
        coverage_from=START,
        coverage_to=END,
        member_counts={pool: 1 for pool in POOLS},
    )
    bundle = {
        "schema_version": RELEASE_BUNDLE_SCHEMA,
        "coverage_from": START,
        "coverage_to": END,
        "artifact_manifest_sha256s": references,
    }
    return ProductionPitReleaseOrchestrator(store, policy=policy), bundle


def test_dry_run_lists_every_missing_required_artifact(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    store = ApprovedProviderArtifactStore(
        tmp_path / "approved",
        trusted_approval_keys={"fixture_approval_key": public_key},
    )
    policy = ProductionPitReleasePolicy(
        coverage_from=START,
        coverage_to=END,
        member_counts={pool: 1 for pool in POOLS},
    )
    report = ProductionPitReleaseOrchestrator(store, policy=policy).dry_run(
        {
            "schema_version": RELEASE_BUNDLE_SCHEMA,
            "coverage_from": START,
            "coverage_to": END,
            "artifact_manifest_sha256s": [],
        }
    )

    assert report["schema_version"] == READINESS_SCHEMA
    assert report["ready"] is False
    assert report["runtime_data_changed"] is False
    assert report["production_tables_written"] is False
    assert report["blocker_count"] == 10
    assert {item["code"] for item in report["blockers"]} == {
        "required_artifact_missing"
    }


def test_complete_approved_release_is_ready_and_atomically_authorised(
    tmp_path: Path,
) -> None:
    orchestrator, bundle = _fixture(tmp_path)
    report = orchestrator.dry_run(bundle)

    assert report["ready"] is True
    assert report["blocker_count"] == 0
    assert report["plan"]["coverage"] == {
        "policy": orchestrator.policy.document(),
        "trading_session_count": 2,
        "ever_member_security_count": 4,
        "member_session_count": 8,
        "tradable_member_session_count": 8,
    }

    registry_path = tmp_path / "release-registry.db"
    registry = AtomicPitReleaseRegistry(registry_path)
    first = orchestrator.activate(
        bundle,
        confirmation_plan_sha256=report["plan_sha256"],
        registry=registry,
        actor_user_id=42,
    )
    second = orchestrator.activate(
        bundle,
        confirmation_plan_sha256=report["plan_sha256"],
        registry=registry,
        actor_user_id=42,
    )
    assert first["authorised"] is True and first["runtime_materialised"] is False
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    with sqlite3.connect(registry_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "pit_release_authorizations" in tables
        assert "pit_master_batches" not in tables
        assert "price_ledger_batches" not in tables
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_release_authorized_artifacts"
        ).fetchone()[0] == 10


def test_missing_temporal_evidence_and_prices_fail_closed(tmp_path: Path) -> None:
    def break_prices(rows: list[dict[str, Any]]) -> None:
        rows[0].pop("available_at")
        rows.pop()

    orchestrator, bundle = _fixture(
        tmp_path,
        mutate=("dual_price_ledger", "all_a", break_prices),
    )
    report = orchestrator.dry_run(bundle)

    assert report["ready"] is False
    assert {item["code"] for item in report["blockers"]} == {
        "dual_price_ledger_invalid"
    }
    with pytest.raises(ReleaseActivationBlocked):
        orchestrator.activate(
            bundle,
            confirmation_plan_sha256=report["plan_sha256"],
            registry=AtomicPitReleaseRegistry(tmp_path / "blocked.db"),
            actor_user_id=42,
        )
    assert not (tmp_path / "blocked.db").exists()


def test_activation_confirmation_and_registry_isolation_are_enforced(
    tmp_path: Path,
) -> None:
    orchestrator, bundle = _fixture(tmp_path)
    report = orchestrator.dry_run(bundle)
    with pytest.raises(ReleaseActivationBlocked, match="fresh orchestrator"):
        AtomicPitReleaseRegistry(tmp_path / "forged.db").activate(
            report=report,
            actor_user_id=42,
        )
    assert not (tmp_path / "forged.db").exists()
    registry_path = tmp_path / "application.db"
    with sqlite3.connect(registry_path) as connection:
        connection.execute("CREATE TABLE experiments (id INTEGER PRIMARY KEY)")

    with pytest.raises(ReleaseActivationBlocked, match="confirmation"):
        orchestrator.activate(
            bundle,
            confirmation_plan_sha256="0" * 64,
            registry=AtomicPitReleaseRegistry(tmp_path / "unused.db"),
            actor_user_id=42,
        )
    assert not (tmp_path / "unused.db").exists()

    with pytest.raises(ReleaseActivationBlocked, match="non-release tables"):
        orchestrator.activate(
            bundle,
            confirmation_plan_sha256=report["plan_sha256"],
            registry=AtomicPitReleaseRegistry(registry_path),
            actor_user_id=42,
        )
    with sqlite3.connect(registry_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM experiments"
        ).fetchone()[0] == 0


def test_content_addressed_payload_tampering_is_reported_and_not_used(
    tmp_path: Path,
) -> None:
    orchestrator, bundle = _fixture(tmp_path)
    manifest_sha256 = bundle["artifact_manifest_sha256s"][0]
    manifest_path = (
        tmp_path
        / "approved"
        / "manifests"
        / "sha256"
        / manifest_sha256[:2]
        / f"{manifest_sha256}.json"
    )
    manifest = json.loads(manifest_path.read_text())
    payload_sha256 = manifest["payload_sha256"]
    payload_path = (
        tmp_path
        / "approved"
        / "artifacts"
        / "sha256"
        / payload_sha256[:2]
        / f"{payload_sha256}.json"
    )
    payload_path.write_bytes(payload_path.read_bytes() + b" ")

    report = orchestrator.dry_run(bundle)

    assert report["ready"] is False
    assert "approved_artifact_invalid" in {
        item["code"] for item in report["blockers"]
    }
    assert report["runtime_data_changed"] is False


def test_adjustment_factor_change_requires_effective_action_evidence(
    tmp_path: Path,
) -> None:
    def change_factor(rows: list[dict[str, Any]]) -> None:
        target = next(
            row
            for row in rows
            if row["security_code"] == "000001" and row["trading_date"] == END
        )
        target["adjustment_factor"] = 2
        target["research_adjusted"] = {
            **target["research_adjusted"],
            "open": 20,
            "high": 22,
            "low": 18,
            "close": 20,
        }

    orchestrator, bundle = _fixture(
        tmp_path,
        mutate=("dual_price_ledger", "all_a", change_factor),
    )
    report = orchestrator.dry_run(bundle)

    assert report["ready"] is False
    assert "adjustment_factor_change_unexplained" in {
        item["code"] for item in report["blockers"]
    }


def _authorise_fixture(
    tmp_path: Path,
) -> tuple[ProductionPitReleaseOrchestrator, str, AtomicPitReleaseRegistry]:
    orchestrator, bundle = _fixture(tmp_path)
    report = orchestrator.dry_run(bundle)
    assert report["ready"] is True
    registry = AtomicPitReleaseRegistry(tmp_path / "release-registry.db")
    orchestrator.activate(
        bundle,
        confirmation_plan_sha256=report["plan_sha256"],
        registry=registry,
        actor_user_id=42,
    )
    return orchestrator, report["plan_sha256"], registry


def test_authorised_release_materialises_native_runtime_generation(
    tmp_path: Path,
) -> None:
    orchestrator, plan_sha256, registry = _authorise_fixture(tmp_path)
    runtime_root = tmp_path / "runtime"
    result = ProductionPitReleaseMaterializer(
        registry=registry,
        artifact_store=orchestrator.artifact_store,
        runtime_root=runtime_root,
    ).materialize(plan_sha256)

    assert result["runtime_materialised"] is True
    assert result["idempotent"] is False
    assert len(result["build"]["runtime_bindings"]) == 4
    view = ProductionPitRuntimeReader(runtime_root).load()
    assert view is not None and view.plan_sha256 == plan_sha256
    membership = view.pit_master().query_as_of(
        domain="index_membership", scope_id="csi300", as_of=START
    )
    assert membership["available"] is True
    assert [row["security_code"] for row in membership["records"]] == [
        "000001"
    ]
    with sqlite3.connect(view.runtime_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM price_ledger_runtime_bindings"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM production_pit_artifact_rows"
        ).fetchone()[0] > 0

    repeated = ProductionPitReleaseMaterializer(
        registry=registry,
        artifact_store=orchestrator.artifact_store,
        runtime_root=runtime_root,
    ).materialize(plan_sha256)
    assert repeated["idempotent"] is True
    assert repeated["generation_id"] == result["generation_id"]


def test_materializer_rejects_changed_signed_artifact_before_staging(
    tmp_path: Path,
) -> None:
    orchestrator, plan_sha256, registry = _authorise_fixture(tmp_path)
    manifest_sha256 = registry.load_authorization(plan_sha256)["artifacts"][0][
        "manifest_sha256"
    ]
    manifest_path = (
        tmp_path
        / "approved"
        / "manifests"
        / "sha256"
        / manifest_sha256[:2]
        / f"{manifest_sha256}.json"
    )
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    with pytest.raises(
        ProductionPitMaterializationError,
        match="authorization is invalid",
    ):
        ProductionPitReleaseMaterializer(
            registry=registry,
            artifact_store=orchestrator.artifact_store,
            runtime_root=tmp_path / "runtime",
        ).materialize(plan_sha256)
    assert ProductionPitRuntimeReader(tmp_path / "runtime").load() is None


def test_generation_failure_preserves_old_reader_then_switches_whole_view(
    tmp_path: Path,
) -> None:
    old_orchestrator, old_plan, old_registry = _authorise_fixture(
        tmp_path / "old"
    )
    runtime_root = tmp_path / "runtime"
    ProductionPitReleaseMaterializer(
        registry=old_registry,
        artifact_store=old_orchestrator.artifact_store,
        runtime_root=runtime_root,
    ).materialize(old_plan)
    old_view = ProductionPitRuntimeReader(runtime_root).load()
    assert old_view is not None

    def shift_prices(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            for ledger_name in ("raw", "research_adjusted"):
                for field in ("open", "high", "low", "close"):
                    row[ledger_name][field] = float(
                        row[ledger_name][field]
                    ) + 1

    new_root = tmp_path / "new"
    new_orchestrator, new_bundle = _fixture(
        new_root,
        mutate=("dual_price_ledger", "all_a", shift_prices),
    )
    new_report = new_orchestrator.dry_run(new_bundle)
    assert new_report["ready"] is True
    new_registry = AtomicPitReleaseRegistry(new_root / "release-registry.db")
    new_orchestrator.activate(
        new_bundle,
        confirmation_plan_sha256=new_report["plan_sha256"],
        registry=new_registry,
        actor_user_id=42,
    )

    class FailingStore(GenerationManifestStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root, required_artifacts={"runtime_db", "release_evidence"})
            self.installs = 0

        def _install_staged_artifact(self, source: Path, target: Path) -> None:
            self.installs += 1
            if self.installs == 2:
                raise OSError("fixture publication failure")
            super()._install_staged_artifact(source, target)

    with pytest.raises(OSError, match="fixture publication failure"):
        ProductionPitReleaseMaterializer(
            registry=new_registry,
            artifact_store=new_orchestrator.artifact_store,
            runtime_root=runtime_root,
            generation_store=FailingStore(runtime_root),
        ).materialize(new_report["plan_sha256"])
    after_failure = ProductionPitRuntimeReader(runtime_root).load()
    assert after_failure is not None
    assert after_failure.generation_id == old_view.generation_id
    assert after_failure.plan_sha256 == old_plan

    ProductionPitReleaseMaterializer(
        registry=new_registry,
        artifact_store=new_orchestrator.artifact_store,
        runtime_root=runtime_root,
    ).materialize(new_report["plan_sha256"])
    new_view = ProductionPitRuntimeReader(runtime_root).load()
    assert new_view is not None
    assert new_view.plan_sha256 == new_report["plan_sha256"]
    assert new_view.generation_id != old_view.generation_id
    # A reader that already resolved the prior generation keeps a complete,
    # hash-verified immutable database rather than observing mixed files.
    assert old_view.pit_master().query_as_of(
        domain="index_membership", scope_id="csi300", as_of=START
    )["available"] is True
