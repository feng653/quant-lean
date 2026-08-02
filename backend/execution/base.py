"""Broker-neutral execution contracts and fail-closed adapter interface."""

from __future__ import annotations

import importlib.util
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    REJECTED = "rejected"
    PENDING = "pending"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"


class ConnectionStatus(str, Enum):
    UNAVAILABLE = "unavailable"
    CONFIGURED = "configured"
    HEALTHY = "healthy"
    DEGRADED = "degraded"


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    account_id: str
    currency: str = "CNY"
    total_assets: float
    cash: float
    market_value: float
    frozen_cash: float = 0
    as_of: datetime


class PositionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    account_id: str
    symbol: str
    quantity: int
    available_quantity: int
    average_cost: float | None = None
    market_price: float | None = None
    market_value: float | None = None
    as_of: datetime


class OrderRequest(BaseModel):
    """Normalized order intent.

    Business constraints intentionally live in ``validate_order`` so the
    validation endpoint can return all actionable errors in one response.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: float | None = None
    account_id: str | None = None
    client_order_id: str | None = Field(default=None, max_length=64)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class OrderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    accepted: bool
    status: OrderStatus
    client_order_id: str | None = None
    broker_order_id: str | None = None
    message: str
    submitted_at: datetime | None = None
    raw_status: str | None = None


class ExecutionCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported_order_types: set[OrderType]
    supports_account_query: bool
    supports_position_query: bool
    supports_order_validation: bool = True
    supports_order_cancel: bool = False
    live_order_submission: bool = False


class ConnectionHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ConnectionStatus
    ready: bool
    message: str
    checked_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    details: dict[str, Any] = Field(default_factory=dict)


class OrderValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    valid: bool
    capability_supported: bool
    adapter_ready: bool
    submission_enabled: bool
    can_submit: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    health: ConnectionHealth


class AdapterUnavailableError(RuntimeError):
    """Raised when an adapter cannot safely perform a broker operation."""


SdkProbe = Callable[[str], bool]


def detect_optional_module(module_name: str) -> bool:
    """Detect an optional SDK without importing it or running SDK side effects."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class ExecutionAdapter(ABC):
    """Broker adapter contract.

    Concrete adapters must remain fail-closed until both connectivity and live
    submission are deliberately implemented and enabled.
    """

    adapter_id: str
    display_name: str

    @property
    @abstractmethod
    def capabilities(self) -> ExecutionCapabilities:
        """Return explicitly supported adapter operations."""

    @abstractmethod
    def health(self) -> ConnectionHealth:
        """Return a side-effect-free readiness snapshot."""

    def validate_order(self, order: OrderRequest) -> list[str]:
        errors: list[str] = []
        if not re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", order.symbol):
            errors.append("证券代码必须为 6 位数字，可选 .SH/.SZ/.BJ 后缀")
        if order.quantity <= 0:
            errors.append("委托数量必须大于 0")
        if order.order_type not in self.capabilities.supported_order_types:
            errors.append(f"适配器不支持 {order.order_type.value} 委托")
        if order.order_type is OrderType.LIMIT:
            if order.limit_price is None or order.limit_price <= 0:
                errors.append("限价委托必须提供大于 0 的限价")
        elif order.limit_price is not None:
            errors.append("市价委托不能提供限价")
        errors.extend(self._validate_adapter_order(order))
        return errors

    def _validate_adapter_order(self, order: OrderRequest) -> list[str]:
        return []

    @abstractmethod
    def get_account(self, account_id: str | None = None) -> AccountSnapshot:
        """Fetch a normalized broker account snapshot."""

    @abstractmethod
    def get_positions(
        self,
        account_id: str | None = None,
    ) -> list[PositionSnapshot]:
        """Fetch normalized positions."""

    @abstractmethod
    def submit_order(self, order: OrderRequest) -> OrderResult:
        """Submit an order, or fail explicitly when unavailable."""


class OptionalSdkAdapter(ExecutionAdapter, ABC):
    """Common readiness logic for locally installed, optional broker SDKs."""

    sdk_module: str
    required_config: tuple[str, ...]

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        sdk_probe: SdkProbe | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._sdk_probe = sdk_probe or detect_optional_module

    def health(self) -> ConnectionHealth:
        missing_config = [
            name for name in self.required_config if not self._env.get(name, "").strip()
        ]
        sdk_available = self._sdk_probe(self.sdk_module)
        details = {
            "sdk_module": self.sdk_module,
            "sdk_available": sdk_available,
            "missing_config": missing_config,
            "live_order_submission_enabled": self.capabilities.live_order_submission,
        }
        if missing_config or not sdk_available:
            reasons: list[str] = []
            if missing_config:
                reasons.append(f"缺少配置: {', '.join(missing_config)}")
            if not sdk_available:
                reasons.append(f"未安装可选 SDK: {self.sdk_module}")
            return ConnectionHealth(
                status=ConnectionStatus.UNAVAILABLE,
                ready=False,
                message="；".join(reasons),
                details=details,
            )

        return ConnectionHealth(
            status=ConnectionStatus.CONFIGURED,
            ready=False,
            message="SDK 与配置已检测到，但实盘连接和下单尚未启用",
            details=details,
        )

    def _configured_account_id(self) -> str | None:
        account_key = next(
            (name for name in self.required_config if name.endswith("ACCOUNT_ID")),
            None,
        )
        return self._env.get(account_key) if account_key else None

    def _validate_adapter_order(self, order: OrderRequest) -> list[str]:
        configured_account = self._configured_account_id()
        if (
            configured_account
            and order.account_id
            and order.account_id != configured_account
        ):
            return ["订单账户与适配器配置账户不一致"]
        return []

    def _raise_unavailable(self, operation: str) -> NoReturn:
        health = self.health()
        raise AdapterUnavailableError(
            f"{self.display_name} {operation}不可用: {health.message}"
        )
