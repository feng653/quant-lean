"""Checkpointed PIT-union backfill for the immutable dual-price ledger.

This module intentionally does not read legacy pool Parquet as truth.  It
fetches every security once for the union of verified point-in-time timelines,
derives hfq from the exact raw/preclose response, imports canonical batches,
then stores lightweight scope bindings instead of copying prices per pool.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Awaitable, Callable, Mapping

import pandas as pd

from backend.config import settings
from backend.data.point_in_time_master import PointInTimeMasterStore
from backend.data.point_in_time_universe import (
    PointInTimeUniverseTimeline,
    resolve_point_in_time_universe,
)
from backend.data.price_ledger import (
    IMPORT_SCHEMA_VERSION,
    PriceLedgerStore,
    PriceLedgerValidationError,
)
from backend.data.sources.baostock_source import (
    BaoStockLedgerFetchResult,
    BaoStockSource,
    rebuild_hfq_panel,
)
from backend.data.versioning import canonical_digest

BACKFILL_PLAN_SCHEMA = "price-ledger-backfill-plan/v1"
BACKFILL_CHECKPOINT_SCHEMA = "price-ledger-backfill-checkpoint/v1"
BACKFILL_REPORT_SCHEMA = "price-ledger-backfill-report/v1"

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class PriceLedgerBackfillError(RuntimeError):
    """A production backfill cannot proceed without complete verified evidence."""


@dataclass(frozen=True, slots=True)
class BackfillBudget:
    chunk_size: int = 20
    min_available_memory_mb: int = 1024
    max_memory_used_ratio: float = 0.90
    rate_limit_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not 1 <= self.chunk_size <= 200:
            raise ValueError("chunk_size must be in [1, 200]")
        if self.min_available_memory_mb < 256:
            raise ValueError("min_available_memory_mb is too small")
        if not 0.1 <= self.max_memory_used_ratio <= 0.98:
            raise ValueError("max_memory_used_ratio is invalid")
        if not 0 <= self.rate_limit_seconds <= 60:
            raise ValueError("rate_limit_seconds is invalid")


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    timelines: tuple[dict[str, Any], ...]
    trading_dates: tuple[str, ...]
    source_version: str
    source_dataset: str = "query_history_k_data_plus_raw_preclose"
    schema_version: str = BACKFILL_PLAN_SCHEMA

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BackfillPlan:
        if value.get("schema_version") != BACKFILL_PLAN_SCHEMA:
            raise PriceLedgerBackfillError("unsupported backfill plan schema")
        timelines = value.get("timelines")
        dates = tuple(str(item) for item in value.get("trading_dates") or [])
        if (
            not isinstance(timelines, list)
            or not timelines
            or not dates
            or tuple(sorted(set(dates))) != dates
        ):
            raise PriceLedgerBackfillError(
                "backfill plan requires canonical timelines and trading dates"
            )
        for item in dates:
            try:
                if pd.Timestamp(item).strftime("%Y-%m-%d") != item:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise PriceLedgerBackfillError(
                    "backfill trading date is invalid"
                ) from exc
        source_version = str(value.get("source_version") or "").strip()
        source_dataset = str(
            value.get("source_dataset")
            or "query_history_k_data_plus_raw_preclose"
        ).strip()
        if (
            not source_version
            or len(source_version) > 80
            or not source_dataset
            or len(source_dataset) > 80
        ):
            raise PriceLedgerBackfillError(
                "backfill source identity is invalid"
            )
        return cls(
            timelines=tuple(dict(item) for item in timelines),
            trading_dates=dates,
            source_version=source_version,
            source_dataset=source_dataset,
        )

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "timelines": list(self.timelines),
            "trading_dates": list(self.trading_dates),
            "source_version": self.source_version,
            "source_dataset": self.source_dataset,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _read_json(path: Path, *, max_bytes: int = 32 * 1024 * 1024) -> Any:
    if path.is_symlink():
        raise PriceLedgerBackfillError("backfill input cannot be a symlink")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PriceLedgerBackfillError("backfill input is unavailable") from exc
    if size <= 0 or size > max_bytes:
        raise PriceLedgerBackfillError("backfill input size is invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PriceLedgerBackfillError("backfill input is invalid JSON") from exc


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PriceLedgerBackfillError("checkpoint cannot be a symlink")
    payload = (_canonical_json(value) + "\n").encode("utf-8")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(file_descriptor, 0o600)
        handle = os.fdopen(file_descriptor, "wb")
        file_descriptor = -1
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _load_checkpoint(path: Path, *, plan_sha256: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    checkpoint = _read_json(path)
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("schema_version") != BACKFILL_CHECKPOINT_SCHEMA
        or checkpoint.get("plan_sha256") != plan_sha256
    ):
        raise PriceLedgerBackfillError(
            "checkpoint does not match the immutable backfill plan"
        )
    expected = checkpoint.get("content_sha256")
    unsigned = dict(checkpoint)
    unsigned.pop("content_sha256", None)
    if expected != _content_hash(unsigned):
        raise PriceLedgerBackfillError("checkpoint integrity verification failed")
    return checkpoint


def _save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    unsigned = dict(checkpoint)
    unsigned.pop("content_sha256", None)
    checkpoint = {**unsigned, "content_sha256": _content_hash(unsigned)}
    _atomic_write_json(path, checkpoint)


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty or not isinstance(frame.columns, pd.MultiIndex):
        return []
    records: list[dict[str, Any]] = []
    codes = sorted({str(item) for item in frame.columns.get_level_values(0)})
    for code in codes:
        available = {
            str(field).lower(): column
            for column in frame[code].columns
            for field in [column]
        }
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(available):
            raise PriceLedgerBackfillError(
                f"source panel lacks ledger fields for {code}"
            )
        subset = frame[code][sorted(required)].dropna(how="all")
        for day, row in subset.iterrows():
            if row.isna().any():
                raise PriceLedgerBackfillError(
                    "source returned a partial OHLCV observation"
                )
            records.append(
                {
                    "security_code": code,
                    "date": pd.Timestamp(day).strftime("%Y-%m-%d"),
                    **{field: float(row[field]) for field in sorted(required)},
                }
            )
    records.sort(key=lambda item: (item["security_code"], item["date"]))
    return records


def _resource_report(budget: BackfillBudget) -> dict[str, Any]:
    from backend.jobs.resources import SystemLoadProvider

    snapshot = SystemLoadProvider().sample()
    if (
        snapshot.memory_available_mb is None
        or snapshot.memory_used_ratio is None
    ):
        return {
            "admitted": False,
            "reason": "resource_measurement_unavailable",
        }
    available_mb = int(snapshot.memory_available_mb)
    used_ratio = float(snapshot.memory_used_ratio)
    admitted = bool(
        available_mb >= budget.min_available_memory_mb
        and used_ratio <= budget.max_memory_used_ratio
    )
    return {
        "admitted": admitted,
        "reason": None if admitted else "resource_budget_exceeded",
        "available_memory_mb": available_mb,
        "memory_used_ratio": used_ratio,
        "min_available_memory_mb": budget.min_available_memory_mb,
        "max_memory_used_ratio": budget.max_memory_used_ratio,
    }


class PriceLedgerBackfillService:
    """Run one exact, resumable canonical backfill from verified PIT timelines."""

    def __init__(
        self,
        *,
        ledger_store: PriceLedgerStore,
        pit_store: PointInTimeMasterStore,
        source: BaoStockSource | None = None,
    ) -> None:
        self.ledger_store = ledger_store
        self.pit_store = pit_store
        self.source = source or BaoStockSource(price_adjustment="raw")

    def verify_plan(
        self,
        plan: BackfillPlan,
    ) -> tuple[PointInTimeUniverseTimeline, ...]:
        """Resolve every timeline again from the local immutable PIT master."""

        resolved: list[PointInTimeUniverseTimeline] = []
        for identity in plan.timelines:
            scope_id = str(identity.get("pool_id") or "")
            if not scope_id:
                raise PriceLedgerBackfillError(
                    "point-in-time timeline scope is missing"
                )
            try:
                timeline = resolve_point_in_time_universe(
                    self.pit_store,
                    pool_id=scope_id,
                    trading_dates=plan.trading_dates,
                    expected_count=(
                        int(identity["expected_count"])
                        if identity.get("expected_count") is not None
                        else None
                    ),
                )
            except Exception as exc:
                raise PriceLedgerBackfillError(
                    "actual backfill blocked: production PIT timeline is not ready"
                ) from exc
            if canonical_digest(timeline.identity()) != canonical_digest(identity):
                raise PriceLedgerBackfillError(
                    "actual backfill blocked: PIT timeline identity drifted"
                )
            resolved.append(timeline)
        if len({item.pool_id for item in resolved}) != len(resolved):
            raise PriceLedgerBackfillError("backfill plan contains duplicate scopes")
        return tuple(resolved)

    async def run(
        self,
        plan: BackfillPlan,
        *,
        checkpoint_path: Path,
        imported_by_user_id: int,
        budget: BackfillBudget | None = None,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
        stop_after_chunks: int | None = None,
    ) -> dict[str, Any]:
        budget = budget or BackfillBudget()
        timelines = self.verify_plan(plan)
        union_codes = sorted(
            {code for timeline in timelines for code in timeline.union_codes}
        )
        if not union_codes:
            raise PriceLedgerBackfillError(
                "actual backfill blocked: PIT union is empty"
            )
        plan_sha256 = canonical_digest(plan.identity())
        chunks = [
            union_codes[position : position + budget.chunk_size]
            for position in range(0, len(union_codes), budget.chunk_size)
        ]
        canonical_scope = "canonical_cn_a_" + plan_sha256[:24]
        if dry_run:
            return {
                "schema_version": BACKFILL_REPORT_SCHEMA,
                "status": "dry_run_valid",
                "plan_sha256": plan_sha256,
                "scope_ids": [item.pool_id for item in timelines],
                "canonical_scope_id": canonical_scope,
                "trading_date_count": len(plan.trading_dates),
                "union_code_count": len(union_codes),
                "chunk_count": len(chunks),
                "network_requested": False,
                "database_written": False,
                "limitations": [
                    "dry_run_did_not_fetch_or_import_prices",
                    "baostock_public_source_not_live_certified",
                ],
            }

        checkpoint = _load_checkpoint(
            checkpoint_path,
            plan_sha256=plan_sha256,
        )
        if checkpoint is None:
            checkpoint = {
                "schema_version": BACKFILL_CHECKPOINT_SCHEMA,
                "plan_sha256": plan_sha256,
                "canonical_scope_id": canonical_scope,
                "retrieved_at": datetime.now(UTC).isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "completed_chunks": {},
                "status": "running",
            }
            _save_checkpoint(checkpoint_path, checkpoint)
        completed = dict(checkpoint.get("completed_chunks") or {})
        all_suspensions: list[dict[str, str]] = []
        batch_ids: list[str] = []
        gap_reports: list[dict[str, Any]] = []
        fetched_this_run = 0

        expected_by_code: dict[str, set[str]] = {}
        for timeline in timelines:
            for day, members in zip(
                timeline.dates,
                timeline.members_by_date,
                strict=True,
            ):
                for code in members:
                    expected_by_code.setdefault(code, set()).add(day)

        for position, codes in enumerate(chunks):
            chunk_id = f"{position:05d}_" + canonical_digest(codes)[:16]
            saved = completed.get(chunk_id)
            if isinstance(saved, dict) and saved.get("batch_id"):
                batch_ids.append(str(saved["batch_id"]))
                all_suspensions.extend(saved.get("suspensions") or [])
                if isinstance(saved.get("gap_report"), dict):
                    gap_reports.append(dict(saved["gap_report"]))
                continue
            resource = _resource_report(budget)
            if not resource["admitted"]:
                raise PriceLedgerBackfillError(
                    f"backfill paused: {resource['reason']}"
                )
            if progress is not None:
                await progress(
                    {
                        "stage": "fetching",
                        "chunk_index": position,
                        "chunk_count": len(chunks),
                        "completed_code_count": position * budget.chunk_size,
                        "total_code_count": len(union_codes),
                    }
                )
            fetched: BaoStockLedgerFetchResult = (
                await self.source.fetch_ledger_daily_result(
                    codes,
                    plan.trading_dates[0],
                    plan.trading_dates[-1],
                    progress=progress,
                )
            )
            raw_panel = fetched.frame.reindex(
                pd.DatetimeIndex(plan.trading_dates, name="date")
            )
            adjusted_panel, factor_evidence = rebuild_hfq_panel(fetched.frame)
            adjusted_panel = adjusted_panel.reindex(
                pd.DatetimeIndex(plan.trading_dates, name="date")
            )
            raw_records = _frame_records(raw_panel)
            adjusted_records = _frame_records(adjusted_panel)
            raw_identities = {
                (item["security_code"], item["date"]) for item in raw_records
            }
            adjusted_identities = {
                (item["security_code"], item["date"])
                for item in adjusted_records
            }
            if raw_identities != adjusted_identities:
                raise PriceLedgerBackfillError(
                    "raw and adjusted observations diverged"
                )
            statuses = {
                (item["security_code"], item["date"]): item["status"]
                for item in fetched.status_rows
                if item["security_code"] in set(codes)
                and item["date"] in set(plan.trading_dates)
            }
            expected = {
                (code, day)
                for code in codes
                for day in expected_by_code.get(code, set())
            }
            unresolved: list[dict[str, str]] = []
            inconsistent: list[dict[str, str]] = []
            suspensions: list[dict[str, str]] = []
            for code, day in sorted(expected):
                status = statuses.get((code, day))
                observed = (code, day) in raw_identities
                if status == "suspended" and not observed:
                    suspensions.append(
                        {
                            "security_code": code,
                            "date": day,
                            "status": "suspended",
                        }
                    )
                elif status == "traded" and observed:
                    continue
                elif status is None:
                    unresolved.append(
                        {
                            "security_code": code,
                            "date": day,
                            "reason": "source_status_missing",
                        }
                    )
                else:
                    inconsistent.append(
                        {
                            "security_code": code,
                            "date": day,
                            "reason": "status_price_inconsistent",
                        }
                    )
            gap_report = {
                "chunk_id": chunk_id,
                "requested_code_count": len(codes),
                "expected_membership_observation_count": len(expected),
                "traded_observation_count": len(expected & raw_identities),
                "suspension_observation_count": len(suspensions),
                "unresolved_gap_count": len(unresolved),
                "inconsistent_observation_count": len(inconsistent),
                "unresolved_examples": unresolved[:100],
                "inconsistent_examples": inconsistent[:100],
                "source_evidence_sha256": fetched.evidence["content_sha256"],
            }
            gap_reports.append(gap_report)
            if unresolved or inconsistent:
                checkpoint["status"] = "blocked_source_gaps"
                checkpoint["last_gap_report"] = gap_report
                _save_checkpoint(checkpoint_path, checkpoint)
                raise PriceLedgerBackfillError(
                    "actual backfill blocked: source has unresolved PIT gaps"
                )
            if not raw_records:
                raise PriceLedgerBackfillError(
                    "actual backfill blocked: chunk has no traded prices"
                )
            coverage_from = min(item["date"] for item in raw_records)
            coverage_to = max(item["date"] for item in raw_records)
            retrieved_at = str(checkpoint["retrieved_at"])
            raw_source = {
                "provider": "baostock:official",
                "dataset": plan.source_dataset,
                "version": plan.source_version,
                "adjustment": "raw",
                "evidence_level": "declared",
                "retrieved_at": retrieved_at,
                "content_sha256": _content_hash(
                    {
                        "fetch_evidence": fetched.evidence,
                        "status_rows": list(fetched.status_rows),
                    }
                ),
            }
            research_source = {
                "provider": "quant-platform:deterministic",
                "dataset": "baostock_raw_preclose_hfq",
                "version": plan.source_version,
                "adjustment": "hfq",
                "evidence_level": "declared",
                "retrieved_at": retrieved_at,
                "content_sha256": _content_hash(
                    {
                        "raw_source_sha256": raw_source["content_sha256"],
                        "factor_evidence": factor_evidence,
                        "adjusted_records_sha256": _content_hash(
                            adjusted_records
                        ),
                    }
                ),
            }
            imported = self.ledger_store.import_batch(
                schema_version=IMPORT_SCHEMA_VERSION,
                scope_id=canonical_scope,
                coverage_from=coverage_from,
                coverage_to=coverage_to,
                raw_source=raw_source,
                research_source=research_source,
                corporate_action_source=None,
                raw_prices=raw_records,
                research_prices=adjusted_records,
                corporate_actions=[],
                imported_by_user_id=imported_by_user_id,
            )
            batch_ids.append(str(imported["batch_id"]))
            all_suspensions.extend(suspensions)
            completed[chunk_id] = {
                "batch_id": str(imported["batch_id"]),
                "batch_digest": str(imported["batch_digest"]),
                "codes_sha256": canonical_digest(codes),
                "source_evidence_sha256": fetched.evidence["content_sha256"],
                "suspensions": suspensions,
                "gap_report": gap_report,
            }
            checkpoint["completed_chunks"] = completed
            checkpoint["status"] = "running"
            _save_checkpoint(checkpoint_path, checkpoint)
            fetched_this_run += 1
            if (
                stop_after_chunks is not None
                and fetched_this_run >= stop_after_chunks
            ):
                raise PriceLedgerBackfillError(
                    "backfill interrupted after requested test chunk count"
                )
            if budget.rate_limit_seconds:
                await asyncio.sleep(budget.rate_limit_seconds)

        bindings: list[dict[str, Any]] = []
        for timeline in timelines:
            timeline_suspensions = [
                item
                for item in all_suspensions
                if item["security_code"] in set(timeline.union_codes)
                and item["date"] in set(timeline.dates)
                and item["security_code"]
                in set(timeline.members_on(item["date"]))
            ]
            bindings.append(
                self.ledger_store.bind_runtime_scope(
                    scope_id=timeline.pool_id,
                    timeline_identity=timeline.identity(),
                    trading_dates=timeline.dates,
                    batch_ids=batch_ids,
                    status_source={
                        "provider": "baostock:official",
                        "dataset": plan.source_dataset,
                        "version": plan.source_version,
                        "adjustment": "trading_status",
                        "evidence_level": "declared",
                        "retrieved_at": str(checkpoint["retrieved_at"]),
                        "content_sha256": _content_hash(
                            {
                                "plan_sha256": plan_sha256,
                                "completed_source_evidence": sorted(
                                    str(item["source_evidence_sha256"])
                                    for item in completed.values()
                                ),
                                "suspensions": timeline_suspensions,
                            }
                        ),
                    },
                    suspension_observations=timeline_suspensions,
                    bound_by_user_id=imported_by_user_id,
                )
            )

        report = {
            "schema_version": BACKFILL_REPORT_SCHEMA,
            "status": "completed",
            "plan_sha256": plan_sha256,
            "canonical_scope_id": canonical_scope,
            "scope_ids": [item.pool_id for item in timelines],
            "trading_date_count": len(plan.trading_dates),
            "union_code_count": len(union_codes),
            "chunk_count": len(chunks),
            "batch_ids": sorted(set(batch_ids)),
            "bindings": bindings,
            "gap_reports": gap_reports,
            "limitations": [
                "baostock_public_source_not_live_certified",
                "corporate_action_authoritative_evidence_missing",
                "adjustment_factor_changes_may_be_unexplained",
                "legacy_parquet_was_not_used_or_modified",
            ],
        }
        checkpoint["status"] = "completed"
        checkpoint["report_sha256"] = _content_hash(report)
        _save_checkpoint(checkpoint_path, checkpoint)
        return report


def _default_checkpoint(plan: BackfillPlan) -> Path:
    digest = canonical_digest(plan.identity())[:24]
    return settings.abs_path(
        f"{settings.DATA_STAGING_DIR}/price-ledger-{digest}.checkpoint.json"
    )


async def _run_cli(args: argparse.Namespace) -> dict[str, Any]:
    raw_plan = _read_json(Path(args.plan))
    if not isinstance(raw_plan, Mapping):
        raise PriceLedgerBackfillError("backfill plan must be a JSON object")
    plan = BackfillPlan.from_mapping(raw_plan)
    checkpoint = (
        Path(args.checkpoint)
        if args.checkpoint
        else _default_checkpoint(plan)
    )
    service = PriceLedgerBackfillService(
        ledger_store=PriceLedgerStore(
            Path(args.database) if args.database else None
        ),
        pit_store=PointInTimeMasterStore(
            Path(args.database) if args.database else None
        ),
    )
    return await service.run(
        plan,
        checkpoint_path=checkpoint,
        imported_by_user_id=args.user_id,
        budget=BackfillBudget(
            chunk_size=args.chunk_size,
            min_available_memory_mb=args.min_available_memory_mb,
            max_memory_used_ratio=args.max_memory_used_ratio,
            rate_limit_seconds=args.rate_limit_seconds,
        ),
        dry_run=args.dry_run,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill canonical raw/hfq prices from a verified PIT union",
    )
    parser.add_argument("--plan", required=True)
    parser.add_argument("--checkpoint")
    parser.add_argument("--database")
    parser.add_argument("--user-id", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument("--min-available-memory-mb", type=int, default=1024)
    parser.add_argument("--max-memory-used-ratio", type=float, default=0.90)
    parser.add_argument("--rate-limit-seconds", type=float, default=0.25)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(_run_cli(args))
    except (PriceLedgerBackfillError, PriceLedgerValidationError) as exc:
        print(
            _canonical_json(
                {
                    "schema_version": BACKFILL_REPORT_SCHEMA,
                    "status": "blocked",
                    "reason": str(exc),
                }
            )
        )
        return 2
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
