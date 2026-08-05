"""L2 自动体检机：合成数据全链路 fixture（注册→实验→回测→模拟盘初始化）。

合成数据规格（阶段 2.2）：
- 300 只股票 × 300 个交易日 OHLCV pivot（MultiIndex: code/field）
- PIT 会员 fixture（csi300/500/800/1000 全激活）
- benchmark 收盘序列
- 资源控制器 mock 为健康（避免宿主 IO/内存压力误判暂停调度）
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.data.cache_readiness import CachedBenchmarkData, CachedMarketData
from backend.data.point_in_time_master import (
    IMPORT_SCHEMA_VERSION,
    PointInTimeMasterStore,
    _authorize_governed_import,
    _digest,
)
from backend.db.init import init_databases
from backend.jobs import broker as broker_module
from backend.jobs.broker import JobBroker
from backend.main import app


E2E_CODES = [f"{index:06d}" for index in range(1, 301)]
E2E_TEST_START = "2024-01-02"
E2E_TEST_END = "2024-03-29"


def build_membership_fixture(database: Path, codes: list[str]) -> None:
    """Activate governed CSI membership packages so PIT universe resolution passes."""
    store = PointInTimeMasterStore(database)
    package_id = "pitpkg_" + "1" * 32
    package_sha256 = "2" * 64
    receipts: list[dict[str, str]] = []
    for index, scope_id in enumerate(("csi300", "csi500", "csi800", "csi1000")):
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


def build_market_fixture(codes: list[str]) -> pd.DataFrame:
    """Deterministic OHLCV pivot: 300 codes x ~300 business days."""
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


def build_benchmark_fixture(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(
        3_000.0 + pd.Series(range(len(frame)), dtype="float64").values,
        index=frame.index,
        name="close",
    )


@pytest.fixture()
def health_check_env(tmp_path, monkeypatch):
    """Isolated databases + synthetic PIT data + healthy resource controller."""
    experiment_db = tmp_path / "experiment.db"
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(experiment_db))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(
        settings,
        "RESEARCH_SNAPSHOT_DIR",
        str(tmp_path / "research_snapshots"),
    )
    monkeypatch.setattr(
        settings,
        "JWT_SECRET",
        "integration-test-secret-" + ("s" * 48),
    )
    monkeypatch.setattr(
        broker_module,
        "_broker_instance",
        JobBroker(str(tmp_path / "jobs.db")),
    )
    # 体检机只验证业务链路，不验证宿主资源调度：关闭自动调度器，
    # 避免 research_data_refresh 等后台 job 抢占/阻塞 exclusive 回测。
    monkeypatch.setattr(settings, "PAPER_SIMULATION_AUTO_RUN", False)
    monkeypatch.setattr(settings, "MODEL_RETRAIN_AUTO_RUN", False)
    monkeypatch.setattr(settings, "PIT_AUTOMATION_AUTO_RUN", False)
    monkeypatch.setattr(settings, "PIT_CANDIDATE_PREFLIGHT_AUTO_RUN", False)
    monkeypatch.setattr(settings, "RESEARCH_DATA_REFRESH_AUTO_RUN", False)

    asyncio.run(init_databases())
    build_membership_fixture(experiment_db, E2E_CODES)
    research_frame = build_market_fixture(E2E_CODES)
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
    benchmark = build_benchmark_fixture(research_frame)

    async def allow_fixture(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(pool_id="csi300", market=market)

    async def load_market(*_args: Any, **_kwargs: Any) -> CachedMarketData:
        return market

    async def load_benchmark(*_args: Any, **_kwargs: Any) -> CachedBenchmarkData:
        return CachedBenchmarkData(series=benchmark, report={"ready": True})

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

    import backend.jobs.resources as resources_module
    from backend.jobs.resources import CapacityDecision

    healthy_metrics = resources_module.SystemLoadSnapshot(
        cpu_count=8,
        load_1m=1.0,
        normalized_load=0.1,
        memory_total_mb=16384,
        memory_available_mb=8000,
        memory_used_ratio=0.4,
        swap_used_mb=0,
        source="fixture",
        disk_free_mb=50_000,
        io_pressure=0.05,
        io_source="fixture",
    )

    def healthy_decide(self) -> CapacityDecision:
        return CapacityDecision(
            capacity=2,
            configured_max=2,
            degraded=False,
            reasons=(),
            metrics=healthy_metrics,
            pause_heavy=False,
            admission_mode="normal",
        )

    monkeypatch.setattr(
        resources_module.AdaptiveCapacityController,
        "decide",
        healthy_decide,
    )

    test_client = TestClient(app)
    with test_client:
        yield {
            "client": test_client,
            "experiment_db": experiment_db,
        }


@pytest.fixture()
def health_check_session(health_check_env):
    """Registered admin session against the isolated health-check app."""
    client = health_check_env["client"]
    response = client.post(
        "/api/auth/register",
        json={
            "username": "e2e_admin",
            "password": "e2e-pass-123",
            "display_name": "E2E Admin",
        },
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["access_token"]
    return {
        "client": client,
        "headers": {"Authorization": f"Bearer {token}"},
        "experiment_db": health_check_env["experiment_db"],
    }
