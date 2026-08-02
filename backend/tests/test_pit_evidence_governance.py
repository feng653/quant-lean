from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import point_in_time
from backend.data.pit_evidence_governance import (
    ContentAddressedArtifactStore,
    PitEvidenceConflictError,
    PitEvidenceGovernance,
    PitEvidenceIntegrityError,
    PitEvidenceStateError,
)
from backend.data.point_in_time_master import PointInTimeMasterStore
from backend.data.point_in_time_master import PointInTimeValidationError
from backend.data.sources.csindex_history import (
    adjustment_review_proposal,
    parse_adjustment_attachments,
)
from backend.data.sources.csindex_pit import (
    ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION,
    INDEPENDENT_ROW_REVIEW_METHOD,
    TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION,
    ArtifactEvidence,
    CsindexEvidenceError,
    CsindexOfficialCollector,
    archive_review_manifest_sha256,
    build_staging_package,
    canonical_archive_review_rows,
    parse_archive_pages,
    validate_archive_review_decisions,
)
from backend.dependencies import get_current_user
from backend.tests.test_csindex_pit_source import _evidence

_CALENDAR_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x17" * 32)
_CALENDAR_KEY_ID = "fixture-calendar-key"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _calendar_trust_registry() -> dict[str, dict[str, str]]:
    public_key = _CALENDAR_PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        _CALENDAR_KEY_ID: {
            "public_key_base64": base64.b64encode(public_key).decode(),
            "provider": "fixture_exchange",
            "evidence_level": "exchange_authoritative",
        }
    }


def _signed_calendar(trading_days: list[str]) -> dict[str, Any]:
    signed = {
        "schema_version": "authoritative-trading-calendar/v2",
        "source": {
            "provider": "fixture_exchange",
            "evidence_level": "exchange_authoritative",
            "version": "fixture-v1",
            "retrieved_at": "2024-06-18T00:00:00Z",
            "signature_key_id": _CALENDAR_KEY_ID,
        },
        "trading_days": trading_days,
    }
    return {
        **signed,
        "signature": base64.b64encode(
            _CALENDAR_PRIVATE_KEY.sign(_canonical_bytes(signed))
        ).decode(),
    }


def _package_and_artifacts(
    governance: PitEvidenceGovernance | None = None,
    *,
    forge_proposal_hash: bool = False,
    omit_automatic_candidate: bool = False,
) -> tuple[
    dict[str, Any],
    list[ArtifactEvidence],
]:
    anchors, events, archive = _evidence()
    if omit_automatic_candidate:
        original_page = archive.pages[0]
        page_document = json.loads(original_page.payload)
        omitted = next(
            row
            for row in page_document["data"]
            if str(row["id"]) not in {event.announcement_id for event in events}
        )
        omitted["title"] = "关于沪深300指数样本调整结果的公告"
        omitted["theme"] = "指数调样"
        changed_payload = _canonical_bytes(page_document)
        changed_page = ArtifactEvidence(
            role=original_page.role,
            url=original_page.url,
            retrieved_at=original_page.retrieved_at,
            content_sha256=hashlib.sha256(changed_payload).hexdigest(),
            payload=changed_payload,
            request_payload=original_page.request_payload,
            request_sha256=original_page.request_sha256,
        )
        archive = parse_archive_pages(
            pages=[changed_page],
            adjustment_announcement_ids=[
                event.announcement_id for event in events
            ],
            coverage_from=archive.coverage_from,
            coverage_to=archive.coverage_to,
        )
    archive_manifest_sha256 = archive_review_manifest_sha256(archive)
    review_rows = canonical_archive_review_rows(archive.pages)
    target_ids = {event.announcement_id for event in events}
    proposal_sha256_by_id: dict[str, str] = {}
    for event in events:
        parsed_changes, parser_evidence = parse_adjustment_attachments(
            attachments=event.attachments,
            expected_counts=event.announced_counts,
        )
        assert parsed_changes == event.changes
        proposal_sha256_by_id[event.announcement_id] = hashlib.sha256(
            _canonical_bytes(
                adjustment_review_proposal(event, parser_evidence)
            )
        ).hexdigest()
    review_document = {
        "schema_version": "csindex-pit-review-decisions/v2",
        "archive_manifest_sha256": archive_manifest_sha256,
        "archive_review_rows_sha256": hashlib.sha256(
            _canonical_bytes(review_rows)
        ).hexdigest(),
        "reviewer": {
            "user_id": 2,
            "identity": "fixture-independent-reviewer",
            "reviewed_at": "2024-06-18T00:00:00Z",
        },
        "archive_row_decisions": [
            {
                "announcement_id": row["announcement_id"],
                "row_sha256": row["row_sha256"],
                "disposition": (
                    "target_adjustment"
                    if row["announcement_id"] in target_ids
                    else "not_target"
                ),
                "reason": "fixture row independently dispositioned",
            }
            for row in review_rows
        ],
        "event_decisions": [
            {
                "announcement_id": event.announcement_id,
                "decision": "accepted",
                "proposal_sha256": (
                    hashlib.sha256(b"forged-proposal").hexdigest()
                    if forge_proposal_hash
                    else proposal_sha256_by_id[event.announcement_id]
                ),
                "reason": "fixture event independently matched",
            }
            for event in events
        ],
    }
    _dispositions, dispositions_sha256 = validate_archive_review_decisions(
        review_document,
        pages=archive.pages,
        archive_manifest_sha256=archive_manifest_sha256,
    )
    review_payload = _canonical_bytes(review_document)
    review_evidence = {
        "schema_version": ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION,
        "role": "review_decisions",
        "review_method": INDEPENDENT_ROW_REVIEW_METHOD,
        "content_sha256": hashlib.sha256(review_payload).hexdigest(),
        "reviewed_changes_sha256": "",
        "archive_manifest_sha256": archive_manifest_sha256,
        "archive_review_rows_sha256": review_document[
            "archive_review_rows_sha256"
        ],
        "archive_row_dispositions_sha256": dispositions_sha256,
    }
    calendar_document = _signed_calendar(["2024-06-14", "2024-06-17"])
    calendar_payload = _canonical_bytes(calendar_document)
    calendar_evidence = {
        "schema_version": TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION,
        "role": "trading_calendar",
        "provider": "fixture_exchange",
        "evidence_level": "exchange_authoritative",
        "version": "fixture-v1",
        "retrieved_at": "2024-06-18T00:00:00Z",
        "signature_key_id": _CALENDAR_KEY_ID,
        "signed_payload_sha256": hashlib.sha256(
            _canonical_bytes(
                {
                    key: calendar_document[key]
                    for key in ("schema_version", "source", "trading_days")
                }
            )
        ).hexdigest(),
        "content_sha256": hashlib.sha256(calendar_payload).hexdigest(),
        "sessions_sha256": hashlib.sha256(
            _canonical_bytes(calendar_document["trading_days"])
        ).hexdigest(),
        "sessions": calendar_document["trading_days"],
    }
    package = build_staging_package(
        anchors=anchors,
        announcements=events,
        archive=archive,
        trading_days=[date(2024, 6, 14), date(2024, 6, 17)],
        coverage_from=date(2024, 6, 1),
        coverage_to=date(2024, 6, 17),
        trading_calendar_evidence=calendar_evidence,
        review_evidence=review_evidence,
    )
    if governance is not None:
        governance.record_auxiliary_artifact(
            kind="review_decisions",
            payload=review_payload,
            expected_sha256=review_evidence["content_sha256"],
            actor_user_id=2,
        )
        governance.record_auxiliary_artifact(
            kind="trading_calendar",
            payload=calendar_payload,
            expected_sha256=calendar_evidence["content_sha256"],
            actor_user_id=1,
        )
    artifacts = (
        [item.artifact for item in anchors.values()]
        + list(archive.pages)
        + [
            artifact
            for event in events
            for artifact in (event.announcement, *event.attachments)
        ]
    )
    return package, artifacts


def _governance(tmp_path: Path, *, master: Any | None = None):
    root = tmp_path / "pit_evidence"
    return PitEvidenceGovernance(
        root=root,
        database_path=root / "governance.db",
        master_store=master
        or PointInTimeMasterStore(tmp_path / "experiment.db"),
        trusted_calendar_keys=_calendar_trust_registry(),
    )


def _attestations() -> dict[str, Any]:
    return {
        "schema_version": "pit-evidence-attestation/v1",
        "all_adjustment_rows_reviewed": True,
        "archive_completeness_reviewed": True,
        "source_terms_acknowledged": True,
        "local_research_only": True,
        "redistribution_not_authorized": True,
    }


def _stage_approved(
    governance: PitEvidenceGovernance,
) -> tuple[str, dict[str, Any]]:
    package, artifacts = _package_and_artifacts(governance)
    staged = governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )
    governance.decide(
        package_id=staged["package_id"],
        expected_revision=1,
        decision="approved",
        actor_user_id=3,
        reason="offline fixture evidence reviewed",
        attestations=_attestations(),
    )
    return staged["package_id"], package


def test_content_store_is_atomic_content_addressed_and_fails_for_tamper(
    tmp_path: Path,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "managed")
    payload = b"official immutable bytes"
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    first = store.put(payload, expected_sha256=digest)
    second = store.put(payload, expected_sha256=digest)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert store.read(digest) == payload
    assert store._path(digest).relative_to(store.root).parts[:2] == (
        "artifacts",
        "sha256",
    )
    with pytest.raises(PitEvidenceIntegrityError, match="digest is invalid"):
        store.read("../private")

    store._path(digest).write_bytes(b"tampered")
    with pytest.raises(PitEvidenceIntegrityError, match="integrity mismatch"):
        store.read(digest)

def test_content_store_portable_publish_preserves_no_replace_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "managed")
    monkeypatch.setattr(
        store,
        "_supports_secure_directory_fd",
        lambda: False,
    )
    payload = b"portable immutable bytes"
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    first = store.put(payload, expected_sha256=digest)
    second = store.put(payload, expected_sha256=digest)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert store.read(digest) == payload
    assert not list(store._path(digest).parent.glob(".*.tmp"))

def test_content_store_rejects_symbolic_link_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    os.symlink(actual, linked)
    with pytest.raises(PitEvidenceIntegrityError, match="symbolic link"):
        ContentAddressedArtifactStore(linked)


def test_official_collector_streams_bounded_raw_responses() -> None:
    async def scenario() -> None:
        detail = {
            "code": "200",
            "data": {
                "id": 15267,
                "enclosureList": [
                    {
                        "fileUrl": (
                            "https://oss-ch.csindex.com.cn/notice/fixture.pdf"
                        )
                    }
                ],
            },
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oss-ch.csindex.com.cn":
                return httpx.Response(200, content=b"%PDF reviewed manually")
            return httpx.Response(200, json=detail)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            collector = CsindexOfficialCollector(client)
            announcement, attachments = await collector.fetch_announcement(
                "15267"
            )
        assert announcement.announcement_id == "15267"
        assert len(attachments) == 1
        assert attachments[0].payload.startswith(b"%PDF")

        async def oversized(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": str(26 * 1024 * 1024)},
                content=b"x",
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(oversized)
        ) as client:
            collector = CsindexOfficialCollector(client)
            with pytest.raises(CsindexEvidenceError, match="collection failed"):
                await collector.fetch_archive_page(page=1)

    asyncio.run(scenario())


def test_stage_approve_import_and_retry_are_end_to_end_idempotent(
    tmp_path: Path,
) -> None:
    master = PointInTimeMasterStore(tmp_path / "experiment.db")
    governance = _governance(tmp_path, master=master)
    package, artifacts = _package_and_artifacts(governance)
    staged = governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )
    repeated = governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )
    assert staged["status"] == "pending"
    assert repeated["idempotent"] is True

    approved = governance.decide(
        package_id=staged["package_id"],
        expected_revision=1,
        decision="approved",
        actor_user_id=3,
        reason="all official evidence reviewed",
        attestations=_attestations(),
    )
    assert approved["revision"] == 2
    imported = governance.import_approved_package(
        package_id=staged["package_id"],
        actor_user_id=2,
    )
    retried = governance.import_approved_package(
        package_id=staged["package_id"],
        actor_user_id=2,
    )
    assert imported["status"] == "imported"
    assert len(imported["imports"]) == 4
    assert retried["idempotent"] is True
    events = governance.get_events(staged["package_id"])["events"]
    assert [item["event_type"] for item in events] == [
        "package_staged",
        "package_approved",
        "package_imported",
    ]
    assert events[1]["event"]["attestations"] == _attestations()
    assert imported["decision_attestations"] == _attestations()
    before = master.query_as_of(
        domain="index_membership",
        scope_id="csi300",
        as_of="2024-06-14",
    )
    after = master.query_as_of(
        domain="index_membership",
        scope_id="csi300",
        as_of="2024-06-17",
    )
    assert "900001" in {item["security_code"] for item in before["records"]}
    assert "000300" in {item["security_code"] for item in after["records"]}


def test_unattested_history_can_stage_but_never_approve_or_import(
    tmp_path: Path,
) -> None:
    master = PointInTimeMasterStore(tmp_path / "experiment.db")
    governance = _governance(tmp_path, master=master)
    anchors, events, archive = _evidence()
    package = build_staging_package(
        anchors=anchors,
        announcements=events,
        archive=archive,
        trading_days=[date(2024, 6, 14), date(2024, 6, 17)],
        coverage_from=date(2024, 6, 1),
        coverage_to=date(2024, 6, 17),
    )
    artifacts = (
        [item.artifact for item in anchors.values()]
        + list(archive.pages)
        + [
            artifact
            for event in events
            for artifact in (event.announcement, *event.attachments)
        ]
    )
    staged = governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )
    with pytest.raises(PitEvidenceStateError, match="authoritative calendar"):
        governance.decide(
            package_id=staged["package_id"],
            expected_revision=1,
            decision="approved",
            actor_user_id=2,
            reason="a global attestation must not bypass row evidence",
            attestations=_attestations(),
        )
    assert governance.get_package(staged["package_id"])["status"] == "pending"
    master.initialize_schema()
    with sqlite3.connect(master.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_master_batches"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_master_intervals"
        ).fetchone()[0] == 0


def test_global_archive_review_boolean_cannot_replace_row_dispositions() -> None:
    _anchors, events, archive = _evidence()
    legacy = {
        "schema_version": "csindex-pit-review-decisions/v1",
        "archive_manifest_sha256": archive_review_manifest_sha256(archive),
        "archive_review_rows_sha256": hashlib.sha256(b"legacy").hexdigest(),
        "archive_rows_all_reviewed": True,
        "reviewer": {
            "identity": "legacy-reviewer",
            "reviewed_at": "2024-06-18T00:00:00Z",
        },
        "event_decisions": [
            {
                "announcement_id": events[0].announcement_id,
                "decision": "accepted",
                "proposal_sha256": "a" * 64,
                "reason": "global boolean only",
            }
        ],
    }
    with pytest.raises(CsindexEvidenceError, match="not bound"):
        validate_archive_review_decisions(
            legacy,
            pages=archive.pages,
            archive_manifest_sha256=archive_review_manifest_sha256(archive),
        )


def test_review_decision_must_bind_replayed_event_proposal(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    package, artifacts = _package_and_artifacts(
        governance,
        forge_proposal_hash=True,
    )

    with pytest.raises(
        PitEvidenceIntegrityError,
        match="does not match replayed proposal",
    ):
        governance.stage_package(
            package=package,
            artifacts=artifacts,
            actor_user_id=1,
        )


def test_direct_package_cannot_exclude_automatic_adjustment_candidate(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    package, artifacts = _package_and_artifacts(
        governance,
        omit_automatic_candidate=True,
    )

    with pytest.raises(
        PitEvidenceIntegrityError,
        match="automatic target candidate cannot be excluded",
    ):
        governance.stage_package(
            package=package,
            artifacts=artifacts,
            actor_user_id=1,
        )


def test_self_declared_calendar_provider_is_not_authoritative(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    document = {
        "schema_version": "authoritative-trading-calendar/v2",
        "source": {
            "provider": "self_declared_exchange",
            "evidence_level": "exchange_authoritative",
            "version": "invented-v1",
            "retrieved_at": "2024-06-18T00:00:00Z",
            "signature_key_id": "unregistered-key",
        },
        "trading_days": ["2024-06-14", "2024-06-17"],
        "signature": base64.b64encode(b"x" * 64).decode(),
    }
    payload = _canonical_bytes(document)

    with pytest.raises(
        PitEvidenceIntegrityError,
        match="signer is not governed and trusted",
    ):
        governance.record_auxiliary_artifact(
            kind="trading_calendar",
            payload=payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            actor_user_id=1,
        )


def test_review_stager_and_approver_must_be_distinct_users(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    package, artifacts = _package_and_artifacts(governance)
    with pytest.raises(PitEvidenceStateError, match="distinct authenticated"):
        governance.stage_package(
            package=package,
            artifacts=artifacts,
            actor_user_id=2,
        )

    staged = governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )
    with pytest.raises(PitEvidenceStateError, match="distinct authenticated"):
        governance.decide(
            package_id=staged["package_id"],
            expected_revision=1,
            decision="approved",
            actor_user_id=2,
            reason="reviewer cannot self-approve",
            attestations=_attestations(),
        )
    with pytest.raises(PitEvidenceStateError, match="distinct from its stager"):
        governance.decide(
            package_id=staged["package_id"],
            expected_revision=1,
            decision="approved",
            actor_user_id=1,
            reason="stager cannot self-approve",
            attestations=_attestations(),
        )


def test_canonical_csi_scope_cannot_bypass_governance_with_other_provider(
    tmp_path: Path,
) -> None:
    package, _artifacts = _package_and_artifacts()
    document = {
        **package["imports"][0],
        "source": {
            **package["imports"][0]["source"],
            "provider": "other",
            "evidence_level": "public_cross_validated",
        },
    }
    with pytest.raises(
        PointInTimeValidationError,
        match="governance approval",
    ):
        PointInTimeMasterStore(tmp_path / "master.db").import_batch(
            **document,
            imported_by_user_id=1,
        )


def test_package_import_records_must_replay_from_retained_evidence(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    package, artifacts = _package_and_artifacts(governance)
    package["imports"][0]["records"][0]["member_name"] = "forged"
    with pytest.raises(PitEvidenceIntegrityError, match="retained evidence"):
        governance.stage_package(
            package=package,
            artifacts=artifacts,
            actor_user_id=1,
        )


def test_direct_governance_service_rejects_oversized_package_before_writes(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    package, artifacts = _package_and_artifacts()
    package["oversized_padding"] = "x" * (20 * 1024 * 1024)
    with pytest.raises(PitEvidenceIntegrityError, match="20 MiB"):
        governance.stage_package(
            package=package,
            artifacts=artifacts,
            actor_user_id=1,
        )
    with sqlite3.connect(governance.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_evidence_artifacts"
        ).fetchone()[0] == 0


def test_decision_is_compare_and_swap_and_rejection_blocks_import(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    package, artifacts = _package_and_artifacts(governance)
    staged = governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )

    def approve(actor: int) -> str:
        try:
            governance.decide(
                package_id=staged["package_id"],
                expected_revision=1,
                decision="approved",
                actor_user_id=actor,
                reason=f"review {actor}",
                attestations=_attestations(),
            )
            return "approved"
        except PitEvidenceConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(approve, (3, 4)))
    assert sorted(outcomes) == ["approved", "conflict"]

    rejected_governance = _governance(tmp_path / "rejected")
    package, artifacts = _package_and_artifacts(rejected_governance)
    rejected = rejected_governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )
    rejected_governance.decide(
        package_id=rejected["package_id"],
        expected_revision=1,
        decision="rejected",
        actor_user_id=2,
        reason="attachment rows not independently reviewed",
    )
    with pytest.raises(PitEvidenceStateError, match="approved"):
        rejected_governance.import_approved_package(
            package_id=rejected["package_id"],
            actor_user_id=2,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {
            **_attestations(),
            "all_adjustment_rows_reviewed": False,
        },
        {
            key: value
            for key, value in _attestations().items()
            if key != "archive_completeness_reviewed"
        },
        {
            **_attestations(),
            "source_terms_acknowledged": False,
        },
        {
            **_attestations(),
            "local_research_only": False,
        },
        {
            **_attestations(),
            "redistribution_not_authorized": False,
        },
    ],
)
def test_approval_requires_every_structured_attestation(
    tmp_path: Path,
    invalid: dict[str, Any] | None,
) -> None:
    governance = _governance(tmp_path)
    package, artifacts = _package_and_artifacts(governance)
    staged = governance.stage_package(
        package=package,
        artifacts=artifacts,
        actor_user_id=1,
    )
    with pytest.raises(PitEvidenceStateError, match="attestations"):
        governance.decide(
            package_id=staged["package_id"],
            expected_revision=1,
            decision="approved",
            actor_user_id=2,
            reason="free text is not sufficient",
            attestations=invalid,
        )
    assert governance.get_package(staged["package_id"])["status"] == "pending"


def test_import_reverifies_artifacts_and_immutable_package(
    tmp_path: Path,
) -> None:
    governance = _governance(tmp_path)
    package_id, package = _stage_approved(governance)
    digest = package["evidence_manifest"]["anchors"][0]["artifact"][
        "content_sha256"
    ]
    governance.artifacts._path(digest).write_bytes(b"tampered")
    with pytest.raises(PitEvidenceIntegrityError, match="integrity mismatch"):
        governance.import_approved_package(
            package_id=package_id,
            actor_user_id=2,
        )

    missing_governance = _governance(tmp_path / "missing")
    package_id, package = _stage_approved(missing_governance)
    digest = package["evidence_manifest"]["anchors"][0]["artifact"][
        "content_sha256"
    ]
    missing_governance.artifacts._path(digest).unlink()
    with pytest.raises(PitEvidenceIntegrityError, match="unavailable"):
        missing_governance.import_approved_package(
            package_id=package_id,
            actor_user_id=2,
        )

    changed_governance = _governance(tmp_path / "changed")
    package_id, _package = _stage_approved(changed_governance)
    with sqlite3.connect(changed_governance.database_path) as connection:
        connection.execute("DROP TRIGGER pit_evidence_package_payload_immutable")
        connection.execute(
            """
            UPDATE pit_evidence_packages SET package_json='{}'
            WHERE package_id=?
            """,
            (package_id,),
        )
    with pytest.raises(PitEvidenceIntegrityError):
        changed_governance.import_approved_package(
            package_id=package_id,
            actor_user_id=2,
        )

    attestation_governance = _governance(tmp_path / "attestation_changed")
    package_id, _package = _stage_approved(attestation_governance)
    changed = {
        **_attestations(),
        "redistribution_not_authorized": False,
    }
    with sqlite3.connect(attestation_governance.database_path) as connection:
        connection.execute("DROP TRIGGER pit_evidence_attestations_once")
        connection.execute(
            """
            UPDATE pit_evidence_packages
            SET decision_attestations_json=?
            WHERE package_id=?
            """,
            (
                json.dumps(changed, sort_keys=True),
                package_id,
            ),
        )
    with pytest.raises(PitEvidenceIntegrityError, match="attestations"):
        attestation_governance.import_approved_package(
            package_id=package_id,
            actor_user_id=2,
        )


def test_crash_after_master_write_resumes_without_duplicate_batches(
    tmp_path: Path,
) -> None:
    underlying = PointInTimeMasterStore(tmp_path / "experiment.db")

    class CrashOnce:
        crashed = False

        def import_batch(self, **kwargs: Any) -> dict[str, Any]:
            result = underlying.import_batch(**kwargs)
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("simulated crash after durable master write")
            return result

        def activate_governed_csi_package(
            self,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return underlying.activate_governed_csi_package(**kwargs)

    governance = _governance(tmp_path, master=CrashOnce())
    package_id, _package = _stage_approved(governance)
    with pytest.raises(RuntimeError, match="simulated crash"):
        governance.import_approved_package(
            package_id=package_id,
            actor_user_id=2,
        )
    assert governance.get_package(package_id)["status"] == "approved"
    assert underlying.query_as_of(
        domain="index_membership",
        scope_id="csi300",
        as_of="2024-06-17",
    )["available"] is False
    completed = governance.import_approved_package(
        package_id=package_id,
        actor_user_id=2,
    )
    assert completed["status"] == "imported"
    with sqlite3.connect(underlying.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_master_batches"
        ).fetchone()[0] == 4


def test_partial_multiscope_failure_never_activates_any_scope(
    tmp_path: Path,
) -> None:
    underlying = PointInTimeMasterStore(tmp_path / "experiment.db")

    class FailThird:
        calls = 0

        def import_batch(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("permanent third-scope conflict")
            return underlying.import_batch(**kwargs)

        def activate_governed_csi_package(
            self,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return underlying.activate_governed_csi_package(**kwargs)

    governance = _governance(tmp_path, master=FailThird())
    package_id, _package = _stage_approved(governance)
    with pytest.raises(RuntimeError, match="third-scope"):
        governance.import_approved_package(
            package_id=package_id,
            actor_user_id=2,
        )
    for scope_id in ("csi300", "csi500", "csi800", "csi1000"):
        assert underlying.query_as_of(
            domain="index_membership",
            scope_id=scope_id,
            as_of="2024-06-17",
        )["available"] is False
    with sqlite3.connect(underlying.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_master_governed_activations"
        ).fetchone()[0] == 0


def _artifact_body(artifact: ArtifactEvidence) -> dict[str, Any]:
    result = {
        **artifact.manifest(),
        "payload_base64": base64.b64encode(artifact.payload).decode(),
        "request_payload_base64": (
            base64.b64encode(artifact.request_payload).decode()
            if artifact.request_payload is not None
            else None
        ),
    }
    result.pop("parser_version")
    return result


def test_governance_api_requires_admin_and_direct_csindex_import_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governance = _governance(tmp_path)
    monkeypatch.setattr(point_in_time, "_governance", lambda: governance)
    monkeypatch.setattr(
        point_in_time,
        "_store",
        lambda **_kwargs: governance.master_store,
    )
    current_user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:update"],
    }
    app = FastAPI()
    app.include_router(point_in_time.router)
    app.dependency_overrides[get_current_user] = lambda: current_user
    package, artifacts = _package_and_artifacts(governance)
    request = {"package": package}
    with TestClient(app) as client:
        denied = client.post(
            "/api/data/point-in-time/governance/packages",
            json=request,
        )
        current_user = {
            "id": 1,
            "is_admin": True,
            "permissions": [],
        }
        uploaded = [
            client.post(
                "/api/data/point-in-time/governance/artifacts",
                json=_artifact_body(artifact),
            )
            for artifact in artifacts
        ]
        staged = client.post(
            "/api/data/point-in-time/governance/packages",
            json=request,
        )
        direct = client.post(
            "/api/data/point-in-time/imports",
            json=package["imports"][0],
        )
        package_id = staged.json()["data"]["package_id"]
        missing_attestations = client.post(
            f"/api/data/point-in-time/governance/packages/{package_id}/decision",
            json={
                "expected_revision": 1,
                "decision": "approved",
                "reason": "text alone",
            },
        )
        invalid_attestations = client.post(
            f"/api/data/point-in-time/governance/packages/{package_id}/decision",
            json={
                "expected_revision": 1,
                "decision": "approved",
                "reason": "one false declaration",
                "attestations": {
                    **_attestations(),
                    "local_research_only": False,
                },
            },
        )
        current_user = {
            "id": 3,
            "is_admin": True,
            "permissions": [],
        }
        approved = client.post(
            f"/api/data/point-in-time/governance/packages/{package_id}/decision",
            json={
                "expected_revision": 1,
                "decision": "approved",
                "reason": "fixture approved",
                "attestations": _attestations(),
            },
        )
        imported = client.post(
            f"/api/data/point-in-time/governance/packages/{package_id}/import"
        )
        events = client.get(
            f"/api/data/point-in-time/governance/packages/{package_id}/events"
        )
    assert denied.status_code == 403
    assert all(item.status_code == 200 for item in uploaded)
    assert staged.status_code == 200
    assert direct.status_code == 409
    assert direct.json()["detail"]["code"] == (
        "pit_evidence_governance_required"
    )
    assert missing_attestations.status_code == 422
    assert invalid_attestations.status_code == 422
    assert approved.status_code == 200
    assert imported.status_code == 200
    assert imported.json()["data"]["status"] == "imported"
    assert events.status_code == 200
    assert len(events.json()["data"]["events"]) == 3
