"""Durable, immutable and user-isolated factor research run records."""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.research.factor_catalog import (
    factor_definition_digest,
    get_factor_definition,
)

RUN_SCHEMA_VERSION = "factor-research-run/v3"
_SCHEMA_LOCK = threading.Lock()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_digest(
    *,
    run_id: str,
    owner_user_id: int,
    factor_id: str,
    request_digest: str,
    dataset_digest: str,
    result_digest: str,
    schema_version: str,
    created_at: str,
    source_job_uuid: str | None,
) -> str:
    """Digest immutable run identity without the mutable archive marker."""
    return _sha256_text(
        _canonical_json(
            {
                "run_id": run_id,
                "owner_user_id": owner_user_id,
                "factor_id": factor_id,
                "request_digest": request_digest,
                "dataset_digest": dataset_digest,
                "result_digest": result_digest,
                "schema_version": schema_version,
                "created_at": created_at,
                "source_job_uuid": source_job_uuid,
            }
        )
    )


class FactorResearchIntegrityError(ValueError):
    """Persisted factor research evidence no longer matches its digests."""


class FactorResearchPayloadTooLargeError(ValueError):
    """Persisted evidence exceeds the bounded export contract."""


class FactorResearchRunStore:
    """Small SQLite store; result payloads are insert-only after completion."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            settings.abs_path(settings.EXPERIMENT_DB)
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _ensure_schema(self) -> None:
        with _SCHEMA_LOCK, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS factor_research_runs (
                    run_id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    factor_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    request_digest TEXT,
                    dataset_digest TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    run_digest TEXT,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_job_uuid TEXT,
                    factor_version TEXT,
                    factor_definition_digest TEXT,
                    factor_definition_json TEXT,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_factor_runs_owner_created
                ON factor_research_runs(owner_user_id, created_at DESC, run_id DESC);
                CREATE TRIGGER IF NOT EXISTS factor_runs_no_delete
                BEFORE DELETE ON factor_research_runs
                BEGIN
                    SELECT RAISE(ABORT, 'factor research run cannot be deleted');
                END;
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(factor_research_runs)"
                ).fetchall()
            }
            if "source_job_uuid" not in columns:
                connection.execute(
                    "ALTER TABLE factor_research_runs ADD COLUMN source_job_uuid TEXT"
                )
            if "request_digest" not in columns:
                connection.execute(
                    "ALTER TABLE factor_research_runs ADD COLUMN request_digest TEXT"
                )
            if "run_digest" not in columns:
                connection.execute(
                    "ALTER TABLE factor_research_runs ADD COLUMN run_digest TEXT"
                )
            if "factor_version" not in columns:
                connection.execute(
                    "ALTER TABLE factor_research_runs ADD COLUMN factor_version TEXT"
                )
            if "factor_definition_digest" not in columns:
                connection.execute(
                    """
                    ALTER TABLE factor_research_runs
                    ADD COLUMN factor_definition_digest TEXT
                    """
                )
            if "factor_definition_json" not in columns:
                connection.execute(
                    """
                    ALTER TABLE factor_research_runs
                    ADD COLUMN factor_definition_json TEXT
                    """
                )
            rows = connection.execute(
                """
                SELECT * FROM factor_research_runs
                WHERE request_digest IS NULL OR run_digest IS NULL
                """
            ).fetchall()
            for row in rows:
                request_digest = _sha256_text(str(row["request_json"]))
                run_digest = _run_digest(
                    run_id=str(row["run_id"]),
                    owner_user_id=int(row["owner_user_id"]),
                    factor_id=str(row["factor_id"]),
                    request_digest=request_digest,
                    dataset_digest=str(row["dataset_digest"]),
                    result_digest=str(row["result_digest"]),
                    schema_version=str(row["schema_version"]),
                    created_at=str(row["created_at"]),
                    source_job_uuid=row["source_job_uuid"],
                )
                connection.execute(
                    """
                    UPDATE factor_research_runs
                    SET request_digest = ?, run_digest = ?
                    WHERE run_id = ?
                    """,
                    (request_digest, run_digest, row["run_id"]),
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_runs_source_job
                ON factor_research_runs(source_job_uuid)
                WHERE source_job_uuid IS NOT NULL
                """
            )
            trigger = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='trigger' AND name='factor_runs_immutable_update'
                """
            ).fetchone()
            trigger_sql = str(trigger[0]) if trigger is not None else ""
            if (
                trigger is None
                or "source_job_uuid" not in trigger_sql
                or "request_digest" not in trigger_sql
                or "run_digest" not in trigger_sql
                or "factor_definition_json" not in trigger_sql
            ):
                connection.execute(
                    "DROP TRIGGER IF EXISTS factor_runs_immutable_update"
                )
                connection.execute(
                    """
                    CREATE TRIGGER factor_runs_immutable_update
                    BEFORE UPDATE OF
                        owner_user_id, factor_id, request_json, result_json,
                        request_digest, dataset_digest, result_digest, run_digest,
                        source_job_uuid, factor_version,
                        factor_definition_digest, factor_definition_json,
                        schema_version, created_at
                    ON factor_research_runs
                    BEGIN
                        SELECT RAISE(ABORT, 'factor research run is immutable');
                    END
                    """
                )

    def create(
        self,
        *,
        owner_user_id: int,
        factor_id: str,
        request: dict[str, Any],
        result: dict[str, Any],
        source_job_uuid: str | None = None,
    ) -> dict[str, Any]:
        request_json = _canonical_json(request)
        result_json = _canonical_json(result)
        request_digest = _sha256_text(request_json)
        dataset_digest = str(result["dataset"]["content_sha256"])
        result_digest = _sha256_text(result_json)
        factor_definition = result.get("factor")
        if not isinstance(factor_definition, dict):
            # Compatibility for trusted internal callers that predate v3
            # result payloads.  Persist the exact current code manifest now;
            # subsequent reads never resolve it dynamically.
            factor_definition = get_factor_definition(factor_id)
        factor_definition_json = _canonical_json(factor_definition)
        factor_version = str(
            factor_definition.get("version") or "legacy_unversioned"
        )
        factor_definition_digest = str(
            factor_definition.get("definition_digest")
            or hashlib.sha256(
                factor_definition_json.encode("utf-8")
            ).hexdigest()
        )
        run_id = "frun_" + uuid.uuid4().hex
        created_at = utc_now_iso()
        run_digest = _run_digest(
            run_id=run_id,
            owner_user_id=owner_user_id,
            factor_id=factor_id,
            request_digest=request_digest,
            dataset_digest=dataset_digest,
            result_digest=result_digest,
            schema_version=RUN_SCHEMA_VERSION,
            created_at=created_at,
            source_job_uuid=source_job_uuid,
        )
        with self._connect() as connection:
            if source_job_uuid is not None:
                existing = connection.execute(
                    """
                    SELECT run_id, created_at, request_digest, dataset_digest,
                           result_digest, run_digest, archived_at
                    FROM factor_research_runs
                    WHERE source_job_uuid = ? AND owner_user_id = ?
                    """,
                    (source_job_uuid, owner_user_id),
                ).fetchone()
                if existing is not None:
                    return dict(existing)
            connection.execute(
                """
                INSERT INTO factor_research_runs (
                    run_id, owner_user_id, factor_id, request_json, result_json,
                    request_digest, dataset_digest, result_digest, run_digest,
                    schema_version, created_at, source_job_uuid, factor_version,
                    factor_definition_digest, factor_definition_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    owner_user_id,
                    factor_id,
                    request_json,
                    result_json,
                    request_digest,
                    dataset_digest,
                    result_digest,
                    run_digest,
                    RUN_SCHEMA_VERSION,
                    created_at,
                    source_job_uuid,
                    factor_version,
                    factor_definition_digest,
                    factor_definition_json,
                ),
            )
        return {
            "run_id": run_id,
            "created_at": created_at,
            "request_digest": request_digest,
            "dataset_digest": dataset_digest,
            "result_digest": result_digest,
            "run_digest": run_digest,
            "archived_at": None,
        }

    @staticmethod
    def _record(row: sqlite3.Row, *, include_result: bool) -> dict[str, Any]:
        request_json = str(row["request_json"])
        result_json = str(row["result_json"])
        request_digest = _sha256_text(request_json)
        result_digest = _sha256_text(result_json)
        if request_digest != row["request_digest"]:
            raise FactorResearchIntegrityError(
                "factor research request integrity mismatch"
            )
        if result_digest != row["result_digest"]:
            raise FactorResearchIntegrityError(
                "factor research result integrity mismatch"
            )
        run_digest = _run_digest(
            run_id=str(row["run_id"]),
            owner_user_id=int(row["owner_user_id"]),
            factor_id=str(row["factor_id"]),
            request_digest=request_digest,
            dataset_digest=str(row["dataset_digest"]),
            result_digest=result_digest,
            schema_version=str(row["schema_version"]),
            created_at=str(row["created_at"]),
            source_job_uuid=row["source_job_uuid"],
        )
        if run_digest != row["run_digest"]:
            raise FactorResearchIntegrityError(
                "factor research run integrity mismatch"
            )
        try:
            request = json.loads(request_json)
        except json.JSONDecodeError as exc:
            raise FactorResearchIntegrityError(
                "factor research request is not valid JSON"
            ) from exc
        if not isinstance(request, dict):
            raise FactorResearchIntegrityError(
                "factor research request has an invalid shape"
            )
        payload: dict[str, Any] = {
            "run_id": row["run_id"],
            "factor_id": row["factor_id"],
            "request": request,
            "request_digest": request_digest,
            "dataset_digest": row["dataset_digest"],
            "result_digest": result_digest,
            "run_digest": run_digest,
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
            "source_job_uuid": row["source_job_uuid"],
            "archived_at": row["archived_at"],
        }
        stored_factor_json = (
            row["factor_definition_json"]
            if "factor_definition_json" in row.keys()
            else None
        )
        if stored_factor_json:
            factor_definition = json.loads(str(stored_factor_json))
            expected_digest = str(row["factor_definition_digest"])
            declared_digest = factor_definition.get("definition_digest")
            actual_digest = (
                factor_definition_digest(factor_definition)
                if declared_digest is not None
                else hashlib.sha256(
                    _canonical_json(factor_definition).encode("utf-8")
                ).hexdigest()
            )
            if (
                str(declared_digest or actual_digest) != expected_digest
                or actual_digest != expected_digest
                or str(
                    factor_definition.get("version") or "legacy_unversioned"
                )
                != row["factor_version"]
            ):
                raise FactorResearchIntegrityError(
                    "factor research definition integrity mismatch"
                )
            payload["factor_definition"] = factor_definition
            payload["factor_version"] = row["factor_version"]
            payload["factor_definition_digest"] = expected_digest
        if include_result:
            try:
                result = json.loads(result_json)
            except json.JSONDecodeError as exc:
                raise FactorResearchIntegrityError(
                    "factor research result is not valid JSON"
                ) from exc
            dataset = result.get("dataset") if isinstance(result, dict) else None
            factor = result.get("factor") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or not isinstance(dataset, dict)
                or dataset.get("content_sha256") != row["dataset_digest"]
            ):
                raise FactorResearchIntegrityError(
                    "factor research dataset integrity mismatch"
                )
            if request.get("factor_id") not in (None, row["factor_id"]):
                raise FactorResearchIntegrityError(
                    "factor research request factor mismatch"
                )
            if factor is not None and not isinstance(factor, dict):
                raise FactorResearchIntegrityError(
                    "factor research result factor has an invalid shape"
                )
            if (factor or {}).get("factor_id") not in (
                None,
                row["factor_id"],
            ):
                raise FactorResearchIntegrityError(
                    "factor research result factor mismatch"
                )
            if "factor_definition" not in payload:
                legacy_factor = result.get("factor")
                if not isinstance(legacy_factor, dict):
                    raise FactorResearchIntegrityError(
                        "factor research legacy definition missing"
                    )
                legacy_json = _canonical_json(legacy_factor)
                payload["factor_definition"] = legacy_factor
                payload["factor_version"] = "legacy_unversioned"
                payload["factor_definition_digest"] = hashlib.sha256(
                    legacy_json.encode("utf-8")
                ).hexdigest()
            payload["result"] = result
        return payload

    def get(self, *, owner_user_id: int, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM factor_research_runs
                WHERE run_id = ? AND owner_user_id = ?
                """,
                (run_id, owner_user_id),
            ).fetchone()
        return self._record(row, include_result=True) if row else None

    def get_for_export(
        self,
        *,
        owner_user_id: int,
        run_id: str,
        max_payload_bytes: int,
    ) -> dict[str, Any] | None:
        """Read one owned run after rejecting oversized persisted JSON."""
        with self._connect() as connection:
            size_row = connection.execute(
                """
                SELECT length(CAST(request_json AS BLOB))
                       + length(CAST(result_json AS BLOB)) AS payload_bytes
                FROM factor_research_runs
                WHERE run_id = ? AND owner_user_id = ?
                """,
                (run_id, owner_user_id),
            ).fetchone()
            if size_row is None:
                return None
            if int(size_row["payload_bytes"] or 0) > max_payload_bytes:
                raise FactorResearchPayloadTooLargeError(
                    "factor research evidence exceeds export size limit"
                )
            row = connection.execute(
                """
                SELECT * FROM factor_research_runs
                WHERE run_id = ? AND owner_user_id = ?
                """,
                (run_id, owner_user_id),
            ).fetchone()
        return self._record(row, include_result=True) if row else None

    def source_job_status(self, source_job_uuid: str | None) -> str | None:
        """Return a linked job status when this database has a job ledger."""
        if source_job_uuid is None:
            return None
        with self._connect() as connection:
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'jobs'
                """
            ).fetchone()
            if table is None:
                return None
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_uuid = ?",
                (source_job_uuid,),
            ).fetchone()
        return str(row["status"]) if row is not None else None

    def list(
        self,
        *,
        owner_user_id: int,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for internal callers that need the first page."""
        rows, _ = self.query(
            owner_user_id=owner_user_id,
            include_archived=include_archived,
            page=1,
            page_size=limit,
        )
        return rows

    def query(
        self,
        *,
        owner_user_id: int,
        include_archived: bool = False,
        factor_id: str | None = None,
        query: str | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a stable, owner-isolated page without exposing result payloads."""
        if page < 1 or not 1 <= page_size <= 200:
            raise ValueError("invalid factor research pagination")
        order_by = {
            "newest": "created_at DESC, run_id DESC",
            "oldest": "created_at ASC, run_id ASC",
            "factor": "factor_id ASC, created_at DESC, run_id DESC",
            "horizon": (
                "CAST(json_extract(request_json, '$.primary_horizon') AS INTEGER) "
                "ASC, created_at DESC, run_id DESC"
            ),
        }.get(sort)
        if order_by is None:
            raise ValueError("invalid factor research sort")

        clauses = ["owner_user_id = ?"]
        values: list[Any] = [owner_user_id]
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if factor_id:
            clauses.append("factor_id = ?")
            values.append(factor_id)
        normalized_query = (query or "").strip()
        if normalized_query:
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append(
                "(run_id LIKE ? ESCAPE '\\' OR factor_id LIKE ? ESCAPE '\\')"
            )
            values.extend([f"%{escaped}%", f"%{escaped}%"])
        where = " AND ".join(clauses)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM factor_research_runs WHERE {where}",
                    values,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM factor_research_runs
                WHERE {where}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                [*values, page_size, (page - 1) * page_size],
            ).fetchall()
        return (
            [self._record(row, include_result=False) for row in rows],
            total,
        )

    def archive(self, *, owner_user_id: int, run_id: str) -> bool:
        archived_at = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE factor_research_runs SET archived_at = ?
                WHERE run_id = ? AND owner_user_id = ? AND archived_at IS NULL
                """,
                (archived_at, run_id, owner_user_id),
            )
        return cursor.rowcount == 1
