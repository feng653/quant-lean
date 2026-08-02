import pytest
from fastapi import HTTPException

from backend.api.execution import ValidateOrderBody, validate_order
from backend.execution.base import (
    AdapterUnavailableError,
    ConnectionStatus,
    OrderRequest,
    OrderSide,
    OrderType,
)
from backend.execution.ptrade import PTradeExecutionAdapter
from backend.execution.qmt import QmtExecutionAdapter
from backend.execution.registry import ExecutionAdapterRegistry


def _limit_order(**overrides) -> OrderRequest:
    values = {
        "symbol": "600000.SH",
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": 100,
        "limit_price": 10.5,
    }
    values.update(overrides)
    return OrderRequest(**values)


@pytest.mark.parametrize(
    ("adapter", "expected_config"),
    [
        (
            QmtExecutionAdapter(env={}, sdk_probe=lambda _: False),
            {"QMT_ACCOUNT_ID", "QMT_USERDATA_PATH"},
        ),
        (
            PTradeExecutionAdapter(env={}, sdk_probe=lambda _: False),
            {"PTRADE_ACCOUNT_ID", "PTRADE_API_URL"},
        ),
    ],
)
def test_missing_sdk_and_config_are_explicitly_unavailable(
    adapter,
    expected_config,
):
    health = adapter.health()

    assert health.status is ConnectionStatus.UNAVAILABLE
    assert health.ready is False
    assert health.details["sdk_available"] is False
    assert set(health.details["missing_config"]) == expected_config
    assert "未安装可选 SDK" in health.message


def test_detected_sdk_and_config_still_do_not_enable_live_submission():
    adapter = QmtExecutionAdapter(
        env={
            "QMT_ACCOUNT_ID": "account-1",
            "QMT_USERDATA_PATH": "C:/qmt/userdata",
        },
        sdk_probe=lambda module: module == "xtquant",
    )

    health = adapter.health()

    assert health.status is ConnectionStatus.CONFIGURED
    assert health.ready is False
    assert adapter.capabilities.live_order_submission is False
    with pytest.raises(AdapterUnavailableError, match="下单不可用"):
        adapter.submit_order(_limit_order(account_id="account-1"))


def test_local_order_validation_reports_all_actionable_errors():
    adapter = QmtExecutionAdapter(env={}, sdk_probe=lambda _: False)
    order = _limit_order(symbol="BAD", quantity=0, limit_price=None)

    errors = adapter.validate_order(order)

    assert any("证券代码" in error for error in errors)
    assert any("委托数量" in error for error in errors)
    assert any("限价" in error for error in errors)


def test_registry_rejects_duplicate_ids_and_lists_deterministically():
    registry = ExecutionAdapterRegistry()
    ptrade = PTradeExecutionAdapter(env={}, sdk_probe=lambda _: False)
    qmt = QmtExecutionAdapter(env={}, sdk_probe=lambda _: False)
    registry.register(qmt)
    registry.register(ptrade)

    assert [adapter.adapter_id for adapter in registry.list()] == ["ptrade", "qmt"]
    assert registry.get("qmt") is qmt
    with pytest.raises(ValueError, match="已注册"):
        registry.register(qmt)


def test_validation_endpoint_is_fail_closed(monkeypatch):
    registry = ExecutionAdapterRegistry()
    registry.register(QmtExecutionAdapter(env={}, sdk_probe=lambda _: False))
    monkeypatch.setattr(
        "backend.api.execution.get_execution_registry",
        lambda: registry,
    )
    body = ValidateOrderBody(adapter_id="qmt", order=_limit_order())

    response = validate_order(body, user={"id": 1})

    assert response["data"]["valid"] is True
    assert response["data"]["capability_supported"] is True
    assert response["data"]["adapter_ready"] is False
    assert response["data"]["submission_enabled"] is False
    assert response["data"]["can_submit"] is False


def test_unknown_adapter_returns_404(monkeypatch):
    monkeypatch.setattr(
        "backend.api.execution.get_execution_registry",
        ExecutionAdapterRegistry,
    )

    with pytest.raises(HTTPException) as error:
        validate_order(
            ValidateOrderBody(adapter_id="missing", order=_limit_order()),
            user={"id": 1},
        )

    assert error.value.status_code == 404


def test_query_methods_are_also_fail_closed():
    adapter = PTradeExecutionAdapter(env={}, sdk_probe=lambda _: False)

    with pytest.raises(AdapterUnavailableError, match="账户查询不可用"):
        adapter.get_account()
    with pytest.raises(AdapterUnavailableError, match="持仓查询不可用"):
        adapter.get_positions()
