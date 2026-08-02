"""Durable scheduler for bounded, quarantine-only provider preflights.

This entry point is deliberately separate from the PIT import/activation state
machine.  It submits a public, credential-free request scope to the existing
job broker.  Only the executing worker resolves the provider secret, and the
only persistence target is the content-addressed provider candidate store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from backend.config import settings
from backend.data.provider_artifacts import ContentAddressedProviderArtifactStore
from backend.data.sources.tushare_candidate import (
    DATASET_SPECS,
    TushareCandidateClient,
    TushareCandidateError,
    run_standard_preflight,
)
from backend.dependencies import get_job_broker
from backend.services.pit_durable_update import require_automation_service_identity

logger = logging.getLogger("quant_platform.candidate_preflight_scheduler")

JOB_SCHEMA = "provider-candidate-preflight-job/v1"
OUTCOME_SCHEMA = "provider-candidate-preflight-outcome/v1"
ERROR_SCHEMA = "provider-candidate-preflight-error/v1"
JOB_TYPE = "candidate_data_preflight"
_SOURCE = "candidate_preflight_scheduler"
_PROVIDER = "tushare_pro"
_TS_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_INDEX_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")


class CandidatePreflightJobError(RuntimeError):
    """A credential-free candidate probe could not satisfy its boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "candidate_preflight_failed",
        retryable: bool = False,
        report_sha256: str | None = None,
        required_failures: list[str] | None = None,
        provider_diagnostics: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.report_sha256 = (
            report_sha256 if report_sha256 and re.fullmatch(r"[0-9a-f]{64}", report_sha256) else None
        )
        self.required_failures = sorted(
            {
                value
                for value in (required_failures or [])
                if re.fullmatch(r"[a-z0-9_]{1,80}", value)
            }
        )[:32]
        self.provider_diagnostics = sorted(
            {
                value
                for value in (provider_diagnostics or [])
                if re.fullmatch(r"[a-z0-9_]{1,80}", value)
            }
        )[:32]

    def public_result(self) -> dict[str, Any]:
        """Return a bounded, credential-free durable failure summary."""

        return {
            "schema_version": ERROR_SCHEMA,
            "preflight_outcome": "failed",
            "code": self.code,
            "retryable": self.retryable,
            "report_sha256": self.report_sha256,
            "required_failures": self.required_failures,
            "provider_diagnostics": self.provider_diagnostics,
            "candidate_collection_valid": False,
            "production_pit_ready": False,
            "production_import_performed": False,
            "activation_performed": False,
        }


def _cycle_key(now: datetime | None = None) -> str:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    minutes = max(int(settings.PIT_CANDIDATE_PREFLIGHT_SCAN_MINUTES), 1)
    slot = (current.hour * 60 + current.minute) // minutes
    return f"candidate-preflight:{current.date().isoformat()}:{slot}"


def _bounded_request(now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    lookback = min(
        max(int(settings.PIT_CANDIDATE_PREFLIGHT_LOOKBACK_DAYS), 2),
        31,
    )
    end = current.date() - timedelta(days=1)
    start = end - timedelta(days=lookback - 1)
    return {
        "schema_version": JOB_SCHEMA,
        "idempotency_key": _cycle_key(current),
        "source": _SOURCE,
        "provider": _PROVIDER,
        "ts_code": str(settings.PIT_CANDIDATE_PREFLIGHT_TS_CODE).strip().upper(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "index_code": str(settings.PIT_CANDIDATE_PREFLIGHT_INDEX_CODE)
        .strip()
        .upper(),
        "cross_check": bool(settings.PIT_CANDIDATE_PREFLIGHT_CROSS_CHECK),
        "quarantine_only": True,
        "production_import_permitted": False,
        "activation_permitted": False,
    }


def _validated_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = dict(value)
    required_exact = {
        "schema_version": JOB_SCHEMA,
        "source": _SOURCE,
        "provider": _PROVIDER,
        "quarantine_only": True,
        "production_import_permitted": False,
        "activation_permitted": False,
    }
    if any(request.get(key) != expected for key, expected in required_exact.items()):
        raise CandidatePreflightJobError(
            "candidate preflight request crossed its quarantine boundary"
        )
    allowed = {
        *required_exact,
        "idempotency_key",
        "ts_code",
        "start",
        "end",
        "index_code",
        "cross_check",
    }
    if set(request) != allowed:
        raise CandidatePreflightJobError("candidate preflight request fields are invalid")
    if not _TS_CODE.fullmatch(str(request.get("ts_code") or "")):
        raise CandidatePreflightJobError("candidate preflight security code is invalid")
    if not _INDEX_CODE.fullmatch(str(request.get("index_code") or "")):
        raise CandidatePreflightJobError("candidate preflight index code is invalid")
    if not re.fullmatch(
        r"candidate-preflight:[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9]{1,4}",
        str(request.get("idempotency_key") or ""),
    ):
        raise CandidatePreflightJobError("candidate preflight idempotency key is invalid")
    if not isinstance(request.get("cross_check"), bool):
        raise CandidatePreflightJobError("candidate preflight cross-check flag is invalid")
    try:
        start = date.fromisoformat(str(request["start"]))
        end = date.fromisoformat(str(request["end"]))
    except (KeyError, ValueError) as exc:
        raise CandidatePreflightJobError(
            "candidate preflight date window is invalid"
        ) from exc
    if start > end or (end - start).days > 30 or end > datetime.now(UTC).date():
        raise CandidatePreflightJobError("candidate preflight date window is unsafe")
    return request


async def enqueue_candidate_preflight(
    *, now: datetime | None = None
) -> tuple[str, dict[str, Any]]:
    """Submit exactly one durable job for a UTC scan slot.

    The service identity is revalidated before each submission.  Neither this
    function nor the durable job payload reads or contains the provider token.
    Terminal jobs are deduplicated too, so an API restart in the same slot does
    not repeat provider calls or manufacture a second observation time.
    """

    require_automation_service_identity()
    request = _validated_request(_bounded_request(now))
    job_uuid = await get_job_broker().submit_job(
        job_type=JOB_TYPE,
        params=request,
        user_id=None,
        resource_type="provider_candidate_cycle",
        resource_id=str(request["idempotency_key"]),
        deduplicate_active=True,
        deduplicate_existing=True,
    )
    return job_uuid, request


def _latest_complete_month_for_cycle(request: Mapping[str, Any]) -> tuple[str, str]:
    cycle_key = str(request.get("idempotency_key") or "")
    try:
        cycle_date = date.fromisoformat(cycle_key.split(":", maxsplit=2)[1])
    except (IndexError, ValueError) as exc:
        raise CandidatePreflightJobError(
            "candidate preflight cycle date is invalid",
            code="candidate_preflight_request_invalid",
        ) from exc
    current_month = cycle_date.replace(day=1)
    previous_end = current_month - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    return previous_start.strftime("%Y%m%d"), previous_end.strftime("%Y%m%d")


def _is_latest_month_publication_deferred(
    payload: Mapping[str, Any], request: Mapping[str, Any]
) -> bool:
    """Recognize one narrow, auditable insufficient-coverage condition.

    This does not assert why the provider returned no snapshot.  It only says
    that the exact latest fully elapsed month was requested, the provider
    accepted that complete-month request, and every other required check
    passed.  Historical gaps, partial snapshots and transport failures are not
    deferred by this predicate.
    """

    if payload.get("candidate_collection_valid") is not False:
        return False
    if payload.get("required_failures") != ["index_weight"]:
        return False
    plan_validation = payload.get("plan_validation")
    if not isinstance(plan_validation, Mapping) or plan_validation.get("failures") != []:
        return False
    probe = payload.get("index_weight_monthly_probe")
    if not isinstance(probe, Mapping):
        return False
    if (
        probe.get("status") != "no_monthly_snapshot_returned"
        or probe.get("reason") != "provider_returned_empty_complete_month"
    ):
        return False
    requested_month = probe.get("requested_complete_month")
    if not isinstance(requested_month, Mapping):
        return False
    expected_start, expected_end = _latest_complete_month_for_cycle(request)
    if requested_month != {"start_date": expected_start, "end_date": expected_end}:
        return False
    request_scope = payload.get("request_scope")
    if not isinstance(request_scope, Mapping) or any(
        request_scope.get(field) != request.get(field)
        for field in ("ts_code", "start", "end", "index_code")
    ):
        return False
    datasets = payload.get("datasets")
    if not isinstance(datasets, list):
        return False
    required_statuses: dict[str, list[str]] = {
        dataset: [] for dataset, spec in DATASET_SPECS.items() if not spec.optional
    }
    for item in datasets:
        if not isinstance(item, Mapping):
            return False
        dataset = str(item.get("dataset") or "")
        if dataset in required_statuses:
            required_statuses[dataset].append(str(item.get("status") or ""))
    return all(
        statuses == (["insufficient_rows"] if dataset == "index_weight" else ["ok"])
        for dataset, statuses in required_statuses.items()
    )


def _failure_from_report(payload: Mapping[str, Any]) -> CandidatePreflightJobError:
    required_failures = payload.get("required_failures")
    bounded_failures = (
        [str(value) for value in required_failures]
        if isinstance(required_failures, list)
        else []
    )
    diagnostics: list[str] = []
    retryable = False
    datasets = payload.get("datasets")
    if isinstance(datasets, list):
        for item in datasets:
            if not isinstance(item, Mapping) or item.get("status") != "failed":
                continue
            diagnostic = item.get("diagnostic")
            if not isinstance(diagnostic, Mapping):
                continue
            code = str(diagnostic.get("code") or "")
            if code:
                diagnostics.append(code)
            retryable = retryable or diagnostic.get("retryable") is True
    if any("permission" in code or "points" in code for code in diagnostics):
        code = "candidate_provider_authorization_failed"
    elif any(
        code.startswith("provider_network_")
        or code.startswith("provider_http_rate_")
        or code == "provider_service_unavailable"
        or code == "explicit_proxy_transport_failed"
        for code in diagnostics
    ):
        code = "candidate_provider_transport_failed"
    elif diagnostics:
        code = "candidate_provider_contract_failed"
    else:
        code = "candidate_provider_required_coverage_missing"
    return CandidatePreflightJobError(
        f"{code}; inspect the credential-free quarantine result",
        code=code,
        retryable=retryable,
        report_sha256=str(payload.get("stored_report_sha256") or ""),
        required_failures=bounded_failures,
        provider_diagnostics=diagnostics,
    )


def _assert_quarantine_report(
    report: Mapping[str, Any], token: str, request: Mapping[str, Any]
) -> dict[str, Any]:
    payload = dict(report)
    promotion = payload.get("promotion")
    if (
        payload.get("classification") != "quarantine"
        or payload.get("production_pit_ready") is not False
        or not isinstance(promotion, Mapping)
        or promotion.get("eligible") is not False
    ):
        raise CandidatePreflightJobError(
            "candidate provider report attempted to cross the production boundary"
        )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if token and token in encoded:
        raise CandidatePreflightJobError(
            "candidate provider report unexpectedly contained credentials"
        )
    if payload.get("candidate_collection_valid") is not True:
        if _is_latest_month_publication_deferred(payload, request):
            probe = payload["index_weight_monthly_probe"]
            payload["preflight_outcome"] = "deferred_insufficient_coverage"
            payload["outcome"] = {
                "schema_version": OUTCOME_SCHEMA,
                "status": "deferred",
                "code": "latest_complete_month_snapshot_unavailable",
                "cause": "publication_lag_retention_or_entitlement_unresolved",
                "requested_complete_month": dict(probe["requested_complete_month"]),
                "fresh_candidate_coverage": False,
                "retry_policy": "next_scheduler_slot",
                "observation_window_shifted": False,
                "production_import_performed": False,
                "activation_performed": False,
            }
            return payload
        raise _failure_from_report(payload)
    payload["preflight_outcome"] = "candidate_collected"
    payload["outcome"] = {
        "schema_version": OUTCOME_SCHEMA,
        "status": "collected",
        "code": "required_candidate_checks_passed",
        "fresh_candidate_coverage": True,
        "observation_window_shifted": False,
        "production_import_performed": False,
        "activation_performed": False,
    }
    return payload


async def run_candidate_preflight_job(params: Mapping[str, Any]) -> dict[str, Any]:
    """Execute a validated job without any PIT import or activation capability."""

    request = _validated_request(params)
    # Revalidate deactivation/permission changes at execution time. The actor
    # is intentionally not forwarded to the provider or stored in artifacts.
    require_automation_service_identity()
    token = settings.TUSHARE_TOKEN.get_secret_value()
    evidence_root = (
        settings.abs_path(settings.PIT_EVIDENCE_DIR)
        / "provider_candidates"
        / "tushare"
    )
    store = ContentAddressedProviderArtifactStore(evidence_root)
    client = TushareCandidateClient(
        token=token,
        store=store,
        proxy_url=settings.PIT_CANDIDATE_OUTBOUND_PROXY_URL.get_secret_value(),
    )
    try:
        report = await run_standard_preflight(
            client,
            ts_code=str(request["ts_code"]),
            start=str(request["start"]),
            end=str(request["end"]),
            index_code=str(request["index_code"]),
            cross_check=bool(request["cross_check"]),
        )
    except TushareCandidateError:
        # The adapter already emits bounded, provider-body-free errors. Keep a
        # stable public diagnostic so future adapters cannot leak a response.
        raise CandidatePreflightJobError(
            "candidate provider preflight failed; inspect quarantine audit"
        ) from None
    return _assert_quarantine_report(report, token, request)


async def run_candidate_preflight_scheduler() -> None:
    """Periodically enqueue credential-free, quarantine-only candidate jobs."""

    if not settings.PIT_CANDIDATE_PREFLIGHT_AUTO_RUN:
        logger.info("Candidate provider preflight scheduler disabled by configuration")
        return
    minutes = max(int(settings.PIT_CANDIDATE_PREFLIGHT_SCAN_MINUTES), 1)

    async def scan() -> None:
        try:
            job_uuid, request = await enqueue_candidate_preflight()
            logger.info(
                "Candidate provider preflight cycle %s is queued as job %.12s",
                request["idempotency_key"],
                job_uuid,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unable to enqueue candidate provider preflight")

    scheduler = AsyncIOScheduler(timezone=UTC)
    scheduler.add_job(
        scan,
        trigger=IntervalTrigger(minutes=minutes),
        id="candidate-provider-preflight-scan",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=max(minutes * 60, 60),
    )
    scheduler.start()
    logger.info(
        "Candidate provider preflight scheduler enabled: scan every %d minute(s)",
        minutes,
    )
    await scan()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
