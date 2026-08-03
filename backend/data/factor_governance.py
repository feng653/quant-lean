"""Transactional governance for factor manifests and exported strategies.

Executable factor implementations remain code-reviewed Python.  This store only
persists immutable JSON manifests, lifecycle pointers, evidence bindings and
append-only audit events.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso

import hashlib
import json
import math
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Literal

from backend.config import settings
from backend.data.factor_research_runs import (
    FactorResearchIntegrityError,
    FactorResearchRunStore,
)
from backend.research.factor_catalog import (
    FACTOR_CATALOG,
    factor_definition_digest,
)

_SCHEMA_LOCK = threading.Lock()
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_RUN_ID = re.compile(r"frun_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


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


class FactorGovernanceError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class FactorGovernanceStore:
    """SQLite source of truth for lifecycle state and strategy versions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.abs_path(settings.EXPERIMENT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure every evidence row has the current immutable digest contract
        # before governance reads it in a publication transaction.
        FactorResearchRunStore(self.path)
        self._ensure_schema()
        self._register_code_manifests()

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
                CREATE TABLE IF NOT EXISTS factor_catalog_versions (
                    factor_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    definition_digest TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'published'
                        CHECK(status IN ('published', 'deprecated')),
                    supersedes_version TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    registered_at TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    published_by INTEGER,
                    deprecated_at TEXT,
                    deprecated_by INTEGER,
                    PRIMARY KEY(factor_id, version),
                    UNIQUE(factor_id, definition_digest)
                );

                CREATE TABLE IF NOT EXISTS factor_strategy_series (
                    strategy_id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    current_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS factor_strategy_versions (
                    strategy_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    definition_json TEXT NOT NULL,
                    definition_digest TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_id, version),
                    UNIQUE(strategy_id, definition_digest),
                    FOREIGN KEY(strategy_id)
                        REFERENCES factor_strategy_series(strategy_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS factor_governance_requests (
                    actor_user_id INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_user_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS factor_governance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_factor_governance_events_entity
                ON factor_governance_events(entity_type, entity_id, id);

                CREATE TRIGGER IF NOT EXISTS factor_catalog_identity_immutable
                BEFORE UPDATE OF
                    factor_id, version, definition_digest, manifest_json,
                    supersedes_version, registered_at
                ON factor_catalog_versions
                BEGIN
                    SELECT RAISE(ABORT, 'factor definition is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS factor_strategy_versions_no_update
                BEFORE UPDATE ON factor_strategy_versions
                BEGIN
                    SELECT RAISE(ABORT, 'factor strategy evidence is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS factor_strategy_versions_no_delete
                BEFORE DELETE ON factor_strategy_versions
                BEGIN
                    SELECT RAISE(ABORT, 'factor strategy evidence is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS factor_governance_events_no_update
                BEFORE UPDATE ON factor_governance_events
                BEGIN
                    SELECT RAISE(ABORT, 'factor governance audit is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS factor_governance_events_no_delete
                BEFORE DELETE ON factor_governance_events
                BEGIN
                    SELECT RAISE(ABORT, 'factor governance audit is append-only');
                END;
                """
            )

    def _register_code_manifests(self) -> None:
        """Insert reviewed manifests once and fail closed on code identity drift."""
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for source in FACTOR_CATALOG:
                manifest = dict(source)
                digest = str(manifest["definition_digest"])
                if factor_definition_digest(manifest) != digest:
                    raise FactorGovernanceError(
                        "factor_manifest_digest_invalid",
                        "代码中的因子定义摘要无效",
                        status_code=500,
                    )
                existing = connection.execute(
                    """
                    SELECT definition_digest, manifest_json
                    FROM factor_catalog_versions
                    WHERE factor_id=? AND version=?
                    """,
                    (manifest["factor_id"], manifest["version"]),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["definition_digest"] != digest
                        or existing["manifest_json"] != _canonical_json(manifest)
                    ):
                        raise FactorGovernanceError(
                            "factor_version_overwrite_blocked",
                            (
                                f"因子 {manifest['factor_id']}@{manifest['version']} "
                                "已存在不同定义，必须发布新版本"
                            ),
                            status_code=500,
                        )
                    continue
                supersedes = manifest.get("supersedes")
                if supersedes is not None:
                    parent = connection.execute(
                        """
                        SELECT 1 FROM factor_catalog_versions
                        WHERE factor_id=? AND version=?
                        """,
                        (manifest["factor_id"], supersedes),
                    ).fetchone()
                    if parent is None:
                        raise FactorGovernanceError(
                            "factor_supersedes_missing",
                            "因子清单引用了不存在的前序版本",
                            status_code=500,
                        )
                connection.execute(
                    """
                    INSERT INTO factor_catalog_versions (
                        factor_id, version, definition_digest, manifest_json,
                        status, supersedes_version, revision, registered_at,
                        published_at
                    ) VALUES (?, ?, ?, ?, 'published', ?, 1, ?, ?)
                    """,
                    (
                        manifest["factor_id"],
                        manifest["version"],
                        digest,
                        _canonical_json(manifest),
                        supersedes,
                        now,
                        now,
                    ),
                )
            for source in FACTOR_CATALOG:
                for dependency in source.get("dependencies") or []:
                    if not isinstance(dependency, dict):
                        raise FactorGovernanceError(
                            "factor_dependency_invalid",
                            "因子依赖清单格式无效",
                            status_code=500,
                        )
                    target = connection.execute(
                        """
                        SELECT 1 FROM factor_catalog_versions
                        WHERE factor_id=? AND version=?
                        """,
                        (
                            dependency.get("factor_id"),
                            dependency.get("version"),
                        ),
                    ).fetchone()
                    if target is None:
                        raise FactorGovernanceError(
                            "factor_dependency_missing",
                            "因子清单引用了不存在的依赖版本",
                            status_code=500,
                        )

    @staticmethod
    def _validate_key(idempotency_key: str) -> None:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise FactorGovernanceError(
                "idempotency_key_invalid",
                "idempotency_key 必须为 8..128 位安全字符",
                status_code=422,
            )

    def _replay(
        self,
        connection: sqlite3.Connection,
        *,
        actor_user_id: int,
        idempotency_key: str,
        operation: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT operation, request_digest, response_json
            FROM factor_governance_requests
            WHERE actor_user_id=? AND idempotency_key=?
            """,
            (actor_user_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_digest"] != request_digest:
            raise FactorGovernanceError(
                "idempotency_conflict",
                "同一幂等键已用于不同请求",
            )
        return json.loads(row["response_json"])

    @staticmethod
    def _record_request(
        connection: sqlite3.Connection,
        *,
        actor_user_id: int,
        idempotency_key: str,
        operation: str,
        request_digest: str,
        response: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO factor_governance_requests (
                actor_user_id, idempotency_key, operation, request_digest,
                response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                idempotency_key,
                operation,
                request_digest,
                _canonical_json(response),
                now,
            ),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        actor_user_id: int,
        entity_type: str,
        entity_id: str,
        event_type: str,
        entity_revision: int,
        payload: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO factor_governance_events (
                actor_user_id, entity_type, entity_id, event_type,
                entity_revision, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                entity_type,
                entity_id,
                event_type,
                entity_revision,
                _canonical_json(payload),
                now,
            ),
        )

    def list_catalog(self, *, include_deprecated: bool = True) -> list[dict[str, Any]]:
        current_versions = {
            str(item["factor_id"]): str(item["version"])
            for item in FACTOR_CATALOG
        }
        clause = "" if include_deprecated else "WHERE status='published'"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM factor_catalog_versions
                {clause}
                ORDER BY factor_id, version
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(row["manifest_json"])
            item.update(
                {
                    "status": row["status"],
                    "deprecated": row["status"] == "deprecated",
                    "current": current_versions.get(str(row["factor_id"]))
                    == str(row["version"]),
                    "revision": row["revision"],
                    "published_at": row["published_at"],
                    "deprecated_at": row["deprecated_at"],
                }
            )
            result.append(item)
        return result

    def set_factor_status(
        self,
        *,
        factor_id: str,
        version: str,
        definition_digest: str,
        status: Literal["published", "deprecated"],
        expected_revision: int,
        actor_user_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_key(idempotency_key)
        request = {
            "factor_id": factor_id,
            "version": version,
            "definition_digest": definition_digest,
            "status": status,
            "expected_revision": expected_revision,
        }
        request_digest = _digest(request)
        operation = f"factor_{status}"
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                operation=operation,
                request_digest=request_digest,
            )
            if replay is not None:
                return replay
            row = connection.execute(
                """
                SELECT * FROM factor_catalog_versions
                WHERE factor_id=? AND version=?
                """,
                (factor_id, version),
            ).fetchone()
            if row is None or row["definition_digest"] != definition_digest:
                raise FactorGovernanceError(
                    "factor_code_manifest_not_found",
                    "只允许治理随受审代码注册的精确因子版本",
                    status_code=404,
                )
            if row["revision"] != expected_revision:
                raise FactorGovernanceError(
                    "factor_revision_conflict",
                    "因子状态已被其他请求修改，请刷新后重试",
                )
            manifest = json.loads(row["manifest_json"])
            if status == "published":
                for dependency in manifest.get("dependencies") or []:
                    target = connection.execute(
                        """
                        SELECT status FROM factor_catalog_versions
                        WHERE factor_id=? AND version=?
                        """,
                        (
                            dependency.get("factor_id"),
                            dependency.get("version"),
                        ),
                    ).fetchone()
                    if target is None or target["status"] != "published":
                        raise FactorGovernanceError(
                            "factor_dependency_not_published",
                            "因子依赖版本尚未发布",
                        )
            else:
                published = connection.execute(
                    """
                    SELECT factor_id, version, manifest_json
                    FROM factor_catalog_versions
                    WHERE status='published'
                    """
                ).fetchall()
                blockers = [
                    f"{item['factor_id']}@{item['version']}"
                    for item in published
                    if any(
                        dependency.get("factor_id") == factor_id
                        and dependency.get("version") == version
                        for dependency in json.loads(item["manifest_json"]).get(
                            "dependencies"
                        )
                        or []
                    )
                ]
                if blockers:
                    raise FactorGovernanceError(
                        "factor_dependency_in_use",
                        "仍有已发布因子依赖此版本: " + ", ".join(blockers),
                    )
            revision = expected_revision + 1
            connection.execute(
                """
                UPDATE factor_catalog_versions
                SET status=?, revision=?,
                    deprecated_at=?, deprecated_by=?,
                    published_at=CASE WHEN ?='published' THEN ? ELSE published_at END,
                    published_by=CASE WHEN ?='published' THEN ? ELSE published_by END
                WHERE factor_id=? AND version=? AND revision=?
                """,
                (
                    status,
                    revision,
                    now if status == "deprecated" else None,
                    actor_user_id if status == "deprecated" else None,
                    status,
                    now,
                    status,
                    actor_user_id,
                    factor_id,
                    version,
                    expected_revision,
                ),
            )
            response = {
                "factor_id": factor_id,
                "version": version,
                "definition_digest": definition_digest,
                "status": status,
                "deprecated": status == "deprecated",
                "revision": revision,
            }
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                entity_type="factor_definition",
                entity_id=f"{factor_id}@{version}",
                event_type=operation,
                entity_revision=revision,
                payload={"definition_digest": definition_digest},
                now=now,
            )
            self._record_request(
                connection,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                operation=operation,
                request_digest=request_digest,
                response=response,
                now=now,
            )
            return response

    def _validated_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        owner_user_id: int,
        run_ids: list[str],
        component_ids: set[str],
    ) -> list[dict[str, str]]:
        if not run_ids:
            raise FactorGovernanceError(
                "factor_strategy_evidence_required",
                "导出因子组合至少需要一个已完成研究运行",
                status_code=422,
            )
        if len(set(run_ids)) != len(run_ids) or any(
            not _RUN_ID.fullmatch(run_id) for run_id in run_ids
        ):
            raise FactorGovernanceError(
                "factor_strategy_evidence_invalid",
                "研究运行标识无效或重复",
                status_code=422,
            )
        placeholders = ",".join("?" for _ in run_ids)
        rows = connection.execute(
            f"""
            SELECT * FROM factor_research_runs
            WHERE owner_user_id=? AND run_id IN ({placeholders})
            """,
            (owner_user_id, *run_ids),
        ).fetchall()
        by_id = {str(row["run_id"]): row for row in rows}
        if len(by_id) != len(run_ids):
            # Do not disclose whether a foreign user's run exists.
            raise FactorGovernanceError(
                "factor_strategy_evidence_not_found",
                "研究运行不存在",
                status_code=404,
            )
        evidence: list[dict[str, str]] = []
        for run_id in run_ids:
            row = by_id[run_id]
            try:
                verified = FactorResearchRunStore._record(
                    row,
                    include_result=True,
                )
            except (FactorResearchIntegrityError, KeyError, TypeError, ValueError) as exc:
                raise FactorGovernanceError(
                    "factor_strategy_evidence_integrity_invalid",
                    "研究运行证据完整性校验失败",
                    status_code=409,
                ) from exc
            factor_id = str(verified["factor_id"])
            dataset_digest = str(verified["dataset_digest"])
            computed_result = str(verified["result_digest"])
            if (
                factor_id not in component_ids
                or not _DIGEST.fullmatch(dataset_digest)
            ):
                raise FactorGovernanceError(
                    "factor_strategy_evidence_integrity_invalid",
                    "研究运行证据完整性校验失败",
                    status_code=409,
                )
            factor = verified.get("factor_definition")
            if not isinstance(factor, dict):
                raise FactorGovernanceError(
                    "factor_strategy_factor_snapshot_missing",
                    "研究运行缺少因子定义快照",
                    status_code=409,
                )
            factor_version = str(factor.get("version") or "legacy_unversioned")
            factor_digest = str(factor.get("definition_digest") or _digest(factor))
            evidence.append(
                {
                    "run_id": run_id,
                    "factor_id": factor_id,
                    "factor_version": factor_version,
                    "factor_definition_digest": factor_digest,
                    "dataset_digest": dataset_digest,
                    "result_digest": computed_result,
                }
            )
        return evidence

    @staticmethod
    def _normalize_components(
        components: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not 1 <= len(components) <= 20:
            raise FactorGovernanceError(
                "factor_strategy_components_invalid",
                "components 必须包含 1..20 个因子",
                status_code=422,
            )
        known = {str(item["factor_id"]) for item in FACTOR_CATALOG}
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for item in components:
            factor_id = str(item.get("factor_id") or "")
            weight = item.get("weight")
            if (
                factor_id not in known
                or factor_id in seen
                or isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
                or not 0 < float(weight) <= 100
            ):
                raise FactorGovernanceError(
                    "factor_strategy_components_invalid",
                    "components 包含未知、重复或无效权重的因子",
                    status_code=422,
                )
            seen.add(factor_id)
            normalized.append({"factor_id": factor_id, "weight": float(weight)})
        return normalized

    def publish_strategy(
        self,
        *,
        name: str,
        components: list[dict[str, Any]],
        top_k_pct: float,
        research_run_ids: list[str],
        owner_user_id: int,
        actor_user_id: int,
        idempotency_key: str,
        strategy_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        self._validate_key(idempotency_key)
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 80:
            raise FactorGovernanceError(
                "factor_strategy_name_invalid",
                "策略名称必须为 1..80 个非空字符",
                status_code=422,
            )
        if (
            isinstance(top_k_pct, bool)
            or not isinstance(top_k_pct, (int, float))
            or not math.isfinite(float(top_k_pct))
            or not 0 < float(top_k_pct) <= 1
        ):
            raise FactorGovernanceError(
                "factor_strategy_top_k_invalid",
                "top_k_pct 必须为 (0, 1] 内的有限数字",
                status_code=422,
            )
        normalized_components = self._normalize_components(components)
        request = {
            "name": normalized_name,
            "components": normalized_components,
            "top_k_pct": float(top_k_pct),
            "research_run_ids": research_run_ids,
            "strategy_id": strategy_id,
            "expected_version": expected_version,
        }
        request_digest = _digest(request)
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                operation="strategy_publish",
                request_digest=request_digest,
            )
            if replay is not None:
                return replay
            evidence = self._validated_evidence(
                connection,
                owner_user_id=owner_user_id,
                run_ids=research_run_ids,
                component_ids={
                    str(item["factor_id"]) for item in normalized_components
                },
            )
            current_definitions = {
                str(item["factor_id"]): item for item in FACTOR_CATALOG
            }
            versioned_components = [
                {
                    **component,
                    "factor_version": str(
                        current_definitions[str(component["factor_id"])]["version"]
                    ),
                    "factor_definition_digest": str(
                        current_definitions[str(component["factor_id"])][
                            "definition_digest"
                        ]
                    ),
                    "evidence_bound": any(
                        item["factor_id"] == component["factor_id"]
                        for item in evidence
                    ),
                }
                for component in normalized_components
            ]
            if strategy_id is None:
                identity = {
                    "owner_user_id": owner_user_id,
                    "components": normalized_components,
                    "top_k_pct": float(top_k_pct),
                }
                strategy_id = "factor_combo_" + _digest(identity)[:12]
                existing = connection.execute(
                    """
                    SELECT owner_user_id, current_version
                    FROM factor_strategy_series WHERE strategy_id=?
                    """,
                    (strategy_id,),
                ).fetchone()
                if existing is None:
                    version = 1
                    connection.execute(
                        """
                        INSERT INTO factor_strategy_series (
                            strategy_id, owner_user_id, current_version,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, 1, 1, ?, ?)
                        """,
                        (strategy_id, owner_user_id, now, now),
                    )
                    series_revision = 1
                else:
                    if int(existing["owner_user_id"]) != owner_user_id:
                        raise FactorGovernanceError(
                            "factor_strategy_owner_conflict",
                            "策略归属冲突",
                            status_code=404,
                        )
                    # A missing explicit strategy_id means create semantics.
                    # Stable default idempotency below makes exact retries replay.
                    raise FactorGovernanceError(
                        "factor_strategy_already_exists",
                        "相同因子组合已存在；发布新版本时需指定 strategy_id",
                    )
            else:
                if not re.fullmatch(r"factor_combo_[0-9a-f]{12}", strategy_id):
                    raise FactorGovernanceError(
                        "factor_strategy_id_invalid",
                        "strategy_id 无效",
                        status_code=422,
                    )
                row = connection.execute(
                    """
                    SELECT owner_user_id, current_version, revision
                    FROM factor_strategy_series WHERE strategy_id=?
                    """,
                    (strategy_id,),
                ).fetchone()
                if row is None or int(row["owner_user_id"]) != owner_user_id:
                    raise FactorGovernanceError(
                        "factor_strategy_not_found",
                        "策略不存在",
                        status_code=404,
                    )
                if expected_version is None or int(row["current_version"]) != expected_version:
                    raise FactorGovernanceError(
                        "factor_strategy_version_conflict",
                        "策略版本已变化，请刷新后重试",
                    )
                version = expected_version + 1
                series_revision = int(row["revision"]) + 1

            definition: dict[str, Any] = {
                "schema_version": "factor-combination-strategy/v2",
                "strategy_id": strategy_id,
                "name": normalized_name,
                "version": f"1.0.{version - 1}",
                "strategy_version": version,
                "components": versioned_components,
                "top_k_pct": float(top_k_pct),
                "owner_user_id": owner_user_id,
                "research_evidence": evidence,
                "legacy_unbound": False,
                "created_at": now,
            }
            definition["definition_sha256"] = _digest(definition)
            connection.execute(
                """
                INSERT INTO factor_strategy_versions (
                    strategy_id, version, definition_json,
                    definition_digest, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    version,
                    _canonical_json(definition),
                    definition["definition_sha256"],
                    actor_user_id,
                    now,
                ),
            )
            if version > 1:
                connection.execute(
                    """
                    UPDATE factor_strategy_series
                    SET current_version=?, revision=?, updated_at=?
                    WHERE strategy_id=?
                    """,
                    (version, series_revision, now, strategy_id),
                )
            response = {
                **definition,
                "series_revision": series_revision,
                "idempotent_replay": False,
            }
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                entity_type="factor_strategy",
                entity_id=strategy_id,
                event_type="strategy_created" if version == 1 else "strategy_published",
                entity_revision=series_revision,
                payload={
                    "strategy_version": version,
                    "definition_sha256": definition["definition_sha256"],
                    "research_run_ids": research_run_ids,
                },
                now=now,
            )
            self._record_request(
                connection,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                operation="strategy_publish",
                request_digest=request_digest,
                response=response,
                now=now,
            )
            return response

    def rollback_strategy(
        self,
        *,
        strategy_id: str,
        target_version: int,
        expected_version: int,
        owner_user_id: int,
        actor_user_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_key(idempotency_key)
        request = {
            "strategy_id": strategy_id,
            "target_version": target_version,
            "expected_version": expected_version,
        }
        request_digest = _digest(request)
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                operation="strategy_rollback",
                request_digest=request_digest,
            )
            if replay is not None:
                return replay
            row = connection.execute(
                """
                SELECT owner_user_id, current_version, revision
                FROM factor_strategy_series WHERE strategy_id=?
                """,
                (strategy_id,),
            ).fetchone()
            if row is None or int(row["owner_user_id"]) != owner_user_id:
                raise FactorGovernanceError(
                    "factor_strategy_not_found",
                    "策略不存在；legacy_unbound 策略不可晋级或回滚",
                    status_code=404,
                )
            if int(row["current_version"]) != expected_version:
                raise FactorGovernanceError(
                    "factor_strategy_version_conflict",
                    "策略版本已变化，请刷新后重试",
                )
            if target_version >= expected_version:
                raise FactorGovernanceError(
                    "factor_strategy_rollback_target_invalid",
                    "回滚目标必须早于当前版本",
                    status_code=422,
                )
            target = connection.execute(
                """
                SELECT definition_json FROM factor_strategy_versions
                WHERE strategy_id=? AND version=?
                """,
                (strategy_id, target_version),
            ).fetchone()
            if target is None:
                raise FactorGovernanceError(
                    "factor_strategy_target_missing",
                    "目标策略版本不存在",
                    status_code=404,
                )
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                UPDATE factor_strategy_series
                SET current_version=?, revision=?, updated_at=?
                WHERE strategy_id=?
                """,
                (target_version, revision, now, strategy_id),
            )
            definition = json.loads(target["definition_json"])
            response = {
                **definition,
                "series_revision": revision,
                "rolled_back_from": expected_version,
                "idempotent_replay": False,
            }
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                entity_type="factor_strategy",
                entity_id=strategy_id,
                event_type="strategy_rolled_back",
                entity_revision=revision,
                payload={
                    "from_version": expected_version,
                    "target_version": target_version,
                },
                now=now,
            )
            self._record_request(
                connection,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                operation="strategy_rollback",
                request_digest=request_digest,
                response=response,
                now=now,
            )
            return response

    def list_active_strategy_definitions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT versions.definition_json, series.revision
                FROM factor_strategy_series AS series
                JOIN factor_strategy_versions AS versions
                  ON versions.strategy_id=series.strategy_id
                 AND versions.version=series.current_version
                ORDER BY series.strategy_id
                """
            ).fetchall()
        result = []
        for row in rows:
            definition = json.loads(row["definition_json"])
            if _digest(
                {
                    key: value
                    for key, value in definition.items()
                    if key != "definition_sha256"
                }
            ) != definition.get("definition_sha256"):
                raise FactorGovernanceError(
                    "factor_strategy_definition_integrity_invalid",
                    "策略版本定义完整性校验失败",
                    status_code=500,
                )
            definition["series_revision"] = int(row["revision"])
            result.append(definition)
        return result

    def list_strategy_versions(
        self,
        *,
        strategy_id: str,
        owner_user_id: int,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            series = connection.execute(
                """
                SELECT * FROM factor_strategy_series
                WHERE strategy_id=? AND owner_user_id=?
                """,
                (strategy_id, owner_user_id),
            ).fetchone()
            if series is None:
                return None
            rows = connection.execute(
                """
                SELECT version, definition_json, definition_digest, created_at
                FROM factor_strategy_versions
                WHERE strategy_id=? ORDER BY version DESC
                """,
                (strategy_id,),
            ).fetchall()
        return {
            "strategy_id": strategy_id,
            "current_version": int(series["current_version"]),
            "series_revision": int(series["revision"]),
            "versions": [
                {
                    "version": int(row["version"]),
                    "definition_sha256": row["definition_digest"],
                    "created_at": row["created_at"],
                    "research_evidence": json.loads(row["definition_json"])[
                        "research_evidence"
                    ],
                }
                for row in rows
            ],
        }

    def list_audit_events(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM factor_governance_events
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
