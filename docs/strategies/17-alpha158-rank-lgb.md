# Alpha158 LightGBM 排序学习（alpha158_rank_lgb_v1）

## 原理

复用平台 Alpha158 特征，以未来收益的日截面相关等级作为标签，使用 LightGBM `lambdarank` 按交易日分组训练。平台按月驱动 Walk-Forward 重训练；预测因子整体滞后一日。

## 主要参数

- `n_estimators`、`num_leaves`、`learning_rate`：模型容量。
- `label_horizon_days`：标签周期，默认 21。
- `top_k_pct`：预测后买入比例。
- `retrain_months`、`min_train_months`：平台训练调度。

Windows 环境中模块先加载 LightGBM 原生库，再导入 pandas，以规避 DLL 加载顺序问题。
