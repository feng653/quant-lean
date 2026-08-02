"""Bounded repair of terminal backtest jobs and stale experiment state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

RECONCILIATION_EVENT_STAGE = "experiment_state_reconciled"
DEFAULT_RECONCILIATION_LIMIT = 100
MAX_RECONCILIATION_LIMIT = 500

_ACTIVE_EXPERIMENT_STATUSES = ("pending", "running", "cancel_requested")
_REPAIRABLE_JOB_STATUSES = ("failed", "cancelled")


@dataclass(frozen=True)
class ReconciliationResult:
    """Summary of one bounded reconciliation pass."""

    scanned: int
    repaired: int


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})")
    }


def record_reconciliation_event(
    conn: sqlite3.Connection,
    *,
    job_uuid: str,
    status: str,
    previous_experiment_status: str,
    source: str,
) -> None:
    """Write one non-sensitive, idempotent audit event for a state repair."""
    if not _table_exists(conn, "job_events"):
        return
    conn.execute(
        """
        INSERT INTO job_events
            (job_uuid, status, progress, stage, message)
        SELECT ?, ?, 1.0, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM job_events
            WHERE job_uuid=? AND stage=?
        )
        """,
        (
            job_uuid,
            status,
            RECONCILIATION_EVENT_STAGE,
            (
                "实验状态已根据终态任务自动协调"
                f"（来源: {source}，原状态: {previous_experiment_status}）"
            ),
            job_uuid,
            RECONCILIATION_EVENT_STAGE,
        ),
    )


def _refresh_related_sweeps(
    conn: sqlite3.Connection,
    *,
    experiment_id: int,
    user_id: int,
) -> None:
    """Refresh sweep accounting without crossing an ownership boundary."""
    sweep_columns = _table_columns(conn, "param_sweeps")
    if (
        not {
            "id",
            "user_id",
            "total_experiments",
            "completed_experiments",
            "status",
        }
        <= sweep_columns
        or not _table_exists(conn, "sweep_experiments")
    ):
        return
    conn.execute(
        """
        UPDATE param_sweeps
        SET completed_experiments = (
                SELECT COUNT(*) FROM sweep_experiments se
                JOIN experiments e ON e.id = se.experiment_id
                WHERE se.sweep_id = param_sweeps.id
                  AND e.user_id = param_sweeps.user_id
                  AND e.status IN ('completed', 'failed', 'cancelled')
            ),
            status = CASE
                WHEN total_experiments <= (
                    SELECT COUNT(*) FROM sweep_experiments se
                    JOIN experiments e ON e.id = se.experiment_id
                    WHERE se.sweep_id = param_sweeps.id
                      AND e.user_id = param_sweeps.user_id
                      AND e.status IN ('completed', 'failed', 'cancelled')
                ) THEN 'completed'
                ELSE 'running'
            END
        WHERE user_id=?
          AND id IN (
              SELECT sweep_id FROM sweep_experiments WHERE experiment_id=?
          )
        """,
        (user_id, experiment_id),
    )


def reconcile_terminal_backtest_state(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_RECONCILIATION_LIMIT,
    source: str = "maintenance",
) -> ReconciliationResult:
    """Repair active experiments whose latest owned backtest job is terminal.

    The caller owns the surrounding write transaction. Selection is deliberately
    fail-closed: legacy rows without both ownership and explicit resource
    metadata are not inferred from free-form JSON.
    """
    job_columns = _table_columns(conn, "jobs")
    experiment_columns = _table_columns(conn, "experiments")
    required_job_columns = {
        "id",
        "job_uuid",
        "job_type",
        "resource_type",
        "resource_id",
        "status",
        "user_id",
    }
    required_experiment_columns = {"id", "user_id", "status"}
    if (
        not required_job_columns <= job_columns
        or not required_experiment_columns <= experiment_columns
        or not _table_exists(conn, "job_events")
    ):
        return ReconciliationResult(scanned=0, repaired=0)

    bounded_limit = min(max(int(limit), 1), MAX_RECONCILIATION_LIMIT)
    rows = conn.execute(
        f"""
        SELECT
            j.id AS job_id,
            j.job_uuid,
            j.status AS job_status,
            j.user_id,
            j.resource_id,
            e.id AS experiment_id,
            e.status AS experiment_status
        FROM jobs j
        JOIN experiments e
          ON CAST(e.id AS TEXT)=j.resource_id
         AND e.user_id=j.user_id
        WHERE j.job_type='backtest'
          AND j.resource_type='experiment'
          AND j.status IN ({",".join("?" for _ in _REPAIRABLE_JOB_STATUSES)})
          AND e.status IN ({",".join("?" for _ in _ACTIVE_EXPERIMENT_STATUSES)})
          AND NOT EXISTS (
              SELECT 1
              FROM jobs newer
              WHERE newer.job_type='backtest'
                AND newer.resource_type='experiment'
                AND newer.resource_id=j.resource_id
                AND newer.user_id=j.user_id
                AND newer.id > j.id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM jobs completed
              WHERE completed.job_type='backtest'
                AND completed.resource_type='experiment'
                AND completed.resource_id=j.resource_id
                AND completed.user_id=j.user_id
                AND completed.status='completed'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM job_events event
              WHERE event.job_uuid=j.job_uuid
                AND event.stage=?
          )
        ORDER BY j.id ASC
        LIMIT ?
        """,
        (
            *_REPAIRABLE_JOB_STATUSES,
            *_ACTIVE_EXPERIMENT_STATUSES,
            RECONCILIATION_EVENT_STAGE,
            bounded_limit,
        ),
    ).fetchall()

    repaired = 0
    for row in rows:
        job_status = str(row["job_status"])
        previous_status = str(row["experiment_status"])
        message = (
            "后台任务失败，状态已自动协调"
            if job_status == "failed"
            else "后台任务已取消，状态已自动协调"
        )
        updates = ["status=?", "progress_message=?"]
        values: list[object] = [job_status, message]
        if "progress_pct" in experiment_columns:
            updates.append("progress_pct=100")
        if "completed_at" in experiment_columns:
            updates.append("completed_at=COALESCE(completed_at, datetime('now'))")
        if "error_log" in experiment_columns:
            updates.append("error_log=?")
            values.append(
                "后台任务执行失败；详细信息请查看任务中心的脱敏审计记录"
                if job_status == "failed"
                else None
            )
        values.extend(
            [
                int(row["experiment_id"]),
                int(row["user_id"]),
                previous_status,
            ]
        )
        cursor = conn.execute(
            f"""
            UPDATE experiments
            SET {", ".join(updates)}
            WHERE id=? AND user_id=? AND status=?
              AND status!='completed'
            """,
            values,
        )
        if cursor.rowcount != 1:
            continue
        repaired += 1
        record_reconciliation_event(
            conn,
            job_uuid=str(row["job_uuid"]),
            status=job_status,
            previous_experiment_status=previous_status,
            source=source,
        )
        _refresh_related_sweeps(
            conn,
            experiment_id=int(row["experiment_id"]),
            user_id=int(row["user_id"]),
        )

    return ReconciliationResult(scanned=len(rows), repaired=repaired)
