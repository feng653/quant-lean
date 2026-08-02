"""Preregistered research workflow with immutable evidence and approval gates."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping

import aiosqlite

from backend.services.ml_promotion_evidence import (
    MLPromotionEvidenceError,
    verify_experiment_model_promotion_evidence,
)
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    canonical_json_bytes,
    canonical_sha256,
)
from backend.data.market_quality import (
    MarketDataQualityError,
    MarketDataQualitySnapshot,
)


HYPOTHESIS_TRANSITIONS = {
    "draft": {"submitted"},
    "submitted": {"withdrawn"},
    "withdrawn": set(),
}
GROUP_TRANSITIONS = {
    "draft": {"active"},
    "active": {"closed"},
    "closed": set(),
}
PROMOTION_TRANSITIONS = {
    "draft": {"reviewed"},
    "reviewed": {"approved", "rejected"},
    "approved": {"revoked"},
    "rejected": {"revoked"},
    "revoked": set(),
}
PROMOTE_PERMISSION = "experiments:promote"


class WorkflowError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        blockers: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.blockers = blockers or []

    def detail(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.blockers:
            result["blockers"] = self.blockers
        return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _is_admin(user: Mapping[str, Any]) -> bool:
    return bool(user.get("is_admin"))


def _can_promote(user: Mapping[str, Any]) -> bool:
    return _is_admin(user) or PROMOTE_PERMISSION in user.get("permissions", [])


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise WorkflowError(
            409,
            "stored_json_invalid",
            "Stored research workflow JSON is invalid",
        ) from exc


def _row_dict(
    row: aiosqlite.Row,
    *,
    json_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    result = dict(row)
    for field in json_fields:
        result[field.removesuffix("_json")] = _decode_json(
            result.pop(field),
            {},
        )
    return result


class ResearchWorkflowService:
    """Owns all state transitions; no deployment or execution dependencies."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def _connect(self) -> aiosqlite.Connection:
        connection = await aiosqlite.connect(str(self.db_path))
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    async def _audit(
        connection: aiosqlite.Connection,
        *,
        owner_user_id: int,
        actor_user_id: int,
        entity_type: str,
        entity_id: int,
        event_type: str,
        entity_version: int,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO research_workflow_events
                (owner_user_id, actor_user_id, entity_type, entity_id,
                 event_type, from_status, to_status, entity_version,
                 payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_user_id,
                actor_user_id,
                entity_type,
                entity_id,
                event_type,
                from_status,
                to_status,
                entity_version,
                _json(dict(payload or {})),
                _now(),
            ),
        )

    @staticmethod
    async def _owned(
        connection: aiosqlite.Connection,
        table: str,
        entity_id: int,
        user: Mapping[str, Any],
    ) -> aiosqlite.Row:
        cursor = await connection.execute(
            f"SELECT * FROM {table} WHERE id=?",
            (entity_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise WorkflowError(404, "not_found", "Research entity not found")
        if not _is_admin(user) and int(row["user_id"]) != int(user["id"]):
            raise WorkflowError(404, "not_found", "Research entity not found")
        return row

    async def create_hypothesis(
        self,
        payload: Mapping[str, Any],
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """
                SELECT * FROM research_hypotheses
                WHERE user_id=? AND idempotency_key=?
                """,
                (user["id"], payload["idempotency_key"]),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                same_request = (
                    existing["title"] == payload["title"]
                    and existing["falsifiable_statement"]
                    == payload["falsifiable_statement"]
                    and existing["preregistered_metrics_json"]
                    == _json(payload["preregistered_metrics"])
                    and existing["risk_acceptance_json"]
                    == _json(payload["risk_acceptance"])
                )
                if not same_request:
                    raise WorkflowError(
                        409,
                        "idempotency_conflict",
                        "Idempotency key belongs to another hypothesis request",
                    )
                await connection.commit()
                return self._hypothesis(existing)
            timestamp = _now()
            cursor = await connection.execute(
                """
                INSERT INTO research_hypotheses
                    (user_id, title, falsifiable_statement,
                     preregistered_metrics_json, risk_acceptance_json,
                     status, version, idempotency_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)
                """,
                (
                    user["id"],
                    payload["title"],
                    payload["falsifiable_statement"],
                    _json(payload["preregistered_metrics"]),
                    _json(payload["risk_acceptance"]),
                    payload["idempotency_key"],
                    timestamp,
                    timestamp,
                ),
            )
            entity_id = int(cursor.lastrowid)
            await self._audit(
                connection,
                owner_user_id=int(user["id"]),
                actor_user_id=int(user["id"]),
                entity_type="hypothesis",
                entity_id=entity_id,
                event_type="created",
                entity_version=1,
                to_status="draft",
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_hypotheses WHERE id=?",
                (entity_id,),
            )
            return self._hypothesis(await cursor.fetchone())
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def update_hypothesis(
        self,
        hypothesis_id: int,
        payload: Mapping[str, Any],
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            row = await self._owned(
                connection,
                "research_hypotheses",
                hypothesis_id,
                user,
            )
            if row["status"] != "draft":
                raise WorkflowError(
                    409,
                    "hypothesis_core_immutable",
                    "Submitted hypothesis core fields are immutable",
                )
            next_version = int(row["version"]) + 1
            cursor = await connection.execute(
                """
                UPDATE research_hypotheses
                SET title=?, falsifiable_statement=?,
                    preregistered_metrics_json=?, risk_acceptance_json=?,
                    version=?, updated_at=?
                WHERE id=? AND version=? AND status='draft'
                """,
                (
                    payload["title"],
                    payload["falsifiable_statement"],
                    _json(payload["preregistered_metrics"]),
                    _json(payload["risk_acceptance"]),
                    next_version,
                    _now(),
                    hypothesis_id,
                    payload["expected_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Hypothesis version changed",
                )
            await self._audit(
                connection,
                owner_user_id=int(row["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="hypothesis",
                entity_id=hypothesis_id,
                event_type="core_updated",
                entity_version=next_version,
                from_status="draft",
                to_status="draft",
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_hypotheses WHERE id=?",
                (hypothesis_id,),
            )
            return self._hypothesis(await cursor.fetchone())
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def transition_hypothesis(
        self,
        hypothesis_id: int,
        *,
        target_status: str,
        expected_version: int,
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            row = await self._owned(
                connection,
                "research_hypotheses",
                hypothesis_id,
                user,
            )
            current = str(row["status"])
            if target_status not in HYPOTHESIS_TRANSITIONS[current]:
                raise WorkflowError(
                    409,
                    "invalid_state_transition",
                    f"Cannot transition hypothesis {current} to {target_status}",
                )
            next_version = int(row["version"]) + 1
            timestamp_field = (
                "submitted_at" if target_status == "submitted"
                else "withdrawn_at"
            )
            cursor = await connection.execute(
                f"""
                UPDATE research_hypotheses
                SET status=?, version=?, updated_at=?, {timestamp_field}=?
                WHERE id=? AND version=? AND status=?
                """,
                (
                    target_status,
                    next_version,
                    _now(),
                    _now(),
                    hypothesis_id,
                    expected_version,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Hypothesis version changed",
                )
            await self._audit(
                connection,
                owner_user_id=int(row["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="hypothesis",
                entity_id=hypothesis_id,
                event_type=target_status,
                entity_version=next_version,
                from_status=current,
                to_status=target_status,
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_hypotheses WHERE id=?",
                (hypothesis_id,),
            )
            return self._hypothesis(await cursor.fetchone())
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    @staticmethod
    def _hypothesis(row: aiosqlite.Row) -> dict[str, Any]:
        return _row_dict(
            row,
            json_fields=(
                "preregistered_metrics_json",
                "risk_acceptance_json",
            ),
        )

    async def get_hypothesis(
        self,
        hypothesis_id: int,
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            return self._hypothesis(
                await self._owned(
                    connection,
                    "research_hypotheses",
                    hypothesis_id,
                    user,
                )
            )
        finally:
            await connection.close()

    async def create_group(
        self,
        payload: Mapping[str, Any],
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """
                SELECT * FROM research_experiment_groups
                WHERE user_id=? AND idempotency_key=?
                """,
                (user["id"], payload["idempotency_key"]),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                same_request = (
                    int(existing["hypothesis_id"])
                    == int(payload["hypothesis_id"])
                    and existing["name"] == payload["name"]
                    and existing["strategy_id"] == payload["strategy_id"]
                    and existing["selection_protocol_json"]
                    == _json(payload["selection_protocol"])
                    and existing["locked_protocol_json"]
                    == _json(payload["locked_protocol"])
                    and existing["manifest_policy_json"]
                    == _json(payload["manifest_policy"])
                )
                if not same_request:
                    raise WorkflowError(
                        409,
                        "idempotency_conflict",
                        "Idempotency key belongs to another group request",
                    )
                await connection.commit()
                return self._group(existing)
            hypothesis = await self._owned(
                connection,
                "research_hypotheses",
                int(payload["hypothesis_id"]),
                user,
            )
            if hypothesis["status"] != "submitted":
                raise WorkflowError(
                    409,
                    "hypothesis_not_submitted",
                    "Experiment groups require a submitted hypothesis",
                )
            timestamp = _now()
            cursor = await connection.execute(
                """
                INSERT INTO research_experiment_groups
                    (user_id, hypothesis_id, name, strategy_id,
                     selection_protocol_json, locked_protocol_json,
                     manifest_policy_json, status, version, idempotency_key,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)
                """,
                (
                    hypothesis["user_id"],
                    hypothesis["id"],
                    payload["name"],
                    payload["strategy_id"],
                    _json(payload["selection_protocol"]),
                    _json(payload["locked_protocol"]),
                    _json(payload["manifest_policy"]),
                    payload["idempotency_key"],
                    timestamp,
                    timestamp,
                ),
            )
            group_id = int(cursor.lastrowid)
            await self._audit(
                connection,
                owner_user_id=int(hypothesis["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="group",
                entity_id=group_id,
                event_type="created",
                entity_version=1,
                to_status="draft",
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_experiment_groups WHERE id=?",
                (group_id,),
            )
            return self._group(await cursor.fetchone())
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    @staticmethod
    def _group(row: aiosqlite.Row) -> dict[str, Any]:
        return _row_dict(
            row,
            json_fields=(
                "selection_protocol_json",
                "locked_protocol_json",
                "manifest_policy_json",
            ),
        )

    async def get_group(
        self,
        group_id: int,
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            group = await self._owned(
                connection,
                "research_experiment_groups",
                group_id,
                user,
            )
            cursor = await connection.execute(
                """
                SELECT id, experiment_id, role, source_trial_id, created_at
                FROM research_trials WHERE group_id=? ORDER BY id
                """,
                (group_id,),
            )
            result = self._group(group)
            result["trials"] = [dict(row) for row in await cursor.fetchall()]
            return result
        finally:
            await connection.close()

    async def transition_group(
        self,
        group_id: int,
        *,
        target_status: str,
        expected_version: int,
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            row = await self._owned(
                connection,
                "research_experiment_groups",
                group_id,
                user,
            )
            current = str(row["status"])
            if target_status not in GROUP_TRANSITIONS[current]:
                raise WorkflowError(
                    409,
                    "invalid_state_transition",
                    f"Cannot transition group {current} to {target_status}",
                )
            next_version = int(row["version"]) + 1
            timestamp_field = (
                "activated_at" if target_status == "active" else "closed_at"
            )
            cursor = await connection.execute(
                f"""
                UPDATE research_experiment_groups
                SET status=?, version=?, updated_at=?, {timestamp_field}=?
                WHERE id=? AND version=? AND status=?
                """,
                (
                    target_status,
                    next_version,
                    _now(),
                    _now(),
                    group_id,
                    expected_version,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Experiment group version changed",
                )
            await self._audit(
                connection,
                owner_user_id=int(row["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="group",
                entity_id=group_id,
                event_type=target_status,
                entity_version=next_version,
                from_status=current,
                to_status=target_status,
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_experiment_groups WHERE id=?",
                (group_id,),
            )
            return self._group(await cursor.fetchone())
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def link_trial(
        self,
        group_id: int,
        payload: Mapping[str, Any],
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            group = await self._owned(
                connection,
                "research_experiment_groups",
                group_id,
                user,
            )
            cursor = await connection.execute(
                """
                SELECT * FROM research_trials
                WHERE user_id=? AND idempotency_key=?
                """,
                (group["user_id"], payload["idempotency_key"]),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if (
                    int(existing["group_id"]) != group_id
                    or int(existing["experiment_id"])
                    != int(payload["experiment_id"])
                    or existing["role"] != payload["role"]
                ):
                    raise WorkflowError(
                        409,
                        "idempotency_conflict",
                        "Idempotency key belongs to another trial link",
                    )
                await connection.commit()
                return dict(existing)
            if group["status"] != "active":
                raise WorkflowError(
                    409,
                    "group_not_active",
                    "Trials may only be linked to an active group",
                )
            if int(group["version"]) != int(payload["expected_group_version"]):
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Experiment group version changed",
                )
            cursor = await connection.execute(
                "SELECT * FROM experiments WHERE id=?",
                (payload["experiment_id"],),
            )
            experiment = await cursor.fetchone()
            if (
                experiment is None
                or int(experiment["user_id"]) != int(group["user_id"])
            ):
                raise WorkflowError(
                    404,
                    "experiment_not_found",
                    "Experiment not found",
                )
            if experiment["strategy_id"] != group["strategy_id"]:
                raise WorkflowError(
                    422,
                    "strategy_mismatch",
                    "Trial strategy does not match its group",
                )
            protocol = _decode_json(
                group[
                    "selection_protocol_json"
                    if payload["role"] == "selection"
                    else "locked_protocol_json"
                ],
                {},
            )
            if (
                experiment["test_start"] != protocol.get("start")
                or experiment["test_end"] != protocol.get("end")
            ):
                raise WorkflowError(
                    422,
                    "protocol_window_mismatch",
                    "Experiment window does not match the preregistered protocol",
                )

            source_trial_id: int | None = None
            if payload["role"] == "locked_test":
                source_trial_id = await self._verify_locked_provenance(
                    connection,
                    group_id,
                    experiment,
                )
            cursor = await connection.execute(
                """
                INSERT INTO research_trials
                    (user_id, group_id, experiment_id, role, source_trial_id,
                     idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group["user_id"],
                    group_id,
                    experiment["id"],
                    payload["role"],
                    source_trial_id,
                    payload["idempotency_key"],
                    _now(),
                ),
            )
            trial_id = int(cursor.lastrowid)
            next_version = int(group["version"]) + 1
            updated = await connection.execute(
                """
                UPDATE research_experiment_groups
                SET version=?, updated_at=?
                WHERE id=? AND version=?
                """,
                (
                    next_version,
                    _now(),
                    group_id,
                    payload["expected_group_version"],
                ),
            )
            if updated.rowcount != 1:
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Experiment group version changed",
                )
            await self._audit(
                connection,
                owner_user_id=int(group["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="trial",
                entity_id=trial_id,
                event_type="linked",
                entity_version=1,
                to_status=payload["role"],
                payload={
                    "group_id": group_id,
                    "experiment_id": experiment["id"],
                    "source_trial_id": source_trial_id,
                },
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_trials WHERE id=?",
                (trial_id,),
            )
            return dict(await cursor.fetchone())
        except aiosqlite.IntegrityError as exc:
            await connection.rollback()
            raise WorkflowError(
                409,
                "trial_link_conflict",
                "Trial link or locked-test slot already exists",
            ) from exc
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    @staticmethod
    async def _verify_locked_provenance(
        connection: aiosqlite.Connection,
        group_id: int,
        experiment: aiosqlite.Row,
    ) -> int:
        source_experiment_id = experiment["source_experiment_id"]
        if source_experiment_id is None:
            raise WorkflowError(
                422,
                "locked_provenance_missing",
                "Locked-test trial must come from an explicit manual promotion",
            )
        cursor = await connection.execute(
            """
            SELECT id FROM research_trials
            WHERE group_id=? AND role='selection' AND experiment_id=?
            """,
            (group_id, source_experiment_id),
        )
        source_trial = await cursor.fetchone()
        cursor = await connection.execute(
            """
            SELECT id FROM param_sweeps
            WHERE promoted_experiment_id=?
              AND promotion_source_experiment_id=?
              AND research_trust='locked_test'
              AND user_id=?
            """,
            (
                experiment["id"],
                source_experiment_id,
                experiment["user_id"],
            ),
        )
        sweep = await cursor.fetchone()
        run_spec = _decode_json(experiment["run_spec"], {})
        if (
            source_trial is None
            or sweep is None
            or run_spec.get("research_trust") != "locked_test"
        ):
            raise WorkflowError(
                422,
                "locked_provenance_invalid",
                "Locked-test trial is not the unique manual sweep promotion",
            )
        return int(source_trial["id"])

    async def create_report(
        self,
        group_id: int,
        payload: Mapping[str, Any],
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            group = await self._owned(
                connection,
                "research_experiment_groups",
                group_id,
                user,
            )
            cursor = await connection.execute(
                """
                SELECT * FROM research_reports
                WHERE user_id=? AND idempotency_key=?
                """,
                (group["user_id"], payload["idempotency_key"]),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if (
                    int(existing["group_id"]) != group_id
                    or existing["report_type"] != payload["report_type"]
                ):
                    raise WorkflowError(
                        409,
                        "idempotency_conflict",
                        "Idempotency key belongs to another report",
                    )
                await connection.commit()
                return self._report(existing)
            if int(group["version"]) != int(payload["expected_group_version"]):
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Experiment group version changed",
                )
            role = (
                "selection"
                if payload["report_type"] == "selection"
                else "locked_test"
            )
            trials = await self._report_trials(connection, group, role)
            snapshot = {
                "schema_version": "research-report/v1",
                "group_id": group_id,
                "hypothesis_id": group["hypothesis_id"],
                "strategy_id": group["strategy_id"],
                "report_type": payload["report_type"],
                "evidence_scope": (
                    "selection_only"
                    if payload["report_type"] == "selection"
                    else "strict_locked_test_final"
                ),
                "deployment_eligible": False,
                "trials": trials,
                "created_at": _now(),
            }
            snapshot_hash = canonical_sha256(snapshot)
            cursor = await connection.execute(
                """
                INSERT INTO research_reports
                    (user_id, group_id, report_type, snapshot_json,
                     snapshot_hash, status, version, idempotency_key,
                     created_at)
                VALUES (?, ?, ?, ?, ?, 'generated', 1, ?, ?)
                """,
                (
                    group["user_id"],
                    group_id,
                    payload["report_type"],
                    _json(snapshot),
                    snapshot_hash,
                    payload["idempotency_key"],
                    _now(),
                ),
            )
            report_id = int(cursor.lastrowid)
            next_version = int(group["version"]) + 1
            updated = await connection.execute(
                """
                UPDATE research_experiment_groups
                SET version=?, updated_at=?
                WHERE id=? AND version=?
                """,
                (
                    next_version,
                    _now(),
                    group_id,
                    payload["expected_group_version"],
                ),
            )
            if updated.rowcount != 1:
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Experiment group version changed",
                )
            await self._audit(
                connection,
                owner_user_id=int(group["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="report",
                entity_id=report_id,
                event_type="generated",
                entity_version=1,
                to_status="generated",
                payload={
                    "group_id": group_id,
                    "report_type": payload["report_type"],
                    "snapshot_hash": snapshot_hash,
                },
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_reports WHERE id=?",
                (report_id,),
            )
            return self._report(await cursor.fetchone())
        except aiosqlite.IntegrityError as exc:
            await connection.rollback()
            raise WorkflowError(
                409,
                "report_conflict",
                "This immutable report already exists",
            ) from exc
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def _report_trials(
        self,
        connection: aiosqlite.Connection,
        group: aiosqlite.Row,
        role: str,
    ) -> list[dict[str, Any]]:
        cursor = await connection.execute(
            """
            SELECT t.id AS trial_id, t.source_trial_id, e.*
            FROM research_trials t
            JOIN experiments e ON e.id=t.experiment_id
            WHERE t.group_id=? AND t.role=?
            ORDER BY t.id
            """,
            (group["id"], role),
        )
        rows = await cursor.fetchall()
        if not rows:
            raise WorkflowError(
                409,
                f"{role}_trial_missing",
                f"No {role} trial is linked",
            )
        if role == "locked_test" and len(rows) != 1:
            raise WorkflowError(
                409,
                "locked_trial_not_unique",
                "Exactly one locked-test trial is required",
            )
        evidence: list[dict[str, Any]] = []
        for experiment in rows:
            if experiment["status"] != "completed":
                raise WorkflowError(
                    409,
                    "trial_not_completed",
                    f"Experiment {experiment['id']} is not completed",
                )
            manifest = await self._manifest_evidence(
                connection,
                int(experiment["id"]),
                _decode_json(group["manifest_policy_json"], {}),
                expected_strategy_id=str(group["strategy_id"]),
            )
            if role == "locked_test":
                await self._verify_locked_provenance(
                    connection,
                    int(group["id"]),
                    experiment,
                )
            evidence.append(
                {
                    "trial_id": experiment["trial_id"],
                    "experiment_id": experiment["id"],
                    "source_trial_id": experiment["source_trial_id"],
                    "role": role,
                    "status": experiment["status"],
                    "test_window": {
                        "start": experiment["test_start"],
                        "end": experiment["test_end"],
                    },
                    "metrics": await self._metrics(
                        connection,
                        int(experiment["id"]),
                    ),
                    "manifest": manifest,
                    "manual_locked_promotion_verified": role == "locked_test",
                }
            )
        return evidence

    @staticmethod
    async def _manifest_evidence(
        connection: aiosqlite.Connection,
        experiment_id: int,
        policy: Mapping[str, Any],
        *,
        expected_strategy_id: str,
    ) -> dict[str, Any]:
        cursor = await connection.execute(
            """
            SELECT schema_version, manifest_json, manifest_hash
            FROM research_run_manifests WHERE experiment_id=?
            """,
            (experiment_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise WorkflowError(
                409,
                "manifest_missing",
                f"Experiment {experiment_id} has no RunManifest",
            )
        manifest = _decode_json(row["manifest_json"], {})
        actual_hash = canonical_sha256(manifest)
        expected_schema = policy.get("schema_version", RUN_MANIFEST_SCHEMA)
        if actual_hash != row["manifest_hash"]:
            raise WorkflowError(
                409,
                "manifest_integrity_failure",
                f"Experiment {experiment_id} RunManifest was tampered",
            )
        manifest_experiment = manifest.get("experiment", {})
        if (
            row["schema_version"] != expected_schema
            or manifest.get("schema_version") != expected_schema
        ):
            raise WorkflowError(
                409,
                "manifest_schema_mismatch",
                f"Experiment {experiment_id} RunManifest schema is unsupported",
            )
        if (
            int(manifest_experiment.get("experiment_id") or 0)
            != experiment_id
            or manifest_experiment.get("strategy_id")
            != expected_strategy_id
        ):
            raise WorkflowError(
                409,
                "manifest_identity_mismatch",
                f"Experiment {experiment_id} RunManifest identity is invalid",
            )
        return {
            "schema_version": row["schema_version"],
            "manifest_hash": row["manifest_hash"],
            "git": manifest.get("environment", {}).get("git", {}),
            "dataset": manifest.get("dataset", {}),
            "windows": manifest.get("windows", {}),
            "benchmark": manifest.get("benchmark"),
            "market_data_quality": manifest.get("market_data_quality"),
            "universe": manifest.get("universe", {}),
            "research_risk_warnings": manifest.get(
                "research_risk_warnings",
                [],
            ),
            "execution": manifest.get("execution", {}),
        }

    @staticmethod
    async def _metrics(
        connection: aiosqlite.Connection,
        experiment_id: int,
    ) -> dict[str, float | int | None]:
        cursor = await connection.execute(
            "PRAGMA table_info(experiment_metrics)"
        )
        columns = {row[1] for row in await cursor.fetchall()}
        if {"metric_name", "metric_value"} <= columns:
            cursor = await connection.execute(
                """
                SELECT metric_name, metric_value
                FROM experiment_metrics
                WHERE experiment_id=? AND period='full'
                ORDER BY id
                """,
                (experiment_id,),
            )
            return {
                str(row["metric_name"]): row["metric_value"]
                for row in await cursor.fetchall()
            }
        cursor = await connection.execute(
            """
            SELECT * FROM experiment_metrics
            WHERE experiment_id=? ORDER BY id DESC LIMIT 1
            """,
            (experiment_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {}
        return {
            key: row[key]
            for key in row.keys()
            if key not in {"id", "experiment_id"} and row[key] is not None
        }

    @staticmethod
    def _report(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        snapshot = _decode_json(result.pop("snapshot_json"), {})
        actual_hash = canonical_sha256(snapshot)
        if actual_hash != result["snapshot_hash"]:
            raise WorkflowError(
                409,
                "report_integrity_failure",
                "Research report snapshot was tampered",
            )
        result["snapshot"] = snapshot
        return result

    async def create_promotion(
        self,
        group_id: int,
        payload: Mapping[str, Any],
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            group = await self._owned(
                connection,
                "research_experiment_groups",
                group_id,
                user,
            )
            cursor = await connection.execute(
                """
                SELECT * FROM research_promotions
                WHERE user_id=? AND idempotency_key=?
                """,
                (group["user_id"], payload["idempotency_key"]),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if (
                    int(existing["group_id"]) != group_id
                    or int(existing["report_id"]) != int(payload["report_id"])
                    or existing["rationale"] != payload["rationale"]
                ):
                    raise WorkflowError(
                        409,
                        "idempotency_conflict",
                        "Idempotency key belongs to another promotion",
                    )
                await connection.commit()
                return self._promotion(existing)
            if int(group["version"]) != int(payload["expected_group_version"]):
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Experiment group version changed",
                )
            cursor = await connection.execute(
                """
                SELECT * FROM research_reports
                WHERE id=? AND group_id=? AND user_id=?
                """,
                (
                    payload["report_id"],
                    group_id,
                    group["user_id"],
                ),
            )
            report = await cursor.fetchone()
            if report is None:
                raise WorkflowError(
                    404,
                    "report_not_found",
                    "Research report not found",
                )
            # Verify immutable report before binding a promotion to it.
            self._report(report)
            timestamp = _now()
            cursor = await connection.execute(
                """
                INSERT INTO research_promotions
                    (user_id, group_id, report_id, status, rationale,
                     blockers_json, version, idempotency_key,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'draft', ?, '[]', 1, ?, ?, ?)
                """,
                (
                    group["user_id"],
                    group_id,
                    report["id"],
                    payload["rationale"],
                    payload["idempotency_key"],
                    timestamp,
                    timestamp,
                ),
            )
            promotion_id = int(cursor.lastrowid)
            next_version = int(group["version"]) + 1
            updated = await connection.execute(
                """
                UPDATE research_experiment_groups
                SET version=?, updated_at=?
                WHERE id=? AND version=?
                """,
                (
                    next_version,
                    _now(),
                    group_id,
                    payload["expected_group_version"],
                ),
            )
            if updated.rowcount != 1:
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Experiment group version changed",
                )
            await self._audit(
                connection,
                owner_user_id=int(group["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="promotion",
                entity_id=promotion_id,
                event_type="created",
                entity_version=1,
                to_status="draft",
                payload={
                    "group_id": group_id,
                    "report_id": report["id"],
                    "external_action": "none",
                },
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_promotions WHERE id=?",
                (promotion_id,),
            )
            return self._promotion(await cursor.fetchone())
        except aiosqlite.IntegrityError as exc:
            await connection.rollback()
            raise WorkflowError(
                409,
                "promotion_conflict",
                "A promotion already exists for this group",
            ) from exc
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    @staticmethod
    def _promotion(row: aiosqlite.Row) -> dict[str, Any]:
        return _row_dict(row, json_fields=("blockers_json",))

    async def transition_promotion(
        self,
        promotion_id: int,
        payload: Mapping[str, Any],
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT * FROM research_promotions WHERE id=?",
                (promotion_id,),
            )
            promotion = await cursor.fetchone()
            if promotion is None or (
                int(promotion["user_id"]) != int(user["id"])
                and not _can_promote(user)
            ):
                raise WorkflowError(
                    404,
                    "not_found",
                    "Research entity not found",
                )
            current = str(promotion["status"])
            target = str(payload["target_status"])
            if target not in PROMOTION_TRANSITIONS[current]:
                raise WorkflowError(
                    409,
                    "invalid_state_transition",
                    f"Cannot transition promotion {current} to {target}",
                )
            if target in {"approved", "rejected", "revoked"} and not _can_promote(
                user
            ):
                raise WorkflowError(
                    403,
                    "promotion_permission_required",
                    f"{PROMOTE_PERMISSION} permission is required",
                )
            blockers = await self._promotion_blockers(
                connection,
                promotion,
            )
            if target == "approved" and blockers:
                raise WorkflowError(
                    409,
                    "promotion_gate_blocked",
                    "Promotion approval gates failed",
                    blockers=blockers,
                )
            next_version = int(promotion["version"]) + 1
            assignments = [
                "status=?",
                "blockers_json=?",
                "version=?",
                "updated_at=?",
            ]
            values: list[Any] = [
                target,
                _json(blockers),
                next_version,
                _now(),
            ]
            if payload.get("rationale"):
                assignments.append("rationale=?")
                values.append(payload["rationale"])
            if target == "reviewed":
                assignments.extend(["reviewed_at=?", "reviewed_by=?"])
                values.extend([_now(), user["id"]])
            elif target in {"approved", "rejected"}:
                assignments.extend(["decided_at=?", "decided_by=?"])
                values.extend([_now(), user["id"]])
            else:
                assignments.extend(["revoked_at=?", "revoked_by=?"])
                values.extend([_now(), user["id"]])
            values.extend(
                [
                    promotion_id,
                    payload["expected_version"],
                    current,
                ]
            )
            cursor = await connection.execute(
                f"""
                UPDATE research_promotions
                SET {", ".join(assignments)}
                WHERE id=? AND version=? AND status=?
                """,
                values,
            )
            if cursor.rowcount != 1:
                raise WorkflowError(
                    409,
                    "version_conflict",
                    "Promotion version changed",
                )
            await self._audit(
                connection,
                owner_user_id=int(promotion["user_id"]),
                actor_user_id=int(user["id"]),
                entity_type="promotion",
                entity_id=promotion_id,
                event_type=target,
                entity_version=next_version,
                from_status=current,
                to_status=target,
                payload={
                    "blockers": blockers,
                    "external_action": "none",
                },
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM research_promotions WHERE id=?",
                (promotion_id,),
            )
            return self._promotion(await cursor.fetchone())
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def _promotion_blockers(
        self,
        connection: aiosqlite.Connection,
        promotion: aiosqlite.Row,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        cursor = await connection.execute(
            """
            SELECT * FROM research_reports
            WHERE id=? AND group_id=?
            """,
            (promotion["report_id"], promotion["group_id"]),
        )
        report_row = await cursor.fetchone()
        if report_row is None:
            return [
                self._blocker(
                    "final_report_missing",
                    "promotion.report_id",
                    "A strict locked-test final report is required",
                )
            ]
        try:
            report = self._report(report_row)
        except WorkflowError:
            return [
                self._blocker(
                    "report_integrity_failure",
                    "promotion.report_id",
                    "The bound report failed integrity verification",
                )
            ]
        if report["report_type"] != "final":
            blockers.append(
                self._blocker(
                    "final_report_missing",
                    "report.report_type",
                    "Selection evidence cannot claim deployment readiness",
                )
            )
        snapshot = report["snapshot"]
        trials = snapshot.get("trials", [])
        if (
            snapshot.get("evidence_scope") != "strict_locked_test_final"
            or len(trials) != 1
            or trials[0].get("role") != "locked_test"
            or not trials[0].get("manual_locked_promotion_verified")
        ):
            blockers.append(
                self._blocker(
                    "locked_test_gate_failed",
                    "report.snapshot.trials",
                    "Exactly one manually promoted locked-test trial is required",
                )
            )
        cursor = await connection.execute(
            """
            SELECT h.*
            FROM research_experiment_groups g
            JOIN research_hypotheses h ON h.id=g.hypothesis_id
            WHERE g.id=?
            """,
            (promotion["group_id"],),
        )
        hypothesis = await cursor.fetchone()
        if hypothesis is None or hypothesis["status"] != "submitted":
            blockers.append(
                self._blocker(
                    "hypothesis_not_submitted",
                    "group.hypothesis_id",
                    "A submitted preregistered hypothesis is required",
                )
            )
            return blockers
        accepted = set(
            _decode_json(
                hypothesis["risk_acceptance_json"],
                {},
            ).get("accepted_risks", [])
        )
        if len(trials) == 1:
            evidence = trials[0].get("manifest", {})
            experiment_id = trials[0].get("experiment_id")
            cursor = await connection.execute(
                """
                SELECT g.manifest_policy_json, e.*
                FROM research_experiment_groups g
                JOIN experiments e ON e.id=?
                WHERE g.id=?
                """,
                (experiment_id, promotion["group_id"]),
            )
            live = await cursor.fetchone()
            if live is None or live["status"] != "completed":
                blockers.append(
                    self._blocker(
                        "locked_test_not_completed",
                        "report.trials[0].experiment_id",
                        "The locked-test experiment must remain completed",
                    )
                )
            else:
                try:
                    actual_evidence = await self._manifest_evidence(
                        connection,
                        int(experiment_id),
                        _decode_json(live["manifest_policy_json"], {}),
                        expected_strategy_id=str(live["strategy_id"]),
                    )
                    if (
                        actual_evidence["manifest_hash"]
                        != evidence.get("manifest_hash")
                    ):
                        blockers.append(
                            self._blocker(
                                "manifest_changed_after_report",
                                "report.trials[0].manifest_hash",
                                "RunManifest no longer matches the report snapshot",
                            )
                        )
                    await self._verify_locked_provenance(
                        connection,
                        int(promotion["group_id"]),
                        live,
                    )
                    evidence = actual_evidence
                except WorkflowError as exc:
                    blockers.append(
                        self._blocker(
                            exc.code,
                            "report.trials[0].manifest",
                            exc.message,
                        )
                    )
                blockers.extend(
                    await self._ml_promotion_blockers(connection, live)
                )
            blockers.extend(self._manifest_gate_blockers(evidence, accepted))
            metrics = trials[0].get("metrics", {})
            blockers.extend(
                self._metric_blockers(
                    _decode_json(
                        hypothesis["preregistered_metrics_json"],
                        [],
                    ),
                    metrics,
                )
            )
        return blockers

    @staticmethod
    async def _ml_promotion_blockers(
        connection: aiosqlite.Connection,
        experiment: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        try:
            requires_training = bool(int(experiment["requires_training"]))
        except (KeyError, TypeError, ValueError):
            return [
                ResearchWorkflowService._blocker(
                    "ml_training_contract_unknown",
                    "experiment.requires_training",
                    "The experiment training contract is missing or invalid",
                )
            ]
        if not requires_training:
            return []
        # AlphaMaster owns a private self-walk-forward loop and therefore cannot
        # provide the platform's purged validation artifact yet.
        if str(experiment["strategy_id"] or "") == "alphamaster_gbr_v1":
            return [
                ResearchWorkflowService._blocker(
                    "ml_contract_noncompliant",
                    "experiment.strategy_id",
                    (
                        "AlphaMaster uses legacy self-managed training and cannot "
                        "be promoted until it implements TrainableStrategy"
                    ),
                    expected="platform-trainable-strategy/v1",
                    actual="legacy-self-managed-training",
                )
            ]
        try:
            await verify_experiment_model_promotion_evidence(
                connection,
                experiment,
            )
        except MLPromotionEvidenceError as exc:
            return [
                ResearchWorkflowService._blocker(
                    exc.code,
                    exc.field,
                    exc.message,
                    expected=exc.expected,
                    actual=exc.actual,
                )
            ]
        except Exception as exc:
            # Database/schema/filesystem drift is a blocker, never an approval
            # endpoint 500 or an implicit legacy fallback.
            return [
                ResearchWorkflowService._blocker(
                    "ml_evidence_verification_failed",
                    "experiment.model_artifacts",
                    "Training evidence verification failed closed",
                    actual=type(exc).__name__,
                )
            ]
        return []

    @staticmethod
    def _manifest_gate_blockers(
        evidence: Mapping[str, Any],
        accepted_risks: set[str],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if evidence.get("schema_version") != RUN_MANIFEST_SCHEMA:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "manifest_schema_mismatch",
                    "manifest.schema_version",
                    "RunManifest v1 is required",
                )
            )
        manifest_hash = evidence.get("manifest_hash")
        if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "manifest_incomplete",
                    "manifest.manifest_hash",
                    "A complete verified RunManifest hash is required",
                )
            )
        if evidence.get("git", {}).get("dirty") is not False:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "dirty_code",
                    "manifest.git.dirty",
                    "Approval requires clean version-controlled code",
                )
            )
        dataset = evidence.get("dataset", {})
        if (
            not dataset.get("digest")
            or int(dataset.get("rows") or 0) <= 0
            or int(dataset.get("columns") or 0) <= 0
        ):
            blockers.append(
                ResearchWorkflowService._blocker(
                    "dataset_lineage_incomplete",
                    "manifest.dataset",
                    "Dataset lineage must be complete",
                )
            )
        market_data_quality = evidence.get("market_data_quality")
        if not isinstance(market_data_quality, Mapping):
            blockers.append(
                ResearchWorkflowService._blocker(
                    "market_data_quality_missing",
                    "manifest.market_data_quality",
                    "Verified market-data quality evidence is mandatory",
                )
            )
        else:
            try:
                quality_snapshot = MarketDataQualitySnapshot.from_dict(
                    market_data_quality
                )
            except MarketDataQualityError as exc:
                blockers.append(
                    ResearchWorkflowService._blocker(
                        "market_data_quality_integrity_failed",
                        "manifest.market_data_quality",
                        "Market-data quality evidence is invalid",
                        actual=str(exc),
                    )
                )
            else:
                if not quality_snapshot.is_clean:
                    blockers.append(
                        ResearchWorkflowService._blocker(
                            "market_data_quality_failed",
                            "manifest.market_data_quality.fatal",
                            "Fatal market-data quality issues block approval",
                            actual=list(quality_snapshot.fatal_codes),
                        )
                    )
        universe = evidence.get("universe", {})
        if not universe.get("snapshot_hash"):
            blockers.append(
                ResearchWorkflowService._blocker(
                    "universe_lineage_incomplete",
                    "manifest.universe.snapshot_hash",
                    "Universe snapshot hash is required",
                )
            )
        if universe.get("point_in_time") is not True:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "point_in_time_required",
                    "manifest.universe.point_in_time",
                    "Point-in-time universe evidence is mandatory",
                )
            )
        quality = universe.get("quality", {})
        if quality.get("is_clean") is not True:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "universe_data_quality_failed",
                    "manifest.universe.quality",
                    "Universe data quality must be clean",
                )
            )
        risks = set(evidence.get("research_risk_warnings", []))
        unaccepted = sorted(risks - accepted_risks)
        if unaccepted:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "unaccepted_research_risk",
                    "hypothesis.risk_acceptance",
                    "All declared research risks must be preregistered",
                    actual=unaccepted,
                )
            )
        blockers.extend(
            ResearchWorkflowService._benchmark_gate_blockers(evidence)
        )
        blockers.extend(
            ResearchWorkflowService._execution_gate_blockers(
                evidence.get("execution", {})
            )
        )
        return blockers

    @staticmethod
    def _benchmark_gate_blockers(
        evidence: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        benchmark = evidence.get("benchmark")
        if not isinstance(benchmark, Mapping):
            return [
                ResearchWorkflowService._blocker(
                    "benchmark_evidence_missing",
                    "manifest.benchmark",
                    "Verified benchmark evidence is mandatory",
                )
            ]
        if benchmark.get("available") is not True:
            return [
                ResearchWorkflowService._blocker(
                    "benchmark_unavailable",
                    "manifest.benchmark.available",
                    "The benchmark must be available for the full test window",
                    expected=True,
                    actual=benchmark.get("available"),
                )
            ]

        blockers: list[dict[str, Any]] = []
        code = benchmark.get("code")
        if not (
            isinstance(code, str)
            and len(code) == 6
            and code.isascii()
            and code.isdigit()
        ):
            blockers.append(
                ResearchWorkflowService._blocker(
                    "benchmark_identity_invalid",
                    "manifest.benchmark.code",
                    "Benchmark provenance requires a six-digit index code",
                    actual=code,
                )
            )

        digest = benchmark.get("sha256")
        if not ResearchWorkflowService._is_sha256(digest):
            blockers.append(
                ResearchWorkflowService._blocker(
                    "benchmark_hash_invalid",
                    "manifest.benchmark.sha256",
                    "Benchmark content must have a valid SHA-256 digest",
                    actual=digest,
                )
            )

        snapshot = benchmark.get("snapshot")
        if not isinstance(snapshot, Mapping):
            blockers.append(
                ResearchWorkflowService._blocker(
                    "benchmark_snapshot_missing",
                    "manifest.benchmark.snapshot",
                    "An immutable benchmark snapshot is required",
                )
            )
        else:
            key = snapshot.get("key")
            file_hash = snapshot.get("file_sha256")
            expected_relative_key = (
                f"benchmark/{key}.parquet"
                if ResearchWorkflowService._is_sha256(key)
                else None
            )
            try:
                size_bytes = int(snapshot["size_bytes"])
            except (KeyError, TypeError, ValueError):
                size_bytes = 0
            snapshot_valid = (
                snapshot.get("schema_version")
                == "research-data-snapshot/v1"
                and snapshot.get("kind") == "benchmark"
                and ResearchWorkflowService._is_sha256(key)
                and file_hash == key
                and snapshot.get("relative_key") == expected_relative_key
                and snapshot.get("format") == "parquet"
                and size_bytes > 0
                and isinstance(snapshot.get("schema"), Mapping)
                and isinstance(snapshot.get("series"), Mapping)
            )
            if not snapshot_valid:
                blockers.append(
                    ResearchWorkflowService._blocker(
                        "benchmark_snapshot_integrity_failed",
                        "manifest.benchmark.snapshot",
                        "Benchmark snapshot provenance is incomplete or inconsistent",
                    )
                )

        windows = evidence.get("windows")
        try:
            test_start = date.fromisoformat(str(windows["test_start"]))
            test_end = date.fromisoformat(str(windows["test_end"]))
            fetch_start = date.fromisoformat(str(benchmark["fetch_start"]))
            fetch_end = date.fromisoformat(str(benchmark["fetch_end"]))
            windows_valid = (
                fetch_start < test_start <= test_end
                and fetch_end == test_end
            )
        except (KeyError, TypeError, ValueError):
            windows_valid = False
        if not windows_valid:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "benchmark_window_misaligned",
                    "manifest.benchmark.fetch_start",
                    (
                        "Benchmark data must begin before the test window "
                        "and end exactly at the locked test end"
                    ),
                    expected={
                        "fetch_start": "before windows.test_start",
                        "fetch_end": "equal to windows.test_end",
                    },
                    actual={
                        "fetch_start": benchmark.get("fetch_start"),
                        "fetch_end": benchmark.get("fetch_end"),
                        "test_start": (
                            windows.get("test_start")
                            if isinstance(windows, Mapping)
                            else None
                        ),
                        "test_end": (
                            windows.get("test_end")
                            if isinstance(windows, Mapping)
                            else None
                        ),
                    },
                )
            )
        return blockers

    @staticmethod
    def _is_sha256(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        return all(character in "0123456789abcdef" for character in value)

    @staticmethod
    def _execution_gate_blockers(
        execution: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if not isinstance(execution, Mapping):
            return [
                ResearchWorkflowService._blocker(
                    "execution_risk_gate_failed",
                    "manifest.execution",
                    "Execution assumptions are missing",
                )
            ]

        try:
            costs = execution["cost_model"]
            constraints = execution["execution_constraints"]
            structural_valid = (
                isinstance(costs, Mapping)
                and isinstance(constraints, Mapping)
                and math.isfinite(float(execution["initial_capital"]))
                and float(execution["initial_capital"]) > 0
                and int(execution["max_positions"]) > 0
                and int(constraints["lot_size"]) > 0
                and execution["rebalance_mode"]
                in {"signal_driven", "monthly_liquidate_compat"}
                and execution["portfolio_signal_mode"]
                in {"event_orders", "target_weights"}
                and execution["signal_timing"]
                == "signal_on_T_fill_next_session_open"
            )
        except (KeyError, TypeError, ValueError):
            costs = (
                execution.get("cost_model")
                if isinstance(execution.get("cost_model"), Mapping)
                else {}
            )
            constraints = (
                execution.get("execution_constraints")
                if isinstance(
                    execution.get("execution_constraints"),
                    Mapping,
                )
                else {}
            )
            structural_valid = False
        if not structural_valid:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "execution_risk_gate_failed",
                    "manifest.execution",
                    (
                        "Execution capital, lot size, position limit and signal "
                        "timing must be complete"
                    ),
                )
            )

        cost_fields = (
            (
                "commission_rate",
                "execution_commission_required",
                "Commission rate",
            ),
            (
                "slippage_rate",
                "execution_slippage_required",
                "Slippage rate",
            ),
            (
                "stamp_duty_rate",
                "execution_stamp_duty_required",
                "Stamp-duty rate",
            ),
            (
                "min_commission",
                "execution_minimum_commission_required",
                "Minimum commission",
            ),
        )
        for field, code, label in cost_fields:
            raw_value = costs.get(field)
            try:
                value = float(raw_value)
                valid_cost = math.isfinite(value) and value > 0
                if field != "min_commission":
                    valid_cost = valid_cost and value <= 1
            except (TypeError, ValueError):
                valid_cost = False
            if not valid_cost:
                blockers.append(
                    ResearchWorkflowService._blocker(
                        code,
                        f"manifest.execution.cost_model.{field}",
                        f"{label} must be explicitly set to a finite positive value",
                        expected="0 < value <= 1" if field != "min_commission" else "> 0",
                        actual=raw_value,
                    )
                )

        participation = constraints.get("volume_participation")
        if participation is None:
            blockers.append(
                ResearchWorkflowService._blocker(
                    "execution_volume_participation_required",
                    (
                        "manifest.execution.execution_constraints."
                        "volume_participation"
                    ),
                    "A conservative volume-participation limit is mandatory",
                    expected="0 < value <= 0.2",
                    actual=participation,
                )
            )
        else:
            try:
                participation_value = float(participation)
                participation_valid = (
                    math.isfinite(participation_value)
                    and 0 < participation_value <= 0.2
                )
            except (TypeError, ValueError):
                participation_valid = False
            if not participation_valid:
                blockers.append(
                    ResearchWorkflowService._blocker(
                        "execution_volume_participation_invalid",
                        (
                            "manifest.execution.execution_constraints."
                            "volume_participation"
                        ),
                        "Volume participation must be within the conservative limit",
                        expected="0 < value <= 0.2",
                        actual=participation,
                    )
                )
        return blockers

    @staticmethod
    def _metric_blockers(
        preregistered: list[Mapping[str, Any]],
        actual: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        comparisons = {
            "gt": lambda left, right: left > right,
            "gte": lambda left, right: left >= right,
            "lt": lambda left, right: left < right,
            "lte": lambda left, right: left <= right,
        }
        for metric in preregistered:
            name = str(metric.get("name"))
            value = actual.get(name)
            operator = str(metric.get("operator"))
            threshold = metric.get("threshold")
            try:
                passed = comparisons[operator](
                    float(value),
                    float(threshold),
                )
            except (KeyError, TypeError, ValueError):
                passed = False
            if not passed:
                blockers.append(
                    ResearchWorkflowService._blocker(
                        "preregistered_metric_failed",
                        f"report.metrics.{name}",
                        f"Preregistered metric {name} did not pass",
                        expected={
                            "operator": operator,
                            "threshold": threshold,
                        },
                        actual=value,
                    )
                )
        return blockers

    @staticmethod
    def _blocker(
        code: str,
        field: str,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
    ) -> dict[str, Any]:
        result = {
            "code": code,
            "field": field,
            "message": message,
        }
        if expected is not None:
            result["expected"] = expected
        if actual is not None:
            result["actual"] = actual
        return result

    async def get_promotion(
        self,
        promotion_id: int,
        user: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection = await self._connect()
        try:
            return self._promotion(
                await self._owned(
                    connection,
                    "research_promotions",
                    promotion_id,
                    user,
                )
            )
        finally:
            await connection.close()

    async def verify_deployment_binding(
        self,
        promotion_id: int,
        *,
        owner_user_id: int,
        experiment_id: int,
        strategy_id: str,
        params_hash: str,
        model_artifact_id: int | None,
    ) -> dict[str, Any]:
        """Resolve immutable evidence for an executable deployment.

        This is intentionally stricter than the read endpoint: deployment
        execution never inherits administrator visibility and must match the
        promotion owner exactly.
        """
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                SELECT * FROM research_promotions
                WHERE id=? AND user_id=?
                """,
                (promotion_id, owner_user_id),
            )
            promotion = await cursor.fetchone()
            if promotion is None:
                raise WorkflowError(
                    404,
                    "promotion_binding_not_found",
                    "Approved research promotion was not found for this owner",
                )
            if promotion["status"] != "approved":
                raise WorkflowError(
                    409,
                    "promotion_not_approved",
                    "Research promotion is not approved or was revoked",
                )

            cursor = await connection.execute(
                """
                SELECT * FROM research_reports
                WHERE id=? AND group_id=? AND user_id=?
                """,
                (
                    promotion["report_id"],
                    promotion["group_id"],
                    owner_user_id,
                ),
            )
            report_row = await cursor.fetchone()
            if report_row is None:
                raise WorkflowError(
                    409,
                    "promotion_report_missing",
                    "The promotion's immutable final report is missing",
                )
            report = self._report(report_row)
            snapshot = report["snapshot"]
            trials = snapshot.get("trials", [])
            if (
                report["report_type"] != "final"
                or snapshot.get("evidence_scope")
                != "strict_locked_test_final"
                or len(trials) != 1
                or trials[0].get("role") != "locked_test"
            ):
                raise WorkflowError(
                    409,
                    "promotion_trial_binding_invalid",
                    "Promotion is not bound to one strict locked-test trial",
                )
            trial = trials[0]
            try:
                trial_experiment_id = int(trial["experiment_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise WorkflowError(
                    409,
                    "promotion_trial_binding_invalid",
                    "Promotion locked-test experiment identity is invalid",
                ) from exc
            if trial_experiment_id != int(experiment_id):
                raise WorkflowError(
                    409,
                    "promotion_experiment_mismatch",
                    "Deployment experiment does not match the promoted trial",
                )

            cursor = await connection.execute(
                """
                SELECT * FROM experiments
                WHERE id=? AND user_id=?
                """,
                (experiment_id, owner_user_id),
            )
            experiment = await cursor.fetchone()
            if experiment is None:
                raise WorkflowError(
                    404,
                    "promotion_binding_not_found",
                    "Promoted experiment was not found for this owner",
                )
            if (
                experiment["status"] != "completed"
                or experiment["strategy_id"] != strategy_id
                or experiment["params_hash"] != params_hash
            ):
                raise WorkflowError(
                    409,
                    "promotion_experiment_identity_changed",
                    "Promoted experiment identity no longer matches deployment",
                )

            cursor = await connection.execute(
                """
                SELECT * FROM research_run_manifests
                WHERE experiment_id=?
                """,
                (experiment_id,),
            )
            manifest_rows = await cursor.fetchall()
            if len(manifest_rows) != 1:
                raise WorkflowError(
                    409,
                    "promotion_manifest_invalid",
                    "Exactly one immutable RunManifest is required",
                )
            manifest_row = manifest_rows[0]
            manifest = _decode_json(manifest_row["manifest_json"], {})
            manifest_hash = canonical_sha256(manifest)
            report_manifest_hash = (trial.get("manifest") or {}).get(
                "manifest_hash"
            )
            if (
                manifest_row["schema_version"] != RUN_MANIFEST_SCHEMA
                or manifest_hash != manifest_row["manifest_hash"]
                or manifest_hash != report_manifest_hash
            ):
                raise WorkflowError(
                    409,
                    "promotion_manifest_changed",
                    "RunManifest no longer matches the promoted final report",
                )

            blockers = await self._promotion_blockers(connection, promotion)
            if blockers:
                raise WorkflowError(
                    409,
                    "promotion_evidence_invalid",
                    "Approved promotion no longer passes its research gates",
                    blockers=blockers,
                )

            model_identity: dict[str, Any] = {
                "model_artifact_id": None,
                "model_sha256": None,
                "model_evidence_hash": None,
            }
            if bool(int(experiment["requires_training"])):
                try:
                    evidence = (
                        await verify_experiment_model_promotion_evidence(
                            connection,
                            experiment,
                        )
                    )
                except MLPromotionEvidenceError as exc:
                    raise WorkflowError(
                        409,
                        "promotion_model_evidence_changed",
                        "Promoted model evidence no longer verifies",
                        blockers=[
                            self._blocker(
                                exc.code,
                                exc.field,
                                exc.message,
                                expected=exc.expected,
                                actual=exc.actual,
                            )
                        ],
                    ) from exc
                except Exception as exc:
                    raise WorkflowError(
                        409,
                        "promotion_model_evidence_changed",
                        "Promoted model evidence verification failed closed",
                        blockers=[
                            self._blocker(
                                "ml_evidence_verification_failed",
                                "experiment.model_artifacts",
                                "Training evidence verification failed closed",
                                actual=type(exc).__name__,
                            )
                        ],
                    ) from exc
                cursor = await connection.execute(
                    """
                    SELECT id FROM model_artifacts
                    WHERE experiment_id=? AND is_latest=1
                    ORDER BY id
                    """,
                    (experiment_id,),
                )
                artifacts = await cursor.fetchall()
                if len(artifacts) != 1:
                    raise WorkflowError(
                        409,
                        "promotion_model_identity_invalid",
                        "Exactly one promoted model artifact is required",
                    )
                promoted_artifact_id = int(artifacts[0]["id"])
                if (
                    model_artifact_id is None
                    or promoted_artifact_id != int(model_artifact_id)
                ):
                    raise WorkflowError(
                        409,
                        "promotion_model_mismatch",
                        "Deployment model does not match promoted evidence",
                    )
                model_identity = {
                    "model_artifact_id": promoted_artifact_id,
                    "model_sha256": evidence["model_sha256"],
                    "model_evidence_hash": canonical_sha256(evidence),
                }
            elif model_artifact_id is not None:
                raise WorkflowError(
                    409,
                    "unexpected_promotion_model",
                    "Non-training promotion cannot bind a model artifact",
                )

            return {
                "schema_version": "research-promotion-binding/v1",
                "promotion_id": int(promotion["id"]),
                "promotion_version": int(promotion["version"]),
                "report_id": int(report["id"]),
                "report_hash": str(report["snapshot_hash"]),
                "experiment_id": int(experiment_id),
                "manifest_hash": manifest_hash,
                **model_identity,
            }
        finally:
            await connection.close()

    async def list_events(
        self,
        *,
        entity_type: str,
        entity_id: int,
        user: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                SELECT * FROM research_workflow_events
                WHERE entity_type=? AND entity_id=?
                ORDER BY id
                """,
                (entity_type, entity_id),
            )
            rows = await cursor.fetchall()
            if not rows:
                raise WorkflowError(404, "not_found", "Audit events not found")
            if not _is_admin(user) and any(
                int(row["owner_user_id"]) != int(user["id"]) for row in rows
            ):
                raise WorkflowError(404, "not_found", "Audit events not found")
            return [
                _row_dict(row, json_fields=("payload_json",))
                for row in rows
            ]
        finally:
            await connection.close()
