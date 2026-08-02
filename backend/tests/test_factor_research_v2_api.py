import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import factor_research
from backend.api.factor_research import (
    CompareFactorRunsBody,
    ExportFactorStrategyBody,
    FactorResearchBody,
)
from backend.config import settings
from backend.data.cache import DataCache
from backend.data.cache_readiness import CachedMarketData
from backend.data.factor_research_runs import FactorResearchRunStore
from backend.data.point_in_time_master import PointInTimeValidationError
from backend.dependencies import get_current_user, get_job_broker
from backend.services import factor_research as factor_research_service


def _allow_isolated_pit_factor_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    market: CachedMarketData | None = None,
) -> None:
    async def allow(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            pool_id="csi300",
            market=market,
        )

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        allow,
    )


def _result(factor_id: str = "momentum_20") -> dict[str, Any]:
    return {
        "request": {"primary_horizon": 5},
        "dataset": {"content_sha256": "a" * 64},
        "ic": {
            "5": {
                "summary": {
                    "rank_ic": {
                        "mean": 0.1,
                        "icir": 0.5,
                        "positive_ratio": 0.6,
                    }
                }
            }
        },
        "quantile_returns": {
            "long_short": {"mean": 0.02},
            "monotonicity": 0.8,
        },
        "factor": {"factor_id": factor_id},
    }


def test_analyze_rejects_before_legacy_cache_can_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacyCache:
        async def load_pivot_with_provenance(self, cache_key: str):
            raise AssertionError(f"legacy cache must not be loaded: {cache_key}")

    async def reject(**_kwargs: Any) -> None:
        from backend.data.pit_runtime import PitRuntimeDataError

        raise PitRuntimeDataError(
            "canonical_runtime_binding_missing",
            "PIT binding unavailable at /secret/cache/path",
        )

    monkeypatch.setattr(factor_research, "DataCache", LegacyCache)
    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        reject,
    )
    response = asyncio.run(
        factor_research.analyze_factor(
            FactorResearchBody(
                start="2024-01-01",
                end="2024-02-01",
            ),
            {"id": 7},
        )
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["code"] == "canonical_runtime_binding_missing"
    assert payload["action"] == "refresh_in_data_center"
    assert "/secret/" not in response.body.decode()


def test_factor_job_submission_uses_authenticated_owner_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_isolated_pit_factor_fixture(monkeypatch)
    captured: dict[str, Any] = {}

    class Broker:
        async def submit_job(self, job_type: str, params: dict[str, Any], **kwargs: Any):
            captured.update(
                job_type=job_type,
                params=params,
                **kwargs,
            )
            return "factor-job-1"

    response = asyncio.run(
        factor_research.submit_factor_research_job(
            FactorResearchBody(
                factor_id="momentum_20",
                start="2024-01-01",
                end="2024-02-01",
            ),
            {"id": 7},
            Broker(),
        )
    )

    assert response == {"data": {"job_id": "factor-job-1", "status": "pending"}}
    assert captured["job_type"] == "factor_research"
    assert captured["user_id"] == 7
    assert "owner_user_id" not in captured["params"]
    assert captured["resource_type"] == "factor_research"


def test_factor_job_submission_rejects_unavailable_neutralization_before_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_isolated_pit_factor_fixture(monkeypatch)
    class Broker:
        async def submit_job(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("unavailable neutralization must not be queued")

    async def capability(_cache: Any, cache_key: str) -> dict[str, Any]:
        assert cache_key == "csi300"
        return {
            "neutralization": {
                "modes": {
                    "industry": {
                        "ready": False,
                        "reason": (
                            "current_snapshot_not_valid_for_historical_research"
                        ),
                    }
                }
            }
        }

    monkeypatch.setattr(factor_research, "_cache_capability", capability)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            factor_research.submit_factor_research_job(
                FactorResearchBody(
                    start="2024-01-01",
                    end="2024-02-01",
                    neutralization="industry",
                ),
                {"id": 7},
                Broker(),
            )
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == (
        "current_snapshot_not_valid_for_historical_research"
    )


def test_real_cache_parquet_load_is_offloaded_from_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = DataCache(str(tmp_path / "cache"))

    async def blocking_load(_cache_key: str):
        time.sleep(0.1)
        return None, None

    monkeypatch.setattr(
        cache,
        "_load_verified_pivot_unlocked",
        blocking_load,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            factor_research_service._load_verified_cache(cache, "csi300")
        )
        await asyncio.sleep(0.02)
        assert task.done() is False
        await task

    asyncio.run(scenario())


def test_compare_rejects_another_users_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FactorResearchRunStore(tmp_path / "runs.db")
    first = store.create(
        owner_user_id=7,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20"},
        result=_result(),
    )
    second = store.create(
        owner_user_id=8,
        factor_id="low_volatility_20",
        request={"factor_id": "low_volatility_20"},
        result=_result("low_volatility_20"),
    )
    monkeypatch.setattr(factor_research, "_run_store", lambda: store)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            factor_research.compare_factor_runs(
                CompareFactorRunsBody(
                    run_ids=[first["run_id"], second["run_id"]],
                ),
                {"id": 7},
            )
        )
    assert getattr(exc_info.value, "status_code", None) == 404


def test_export_binds_owned_research_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FactorResearchRunStore(tmp_path / "runs.db")
    run = store.create(
        owner_user_id=7,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20"},
        result=_result(),
    )
    monkeypatch.setattr(factor_research, "_run_store", lambda: store)
    captured: dict[str, Any] = {}

    def fake_export(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "strategy_id": "factor_combo_123456789abc",
            "components": kwargs["components"],
            "version": "1.0.0",
            "top_k_pct": 0.1,
            "research_evidence": [{"run_id": run["run_id"]}],
        }

    class Store:
        publish_strategy = staticmethod(fake_export)

    class Registry:
        @staticmethod
        def replace_strategy_class(_strategy_class: Any) -> None:
            return None

    monkeypatch.setattr(factor_research, "_governance_store", Store)
    monkeypatch.setattr(factor_research, "get_registry", Registry)
    response = asyncio.run(
        factor_research.export_strategy(
            ExportFactorStrategyBody(
                name="证据策略",
                components=[{"factor_id": "momentum_20", "weight": 1}],
                research_run_ids=[run["run_id"]],
            ),
            {"id": 7},
        )
    )

    assert response["data"]["strategy_id"].startswith("factor_combo_")
    assert captured["research_run_ids"] == [run["run_id"]]
    assert captured["owner_user_id"] == 7


def test_export_enforces_locked_protocol_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FactorResearchRunStore(tmp_path / "runs.db")
    result = _result()
    result["protocol_review"] = {
        "protocol_id": "fproto_" + "a" * 32,
        "version": 1,
        "payload_digest": "b" * 64,
        "passed": False,
        "export_rules": {
            "allow_strategy_export": True,
            "require_all_thresholds": True,
            "require_dataset_consistency": True,
            "minimum_evidence_runs": 1,
        },
    }
    run = store.create(
        owner_user_id=7,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20"},
        result=result,
    )
    monkeypatch.setattr(factor_research, "_run_store", lambda: store)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            factor_research.export_strategy(
                ExportFactorStrategyBody(
                    name="未达标策略",
                    components=[{"factor_id": "momentum_20", "weight": 1}],
                    research_run_ids=[run["run_id"]],
                ),
                {"id": 7},
            )
        )
    assert exc_info.value.status_code == 422
    assert "未通过预注册阈值" in exc_info.value.detail


def test_factor_router_requires_authentication_before_readiness() -> None:
    app = FastAPI()
    app.include_router(factor_research.router)
    with TestClient(app) as client:
        assert client.get("/api/factor-research/readiness").status_code == 401


def test_factor_router_enforces_permission() -> None:
    app = FastAPI()
    app.include_router(factor_research.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": [],
    }
    with TestClient(app) as client:
        response = client.get("/api/factor-research/runs")
    assert response.status_code == 403
    assert response.json()["detail"] == "需要权限: data:read"


def test_factor_run_route_has_stable_pagination_and_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FactorResearchRunStore(tmp_path / "runs.db")
    for factor_id in ("momentum_20", "short_reversal_5", "momentum_20"):
        store.create(
            owner_user_id=7,
            factor_id=factor_id,
            request={"factor_id": factor_id, "primary_horizon": 5},
            result=_result(factor_id),
        )
    monkeypatch.setattr(factor_research, "_run_store", lambda: store)
    app = FastAPI()
    app.include_router(factor_research.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read"],
    }

    with TestClient(app) as client:
        response = client.get(
            "/api/factor-research/runs",
            params={
                "factor_id": "momentum_20",
                "sort": "factor",
                "page": 2,
                "page_size": 1,
            },
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["total"] == 2
    assert payload["page"] == 2
    assert payload["page_size"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["factor_id"] == "momentum_20"


def test_factor_governance_routes_require_high_permission_and_evidence() -> None:
    app = FastAPI()
    app.include_router(factor_research.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read", "strategies:scan"],
    }
    with TestClient(app) as client:
        lifecycle = client.post(
            "/api/factor-research/catalog/momentum_20/versions/1.0.0/deprecate",
            json={
                "definition_digest": "a" * 64,
                "expected_revision": 1,
                "idempotency_key": "factor-deprecate-route",
            },
        )
        no_evidence = client.post(
            "/api/factor-research/export-strategy",
            json={
                "name": "无证据组合",
                "components": [{"factor_id": "momentum_20", "weight": 1}],
                "top_k_pct": 0.1,
                "research_run_ids": [],
            },
        )
    assert lifecycle.status_code == 403
    assert lifecycle.json()["detail"] == "需要权限: admin:users"
    assert no_evidence.status_code == 422


def test_factor_job_route_returns_accepted_and_requires_data_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_isolated_pit_factor_fixture(monkeypatch)
    class Broker:
        async def submit_job(self, *_args, **_kwargs):
            return "factor-job-route-1"

    app = FastAPI()
    app.include_router(factor_research.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read"],
    }
    app.dependency_overrides[get_job_broker] = lambda: Broker()
    with TestClient(app) as client:
        response = client.post(
            "/api/factor-research/jobs",
            json={
                "factor_id": "momentum_20",
                "start": "2024-01-01",
                "end": "2024-02-01",
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "data": {"job_id": "factor-job-route-1", "status": "pending"}
    }


def test_readiness_discovers_only_safe_custom_cache_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    safe = "custom_" + "a" * 16
    (daily / f"{safe}.parquet").touch()
    (daily / "custom_not-a-digest.parquet").touch()
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path))

    candidates = factor_research._candidate_cache_keys()

    assert safe in candidates
    assert "custom_not-a-digest" not in candidates
    assert all(str(tmp_path) not in item for item in candidates)


def test_readiness_exposes_point_in_time_neutralization_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.bdate_range("2024-01-01", periods=5)
    columns = pd.MultiIndex.from_product(
        [["000001"], ["close"]],
    )
    frame = pd.DataFrame(10.0, index=dates, columns=columns)

    class Cache:
        async def get_cache_info(self, cache_key: str):
            assert cache_key == "csi300"
            return {
                "exists": True,
                "schema_version": 4,
                "fields": ["close"],
                "source_trust": "public_cross_validated_research_only",
                "date_start": "2024-01-01",
                "date_end": "2024-01-05",
                "n_dates": 5,
                "n_stocks": 1,
                "source_provenance": {
                    "providers": ["fixture"],
                    "evidence_levels": ["public_cross_validated"],
                },
            }

        async def load_pivot_with_provenance(self, _cache_key: str):
            return frame, {
                "providers": ["fixture"],
                "evidence_levels": ["public_cross_validated"],
            }

    class Store:
        def inspect_research_coverage(self, **kwargs: Any):
            assert kwargs["security_codes"] == ["000001"]
            return {
                "schema_version": "point-in-time-readiness/v1",
                "ready": False,
                "universe": {"ready": True, "reason": None},
                "security_master": {"ready": True, "reason": None},
                "industry": {
                    "ready": False,
                    "neutralization_ready": False,
                    "reason": (
                        "current_snapshot_not_valid_for_historical_research"
                    ),
                },
                "limitations": [
                    "current_snapshot_not_valid_for_historical_research"
                ],
            }

    monkeypatch.setattr(
        factor_research,
        "PointInTimeMasterStore",
        Store,
    )
    capability = asyncio.run(
        factor_research._cache_capability(Cache(), "csi300")  # type: ignore[arg-type]
    )
    assert capability["ready"] is True
    assert capability["ready_for_unbiased_research"] is False
    assert capability["neutralization_ready"] is False
    assert capability["point_in_time"]["industry"]["reason"] == (
        "current_snapshot_not_valid_for_historical_research"
    )
    assert capability["neutralization"]["modes"]["industry"] == {
        "ready": False,
        "reason": "current_snapshot_not_valid_for_historical_research",
    }
    assert capability["neutralization"]["modes"]["size"] == {
        "ready": False,
        "reason": "point_in_time_size_field_missing",
    }


def test_readiness_fails_closed_for_invalid_point_in_time_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.bdate_range("2024-01-01", periods=5)
    frame = pd.DataFrame(
        10.0,
        index=dates,
        columns=pd.MultiIndex.from_product([["000001.SZ"], ["close"]]),
    )

    class Cache:
        async def get_cache_info(self, _cache_key: str):
            return {
                "exists": True,
                "schema_version": 4,
                "fields": ["close"],
                "source_trust": "public_cross_validated_research_only",
                "date_start": "2024-01-01",
                "date_end": "2024-01-05",
            }

        async def load_pivot_with_provenance(self, _cache_key: str):
            return frame, {}

    class Store:
        def inspect_research_coverage(self, **_kwargs: Any):
            raise PointInTimeValidationError("unsafe/private/input")

    monkeypatch.setattr(factor_research, "PointInTimeMasterStore", Store)
    capability = asyncio.run(
        factor_research._cache_capability(Cache(), "csi300")  # type: ignore[arg-type]
    )
    assert capability["ready"] is True
    assert capability["ready_for_unbiased_research"] is False
    assert capability["point_in_time"]["industry"]["reason"] == (
        "point_in_time_identity_invalid"
    )
    assert "unsafe/private/input" not in json.dumps(capability)


def test_analyze_trusted_cache_persists_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allowed_isolated_cpu_executor: object,
) -> None:
    dates = pd.bdate_range("2024-01-01", periods=45)
    codes = [f"{index:06d}" for index in range(20)]
    columns = pd.MultiIndex.from_product([codes, ["close", "amount"]])
    rows = []
    for day in range(len(dates)):
        row: list[float] = []
        for rank in range(1, len(codes) + 1):
            row.extend(
                [
                    float(10 + rank) * (1 + rank / 10_000) ** day,
                    float(rank * 1_000_000 + day),
                ]
            )
        rows.append(row)
    pivot = pd.DataFrame(rows, index=dates, columns=columns)
    provenance = {
        "providers": ["audited-test-provider"],
        "evidence_levels": ["public_aggregator"],
        "adjustments": ["qfq"],
        "content_sha256": "b" * 64,
        "all_batches_raw_cross_validated": True,
        "all_batches_adjusted_factor_validated": True,
    }

    class TrustedCache:
        async def load_pivot_with_provenance(self, cache_key: str):
            assert cache_key.startswith("custom_")
            return pivot, provenance

        @staticmethod
        def _source_trust(value: dict[str, Any]) -> str:
            assert value is provenance
            return "public_cross_validated_research_only"

    store = FactorResearchRunStore(tmp_path / "runs.db")
    market = CachedMarketData(
        frame=pivot,
        source_provenance=provenance,
        report={"ready": True},
    )
    _allow_isolated_pit_factor_fixture(monkeypatch, market=market)
    monkeypatch.setattr(factor_research, "DataCache", TrustedCache)
    monkeypatch.setattr(factor_research, "_run_store", lambda: store)

    response = asyncio.run(
        factor_research.analyze_factor(
            FactorResearchBody(
                    factor_id="price_efficiency_20",
                    pool_preset="custom",
                    pool_custom_codes=codes,
                start="2024-01-29",
                end="2024-02-29",
                horizons=[1, 5],
                primary_horizon=5,
                quantiles=5,
                related_factor_ids=["momentum_20"],
                default_cost_bps=10,
                cost_scenarios_bps=[0, 10, 20],
                rebalance_interval=5,
            ),
            {"id": 7},
        )
    )

    assert isinstance(response, dict), getattr(response, "body", b"")
    run_id = response["data"]["run"]["run_id"]
    stored = store.get(owner_user_id=7, run_id=run_id)
    assert stored is not None
    assert stored["factor_id"] == "price_efficiency_20"
    assert stored["dataset_digest"] == response["data"]["dataset"]["content_sha256"]
    assert response["data"]["schema_version"] == "factor-research/v4"
    assert response["data"]["runtime_code"]["identity"]["sha"]
    assert (
        stored["result"]["runtime_code"]
        == response["data"]["runtime_code"]
    )
    assert response["data"]["implementation"]["capacity"]["status"] in {
        "available",
        "partial",
    }
    assert response["data"]["implementation"]["net_default"]["cost_bps"] == 10
    assert response["data"]["multi_factor"]["correlation"]["pearson"][
        "factors"
    ] == ["momentum_20", "price_efficiency_20"]
    assert len(
        response["data"]["multi_factor"]["orthogonalization"]["input_digest"]
    ) == 64
    assert response["data"]["multi_factor"]["publication"]["status"] == (
        "not_published"
    )
