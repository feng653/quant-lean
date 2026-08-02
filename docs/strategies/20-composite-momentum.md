# 动量优选策略组合（composite_momentum_v1）

## 原理

估算各子策略最近 `lookback_days` 日纸面收益，以正向年化 Sharpe 为权重，负值截断为零并按月更新。所有策略均无正分时回退等权。

## 参数

- `sub_strategy_ids`：原子子策略列表。
- `lookback_days`：默认 63。

纸面绩效仅用于组合定权，不替代正式回测结果。
