"""Broker execution abstractions and disabled-by-default adapter scaffolds."""

from backend.execution.base import (
    AccountSnapshot,
    AdapterUnavailableError,
    ConnectionHealth,
    ConnectionStatus,
    ExecutionAdapter,
    ExecutionCapabilities,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidationResult,
    PositionSnapshot,
)
from backend.execution.ptrade import PTradeExecutionAdapter
from backend.execution.qmt import QmtExecutionAdapter
from backend.execution.registry import (
    ExecutionAdapterRegistry,
    get_execution_registry,
)

__all__ = [
    "AccountSnapshot",
    "AdapterUnavailableError",
    "ConnectionHealth",
    "ConnectionStatus",
    "ExecutionAdapter",
    "ExecutionAdapterRegistry",
    "ExecutionCapabilities",
    "OrderRequest",
    "OrderResult",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "OrderValidationResult",
    "PTradeExecutionAdapter",
    "PositionSnapshot",
    "QmtExecutionAdapter",
    "get_execution_registry",
]

