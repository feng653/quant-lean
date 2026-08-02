from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.execution import router
from backend.config import Settings
from backend.dependencies import get_current_user
from backend.execution.base import (
    AccountSnapshot,
    ConnectionHealth,
    ConnectionStatus,
    ExecutionAdapter,
    ExecutionCapabilities,
    OrderRequest,
    OrderResult,
    OrderType,
    PositionSnapshot,
)
from backend.execution.ptrade import PTradeExecutionAdapter
from backend.execution.qmt import QmtExecutionAdapter
from backend.execution.registry import ExecutionAdapterRegistry
from backend.services.live_readiness import build_live_readiness_report


def _registry(*adapters: ExecutionAdapter) -> ExecutionAdapterRegistry:
    registry = ExecutionAdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        TRADING_SIM_DB=str(tmp_path / "simulation.db"),
        TRADING_LIVE_DB=str(tmp_path / "live.db"),
    )


class _UnsafeAdapter(ExecutionAdapter):
    adapter_id = "unknown-broker"
    display_name = "Unknown"

    @property
    def capabilities(self) -> ExecutionCapabilities:
        return ExecutionCapabilities(
            supported_order_types={OrderType.MARKET, OrderType.LIMIT},
            supports_account_query=True,
            supports_position_query=True,
            supports_order_validation=True,
            supports_order_cancel=True,
            live_order_submission=True,
        )

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(
            status=ConnectionStatus.HEALTHY,
            ready=True,
            message="self-declared healthy",
        )

    def get_account(self, account_id: str | None = None) -> AccountSnapshot:
        raise AssertionError("readiness must not query an account")

    def get_positions(
        self,
        account_id: str | None = None,
    ) -> list[PositionSnapshot]:
        raise AssertionError("readiness must not query positions")

    def submit_order(self, order: OrderRequest) -> OrderResult:
        raise AssertionError("readiness must never submit an order")


class _UnsafeKnownAdapter(_UnsafeAdapter):
    adapter_id = "qmt"
    display_name = "QMT capability mutation"


def test_report_is_deterministic_and_qmt_ptrade_remain_locked(tmp_path):
    registry = _registry(
        QmtExecutionAdapter(env={}, sdk_probe=lambda _: False),
        PTradeExecutionAdapter(env={}, sdk_probe=lambda _: False),
    )

    first = build_live_readiness_report(
        registry=registry,
        config=_settings(tmp_path),
    )
    second = build_live_readiness_report(
        registry=registry,
        config=_settings(tmp_path),
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.ready is False
    assert first.certification == "not_certified"
    assert first.platform_scope == "research_and_paper_trading_only"
    assert first.blocker_count == len(first.blockers)
    assert first.blocker_count > 0
    assert {adapter.adapter_id for adapter in first.adapters} == {"ptrade", "qmt"}
    assert all(adapter.certified is False for adapter in first.adapters)
    assert all(adapter.health_ready is False for adapter in first.adapters)
    assert all(
        adapter.declared_capabilities["live_order_submission"] is False
        for adapter in first.adapters
    )


def test_unknown_adapter_cannot_bypass_gate(tmp_path):
    report = build_live_readiness_report(
        registry=_registry(_UnsafeAdapter()),
        config=_settings(tmp_path),
    )

    adapter = report.adapters[0]
    assert report.ready is False
    assert report.certification == "not_certified"
    assert adapter.recognized_scaffold is False
    assert adapter.fail_closed is False
    assert "unknown_adapter" in adapter.blockers
    assert "uncertified_live_submission_declared" in adapter.blockers
    assert any(
        blocker.capability_id == "live_order_submission"
        for blocker in report.blockers
    )


def test_known_adapter_capability_change_cannot_bypass_gate(tmp_path):
    report = build_live_readiness_report(
        registry=_registry(_UnsafeKnownAdapter()),
        config=_settings(tmp_path),
    )

    adapter = report.adapters[0]
    assert report.ready is False
    assert report.certification == "not_certified"
    assert adapter.recognized_scaffold is True
    assert adapter.health_ready is True
    assert adapter.declared_capabilities["live_order_submission"] is True
    assert "unknown_adapter" not in adapter.blockers
    assert "adapter_not_certified" in adapter.blockers
    assert "health_declaration_not_live_certification" in adapter.blockers
    assert "uncertified_live_submission_declared" in adapter.blockers


def test_empty_registry_and_shared_database_fail_closed(tmp_path):
    config = Settings(
        _env_file=None,
        TRADING_SIM_DB=str(tmp_path / "shared.db"),
        TRADING_LIVE_DB=str(tmp_path / "shared.db"),
    )

    report = build_live_readiness_report(
        registry=ExecutionAdapterRegistry(),
        config=config,
    )

    assert report.ready is False
    assert report.adapters == []
    separate_ledger = next(
        capability
        for domain in report.domains
        for capability in domain.capabilities
        if capability.capability_id == "separate_live_ledger"
    )
    assert separate_ledger.status.value == "missing"


def _client(user: dict[str, Any]) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_live_readiness_requires_trading_read_permission():
    response = _client(
        {
            "id": 9,
            "is_admin": False,
            "permissions": [],
        }
    ).get("/api/execution/live-readiness")

    assert response.status_code == 403
    assert response.json()["detail"] == "需要权限: trading:read"


def test_live_readiness_is_global_read_only_evidence(monkeypatch, tmp_path):
    registry = _registry(QmtExecutionAdapter(env={}, sdk_probe=lambda _: False))
    monkeypatch.setattr(
        "backend.api.execution.get_execution_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "backend.services.live_readiness.get_execution_registry",
        lambda: registry,
    )

    response = _client(
        {
            "id": 17,
            "is_admin": False,
            "permissions": ["trading:read"],
        }
    ).get("/api/execution/live-readiness")

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["ready"] is False
    assert report["certification"] == "not_certified"
    assert "user_id" not in report
    assert "account_id" not in str(report)


def test_execution_router_exposes_no_live_order_mutation_route():
    app = FastAPI()
    app.include_router(router)
    route_methods = {
        (path, method.upper())
        for path, operation in app.openapi()["paths"].items()
        for method in operation
        if path.startswith("/api/execution")
    }

    assert ("/api/execution/live-readiness", "GET") in route_methods
    assert ("/api/execution/orders/validate", "POST") in route_methods
    mutations = {
        (path, method)
        for path, method in route_methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
        and path != "/api/execution/orders/validate"
    }
    assert mutations == set()
    assert not any(
        token in path.casefold()
        for path, _ in route_methods
        for token in ("/submit", "/send", "/cancel", "/fill", "/live-order")
    )
