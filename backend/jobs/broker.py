"""Durable background-job queue backed by SQLite."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from backend.config import settings
from backend.core.security_boundaries import (
    UnsafePayloadError,
    canonical_json_for_storage,
    sanitize_diagnostic,
    sanitize_public_payload,
)
from backend.jobs.alert_delivery import (
    acknowledge_alert_delivery,
    alert_delivery_summary,
    initialize_alert_delivery_schema,
    process_alert_delivery_outbox,
    queue_slo_alert_delivery,
)
from backend.jobs.observability import structured_log
from backend.jobs.state_reconciler import (
    DEFAULT_RECONCILIATION_LIMIT,
    reconcile_terminal_backtest_state,
    record_reconciliation_event,
)
from backend.version import runtime_code_evidence

logger = logging.getLogger("quant_platform.jobs")

_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS = 0.25
_PROGRESS_BUSY_RETRY_ATTEMPTS = 5
_PROGRESS_BUSY_RETRY_BASE_SECONDS = 0.05
_DEFAULT_EXPIRED_CLAIM_RECOVERY_LIMIT = 100
_MAX_EXPIRED_CLAIM_RECOVERY_LIMIT = 500
_EXPIRED_CLAIM_HEALTH_SAMPLE_LIMIT = 5
_LEASE_RECOVERY_EVENT_STAGE = "lease_expired_recovered"

VALID_STATUSES = {
    "pending",
    "running",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_OPERATIONAL_EVENT_NAMES = {
    "service_start",
    "service_stop",
    "scheduler_restart",
    "sqlite_contention",
    "websocket_connected",
    "websocket_disconnected",
    "job_terminal",
    "data_refresh_progress",
    "cache_quality",
}
_OPERATIONAL_CATEGORIES = {
    "service",
    "scheduler",
    "storage",
    "websocket",
    "job",
    "data",
    "cache",
}
_SLO_OBJECTIVE_NAMES = {
    "job_success_rate",
    "queue_wait_p95_seconds",
    "sqlite_contention_events",
    "service_starts",
}
_SLO_STATES = {"healthy", "breaching"}
_SLO_TRANSITIONS = {"breach", "recovery"}
_BOUNDED_STAGES = {
    "queued",
    "starting",
    "loading_data",
    "updating_data",
    "fetching",
    "market_data_primary",
    "market_data_reference",
    "market_data_validation",
    "market_data_execution_binding",
    "provider_collection",
    "research_import",
    "research_import_prepare",
    "research_import_spool",
    "research_import_verify_existing",
    "research_import_write",
    "research_import_write_market",
    "research_import_integrity",
    "research_import_binding",
    "research_import_hash",
    "research_import_activate",
    "pit_governance_collection",
    "validating",
    "persisting",
    "training",
    "backtesting",
    "simulation_backfill",
    "jobs",
    "training",
    "realtime",
    "notifications",
    "completed",
    "failed",
    "cancelled",
    "other",
}

JOB_TYPE_METADATA: dict[str, tuple[str, str | None, str | None]] = {
    "backtest": ("回测实验", "experiment", "experiment_id"),
    "daily_simulation": ("模拟盘日结", "portfolio", "portfolio_id"),
    "simulation_backfill": ("模拟盘历史回放", "portfolio", "portfolio_id"),
    "data_update": ("行情数据更新", "data_pool", "pool_id"),
    "pit_governance_refresh": ("PIT 治理证据刷新", "pit_governance", "pool_id"),
    "research_data_refresh": (
        "研究数据刷新",
        "research_data_source",
        "source_id",
    ),
    "pit_durable_update": ("PIT 自动更新", "pit_automation", "idempotency_key"),
    "candidate_data_preflight": (
        "候选数据预检",
        "provider_candidate_cycle",
        "idempotency_key",
    ),
    "retrain": ("模型重训练", "deployment", "deployment_id"),
    "factor_research": ("因子研究", "factor_research", "factor_id"),
}
EXCLUSIVE_JOB_TYPES = {"simulation_backfill", "retrain", "factor_research"}
CACHE_READER_JOB_TYPES = {"backtest", "daily_simulation"}
POOL_ALIASES = {
    "hs300": "csi300",
    "zz500": "csi500",
    "zz800": "csi800",
    "zz1000": "csi1000",
}
_EXECUTION_CLAIM: contextvars.ContextVar[tuple[str, str, int] | None] = (
    contextvars.ContextVar("job_execution_claim", default=None)
)


class JobCancelledError(RuntimeError):
    """Raised at a cooperative cancellation checkpoint."""


class JobQueueFullError(RuntimeError):
    """Raised when non-critical work is rejected by queue backpressure."""


class JobLeaseLostError(RuntimeError):
    """Raised when an obsolete worker attempts to update a reclaimed job."""


def _is_sqlite_busy(error: BaseException) -> bool:
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    busy_codes = {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }
    if code in busy_codes:
        return True
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_priority(job_type: str, params: dict[str, Any]) -> int:
    if job_type == "daily_simulation":
        return 100
    if job_type == "data_update":
        # A scheduled refresh is an explicit dependency of paper simulation.
        return 110 if params.get("source") == "paper_scheduler" else 80
    if job_type == "pit_governance_refresh":
        return 70
    if job_type == "research_data_refresh":
        return 65
    if job_type == "pit_durable_update":
        return 75
    if job_type == "candidate_data_preflight":
        return 70
    if job_type == "backtest":
        return 40 if params.get("sweep_id") is not None else 60
    if job_type == "simulation_backfill":
        return 30
    if job_type == "retrain":
        return 20
    if job_type == "factor_research":
        return 30
    return 10


def _queue_group(job_type: str, params: dict[str, Any], job_uuid: str) -> str:
    if job_type == "backtest" and params.get("sweep_id") is not None:
        return f"sweep:{params['sweep_id']}"
    return f"job:{job_uuid}"


def _effective_priority_sql(alias: str) -> str:
    """Age non-critical work up to 99 while preserving critical priority."""
    quantum = max(int(settings.JOB_SCHEDULER_AGING_SECONDS), 1)
    base = f"COALESCE({alias}.priority, 10)"
    age_seconds = (
        "(julianday('now') - "
        f"COALESCE(julianday({alias}.created_at), julianday('now'))) * 86400"
    )
    return (
        f"CASE WHEN {base} >= 100 THEN {base} "
        f"ELSE MIN(99, {base} + "
        f"CAST(MAX({age_seconds}, 0) / {quantum} AS INTEGER)) END"
    )


def _data_resource(params: dict[str, Any]) -> str | None:
    pool_id = params.get("pool_id")
    if pool_id is None:
        return "*"
    normalized = str(pool_id).strip().lower()
    if normalized in ("", "all"):
        return "*"
    return POOL_ALIASES.get(normalized, normalized)


def _load_windows_kernel32() -> Any:
    """Return a typed subset of the Windows process-query API."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    return kernel32


def _windows_process_alive(pid: int) -> bool:
    """Query a Windows process without sending it a signal.

    Python implements ``os.kill`` on Windows with ``TerminateProcess`` for
    ordinary signal values.  Consequently the POSIX ``os.kill(pid, 0)`` probe
    is destructive there, including when a scheduler checks its own PID.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    error_invalid_parameter = 87
    still_active = 259

    kernel32 = _load_windows_kernel32()
    process = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not process:
        error = int(kernel32.GetLastError())
        if error == error_invalid_parameter:
            return False
        # Access denial proves that the PID is protected but present. Unknown
        # query failures are also conservative so a transient OS error cannot
        # cause two schedulers to execute the same work.
        if error != error_access_denied:
            logger.debug("Windows PID %s query failed with error %s", pid, error)
        return True

    exit_code = wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(process)


def _process_alive(pid: int, platform_name: str | None = None) -> bool:
    """Best-effort local PID check; permission denial means conservatively alive."""
    if pid <= 0:
        return False
    if (platform_name or sys.platform) == "win32":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_start_identity(pid: int) -> str | None:
    """Best-effort PID-reuse fence for the local scheduler lease."""
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        if proc_stat.is_file():
            raw = proc_stat.read_text(encoding="utf-8")
            # Field 2 (comm) is parenthesised and may itself contain spaces.
            fields_after_comm = raw[raw.rfind(")") + 2 :].split()
            return f"linux:{fields_after_comm[19]}"
        if sys.platform == "darwin":
            import ctypes

            class _ProcBsdInfo(ctypes.Structure):
                _fields_ = [
                    ("flags", ctypes.c_uint32),
                    ("status", ctypes.c_uint32),
                    ("xstatus", ctypes.c_uint32),
                    ("pid", ctypes.c_uint32),
                    ("ppid", ctypes.c_uint32),
                    ("uid", ctypes.c_uint32),
                    ("gid", ctypes.c_uint32),
                    ("ruid", ctypes.c_uint32),
                    ("rgid", ctypes.c_uint32),
                    ("svuid", ctypes.c_uint32),
                    ("svgid", ctypes.c_uint32),
                    ("reserved", ctypes.c_uint32),
                    ("comm", ctypes.c_char * 16),
                    ("name", ctypes.c_char * 32),
                    ("nfiles", ctypes.c_uint32),
                    ("pgid", ctypes.c_uint32),
                    ("pjobc", ctypes.c_uint32),
                    ("e_tdev", ctypes.c_uint32),
                    ("e_tpgid", ctypes.c_uint32),
                    ("nice", ctypes.c_int32),
                    ("start_seconds", ctypes.c_uint64),
                    ("start_microseconds", ctypes.c_uint64),
                ]

            info = _ProcBsdInfo()
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            copied = libproc.proc_pidinfo(
                pid,
                3,  # PROC_PIDTBSDINFO
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if copied != ctypes.sizeof(info):
                return None
            return f"darwin:{info.start_seconds}:{info.start_microseconds}"
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            process = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                0x1000, False, pid
            )
            if not process:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
                    process,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                value = (int(creation.dwHighDateTime) << 32) | int(
                    creation.dwLowDateTime
                )
                return f"windows:{value}"
            finally:
                ctypes.windll.kernel32.CloseHandle(process)  # type: ignore[attr-defined]
        output = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        return f"ps:{' '.join(output.split())}" if output else None
    except (AttributeError, IndexError, OSError, ValueError, subprocess.SubprocessError):
        return None


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
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _refresh_related_sweeps(
    conn: sqlite3.Connection, experiment_id: str
) -> None:
    sweep_columns = _table_columns(conn, "param_sweeps")
    if (
        not {"id", "total_experiments", "completed_experiments", "status"}
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
                  AND e.status IN ('completed', 'failed', 'cancelled')
            ),
            status = CASE
                WHEN total_experiments <= (
                    SELECT COUNT(*) FROM sweep_experiments se
                    JOIN experiments e ON e.id = se.experiment_id
                    WHERE se.sweep_id = param_sweeps.id
                      AND e.status IN ('completed', 'failed', 'cancelled')
                ) THEN 'completed'
                ELSE 'running'
            END
        WHERE id IN (
            SELECT sweep_id FROM sweep_experiments WHERE experiment_id = ?
        )
        """,
        (experiment_id,),
    )


def _reconcile_backtest_experiment(
    conn: sqlite3.Connection,
    job: sqlite3.Row | dict[str, Any],
    *,
    status: str,
    message: str,
) -> None:
    """Keep a backtest job, experiment and parent sweep in one transaction."""
    record = dict(job)
    if (
        record.get("job_type") != "backtest"
        or record.get("resource_type") != "experiment"
        or record.get("resource_id") is None
    ):
        return
    columns = _table_columns(conn, "experiments")
    if not {"id", "status"} <= columns:
        return
    updates = ["status=?"]
    values: list[Any] = [status]
    optional_values = {
        "progress_message": message,
        "progress_pct": 0 if status == "pending" else 100,
        "completed_at": None if status == "pending" else _utc_now(),
        "error_log": None,
    }
    for column, value in optional_values.items():
        if column in columns:
            updates.append(f"{column}=?")
            values.append(value)
    previous = conn.execute(
        "SELECT status FROM experiments WHERE CAST(id AS TEXT)=?",
        (str(record["resource_id"]),),
    ).fetchone()
    values.append(str(record["resource_id"]))
    cursor = conn.execute(
        f"""
        UPDATE experiments
        SET {", ".join(updates)}
        WHERE CAST(id AS TEXT)=? AND status!='completed'
        """,
        values,
    )
    state_changed = (
        cursor.rowcount == 1
        and previous is not None
        and previous["status"] != status
    )
    if (
        state_changed
        and status in ("failed", "cancelled")
        and record.get("job_uuid")
    ):
        record_reconciliation_event(
            conn,
            job_uuid=str(record["job_uuid"]),
            status=status,
            previous_experiment_status=str(previous["status"]),
            source="runtime",
        )
    if state_changed:
        _refresh_related_sweeps(conn, str(record["resource_id"]))


def _hydrate_dispatch_resource(
    conn: sqlite3.Connection, job: dict[str, Any]
) -> dict[str, Any]:
    """Attach an internal cache resource key used only during dispatch."""
    job_type = str(job.get("job_type"))
    raw_params = job.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    job["params"] = params
    if job_type in EXCLUSIVE_JOB_TYPES:
        job["_dispatch_heavy"] = True
        job["_dispatch_exclusive"] = True
        job["_dispatch_slots"] = 2
        return job
    if job_type in {
        "data_update",
        "pit_governance_refresh",
        "pit_durable_update",
        "candidate_data_preflight",
        "research_data_refresh",
    }:
        job["_dispatch_heavy"] = True
        job["_dispatch_pool"] = _data_resource(params)
        job["_dispatch_slots"] = 1
        return job
    if job_type == "daily_simulation":
        job["_dispatch_heavy"] = False
        # A simulation may read several deployment pools. Until its complete
        # cache set is materialised into the job payload, treat it as a global
        # reader so a manual run cannot overlap an unrelated cache refresh.
        job["_dispatch_pool"] = "*"
        job["_dispatch_slots"] = 1
        return job
    if job_type != "backtest":
        job["_dispatch_heavy"] = False
        job["_dispatch_slots"] = 1
        return job
    pool = params.get("pool_preset")
    custom_codes = params.get("pool_custom_codes")
    requires_training = params.get("requires_training")
    experiment_id = params.get("experiment_id") or job.get("resource_id")
    if experiment_id is not None:
        experiment_columns = _table_columns(conn, "experiments")
    else:
        experiment_columns = set()
    selected_columns: list[str] = []
    if "pool_preset" in experiment_columns and (pool is None or pool == ""):
        selected_columns.append("pool_preset")
        if "pool_custom_codes" in experiment_columns:
            selected_columns.append("pool_custom_codes")
    if "requires_training" in experiment_columns and requires_training is None:
        selected_columns.append("requires_training")
    if selected_columns:
        row = conn.execute(
            f"SELECT {', '.join(selected_columns)} FROM experiments "
            "WHERE CAST(id AS TEXT)=?",
            (str(experiment_id),),
        ).fetchone()
        if row is not None:
            if "pool_preset" in selected_columns:
                pool = row["pool_preset"]
            if "pool_custom_codes" in selected_columns:
                custom_codes = row["pool_custom_codes"]
        if row is not None and "requires_training" in selected_columns:
            requires_training = bool(row["requires_training"])
    if pool in (None, ""):
        pool = "csi300"
    custom_code_count = 0
    if str(pool) == "custom" and custom_codes:
        import hashlib

        parsed_codes = custom_codes
        if isinstance(custom_codes, str):
            try:
                parsed_codes = json.loads(custom_codes)
            except json.JSONDecodeError:
                parsed_codes = custom_codes.split(",")
            if isinstance(parsed_codes, str):
                parsed_codes = parsed_codes.split(",")
        if not isinstance(parsed_codes, list):
            parsed_codes = [parsed_codes]
        canonical_items = sorted(
            {
                str(code).strip()
                for code in parsed_codes
                if str(code).strip()
            }
        )
        custom_code_count = len(canonical_items)
        canonical_codes = ",".join(canonical_items)
        digest = hashlib.sha256(canonical_codes.encode("utf-8")).hexdigest()[:16]
        pool = f"custom_{digest}"
    normalized_pool = str(pool).strip().lower()
    job["_dispatch_pool"] = POOL_ALIASES.get(normalized_pool, normalized_pool)
    light_limit = max(int(settings.JOB_SCHEDULER_LIGHT_BACKTEST_MAX_CODES), 1)
    exclusive = (
        bool(requires_training)
        or not job["_dispatch_pool"].startswith("custom_")
        or custom_code_count <= 0
        or custom_code_count > light_limit
    )
    job["_dispatch_exclusive"] = exclusive
    job["_dispatch_heavy"] = exclusive
    job["_dispatch_slots"] = 2 if exclusive else 1
    return job


def _decode_json_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in ("params", "result", "runtime_code_identity"):
        if isinstance(result.get(field), str):
            try:
                result[field] = json.loads(result[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return result


def _runtime_code_evidence_json() -> str:
    return json.dumps(
        runtime_code_evidence(),
        ensure_ascii=False,
        sort_keys=True,
    )


def sanitize_job_payload(value: Any) -> Any:
    """Remove credentials and host-local paths from client-visible job data."""
    return sanitize_public_payload(value)


def _validated_job_params(params: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(params, dict):
        raise UnsafePayloadError("job_params_must_be_object", "params")
    encoded = canonical_json_for_storage(
        params,
        field="params",
        max_bytes=512 * 1024,
        max_depth=12,
        max_nodes=25_000,
        forbid_sensitive_keys=True,
    )
    return params, encoded


def _validate_submission_identity(
    *,
    job_type: str,
    user_id: int | None,
    attempt: int,
    display_name: str | None,
    parent_job_uuid: str | None,
) -> None:
    if job_type not in JOB_TYPE_METADATA:
        raise UnsafePayloadError("job_type_unsupported", "job_type")
    if user_id is not None and (
        isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1
    ):
        raise UnsafePayloadError("job_user_invalid", "user_id")
    if isinstance(attempt, bool) or not 1 <= int(attempt) <= 100:
        raise UnsafePayloadError("job_attempt_invalid", "attempt")
    if display_name is not None and len(str(display_name)) > 200:
        raise UnsafePayloadError("job_display_name_too_long", "display_name")
    if parent_job_uuid is not None and not (
        len(parent_job_uuid) == 32
        and all(character in "0123456789abcdef" for character in parent_job_uuid)
    ):
        raise UnsafePayloadError("parent_job_uuid_invalid", "parent_job_uuid")


def _bounded_label(value: Any, allowed: set[str], fallback: str) -> str:
    label = str(value or "").strip().lower()
    return label if label in allowed else fallback


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return round(float(ordered[index]), 3)


def _parse_db_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


class JobBroker:
    """Persist and atomically dispatch jobs from a SQLite priority queue."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = str(settings.abs_path(settings.EXPERIMENT_DB))
        self._db_path = Path(db_path)
        self._lock = asyncio.Lock()
        self._worker_started_at: str | None = None
        self._worker_heartbeat_at: str | None = None
        self._worker_heartbeat_monotonic: float | None = None
        self._scheduler_status: dict[str, Any] = {
            "desired_capacity": 1,
            "configured_max": min(
                max(int(settings.JOB_SCHEDULER_MAX_CONCURRENCY), 1), 2
            ),
            "running_slots": 0,
            "degraded": True,
            "reasons": ["starting"],
            "metrics": None,
            "execution_mode": "hybrid_spawn_factor_research",
            "leader": False,
            "pause_heavy": False,
            "admission_mode": "normal",
        }
        self._pending_sqlite_contention = 0
        self._wake_event = asyncio.Event()
        self._ensure_db()

    def _get_conn(
        self, *, timeout_seconds: float | None = None
    ) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = 5.0 if timeout_seconds is None else max(timeout_seconds, 0.0)
        conn = sqlite3.connect(str(self._db_path), timeout=timeout)
        conn.execute(f"PRAGMA busy_timeout={max(round(timeout * 1000), 0)}")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        conn = self._get_conn()
        # Serialize additive schema migration across simultaneously starting
        # API processes. SQLite DDL participates in this explicit transaction.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type            TEXT NOT NULL,
                display_name        TEXT,
                params              TEXT,
                status              TEXT DEFAULT 'pending',
                progress            REAL DEFAULT 0.0,
                progress_message    TEXT,
                current_stage       TEXT,
                result              TEXT,
                error               TEXT,
                resource_type       TEXT,
                resource_id         TEXT,
                parent_job_uuid     TEXT,
                attempt             INTEGER DEFAULT 1,
                worker_id           TEXT,
                priority            INTEGER DEFAULT 10,
                queue_group         TEXT,
                lease_generation    INTEGER DEFAULT 0,
                lease_expires_at    TEXT,
                cancel_requested_at TEXT,
                heartbeat_at        TEXT,
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now')),
                started_at          TEXT,
                completed_at        TEXT,
                user_id             INTEGER,
                job_uuid            TEXT UNIQUE NOT NULL,
                runtime_code_identity TEXT
            )
            """
        )
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        additions = {
            "params": "TEXT",
            "status": "TEXT DEFAULT 'pending'",
            "progress": "REAL DEFAULT 0.0",
            "result": "TEXT",
            "error": "TEXT",
            "created_at": "TEXT",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "user_id": "INTEGER",
            "display_name": "TEXT",
            "progress_message": "TEXT",
            "current_stage": "TEXT",
            "resource_type": "TEXT",
            "resource_id": "TEXT",
            "parent_job_uuid": "TEXT",
            "attempt": "INTEGER DEFAULT 1",
            "worker_id": "TEXT",
            "priority": "INTEGER DEFAULT 10",
            "queue_group": "TEXT",
            "lease_generation": "INTEGER DEFAULT 0",
            "lease_expires_at": "TEXT",
            "cancel_requested_at": "TEXT",
            "heartbeat_at": "TEXT",
            "updated_at": "TEXT",
            "runtime_code_identity": "TEXT",
        }
        for name, definition in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_dispatch
            ON jobs(status, priority DESC, id ASC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_uuid    TEXT NOT NULL,
                status      TEXT,
                progress    REAL,
                stage       TEXT,
                message     TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_events_job
            ON job_events(job_uuid, id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operational_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_name  TEXT NOT NULL,
                category    TEXT NOT NULL,
                job_type    TEXT,
                outcome     TEXT,
                stage       TEXT,
                value       REAL NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_operational_events_window
            ON operational_events(created_at, event_name)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slo_alert_state (
                objective                  TEXT PRIMARY KEY,
                status                     TEXT NOT NULL,
                pending_status             TEXT,
                consecutive_observations   INTEGER NOT NULL DEFAULT 0,
                last_transition_at         TEXT,
                last_breach_notified_at    TEXT,
                last_recovery_notified_at  TEXT,
                updated_at                 TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slo_alert_events (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                objective             TEXT NOT NULL,
                transition            TEXT NOT NULL,
                actual                REAL,
                threshold             REAL NOT NULL,
                window_hours          INTEGER NOT NULL,
                notification_emitted  INTEGER NOT NULL DEFAULT 1,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_slo_alert_events_recent
            ON slo_alert_events(created_at DESC, objective)
            """
        )
        initialize_alert_delivery_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_scheduler_lease (
                singleton_id     INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                owner_id         TEXT NOT NULL,
                owner_host       TEXT,
                owner_pid        INTEGER,
                owner_process_start TEXT,
                heartbeat_at     TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL
            )
            """
        )
        lease_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(job_scheduler_lease)")
        }
        if "owner_host" not in lease_columns:
            conn.execute("ALTER TABLE job_scheduler_lease ADD COLUMN owner_host TEXT")
        if "owner_pid" not in lease_columns:
            conn.execute("ALTER TABLE job_scheduler_lease ADD COLUMN owner_pid INTEGER")
        if "owner_process_start" not in lease_columns:
            conn.execute(
                "ALTER TABLE job_scheduler_lease ADD COLUMN owner_process_start TEXT"
            )
        conn.execute(
            "UPDATE jobs SET updated_at = COALESCE(updated_at, created_at), "
            "attempt = COALESCE(attempt, 1), priority = COALESCE(priority, 10), "
            "lease_generation = COALESCE(lease_generation, 0)"
        )
        legacy_rows = conn.execute(
            """
            SELECT job_uuid, job_type, params FROM jobs
            WHERE queue_group IS NULL
            """
        ).fetchall()
        for row in legacy_rows:
            decoded = _decode_json_fields(row)
            raw_params = decoded.get("params")
            params = raw_params if isinstance(raw_params, dict) else {}
            conn.execute(
                "UPDATE jobs SET priority=?, queue_group=? WHERE job_uuid=?",
                (
                    _job_priority(str(decoded["job_type"]), params),
                    _queue_group(
                        str(decoded["job_type"]),
                        params,
                        str(decoded["job_uuid"]),
                    ),
                    decoded["job_uuid"],
                ),
            )
        conn.commit()
        conn.close()

    @staticmethod
    def _insert_operational_event(
        conn: sqlite3.Connection,
        event_name: str,
        category: str,
        *,
        job_type: str | None = None,
        outcome: str | None = None,
        stage: str | None = None,
        value: float = 1.0,
    ) -> None:
        """Insert one low-cardinality event; reject arbitrary label values."""
        if event_name not in _OPERATIONAL_EVENT_NAMES:
            raise ValueError("unsupported operational event")
        if category not in _OPERATIONAL_CATEGORIES:
            raise ValueError("unsupported operational category")
        bounded_job_type = (
            str(job_type)
            if job_type in JOB_TYPE_METADATA
            else "other"
            if job_type
            else None
        )
        bounded_outcome = (
            _bounded_label(
                outcome,
                {"completed", "failed", "cancelled", "connected", "disconnected"},
                "other",
            )
            if outcome
            else None
        )
        bounded_stage = (
            _bounded_label(stage, _BOUNDED_STAGES, "other")
            if stage
            else None
        )
        conn.execute(
            """
            INSERT INTO operational_events
                (event_name, category, job_type, outcome, stage, value)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_name,
                category,
                bounded_job_type,
                bounded_outcome,
                bounded_stage,
                float(value),
            ),
        )

    async def record_operational_event(
        self,
        event_name: str,
        category: str,
        *,
        job_type: str | None = None,
        outcome: str | None = None,
        stage: str | None = None,
        value: float = 1.0,
    ) -> None:
        """Persist one bounded event without exposing request/user identifiers."""
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                self._insert_operational_event(
                    conn,
                    event_name,
                    category,
                    job_type=job_type,
                    outcome=outcome,
                    stage=stage,
                    value=value,
                )
                retention = max(
                    min(int(settings.JOB_OBSERVABILITY_RETENTION_HOURS), 24 * 31),
                    1,
                )
                conn.execute(
                    "DELETE FROM operational_events "
                    "WHERE julianday(created_at) < julianday('now', ?)",
                    (f"-{retention} hours",),
                )
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if _is_sqlite_busy(exc):
                    self._pending_sqlite_contention += 1
                    return
                raise
            finally:
                conn.close()

    def note_sqlite_contention(self) -> None:
        """Count contention without attempting another write while DB is busy."""
        self._pending_sqlite_contention += 1

    async def flush_operational_counters(self) -> None:
        pending = self._pending_sqlite_contention
        if pending <= 0:
            return
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                self._insert_operational_event(
                    conn,
                    "sqlite_contention",
                    "storage",
                    value=float(pending),
                )
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if not _is_sqlite_busy(exc):
                    raise
                return
            finally:
                conn.close()
        self._pending_sqlite_contention = max(
            self._pending_sqlite_contention - pending,
            0,
        )

    @staticmethod
    def _metadata(
        job_type: str,
        params: dict[str, Any],
        display_name: str | None,
        resource_type: str | None,
        resource_id: str | int | None,
    ) -> tuple[str, str | None, str | None]:
        default_name, default_resource_type, resource_key = JOB_TYPE_METADATA.get(
            job_type, (job_type, None, None)
        )
        inferred_id = params.get(resource_key) if resource_key else None
        resolved_id = resource_id if resource_id is not None else inferred_id
        return (
            display_name or default_name,
            resource_type or default_resource_type,
            str(resolved_id) if resolved_id is not None else None,
        )

    async def _emit_change(self, job: dict[str, Any] | None) -> None:
        if not job:
            return
        try:
            from backend.ws.jobs import publish_job_change

            await publish_job_change(
                user_id=job.get("user_id"),
                payload={
                    "type": "job_updated",
                    "job_uuid": job["job_uuid"],
                    "job_type": job["job_type"],
                    "status": job["status"],
                    "progress": job.get("progress", 0),
                    "progress_message": job.get("progress_message"),
                    "updated_at": job.get("updated_at"),
                },
            )
        except Exception:
            logger.debug("Unable to publish job update", exc_info=True)

    async def submit_job(
        self,
        job_type: str,
        params: dict[str, Any] | None = None,
        user_id: int | None = None,
        *,
        display_name: str | None = None,
        resource_type: str | None = None,
        resource_id: str | int | None = None,
        parent_job_uuid: str | None = None,
        attempt: int = 1,
        deduplicate_active: bool = False,
        deduplicate_existing: bool = False,
    ) -> str:
        params = params or {}
        params, params_json = _validated_job_params(params)
        _validate_submission_identity(
            job_type=job_type,
            user_id=user_id,
            attempt=attempt,
            display_name=display_name,
            parent_job_uuid=parent_job_uuid,
        )
        job_uuid = uuid.uuid4().hex
        priority = _job_priority(job_type, params)
        queue_group = _queue_group(job_type, params, job_uuid)
        resolved_name, resolved_type, resolved_id = self._metadata(
            job_type, params, display_name, resource_type, resource_id
        )
        runtime_identity_json = _runtime_code_evidence_json()
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if (
                    (deduplicate_active or deduplicate_existing)
                    and resolved_type is not None
                    and resolved_id is not None
                ):
                    existing = conn.execute(
                        """
                        SELECT job_uuid
                        FROM jobs
                        WHERE job_type=? AND resource_type=? AND resource_id=?
                          AND (
                            ?=1 OR status IN (
                                'pending', 'running', 'cancel_requested'
                            )
                          )
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (
                            job_type,
                            resolved_type,
                            resolved_id,
                            int(deduplicate_existing),
                        ),
                    ).fetchone()
                    if existing is not None:
                        conn.commit()
                        return str(existing["job_uuid"])
                pending_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM jobs
                        WHERE status IN ('pending', 'running', 'cancel_requested')
                        """
                    ).fetchone()[0]
                )
                if user_id is not None:
                    user_active = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM jobs
                            WHERE user_id=?
                              AND status IN (
                                  'pending', 'running', 'cancel_requested'
                              )
                            """,
                            (user_id,),
                        ).fetchone()[0]
                    )
                    if user_active >= max(
                        int(settings.JOB_SCHEDULER_MAX_ACTIVE_PER_USER), 1
                    ):
                        raise JobQueueFullError(
                            "当前账号的活动任务已达到上限，请等待后重试"
                        )
                if (
                    pending_count
                    >= max(int(settings.JOB_SCHEDULER_MAX_PENDING_JOBS), 1)
                    and priority < 100
                ):
                    raise JobQueueFullError(
                        "任务队列已达到背压上限，请等待现有任务完成后重试"
                    )
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_type, display_name, params, status, progress,
                        progress_message, current_stage, resource_type, resource_id,
                        parent_job_uuid, attempt, priority, queue_group,
                        user_id, job_uuid, runtime_code_identity, updated_at
                    )
                    VALUES (?, ?, ?, 'pending', 0, '等待执行', 'queued', ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        job_type,
                        resolved_name,
                        params_json,
                        resolved_type,
                        resolved_id,
                        parent_job_uuid,
                        attempt,
                        priority,
                        queue_group,
                        user_id,
                        job_uuid,
                        runtime_identity_json,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO job_events (job_uuid, status, progress, stage, message)
                    VALUES (?, 'pending', 0, 'queued', '任务已提交')
                    """,
                    (job_uuid,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        self._wake_event.set()
        await self._emit_change(await self.get_job_status(job_uuid))
        return job_uuid

    async def submit_jobs_batch(
        self,
        submissions: list[dict[str, Any]],
        *,
        sweep_id: int | None = None,
        reset_experiment_ids: list[int] | None = None,
        replace_experiment_ids: list[int] | None = None,
    ) -> list[str]:
        """Persist a group of jobs atomically and wake the scheduler once.

        Sweep creation and repair use this path so the scheduler cannot claim
        the first member while the API is still inserting later members.
        When repairing a sweep, experiment resets/replacements and job inserts
        share the same SQLite transaction. A failed experiment that already
        owns an immutable research manifest must be replaced instead of reset.
        """
        if not submissions:
            return []
        reset_ids = [int(item) for item in (reset_experiment_ids or [])]
        replace_ids = [int(item) for item in (replace_experiment_ids or [])]
        if len(set(reset_ids)) != len(reset_ids):
            raise ValueError("reset_experiment_ids contains duplicates")
        if len(set(replace_ids)) != len(replace_ids):
            raise ValueError("replace_experiment_ids contains duplicates")
        if set(reset_ids) & set(replace_ids):
            raise ValueError("reset and replace experiment IDs overlap")
        if replace_ids and sweep_id is None:
            raise ValueError("replacing sweep members requires sweep_id")

        prepared: list[dict[str, Any]] = []
        for submission in submissions:
            params = dict(submission.get("params") or {})
            job_type = str(submission["job_type"])
            params, params_json = _validated_job_params(params)
            attempt = int(submission.get("attempt", 1))
            _validate_submission_identity(
                job_type=job_type,
                user_id=submission.get("user_id"),
                attempt=attempt,
                display_name=submission.get("display_name"),
                parent_job_uuid=submission.get("parent_job_uuid"),
            )
            job_uuid = uuid.uuid4().hex
            resolved_name, resolved_type, resolved_id = self._metadata(
                job_type,
                params,
                submission.get("display_name"),
                submission.get("resource_type"),
                submission.get("resource_id"),
            )
            prepared.append(
                {
                    "job_type": job_type,
                    "params": params,
                    "params_json": params_json,
                    "user_id": submission.get("user_id"),
                    "display_name": resolved_name,
                    "resource_type": resolved_type,
                    "resource_id": resolved_id,
                    "parent_job_uuid": submission.get("parent_job_uuid"),
                    "attempt": attempt,
                    "priority": _job_priority(job_type, params),
                    "job_uuid": job_uuid,
                    "queue_group": _queue_group(job_type, params, job_uuid),
                }
            )

        runtime_identity_json = _runtime_code_evidence_json()
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                pending_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM jobs
                        WHERE status IN ('pending', 'running', 'cancel_requested')
                        """
                    ).fetchone()[0]
                )
                noncritical_count = sum(
                    1 for item in prepared if item["priority"] < 100
                )
                incoming_by_user: dict[int, int] = {}
                for item in prepared:
                    item_user_id = item["user_id"]
                    if item_user_id is not None:
                        incoming_by_user[int(item_user_id)] = (
                            incoming_by_user.get(int(item_user_id), 0) + 1
                        )
                per_user_limit = max(
                    int(settings.JOB_SCHEDULER_MAX_ACTIVE_PER_USER), 1
                )
                for item_user_id, incoming in incoming_by_user.items():
                    existing = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM jobs
                            WHERE user_id=?
                              AND status IN (
                                  'pending', 'running', 'cancel_requested'
                              )
                            """,
                            (item_user_id,),
                        ).fetchone()[0]
                    )
                    if existing + incoming > per_user_limit:
                        raise JobQueueFullError(
                            "当前账号的活动任务剩余容量不足，未提交本批任务"
                        )
                if (
                    noncritical_count
                    and pending_count + noncritical_count
                    > max(int(settings.JOB_SCHEDULER_MAX_PENDING_JOBS), 1)
                ):
                    raise JobQueueFullError(
                        "任务队列剩余容量不足，未提交本批任务"
                    )

                recovery_ids = [*reset_ids, *replace_ids]
                if recovery_ids:
                    placeholders = ",".join("?" for _ in recovery_ids)
                    active = int(
                        conn.execute(
                            f"""
                            SELECT COUNT(*) FROM jobs
                            WHERE resource_type='experiment'
                              AND CAST(resource_id AS INTEGER) IN ({placeholders})
                              AND status IN ('pending', 'running', 'cancel_requested')
                            """,
                            recovery_ids,
                        ).fetchone()[0]
                    )
                    if active:
                        raise RuntimeError("待恢复实验仍有活动任务")

                replacement_map: dict[int, int] = {}
                for old_id in replace_ids:
                    source = conn.execute(
                        """
                        SELECT e.*
                        FROM experiments e
                        JOIN sweep_experiments se ON se.experiment_id=e.id
                        JOIN research_run_manifests rm ON rm.experiment_id=e.id
                        WHERE e.id=? AND e.status='failed' AND se.sweep_id=?
                        """,
                        (old_id, sweep_id),
                    ).fetchone()
                    if source is None:
                        raise RuntimeError(
                            "待替换实验状态、扫描归属或研究清单已变化"
                        )
                    cursor = conn.execute(
                        """
                        INSERT INTO experiments (
                            user_id, name, strategy_id, strategy_category,
                            is_starred, labels, pool_preset, pool_custom_codes,
                            pool_industries, train_start, train_end, test_start,
                            test_end, params, params_hash, mode,
                            requires_training, retrain_frequency, status,
                            progress_pct, progress_message, source_experiment_id,
                            run_spec
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, 'pending', 0, '等待恢复副本执行', ?, ?)
                        """,
                        (
                            source["user_id"],
                            f"{source['name']} · 恢复副本",
                            source["strategy_id"],
                            source["strategy_category"],
                            source["is_starred"],
                            source["labels"],
                            source["pool_preset"],
                            source["pool_custom_codes"],
                            source["pool_industries"],
                            source["train_start"],
                            source["train_end"],
                            source["test_start"],
                            source["test_end"],
                            source["params"],
                            source["params_hash"],
                            source["mode"],
                            source["requires_training"],
                            source["retrain_frequency"],
                            source["source_experiment_id"],
                            source["run_spec"],
                        ),
                    )
                    new_id = int(cursor.lastrowid)
                    updated = conn.execute(
                        """
                        UPDATE sweep_experiments
                        SET experiment_id=?
                        WHERE sweep_id=? AND experiment_id=?
                        """,
                        (new_id, sweep_id, old_id),
                    ).rowcount
                    if updated != 1:
                        raise RuntimeError("扫描成员替换失败")
                    replacement_map[old_id] = new_id

                if replacement_map:
                    for item in prepared:
                        old_id = int(item["params"].get("experiment_id", 0))
                        new_id = replacement_map.get(old_id)
                        if new_id is None:
                            continue
                        item["params"]["experiment_id"] = new_id
                        item["resource_id"] = str(new_id)
                        item["display_name"] = (
                            f"{item['display_name']} · 副本 #{new_id}"
                        )

                if reset_ids:
                    placeholders = ",".join("?" for _ in reset_ids)
                    updated = conn.execute(
                        f"""
                        UPDATE experiments
                        SET status='pending', progress_pct=0,
                            progress_message='等待恢复执行', error_log=NULL,
                            started_at=NULL, completed_at=NULL
                        WHERE id IN ({placeholders}) AND status='failed'
                        """,
                        reset_ids,
                    ).rowcount
                    if updated != len(reset_ids):
                        raise RuntimeError("待恢复实验状态已变化，请刷新后重试")

                for item in prepared:
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            job_type, display_name, params, status, progress,
                            progress_message, current_stage, resource_type,
                            resource_id, parent_job_uuid, attempt, priority,
                            queue_group, user_id, job_uuid,
                            runtime_code_identity, updated_at
                        )
                        VALUES (?, ?, ?, 'pending', 0, '等待执行', 'queued', ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        """,
                        (
                            item["job_type"],
                            item["display_name"],
                            _validated_job_params(item["params"])[1],
                            item["resource_type"],
                            item["resource_id"],
                            item["parent_job_uuid"],
                            item["attempt"],
                            item["priority"],
                            item["queue_group"],
                            item["user_id"],
                            item["job_uuid"],
                            runtime_identity_json,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO job_events
                            (job_uuid, status, progress, stage, message)
                        VALUES (?, 'pending', 0, 'queued', '任务已批量提交')
                        """,
                        (item["job_uuid"],),
                    )

                if sweep_id is not None:
                    conn.execute(
                        """
                        UPDATE param_sweeps
                        SET status='running',
                            completed_experiments=(
                                SELECT COUNT(*)
                                FROM sweep_experiments se
                                JOIN experiments e ON e.id=se.experiment_id
                                WHERE se.sweep_id=?
                                  AND e.status IN ('completed','failed','cancelled')
                            )
                        WHERE id=?
                        """,
                        (sweep_id, sweep_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        self._wake_event.set()
        job_uuids = [str(item["job_uuid"]) for item in prepared]
        for job_uuid in job_uuids:
            await self._emit_change(await self.get_job_status(job_uuid))
        return job_uuids

    async def list_jobs(self) -> list[dict[str, Any]]:
        items, _ = await self.query_jobs(
            page=1,
            page_size=10_000,
            include_all=True,
            include_system=True,
        )
        return items

    async def query_jobs(
        self,
        *,
        user_id: int | None = None,
        include_all: bool = False,
        include_system: bool = False,
        status: str | None = None,
        job_type: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        values: list[Any] = []
        if not include_all:
            clauses.append("user_id = ?")
            values.append(user_id)
        elif not include_system:
            clauses.append("user_id IS NOT NULL")
        if status:
            clauses.append("status = ?")
            values.append(status)
        if job_type:
            clauses.append("job_type = ?")
            values.append(job_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        queued_priority = _effective_priority_sql("queued")
        job_priority = _effective_priority_sql("jobs")
        conn = self._get_conn()
        total = int(conn.execute(f"SELECT COUNT(*) FROM jobs{where}", values).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT jobs.*,
                CASE WHEN jobs.status = 'pending' THEN (
                    SELECT 1 + COUNT(*) FROM jobs queued
                    WHERE queued.status = 'pending'
                      AND (
                        {queued_priority} > {job_priority}
                        OR (
                            {queued_priority} = {job_priority}
                            AND (
                                queued.priority > jobs.priority
                                OR (
                                    queued.priority = jobs.priority
                                    AND queued.id < jobs.id
                                )
                            )
                        )
                      )
                ) ELSE NULL END AS queue_position
            FROM jobs{where}
            ORDER BY jobs.id DESC
            LIMIT ? OFFSET ?
            """,
            [*values, page_size, (page - 1) * page_size],
        ).fetchall()
        decoded_rows = [
            _hydrate_dispatch_resource(conn, _decode_json_fields(row))
            for row in rows
        ]
        conn.close()
        return [
            self._decorate_queue_reason(row)
            for row in decoded_rows
        ], total

    async def get_job_status(self, job_uuid: str) -> Optional[dict[str, Any]]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE job_uuid = ?", (job_uuid,)).fetchone()
        if row is None:
            conn.close()
            return None
        decoded = _hydrate_dispatch_resource(conn, _decode_json_fields(row))
        conn.close()
        return self._decorate_queue_reason(decoded)

    def _decorate_queue_reason(self, job: dict[str, Any]) -> dict[str, Any]:
        is_heavy = bool(job.get("_dispatch_heavy"))
        for private_key in (
            "_dispatch_pool",
            "_dispatch_exclusive",
            "_dispatch_slots",
            "_dispatch_heavy",
        ):
            job.pop(private_key, None)
        if job.get("status") != "pending":
            job["queue_reason"] = None
            return job
        reasons = list(self._scheduler_status.get("reasons") or [])
        if (
            self._scheduler_status.get("pause_heavy")
            and is_heavy
        ):
            job["queue_reason"] = "resource_pressure_heavy_jobs_paused"
        elif not self._scheduler_status.get("leader"):
            job["queue_reason"] = (
                reasons[0] if reasons else "scheduler_not_leader"
            )
        elif reasons and self._scheduler_status.get("desired_capacity", 1) <= 1:
            job["queue_reason"] = reasons[0]
        else:
            job["queue_reason"] = "waiting_for_capacity"
        return job

    async def list_job_events(self, job_uuid: str, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT status, progress, stage, message, created_at
            FROM job_events WHERE job_uuid = ? ORDER BY id DESC LIMIT ?
            """,
            (job_uuid, limit),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    async def request_cancel(self, job_uuid: str) -> str | None:
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM jobs WHERE job_uuid = ?", (job_uuid,)
                ).fetchone()
                if row is None or row["status"] not in (
                    "pending",
                    "running",
                    "cancel_requested",
                ):
                    conn.rollback()
                    return None
                if (
                    row["job_type"] == "research_data_refresh"
                    and row["current_stage"] == "research_import_activate"
                ):
                    # The complete immutable generation is crossing its atomic
                    # pointer commit.  A cancellation accepted here could make
                    # the job say cancelled after data was activated.
                    conn.rollback()
                    return None
                next_status = (
                    "cancelled" if row["status"] == "pending" else "cancel_requested"
                )
                completed_sql = (
                    ", completed_at = datetime('now')"
                    if next_status == "cancelled"
                    else ""
                )
                conn.execute(
                    f"""
                    UPDATE jobs
                    SET status = ?, cancel_requested_at = datetime('now'),
                        progress_message = ?,
                        updated_at = datetime('now'){completed_sql}
                    WHERE job_uuid = ?
                    """,
                    (
                        next_status,
                        "已取消" if next_status == "cancelled" else "正在安全停止",
                        job_uuid,
                    ),
                )
                if next_status == "cancelled":
                    _reconcile_backtest_experiment(
                        conn,
                        row,
                        status="cancelled",
                        message="任务在排队时取消",
                    )
                conn.execute(
                    """
                    INSERT INTO job_events
                        (job_uuid, status, progress, stage, message)
                    VALUES (?, ?, ?, 'cancelling', ?)
                    """,
                    (
                        job_uuid,
                        next_status,
                        row["progress"],
                        "任务已取消"
                        if next_status == "cancelled"
                        else "已请求取消",
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        await self._emit_change(await self.get_job_status(job_uuid))
        return next_status

    async def cancel_job(self, job_uuid: str) -> bool:
        return await self.request_cancel(job_uuid) is not None

    async def is_cancel_requested(self, job_uuid: str) -> bool:
        conn = self._get_conn()
        row = conn.execute("SELECT status FROM jobs WHERE job_uuid = ?", (job_uuid,)).fetchone()
        conn.close()
        return row is not None and row["status"] in ("cancel_requested", "cancelled")

    async def raise_if_cancelled(self, job_uuid: str) -> None:
        if await self.is_cancel_requested(job_uuid):
            await self.update_job_progress(
                job_uuid,
                status="cancelled",
                message="任务已安全停止",
                stage="cancelled",
            )
            raise JobCancelledError("任务已取消")

    async def update_job_progress(
        self,
        job_uuid: str,
        progress: float | None = None,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        *,
        message: str | None = None,
        stage: str | None = None,
    ) -> None:
        if status and status not in VALID_STATUSES:
            raise ValueError(f"无效状态: {status}")
        if progress is not None and not math.isfinite(float(progress)):
            raise UnsafePayloadError("job_progress_non_finite", "progress")
        result_json = (
            canonical_json_for_storage(
                result,
                field="result",
                max_bytes=2 * 1024 * 1024,
                max_depth=16,
                max_nodes=100_000,
            )
            if result is not None
            else None
        )
        safe_error = (
            sanitize_diagnostic(error, max_length=16_384)
            if error is not None
            else None
        )
        safe_message = (
            sanitize_diagnostic(message, max_length=500)
            if message is not None
            else None
        )
        safe_stage = None
        if stage is not None:
            safe_stage = str(stage).strip().lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,80}", safe_stage):
                raise UnsafePayloadError("job_stage_invalid", "stage")
        for attempt in range(_PROGRESS_BUSY_RETRY_ATTEMPTS):
            try:
                await self._update_job_progress_once(
                    job_uuid,
                    progress,
                    status,
                    result_json,
                    safe_error,
                    message=safe_message,
                    stage=safe_stage,
                )
                break
            except sqlite3.OperationalError as exc:
                if (
                    not _is_sqlite_busy(exc)
                    or attempt + 1 >= _PROGRESS_BUSY_RETRY_ATTEMPTS
                ):
                    raise
                await asyncio.sleep(
                    _PROGRESS_BUSY_RETRY_BASE_SECONDS * (2**attempt)
                )
        await self._emit_change(await self.get_job_status(job_uuid))

    async def _update_job_progress_once(
        self,
        job_uuid: str,
        progress: float | None,
        status: str | None,
        result_json: str | None,
        error: str | None,
        *,
        message: str | None,
        stage: str | None,
    ) -> None:
        execution_claim = _EXECUTION_CLAIM.get()
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    """
                    SELECT status, progress, worker_id, lease_generation
                    FROM jobs WHERE job_uuid = ?
                    """,
                    (job_uuid,),
                ).fetchone()
                if execution_claim is not None and execution_claim[0] == job_uuid:
                    _, worker_id, generation = execution_claim
                    if (
                        current is None
                        or current["worker_id"] != worker_id
                        or int(current["lease_generation"] or 0) != generation
                    ):
                        raise JobLeaseLostError(
                            f"job lease lost before progress update: {job_uuid}"
                        )
                if current is None or current["status"] in TERMINAL_STATUSES:
                    conn.rollback()
                    return
                next_status = status
                if current["status"] == "cancel_requested" and status in (
                    "completed",
                    "failed",
                ):
                    next_status = "cancelled"
                    result_json = None
                    error = None
                    message = "任务已安全停止"
                    stage = "cancelled"
                allowed_transitions = {
                    "pending": {
                        "pending",
                        "running",
                        "cancel_requested",
                        "completed",
                        "failed",
                        "cancelled",
                    },
                    "running": {
                        "running",
                        "cancel_requested",
                        "completed",
                        "failed",
                        "cancelled",
                    },
                    "cancel_requested": {"cancel_requested", "cancelled"},
                }
                if next_status is not None and next_status not in (
                    allowed_transitions.get(str(current["status"])) or set()
                ):
                    raise UnsafePayloadError(
                        "job_status_transition_invalid", "status"
                    )
                updates = [
                    "updated_at = datetime('now')",
                    "heartbeat_at = datetime('now')",
                ]
                values: list[Any] = []
                resolved_progress = (
                    current["progress"]
                    if progress is None
                    else max(0.0, min(progress, 1.0))
                )
                updates.append("progress = ?")
                values.append(resolved_progress)
                if next_status:
                    updates.append("status = ?")
                    values.append(next_status)
                    if next_status == "running":
                        updates.append(
                            "started_at = COALESCE(started_at, datetime('now'))"
                        )
                    elif next_status in TERMINAL_STATUSES:
                        updates.append("completed_at = datetime('now')")
                if result_json is not None:
                    updates.append("result = ?")
                    values.append(result_json)
                if error is not None:
                    updates.append("error = ?")
                    values.append(error)
                if message is not None:
                    updates.append("progress_message = ?")
                    values.append(message)
                if stage is not None:
                    updates.append("current_stage = ?")
                    values.append(stage)
                values.append(job_uuid)
                conn.execute(
                    f"UPDATE jobs SET {', '.join(updates)} WHERE job_uuid = ?",
                    values,
                )
                event_status = next_status or current["status"]
                conn.execute(
                    """
                    INSERT INTO job_events
                        (job_uuid, status, progress, stage, message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job_uuid,
                        event_status,
                        resolved_progress,
                        stage,
                        message,
                    ),
                )
                job_type_row = conn.execute(
                    "SELECT job_type FROM jobs WHERE job_uuid=?",
                    (job_uuid,),
                ).fetchone()
                job_type = (
                    str(job_type_row["job_type"])
                    if job_type_row is not None
                    else None
                )
                if next_status in TERMINAL_STATUSES:
                    self._insert_operational_event(
                        conn,
                        "job_terminal",
                        "job",
                        job_type=job_type,
                        outcome=next_status,
                        stage=stage or next_status,
                    )
                elif job_type == "data_update" and (
                    progress is not None or stage is not None
                ):
                    self._insert_operational_event(
                        conn,
                        "data_refresh_progress",
                        "data",
                        job_type=job_type,
                        stage=stage,
                        value=resolved_progress,
                    )
                conn.commit()
                if next_status in TERMINAL_STATUSES:
                    structured_log(
                        logger,
                        logging.INFO,
                        "job_terminal",
                        component="job_broker",
                        job_type=job_type or "other",
                        outcome=next_status,
                        stage=_bounded_label(
                            stage or next_status,
                            _BOUNDED_STAGES,
                            "other",
                        ),
                    )
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def wait_for_work(self, timeout: float) -> None:
        """Wait for a local enqueue notification, with polling for other processes."""
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=max(timeout, 0.05))
        except asyncio.TimeoutError:
            return
        finally:
            self._wake_event.clear()

    async def recover_pending_jobs(self) -> int:
        await self.recover_expired_claims(source="scheduler_start")
        conn = self._get_conn(timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS)
        try:
            conn.execute("BEGIN IMMEDIATE")
            reconciliation = reconcile_terminal_backtest_state(
                conn,
                limit=DEFAULT_RECONCILIATION_LIMIT,
                source="startup",
            )
            conn.commit()
            rows = conn.execute(
                "SELECT job_uuid FROM jobs WHERE status='pending' ORDER BY id ASC"
            ).fetchall()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if rows:
            self._wake_event.set()
            logger.info("Found %d dispatchable pending jobs after recovery", len(rows))
        if reconciliation.repaired:
            logger.warning(
                "Reconciled %d stale terminal backtest experiment states at startup",
                reconciliation.repaired,
            )
        return len(rows)

    async def recover_expired_claims(
        self,
        *,
        limit: int = _DEFAULT_EXPIRED_CLAIM_RECOVERY_LIMIT,
        source: str = "periodic",
    ) -> dict[str, int]:
        """Recover one bounded batch of expired active claims.

        The write transaction and generation/owner predicates form the
        takeover fence. A heartbeat committed before this transaction removes
        the row from the candidate set; an obsolete worker is fenced as soon
        as recovery increments the generation and clears its worker identity.
        """
        bounded_limit = min(
            max(int(limit), 1),
            _MAX_EXPIRED_CLAIM_RECOVERY_LIMIT,
        )
        recovered_running = 0
        recovered_cancel_requested = 0
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                expired = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status IN ('running', 'cancel_requested')
                      AND (
                        lease_expires_at IS NULL
                        OR lease_expires_at <= datetime('now')
                      )
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
                for job in expired:
                    status = str(job["status"])
                    common_fence = (
                        job["job_uuid"],
                        job["worker_id"],
                        int(job["lease_generation"] or 0),
                    )
                    if status == "running":
                        message = "任务租约已过期，等待重新执行"
                        cursor = conn.execute(
                            """
                            UPDATE jobs
                            SET status='pending', started_at=NULL, worker_id=NULL,
                                heartbeat_at=NULL, lease_expires_at=NULL,
                                lease_generation=COALESCE(lease_generation, 0) + 1,
                                progress_message=?, current_stage='queued',
                                updated_at=datetime('now'),
                                error=COALESCE(
                                    error,
                                    'worker lease expired before completion'
                                )
                            WHERE job_uuid=? AND worker_id IS ?
                              AND lease_generation=? AND status='running'
                              AND (
                                lease_expires_at IS NULL
                                OR lease_expires_at <= datetime('now')
                              )
                            """,
                            (message, *common_fence),
                        )
                        next_status = "pending"
                    else:
                        message = "任务租约已过期，已完成取消"
                        cursor = conn.execute(
                            """
                            UPDATE jobs
                            SET status='cancelled', worker_id=NULL,
                                heartbeat_at=NULL, lease_expires_at=NULL,
                                lease_generation=COALESCE(lease_generation, 0) + 1,
                                completed_at=datetime('now'),
                                progress_message=?, current_stage='cancelled',
                                updated_at=datetime('now')
                            WHERE job_uuid=? AND worker_id IS ?
                              AND lease_generation=?
                              AND status='cancel_requested'
                              AND (
                                lease_expires_at IS NULL
                                OR lease_expires_at <= datetime('now')
                              )
                            """,
                            (message, *common_fence),
                        )
                        next_status = "cancelled"
                    if cursor.rowcount != 1:
                        continue
                    conn.execute(
                        """
                        INSERT INTO job_events
                            (job_uuid, status, progress, stage, message)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            job["job_uuid"],
                            next_status,
                            job["progress"],
                            _LEASE_RECOVERY_EVENT_STAGE,
                            message,
                        ),
                    )
                    _reconcile_backtest_experiment(
                        conn,
                        job,
                        status=next_status,
                        message=message,
                    )
                    if status == "running":
                        recovered_running += 1
                    else:
                        recovered_cancel_requested += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        recovered = recovered_running + recovered_cancel_requested
        if recovered_running:
            self._wake_event.set()
        if recovered:
            structured_log(
                logger,
                logging.WARNING,
                "job_lease_recovery",
                component="job_broker",
                reason="expired_claim",
                outcome="recovered",
                stage=source,
                count=recovered,
            )
        return {
            "recovered": recovered,
            "running": recovered_running,
            "cancel_requested": recovered_cancel_requested,
        }

    def expired_claim_health_snapshot(self) -> dict[str, Any]:
        """Return a bounded, non-sensitive view of expired active claims."""
        conn = self._get_conn(
            timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
        )
        try:
            rows = conn.execute(
                """
                SELECT job_uuid, status, COUNT(*) OVER() AS expired_count
                FROM jobs
                WHERE status IN ('running', 'cancel_requested')
                  AND (
                    lease_expires_at IS NULL
                    OR lease_expires_at <= datetime('now')
                  )
                ORDER BY id ASC
                LIMIT ?
                """,
                (_EXPIRED_CLAIM_HEALTH_SAMPLE_LIMIT,),
            ).fetchall()
        finally:
            conn.close()
        expired_count = int(rows[0]["expired_count"]) if rows else 0
        return {
            "healthy": expired_count == 0,
            "expired_count": expired_count,
            "sample": [
                {
                    "claim_ref": hashlib.sha256(
                        str(row["job_uuid"]).encode("utf-8")
                    ).hexdigest()[:12],
                    "status": str(row["status"]),
                }
                for row in rows
            ],
        }

    async def reconcile_terminal_backtest_jobs(
        self,
        *,
        limit: int = DEFAULT_RECONCILIATION_LIMIT,
    ) -> int:
        """Run one bounded, auditable maintenance reconciliation pass."""
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = reconcile_terminal_backtest_state(
                    conn,
                    limit=limit,
                    source="periodic",
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        if result.repaired:
            logger.warning(
                "Reconciled %d stale terminal backtest experiment states",
                result.repaired,
            )
        return result.repaired

    async def claim_job(
        self,
        job_uuid: str,
        *,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
    ) -> bool:
        resolved_worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        ttl = max(lease_seconds or int(settings.JOB_SCHEDULER_LEASE_SECONDS), 10)
        async with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', progress = MAX(progress, 0.01),
                    progress_message = '任务已开始', current_stage = 'starting',
                    worker_id = ?, heartbeat_at = datetime('now'),
                    lease_generation = COALESCE(lease_generation, 0) + 1,
                    lease_expires_at = datetime('now', ?),
                    updated_at = datetime('now'),
                    started_at = COALESCE(started_at, datetime('now'))
                WHERE job_uuid = ? AND status = 'pending'
                """,
                (resolved_worker_id, f"+{ttl} seconds", job_uuid),
            )
            claimed = cursor.rowcount == 1
            if claimed:
                conn.execute(
                    """
                    INSERT INTO job_events (job_uuid, status, progress, stage, message)
                    VALUES (?, 'running', 0.01, 'starting', '任务已开始')
                    """,
                    (job_uuid,),
                )
            conn.commit()
            conn.close()
        if claimed:
            await self._emit_change(await self.get_job_status(job_uuid))
        return claimed

    @staticmethod
    def _candidate_compatible(
        candidate: dict[str, Any],
        running: list[dict[str, Any]],
        dependency_status: str | None,
    ) -> bool:
        job_type = str(candidate.get("job_type"))
        params = candidate.get("params") or {}
        if job_type == "daily_simulation" and params.get("required_data_job_uuid"):
            if dependency_status in {"pending", "running", "cancel_requested"}:
                return False
        if running and (
            bool(candidate.get("_dispatch_exclusive"))
            or any(bool(item.get("_dispatch_exclusive")) for item in running)
        ):
            return False
        group = candidate.get("queue_group")
        if group and str(group).startswith("sweep:"):
            same_group = sum(item.get("queue_group") == group for item in running)
            if same_group >= max(int(settings.JOB_SCHEDULER_SWEEP_MAX_RUNNING), 1):
                return False
        if job_type in {
            "data_update",
            "pit_governance_refresh",
        }:
            target = candidate.get("_dispatch_pool") or _data_resource(params)
            for item in running:
                if item.get("job_type") in {
                    "data_update",
                    "pit_governance_refresh",
                }:
                    return False
                if item.get("job_type") not in {
                    "data_update",
                    "pit_governance_refresh",
                    *CACHE_READER_JOB_TYPES,
                }:
                    continue
                active_target = item.get("_dispatch_pool")
                if target == "*" or active_target == "*" or target == active_target:
                    return False
        elif job_type in CACHE_READER_JOB_TYPES:
            target = candidate.get("_dispatch_pool")
            for item in running:
                if item.get("job_type") not in {
                    "data_update",
                    "pit_governance_refresh",
                }:
                    continue
                active_target = item.get("_dispatch_pool")
                if target == "*" or active_target == "*" or target == active_target:
                    return False
        return True

    async def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim the highest-priority compatible pending job."""
        ttl = max(lease_seconds or int(settings.JOB_SCHEDULER_LEASE_SECONDS), 10)
        async with self._lock:
            # Claiming is periodic scheduler work, so it must not block the API
            # event loop for sqlite3's five-second default when another
            # experiment transaction owns the writer lock. The transaction
            # remains atomic; a busy BEGIN changes no rows and is retried by
            # the scheduler after bounded backoff.
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                # Avoid acquiring a writer reservation on every idle poll.
                if (
                    conn.execute(
                        "SELECT 1 FROM jobs WHERE status='pending' LIMIT 1"
                    ).fetchone()
                    is None
                ):
                    return None
                conn.execute("BEGIN IMMEDIATE")
                running = [
                    _hydrate_dispatch_resource(conn, _decode_json_fields(row))
                    for row in conn.execute(
                        """
                        SELECT * FROM jobs
                        WHERE status IN ('running', 'cancel_requested')
                        ORDER BY id ASC
                        """
                    ).fetchall()
                ]
                candidates = conn.execute(
                    f"""
                    SELECT * FROM jobs WHERE status='pending'
                    ORDER BY {_effective_priority_sql("jobs")} DESC,
                             priority DESC, id ASC
                    """
                ).fetchall()
                selected: dict[str, Any] | None = None
                for row in candidates:
                    candidate = _hydrate_dispatch_resource(
                        conn, _decode_json_fields(row)
                    )
                    if (
                        self._scheduler_status.get("pause_heavy")
                        and bool(candidate.get("_dispatch_heavy"))
                    ):
                        # Keep the heavy job pending while allowing a bounded
                        # light task to use the surviving slot.
                        continue
                    dependency_status: str | None = None
                    required = (candidate.get("params") or {}).get(
                        "required_data_job_uuid"
                    )
                    if required:
                        dependency = conn.execute(
                            "SELECT status FROM jobs WHERE job_uuid=?",
                            (str(required),),
                        ).fetchone()
                        dependency_status = (
                            str(dependency["status"]) if dependency is not None else "missing"
                        )
                    compatible = self._candidate_compatible(
                        candidate, running, dependency_status
                    )
                    if compatible:
                        selected = candidate
                        break
                    # Once an exclusive/heavy candidate reaches the front, stop
                    # filling spare slots with lower-priority work so currently
                    # running light jobs can drain and the heavy job can start.
                    if candidate.get("_dispatch_exclusive"):
                        break
                if selected is None:
                    conn.rollback()
                    return None
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status='running', progress=MAX(progress, 0.01),
                        progress_message='任务已开始', current_stage='starting',
                        worker_id=?, heartbeat_at=datetime('now'),
                        lease_generation=COALESCE(lease_generation, 0) + 1,
                        lease_expires_at=datetime('now', ?),
                        updated_at=datetime('now'),
                        started_at=COALESCE(started_at, datetime('now'))
                    WHERE job_uuid=? AND status='pending'
                    """,
                    (
                        worker_id,
                        f"+{ttl} seconds",
                        selected["job_uuid"],
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None
                conn.execute(
                    """
                    INSERT INTO job_events
                        (job_uuid, status, progress, stage, message)
                    VALUES (?, 'running', 0.01, 'starting', '任务已开始')
                    """,
                    (selected["job_uuid"],),
                )
                claimed_row = conn.execute(
                    "SELECT * FROM jobs WHERE job_uuid=?",
                    (selected["job_uuid"],),
                ).fetchone()
                claimed = _decode_json_fields(claimed_row)
                for key in (
                    "_dispatch_pool",
                    "_dispatch_exclusive",
                    "_dispatch_slots",
                    "_dispatch_heavy",
                ):
                    if key in selected:
                        claimed[key] = selected[key]
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        await self._emit_change(claimed)
        return claimed

    @contextmanager
    def execution_claim(
        self,
        job_uuid: str,
        worker_id: str,
        lease_generation: int,
    ) -> Iterator[None]:
        token = _EXECUTION_CLAIM.set((job_uuid, worker_id, lease_generation))
        try:
            yield
        finally:
            _EXECUTION_CLAIM.reset(token)

    async def heartbeat_claims(
        self,
        claims: list[tuple[str, str, int]],
        *,
        lease_seconds: int | None = None,
    ) -> None:
        if not claims:
            return
        ttl = max(lease_seconds or int(settings.JOB_SCHEDULER_LEASE_SECONDS), 10)
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                for job_uuid, worker_id, generation in claims:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET heartbeat_at=datetime('now'),
                            lease_expires_at=datetime('now', ?),
                            updated_at=datetime('now')
                        WHERE job_uuid=? AND worker_id=? AND lease_generation=?
                          AND status IN ('running', 'cancel_requested')
                        """,
                        (f"+{ttl} seconds", job_uuid, worker_id, generation),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def release_claims(
        self, claims: list[tuple[str, str, int]], *, reason: str
    ) -> int:
        """Return this process' interrupted claims to pending on graceful exit."""
        if not claims:
            return 0
        released = 0
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                for job_uuid, worker_id, generation in claims:
                    row = conn.execute(
                        """
                        SELECT * FROM jobs
                        WHERE job_uuid=? AND worker_id=? AND lease_generation=?
                          AND status IN ('running', 'cancel_requested')
                        """,
                        (job_uuid, worker_id, generation),
                    ).fetchone()
                    if row is None:
                        continue
                    if row["status"] == "cancel_requested":
                        cursor = conn.execute(
                            """
                            UPDATE jobs
                            SET status='cancelled', lease_expires_at=NULL,
                                completed_at=datetime('now'),
                                progress_message='服务关闭时完成取消',
                                current_stage='cancelled',
                                updated_at=datetime('now')
                            WHERE job_uuid=? AND worker_id=? AND lease_generation=?
                              AND status='cancel_requested'
                            """,
                            (job_uuid, worker_id, generation),
                        )
                        _reconcile_backtest_experiment(
                            conn,
                            row,
                            status="cancelled",
                            message="服务关闭时完成取消",
                        )
                    else:
                        cursor = conn.execute(
                            """
                            UPDATE jobs
                            SET status='pending', worker_id=NULL,
                                lease_expires_at=NULL, started_at=NULL,
                                progress_message=?, current_stage='queued',
                                updated_at=datetime('now')
                            WHERE job_uuid=? AND worker_id=? AND lease_generation=?
                              AND status='running'
                            """,
                            (reason, job_uuid, worker_id, generation),
                        )
                        if cursor.rowcount:
                            _reconcile_backtest_experiment(
                                conn,
                                row,
                                status="pending",
                                message=reason,
                            )
                    released += cursor.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        if released:
            self._wake_event.set()
        return released

    async def acquire_scheduler_lease(
        self, owner_id: str, *, lease_seconds: int | None = None
    ) -> bool:
        ttl = max(lease_seconds or int(settings.JOB_SCHEDULER_LEASE_SECONDS), 10)
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT owner_id, owner_host, owner_pid, owner_process_start,
                           lease_expires_at
                    FROM job_scheduler_lease
                    WHERE singleton_id=1
                    """
                ).fetchone()
                if row is not None and row["owner_id"] != owner_id:
                    same_host_process = (
                        row["owner_host"] == socket.gethostname()
                        and row["owner_pid"] is not None
                    )
                    same_live_process = False
                    if same_host_process:
                        owner_pid = int(row["owner_pid"])
                        same_live_process = _process_alive(owner_pid)
                        stored_start = row["owner_process_start"]
                        if same_live_process and stored_start:
                            observed_start = _process_start_identity(owner_pid)
                            # An unavailable identity check fails conservatively;
                            # a mismatch confirms PID reuse and permits takeover.
                            same_live_process = (
                                observed_start is None
                                or str(observed_start) == str(stored_start)
                            )
                    lease_unexpired = bool(
                        conn.execute(
                            "SELECT ? > datetime('now')",
                            (row["lease_expires_at"],),
                        ).fetchone()[0]
                    )
                    if same_live_process or (
                        not same_host_process and lease_unexpired
                    ):
                        conn.rollback()
                        return False
                conn.execute(
                    """
                    INSERT INTO job_scheduler_lease
                        (singleton_id, owner_id, owner_host, owner_pid,
                         owner_process_start, heartbeat_at, lease_expires_at)
                    VALUES (1, ?, ?, ?, ?, datetime('now'), datetime('now', ?))
                    ON CONFLICT(singleton_id) DO UPDATE SET
                        owner_id=excluded.owner_id,
                        owner_host=excluded.owner_host,
                        owner_pid=excluded.owner_pid,
                        owner_process_start=excluded.owner_process_start,
                        heartbeat_at=excluded.heartbeat_at,
                        lease_expires_at=excluded.lease_expires_at
                    """,
                    (
                        owner_id,
                        socket.gethostname(),
                        os.getpid(),
                        _process_start_identity(os.getpid()),
                        f"+{ttl} seconds",
                    ),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def renew_scheduler_lease(
        self, owner_id: str, *, lease_seconds: int | None = None
    ) -> bool:
        ttl = max(lease_seconds or int(settings.JOB_SCHEDULER_LEASE_SECONDS), 10)
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                cursor = conn.execute(
                    """
                    UPDATE job_scheduler_lease
                    SET heartbeat_at=datetime('now'),
                        lease_expires_at=datetime('now', ?)
                    WHERE singleton_id=1 AND owner_id=?
                    """,
                    (f"+{ttl} seconds", owner_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return cursor.rowcount == 1

    async def release_scheduler_lease(self, owner_id: str) -> None:
        async with self._lock:
            conn = self._get_conn(
                timeout_seconds=_SCHEDULER_CLAIM_BUSY_TIMEOUT_SECONDS
            )
            try:
                conn.execute(
                    "DELETE FROM job_scheduler_lease "
                    "WHERE singleton_id=1 AND owner_id=?",
                    (owner_id,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def retry_job(self, job_uuid: str, user_id: int | None = None) -> str | None:
        """Atomically create at most one direct retry for a terminal job.

        The former read-then-submit sequence allowed two concurrent API calls to
        enqueue duplicate children.  ``BEGIN IMMEDIATE`` also fences retries
        across multiple application processes sharing the SQLite queue.
        """
        new_job_uuid = uuid.uuid4().hex
        queued: dict[str, Any] | None = None
        runtime_identity_json = _runtime_code_evidence_json()
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                source_row = conn.execute(
                    "SELECT * FROM jobs WHERE job_uuid = ?",
                    (job_uuid,),
                ).fetchone()
                if source_row is None or source_row["status"] not in (
                    "failed",
                    "cancelled",
                ):
                    conn.rollback()
                    return None

                existing_child = conn.execute(
                    """
                    SELECT job_uuid FROM jobs
                    WHERE parent_job_uuid = ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (job_uuid,),
                ).fetchone()
                if existing_child is not None:
                    conn.rollback()
                    return None

                source = _decode_json_fields(source_row)
                params = source.get("params") or {}
                priority = _job_priority(str(source["job_type"]), params)
                queue_group = _queue_group(
                    str(source["job_type"]), params, new_job_uuid
                )
                pending_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM jobs
                        WHERE status IN ('pending', 'running', 'cancel_requested')
                        """
                    ).fetchone()[0]
                )
                if (
                    pending_count
                    >= max(int(settings.JOB_SCHEDULER_MAX_PENDING_JOBS), 1)
                    and priority < 100
                ):
                    raise JobQueueFullError(
                        "任务队列已达到背压上限，请等待现有任务完成后重试"
                    )
                resolved_user_id = (
                    source.get("user_id") if user_id is None else user_id
                )
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_type, display_name, params, status, progress,
                        progress_message, current_stage, resource_type, resource_id,
                        parent_job_uuid, attempt, priority, queue_group,
                        user_id, job_uuid, runtime_code_identity, updated_at
                    )
                    VALUES (?, ?, ?, 'pending', 0, '等待执行', 'queued', ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        source["job_type"],
                        source.get("display_name"),
                        json.dumps(params, ensure_ascii=False),
                        source.get("resource_type"),
                        source.get("resource_id"),
                        job_uuid,
                        int(source.get("attempt") or 1) + 1,
                        priority,
                        queue_group,
                        resolved_user_id,
                        new_job_uuid,
                        runtime_identity_json,
                    ),
                )
                _reconcile_backtest_experiment(
                    conn,
                    source,
                    status="pending",
                    message="重试已排队",
                )
                conn.execute(
                    """
                    INSERT INTO job_events
                        (job_uuid, status, progress, stage, message)
                    VALUES (?, 'pending', 0, 'queued', '任务重试已提交')
                    """,
                    (new_job_uuid,),
                )
                conn.commit()
                queued = {
                    "job_uuid": new_job_uuid,
                    "job_type": source["job_type"],
                    "params": params,
                    "user_id": resolved_user_id,
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        if queued is None:
            return None
        self._wake_event.set()
        await self._emit_change(await self.get_job_status(new_job_uuid))
        return new_job_uuid

    async def require_completed_job(
        self,
        job_uuid: str,
        *,
        expected_type: str | None = None,
    ) -> dict[str, Any]:
        """Return a completed dependency or fail before dependent side effects."""
        job = await self.get_job_status(job_uuid)
        if job is None:
            raise RuntimeError(f"required job does not exist: {job_uuid}")
        if expected_type is not None and job.get("job_type") != expected_type:
            raise RuntimeError(
                f"required job {job_uuid} has type {job.get('job_type')}, "
                f"expected {expected_type}"
            )
        if job.get("status") != "completed":
            raise RuntimeError(
                f"required job {job_uuid} is {job.get('status')}, expected completed"
            )
        return job

    async def get_latest_job(
        self, job_type: str, user_id: int | None = None
    ) -> Optional[dict[str, Any]]:
        conn = self._get_conn()
        if user_id is None:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_type = ? ORDER BY id DESC LIMIT 1",
                (job_type,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM jobs WHERE job_type = ? AND user_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (job_type, user_id),
            ).fetchone()
        conn.close()
        return _decode_json_fields(row) if row is not None else None

    async def get_latest_system_job(self, job_type: str) -> Optional[dict[str, Any]]:
        """Return the latest unattended job without exposing another user."""

        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM jobs
            WHERE job_type=? AND user_id IS NULL
            ORDER BY id DESC LIMIT 1""",
            (job_type,),
        ).fetchone()
        conn.close()
        return _decode_json_fields(row) if row is not None else None

    async def get_summary(
        self, *, user_id: int | None = None, include_all: bool = False
    ) -> dict[str, Any]:
        where = "" if include_all else " WHERE user_id = ?"
        values: list[Any] = [] if include_all else [user_id]
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT status, COUNT(*) AS count FROM jobs{where} GROUP BY status", values
        ).fetchall()
        conn.close()
        counts = {status: 0 for status in VALID_STATUSES}
        counts.update({row["status"]: int(row["count"]) for row in rows})
        active = counts["pending"] + counts["running"] + counts["cancel_requested"]
        return {
            "counts": counts,
            "active": active,
            "worker": self.worker_health_snapshot(),
        }

    async def get_observability(self, *, window_hours: int = 24) -> dict[str, Any]:
        """Return bounded, aggregate operational telemetry for administrators."""
        hours = max(
            min(
                int(window_hours),
                max(int(settings.JOB_OBSERVABILITY_RETENTION_HOURS), 1),
            ),
            1,
        )
        return await asyncio.to_thread(self._get_observability_sync, hours)

    async def evaluate_slo_alerts(self, *, window_hours: int | None = None) -> None:
        """Evaluate fixed SLOs and persist debounced state transitions.

        The scheduler calls this maintenance method.  The read-only
        observability endpoint never creates alert state as a side effect.
        """
        hours = max(
            min(
                int(window_hours or settings.JOB_SLO_WINDOW_HOURS),
                max(int(settings.JOB_OBSERVABILITY_RETENTION_HOURS), 1),
            ),
            1,
        )
        await asyncio.to_thread(self._evaluate_slo_alerts_sync, hours)
        # Outbox work occurs only after the SLO transaction commits.  It is
        # bounded, disabled by default, and converts network failures to
        # durable retry state instead of delaying the scheduler loop.
        await asyncio.to_thread(process_alert_delivery_outbox, str(self._db_path))

    def _get_observability_sync(self, hours: int) -> dict[str, Any]:
        since_modifier = f"-{hours} hours"
        conn = self._get_conn()
        jobs = conn.execute(
            """
            SELECT job_type, status, created_at, started_at, completed_at,
                   current_stage, progress
            FROM jobs
            WHERE julianday(created_at) >= julianday('now', ?)
            ORDER BY id DESC LIMIT 10000
            """,
            (since_modifier,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT event_name, category, job_type, outcome, stage,
                   SUM(value) AS value
            FROM operational_events
            WHERE julianday(created_at) >= julianday('now', ?)
            GROUP BY event_name, category, job_type, outcome, stage
            ORDER BY event_name, job_type, outcome, stage
            """,
            (since_modifier,),
        ).fetchall()
        data_refreshes = conn.execute(
            """
            SELECT status, current_stage, progress, progress_message, updated_at
            FROM jobs
            WHERE job_type='data_update'
              AND julianday(created_at) >= julianday('now', ?)
            ORDER BY id DESC LIMIT 20
            """,
            (since_modifier,),
        ).fetchall()
        alert_states = conn.execute(
            """
            SELECT objective, status, pending_status, consecutive_observations,
                   last_transition_at, updated_at
            FROM slo_alert_state
            ORDER BY objective
            """
        ).fetchall()
        alert_events = conn.execute(
            """
            SELECT objective, transition, actual, threshold, window_hours,
                   notification_emitted, created_at
            FROM slo_alert_events
            ORDER BY id DESC LIMIT 20
            """
        ).fetchall()
        delivery_summary = alert_delivery_summary(conn)
        conn.close()

        by_type: dict[str, dict[str, Any]] = {}
        queue_waits: list[float] = []
        run_durations: list[float] = []
        for row in jobs:
            job_type = (
                str(row["job_type"])
                if row["job_type"] in JOB_TYPE_METADATA
                else "other"
            )
            item = by_type.setdefault(
                job_type,
                {
                    "submitted": 0,
                    "completed": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "active": 0,
                    "_queue_waits": [],
                    "_run_durations": [],
                },
            )
            item["submitted"] += 1
            status = str(row["status"])
            if status in TERMINAL_STATUSES:
                item[status] += 1
            else:
                item["active"] += 1
            created = _parse_db_time(row["created_at"])
            started = _parse_db_time(row["started_at"])
            completed = _parse_db_time(row["completed_at"])
            if created is not None and started is not None:
                wait = max((started - created).total_seconds(), 0.0)
                item["_queue_waits"].append(wait)
                queue_waits.append(wait)
            if started is not None and completed is not None:
                duration = max((completed - started).total_seconds(), 0.0)
                item["_run_durations"].append(duration)
                run_durations.append(duration)

        for item in by_type.values():
            terminal = item["completed"] + item["failed"] + item["cancelled"]
            item["success_rate"] = (
                round(item["completed"] / terminal, 4) if terminal else None
            )
            item["failure_rate"] = (
                round(item["failed"] / terminal, 4) if terminal else None
            )
            item["cancel_rate"] = (
                round(item["cancelled"] / terminal, 4) if terminal else None
            )
            waits = item.pop("_queue_waits")
            durations = item.pop("_run_durations")
            item["queue_wait_seconds"] = {
                "p50": _percentile(waits, 0.5),
                "p95": _percentile(waits, 0.95),
            }
            item["run_duration_seconds"] = {
                "p50": _percentile(durations, 0.5),
                "p95": _percentile(durations, 0.95),
            }

        event_rows = [
            {
                "event_name": row["event_name"],
                "category": row["category"],
                "job_type": row["job_type"],
                "outcome": row["outcome"],
                "stage": row["stage"],
                "value": round(float(row["value"] or 0), 3),
            }
            for row in events
        ]
        sqlite_contention = sum(
            float(item["value"])
            for item in event_rows
            if item["event_name"] == "sqlite_contention"
        ) + self._pending_sqlite_contention
        service_restarts = sum(
            float(item["value"])
            for item in event_rows
            if item["event_name"] == "service_start"
        )
        terminal_jobs = sum(
            item["completed"] + item["failed"] + item["cancelled"]
            for item in by_type.values()
        )
        completed_jobs = sum(item["completed"] for item in by_type.values())
        success_rate = completed_jobs / terminal_jobs if terminal_jobs else None
        queue_p95 = _percentile(queue_waits, 0.95)
        duration_p95 = _percentile(run_durations, 0.95)
        objectives = {
            "job_success_rate": {
                "target": 0.95,
                "actual": round(success_rate, 4) if success_rate is not None else None,
                "minimum_samples": 5,
                "sample_count": terminal_jobs,
                "met": (
                    success_rate >= 0.95
                    if success_rate is not None and terminal_jobs >= 5
                    else None
                ),
            },
            "queue_wait_p95_seconds": {
                "target_max": 300,
                "actual": queue_p95,
                "met": queue_p95 <= 300 if queue_p95 is not None else None,
            },
            "sqlite_contention_events": {
                "target_max": 5,
                "actual": int(sqlite_contention),
                "met": sqlite_contention <= 5,
            },
            "service_starts": {
                "target_max": 2,
                "actual": int(service_restarts),
                "met": service_restarts <= 2,
            },
        }
        return {
            "schema_version": "operations-observability/v1",
            "window_hours": hours,
            "retention_hours": max(
                int(settings.JOB_OBSERVABILITY_RETENTION_HOURS), 1
            ),
            "labels": {
                "bounded": True,
                "allowed": [
                    "event_name",
                    "category",
                    "job_type",
                    "outcome",
                    "stage",
                ],
                "prohibited": ["user", "path", "token", "job_uuid"],
            },
            "jobs": {
                "sample_count": len(jobs),
                "by_type": by_type,
                "queue_wait_seconds": {
                    "p50": _percentile(queue_waits, 0.5),
                    "p95": queue_p95,
                },
                "run_duration_seconds": {
                    "p50": _percentile(run_durations, 0.5),
                    "p95": duration_p95,
                },
            },
            "data_refresh": {
                "recent": [
                    {
                        "status": row["status"],
                        "stage": _bounded_label(
                            row["current_stage"],
                            _BOUNDED_STAGES,
                            "other",
                        ),
                        "progress": round(float(row["progress"] or 0), 4),
                        "message": str(row["progress_message"] or "")[:160],
                        "updated_at": row["updated_at"],
                    }
                    for row in data_refreshes
                ]
            },
            "events": event_rows,
            "slo": {
                "schema_version": "operations-slo/v1",
                "objectives": objectives,
                "alerting": {
                    "confirmations_required": max(
                        int(settings.JOB_SLO_CONFIRMATIONS_REQUIRED), 1
                    ),
                    "cooldown_seconds": max(
                        int(settings.JOB_SLO_ALERT_COOLDOWN_SECONDS), 0
                    ),
                    "evaluation_interval_seconds": max(
                        int(settings.JOB_SLO_EVALUATION_SECONDS), 5
                    ),
                    "states": {
                        str(row["objective"]): {
                            "status": row["status"],
                            "pending_status": row["pending_status"],
                            "consecutive_observations": int(
                                row["consecutive_observations"] or 0
                            ),
                            "last_transition_at": row["last_transition_at"],
                            "updated_at": row["updated_at"],
                        }
                        for row in alert_states
                        if row["objective"] in _SLO_OBJECTIVE_NAMES
                    },
                    "recent": [
                        {
                            "objective": row["objective"],
                            "transition": row["transition"],
                            "actual": (
                                round(float(row["actual"]), 4)
                                if row["actual"] is not None
                                else None
                            ),
                            "threshold": round(float(row["threshold"]), 4),
                            "window_hours": int(row["window_hours"]),
                            "notification_emitted": bool(
                                row["notification_emitted"]
                            ),
                            "created_at": row["created_at"],
                        }
                        for row in alert_events
                        if row["objective"] in _SLO_OBJECTIVE_NAMES
                        and row["transition"] in _SLO_TRANSITIONS
                    ],
                    "external_delivery": delivery_summary,
                },
            },
            "worker": self.worker_health_snapshot(),
        }

    def _evaluate_slo_alerts_sync(self, hours: int) -> None:
        payload = self._get_observability_sync(hours)
        objectives = payload["slo"]["objectives"]
        confirmations = max(int(settings.JOB_SLO_CONFIRMATIONS_REQUIRED), 1)
        cooldown_seconds = max(int(settings.JOB_SLO_ALERT_COOLDOWN_SECONDS), 0)
        transitions_to_log: list[dict[str, Any]] = []
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for objective in sorted(_SLO_OBJECTIVE_NAMES):
                metric = objectives.get(objective)
                if not isinstance(metric, dict):
                    continue
                met = metric.get("met")
                if met is None:
                    conn.execute(
                        """
                        UPDATE slo_alert_state
                        SET pending_status=NULL, consecutive_observations=0,
                            updated_at=datetime('now')
                        WHERE objective=?
                        """,
                        (objective,),
                    )
                    continue
                desired = "healthy" if bool(met) else "breaching"
                threshold_raw = (
                    metric.get("target")
                    if metric.get("target") is not None
                    else metric.get("target_max")
                )
                if threshold_raw is None:
                    continue
                threshold = float(threshold_raw)
                actual = (
                    float(metric["actual"])
                    if metric.get("actual") is not None
                    else None
                )
                row = conn.execute(
                    "SELECT * FROM slo_alert_state WHERE objective=?",
                    (objective,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO slo_alert_state
                            (objective, status, updated_at)
                        VALUES (?, 'healthy', datetime('now'))
                        """,
                        (objective,),
                    )
                    if desired == "healthy":
                        continue
                    row = conn.execute(
                        "SELECT * FROM slo_alert_state WHERE objective=?",
                        (objective,),
                    ).fetchone()
                    if row is None:  # Defensive; the insert is in this transaction.
                        raise RuntimeError("failed to initialize SLO alert state")

                current = (
                    str(row["status"])
                    if row["status"] in _SLO_STATES
                    else "healthy"
                )
                if current == desired:
                    conn.execute(
                        """
                        UPDATE slo_alert_state
                        SET pending_status=NULL, consecutive_observations=0,
                            updated_at=datetime('now')
                        WHERE objective=?
                        """,
                        (objective,),
                    )
                    continue
                pending = str(row["pending_status"] or "")
                count = (
                    int(row["consecutive_observations"] or 0) + 1
                    if pending == desired
                    else 1
                )
                if count < confirmations:
                    conn.execute(
                        """
                        UPDATE slo_alert_state
                        SET pending_status=?, consecutive_observations=?,
                            updated_at=datetime('now')
                        WHERE objective=?
                        """,
                        (desired, count, objective),
                    )
                    continue

                transition = "breach" if desired == "breaching" else "recovery"
                notified_column = (
                    "last_breach_notified_at"
                    if transition == "breach"
                    else "last_recovery_notified_at"
                )
                previous_notified = _parse_db_time(row[notified_column])
                now = datetime.now(timezone.utc)
                notification_emitted = (
                    previous_notified is None
                    or (now - previous_notified).total_seconds() >= cooldown_seconds
                )
                conn.execute(
                    f"""
                    UPDATE slo_alert_state
                    SET status=?, pending_status=NULL,
                        consecutive_observations=0,
                        last_transition_at=datetime('now'),
                        {notified_column}=CASE
                            WHEN ? THEN datetime('now') ELSE {notified_column}
                        END,
                        updated_at=datetime('now')
                    WHERE objective=?
                    """,
                    (desired, int(notification_emitted), objective),
                )
                event_cursor = conn.execute(
                    """
                    INSERT INTO slo_alert_events
                        (objective, transition, actual, threshold, window_hours,
                         notification_emitted)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        objective,
                        transition,
                        actual,
                        threshold,
                        hours,
                        int(notification_emitted),
                    ),
                )
                if notification_emitted:
                    queue_slo_alert_delivery(
                        conn,
                        alert_event_id=int(event_cursor.lastrowid),
                        objective=objective,
                        transition=transition,
                        actual=actual,
                        threshold=threshold,
                        window_hours=hours,
                    )
                transitions_to_log.append(
                    {
                        "objective": objective,
                        "transition": transition,
                        "actual": actual,
                        "threshold": threshold,
                        "notification_emitted": notification_emitted,
                    }
                )
            retention = max(
                min(int(settings.JOB_OBSERVABILITY_RETENTION_HOURS), 24 * 31),
                1,
            )
            conn.execute(
                "DELETE FROM slo_alert_events "
                "WHERE julianday(created_at) < julianday('now', ?)",
                (f"-{retention} hours",),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        for transition in transitions_to_log:
            if not transition["notification_emitted"]:
                continue
            structured_log(
                logger,
                logging.WARNING
                if transition["transition"] == "breach"
                else logging.INFO,
                "slo_alert",
                component="job_broker",
                reason=transition["objective"],
                outcome=transition["transition"],
                actual=transition["actual"],
                threshold=transition["threshold"],
                window_hours=hours,
            )
    async def acknowledge_slo_alert_delivery(self, delivery_id: str) -> bool:
        """Record an administrator acknowledgement for a delivered breach."""
        return await asyncio.to_thread(
            acknowledge_alert_delivery,
            str(self._db_path),
            delivery_id,
        )

    def worker_health_snapshot(self) -> dict[str, Any]:
        """Return in-memory worker freshness without touching SQLite."""
        worker_online = (
            self._worker_heartbeat_monotonic is not None
            and time.monotonic() - self._worker_heartbeat_monotonic
            < max(float(settings.JOB_SCHEDULER_LEASE_SECONDS), 15.0)
        )
        return {
            "online": worker_online,
            "capacity": self._scheduler_status["desired_capacity"],
            "started_at": self._worker_started_at,
            "heartbeat_at": self._worker_heartbeat_at,
            **self._scheduler_status,
        }

    def set_scheduler_status(self, **status: Any) -> None:
        self._scheduler_status.update(status)

    def mark_worker_started(self) -> None:
        self._worker_started_at = _utc_now()
        self.mark_worker_heartbeat()

    def mark_worker_heartbeat(self) -> None:
        self._worker_heartbeat_at = _utc_now()
        self._worker_heartbeat_monotonic = time.monotonic()

    def mark_worker_stopped(self) -> None:
        self._worker_heartbeat_monotonic = None
        self._scheduler_status.update(
            {
                "desired_capacity": 0,
                "leader": False,
                "running_slots": 0,
                "degraded": True,
                "reasons": ["worker_stopped"],
            }
        )

    async def shutdown(self) -> None:
        self.mark_worker_stopped()
        self._wake_event.clear()
        logger.info("Job broker shutdown complete")


_broker_instance: JobBroker | None = None


def get_broker() -> JobBroker:
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = JobBroker()
    return _broker_instance
