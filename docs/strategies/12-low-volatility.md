# 低波动因子（low_volatility_v1）

## 原理

计算历史收益标准差或下行标准差，取负后做截面排名，每月首个交易日选择低波动股票。滚动波动率整体滞后一日，避免前视。

## 参数

- `lookback_days`：默认 120。
- `vol_method`：`standard` 或 `downside`。
- `top_k_pct`：默认 10%。

无需训练；分数越高表示历史波动越低。
