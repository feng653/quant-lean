"""Append-only provider licence/archive evidence metadata.

This registry captures *claims* about an externally retained document without
storing the document, credentials, a local path, or URL query fragments.  A
review event is useful governance evidence, but intentionally has no connection
to :mod:`backend.data.production_pit_release`: production still requires the
signed approved artifact and independent official reconciliation gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


LICENCE_EVIDENCE_REGISTRY_SCHEMA = "provider-licence-evidence-registry/v1"
LICENCE_EVIDENCE_RECORD_SCHEMA = "provider-licence-evidence-record/v1"
LICENCE_EVIDENCE_REVIEW_SCHEMA = "provider-licence-evidence-review/v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DECISIONS = {"approved", "rejected"}
_MAX_DOCUMENT_SIZE_BYTES = 1024 * 1024 * 1024


class LicenceEvidenceError(RuntimeError):
    """Base error for a malformed or unreadable evidence registry."""


class LicenceEvidenceConflict(LicenceEvidenceError):
    """An immutable evidence identity or review already exists."""


class LicenceEvidenceValidationError(LicenceEvidenceError):
    """Input cannot safely be represented by the evidence contract."""


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
        raise LicenceEvidenceValidationError(
            "licence evidence metadata is not canonicalisable"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _safe_id(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise LicenceEvidenceValidationError(f"{field_name} is invalid")
    return normalized


def _iso_date(value: Any, field_name: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise LicenceEvidenceValidationError(
            f"{field_name} must be YYYY-MM-DD"
        ) from exc


def _timestamp(value: Any, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LicenceEvidenceValidationError(
            f"{field_name} must be RFC3339"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LicenceEvidenceValidationError(
            f"{field_name} must include a timezone"
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _positive_int(value: Any, field_name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise LicenceEvidenceValidationError(f"{field_name} is invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise LicenceEvidenceValidationError(f"{field_name} is invalid") from exc
    if normalized < 1 or (maximum is not None and normalized > maximum):
        raise LicenceEvidenceValidationError(f"{field_name} is invalid")
    return normalized


def _reference_descriptor(reference: Any) -> dict[str, str] | None:
    """Return a useful but non-reversible descriptor for a path or URL.

    The raw value never reaches SQLite or response objects. URLs retain only
    their public origin. Their fingerprint covers the origin and path, while
    credentials and query/fragment values do not even influence that digest.
    Local references are represented by a SHA-256 fingerprint only.
    """

    if reference is None:
        return None
    raw = str(reference).strip()
    if not raw:
        return None
    if len(raw.encode("utf-8")) > 4096:
        raise LicenceEvidenceValidationError("document_reference is too long")
    try:
        parsed = urlsplit(raw)
        parsed_hostname = parsed.hostname
        if parsed.scheme.lower() in {"http", "https"} and parsed_hostname:
            host = parsed_hostname.encode("idna").decode("ascii").lower()
            port = parsed.port
        else:
            host = ""
            port = None
    except (UnicodeError, ValueError) as exc:
        raise LicenceEvidenceValidationError(
            "document_reference URL is invalid"
        ) from exc
    if host:
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        display_host = f"[{host}]" if ":" in host else host
        origin = f"{parsed.scheme.lower()}://{display_host}"
        if port is not None and port != default_port:
            origin = f"{origin}:{port}"
        fingerprint_source = f"{origin}{parsed.path}".encode("utf-8")
        return {
            "kind": "remote_url",
            "origin": origin,
            "reference_sha256": hashlib.sha256(fingerprint_source).hexdigest(),
        }
    return {
        "kind": "local_or_opaque",
        "reference_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


_REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS licence_evidence_registry_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS licence_evidence_records (
    record_sha256 TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    document_sha256 TEXT NOT NULL,
    registered_by_user_id INTEGER NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS licence_evidence_reviews (
    review_sha256 TEXT PRIMARY KEY,
    record_sha256 TEXT NOT NULL UNIQUE,
    review_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reviewed_by_user_id INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY (record_sha256) REFERENCES licence_evidence_records(record_sha256)
);
CREATE TRIGGER IF NOT EXISTS licence_evidence_records_no_update
BEFORE UPDATE ON licence_evidence_records
BEGIN SELECT RAISE(ABORT, 'licence evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS licence_evidence_records_no_delete
BEFORE DELETE ON licence_evidence_records
BEGIN SELECT RAISE(ABORT, 'licence evidence cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS licence_evidence_reviews_no_update
BEFORE UPDATE ON licence_evidence_reviews
BEGIN SELECT RAISE(ABORT, 'licence evidence review is immutable'); END;
CREATE TRIGGER IF NOT EXISTS licence_evidence_reviews_no_delete
BEFORE DELETE ON licence_evidence_reviews
BEGIN SELECT RAISE(ABORT, 'licence evidence review cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS licence_evidence_metadata_no_update
BEFORE UPDATE ON licence_evidence_registry_metadata
BEGIN SELECT RAISE(ABORT, 'licence evidence registry metadata is immutable'); END;
CREATE TRIGGER IF NOT EXISTS licence_evidence_metadata_no_delete
BEFORE DELETE ON licence_evidence_registry_metadata
BEGIN SELECT RAISE(ABORT, 'licence evidence registry metadata cannot be deleted'); END;
"""


class ProviderLicenceEvidenceRegistry:
    """Dedicated, append-only metadata registry with read-time integrity checks."""

    _allowed_tables = {
        "licence_evidence_registry_metadata",
        "licence_evidence_records",
        "licence_evidence_reviews",
    }
    _required_triggers = {
        "licence_evidence_records_no_update",
        "licence_evidence_records_no_delete",
        "licence_evidence_reviews_no_update",
        "licence_evidence_reviews_no_delete",
        "licence_evidence_metadata_no_update",
        "licence_evidence_metadata_no_delete",
    }

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        if self.path.exists() and (
            self.path.is_symlink()
            or not stat.S_ISREG(self.path.lstat().st_mode)
        ):
            raise LicenceEvidenceError("licence evidence registry path is unsafe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            connection.close()
            raise LicenceEvidenceError(
                "licence evidence registry permissions cannot be restricted"
            ) from exc
        return connection

    @classmethod
    def _assert_dedicated(cls, connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables - cls._allowed_tables:
            raise LicenceEvidenceError(
                "registry path contains non-licence-evidence tables"
            )

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        ProviderLicenceEvidenceRegistry._assert_dedicated(connection)
        connection.executescript(_REGISTRY_SQL)
        connection.execute(
            "INSERT OR IGNORE INTO licence_evidence_registry_metadata "
            "VALUES (1, ?)",
            (LICENCE_EVIDENCE_REGISTRY_SCHEMA,),
        )
        schema = connection.execute(
            "SELECT schema_version FROM licence_evidence_registry_metadata "
            "WHERE singleton=1"
        ).fetchone()
        if schema is None or schema[0] != LICENCE_EVIDENCE_REGISTRY_SCHEMA:
            raise LicenceEvidenceError("licence evidence registry schema changed")
        connection.commit()

    @classmethod
    def _verify(cls, connection: sqlite3.Connection) -> None:
        cls._assert_dedicated(connection)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != cls._allowed_tables:
            raise LicenceEvidenceError("licence evidence registry is incomplete")
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        if not cls._required_triggers.issubset(triggers):
            raise LicenceEvidenceError(
                "licence evidence immutability guard is incomplete"
            )
        schema = connection.execute(
            "SELECT schema_version FROM licence_evidence_registry_metadata "
            "WHERE singleton=1"
        ).fetchone()
        if schema is None or schema[0] != LICENCE_EVIDENCE_REGISTRY_SCHEMA:
            raise LicenceEvidenceError("licence evidence registry schema changed")

    def register(
        self,
        *,
        provider_id: str,
        source_scope: str,
        licence_scope: str,
        document_sha256: str,
        document_size_bytes: int | None,
        document_reference: str | None,
        claimed_effective_from: str,
        claimed_effective_to: str,
        claimed_available_from: str,
        claimed_available_to: str,
        obtained_at: str,
        actor_user_id: int,
    ) -> dict[str, Any]:
        provider = _safe_id(provider_id, "provider_id")
        source = _safe_id(source_scope, "source_scope")
        scope = _safe_id(licence_scope, "licence_scope")
        if not _SHA256.fullmatch(str(document_sha256)):
            raise LicenceEvidenceValidationError("document_sha256 is invalid")
        normalized_size = (
            _positive_int(
                document_size_bytes,
                "document_size_bytes",
                maximum=_MAX_DOCUMENT_SIZE_BYTES,
            )
            if document_size_bytes is not None
            else None
        )
        normalized_actor = _positive_int(actor_user_id, "actor_user_id")
        effective_from = _iso_date(
            claimed_effective_from, "claimed_effective_from"
        )
        effective_to = _iso_date(claimed_effective_to, "claimed_effective_to")
        available_from = _iso_date(
            claimed_available_from, "claimed_available_from"
        )
        available_to = _iso_date(claimed_available_to, "claimed_available_to")
        if effective_from > effective_to or available_from > available_to:
            raise LicenceEvidenceValidationError("claimed period is invalid")
        obtained = _timestamp(obtained_at, "obtained_at")
        now = datetime.now(UTC)
        if datetime.fromisoformat(obtained.replace("Z", "+00:00")) > now + timedelta(minutes=5):
            raise LicenceEvidenceValidationError("obtained_at is in the future")
        registered_at = now.isoformat().replace("+00:00", "Z")
        record: dict[str, Any] = {
            "schema_version": LICENCE_EVIDENCE_RECORD_SCHEMA,
            "provider_id": provider,
            "source_scope": source,
            "licence_scope": scope,
            "document_sha256": document_sha256,
            "document_size_bytes": normalized_size,
            "document_reference": _reference_descriptor(document_reference),
            "claimed_effective_from": effective_from,
            "claimed_effective_to": effective_to,
            "claimed_available_from": available_from,
            "claimed_available_to": available_to,
            "obtained_at": obtained,
            "registered_by_user_id": normalized_actor,
            "registered_at": registered_at,
            "initial_state": "unverified",
            "production_release_authorized": False,
        }
        record_sha256 = _digest(record)
        record_json = _canonical_bytes(record).decode("utf-8")
        try:
            with self._connect() as connection:
                self._initialize(connection)
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT record_json FROM licence_evidence_records "
                    "WHERE record_sha256=?",
                    (record_sha256,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != record_json:
                        raise LicenceEvidenceConflict(
                            "existing licence evidence identity differs"
                        )
                    connection.rollback()
                    return self.get(record_sha256)
                connection.execute(
                    """
                    INSERT INTO licence_evidence_records (
                        record_sha256, record_json, provider_id,
                        document_sha256, registered_by_user_id, registered_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_sha256,
                        record_json,
                        provider,
                        document_sha256,
                        normalized_actor,
                        registered_at,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise LicenceEvidenceConflict(
                "licence evidence immutable identity conflicts"
            ) from exc
        return self.get(record_sha256)

    def review(
        self,
        *,
        record_sha256: str,
        document_sha256: str,
        decision: str,
        reason_code: str,
        reviewer_user_id: int,
    ) -> dict[str, Any]:
        if not _SHA256.fullmatch(str(record_sha256)):
            raise LicenceEvidenceValidationError("record_sha256 is invalid")
        if not _SHA256.fullmatch(str(document_sha256)):
            raise LicenceEvidenceValidationError("document_sha256 is invalid")
        normalized_decision = str(decision).strip().lower()
        if normalized_decision not in _DECISIONS:
            raise LicenceEvidenceValidationError("decision is invalid")
        reason = _safe_id(reason_code, "reason_code")
        normalized_reviewer = _positive_int(
            reviewer_user_id, "reviewer_user_id"
        )
        reviewed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            with self._connect() as connection:
                self._verify(connection)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT record_json "
                    "FROM licence_evidence_records WHERE record_sha256=?",
                    (record_sha256,),
                ).fetchone()
                if row is None:
                    raise LicenceEvidenceValidationError(
                        "licence evidence record is unavailable"
                    )
                try:
                    registered_record = json.loads(str(row["record_json"]))
                except json.JSONDecodeError as exc:
                    raise LicenceEvidenceError(
                        "licence evidence record is unreadable"
                    ) from exc
                if (
                    not isinstance(registered_record, dict)
                    or _digest(registered_record) != record_sha256
                ):
                    raise LicenceEvidenceError(
                        "licence evidence record integrity mismatch"
                    )
                if registered_record.get("document_sha256") != document_sha256:
                    raise LicenceEvidenceValidationError(
                        "document digest does not match registered evidence"
                    )
                if int(registered_record.get("registered_by_user_id", 0)) == int(
                    normalized_reviewer
                ):
                    raise LicenceEvidenceValidationError(
                        "licence evidence requires an independent reviewer"
                    )
                review = {
                    "schema_version": LICENCE_EVIDENCE_REVIEW_SCHEMA,
                    "record_sha256": record_sha256,
                    "document_sha256": document_sha256,
                    "decision": normalized_decision,
                    "reason_code": reason,
                    "reviewed_by_user_id": normalized_reviewer,
                    "reviewed_at": reviewed_at,
                    "production_release_authorized": False,
                }
                review_sha256 = _digest(review)
                connection.execute(
                    """
                    INSERT INTO licence_evidence_reviews (
                        review_sha256, record_sha256, review_json, decision,
                        reviewed_by_user_id, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_sha256,
                        record_sha256,
                        _canonical_bytes(review).decode("utf-8"),
                        normalized_decision,
                        normalized_reviewer,
                        reviewed_at,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise LicenceEvidenceConflict(
                "licence evidence already has an immutable review"
            ) from exc
        return self.get(record_sha256)

    def get(self, record_sha256: str) -> dict[str, Any]:
        if not _SHA256.fullmatch(str(record_sha256)) or not self.path.is_file():
            raise LicenceEvidenceValidationError(
                "licence evidence record is unavailable"
            )
        with self._connect() as connection:
            self._verify(connection)
            row = connection.execute(
                "SELECT record_json, provider_id, document_sha256, "
                "registered_by_user_id, registered_at "
                "FROM licence_evidence_records "
                "WHERE record_sha256=?",
                (record_sha256,),
            ).fetchone()
            if row is None:
                raise LicenceEvidenceValidationError(
                    "licence evidence record is unavailable"
                )
            review_row = connection.execute(
                "SELECT review_sha256, review_json, decision, "
                "reviewed_by_user_id, reviewed_at "
                "FROM licence_evidence_reviews "
                "WHERE record_sha256=?",
                (record_sha256,),
            ).fetchone()
        try:
            record = json.loads(str(row["record_json"]))
        except json.JSONDecodeError as exc:
            raise LicenceEvidenceError("licence evidence record is unreadable") from exc
        if not isinstance(record, dict) or _digest(record) != record_sha256:
            raise LicenceEvidenceError("licence evidence record integrity mismatch")
        if (
            row["provider_id"] != record.get("provider_id")
            or row["document_sha256"] != record.get("document_sha256")
            or int(row["registered_by_user_id"])
            != int(record.get("registered_by_user_id", 0))
            or row["registered_at"] != record.get("registered_at")
        ):
            raise LicenceEvidenceError(
                "licence evidence registry index integrity mismatch"
            )
        review: dict[str, Any] | None = None
        if review_row is not None:
            try:
                review = json.loads(str(review_row["review_json"]))
            except json.JSONDecodeError as exc:
                raise LicenceEvidenceError(
                    "licence evidence review is unreadable"
                ) from exc
            if (
                not isinstance(review, dict)
                or _digest(review) != review_row["review_sha256"]
                or review.get("record_sha256") != record_sha256
                or review.get("document_sha256") != record.get("document_sha256")
                or review_row["decision"] != review.get("decision")
                or int(review_row["reviewed_by_user_id"])
                != int(review.get("reviewed_by_user_id", 0))
                or review_row["reviewed_at"] != review.get("reviewed_at")
            ):
                raise LicenceEvidenceError("licence evidence review integrity mismatch")
        return {
            "schema_version": LICENCE_EVIDENCE_REGISTRY_SCHEMA,
            "record_sha256": record_sha256,
            "state": review.get("decision") if review else "unverified",
            "record": record,
            "review": review,
            "production_release_authorized": False,
        }

    def list(self, *, provider_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        provider = _safe_id(provider_id, "provider_id") if provider_id else None
        with self._connect() as connection:
            self._verify(connection)
            if provider is None:
                rows = connection.execute(
                    "SELECT record_sha256 FROM licence_evidence_records "
                    "ORDER BY registered_at, record_sha256"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT record_sha256 FROM licence_evidence_records "
                    "WHERE provider_id=? ORDER BY registered_at, record_sha256",
                    (provider,),
                ).fetchall()
        return [self.get(str(row["record_sha256"])) for row in rows]
