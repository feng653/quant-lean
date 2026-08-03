from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from backend.config import Settings, settings
from backend.jobs.broker import JobBroker
from backend.services import candidate_preflight_scheduler as scheduler


class _Broker:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []

    async def submit_job(self, **kwargs: Any) -> str:
        self.submissions.append(kwargs)
        return "candidate-job-1"


def _report() -> dict[str, Any]:
    return {
        "schema_version": "tushare-candidate-preflight/v1",
        "candidate_collection_valid": True,
        "classification": "quarantine",
        "production_pit_ready": False,
        "promotion": {
            "eligible": False,
            "blockers": ["candidate_quarantine_only"],
        },
        "stored_report_sha256": "a" * 64,
    }


def _deferred_report(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "tushare-candidate-preflight/v1",
        "candidate_collection_valid": False,
        "classification": "quarantine",
        "production_pit_ready": False,
        "promotion": {
            "eligible": False,
            "blockers": ["candidate_quarantine_only"],
        },
        "stored_report_sha256": "b" * 64,
        "request_scope": {
            field: request[field]
            for field in ("ts_code", "start", "end", "index_code")
        },
        "required_failures": ["index_weight"],
        "plan_validation": {"open_session_count": 10, "failures": []},
        "datasets": [
            {
                "dataset": dataset,
                "status": (
                    "insufficient_rows" if dataset == "index_weight" else "ok"
                ),
                "optional": False,
                "row_count": 0 if dataset == "index_weight" else 1,
                "minimum_rows": 300 if dataset == "index_weight" else 1,
                "diagnostic": None,
            }
            for dataset, spec in scheduler.DATASET_SPECS.items()
            if not spec.optional
        ],
        "index_weight_monthly_probe": {
            "status": "no_monthly_snapshot_returned",
            "reason": "provider_returned_empty_complete_month",
            "requested_complete_month": {
                "start_date": "20260701",
                "end_date": "20260731",
            },
            "expected_index_code": request["index_code"],
            "minimum_member_rows": 300,
            "vendor_trade_dates": [],
        },
    }


def test_tushare_setting_is_redacted_by_default() -> None:
    secret = "fixture-settings-secret"
    configured = Settings(_env_file=None, TUSHARE_TOKEN=secret)
    assert configured.TUSHARE_TOKEN.get_secret_value() == secret
    assert secret not in repr(configured)
    assert secret not in repr(configured.model_dump())


def test_scheduler_submission_is_bounded_credential_free_and_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _Broker()
    monkeypatch.setattr(scheduler, "get_job_broker", lambda: broker)
    monkeypatch.setattr(
        scheduler,
        "require_automation_service_identity",
        lambda: {"id": 41, "is_admin": False},
    )
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", SecretStr("fixture-secret-token"))
    monkeypatch.setattr(settings, "PIT_CANDIDATE_PREFLIGHT_SCAN_MINUTES", 360)
    monkeypatch.setattr(settings, "PIT_CANDIDATE_PREFLIGHT_LOOKBACK_DAYS", 14)

    job_uuid, request = asyncio.run(
        scheduler.enqueue_candidate_preflight(
            now=datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
        )
    )

    assert job_uuid == "candidate-job-1"
    assert request["idempotency_key"] == "candidate-preflight:2026-08-02:1"
    assert request["start"] == "2026-07-19"
    assert request["end"] == "2026-08-01"
    assert request["quarantine_only"] is True
    assert request["production_import_permitted"] is False
    assert request["activation_permitted"] is False
    serialized = json.dumps(broker.submissions, sort_keys=True)
    assert "fixture-secret-token" not in serialized
    assert '"token"' not in serialized.lower()
    assert broker.submissions == [
        {
            "job_type": "candidate_data_preflight",
            "params": request,
            "user_id": None,
            "resource_type": "provider_candidate_cycle",
            "resource_id": "candidate-preflight:2026-08-02:1",
            "deduplicate_active": True,
            "deduplicate_existing": True,
        }
    ]


def test_broker_deduplicates_completed_candidate_cycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        params = scheduler._bounded_request(
            datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
        )
        kwargs = {
            "job_type": "candidate_data_preflight",
            "params": params,
            "user_id": None,
            "resource_type": "provider_candidate_cycle",
            "resource_id": params["idempotency_key"],
            "deduplicate_active": True,
            "deduplicate_existing": True,
        }
        first = await broker.submit_job(**kwargs)
        await broker.update_job_progress(
            first,
            progress=1,
            status="completed",
            stage="completed",
        )
        repeated = await broker.submit_job(**kwargs)
        assert repeated == first
        jobs, total = await broker.query_jobs(
            include_all=True,
            include_system=True,
            job_type="candidate_data_preflight",
            page_size=20,
        )
        assert total == 1
        assert jobs[0]["status"] == "completed"
        await broker.shutdown()

    asyncio.run(scenario())


def test_worker_persists_only_quarantine_report_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "fixture-worker-secret"
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", SecretStr(secret))
    monkeypatch.setattr(settings, "PIT_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(
        scheduler,
        "require_automation_service_identity",
        lambda: {"id": 41, "is_admin": False},
    )
    observed: list[dict[str, Any]] = []

    async def preflight(client: Any, **kwargs: Any) -> dict[str, Any]:
        observed.append(
            {
                "kwargs": kwargs,
                "store": str(client.store.root),
            }
        )
        return _report()

    monkeypatch.setattr(scheduler, "run_standard_preflight", preflight)
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    result = asyncio.run(scheduler.run_candidate_preflight_job(request))

    assert result["classification"] == "quarantine"
    assert result["production_pit_ready"] is False
    assert result["promotion"]["eligible"] is False
    assert secret not in json.dumps(result, sort_keys=True)
    assert observed[0]["kwargs"] == {
        "ts_code": "000001.SZ",
        "start": "2026-07-19",
        "end": "2026-08-01",
        "index_code": "000300.SH",
        "cross_check": True,
    }
    assert observed[0]["store"].endswith(
        "provider_candidates/tushare"
    )


def test_latest_complete_month_empty_snapshot_is_completed_as_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "fixture-worker-secret"
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", SecretStr(secret))
    monkeypatch.setattr(settings, "PIT_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(
        scheduler,
        "require_automation_service_identity",
        lambda: {"id": 41, "is_admin": False},
    )
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )

    async def preflight(_client: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["start"] == "2026-07-19"
        assert kwargs["end"] == "2026-08-01"
        return _deferred_report(request)

    monkeypatch.setattr(scheduler, "run_standard_preflight", preflight)
    result = asyncio.run(scheduler.run_candidate_preflight_job(request))

    assert result["preflight_outcome"] == "deferred_insufficient_coverage"
    assert result["candidate_collection_valid"] is False
    assert result["production_pit_ready"] is False
    assert result["promotion"]["eligible"] is False
    assert result["outcome"] == {
        "schema_version": "provider-candidate-preflight-outcome/v1",
        "status": "deferred",
        "code": "latest_complete_month_snapshot_unavailable",
        "cause": "publication_lag_retention_or_entitlement_unresolved",
        "requested_complete_month": {
            "start_date": "20260701",
            "end_date": "20260731",
        },
        "fresh_candidate_coverage": False,
        "retry_policy": "next_scheduler_slot",
        "observation_window_shifted": False,
        "production_import_performed": False,
        "activation_performed": False,
    }
    assert secret not in json.dumps(result, sort_keys=True)


def test_empty_historical_month_is_not_misclassified_as_publication_lag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", SecretStr("fixture-secret"))
    monkeypatch.setattr(settings, "PIT_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(
        scheduler,
        "require_automation_service_identity",
        lambda: {"id": 41, "is_admin": False},
    )
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    report = _deferred_report(request)
    report["index_weight_monthly_probe"]["requested_complete_month"] = {
        "start_date": "20260601",
        "end_date": "20260630",
    }

    async def preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return report

    monkeypatch.setattr(scheduler, "run_standard_preflight", preflight)
    with pytest.raises(scheduler.CandidatePreflightJobError) as caught:
        asyncio.run(scheduler.run_candidate_preflight_job(request))
    assert caught.value.public_result()["code"] == (
        "candidate_provider_required_coverage_missing"
    )
    assert caught.value.public_result()["report_sha256"] == "b" * 64


def test_permission_failure_has_bounded_structured_job_diagnostic() -> None:
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    report = _deferred_report(request)
    index_item = next(
        item for item in report["datasets"] if item["dataset"] == "index_weight"
    )
    index_item.update(
        status="failed",
        diagnostic={
            "code": "provider_permission_or_points_required",
            "provider_code": "-2001",
            "retryable": False,
        },
    )
    report["index_weight_monthly_probe"] = {
        "status": "not_collected",
        "reason": "index_weight_request_failed_before_classification",
    }

    with pytest.raises(scheduler.CandidatePreflightJobError) as caught:
        scheduler._assert_quarantine_report(report, "fixture-secret", request)
    result = caught.value.public_result()
    assert result == {
        "schema_version": "provider-candidate-preflight-error/v1",
        "preflight_outcome": "failed",
        "code": "candidate_provider_authorization_failed",
        "retryable": False,
        "report_sha256": "b" * 64,
        "required_failures": ["index_weight"],
        "provider_diagnostics": ["provider_permission_or_points_required"],
        "candidate_collection_valid": False,
        "production_pit_ready": False,
        "production_import_performed": False,
        "activation_performed": False,
    }
    assert "-2001" not in json.dumps(result, sort_keys=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: report.update(classification="production"),
        lambda report: report.update(production_pit_ready=True),
        lambda report: report["promotion"].update(eligible=True),
    ],
)
def test_worker_rejects_any_promotion_capable_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", SecretStr("fixture-secret-token"))
    monkeypatch.setattr(settings, "PIT_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(
        scheduler,
        "require_automation_service_identity",
        lambda: {"id": 41, "is_admin": False},
    )
    unsafe = _report()
    mutate(unsafe)

    async def preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return unsafe

    monkeypatch.setattr(scheduler, "run_standard_preflight", preflight)
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    with pytest.raises(
        scheduler.CandidatePreflightJobError,
        match="production boundary",
    ):
        asyncio.run(scheduler.run_candidate_preflight_job(request))


def test_enabled_scheduler_runs_initial_scan_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scanned = asyncio.Event()

        async def enqueue() -> tuple[str, dict[str, Any]]:
            scanned.set()
            return "candidate-job-1", {
                "idempotency_key": "candidate-preflight:2026-08-02:1"
            }

        monkeypatch.setattr(settings, "PIT_CANDIDATE_PREFLIGHT_AUTO_RUN", True)
        monkeypatch.setattr(settings, "PIT_CANDIDATE_PREFLIGHT_SCAN_MINUTES", 360)
        monkeypatch.setattr(scheduler, "enqueue_candidate_preflight", enqueue)
        task = asyncio.create_task(scheduler.run_candidate_preflight_scheduler())
        await asyncio.wait_for(scanned.wait(), timeout=1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_formal_job_worker_dispatch_executes_candidate_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.jobs.handlers import execute_job
    from backend.jobs import broker as broker_module

    class ExecutionBroker:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []
            self.cancel_checks = 0

        async def update_job_progress(self, job_uuid: str, **kwargs: Any) -> None:
            self.updates.append({"job_uuid": job_uuid, **kwargs})

        async def raise_if_cancelled(self, _job_uuid: str) -> None:
            self.cancel_checks += 1

    execution_broker = ExecutionBroker()
    monkeypatch.setattr(broker_module, "get_broker", lambda: execution_broker)

    async def execute(params: dict[str, Any]) -> dict[str, Any]:
        assert params["quarantine_only"] is True
        return _report()

    monkeypatch.setattr(scheduler, "run_candidate_preflight_job", execute)
    params = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    asyncio.run(
        execute_job(
            {
                "job_uuid": "a" * 32,
                "job_type": "candidate_data_preflight",
                "params": params,
                "user_id": None,
            }
        )
    )

    assert execution_broker.cancel_checks == 2
    assert execution_broker.updates[-1]["status"] == "completed"
    assert execution_broker.updates[-1]["result"]["classification"] == "quarantine"


def test_formal_worker_persists_deferred_terminal_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.jobs.handlers import execute_job
    from backend.jobs import broker as broker_module

    class ExecutionBroker:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        async def update_job_progress(self, job_uuid: str, **kwargs: Any) -> None:
            self.updates.append({"job_uuid": job_uuid, **kwargs})

        async def raise_if_cancelled(self, _job_uuid: str) -> None:
            return None

    execution_broker = ExecutionBroker()
    monkeypatch.setattr(broker_module, "get_broker", lambda: execution_broker)
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    deferred = _deferred_report(request)
    deferred["preflight_outcome"] = "deferred_insufficient_coverage"

    async def execute(_params: dict[str, Any]) -> dict[str, Any]:
        return deferred

    monkeypatch.setattr(scheduler, "run_candidate_preflight_job", execute)
    asyncio.run(
        execute_job(
            {
                "job_uuid": "c" * 32,
                "job_type": "candidate_data_preflight",
                "params": request,
                "user_id": None,
            }
        )
    )

    terminal = execution_broker.updates[-1]
    assert terminal["status"] == "completed"
    assert terminal["stage"] == "candidate_preflight_deferred"
    assert "覆盖不足" in terminal["message"]
    assert terminal["result"]["candidate_collection_valid"] is False


def test_formal_worker_persists_structured_failure_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.jobs.handlers import execute_job
    from backend.jobs import broker as broker_module

    class ExecutionBroker:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        async def update_job_progress(self, job_uuid: str, **kwargs: Any) -> None:
            self.updates.append({"job_uuid": job_uuid, **kwargs})

        async def raise_if_cancelled(self, _job_uuid: str) -> None:
            return None

    execution_broker = ExecutionBroker()
    monkeypatch.setattr(broker_module, "get_broker", lambda: execution_broker)
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    failure = scheduler.CandidatePreflightJobError(
        "candidate_provider_transport_failed; inspect quarantine result",
        code="candidate_provider_transport_failed",
        retryable=True,
        report_sha256="d" * 64,
        required_failures=["daily"],
        provider_diagnostics=["provider_network_timeout"],
    )

    async def execute(_params: dict[str, Any]) -> dict[str, Any]:
        raise failure

    monkeypatch.setattr(scheduler, "run_candidate_preflight_job", execute)
    with pytest.raises(scheduler.CandidatePreflightJobError):
        asyncio.run(
            execute_job(
                {
                    "job_uuid": "d" * 32,
                    "job_type": "candidate_data_preflight",
                    "params": request,
                    "user_id": None,
                }
            )
        )
    persisted = execution_broker.updates[-1]
    assert persisted["stage"] == "candidate_preflight_failed"
    assert persisted["result"]["code"] == "candidate_provider_transport_failed"
    assert persisted["result"]["retryable"] is True


def test_invalid_or_future_job_request_is_rejected_before_provider_access() -> None:
    request = scheduler._bounded_request(
        datetime(2026, 8, 2, 10, 15, tzinfo=UTC)
    )
    request["activation_permitted"] = True
    with pytest.raises(scheduler.CandidatePreflightJobError, match="quarantine"):
        scheduler._validated_request(request)
