"""Immutable, user-isolated preregistration protocols for factor research."""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from backend.config import settings


PROTOCOL_SCHEMA = "factor-research-protocol/v1"
_SCHEMA_LOCK = threading.Lock()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class FactorProtocolError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class FactorResearchProtocolStore:
    """Versioned protocol store.

    Payloads are insert-only even while a version is a draft. Editing is
    represented by creating a new version, so locking or using a version can
    never rewrite the preregistered question or thresholds.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.abs_path(settings.EXPERIMENT_DB)
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
                CREATE TABLE IF NOT EXISTS factor_research_protocol_series (
                    protocol_id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS factor_research_protocol_versions (
                    protocol_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft', 'locked')),
                    created_at TEXT NOT NULL,
                    locked_at TEXT,
                    PRIMARY KEY(protocol_id, version),
                    UNIQUE(protocol_id, payload_digest),
                    FOREIGN KEY(protocol_id)
                        REFERENCES factor_research_protocol_series(protocol_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_factor_protocol_owner_updated
                ON factor_research_protocol_series(
                    owner_user_id, updated_at DESC, protocol_id
                );
                CREATE TRIGGER IF NOT EXISTS factor_protocol_payload_immutable
                BEFORE UPDATE OF
                    protocol_id, version, payload_json, payload_digest, created_at
                ON factor_research_protocol_versions
                BEGIN
                    SELECT RAISE(ABORT, 'factor research protocol is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factor_protocol_version_no_delete
                BEFORE DELETE ON factor_research_protocol_versions
                BEGIN
                    SELECT RAISE(ABORT, 'factor research protocol cannot be deleted');
                END;
                CREATE TRIGGER IF NOT EXISTS factor_protocol_lock_no_reversal
                BEFORE UPDATE OF status, locked_at
                ON factor_research_protocol_versions
                WHEN (
                    OLD.status = 'locked'
                    AND (
                        NEW.status IS NOT OLD.status
                        OR NEW.locked_at IS NOT OLD.locked_at
                    )
                ) OR (
                    NEW.status = 'locked' AND NEW.locked_at IS NULL
                )
                BEGIN
                    SELECT RAISE(ABORT, 'factor research protocol lock is immutable');
                END;
                """
            )

    @staticmethod
    def _version_record(
        row: sqlite3.Row,
        *,
        used_run_count: int = 0,
    ) -> dict[str, Any]:
        payload_json = str(row["payload_json"])
        actual_digest = _digest(payload_json)
        if actual_digest != row["payload_digest"]:
            raise FactorProtocolError(
                "protocol_integrity_invalid",
                "研究协议完整性校验失败",
                409,
            )
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise FactorProtocolError(
                "protocol_integrity_invalid",
                "研究协议载荷不是有效 JSON",
                409,
            ) from exc
        return {
            "protocol_id": row["protocol_id"],
            "version": int(row["version"]),
            "status": row["status"],
            "payload": payload,
            "payload_digest": actual_digest,
            "created_at": row["created_at"],
            "locked_at": row["locked_at"],
            "used_run_count": used_run_count,
        }

    @staticmethod
    def _usage_counts(
        connection: sqlite3.Connection,
        owner_user_id: int,
    ) -> dict[tuple[str, int], int]:
        # Protocol identity is embedded in each immutable request. JSON1 is
        # available in supported SQLite builds; a missing legacy table simply
        # means there are no uses yet.
        exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='factor_research_runs'
            """
        ).fetchone()
        if exists is None:
            return {}
        rows = connection.execute(
            """
            SELECT
                json_extract(request_json, '$.protocol.protocol_id') AS protocol_id,
                json_extract(request_json, '$.protocol.version') AS version,
                COUNT(*) AS run_count
            FROM factor_research_runs
            WHERE owner_user_id = ?
              AND json_type(request_json, '$.protocol.protocol_id') = 'text'
            GROUP BY protocol_id, version
            """,
            (owner_user_id,),
        ).fetchall()
        return {
            (str(row["protocol_id"]), int(row["version"])): int(row["run_count"])
            for row in rows
        }

    def create(
        self,
        *,
        owner_user_id: int,
        name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        protocol_id = "fproto_" + uuid.uuid4().hex
        now = utc_now_iso()
        payload_json = _canonical_json(payload)
        payload_digest = _digest(payload_json)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO factor_research_protocol_series
                    (protocol_id, owner_user_id, name, current_version,
                     created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (protocol_id, owner_user_id, name, now, now),
            )
            connection.execute(
                """
                INSERT INTO factor_research_protocol_versions
                    (protocol_id, version, payload_json, payload_digest,
                     status, created_at)
                VALUES (?, 1, ?, ?, 'draft', ?)
                """,
                (protocol_id, payload_json, payload_digest, now),
            )
        return self.get(
            owner_user_id=owner_user_id,
            protocol_id=protocol_id,
        )

    def create_version(
        self,
        *,
        owner_user_id: int,
        protocol_id: str,
        expected_current_version: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload_json = _canonical_json(payload)
        payload_digest = _digest(payload_json)
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            series = connection.execute(
                """
                SELECT current_version FROM factor_research_protocol_series
                WHERE protocol_id = ? AND owner_user_id = ?
                """,
                (protocol_id, owner_user_id),
            ).fetchone()
            if series is None:
                raise FactorProtocolError(
                    "protocol_not_found",
                    "研究协议不存在或当前账号无权访问",
                    404,
                )
            current = int(series["current_version"])
            if current != expected_current_version:
                raise FactorProtocolError(
                    "protocol_version_conflict",
                    "研究协议已被更新，请刷新后重试",
                    409,
                )
            duplicate = connection.execute(
                """
                SELECT version FROM factor_research_protocol_versions
                WHERE protocol_id = ? AND payload_digest = ?
                """,
                (protocol_id, payload_digest),
            ).fetchone()
            if duplicate is not None:
                raise FactorProtocolError(
                    "protocol_payload_duplicate",
                    f"相同协议内容已存在于 v{int(duplicate['version'])}",
                    409,
                )
            version = current + 1
            connection.execute(
                """
                INSERT INTO factor_research_protocol_versions
                    (protocol_id, version, payload_json, payload_digest,
                     status, created_at)
                VALUES (?, ?, ?, ?, 'draft', ?)
                """,
                (protocol_id, version, payload_json, payload_digest, now),
            )
            connection.execute(
                """
                UPDATE factor_research_protocol_series
                SET current_version = ?, updated_at = ?
                WHERE protocol_id = ? AND owner_user_id = ?
                """,
                (version, now, protocol_id, owner_user_id),
            )
        return self.get(owner_user_id=owner_user_id, protocol_id=protocol_id)

    def lock(
        self,
        *,
        owner_user_id: int,
        protocol_id: str,
        version: int,
        payload_digest: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT v.*
                FROM factor_research_protocol_versions v
                JOIN factor_research_protocol_series s
                  ON s.protocol_id = v.protocol_id
                WHERE v.protocol_id = ? AND v.version = ?
                  AND s.owner_user_id = ?
                """,
                (protocol_id, version, owner_user_id),
            ).fetchone()
            if row is None:
                raise FactorProtocolError(
                    "protocol_not_found",
                    "研究协议版本不存在或当前账号无权访问",
                    404,
                )
            record = self._version_record(row)
            if record["payload_digest"] != payload_digest:
                raise FactorProtocolError(
                    "protocol_digest_conflict",
                    "协议摘要已变化，请重新审查后锁定",
                    409,
                )
            if row["status"] == "draft":
                connection.execute(
                    """
                    UPDATE factor_research_protocol_versions
                    SET status = 'locked', locked_at = ?
                    WHERE protocol_id = ? AND version = ? AND status = 'draft'
                    """,
                    (utc_now_iso(), protocol_id, version),
                )
        return self.get_version(
            owner_user_id=owner_user_id,
            protocol_id=protocol_id,
            version=version,
        )

    def list(self, *, owner_user_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.name, s.current_version, s.created_at AS series_created_at,
                       s.updated_at, v.*
                FROM factor_research_protocol_series s
                JOIN factor_research_protocol_versions v
                  ON v.protocol_id = s.protocol_id
                WHERE s.owner_user_id = ?
                ORDER BY s.updated_at DESC, v.version DESC
                """,
                (owner_user_id,),
            ).fetchall()
            counts = self._usage_counts(connection, owner_user_id)
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            protocol_id = str(row["protocol_id"])
            item = grouped.setdefault(
                protocol_id,
                {
                    "protocol_id": protocol_id,
                    "name": row["name"],
                    "current_version": int(row["current_version"]),
                    "created_at": row["series_created_at"],
                    "updated_at": row["updated_at"],
                    "versions": [],
                },
            )
            item["versions"].append(
                self._version_record(
                    row,
                    used_run_count=counts.get(
                        (protocol_id, int(row["version"])),
                        0,
                    ),
                )
            )
        return list(grouped.values())

    def get(self, *, owner_user_id: int, protocol_id: str) -> dict[str, Any]:
        items = [
            item
            for item in self.list(owner_user_id=owner_user_id)
            if item["protocol_id"] == protocol_id
        ]
        if not items:
            raise FactorProtocolError(
                "protocol_not_found",
                "研究协议不存在或当前账号无权访问",
                404,
            )
        return items[0]

    def get_version(
        self,
        *,
        owner_user_id: int,
        protocol_id: str,
        version: int,
    ) -> dict[str, Any]:
        protocol = self.get(
            owner_user_id=owner_user_id,
            protocol_id=protocol_id,
        )
        for item in protocol["versions"]:
            if item["version"] == version:
                return item
        raise FactorProtocolError(
            "protocol_not_found",
            "研究协议版本不存在或当前账号无权访问",
            404,
        )

    def require_locked(
        self,
        *,
        owner_user_id: int,
        reference: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = self.get_version(
            owner_user_id=owner_user_id,
            protocol_id=str(reference.get("protocol_id") or ""),
            version=int(reference.get("version") or 0),
        )
        if record["status"] != "locked":
            raise FactorProtocolError(
                "protocol_not_locked",
                "研究协议必须先锁定，才能发起预注册研究",
                409,
            )
        if record["payload_digest"] != reference.get("payload_digest"):
            raise FactorProtocolError(
                "protocol_digest_conflict",
                "研究请求中的协议摘要与不可变版本不一致",
                409,
            )
        payload = record["payload"]
        expected = {
            "factor_ids": [
                request.get("factor_id"),
                *(request.get("related_factor_ids") or []),
            ],
            "pool_id": request.get("pool_preset"),
            "start": request.get("start"),
            "end": request.get("end"),
            "horizons": request.get("horizons"),
            "primary_horizon": request.get("primary_horizon"),
            "quantiles": request.get("quantiles"),
            "rebalance_interval": request.get("rebalance_interval"),
            "default_cost_bps": request.get("default_cost_bps"),
            "cost_scenarios_bps": request.get("cost_scenarios_bps"),
            "neutralization": request.get("neutralization"),
        }
        actual = {
            "factor_ids": payload.get("factor_ids"),
            "pool_id": payload.get("data", {}).get("pool_id"),
            "start": payload.get("window", {}).get("start"),
            "end": payload.get("window", {}).get("end"),
            **{
                key: payload.get("implementation", {}).get(key)
                for key in (
                    "horizons",
                    "primary_horizon",
                    "quantiles",
                    "rebalance_interval",
                    "default_cost_bps",
                    "cost_scenarios_bps",
                    "neutralization",
                )
            },
        }
        mismatches = [
            key for key, value in expected.items() if actual.get(key) != value
        ]
        if mismatches:
            raise FactorProtocolError(
                "protocol_request_mismatch",
                "研究配置偏离锁定协议: " + ", ".join(mismatches),
                422,
            )
        return record


def evaluate_protocol(
    protocol: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate preregistered thresholds without changing the research result."""
    payload = protocol["payload"]
    primary = str(payload["implementation"]["primary_horizon"])
    thresholds = payload["thresholds"]
    checks = [
        {
            "metric": "rank_ic_mean",
            "operator": ">=",
            "threshold": thresholds["rank_ic_mean_min"],
            "actual": result["ic"][primary]["summary"]["rank_ic"]["mean"],
        },
        {
            "metric": "rank_ic_ir",
            "operator": ">=",
            "threshold": thresholds["rank_ic_ir_min"],
            "actual": result["ic"][primary]["summary"]["rank_ic"]["icir"],
        },
        {
            "metric": "long_short_mean",
            "operator": ">=",
            "threshold": thresholds["long_short_mean_min"],
            "actual": result["quantile_returns"]["long_short"]["mean"],
        },
    ]
    for check in checks:
        actual = check["actual"]
        check["passed"] = (
            isinstance(actual, (int, float))
            and actual >= check["threshold"]
        )
    return {
        "schema_version": "factor-research-protocol-review/v1",
        "protocol_id": protocol["protocol_id"],
        "version": protocol["version"],
        "payload_digest": protocol["payload_digest"],
        "question": payload["question"],
        "hypothesis": payload["hypothesis"],
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
        "export_rules": payload["export_rules"],
        "read_only": True,
    }
