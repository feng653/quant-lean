"""Versioned non-production research data imported from retained observations.

The store deliberately lives outside the activated PIT master and price
ledger.  Importing a provider candidate makes it useful for exploratory
research while preserving its limitations; it can never authorize live use.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso
from backend.core.hashing import file_sha256

import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import tempfile
import weakref
from contextvars import ContextVar
from datetime import date, datetime, timezone
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd

from backend.config import settings
from backend.data.provider_artifacts import (
    ContentAddressedProviderArtifactStore,
    ProviderArtifactError,
    canonical_json_bytes,
    canonical_sha256,
)
from backend.data.point_in_time_universe import (
    PointInTimeUniverseTimeline,
    _timeline_hash,
)


RESEARCH_GENERATION_SCHEMA = "research-data-generation/v1"
RESEARCH_SOURCE_REPORT_SCHEMA = "research-data-sources/v1"
RESEARCH_CONFLICT_REPORT_SCHEMA = "research-data-conflicts/v1"
RESEARCH_MARKET_CACHE_SCHEMA = 4
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INDEX_POOL = {
    "000300.SH": "csi300",
    "000905.SH": "csi500",
    "000906.SH": "csi800",
    "000852.SH": "csi1000",
}
_IMPORT_OWNER_MAX_AGE_SECONDS = 24 * 60 * 60
_ROW_SPOOL_NAME = re.compile(
    r"^\.research-spool\.[A-Za-z0-9_]+(?:\.sqlite)?$"
)
_GENERATION_BUILD_NAME = re.compile(
    r"^\.research\.[A-Za-z0-9_]+\.sqlite$"
)


class ResearchDataStoreError(RuntimeError):
    """A research generation or retained provider observation is invalid."""


ResearchImportProgress = Callable[[dict[str, Any]], None]


_IMPORT_SPOOLS: ContextVar[list["_ResearchRowSpool"] | None] = ContextVar(
    "research_import_spools", default=None
)


def _close_import_spools(method):
    """Guarantee disk spools are removed on every import return or exception."""

    @wraps(method)
    def wrapped(*args, **kwargs):
        owned: list[_ResearchRowSpool] = []
        token = _IMPORT_SPOOLS.set(owned)
        try:
            return method(*args, **kwargs)
        finally:
            for spool in reversed(owned):
                spool.close()
            _IMPORT_SPOOLS.reset(token)

    return wrapped


class _ResearchRowSpool:
    """Disk-backed row batches keep full-history imports below RAM limits."""

    def __init__(self, directory: Path) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=".research-spool.", suffix=".sqlite", dir=directory
        )
        os.close(descriptor)
        self.path = Path(name)
        self.metadata_path = self.path.with_name(f"{self.path.name}.meta.json")
        os.chmod(self.path, 0o600)
        _write_import_temp_metadata(self.path, self.metadata_path, kind="row_spool")
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE rows (kind TEXT NOT NULL, sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.execute("CREATE INDEX idx_spool_kind ON rows(kind, sequence)")
        self.counts: dict[str, int] = {}
        self._finalizer = weakref.finalize(
            self,
            self._cleanup,
            self.connection,
            self.path,
            self.metadata_path,
        )
        owned = _IMPORT_SPOOLS.get()
        if owned is not None:
            owned.append(self)

    @staticmethod
    def _cleanup(
        connection: sqlite3.Connection, path: Path, metadata_path: Path
    ) -> None:
        try:
            connection.close()
        finally:
            path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)

    def extend(self, kind: str, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        self.connection.executemany(
            "INSERT INTO rows(kind, payload) VALUES (?, ?)",
            [
                (kind, json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                for row in rows
            ],
        )
        self.connection.commit()
        self.counts[kind] = self.counts.get(kind, 0) + len(rows)

    def iter_rows(self, kind: str, *, batch_size: int = 10_000):
        cursor = self.connection.execute(
            "SELECT payload FROM rows WHERE kind=? ORDER BY sequence", (kind,)
        )
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            for row in batch:
                yield tuple(json.loads(row[0]))

    def close(self) -> None:
        if self._finalizer.alive:
            self._finalizer()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _write_import_temp_metadata(path: Path, metadata_path: Path, *, kind: str) -> None:
    metadata_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "research-import-temporary/v1",
                "kind": kind,
                "path_name": path.name,
                "owner_pid": os.getpid(),
                "owner_host": socket.gethostname(),
                "process_start_identity": _process_start_identity(os.getpid()),
                "created_at": utc_now_iso(),
                "recover_by": "rebuild_from_content_addressed_checkpoint",
                "active_generation_eligible": False,
            }
        )
    )
    os.chmod(metadata_path, 0o600)


def _process_start_identity(pid: int) -> str | None:
    """Return an OS process-birth identity so a reused PID is not trusted."""

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        # Linux field 22 is the start time in clock ticks since boot.  The
        # command name may contain spaces, so split only after the final ')'.
        fields = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return f"proc:{fields[19]}"
    except (IndexError, OSError):
        pass
    try:
        observed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = observed.stdout.strip()
    return f"ps:{value}" if observed.returncode == 0 and value else None


def _month_count(first_month: str, last_month: str) -> int:
    try:
        first = datetime.strptime(first_month, "%Y-%m")
        last = datetime.strptime(last_month, "%Y-%m")
    except ValueError as exc:
        raise ResearchDataStoreError("research generation month range is invalid") from exc
    count = (last.year - first.year) * 12 + last.month - first.month + 1
    if count <= 0:
        raise ResearchDataStoreError("research generation month range is invalid")
    return count


@lru_cache(maxsize=8)
def _verifiedfile_sha256(
    path_text: str, expected: str, size: int, modified_ns: int
) -> bool:
    """Hash once per immutable file identity, not on every UI status poll."""

    del size, modified_ns
    return file_sha256(Path(path_text)) == expected


class ResearchDataStore:
    """Immutable SQLite generations with one atomic active pointer."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or settings.abs_path(settings.RESEARCH_DATA_DIR)
        self.root = Path(os.path.abspath(Path(configured).expanduser()))
        self.generations = self.root / "generations"
        self.active_pointer = self.root / "active.json"

    def _ensure_write_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.generations.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.generations, 0o700)

    @staticmethod
    def default_tushare_evidence_root() -> Path:
        return (
            settings.abs_path(settings.PIT_EVIDENCE_DIR)
            / "provider_candidates"
            / "tushare_backfill"
        )

    @staticmethod
    def _load_checkpoint(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchDataStoreError("tushare checkpoint is unreadable") from exc
        checksum = payload.pop("checkpoint_sha256", None)
        if checksum != canonical_sha256(payload):
            raise ResearchDataStoreError("tushare checkpoint digest changed")
        payload["checkpoint_sha256"] = checksum
        if not isinstance(payload.get("completed"), Mapping):
            raise ResearchDataStoreError("tushare checkpoint completed set is invalid")
        return payload

    def _best_tushare_checkpoint(self, evidence_root: Path) -> tuple[Path, dict[str, Any]]:
        candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
        checkpoint_dir = evidence_root / "checkpoints"
        if not checkpoint_dir.is_dir():
            raise ResearchDataStoreError("tushare checkpoint directory is missing")
        for path in checkpoint_dir.glob("*.json"):
            payload = self._load_checkpoint(path)
            index_count = sum(
                1
                for result in payload["completed"].values()
                if isinstance(result, Mapping)
                and isinstance(result.get("task"), Mapping)
                and result["task"].get("dataset") == "index_weight"
                and isinstance(result.get("validation"), Mapping)
                and result["validation"].get("status")
                == "complete_monthly_snapshot_candidate"
            )
            candidates.append((index_count, str(payload.get("updated_at") or ""), path, payload))
        if not candidates or max(item[0] for item in candidates) == 0:
            raise ResearchDataStoreError("no complete Tushare index history observation exists")
        _count, _updated, path, payload = max(candidates, key=lambda item: (item[0], item[1]))
        return path, payload

    @staticmethod
    def _matching_tushare_report(
        evidence_root: Path, source_manifests: set[str]
    ) -> str | None:
        """Find an intact report that binds the imported monthly receipts."""

        report_root = evidence_root / "reports" / "sha256"
        if not report_root.is_dir():
            return None
        candidates: list[tuple[str, str]] = []
        for path in report_root.glob("*/*.json"):
            digest = path.stem
            if not _SHA256.fullmatch(digest) or path.is_symlink():
                continue
            try:
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    continue
                report = json.loads(raw)
                coverage = report.get("index_month_coverage")
                if not isinstance(coverage, list):
                    continue
                report_manifests = {
                    str(item.get("manifest_sha256") or "")
                    for item in coverage
                    if isinstance(item, Mapping)
                    and item.get("status")
                    == "complete_monthly_snapshot_candidate"
                }
                if report_manifests == source_manifests:
                    candidates.append(
                        (str(report.get("observed_at") or ""), digest)
                    )
            except (OSError, TypeError, json.JSONDecodeError):
                continue
        return max(candidates)[1] if candidates else None

    @staticmethod
    def _artifact_rows(
        artifact_store: ContentAddressedProviderArtifactStore,
        result: Mapping[str, Any],
        *,
        expected_dataset: str,
        expected_params: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        receipt = result.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ResearchDataStoreError("Tushare artifact receipt is invalid")
        manifest_sha = str(receipt.get("manifest_sha256") or "")
        if not _SHA256.fullmatch(manifest_sha):
            raise ResearchDataStoreError("Tushare artifact receipt digest is invalid")
        manifest, response = artifact_store.read(manifest_sha)
        request = manifest.get("request")
        if (
            manifest.get("provider") != "tushare_pro"
            or manifest.get("dataset") != expected_dataset
            or manifest.get("classification") != "quarantine"
            or not isinstance(request, Mapping)
            or request.get("params") != dict(expected_params)
        ):
            raise ResearchDataStoreError("Tushare artifact scope changed")
        try:
            document = json.loads(response)
            fields = document["data"]["fields"]
            items = document["data"]["items"]
            rows = [dict(zip(fields, item, strict=True)) for item in items]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ResearchDataStoreError("Tushare response table is invalid") from exc
        return manifest_sha, manifest, rows

    def _collect_index_history(
        self,
        evidence: Path,
        checkpoint: Mapping[str, Any],
    ) -> tuple[
        list[tuple[str, str, str, str, float | None, str, str]], set[str]
    ]:
        artifact_store = ContentAddressedProviderArtifactStore(evidence)
        source_rows: list[tuple[str, str, str, str, float | None, str, str]] = []
        source_manifests: set[str] = set()
        for result in checkpoint["completed"].values():
            if not isinstance(result, Mapping):
                continue
            task = result.get("task")
            validation = result.get("validation")
            if (
                not isinstance(task, Mapping)
                or task.get("dataset") != "index_weight"
                or not isinstance(validation, Mapping)
                or validation.get("status") != "complete_monthly_snapshot_candidate"
            ):
                continue
            params = task.get("params")
            if not isinstance(params, Mapping):
                raise ResearchDataStoreError("Tushare index task parameters are invalid")
            manifest_sha, manifest, rows = self._artifact_rows(
                artifact_store,
                result,
                expected_dataset="index_weight",
                expected_params=params,
            )
            pool_id = _INDEX_POOL.get(str(params.get("index_code")))
            start_date = str(params.get("start_date") or "")
            if pool_id is None or not re.fullmatch(r"\d{8}", start_date):
                raise ResearchDataStoreError("Tushare index request scope is invalid")
            month = f"{start_date[:4]}-{start_date[4:6]}"
            rows_by_date: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                rows_by_date.setdefault(str(row.get("trade_date") or ""), []).append(row)
            selected_trade_date, selected_rows = max(
                rows_by_date.items(),
                key=lambda item: (len({str(row.get("con_code") or "") for row in item[1]}), item[0]),
                default=("", []),
            )
            observed_codes: set[str] = set()
            for row in selected_rows:
                code = str(row.get("con_code") or "")
                trade_date = selected_trade_date
                if (
                    not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code)
                    or not re.fullmatch(r"\d{8}", trade_date)
                    or code in observed_codes
                ):
                    raise ResearchDataStoreError("Tushare index member row is invalid")
                observed_codes.add(code)
                weight_value = row.get("weight")
                weight = float(weight_value) if weight_value not in {None, ""} else None
                if weight is not None and (not math.isfinite(weight) or weight < 0):
                    raise ResearchDataStoreError("Tushare index member weight is invalid")
                source_rows.append(
                    (
                        pool_id,
                        month,
                        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
                        code,
                        weight,
                        manifest_sha,
                        str(manifest["bitemporal"]["ingested_at"]),
                    )
                )
            source_manifests.add(manifest_sha)
        if not source_rows:
            raise ResearchDataStoreError("Tushare index history import is empty")
        return source_rows, source_manifests

    def import_tushare_index_history(
        self, evidence_root: str | Path | None = None
    ) -> dict[str, Any]:
        """Materialize all verified complete monthly index artifacts.

        The output remains ``research_only``.  Provider ingestion time is
        retained as observation metadata and is never mislabelled as the
        historical market availability timestamp.
        """

        self._ensure_write_directories()
        evidence = Path(evidence_root or self.default_tushare_evidence_root())
        checkpoint_path, checkpoint = self._best_tushare_checkpoint(evidence)
        source_rows, source_manifests = self._collect_index_history(
            evidence, checkpoint
        )
        warnings = {
            "provider_available_at_missing",
            "monthly_snapshot_not_exact_intramonth_timeline",
            "research_only_not_live_eligible",
        }
        candidate_report_sha256 = self._matching_tushare_report(
            evidence, source_manifests
        )
        identity = {
            "schema_version": RESEARCH_GENERATION_SCHEMA,
            "provider": "tushare",
            "source_manifests": sorted(source_manifests),
            "row_count": len(source_rows),
        }
        generation_id = canonical_sha256(identity)
        target = self.generations / f"{generation_id}.sqlite"
        target_existed = target.exists()
        orphan: Path | None = None
        if target_existed and not target.with_suffix(".binding.json").is_file():
            orphan = self._quarantine_unbound_generation(target, generation_id)
            target_existed = False
        if not target_existed:
            descriptor, name = tempfile.mkstemp(prefix=".research.", suffix=".sqlite", dir=self.generations)
            os.close(descriptor)
            temporary = Path(name)
            try:
                connection = sqlite3.connect(temporary)
                connection.executescript(
                    """
                    PRAGMA journal_mode=DELETE;
                    CREATE TABLE generation_metadata (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL
                    );
                    CREATE TABLE index_membership (
                        pool_id TEXT NOT NULL,
                        observation_month TEXT NOT NULL,
                        vendor_trade_date TEXT NOT NULL,
                        security_code TEXT NOT NULL,
                        weight REAL,
                        source_manifest_sha256 TEXT NOT NULL,
                        ingested_at TEXT NOT NULL,
                        PRIMARY KEY (pool_id, observation_month, security_code)
                    );
                    CREATE INDEX idx_research_pool_month
                    ON index_membership(pool_id, observation_month);
                    """
                )
                connection.executemany(
                    """INSERT INTO index_membership
                    (pool_id, observation_month, vendor_trade_date, security_code,
                     weight, source_manifest_sha256, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    sorted(source_rows),
                )
                metadata = {
                    **identity,
                    "identity": identity,
                    "checkpoint_sha256": checkpoint["checkpoint_sha256"],
                    "candidate_report_sha256": candidate_report_sha256,
                    "generation_id": generation_id,
                    "classification": "vendor_research_trusted",
                    "research_trust_profile": "tushare_research_trusted",
                    "created_at": utc_now_iso(),
                    "checkpoint_file": checkpoint_path.name,
                    "warnings": sorted(warnings),
                    "live_eligible": False,
                }
                connection.executemany(
                    "INSERT INTO generation_metadata(key, value_json) VALUES (?, ?)",
                    [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in metadata.items()],
                )
                connection.commit()
                result = connection.execute("PRAGMA integrity_check").fetchone()
                connection.close()
                if result is None or result[0] != "ok":
                    raise ResearchDataStoreError("research generation integrity check failed")
                os.chmod(temporary, 0o600)
                self._write_generation_binding(
                    temporary,
                    generation_id,
                    identity,
                    binding_path=target.with_suffix(".binding.json"),
                )
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        if target_existed:
            self._verify_existing_generation(target, identity)
        if orphan is not None:
            orphan.unlink(missing_ok=True)
        pointer = {
            "schema_version": RESEARCH_GENERATION_SCHEMA,
            "generation_id": generation_id,
            "provider": "tushare",
            "file_sha256": file_sha256(target),
            "file_size": target.stat().st_size,
            "file_mtime_ns": target.stat().st_mtime_ns,
            "activated_for": ["exploratory_research", "paper_simulation"],
            "live_eligible": False,
            "research_trust_profile": "tushare_research_trusted",
            "updated_at": utc_now_iso(),
        }
        self._atomic_json(self.active_pointer, pointer)
        report = self.status()
        report["import"] = {
            "source_checkpoint_sha256": checkpoint["checkpoint_sha256"],
            "source_manifest_count": len(source_manifests),
            "rows_imported": len(source_rows),
            "production_tables_changed": False,
        }
        return report

    @_close_import_spools
    def import_tushare_reconciled_history(
        self,
        evidence_root: str | Path | None = None,
        *,
        run_id: str,
        candidate_report_sha256: str | None = None,
        collection_report: Mapping[str, Any] | None = None,
        progress: ResearchImportProgress | None = None,
    ) -> dict[str, Any]:
        """Publish one exact checkpoint's usable research rows.

        Only sessions whose persisted reconciliation says ``valid=true`` are
        admitted.  A partial checkpoint therefore creates a partial research
        generation with explicit coverage and pending counts; it is never
        silently upgraded to production PIT or live eligibility.
        """

        if not re.fullmatch(r"[0-9a-f]{32}", str(run_id)):
            raise ResearchDataStoreError("Tushare research run_id is invalid")
        self._ensure_write_directories()
        spool_recovery = self.quarantine_stale_import_spools()
        self._report_import_progress(
            progress,
            fraction=0.76,
            stage="research_import_prepare",
            message="正在校验 checkpoint 并准备研究数据导入",
        )
        evidence = Path(evidence_root or self.default_tushare_evidence_root())
        checkpoint_path = evidence / "checkpoints" / f"{run_id}.json"
        if not checkpoint_path.is_file():
            raise ResearchDataStoreError("Tushare research checkpoint is missing")
        checkpoint = self._load_checkpoint(checkpoint_path)
        if checkpoint.get("run_id") != run_id:
            raise ResearchDataStoreError("Tushare research checkpoint run changed")
        plan = checkpoint.get("plan")
        if not isinstance(plan, Mapping):
            raise ResearchDataStoreError("Tushare research checkpoint plan is invalid")

        index_rows, index_manifests = self._collect_index_history(
            evidence, checkpoint
        )
        artifact_store = ContentAddressedProviderArtifactStore(evidence)
        completed = checkpoint["completed"]
        reconciliation = checkpoint.get("session_reconciliation")
        if not isinstance(reconciliation, Mapping):
            raise ResearchDataStoreError("Tushare session reconciliation is invalid")

        completed_by_session: dict[str, dict[str, Mapping[str, Any]]] = {}
        slow_results: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
        for result in completed.values():
            if not isinstance(result, Mapping):
                continue
            if result.get("optional_failure"):
                continue
            task = result.get("task")
            if not isinstance(task, Mapping):
                continue
            dataset = str(task.get("dataset") or "")
            params = task.get("params")
            if not isinstance(params, Mapping):
                continue
            trade_date = str(params.get("trade_date") or "")
            if dataset in {"daily", "adj_factor", "daily_basic", "suspend_d"} and re.fullmatch(
                r"\d{8}", trade_date
            ):
                completed_by_session.setdefault(trade_date, {})[dataset] = result
            elif dataset in {
                "stock_basic",
                "sw_classify",
                "sw_membership",
                "dividend",
                "namechange",
                "index_daily",
            }:
                slow_results.append((dataset, params, result))

        spool = _ResearchRowSpool(self.generations)
        source_manifests = set(index_manifests)
        valid_sessions: list[str] = []
        missing_factor = 0
        missing_basic = 0

        strict_reconciliation_warning_count = 0
        reconciliation_items = sorted(reconciliation.items())
        reconciliation_total = max(len(reconciliation_items), 1)
        for reconciliation_index, (compact_date, reconciliation_row) in enumerate(
            reconciliation_items, start=1
        ):
            if reconciliation_index == 1 or reconciliation_index % 25 == 0:
                self._report_import_progress(
                    progress,
                    fraction=0.76 + 0.12 * reconciliation_index / reconciliation_total,
                    stage="research_import_spool",
                    message=(
                        "正在解析并暂存研究行情："
                        f"{reconciliation_index}/{len(reconciliation_items)} 个交易日"
                    ),
                    completed_sessions=reconciliation_index,
                    total_sessions=len(reconciliation_items),
                )
            if (
                not re.fullmatch(r"\d{8}", str(compact_date))
                or not isinstance(reconciliation_row, Mapping)
            ):
                continue
            if reconciliation_row.get("valid") is not True:
                strict_reconciliation_warning_count += 1
            session_results = completed_by_session.get(str(compact_date), {})
            if not {"daily", "adj_factor"}.issubset(session_results):
                raise ResearchDataStoreError(
                    "reconciled Tushare session artifacts are incomplete"
                )
            manifest_binding = reconciliation_row.get("dataset_manifest_sha256")
            if not isinstance(manifest_binding, Mapping):
                raise ResearchDataStoreError("session reconciliation binding is missing")
            datasets: dict[str, tuple[str, dict[str, Any], list[dict[str, Any]]]] = {}
            for dataset, result in sorted(session_results.items()):
                params = result["task"]["params"]
                artifact = self._artifact_rows(
                    artifact_store,
                    result,
                    expected_dataset=dataset,
                    expected_params=params,
                )
                if manifest_binding.get(dataset) != artifact[0]:
                    raise ResearchDataStoreError(
                        "session reconciliation artifact binding changed"
                    )
                datasets[dataset] = artifact
                source_manifests.add(artifact[0])
            for optional_dataset in ("daily_basic", "suspend_d"):
                if optional_dataset not in datasets:
                    datasets[optional_dataset] = (
                        "",
                        {"bitemporal": {"ingested_at": ""}},
                        [],
                    )

            factors = {
                str(row.get("ts_code") or ""): row
                for row in datasets["adj_factor"][2]
            }
            basics = {
                str(row.get("ts_code") or ""): row
                for row in datasets["daily_basic"][2]
            }
            ingested_at = max(
                str(artifact[1]["bitemporal"]["ingested_at"])
                for artifact in datasets.values()
            )
            trade_date = (
                f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:]}"
            )
            observed_codes: set[str] = set()
            session_market_rows: list[tuple[Any, ...]] = []
            for row in datasets["daily"][2]:
                code = str(row.get("ts_code") or "")
                if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code) or code in observed_codes:
                    raise ResearchDataStoreError("Tushare daily identity is invalid")
                observed_codes.add(code)
                factor_row = factors.get(code)
                basic_row = basics.get(code)
                if factor_row is None:
                    missing_factor += 1
                    continue
                if basic_row is None:
                    missing_basic += 1
                    basic_row = {}
                try:
                    raw_values = [float(row[field]) for field in ("open", "high", "low", "close")]
                    pre_close = float(row["pre_close"]) if row.get("pre_close") not in {None, ""} else None
                    volume = float(row["vol"])
                    amount = float(row["amount"])
                    factor = float(factor_row["adj_factor"])
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise ResearchDataStoreError("Tushare daily numeric row is invalid") from exc
                numeric = [*raw_values, volume, amount, factor]
                if (
                    not all(math.isfinite(value) for value in numeric)
                    or min(raw_values) <= 0
                    or volume < 0
                    or amount < 0
                    or factor <= 0
                    or (pre_close is not None and (not math.isfinite(pre_close) or pre_close <= 0))
                ):
                    raise ResearchDataStoreError("Tushare daily numeric row is corrupt")
                basic_values: list[float | None] = []
                for field in (
                    "turnover_rate",
                    "volume_ratio",
                    "pe",
                    "pb",
                    "total_share",
                    "float_share",
                    "total_mv",
                    "circ_mv",
                ):
                    value = basic_row.get(field)
                    if value in {None, ""}:
                        basic_values.append(None)
                        continue
                    try:
                        number = float(value)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ResearchDataStoreError(
                            "Tushare daily-basic numeric row is invalid"
                        ) from exc
                    if not math.isfinite(number):
                        raise ResearchDataStoreError(
                            "Tushare daily-basic numeric row is corrupt"
                        )
                    if field in {"total_share", "float_share", "total_mv", "circ_mv"}:
                        number *= 10_000.0
                    basic_values.append(number)
                session_market_rows.append(
                    (
                        trade_date,
                        code,
                        *raw_values,
                        pre_close,
                        *(value * factor for value in raw_values),
                        volume,
                        amount,
                        volume * 100.0,
                        amount * 1_000.0,
                        factor,
                        *basic_values,
                        json.dumps(basic_row, ensure_ascii=False, sort_keys=True),
                        datasets["daily"][0],
                        datasets["adj_factor"][0],
                        datasets["daily_basic"][0],
                        ingested_at,
                    )
                )
            session_status_rows: list[tuple[Any, ...]] = []
            for row in datasets["suspend_d"][2]:
                code = str(row.get("ts_code") or "")
                if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
                    raise ResearchDataStoreError("Tushare suspension identity is invalid")
                session_status_rows.append(
                    (
                        trade_date,
                        code,
                        str(row.get("suspend_timing") or ""),
                        str(row.get("suspend_type") or ""),
                        datasets["suspend_d"][0],
                        str(datasets["suspend_d"][1]["bitemporal"]["ingested_at"]),
                    )
                )
            if session_market_rows:
                spool.extend("market_daily", session_market_rows)
                spool.extend("trading_status", session_status_rows)
                valid_sessions.append(trade_date)

        for dataset, params, result in slow_results:
            manifest_sha, manifest, rows = self._artifact_rows(
                artifact_store,
                result,
                expected_dataset=dataset,
                expected_params=params,
            )
            source_manifests.add(manifest_sha)
            ingested_at = str(manifest["bitemporal"]["ingested_at"])
            slow_rows: list[tuple[Any, ...]] = []
            slow_kind: str | None = None
            for row in rows:
                payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
                if dataset == "stock_basic":
                    code = str(row.get("ts_code") or "")
                    if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
                        slow_kind = "security_master"
                        slow_rows.append(
                            (
                                code,
                                str(row.get("list_status") or ""),
                                str(row.get("list_date") or ""),
                                str(row.get("delist_date") or ""),
                                payload,
                                manifest_sha,
                                ingested_at,
                            )
                        )
                elif dataset in {"sw_classify", "sw_membership"}:
                    slow_kind = "industry"
                    slow_rows.append(
                        (
                            dataset,
                            str(row.get("ts_code") or row.get("index_code") or ""),
                            str(row.get("in_date") or ""),
                            str(row.get("out_date") or ""),
                            payload,
                            manifest_sha,
                            ingested_at,
                        )
                    )
                elif dataset in {"dividend", "namechange"}:
                    slow_kind = "events"
                    slow_rows.append(
                        (
                            dataset,
                            str(row.get("ts_code") or ""),
                            str(
                                row.get("ann_date")
                                or row.get("start_date")
                                or row.get("end_date")
                                or ""
                            ),
                            payload,
                            manifest_sha,
                            ingested_at,
                        )
                    )
                elif dataset == "index_daily":
                    code = str(row.get("ts_code") or "")
                    compact = str(row.get("trade_date") or "")
                    try:
                        close = float(row["close"])
                    except (KeyError, TypeError, ValueError, OverflowError) as exc:
                        raise ResearchDataStoreError("Tushare benchmark row is invalid") from exc
                    if (
                        code not in _INDEX_POOL
                        or not re.fullmatch(r"\d{8}", compact)
                        or not math.isfinite(close)
                        or close <= 0
                    ):
                        raise ResearchDataStoreError("Tushare benchmark row is corrupt")
                    slow_kind = "benchmark"
                    slow_rows.append(
                        (
                            code,
                            f"{compact[:4]}-{compact[4:6]}-{compact[6:]}",
                            close,
                            payload,
                            manifest_sha,
                            ingested_at,
                        )
                    )
            if slow_kind is not None:
                spool.extend(slow_kind, slow_rows)

        market_dates = sorted(set(valid_sessions))
        warnings = {
            "provider_available_at_missing",
            "monthly_snapshot_not_exact_intramonth_timeline",
            "single_source_not_cross_validated",
            "historical_revision_retention_not_proven",
            "production_dual_price_ledger_not_authorized",
            "research_only_not_live_eligible",
        }
        if not spool.counts.get("market_daily"):
            warnings.add("reconciled_market_sessions_not_yet_available")
        if missing_factor:
            warnings.add("all_market_adjustment_factor_rows_missing")
        if missing_basic:
            warnings.add("all_market_daily_basic_rows_missing")
        if strict_reconciliation_warning_count:
            warnings.add("strict_tradability_reconciliation_failed_warning_only")
        if not spool.counts.get("benchmark"):
            warnings.add("benchmark_index_daily_not_materialized")
        collection_progress = (
            dict(collection_report.get("progress") or {})
            if isinstance(collection_report, Mapping)
            else {}
        )
        collection_complete = collection_progress.get("complete") is True
        plan_last_month = str(plan.get("last_month") or "")
        expected_index_months = 4 * _month_count("2016-01", plan_last_month)
        observed_index_months = len(
            {(row[0], row[1]) for row in index_rows}
        )
        conditionally_trusted = bool(
            collection_complete
            and plan.get("first_month") == "2016-01"
            and observed_index_months == expected_index_months
            and not (
                collection_report.get("optional_failures")
                if isinstance(collection_report, Mapping)
                else []
            )
        )
        classification = (
            "vendor_research_trusted"
            if conditionally_trusted
            else "single_source_research"
        )
        trust_profile = (
            "tushare_research_trusted"
            if conditionally_trusted
            else "single_source_research_warning_only"
        )
        coverage = {
            "date_start": market_dates[0] if market_dates else None,
            "date_end": market_dates[-1] if market_dates else None,
            "reconciled_session_count": len(market_dates),
            "market_row_count": spool.counts.get("market_daily", 0),
            "index_snapshot_count": observed_index_months,
            "expected_index_snapshot_count": expected_index_months,
            "planned_tasks": collection_progress.get("planned_tasks"),
            "completed_tasks": collection_progress.get("completed_tasks"),
            "pending_tasks": collection_progress.get("pending_tasks"),
            "failed_task_count": len(checkpoint.get("failures") or {}),
            "strict_reconciliation_warning_count": strict_reconciliation_warning_count,
        }
        report_sha = str(candidate_report_sha256 or "") or None
        if report_sha is not None and not _SHA256.fullmatch(report_sha):
            raise ResearchDataStoreError("Tushare candidate report digest is invalid")
        if report_sha is not None:
            report_path = (
                evidence / "reports" / "sha256" / report_sha[:2] / f"{report_sha}.json"
            )
            try:
                report_bytes = report_path.read_bytes()
                retained_report = json.loads(report_bytes)
            except (OSError, json.JSONDecodeError) as exc:
                raise ResearchDataStoreError("Tushare candidate report is unavailable") from exc
            if (
                hashlib.sha256(report_bytes).hexdigest() != report_sha
                or not isinstance(retained_report, Mapping)
                or retained_report.get("run_id") != run_id
                or (retained_report.get("checkpoint") or {}).get("sha256")
                != checkpoint["checkpoint_sha256"]
            ):
                raise ResearchDataStoreError("Tushare candidate report binding changed")
            if isinstance(collection_report, Mapping):
                supplied = dict(collection_report)
                supplied.pop("stored_report_sha256", None)
                if supplied != dict(retained_report):
                    raise ResearchDataStoreError("Tushare collection report changed")
        identity = {
            "schema_version": RESEARCH_GENERATION_SCHEMA,
            "market_cache_schema_version": RESEARCH_MARKET_CACHE_SCHEMA,
            "provider": "tushare",
            "run_id": run_id,
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
            "source_manifests": sorted(source_manifests),
            "session_reconciliation_sha256": canonical_sha256(reconciliation),
            "coverage": coverage,
            "warnings": sorted(warnings),
            "row_counts": {
                "index_membership": len(index_rows),
                "market_daily": spool.counts.get("market_daily", 0),
                "trading_status": spool.counts.get("trading_status", 0),
                "security_master": spool.counts.get("security_master", 0),
                "industry": spool.counts.get("industry", 0),
                "events": spool.counts.get("events", 0),
                "benchmark": spool.counts.get("benchmark", 0),
            },
        }
        generation_id = canonical_sha256(identity)
        target = self.generations / f"{generation_id}.sqlite"
        orphan: Path | None = None
        if target.exists() and not target.with_suffix(".binding.json").is_file():
            orphan = self._quarantine_unbound_generation(target, generation_id)
        if target.exists():
            self._report_import_progress(
                progress,
                fraction=0.91,
                stage="research_import_verify_existing",
                message="正在复核已存在的不可变研究 generation",
            )
            self._verify_existing_generation(target, identity)
        else:
            self._report_import_progress(
                progress,
                fraction=0.90,
                stage="research_import_write",
                message="正在写入不可变研究 generation",
            )
            self._write_research_generation(
                target=target,
                identity=identity,
                generation_id=generation_id,
                classification=classification,
                trust_profile=trust_profile,
                index_rows=index_rows,
                market_rows=spool.iter_rows("market_daily"),
                status_rows=spool.iter_rows("trading_status"),
                security_rows=spool.iter_rows("security_master"),
                industry_rows=spool.iter_rows("industry"),
                event_rows=spool.iter_rows("events"),
                benchmark_rows=spool.iter_rows("benchmark"),
                row_counts=spool.counts,
                progress=progress,
            )
        if orphan is not None:
            orphan.unlink(missing_ok=True)
        self._report_import_progress(
            progress,
            fraction=0.97,
            stage="research_import_hash",
            message="正在校验 generation 文件摘要",
        )
        pointer = {
            "schema_version": RESEARCH_GENERATION_SCHEMA,
            "generation_id": generation_id,
            "provider": "tushare",
            "file_sha256": file_sha256(target),
            "file_size": target.stat().st_size,
            "file_mtime_ns": target.stat().st_mtime_ns,
            "activated_for": ["exploratory_research", "paper_simulation"],
            "live_eligible": False,
            "research_trust_profile": trust_profile,
            "candidate_report_sha256": report_sha,
            "updated_at": utc_now_iso(),
        }
        # Build the potentially scan-heavy status response before entering the
        # non-cancellable pointer commit window.  The commit tail must stay
        # bounded to the atomic pointer fsync plus in-memory result assembly;
        # otherwise scheduler shutdown could time out after the pointer changed
        # while the job still appears non-terminal.
        report = self._status_for_generation(pointer, target)
        self._report_import_progress(
            progress,
            fraction=0.99,
            stage="research_import_activate",
            message="正在原子激活完整研究 generation",
            cancellable=False,
        )
        self._atomic_json(self.active_pointer, pointer)
        report["activation_committed"] = True
        report["import"] = {
            "source_run_id": run_id,
            "source_checkpoint_sha256": checkpoint["checkpoint_sha256"],
            "source_manifest_count": len(source_manifests),
            "rows_imported": identity["row_counts"],
            "production_tables_changed": False,
            "spool_recovery": spool_recovery,
        }
        return report

    @staticmethod
    def _report_import_progress(
        progress: ResearchImportProgress | None,
        *,
        fraction: float,
        stage: str,
        message: str,
        **details: Any,
    ) -> None:
        if progress is not None:
            progress(
                {
                    "overall_fraction": min(max(float(fraction), 0.0), 0.99),
                    "stage": stage,
                    "message": message,
                    **details,
                }
            )

    def quarantine_stale_import_spools(self) -> dict[str, Any]:
        """Isolate crash leftovers; rebuilds always use sealed source artifacts.

        A spool is never a generation and is never resumed directly.  A spool
        with a live owner PID is left alone.  Legacy/unowned or dead-owner files
        are atomically moved to quarantine so a failed 5GB import cannot be
        mistaken for data.  Operators may delete quarantined files after
        confirming the next generation; this hotfix intentionally retains them.
        """

        self._ensure_write_directories()
        quarantine = self.generations / "import-quarantine"
        quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        moved: list[dict[str, Any]] = []
        skipped_live: list[str] = []
        # Exact main-file formats intentionally exclude SQLite -journal/-wal/
        # -shm sidecars.  Moving a sidecar from a live spool corrupts it.
        candidates = {
            path
            for path in self.generations.iterdir()
            if path.is_file()
            and (
                _ROW_SPOOL_NAME.fullmatch(path.name)
                or _GENERATION_BUILD_NAME.fullmatch(path.name)
            )
        }
        for path in sorted(candidates):
            if not path.is_file() or path.name.endswith(".meta.json"):
                continue
            metadata_path = path.with_name(f"{path.name}.meta.json")
            owner_live = False
            if metadata_path.is_file():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    owner_pid = int(metadata.get("owner_pid") or 0)
                    created_at = datetime.fromisoformat(
                        str(metadata.get("created_at") or "")
                    )
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    age_seconds = (
                        datetime.now(timezone.utc)
                        - created_at.astimezone(timezone.utc)
                    ).total_seconds()
                    fresh = 0 <= age_seconds <= _IMPORT_OWNER_MAX_AGE_SECONDS
                    owner_host = str(metadata.get("owner_host") or "")
                    if fresh and owner_host and owner_host != socket.gethostname():
                        # On a shared directory we cannot inspect a remote PID.
                        # Fresh foreign ownership is therefore fail-safe live/
                        # unknown; only the explicit age policy may expire it.
                        owner_live = True
                    elif owner_pid > 0 and fresh:
                        try:
                            os.kill(owner_pid, 0)
                            expected_start = metadata.get(
                                "process_start_identity"
                            )
                            observed_start = _process_start_identity(owner_pid)
                            owner_live = bool(
                                not expected_start
                                or not observed_start
                                or expected_start == observed_start
                            )
                        except ProcessLookupError:
                            owner_live = False
                        except PermissionError:
                            owner_live = True
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    owner_live = False
            if owner_live:
                skipped_live.append(path.name)
                continue
            destination = quarantine / (
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-"
                f"{path.name.lstrip('.')}"
            )
            try:
                os.replace(path, destination)
                moved_metadata: str | None = None
                if metadata_path.is_file():
                    metadata_destination = destination.with_name(
                        f"{destination.name}.meta.json"
                    )
                    os.replace(metadata_path, metadata_destination)
                    moved_metadata = metadata_destination.name
            except OSError as exc:
                raise ResearchDataStoreError(
                    "stale research import spool cannot be quarantined"
                ) from exc
            moved.append(
                {
                    "source_name": path.name,
                    "quarantine_name": destination.name,
                    "metadata_name": moved_metadata,
                    "byte_count": destination.stat().st_size,
                    "recovery": "rebuild_from_content_addressed_checkpoint",
                    "temporary_kind": (
                        "row_spool"
                        if path.name.startswith(".research-spool.")
                        else "generation_build"
                    ),
                }
            )
        return {
            "schema_version": "research-import-spool-recovery/v1",
            "quarantined": moved,
            "live_spools_skipped": skipped_live,
            "automatic_delete": False,
            "active_generation_changed": False,
        }

    @staticmethod
    def _verify_existing_generation(target: Path, identity: Mapping[str, Any]) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            row = connection.execute(
                "SELECT value_json FROM generation_metadata WHERE key='identity'"
            ).fetchone()
            if row is None:
                raise ResearchDataStoreError("existing research generation is corrupt")
            if json.loads(row[0]) != dict(identity):
                raise ResearchDataStoreError("existing research generation identity changed")
            binding_path = target.with_suffix(".binding.json")
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            checksum = binding.pop("content_sha256", None)
            if (
                checksum != canonical_sha256(binding)
                or binding.get("generation_id") != target.stem
                or binding.get("identity_sha256") != canonical_sha256(identity)
                or binding.get("file_sha256") != file_sha256(target)
                or binding.get("file_size") != target.stat().st_size
            ):
                raise ResearchDataStoreError("existing research generation binding changed")
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise ResearchDataStoreError("existing research generation is unreadable") from exc
        finally:
            if connection is not None:
                connection.close()

    def _write_generation_binding(
        self,
        file_path: Path,
        generation_id: str,
        identity: Mapping[str, Any],
        *,
        binding_path: Path | None = None,
    ) -> None:
        binding = {
            "schema_version": "research-generation-binding/v1",
            "generation_id": generation_id,
            "identity_sha256": canonical_sha256(identity),
            "file_sha256": file_sha256(file_path),
            "file_size": file_path.stat().st_size,
        }
        binding["content_sha256"] = canonical_sha256(binding)
        self._atomic_json(
            binding_path or file_path.with_suffix(".binding.json"), binding
        )

    def _quarantine_unbound_generation(
        self, target: Path, generation_id: str
    ) -> Path:
        """Move a pre-binding crash orphan aside before deterministic rebuild."""

        try:
            pointer = json.loads(self.active_pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pointer = {}
        if pointer.get("generation_id") == generation_id:
            raise ResearchDataStoreError(
                "active research generation binding is missing"
            )
        descriptor, name = tempfile.mkstemp(
            prefix=f".orphan.{generation_id}.", suffix=".sqlite", dir=self.generations
        )
        os.close(descriptor)
        orphan = Path(name)
        os.replace(target, orphan)
        return orphan

    def _write_research_generation(
        self,
        *,
        target: Path,
        identity: Mapping[str, Any],
        generation_id: str,
        classification: str,
        trust_profile: str,
        index_rows: list[tuple[Any, ...]],
        market_rows: Iterable[tuple[Any, ...]],
        status_rows: Iterable[tuple[Any, ...]],
        security_rows: Iterable[tuple[Any, ...]],
        industry_rows: Iterable[tuple[Any, ...]],
        event_rows: Iterable[tuple[Any, ...]],
        benchmark_rows: Iterable[tuple[Any, ...]],
        row_counts: Mapping[str, int],
        progress: ResearchImportProgress | None,
    ) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=".research.", suffix=".sqlite", dir=self.generations
        )
        os.close(descriptor)
        temporary = Path(name)
        temporary_metadata = temporary.with_name(f"{temporary.name}.meta.json")
        _write_import_temp_metadata(
            temporary,
            temporary_metadata,
            kind="generation_build",
        )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(temporary)
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                CREATE TABLE generation_metadata (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE index_membership (
                    pool_id TEXT NOT NULL,
                    observation_month TEXT NOT NULL,
                    vendor_trade_date TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    weight REAL,
                    source_manifest_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (pool_id, observation_month, security_code)
                );
                CREATE INDEX idx_research_pool_date
                ON index_membership(pool_id, vendor_trade_date);
                CREATE TABLE market_daily (
                    trade_date TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    raw_open REAL NOT NULL,
                    raw_high REAL NOT NULL,
                    raw_low REAL NOT NULL,
                    raw_close REAL NOT NULL,
                    raw_pre_close REAL,
                    hfq_open REAL NOT NULL,
                    hfq_high REAL NOT NULL,
                    hfq_low REAL NOT NULL,
                    hfq_close REAL NOT NULL,
                    provider_vol_lots REAL NOT NULL,
                    provider_amount_thousand_cny REAL NOT NULL,
                    volume REAL NOT NULL,
                    amount REAL NOT NULL,
                    adj_factor REAL NOT NULL,
                    turnover_rate REAL,
                    volume_ratio REAL,
                    pe REAL,
                    pb REAL,
                    total_share REAL,
                    float_share REAL,
                    total_mv REAL,
                    circ_mv REAL,
                    daily_basic_json TEXT NOT NULL,
                    daily_manifest_sha256 TEXT NOT NULL,
                    factor_manifest_sha256 TEXT NOT NULL,
                    basic_manifest_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, security_code)
                );
                CREATE INDEX idx_research_market_code_date
                ON market_daily(security_code, trade_date);
                CREATE TABLE trading_status (
                    trade_date TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    suspend_timing TEXT NOT NULL,
                    suspend_type TEXT NOT NULL,
                    source_manifest_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE INDEX idx_research_status_code_date
                ON trading_status(security_code, trade_date);
                CREATE TABLE security_master (
                    security_code TEXT NOT NULL,
                    list_status TEXT NOT NULL,
                    list_date TEXT NOT NULL,
                    delist_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_manifest_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (security_code, list_status, source_manifest_sha256)
                );
                CREATE TABLE industry_observation (
                    dataset TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    effective_start TEXT NOT NULL,
                    effective_end TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_manifest_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE corporate_event (
                    dataset TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_manifest_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE benchmark_daily (
                    index_code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    close REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_manifest_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (index_code, trade_date)
                );
                """
            )
            connection.executemany(
                """INSERT INTO index_membership VALUES (?, ?, ?, ?, ?, ?, ?)""",
                sorted(index_rows),
            )
            connection.executemany(
                """INSERT INTO market_daily VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._progress_rows(
                    market_rows,
                    progress=progress,
                    stage="research_import_write_market",
                    message="正在写入研究行情",
                    fraction_start=0.91,
                    fraction_span=0.035,
                    total=int(row_counts.get("market_daily") or 0),
                ),
            )
            connection.executemany(
                "INSERT INTO trading_status VALUES (?, ?, ?, ?, ?, ?)",
                status_rows,
            )
            connection.executemany(
                "INSERT INTO security_master VALUES (?, ?, ?, ?, ?, ?, ?)",
                security_rows,
            )
            connection.executemany(
                "INSERT INTO industry_observation VALUES (?, ?, ?, ?, ?, ?, ?)",
                industry_rows,
            )
            connection.executemany(
                "INSERT INTO corporate_event VALUES (?, ?, ?, ?, ?, ?)",
                event_rows,
            )
            connection.executemany(
                "INSERT INTO benchmark_daily VALUES (?, ?, ?, ?, ?, ?)",
                benchmark_rows,
            )
            metadata = {
                **dict(identity),
                "identity": dict(identity),
                "generation_id": generation_id,
                "classification": classification,
                "research_trust_profile": trust_profile,
                "created_at": utc_now_iso(),
                "live_eligible": False,
            }
            connection.executemany(
                "INSERT INTO generation_metadata(key, value_json) VALUES (?, ?)",
                [
                    (key, json.dumps(value, ensure_ascii=False, sort_keys=True))
                    for key, value in metadata.items()
                ],
            )
            connection.commit()
            self._report_import_progress(
                progress,
                fraction=0.95,
                stage="research_import_integrity",
                message="正在执行 generation 数据库完整性检查",
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ResearchDataStoreError("research generation integrity check failed")
            connection.close()
            connection = None
            os.chmod(temporary, 0o600)
            self._report_import_progress(
                progress,
                fraction=0.965,
                stage="research_import_binding",
                message="正在生成不可变 generation 绑定摘要",
            )
            self._write_generation_binding(
                temporary,
                generation_id,
                identity,
                binding_path=target.with_suffix(".binding.json"),
            )
            os.replace(temporary, target)
            directory_fd = os.open(self.generations, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except sqlite3.Error as exc:
            raise ResearchDataStoreError("research generation write failed") from exc
        finally:
            if connection is not None:
                connection.close()
            if temporary.exists():
                temporary.unlink()
            temporary_metadata.unlink(missing_ok=True)

    @classmethod
    def _progress_rows(
        cls,
        rows: Iterable[tuple[Any, ...]],
        *,
        progress: ResearchImportProgress | None,
        stage: str,
        message: str,
        fraction_start: float,
        fraction_span: float,
        total: int,
    ) -> Iterable[tuple[Any, ...]]:
        for completed, row in enumerate(rows, start=1):
            if completed == 1 or completed % 50_000 == 0:
                fraction = fraction_start + fraction_span * completed / max(total, 1)
                cls._report_import_progress(
                    progress,
                    fraction=fraction,
                    stage=stage,
                    message=f"{message}：{completed}/{total} 行",
                    completed_rows=completed,
                    total_rows=total,
                )
            yield row

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _active(self) -> tuple[dict[str, Any], Path] | None:
        if not self.active_pointer.exists():
            return None
        try:
            pointer = json.loads(self.active_pointer.read_text(encoding="utf-8"))
            generation_id = str(pointer["generation_id"])
            expected = str(pointer["file_sha256"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ResearchDataStoreError("research active pointer is invalid") from exc
        if not _SHA256.fullmatch(generation_id) or not _SHA256.fullmatch(expected):
            raise ResearchDataStoreError("research active pointer digest is invalid")
        target = self.generations / f"{generation_id}.sqlite"
        if not target.is_file():
            raise ResearchDataStoreError("research generation digest changed")
        metadata = target.stat()
        expected_size = pointer.get("file_size")
        expected_mtime = pointer.get("file_mtime_ns")
        if expected_size is not None or expected_mtime is not None:
            if (
                expected_size != metadata.st_size
                or expected_mtime != metadata.st_mtime_ns
            ):
                raise ResearchDataStoreError("research generation file identity changed")
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
                row = connection.execute(
                    "SELECT value_json FROM generation_metadata WHERE key='generation_id'"
                ).fetchone()
                if row is None or json.loads(row[0]) != generation_id:
                    raise ResearchDataStoreError("research generation identity changed")
            except (sqlite3.Error, json.JSONDecodeError) as exc:
                raise ResearchDataStoreError("research generation identity is unreadable") from exc
            finally:
                if connection is not None:
                    connection.close()
        elif not _verifiedfile_sha256(
            str(target), expected, metadata.st_size, metadata.st_mtime_ns
        ):
            raise ResearchDataStoreError("research generation digest changed")
        return pointer, target

    def _generation(
        self, generation_id: str | None = None
    ) -> tuple[dict[str, Any], Path] | None:
        """Resolve the active or an explicitly bound immutable generation.

        Older deployments must never drift to a newly activated generation.
        Runtime reads validate the sealed sidecar and file digest for active
        and historical generations. Lightweight status reads remain on
        :meth:`_active` and do not perform a full hash on every UI poll.
        """

        if generation_id is None:
            active = self._active()
            if active is None:
                return None
            normalized = str(active[0]["generation_id"])
        else:
            normalized = str(generation_id).strip().lower()
            if _SHA256.fullmatch(normalized) is None:
                raise ResearchDataStoreError("research generation id is invalid")
            try:
                active = self._active()
            except ResearchDataStoreError:
                # An explicitly bound historical deployment remains readable
                # even if an unrelated active pointer is damaged.
                active = None
        target = self.generations / f"{normalized}.sqlite"
        if not target.is_file():
            raise ResearchDataStoreError("research generation is unavailable")
        binding_path = target.with_suffix(".binding.json")
        connection: sqlite3.Connection | None = None
        try:
            decoded_binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if not isinstance(decoded_binding, dict):
                raise ResearchDataStoreError(
                    "research generation binding is invalid"
                )
            binding = dict(decoded_binding)
            binding_checksum = binding.pop("content_sha256", None)
            connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            metadata_rows = connection.execute(
                "SELECT key, value_json FROM generation_metadata "
                "WHERE key IN ('generation_id', 'identity')"
            ).fetchall()
            generation_metadata = {
                str(key): json.loads(value) for key, value in metadata_rows
            }
            identity = generation_metadata.get("identity")
            metadata = target.stat()
            if generation_metadata.get("generation_id") != normalized:
                raise ResearchDataStoreError("research generation identity changed")
            if (
                binding_checksum != canonical_sha256(binding)
                or binding.get("schema_version")
                != "research-generation-binding/v1"
                or binding.get("generation_id") != normalized
                or not isinstance(identity, Mapping)
                or binding.get("identity_sha256") != canonical_sha256(identity)
                or binding.get("file_size") != metadata.st_size
                or not _verifiedfile_sha256(
                    str(target),
                    str(binding.get("file_sha256") or ""),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
            ):
                raise ResearchDataStoreError("research generation binding changed")
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise ResearchDataStoreError("research generation database is corrupt")
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise ResearchDataStoreError("research generation is unreadable") from exc
        finally:
            if connection is not None:
                connection.close()
        active_pointer = (
            dict(active[0])
            if active is not None and active[0]["generation_id"] == normalized
            else {}
        )
        return (
            {
                **active_pointer,
                "generation_id": normalized,
                "provider": "tushare",
                "file_sha256": binding["file_sha256"],
                "file_size": binding["file_size"],
                "historical_generation_binding_verified": True,
            },
            target,
        )

    def verify_active_integrity(self, *, deep: bool = True) -> dict[str, Any]:
        """Explicit integrity audit; the deep hash stays off UI status paths."""

        active = self._active()
        if active is None:
            return {"available": False, "verified": False}
        pointer, target = active
        digest_valid = (
            file_sha256(target) == pointer["file_sha256"] if deep else True
        )
        if not digest_valid:
            raise ResearchDataStoreError("research generation digest changed")
        return {
            "available": True,
            "verified": True,
            "deep": deep,
            "generation_id": pointer["generation_id"],
            "file_sha256": pointer["file_sha256"],
        }

    @staticmethod
    def _status_for_generation(
        pointer: Mapping[str, Any], target: Path
    ) -> dict[str, Any]:
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            summary = connection.execute(
                """SELECT COUNT(*) AS row_count, COUNT(DISTINCT pool_id) AS pool_count,
                MIN(observation_month) AS date_start, MAX(observation_month) AS date_end
                FROM index_membership"""
            ).fetchone()
            metadata_rows = connection.execute(
                "SELECT key, value_json FROM generation_metadata"
            ).fetchall()
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            market_summary = (
                connection.execute(
                    """SELECT COUNT(*) AS row_count,
                    COUNT(DISTINCT security_code) AS security_count,
                    COUNT(DISTINCT trade_date) AS session_count,
                    MIN(trade_date) AS date_start, MAX(trade_date) AS date_end
                    FROM market_daily"""
                ).fetchone()
                if "market_daily" in table_names
                else None
            )
        finally:
            connection.close()
        metadata = {str(row["key"]): json.loads(row["value_json"]) for row in metadata_rows}
        market = {
            "available": bool(market_summary and int(market_summary["row_count"])),
            "schema_version": metadata.get("market_cache_schema_version"),
            "row_count": int(market_summary["row_count"]) if market_summary else 0,
            "security_count": int(market_summary["security_count"]) if market_summary else 0,
            "session_count": int(market_summary["session_count"]) if market_summary else 0,
            "date_start": market_summary["date_start"] if market_summary else None,
            "date_end": market_summary["date_end"] if market_summary else None,
            "provider": "tushare",
            "price_adjustment": "hfq",
        }
        return {
            "available": True,
            "generation_id": pointer["generation_id"],
            "provider": pointer["provider"],
            "row_count": int(summary["row_count"]),
            "pool_count": int(summary["pool_count"]),
            "date_start": summary["date_start"],
            "date_end": summary["date_end"],
            "warnings": list(metadata.get("warnings") or []),
            "live_eligible": False,
            "classification": metadata.get("classification", "single_source_research"),
            "research_trust_profile": metadata.get("research_trust_profile"),
            "candidate_report_sha256": (
                pointer.get("candidate_report_sha256")
                or metadata.get("candidate_report_sha256")
            ),
            "market": market,
            "coverage": metadata.get("coverage"),
            "row_counts": metadata.get("row_counts"),
        }

    def status(self) -> dict[str, Any]:
        active = self._active()
        if active is None:
            return {
                "available": False,
                "generation_id": None,
                "provider": "tushare",
                "row_count": 0,
                "pool_count": 0,
                "date_start": None,
                "date_end": None,
                "warnings": ["research_generation_missing"],
                "live_eligible": False,
            }
        pointer, target = active
        return self._status_for_generation(pointer, target)

    def query_pool(self, pool_id: str, as_of: str) -> dict[str, Any]:
        if pool_id not in {*set(_INDEX_POOL.values()), "all_a"}:
            raise ResearchDataStoreError("research pool is unsupported")
        try:
            requested = date.fromisoformat(as_of)
        except ValueError as exc:
            raise ResearchDataStoreError("research pool date must be YYYY-MM-DD") from exc
        active = self._active()
        if active is None:
            return {"available": False, "reason": "research_generation_missing", "records": []}
        pointer, target = active
        if pool_id == "all_a":
            connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                has_master = connection.execute(
                    """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='security_master'"""
                ).fetchone()
                if has_master is None:
                    return {
                        "available": False,
                        "reason": "research_all_a_master_not_materialized",
                        "records": [],
                    }
                compact = requested.strftime("%Y%m%d")
                records = [
                    dict(row)
                    for row in connection.execute(
                        """WITH active AS (
                            SELECT security_code, list_status, list_date, delist_date,
                            payload_json, source_manifest_sha256, ingested_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY security_code
                                ORDER BY
                                    CASE list_status WHEN 'L' THEN 0 WHEN 'P' THEN 1 ELSE 2 END,
                                    ingested_at DESC, source_manifest_sha256 DESC
                            ) AS rank
                            FROM security_master
                            WHERE list_date<=? AND (delist_date='' OR delist_date>=?)
                        )
                        SELECT security_code, list_date,
                        source_manifest_sha256, ingested_at
                        FROM active WHERE rank=1 ORDER BY security_code""",
                        (compact, compact),
                    ).fetchall()
                ]
            finally:
                connection.close()
            return {
                "available": bool(records),
                "reason": None if records else "research_all_a_history_not_covered",
                "pool_id": pool_id,
                "requested_as_of": as_of,
                "resolved_vendor_trade_date": as_of,
                "generation_id": pointer["generation_id"],
                "candidate_report_sha256": pointer.get(
                    "candidate_report_sha256"
                ),
                "classification": "single_source_research",
                "research_trust_profile": pointer.get("research_trust_profile"),
                "records": records,
                "warnings": [
                    "provider_available_at_missing",
                    "security_master_revision_history_not_proven",
                    "not_live_eligible",
                ],
                "live_eligible": False,
            }
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            resolved = connection.execute(
                """SELECT observation_month, vendor_trade_date
                FROM index_membership
                WHERE pool_id=? AND vendor_trade_date<=?
                ORDER BY vendor_trade_date DESC, observation_month DESC
                LIMIT 1""",
                (pool_id, requested.isoformat()),
            ).fetchone()
            if resolved is None:
                return {"available": False, "reason": "research_pool_history_not_covered", "records": []}
            resolved_month = str(resolved["observation_month"])
            resolved_trade_date = str(resolved["vendor_trade_date"])
            records = [
                dict(row)
                for row in connection.execute(
                    """SELECT security_code, weight, vendor_trade_date,
                    source_manifest_sha256, ingested_at
                    FROM index_membership
                    WHERE pool_id=? AND observation_month=? AND vendor_trade_date=?
                    ORDER BY security_code""",
                    (pool_id, resolved_month, resolved_trade_date),
                ).fetchall()
            ]
        finally:
            connection.close()
        return {
            "available": bool(records),
            "reason": None if records else "research_pool_history_empty",
            "pool_id": pool_id,
            "requested_as_of": as_of,
            "resolved_month": resolved_month,
            "resolved_vendor_trade_date": resolved_trade_date,
            "generation_id": pointer["generation_id"],
            "classification": (
                "vendor_research_trusted"
                if pointer.get("research_trust_profile") == "tushare_research_trusted"
                else "single_source_research"
            ),
            "research_trust_profile": pointer.get("research_trust_profile"),
            "records": records,
            "warnings": [
                "monthly_snapshot_not_exact_intramonth_timeline",
                "provider_available_at_missing",
                "survivorship_bias_reduced_not_eliminated",
                "not_live_eligible",
            ],
            "live_eligible": False,
        }

    def pool_statuses(self, as_of: str | None = None) -> list[dict[str, Any]]:
        requested = as_of or date.today().isoformat()
        statuses: list[dict[str, Any]] = []
        for pool_id in [*sorted(set(_INDEX_POOL.values())), "all_a"]:
            result = self.query_pool(pool_id, requested)
            records = result.get("records") or []
            statuses.append(
                {
                    "pool_id": pool_id,
                    "available": bool(result.get("available")),
                    "record_count": len(records),
                    "requested_as_of": requested,
                    "resolved_month": result.get("resolved_month"),
                    "generation_id": result.get("generation_id"),
                    "classification": result.get("classification"),
                    "warnings": list(result.get("warnings") or []),
                    "live_eligible": False,
                }
            )
        return statuses

    def load_market_frame(
        self,
        *,
        pool_id: str,
        required_start: str,
        required_end: str,
        generation_id: str | None = None,
        fields: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Load one bounded research panel from the active or bound generation."""

        if pool_id not in {*set(_INDEX_POOL.values()), "all_a"}:
            raise ResearchDataStoreError("research market pool is unsupported")
        try:
            start = date.fromisoformat(required_start)
            end = date.fromisoformat(required_end)
        except ValueError as exc:
            raise ResearchDataStoreError("research market dates must be YYYY-MM-DD") from exc
        if start > end:
            raise ResearchDataStoreError("research market start must not exceed end")
        active = self._generation(generation_id)
        if active is None:
            return {
                "frame": None,
                "source_provenance": None,
                "report": {
                    "ready": False,
                    "issues": ["research_generation_missing"],
                    "warnings": [],
                },
            }
        pointer, target = active
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        field_sql = {
            "open": "hfq_open AS open",
            "high": "hfq_high AS high",
            "low": "hfq_low AS low",
            "close": "hfq_close AS close",
            "volume": "volume",
            "amount": "amount",
            "adj_factor": "adj_factor",
            "raw_open": "raw_open",
            "raw_high": "raw_high",
            "raw_low": "raw_low",
            "raw_close": "raw_close",
            "raw_pre_close": "raw_pre_close",
            "turnover_rate": "turnover_rate",
            "volume_ratio": "volume_ratio",
            "pe": "pe",
            "pb": "pb",
            "total_share": "total_share",
            "float_share": "float_share",
            "total_mv": "total_mv",
            "circ_mv": "circ_mv",
        }
        requested_fields = tuple(fields) if fields is not None else tuple(field_sql)
        if not requested_fields or any(field not in field_sql for field in requested_fields):
            raise ResearchDataStoreError("research market fields are unsupported")
        if len(set(requested_fields)) != len(requested_fields):
            raise ResearchDataStoreError("research market fields are duplicated")
        fields = requested_fields
        select_columns = "trade_date, security_code, " + ", ".join(
            field_sql[field] for field in fields
        )
        security_lifecycle_rows: list[sqlite3.Row] = []
        try:
            has_market = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_daily'"
            ).fetchone()
            if has_market is None:
                market_frame = pd.DataFrame()
                metadata_rows = connection.execute(
                    "SELECT key, value_json FROM generation_metadata"
                ).fetchall()
            else:
                memberships = (
                    connection.execute(
                        """SELECT vendor_trade_date, security_code
                        FROM index_membership
                        WHERE pool_id=? AND vendor_trade_date<=?
                        ORDER BY vendor_trade_date, security_code""",
                        (pool_id, required_end),
                    ).fetchall()
                    if pool_id != "all_a"
                    else []
                )
                if pool_id == "all_a":
                    security_lifecycle_rows = connection.execute(
                        """SELECT security_code, list_status, list_date, delist_date,
                        source_manifest_sha256, ingested_at
                        FROM security_master ORDER BY security_code, list_date"""
                    ).fetchall()
                metadata_rows = connection.execute(
                    "SELECT key, value_json FROM generation_metadata"
                ).fetchall()
                session_count = int(
                    connection.execute(
                        """SELECT COUNT(DISTINCT trade_date) FROM market_daily
                        WHERE trade_date>=? AND trade_date<=?""",
                        (required_start, required_end),
                    ).fetchone()[0]
                )
                estimated_codes = (
                    int(
                        connection.execute(
                            """SELECT COUNT(DISTINCT security_code) FROM market_daily
                            WHERE trade_date>=? AND trade_date<=?""",
                            (required_start, required_end),
                        ).fetchone()[0]
                    )
                    if pool_id == "all_a"
                    else max(
                        (
                            len(
                                {
                                    str(row["security_code"])
                                    for row in memberships
                                    if str(row["vendor_trade_date"]) == snapshot
                                }
                            )
                            for snapshot in {
                                str(row["vendor_trade_date"])
                                for row in memberships
                            }
                        ),
                        default=0,
                    )
                )
                dense_bytes = session_count * estimated_codes * len(fields) * 8
                estimated_bytes = dense_bytes * 4
                if estimated_bytes > 512 * 1024 * 1024:
                    market_frame = pd.DataFrame()
                    memory_budget_exceeded = {
                        "estimated_bytes": estimated_bytes,
                        "dense_frame_bytes": dense_bytes,
                        "copy_multiplier": 4,
                        "limit_bytes": 512 * 1024 * 1024,
                        "session_count": session_count,
                        "estimated_security_count": estimated_codes,
                    }
                else:
                    memory_budget_exceeded = None
                    chunks: list[pd.DataFrame] = []
                    if pool_id == "all_a":
                        chunks.append(
                            pd.read_sql_query(
                                f"""SELECT {select_columns} FROM market_daily
                                WHERE trade_date>=? AND trade_date<=?
                                ORDER BY trade_date, security_code""",
                                connection,
                                params=(required_start, required_end),
                            )
                        )
                    else:
                        snapshots: dict[str, set[str]] = {}
                        for row in memberships:
                            snapshots.setdefault(
                                str(row["vendor_trade_date"]), set()
                            ).add(str(row["security_code"]))
                        snapshot_dates = sorted(snapshots)
                        for position, snapshot in enumerate(snapshot_dates):
                            interval_start = max(required_start, snapshot)
                            interval_end = required_end
                            if position + 1 < len(snapshot_dates):
                                next_date = date.fromisoformat(
                                    snapshot_dates[position + 1]
                                )
                                interval_end = min(
                                    interval_end,
                                    (next_date.fromordinal(next_date.toordinal() - 1)).isoformat(),
                                )
                            if interval_start > interval_end:
                                continue
                            codes = sorted(snapshots[snapshot])
                            placeholders = ",".join("?" for _ in codes)
                            chunks.append(
                                pd.read_sql_query(
                                    f"""SELECT {select_columns} FROM market_daily
                                    WHERE trade_date>=? AND trade_date<=?
                                    AND security_code IN ({placeholders})
                                    ORDER BY trade_date, security_code""",
                                    connection,
                                    params=(interval_start, interval_end, *codes),
                                )
                            )
                    market_frame = (
                        pd.concat(chunks, ignore_index=True)
                        if chunks
                        else pd.DataFrame()
                    )
        finally:
            connection.close()
        metadata = {
            str(row["key"]): json.loads(row["value_json"])
            for row in metadata_rows
        }
        if "memory_budget_exceeded" in locals() and memory_budget_exceeded is not None:
            return {
                "frame": None,
                "source_provenance": None,
                "report": {
                    "ready": False,
                    "schema_version": RESEARCH_MARKET_CACHE_SCHEMA,
                    "generation_id": pointer["generation_id"],
                    "issues": ["research_window_memory_budget_exceeded"],
                    "warnings": list(metadata.get("warnings") or []),
                    "memory_estimate": memory_budget_exceeded,
                    "source_providers": ["tushare"],
                    "live_eligible": False,
                },
            }
        if market_frame.empty:
            return {
                "frame": None,
                "source_provenance": None,
                "report": {
                    "ready": False,
                    "schema_version": RESEARCH_MARKET_CACHE_SCHEMA,
                    "generation_id": pointer["generation_id"],
                    "issues": ["research_market_window_not_covered"],
                    "warnings": list(metadata.get("warnings") or []),
                    "source_providers": ["tushare"],
                    "live_eligible": False,
                },
            }

        frame = market_frame.set_index(["trade_date", "security_code"])[
            list(fields)
        ].unstack("security_code")
        frame.columns = frame.columns.swaplevel(0, 1)
        frame.columns.names = ["security_code", "field"]
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="date")
        frame = frame.sort_index().sort_index(axis=1)
        observed_start = frame.index.min().strftime("%Y-%m-%d")
        observed_end = frame.index.max().strftime("%Y-%m-%d")
        warnings = list(metadata.get("warnings") or [])
        if observed_start > required_start or observed_end < required_end:
            warnings.append("requested_window_partially_covered")
        timeline_identity: dict[str, Any] | None = None
        if pool_id != "all_a":
            snapshot_dates = sorted(snapshots)
            date_text = tuple(day.strftime("%Y-%m-%d") for day in frame.index)
            members_by_date: list[tuple[str, ...]] = []
            for day in date_text:
                eligible = [snapshot for snapshot in snapshot_dates if snapshot <= day]
                if not eligible:
                    members_by_date = []
                    break
                members_by_date.append(tuple(sorted(snapshots[eligible[-1]])))
            if members_by_date and all(members_by_date):
                union_codes = tuple(
                    sorted({code for members in members_by_date for code in members})
                )
                source_batches = (
                    {
                        "batch_id": f"research-generation:{pointer['generation_id']}",
                        "batch_digest": pointer["generation_id"],
                        "coverage_from": date_text[0],
                        "coverage_to": date_text[-1],
                    },
                )
                timeline = PointInTimeUniverseTimeline(
                    pool_id=pool_id,
                    dates=date_text,
                    members_by_date=tuple(members_by_date),
                    union_codes=union_codes,
                    source_batches=source_batches,
                    timeline_hash=_timeline_hash(
                        pool_id=pool_id,
                        dates=date_text,
                        members_by_date=members_by_date,
                    ),
                    coverage_from=date_text[0],
                    coverage_to=date_text[-1],
                    expected_count=None,
                    as_known_at=None,
                    bitemporal_availability_verified=False,
                )
                timeline_identity = timeline.identity()
            else:
                warnings.append("research_membership_timeline_incomplete")
        else:
            date_text = tuple(day.strftime("%Y-%m-%d") for day in frame.index)
            observed_codes = set(frame.columns.get_level_values(0).astype(str))
            lifecycle_records: dict[
                str, list[tuple[str, str, str, str, str]]
            ] = {}
            for row in security_lifecycle_rows:
                code = str(row["security_code"])
                if code not in observed_codes:
                    continue
                list_status = str(row["list_status"] or "").upper()
                list_date = str(row["list_date"] or "")
                delist_date = str(row["delist_date"] or "")
                if list_status in {"L", "D"} and re.fullmatch(
                    r"\d{8}", list_date
                ):
                    lifecycle_records.setdefault(code, []).append(
                        (
                            list_status,
                            list_date,
                            delist_date,
                            str(row["ingested_at"] or ""),
                            str(row["source_manifest_sha256"] or ""),
                        )
                    )
            # A generation can retain more than one status observation for a
            # security.  Use the newest observation (and prefer ``D`` on an
            # exact timestamp tie) so an older ``L`` row cannot make a later
            # ``D`` row look indefinitely listed.
            lifecycle: dict[str, tuple[str, str]] = {}
            invalid_status_codes: set[str] = set()
            for code, records in lifecycle_records.items():
                valid_records = []
                for record in records:
                    if record[0] == "D" and re.fullmatch(
                        r"\d{8}", record[2]
                    ) is None:
                        invalid_status_codes.add(code)
                        continue
                    valid_records.append(record)
                if not valid_records:
                    continue
                selected = max(
                    valid_records,
                    key=lambda record: (
                        record[3],
                        record[0] == "D",
                        record[4],
                    ),
                )
                lifecycle[code] = (
                    selected[1],
                    selected[2] if selected[0] == "D" else "",
                )
            missing_master = sorted(observed_codes - set(lifecycle))
            if missing_master:
                warnings.append("all_a_security_master_coverage_incomplete")
            if invalid_status_codes:
                warnings.append("all_a_security_master_status_incomplete")
            members_by_date = []
            for day in (date_text if not missing_master else ()):
                compact = day.replace("-", "")
                members = tuple(
                    sorted(
                        code
                        for code, (listed, delisted) in lifecycle.items()
                        if listed <= compact
                        and (not delisted or delisted >= compact)
                    )
                )
                if not members:
                    members_by_date = []
                    break
                members_by_date.append(members)
            if members_by_date:
                union_codes = tuple(
                    sorted({code for members in members_by_date for code in members})
                )
                source_batches = (
                    {
                        "batch_id": f"research-generation:{pointer['generation_id']}",
                        "batch_digest": pointer["generation_id"],
                        "coverage_from": date_text[0],
                        "coverage_to": date_text[-1],
                    },
                )
                timeline = PointInTimeUniverseTimeline(
                    pool_id=pool_id,
                    dates=date_text,
                    members_by_date=tuple(members_by_date),
                    union_codes=union_codes,
                    source_batches=source_batches,
                    timeline_hash=_timeline_hash(
                        pool_id=pool_id,
                        dates=date_text,
                        members_by_date=members_by_date,
                    ),
                    coverage_from=date_text[0],
                    coverage_to=date_text[-1],
                    expected_count=None,
                    as_known_at=None,
                    bitemporal_availability_verified=False,
                )
                timeline_identity = timeline.identity()
                warnings.append(
                    "all_a_membership_derived_from_security_lifecycle"
                )
            else:
                warnings.append("research_membership_timeline_incomplete")
        source_provenance = {
            "schema_version": "research-cache-source-provenance/v1",
            "provider": "tushare",
            "endpoint": "tushare_pro:daily+adj_factor+daily_basic+suspend_d",
            "adjustment": "hfq",
            "transformation": "raw_ohlc_times_adj_factor",
            "generation_id": pointer["generation_id"],
            "checkpoint_sha256": metadata.get("checkpoint_sha256"),
            "source_manifest_count": len(metadata.get("source_manifests") or []),
            "source_manifests_sha256": canonical_sha256(
                metadata.get("source_manifests") or []
            ),
            "cross_validated": False,
            "live_eligible": False,
            "units": {
                "provider_vol": "lots_100_shares",
                "provider_amount": "thousand_cny",
                "volume": "shares",
                "amount": "cny",
                "provider_total_share": "ten_thousand_shares",
                "provider_float_share": "ten_thousand_shares",
                "provider_total_mv": "ten_thousand_cny",
                "provider_circ_mv": "ten_thousand_cny",
                "total_share": "shares",
                "float_share": "shares",
                "total_mv": "cny",
                "circ_mv": "cny",
            },
            "transformations": [
                "hfq_ohlc=raw_ohlc*adj_factor",
                "volume_shares=provider_vol_lots*100",
                "amount_cny=provider_amount_thousand_cny*1000",
                "share_counts=provider_ten_thousand_shares*10000",
                "market_values_cny=provider_ten_thousand_cny*10000",
            ],
        }
        source_provenance["content_sha256"] = canonical_sha256(source_provenance)
        return {
            "frame": frame,
            "source_provenance": source_provenance,
            "report": {
                "ready": True,
                "schema_version": RESEARCH_MARKET_CACHE_SCHEMA,
                "generation_id": pointer["generation_id"],
                "candidate_report_sha256": pointer.get(
                    "candidate_report_sha256"
                ),
                "date_start": observed_start,
                "date_end": observed_end,
                "n_dates": len(frame.index),
                "n_stocks": len(set(frame.columns.get_level_values(0))),
                "fields": list(fields),
                "price_adjustment": "hfq",
                "source_providers": ["tushare"],
                "issues": [],
                "warnings": list(dict.fromkeys(warnings)),
                "timeline_identity": timeline_identity,
                "live_eligible": False,
            },
        }

    def load_benchmark(
        self,
        *,
        index_code: str,
        required_start: str,
        required_end: str,
        generation_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(index_code).strip().upper()
        if re.fullmatch(r"\d{6}", normalized):
            normalized = f"{normalized}.SH"
        if normalized not in _INDEX_POOL:
            raise ResearchDataStoreError("research benchmark is unsupported")
        try:
            start = date.fromisoformat(required_start)
            end = date.fromisoformat(required_end)
        except ValueError as exc:
            raise ResearchDataStoreError("research benchmark dates must be YYYY-MM-DD") from exc
        if start > end:
            raise ResearchDataStoreError("research benchmark start must not exceed end")
        active = self._generation(generation_id)
        if active is None:
            return {
                "series": None,
                "report": {"ready": False, "issues": ["research_generation_missing"]},
            }
        pointer, target = active
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmark_daily'"
            ).fetchone()
            rows = (
                connection.execute(
                    """SELECT trade_date, close, source_manifest_sha256
                    FROM benchmark_daily WHERE index_code=?
                    AND trade_date>=? AND trade_date<=?
                    ORDER BY trade_date""",
                    (normalized, required_start, required_end),
                ).fetchall()
                if exists is not None
                else []
            )
        finally:
            connection.close()
        if not rows:
            return {
                "series": None,
                "report": {
                    "ready": False,
                    "generation_id": pointer["generation_id"],
                    "index_code": normalized,
                    "issues": ["benchmark_index_daily_not_materialized"],
                    "warnings": ["benchmark_missing_warning_only_for_research"],
                    "live_eligible": False,
                },
            }
        series = pd.Series(
            [float(row["close"]) for row in rows],
            index=pd.DatetimeIndex(
                pd.to_datetime([str(row["trade_date"]) for row in rows]),
                name="date",
            ),
            name="close",
            dtype="float64",
        )
        manifests = sorted({str(row["source_manifest_sha256"]) for row in rows})
        observed_start = series.index.min().date()
        observed_end = series.index.max().date()
        observed_dates = [timestamp.date() for timestamp in series.index]
        maximum_internal_gap_days = max(
            (
                (right - left).days
                for left, right in zip(observed_dates, observed_dates[1:], strict=False)
            ),
            default=0,
        )
        coverage_complete = bool(
            (observed_start - start).days <= 7
            and (end - observed_end).days <= 7
            and maximum_internal_gap_days <= 7
        )
        return {
            "series": series,
            "report": {
                "ready": True,
                "coverage_complete": coverage_complete,
                "generation_id": pointer["generation_id"],
                "index_code": normalized,
                "date_start": series.index.min().strftime("%Y-%m-%d"),
                "date_end": series.index.max().strftime("%Y-%m-%d"),
                "observations": len(series),
                "maximum_internal_gap_days": maximum_internal_gap_days,
                "source_provider": "tushare",
                "source_manifests_sha256": canonical_sha256(manifests),
                "source_manifest_count": len(manifests),
                "warnings": [
                    "single_source_not_cross_validated",
                    *(
                        []
                        if coverage_complete
                        else ["requested_benchmark_window_partially_covered"]
                    ),
                ],
                "live_eligible": False,
            },
        }

    def conflict_report(self) -> dict[str, Any]:
        """Compare the latest research snapshot to activated local PIT evidence."""

        status = self.status()
        conflicts: list[dict[str, Any]] = []
        comparisons: list[dict[str, Any]] = []
        uncompared: list[dict[str, Any]] = []
        if not status["available"]:
            return {
                "schema_version": RESEARCH_CONFLICT_REPORT_SCHEMA,
                "status": "insufficient_sources",
                "comparisons": [],
                "conflicts": [],
                "conflict_count": 0,
                "uncompared": [
                    {
                        "left_source": "tushare",
                        "right_source": "activated_local",
                        "reason": "research_generation_unavailable",
                    }
                ],
            }
        from backend.data.point_in_time_master import (
            PointInTimeIntegrityError,
            PointInTimeMasterStore,
            PointInTimeValidationError,
        )

        local_store = PointInTimeMasterStore()
        requested = date.today().isoformat()
        for pool_id in sorted(set(_INDEX_POOL.values())):
            research = self.query_pool(pool_id, requested)
            research_codes = {row["security_code"].split(".")[0] for row in research["records"]}
            research_weights = {
                str(row["security_code"]).split(".")[0]: row.get("weight")
                for row in research["records"]
            }
            vendor_date = max((str(row["vendor_trade_date"]) for row in research["records"]), default=requested)
            try:
                local = local_store.query_as_of(
                    domain="index_membership", scope_id=pool_id, as_of=vendor_date
                )
                local_codes = {
                    str(row["security_code"]).split(".")[0]
                    for row in local.get("records", [])
                } if local.get("available") else set()
                local_weights = {
                    str(row["security_code"]).split(".")[0]: row.get("weight")
                    for row in local.get("records", [])
                } if local.get("available") else {}
            except (PointInTimeIntegrityError, PointInTimeValidationError):
                local_codes = set()
                local_weights = {}
            if not local_codes:
                comparison = {
                    "left_source": "tushare",
                    "right_source": "activated_local",
                    "pool_id": pool_id,
                    "as_of": vendor_date,
                    "status": "right_source_unavailable",
                    "left_count": len(research_codes),
                    "right_count": 0,
                    "independent": False,
                    "lineage_status": "same_or_unproven_lineage",
                }
                comparisons.append(comparison)
                uncompared.append(
                    {
                        **comparison,
                        "reason": "right_source_unavailable",
                        "fields": ["membership", "weight", "ohlcv", "status"],
                    }
                )
                continue
            only_tushare = sorted(research_codes - local_codes)
            only_local = sorted(local_codes - research_codes)
            weight_conflicts: list[dict[str, Any]] = []
            weight_uncompared = 0
            for code in sorted(research_codes & local_codes):
                left = research_weights.get(code)
                right = local_weights.get(code)
                if left is None or right is None:
                    weight_uncompared += 1
                    continue
                try:
                    delta = abs(float(left) - float(right))
                except (TypeError, ValueError, OverflowError):
                    weight_uncompared += 1
                    continue
                if delta > 1e-8:
                    weight_conflicts.append(
                        {
                            "security_code": code,
                            "field": "weight",
                            "left_value": float(left),
                            "right_value": float(right),
                            "absolute_delta": delta,
                            "tolerance": 1e-8,
                        }
                    )
            comparison = {
                "left_source": "tushare",
                "right_source": "activated_local",
                "pool_id": pool_id,
                "as_of": vendor_date,
                "status": (
                    "conflict"
                    if only_tushare or only_local or weight_conflicts
                    else "match_not_independent"
                ),
                "left_count": len(research_codes),
                "right_count": len(local_codes),
                "only_left_count": len(only_tushare),
                "only_right_count": len(only_local),
                "only_left_sample": only_tushare[:20],
                "only_right_sample": only_local[:20],
                "weight_conflict_count": len(weight_conflicts),
                "weight_conflict_sample": weight_conflicts[:20],
                "weight_uncompared_count": weight_uncompared,
                "independent": False,
                "lineage_status": "same_or_unproven_lineage",
                "cross_validated": False,
                "left_values_sha256": canonical_sha256(
                    {code: research_weights.get(code) for code in sorted(research_codes)}
                ),
                "right_values_sha256": canonical_sha256(
                    {code: local_weights.get(code) for code in sorted(local_codes)}
                ),
            }
            comparisons.append(comparison)
            if comparison["status"] == "conflict":
                conflicts.append(comparison)
            uncompared.append(
                {
                    "left_source": "tushare",
                    "right_source": "activated_local",
                    "pool_id": pool_id,
                    "as_of": vendor_date,
                    "reason": "not_independently_cross_validated",
                    "fields": ["ohlcv", "daily_basic", "suspension", "events"],
                }
            )
        sources_sufficient = bool(comparisons) and any(
            row.get("right_count", 0) for row in comparisons
        )
        return {
            "schema_version": RESEARCH_CONFLICT_REPORT_SCHEMA,
            "status": (
                "insufficient_sources"
                if not sources_sufficient
                else "conflicts_observed"
                if conflicts
                else "completed_with_uncompared"
            ),
            "comparisons": comparisons,
            "conflicts": conflicts,
            "conflict_count": len(conflicts),
            "uncompared": uncompared,
            "cross_validated": False,
            "interpretation": "research warning only; live eligibility remains false",
        }


def research_source_report(root: str | Path | None = None) -> dict[str, Any]:
    store = ResearchDataStore(root)
    try:
        tushare_status = store.status()
    except (ResearchDataStoreError, ProviderArtifactError) as exc:
        tushare_status = {
            "available": False,
            "generation_id": None,
            "row_count": 0,
            "date_start": None,
            "date_end": None,
            "warnings": [type(exc).__name__],
            "live_eligible": False,
        }
    try:
        import importlib.util

        baostock_installed = importlib.util.find_spec("baostock") is not None
    except (ImportError, ValueError):
        baostock_installed = False
    local_available = False
    local_count = 0
    local_last_observation: str | None = None
    try:
        from backend.data.point_in_time_master import PointInTimeMasterStore

        local_store = PointInTimeMasterStore()
        for pool_id in sorted(set(_INDEX_POOL.values())):
            observation = local_store.resolve_display_observation(
                domain="index_membership",
                scope_id=pool_id,
                requested_as_of=date.today().isoformat(),
            )
            query = observation.get("query") or {}
            records = query.get("records") or []
            if query.get("available") and records:
                local_available = True
                local_count += len(records)
                resolved = observation.get("resolved_as_of")
                if resolved and (
                    local_last_observation is None
                    or str(resolved) > local_last_observation
                ):
                    local_last_observation = str(resolved)
    except Exception:
        local_available = False
        local_count = 0
        local_last_observation = None

    sources = [
        {
            "source_id": "tushare",
            "display_name": "Tushare Pro",
            "installed": True,
            "configured": bool(settings.TUSHARE_TOKEN.get_secret_value()),
            "available": bool(tushare_status.get("available")),
            "refreshable": True,
            "classification": tushare_status.get(
                "classification", "single_source_research"
            ),
            "research_trust_profile": tushare_status.get(
                "research_trust_profile",
                "single_source_research_warning_only",
            ),
            "capabilities": ["historical_index_membership", "daily", "adjustment_factor", "daily_basic", "security_master", "calendar", "corporate_actions", "benchmark_index_daily"],
            "last_observation": (
                (tushare_status.get("market") or {}).get("date_end")
                or tushare_status.get("date_end")
            ),
            "row_count": int(
                sum(
                    int(value or 0)
                    for value in (tushare_status.get("row_counts") or {}).values()
                )
                or tushare_status.get("row_count")
                or 0
            ),
            "generation_id": tushare_status.get("generation_id"),
            "warnings": list(tushare_status.get("warnings") or []),
            "datasets": [
                {
                    "dataset": "index_membership",
                    "status": (
                        "retained_research_generation"
                        if tushare_status.get("available")
                        else "not_materialized"
                    ),
                    "record_count": int(tushare_status.get("row_count") or 0),
                },
                *[
                    {
                        "dataset": dataset,
                        "status": (
                            "retained_research_generation"
                            if int(
                                (tushare_status.get("row_counts") or {}).get(
                                    {
                                        "daily": "market_daily",
                                        "adjustment_factor": "market_daily",
                                        "daily_basic": "market_daily",
                                        "security_master": "security_master",
                                        "corporate_actions": "events",
                                        "benchmark_index_daily": "benchmark",
                                    }.get(dataset, dataset),
                                    0,
                                )
                                or 0
                            )
                            else "collection_supported_not_materialized"
                        ),
                        "record_count": int(
                            (tushare_status.get("row_counts") or {}).get(
                                {
                                    "daily": "market_daily",
                                    "adjustment_factor": "market_daily",
                                    "daily_basic": "market_daily",
                                    "security_master": "security_master",
                                    "corporate_actions": "events",
                                    "benchmark_index_daily": "benchmark",
                                }.get(dataset, dataset),
                                0,
                            )
                            or 0
                        ),
                    }
                    for dataset in (
                        "daily",
                        "adjustment_factor",
                        "daily_basic",
                        "security_master",
                        "calendar",
                        "corporate_actions",
                        "benchmark_index_daily",
                    )
                ],
            ],
            "live_eligible": False,
        },
        {
            "source_id": "baostock",
            "display_name": "BaoStock",
            "installed": baostock_installed,
            "configured": baostock_installed,
            "available": False,
            "refreshable": False,
            "classification": "cross_check_only",
            "capabilities": ["daily", "trade_status_cross_check"],
            "last_observation": None,
            "row_count": 0,
            "generation_id": None,
            "warnings": [
                "no_retained_observation",
                "no_historical_index_membership",
                "cross_check_only",
            ],
            "datasets": [
                {"dataset": "daily", "status": "not_retained", "record_count": 0},
                {
                    "dataset": "trade_status_cross_check",
                    "status": "provider_unverified",
                    "record_count": 0,
                },
            ],
            "live_eligible": False,
        },
        {
            "source_id": "activated_local",
            "display_name": "本地已激活数据",
            "installed": True,
            "configured": True,
            "available": local_available,
            "refreshable": False,
            "classification": "local_runtime",
            "capabilities": ["activated_index_membership", "legacy_price_cache", "canonical_price_ledger_if_bound"],
            "last_observation": local_last_observation,
            "row_count": local_count,
            "generation_id": None,
            "warnings": (
                ["availability_varies_by_pool_and_date"]
                if local_available
                else ["activated_local_pool_unavailable"]
            ),
            "datasets": [
                {
                    "dataset": "activated_index_membership",
                    "status": "available" if local_available else "unavailable",
                    "record_count": local_count,
                }
            ],
            "live_eligible": False,
        },
    ]
    return {
        "schema_version": RESEARCH_SOURCE_REPORT_SCHEMA,
        "mode": "research_and_paper_warning_only",
        "live_trading_policy": "hard_locked",
        "sources": sources,
    }
