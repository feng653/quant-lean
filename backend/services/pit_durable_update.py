"""Durable, policy-gated orchestration for unattended PIT updates.

The state ledger is deliberately independent from experiments and paper
portfolios.  Each transition is committed before the next side effect and all
effectful adapter calls receive a stable idempotency key.  The adapter remains
responsible for the existing evidence/governance checks; this module never
manufactures an administrator or an approval attestation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Literal, Protocol, TypeVar

from backend.config import settings
from backend.core.security_boundaries import sanitize_diagnostic

AUTOMATION_SCHEMA_VERSION = "pit-durable-update/v1"
POLICY_SCHEMA_VERSION = "pit-green-auto-activation-policy/v2"
# Green auto-activation is a production boundary.  The durable scheduler must
# never promote legacy imports, even if a caller constructs a permissive-looking
# policy object.  Legacy v1 remains readable for audit/recovery only.
PIT_BITEMPORAL_IMPORT_SCHEMA = "point-in-time-master-import/v2"
STAGES = (
    "collect",
    "validate",
    "classify",
    "import",
    "activate",
    "canary",
    "monitor",
)
TERMINAL_STATUSES = {"completed", "hold", "quarantined"}
_T = TypeVar("_T")


class PitAutomationError(RuntimeError):
    """Base error for the durable PIT updater."""


class PitAutomationIdentityError(PitAutomationError):
    """Configured unattended actor is absent or is not a service identity."""


class PitAutomationLeaseError(PitAutomationError):
    """Another worker currently owns the state-machine lease."""


class PitAutomationPolicyError(PitAutomationError):
    """A candidate attempted to cross a policy boundary."""


class PitAutomationStageTimeout(PitAutomationError):
    """One effectful stage exceeded its bounded execution deadline."""

    def __init__(self, *, stage: str, timeout_seconds: float) -> None:
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"PIT automation stage {stage!r} exceeded its "
            f"{timeout_seconds:g}-second timeout"
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class PitAutomationPolicy:
    """Explicit local pre-authorization; disabled defaults fail closed."""

    personal_mode: bool = False
    auto_activate_green: bool = False
    allowed_provider: str = "csindex_official"
    allowed_import_schema: str = PIT_BITEMPORAL_IMPORT_SCHEMA

    def __post_init__(self) -> None:
        if self.allowed_import_schema != PIT_BITEMPORAL_IMPORT_SCHEMA:
            raise PitAutomationPolicyError(
                "unattended PIT automation only accepts the bitemporal v2 import schema"
            )

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "personal_mode": self.personal_mode,
            "auto_activate_green": self.auto_activate_green,
            "allowed_provider": self.allowed_provider,
            "allowed_import_schema": self.allowed_import_schema,
            "required_classification": "green",
            "required_change": "append_only",
            "require_same_source": True,
            "require_same_schema": True,
            "require_validated_evidence": True,
        }

    @property
    def policy_sha256(self) -> str:
        return _digest(self.document())

    def permits(self, classification: dict[str, Any]) -> bool:
        return bool(
            self.personal_mode
            and self.auto_activate_green
            and classification.get("classification") == "green"
            and classification.get("same_source") is True
            and classification.get("same_schema") is True
            and classification.get("append_only") is True
            and classification.get("evidence_valid") is True
            and classification.get("provider") == self.allowed_provider
            # Legacy data may be retained for historical reads, but it cannot
            # cross the unattended import/activation boundary.
            and classification.get("import_schema") == PIT_BITEMPORAL_IMPORT_SCHEMA
        )


class PitAutomationActions(Protocol):
    async def collect(self, *, idempotency_key: str, actor_user_id: int) -> dict[str, Any]: ...

    async def validate(
        self, candidate: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def classify(
        self, validated: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def import_candidate(
        self,
        validated: dict[str, Any],
        classification: dict[str, Any],
        *,
        idempotency_key: str,
        actor_user_id: int,
        policy_sha256: str,
    ) -> dict[str, Any]: ...

    async def activate(
        self,
        imported: dict[str, Any],
        *,
        idempotency_key: str,
        actor_user_id: int,
        policy_sha256: str,
    ) -> dict[str, Any]: ...

    async def canary(
        self, activated: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def monitor(
        self, activated: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...


def require_automation_service_identity(
    *,
    user_id: int | None = None,
    username: str | None = None,
    users_db: Path | None = None,
) -> dict[str, Any]:
    """Resolve an active, non-admin service account with only explicit grants."""

    expected_id = int(settings.PIT_AUTOMATION_SERVICE_USER_ID if user_id is None else user_id)
    expected_name = str(
        settings.PIT_AUTOMATION_SERVICE_USERNAME if username is None else username
    ).strip()
    if expected_id < 1 or not expected_name:
        raise PitAutomationIdentityError("PIT automation service identity is not configured")
    path = users_db or settings.abs_path(settings.USERS_DB)
    if not path.exists() or path.is_symlink():
        raise PitAutomationIdentityError("PIT automation service identity is unavailable")
    try:
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT id, username, is_admin, is_active FROM users WHERE id=?",
                (expected_id,),
            ).fetchone()
            permissions = {
                str(item[0])
                for item in connection.execute(
                    "SELECT permission FROM user_permissions WHERE user_id=?",
                    (expected_id,),
                ).fetchall()
            }
    except sqlite3.Error as exc:
        raise PitAutomationIdentityError(
            "PIT automation service identity cannot be verified"
        ) from exc
    if (
        row is None
        or str(row["username"]) != expected_name
        or not bool(row["is_active"])
        or bool(row["is_admin"])
        or "data:update" not in permissions
    ):
        raise PitAutomationIdentityError(
            "PIT automation requires the configured active non-admin service identity"
        )
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "is_admin": False,
        "permissions": sorted(permissions),
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pit_automation_runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_user_id INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    candidate_json TEXT,
    validated_json TEXT,
    classification_json TEXT,
    import_json TEXT,
    activation_json TEXT,
    canary_json TEXT,
    monitor_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error TEXT,
    next_retry_at TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pit_automation_due
ON pit_automation_runs(status, next_retry_at, updated_at);
CREATE TABLE IF NOT EXISTS pit_automation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES pit_automation_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS pit_automation_events_no_update
BEFORE UPDATE ON pit_automation_events BEGIN
    SELECT RAISE(ABORT, 'PIT automation events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS pit_automation_events_no_delete
BEFORE DELETE ON pit_automation_events BEGIN
    SELECT RAISE(ABORT, 'PIT automation events are append-only');
END;
"""


class PitDurableUpdateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.abs_path(settings.PIT_EVIDENCE_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in (
            "policy_json",
            "candidate_json",
            "validated_json",
            "classification_json",
            "import_json",
            "activation_json",
            "canary_json",
            "monitor_json",
        ):
            value = result.pop(key, None)
            result[key.removesuffix("_json")] = json.loads(value) if value else None
        result["schema_version"] = AUTOMATION_SCHEMA_VERSION
        return result

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        stage: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        encoded = _json(payload)
        connection.execute(
            "INSERT INTO pit_automation_events "
            "(run_id, stage, event_type, event_json, event_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                stage,
                event_type,
                encoded,
                hashlib.sha256(encoded.encode()).hexdigest(),
                _iso(_now()),
            ),
        )

    def ensure_run(
        self,
        *,
        idempotency_key: str,
        actor_user_id: int,
        policy: PitAutomationPolicy,
    ) -> dict[str, Any]:
        if not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("PIT automation idempotency key is invalid")
        now = _iso(_now())
        run_id = "pitauto_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
        policy_json = _json(policy.document())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pit_automation_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO pit_automation_runs
                    (run_id, idempotency_key, stage, status, actor_user_id,
                     policy_json, policy_sha256, created_at, updated_at)
                    VALUES (?, ?, 'collect', 'pending', ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        idempotency_key,
                        actor_user_id,
                        policy_json,
                        policy.policy_sha256,
                        now,
                        now,
                    ),
                )
                self._event(
                    connection,
                    run_id=run_id,
                    stage="collect",
                    event_type="run_created",
                    payload={"actor_user_id": actor_user_id, "policy_sha256": policy.policy_sha256},
                )
            elif (
                int(existing["actor_user_id"]) != actor_user_id
                or str(existing["policy_sha256"]) != policy.policy_sha256
                or str(existing["policy_json"]) != policy_json
            ):
                raise PitAutomationPolicyError(
                    "idempotency key is already bound to another actor or policy"
                )
            row = connection.execute(
                "SELECT * FROM pit_automation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._decode(row)

    def acquire(self, run_id: str, owner: str, lease_seconds: int) -> int:
        now = _now()
        expires = _iso(now + timedelta(seconds=max(lease_seconds, 5)))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_running_locked(connection, now=now)
            cursor = connection.execute(
                """UPDATE pit_automation_runs
                SET lease_owner=?, lease_generation=lease_generation+1,
                    lease_expires_at=?, status='running', updated_at=?
                WHERE run_id=? AND status NOT IN ('completed','hold','quarantined')
                  AND (lease_owner IS NULL OR lease_owner=? OR lease_expires_at<=?)""",
                (owner, expires, _iso(now), run_id, owner, _iso(now)),
            )
            if cursor.rowcount != 1:
                raise PitAutomationLeaseError("PIT automation run lease is held")
            generation = int(
                connection.execute(
                    "SELECT lease_generation FROM pit_automation_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            connection.commit()
        return generation

    def _recover_expired_running_locked(
        self, connection: sqlite3.Connection, *, now: datetime
    ) -> list[str]:
        """Make crashed workers' expired stages scheduler-retryable.

        A process can die between an external effect and ``commit_stage``.
        Leaving its row ``running`` would hide it from the scheduler forever;
        instead we retain the stage and idempotency key, release only the
        expired lease, and record an auditable immediate retry.  Re-running
        the stage is safe because the adapter receives that stable key.

        The caller must hold ``BEGIN IMMEDIATE`` so a live worker cannot be
        recovered while it renews its lease.
        """

        now_iso = _iso(now)
        rows = connection.execute(
            """SELECT run_id, idempotency_key, stage, attempt_count, lease_expires_at
            FROM pit_automation_runs
            WHERE status='running' AND lease_expires_at IS NOT NULL
              AND lease_expires_at<=?
            ORDER BY updated_at, created_at""",
            (now_iso,),
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            attempt = int(row["attempt_count"]) + 1
            cursor = connection.execute(
                """UPDATE pit_automation_runs
                SET status='retry_wait', attempt_count=?,
                    last_error_code='lease_expired_recovered',
                    last_error='PIT automation worker lease expired before stage commit',
                    next_retry_at=?, lease_owner=NULL, lease_expires_at=NULL,
                    updated_at=?
                WHERE run_id=? AND status='running' AND lease_expires_at=?""",
                (
                    attempt,
                    now_iso,
                    now_iso,
                    str(row["run_id"]),
                    str(row["lease_expires_at"]),
                ),
            )
            if cursor.rowcount != 1:
                continue
            self._event(
                connection,
                run_id=str(row["run_id"]),
                stage=str(row["stage"]),
                event_type="lease_expired_recovered",
                payload={
                    "attempt": attempt,
                    "retry_at": now_iso,
                    "expired_lease_at": str(row["lease_expires_at"]),
                    "error_code": "lease_expired_recovered",
                },
            )
            recovered.append(str(row["idempotency_key"]))
        return recovered

    def recover_expired_running(self) -> list[str]:
        """Persistently convert stale running leases into due retries."""

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovered = self._recover_expired_running_locked(connection, now=now)
            connection.commit()
        return recovered

    def get(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pit_automation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._decode(row)

    def renew(
        self,
        *,
        run_id: str,
        owner: str,
        generation: int,
        lease_seconds: int,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE pit_automation_runs SET lease_expires_at=?, updated_at=?
                WHERE run_id=? AND lease_owner=? AND lease_generation=?
                  AND status='running'""",
                (
                    _iso(now + timedelta(seconds=max(lease_seconds, 5))),
                    _iso(now),
                    run_id,
                    owner,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                raise PitAutomationLeaseError("PIT automation lease changed")

    def latest(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pit_automation_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def due_retry_keys(self, limit: int = 20) -> list[str]:
        """Recover stale leases and return retryable cycles without a day skip."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_running_locked(connection, now=_now())
            rows = connection.execute(
                """SELECT idempotency_key FROM pit_automation_runs
                WHERE status='retry_wait' AND next_retry_at<=?
                ORDER BY next_retry_at, created_at LIMIT ?""",
                (_iso(_now()), max(1, min(limit, 100))),
            ).fetchall()
            connection.commit()
        return [str(row[0]) for row in rows]

    def commit_stage(
        self,
        *,
        run_id: str,
        owner: str,
        generation: int,
        current_stage: str,
        next_stage: str,
        result_column: str,
        result: dict[str, Any],
    ) -> None:
        if current_stage not in STAGES or next_stage not in (*STAGES, "completed"):
            raise ValueError("invalid PIT automation transition")
        if result_column not in {
            "candidate_json",
            "validated_json",
            "classification_json",
            "import_json",
            "activation_json",
            "canary_json",
            "monitor_json",
        }:
            raise ValueError("invalid PIT automation result column")
        now = _iso(_now())
        completed = next_stage == "completed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""UPDATE pit_automation_runs SET {result_column}=?, stage=?,
                status=?, attempt_count=0, last_error_code=NULL, last_error=NULL,
                next_retry_at=NULL, updated_at=?, completed_at=?,
                lease_owner=CASE WHEN ? THEN NULL ELSE lease_owner END,
                lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END
                WHERE run_id=? AND stage=? AND lease_owner=? AND lease_generation=?""",
                (
                    _json(result),
                    next_stage,
                    "completed" if completed else "running",
                    now,
                    now if completed else None,
                    completed,
                    completed,
                    run_id,
                    current_stage,
                    owner,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                raise PitAutomationLeaseError("PIT automation lease or stage changed")
            self._event(
                connection,
                run_id=run_id,
                stage=current_stage,
                event_type="stage_completed",
                payload={"next_stage": next_stage, "result_sha256": _digest(result)},
            )
            connection.commit()

    def terminal(
        self,
        *,
        run_id: str,
        owner: str,
        generation: int,
        status: Literal["hold", "quarantined"],
        reason: str,
        classification: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE pit_automation_runs SET status=?, classification_json=?, last_error_code=?,
                last_error=?, next_retry_at=NULL, lease_owner=NULL,
                lease_expires_at=NULL, updated_at=?, completed_at=?
                WHERE run_id=? AND lease_owner=? AND lease_generation=?""",
                (
                    status,
                    _json(classification),
                    f"classification_{status}",
                    sanitize_diagnostic(reason, max_length=500),
                    _iso(_now()),
                    _iso(_now()),
                    run_id,
                    owner,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                raise PitAutomationLeaseError("PIT automation lease changed")
            self._event(
                connection,
                run_id=run_id,
                stage="classify",
                event_type=status,
                payload={"reason": sanitize_diagnostic(reason, max_length=500)},
            )
            connection.commit()

    def retry(
        self,
        *,
        run_id: str,
        owner: str,
        generation: int,
        stage: str,
        error: BaseException,
        base_seconds: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt_count FROM pit_automation_runs WHERE run_id=? AND lease_owner=? AND lease_generation=?",
                (run_id, owner, generation),
            ).fetchone()
            if row is None:
                raise PitAutomationLeaseError("PIT automation lease changed")
            attempt = int(row[0]) + 1
            delay = min(max(base_seconds, 1) * (2 ** min(attempt - 1, 8)), 3600)
            retry_at = _iso(_now() + timedelta(seconds=delay))
            timeout_seconds: float | None = None
            if isinstance(error, PitAutomationStageTimeout):
                code = f"{stage}_timeout"
                timeout_seconds = error.timeout_seconds
            else:
                code = f"{stage}_{type(error).__name__}"[:120]
            detail = sanitize_diagnostic(str(error), max_length=500)
            connection.execute(
                """UPDATE pit_automation_runs SET status='retry_wait',
                attempt_count=?, last_error_code=?, last_error=?, next_retry_at=?,
                lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                WHERE run_id=? AND lease_owner=? AND lease_generation=?""",
                (attempt, code, detail, retry_at, _iso(_now()), run_id, owner, generation),
            )
            self._event(
                connection,
                run_id=run_id,
                stage=stage,
                event_type="retry_scheduled",
                payload={
                    "attempt": attempt,
                    "retry_at": retry_at,
                    "error_code": code,
                    **(
                        {"timeout_seconds": timeout_seconds}
                        if timeout_seconds is not None
                        else {}
                    ),
                },
            )
            connection.commit()
        return self.get(run_id)


class PitDurableUpdateMachine:
    def __init__(
        self,
        *,
        store: PitDurableUpdateStore,
        actions: PitAutomationActions,
        policy: PitAutomationPolicy,
        actor_user_id: int,
        lease_seconds: int = 120,
        stage_timeout_seconds: float = 300,
        retry_base_seconds: int = 30,
    ) -> None:
        self.store = store
        self.actions = actions
        self.policy = policy
        self.actor_user_id = actor_user_id
        self.lease_seconds = lease_seconds
        self.stage_timeout_seconds = float(stage_timeout_seconds)
        if self.stage_timeout_seconds <= 0:
            raise ValueError("PIT automation stage timeout must be positive")
        self.retry_base_seconds = retry_base_seconds

    async def _invoke_with_lease(
        self,
        awaitable: Awaitable[_T],
        *,
        run_id: str,
        owner: str,
        generation: int,
        stage: str,
    ) -> _T:
        """Keep ownership alive during slow network/import adapter calls."""

        task = asyncio.ensure_future(awaitable)

        def _observe_terminal_effect(completed: asyncio.Future[_T]) -> None:
            """Mark a task exception retrieved even when it stops the event loop.

            ``KeyboardInterrupt`` and ``SystemExit`` raised by an adapter task
            can cause ``asyncio.run`` to begin shutdown before the awaiting
            state-machine task gets another scheduling turn.  Observing the
            completed task here prevents a second, misleading "Task exception
            was never retrieved" report; it does not suppress the exception
            from the state machine.
            """

            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(_observe_terminal_effect)
        interval = max(min(self.lease_seconds / 3, 30), 0.01)
        deadline = asyncio.get_running_loop().time() + self.stage_timeout_seconds
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise PitAutomationStageTimeout(
                        stage=stage, timeout_seconds=self.stage_timeout_seconds
                    )
                done, _pending = await asyncio.wait(
                    {task}, timeout=min(interval, remaining)
                )
                if done:
                    return await task
                self.store.renew(
                    run_id=run_id,
                    owner=owner,
                    generation=generation,
                    lease_seconds=self.lease_seconds,
                )
        except BaseException:
            if not task.done():
                task.cancel()
                # Do not leave a cancellation-delivery race to event-loop
                # shutdown.  Only consume the expected cancellation from the
                # child; any non-cancellation BaseException remains visible.
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise

    async def run(self, idempotency_key: str) -> dict[str, Any]:
        run = self.store.ensure_run(
            idempotency_key=idempotency_key,
            actor_user_id=self.actor_user_id,
            policy=self.policy,
        )
        if run["status"] in TERMINAL_STATUSES:
            return run
        owner = f"pit-worker:{uuid.uuid4().hex}"
        generation = self.store.acquire(run["run_id"], owner, self.lease_seconds)
        while True:
            run = self.store.get(run["run_id"])
            stage = str(run["stage"])
            key = f"{run['idempotency_key']}:{stage}"
            try:
                if stage == "collect":
                    result = await self._invoke_with_lease(
                        self.actions.collect(
                            idempotency_key=key,
                            actor_user_id=self.actor_user_id,
                        ),
                        run_id=run["run_id"],
                        owner=owner,
                        generation=generation,
                        stage=stage,
                    )
                    column, next_stage = "candidate_json", "validate"
                elif stage == "validate":
                    result = await self._invoke_with_lease(
                        self.actions.validate(run["candidate"], idempotency_key=key),
                        run_id=run["run_id"],
                        owner=owner,
                        generation=generation,
                        stage=stage,
                    )
                    if result.get("evidence_valid") is not True:
                        raise PitAutomationPolicyError("candidate evidence did not validate")
                    column, next_stage = "validated_json", "classify"
                elif stage == "classify":
                    result = await self._invoke_with_lease(
                        self.actions.classify(run["validated"], idempotency_key=key),
                        run_id=run["run_id"],
                        owner=owner,
                        generation=generation,
                        stage=stage,
                    )
                    label = result.get("classification")
                    if label == "red":
                        self.store.terminal(
                            run_id=run["run_id"],
                            owner=owner,
                            generation=generation,
                            status="quarantined",
                            reason=str(result.get("reason") or "red candidate"),
                            classification=result,
                        )
                        return self.store.get(run["run_id"])
                    if label != "green" or not self.policy.permits(result):
                        self.store.terminal(
                            run_id=run["run_id"],
                            owner=owner,
                            generation=generation,
                            status="hold",
                            reason=str(result.get("reason") or "candidate is not pre-authorized"),
                            classification=result,
                        )
                        return self.store.get(run["run_id"])
                    column, next_stage = "classification_json", "import"
                elif stage == "import":
                    result = await self._invoke_with_lease(
                        self.actions.import_candidate(
                            run["validated"],
                            run["classification"],
                            idempotency_key=key,
                            actor_user_id=self.actor_user_id,
                            policy_sha256=self.policy.policy_sha256,
                        ),
                        run_id=run["run_id"],
                        owner=owner,
                        generation=generation,
                        stage=stage,
                    )
                    column, next_stage = "import_json", "activate"
                elif stage == "activate":
                    result = await self._invoke_with_lease(
                        self.actions.activate(
                            run["import"],
                            idempotency_key=key,
                            actor_user_id=self.actor_user_id,
                            policy_sha256=self.policy.policy_sha256,
                        ),
                        run_id=run["run_id"],
                        owner=owner,
                        generation=generation,
                        stage=stage,
                    )
                    if result.get("activated") is not True:
                        raise PitAutomationPolicyError("adapter did not prove activation")
                    column, next_stage = "activation_json", "canary"
                elif stage == "canary":
                    result = await self._invoke_with_lease(
                        self.actions.canary(run["activation"], idempotency_key=key),
                        run_id=run["run_id"],
                        owner=owner,
                        generation=generation,
                        stage=stage,
                    )
                    if result.get("healthy") is not True:
                        raise PitAutomationPolicyError("PIT activation canary failed")
                    column, next_stage = "canary_json", "monitor"
                elif stage == "monitor":
                    result = await self._invoke_with_lease(
                        self.actions.monitor(run["activation"], idempotency_key=key),
                        run_id=run["run_id"],
                        owner=owner,
                        generation=generation,
                        stage=stage,
                    )
                    if result.get("healthy") is not True:
                        raise PitAutomationPolicyError("PIT activation monitor failed")
                    column, next_stage = "monitor_json", "completed"
                else:
                    return run
                self.store.commit_stage(
                    run_id=run["run_id"],
                    owner=owner,
                    generation=generation,
                    current_stage=stage,
                    next_stage=next_stage,
                    result_column=column,
                    result=result,
                )
                if next_stage == "completed":
                    return self.store.get(run["run_id"])
            except (
                asyncio.CancelledError,
                PitAutomationLeaseError,
                KeyboardInterrupt,
                SystemExit,
            ):
                raise
            except BaseException as exc:
                return self.store.retry(
                    run_id=run["run_id"],
                    owner=owner,
                    generation=generation,
                    stage=stage,
                    error=exc,
                    base_seconds=self.retry_base_seconds,
                )


class GovernedPitAutomationActions:
    """Production adapter which preserves the existing governance boundary.

    Collection and validation are automatic.  Import is accepted only for an
    already approved package; this adapter never creates legal attestations or
    impersonates an administrator.  Current governance imports activate all
    four receipts atomically, so ``activate`` verifies that durable outcome.
    """

    async def collect(self, *, idempotency_key: str, actor_user_id: int) -> dict[str, Any]:
        del idempotency_key
        from backend.services.maintenance import run_pit_governance_refresh

        return await run_pit_governance_refresh(actor_user_id=actor_user_id)

    async def validate(self, candidate: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        del idempotency_key
        from backend.data.pit_evidence_governance import PitEvidenceGovernance

        package_id = str(candidate.get("package_id") or "")
        summary = PitEvidenceGovernance().get_package(package_id)
        return {
            "evidence_valid": True,
            "package_id": package_id,
            "package_sha256": summary["package_sha256"],
            "governance_status": summary["status"],
            "provider": "csindex_official",
            "import_schema": "point-in-time-master-import/v1",
            "collection": candidate,
        }

    async def classify(self, validated: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        del idempotency_key
        # The existing governance journal is authoritative for whether all
        # independent evidence/attestations exist. Pending material is yellow.
        approved = validated.get("governance_status") in {"approved", "imported"}
        return {
            # Existing v2 packages replay a complete window.  Until an exact
            # active-vs-candidate interval comparator proves a pure suffix,
            # production must not infer append-only from approval alone.
            "classification": "yellow",
            "same_source": True,
            "same_schema": True,
            "append_only": False,
            "evidence_valid": validated.get("evidence_valid") is True,
            "provider": validated.get("provider"),
            "import_schema": validated.get("import_schema"),
            "reason": (
                "append-only relation to the active timeline is not proven"
                if approved
                else "independent governance approval is still required"
            ),
        }

    async def import_candidate(
        self,
        validated: dict[str, Any],
        classification: dict[str, Any],
        *,
        idempotency_key: str,
        actor_user_id: int,
        policy_sha256: str,
    ) -> dict[str, Any]:
        del classification, idempotency_key, policy_sha256
        from backend.data.pit_evidence_governance import PitEvidenceGovernance

        result = PitEvidenceGovernance().import_approved_package(
            package_id=str(validated["package_id"]), actor_user_id=actor_user_id
        )
        return {"package_id": validated["package_id"], "governance": result}

    async def activate(
        self,
        imported: dict[str, Any],
        *,
        idempotency_key: str,
        actor_user_id: int,
        policy_sha256: str,
    ) -> dict[str, Any]:
        del idempotency_key, actor_user_id, policy_sha256
        governance = imported.get("governance") or {}
        return {
            "activated": governance.get("status") == "imported",
            "package_id": imported.get("package_id"),
            "receipts": governance.get("imports", []),
        }

    async def canary(self, activated: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        del idempotency_key
        receipts = activated.get("receipts")
        return {"healthy": isinstance(receipts, list) and len(receipts) == 4}

    async def monitor(self, activated: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        del idempotency_key
        return {"healthy": activated.get("activated") is True}


def configured_policy() -> PitAutomationPolicy:
    return PitAutomationPolicy(
        personal_mode=bool(settings.PIT_AUTOMATION_PERSONAL_MODE),
        auto_activate_green=bool(settings.PIT_AUTOMATION_AUTO_ACTIVATE_GREEN),
    )


async def run_configured_pit_update(idempotency_key: str) -> dict[str, Any]:
    actor = require_automation_service_identity()
    machine = PitDurableUpdateMachine(
        store=PitDurableUpdateStore(),
        actions=GovernedPitAutomationActions(),
        policy=configured_policy(),
        actor_user_id=int(actor["id"]),
        lease_seconds=int(settings.PIT_AUTOMATION_LEASE_SECONDS),
        stage_timeout_seconds=float(settings.PIT_AUTOMATION_STAGE_TIMEOUT_SECONDS),
        retry_base_seconds=int(settings.PIT_AUTOMATION_RETRY_BASE_SECONDS),
    )
    return await machine.run(idempotency_key)
