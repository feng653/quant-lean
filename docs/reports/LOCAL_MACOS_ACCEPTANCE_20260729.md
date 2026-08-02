# macOS 本机全量验收与数据链路安全复核（2026-07-29）

## 结论

- Apple Silicon（M2、8 GB）本机环境通过：Python 3.11.15、Node.js
  22.22.0，LightGBM 4.7、XGBoost 3.2、PyTorch 2.13/MPS 自检正常。
- 后端登记的 21/21 个策略均能由 API 创建并完成实验；其中 6/6 个训练型
  策略完成训练并生成带哈希的模型产物。
- Chromium 实际页面验收中，前端成功为 21/21 个策略构造并提交有效请求，
  无页面脚本错误。`/data` 页面侧栏在底部继续滚动时位置保持不变。
- 新增因子研究前后端闭环：因子目录、单/多因子分析、IC/RankIC、分层收益、
  衰减分析，以及将因子组合导出到策略池。导出策略已完成一次端到端实验。
- 巨潮资讯 `008001` 行业分类在沪深 300 缓存池上完成 288/288 映射，覆盖率
  100%，共 91 个行业大类。行业数据读取改为缓存只读，外部刷新要求
  `data:update` 权限。

本报告中的收益和 IC 仅来自确定性合成数据，证据等级为 `declared`，用于验证
软件链路，不代表真实市场盈利能力，也不具备实盘准入资格。

## 环境与数据

| 项目 | 验收值 |
|---|---|
| 机器 | Apple M2，8 GB，macOS |
| Python | 3.11.15 |
| Node.js | 22.22.0 |
| 合成行情区间 | 2015-01-01 至 2024-03-29 |
| 合成股票数 | 30 |
| 数据证据等级 | `declared` / `deterministic_synthetic` |
| 策略提交浏览器 | Chromium 140.0.7339.16 |

公开行情源在本次网络环境中出现 `RemoteDisconnected`，因此没有把合成结果
伪装为公开或验证数据；旧 schema 3 行情缓存也按预期被拒绝。

## 21 个策略端到端结果

| 策略 | 类型 | 训练 | 状态 | 用时（秒） |
|---|---|---:|---|---:|
| liquidity_factor_v1 | factor | 否 | completed | 2.042 |
| low_volatility_v1 | factor | 否 | completed | 0.736 |
| multi_factor_score_v1 | factor | 否 | completed | 0.716 |
| alphamaster_gbr_v1 | factor | 是 | completed | 13.015 |
| momentum_cross_v1 | factor | 否 | completed | 0.754 |
| short_reversal_v1 | factor | 否 | completed | 0.726 |
| composite_regime_v1 | composite | 否 | completed | 1.276 |
| composite_equal_v1 | composite | 否 | completed | 0.710 |
| composite_riskparity_v1 | composite | 否 | completed | 1.228 |
| composite_momentum_v1 | composite | 否 | completed | 1.429 |
| alpha158_xgb_v1 | ml | 是 | completed | 3.312 |
| alpha158_lgb_v1 | ml | 是 | completed | 3.786 |
| lstm_rank_v1 | ml | 是 | completed | 20.110 |
| alpha158_rank_lgb_v1 | ml | 是 | completed | 6.404 |
| transformer_rank_v1 | ml | 是 | completed | 255.616 |
| risk_parity_v1 | portfolio | 否 | completed | 0.853 |
| macd_signal_v1 | technical | 否 | completed | 0.724 |
| bollinger_breakout_v1 | technical | 否 | completed | 0.723 |
| ma_cross_v1 | technical | 否 | completed | 0.725 |
| donchian_breakout_v1 | technical | 否 | completed | 0.844 |
| rsi_reversal_v1 | technical | 否 | completed | 0.725 |

### 训练产物

| 策略 | 样本数 | 特征数 | 验证 RankIC | 产物 |
|---|---:|---:|---:|---|
| alphamaster_gbr_v1 | 65,970 | 13 | — | `experiment_25/model_v1.joblib` |
| alpha158_xgb_v1 | 66,600 | 50 | 0.77949 | `experiment_14/model_v1.joblib` |
| alpha158_lgb_v1 | 66,600 | 50 | 0.77032 | `experiment_15/model_v1.joblib` |
| lstm_rank_v1 | 43,680 | — | 0.77228 | `experiment_16/model_v1.joblib` |
| alpha158_rank_lgb_v1 | 66,570 | 50 | 0.81743 | `experiment_17/model_v1.joblib` |
| transformer_rank_v1 | 42,480 | — | 0.81528 | `experiment_18/model_v1.joblib` |

AlphaMaster 的首次全量运行暴露出旧式自训练策略没有写入完整训练遥测；修复后
以实验 25 复验，训练耗时 12.058 秒并记录 65,970 个样本、13 个特征和模型
产物。

## 前端与因子研究验收

- 实际浏览器打开新建实验页，逐一选择 21 个策略并提交；21 个请求均包含各自
  默认参数、30 只自定义股票和测试区间，`page_errors = []`。
- `/data` 布局复验：滚动前后 `window.scrollY = 0`，侧栏顶部与底部分别固定
  为 0 和 720 px。
- 因子研究对 30 只合成股票完成动量因子分析，2022–2023 年 RankIC 均值
  0.6647；该数值只说明分析链路和统计输出有效。
- 因子组合 `factor_combo_7dc7d3c9c5df` 导出后立即出现在策略池，并完成实验
  26。组合策略采用数据定义和白名单因子注册，不执行动态代码或 `eval`。
- 新增因子只需在因子目录注册一个构建器；分析与导出自动读取同一注册表。

## 行业分类验收

- 来源：AKShare 封装的巨潮资讯行业分类。
- 标准：证监会行业分类标准，分类代码 `008001`，当前行业大类。
- 沪深 300 本地池：288 只代码，映射 288，只读就绪覆盖率 100%。
- 首次刷新约 99.67 秒；刷新现在是显式写操作，读取端点不会隐式击穿外部源。
- 目录及映射缓存均绑定 SHA-256；映射采用临时文件加原子替换写入。
- 当前分类不是历史时点（PIT）分类，历史研究必须保留 `non_point_in_time`
  风险，并禁止直接晋级实盘。

## 本轮安全修复

1. 真实缓存来源身份及调整方式由已验证的 source provenance 提供，不再硬编码。
2. 数据质量报告和复现实验绑定来源 provenance SHA-256。
3. `declared` 数据不会在组合链路中被错误提升为 `verified`。
4. 模型数据库只保存相对 storage key，拒绝目录穿越和存储根目录外路径。
5. 远端训练完成、失败、取消后撤销令牌；并发完成采用唯一临时产物和 CAS。
6. 因子研究输入绑定精确数据窗口、列、上下文和来源哈希，并限制 500 只股票、
   十年区间；CPU 工作移出事件循环。
7. 导出策略定义执行完整 schema、有限数值、ID、版本、所有者及内容哈希校验，
   写入失败可回滚。
8. 行业目录读取与外部刷新分权，缓存带内容哈希并原子落盘。

## 尚未解除的限制

- 巨潮当前行业分类尚非历史时点数据。
- 因子研究当前为同步请求；大规模研究应升级为可恢复任务。
- 导出策略 JSON 适用于本地单进程，生产多进程需要事务数据库。
- 因子研究结果尚未持久化为不可变研究运行，导出也未绑定研究运行 ID。
- 本次公开行情源连接失败，仍需在可用网络中完成可信真实行情全量重建和认证。

## 验收证据

最终代码门禁：

- Ruff：后端、集成测试及 API 对齐测试范围通过。
- 后端单元测试：521 passed。
- 集成测试：10 passed。
- API 对齐测试：67 passed、3 skipped。
- 前端：lint、TypeScript、83 个单元测试及生产构建通过。
- `npm audit --audit-level=high`：0 vulnerabilities。

原始详细结果保存在本机临时目录，不纳入 Git：

- `/tmp/quant-platform-full-acceptance-20260729.json`
- `/tmp/quant-platform-ui-acceptance-20260729.json`
- `/tmp/quant-platform-ui-acceptance-20260729.png`
- `/tmp/quant-platform-sidebar-check.mjs`
- `/tmp/quant-platform-acceptance-runtime-20260729`

临时运行目录包含测试账户与合成实验数据库，已从仓库工作树移出。
