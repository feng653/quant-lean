"""Guojin QMT adapter scaffold.

No QMT SDK module is imported at module import time, and all broker operations
remain disabled until a real implementation is explicitly introduced.
"""

from __future__ import annotations

from backend.execution.base import (
    AccountSnapshot,
    ExecutionCapabilities,
    OptionalSdkAdapter,
    OrderRequest,
    OrderResult,
    OrderType,
    PositionSnapshot,
)


class QmtExecutionAdapter(OptionalSdkAdapter):
    adapter_id = "qmt"
    display_name = "国金证券 QMT"
    sdk_module = "xtquant"
    required_config = ("QMT_ACCOUNT_ID", "QMT_USERDATA_PATH")

    @property
    def capabilities(self) -> ExecutionCapabilities:
        return ExecutionCapabilities(
            supported_order_types={OrderType.MARKET, OrderType.LIMIT},
            supports_account_query=True,
            supports_position_query=True,
            supports_order_cancel=False,
            live_order_submission=False,
        )

    def get_account(self, account_id: str | None = None) -> AccountSnapshot:
        self._raise_unavailable("账户查询")

    def get_positions(
        self,
        account_id: str | None = None,
    ) -> list[PositionSnapshot]:
        self._raise_unavailable("持仓查询")

    def submit_order(self, order: OrderRequest) -> OrderResult:
        self._raise_unavailable("下单")

