"""Bounded refresh orchestration for non-production research data."""

from __future__ import annotations

import json
import os
import tempfile
import asyncio
import concurrent.futures
import contextvars
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend.config import settings
from backend.data.provider_artifacts import (
    ContentAddressedProviderArtifactStore,
    canonical_json_bytes,
    canonical_sha256,
)
from backend.data.research_data_store import ResearchDataStore, ResearchDataStoreError
from backend.data.sources.tushare_candidate import TushareCandidateClient
from backend.data.sources.tushare_candidate import TushareCandidateError
from backend.data.sources.tushare_pit_backfill import (
    DEFAULT_LAST_COMPLETE_MONTH,
    TusharePitBackfillCollector,
    TusharePitBackfillPlan,
)


ResearchRefreshProgress = Callable[[dict[str, Any]], Awaitable[None]]
ResearchRefreshBlockingProgress = Callable[[dict[str, Any]], None]


class ResearchDataRefreshError(RuntimeError):
    """A research-only provider refresh cannot proceed safely."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(str(result.get("message") or "research data refresh failed"))
        self.result = result


def _collection_failure_code(failure: Any) -> str:
    if not isinstance(failure, dict):
        return ""
    if failure.get("code"):
        return str(failure["code"])
    diagnostic = failure.get("diagnostic")
    return str(diagnostic.get("code") or "") if isinstance(diagnostic, dict) else ""


def last_complete_month(today: date | None = None) -> str:
    current = (today or date.today()).replace(day=1)
    previous = current.fromordinal(current.toordinal() - 1)
    return previous.strftime("%Y-%m")


def _write_supported_month_state(path: Path, payload: dict[str, Any]) -> None:
    sealed = dict(payload)
    sealed["content_sha256"] = canonical_sha256(sealed)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".latest-supported.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(sealed))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def _latest_supported_month(
    *,
    client: TushareCandidateClient,
    source_root: Path,
    calendar_candidate: str,
) -> str:
    """Probe newly elapsed months and retain a durable coverage-lag receipt."""

    state_path = source_root / "latest-supported-month.json"
    supported = DEFAULT_LAST_COMPLETE_MONTH
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            checksum = state.pop("content_sha256", None)
            if checksum == canonical_sha256(state):
                supported = max(supported, str(state.get("latest_supported_month") or ""))
                if (
                    state.get("last_probed_month") == calendar_candidate
                    and state.get("status") == "provider_lag"
                ):
                    try:
                        observed = datetime.fromisoformat(str(state["updated_at"]))
                        age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
                        if age.total_seconds() < 24 * 60 * 60:
                            return supported
                    except (KeyError, TypeError, ValueError):
                        pass
        except (OSError, TypeError, json.JSONDecodeError):
            pass
    if calendar_candidate <= supported:
        return supported
    first = datetime.strptime(calendar_candidate, "%Y-%m").date()
    start_date = first.strftime("%Y%m%d")
    next_month = (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )
    end_date = date.fromordinal(next_month.toordinal() - 1).strftime("%Y%m%d")
    receipts: list[dict[str, Any]] = []
    complete = True
    expected = {
        "000300.SH": 300,
        "000905.SH": 500,
        "000906.SH": 800,
        "000852.SH": 1_000,
    }
    diagnostic: str | None = None
    try:
        for index_code in expected:
            membership = await client.fetch(
                "index_weight",
                {
                    "index_code": index_code,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
            benchmark = await client.fetch(
                "index_daily",
                {
                    "ts_code": index_code,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
            by_date: dict[str, set[str]] = {}
            for row in membership.rows:
                by_date.setdefault(str(row.get("trade_date") or ""), set()).add(
                    str(row.get("con_code") or "")
                )
            member_count = max((len(codes) for codes in by_date.values()), default=0)
            index_complete = member_count >= expected[index_code] and bool(benchmark.rows)
            complete = complete and index_complete
            receipts.append(
                {
                    "index_code": index_code,
                    "member_count": member_count,
                    "benchmark_observations": len(benchmark.rows),
                    "complete": index_complete,
                    "membership_manifest_sha256": membership.receipt["manifest_sha256"],
                    "benchmark_manifest_sha256": benchmark.receipt["manifest_sha256"],
                }
            )
    except TushareCandidateError as exc:
        complete = False
        diagnostic = exc.diagnostic()["code"]
    if complete:
        supported = calendar_candidate
    _write_supported_month_state(
        state_path,
        {
            "schema_version": "tushare-latest-supported-month/v1",
            "latest_supported_month": supported,
            "last_probed_month": calendar_candidate,
            "status": "complete" if complete else "provider_lag",
            "diagnostic": diagnostic,
            "receipts": receipts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "contains_credentials": False,
        },
    )
    return supported


async def run_research_data_refresh(
    *,
    source_id: str,
    from_month: str,
    to_month: str | None,
    max_calls: int,
    retry_optional_failures: bool = True,
    progress: ResearchRefreshProgress | None = None,
    blocking_progress: ResearchRefreshBlockingProgress | None = None,
    evidence_root: str | Path | None = None,
    research_root: str | Path | None = None,
) -> dict[str, Any]:
    """Advance one bounded provider batch and publish a research generation.

    Publication changes only the dedicated research store.  A partial
    Tushare checkpoint is useful when its individual monthly index snapshots
    are complete; unresolved gaps and provider conflicts remain warnings.
    """

    normalized_source = str(source_id).strip().lower()
    if normalized_source != "tushare":
        raise ResearchDataRefreshError(
            {
                "schema_version": "research-data-refresh/v1",
                "status": "unsupported",
                "source_id": normalized_source,
                "message": "该数据源目前只用于交叉验证，尚无通用股票池历史刷新器",
                "research_data_changed": False,
                "production_data_changed": False,
                "live_eligible": False,
            }
        )
    token = settings.TUSHARE_TOKEN.get_secret_value()
    if not token:
        raise ResearchDataRefreshError(
            {
                "schema_version": "research-data-refresh/v1",
                "status": "blocked",
                "source_id": "tushare",
                "message": "Tushare Token 未配置",
                "research_data_changed": False,
                "production_data_changed": False,
                "live_eligible": False,
            }
        )
    source_root = Path(
        evidence_root or ResearchDataStore.default_tushare_evidence_root()
    )
    if progress is not None:
        await progress(
            {
                "overall_fraction": 0.05,
                "stage": "provider_collection",
                "message": "正在按调用预算获取 Tushare 研究候选数据",
            }
        )
    artifact_store = ContentAddressedProviderArtifactStore(source_root)
    client = TushareCandidateClient(
        token=token,
        store=artifact_store,
        proxy_url=settings.PIT_CANDIDATE_OUTBOUND_PROXY_URL.get_secret_value(),
    )
    resolved_to = to_month or await _latest_supported_month(
        client=client,
        source_root=source_root,
        calendar_candidate=last_complete_month(),
    )
    plan = TusharePitBackfillPlan(
        first_month=from_month,
        last_month=resolved_to,
    )
    collection = await TusharePitBackfillCollector(
        client=client,
        plan=plan,
        max_calls=max_calls,
        retry_optional_failures=retry_optional_failures,
        progress=blocking_progress,
    ).run()
    if progress is not None:
        await progress(
            {
                "overall_fraction": 0.75,
                "stage": "research_import",
                "message": "正在生成独立的研究股票池历史版本",
            }
        )
    store = ResearchDataStore(research_root)
    before_status = store.status()
    before_generation = before_status.get("generation_id")
    previous_sessions = int(
        (before_status.get("coverage") or {}).get("reconciled_session_count") or 0
    )
    collected_sessions = int(
        collection["progress"].get("reconciled_session_count") or 0
    )
    completed_this_invocation = int(
        collection["progress"].get("completed_this_invocation") or 0
    )
    first_retryable_optional_failure = any(
        isinstance(failure, dict)
        and isinstance(failure.get("diagnostic"), dict)
        and failure["diagnostic"].get("retryable") is True
        and int(failure.get("attempt_count") or 0) == 1
        for failure in collection.get("optional_failures", [])
    )
    pending = int(collection["progress"]["pending_tasks"])
    raw_failures = collection.get("failures") or []
    failure_values = (
        list(raw_failures.values())
        if isinstance(raw_failures, dict)
        else list(raw_failures)
    )
    blocking_failures = [
        failure
        for failure in failure_values
        if not (
            pending > 0
            and _collection_failure_code(failure)
            == "historical_member_session_coverage_invalid"
        )
    ]
    should_publish = bool(
        collected_sessions > 0
        and (
            before_generation is None
            or not (before_status.get("market") or {}).get("available")
            or pending == 0
            or collected_sessions - previous_sessions >= 252
        )
    )
    imported: dict[str, Any] | None
    import_warning: str | None = None
    try:
        imported = (
            store.import_tushare_reconciled_history(
                source_root,
                run_id=plan.run_id,
                candidate_report_sha256=collection.get("stored_report_sha256"),
                collection_report=collection,
                progress=blocking_progress,
            )
            if should_publish
            else None
        )
        if not should_publish:
            import_warning = (
                "reconciled_market_sessions_not_yet_available"
                if collected_sessions == 0
                else "generation_publish_deferred_until_252_new_sessions"
            )
    except ResearchDataStoreError as exc:
        # An early bounded batch may not yet contain even one complete index
        # month.  Preserve the old active generation and report collection
        # progress rather than selecting another run's checkpoint.
        if str(exc) not in {
            "Tushare index history import is empty",
            "no complete Tushare index history observation exists",
        }:
            raise
        imported = None
        import_warning = "complete_index_snapshot_not_yet_collected"
    after_generation = store.status().get("generation_id")
    conflicts = store.conflict_report()
    research_changed = bool(
        imported is not None and after_generation != before_generation
    )
    report = {
        "schema_version": "research-data-refresh/v1",
        "status": (
            "completed_with_warnings"
            if pending == 0
            else "partial_generation_activated"
            if imported is not None
            else "collection_in_progress"
        ),
        "source_id": "tushare",
        "scope": {"from_month": from_month, "to_month": resolved_to},
        "collection": {
            "run_id": collection["run_id"],
            "completed_tasks": collection["progress"]["completed_tasks"],
            "planned_tasks": collection["progress"]["planned_tasks"],
            "pending_tasks": collection["progress"]["pending_tasks"],
            "calls_this_invocation": collection["progress"]["calls_this_invocation"],
            "completed_this_invocation": completed_this_invocation,
            "reconciled_session_count": collected_sessions,
            "complete": collection["progress"].get("complete") is True,
            "failures": collection["failures"],
            "optional_failures": collection.get("optional_failures", []),
            "classification": collection["classification"],
        },
        "research_generation": imported or store.status(),
        "import_warning": import_warning,
        "cross_source_conflicts": conflicts,
        "research_data_changed": research_changed,
        "activation_committed": bool(
            imported is not None and imported.get("activation_committed") is True
        ),
        "continuation_required": bool(
            pending > 0
            and not blocking_failures
            and int(collection["progress"]["calls_this_invocation"]) > 0
            and (
                completed_this_invocation > 0
                or first_retryable_optional_failure
            )
        ),
        "continuation_blockers": blocking_failures,
        "production_data_changed": False,
        "allowed_uses": ["exploratory_research", "paper_simulation"],
        "risk_policy": "warning_only",
        "live_eligible": False,
    }
    if progress is not None:
        activation_committed = bool(
            imported is not None and imported.get("activation_committed") is True
        )
        await progress(
            {
                "overall_fraction": 0.99 if activation_committed else 1.0,
                # Keep the broker inside its non-cancellable commit stage until
                # the owning worker atomically writes terminal completed state.
                # Otherwise a late cancel could be accepted after activation.
                "stage": (
                    "research_import_activate"
                    if activation_committed
                    else "completed"
                ),
                "message": (
                    "研究数据 generation 已原子激活，正在提交任务完成状态"
                    if activation_committed
                    else "研究数据刷新完成；风险和跨源冲突已保留"
                ),
                "cancellable": not activation_committed,
            }
        )
    return report


async def run_research_data_refresh_responsive(
    *,
    source_id: str,
    from_month: str,
    to_month: str | None,
    max_calls: int,
    retry_optional_failures: bool = True,
    progress: ResearchRefreshProgress | None = None,
    evidence_root: str | Path | None = None,
    research_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run the complete refresh on a worker thread without starving the API loop.

    The collector is async but performs substantial checkpoint reconciliation and
    JSON work between awaits.  Moving only the final SQLite materialization is
    therefore insufficient.  A private event loop owns the whole refresh in one
    executor thread; progress and cancellation checks are synchronously bridged
    back to the server loop.  The thread never receives a copied market frame --
    both loops exchange only small progress dictionaries.
    """

    caller_loop = asyncio.get_running_loop()
    caller_context = contextvars.copy_context()
    stop_requested = threading.Event()
    commit_started = threading.Event()
    cancellation_state_lock = threading.Lock()

    def submit_progress(event: dict[str, Any]) -> concurrent.futures.Future[None]:
        submitted: concurrent.futures.Future[None] = concurrent.futures.Future()

        def schedule() -> None:
            try:
                task = caller_loop.create_task(
                    progress(dict(event)),  # type: ignore[misc]
                    context=caller_context.copy(),
                )
            except BaseException as exc:
                try:
                    submitted.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    pass
                return

            def complete(done: asyncio.Task[None]) -> None:
                # The executor-side waiter may have cancelled its concurrent
                # future after the outer job was cancelled.  Late task
                # completion must not call set_result/set_exception twice.
                if submitted.done():
                    return
                if done.cancelled():
                    submitted.cancel()
                    return
                exception = done.exception()
                try:
                    if exception is not None:
                        submitted.set_exception(exception)
                    else:
                        submitted.set_result(None)
                except concurrent.futures.InvalidStateError:
                    # Cancellation can win between done() above and this
                    # cross-thread completion attempt.
                    pass

            task.add_done_callback(complete)

            def cancel_progress(done: concurrent.futures.Future[None]) -> None:
                if done.cancelled() and not task.done():
                    caller_loop.call_soon_threadsafe(task.cancel)

            submitted.add_done_callback(cancel_progress)

        caller_loop.call_soon_threadsafe(schedule, context=caller_context.copy())
        return submitted

    def relay_progress(event: dict[str, Any]) -> None:
        with cancellation_state_lock:
            if stop_requested.is_set():
                raise asyncio.CancelledError(
                    "research refresh cancellation requested"
                )
            if event.get("cancellable") is False:
                # This is the short, explicit pointer commit region.  Once it
                # begins, an outer scheduler shutdown must let the owner record
                # terminal completed state rather than release a changed active
                # generation as pending/cancelled.
                commit_started.set()
        if progress is None:
            return
        # Schedule with the job execution-claim ContextVar captured from the
        # owning worker task; otherwise cross-thread progress updates would
        # lose the broker's lease-generation fence.
        submitted = submit_progress(event)
        while True:
            try:
                submitted.result(timeout=0.25)
                return
            except concurrent.futures.TimeoutError:
                with cancellation_state_lock:
                    should_stop = stop_requested.is_set()
                if should_stop:
                    submitted.cancel()
                    raise asyncio.CancelledError(
                        "research refresh cancellation requested"
                    )

    async def worker_refresh() -> dict[str, Any]:
        async def async_progress(event: dict[str, Any]) -> None:
            relay_progress(event)

        return await run_research_data_refresh(
            source_id=source_id,
            from_month=from_month,
            to_month=to_month,
            max_calls=max_calls,
            retry_optional_failures=retry_optional_failures,
            progress=async_progress,
            blocking_progress=relay_progress,
            evidence_root=evidence_root,
            research_root=research_root,
        )

    executor_future = caller_loop.run_in_executor(None, lambda: asyncio.run(worker_refresh()))
    try:
        # Shielding is important: cancelling the asyncio waiter cannot stop a
        # running executor thread.  The cooperative flag lets it leave at the
        # next persisted collector/materializer boundary instead.
        return await asyncio.shield(executor_future)
    except asyncio.CancelledError:
        with cancellation_state_lock:
            inside_commit = commit_started.is_set()
            if not inside_commit:
                stop_requested.set()
        if inside_commit:
            # launchd/scheduler cancellation is suppressed only for the short
            # atomic commit tail. Repeated cancel() calls cannot terminate an
            # executor thread, so propagating one here would split the active
            # pointer from the persisted job terminal state. Hard shutdown is
            # handled by launchd/SIGKILL and the atomic, idempotent recovery
            # path; cooperative shutdown waits for this worker to return so
            # _execute_job can persist status=completed.
            while True:
                try:
                    return await asyncio.shield(executor_future)
                except asyncio.CancelledError:
                    continue
        try:
            await asyncio.shield(executor_future)
        except BaseException:
            pass
        raise
