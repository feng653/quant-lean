"""Durable, bounded Tushare PIT candidate backfill into quarantine only.

The collector deliberately depends only on the candidate artifact boundary.  It
cannot import, approve, activate, or mutate runtime PIT/cache databases.  A run
is a sequence of small deterministic tasks; every successful provider response
is content-addressed before the mutable checkpoint advances.
"""

from __future__ import annotations

import calendar
import fcntl
import json
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from backend.data.provider_artifacts import (
    ProviderArtifactError,
    canonical_json_bytes,
    canonical_sha256,
    utc_now,
)
from backend.data.sources.tushare_candidate import (
    TushareCandidateClient,
    TushareCandidateError,
)


TUSHARE_PIT_BACKFILL_SCHEMA = "tushare-pit-candidate-backfill/v4"
TUSHARE_PIT_BACKFILL_CHECKPOINT_SCHEMA = (
    "tushare-pit-candidate-backfill-checkpoint/v3"
)
_LEGACY_CHECKPOINT_SCHEMAS = frozenset(
    {
        "tushare-pit-candidate-backfill-checkpoint/v1",
        "tushare-pit-candidate-backfill-checkpoint/v2",
    }
)
DEFAULT_FIRST_MONTH = "2016-01"
DEFAULT_LAST_COMPLETE_MONTH = "2026-06"
FOUR_INDEX_CODES = ("000300.SH", "000905.SH", "000906.SH", "000852.SH")
_INDEX_MINIMUM_MEMBERS = {
    "000300.SH": 300,
    "000905.SH": 500,
    "000906.SH": 800,
    "000852.SH": 1_000,
}
_MONTH = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CALLS_PER_INVOCATION = 128
_SESSION_RECONCILIATION_SCHEMA = "tushare-session-universe-intersection/v2"
_SUSPEND_INTERVAL = re.compile(
    r"^(?P<start>\d{2}:\d{2})\s*(?:-|—|~|至)\s*(?P<end>\d{2}:\d{2})$"
)
_FULL_DAY_SUSPEND_LABELS = frozenset({"全天", "全日", "09:30-15:00"})


class TusharePitBackfillError(RuntimeError):
    """A backfill plan/checkpoint violates the quarantine contract."""


@dataclass(frozen=True)
class BackfillTask:
    task_id: str
    category: str
    dataset: str
    params: dict[str, Any]
    required: bool

    @classmethod
    def build(
        cls,
        *,
        category: str,
        dataset: str,
        params: Mapping[str, Any],
        required: bool,
    ) -> "BackfillTask":
        body = {
            "category": str(category),
            "dataset": str(dataset),
            "params": dict(params),
            "required": bool(required),
        }
        return cls(
            task_id=canonical_sha256(body)[:32],
            category=body["category"],
            dataset=body["dataset"],
            params=body["params"],
            required=body["required"],
        )

    def public_scope(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "dataset": self.dataset,
            "params": self.params,
            "required": self.required,
        }


@dataclass(frozen=True)
class TusharePitBackfillPlan:
    first_month: str = DEFAULT_FIRST_MONTH
    last_month: str = DEFAULT_LAST_COMPLETE_MONTH
    sample_size: int = 30
    event_sample_size: int = 10
    market_chunk_months: int = 12

    def __post_init__(self) -> None:
        first = _parse_month(self.first_month, "first_month")
        last = _parse_month(self.last_month, "last_month")
        if first > last:
            raise TusharePitBackfillError("first_month must not be after last_month")
        if _month_end(last) >= date.today().replace(day=1):
            raise TusharePitBackfillError("last_month must be a fully elapsed month")
        if not 1 <= self.sample_size <= 100:
            raise TusharePitBackfillError("sample_size must be between 1 and 100")
        if not 0 <= self.event_sample_size <= self.sample_size:
            raise TusharePitBackfillError(
                "event_sample_size must be between zero and sample_size"
            )
        if not 1 <= self.market_chunk_months <= 12:
            raise TusharePitBackfillError(
                "market_chunk_months must be between 1 and 12"
            )

    @property
    def run_id(self) -> str:
        return canonical_sha256(self.public_scope())[:32]

    def public_scope(self) -> dict[str, Any]:
        return {
            "first_month": self.first_month,
            "last_month": self.last_month,
            "four_index_codes": list(FOUR_INDEX_CODES),
            "sample_size": self.sample_size,
            "event_sample_size": self.event_sample_size,
            "market_chunk_months": self.market_chunk_months,
            "classification": "quarantine",
        }

    def foundation_tasks(self) -> list[BackfillTask]:
        tasks: list[BackfillTask] = []
        for month in _iter_months(self.first_month, self.last_month):
            start, end = _month_range(month)
            for index_code in FOUR_INDEX_CODES:
                tasks.append(
                    BackfillTask.build(
                        category="index_weight_monthly",
                        dataset="index_weight",
                        params={
                            "index_code": index_code,
                            "start_date": start,
                            "end_date": end,
                        },
                        required=True,
                    )
                )
        for status in ("L", "D", "P"):
            tasks.append(
                BackfillTask.build(
                    category="security_master",
                    dataset="stock_basic",
                    params={"list_status": status},
                    required=status == "L",
                )
            )
        for start, end in _chunk_ranges(
            self.first_month, self.last_month, chunk_months=12
        ):
            tasks.append(
                BackfillTask.build(
                    category="trading_calendar",
                    dataset="trade_cal",
                    params={
                        "exchange": "SSE",
                        "start_date": start,
                        "end_date": end,
                    },
                    required=True,
                )
            )
            for index_code in FOUR_INDEX_CODES:
                tasks.append(
                    BackfillTask.build(
                        category="benchmark_index_daily",
                        dataset="index_daily",
                        params={
                            "ts_code": index_code,
                            "start_date": start,
                            "end_date": end,
                        },
                        required=False,
                    )
                )
        tasks.append(
            BackfillTask.build(
                category="industry_catalog",
                dataset="sw_classify",
                params={"level": "L1", "src": "SW2021"},
                required=False,
            )
        )
        return tasks

    def session_market_tasks(self, open_sessions: Sequence[str]) -> list[BackfillTask]:
        """Plan validated all-market cross-sections for canonical open sessions."""

        tasks: list[BackfillTask] = []
        for trade_date in sorted(set(open_sessions)):
            if not re.fullmatch(r"\d{8}", trade_date):
                raise TusharePitBackfillError(
                    "canonical trading session must be YYYYMMDD"
                )
            for dataset in ("daily", "adj_factor", "daily_basic", "suspend_d"):
                tasks.append(
                    BackfillTask.build(
                        category="all_market_session_cross_section",
                        dataset=dataset,
                        params={"trade_date": trade_date},
                        required=dataset in {"daily", "adj_factor"},
                    )
                )
        return tasks

    def full_universe_metadata_tasks(
        self, membership_months: Mapping[str, Sequence[str]]
    ) -> list[BackfillTask]:
        """Keep slowly changing industry/events on the historical member boundary."""

        tasks: list[BackfillTask] = []
        for code in sorted(membership_months):
            if not membership_months[code]:
                raise TusharePitBackfillError(
                    f"historical constituent {code} has no membership month"
                )
            tasks.append(
                BackfillTask.build(
                    category="full_universe_industry_membership",
                    dataset="sw_membership",
                    params={"ts_code": code, "is_new": "N"},
                    required=False,
                )
            )
            for dataset in ("dividend", "namechange"):
                tasks.append(
                    BackfillTask.build(
                        category="full_universe_corporate_event",
                        dataset=dataset,
                        params={"ts_code": code},
                        required=False,
                    )
                )
        return tasks


def _parse_month(value: str, field: str) -> date:
    if not _MONTH.fullmatch(str(value)):
        raise TusharePitBackfillError(f"{field} must be YYYY-MM")
    return datetime.strptime(value, "%Y-%m").date()


def _month_end(first: date) -> date:
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _month_range(month: str) -> tuple[str, str]:
    first = _parse_month(month, "month")
    return first.strftime("%Y%m%d"), _month_end(first).strftime("%Y%m%d")


def _iter_months(first_month: str, last_month: str) -> Iterator[str]:
    current = _parse_month(first_month, "first_month")
    last = _parse_month(last_month, "last_month")
    while current <= last:
        yield current.strftime("%Y-%m")
        current = (
            current.replace(year=current.year + 1, month=1)
            if current.month == 12
            else current.replace(month=current.month + 1)
        )


def _chunk_ranges(
    first_month: str, last_month: str, *, chunk_months: int
) -> list[tuple[str, str]]:
    months = list(_iter_months(first_month, last_month))
    ranges: list[tuple[str, str]] = []
    for offset in range(0, len(months), chunk_months):
        chunk = months[offset : offset + chunk_months]
        start, _ = _month_range(chunk[0])
        _, end = _month_range(chunk[-1])
        ranges.append((start, end))
    return ranges


def _deterministic_sample(codes: Sequence[str], size: int) -> list[str]:
    unique = sorted(
        {
            str(code).strip()
            for code in codes
            if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", str(code).strip())
        }
    )
    if len(unique) <= size:
        return unique
    if size == 1:
        return [unique[len(unique) // 2]]
    indexes = [round(position * (len(unique) - 1) / (size - 1)) for position in range(size)]
    return [unique[index] for index in indexes]


def _receipt_manifest_sha(result: Mapping[str, Any]) -> str:
    receipt = result.get("receipt")
    value = receipt.get("manifest_sha256") if isinstance(receipt, Mapping) else None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise TusharePitBackfillError("checkpoint artifact receipt is invalid")
    return value


def _positive_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _clock_minutes(value: str) -> int | None:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _suspend_timing_kind(value: Any) -> str:
    timing = str(value or "").strip()
    if timing in _FULL_DAY_SUSPEND_LABELS:
        return "full_day"
    match = _SUSPEND_INTERVAL.fullmatch(timing)
    if match is None:
        return "ambiguous"
    start = _clock_minutes(match.group("start"))
    end = _clock_minutes(match.group("end"))
    if start is None or end is None or start >= end:
        return "ambiguous"
    regular_open, regular_close = 9 * 60 + 30, 15 * 60
    if start < regular_open or end > regular_close:
        return "ambiguous"
    return (
        "full_day"
        if start <= regular_open and end >= regular_close
        else "partial_session"
    )


def _classify_suspend_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    details = [
        {
            "suspend_timing": str(row.get("suspend_timing") or "").strip(),
            "suspend_type": str(row.get("suspend_type") or "").strip(),
            "timing_kind": _suspend_timing_kind(row.get("suspend_timing")),
        }
        for row in records
    ]
    kinds = {row["timing_kind"] for row in details}
    types = {row["suspend_type"] for row in details}
    if not details or types != {"S"} or "ambiguous" in kinds:
        status = "ambiguous_suspend_semantics"
    elif "full_day" in kinds:
        status = "explicit_full_day_suspension_candidate"
    else:
        status = "explicit_partial_session_suspension_candidate"
    return {"status": status, "records": details}


class BackfillCheckpointStore:
    """Private mutable pointer to immutable artifact receipts."""

    def __init__(self, evidence_root: Path, run_id: str) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise TusharePitBackfillError("run_id is invalid")
        self.root = Path(os.path.abspath(evidence_root.expanduser()))
        self.directory = self.root / "checkpoints"
        self.path = self.directory / f"{run_id}.json"
        self.lock_path = self.directory / f"{run_id}.lock"
        self._secure_directory(self.root)
        self._secure_directory(self.directory)

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise TusharePitBackfillError("checkpoint directory is unsafe")
        os.chmod(path, 0o700)

    @staticmethod
    def _secure_file(path: Path) -> None:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise TusharePitBackfillError("checkpoint file is unsafe")
        os.chmod(path, 0o600)

    @contextmanager
    def lease(self) -> Iterator[None]:
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TusharePitBackfillError(
                    "another process owns this backfill checkpoint"
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self, plan: TusharePitBackfillPlan) -> dict[str, Any]:
        if not self.path.exists():
            payload: dict[str, Any] = {
                "schema_version": TUSHARE_PIT_BACKFILL_CHECKPOINT_SCHEMA,
                "run_id": plan.run_id,
                "plan": plan.public_scope(),
                "completed": {},
                "failures": {},
                "optional_failures": {},
                "session_reconciliation": {},
                "updated_at": utc_now(),
            }
            return self._seal(payload)
        self._secure_file(self.path)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TusharePitBackfillError("checkpoint is unreadable") from exc
        checksum = payload.pop("checkpoint_sha256", None)
        if checksum != canonical_sha256(payload):
            raise TusharePitBackfillError("checkpoint digest changed")
        schema_version = payload.get("schema_version")
        if (
            schema_version
            not in {TUSHARE_PIT_BACKFILL_CHECKPOINT_SCHEMA, *_LEGACY_CHECKPOINT_SCHEMAS}
            or payload.get("run_id") != plan.run_id
            or payload.get("plan") != plan.public_scope()
        ):
            raise TusharePitBackfillError("checkpoint plan changed")
        if schema_version in _LEGACY_CHECKPOINT_SCHEMAS:
            payload["schema_version"] = TUSHARE_PIT_BACKFILL_CHECKPOINT_SCHEMA
            failures = payload.get("failures")
            current_failures: dict[str, Any] = {}
            legacy_failures = dict(payload.get("legacy_failures") or {})
            if isinstance(failures, Mapping):
                for task_id, failure in failures.items():
                    task_scope = (
                        failure.get("task") if isinstance(failure, Mapping) else None
                    )
                    category = (
                        str(task_scope.get("category") or "")
                        if isinstance(task_scope, Mapping)
                        else ""
                    )
                    if category in {
                        "index_weight_monthly",
                        "security_master",
                        "trading_calendar",
                        "industry_catalog",
                    }:
                        current_failures[str(task_id)] = failure
                    else:
                        legacy_failures[str(task_id)] = failure
            payload["failures"] = current_failures
            payload["legacy_failures"] = legacy_failures
            migrations = payload.get("migrations")
            history = list(migrations) if isinstance(migrations, list) else []
            history.append(
                {
                    "from": schema_version,
                    "to": TUSHARE_PIT_BACKFILL_CHECKPOINT_SCHEMA,
                    "reason": "replace_per_security_market_calls_with_session_cross_sections",
                    "legacy_completed_evidence_retained": True,
                    "legacy_receipts_counted_as_cross_sections": False,
                }
            )
            payload["migrations"] = history
        payload.setdefault("session_reconciliation", {})
        payload.setdefault("optional_failures", {})
        return self._seal(payload)

    @staticmethod
    def _seal(payload: Mapping[str, Any]) -> dict[str, Any]:
        sealed = dict(payload)
        sealed["checkpoint_sha256"] = canonical_sha256(sealed)
        return sealed

    def save(self, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(checkpoint)
        checksum = payload.pop("checkpoint_sha256", None)
        if checksum != canonical_sha256(payload):
            raise TusharePitBackfillError("refusing an unsealed checkpoint")
        payload["updated_at"] = utc_now()
        sealed = self._seal(payload)
        encoded = canonical_json_bytes(sealed)
        descriptor, name = tempfile.mkstemp(prefix=".checkpoint.", dir=self.directory)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._secure_file(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return sealed


class TusharePitBackfillCollector:
    def __init__(
        self,
        *,
        client: TushareCandidateClient,
        plan: TusharePitBackfillPlan,
        max_calls: int = 16,
        retry_optional_failures: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not 1 <= max_calls <= _MAX_CALLS_PER_INVOCATION:
            raise TusharePitBackfillError(
                f"max_calls must be between 1 and {_MAX_CALLS_PER_INVOCATION}"
            )
        self.client = client
        self.plan = plan
        self.max_calls = max_calls
        self.retry_optional_failures = bool(retry_optional_failures)
        self.progress = progress
        self._calls_started = 0
        self.checkpoints = BackfillCheckpointStore(client.store.root, plan.run_id)

    def _report_call_boundary(self, task: BackfillTask) -> None:
        if self.progress is None:
            return
        self._calls_started += 1
        self.progress(
            {
                "overall_fraction": min(
                    0.05 + 0.65 * self._calls_started / self.max_calls,
                    0.70,
                ),
                "stage": "provider_collection",
                "message": (
                    "正在采集研究候选数据："
                    f"{self._calls_started}/{self.max_calls} 次本批调用"
                ),
                "provider_calls_started": self._calls_started,
                "provider_call_budget": self.max_calls,
                "dataset": task.dataset,
            }
        )

    async def run(self) -> dict[str, Any]:
        with self.checkpoints.lease():
            checkpoint = self.checkpoints.load(self.plan)
            if self.retry_optional_failures:
                reopened = [
                    task_id
                    for task_id, result in checkpoint["completed"].items()
                    if isinstance(result, Mapping) and result.get("optional_failure")
                ]
                for task_id in reopened:
                    checkpoint["completed"].pop(task_id, None)
                if reopened:
                    checkpoint["checkpoint_sha256"] = canonical_sha256(
                        {
                            key: value
                            for key, value in checkpoint.items()
                            if key != "checkpoint_sha256"
                        }
                    )
                    persisted = self.checkpoints.save(checkpoint)
                    checkpoint.clear()
                    checkpoint.update(persisted)
            completed = checkpoint["completed"]
            completed_before_provider_calls = len(completed)
            calls = 0
            foundation = self.plan.foundation_tasks()
            reused_prior_tasks = self._reuse_prior_checkpoint_tasks(
                foundation, checkpoint
            )
            calls += await self._execute_tasks(
                foundation,
                checkpoint,
                budget=self.max_calls - calls,
            )
            foundation_complete = all(
                task.task_id in completed or not task.required for task in foundation
            )
            membership_months: dict[str, list[str]] = {}
            open_sessions: list[str] = []
            market_tasks: list[BackfillTask] = []
            metadata_tasks: list[BackfillTask] = []
            reused_legacy_tasks = 0
            if foundation_complete:
                membership_months = self._historical_membership(
                    foundation, completed
                )
                open_sessions = self._canonical_open_sessions(foundation, completed)
                market_tasks = self.plan.session_market_tasks(open_sessions)
                metadata_tasks = self.plan.full_universe_metadata_tasks(
                    membership_months
                )
                reused_prior_tasks += self._reuse_prior_checkpoint_tasks(
                    [*market_tasks, *metadata_tasks], checkpoint
                )
                reused_legacy_tasks = self._reuse_compatible_completed_tasks(
                    [*market_tasks, *metadata_tasks], checkpoint
                )
                if calls < self.max_calls:
                    calls += await self._execute_tasks(
                        [*market_tasks, *metadata_tasks],
                        checkpoint,
                        budget=self.max_calls - calls,
                    )
                self._reconcile_completed_sessions(
                    market_tasks=market_tasks,
                    membership_months=membership_months,
                    foundation=foundation,
                    checkpoint=checkpoint,
                )
            report = self._coverage_report(
                checkpoint=checkpoint,
                foundation=foundation,
                market_tasks=market_tasks,
                metadata_tasks=metadata_tasks,
                open_sessions=open_sessions,
                membership_months=membership_months,
                reused_legacy_tasks=reused_legacy_tasks,
                reused_prior_tasks=reused_prior_tasks,
                calls_this_invocation=calls,
                completed_this_invocation=max(
                    len(completed) - completed_before_provider_calls, 0
                ),
            )
            report_digest = self.client.store.record_report(report)
            report["stored_report_sha256"] = report_digest
            return report

    async def _execute_tasks(
        self,
        tasks: Sequence[BackfillTask],
        checkpoint: dict[str, Any],
        *,
        budget: int,
    ) -> int:
        completed = checkpoint["completed"]
        calls = 0
        for task in tasks:
            if task.task_id in completed:
                continue
            if calls >= budget:
                break
            calls += 1
            # This boundary occurs before every provider request.  In the
            # responsive refresh runner it relays cancellation and durable job
            # progress to the server loop without moving any response rows.
            self._report_call_boundary(task)
            try:
                observation = await self.client.fetch(task.dataset, task.params)
                validation = self._validate_observation(task, observation.rows)
            except (TushareCandidateError, ProviderArtifactError) as exc:
                failure = {
                    "task": task.public_scope(),
                    "diagnostic": (
                        exc.diagnostic()
                        if isinstance(exc, TushareCandidateError)
                        else {"code": "candidate_artifact_error", "retryable": False}
                    ),
                    "observed_at": utc_now(),
                }
                if not task.required:
                    if failure["diagnostic"].get("retryable") is True:
                        previous = checkpoint["optional_failures"].get(task.task_id)
                        failure["attempt_count"] = (
                            int(previous.get("attempt_count") or 0) + 1
                            if isinstance(previous, Mapping)
                            else 1
                        )
                        checkpoint["optional_failures"][task.task_id] = failure
                    else:
                        completed[task.task_id] = {
                            "task": task.public_scope(),
                            "row_count": 0,
                            "validation": {"status": "optional_source_unavailable"},
                            "optional_failure": failure["diagnostic"],
                            "observed_at": failure["observed_at"],
                        }
                    checkpoint["failures"].pop(task.task_id, None)
                else:
                    checkpoint["failures"][task.task_id] = failure
                checkpoint["checkpoint_sha256"] = canonical_sha256(
                    {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
                )
                persisted = self.checkpoints.save(checkpoint)
                checkpoint.clear()
                checkpoint.update(persisted)
                if task.required:
                    break
                continue
            if (
                task.dataset == "index_weight"
                and validation["status"] != "complete_monthly_snapshot_candidate"
            ):
                checkpoint["failures"][task.task_id] = {
                    "task": task.public_scope(),
                    "diagnostic": {
                        "code": "incomplete_index_weight_monthly_snapshot",
                        "retryable": True,
                    },
                    "receipt": observation.receipt,
                    "row_count": len(observation.rows),
                    "validation": validation,
                    "observed_at": observation.manifest["bitemporal"]["ingested_at"],
                }
                checkpoint["checkpoint_sha256"] = canonical_sha256(
                    {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
                )
                persisted = self.checkpoints.save(checkpoint)
                checkpoint.clear()
                checkpoint.update(persisted)
                break
            completed[task.task_id] = {
                "task": task.public_scope(),
                "receipt": observation.receipt,
                "row_count": len(observation.rows),
                "validation": validation,
                "observed_at": observation.manifest["bitemporal"]["ingested_at"],
            }
            checkpoint["failures"].pop(task.task_id, None)
            checkpoint["optional_failures"].pop(task.task_id, None)
            checkpoint["checkpoint_sha256"] = canonical_sha256(
                {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
            )
            persisted = self.checkpoints.save(checkpoint)
            checkpoint.clear()
            checkpoint.update(persisted)
        return calls

    def _reuse_prior_checkpoint_tasks(
        self,
        tasks: Sequence[BackfillTask],
        checkpoint: dict[str, Any],
    ) -> int:
        """Reuse exact content-addressed receipts when the supported month grows."""

        pending = {task.task_id: task for task in tasks if task.task_id not in checkpoint["completed"]}
        if not pending:
            return 0
        reusable: dict[str, Mapping[str, Any]] = {}
        for path in self.checkpoints.directory.glob("*.json"):
            if path == self.checkpoints.path or path.is_symlink():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                checksum = payload.pop("checkpoint_sha256", None)
                prior_plan = payload.get("plan")
                if (
                    checksum != canonical_sha256(payload)
                    or not isinstance(prior_plan, Mapping)
                    or prior_plan.get("first_month") != self.plan.first_month
                    or str(prior_plan.get("last_month") or "") >= self.plan.last_month
                    or not isinstance(payload.get("completed"), Mapping)
                ):
                    continue
                for task_id, result in payload["completed"].items():
                    if (
                        task_id in pending
                        and isinstance(result, Mapping)
                        and not result.get("optional_failure")
                    ):
                        reusable[str(task_id)] = result
            except (OSError, TypeError, json.JSONDecodeError):
                continue
        reused = 0
        for task_id, result in reusable.items():
            task = pending[task_id]
            manifest, _ = self.client.store.read(_receipt_manifest_sha(result))
            request = manifest.get("request")
            if (
                manifest.get("classification") != "quarantine"
                or manifest.get("dataset") != task.dataset
                or not isinstance(request, Mapping)
                or request.get("params") != task.params
            ):
                raise TusharePitBackfillError(
                    "prior checkpoint artifact does not match expanded plan"
                )
            checkpoint["completed"][task_id] = {
                **dict(result),
                "task": task.public_scope(),
                "reused_from_prior_checkpoint": True,
            }
            reused += 1
        if reused:
            checkpoint["checkpoint_sha256"] = canonical_sha256(
                {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
            )
            persisted = self.checkpoints.save(checkpoint)
            checkpoint.clear()
            checkpoint.update(persisted)
        return reused

    def _reuse_compatible_completed_tasks(
        self,
        tasks: Sequence[BackfillTask],
        checkpoint: dict[str, Any],
    ) -> int:
        """Reuse v1 sample receipts only when their exact public call matches."""

        completed = checkpoint["completed"]
        compatible: dict[tuple[str, str], Mapping[str, Any]] = {}
        for result in list(completed.values()):
            old_task = result.get("task") if isinstance(result, Mapping) else None
            if not isinstance(old_task, Mapping):
                continue
            dataset = str(old_task.get("dataset") or "")
            params = old_task.get("params")
            if dataset and isinstance(params, Mapping):
                compatible[(dataset, canonical_sha256(dict(params)))] = result
        reused = 0
        for task in tasks:
            if task.task_id in completed:
                continue
            result = compatible.get((task.dataset, canonical_sha256(task.params)))
            if result is None:
                continue
            manifest, _ = self.client.store.read(_receipt_manifest_sha(result))
            request = manifest.get("request")
            if (
                manifest.get("classification") != "quarantine"
                or manifest.get("dataset") != task.dataset
                or not isinstance(request, Mapping)
                or request.get("params") != task.params
            ):
                raise TusharePitBackfillError(
                    "legacy checkpoint artifact does not match full-universe task"
                )
            completed[task.task_id] = {
                **dict(result),
                "task": task.public_scope(),
                "reused_from_legacy_task_id": old_task.get("task_id"),
            }
            reused += 1
        if reused:
            checkpoint["checkpoint_sha256"] = canonical_sha256(
                {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
            )
            persisted = self.checkpoints.save(checkpoint)
            checkpoint.clear()
            checkpoint.update(persisted)
        return reused

    @staticmethod
    def _validate_observation(
        task: BackfillTask, rows: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        if task.dataset == "index_weight":
            index_code = str(task.params["index_code"])
            by_date: dict[str, set[str]] = {}
            start_date = str(task.params["start_date"])
            end_date = str(task.params["end_date"])
            for row in rows:
                if row.get("index_code") != index_code:
                    raise TusharePitBackfillError("index response scope changed")
                trade_date = str(row.get("trade_date") or "")
                con_code = str(row.get("con_code") or "")
                if trade_date and not start_date <= trade_date <= end_date:
                    raise TusharePitBackfillError("index response date scope changed")
                if trade_date and re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", con_code):
                    by_date.setdefault(trade_date, set()).add(con_code)
            maximum = max((len(codes) for codes in by_date.values()), default=0)
            minimum = _INDEX_MINIMUM_MEMBERS[index_code]
            return {
                "status": (
                    "complete_monthly_snapshot_candidate"
                    if maximum >= minimum
                    else "incomplete_monthly_snapshot"
                ),
                "maximum_unique_members_on_one_date": maximum,
                "minimum_expected_members": minimum,
                "snapshot_dates": sorted(by_date),
            }
        expected_code = task.params.get("ts_code")
        if expected_code:
            for row in rows:
                if str(row.get("ts_code") or "") != expected_code:
                    raise TusharePitBackfillError("security response scope changed")
                trade_date = row.get("trade_date")
                if trade_date and (
                    str(trade_date) < str(task.params.get("start_date", trade_date))
                    or str(trade_date) > str(task.params.get("end_date", trade_date))
                ):
                    raise TusharePitBackfillError("security response date scope changed")
        if task.dataset == "index_daily":
            observed_dates: set[str] = set()
            for row in rows:
                trade_date = str(row.get("trade_date") or "")
                if not re.fullmatch(r"\d{8}", trade_date):
                    raise TusharePitBackfillError(
                        "benchmark response contains an invalid date"
                    )
                if trade_date in observed_dates:
                    raise TusharePitBackfillError(
                        "benchmark response contains duplicate dates"
                    )
                observed_dates.add(trade_date)
                try:
                    open_value = float(row["open"])
                    high_value = float(row["high"])
                    low_value = float(row["low"])
                    close_value = float(row["close"])
                    volume = float(row.get("vol") or 0)
                    amount = float(row.get("amount") or 0)
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise TusharePitBackfillError(
                        "benchmark response contains invalid numerics"
                    ) from exc
                if (
                    not all(
                        math.isfinite(value)
                        for value in (
                            open_value,
                            high_value,
                            low_value,
                            close_value,
                            volume,
                            amount,
                        )
                    )
                    or min(open_value, high_value, low_value, close_value) <= 0
                    or high_value < max(open_value, low_value, close_value)
                    or low_value > min(open_value, high_value, close_value)
                    or volume < 0
                    or amount < 0
                ):
                    raise TusharePitBackfillError(
                        "benchmark response contains corrupt market values"
                    )
        requested_trade_date = task.params.get("trade_date")
        if requested_trade_date:
            observed_codes: set[str] = set()
            observed_suspend_rows: set[tuple[str, str, str]] = set()
            for row in rows:
                if str(row.get("trade_date") or "") != requested_trade_date:
                    raise TusharePitBackfillError(
                        "session cross-section response date scope changed"
                    )
                code = str(row.get("ts_code") or "")
                if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
                    raise TusharePitBackfillError(
                        "session cross-section contains an invalid security code"
                    )
                if task.dataset != "suspend_d" and code in observed_codes:
                    raise TusharePitBackfillError(
                        "session cross-section contains duplicate securities"
                    )
                if task.dataset == "suspend_d":
                    suspend_key = (
                        code,
                        str(row.get("suspend_timing") or "").strip(),
                        str(row.get("suspend_type") or "").strip(),
                    )
                    if suspend_key in observed_suspend_rows:
                        raise TusharePitBackfillError(
                            "session cross-section contains duplicate suspend records"
                        )
                    observed_suspend_rows.add(suspend_key)
                observed_codes.add(code)
        if task.dataset == "trade_cal":
            start_date = str(task.params["start_date"])
            end_date = str(task.params["end_date"])
            observed_dates: set[str] = set()
            for row in rows:
                if row.get("exchange") != task.params["exchange"]:
                    raise TusharePitBackfillError(
                        "trading calendar response exchange scope changed"
                    )
                cal_date = str(row.get("cal_date") or "")
                if not start_date <= cal_date <= end_date:
                    raise TusharePitBackfillError(
                        "trading calendar response date scope changed"
                    )
                if cal_date in observed_dates:
                    raise TusharePitBackfillError(
                        "trading calendar contains duplicate dates"
                    )
                observed_dates.add(cal_date)
            first = datetime.strptime(start_date, "%Y%m%d").date()
            last = datetime.strptime(end_date, "%Y%m%d").date()
            expected_dates = {
                (first + timedelta(days=offset)).strftime("%Y%m%d")
                for offset in range((last - first).days + 1)
            }
            if observed_dates != expected_dates:
                raise TusharePitBackfillError(
                    "canonical trading calendar date coverage is incomplete"
                )
        if task.dataset == "stock_basic":
            expected_status = task.params["list_status"]
            if any(row.get("list_status") != expected_status for row in rows):
                raise TusharePitBackfillError(
                    "security master response listing-status scope changed"
                )
        return {
            "status": "observed" if rows else "empty_candidate_observation",
        }

    def _historical_membership(
        self,
        foundation: Sequence[BackfillTask],
        completed: Mapping[str, Any],
    ) -> dict[str, list[str]]:
        membership: dict[str, set[str]] = {}
        for task in foundation:
            if task.category != "index_weight_monthly":
                continue
            result = completed.get(task.task_id)
            if not isinstance(result, Mapping):
                raise TusharePitBackfillError(
                    "historical index snapshot is missing from checkpoint"
                )
            validation = result.get("validation")
            if (
                not isinstance(validation, Mapping)
                or validation.get("status")
                != "complete_monthly_snapshot_candidate"
            ):
                raise TusharePitBackfillError(
                    "historical index snapshot is not classified complete"
                )
            manifest, response = self.client.store.read(
                _receipt_manifest_sha(result)
            )
            request = manifest.get("request")
            if (
                manifest.get("classification") != "quarantine"
                or manifest.get("dataset") != "index_weight"
                or not isinstance(request, Mapping)
                or request.get("params") != task.params
            ):
                raise TusharePitBackfillError(
                    "index artifact scope or quarantine classification changed"
                )
            document = json.loads(response)
            fields = document["data"]["fields"]
            rows = [
                dict(zip(fields, item, strict=True))
                for item in document["data"]["items"]
            ]
            artifact_validation = self._validate_observation(task, rows)
            if (
                artifact_validation.get("status")
                != "complete_monthly_snapshot_candidate"
            ):
                raise TusharePitBackfillError(
                    "historical index artifact is not a complete snapshot"
                )
            observed_codes: set[str] = set()
            for row in rows:
                code = str(row.get("con_code") or "")
                if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
                    observed_codes.add(code)
            minimum = _INDEX_MINIMUM_MEMBERS[str(task.params["index_code"])]
            if len(observed_codes) < minimum:
                raise TusharePitBackfillError(
                    "historical index artifact no longer meets member minimum"
                )
            month = (
                f"{str(task.params['start_date'])[:4]}-"
                f"{str(task.params['start_date'])[4:6]}"
            )
            for code in observed_codes:
                membership.setdefault(code, set()).add(month)
        if not membership:
            raise TusharePitBackfillError("historical index universe is empty")
        return {code: sorted(months) for code, months in sorted(membership.items())}

    def _artifact_rows(
        self, task: BackfillTask, result: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        manifest, response = self.client.store.read(_receipt_manifest_sha(result))
        request = manifest.get("request")
        if (
            manifest.get("classification") != "quarantine"
            or manifest.get("dataset") != task.dataset
            or not isinstance(request, Mapping)
            or request.get("params") != task.params
        ):
            raise TusharePitBackfillError(
                "candidate artifact scope or quarantine classification changed"
            )
        try:
            document = json.loads(response)
            fields = document["data"]["fields"]
            items = document["data"]["items"]
            rows = [dict(zip(fields, item, strict=True)) for item in items]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TusharePitBackfillError(
                "candidate artifact response table is invalid"
            ) from exc
        self._validate_observation(task, rows)
        return manifest, rows

    def _canonical_open_sessions(
        self,
        foundation: Sequence[BackfillTask],
        completed: Mapping[str, Any],
    ) -> list[str]:
        calendar_state: dict[str, int] = {}
        for task in foundation:
            if task.category != "trading_calendar":
                continue
            result = completed.get(task.task_id)
            if not isinstance(result, Mapping):
                raise TusharePitBackfillError(
                    "canonical trading calendar artifact is missing"
                )
            _, rows = self._artifact_rows(task, result)
            for row in rows:
                session = str(row.get("cal_date") or "")
                is_open = row.get("is_open")
                if not re.fullmatch(r"\d{8}", session) or is_open not in {0, 1, "0", "1"}:
                    raise TusharePitBackfillError(
                        "canonical trading calendar row is invalid"
                    )
                normalized = int(is_open)
                previous = calendar_state.setdefault(session, normalized)
                if previous != normalized:
                    raise TusharePitBackfillError(
                        "canonical trading calendar contains conflicting sessions"
                    )
        open_sessions = sorted(
            session for session, is_open in calendar_state.items() if is_open == 1
        )
        if not open_sessions:
            raise TusharePitBackfillError("canonical trading calendar has no open session")
        return open_sessions

    def _security_master_rows(
        self,
        foundation: Sequence[BackfillTask],
        completed: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        by_code: dict[str, list[dict[str, Any]]] = {}
        for task in foundation:
            if task.category != "security_master":
                continue
            result = completed.get(task.task_id)
            if not isinstance(result, Mapping):
                if task.required:
                    raise TusharePitBackfillError("security master artifact is missing")
                continue
            if result.get("optional_failure"):
                continue
            _, rows = self._artifact_rows(task, result)
            for row in rows:
                code = str(row.get("ts_code") or "")
                if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
                    by_code.setdefault(code, []).append(row)
        return by_code

    @staticmethod
    def _security_active_on(
        records: Sequence[Mapping[str, Any]], trade_date: str
    ) -> bool:
        return any(
            str(row.get("list_date") or "99999999") <= trade_date
            and (
                not str(row.get("delist_date") or "").strip()
                or trade_date <= str(row.get("delist_date"))
            )
            for row in records
        )

    def _reconcile_completed_sessions(
        self,
        *,
        market_tasks: Sequence[BackfillTask],
        membership_months: Mapping[str, Sequence[str]],
        foundation: Sequence[BackfillTask],
        checkpoint: dict[str, Any],
    ) -> None:
        completed = checkpoint["completed"]
        reconciled = checkpoint.setdefault("session_reconciliation", {})
        tasks_by_session: dict[str, dict[str, BackfillTask]] = {}
        for task in market_tasks:
            tasks_by_session.setdefault(str(task.params["trade_date"]), {})[
                task.dataset
            ] = task
        security_master = self._security_master_rows(foundation, completed)
        changed = False
        for trade_date, tasks in sorted(tasks_by_session.items()):
            if set(tasks) != {"daily", "adj_factor", "daily_basic", "suspend_d"}:
                raise TusharePitBackfillError("session cross-section plan is incomplete")
            if any(
                task.task_id not in completed
                for task in tasks.values()
                if task.required
            ):
                continue
            receipt_hashes = {
                dataset: _receipt_manifest_sha(completed[task.task_id])
                for dataset, task in sorted(tasks.items())
                if task.task_id in completed
                and not completed[task.task_id].get("optional_failure")
            }
            month = f"{trade_date[:4]}-{trade_date[4:6]}"
            required_members = sorted(
                code for code, months in membership_months.items() if month in months
            )
            membership_hash = canonical_sha256(required_members)
            previous = reconciled.get(trade_date)
            if (
                isinstance(previous, Mapping)
                and previous.get("schema_version") == _SESSION_RECONCILIATION_SCHEMA
                and previous.get("dataset_manifest_sha256") == receipt_hashes
                and previous.get("required_members_sha256") == membership_hash
            ):
                continue
            rows_by_dataset: dict[str, list[dict[str, Any]]] = {}
            for dataset, task in tasks.items():
                if (
                    task.task_id in completed
                    and not completed[task.task_id].get("optional_failure")
                ):
                    _, rows = self._artifact_rows(task, completed[task.task_id])
                    rows_by_dataset[dataset] = rows
                else:
                    rows_by_dataset[dataset] = []
            daily_rows = {
                str(row["ts_code"]): row for row in rows_by_dataset["daily"]
            }
            adjusted = {
                str(row["ts_code"]) for row in rows_by_dataset["adj_factor"]
            }
            daily_basic = {
                str(row["ts_code"]) for row in rows_by_dataset["daily_basic"]
            }
            suspend_rows: dict[str, list[dict[str, Any]]] = {}
            for row in rows_by_dataset["suspend_d"]:
                suspend_rows.setdefault(str(row["ts_code"]), []).append(row)
            blockers: list[dict[str, Any]] = []
            non_tradable = 0
            observed_liquidity = 0
            status_counts: dict[str, int] = {}
            restricted_or_ambiguous_members: list[dict[str, Any]] = []
            for code in required_members:
                daily_row = daily_rows.get(code)
                code_suspend_rows = suspend_rows.get(code, [])
                member_status: str
                if daily_row is not None:
                    positive_liquidity = _positive_number(
                        daily_row.get("vol")
                    ) and _positive_number(daily_row.get("amount"))
                    if code_suspend_rows:
                        suspend = _classify_suspend_records(code_suspend_rows)
                        if not positive_liquidity:
                            member_status = "daily_suspend_without_positive_liquidity"
                            blockers.append({"code": code, "reason": member_status})
                        elif suspend["status"] == (
                            "explicit_partial_session_suspension_candidate"
                        ):
                            member_status = (
                                "observed_liquidity_with_explicit_partial_suspension"
                            )
                            observed_liquidity += 1
                        elif suspend["status"] == (
                            "explicit_full_day_suspension_candidate"
                        ):
                            member_status = "daily_conflicts_with_full_day_suspension"
                            blockers.append({"code": code, "reason": member_status})
                        else:
                            member_status = "daily_suspend_semantics_ambiguous"
                            blockers.append({"code": code, "reason": member_status})
                        restricted_or_ambiguous_members.append(
                            {
                                "code": code,
                                "status": member_status,
                                "suspend_evidence": suspend,
                            }
                        )
                    elif positive_liquidity:
                        member_status = "observed_daily_liquidity_without_suspend"
                        observed_liquidity += 1
                    else:
                        member_status = "daily_without_positive_liquidity"
                        blockers.append({"code": code, "reason": member_status})
                    if code not in adjusted:
                        blockers.append(
                            {"code": code, "reason": "adjustment_factor_missing"}
                        )
                    if code not in daily_basic:
                        blockers.append(
                            {"code": code, "reason": "daily_basic_missing"}
                        )
                elif code_suspend_rows:
                    suspend = _classify_suspend_records(code_suspend_rows)
                    if suspend["status"] == "explicit_full_day_suspension_candidate":
                        member_status = "candidate_full_day_suspension_without_daily"
                        non_tradable += 1
                    elif suspend["status"] == (
                        "explicit_partial_session_suspension_candidate"
                    ):
                        member_status = "partial_suspension_without_daily_unexplained"
                        blockers.append({"code": code, "reason": member_status})
                    else:
                        member_status = "suspend_without_daily_semantics_ambiguous"
                        blockers.append({"code": code, "reason": member_status})
                    restricted_or_ambiguous_members.append(
                        {
                            "code": code,
                            "status": member_status,
                            "suspend_evidence": suspend,
                        }
                    )
                else:
                    records = security_master.get(code, [])
                    if records and not self._security_active_on(records, trade_date):
                        member_status = "security_master_not_active"
                        non_tradable += 1
                    else:
                        member_status = (
                            "security_master_missing_and_no_non_tradable_evidence"
                            if not records
                            else "tradable_member_missing_without_suspend_evidence"
                        )
                        blockers.append({"code": code, "reason": member_status})
                if any(row.get("code") == code for row in blockers) and not any(
                    row.get("code") == code
                    for row in restricted_or_ambiguous_members
                ):
                    restricted_or_ambiguous_members.append(
                        {"code": code, "status": member_status}
                    )
                status_counts[member_status] = status_counts.get(member_status, 0) + 1
            reconciled[trade_date] = {
                "schema_version": _SESSION_RECONCILIATION_SCHEMA,
                "trade_date": trade_date,
                "universe_source": "historical_monthly_index_weight_artifacts",
                "session_source": "canonical_trade_cal_artifact",
                "intersection_stage": "local_after_full_market_artifact_capture",
                "dataset_manifest_sha256": receipt_hashes,
                "required_members_sha256": membership_hash,
                "required_member_count": len(required_members),
                "observed_positive_liquidity_member_count": observed_liquidity,
                "candidate_non_tradable_evidence_count": non_tradable,
                "status_counts": dict(sorted(status_counts.items())),
                "restricted_or_ambiguous_members": restricted_or_ambiguous_members,
                "ambiguous_member_count": sum(
                    "ambiguous" in row["status"]
                    or row["status"]
                    in {
                        "partial_suspension_without_daily_unexplained",
                        "daily_suspend_without_positive_liquidity",
                        "daily_without_positive_liquidity",
                        "security_master_missing_and_no_non_tradable_evidence",
                    }
                    for row in restricted_or_ambiguous_members
                ),
                "conflicting_member_count": sum(
                    row["status"] == "daily_conflicts_with_full_day_suspension"
                    for row in restricted_or_ambiguous_members
                ),
                "production_full_day_tradability_proven": False,
                "blockers": blockers,
                "valid": not blockers,
            }
            changed = True
        if changed:
            checkpoint["checkpoint_sha256"] = canonical_sha256(
                {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
            )
            persisted = self.checkpoints.save(checkpoint)
            checkpoint.clear()
            checkpoint.update(persisted)

    def _coverage_report(
        self,
        *,
        checkpoint: Mapping[str, Any],
        foundation: Sequence[BackfillTask],
        market_tasks: Sequence[BackfillTask],
        metadata_tasks: Sequence[BackfillTask],
        open_sessions: Sequence[str],
        membership_months: Mapping[str, Sequence[str]],
        reused_legacy_tasks: int,
        reused_prior_tasks: int,
        calls_this_invocation: int,
        completed_this_invocation: int,
    ) -> dict[str, Any]:
        completed = checkpoint["completed"]
        known_tasks = [*foundation, *market_tasks, *metadata_tasks]
        by_category: dict[str, dict[str, int]] = {}
        index_month_coverage: list[dict[str, Any]] = []
        incomplete_index_months: list[dict[str, Any]] = []
        for task in known_tasks:
            counts = by_category.setdefault(
                task.category, {"planned": 0, "completed": 0, "nonempty": 0}
            )
            counts["planned"] += 1
            result = completed.get(task.task_id)
            if result is None:
                if task.category == "index_weight_monthly":
                    failure = checkpoint["failures"].get(task.task_id)
                    index_month_coverage.append(
                        {
                            "index_code": task.params["index_code"],
                            "month": (
                                f"{str(task.params['start_date'])[:4]}-"
                                f"{str(task.params['start_date'])[4:6]}"
                            ),
                            "status": "failed" if failure else "pending",
                            "diagnostic": failure.get("diagnostic") if failure else None,
                        }
                    )
                continue
            counts["completed"] += 1
            if result.get("optional_failure"):
                continue
            if result["row_count"] > 0:
                counts["nonempty"] += 1
            if (
                task.category == "index_weight_monthly"
                and result["validation"]["status"]
                != "complete_monthly_snapshot_candidate"
            ):
                incomplete_index_months.append(
                    {
                        "index_code": task.params["index_code"],
                        "month": str(task.params["start_date"])[:6],
                        "row_count": result["row_count"],
                        "validation": result["validation"],
                    }
                )
            if task.category == "index_weight_monthly":
                index_month_coverage.append(
                    {
                        "index_code": task.params["index_code"],
                        "month": (
                            f"{str(task.params['start_date'])[:4]}-"
                            f"{str(task.params['start_date'])[4:6]}"
                        ),
                        "status": result["validation"]["status"],
                        "row_count": result["row_count"],
                        "maximum_unique_members_on_one_date": result["validation"][
                            "maximum_unique_members_on_one_date"
                        ],
                        "manifest_sha256": result["receipt"]["manifest_sha256"],
                    }
                )
        total = len(known_tasks)
        completed_total = sum(task.task_id in completed for task in known_tasks)
        foundation_complete = all(
            task.task_id in completed or not task.required for task in foundation
        )
        full_universe_plan_materialized = bool(market_tasks) and bool(metadata_tasks)
        session_reconciliation = checkpoint.get("session_reconciliation", {})
        reconciliation_rows = (
            list(session_reconciliation.values())
            if isinstance(session_reconciliation, Mapping)
            else []
        )
        reconciliation_blockers = [
            {
                "code": "historical_member_session_coverage_invalid",
                "retryable": False,
                "trade_date": row.get("trade_date"),
                "blockers": row.get("blockers"),
            }
            for row in reconciliation_rows
            if isinstance(row, Mapping) and row.get("valid") is not True
        ]
        aggregate_status_counts: dict[str, int] = {}
        aggregate_ambiguity = 0
        aggregate_conflicts = 0
        for row in reconciliation_rows:
            if not isinstance(row, Mapping):
                continue
            counts = row.get("status_counts")
            if isinstance(counts, Mapping):
                for status, count in counts.items():
                    aggregate_status_counts[str(status)] = (
                        aggregate_status_counts.get(str(status), 0) + int(count)
                    )
            aggregate_ambiguity += int(row.get("ambiguous_member_count") or 0)
            aggregate_conflicts += int(row.get("conflicting_member_count") or 0)
        all_sessions_reconciled = (
            bool(open_sessions)
            and len(reconciliation_rows) == len(open_sessions)
            and not reconciliation_blockers
        )
        collection_complete = (
            foundation_complete
            and full_universe_plan_materialized
            and completed_total == total
            and not checkpoint["failures"]
            and all_sessions_reconciled
        )
        candidate_collection_valid = collection_complete and not incomplete_index_months
        optional_failures = [
            {
                "task": result.get("task"),
                "diagnostic": result.get("optional_failure"),
                "observed_at": result.get("observed_at"),
            }
            for result in completed.values()
            if isinstance(result, Mapping) and result.get("optional_failure")
        ]
        optional_failures.extend(
            failure
            for failure in checkpoint.get("optional_failures", {}).values()
            if isinstance(failure, Mapping)
        )
        report: dict[str, Any] = {
            "schema_version": TUSHARE_PIT_BACKFILL_SCHEMA,
            "run_id": self.plan.run_id,
            "observed_at": utc_now(),
            "classification": "quarantine",
            "transport": self.client.transport_diagnostic(),
            "plan": self.plan.public_scope(),
            "progress": {
                "calls_this_invocation": calls_this_invocation,
                "completed_this_invocation": completed_this_invocation,
                "max_calls_per_invocation": self.max_calls,
                "foundation_complete": foundation_complete,
                "full_historical_universe_plan_materialized": (
                    full_universe_plan_materialized
                ),
                "canonical_open_session_count": len(open_sessions),
                "reconciled_session_count": len(reconciliation_rows),
                "all_sessions_reconciled": all_sessions_reconciled,
                "session_cross_section_task_count": len(market_tasks),
                "historical_member_metadata_task_count": len(metadata_tasks),
                "planned_tasks": total,
                "completed_tasks": completed_total,
                "pending_tasks": total - completed_total,
                "all_index_historical_security_count": len(membership_months),
                "full_universe_planned_security_count": len(membership_months),
                "legacy_completed_evidence_count": sum(
                    isinstance(result, Mapping)
                    and isinstance(result.get("task"), Mapping)
                    and result["task"].get("category")
                    in {
                        "sample_market_or_state",
                        "sample_industry_membership",
                        "sample_corporate_event",
                        "full_universe_market_or_state",
                    }
                    for result in checkpoint["completed"].values()
                ),
                "legacy_failure_count": len(checkpoint.get("legacy_failures", {})),
                "legacy_receipts_counted_as_session_cross_sections": False,
                "full_universe_membership_months": sum(
                    len(months) for months in membership_months.values()
                ),
                "sampled_security_count": len(
                    _deterministic_sample(
                        list(membership_months), self.plan.sample_size
                    )
                ),
                "sampled_security_count_is_diagnostic_only": True,
                "reused_legacy_task_count": reused_legacy_tasks,
                "reused_prior_checkpoint_task_count": reused_prior_tasks,
                "complete": collection_complete,
            },
            "coverage_by_category": by_category,
            "index_month_coverage": index_month_coverage,
            "incomplete_index_months": incomplete_index_months,
            "session_universe_intersection": {
                "method": "local_after_full_market_artifact_capture",
                "universe_source": "historical_monthly_index_weight_artifacts",
                "session_source": "canonical_trade_cal_artifacts",
                "valid_sessions": sum(
                    isinstance(row, Mapping) and row.get("valid") is True
                    for row in reconciliation_rows
                ),
                "invalid_sessions": len(reconciliation_blockers),
                "member_status_counts": dict(sorted(aggregate_status_counts.items())),
                "ambiguous_member_count": aggregate_ambiguity,
                "conflicting_member_count": aggregate_conflicts,
                "production_full_day_tradability_proven": False,
                "production_semantics": (
                    "daily liquidity and suspend observations are candidate evidence; "
                    "neither proves full-day production tradability without governed review"
                ),
            },
            "failures": [
                *list(checkpoint["failures"].values()),
                *reconciliation_blockers,
            ],
            "optional_failures": optional_failures,
            "candidate_collection_valid": candidate_collection_valid,
            "checkpoint": {
                "sha256": checkpoint["checkpoint_sha256"],
                "contains_credentials": False,
            },
            "production_pit_ready": False,
            "runtime_data_changed": False,
            "promotion": {
                "eligible": False,
                "blockers": [
                    "candidate_quarantine_only",
                    "provider_retention_terms_unverified",
                    "historical_available_at_not_proven",
                    "historical_revision_retention_not_proven",
                    "monthly_membership_effective_window_not_authoritatively_resolved",
                    "independent_authoritative_event_review_required",
                    "governed_import_and_activation_required",
                ],
            },
        }
        report["report_sha256"] = canonical_sha256(report)
        return report
