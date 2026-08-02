"""Execution adapter registry."""

from __future__ import annotations

from backend.execution.base import ExecutionAdapter
from backend.execution.ptrade import PTradeExecutionAdapter
from backend.execution.qmt import QmtExecutionAdapter


class ExecutionAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ExecutionAdapter] = {}

    def register(self, adapter: ExecutionAdapter) -> None:
        if adapter.adapter_id in self._adapters:
            raise ValueError(f"执行适配器已注册: {adapter.adapter_id}")
        self._adapters[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> ExecutionAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as error:
            raise KeyError(f"执行适配器不存在: {adapter_id}") from error

    def list(self) -> list[ExecutionAdapter]:
        return [self._adapters[key] for key in sorted(self._adapters)]


_default_registry: ExecutionAdapterRegistry | None = None


def get_execution_registry() -> ExecutionAdapterRegistry:
    global _default_registry
    if _default_registry is None:
        registry = ExecutionAdapterRegistry()
        registry.register(QmtExecutionAdapter())
        registry.register(PTradeExecutionAdapter())
        _default_registry = registry
    return _default_registry

