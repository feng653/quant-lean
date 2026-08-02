"""PTrade adapter scaffold with delayed optional SDK detection."""

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


class PTradeExecutionAdapter(OptionalSdkAdapter):
    adapter_id = "ptrade"
    display_name = "PTrade"
    sdk_module = "ptrade"
    required_config = ("PTRADE_ACCOUNT_ID", "PTRADE_API_URL")

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

