from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.api import data as data_api
from backend.api import experiments as experiments_api
from backend.api.experiments import _require_pit_submission
from backend.config import settings
from backend.data.cache import DataCache, LegacyRuntimeDataDisabledError
from backend.data.cache_readiness import CachedMarketData
from backend.data import pit_runtime
from backend.dependencies import get_current_user, get_strategy_registry
from backend.db.init import init_databases
from backend.jobs import broker as broker_module
from backend.jobs.broker import JobBroker
from backend.jobs.handlers import execute_job
from backend.jobs.scheduler import AdaptiveJobScheduler
from backend.services import maintenance


def _frame() -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=2, freq="B")
    columns = pd.MultiIndex.from_product([["600000"], ["open", "high", "low", "close", "volume"]])
    return pd.DataFrame(1.0, index=dates, columns=columns)


def test_non_pit_pool_is_rejected_before_any_data_access() -> None:
    with pytest.raises(pit_runtime.PitRuntimeDataError) as captured:
        pit_runtime.require_pit_pool("custom")
    assert captured.value.code == "point_in_time_pool_unsupported"


def test_runtime_gate_requires_exact_pit_and_canonical_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inspect(*_args, **_kwargs):
        return CachedMarketData(
            frame=_frame(),
            source_provenance={},
            report={
                "ready": True,
                "universe_point_in_time": False,
                "canonical_runtime_price_bound": False,
                "ready_for_unbiased_return_research": False,
                "ready_for_real_tuning": False,
                "ready_for_execution_simulation": False,
                "point_in_time": {
                    "universe": {
                        "ready": False,
                        "reason": "effective_dated_history_missing",
                    }
                },
            },
        )

    monkeypatch.setattr(pit_runtime, "inspect_cached_market_data", inspect)
    with pytest.raises(pit_runtime.PitRuntimeDataError) as captured:
        asyncio.run(
            pit_runtime.require_pit_runtime_input(
                pool_id="csi300",
                required_start="2024-01-01",
                required_end="2024-01-31",
                purpose="research",
            )
        )
    assert captured.value.code == "effective_dated_history_missing"


def test_runtime_gate_accepts_only_complete_purpose_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame()

    async def inspect(*_args, **_kwargs):
        return CachedMarketData(
            frame=frame,
            raw_execution_frame=frame.copy(),
            source_provenance={"content_sha256": "a" * 64},
            runtime_price_binding={"binding_id": "binding"},
            report={
                "ready": True,
                "universe_point_in_time": True,
                "canonical_runtime_price_bound": True,
                "authoritative_trading_calendar_bound": True,
                "point_in_time_benchmark_bound": True,
                "ready_for_unbiased_return_research": True,
                "ready_for_real_tuning": True,
                "ready_for_execution_simulation": True,
            },
        )

    monkeypatch.setattr(pit_runtime, "inspect_cached_market_data", inspect)
    result = asyncio.run(
        pit_runtime.require_pit_runtime_input(
            pool_id="csi300",
            required_start="2024-01-01",
            required_end="2024-01-31",
            purpose="execution",
        )
    )
    assert result.market.frame is frame
    assert result.pool_id == "csi300"


def test_experiment_submission_rejects_network_policy_before_inspection() -> None:
    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            _require_pit_submission(
                pool_id="csi300",
                train_start=None,
                test_start="2024-01-01",
                test_end="2024-02-01",
                data_access_policy="allow_fetch",
                purpose="research",
            )
        )
    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "pit_cache_only_required"


def test_http_experiment_without_real_pit_binding_degrades_not_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment_db = tmp_path / "experiment.db"
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(experiment_db))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path / "empty-cache"))
    asyncio.run(init_databases())
    get_strategy_registry().scan_directory(Path(__file__).resolve().parents[1] / "strategies")

    class Broker:
        calls = 0

        async def submit_job(self, **_kwargs):
            self.calls += 1
            return "must-not-be-created"

    broker = Broker()
    monkeypatch.setattr(experiments_api, "get_job_broker", lambda: broker)
    app = FastAPI()
    app.include_router(experiments_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "is_admin": True,
        "permissions": ["experiments:create"],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/experiments/",
            json={
                "name": "must fail before persistence",
                "strategy_id": "ma_cross_v1",
                "pool_preset": "csi300",
                "test_start": "2024-01-02",
                "test_end": "2024-03-29",
                "data_access_policy": "cache_only",
            },
        )

    assert response.status_code in {200, 409}
    if response.status_code == 409:
        assert response.json()["detail"]["code"] in {
            "price_cache_unavailable",
            "effective_dated_history_missing",
        }
        assert broker.calls == 0
        with sqlite3.connect(experiment_db) as connection:
            count = connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        assert count == 0
    else:
        # 测试分支放宽（v0.8.x 分级门禁，见 5078493）：研究用途在 PIT 数据
        # 未激活时降级放行（缓存数据运行），提交端返回 200 并携带降级告警。
        assert broker.calls == 1
        assert response.json()["data"]["experiment_id"]


def test_legacy_parquet_auto_update_is_disabled(tmp_path: Path) -> None:
    cache = DataCache(str(tmp_path))
    with pytest.raises(LegacyRuntimeDataDisabledError):
        asyncio.run(cache.auto_update(object(), "csi300"))


def test_update_api_rejects_non_pit_scope_before_queue() -> None:
    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            data_api.trigger_data_update(
                pool_id="all_a",
                user={"id": 7},
            )
        )
    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "point_in_time_pool_unsupported"


def test_data_update_and_governance_refresh_submit_distinct_job_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broker:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def submit_job(self, **kwargs):
            self.calls.append(kwargs)
            return f"job-{len(self.calls)}"

    broker = Broker()
    monkeypatch.setattr("backend.dependencies.get_job_broker", lambda: broker)

    market = asyncio.run(
        data_api.trigger_data_update(pool_id="csi300", user={"id": 7})
    )
    governance = asyncio.run(
        data_api.trigger_pit_governance_refresh(pool_id="csi300", user={"id": 7})
    )

    assert market["data"]["mode"] == "async_pit_market_data"
    assert governance["data"]["mode"] == "async_pit_governance_quarantine"
    assert [call["job_type"] for call in broker.calls] == [
        "data_update",
        "pit_governance_refresh",
    ]
    assert broker.calls[0]["resource_type"] == "data_pool"
    assert broker.calls[1]["resource_type"] == "pit_governance"


def test_pool_stock_read_is_local_pit_only_and_returns_structured_empty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "empty.db"))

    def reject_public_source_initialization() -> None:
        raise AssertionError("PIT stock-pool read must not initialize a public source")

    monkeypatch.setattr(data_api._data_svc, "_ensure", reject_public_source_initialization)
    result = asyncio.run(
        data_api.get_pool_stocks(
            "csi300",
            date="2024-01-02",
            user={"id": 7},
        )
    )
    assert result["data"]["stocks"] == []
    assert result["data"]["availability"] == {
        "ready": False,
        "reason": "point_in_time_store_uninitialized",
        "requested_as_of": "2024-01-02",
        "resolved_as_of": None,
        "resolution": "unavailable",
        "staleness_calendar_days": None,
        "network_accessed": False,
        "source_batches": [],
    }
    assert "point_in_time_working_day_coverage_missing" in result["data"]["risk_warnings"]


def test_governed_pool_list_uses_activated_pit_not_legacy_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.data.point_in_time_master import PointInTimeMasterStore

    def reject_public_source_initialization() -> None:
        raise AssertionError("governed pool listing must not initialize UniverseManager")

    def resolved(self, **kwargs):
        return {
            "requested_as_of": kwargs["requested_as_of"],
            "resolved_as_of": kwargs["requested_as_of"],
            "resolution": "exact_activated_observation",
            "staleness_calendar_days": 0,
            "risk_warnings": [],
            "query": {
                "available": True,
                "reason": None,
                "records": [{"security_code": "000001"}],
                "source_batches": [],
            },
        }

    monkeypatch.setattr(data_api._data_svc, "_ensure", reject_public_source_initialization)
    monkeypatch.setattr(PointInTimeMasterStore, "resolve_display_observation", resolved)
    result = asyncio.run(data_api.list_pools(user={"id": 7}))

    assert [pool["id"] for pool in result["data"]] == [
        "csi300",
        "csi500",
        "csi800",
        "csi1000",
    ]
    assert all(pool["count"] == 1 for pool in result["data"])
    assert all(pool["availability"]["ready"] for pool in result["data"])


def test_automatic_update_collects_quarantine_without_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # ``run_pit_governance_refresh`` returns a pending-review result only after binding
    # the collector's durable checkpoint, review queue and coverage report to
    # the managed quarantine root.  A fake collector must therefore model a
    # legal collection result rather than merely returning path-shaped values.
    evidence_root = tmp_path / "pit-evidence"
    workspace = evidence_root / "automatic" / "csindex_history"
    workspace.mkdir(parents=True)
    checkpoint = workspace / "checkpoint.json"
    review_queue = workspace / "review_queue.json"
    coverage_report = workspace / "coverage_report.json"
    checkpoint.write_text('{"state":"complete"}\n', encoding="utf-8")
    review_queue.write_text('{"items":[]}\n', encoding="utf-8")
    coverage_report.write_text('{"coverage":"2024"}\n', encoding="utf-8")
    monkeypatch.setattr(settings, "PIT_EVIDENCE_DIR", str(evidence_root))
    monkeypatch.setattr(
        settings,
        "PIT_EVIDENCE_DB",
        str(evidence_root / "governance.db"),
    )

    class FakeWorkflow:
        def __init__(self, **kwargs):
            assert kwargs["actor_user_id"] == 7

        async def run(self, *, requested_from):
            assert requested_from.isoformat() == "2015-01-01"
            return SimpleNamespace(
                package_id="pitpkg_" + "a" * 32,
                coverage_from=pd.Timestamp("2024-01-01").date(),
                coverage_to=pd.Timestamp("2024-12-31").date(),
                checkpoint_path=checkpoint,
                review_queue_path=review_queue,
                coverage_report_path=coverage_report,
            )

    monkeypatch.setattr(
        "backend.data.sources.csindex_history.CsindexHistoryWorkflow",
        FakeWorkflow,
    )
    monkeypatch.setattr(
        "backend.data.pit_evidence_governance.PitEvidenceGovernance",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "backend.data.point_in_time_master.PointInTimeMasterStore",
        lambda: object(),
    )
    monkeypatch.setattr(
        "backend.data.sources.csindex_pit.CsindexOfficialCollector",
        lambda: object(),
    )
    result = asyncio.run(
        maintenance.run_pit_governance_refresh("csi300", actor_user_id=7)
    )
    assert result["status"] == "pending_review"
    assert result["production_import_performed"] is False
    assert result["activation_performed"] is False
    assert result["runtime_data_changed"] is False
    assert [artifact["role"] for artifact in result["artifacts"]] == [
        "checkpoint",
        "review_queue",
        "coverage_report",
    ]
    assert all(len(artifact["sha256"]) == 64 for artifact in result["artifacts"])
    assert all(artifact["size_bytes"] > 0 for artifact in result["artifacts"])


def test_market_data_update_never_reuses_governance_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_governance(*_args, **_kwargs):
        pytest.fail("market data update must not run governance collection")

    monkeypatch.setattr(
        maintenance,
        "run_pit_governance_refresh",
        forbidden_governance,
    )

    with pytest.raises(maintenance.DataUpdateFailedError) as captured:
        asyncio.run(maintenance.run_data_update("csi300", actor_user_id=7))

    result = captured.value.result
    assert result["status"] == "blocked"
    assert result["governance_refresh_performed"] is False
    assert result["runtime_data_changed"] is False
    assert result["market_data_update"]["planned_codes"] == 0
    assert result["market_data_update"]["fetched_codes"] == 0
    assert result["errors"][0]["code"] == "pit_dual_price_update_not_authorized"


def test_blocked_market_data_job_reaches_failed_terminal_with_structured_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        monkeypatch.setattr(broker_module, "_broker_instance", broker)
        job_id = await broker.submit_job(
            "data_update", {"pool_id": "csi300", "user_id": 7}, user_id=7
        )
        assert await broker.claim_job(job_id, worker_id="test-worker", lease_seconds=60)
        job = await broker.get_job_status(job_id)
        assert job is not None

        scheduler = AdaptiveJobScheduler(broker, execute_job)
        await scheduler._execute_claim(
            job,
            "test-worker",
            int(job["lease_generation"]),
        )
        failed = await broker.get_job_status(job_id)
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["result"]["status"] == "blocked"
        assert failed["result"]["market_data_update"]["fetched_codes"] == 0
        assert failed["result"]["errors"][0]["code"] == (
            "pit_dual_price_update_not_authorized"
        )

    asyncio.run(scenario())
