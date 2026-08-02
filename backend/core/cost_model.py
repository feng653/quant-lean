"""A股交易成本模型 —— 佣金、滑点、印花税."""

from __future__ import annotations



class CostModel:
    """A股交易成本计算器。

    默认参数（万三佣金 + 千一滑点 + 千一卖方印花税）:
        commission_rate=0.0003   # 万三
        slippage_rate=0.001      # 千一
        stamp_duty_rate=0.001    # 千一（仅卖出）

    Attributes:
        commission_rate: 佣金费率（双边）。
        slippage_rate: 滑点比例（双边）。
        stamp_duty_rate: 印花税率（仅卖出）。
        min_commission: 最低佣金（元），默认 5.0。
    """

    def __init__(
        self,
        commission_rate: float = 0.0003,
        slippage_rate: float = 0.001,
        stamp_duty_rate: float = 0.001,
        min_commission: float = 5.0,
    ) -> None:
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.min_commission = min_commission

    def calc_buy_cost(self, price: float, shares: int) -> float:
        """计算买入总成本 = 成交金额 + 佣金 + 滑点成本。

        Args:
            price: 买入单价。
            shares: 买入股数。

        Returns:
            买入实际需要支出的总金额。
        """
        trade_amount = price * shares
        commission = max(trade_amount * self.commission_rate, self.min_commission)
        slippage = trade_amount * self.slippage_rate
        return trade_amount + commission + slippage

    def calc_sell_cost(self, price: float, shares: int) -> float:
        """计算卖出净收入 = 成交金额 - 佣金 - 印花税 - 滑点成本。

        Args:
            price: 卖出单价。
            shares: 卖出股数。

        Returns:
            卖出实际到账金额。
        """
        trade_amount = price * shares
        commission = max(trade_amount * self.commission_rate, self.min_commission)
        stamp_duty = trade_amount * self.stamp_duty_rate
        slippage = trade_amount * self.slippage_rate
        return trade_amount - commission - stamp_duty - slippage

    def calc_shares(self, capital: float, price: float) -> int:
        """根据可用资金和单价计算可买入股数（按100股整手向下取整）。

        Args:
            capital: 可用资金。
            price: 买入单价。

        Returns:
            可买入股数（100的整数倍），最小为0。
        """
        if price <= 0 or capital <= 0:
            return 0
        # FIXED: reviewer issue #12 — 用数学公式替代 while 循环
        # 买入总费率 = 佣金率 + 滑点率 (印花税仅卖出)
        all_rates = self.commission_rate + self.slippage_rate
        # shares = floor(capital / (price * (1 + all_rates)) / 100) * 100
        raw_shares = capital / (price * (1 + all_rates))
        # 预留最低佣金的影响（如果按公式算出的佣金低于最低佣金）
        min_commission_check = price * (raw_shares // 100 * 100) * self.commission_rate
        if min_commission_check > 0 and min_commission_check < self.min_commission:
            # 需要额外扣除最低佣金差额
            effective_capital = capital - (self.min_commission - min_commission_check)
            raw_shares = effective_capital / (price * (1 + self.slippage_rate))
        shares = int(raw_shares // 100 * 100)
        return max(shares, 0)
