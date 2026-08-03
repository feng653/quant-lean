"""Fail-closed archival of legacy (non-PIT) market caches.

This module deliberately does *not* provide a generic ``rm -rf data`` helper.
The only automatic target is ``DATA_CACHE_DIR``: under the PIT-only policy it
contains legacy/current-snapshot market cache material and is never an
authoritative PIT master or dual-price ledger.  Experiment, paper-trading,
model, snapshot, evidence and backup stores are inventoried as protected
dependencies, never selected for deletion.

Even a successful archive is not evidence that production PIT data is ready.
The caller must prove the four governed pools for one explicit historical
window, then stop every application listener before an archive can be made.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
from typing import Any, Awaitable, Callable, Iterable
import uuid

from backend.config import settings
from backend.data.pit_runtime import PitRuntimeDataError, inspect_pit_runtime_input


CLEANUP_SCHEMA = "non-pit-cache-cleanup/v1"
GOVERNED_POOLS = ("csi300", "csi500", "csi800", "csi1000")
_WINDOW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROTECTED_TOP_LEVEL = {
    "backups",
    "models",
    "pit_evidence",
    "research_snapshots",
    "staging",
}
_PROTECTED_DATABASES = {
    "users.db",
    "experiment.db",
    "jobs.db",
    "trading_sim.db",
    "trading_live.db",
}


class NonPitCleanupError(RuntimeError):
    """Base error for a maintenance operation that must stop safely."""


class NonPitCleanupBlocked(NonPitCleanupError):
    """The requested archive has not satisfied all irreversible-action gates."""


class NonPitCleanupIntegrityError(NonPitCleanupError):
    """A cache/archive file is not a regular file or changed during a run."""


@dataclass(frozen=True)
class InventoryTarget:
    relative_path: str
    file_count: int
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class CleanupInventory:
    schema_version: str
    data_root: str
    generated_at: str
    targets: tuple[InventoryTarget, ...]
    protected_paths: tuple[str, ...]
    database_inventory: tuple[dict[str, Any], ...]
    unresolved_paths: tuple[str, ...]
    inventory_sha256: str

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["targets"] = [asdict(item) for item in self.targets]
        payload["protected_paths"] = list(self.protected_paths)
        payload["database_inventory"] = [dict(item) for item in self.database_inventory]
        payload["unresolved_paths"] = list(self.unresolved_paths)
        return payload


@dataclass(frozen=True)
class CleanupGate:
    ready: bool
    coverage_start: str
    coverage_end: str
    scopes: dict[str, dict[str, Any]]
    service_listeners: tuple[str, ...]
    blockers: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLEANUP_SCHEMA,
            "ready": self.ready,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "scopes": self.scopes,
            "service_listeners": list(self.service_listeners),
            "blockers": list(self.blockers),
        }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _resolve_root(data_root: Path | str | None) -> Path:
    root = Path(data_root or settings.abs_path(settings.DATABASE_DIR)).expanduser()
    resolved = root.resolve(strict=False)
    if resolved == resolved.parent:
        raise NonPitCleanupError("data root must not be a filesystem root")
    return resolved


def _under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root)
        return True
    except ValueError:
        return False


def _assert_regular(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise NonPitCleanupIntegrityError("cache artifact is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise NonPitCleanupIntegrityError("cache artifact must be a regular non-symlink file")


def _tree_snapshot(path: Path) -> tuple[int, int, str]:
    """Return a deterministic digest without following links or directories."""

    if not path.exists():
        return (0, 0, _digest([]))
    if path.is_symlink() or not path.is_dir():
        raise NonPitCleanupIntegrityError("cleanup target must be a directory")
    rows: list[dict[str, Any]] = []
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            raise NonPitCleanupIntegrityError("cleanup target contains a symlink")
        if child.is_dir():
            continue
        _assert_regular(child)
        digest = hashlib.sha256()
        with child.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        rows.append(
            {
                "path": relative,
                "size_bytes": child.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return (len(rows), sum(int(row["size_bytes"]) for row in rows), _digest(rows))


def _inspect_database(path: Path) -> dict[str, Any]:
    """Read only schema/integrity metadata; never surface database contents."""

    row: dict[str, Any] = {"relative_path": path.name, "status": "unreadable"}
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            tables = [
                str(item[0])
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        row["reason"] = type(exc).__name__
        return row
    row.update(status="ok" if integrity == "ok" else "integrity_invalid", table_count=len(tables))
    # Table names are enough to identify durable dependency domains while
    # avoiding experimental parameters, account information or result data.
    row["recognized_domains"] = sorted(
        {
            "pit_master" if name.startswith("pit_master_") else
            "price_ledger" if name.startswith("price_ledger_") else
            "experiments" if "experiment" in name else
            "paper_trading" if "portfolio" in name or "order" in name else
            "other"
            for name in tables
        }
    )
    return row


def build_inventory(data_root: Path | str | None = None) -> CleanupInventory:
    """Inventory the only automatic target and all protected local dependencies."""

    root = _resolve_root(data_root)
    cache = root / "cache"
    targets: list[InventoryTarget] = []
    if cache.exists():
        count, size, digest = _tree_snapshot(cache)
        targets.append(
            InventoryTarget(
                relative_path="cache",
                file_count=count,
                byte_count=size,
                sha256=digest,
            )
        )
    protected = [name for name in sorted(_PROTECTED_TOP_LEVEL) if (root / name).exists()]
    protected.extend(name for name in sorted(_PROTECTED_DATABASES) if (root / name).exists())
    databases = tuple(_inspect_database(path) for path in sorted(root.glob("*.db")))
    known = {"cache", *protected, *(item["relative_path"] for item in databases)}
    unresolved = tuple(
        path.name
        for path in sorted(root.iterdir())
        if path.name not in known and path.name != "maintenance"
    ) if root.exists() else ()
    unsigned = {
        "schema_version": CLEANUP_SCHEMA,
        "data_root": str(root),
        "targets": [asdict(item) for item in targets],
        "protected_paths": protected,
        "database_inventory": list(databases),
        "unresolved_paths": list(unresolved),
    }
    return CleanupInventory(
        schema_version=CLEANUP_SCHEMA,
        data_root=str(root),
        generated_at=utc_now_iso(),
        targets=tuple(targets),
        protected_paths=tuple(protected),
        database_inventory=databases,
        unresolved_paths=unresolved,
        inventory_sha256=_digest(unsigned),
    )


def _probe_listeners(
    endpoints: Iterable[tuple[str, int]] = (
        ("127.0.0.1", 8000),
        ("127.0.0.1", 5173),
        ("127.0.0.1", 443),
    ),
) -> tuple[str, ...]:
    active: list[str] = []
    for host, port in endpoints:
        try:
            with socket.create_connection((host, int(port)), timeout=0.25):
                active.append(f"{host}:{port}")
        except OSError:
            pass
    return tuple(active)


async def _default_runtime_report(
    pool_id: str,
    coverage_start: str,
    coverage_end: str,
) -> dict[str, Any]:
    try:
        runtime = await inspect_pit_runtime_input(
            pool_id=pool_id,
            required_start=coverage_start,
            required_end=coverage_end,
            purpose="research",
            require_benchmark=True,
        )
    except PitRuntimeDataError as exc:
        return {"runtime_ready": False, "failure_code": exc.code, **dict(exc.report)}
    return {"runtime_ready": True, **dict(runtime.market.report)}


RuntimeReportProvider = Callable[[str, str, str], Awaitable[dict[str, Any]]]


def _scope_gate(report: dict[str, Any]) -> tuple[bool, list[str]]:
    ledger = report.get("price_ledger") if isinstance(report.get("price_ledger"), dict) else {}
    roles = ledger.get("roles") if isinstance(ledger.get("roles"), dict) else {}
    raw = roles.get("raw_execution") if isinstance(roles.get("raw_execution"), dict) else {}
    adjusted = roles.get("research_adjusted") if isinstance(roles.get("research_adjusted"), dict) else {}
    universe = report.get("point_in_time") if isinstance(report.get("point_in_time"), dict) else {}
    universe_detail = universe.get("universe") if isinstance(universe.get("universe"), dict) else {}
    checks = {
        "pit_runtime_not_ready": bool(report.get("runtime_ready")),
        "pit_membership_not_ready": bool(report.get("universe_point_in_time")),
        "pit_membership_available_at_not_verified": bool(
            universe_detail.get("bitemporal_availability_verified")
        ),
        "dual_price_ledger_not_complete": bool(ledger.get("dual_ledger_complete")),
        "raw_execution_role_not_ready": bool(raw.get("available") and raw.get("trusted")),
        "research_adjusted_role_not_ready": bool(adjusted.get("available") and adjusted.get("trusted")),
        "price_available_at_not_verified": bool(ledger.get("bitemporal_availability_verified")),
        "runtime_binding_not_ready": bool(report.get("canonical_runtime_price_bound")),
        "authoritative_calendar_not_bound": bool(report.get("authoritative_trading_calendar_bound")),
        "pit_benchmark_not_bound": bool(report.get("point_in_time_benchmark_bound")),
        "unbiased_research_not_ready": bool(report.get("ready_for_unbiased_return_research")),
    }
    return (all(checks.values()), [name for name, passed in checks.items() if not passed])


async def inspect_cleanup_gate(
    *,
    coverage_start: str,
    coverage_end: str,
    runtime_report_provider: RuntimeReportProvider | None = None,
    listener_probe: Callable[[], tuple[str, ...]] = _probe_listeners,
) -> CleanupGate:
    """Read all four pool gates and listeners without changing runtime state."""

    provider = runtime_report_provider or _default_runtime_report
    scopes: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    for pool_id in GOVERNED_POOLS:
        report = await provider(pool_id, coverage_start, coverage_end)
        scope_ready, reasons = _scope_gate(report)
        scopes[pool_id] = {
            "ready": scope_ready,
            "reasons": reasons,
            "runtime_failure_code": report.get("failure_code"),
        }
        blockers.extend(f"{pool_id}:{reason}" for reason in reasons)
    listeners = tuple(listener_probe())
    blockers.extend(f"service_listener_active:{item}" for item in listeners)
    return CleanupGate(
        ready=not blockers,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        scopes=scopes,
        service_listeners=listeners,
        blockers=tuple(sorted(set(blockers))),
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = _canonical_bytes(payload)
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def write_dry_run_report(
    *, inventory: CleanupInventory,
    gate: CleanupGate,
    output_path: Path | str,
) -> dict[str, Any]:
    """Persist a local, non-secret audit report for a dry-run or blocked run."""

    payload = {
        "schema_version": CLEANUP_SCHEMA,
        "kind": "dry_run",
        "created_at": utc_now_iso(),
        "inventory": inventory.public_dict(),
        "gate": gate.public_dict(),
        "execute_performed": False,
        "production_data_deleted": False,
    }
    _write_json(Path(output_path), payload)
    return payload


def _require_confirmation(
    *,
    inventory: CleanupInventory,
    maintenance_window_id: str | None,
    inventory_sha256: str | None,
    second_confirmation: str | None,
) -> None:
    if not isinstance(maintenance_window_id, str) or _WINDOW_ID.fullmatch(maintenance_window_id) is None:
        raise NonPitCleanupBlocked("a valid explicit maintenance window id is required")
    if inventory_sha256 != inventory.inventory_sha256 or _SHA256.fullmatch(str(inventory_sha256 or "")) is None:
        raise NonPitCleanupBlocked("inventory hash confirmation does not match this dry-run")
    if second_confirmation != "ARCHIVE_NON_PIT_CACHE":
        raise NonPitCleanupBlocked("second confirmation must equal ARCHIVE_NON_PIT_CACHE")


def execute_cleanup(
    *,
    inventory: CleanupInventory,
    gate: CleanupGate,
    maintenance_window_id: str | None,
    inventory_sha256: str | None,
    second_confirmation: str | None,
) -> dict[str, Any]:
    """Atomically move legacy cache to a recoverable archive, never delete it."""

    if not gate.ready:
        raise NonPitCleanupBlocked("cleanup gate is closed: " + ", ".join(gate.blockers))
    _require_confirmation(
        inventory=inventory,
        maintenance_window_id=maintenance_window_id,
        inventory_sha256=inventory_sha256,
        second_confirmation=second_confirmation,
    )
    root = _resolve_root(inventory.data_root)
    if root != Path(inventory.data_root):
        raise NonPitCleanupIntegrityError("inventory root changed before cleanup")
    current = build_inventory(root)
    if current.inventory_sha256 != inventory.inventory_sha256:
        raise NonPitCleanupIntegrityError("inventory changed after dry-run; rerun audit")
    cache = root / "cache"
    if not cache.exists():
        raise NonPitCleanupBlocked("legacy cache target is absent; nothing to archive")
    if len(inventory.targets) != 1 or inventory.targets[0].relative_path != "cache":
        raise NonPitCleanupIntegrityError("cleanup target contract is invalid")
    archive_root = root / "maintenance" / "non_pit_cleanup"
    run_id = f"archive_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:12]}"
    run_root = archive_root / run_id
    archived_cache = run_root / "cache"
    run_root.mkdir(parents=True, mode=0o700)
    moved = False
    recreated = False
    try:
        os.replace(cache, archived_cache)
        moved = True
        archived_snapshot = _tree_snapshot(archived_cache)
        target = inventory.targets[0]
        if archived_snapshot != (target.file_count, target.byte_count, target.sha256):
            raise NonPitCleanupIntegrityError("archive verification does not match dry-run inventory")
        cache.mkdir(mode=0o700)
        recreated = True
        receipt = {
            "schema_version": CLEANUP_SCHEMA,
            "kind": "archive_receipt",
            "created_at": utc_now_iso(),
            "run_id": run_id,
            "maintenance_window_id": maintenance_window_id,
            "inventory_sha256": inventory.inventory_sha256,
            "target": asdict(target),
            "gate": gate.public_dict(),
            "archive_verified": True,
            "production_data_deleted": False,
            "restore_command": "use cleanup_non_pit_data.py --restore-run with double confirmation",
        }
        _write_json(run_root / "receipt.json", receipt)
        return receipt
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            if recreated and cache.exists():
                cache.rmdir()
            if moved and archived_cache.exists() and not cache.exists():
                os.replace(archived_cache, cache)
                target = inventory.targets[0]
                if _tree_snapshot(cache) != (target.file_count, target.byte_count, target.sha256):
                    raise NonPitCleanupIntegrityError("rollback verification failed")
        except Exception as rollback_exc:  # pragma: no cover - catastrophic filesystem failure
            rollback_error = rollback_exc
        if rollback_error is not None:
            raise NonPitCleanupIntegrityError("cleanup failed and rollback failed") from rollback_error
        if isinstance(exc, NonPitCleanupError):
            raise
        raise NonPitCleanupError("cleanup failed; source cache was rolled back") from exc


def restore_archive(
    *,
    data_root: Path | str,
    run_id: str,
    second_confirmation: str | None,
) -> dict[str, Any]:
    """Restore one verified archive only into an absent/empty cache directory."""

    if second_confirmation != "RESTORE_NON_PIT_CACHE":
        raise NonPitCleanupBlocked("second confirmation must equal RESTORE_NON_PIT_CACHE")
    root = _resolve_root(data_root)
    if _WINDOW_ID.fullmatch(run_id) is None:
        raise NonPitCleanupBlocked("archive run id is invalid")
    run_root = root / "maintenance" / "non_pit_cleanup" / run_id
    if not _under(root, run_root) or not (run_root / "receipt.json").is_file():
        raise NonPitCleanupBlocked("archive receipt is unavailable")
    try:
        receipt = json.loads((run_root / "receipt.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NonPitCleanupIntegrityError("archive receipt is unreadable") from exc
    target = receipt.get("target") if isinstance(receipt, dict) else None
    if not isinstance(target, dict) or target.get("relative_path") != "cache":
        raise NonPitCleanupIntegrityError("archive receipt target is invalid")
    archived = run_root / "cache"
    expected = (int(target.get("file_count")), int(target.get("byte_count")), str(target.get("sha256")))
    if _tree_snapshot(archived) != expected:
        raise NonPitCleanupIntegrityError("archive integrity verification failed")
    cache = root / "cache"
    if cache.exists():
        if cache.is_symlink() or not cache.is_dir() or any(cache.iterdir()):
            raise NonPitCleanupBlocked("refusing to overwrite a non-empty active cache")
        cache.rmdir()
    os.replace(archived, cache)
    if _tree_snapshot(cache) != expected:  # pragma: no cover - protects filesystem failure
        raise NonPitCleanupIntegrityError("restored cache integrity verification failed")
    result = {"schema_version": CLEANUP_SCHEMA, "kind": "restore_receipt", "run_id": run_id, "restored_at": utc_now_iso()}
    _write_json(run_root / "restore-receipt.json", result)
    return result
