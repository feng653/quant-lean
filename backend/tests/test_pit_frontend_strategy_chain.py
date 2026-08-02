from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import experiments as experiments_api
from backend.config import settings
from backend.data.cache_readiness import CachedBenchmarkData, CachedMarketData
from backend.data.point_in_time_master import (
    IMPORT_SCHEMA_VERSION,
    PointInTimeMasterStore,
    _authorize_governed_import,
    _digest,
)
from backend.main import _init_databases, _run_experiment
from backend.dependencies import get_current_user, get_strategy_registry
from backend.services.research_manifest import load_run_manifest


class _Broker:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def submit_job(self, **kwargs: Any) -> str:
        self.submissions.append(kwargs)
        return f"fixture-job-{len(self.submissions)}"

    async def update_job_progress(self, job_uuid: str, **kwargs: Any) -> None:
        self.updates.append({"job_uuid": job_uuid, **kwargs})

    async def raise_if_cancelled(self, _job_uuid: str) -> None:
        return None

    async def is_cancel_requested(self, _job_uuid: str) -> bool:
        return False


def _activate_membership_fixture(database: Path, codes: list[str]) -> None:
    store = PointInTimeMasterStore(database)
    package_id = "pitpkg_" + "1" * 32
    package_sha256 = "2" * 64
    receipts: list[dict[str, str]] = []
    for index, scope_id in enumerate(
        ("csi300", "csi500", "csi800", "csi1000")
    ):
        document = {
            "schema_version": IMPORT_SCHEMA_VERSION,
            "domain": "index_membership",
            "scope_id": scope_id,
            "evidence_kind": "effective_dated_history",
            "coverage_from": "2022-01-01",
            "coverage_to": "2025-12-31",
            "source": {
                "provider": "csindex_official",
                "dataset": "isolated-governed-membership-fixture",
                "version": "fixture-v1",
                "evidence_level": "index_provider_authoritative",
                "retrieved_at": "2026-01-02T00:00:00Z",
                "content_sha256": f"{index:x}" * 64,
            },
            "records": [
                {
                    "security_code": code,
                    "effective_from": "2022-01-01",
                    "effective_to": "2025-12-31",
                    "member_name": f"fixture-{code}",
                }
                for code in codes
            ],
        }
        imported = store.import_batch(
            **document,
            imported_by_user_id=1,
            _governed_authorization=_authorize_governed_import(
                package_id=package_id,
                package_sha256=package_sha256,
                document_sha256=_digest(document),
            ),
        )
        receipts.append(
            {
                "scope_id": scope_id,
                "batch_id": imported["batch_id"],
                "batch_digest": imported["batch_digest"],
            }
        )
    store.activate_governed_csi_package(
        package_id=package_id,
        package_sha256=package_sha256,
        receipts=receipts,
    )


def _market_fixture(codes: list[str]) -> pd.DataFrame:
    dates = pd.bdate_range("2022-11-28", "2024-03-29", name="date")
    columns = pd.MultiIndex.from_product(
        [codes, ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    frame = pd.DataFrame(index=dates, columns=columns, dtype="float64")
    steps = pd.Series(range(len(dates)), index=dates, dtype="float64")
    for rank, code in enumerate(codes, start=1):
        close = 8.0 + rank / 20 + steps * (0.002 + rank / 1_000_000)
        frame[(code, "open")] = close * 0.999
        frame[(code, "high")] = close * 1.01
        frame[(code, "low")] = close * 0.99
        frame[(code, "close")] = close
        frame[(code, "volume")] = 1_000_000.0 + rank
    return frame


def test_three_frontend_equivalent_strategy_runs_bind_only_isolated_pit_fixture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    experiment_db = tmp_path / "experiment.db"
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(experiment_db))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        settings,
        "RESEARCH_SNAPSHOT_DIR",
        str(tmp_path / "research_snapshots"),
    )
    asyncio.run(_init_databases())
    codes = [f"{index:06d}" for index in range(1, 301)]
    _activate_membership_fixture(experiment_db, codes)
    research_frame = _market_fixture(codes)
    raw_frame = research_frame.copy(deep=True)
    binding_digest = hashlib.sha256(b"isolated-pit-price-binding").hexdigest()
    provenance = {
        "providers": ["isolated_governed_fixture"],
        "adjustments": ["hfq"],
        "content_sha256": hashlib.sha256(b"isolated-pit-prices").hexdigest(),
    }
    runtime_binding = {
        "binding_id": "bind_" + "3" * 32,
        "binding_digest": binding_digest,
        "canonical_evidence_sha256": hashlib.sha256(
            b"isolated-canonical-evidence"
        ).hexdigest(),
        "batch_ids": ["price_fixture_batch"],
        "batch_digests": ["4" * 64],
    }
    market = CachedMarketData(
        frame=research_frame,
        raw_execution_frame=raw_frame,
        source_provenance=provenance,
        runtime_price_binding=runtime_binding,
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
    benchmark = pd.Series(
        3_000.0 + pd.Series(range(len(research_frame)), dtype="float64").values,
        index=research_frame.index,
        name="close",
    )

    async def allow_fixture(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(pool_id="csi300", market=market)

    async def load_market(*_args: Any, **_kwargs: Any) -> CachedMarketData:
        return market

    async def load_benchmark(*_args: Any, **_kwargs: Any) -> CachedBenchmarkData:
        return CachedBenchmarkData(
            series=benchmark,
            report={"ready": True},
        )

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        allow_fixture,
    )
    monkeypatch.setattr(
        "backend.data.cache_readiness.require_cached_market_data",
        load_market,
    )
    monkeypatch.setattr(
        "backend.data.cache_readiness.require_cached_benchmark",
        load_benchmark,
    )
    broker = _Broker()
    monkeypatch.setattr(experiments_api, "get_job_broker", lambda: broker)
    monkeypatch.setattr("backend.jobs.broker.get_broker", lambda: broker)
    get_strategy_registry().scan_directory(
        Path(__file__).resolve().parents[1] / "strategies"
    )

    app = FastAPI()
    app.include_router(experiments_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "is_admin": True,
        "permissions": ["experiments:create", "experiments:read"],
    }

    strategy_ids = ["ma_cross_v1", "macd_signal_v1", "rsi_reversal_v1"]
    experiment_ids: list[int] = []
    with TestClient(app) as client:
        for strategy_id in strategy_ids:
            response = client.post(
                "/api/experiments/",
                json={
                    "name": f"PIT fixture · {strategy_id}",
                    "strategy_id": strategy_id,
                    "pool_preset": "csi300",
                    "test_start": "2024-01-02",
                    "test_end": "2024-03-29",
                    "data_access_policy": "cache_only",
                },
            )
            assert response.status_code == 200, response.text
            experiment_ids.append(
                int(response.json()["data"]["experiment_id"])
            )

    assert len(broker.submissions) == 3
    for experiment_id in experiment_ids:
        asyncio.run(_run_experiment(experiment_id, f"fixture-{experiment_id}"))

    with sqlite3.connect(experiment_db) as connection:
        statuses = connection.execute(
            "SELECT id, status FROM experiments ORDER BY id"
        ).fetchall()
    assert statuses == [(item, "completed") for item in experiment_ids]

    for experiment_id in experiment_ids:
        envelope = asyncio.run(load_run_manifest(experiment_db, experiment_id))
        manifest = envelope["manifest"]
        assert manifest["experiment"]["data_access_policy"] == "cache_only"
        assert manifest["universe"]["point_in_time"] is True
        timeline = manifest["universe"]["timeline_identity"]
        assert timeline["timeline_hash"]
        assert timeline["source_batches"]
        assert manifest["execution"]["canonical_price_binding"][
            "binding_digest"
        ] == binding_digest
        assert manifest["pit_runtime"] == {
            "schema_version": "pit-runtime-binding/v1",
            "verified": True,
            "network_accessed": False,
            "legacy_or_static_fallback_allowed": False,
            "timeline_hash": timeline["timeline_hash"],
            "canonical_price_binding_id": runtime_binding["binding_id"],
            "canonical_price_binding_digest": binding_digest,
        }
