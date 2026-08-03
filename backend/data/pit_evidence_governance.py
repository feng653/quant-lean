"""Managed evidence and approval workflow for official PIT imports.

Large source artifacts live in a content-addressed filesystem store, not in the
experiment database.  A separate SQLite governance journal binds those bytes
to an immutable staging package and records compare-and-swap decisions.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from backend.config import settings
from backend.data.point_in_time_master import (
    PointInTimeMasterStore,
    _authorize_governed_import,
)
from backend.data.sources.csindex_pit import (
    ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION,
    AUTHORITATIVE_CALENDAR_LEVELS,
    HISTORICAL_REPLAY_PACKAGE_KIND,
    INDEPENDENT_ROW_REVIEW_METHOD,
    PARSER_VERSION,
    STAGING_SCHEMA_VERSION,
    TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION,
    UNATTESTED_REVIEW_METHOD,
    AdjustmentAnnouncement,
    ArtifactEvidence,
    Constituent,
    CurrentAnchor,
    CsindexEvidenceError,
    ScopeAdjustment,
    archive_review_manifest_sha256,
    build_staging_package,
    canonical_archive_review_rows,
    is_automatic_target_archive_row,
    parse_announcement_metadata,
    parse_archive_pages,
    parse_current_constituent_xls,
    validate_archive_review_decisions,
)

GOVERNANCE_SCHEMA_VERSION = "pit-evidence-governance/v1"
APPROVAL_ATTESTATION_SCHEMA_VERSION = "pit-evidence-attestation/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_ID = re.compile(r"^pitpkg_[0-9a-f]{32}$")
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


class PitEvidenceError(RuntimeError):
    """Base fail-closed governance error."""


class PitEvidenceIntegrityError(PitEvidenceError):
    """Stored evidence or an immutable package no longer matches its digest."""


class PitEvidenceConflictError(PitEvidenceError):
    """A CAS decision or package state transition lost a race."""


class PitEvidenceStateError(PitEvidenceError):
    """The requested operation is forbidden in the current package state."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


GOVERNANCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pit_evidence_artifacts (
    content_sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    first_recorded_at TEXT NOT NULL,
    first_recorded_by INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pit_evidence_packages (
    package_id TEXT PRIMARY KEY,
    package_sha256 TEXT NOT NULL UNIQUE,
    package_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'imported')
    ),
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    decided_at TEXT,
    decided_by INTEGER,
    decision_reason TEXT,
    decision_attestations_json TEXT,
    imported_at TEXT,
    imported_by INTEGER
);

CREATE TABLE IF NOT EXISTS pit_evidence_auxiliary_artifacts (
    content_sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('trading_calendar', 'review_decisions')),
    provenance_json TEXT NOT NULL,
    provenance_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    recorded_by INTEGER NOT NULL,
    FOREIGN KEY (content_sha256)
        REFERENCES pit_evidence_artifacts(content_sha256)
);

CREATE TABLE IF NOT EXISTS pit_evidence_package_artifacts (
    package_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (package_id, content_sha256),
    FOREIGN KEY (package_id)
        REFERENCES pit_evidence_packages(package_id),
    FOREIGN KEY (content_sha256)
        REFERENCES pit_evidence_artifacts(content_sha256)
);

CREATE TABLE IF NOT EXISTS pit_evidence_package_imports (
    package_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    batch_digest TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (package_id, scope_id),
    FOREIGN KEY (package_id)
        REFERENCES pit_evidence_packages(package_id)
);

CREATE TABLE IF NOT EXISTS pit_evidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    package_id TEXT,
    actor_user_id INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pit_evidence_events_package
ON pit_evidence_events(package_id, id);

CREATE TRIGGER IF NOT EXISTS pit_evidence_artifacts_no_update
BEFORE UPDATE ON pit_evidence_artifacts
BEGIN
    SELECT RAISE(ABORT, 'PIT artifact identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_artifacts_no_delete
BEFORE DELETE ON pit_evidence_artifacts
BEGIN
    SELECT RAISE(ABORT, 'PIT artifact identity cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_auxiliary_artifacts_no_update
BEFORE UPDATE ON pit_evidence_auxiliary_artifacts
BEGIN
    SELECT RAISE(ABORT, 'PIT auxiliary provenance is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_auxiliary_artifacts_no_delete
BEFORE DELETE ON pit_evidence_auxiliary_artifacts
BEGIN
    SELECT RAISE(ABORT, 'PIT auxiliary provenance cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_package_payload_immutable
BEFORE UPDATE OF
    package_id, package_sha256, package_json, created_at, created_by
ON pit_evidence_packages
BEGIN
    SELECT RAISE(ABORT, 'PIT package payload is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_packages_no_delete
BEFORE DELETE ON pit_evidence_packages
BEGIN
    SELECT RAISE(ABORT, 'PIT package cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_package_artifacts_no_update
BEFORE UPDATE ON pit_evidence_package_artifacts
BEGIN
    SELECT RAISE(ABORT, 'PIT package evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_package_artifacts_no_delete
BEFORE DELETE ON pit_evidence_package_artifacts
BEGIN
    SELECT RAISE(ABORT, 'PIT package evidence cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_package_imports_no_update
BEFORE UPDATE ON pit_evidence_package_imports
BEGIN
    SELECT RAISE(ABORT, 'PIT package import receipt is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_package_imports_no_delete
BEFORE DELETE ON pit_evidence_package_imports
BEGIN
    SELECT RAISE(ABORT, 'PIT package import receipt cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_events_no_update
BEFORE UPDATE ON pit_evidence_events
BEGIN
    SELECT RAISE(ABORT, 'PIT evidence event is append-only');
END;

CREATE TRIGGER IF NOT EXISTS pit_evidence_events_no_delete
BEFORE DELETE ON pit_evidence_events
BEGIN
    SELECT RAISE(ABORT, 'PIT evidence event is append-only');
END;
"""

PIT_APPROVAL_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS pit_evidence_attestations_once
BEFORE UPDATE OF decision_attestations_json
ON pit_evidence_packages
WHEN OLD.status != 'pending'
  OR OLD.decision_attestations_json IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'PIT approval attestations are immutable');
END;
"""


def _validate_approval_attestations(value: Any) -> dict[str, Any]:
    required = {
        "schema_version": APPROVAL_ATTESTATION_SCHEMA_VERSION,
        "all_adjustment_rows_reviewed": True,
        "archive_completeness_reviewed": True,
        "source_terms_acknowledged": True,
        "local_research_only": True,
        "redistribution_not_authorized": True,
    }
    if (
        not isinstance(value, dict)
        or set(value) != set(required)
        or value.get("schema_version")
        != APPROVAL_ATTESTATION_SCHEMA_VERSION
        or any(
            value.get(key) is not True
            for key in required
            if key != "schema_version"
        )
    ):
        raise PitEvidenceStateError(
            "all structured approval attestations must be explicit and true"
        )
    return required


class ContentAddressedArtifactStore:
    """Atomic, no-follow SHA-256 artifact storage under one managed root."""

    def __init__(self, root: Path | None = None) -> None:
        configured = root or settings.abs_path(settings.PIT_EVIDENCE_DIR)
        self.root = Path(configured).absolute()
        self._initialize_root()
        self.content_root = self.root / "artifacts" / "sha256"
        self.content_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_managed_directory(self.content_root)

    @staticmethod
    def _assert_managed_directory(path: Path) -> None:
        current = path
        while True:
            if current.is_symlink():
                raise PitEvidenceIntegrityError(
                    "managed evidence path contains a symbolic link"
                )
            if current.parent == current:
                break
            current = current.parent

    def _initialize_root(self) -> None:
        if self.root.exists() and self.root.is_symlink():
            raise PitEvidenceIntegrityError(
                "managed evidence root cannot be a symbolic link"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir():
            raise PitEvidenceIntegrityError(
                "managed evidence root is not a directory"
            )
        self._assert_managed_directory(self.root)

    @staticmethod
    def _validate_digest(content_sha256: str) -> str:
        if not _SHA256.fullmatch(content_sha256):
            raise PitEvidenceIntegrityError("artifact digest is invalid")
        return content_sha256

    def _path(self, content_sha256: str) -> Path:
        digest = self._validate_digest(content_sha256)
        return self.content_root / digest[:2] / digest

    @staticmethod
    def _supports_secure_directory_fd() -> bool:
        """Return whether the host supports the POSIX dir-fd publish path."""

        return bool(
            hasattr(os, "O_DIRECTORY")
            and os.open in os.supports_dir_fd
            and os.link in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd
        )

    @staticmethod
    def _write_payload(file_fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(file_fd, view[written:])

    def _publish_portable(
        self,
        *,
        path: Path,
        temporary: str,
        payload: bytes,
    ) -> None:
        """Publish without replacement on hosts that lack POSIX dir-fds."""

        temporary_path = path.parent / temporary
        file_fd: int | None = None
        try:
            file_fd = os.open(
                temporary_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOINHERIT", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            self._write_payload(file_fd, payload)
            os.fsync(file_fd)
            os.close(file_fd)
            file_fd = None
            self._assert_managed_directory(path.parent)
            try:
                os.link(
                    temporary_path,
                    path,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
        finally:
            if file_fd is not None:
                os.close(file_fd)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def put(self, payload: bytes, *, expected_sha256: str) -> dict[str, Any]:
        digest = self._validate_digest(expected_sha256)
        if not payload or len(payload) > _MAX_ARTIFACT_BYTES:
            raise PitEvidenceIntegrityError("artifact payload size is invalid")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise PitEvidenceIntegrityError("artifact payload digest mismatch")
        path = self._path(digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_managed_directory(path.parent)
        if path.exists() or path.is_symlink():
            existing = self.read(digest)
            return {
                "content_sha256": digest,
                "size_bytes": len(existing),
                "idempotent": True,
            }

        temporary = f".{digest}.{uuid.uuid4().hex}.tmp"
        if not self._supports_secure_directory_fd():
            self._publish_portable(
                path=path,
                temporary=temporary,
                payload=payload,
            )
            stored = self.read(digest)
            if stored != payload:
                raise PitEvidenceIntegrityError(
                    "artifact atomic write mismatch"
                )
            return {
                "content_sha256": digest,
                "size_bytes": len(stored),
                "idempotent": False,
            }

        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        file_fd: int | None = None
        try:
            file_fd = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            self._write_payload(file_fd, payload)
            os.fsync(file_fd)
            os.close(file_fd)
            file_fd = None
            try:
                os.link(
                    temporary,
                    digest,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if file_fd is not None:
                os.close(file_fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)
        stored = self.read(digest)
        if stored != payload:
            raise PitEvidenceIntegrityError("artifact atomic write mismatch")
        return {
            "content_sha256": digest,
            "size_bytes": len(stored),
            "idempotent": False,
        }

    def read(self, content_sha256: str) -> bytes:
        path = self._path(content_sha256)
        try:
            file_fd = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOINHERIT", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except (FileNotFoundError, OSError) as exc:
            raise PitEvidenceIntegrityError("managed artifact is unavailable") from exc
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PitEvidenceIntegrityError(
                    "managed artifact is not a regular file"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(file_fd)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != content_sha256:
            raise PitEvidenceIntegrityError("managed artifact integrity mismatch")
        return payload


def _manifest_artifact_digests(package: dict[str, Any]) -> set[str]:
    manifest = package.get("evidence_manifest")
    if not isinstance(manifest, dict):
        raise PitEvidenceIntegrityError("package evidence manifest is missing")
    result: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            role = value.get("role")
            digest = value.get("content_sha256")
            if role is not None and digest is not None:
                if not _SHA256.fullmatch(str(digest)):
                    raise PitEvidenceIntegrityError(
                        "manifest artifact digest is invalid"
                    )
                result.add(str(digest))
                request_digest = value.get("request_sha256")
                if request_digest is not None:
                    if not _SHA256.fullmatch(str(request_digest)):
                        raise PitEvidenceIntegrityError(
                            "manifest request digest is invalid"
                        )
                    result.add(str(request_digest))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(manifest)
    if not result:
        raise PitEvidenceIntegrityError("package contains no artifact evidence")
    return result


def _artifact_payloads(
    artifacts: Sequence[ArtifactEvidence],
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for artifact in artifacts:
        payloads[artifact.content_sha256] = artifact.payload
        if artifact.request_payload is not None:
            assert artifact.request_sha256 is not None
            payloads[artifact.request_sha256] = artifact.request_payload
    return payloads


def _derived_auxiliary_payloads(
    package: dict[str, Any],
) -> dict[str, tuple[str, bytes]]:
    manifest = package.get("evidence_manifest")
    if not isinstance(manifest, dict):
        return {}
    result: dict[str, tuple[str, bytes]] = {}
    calendar = manifest.get("trading_calendar")
    if (
        isinstance(calendar, dict)
        and calendar.get("role") == "trading_calendar"
        and calendar.get("provider") == "explicit_unattested_input"
        and isinstance(calendar.get("sessions"), list)
    ):
        payload = _canonical_json(calendar["sessions"]).encode()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != calendar.get("content_sha256"):
            raise PitEvidenceIntegrityError(
                "derived trading calendar artifact digest differs"
            )
        result[digest] = ("trading_calendar", payload)
    review = manifest.get("review_evidence")
    announcements = manifest.get("announcements")
    if (
        isinstance(review, dict)
        and review.get("role") == "review_decisions"
        and review.get("review_method") == UNATTESTED_REVIEW_METHOD
        and isinstance(announcements, list)
    ):
        payload_value = [
            {
                "announcement_id": item.get("announcement_id"),
                "changes": item.get("changes"),
            }
            for item in announcements
            if isinstance(item, dict)
        ]
        payload = _canonical_json(payload_value).encode()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != review.get("content_sha256"):
            raise PitEvidenceIntegrityError(
                "derived review artifact digest differs"
            )
        result[digest] = ("review_decisions", payload)
    return result


class PitEvidenceGovernance:
    """Immutable package journal, explicit approval and verified import."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        database_path: Path | None = None,
        master_store: PointInTimeMasterStore | None = None,
        trusted_calendar_keys: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.artifacts = ContentAddressedArtifactStore(root)
        configured_db = database_path or settings.abs_path(
            settings.PIT_EVIDENCE_DB
        )
        self.database_path = Path(configured_db)
        try:
            self.database_path.absolute().relative_to(self.artifacts.root)
        except ValueError as exc:
            raise PitEvidenceIntegrityError(
                "governance database must remain inside the managed root"
            ) from exc
        self.database_path = self.database_path.absolute()
        if self.database_path.exists() and self.database_path.is_symlink():
            raise PitEvidenceIntegrityError(
                "governance database cannot be a symbolic link"
            )
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts._assert_managed_directory(self.database_path.parent)
        self.master_store = master_store or PointInTimeMasterStore()
        self.trusted_calendar_keys = self._normalize_calendar_trust_registry(
            trusted_calendar_keys
        )
        self.initialize()

    @staticmethod
    def _normalize_calendar_trust_registry(
        configured: Mapping[str, Mapping[str, str]] | None,
    ) -> dict[str, dict[str, str]]:
        if configured is None:
            try:
                raw = json.loads(settings.PIT_CALENDAR_TRUSTED_KEYS_JSON)
            except json.JSONDecodeError as exc:
                raise PitEvidenceIntegrityError(
                    "calendar trust registry is invalid JSON"
                ) from exc
        else:
            raw = dict(configured)
        if not isinstance(raw, dict):
            raise PitEvidenceIntegrityError(
                "calendar trust registry must be an object"
            )
        normalized: dict[str, dict[str, str]] = {}
        for key_id, value in raw.items():
            if (
                not isinstance(key_id, str)
                or not key_id.strip()
                or not isinstance(value, dict)
                or set(value)
                != {"public_key_base64", "provider", "evidence_level"}
                or value.get("evidence_level")
                not in AUTHORITATIVE_CALENDAR_LEVELS
                or not str(value.get("provider") or "").strip()
            ):
                raise PitEvidenceIntegrityError(
                    "calendar trust registry entry is invalid"
                )
            try:
                public_key = base64.b64decode(
                    str(value.get("public_key_base64") or ""),
                    validate=True,
                )
                Ed25519PublicKey.from_public_bytes(public_key)
            except (binascii.Error, ValueError) as exc:
                raise PitEvidenceIntegrityError(
                    "calendar trust registry public key is invalid"
                ) from exc
            normalized[key_id] = {
                "public_key_base64": base64.b64encode(public_key).decode(),
                "provider": str(value["provider"]),
                "evidence_level": str(value["evidence_level"]),
            }
        return normalized

    def verify_trading_calendar_payload(
        self,
        payload: bytes,
    ) -> tuple[dict[str, Any], list[str], str]:
        """Verify a calendar against a provisioned Ed25519 trust anchor."""

        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PitEvidenceIntegrityError(
                "trading calendar artifact is invalid JSON"
            ) from exc
        source = document.get("source") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or set(document)
            != {"schema_version", "source", "trading_days", "signature"}
            or document.get("schema_version")
            != "authoritative-trading-calendar/v2"
            or not isinstance(source, dict)
            or set(source)
            != {
                "provider",
                "evidence_level",
                "version",
                "retrieved_at",
                "signature_key_id",
            }
        ):
            raise PitEvidenceIntegrityError(
                "trading calendar signed schema is invalid"
            )
        key_id = str(source.get("signature_key_id") or "")
        trust = self.trusted_calendar_keys.get(key_id)
        if (
            trust is None
            or source.get("provider") != trust["provider"]
            or source.get("evidence_level") != trust["evidence_level"]
            or not str(source.get("version") or "").strip()
        ):
            raise PitEvidenceIntegrityError(
                "trading calendar signer is not governed and trusted"
            )
        try:
            parsed_retrieved = datetime.fromisoformat(
                str(source.get("retrieved_at") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise PitEvidenceIntegrityError(
                "trading calendar retrieved_at is invalid"
            ) from exc
        if parsed_retrieved.tzinfo is None:
            raise PitEvidenceIntegrityError(
                "trading calendar retrieved_at must include timezone"
            )
        sessions = document.get("trading_days")
        if not isinstance(sessions, list) or not sessions:
            raise PitEvidenceIntegrityError(
                "trading calendar sessions are missing"
            )
        try:
            parsed_sessions = [date.fromisoformat(str(item)) for item in sessions]
        except ValueError as exc:
            raise PitEvidenceIntegrityError(
                "trading calendar session is invalid"
            ) from exc
        normalized_sessions = [item.isoformat() for item in parsed_sessions]
        if normalized_sessions != sorted(set(normalized_sessions)):
            raise PitEvidenceIntegrityError(
                "trading calendar sessions must be sorted and unique"
            )
        signed_document = {
            "schema_version": document["schema_version"],
            "source": source,
            "trading_days": normalized_sessions,
        }
        signed_payload = _canonical_json(signed_document).encode()
        try:
            signature = base64.b64decode(
                str(document.get("signature") or ""),
                validate=True,
            )
            public_key = base64.b64decode(
                trust["public_key_base64"],
                validate=True,
            )
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                signed_payload,
            )
        except (binascii.Error, InvalidSignature, ValueError) as exc:
            raise PitEvidenceIntegrityError(
                "trading calendar signature is invalid"
            ) from exc
        return (
            document,
            normalized_sessions,
            hashlib.sha256(signed_payload).hexdigest(),
        )

    def _connect(self) -> sqlite3.Connection:
        if self.database_path.exists() and self.database_path.is_symlink():
            raise PitEvidenceIntegrityError(
                "governance database cannot be a symbolic link"
            )
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(GOVERNANCE_SCHEMA_SQL)
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(pit_evidence_packages)"
                ).fetchall()
            }
            if "decision_attestations_json" not in columns:
                connection.execute(
                    """
                    ALTER TABLE pit_evidence_packages
                    ADD COLUMN decision_attestations_json TEXT
                    """
                )
            connection.executescript(PIT_APPROVAL_TRIGGER_SQL)
        os.chmod(self.database_path, 0o600)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        package_id: str | None,
        actor_user_id: int,
        event: dict[str, Any],
    ) -> None:
        event_json = _canonical_json(event)
        connection.execute(
            """
            INSERT INTO pit_evidence_events (
                event_type, package_id, actor_user_id, event_json,
                event_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                package_id,
                int(actor_user_id),
                event_json,
                hashlib.sha256(event_json.encode()).hexdigest(),
                utc_now_iso(),
            ),
        )

    def _artifact_from_manifest(
        self,
        value: Any,
    ) -> ArtifactEvidence:
        if not isinstance(value, dict):
            raise PitEvidenceIntegrityError(
                "artifact manifest row is invalid"
            )
        digest = str(value.get("content_sha256") or "")
        request_digest = value.get("request_sha256")
        try:
            published_on = (
                date.fromisoformat(str(value["published_on"]))
                if value.get("published_on") is not None
                else None
            )
            return ArtifactEvidence(
                role=str(value["role"]),  # type: ignore[arg-type]
                url=str(value["url"]),
                retrieved_at=str(value["retrieved_at"]),
                content_sha256=digest,
                payload=self.artifacts.read(digest),
                announcement_id=(
                    str(value["announcement_id"])
                    if value.get("announcement_id") is not None
                    else None
                ),
                published_on=published_on,
                request_payload=(
                    self.artifacts.read(str(request_digest))
                    if request_digest is not None
                    else None
                ),
                request_sha256=(
                    str(request_digest)
                    if request_digest is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PitEvidenceIntegrityError(
                "artifact manifest row cannot be reconstructed"
            ) from exc

    @staticmethod
    def _changes_from_manifest(value: Any) -> dict[str, ScopeAdjustment]:
        if not isinstance(value, dict):
            raise PitEvidenceIntegrityError(
                "reviewed adjustment changes are invalid"
            )
        result: dict[str, ScopeAdjustment] = {}
        for scope_id, rows in value.items():
            if not isinstance(rows, dict) or set(rows) != {
                "additions",
                "removals",
            }:
                raise PitEvidenceIntegrityError(
                    "reviewed adjustment change schema is invalid"
                )

            def constituents(key: str) -> tuple[Constituent, ...]:
                source = rows[key]
                if not isinstance(source, list):
                    raise PitEvidenceIntegrityError(
                        "reviewed adjustment rows are invalid"
                    )
                members: list[Constituent] = []
                for item in source:
                    if not isinstance(item, dict) or set(item) != {
                        "security_code",
                        "member_name",
                    }:
                        raise PitEvidenceIntegrityError(
                            "reviewed adjustment row is invalid"
                        )
                    members.append(
                        Constituent(
                            str(item["security_code"]),
                            str(item["member_name"]),
                        )
                    )
                return tuple(members)

            result[str(scope_id)] = ScopeAdjustment(
                additions=constituents("additions"),
                removals=constituents("removals"),
            )
        return result

    def _rebuild_package_from_evidence(
        self,
        package: dict[str, Any],
    ) -> dict[str, Any]:
        """Replay retained bytes and compare every generated import record."""

        manifest = package.get("evidence_manifest")
        if not isinstance(manifest, dict):
            raise PitEvidenceIntegrityError(
                "package evidence manifest is missing"
            )
        try:
            anchor_rows = manifest["anchors"]
            archive_row = manifest["archive"]
            announcement_rows = manifest["announcements"]
            calendar = manifest["trading_calendar"]
            review_evidence = manifest["review_evidence"]
        except KeyError as exc:
            raise PitEvidenceIntegrityError(
                "package replay evidence is incomplete"
            ) from exc
        if (
            not isinstance(anchor_rows, list)
            or not isinstance(archive_row, dict)
            or not isinstance(announcement_rows, list)
            or not isinstance(calendar, dict)
            or not isinstance(review_evidence, dict)
        ):
            raise PitEvidenceIntegrityError(
                "package replay evidence schema is invalid"
            )
        anchors: dict[str, CurrentAnchor] = {}
        for anchor_row in anchor_rows:
            if not isinstance(anchor_row, dict):
                raise PitEvidenceIntegrityError(
                    "package anchor manifest is invalid"
                )
            scope_id = str(anchor_row.get("scope_id") or "")
            artifact = self._artifact_from_manifest(
                anchor_row.get("artifact")
            )
            try:
                anchor = parse_current_constituent_xls(
                    scope_id=scope_id,  # type: ignore[arg-type]
                    artifact=artifact,
                )
            except CsindexEvidenceError as exc:
                raise PitEvidenceIntegrityError(
                    "retained current anchor cannot be replayed"
                ) from exc
            if (
                scope_id in anchors
                or anchor.observed_on.isoformat()
                != anchor_row.get("observed_on")
            ):
                raise PitEvidenceIntegrityError(
                    "replayed current anchor identity differs"
                )
            anchors[scope_id] = anchor
        page_rows = archive_row.get("pages")
        adjustment_ids = archive_row.get("adjustment_announcement_ids")
        if not isinstance(page_rows, list) or not isinstance(
            adjustment_ids,
            list,
        ):
            raise PitEvidenceIntegrityError(
                "archive replay manifest is invalid"
            )
        pages = [self._artifact_from_manifest(item) for item in page_rows]
        try:
            archive = parse_archive_pages(
                pages=pages,
                adjustment_announcement_ids=[
                    str(item) for item in adjustment_ids
                ],
                coverage_from=date.fromisoformat(
                    str(archive_row["coverage_from"])
                ),
                coverage_to=date.fromisoformat(
                    str(archive_row["coverage_to"])
                ),
            )
        except (CsindexEvidenceError, KeyError, ValueError) as exc:
            raise PitEvidenceIntegrityError(
                "retained archive cannot be replayed"
            ) from exc
        if archive.manifest() != archive_row:
            raise PitEvidenceIntegrityError(
                "replayed archive differs from package manifest"
            )
        announcements: list[AdjustmentAnnouncement] = []
        proposal_sha256_by_id: dict[str, str] = {}
        for announcement_row in announcement_rows:
            if not isinstance(announcement_row, dict):
                raise PitEvidenceIntegrityError(
                    "announcement replay manifest is invalid"
                )
            announcement = self._artifact_from_manifest(
                announcement_row.get("announcement")
            )
            attachment_rows = announcement_row.get("attachments")
            if not isinstance(attachment_rows, list):
                raise PitEvidenceIntegrityError(
                    "announcement attachments are invalid"
                )
            attachments = [
                self._artifact_from_manifest(item)
                for item in attachment_rows
            ]
            changes = self._changes_from_manifest(
                announcement_row.get("changes")
            )
            try:
                # Lazy import avoids a module-import cycle: the history
                # orchestrator itself depends on this governance service.
                from backend.data.sources.csindex_history import (
                    CsindexAttachmentSchemaError,
                    adjustment_review_proposal,
                    parse_adjustment_attachments,
                )

                parsed_changes, parser_evidence = (
                    parse_adjustment_attachments(
                        attachments=attachments,
                        expected_counts={
                            str(scope_id): int(count)
                            for scope_id, count in dict(
                                announcement_row.get("announced_counts") or {}
                            ).items()
                        },
                    )
                )
                if parsed_changes != changes:
                    raise PitEvidenceIntegrityError(
                        "reviewed rows differ from retained attachment tables"
                    )
                rebuilt = parse_announcement_metadata(
                    announcement=announcement,
                    attachments=attachments,
                    reviewed_changes=changes,
                )
            except (
                CsindexEvidenceError,
                CsindexAttachmentSchemaError,
            ) as exc:
                raise PitEvidenceIntegrityError(
                    "retained adjustment announcement cannot be replayed"
                ) from exc
            if rebuilt.manifest() != announcement_row:
                raise PitEvidenceIntegrityError(
                    "replayed adjustment differs from package manifest"
                )
            proposal_sha256_by_id[rebuilt.announcement_id] = (
                _canonical_sha256(
                    adjustment_review_proposal(rebuilt, parser_evidence)
                )
            )
            announcements.append(rebuilt)
        sessions = calendar.get("sessions")
        if not isinstance(sessions, list):
            raise PitEvidenceIntegrityError(
                "trading calendar sessions are missing"
            )
        try:
            trading_days = [date.fromisoformat(str(item)) for item in sessions]
        except ValueError as exc:
            raise PitEvidenceIntegrityError(
                "trading calendar session is invalid"
            ) from exc
        if (
            trading_days != sorted(set(trading_days))
            or _canonical_sha256(sessions)
            != calendar.get("sessions_sha256")
            or not str(calendar.get("provider") or "").strip()
            or not str(calendar.get("evidence_level") or "").strip()
            or not str(calendar.get("version") or "").strip()
            or not str(calendar.get("signature_key_id") or "").strip()
            or not _SHA256.fullmatch(
                str(calendar.get("signed_payload_sha256") or "")
            )
        ):
            raise PitEvidenceIntegrityError(
                "trading calendar evidence does not match its sessions"
            )
        calendar_payload = self.artifacts.read(
            str(calendar.get("content_sha256") or "")
        )
        if calendar.get("provider") == "explicit_unattested_input":
            if calendar_payload != _canonical_json(sessions).encode():
                raise PitEvidenceIntegrityError(
                    "derived trading calendar artifact differs"
                )
        else:
            try:
                (
                    calendar_document,
                    verified_sessions,
                    signed_payload_sha256,
                ) = self.verify_trading_calendar_payload(calendar_payload)
            except PitEvidenceIntegrityError as exc:
                raise PitEvidenceIntegrityError(
                    "retained trading calendar artifact is not authoritative"
                ) from exc
            source = (
                calendar_document.get("source")
                if isinstance(calendar_document, dict)
                else None
            )
            if (
                not isinstance(source, dict)
                or source.get("provider") != calendar.get("provider")
                or source.get("evidence_level")
                != calendar.get("evidence_level")
                or source.get("version") != calendar.get("version")
                or source.get("retrieved_at") != calendar.get("retrieved_at")
                or source.get("signature_key_id")
                != calendar.get("signature_key_id")
                or signed_payload_sha256
                != calendar.get("signed_payload_sha256")
                or verified_sessions != sessions
            ):
                raise PitEvidenceIntegrityError(
                    "retained trading calendar fields differ from manifest"
                )
        reviewed_payload = self.artifacts.read(
            str(review_evidence.get("content_sha256") or "")
        )
        reviewed_rows = [
            {
                "announcement_id": item.get("announcement_id"),
                "changes": item.get("changes"),
            }
            for item in announcement_rows
        ]
        if (
            _canonical_sha256(reviewed_rows)
            != review_evidence.get("reviewed_changes_sha256")
        ):
            raise PitEvidenceIntegrityError(
                "reviewed changes digest differs from announcements"
            )
        if review_evidence.get("review_method") == UNATTESTED_REVIEW_METHOD:
            if reviewed_payload != _canonical_json(reviewed_rows).encode():
                raise PitEvidenceIntegrityError(
                    "derived reviewed changes artifact differs"
                )
        else:
            try:
                review_document = json.loads(reviewed_payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PitEvidenceIntegrityError(
                    "retained review decision artifact is invalid"
                ) from exc
            if not isinstance(review_document, dict):
                raise PitEvidenceIntegrityError(
                    "retained review decision is incomplete"
                )
            try:
                dispositions, dispositions_sha256 = (
                    validate_archive_review_decisions(
                        review_document,
                        pages=archive.pages,
                        archive_manifest_sha256=(
                            archive_review_manifest_sha256(archive)
                        ),
                    )
                )
            except CsindexEvidenceError as exc:
                raise PitEvidenceIntegrityError(
                    "retained row-level review decision is incomplete"
                ) from exc
            if (
                review_evidence.get("review_method")
                != INDEPENDENT_ROW_REVIEW_METHOD
                or review_evidence.get("archive_manifest_sha256")
                != review_document.get("archive_manifest_sha256")
                or review_evidence.get("archive_review_rows_sha256")
                != review_document.get("archive_review_rows_sha256")
                or review_evidence.get("archive_row_dispositions_sha256")
                != dispositions_sha256
            ):
                raise PitEvidenceIntegrityError(
                    "retained row-level review binding differs from manifest"
                )
            imported_announcement_ids = {
                str(item.get("announcement_id") or "")
                for item in announcement_rows
            }
            target_disposition_ids = {
                announcement_id
                for announcement_id, disposition in dispositions.items()
                if disposition["disposition"] == "target_adjustment"
            }
            if target_disposition_ids != imported_announcement_ids:
                raise PitEvidenceIntegrityError(
                    "target row dispositions do not match imported announcements"
                )
            automatic_candidate_ids = {
                row["announcement_id"]
                for row in canonical_archive_review_rows(archive.pages)
                if is_automatic_target_archive_row(row)
            }
            if not automatic_candidate_ids <= target_disposition_ids:
                raise PitEvidenceIntegrityError(
                    "automatic target candidate cannot be excluded by direct "
                    "package construction"
                )
            event_decisions = review_document.get("event_decisions")
            assert isinstance(event_decisions, list)
            accepted_ids: set[str] = set()
            for item in event_decisions:
                if (
                    not isinstance(item, dict)
                    or set(item)
                    != {
                        "announcement_id",
                        "decision",
                        "proposal_sha256",
                        "reason",
                    }
                    or item.get("decision") != "accepted"
                    or not _SHA256.fullmatch(
                        str(item.get("proposal_sha256") or "")
                    )
                    or not str(item.get("reason") or "").strip()
                ):
                    raise PitEvidenceIntegrityError(
                        "retained target-event decision is invalid"
                    )
                announcement_id = str(item.get("announcement_id") or "")
                if announcement_id in accepted_ids:
                    raise PitEvidenceIntegrityError(
                        "retained target-event decision is duplicate"
                    )
                if item.get("proposal_sha256") != proposal_sha256_by_id.get(
                    announcement_id
                ):
                    raise PitEvidenceIntegrityError(
                        "retained target-event decision does not match replayed "
                        "proposal"
                    )
                accepted_ids.add(announcement_id)
            if accepted_ids != imported_announcement_ids:
                raise PitEvidenceIntegrityError(
                    "retained target-event decisions do not match imports"
                )
        imports = package.get("imports")
        if not isinstance(imports, list) or not imports:
            raise PitEvidenceIntegrityError("package imports are missing")
        coverage_from = {str(item.get("coverage_from")) for item in imports}
        coverage_to = {str(item.get("coverage_to")) for item in imports}
        if len(coverage_from) != 1 or len(coverage_to) != 1:
            raise PitEvidenceIntegrityError(
                "package import coverage is inconsistent"
            )
        try:
            rebuilt_package = build_staging_package(
                anchors=anchors,
                announcements=announcements,
                archive=archive,
                trading_days=trading_days,
                coverage_from=date.fromisoformat(coverage_from.pop()),
                coverage_to=date.fromisoformat(coverage_to.pop()),
                trading_calendar_evidence=calendar,
                review_evidence=review_evidence,
                package_kind=str(manifest.get("package_kind") or ""),
            )
        except (CsindexEvidenceError, ValueError, RuntimeError) as exc:
            raise PitEvidenceIntegrityError(
                "retained package evidence cannot rebuild imports"
            ) from exc
        if rebuilt_package != package:
            raise PitEvidenceIntegrityError(
                "package imports differ from retained evidence replay"
            )
        return rebuilt_package

    def _require_production_import_contract(
        self,
        package: dict[str, Any],
    ) -> None:
        """Reject observation-only or unattested packages before approval/import."""

        manifest = package.get("evidence_manifest")
        documents = package.get("imports")
        if not isinstance(manifest, dict) or not isinstance(documents, list):
            raise PitEvidenceStateError(
                "production PIT evidence contract is missing"
            )
        calendar = manifest.get("trading_calendar")
        review = manifest.get("review_evidence")
        if (
            manifest.get("package_kind") != HISTORICAL_REPLAY_PACKAGE_KIND
            or not isinstance(calendar, dict)
            or calendar.get("schema_version")
            != TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION
            or calendar.get("provider") == "explicit_unattested_input"
            or calendar.get("evidence_level")
            not in AUTHORITATIVE_CALENDAR_LEVELS
            or calendar.get("signature_key_id")
            not in self.trusted_calendar_keys
            or not _SHA256.fullmatch(
                str(calendar.get("signed_payload_sha256") or "")
            )
            or not isinstance(review, dict)
            or review.get("schema_version")
            != ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION
            or review.get("review_method") != INDEPENDENT_ROW_REVIEW_METHOD
            or any(
                not _SHA256.fullmatch(str(review.get(field) or ""))
                for field in (
                    "content_sha256",
                    "reviewed_changes_sha256",
                    "archive_manifest_sha256",
                    "archive_review_rows_sha256",
                    "archive_row_dispositions_sha256",
                )
            )
        ):
            raise PitEvidenceStateError(
                "production PIT import requires an authoritative calendar "
                "artifact and independent hash-bound row-level review"
            )
        if not documents or any(
            not isinstance(document, dict)
            or document.get("evidence_kind") != "effective_dated_history"
            or str(document.get("coverage_from") or "")
            >= str(document.get("coverage_to") or "")
            for document in documents
        ):
            raise PitEvidenceStateError(
                "current-anchor observations cannot be approved or imported"
            )

    def _validate_package(
        self,
        package: dict[str, Any],
    ) -> tuple[str, set[str]]:
        try:
            encoded_package = _canonical_json(package).encode()
        except (TypeError, ValueError) as exc:
            raise PitEvidenceIntegrityError(
                "governed package is not canonical JSON"
            ) from exc
        if len(encoded_package) > 20 * 1024 * 1024:
            raise PitEvidenceIntegrityError(
                "governed package exceeds 20 MiB"
            )
        if package.get("schema_version") != STAGING_SCHEMA_VERSION:
            raise PitEvidenceIntegrityError("unsupported staging package")
        approval = package.get("approval")
        if approval != {
            "automatic_import_permitted": False,
            "requires_admin_attestation": True,
            "license_status": "not_attested_by_platform",
        }:
            raise PitEvidenceIntegrityError("package approval boundary is invalid")
        manifest = package.get("evidence_manifest")
        manifest_digest = package.get("evidence_manifest_sha256")
        if (
            not isinstance(manifest, dict)
            or _canonical_sha256(manifest) != manifest_digest
            or manifest.get("parser_version") != PARSER_VERSION
        ):
            raise PitEvidenceIntegrityError("package manifest integrity mismatch")
        documents = package.get("imports")
        if not isinstance(documents, list) or not documents:
            raise PitEvidenceIntegrityError("package imports are missing")
        scopes: set[str] = set()
        for document in documents:
            if not isinstance(document, dict):
                raise PitEvidenceIntegrityError("package import is invalid")
            scope = str(document.get("scope_id") or "")
            source = document.get("source")
            if (
                not scope
                or scope in scopes
                or document.get("schema_version")
                != "point-in-time-master-import/v1"
                or not isinstance(source, dict)
                or source.get("provider") != "csindex_official"
                or source.get("version") != PARSER_VERSION
                or source.get("content_sha256") != manifest_digest
            ):
                raise PitEvidenceIntegrityError(
                    "package import identity is invalid"
                )
            scopes.add(scope)
        if scopes != {"csi300", "csi500", "csi800", "csi1000"}:
            raise PitEvidenceIntegrityError(
                "governed CSI package requires all four canonical scopes"
            )
        self._rebuild_package_from_evidence(package)
        return _canonical_sha256(package), _manifest_artifact_digests(package)

    def _validate_auxiliary_registrations(
        self,
        connection: sqlite3.Connection,
        package: Mapping[str, Any],
        *,
        stager_user_id: int,
        decision_user_id: int | None = None,
    ) -> None:
        manifest = package.get("evidence_manifest")
        if not isinstance(manifest, dict):
            raise PitEvidenceIntegrityError(
                "package evidence manifest is missing"
            )
        for manifest_key, kind in (
            ("trading_calendar", "trading_calendar"),
            ("review_evidence", "review_decisions"),
        ):
            evidence = manifest.get(manifest_key)
            digest = (
                str(evidence.get("content_sha256") or "")
                if isinstance(evidence, dict)
                else ""
            )
            row = connection.execute(
                "SELECT * FROM pit_evidence_auxiliary_artifacts "
                "WHERE content_sha256=? AND kind=?",
                (digest, kind),
            ).fetchone()
            if row is None:
                raise PitEvidenceIntegrityError(
                    f"{kind} managed provenance is unavailable"
                )
            provenance_json = str(row["provenance_json"])
            if hashlib.sha256(provenance_json.encode()).hexdigest() != str(
                row["provenance_sha256"]
            ):
                raise PitEvidenceIntegrityError(
                    f"{kind} provenance integrity mismatch"
                )
            try:
                provenance = json.loads(provenance_json)
            except json.JSONDecodeError as exc:
                raise PitEvidenceIntegrityError(
                    f"{kind} provenance is invalid"
                ) from exc
            if not isinstance(provenance, dict):
                raise PitEvidenceIntegrityError(
                    f"{kind} provenance is invalid"
                )
            if kind == "trading_calendar":
                if (
                    not isinstance(evidence, dict)
                    or provenance.get("provider")
                    != evidence.get("provider")
                    or provenance.get("evidence_level")
                    != evidence.get("evidence_level")
                    or provenance.get("signature_key_id")
                    != evidence.get("signature_key_id")
                    or provenance.get("signed_payload_sha256")
                    != evidence.get("signed_payload_sha256")
                ):
                    raise PitEvidenceIntegrityError(
                        "calendar provenance differs from package manifest"
                    )
            else:
                if (
                    isinstance(evidence, dict)
                    and evidence.get("review_method")
                    == UNATTESTED_REVIEW_METHOD
                ):
                    if provenance.get("review_method") != UNATTESTED_REVIEW_METHOD:
                        raise PitEvidenceIntegrityError(
                            "unattested review provenance differs from manifest"
                        )
                    continue
                review_payload = self.artifacts.read(digest)
                try:
                    reviewer = json.loads(review_payload)["reviewer"]
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                ) as exc:
                    raise PitEvidenceIntegrityError(
                        "review provenance cannot be replayed"
                    ) from exc
                reviewer_user_id = int(provenance.get("reviewer_user_id") or 0)
                if (
                    provenance.get("review_method")
                    != INDEPENDENT_ROW_REVIEW_METHOD
                    or
                    int(reviewer.get("user_id") or 0) != reviewer_user_id
                    or int(row["recorded_by"]) != reviewer_user_id
                    or reviewer_user_id == int(stager_user_id)
                    or (
                        decision_user_id is not None
                        and reviewer_user_id == int(decision_user_id)
                    )
                ):
                    raise PitEvidenceStateError(
                        "independent review requires a distinct authenticated "
                        "reviewer, stager, and approver"
                    )
        if decision_user_id is not None and int(decision_user_id) == int(
            stager_user_id
        ):
            raise PitEvidenceStateError(
                "package approver must be distinct from its stager"
            )

    def stage_package(
        self,
        *,
        package: dict[str, Any],
        artifacts: Sequence[ArtifactEvidence] = (),
        actor_user_id: int,
    ) -> dict[str, Any]:
        try:
            package_size = len(_canonical_json(package).encode())
        except (TypeError, ValueError) as exc:
            raise PitEvidenceIntegrityError(
                "governed package is not canonical JSON"
            ) from exc
        if package_size > 20 * 1024 * 1024:
            raise PitEvidenceIntegrityError(
                "governed package exceeds 20 MiB"
            )
        expected_digests = _manifest_artifact_digests(package)
        auxiliary_payloads = _derived_auxiliary_payloads(package)
        manifest = package.get("evidence_manifest")
        auxiliary_digests: set[str] = set()
        if isinstance(manifest, dict):
            for key in ("trading_calendar", "review_evidence"):
                value = manifest.get(key)
                if isinstance(value, dict) and _SHA256.fullmatch(
                    str(value.get("content_sha256") or "")
                ):
                    auxiliary_digests.add(str(value["content_sha256"]))
        if artifacts:
            payloads = _artifact_payloads(artifacts)
            if set(payloads) != expected_digests - auxiliary_digests:
                raise PitEvidenceIntegrityError(
                    "supplied artifacts do not exactly match package manifest"
                )
            for artifact in artifacts:
                self.record_artifact(
                    artifact=artifact,
                    actor_user_id=actor_user_id,
                )
        for digest, (kind, payload) in sorted(auxiliary_payloads.items()):
            self.record_auxiliary_artifact(
                kind=kind,
                payload=payload,
                expected_sha256=digest,
                actor_user_id=actor_user_id,
            )
        package_sha256, validated_digests = self._validate_package(package)
        if validated_digests != expected_digests:
            raise PitEvidenceIntegrityError(
                "package artifact identities changed during validation"
            )
        artifact_results: dict[str, dict[str, Any]] = {}
        for digest in sorted(expected_digests):
            payload = self.artifacts.read(digest)
            artifact_results[digest] = {
                "content_sha256": digest,
                "size_bytes": len(payload),
                "idempotent": True,
            }
        package_id = "pitpkg_" + package_sha256[:32]
        package_json = _canonical_json(package)
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_auxiliary_registrations(
                connection,
                package,
                stager_user_id=int(actor_user_id),
            )
            existing = connection.execute(
                """
                SELECT * FROM pit_evidence_packages
                WHERE package_sha256=?
                """,
                (package_sha256,),
            ).fetchone()
            if existing is not None:
                if str(existing["package_json"]) != package_json:
                    raise PitEvidenceIntegrityError(
                        "stored package digest collision"
                    )
                connection.rollback()
                return {
                    **self._package_summary(existing, connection=None),
                    "idempotent": True,
                }
            for digest, result in artifact_results.items():
                registered = connection.execute(
                    """
                    SELECT size_bytes FROM pit_evidence_artifacts
                    WHERE content_sha256=?
                    """,
                    (digest,),
                ).fetchone()
                if (
                    registered is None
                    or int(registered["size_bytes"]) != result["size_bytes"]
                ):
                    raise PitEvidenceIntegrityError(
                        "artifact metadata is unavailable"
                    )
            connection.execute(
                """
                INSERT INTO pit_evidence_packages (
                    package_id, package_sha256, package_json, status,
                    revision, created_at, created_by
                ) VALUES (?, ?, ?, 'pending', 1, ?, ?)
                """,
                (
                    package_id,
                    package_sha256,
                    package_json,
                    now,
                    int(actor_user_id),
                ),
            )
            for digest in sorted(expected_digests):
                connection.execute(
                    """
                    INSERT INTO pit_evidence_package_artifacts (
                        package_id, content_sha256
                    ) VALUES (?, ?)
                    """,
                    (package_id, digest),
                )
            self._append_event(
                connection,
                event_type="package_staged",
                package_id=package_id,
                actor_user_id=actor_user_id,
                event={
                    "package_sha256": package_sha256,
                    "artifact_count": len(expected_digests),
                    "status": "pending",
                },
            )
            connection.commit()
        return {
            "schema_version": GOVERNANCE_SCHEMA_VERSION,
            "package_id": package_id,
            "package_sha256": package_sha256,
            "status": "pending",
            "revision": 1,
            "artifact_count": len(expected_digests),
            "idempotent": False,
        }

    def record_auxiliary_artifact(
        self,
        *,
        kind: Literal["trading_calendar", "review_decisions"],
        payload: bytes,
        expected_sha256: str,
        actor_user_id: int,
    ) -> dict[str, Any]:
        """Retain a non-CSI raw review/calendar artifact by content hash."""

        if not payload or len(payload) > _MAX_ARTIFACT_BYTES:
            raise PitEvidenceIntegrityError(
                "auxiliary artifact payload size is invalid"
            )
        if kind == "trading_calendar":
            try:
                untrusted_document = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PitEvidenceIntegrityError(
                    "trading calendar artifact is invalid JSON"
                ) from exc
            if isinstance(untrusted_document, list):
                provenance = {
                    "schema_version": "pit-auxiliary-provenance/v1",
                    "kind": kind,
                    "provider": "explicit_unattested_input",
                    "evidence_level": "unattested",
                    "signature_key_id": "unattested",
                    "signed_payload_sha256": hashlib.sha256(payload).hexdigest(),
                }
            else:
                document, _sessions, signed_payload_sha256 = (
                    self.verify_trading_calendar_payload(payload)
                )
                source = document["source"]
                provenance = {
                    "schema_version": "pit-auxiliary-provenance/v1",
                    "kind": kind,
                    "provider": source["provider"],
                    "evidence_level": source["evidence_level"],
                    "signature_key_id": source["signature_key_id"],
                    "signed_payload_sha256": signed_payload_sha256,
                }
        else:
            try:
                document = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PitEvidenceIntegrityError(
                    "review decision artifact is invalid JSON"
                ) from exc
            if isinstance(document, list):
                provenance = {
                    "schema_version": "pit-auxiliary-provenance/v1",
                    "kind": kind,
                    "review_method": UNATTESTED_REVIEW_METHOD,
                }
            else:
                reviewer = (
                    document.get("reviewer")
                    if isinstance(document, dict)
                    else None
                )
                if (
                    not isinstance(reviewer, dict)
                    or set(reviewer)
                    != {"user_id", "identity", "reviewed_at"}
                    or not isinstance(reviewer.get("user_id"), int)
                    or isinstance(reviewer.get("user_id"), bool)
                    or int(reviewer["user_id"]) <= 0
                ):
                    raise PitEvidenceIntegrityError(
                        "review artifact is not bound to a platform reviewer"
                    )
                provenance = {
                    "schema_version": "pit-auxiliary-provenance/v1",
                    "kind": kind,
                    "review_method": INDEPENDENT_ROW_REVIEW_METHOD,
                    "reviewer_user_id": int(reviewer["user_id"]),
                    "reviewer_identity": str(reviewer.get("identity") or ""),
                    "reviewed_at": str(reviewer.get("reviewed_at") or ""),
                }
        provenance_json = _canonical_json(provenance)
        provenance_sha256 = hashlib.sha256(provenance_json.encode()).hexdigest()
        with self._connect() as connection:
            existing_before_write = connection.execute(
                "SELECT recorded_by FROM pit_evidence_auxiliary_artifacts "
                "WHERE content_sha256=?",
                (expected_sha256,),
            ).fetchone()
        if (
            existing_before_write is None
            and kind == "review_decisions"
            and provenance.get("review_method")
            == INDEPENDENT_ROW_REVIEW_METHOD
            and int(provenance["reviewer_user_id"]) != int(actor_user_id)
        ):
            raise PitEvidenceStateError(
                "review artifact must be recorded by its authenticated reviewer"
            )
        result = self.artifacts.put(
            payload,
            expected_sha256=expected_sha256,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pit_evidence_auxiliary_artifacts "
                "WHERE content_sha256=?",
                (expected_sha256,),
            ).fetchone()
            if (
                existing is None
                and kind == "review_decisions"
                and provenance.get("review_method")
                == INDEPENDENT_ROW_REVIEW_METHOD
                and int(provenance["reviewer_user_id"])
                != int(actor_user_id)
            ):
                raise PitEvidenceStateError(
                    "review artifact must be recorded by its authenticated reviewer"
                )
            if existing is not None and (
                str(existing["kind"]) != kind
                or str(existing["provenance_json"]) != provenance_json
                or str(existing["provenance_sha256"])
                != provenance_sha256
            ):
                raise PitEvidenceIntegrityError(
                    "auxiliary artifact provenance conflicts with its digest"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO pit_evidence_artifacts (
                    content_sha256, size_bytes, first_recorded_at,
                    first_recorded_by
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    expected_sha256,
                    result["size_bytes"],
                    utc_now_iso(),
                    int(actor_user_id),
                ),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO pit_evidence_auxiliary_artifacts (
                        content_sha256, kind, provenance_json,
                        provenance_sha256, recorded_at, recorded_by
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        expected_sha256,
                        kind,
                        provenance_json,
                        provenance_sha256,
                        utc_now_iso(),
                        int(actor_user_id),
                    ),
                )
            self._append_event(
                connection,
                event_type="auxiliary_artifact_recorded",
                package_id=None,
                actor_user_id=actor_user_id,
                event={
                    "kind": kind,
                    "content_sha256": expected_sha256,
                    "size_bytes": result["size_bytes"],
                    "provenance_sha256": provenance_sha256,
                    "idempotent": existing is not None,
                },
            )
            connection.commit()
        return {
            "schema_version": GOVERNANCE_SCHEMA_VERSION,
            "kind": kind,
            "provenance_sha256": provenance_sha256,
            **result,
        }

    def record_artifact(
        self,
        *,
        artifact: ArtifactEvidence,
        actor_user_id: int,
    ) -> dict[str, Any]:
        payloads = {
            artifact.content_sha256: artifact.payload,
        }
        if artifact.request_payload is not None:
            assert artifact.request_sha256 is not None
            payloads[artifact.request_sha256] = artifact.request_payload
        results = {
            digest: self.artifacts.put(payload, expected_sha256=digest)
            for digest, payload in sorted(payloads.items())
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for digest, result in results.items():
                connection.execute(
                    """
                    INSERT OR IGNORE INTO pit_evidence_artifacts (
                        content_sha256, size_bytes, first_recorded_at,
                        first_recorded_by
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        digest,
                        result["size_bytes"],
                        utc_now_iso(),
                        int(actor_user_id),
                    ),
                )
                self._append_event(
                    connection,
                    event_type="artifact_recorded",
                    package_id=None,
                    actor_user_id=actor_user_id,
                    event={
                        "content_sha256": digest,
                        "size_bytes": result["size_bytes"],
                        "artifact": artifact.manifest(),
                        "payload_kind": (
                            "request"
                            if digest == artifact.request_sha256
                            else "response"
                        ),
                    },
                )
            connection.commit()
        return {
            "schema_version": GOVERNANCE_SCHEMA_VERSION,
            "digests": sorted(results),
            "size_bytes": sum(
                int(item["size_bytes"]) for item in results.values()
            ),
            "idempotent": all(
                bool(item["idempotent"]) for item in results.values()
            ),
        }

    @staticmethod
    def _package_summary(
        row: sqlite3.Row,
        *,
        connection: sqlite3.Connection | None,
    ) -> dict[str, Any]:
        decision_attestations: dict[str, Any] | None = None
        if row["decision_attestations_json"] is not None:
            try:
                decoded = json.loads(str(row["decision_attestations_json"]))
            except json.JSONDecodeError as exc:
                raise PitEvidenceIntegrityError(
                    "stored approval attestations are invalid"
                ) from exc
            if not isinstance(decoded, dict):
                raise PitEvidenceIntegrityError(
                    "stored approval attestations are invalid"
                )
            decision_attestations = decoded
        if row["status"] in {"approved", "imported"}:
            try:
                _validate_approval_attestations(decision_attestations)
            except PitEvidenceStateError as exc:
                raise PitEvidenceIntegrityError(
                    "stored approval attestations are invalid"
                ) from exc
        result = {
            "schema_version": GOVERNANCE_SCHEMA_VERSION,
            "package_id": str(row["package_id"]),
            "package_sha256": str(row["package_sha256"]),
            "status": str(row["status"]),
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "decided_at": row["decided_at"],
            "decision_reason": row["decision_reason"],
            "decision_attestations": decision_attestations,
            "imported_at": row["imported_at"],
        }
        if connection is not None:
            receipts = connection.execute(
                """
                SELECT scope_id, batch_id, batch_digest, imported_at
                FROM pit_evidence_package_imports
                WHERE package_id=? ORDER BY scope_id
                """,
                (row["package_id"],),
            ).fetchall()
            result["imports"] = [dict(item) for item in receipts]
        return result

    def get_package(self, package_id: str) -> dict[str, Any]:
        if not _PACKAGE_ID.fullmatch(package_id):
            raise PitEvidenceStateError("package identity is invalid")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pit_evidence_packages WHERE package_id=?",
                (package_id,),
            ).fetchone()
            if row is None:
                raise PitEvidenceStateError("package is unavailable")
            return self._package_summary(row, connection=connection)

    def get_events(self, package_id: str) -> dict[str, Any]:
        if not _PACKAGE_ID.fullmatch(package_id):
            raise PitEvidenceStateError("package identity is invalid")
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM pit_evidence_packages WHERE package_id=?",
                (package_id,),
            ).fetchone()
            if exists is None:
                raise PitEvidenceStateError("package is unavailable")
            rows = connection.execute(
                """
                SELECT id, event_type, actor_user_id, event_json,
                       event_sha256, created_at
                FROM pit_evidence_events
                WHERE package_id=? ORDER BY id
                """,
                (package_id,),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event_json = str(row["event_json"])
            if hashlib.sha256(event_json.encode()).hexdigest() != row[
                "event_sha256"
            ]:
                raise PitEvidenceIntegrityError(
                    "governance event integrity mismatch"
                )
            try:
                event = json.loads(event_json)
            except json.JSONDecodeError as exc:
                raise PitEvidenceIntegrityError(
                    "governance event payload is invalid"
                ) from exc
            events.append(
                {
                    "id": int(row["id"]),
                    "event_type": str(row["event_type"]),
                    "actor_user_id": int(row["actor_user_id"]),
                    "event": event,
                    "event_sha256": str(row["event_sha256"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return {
            "schema_version": GOVERNANCE_SCHEMA_VERSION,
            "package_id": package_id,
            "events": events,
        }

    def decide(
        self,
        *,
        package_id: str,
        expected_revision: int,
        decision: Literal["approved", "rejected"],
        actor_user_id: int,
        reason: str,
        attestations: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not _PACKAGE_ID.fullmatch(package_id):
            raise PitEvidenceStateError("package identity is invalid")
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise PitEvidenceStateError("decision reason is invalid")
        normalized_attestations: dict[str, Any] | None = None
        if decision == "approved":
            normalized_attestations = _validate_approval_attestations(
                attestations
            )
        elif attestations is not None:
            raise PitEvidenceStateError(
                "rejection must not carry approval attestations"
            )
        attestations_json = (
            _canonical_json(normalized_attestations)
            if normalized_attestations is not None
            else None
        )
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if decision == "approved":
                candidate = connection.execute(
                    "SELECT package_json, created_by FROM pit_evidence_packages "
                    "WHERE package_id=? AND status='pending' AND revision=?",
                    (package_id, int(expected_revision)),
                ).fetchone()
                if candidate is None:
                    raise PitEvidenceConflictError(
                        "package decision lost compare-and-swap"
                    )
                try:
                    candidate_package = json.loads(
                        str(candidate["package_json"])
                    )
                except json.JSONDecodeError as exc:
                    raise PitEvidenceIntegrityError(
                        "stored package is invalid"
                    ) from exc
                self._validate_package(candidate_package)
                self._require_production_import_contract(candidate_package)
                self._validate_auxiliary_registrations(
                    connection,
                    candidate_package,
                    stager_user_id=int(candidate["created_by"]),
                    decision_user_id=int(actor_user_id),
                )
            cursor = connection.execute(
                """
                UPDATE pit_evidence_packages
                SET status=?, revision=revision+1, decided_at=?,
                    decided_by=?, decision_reason=?,
                    decision_attestations_json=?
                WHERE package_id=? AND status='pending' AND revision=?
                """,
                (
                    decision,
                    now,
                    int(actor_user_id),
                    normalized_reason,
                    attestations_json,
                    package_id,
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise PitEvidenceConflictError(
                    "package decision lost compare-and-swap"
                )
            self._append_event(
                connection,
                event_type=f"package_{decision}",
                package_id=package_id,
                actor_user_id=actor_user_id,
                event={
                    "expected_revision": int(expected_revision),
                    "new_revision": int(expected_revision) + 1,
                    "reason": normalized_reason,
                    "attestations": normalized_attestations,
                },
            )
            row = connection.execute(
                "SELECT * FROM pit_evidence_packages WHERE package_id=?",
                (package_id,),
            ).fetchone()
            connection.commit()
            assert row is not None
            return self._package_summary(row, connection=None)

    def _verified_approved_package(
        self,
        connection: sqlite3.Connection,
        package_id: str,
    ) -> tuple[sqlite3.Row, dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM pit_evidence_packages WHERE package_id=?",
            (package_id,),
        ).fetchone()
        if row is None:
            raise PitEvidenceStateError("package is unavailable")
        if row["status"] not in {"approved", "imported"}:
            raise PitEvidenceStateError("only an approved package may be imported")
        try:
            stored_attestations = json.loads(
                str(row["decision_attestations_json"])
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise PitEvidenceIntegrityError(
                "stored approval attestations are invalid"
            ) from exc
        try:
            _validate_approval_attestations(stored_attestations)
        except PitEvidenceStateError as exc:
            raise PitEvidenceIntegrityError(
                "stored approval attestations are invalid"
            ) from exc
        try:
            package = json.loads(str(row["package_json"]))
        except json.JSONDecodeError as exc:
            raise PitEvidenceIntegrityError("stored package is invalid") from exc
        package_sha256, expected_digests = self._validate_package(package)
        self._require_production_import_contract(package)
        self._validate_auxiliary_registrations(
            connection,
            package,
            stager_user_id=int(row["created_by"]),
            decision_user_id=int(row["decided_by"]),
        )
        if package_sha256 != row["package_sha256"]:
            raise PitEvidenceIntegrityError("stored package integrity mismatch")
        linked = {
            str(item["content_sha256"])
            for item in connection.execute(
                """
                SELECT content_sha256
                FROM pit_evidence_package_artifacts
                WHERE package_id=?
                """,
                (package_id,),
            ).fetchall()
        }
        if linked != expected_digests:
            raise PitEvidenceIntegrityError("package evidence links changed")
        for digest in sorted(linked):
            self.artifacts.read(digest)
        return row, package

    def import_approved_package(
        self,
        *,
        package_id: str,
        actor_user_id: int,
    ) -> dict[str, Any]:
        if not _PACKAGE_ID.fullmatch(package_id):
            raise PitEvidenceStateError("package identity is invalid")
        durable_receipts: list[dict[str, Any]] = []
        package_sha256 = ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, package = self._verified_approved_package(
                connection,
                package_id,
            )
            if row["status"] == "imported":
                connection.rollback()
                result = self.get_package(package_id)
                result["idempotent"] = True
                return result
            package_sha256 = str(row["package_sha256"])
            for document in package["imports"]:
                authorization = _authorize_governed_import(
                    package_id=package_id,
                    package_sha256=package_sha256,
                    document_sha256=_canonical_sha256(document),
                )
                result = self.master_store.import_batch(
                    **document,
                    imported_by_user_id=int(actor_user_id),
                    _governed_authorization=authorization,
                )
                receipt = {
                    "scope_id": document["scope_id"],
                    "batch_id": result["batch_id"],
                    "batch_digest": result["batch_digest"],
                    "imported_at": utc_now_iso(),
                }
                existing_receipt = connection.execute(
                    """
                    SELECT scope_id, batch_id, batch_digest, imported_at
                    FROM pit_evidence_package_imports
                    WHERE package_id=? AND scope_id=?
                    """,
                    (package_id, receipt["scope_id"]),
                ).fetchone()
                if existing_receipt is not None:
                    if (
                        str(existing_receipt["batch_id"])
                        != receipt["batch_id"]
                        or str(existing_receipt["batch_digest"])
                        != receipt["batch_digest"]
                    ):
                        raise PitEvidenceIntegrityError(
                            "stored scope receipt differs from verified import"
                        )
                    receipt = dict(existing_receipt)
                else:
                    connection.execute(
                        """
                        INSERT INTO pit_evidence_package_imports (
                            package_id, scope_id, batch_id, batch_digest,
                            imported_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            package_id,
                            receipt["scope_id"],
                            receipt["batch_id"],
                            receipt["batch_digest"],
                            receipt["imported_at"],
                        ),
                    )
                durable_receipts.append(receipt)
            if len(durable_receipts) != len(package["imports"]):
                raise PitEvidenceIntegrityError(
                    "governed import receipts are incomplete"
                )
            # Commit all per-scope receipts before making any quarantined batch
            # visible in the separate master database.
            connection.commit()

        activation = self.master_store.activate_governed_csi_package(
            package_id=package_id,
            package_sha256=package_sha256,
            receipts=durable_receipts,
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, _package = self._verified_approved_package(
                connection,
                package_id,
            )
            if row["status"] == "imported":
                connection.rollback()
                result = self.get_package(package_id)
                result["idempotent"] = True
                return result
            now = utc_now_iso()
            cursor = connection.execute(
                """
                UPDATE pit_evidence_packages
                SET status='imported', revision=revision+1,
                    imported_at=?, imported_by=?
                WHERE package_id=? AND status='approved'
                """,
                (now, int(actor_user_id), package_id),
            )
            if cursor.rowcount != 1:
                raise PitEvidenceConflictError("package import state changed")
            self._append_event(
                connection,
                event_type="package_imported",
                package_id=package_id,
                actor_user_id=actor_user_id,
                event={
                    "receipts": durable_receipts,
                    "activation": activation,
                },
            )
            connection.commit()
        result = self.get_package(package_id)
        result["idempotent"] = False
        return result

    def verify_all(self, digests: Iterable[str]) -> None:
        for digest in digests:
            self.artifacts.read(digest)
