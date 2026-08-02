# 流动性因子（liquidity_factor_v1）

## 原理

支持 Amihud 非流动性指标和滚动成交额两种口径。Amihud 口径对非流动性取负，成交额口径取滚动均值的对数；随后进行月度截面排名。所有输入滞后一日。

## 参数

- `lookback_days`：默认 21。
- `method`：`amihud` 或 `amount`。
- `top_k_pct`：默认 10%。

若无 `amount` 字段，策略以 `volume × close` 估算成交额。
