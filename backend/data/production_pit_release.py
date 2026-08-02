"""Fail-closed orchestration for a complete production PIT release.

This module deliberately sits *before* the runtime PIT master and price ledger.
It reads immutable, independently approved provider artifacts, validates the
cross-dataset contract, and writes only an atomic release authorization to a
dedicated registry.  It never fetches data and never mutates application or
legacy-cache databases.

Materialising an authorised release into runtime tables is a separate,
generation-aware deployment step.  Keeping that boundary explicit prevents a
partially imported dataset from becoming query-visible.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


APPROVED_ARTIFACT_SCHEMA = "approved-provider-artifact/v1"
ARTIFACT_PAYLOAD_SCHEMA = "production-pit-artifact-payload/v1"
RELEASE_BUNDLE_SCHEMA = "production-pit-release-bundle/v1"
READINESS_SCHEMA = "production-pit-release-readiness/v1"
REGISTRY_SCHEMA = "production-pit-release-registry/v1"

PIT_RELEASE_POOLS = ("csi300", "csi500", "csi800", "csi1000")
_ARTIFACT_KINDS = {
    "trading_calendar",
    "index_membership",
    "security_master",
    "industry",
    "market_status",
    "dual_price_ledger",
    "corporate_action_evidence",
}
_STATUS_VALUES = {"tradable", "suspended", "listed_not_trading", "delisted"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SECURITY_CODE = re.compile(r"^[0-9]{6}$")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
_MAX_LICENCE_RECEIPT_BYTES = 16 * 1024 * 1024
_ALLOWED_EVIDENCE_LEVELS = {
    "trading_calendar": {"exchange_authoritative", "licensed"},
    "index_membership": {"index_provider_authoritative", "licensed"},
    "security_master": {"exchange_authoritative", "licensed"},
    "industry": {"licensed", "public_cross_validated"},
    "market_status": {"exchange_authoritative", "licensed"},
    "dual_price_ledger": {"exchange_authoritative", "licensed"},
    "corporate_action_evidence": {
        "exchange_authoritative",
        "licensed",
        "public_cross_validated",
    },
}


class ProductionPitReleaseError(RuntimeError):
    """Base error for production PIT release orchestration."""


class ApprovedArtifactError(ProductionPitReleaseError):
    """An approved artifact is unavailable, changed, or not trusted."""


class ReleaseActivationBlocked(ProductionPitReleaseError):
    """The release cannot be atomically authorised."""


_ACTIVATION_TOKEN = object()


@dataclass(frozen=True)
class _ActivationAuthorization:
    plan_sha256: str
    token: object


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApprovedArtifactError("artifact JSON is not canonicalisable") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _iso_date(value: Any, field_name: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ApprovedArtifactError(f"{field_name} must be YYYY-MM-DD") from exc


def _timestamp(value: Any, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ApprovedArtifactError(f"{field_name} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApprovedArtifactError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_id(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ApprovedArtifactError(f"{field_name} is invalid")
    return normalized


def _code(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _SECURITY_CODE.fullmatch(normalized):
        raise ApprovedArtifactError("security_code must contain six digits")
    return normalized


def _temporal(row: Mapping[str, Any], *, effective_field: str) -> dict[str, Any]:
    effective = _timestamp(row.get(effective_field), effective_field)
    available = _timestamp(row.get("available_at"), "available_at")
    ingested = _timestamp(row.get("ingested_at"), "ingested_at")
    try:
        revision = int(row.get("revision"))
    except (TypeError, ValueError) as exc:
        raise ApprovedArtifactError("revision must be a positive integer") from exc
    if revision < 1:
        raise ApprovedArtifactError("revision must be a positive integer")
    if available < effective or ingested < available:
        raise ApprovedArtifactError(
            "temporal order must satisfy effective_at <= available_at <= ingested_at"
        )
    return {
        "effective_at": effective,
        "available_at": available,
        "ingested_at": ingested,
        "revision": revision,
    }


@dataclass(frozen=True)
class ProductionPitReleasePolicy:
    """Machine-verifiable release requirements.

    Production callers should keep the defaults and set ``coverage_to`` to the
    latest completed authoritative trading session.  Tests may use a shorter
    explicit range and smaller member counts.
    """

    coverage_from: str = "2016-01-01"
    coverage_to: str = ""
    pools: tuple[str, ...] = PIT_RELEASE_POOLS
    member_counts: Mapping[str, int] = field(
        default_factory=lambda: {
            "csi300": 300,
            "csi500": 500,
            "csi800": 800,
            "csi1000": 1000,
        }
    )
    security_scope: str = "all_a"
    industry_scope: str = "cninfo_008001"
    ledger_scope: str = "all_a"

    def __post_init__(self) -> None:
        if not self.coverage_to:
            raise ValueError("coverage_to must be the explicit latest completed session boundary")
        start = _iso_date(self.coverage_from, "coverage_from")
        end = _iso_date(self.coverage_to, "coverage_to")
        if start > end:
            raise ValueError("coverage_from must not exceed coverage_to")
        if tuple(self.pools) != PIT_RELEASE_POOLS:
            raise ValueError("production release must contain exactly four CSI pools")
        if set(self.member_counts) != set(PIT_RELEASE_POOLS) or any(
            isinstance(value, bool) or int(value) < 1
            for value in self.member_counts.values()
        ):
            raise ValueError("member_counts must define every CSI pool")

    def document(self) -> dict[str, Any]:
        return {
            "coverage_from": self.coverage_from,
            "coverage_to": self.coverage_to,
            "pools": list(self.pools),
            "member_counts": {
                key: int(self.member_counts[key]) for key in sorted(self.member_counts)
            },
            "security_scope": self.security_scope,
            "industry_scope": self.industry_scope,
            "ledger_scope": self.ledger_scope,
        }


class ApprovedProviderArtifactStore:
    """Read-only verifier for content-addressed approved artifacts."""

    def __init__(
        self,
        root: str | Path,
        *,
        trusted_approval_keys: Mapping[str, str],
    ) -> None:
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self.manifest_root = self.root / "manifests" / "sha256"
        self.payload_root = self.root / "artifacts" / "sha256"
        self.receipt_root = self.root / "licence-receipts" / "sha256"
        self.trusted_keys: dict[str, Ed25519PublicKey] = {}
        for key_id, encoded_key in trusted_approval_keys.items():
            safe_key_id = _safe_id(key_id, "approval key id")
            try:
                raw = base64.b64decode(str(encoded_key), validate=True)
                self.trusted_keys[safe_key_id] = Ed25519PublicKey.from_public_bytes(raw)
            except (ValueError, TypeError) as exc:
                raise ApprovedArtifactError("approval public key is invalid") from exc
        if not self.trusted_keys:
            raise ApprovedArtifactError("at least one trusted approval key is required")

    @staticmethod
    def _regular_file(path: Path, *, root: Path, max_size: int) -> os.stat_result:
        try:
            relative = path.relative_to(root)
            root_metadata = root.lstat()
            if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
                raise ApprovedArtifactError("artifact root is unsafe")
            current = root
            for component in relative.parts[:-1]:
                current = current / component
                metadata = current.lstat()
                if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                    raise ApprovedArtifactError("artifact directory is unsafe")
            metadata = path.lstat()
        except (ValueError, OSError) as exc:
            raise ApprovedArtifactError("artifact object is unavailable") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ApprovedArtifactError("artifact object is unsafe")
        if metadata.st_size < 1 or metadata.st_size > max_size:
            raise ApprovedArtifactError("artifact object size is unsafe")
        return metadata

    @staticmethod
    def _path(root: Path, digest: str, suffix: str = "") -> Path:
        if not _SHA256.fullmatch(str(digest)):
            raise ApprovedArtifactError("content digest is invalid")
        return root / digest[:2] / f"{digest}{suffix}"

    def read(self, manifest_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest_path = self._path(self.manifest_root, manifest_sha256, ".json")
        self._regular_file(
            manifest_path, root=self.root, max_size=_MAX_MANIFEST_BYTES
        )
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApprovedArtifactError("approved manifest is unreadable") from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
            raise ApprovedArtifactError("approved manifest bytes changed")
        if not isinstance(manifest, dict):
            raise ApprovedArtifactError("approved manifest must be an object")
        payload_sha256 = str(manifest.get("payload_sha256") or "")
        payload_path = self._path(self.payload_root, payload_sha256, ".json")
        payload_metadata = self._regular_file(
            payload_path, root=self.root, max_size=_MAX_PAYLOAD_BYTES
        )
        if payload_metadata.st_size != manifest.get("size_bytes"):
            raise ApprovedArtifactError("approved payload size changed")
        try:
            payload_bytes = payload_path.read_bytes()
            payload = json.loads(payload_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApprovedArtifactError("approved payload is unreadable") from exc
        receipt_sha256 = str(manifest.get("licence_receipt_sha256") or "")
        receipt_path = self._path(self.receipt_root, receipt_sha256)
        self._regular_file(
            receipt_path,
            root=self.root,
            max_size=_MAX_LICENCE_RECEIPT_BYTES,
        )
        try:
            receipt_bytes = receipt_path.read_bytes()
        except OSError as exc:
            raise ApprovedArtifactError("licence receipt is unreadable") from exc
        if hashlib.sha256(receipt_bytes).hexdigest() != receipt_sha256:
            raise ApprovedArtifactError("licence receipt bytes changed")
        self.verify(manifest, manifest_sha256, payload, payload_bytes)
        return manifest, payload

    def verify(
        self,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        payload: Mapping[str, Any],
        payload_bytes: bytes,
    ) -> None:
        if manifest.get("schema_version") != APPROVED_ARTIFACT_SCHEMA:
            raise ApprovedArtifactError("approved artifact schema is unsupported")
        if manifest.get("classification") != "approved":
            raise ApprovedArtifactError("artifact is not approved")
        if manifest.get("licence_scope") != "local_research_retention":
            raise ApprovedArtifactError("artifact licence does not permit local retention")
        if not _SHA256.fullmatch(str(manifest.get("licence_receipt_sha256") or "")):
            raise ApprovedArtifactError("licence receipt binding is missing")
        kind = str(manifest.get("artifact_kind") or "")
        if kind not in _ARTIFACT_KINDS:
            raise ApprovedArtifactError("artifact kind is unsupported")
        evidence_level = str(manifest.get("evidence_level") or "")
        if evidence_level not in _ALLOWED_EVIDENCE_LEVELS[kind]:
            raise ApprovedArtifactError("artifact evidence level is insufficient")
        _safe_id(manifest.get("provider"), "provider")
        _safe_id(manifest.get("dataset"), "dataset")
        _safe_id(manifest.get("provider_version"), "provider_version")
        _safe_id(manifest.get("scope_id"), "scope_id")
        start = _iso_date(manifest.get("coverage_from"), "coverage_from")
        end = _iso_date(manifest.get("coverage_to"), "coverage_to")
        if start > end:
            raise ApprovedArtifactError("artifact coverage is invalid")
        if hashlib.sha256(payload_bytes).hexdigest() != manifest.get("payload_sha256"):
            raise ApprovedArtifactError("approved payload bytes changed")
        if len(payload_bytes) != manifest.get("size_bytes"):
            raise ApprovedArtifactError("approved payload size changed")
        rows = payload.get("rows") if isinstance(payload, Mapping) else None
        if (
            payload.get("schema_version") != ARTIFACT_PAYLOAD_SCHEMA
            or not isinstance(rows, list)
            or len(rows) != manifest.get("row_count")
        ):
            raise ApprovedArtifactError("approved payload row contract changed")
        approval = manifest.get("approval")
        if not isinstance(approval, Mapping):
            raise ApprovedArtifactError("approval receipt is missing")
        key_id = _safe_id(approval.get("key_id"), "approval.key_id")
        reviewer = _safe_id(approval.get("reviewer_id"), "approval.reviewer_id")
        stager = _safe_id(manifest.get("staged_by"), "staged_by")
        if reviewer == stager:
            raise ApprovedArtifactError("artifact requires independent approval")
        _timestamp(approval.get("approved_at"), "approval.approved_at")
        signature_text = str(approval.get("signature") or "")
        unsigned = dict(manifest)
        unsigned_approval = dict(approval)
        unsigned_approval.pop("signature", None)
        unsigned["approval"] = unsigned_approval
        try:
            signature = base64.b64decode(signature_text, validate=True)
            trusted_key = self.trusted_keys[key_id]
            trusted_key.verify(signature, _canonical_bytes(unsigned))
        except (ValueError, KeyError, InvalidSignature) as exc:
            raise ApprovedArtifactError("artifact approval signature is invalid") from exc
        if hashlib.sha256(_canonical_bytes(manifest)).hexdigest() != manifest_sha256:
            raise ApprovedArtifactError("approved manifest canonical digest changed")


def _active_codes(
    rows: Sequence[Mapping[str, Any]],
    sessions: Sequence[str],
    *,
    label: str,
) -> dict[str, set[str]]:
    active: dict[str, set[str]] = {session: set() for session in sessions}
    seen: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        code = _code(row.get("security_code"))
        start = _iso_date(row.get("effective_from"), "effective_from")
        end = _iso_date(row.get("effective_to"), "effective_to")
        if start > end:
            raise ApprovedArtifactError(f"{label} interval is invalid")
        temporal = _temporal(row, effective_field="effective_at")
        if temporal["effective_at"][:10] != start:
            raise ApprovedArtifactError(f"{label} effective_at differs from effective_from")
        if any(not (end < prior_start or start > prior_end) for prior_start, prior_end in seen[code]):
            raise ApprovedArtifactError(f"{label} intervals overlap")
        seen[code].append((start, end))
        for session in sessions:
            if start <= session <= end:
                active[session].add(code)
    return active


def _date_ranges_cover(
    ranges: Sequence[tuple[str, str]], required_from: str, required_to: str
) -> bool:
    cursor = date.fromisoformat(required_from)
    required_end = date.fromisoformat(required_to)
    for start_text, end_text in sorted(ranges):
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
        if end < cursor:
            continue
        if start > cursor:
            return False
        cursor = max(cursor, end + timedelta(days=1))
        if cursor > required_end:
            return True
    return cursor > required_end


class ProductionPitReleaseOrchestrator:
    """Validate all PIT release inputs and create an atomic authorisation."""

    def __init__(
        self,
        artifact_store: ApprovedProviderArtifactStore,
        *,
        policy: ProductionPitReleasePolicy | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.policy = policy or ProductionPitReleasePolicy()

    def _required_identities(self) -> set[tuple[str, str]]:
        return {
            *(("index_membership", pool) for pool in self.policy.pools),
            ("trading_calendar", "cn_equity"),
            ("security_master", self.policy.security_scope),
            ("industry", self.policy.industry_scope),
            ("market_status", self.policy.security_scope),
            ("dual_price_ledger", self.policy.ledger_scope),
            ("corporate_action_evidence", self.policy.security_scope),
        }

    @staticmethod
    def _block(
        blockers: list[dict[str, Any]], code: str, message: str, **context: Any
    ) -> None:
        blockers.append({"code": code, "message": message, **context})

    def dry_run(self, bundle: Mapping[str, Any]) -> dict[str, Any]:
        """Return every deterministically discoverable blocker in one report."""

        blockers: list[dict[str, Any]] = []
        artifacts: dict[
            tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]
        ] = defaultdict(list)
        artifact_summaries: list[dict[str, Any]] = []
        if bundle.get("schema_version") != RELEASE_BUNDLE_SCHEMA:
            self._block(blockers, "release_bundle_schema_invalid", "release bundle schema is invalid")
        if bundle.get("coverage_from") != self.policy.coverage_from or bundle.get(
            "coverage_to"
        ) != self.policy.coverage_to:
            self._block(
                blockers,
                "release_coverage_mismatch",
                "bundle coverage differs from the preregistered policy",
            )
        references = bundle.get("artifact_manifest_sha256s")
        if not isinstance(references, list):
            references = []
            self._block(blockers, "artifact_reference_list_missing", "artifact references are missing")
        if len(references) != len(set(map(str, references))):
            self._block(blockers, "duplicate_artifact_reference", "artifact references are duplicated")
        for reference in references:
            digest = str(reference)
            try:
                manifest, payload = self.artifact_store.read(digest)
                identity = (str(manifest["artifact_kind"]), str(manifest["scope_id"]))
                artifacts[identity].append((manifest, payload))
                artifact_summaries.append(
                    {
                        "manifest_sha256": digest,
                        "payload_sha256": manifest["payload_sha256"],
                        "artifact_kind": identity[0],
                        "scope_id": identity[1],
                        "row_count": manifest["row_count"],
                    }
                )
                if (
                    manifest["coverage_from"] < self.policy.coverage_from
                    or manifest["coverage_to"] > self.policy.coverage_to
                ):
                    self._block(
                        blockers,
                        "artifact_coverage_outside_release",
                        "artifact coverage exceeds the preregistered release window",
                        artifact_kind=identity[0],
                        scope_id=identity[1],
                    )
            except (ApprovedArtifactError, KeyError, TypeError) as exc:
                self._block(
                    blockers,
                    "approved_artifact_invalid",
                    str(exc),
                    manifest_sha256=digest,
                )
        missing = sorted(self._required_identities() - set(artifacts))
        for kind, scope in missing:
            self._block(
                blockers,
                "required_artifact_missing",
                "a complete release artifact is missing",
                artifact_kind=kind,
                scope_id=scope,
            )
        for (kind, scope), shards in sorted(artifacts.items()):
            ranges = [
                (manifest["coverage_from"], manifest["coverage_to"])
                for manifest, _payload in shards
            ]
            if not _date_ranges_cover(
                ranges, self.policy.coverage_from, self.policy.coverage_to
            ):
                self._block(
                    blockers,
                    "artifact_shard_coverage_incomplete",
                    "artifact shards do not continuously cover the release window",
                    artifact_kind=kind,
                    scope_id=scope,
                    ranges=sorted(ranges),
                )

        coverage: dict[str, Any] = {
            "policy": self.policy.document(),
            "trading_session_count": 0,
            "ever_member_security_count": 0,
            "member_session_count": 0,
            "tradable_member_session_count": 0,
        }
        required = self._required_identities()
        if required <= set(artifacts):
            self._validate_cross_dataset(artifacts, blockers, coverage)

        artifact_summaries.sort(key=lambda item: (item["artifact_kind"], item["scope_id"]))
        blockers.sort(
            key=lambda item: (
                item["code"],
                str(item.get("artifact_kind") or ""),
                str(item.get("scope_id") or ""),
                str(item.get("manifest_sha256") or ""),
            )
        )
        plan = {
            "schema_version": "production-pit-release-plan/v1",
            "bundle_sha256": _digest(bundle),
            "policy": self.policy.document(),
            "artifacts": artifact_summaries,
            "coverage": coverage,
        }
        plan_sha256 = _digest(plan)
        return {
            "schema_version": READINESS_SCHEMA,
            "mode": "dry_run",
            "ready": not blockers,
            "activation_permitted": not blockers,
            "runtime_data_changed": False,
            "production_tables_written": False,
            "plan_sha256": plan_sha256,
            "plan": plan,
            "blocker_count": len(blockers),
            "blockers": blockers,
        }

    def _validate_cross_dataset(
        self,
        artifacts: Mapping[
            tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]
        ],
        blockers: list[dict[str, Any]],
        coverage: dict[str, Any],
    ) -> None:
        try:
            calendar_rows = self._rows(artifacts, ("trading_calendar", "cn_equity"))
            sessions: list[str] = []
            for row in calendar_rows:
                session = _iso_date(row.get("trading_date"), "trading_date")
                temporal = _temporal(row, effective_field="effective_at")
                if temporal["effective_at"][:10] != session:
                    raise ApprovedArtifactError("calendar effective_at differs from trading_date")
                sessions.append(session)
            sessions = sorted(sessions)
            start_boundary = date.fromisoformat(self.policy.coverage_from)
            end_boundary = date.fromisoformat(self.policy.coverage_to)
            if (
                not sessions
                or sessions != sorted(set(sessions))
                or date.fromisoformat(sessions[0]) < start_boundary
                or date.fromisoformat(sessions[0]) > start_boundary + timedelta(days=7)
                or date.fromisoformat(sessions[-1]) > end_boundary
                or date.fromisoformat(sessions[-1]) < end_boundary - timedelta(days=7)
            ):
                raise ApprovedArtifactError(
                    "authoritative calendar must cover both release boundaries without duplicates"
                )
            coverage["trading_session_count"] = len(sessions)
        except (ApprovedArtifactError, KeyError, TypeError) as exc:
            self._block(blockers, "trading_calendar_invalid", str(exc))
            return

        membership: dict[str, dict[str, set[str]]] = {}
        for pool in self.policy.pools:
            try:
                rows = self._rows(artifacts, ("index_membership", pool))
                active = _active_codes(rows, sessions, label=f"{pool} membership")
                membership[pool] = active
                wrong = [
                    {"trading_date": session, "actual": len(active[session])}
                    for session in sessions
                    if len(active[session]) != int(self.policy.member_counts[pool])
                ]
                if wrong:
                    self._block(
                        blockers,
                        "membership_daily_count_mismatch",
                        "daily index membership count differs from policy",
                        scope_id=pool,
                        expected=int(self.policy.member_counts[pool]),
                        examples=wrong[:20],
                        mismatch_count=len(wrong),
                    )
            except (ApprovedArtifactError, KeyError, TypeError) as exc:
                self._block(
                    blockers,
                    "membership_timeline_invalid",
                    str(exc),
                    scope_id=pool,
                )
        if len(membership) != len(self.policy.pools):
            return
        member_by_session = {
            session: set().union(*(membership[pool][session] for pool in self.policy.pools))
            for session in sessions
        }
        ever_members = set().union(*member_by_session.values())
        coverage["ever_member_security_count"] = len(ever_members)
        coverage["member_session_count"] = sum(len(value) for value in member_by_session.values())

        try:
            security_rows = self._rows(
                artifacts, ("security_master", self.policy.security_scope)
            )
            for row in security_rows:
                if row.get("listing_status") not in {"listed", "delisted"}:
                    raise ApprovedArtifactError("security listing status is missing")
            security_active = _active_codes(security_rows, sessions, label="security master")
            missing_security = [
                (session, code)
                for session in sessions
                for code in member_by_session[session] - security_active[session]
            ]
            if missing_security:
                self._block(
                    blockers,
                    "security_master_member_session_missing",
                    "security master does not cover every member session",
                    missing_count=len(missing_security),
                    examples=missing_security[:20],
                )
        except (ApprovedArtifactError, KeyError, TypeError) as exc:
            self._block(blockers, "security_master_invalid", str(exc))

        try:
            industry_rows = self._rows(
                artifacts, ("industry", self.policy.industry_scope)
            )
            for row in industry_rows:
                _safe_id(row.get("industry_code"), "industry_code")
                if not str(row.get("industry_name") or "").strip():
                    raise ApprovedArtifactError("industry name is missing")
            industry_active = _active_codes(industry_rows, sessions, label="industry")
            missing_industry = [
                (session, code)
                for session in sessions
                for code in member_by_session[session] - industry_active[session]
            ]
            if missing_industry:
                self._block(
                    blockers,
                    "industry_member_session_missing",
                    "industry classification does not cover every member session",
                    missing_count=len(missing_industry),
                    examples=missing_industry[:20],
                )
        except (ApprovedArtifactError, KeyError, TypeError) as exc:
            self._block(blockers, "industry_timeline_invalid", str(exc))

        status: dict[tuple[str, str], str] = {}
        try:
            status_rows = self._rows(
                artifacts, ("market_status", self.policy.security_scope)
            )
            for row in status_rows:
                code = _code(row.get("security_code"))
                session = _iso_date(row.get("trading_date"), "trading_date")
                temporal = _temporal(row, effective_field="effective_at")
                if temporal["effective_at"][:10] != session:
                    raise ApprovedArtifactError("market status effective_at differs from date")
                value = str(row.get("status") or "")
                if value not in _STATUS_VALUES:
                    raise ApprovedArtifactError("market status value is unsupported")
                identity = (session, code)
                if identity in status:
                    raise ApprovedArtifactError("market status identity is duplicated")
                status[identity] = value
            expected = {
                (session, code)
                for session in sessions
                for code in member_by_session[session]
            }
            missing_status = sorted(expected - set(status))
            extra_status = sorted(set(status) - expected)
            if missing_status or extra_status:
                self._block(
                    blockers,
                    "market_status_member_session_mismatch",
                    "market status identities differ from PIT member sessions",
                    missing_count=len(missing_status),
                    extra_count=len(extra_status),
                    missing_examples=missing_status[:20],
                    extra_examples=extra_status[:20],
                )
        except (ApprovedArtifactError, KeyError, TypeError) as exc:
            self._block(blockers, "market_status_invalid", str(exc))

        prices: dict[tuple[str, str], Mapping[str, Any]] = {}
        try:
            price_rows = self._rows(
                artifacts, ("dual_price_ledger", self.policy.ledger_scope)
            )
            for row in price_rows:
                code = _code(row.get("security_code"))
                session = _iso_date(row.get("trading_date"), "trading_date")
                temporal = _temporal(row, effective_field="effective_at")
                if temporal["effective_at"][:10] != session:
                    raise ApprovedArtifactError("price effective_at differs from trading_date")
                identity = (session, code)
                if identity in prices:
                    raise ApprovedArtifactError("price identity is duplicated")
                for ledger_name in ("raw", "research_adjusted"):
                    ledger = row.get(ledger_name)
                    if not isinstance(ledger, Mapping):
                        raise ApprovedArtifactError(f"{ledger_name} OHLCV is missing")
                    values = [float(ledger[field]) for field in ("open", "high", "low", "close")]
                    volume = float(ledger.get("volume", -1))
                    if (
                        any(not math.isfinite(value) or value <= 0 for value in values)
                        or not math.isfinite(volume)
                        or volume < 0
                    ):
                        raise ApprovedArtifactError(f"{ledger_name} price is invalid")
                    if min(values[0], values[3]) < values[2] or max(values[0], values[3]) > values[1]:
                        raise ApprovedArtifactError(f"{ledger_name} OHLC geometry is invalid")
                amount = float(row.get("amount", -1))
                factor = float(row.get("adjustment_factor", 0))
                if (
                    not math.isfinite(amount)
                    or amount < 0
                    or not math.isfinite(factor)
                    or factor <= 0
                ):
                    raise ApprovedArtifactError("price amount or adjustment factor is invalid")
                raw = row["raw"]
                research = row["research_adjusted"]
                if any(
                    not math.isclose(
                        float(research[field]),
                        float(raw[field]) * factor,
                        rel_tol=1e-8,
                        abs_tol=1e-10,
                    )
                    for field in ("open", "high", "low", "close")
                ):
                    raise ApprovedArtifactError(
                        "research price cannot be reproduced from raw and adjustment factor"
                    )
                prices[identity] = row
            tradable = {identity for identity, value in status.items() if value == "tradable"}
            expected_member = {
                (session, code)
                for session in sessions
                for code in member_by_session[session]
            }
            missing_prices = sorted(tradable - set(prices))
            extra_prices = sorted(set(prices) - expected_member)
            coverage["tradable_member_session_count"] = len(tradable)
            if missing_prices or extra_prices:
                self._block(
                    blockers,
                    "dual_price_member_session_mismatch",
                    "tradable members need dual prices; non-members may not enter the release",
                    missing_count=len(missing_prices),
                    extra_count=len(extra_prices),
                    missing_examples=missing_prices[:20],
                    extra_examples=extra_prices[:20],
                )
        except (ApprovedArtifactError, KeyError, TypeError, ValueError) as exc:
            self._block(blockers, "dual_price_ledger_invalid", str(exc))

        try:
            action_rows = self._rows(
                artifacts,
                ("corporate_action_evidence", self.policy.security_scope),
            )
            action_coverage = _active_codes(action_rows, sessions, label="corporate action evidence")
            event_days: set[tuple[str, str]] = set()
            missing_actions = [
                (session, code)
                for session in sessions
                for code in member_by_session[session] - action_coverage[session]
            ]
            for row in action_rows:
                if row.get("evidence_kind") not in {"event", "confirmed_no_event"}:
                    raise ApprovedArtifactError("corporate action evidence kind is invalid")
                if row.get("evidence_kind") == "event" and not str(row.get("reference_id") or ""):
                    raise ApprovedArtifactError("corporate action event reference is missing")
                if row.get("evidence_kind") == "event":
                    if row.get("effective_from") != row.get("effective_to"):
                        raise ApprovedArtifactError(
                            "corporate action event must cover exactly one effective date"
                        )
                    event_days.add(
                        (
                            str(row["effective_from"]),
                            _code(row.get("security_code")),
                        )
                    )
            if missing_actions:
                self._block(
                    blockers,
                    "corporate_action_member_session_missing",
                    "event or explicit no-event evidence is missing",
                    missing_count=len(missing_actions),
                    examples=missing_actions[:20],
                )
            price_by_code: dict[str, list[tuple[str, float]]] = defaultdict(list)
            for (session, code), row in prices.items():
                price_by_code[code].append(
                    (session, float(row["adjustment_factor"]))
                )
            unexplained_changes: list[tuple[str, str]] = []
            for code, observations in price_by_code.items():
                previous_factor: float | None = None
                for session, factor in sorted(observations):
                    if (
                        previous_factor is not None
                        and not math.isclose(factor, previous_factor, rel_tol=1e-12)
                        and (session, code) not in event_days
                    ):
                        unexplained_changes.append((session, code))
                    previous_factor = factor
            if unexplained_changes:
                self._block(
                    blockers,
                    "adjustment_factor_change_unexplained",
                    "adjustment factor changes require effective corporate-action evidence",
                    missing_count=len(unexplained_changes),
                    examples=unexplained_changes[:20],
                )
        except (ApprovedArtifactError, KeyError, TypeError) as exc:
            self._block(blockers, "corporate_action_evidence_invalid", str(exc))

    @staticmethod
    def _rows(
        artifacts: Mapping[
            tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]
        ],
        identity: tuple[str, str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _manifest, payload in artifacts[identity]:
            rows.extend(payload["rows"])
        return rows

    def activate(
        self,
        bundle: Mapping[str, Any],
        *,
        confirmation_plan_sha256: str,
        registry: "AtomicPitReleaseRegistry",
        actor_user_id: int,
    ) -> dict[str, Any]:
        """Revalidate and atomically record a ready release authorisation."""

        report = self.dry_run(bundle)
        if not report["ready"]:
            raise ReleaseActivationBlocked(
                f"release has {report['blocker_count']} unresolved blocker(s)"
            )
        if confirmation_plan_sha256 != report["plan_sha256"]:
            raise ReleaseActivationBlocked("activation confirmation does not match current plan")
        return registry.activate(
            report=report,
            actor_user_id=actor_user_id,
            _authorization=_ActivationAuthorization(
                plan_sha256=report["plan_sha256"],
                token=_ACTIVATION_TOKEN,
            ),
        )


_REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS pit_release_registry_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pit_release_authorizations (
    plan_sha256 TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    authorised_by_user_id INTEGER NOT NULL,
    authorised_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pit_release_authorized_artifacts (
    plan_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    PRIMARY KEY (plan_sha256, manifest_sha256),
    FOREIGN KEY (plan_sha256) REFERENCES pit_release_authorizations(plan_sha256)
);
CREATE TRIGGER IF NOT EXISTS pit_release_authorizations_no_update
BEFORE UPDATE ON pit_release_authorizations
BEGIN SELECT RAISE(ABORT, 'PIT release authorization is immutable'); END;
CREATE TRIGGER IF NOT EXISTS pit_release_authorizations_no_delete
BEFORE DELETE ON pit_release_authorizations
BEGIN SELECT RAISE(ABORT, 'PIT release authorization cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS pit_release_artifacts_no_update
BEFORE UPDATE ON pit_release_authorized_artifacts
BEGIN SELECT RAISE(ABORT, 'PIT release artifact binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS pit_release_artifacts_no_delete
BEFORE DELETE ON pit_release_authorized_artifacts
BEGIN SELECT RAISE(ABORT, 'PIT release artifact binding cannot be deleted'); END;
"""


class AtomicPitReleaseRegistry:
    """Dedicated append-only registry; never opens an application database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def load_authorization(self, plan_sha256: str) -> dict[str, Any]:
        """Load and verify one immutable authorization without creating files."""

        if not _SHA256.fullmatch(str(plan_sha256)) or not self.path.is_file():
            raise ReleaseActivationBlocked("release authorization is unavailable")
        try:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=15,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as exc:
            raise ReleaseActivationBlocked(
                "release authorization registry is unreadable"
            ) from exc
        try:
            with connection:
                self._assert_dedicated(connection)
                schema = connection.execute(
                    """
                    SELECT schema_version FROM pit_release_registry_metadata
                    WHERE singleton=1
                    """
                ).fetchone()
                if schema is None or schema[0] != REGISTRY_SCHEMA:
                    raise ReleaseActivationBlocked(
                        "release registry schema changed"
                    )
                row = connection.execute(
                    """
                    SELECT * FROM pit_release_authorizations
                    WHERE plan_sha256=?
                    """,
                    (plan_sha256,),
                ).fetchone()
                if row is None:
                    raise ReleaseActivationBlocked(
                        "release authorization is unavailable"
                    )
                try:
                    plan = json.loads(str(row["plan_json"]))
                except json.JSONDecodeError as exc:
                    raise ReleaseActivationBlocked(
                        "release authorization plan is unreadable"
                    ) from exc
                if not isinstance(plan, dict) or _digest(plan) != plan_sha256:
                    raise ReleaseActivationBlocked(
                        "release authorization plan integrity mismatch"
                    )
                bindings = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT manifest_sha256, payload_sha256,
                               artifact_kind, scope_id
                        FROM pit_release_authorized_artifacts
                        WHERE plan_sha256=?
                        ORDER BY artifact_kind, scope_id, manifest_sha256
                        """,
                        (plan_sha256,),
                    ).fetchall()
                ]
                planned = sorted(
                    (
                        str(item.get("manifest_sha256") or ""),
                        str(item.get("payload_sha256") or ""),
                        str(item.get("artifact_kind") or ""),
                        str(item.get("scope_id") or ""),
                    )
                    for item in plan.get("artifacts", [])
                    if isinstance(item, Mapping)
                )
                recorded = sorted(
                    (
                        item["manifest_sha256"],
                        item["payload_sha256"],
                        item["artifact_kind"],
                        item["scope_id"],
                    )
                    for item in bindings
                )
                if not bindings or recorded != planned:
                    raise ReleaseActivationBlocked(
                        "release authorization artifact bindings are incomplete"
                    )
                return {
                    "schema_version": REGISTRY_SCHEMA,
                    "plan_sha256": plan_sha256,
                    "plan": plan,
                    "authorised_by_user_id": int(
                        row["authorised_by_user_id"]
                    ),
                    "authorised_at": str(row["authorised_at"]),
                    "artifacts": bindings,
                }
        except sqlite3.Error as exc:
            raise ReleaseActivationBlocked(
                "release authorization registry cannot be verified"
            ) from exc

    @staticmethod
    def _assert_dedicated(connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        allowed = {
            "pit_release_registry_metadata",
            "pit_release_authorizations",
            "pit_release_authorized_artifacts",
        }
        if tables - allowed:
            raise ReleaseActivationBlocked("registry path contains non-release tables")

    def activate(
        self,
        *,
        report: Mapping[str, Any],
        actor_user_id: int,
        _authorization: _ActivationAuthorization | None = None,
    ) -> dict[str, Any]:
        if report.get("schema_version") != READINESS_SCHEMA or report.get("ready") is not True:
            raise ReleaseActivationBlocked("only a ready dry-run report may be authorised")
        if report.get("runtime_data_changed") is not False:
            raise ReleaseActivationBlocked("dry-run report unexpectedly changed runtime data")
        plan = report.get("plan")
        plan_sha256 = str(report.get("plan_sha256") or "")
        if not isinstance(plan, Mapping) or _digest(plan) != plan_sha256:
            raise ReleaseActivationBlocked("release plan integrity mismatch")
        if (
            _authorization is None
            or _authorization.token is not _ACTIVATION_TOKEN
            or _authorization.plan_sha256 != plan_sha256
        ):
            raise ReleaseActivationBlocked(
                "release authorization must come from a fresh orchestrator validation"
            )
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            self._assert_dedicated(connection)
            connection.executescript(_REGISTRY_SQL)
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO pit_release_registry_metadata VALUES (1, ?)",
                (REGISTRY_SCHEMA,),
            )
            schema = connection.execute(
                "SELECT schema_version FROM pit_release_registry_metadata WHERE singleton=1"
            ).fetchone()
            if schema is None or schema[0] != REGISTRY_SCHEMA:
                raise ReleaseActivationBlocked("release registry schema changed")
            existing = connection.execute(
                "SELECT plan_json FROM pit_release_authorizations WHERE plan_sha256=?",
                (plan_sha256,),
            ).fetchone()
            plan_json = _canonical_bytes(plan).decode("utf-8")
            if existing is not None:
                if existing[0] != plan_json:
                    raise ReleaseActivationBlocked("existing release plan differs")
                connection.rollback()
                return {
                    "schema_version": REGISTRY_SCHEMA,
                    "plan_sha256": plan_sha256,
                    "authorised": True,
                    "idempotent": True,
                    "runtime_materialised": False,
                }
            connection.execute(
                """
                INSERT INTO pit_release_authorizations (
                    plan_sha256, plan_json, authorised_by_user_id, authorised_at
                ) VALUES (?, ?, ?, ?)
                """,
                (plan_sha256, plan_json, int(actor_user_id), now),
            )
            for artifact in plan["artifacts"]:
                connection.execute(
                    """
                    INSERT INTO pit_release_authorized_artifacts (
                        plan_sha256, manifest_sha256, payload_sha256,
                        artifact_kind, scope_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        plan_sha256,
                        artifact["manifest_sha256"],
                        artifact["payload_sha256"],
                        artifact["artifact_kind"],
                        artifact["scope_id"],
                    ),
                )
            connection.commit()
        return {
            "schema_version": REGISTRY_SCHEMA,
            "plan_sha256": plan_sha256,
            "authorised": True,
            "idempotent": False,
            "runtime_materialised": False,
        }
