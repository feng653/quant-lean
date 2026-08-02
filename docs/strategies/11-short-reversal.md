# 短期反转因子（short_reversal_v1）

## 原理

以过去 `lookback_days` 日收益的相反数作为因子，每月首个交易日选择截面得分最高的 `top_k_pct` 股票。价格输入整体 `shift(1)`，T 日信号只使用 T-1 及以前数据。

## 参数

- `lookback_days`：回看交易日，默认 21。
- `top_k_pct`：买入截面比例，默认 10%。

无需训练，输出 `BUY` 信号；`score` 和 `weight` 均为 0–1 截面排名。
