# 市场状态策略组合（composite_regime_v1）

## 原理

以股票池等权收益构造市场指数，并比较其与 `regime_ma_days` 日均线。牛市提高趋势族权重，弱势状态提高防御族权重；状态判断使用 T-1 及以前数据。

## 参数

- `sub_strategy_ids`：原子子策略列表。
- `regime_ma_days`：默认 200。
- `dominant_weight`：主导策略族总权重，默认 70%。

默认趋势族为双均线、MACD、唐奇安；防御族为 RSI、短期反转和低波动。禁止组合嵌套与未知策略。
