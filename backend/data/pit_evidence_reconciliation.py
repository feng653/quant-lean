"""Offline, fail-closed reconciliation of quarantined PIT provider evidence.

The reconciler is deliberately a pure reader: it accepts an already-normalised
JSON document and returns an auditable report.  It has no importer, activation,
PIT master, price ledger, cache, database, or network dependency.  A passing
report means only that the supplied candidate observations agree with the
supplied official evidence; it never authorises production use.
"""

from __future__ import annotations

import hmac
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from backend.data.provider_artifacts import canonical_sha256


RECONCILIATION_INPUT_SCHEMA = "pit-evidence-reconciliation-input/v1"
RECONCILIATION_REPORT_SCHEMA = "pit-evidence-reconciliation-report/v1"
REQUIRED_CORPORATE_ACTION_CASES = 20
REQUIRED_CORPORATE_ACTION_TYPES = frozenset(
    {
        "cash_dividend",
        "stock_dividend",
        "capitalisation",
        "rights_issue",
        "split",
    }
)
FOUR_INDEX_COUNTS = {
    "csi300": 300,
    "csi500": 500,
    "csi800": 800,
    "csi1000": 1_000,
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECURITY_CODE = re.compile(r"^[0-9]{6}(?:\.(?:SH|SZ))?$")
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
_SECRET_KEY = re.compile(r"(?:authorization|api[_-]?key|password|secret|token)", re.I)
_OFFICIAL_AVAILABILITY_EVIDENCE = {"official_published_at", "provider_field"}


class EvidenceReconciliationError(ValueError):
    """The reconciliation input cannot be evaluated safely."""


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceReconciliationError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceReconciliationError(
            f"{field} must be an RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceReconciliationError(f"{field} must contain a timezone")
    return parsed.astimezone(UTC)


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise EvidenceReconciliationError(f"{field} must be lowercase SHA-256")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvidenceReconciliationError(f"{field} is invalid")
    return value


def _security_code(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SECURITY_CODE.fullmatch(value.upper()):
        raise EvidenceReconciliationError(f"{field} is not an A-share security code")
    return value[:6]


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceReconciliationError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvidenceReconciliationError(f"{field} must be an array")
    return value


def _assert_no_credentials(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SECRET_KEY.search(str(key)):
                raise EvidenceReconciliationError(
                    f"credential-like field is forbidden: {path}.{key}"
                )
            _assert_no_credentials(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_credentials(nested, f"{path}[{index}]")


def _canonical_term(value: Any, field: str) -> tuple[str, str]:
    if value is None or isinstance(value, bool):
        raise EvidenceReconciliationError(f"{field} is unknown")
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise EvidenceReconciliationError(f"{field} is not finite") from exc
        if not number.is_finite():
            raise EvidenceReconciliationError(f"{field} is not finite")
        normalized = format(number.normalize(), "f")
        return ("number", "0" if normalized == "-0" else normalized)
    if isinstance(value, str) and value.strip():
        return ("text", value.strip())
    raise EvidenceReconciliationError(f"{field} is empty or unsupported")


def _terms(value: Any, field: str) -> tuple[tuple[str, tuple[str, str]], ...]:
    document = _mapping(value, field)
    if not document:
        raise EvidenceReconciliationError(f"{field} must not be empty")
    return tuple(
        sorted(
            (
                _identifier(key, f"{field}.key"),
                _canonical_term(nested, f"{field}.{key}"),
            )
            for key, nested in document.items()
        )
    )


def _members(value: Any, field: str, expected_count: int) -> tuple[str, ...]:
    rows = tuple(
        _security_code(item, f"{field}[{index}]")
        for index, item in enumerate(_sequence(value, field))
    )
    if len(rows) != expected_count:
        raise EvidenceReconciliationError(
            f"{field} expected {expected_count} members, observed {len(rows)}"
        )
    if len(set(rows)) != len(rows):
        raise EvidenceReconciliationError(f"{field} contains duplicate members")
    return rows


def _evidence_identity(
    evidence: Mapping[str, Any],
    *,
    field: str,
    role: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    evidence_id = _identifier(evidence.get("evidence_id"), f"{field}.evidence_id")
    provider = _identifier(evidence.get("provider"), f"{field}.provider").lower()
    authority_level = evidence.get("authority_level")
    classification = evidence.get("classification")
    if role == "official":
        if authority_level != "official" or classification != "official":
            raise EvidenceReconciliationError(
                f"{field} must be classified as official evidence"
            )
        if provider == "tushare":
            raise EvidenceReconciliationError(
                f"{field}.provider cannot be the candidate provider"
            )
        accepted_availability = _OFFICIAL_AVAILABILITY_EVIDENCE
    else:
        if provider != "tushare" or classification != "quarantine":
            raise EvidenceReconciliationError(
                f"{field} must be quarantined Tushare candidate evidence"
            )
        accepted_availability = {"provider_field"}

    artifact_sha256 = _digest(
        evidence.get("artifact_sha256"), f"{field}.artifact_sha256"
    )
    manifest_sha256 = _digest(
        evidence.get("manifest_sha256"), f"{field}.manifest_sha256"
    )
    effective_at = _timestamp(evidence.get("effective_at"), f"{field}.effective_at")
    available_at = _timestamp(evidence.get("available_at"), f"{field}.available_at")
    ingested_at = _timestamp(evidence.get("ingested_at"), f"{field}.ingested_at")
    if ingested_at < available_at:
        raise EvidenceReconciliationError(f"{field}.ingested_at precedes available_at")
    available_evidence = evidence.get("available_at_evidence")
    if available_evidence not in accepted_availability:
        raise EvidenceReconciliationError(
            f"{field}.available_at evidence is missing or not authoritative"
        )
    revision = evidence.get("revision")
    if not isinstance(revision, (str, int)) or isinstance(revision, bool) or not str(revision):
        raise EvidenceReconciliationError(f"{field}.revision is missing")
    revision_evidence = evidence.get("revision_evidence")
    accepted_revision = (
        {"provider_field", "official_document_version"}
        if role == "official"
        else {"provider_field"}
    )
    if revision_evidence not in accepted_revision:
        raise EvidenceReconciliationError(
            f"{field}.revision evidence is missing or not authoritative"
        )
    temporal = {
        "evidence_id": evidence_id,
        "role": role,
        "provider": provider,
        "effective_at": effective_at.isoformat().replace("+00:00", "Z"),
        "available_at": available_at.isoformat().replace("+00:00", "Z"),
        "ingested_at": ingested_at.isoformat().replace("+00:00", "Z"),
        "revision": str(revision),
        "available_at_evidence": str(available_evidence),
        "revision_evidence": str(revision_evidence),
    }
    refs = [
        {
            "evidence_id": evidence_id,
            "role": role,
            "provider": provider,
            "artifact_sha256": artifact_sha256,
            "manifest_sha256": manifest_sha256,
        }
    ]
    return temporal, refs


def _finding(code: str, location: str, detail: str) -> dict[str, str]:
    return {"code": code, "location": location, "detail": detail}


def reconcile_pit_evidence(document: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile one immutable, quarantine-only evidence bundle.

    All unknown, incomplete, mismatched, or non-authoritative observations are
    findings.  The returned report never permits import or activation, including
    when ``reconciliation_passed`` is true.
    """

    source = dict(document)
    input_sha256 = canonical_sha256(source)
    findings: list[dict[str, str]] = []
    evidence_refs: list[dict[str, Any]] = []
    temporal_rows: list[dict[str, Any]] = []
    action_passed = 0
    matched_action_types: Counter[str] = Counter()
    index_passed = 0
    scope_counts: Counter[str] = Counter()

    try:
        _assert_no_credentials(source)
        if source.get("schema_version") != RECONCILIATION_INPUT_SCHEMA:
            raise EvidenceReconciliationError("input schema is unsupported")
        if source.get("classification") != "quarantine":
            raise EvidenceReconciliationError("input must remain quarantine-only")
        _timestamp(source.get("prepared_at"), "prepared_at")
    except EvidenceReconciliationError as exc:
        findings.append(_finding("input_contract_invalid", "$", str(exc)))

    try:
        corporate_cases = _sequence(
            source.get("corporate_action_cases"), "corporate_action_cases"
        )
    except EvidenceReconciliationError as exc:
        corporate_cases = []
        findings.append(_finding("corporate_action_cases_invalid", "$.corporate_action_cases", str(exc)))
    if len(corporate_cases) != REQUIRED_CORPORATE_ACTION_CASES:
        findings.append(
            _finding(
                "corporate_action_case_count_mismatch",
                "$.corporate_action_cases",
                f"expected exactly {REQUIRED_CORPORATE_ACTION_CASES}, observed {len(corporate_cases)}",
            )
        )

    seen_case_ids: set[str] = set()
    for position, item in enumerate(corporate_cases):
        location = f"$.corporate_action_cases[{position}]"
        try:
            case = _mapping(item, location)
            case_id = _identifier(case.get("case_id"), f"{location}.case_id")
            if case_id in seen_case_ids:
                raise EvidenceReconciliationError("case_id is duplicated")
            seen_case_ids.add(case_id)
            official = _mapping(case.get("official"), f"{location}.official")
            candidate = _mapping(case.get("candidate"), f"{location}.candidate")
            official_temporal, official_refs = _evidence_identity(
                official, field=f"{location}.official", role="official"
            )
            candidate_temporal, candidate_refs = _evidence_identity(
                candidate, field=f"{location}.candidate", role="candidate"
            )
            evidence_refs.extend(official_refs + candidate_refs)
            temporal_rows.extend((official_temporal, candidate_temporal))
            compared_fields = {
                "security_code": (
                    _security_code(official.get("security_code"), f"{location}.official.security_code"),
                    _security_code(candidate.get("security_code"), f"{location}.candidate.security_code"),
                ),
                "action_type": (
                    _identifier(official.get("action_type"), f"{location}.official.action_type"),
                    _identifier(candidate.get("action_type"), f"{location}.candidate.action_type"),
                ),
                "effective_at": (
                    official_temporal["effective_at"],
                    candidate_temporal["effective_at"],
                ),
                "terms": (
                    _terms(official.get("terms"), f"{location}.official.terms"),
                    _terms(candidate.get("terms"), f"{location}.candidate.terms"),
                ),
            }
            mismatched = sorted(
                field for field, values in compared_fields.items() if values[0] != values[1]
            )
            if mismatched:
                findings.append(
                    _finding(
                        "corporate_action_mismatch",
                        location,
                        "candidate differs from official evidence: " + ", ".join(mismatched),
                    )
                )
            else:
                action_passed += 1
                matched_action_types[compared_fields["action_type"][0]] += 1
        except EvidenceReconciliationError as exc:
            findings.append(_finding("corporate_action_unknown", location, str(exc)))

    missing_action_types = sorted(
        REQUIRED_CORPORATE_ACTION_TYPES - set(matched_action_types)
    )
    if missing_action_types:
        findings.append(
            _finding(
                "corporate_action_type_coverage_incomplete",
                "$.corporate_action_cases",
                "no passing official/candidate match for: "
                + ", ".join(missing_action_types),
            )
        )

    try:
        index_events = _sequence(source.get("index_member_events"), "index_member_events")
    except EvidenceReconciliationError as exc:
        index_events = []
        findings.append(_finding("index_member_events_invalid", "$.index_member_events", str(exc)))
    seen_event_ids: set[str] = set()
    for position, item in enumerate(index_events):
        location = f"$.index_member_events[{position}]"
        try:
            event = _mapping(item, location)
            event_id = _identifier(event.get("event_id"), f"{location}.event_id")
            if event_id in seen_event_ids:
                raise EvidenceReconciliationError("event_id is duplicated")
            seen_event_ids.add(event_id)
            scope_id = str(event.get("scope_id") or "")
            if scope_id not in FOUR_INDEX_COUNTS:
                raise EvidenceReconciliationError("scope_id is not one of the four governed indices")
            expected_count = FOUR_INDEX_COUNTS[scope_id]
            official = _mapping(event.get("official"), f"{location}.official")
            before = _mapping(event.get("candidate_before"), f"{location}.candidate_before")
            after = _mapping(event.get("candidate_after"), f"{location}.candidate_after")
            official_temporal, official_refs = _evidence_identity(
                official, field=f"{location}.official", role="official"
            )
            before_temporal, before_refs = _evidence_identity(
                before, field=f"{location}.candidate_before", role="candidate"
            )
            after_temporal, after_refs = _evidence_identity(
                after, field=f"{location}.candidate_after", role="candidate"
            )
            evidence_refs.extend(official_refs + before_refs + after_refs)
            temporal_rows.extend((official_temporal, before_temporal, after_temporal))
            before_members = _members(
                before.get("members"), f"{location}.candidate_before.members", expected_count
            )
            after_members = _members(
                after.get("members"), f"{location}.candidate_after.members", expected_count
            )
            addition_rows = tuple(
                _security_code(value, f"{location}.official.additions")
                for value in _sequence(
                    official.get("additions"), f"{location}.official.additions"
                )
            )
            removal_rows = tuple(
                _security_code(value, f"{location}.official.removals")
                for value in _sequence(
                    official.get("removals"), f"{location}.official.removals"
                )
            )
            additions = set(addition_rows)
            removals = set(removal_rows)
            if len(additions) != len(addition_rows) or len(removals) != len(removal_rows):
                raise EvidenceReconciliationError("official add/remove rows contain duplicates")
            if not additions or not removals or len(additions) != len(removals):
                raise EvidenceReconciliationError("official add/remove rows are empty or unbalanced")
            if additions & removals:
                raise EvidenceReconciliationError("official additions and removals overlap")
            event_effective_at = official_temporal["effective_at"]
            if before_temporal["effective_at"] >= event_effective_at:
                raise EvidenceReconciliationError("candidate_before is not before the official event")
            if after_temporal["effective_at"] < event_effective_at:
                raise EvidenceReconciliationError("candidate_after predates the official event")
            expected_after = (set(before_members) - removals) | additions
            if expected_after != set(after_members):
                missing = len(expected_after - set(after_members))
                unexpected = len(set(after_members) - expected_after)
                findings.append(
                    _finding(
                        "index_member_event_mismatch",
                        location,
                        f"replayed official event differs from candidate snapshot: missing={missing}, unexpected={unexpected}",
                    )
                )
            else:
                index_passed += 1
                scope_counts[scope_id] += 1
        except EvidenceReconciliationError as exc:
            findings.append(_finding("index_member_event_unknown", location, str(exc)))

    missing_scopes = sorted(set(FOUR_INDEX_COUNTS) - set(scope_counts))
    if missing_scopes:
        findings.append(
            _finding(
                "four_index_event_coverage_incomplete",
                "$.index_member_events",
                "no passing historical member event for: " + ", ".join(missing_scopes),
            )
        )

    evidence_ids = [str(row["evidence_id"]) for row in temporal_rows]
    duplicate_evidence_ids = sorted(
        evidence_id for evidence_id, count in Counter(evidence_ids).items() if count > 1
    )
    if duplicate_evidence_ids:
        findings.append(
            _finding(
                "evidence_identity_reused",
                "$",
                f"{len(duplicate_evidence_ids)} evidence identifiers are reused",
            )
        )

    revision_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in temporal_rows:
        revision_groups[(row["provider"], row["role"], row["effective_at"])].append(row)
    revision_chains = sum(1 for rows in revision_groups.values() if len(rows) > 1)
    temporal_summary = {
        "observations": len(temporal_rows),
        "effective_at_present": len(temporal_rows),
        "available_at_authoritative": len(temporal_rows),
        "ingested_at_present": len(temporal_rows),
        "revision_authoritative": len(temporal_rows),
        "revision_chains_observed": revision_chains,
        "unknown_or_invalid": sum(
            finding["code"].endswith("_unknown")
            or finding["code"] == "input_contract_invalid"
            for finding in findings
        ),
    }

    unique_refs = {
        (ref["evidence_id"], ref["role"]): ref for ref in evidence_refs
    }
    sorted_refs = sorted(
        unique_refs.values(), key=lambda ref: (ref["role"], ref["evidence_id"])
    )
    passed = not findings
    report: dict[str, Any] = {
        "schema_version": RECONCILIATION_REPORT_SCHEMA,
        "input_sha256": input_sha256,
        "classification": "quarantine",
        "reconciliation_passed": passed,
        "production_pit_ready": False,
        "production_import_permitted": False,
        "activation_permitted": False,
        "production_blockers": [
            "candidate_evidence_is_quarantine_only",
            "continuous_2016_to_current_coverage_not_evaluated",
            "provider_licence_and_retention_receipt_not_evaluated",
            "production_import_review_not_performed",
        ],
        "checks": {
            "corporate_actions": {
                "required": REQUIRED_CORPORATE_ACTION_CASES,
                "observed": len(corporate_cases),
                "matched": action_passed,
                "required_types": sorted(REQUIRED_CORPORATE_ACTION_TYPES),
                "matched_by_type": {
                    action_type: matched_action_types.get(action_type, 0)
                    for action_type in sorted(REQUIRED_CORPORATE_ACTION_TYPES)
                },
            },
            "four_index_member_events": {
                "required_scopes": sorted(FOUR_INDEX_COUNTS),
                "observed": len(index_events),
                "matched": index_passed,
                "matched_by_scope": {
                    scope: scope_counts.get(scope, 0) for scope in sorted(FOUR_INDEX_COUNTS)
                },
            },
            "bitemporal": temporal_summary,
        },
        "findings": sorted(
            findings, key=lambda finding: (finding["code"], finding["location"], finding["detail"])
        ),
        "evidence_refs": sorted_refs,
        "decision": (
            "candidate_matches_supplied_official_evidence_but_remains_quarantined"
            if passed
            else "fail_closed_evidence_reconciliation_incomplete"
        ),
    }
    unsigned = dict(report)
    report["report_sha256"] = canonical_sha256(unsigned)
    return report


def verify_reconciliation_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Verify report identity and permanent quarantine policy fields."""

    payload = dict(report)
    supplied = payload.pop("report_sha256", None)
    if not isinstance(supplied, str) or not _SHA256.fullmatch(supplied):
        raise EvidenceReconciliationError("report_sha256 is invalid")
    if not hmac.compare_digest(canonical_sha256(payload), supplied):
        raise EvidenceReconciliationError("reconciliation report digest changed")
    if payload.get("schema_version") != RECONCILIATION_REPORT_SCHEMA:
        raise EvidenceReconciliationError("reconciliation report schema is unsupported")
    if (
        payload.get("classification") != "quarantine"
        or payload.get("production_pit_ready") is not False
        or payload.get("production_import_permitted") is not False
        or payload.get("activation_permitted") is not False
    ):
        raise EvidenceReconciliationError("reconciliation report crossed quarantine boundary")
    _assert_no_credentials(payload)
    return dict(report)
