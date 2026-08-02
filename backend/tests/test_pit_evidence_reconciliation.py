from __future__ import annotations

import copy
from typing import Any

import pytest

from backend.data.pit_evidence_reconciliation import (
    EvidenceReconciliationError,
    FOUR_INDEX_COUNTS,
    RECONCILIATION_INPUT_SCHEMA,
    reconcile_pit_evidence,
    verify_reconciliation_report,
)


def _evidence(
    evidence_id: str,
    *,
    role: str,
    effective_at: str,
    ordinal: int,
) -> dict[str, Any]:
    is_official = role == "official"
    return {
        "evidence_id": evidence_id,
        "provider": "cninfo_official" if is_official else "tushare",
        "authority_level": "official" if is_official else "candidate",
        "classification": "official" if is_official else "quarantine",
        "artifact_sha256": f"{ordinal % 16:x}" * 64,
        "manifest_sha256": f"{(ordinal + 1) % 16:x}" * 64,
        "effective_at": effective_at,
        "available_at": "2024-01-01T08:00:00Z",
        "ingested_at": "2024-01-01T09:00:00Z",
        "available_at_evidence": (
            "official_published_at" if is_official else "provider_field"
        ),
        "revision": f"revision-{ordinal}",
        "revision_evidence": (
            "official_document_version" if is_official else "provider_field"
        ),
    }


def _valid_document() -> dict[str, Any]:
    action_types = (
        "cash_dividend",
        "stock_dividend",
        "capitalisation",
        "rights_issue",
        "split",
    )
    corporate_action_cases: list[dict[str, Any]] = []
    for index in range(20):
        code = f"{600000 + index:06d}"
        effective_at = f"2024-{index // 2 + 1:02d}-{index % 2 + 10:02d}T00:00:00Z"
        official = _evidence(
            f"corp-official-{index}",
            role="official",
            effective_at=effective_at,
            ordinal=index + 1,
        )
        candidate = _evidence(
            f"corp-candidate-{index}",
            role="candidate",
            effective_at=effective_at,
            ordinal=index + 41,
        )
        terms = {
            "cash_per_share": (index + 1) / 100,
            "tax_basis": "pre_tax",
        }
        common = {
            "security_code": code,
            "action_type": action_types[index % len(action_types)],
            "terms": terms,
        }
        official.update(common)
        candidate.update(copy.deepcopy(common))
        corporate_action_cases.append(
            {
                "case_id": f"corporate-action-{index + 1:02d}",
                "official": official,
                "candidate": candidate,
            }
        )

    index_member_events: list[dict[str, Any]] = []
    for event_index, (scope_id, expected_count) in enumerate(
        FOUR_INDEX_COUNTS.items(), start=1
    ):
        first_code = event_index * 10_000
        before_members = [
            f"{first_code + position:06d}" for position in range(expected_count)
        ]
        addition = f"{950000 + event_index:06d}"
        after_members = [*before_members[1:], addition]
        official = _evidence(
            f"index-official-{scope_id}",
            role="official",
            effective_at="2024-06-17T00:00:00Z",
            ordinal=80 + event_index,
        )
        official.update(additions=[addition], removals=[before_members[0]])
        before = _evidence(
            f"index-before-{scope_id}",
            role="candidate",
            effective_at="2024-06-14T00:00:00Z",
            ordinal=90 + event_index,
        )
        before["members"] = before_members
        after = _evidence(
            f"index-after-{scope_id}",
            role="candidate",
            effective_at="2024-06-17T00:00:00Z",
            ordinal=100 + event_index,
        )
        after["members"] = after_members
        index_member_events.append(
            {
                "event_id": f"fixture-adjustment-{scope_id}",
                "scope_id": scope_id,
                "official": official,
                "candidate_before": before,
                "candidate_after": after,
            }
        )

    return {
        "schema_version": RECONCILIATION_INPUT_SCHEMA,
        "classification": "quarantine",
        "prepared_at": "2026-08-02T08:00:00Z",
        "corporate_action_cases": corporate_action_cases,
        "index_member_events": index_member_events,
    }


def test_reconciliation_matches_twenty_actions_and_four_index_events() -> None:
    report = reconcile_pit_evidence(_valid_document())

    assert report["reconciliation_passed"] is True
    assert report["production_pit_ready"] is False
    assert report["production_import_permitted"] is False
    assert report["activation_permitted"] is False
    actions = report["checks"]["corporate_actions"]
    assert actions["required"] == 20
    assert actions["observed"] == 20
    assert actions["matched"] == 20
    assert actions["matched_by_type"] == {
        "capitalisation": 4,
        "cash_dividend": 4,
        "rights_issue": 4,
        "split": 4,
        "stock_dividend": 4,
    }
    assert report["checks"]["four_index_member_events"]["matched_by_scope"] == {
        "csi1000": 1,
        "csi300": 1,
        "csi500": 1,
        "csi800": 1,
    }
    assert report["checks"]["bitemporal"]["observations"] == 52
    assert report["findings"] == []
    assert verify_reconciliation_report(report) == report


def test_corporate_action_mismatch_fails_closed_without_exposing_values() -> None:
    document = _valid_document()
    document["corporate_action_cases"][4]["candidate"]["terms"][
        "cash_per_share"
    ] = "provider-confidential-value"

    report = reconcile_pit_evidence(document)

    assert report["reconciliation_passed"] is False
    assert report["production_import_permitted"] is False
    finding = next(
        item for item in report["findings"] if item["code"] == "corporate_action_mismatch"
    )
    assert "terms" in finding["detail"]
    assert "provider-confidential-value" not in str(report)


def test_missing_candidate_available_at_is_unknown_and_fails_closed() -> None:
    document = _valid_document()
    document["corporate_action_cases"][0]["candidate"]["available_at"] = None

    report = reconcile_pit_evidence(document)

    assert report["reconciliation_passed"] is False
    assert any(
        item["code"] == "corporate_action_unknown" for item in report["findings"]
    )
    assert report["checks"]["corporate_actions"]["matched"] == 19


def test_index_snapshot_disagreement_identifies_scope_and_fails_closed() -> None:
    document = _valid_document()
    event = document["index_member_events"][2]
    event["candidate_after"]["members"][-1] = "949999"

    report = reconcile_pit_evidence(document)

    assert report["reconciliation_passed"] is False
    assert report["checks"]["four_index_member_events"]["matched_by_scope"][
        "csi800"
    ] == 0
    assert any(
        item["code"] == "index_member_event_mismatch" for item in report["findings"]
    )
    assert any(
        item["code"] == "four_index_event_coverage_incomplete"
        for item in report["findings"]
    )


def test_candidate_declared_ingestion_time_is_not_authoritative_available_at() -> None:
    document = _valid_document()
    document["index_member_events"][0]["candidate_after"][
        "available_at_evidence"
    ] = "declared_ingestion_time"

    report = reconcile_pit_evidence(document)

    assert report["reconciliation_passed"] is False
    assert any(
        item["code"] == "index_member_event_unknown" for item in report["findings"]
    )


def test_report_digest_and_quarantine_boundary_are_tamper_evident() -> None:
    report = reconcile_pit_evidence(_valid_document())
    report["activation_permitted"] = True

    with pytest.raises(EvidenceReconciliationError, match="digest changed"):
        verify_reconciliation_report(report)


def test_credential_like_fields_are_rejected() -> None:
    document = _valid_document()
    document["api_token"] = "must-never-appear"

    report = reconcile_pit_evidence(document)

    assert report["reconciliation_passed"] is False
    assert any(item["code"] == "input_contract_invalid" for item in report["findings"])
    assert "must-never-appear" not in str(report)
