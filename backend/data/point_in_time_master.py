"""Immutable point-in-time security, universe and industry master data.

The store is deliberately append-only.  A current classification snapshot is
valid evidence about its observation date only; it can never be promoted into
historical coverage.  Research callers receive explicit unavailable reasons
instead of silently falling back to today's constituents or industries.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from backend.config import settings

MASTER_SCHEMA_VERSION = "point-in-time-master/v1"
READINESS_SCHEMA_VERSION = "point-in-time-readiness/v1"
IMPORT_SCHEMA_VERSION = "point-in-time-master-import/v1"
BITEMPORAL_IMPORT_SCHEMA_VERSION = "point-in-time-master-import/v2"

Domain = Literal["security", "index_membership", "industry"]
EvidenceKind = Literal["current_snapshot", "effective_dated_history"]

_DOMAINS = {"security", "index_membership", "industry"}
_EVIDENCE_KINDS = {"current_snapshot", "effective_dated_history"}
_RESEARCH_EVIDENCE_LEVELS = {
    "exchange_authoritative",
    "index_provider_authoritative",
    "licensed",
    "public_cross_validated",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SECURITY_CODE = re.compile(r"^[0-9]{6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PIT_PACKAGE_ID = re.compile(r"^pitpkg_[0-9a-f]{32}$")
_GOVERNED_CSI_SCOPES = {"csi300", "csi500", "csi800", "csi1000"}


class PointInTimeMasterError(RuntimeError):
    """Base error for fail-closed master-data operations."""


class PointInTimeValidationError(PointInTimeMasterError):
    """The import or query does not satisfy the public contract."""


class PointInTimeConflictError(PointInTimeMasterError):
    """An immutable interval conflicts with already accepted evidence."""


class PointInTimeIntegrityError(PointInTimeMasterError):
    """Stored evidence no longer matches its immutable digest."""


_GOVERNED_IMPORT_TOKEN = object()
_PRODUCTION_RELEASE_IMPORT_TOKEN = object()


@dataclass(frozen=True)
class _GovernedImportAuthorization:
    package_id: str
    package_sha256: str
    document_sha256: str
    token: object


@dataclass(frozen=True)
class _ProductionReleaseImportAuthorization:
    plan_sha256: str
    manifest_sha256: str
    document_sha256: str
    token: object


def _authorize_production_release_import(
    *,
    plan_sha256: str,
    manifest_sha256: str,
    document_sha256: str,
) -> _ProductionReleaseImportAuthorization:
    """Issue a narrow in-process capability after release revalidation.

    The generation materializer is the only production caller.  Binding the
    exact import document prevents the capability from being replayed for a
    different scope, source, or payload.
    """

    if not all(
        _SHA256.fullmatch(value)
        for value in (plan_sha256, manifest_sha256, document_sha256)
    ):
        raise PointInTimeValidationError(
            "production release authorization digest is invalid"
        )
    return _ProductionReleaseImportAuthorization(
        plan_sha256=plan_sha256,
        manifest_sha256=manifest_sha256,
        document_sha256=document_sha256,
        token=_PRODUCTION_RELEASE_IMPORT_TOKEN,
    )


def _authorize_governed_import(
    *,
    package_id: str,
    package_sha256: str,
    document_sha256: str,
) -> _GovernedImportAuthorization:
    """Issue an in-process capability after governance verified all evidence."""

    return _GovernedImportAuthorization(
        package_id=package_id,
        package_sha256=package_sha256,
        document_sha256=document_sha256,
        token=_GOVERNED_IMPORT_TOKEN,
    )


def _optional_row_value(row: sqlite3.Row, column: str) -> Any:
    """Read a nullable v2 column from either a v2 or genuine v1 row.

    Read methods open the evidence database in SQLite read-only mode.  An
    archived v1 database cannot be migrated in-place, so absent v2 columns
    must mean "no bitemporal proof" rather than an integrity error or an
    implicit upgrade.  SQLite raises :class:`IndexError` for a missing named
    column (rather than ``KeyError``), hence the deliberately narrow fallback.
    """

    try:
        return row[column]
    except (IndexError, KeyError):
        return None


PIT_MASTER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pit_master_batches (
    batch_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    domain TEXT NOT NULL CHECK (
        domain IN ('security', 'index_membership', 'industry')
    ),
    scope_id TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN ('current_snapshot', 'effective_dated_history')
    ),
    coverage_from TEXT NOT NULL,
    coverage_to TEXT NOT NULL,
    source_provider TEXT NOT NULL,
    source_dataset TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_evidence_level TEXT NOT NULL,
    source_retrieved_at TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    batch_digest TEXT NOT NULL UNIQUE,
    imported_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    available_at TEXT,
    ingested_at TEXT,
    revision INTEGER,
    supersedes_batch_id TEXT
);

CREATE TABLE IF NOT EXISTS pit_master_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    security_code TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    effective_at TEXT,
    available_at TEXT,
    ingested_at TEXT,
    revision INTEGER,
    FOREIGN KEY (batch_id) REFERENCES pit_master_batches(batch_id)
);

CREATE TABLE IF NOT EXISTS pit_master_governed_activations (
    batch_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES pit_master_batches(batch_id)
);

CREATE INDEX IF NOT EXISTS idx_pit_master_scope_date
ON pit_master_intervals(
    domain, scope_id, effective_from, effective_to, security_code
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pit_master_interval_identity
ON pit_master_intervals(
    batch_id, domain, scope_id, security_code, effective_from, effective_to
);

CREATE TABLE IF NOT EXISTS point_in_time_batch_supersessions (
    predecessor_batch_id TEXT PRIMARY KEY,
    successor_batch_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (predecessor_batch_id) REFERENCES pit_master_batches(batch_id),
    FOREIGN KEY (successor_batch_id) REFERENCES pit_master_batches(batch_id)
);

CREATE TRIGGER IF NOT EXISTS pit_master_batches_no_update
BEFORE UPDATE ON pit_master_batches
BEGIN
    SELECT RAISE(ABORT, 'point-in-time batch is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_batches_no_delete
BEFORE DELETE ON pit_master_batches
BEGIN
    SELECT RAISE(ABORT, 'point-in-time batch cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_intervals_no_update
BEFORE UPDATE ON pit_master_intervals
BEGIN
    SELECT RAISE(ABORT, 'point-in-time interval is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_intervals_no_delete
BEFORE DELETE ON pit_master_intervals
BEGIN
    SELECT RAISE(ABORT, 'point-in-time interval cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_activations_no_update
BEFORE UPDATE ON pit_master_governed_activations
BEGIN
    SELECT RAISE(ABORT, 'point-in-time activation is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_activations_no_delete
BEFORE DELETE ON pit_master_governed_activations
BEGIN
    SELECT RAISE(ABORT, 'point-in-time activation cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_intervals_no_overlap
BEFORE INSERT ON pit_master_intervals
WHEN EXISTS (
    SELECT 1
    FROM pit_master_intervals existing
    WHERE existing.domain = NEW.domain
      AND existing.scope_id = NEW.scope_id
      AND existing.security_code = NEW.security_code
      AND existing.effective_from <= NEW.effective_to
      AND NEW.effective_from <= existing.effective_to
      AND (
          existing.batch_id = NEW.batch_id
          OR NEW.revision IS NULL
          OR NOT EXISTS (
              SELECT 1 FROM pit_master_batches incoming
              WHERE incoming.batch_id = NEW.batch_id
                AND incoming.supersedes_batch_id = existing.batch_id
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'point-in-time interval overlap');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_supersessions_no_update
BEFORE UPDATE ON point_in_time_batch_supersessions
BEGIN
    SELECT RAISE(ABORT, 'point-in-time supersession is immutable');
END;

CREATE TRIGGER IF NOT EXISTS pit_master_supersessions_no_delete
BEFORE DELETE ON point_in_time_batch_supersessions
BEGIN
    SELECT RAISE(ABORT, 'point-in-time supersession cannot be deleted');
END;
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text) or ".." in text:
        raise PointInTimeValidationError(f"{field} is invalid")
    return text


def _iso_date(value: Any, field: str) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PointInTimeValidationError(f"{field} must be YYYY-MM-DD") from exc
    return parsed.isoformat()


def _utc_timestamp(value: Any, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PointInTimeValidationError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PointInTimeValidationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _security_code(value: Any) -> str:
    text = str(value or "").strip()
    if not _SECURITY_CODE.fullmatch(text):
        raise PointInTimeValidationError("security_code must contain exactly six digits")
    return text


def _next_day(value: str) -> date:
    return date.fromisoformat(value) + timedelta(days=1)


def _intervals_cover(
    intervals: Iterable[tuple[str, str]],
    required_from: str,
    required_to: str,
) -> bool:
    cursor = date.fromisoformat(required_from)
    end = date.fromisoformat(required_to)
    for start_text, end_text in sorted(intervals):
        start = date.fromisoformat(start_text)
        interval_end = date.fromisoformat(end_text)
        if interval_end < cursor:
            continue
        if start > cursor:
            return False
        cursor = interval_end + timedelta(days=1)
        if cursor > end:
            return True
    return cursor > end


class PointInTimeMasterStore:
    """Append-only SQLite master-data store with verified read paths."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        initialize: bool = False,
    ) -> None:
        self.path = path or settings.abs_path(settings.EXPERIMENT_DB)
        if initialize:
            self.initialize_schema()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            if not self.path.exists():
                raise PointInTimeValidationError("point_in_time_store_uninitialized")
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=15,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(PIT_MASTER_SCHEMA_SQL)
            self._migrate_bitemporal_schema(connection)

    @staticmethod
    def _migrate_bitemporal_schema(connection: sqlite3.Connection) -> None:
        """Add nullable v2 evidence columns without relabelling legacy rows."""

        additions = {
            "pit_master_batches": {
                "available_at": "TEXT",
                "ingested_at": "TEXT",
                "revision": "INTEGER",
                "supersedes_batch_id": "TEXT",
            },
            "pit_master_intervals": {
                "effective_at": "TEXT",
                "available_at": "TEXT",
                "ingested_at": "TEXT",
                "revision": "INTEGER",
            },
        }
        for table, columns in additions.items():
            present = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.executescript(
            """
            DROP INDEX IF EXISTS uq_pit_master_interval_identity;
            CREATE UNIQUE INDEX uq_pit_master_interval_identity
            ON pit_master_intervals(
                batch_id, domain, scope_id, security_code,
                effective_from, effective_to
            );
            DROP TRIGGER IF EXISTS pit_master_intervals_no_overlap;
            CREATE TRIGGER pit_master_intervals_no_overlap
            BEFORE INSERT ON pit_master_intervals
            WHEN EXISTS (
                SELECT 1 FROM pit_master_intervals existing
                WHERE existing.domain = NEW.domain
                  AND existing.scope_id = NEW.scope_id
                  AND existing.security_code = NEW.security_code
                  AND existing.effective_from <= NEW.effective_to
                  AND NEW.effective_from <= existing.effective_to
                  AND (
                      existing.batch_id = NEW.batch_id
                      OR NEW.revision IS NULL
                      OR NOT EXISTS (
                          SELECT 1 FROM pit_master_batches incoming
                          WHERE incoming.batch_id = NEW.batch_id
                            AND incoming.supersedes_batch_id = existing.batch_id
                      )
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'point-in-time interval overlap');
            END;
            """
        )

    @staticmethod
    def _tables_exist(connection: sqlite3.Connection) -> bool:
        rows = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table'
              AND name IN (
                  'pit_master_batches',
                  'pit_master_intervals',
                  'pit_master_governed_activations'
              )
            """
        ).fetchall()
        return {str(row["name"]) for row in rows} == {
            "pit_master_batches",
            "pit_master_intervals",
            "pit_master_governed_activations",
        }

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        """Return whether an optional schema table is present without writing.

        Read paths deliberately support pre-bitemporal, read-only evidence
        stores.  Those stores cannot contain a supersession edge because that
        table did not exist when they were created; treating its absence as an
        empty edge set preserves the legacy evidence as legacy rather than
        inventing availability or revision metadata.
        """

        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _normalize_record(
        domain: str,
        scope_id: str,
        record: dict[str, Any],
        *,
        coverage_from: str,
        coverage_to: str,
        evidence_kind: str,
        bitemporal: bool = False,
    ) -> dict[str, Any]:
        allowed_common = {
            "security_code",
            "effective_from",
            "effective_to",
            "effective_at",
            "available_at",
        }
        allowed_by_domain = {
            "security": {"name", "exchange", "listing_status"},
            "index_membership": {"member_name"},
            "industry": {"industry_code", "industry_name"},
        }
        unexpected = set(record) - allowed_common - allowed_by_domain[domain]
        if unexpected:
            raise PointInTimeValidationError("record contains unsupported fields")
        security_code = _security_code(record.get("security_code"))
        effective_from = _iso_date(
            record.get("effective_from"),
            "effective_from",
        )
        effective_to = _iso_date(
            record.get("effective_to"),
            "effective_to",
        )
        if effective_from > effective_to:
            raise PointInTimeValidationError("effective_from must not exceed effective_to")
        if effective_from < coverage_from or effective_to > coverage_to:
            raise PointInTimeValidationError("record interval exceeds declared coverage")
        if evidence_kind == "current_snapshot" and (
            coverage_from != coverage_to
            or effective_from != coverage_from
            or effective_to != coverage_to
        ):
            raise PointInTimeValidationError(
                "current snapshots may cover their observation date only"
            )

        attributes: dict[str, str] = {}
        if domain == "security":
            exchange = _safe_id(record.get("exchange"), "exchange")
            listing_status = _safe_id(
                record.get("listing_status"),
                "listing_status",
            )
            name = str(record.get("name") or "").strip()
            if not name or len(name) > 160:
                raise PointInTimeValidationError("security name is invalid")
            attributes = {
                "exchange": exchange,
                "listing_status": listing_status,
                "name": name,
            }
        elif domain == "index_membership":
            name = str(record.get("member_name") or "").strip()
            if len(name) > 160:
                raise PointInTimeValidationError("member_name is invalid")
            attributes = {"member_name": name}
        else:
            industry_code = _safe_id(
                record.get("industry_code"),
                "industry_code",
            )
            industry_name = str(record.get("industry_name") or "").strip()
            if not industry_name or len(industry_name) > 160:
                raise PointInTimeValidationError("industry_name is invalid")
            attributes = {
                "industry_code": industry_code,
                "industry_name": industry_name,
            }
        normalized = {
            "domain": domain,
            "scope_id": scope_id,
            "security_code": security_code,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "attributes": attributes,
        }
        if bitemporal:
            effective_at = _utc_timestamp(
                record.get("effective_at"),
                "record.effective_at",
            )
            available_at = _utc_timestamp(
                record.get("available_at"),
                "record.available_at",
            )
            if effective_at[:10] != effective_from:
                raise PointInTimeValidationError(
                    "record.effective_at date must equal effective_from"
                )
            normalized.update(
                effective_at=effective_at,
                available_at=available_at,
            )
        return normalized

    def import_batch(
        self,
        *,
        schema_version: str,
        domain: Domain,
        scope_id: str,
        evidence_kind: EvidenceKind,
        coverage_from: str,
        coverage_to: str,
        source: dict[str, Any],
        records: list[dict[str, Any]],
        imported_by_user_id: int,
        _governed_authorization: _GovernedImportAuthorization | None = None,
        _production_release_authorization: (
            _ProductionReleaseImportAuthorization | None
        ) = None,
    ) -> dict[str, Any]:
        """Atomically append one complete, effective-dated domain snapshot."""

        submitted_document = {
            "schema_version": schema_version,
            "domain": domain,
            "scope_id": scope_id,
            "evidence_kind": evidence_kind,
            "coverage_from": coverage_from,
            "coverage_to": coverage_to,
            "source": source,
            "records": records,
        }
        release_authorized = bool(
            _production_release_authorization is not None
            and _production_release_authorization.token
            is _PRODUCTION_RELEASE_IMPORT_TOKEN
            and _production_release_authorization.document_sha256
            == _digest(submitted_document)
            and _SHA256.fullmatch(
                _production_release_authorization.plan_sha256
            )
            and _SHA256.fullmatch(
                _production_release_authorization.manifest_sha256
            )
        )
        governed_csi_scope = domain == "index_membership" and str(scope_id) in _GOVERNED_CSI_SCOPES
        if governed_csi_scope and evidence_kind != "effective_dated_history":
            raise PointInTimeValidationError(
                "canonical CSI current-anchor observations are quarantine-only "
                "and cannot occupy the production interval ledger"
            )
        claimed_official_source = (
            source.get("provider") == "csindex_official"
            or source.get("evidence_level") == "index_provider_authoritative"
        )
        if (governed_csi_scope or claimed_official_source) and not release_authorized:
            if (
                not governed_csi_scope
                or source.get("provider") != "csindex_official"
                or source.get("evidence_level") != "index_provider_authoritative"
                or _governed_authorization is None
                or _governed_authorization.token is not _GOVERNED_IMPORT_TOKEN
                or _governed_authorization.document_sha256 != _digest(submitted_document)
                or not _PIT_PACKAGE_ID.fullmatch(_governed_authorization.package_id)
                or not _SHA256.fullmatch(_governed_authorization.package_sha256)
            ):
                raise PointInTimeValidationError(
                    "official CSI import requires verified governance approval"
                )

        if schema_version not in {
            IMPORT_SCHEMA_VERSION,
            BITEMPORAL_IMPORT_SCHEMA_VERSION,
        }:
            raise PointInTimeValidationError("unsupported point-in-time import schema")
        bitemporal = schema_version == BITEMPORAL_IMPORT_SCHEMA_VERSION
        if domain not in _DOMAINS or evidence_kind not in _EVIDENCE_KINDS:
            raise PointInTimeValidationError("domain or evidence_kind invalid")
        normalized_scope = _safe_id(scope_id, "scope_id")
        start = _iso_date(coverage_from, "coverage_from")
        end = _iso_date(coverage_to, "coverage_to")
        if start > end:
            raise PointInTimeValidationError("coverage_from must not exceed coverage_to")
        if not records or len(records) > 100_000:
            raise PointInTimeValidationError("records must contain between 1 and 100000 rows")
        provider = _safe_id(source.get("provider"), "source.provider")
        dataset = _safe_id(source.get("dataset"), "source.dataset")
        version = _safe_id(source.get("version"), "source.version")
        evidence_level = _safe_id(
            source.get("evidence_level"),
            "source.evidence_level",
        )
        retrieved_at = _utc_timestamp(
            source.get("retrieved_at"),
            "source.retrieved_at",
        )
        available_at: str | None = None
        revision: int | None = None
        supersedes_batch_id: str | None = None
        if bitemporal:
            available_at = _utc_timestamp(
                source.get("available_at"),
                "source.available_at",
            )
            if available_at > retrieved_at:
                raise PointInTimeValidationError(
                    "source.available_at must not exceed source.retrieved_at"
                )
            try:
                revision = int(source.get("revision"))
            except (TypeError, ValueError) as exc:
                raise PointInTimeValidationError(
                    "source.revision must be a positive integer"
                ) from exc
            if revision < 1:
                raise PointInTimeValidationError("source.revision must be a positive integer")
            claimed_predecessor = source.get("supersedes_batch_id")
            if claimed_predecessor is not None:
                supersedes_batch_id = _safe_id(
                    claimed_predecessor,
                    "source.supersedes_batch_id",
                )
        source_digest = str(source.get("content_sha256") or "").lower()
        if not _SHA256.fullmatch(source_digest):
            raise PointInTimeValidationError("source.content_sha256 must be a lowercase SHA-256")
        normalized_records = [
            self._normalize_record(
                domain,
                normalized_scope,
                dict(record),
                coverage_from=start,
                coverage_to=end,
                evidence_kind=evidence_kind,
                bitemporal=bitemporal,
            )
            for record in records
        ]
        normalized_records.sort(
            key=lambda item: (
                item["security_code"],
                item["effective_from"],
                item["effective_to"],
                _canonical_json(item["attributes"]),
            )
        )
        identities: set[tuple[str, str, str]] = set()
        last_by_code: dict[str, str] = {}
        for record in normalized_records:
            identity = (
                record["security_code"],
                record["effective_from"],
                record["effective_to"],
            )
            if identity in identities:
                raise PointInTimeConflictError("duplicate point-in-time interval")
            identities.add(identity)
            previous_end = last_by_code.get(record["security_code"])
            if previous_end is not None and record["effective_from"] <= previous_end:
                raise PointInTimeConflictError("overlapping point-in-time intervals")
            last_by_code[record["security_code"]] = record["effective_to"]

        payload_json = _canonical_json(normalized_records)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        batch_identity = {
            "schema_version": MASTER_SCHEMA_VERSION,
            "domain": domain,
            "scope_id": normalized_scope,
            "evidence_kind": evidence_kind,
            "coverage_from": start,
            "coverage_to": end,
            "source": {
                "provider": provider,
                "dataset": dataset,
                "version": version,
                "evidence_level": evidence_level,
                "retrieved_at": retrieved_at,
                "content_sha256": source_digest,
            },
            "payload_sha256": payload_sha256,
        }
        if bitemporal:
            batch_identity["bitemporal"] = {
                "available_at": available_at,
                "revision": revision,
                "supersedes_batch_id": supersedes_batch_id,
            }
        batch_digest = _digest(batch_identity)
        batch_id = "pit_" + batch_digest[:32]
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        ingested_at = created_at if bitemporal else None

        self.initialize_schema()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT batch_id FROM pit_master_batches
                    WHERE batch_digest = ?
                    """,
                    (batch_digest,),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    return {
                        "batch_id": str(existing["batch_id"]),
                        "batch_digest": batch_digest,
                        "record_count": len(normalized_records),
                        "idempotent": True,
                        "quarantined": governed_csi_scope and not release_authorized,
                        "bitemporal": bitemporal,
                    }
                if bitemporal and supersedes_batch_id is not None:
                    predecessor = connection.execute(
                        "SELECT * FROM pit_master_batches WHERE batch_id=?",
                        (supersedes_batch_id,),
                    ).fetchone()
                    if predecessor is None:
                        raise PointInTimeValidationError(
                            "superseded point-in-time batch does not exist"
                        )
                    if (
                        predecessor["domain"] != domain
                        or predecessor["scope_id"] != normalized_scope
                        or predecessor["evidence_kind"] != evidence_kind
                        or predecessor["revision"] is None
                        or int(predecessor["revision"]) >= int(revision or 0)
                    ):
                        raise PointInTimeValidationError(
                            "point-in-time supersession lineage is invalid"
                        )
                    already_superseded = connection.execute(
                        """
                        SELECT 1 FROM point_in_time_batch_supersessions
                        WHERE predecessor_batch_id=?
                        """,
                        (supersedes_batch_id,),
                    ).fetchone()
                    if already_superseded is not None:
                        raise PointInTimeConflictError(
                            "point-in-time revision already has a successor"
                        )
                connection.execute(
                    """
                    INSERT INTO pit_master_batches (
                        batch_id, schema_version, domain, scope_id,
                        evidence_kind, coverage_from, coverage_to,
                        source_provider, source_dataset, source_version,
                        source_evidence_level, source_retrieved_at,
                        source_digest, payload_json, payload_sha256,
                        batch_digest, imported_by_user_id, created_at,
                        available_at, ingested_at, revision,
                        supersedes_batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        MASTER_SCHEMA_VERSION,
                        domain,
                        normalized_scope,
                        evidence_kind,
                        start,
                        end,
                        provider,
                        dataset,
                        version,
                        evidence_level,
                        retrieved_at,
                        source_digest,
                        payload_json,
                        payload_sha256,
                        batch_digest,
                        int(imported_by_user_id),
                        created_at,
                        available_at,
                        ingested_at,
                        revision,
                        supersedes_batch_id,
                    ),
                )
                for record in normalized_records:
                    row_identity = {
                        "domain": domain,
                        "scope_id": normalized_scope,
                        "security_code": record["security_code"],
                        "effective_from": record["effective_from"],
                        "effective_to": record["effective_to"],
                        "attributes": record["attributes"],
                    }
                    if bitemporal:
                        row_identity.update(
                            effective_at=record["effective_at"],
                            available_at=record["available_at"],
                        )
                    connection.execute(
                        """
                        INSERT INTO pit_master_intervals (
                            batch_id, domain, scope_id, security_code,
                            effective_from, effective_to, attributes_json,
                            row_sha256, effective_at, available_at,
                            ingested_at, revision
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            domain,
                            normalized_scope,
                            record["security_code"],
                            record["effective_from"],
                            record["effective_to"],
                            _canonical_json(record["attributes"]),
                            _digest(row_identity),
                            record.get("effective_at"),
                            record.get("available_at"),
                            ingested_at,
                            revision,
                        ),
                    )
                if release_authorized and governed_csi_scope:
                    assert _production_release_authorization is not None
                    connection.execute(
                        """
                        INSERT INTO pit_master_governed_activations (
                            batch_id, package_id, package_sha256, activated_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            "pitpkg_"
                            + _production_release_authorization.plan_sha256[:32],
                            _production_release_authorization.plan_sha256,
                            created_at,
                        ),
                    )
                if supersedes_batch_id is not None:
                    connection.execute(
                        """
                        INSERT INTO point_in_time_batch_supersessions (
                            predecessor_batch_id, successor_batch_id,
                            recorded_at
                        ) VALUES (?, ?, ?)
                        """,
                        (supersedes_batch_id, batch_id, ingested_at),
                    )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PointInTimeConflictError(
                "point-in-time evidence overlaps an immutable interval"
            ) from exc
        return {
            "batch_id": batch_id,
            "batch_digest": batch_digest,
            "record_count": len(normalized_records),
            "idempotent": False,
            "quarantined": governed_csi_scope and not release_authorized,
            "bitemporal": bitemporal,
            "available_at": available_at,
            "ingested_at": ingested_at,
            "revision": revision,
            "supersedes_batch_id": supersedes_batch_id,
        }

    def activate_governed_csi_package(
        self,
        *,
        package_id: str,
        package_sha256: str,
        receipts: Sequence[dict[str, str]],
    ) -> dict[str, Any]:
        """Atomically make all four governed CSI scopes query-visible.

        Governed imports are quarantined by default.  Activation records for
        CSI 300/500/800/1000 are inserted in one master-database transaction
        only after the governance journal has durable receipts for every
        scope.  A partial or conflicting package therefore remains invisible.
        """

        if not _PIT_PACKAGE_ID.fullmatch(package_id) or not _SHA256.fullmatch(package_sha256):
            raise PointInTimeValidationError("governed package activation identity is invalid")
        if not isinstance(receipts, Sequence) or len(receipts) != 4:
            raise PointInTimeValidationError("governed CSI activation requires four scope receipts")
        by_scope: dict[str, dict[str, str]] = {}
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise PointInTimeValidationError("governed activation receipt is invalid")
            scope_id = str(receipt.get("scope_id") or "")
            batch_id = str(receipt.get("batch_id") or "")
            batch_digest = str(receipt.get("batch_digest") or "")
            if (
                scope_id in by_scope
                or scope_id not in _GOVERNED_CSI_SCOPES
                or not re.fullmatch(r"pit_[0-9a-f]{32}", batch_id)
                or not _SHA256.fullmatch(batch_digest)
            ):
                raise PointInTimeValidationError("governed activation receipt identity is invalid")
            by_scope[scope_id] = {
                "batch_id": batch_id,
                "batch_digest": batch_digest,
            }
        if set(by_scope) != _GOVERNED_CSI_SCOPES:
            raise PointInTimeValidationError("governed CSI activation scope set is incomplete")
        activated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.initialize_schema()
        inserted = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for scope_id, receipt in sorted(by_scope.items()):
                batch = connection.execute(
                    """
                    SELECT batch_id, batch_digest, domain, scope_id, evidence_kind
                    FROM pit_master_batches WHERE batch_id=?
                    """,
                    (receipt["batch_id"],),
                ).fetchone()
                if (
                    batch is None
                    or str(batch["batch_digest"]) != receipt["batch_digest"]
                    or str(batch["domain"]) != "index_membership"
                    or str(batch["scope_id"]) != scope_id
                    or str(batch["evidence_kind"]) != "effective_dated_history"
                ):
                    raise PointInTimeIntegrityError(
                        "governed activation receipt does not match master batch"
                    )
                existing = connection.execute(
                    """
                    SELECT package_id, package_sha256
                    FROM pit_master_governed_activations WHERE batch_id=?
                    """,
                    (receipt["batch_id"],),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["package_id"]) != package_id
                        or str(existing["package_sha256"]) != package_sha256
                    ):
                        raise PointInTimeConflictError(
                            "master batch is activated by another package"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO pit_master_governed_activations (
                        batch_id, package_id, package_sha256, activated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        receipt["batch_id"],
                        package_id,
                        package_sha256,
                        activated_at,
                    ),
                )
                inserted = True
            connection.commit()
        return {
            "package_id": package_id,
            "package_sha256": package_sha256,
            "activated_scopes": sorted(by_scope),
            "idempotent": not inserted,
        }

    @staticmethod
    def _batch_identity(batch: sqlite3.Row) -> dict[str, Any]:
        identity = {
            "schema_version": batch["schema_version"],
            "domain": batch["domain"],
            "scope_id": batch["scope_id"],
            "evidence_kind": batch["evidence_kind"],
            "coverage_from": batch["coverage_from"],
            "coverage_to": batch["coverage_to"],
            "source": {
                "provider": batch["source_provider"],
                "dataset": batch["source_dataset"],
                "version": batch["source_version"],
                "evidence_level": batch["source_evidence_level"],
                "retrieved_at": batch["source_retrieved_at"],
                "content_sha256": batch["source_digest"],
            },
            "payload_sha256": batch["payload_sha256"],
        }
        available_at = _optional_row_value(batch, "available_at")
        if available_at is not None:
            identity["bitemporal"] = {
                "available_at": available_at,
                "revision": _optional_row_value(batch, "revision"),
                "supersedes_batch_id": _optional_row_value(batch, "supersedes_batch_id"),
            }
        return identity

    @staticmethod
    def _batch_provenance(batch: sqlite3.Row) -> dict[str, Any]:
        """Return source identity without exposing storage implementation."""

        provenance = {
            "batch_id": str(batch["batch_id"]),
            "batch_digest": str(batch["batch_digest"]),
            "evidence_kind": str(batch["evidence_kind"]),
            "coverage_from": str(batch["coverage_from"]),
            "coverage_to": str(batch["coverage_to"]),
            "source": {
                "provider": str(batch["source_provider"]),
                "dataset": str(batch["source_dataset"]),
                "version": str(batch["source_version"]),
                "evidence_level": str(batch["source_evidence_level"]),
                "retrieved_at": str(batch["source_retrieved_at"]),
                "content_sha256": str(batch["source_digest"]),
            },
        }
        available_at = _optional_row_value(batch, "available_at")
        ingested_at = _optional_row_value(batch, "ingested_at")
        revision = _optional_row_value(batch, "revision")
        provenance["bitemporal"] = {
            "verified": bool(available_at and ingested_at and revision is not None),
            "available_at": available_at,
            "ingested_at": ingested_at,
            "revision": revision,
            "supersedes_batch_id": _optional_row_value(batch, "supersedes_batch_id"),
        }
        return provenance

    def _verify_batch(
        self,
        connection: sqlite3.Connection,
        batch: sqlite3.Row,
    ) -> list[dict[str, Any]]:
        payload_json = str(batch["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != batch["payload_sha256"]:
            raise PointInTimeIntegrityError("point-in-time payload integrity mismatch")
        if _digest(self._batch_identity(batch)) != batch["batch_digest"]:
            raise PointInTimeIntegrityError("point-in-time batch integrity mismatch")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise PointInTimeIntegrityError("point-in-time payload is invalid") from exc
        rows = connection.execute(
            """
            SELECT * FROM pit_master_intervals
            WHERE batch_id = ?
            ORDER BY security_code, effective_from, effective_to,
                     attributes_json
            """,
            (batch["batch_id"],),
        ).fetchall()
        reconstructed: list[dict[str, Any]] = []
        for row in rows:
            try:
                attributes = json.loads(str(row["attributes_json"]))
            except json.JSONDecodeError as exc:
                raise PointInTimeIntegrityError("point-in-time row attributes are invalid") from exc
            row_identity = {
                "domain": row["domain"],
                "scope_id": row["scope_id"],
                "security_code": row["security_code"],
                "effective_from": row["effective_from"],
                "effective_to": row["effective_to"],
                "attributes": attributes,
            }
            effective_at = _optional_row_value(row, "effective_at")
            if effective_at is not None:
                row_identity.update(
                    effective_at=effective_at,
                    available_at=_optional_row_value(row, "available_at"),
                )
            if _digest(row_identity) != row["row_sha256"]:
                raise PointInTimeIntegrityError("point-in-time row integrity mismatch")
            reconstructed.append(row_identity)
        if reconstructed != payload:
            raise PointInTimeIntegrityError("point-in-time batch rows do not match payload")
        return reconstructed

    def _read_batches(
        self,
        connection: sqlite3.Connection,
        *,
        domain: str,
        scope_id: str,
        start: str,
        end: str,
        as_known_at: str | None = None,
    ) -> list[tuple[sqlite3.Row, list[dict[str, Any]]]]:
        batches = connection.execute(
            """
            SELECT * FROM pit_master_batches
            WHERE domain = ? AND scope_id = ?
              AND coverage_from <= ? AND coverage_to >= ?
              AND (
                    domain != 'index_membership'
                    OR scope_id NOT IN ('csi300','csi500','csi800','csi1000')
                    OR EXISTS (
                        SELECT 1
                        FROM pit_master_governed_activations activation
                        WHERE activation.batch_id=pit_master_batches.batch_id
                    )
              )
            ORDER BY coverage_from, coverage_to, batch_id
            """,
            (domain, scope_id, end, start),
        ).fetchall()
        if as_known_at is not None:
            batches = [
                batch
                for batch in batches
                if _optional_row_value(batch, "available_at") is not None
                and _optional_row_value(batch, "ingested_at") is not None
                and str(_optional_row_value(batch, "available_at")) <= as_known_at
                and str(_optional_row_value(batch, "ingested_at")) <= as_known_at
            ]
        visible_ids = {str(batch["batch_id"]) for batch in batches}
        # v1 stores predate the immutable supersession ledger.  They have no
        # possible revision edges, so retain their normal legacy read behavior
        # instead of failing a read-only query or reclassifying them as v2.
        superseded: set[str] = set()
        if self._table_exists(connection, "point_in_time_batch_supersessions"):
            superseded = {
                str(row["predecessor_batch_id"])
                for row in connection.execute(
                    """
                    SELECT predecessor_batch_id, successor_batch_id
                    FROM point_in_time_batch_supersessions
                    """
                ).fetchall()
                if str(row["successor_batch_id"]) in visible_ids
            }
        batches = [batch for batch in batches if str(batch["batch_id"]) not in superseded]
        return [(batch, self._verify_batch(connection, batch)) for batch in batches]

    @staticmethod
    def _scope_coverage(
        batches: list[tuple[sqlite3.Row, list[dict[str, Any]]]],
        *,
        start: str,
        end: str,
    ) -> tuple[bool, str | None, list[tuple[sqlite3.Row, list[dict[str, Any]]]]]:
        historical = [
            item
            for item in batches
            if item[0]["evidence_kind"] == "effective_dated_history"
            and item[0]["source_evidence_level"] in _RESEARCH_EVIDENCE_LEVELS
        ]
        if _intervals_cover(
            [
                (str(batch["coverage_from"]), str(batch["coverage_to"]))
                for batch, _records in historical
            ],
            start,
            end,
        ):
            return True, None, historical
        if any(batch["evidence_kind"] == "current_snapshot" for batch, _records in batches):
            return (
                False,
                "current_snapshot_not_valid_for_historical_research",
                historical,
            )
        if any(batch["evidence_kind"] == "effective_dated_history" for batch, _records in batches):
            return False, "historical_source_evidence_insufficient", historical
        return False, "effective_dated_history_missing", historical

    def query_as_of(
        self,
        *,
        domain: Domain,
        scope_id: str,
        as_of: str,
        security_codes: Iterable[str] = (),
        as_known_at: str | None = None,
    ) -> dict[str, Any]:
        """Return only verified records valid on the requested calendar date."""

        if domain not in _DOMAINS:
            raise PointInTimeValidationError("domain is invalid")
        scope = _safe_id(scope_id, "scope_id")
        day = _iso_date(as_of, "as_of")
        known_at = _utc_timestamp(as_known_at, "as_known_at") if as_known_at is not None else None
        codes = sorted({_security_code(item) for item in security_codes})
        try:
            connection = self._connect(readonly=True)
        except PointInTimeValidationError:
            return {
                "schema_version": READINESS_SCHEMA_VERSION,
                "available": False,
                "reason": "point_in_time_store_uninitialized",
                "domain": domain,
                "scope_id": scope,
                "as_of": day,
                "records": [],
            }
        try:
            with connection:
                if not self._tables_exist(connection):
                    return {
                        "schema_version": READINESS_SCHEMA_VERSION,
                        "available": False,
                        "reason": "point_in_time_store_uninitialized",
                        "domain": domain,
                        "scope_id": scope,
                        "as_of": day,
                        "records": [],
                    }
                batches = self._read_batches(
                    connection,
                    domain=domain,
                    scope_id=scope,
                    start=day,
                    end=day,
                    as_known_at=known_at,
                )
                ready, reason, historical = self._scope_coverage(
                    batches,
                    start=day,
                    end=day,
                )
                if not ready:
                    return {
                        "schema_version": READINESS_SCHEMA_VERSION,
                        "available": False,
                        "reason": reason,
                        "domain": domain,
                        "scope_id": scope,
                        "as_of": day,
                        "records": [],
                    }
                allowed = set(codes)
                active_records = [
                    record
                    for _batch, batch_records in historical
                    for record in batch_records
                    if record["effective_from"] <= day <= record["effective_to"]
                    and (
                        known_at is None
                        or (
                            record.get("available_at") is not None
                            and record["available_at"] <= known_at
                        )
                    )
                ]
                records = (
                    active_records
                    if not allowed
                    else [record for record in active_records if record["security_code"] in allowed]
                )
                records.sort(key=lambda item: item["security_code"])
                missing = sorted(allowed - {item["security_code"] for item in records})
                return {
                    "schema_version": READINESS_SCHEMA_VERSION,
                    "available": not missing,
                    "reason": ("requested_security_coverage_missing" if missing else None),
                    "domain": domain,
                    "scope_id": scope,
                    "as_of": day,
                    "as_known_at": known_at,
                    "bitemporal_availability_verified": bool(
                        known_at is not None
                        and historical
                        and all(
                            _optional_row_value(batch, "available_at") is not None
                            and _optional_row_value(batch, "ingested_at") is not None
                            for batch, _records in historical
                        )
                    ),
                    "records": records,
                    "source_batches": [
                        self._batch_provenance(batch) for batch, _batch_records in historical
                    ],
                    "missing_security_codes": missing[:100],
                    "missing_security_code_count": len(missing),
                }
        except sqlite3.DatabaseError as exc:
            raise PointInTimeIntegrityError("point-in-time store cannot be verified") from exc

    def resolve_display_observation(
        self,
        *,
        domain: Domain,
        scope_id: str,
        requested_as_of: str,
        security_codes: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Resolve a safe, read-only observation for an interactive display.

        Research execution must always request its exact PIT date through
        :meth:`query_as_of`.  A human-facing pool page is slightly different:
        opening it on Saturday or Sunday should not make Friday's already
        activated evidence disappear.  This helper permits *only* that narrow
        display convenience.  It never calls a provider, never writes data,
        and never walks across a missing weekday (including an unknown market
        holiday), because doing so could hide a failed PIT ingestion.

        The returned ``requested_as_of`` and ``resolved_as_of`` are separate
        by contract so clients cannot mistake a Friday observation for a
        Sunday one.
        """

        requested = _iso_date(requested_as_of, "requested_as_of")
        requested_day = date.fromisoformat(requested)
        exact = self.query_as_of(
            domain=domain,
            scope_id=scope_id,
            as_of=requested,
            security_codes=security_codes,
        )
        if exact.get("available") and exact.get("records"):
            return {
                "requested_as_of": requested,
                "resolved_as_of": requested,
                "resolution": "exact_activated_observation",
                "staleness_calendar_days": 0,
                "risk_warnings": [],
                "query": exact,
            }

        # Do not conceal an activated-but-empty index universe either.  It is
        # an integrity/data-quality failure rather than a reason to select an
        # older constituent list.
        if exact.get("available"):
            return {
                "requested_as_of": requested,
                "resolved_as_of": None,
                "resolution": "unavailable",
                "staleness_calendar_days": None,
                "risk_warnings": ["point_in_time_membership_empty"],
                "query": {
                    **exact,
                    "available": False,
                    "reason": "point_in_time_membership_empty",
                },
            }

        # A missing Monday-Friday observation is a true coverage gap.  Do not
        # infer a trading calendar or silently reuse the preceding session.
        if requested_day.weekday() < 5:
            return {
                "requested_as_of": requested,
                "resolved_as_of": None,
                "resolution": "unavailable",
                "staleness_calendar_days": None,
                "risk_warnings": [
                    "point_in_time_working_day_coverage_missing",
                    str(exact.get("reason") or "point_in_time_universe_missing"),
                ],
                "query": exact,
            }

        # Weekend-only read-through: the immediately preceding Friday must be
        # complete.  If it is missing, stop instead of crossing a weekday.
        prior_day = requested_day - timedelta(days=1)
        while prior_day.weekday() >= 5:
            prior_day -= timedelta(days=1)
        prior = self.query_as_of(
            domain=domain,
            scope_id=scope_id,
            as_of=prior_day.isoformat(),
            security_codes=security_codes,
        )
        if prior.get("available") and prior.get("records"):
            staleness = (requested_day - prior_day).days
            return {
                "requested_as_of": requested,
                "resolved_as_of": prior_day.isoformat(),
                "resolution": "weekend_prior_activated_observation",
                "staleness_calendar_days": staleness,
                "risk_warnings": [
                    "point_in_time_display_uses_prior_activated_observation",
                    f"point_in_time_display_staleness_{staleness}_calendar_days",
                ],
                "query": prior,
            }

        return {
            "requested_as_of": requested,
            "resolved_as_of": None,
            "resolution": "unavailable",
            "staleness_calendar_days": None,
            "risk_warnings": [
                "point_in_time_working_day_coverage_missing",
                str(prior.get("reason") or "point_in_time_universe_missing"),
            ],
            "query": prior,
        }

    def query_effective_history(
        self,
        *,
        domain: Domain,
        scope_id: str,
        start: str,
        end: str,
        as_known_at: str | None = None,
    ) -> dict[str, Any]:
        """Return one verified effective-dated slice without daily re-reads.

        Consumers still decide which records are active on each requested
        trading date.  This method only exposes evidence whose batch coverage
        continuously covers the requested calendar interval and whose payload,
        rows and source identity pass the immutable-store verification.
        """

        if domain not in _DOMAINS:
            raise PointInTimeValidationError("domain is invalid")
        scope = _safe_id(scope_id, "scope_id")
        start_day = _iso_date(start, "start")
        end_day = _iso_date(end, "end")
        if start_day > end_day:
            raise PointInTimeValidationError("start must not exceed end")
        known_at = _utc_timestamp(as_known_at, "as_known_at") if as_known_at is not None else None
        unavailable = {
            "schema_version": READINESS_SCHEMA_VERSION,
            "available": False,
            "domain": domain,
            "scope_id": scope,
            "start": start_day,
            "end": end_day,
            "records": [],
            "source_batches": [],
            "as_known_at": known_at,
            "bitemporal_availability_verified": False,
        }
        try:
            connection = self._connect(readonly=True)
        except PointInTimeValidationError:
            return {
                **unavailable,
                "reason": "point_in_time_store_uninitialized",
            }
        try:
            with connection:
                if not self._tables_exist(connection):
                    return {
                        **unavailable,
                        "reason": "point_in_time_store_uninitialized",
                    }
                batches = self._read_batches(
                    connection,
                    domain=domain,
                    scope_id=scope,
                    start=start_day,
                    end=end_day,
                    as_known_at=known_at,
                )
                ready, reason, historical = self._scope_coverage(
                    batches,
                    start=start_day,
                    end=end_day,
                )
                if not ready:
                    return {**unavailable, "reason": reason}
                records = [
                    record
                    for _batch, batch_records in historical
                    for record in batch_records
                    if record["effective_from"] <= end_day
                    and record["effective_to"] >= start_day
                    and (
                        known_at is None
                        or (
                            record.get("available_at") is not None
                            and record["available_at"] <= known_at
                        )
                    )
                ]
                records.sort(
                    key=lambda item: (
                        item["security_code"],
                        item["effective_from"],
                        item["effective_to"],
                    )
                )
                return {
                    **unavailable,
                    "available": True,
                    "reason": None,
                    "records": records,
                    "source_batches": [
                        self._batch_provenance(batch) for batch, _batch_records in historical
                    ],
                    "bitemporal_availability_verified": bool(
                        known_at is not None
                        and historical
                        and all(
                            _optional_row_value(batch, "available_at") is not None
                            and _optional_row_value(batch, "ingested_at") is not None
                            for batch, _records in historical
                        )
                    ),
                }
        except sqlite3.DatabaseError as exc:
            raise PointInTimeIntegrityError("point-in-time store cannot be verified") from exc

    @staticmethod
    def _records_cover_requirements(
        records: list[dict[str, Any]],
        requirements: dict[str, list[tuple[str, str]]],
    ) -> list[str]:
        by_code: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for record in records:
            by_code[record["security_code"]].append(
                (record["effective_from"], record["effective_to"])
            )
        missing: list[str] = []
        for code, ranges in requirements.items():
            if any(
                not _intervals_cover(by_code.get(code, []), start, end) for start, end in ranges
            ):
                missing.append(code)
        return sorted(missing)

    def inspect_research_coverage(
        self,
        *,
        pool_id: str,
        security_codes: Iterable[str],
        start: str,
        end: str,
        industry_scope: str = "cninfo_008001",
    ) -> dict[str, Any]:
        """Evaluate PIT universe/security/industry coverage for research."""

        pool = _safe_id(pool_id, "pool_id")
        start_day = _iso_date(start, "start")
        end_day = _iso_date(end, "end")
        if start_day > end_day:
            raise PointInTimeValidationError("start must not exceed end")
        codes = sorted({_security_code(item) for item in security_codes})
        scope = _safe_id(industry_scope, "industry_scope")
        unavailable = {
            "ready": False,
            "reason": "point_in_time_store_uninitialized",
        }
        try:
            connection = self._connect(readonly=True)
        except PointInTimeValidationError:
            return {
                "schema_version": READINESS_SCHEMA_VERSION,
                "ready": False,
                "universe": dict(unavailable),
                "security_master": dict(unavailable),
                "industry": {
                    **unavailable,
                    "scope_id": scope,
                    "neutralization_ready": False,
                },
                "limitations": ["point_in_time_store_uninitialized"],
            }
        try:
            with connection:
                if not self._tables_exist(connection):
                    raise PointInTimeValidationError("point_in_time_store_uninitialized")
                universe_batches = self._read_batches(
                    connection,
                    domain="index_membership",
                    scope_id=pool,
                    start=start_day,
                    end=end_day,
                )
                universe_ready, universe_reason, historical_universe = self._scope_coverage(
                    universe_batches,
                    start=start_day,
                    end=end_day,
                )
                membership_records = [
                    record
                    for _batch, batch_records in historical_universe
                    for record in batch_records
                    if record["effective_from"] <= end_day and record["effective_to"] >= start_day
                ]
                membership_codes = {record["security_code"] for record in membership_records}
                missing_price_codes = sorted(membership_codes - set(codes))
                if universe_ready and not membership_records:
                    universe_ready = False
                    universe_reason = "historical_membership_empty"
                if universe_ready and missing_price_codes:
                    universe_ready = False
                    universe_reason = "membership_price_coverage_missing"

                requirements: dict[str, list[tuple[str, str]]] = defaultdict(list)
                if membership_records:
                    for record in membership_records:
                        requirements[record["security_code"]].append(
                            (
                                max(start_day, record["effective_from"]),
                                min(end_day, record["effective_to"]),
                            )
                        )
                else:
                    for code in codes:
                        requirements[code].append((start_day, end_day))

                security_batches = self._read_batches(
                    connection,
                    domain="security",
                    scope_id="cn_equity",
                    start=start_day,
                    end=end_day,
                )
                security_scope_ready, security_reason, historical_security = self._scope_coverage(
                    security_batches,
                    start=start_day,
                    end=end_day,
                )
                security_records = [
                    record
                    for _batch, batch_records in historical_security
                    for record in batch_records
                ]
                missing_security = self._records_cover_requirements(
                    security_records,
                    requirements,
                )
                security_ready = security_scope_ready and not missing_security
                if security_scope_ready and missing_security:
                    security_reason = "security_effective_period_missing"

                industry_batches = self._read_batches(
                    connection,
                    domain="industry",
                    scope_id=scope,
                    start=start_day,
                    end=end_day,
                )
                industry_scope_ready, industry_reason, historical_industry = self._scope_coverage(
                    industry_batches,
                    start=start_day,
                    end=end_day,
                )
                industry_records = [
                    record
                    for _batch, batch_records in historical_industry
                    for record in batch_records
                ]
                missing_industry = self._records_cover_requirements(
                    industry_records,
                    requirements,
                )
                industry_ready = industry_scope_ready and not missing_industry
                if industry_scope_ready and missing_industry:
                    industry_reason = "industry_effective_period_missing"

                limitations = [
                    reason
                    for reason in (
                        universe_reason,
                        security_reason,
                        industry_reason,
                    )
                    if reason
                ]
                ready = universe_ready and security_ready and industry_ready
                return {
                    "schema_version": READINESS_SCHEMA_VERSION,
                    "ready": ready,
                    "query": {
                        "pool_id": pool,
                        "start": start_day,
                        "end": end_day,
                        "security_code_count": len(codes),
                    },
                    "universe": {
                        "ready": universe_ready,
                        "reason": universe_reason,
                        "scope_id": pool,
                        "evidence_kind_required": "effective_dated_history",
                        "member_code_count": len(membership_codes),
                        "missing_price_codes": missing_price_codes[:100],
                        "missing_price_code_count": len(missing_price_codes),
                    },
                    "security_master": {
                        "ready": security_ready,
                        "reason": security_reason,
                        "scope_id": "cn_equity",
                        "evidence_kind_required": "effective_dated_history",
                        "missing_security_codes": missing_security[:100],
                        "missing_security_code_count": len(missing_security),
                    },
                    "industry": {
                        "ready": industry_ready,
                        "neutralization_ready": industry_ready,
                        "reason": industry_reason,
                        "scope_id": scope,
                        "evidence_kind_required": "effective_dated_history",
                        "missing_security_codes": missing_industry[:100],
                        "missing_security_code_count": len(missing_industry),
                    },
                    "limitations": list(dict.fromkeys(limitations)),
                }
        except PointInTimeValidationError:
            return {
                "schema_version": READINESS_SCHEMA_VERSION,
                "ready": False,
                "universe": dict(unavailable),
                "security_master": dict(unavailable),
                "industry": {
                    **unavailable,
                    "scope_id": scope,
                    "neutralization_ready": False,
                },
                "limitations": ["point_in_time_store_uninitialized"],
            }
        except sqlite3.DatabaseError as exc:
            raise PointInTimeIntegrityError("point-in-time store cannot be verified") from exc
