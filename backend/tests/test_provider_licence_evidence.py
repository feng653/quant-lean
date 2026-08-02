from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.provider_licence_evidence import router
from backend.config import settings
from backend.data.production_pit_release import (
    AtomicPitReleaseRegistry,
    ReleaseActivationBlocked,
)
from backend.data.provider_licence_evidence import (
    LicenceEvidenceConflict,
    LicenceEvidenceValidationError,
    ProviderLicenceEvidenceRegistry,
)
from backend.dependencies import get_current_user


DOCUMENT_SHA256 = "a" * 64


def _register(
    registry: ProviderLicenceEvidenceRegistry,
    *,
    actor_user_id: int = 10,
    reference: str | None = None,
) -> dict:
    return registry.register(
        provider_id="tushare",
        source_scope="pit_history",
        licence_scope="local_research_retention",
        document_sha256=DOCUMENT_SHA256,
        document_size_bytes=1024,
        document_reference=reference,
        claimed_effective_from="2026-01-01",
        claimed_effective_to="2027-01-01",
        claimed_available_from="2016-01-01",
        claimed_available_to="2026-06-30",
        obtained_at="2026-08-02T02:00:00+08:00",
        actor_user_id=actor_user_id,
    )


def test_register_redacts_url_secret_path_and_query(tmp_path: Path) -> None:
    path = tmp_path / "licence.db"
    registry = ProviderLicenceEvidenceRegistry(path)
    secret_reference = (
        "https://account:password@docs.example.test/clients/xuhe/receipt.pdf"
        "?token=super-secret&download=1"
    )

    result = _register(registry, reference=secret_reference)

    assert result["state"] == "unverified"
    assert result["production_release_authorized"] is False
    descriptor = result["record"]["document_reference"]
    assert descriptor["kind"] == "remote_url"
    assert descriptor["origin"] == "https://docs.example.test"
    assert len(descriptor["reference_sha256"]) == 64
    same_location_new_token = _register(
        ProviderLicenceEvidenceRegistry(tmp_path / "second.db"),
        reference=(
            "https://docs.example.test/clients/xuhe/receipt.pdf"
            "?token=a-completely-different-secret"
        ),
    )
    assert (
        same_location_new_token["record"]["document_reference"]
        == descriptor
    )
    raw_database = path.read_bytes()
    for sensitive in (
        b"account",
        b"password",
        b"clients/xuhe",
        b"receipt.pdf",
        b"super-secret",
        b"download",
    ):
        assert sensitive not in raw_database
    assert path.stat().st_mode & 0o777 == 0o600


def test_local_reference_is_fingerprint_only(tmp_path: Path) -> None:
    path = tmp_path / "licence.db"
    local_reference = "/Users/xuhe/private/tushare-contract-receipt.pdf"

    result = _register(
        ProviderLicenceEvidenceRegistry(path),
        reference=local_reference,
    )

    descriptor = result["record"]["document_reference"]
    assert descriptor["kind"] == "local_or_opaque"
    assert set(descriptor) == {"kind", "reference_sha256"}
    assert local_reference.encode() not in path.read_bytes()


def test_review_requires_independent_actor_and_matching_document(tmp_path: Path) -> None:
    registry = ProviderLicenceEvidenceRegistry(tmp_path / "licence.db")
    registered = _register(registry, actor_user_id=10)

    with pytest.raises(LicenceEvidenceValidationError, match="independent"):
        registry.review(
            record_sha256=registered["record_sha256"],
            document_sha256=DOCUMENT_SHA256,
            decision="approved",
            reason_code="verified_external_receipt",
            reviewer_user_id=10,
        )
    with pytest.raises(LicenceEvidenceValidationError, match="digest"):
        registry.review(
            record_sha256=registered["record_sha256"],
            document_sha256="b" * 64,
            decision="approved",
            reason_code="verified_external_receipt",
            reviewer_user_id=11,
        )

    approved = registry.review(
        record_sha256=registered["record_sha256"],
        document_sha256=DOCUMENT_SHA256,
        decision="approved",
        reason_code="verified_external_receipt",
        reviewer_user_id=11,
    )

    assert approved["state"] == "approved"
    assert approved["review"]["reviewed_by_user_id"] == 11
    assert approved["production_release_authorized"] is False
    assert approved["review"]["production_release_authorized"] is False
    with pytest.raises(LicenceEvidenceConflict, match="immutable review"):
        registry.review(
            record_sha256=registered["record_sha256"],
            document_sha256=DOCUMENT_SHA256,
            decision="rejected",
            reason_code="changed_mind",
            reviewer_user_id=12,
        )


def test_database_triggers_reject_update_and_delete(tmp_path: Path) -> None:
    path = tmp_path / "licence.db"
    registered = _register(ProviderLicenceEvidenceRegistry(path))

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE licence_evidence_records SET provider_id='other' "
                "WHERE record_sha256=?",
                (registered["record_sha256"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM licence_evidence_records WHERE record_sha256=?",
                (registered["record_sha256"],),
            )


def test_read_time_digest_verification_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "licence.db"
    registry = ProviderLicenceEvidenceRegistry(path)
    registered = _register(registry)
    # Simulate a privileged attacker bypassing the trigger definition itself.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER licence_evidence_records_no_update")
        payload = json.loads(
            connection.execute(
                "SELECT record_json FROM licence_evidence_records"
            ).fetchone()[0]
        )
        payload["provider_id"] = "tampered"
        connection.execute(
            "UPDATE licence_evidence_records SET record_json=?",
            (json.dumps(payload),),
        )

    with pytest.raises(Exception, match="immutability guard|integrity mismatch"):
        registry.get(registered["record_sha256"])


def test_licence_registry_cannot_act_as_release_registry(tmp_path: Path) -> None:
    path = tmp_path / "licence.db"
    _register(ProviderLicenceEvidenceRegistry(path))

    with pytest.raises(ReleaseActivationBlocked, match="non-release tables"):
        AtomicPitReleaseRegistry(path).load_authorization("0" * 64)


def _api_app(user: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def current_user() -> dict:
        return user

    app.dependency_overrides[get_current_user] = current_user
    return app


def _body(reference: str | None = None) -> dict:
    return {
        "provider_id": "tushare",
        "source_scope": "pit_history",
        "licence_scope": "local_research_retention",
        "document_sha256": DOCUMENT_SHA256,
        "document_size_bytes": 1024,
        "document_reference": reference,
        "claimed_effective_from": "2026-01-01",
        "claimed_effective_to": "2027-01-01",
        "claimed_available_from": "2016-01-01",
        "claimed_available_to": "2026-06-30",
        "obtained_at": "2026-08-02T02:00:00+08:00",
    }


def test_api_is_admin_permission_only_and_never_echoes_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "PIT_LICENCE_EVIDENCE_DB",
        str(tmp_path / "licence.db"),
    )
    operator = {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:update"],
    }
    with TestClient(_api_app(operator)) as client:
        denied = client.post("/api/data/provider-licence-evidence/records", json=_body())
    assert denied.status_code == 403

    reference = "https://docs.example.test/secret/path?token=private"
    admin = {"id": 10, "is_admin": True, "permissions": []}
    with TestClient(_api_app(admin)) as client:
        response = client.post(
            "/api/data/provider-licence-evidence/records",
            json=_body(reference),
        )
        contract = client.get("/api/data/provider-licence-evidence/contract")
    assert response.status_code == 201
    serialized = response.text
    assert reference not in serialized
    assert "token=private" not in serialized
    assert response.json()["data"]["state"] == "unverified"
    assert contract.status_code == 200
    assert contract.json()["data"]["production_release_authorized"] is False


def test_api_review_uses_authenticated_actor_and_is_append_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "PIT_LICENCE_EVIDENCE_DB",
        str(tmp_path / "licence.db"),
    )
    with TestClient(
        _api_app({"id": 10, "is_admin": True, "permissions": []})
    ) as client:
        record = client.post(
            "/api/data/provider-licence-evidence/records", json=_body()
        ).json()["data"]
        own_review = client.post(
            f"/api/data/provider-licence-evidence/records/{record['record_sha256']}/reviews",
            json={
                "document_sha256": DOCUMENT_SHA256,
                "decision": "approved",
                "reason_code": "verified_external_receipt",
            },
        )
    assert own_review.status_code == 422

    with TestClient(
        _api_app({"id": 11, "is_admin": True, "permissions": []})
    ) as client:
        review = client.post(
            f"/api/data/provider-licence-evidence/records/{record['record_sha256']}/reviews",
            json={
                "document_sha256": DOCUMENT_SHA256,
                "decision": "approved",
                "reason_code": "verified_external_receipt",
            },
        )
        listed = client.get("/api/data/provider-licence-evidence/records")
    assert review.status_code == 200
    assert review.json()["data"]["state"] == "approved"
    assert listed.json()["data"]["items"][0]["state"] == "approved"
    assert listed.json()["data"]["production_release_authorized"] is False
