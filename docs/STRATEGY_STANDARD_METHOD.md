# 策略标准化编写与研究方法

> 适用范围：本仓库的日频研究、参数扫描、锁定测试和模拟盘链路
>
> 基准实现：`StrategyProtocol`、`TrainableStrategy`、平台 Walk-Forward、
> `RunManifest`、不可变研究快照和研究晋级工作流
>
> 安全边界：当前平台只用于研究与模拟盘；在实盘认证完成前，不得据此接入真实账户

## 1. 目标与强制级别

一份“能运行”的策略代码并不等于一个可信研究。标准策略必须同时满足：

1. **接口一致**：注册中心、实验 API、回测、模拟盘均通过同一策略协议调用。
2. **时间正确**：信号、成交、估值和训练标签的可见时间明确，无前视泄漏。
3. **数据可审计**：行情、股票池、行业、基准和公司行动有来源、版本与完整性摘要。
4. **研究可复现**：参数、代码、环境、数据、窗口、成本和产物绑定不可变证据。
5. **统计诚实**：完整记录尝试次数，selection 与 locked test 分离，披露过拟合风险。
6. **执行可落地**：计入 A 股成本、整手、容量、停牌和价格限制，不能只报告毛收益。
7. **链路可验收**：实验必须能由真实前端发起、由后台任务完成、落库并在前端查看。
8. **失败可解释**：瞬态故障可以有界恢复，完整性错误和策略错误必须失败关闭。

本文使用以下强制级别：

- **必须**：违反即不得合并、不得把结果用于研究晋级。
- **应当**：除非在设计文档中给出可审计理由，否则视为必须。
- **可以**：按策略需要选择。

## 2. 从假设到模拟观察的标准流程

```text
预注册假设和预算
  → 选择可信数据、点时股票池和基准
  → 实现 StrategyProtocol / TrainableStrategy
  → 契约、无前视和确定性测试
  → selection 参数扫描
  → 人工选择唯一候选
  → 创建全新 locked-test 实验
  → 稳健性、成本和容量审查
  → 生成不可变实验 manifest 与数据风险快照
  → 用户确认风险后发布不可变模拟版本

可选高级路径：selection → 人工唯一候选 → 全新 locked test → 不可变 Report →
promotion draft/reviewed/approved。它用于提高研究可信度，不是个人模拟盘的必填审批；
缺失、rejected 或 revoked 在 paper 中保留警告，未来实盘仍硬阻断。
```

禁止从 locked test 结果反向修改参数后仍称其为“锁定测试”。一旦研究人员看过锁定
窗口的结果，该窗口即已消耗；后续修改必须建立新假设和新的未来锁定窗口。

## 3. 代码放置与策略类型

策略文件放在 `backend/strategies/` 的对应子目录。注册中心会递归扫描，不需要手工
维护注册清单。

| 策略类别 | 目录 | 基类 | 典型职责 |
|---|---|---|---|
| 技术规则 | `technical/` | `StrategyProtocol` | 时间序列信号、事件订单 |
| 非训练因子 | `factor/` | `StrategyProtocol` | 横截面打分、目标权重 |
| 机器学习/训练因子 | `ml/` 或 `factor/` | `TrainableStrategy` | 平台驱动训练、验证和预测 |
| 组合配置 | `portfolio/` | `StrategyProtocol` | 风险预算、目标权重 |
| 多策略复合 | `composite/` | `CompositeStrategy` | 调用并融合已注册子策略 |

文件名应使用小写下划线，辅助模块必须以下划线开头，例如 `_common.py`，避免被当作
策略模块扫描。`strategy_id` 使用稳定的小写 ID，例如 `quality_value_v1`；不允许复用
旧 ID 表示不兼容的新逻辑。算法、数据依赖或信号语义发生不兼容变化时，增加策略 ID
主版本并保留旧实验的可解释性。

### 3.1 规则型策略的最小接口

规则型策略必须：

- 继承 `StrategyProtocol`；
- 实现类方法 `metadata()`；
- 实现 `generate_batch_signals()`；
- 对参数间关系实现 `validate_params()`；
- 不在实例间共享可变运行状态；
- 不访问网络、数据库、环境密钥或仓库外文件。

注册中心在每次执行时使用 `create_strategy()` 创建隔离实例。策略不得依赖注册阶段
单例中的状态，也不得通过模块全局变量保存实验结果。

### 3.2 训练型策略的最小接口

训练型策略必须继承 `TrainableStrategy`，并只实现平台钩子：

- `prepare(pivot, params)`：一次性构造特征，结果可缓存于当前实例；
- `fit(pivot, params, train_start, train_end)` 或
  `fit_with_validation(...)`：仅在显式窗口内拟合；
- `predict_scores(model, pivot, params, as_of_date)`：只读取不晚于预测时点的数据；
- 必要时覆写 `label_horizon_days()` 和 `select_signals()`。

月度调度、expanding/rolling 窗口、purge、embargo、验证集、进度、取消和连续失败
处理归 `backend/services/walkforward.py` 所有。**禁止**在
`generate_batch_signals()` 内再写一套私有 walk-forward 循环。这样会绕过平台的
时间边界、取消检查、训练遥测和模型证据。

`retrain_frequency=NEVER` 表示固定窗口只训练一次，并不表示不再按月预测。
周期训练使用 `MONTHLY` 等明确频率。训练型策略必须声明
`portfolio_signal_mode=TARGET_WEIGHTS`。

Windows 上实现 LightGBM 策略时，必须沿用现有原生库加载顺序：在 pandas/pyarrow
之前预加载 LightGBM；macOS 必须先具备 OpenMP 运行时。不得为了让注册扫描成功而
吞掉原生库错误并返回虚假空信号。

### 3.3 组合策略

组合策略通过 `sub_strategies` 声明子策略和角色，通过注册中心创建隔离子实例。
必须拒绝：

- 未注册的子策略；
- 重复子策略；
- 自引用；
- 未经设计和验证的嵌套组合；
- 权重不有限、总和不合法或无法满足上下限；
- 把子策略历史测试收益偷看后当期定权。

组合策略的参数预算和每个子策略的参数预算都必须记入研究尝试总数。

## 4. 元数据、参数与数据契约

### 4.1 `StrategyMetadata`

`metadata()` 至少要准确填写：

- `strategy_id`、`display_name`、语义化 `version`；
- `category` 和 `description`；
- `supported_modes`；
- `requires_training` 和 `retrain_frequency`；
- `portfolio_signal_mode`；
- `params`；
- `max_position_pct` 和支持的仓位模式；
- 便于检索但不夸大收益的 `tags`。

`description` 必须说明信号原理、调仓频率、适用范围和主要失效情形。不得使用
“稳定盈利”“低风险高收益”等未经锁定测试支持的表述。

### 4.2 `ParamField`

每个参数必须有唯一名称、精确类型、默认值、中文解释和合理边界。选择项使用
`choices`；数值项提供 `min/max/step`。布尔值不得被当作整数接受，NaN 和 Infinity
不得进入参数、哈希、指标或报告。

策略自己的参数与平台执行参数分离。`_execution` 由
`PlatformExecutionConfig` 管理，策略不得自行解析或覆盖：

- `initial_capital`
- `max_positions`
- `lot_size`
- `volume_participation`
- `commission_rate`
- `slippage_rate`
- `stamp_duty_rate`
- `min_commission`

`validate_params()` 除单字段范围外，还必须检查参数间不变量，例如快线短于慢线、
skip 短于 lookback、rolling 窗口不短于最小训练期。未知参数必须拒绝，不能静默
忽略，以免前端拼写错误产生“成功但未生效”的实验。

### 4.3 行情数据契约

运行输入是：

- `pd.DatetimeIndex`，唯一、递增；
- 列为 `(code, field)` 的 `pd.MultiIndex`；
- 字段使用小写规范名：`open/high/low/close/volume/amount`；
- 股票代码使用平台规范形式，不在策略内猜测或替换股票池 ID。

新增策略时必须同步更新 `backend/research/validation_matrix.py` 中的
`StrategyDataContract`，声明：

- `required_fields`；
- 可替代字段组 `alternative_fields`；
- `recommended_fields`；
- `min_history_rows`；
- `min_codes`；
- 相应 `validation_mode`。

例如流动性策略应要求 `close`，并要求 `amount` 或 `volume` 至少有一个。不能在
成交额缺失时无提示地把无量价依据的结果当成同等证据。warm-up 数据可以早于测试
起点，但输出信号必须被限制在请求范围内；数据不足应返回明确失败或空证据说明，
不得用未来行补足。

## 5. 无前视与 T/T+1 时间语义

平台的批量日频语义是：

```text
T 日收盘后：策略读取截至 T 的已知数据，生成 signal_date=T
T+1 交易日开盘：使用 T+1 open 撮合
T+1 收盘：使用 T+1 close 估值
```

因此：

- T 日信号绝不能读取 T+1 或更晚的数据；
- T+1 开盘价只用于成交，不能进入 T 日信号；
- T+1 缺少有效开盘价时拒单，禁止回退到收盘价；
- “下一交易日”必须来自交易日历，不能简单加一个自然日；
- 训练标签必须完整落在训练或验证窗口内，不能越过预测边界；
- 复权因子、行业、成分股和财务数据必须按当时可获得时间使用。

策略可以选择更保守的 T-1 因子口径，此时应在原始因子上明确 `shift(1)`，并在设计
文档说明。不得一边使用 T 日收盘确认事件，一边声称信号只使用 T-1；信号定义、
代码和无前视测试必须一致。

### 5.1 无前视测试

每个策略至少应有两类测试：

1. **未来突变测试**：保留截至 T 的输入不变，只修改 T+1 之后数据；截至 T 的信号
   必须完全相同。
2. **截断等价测试**：用完整 pivot 与 `.loc[:T]` 截断 pivot 运行；截至 T 的规范化
   信号必须相同。

训练策略还要测试：

- 标签 horizon、purge 和 embargo 后的最后训练样本；
- validation 标签不越过 `validation_end`；
- 预测日截断后分数不变；
- 同一随机种子和依赖版本产生相同候选、窗口与产物摘要；
- 连续三次真实 fit 失败保留根因并失败，不退化为空信号。

## 6. 点时股票池、行业与证券状态

历史实验必须使用 point-in-time（PIT）股票池。一个合格的
`UniverseSnapshot` 至少绑定：

- 请求的 as-of 日期；
- 来源实际 as-of 日期；
- 精确、排序、去重的代码集合；
- `snapshot_hash`；
- 成分、行业和数据覆盖质量；
- `point_in_time=true`；
- 可见的风险警告。

当前成分股或当前巨潮 `008001` 行业映射只能用于软件链路检查和现状筛选，不能证明
历史点时正确。若 `point_in_time=false`、存在 `survivorship_bias` 或行业覆盖不足，
研究报告必须展示风险，晋级必须失败关闭。

点时证券主数据应覆盖：

- 指数历史纳入和剔除；
- 行业的生效起止日期；
- 上市、退市和退市整理期；
- ST/*ST；
- 停复牌；
- 主板、创业板、科创板和北交所的逐日涨跌停规则；
- 一字板与无法成交状态；
- 分红、拆并股、送转、配股及复权因子。

行业筛选必须先以最终股票代码集合做 readiness 检查。用户已选择行业时，缓存缺失、
过期、哈希无效或覆盖不足必须阻止提交；用户明确清空行业时才代表“不筛行业”。
读取接口不得隐式访问外部数据源，刷新必须是有 `data:update` 权限的显式写操作。

## 7. 数据策略、快照与 RunManifest

### 7.1 `cache_only`

正式参数比较、锁定测试和精确重跑应使用 `data_access_policy=cache_only`。它要求：

- 只读取本地 schema-v4 OHLCV provenance cache；
- 精确覆盖请求股票和时间范围；
- 基准也来自本地可信缓存；
- 不访问网络，不用公共数据或合成数据补洞；
- 缺失时明确失败。

`allow_fetch` 适合显式数据准备，不适合把多次实验建立在运行时可能变化的远端响应
上。合成数据必须标记 `deterministic_synthetic/declared`，只可验收软件链路，不能
对外表述为市场表现。

### 7.2 不可变快照

研究 pivot 和 benchmark 通过 `ResearchSnapshotStore` 保存为内容寻址 Parquet：

- schema 为 `research-data-snapshot/v1`；
- key、文件名和 `file_sha256` 一致；
- 保存大小、相对 key、kind、格式和逻辑 schema；
- 禁止符号链接和越过存储根目录；
- 写后立即回读校验。

任何大小、哈希、schema、轴标签、dtype 或 kind 不一致都必须抛出
`SnapshotIntegrityError`。禁止在捕获后重建同一个实验的“等价”文件继续执行；
应保留失败证据，修复序列化契约，并用新实验或受控恢复副本重跑。

### 7.3 `RunManifest`

每个研究运行的不可变 `RunManifest` 应绑定：

- experiment ID、strategy ID、参数及参数哈希；
- Git SHA、dirty 状态、策略源码哈希；
- Python、平台、关键依赖版本和设备；
- 数据版本、provenance 和数据快照；
- 股票池/行业快照；
- 训练、验证、测试和锁定窗口；
- benchmark 快照；
- 执行成本、容量和约束；
- 数据质量审计与研究风险警告。

清单只允许有限、规范 JSON。不得保存密码、token、API key、Authorization、绝对本机
路径或包含这些含义的键。清单已存在且内容不同是不可变性冲突，不得执行
`UPDATE` 覆盖；如果失败实验已经拥有清单，恢复应创建新的实验副本，让旧实验和旧
清单继续可审计。

## 8. 研究窗口和参数选择

### 8.1 窗口职责

| 窗口 | 用途 | 是否可用于调参 |
|---|---|---:|
| warm-up | 构造指标和特征，不计业绩 | 否 |
| train | 训练模型 | 是，仅限预注册训练过程 |
| validation | 早停、阈值和模型选择 | 是 |
| selection | 比较规则参数或模型配置 | 是 |
| locked test | 最终一次独立评估 | 否 |
| shadow/paper | 上线前运行和运维验证 | 否 |

训练、validation、selection 和 locked test 不得重叠。带未来标签的模型必须使用
purge；预测前必须使用配置的 embargo。Walk-Forward 应优先模拟真实研究时点：
expanding 用全部当时可用历史，rolling 只用预注册长度的最近窗口。

### 8.2 日频 A 股建议范围

软件验收可以使用短窗口和 30 只确定性合成股票，但不能据此选择最优参数。真实
日频策略一般应覆盖至少 8–12 年和多个市场状态。以当前数据时点为例，可采用：

- 2015–2016：warm-up/数据稳定期；
- 2017–2022：多折 walk-forward selection；
- 2023–2024：独立 validation；
- 2025–2026H1：一次性 locked test；
- 之后至少 3–6 个月：模拟盘 shadow。

具体日期必须由数据完整性和假设预先确定，不能为了得到更好曲线而移动。横截面因子
应使用足够宽且点时的股票池；固定 30 只股票和 10% 持仓只得到 3 只标的，不能可靠
代表因子分层、行业暴露或容量。

## 9. 成本、容量和可交易性

所有正式结果至少报告毛收益和净收益。平台执行配置必须非零并随 RunManifest 固化。
基础成本包括：

- 双边佣金和最低佣金；
- 卖方印花税；
- 双边滑点；
- 100 股整手；
- 最大持仓数和单股上限；
- 成交量参与率和部分成交。

研究层还应使用 `cost_stress_scenarios()` 做基础、加倍和严重成本压力测试，并使用
容量曲线评估不同资金规模的填充率和冲击。容量不能只用组合平均 ADV；应检查每只
股票、最拥挤交易日和尾部流动性。

在点时停牌、ST、涨跌停、一字板和退市状态尚未完整时，必须披露限制，不得把“有
有效 open/volume”误当作一定可成交。组合策略还需报告换手、行业集中、持仓重叠和
边际风险贡献。

## 10. 多重检验与稳健性

参数扫描之前必须预注册：

- 假设和经济机制；
- 参数空间与最大 trial 数；
- selection 指标及并列规则；
- 成本和容量假设；
- 选择候选的人工步骤；
- locked-test 窗口和通过门槛。

所有已尝试候选都计入研究家族，包括表现差、失败和取消的候选；不能只把成功或留下
的参数传给统计工具。

平台提供：

- 区块 Bootstrap：处理时序相关下的区间不确定性；
- PSR：单一、预注册候选的概率 Sharpe；
- DSR：按候选数量和非正态性修正选择偏差；
- CSCV/PBO：在同源、日期完全对齐的参数扫描中估计回测过拟合概率。

这些后验诊断默认 `selection_eligible=false`、`promotion_eligible=false`，不能替代
锁定测试。候选数量不得由浏览器任意传入，应从同一扫描或同一研究组的数据库事实
推导。若样本不足、收益不有限、trial 日期不一致或只剩一个候选，必须返回
`insufficient_samples/invalid_input`，不能制造 0 风险结论。

## 11. 确定性与自动化测试

新增策略至少覆盖以下测试矩阵：

### 11.1 单元测试

- `metadata()` 可规范序列化，ID、分类、训练模式和信号模式正确；
- 默认参数、边界、未知参数、错误类型和参数间不变量；
- 已知小样本的精确信号日期、方向、score 和 weight；
- 输入 DataFrame 不被原地修改；
- NaN、Infinity、零成交额、重复/乱序日期和空股票池；
- no-future 两类测试；
- 相同输入重复运行结果完全相同。

### 11.2 训练策略

- 固定 seed，并记录所有库的随机性设置；
- train-once 只训练一次，周期模型按预期重训练；
- rolling/expanding 窗口、purge、embargo 和 validation 正确；
- IC/RankIC 按预测日横截面计算，不把所有日期拍平成一个相关系数；
- 验证有效日和最小横截面不足时阻断；
- 模型字节、大小、哈希和元数据一致；
- macOS CPU/MPS 与可用的 Windows/CUDA 路径输出契约一致。

### 11.3 集成与前端

- 注册扫描后策略详情 API 可见；
- 前端参数表单与 `ParamField` 一致；
- 创建实验只产生一次允许的 readiness 写和一次实验写；
- job 状态、实验状态、metrics/equity/trades/model 产物一致；
- 失败可在详情页看到原始根因；
- 参数扫描整组原子入队；
- 高级研究路径的锁定测试必须来自显式人工候选选择，不能自动挑选或回看调参；
- 普通模拟部署可不绑定 promotion，但必须绑定来源实验、RunManifest、策略/参数/模型身份、
  数据代和不可变风险快照；若选择绑定 promotion，其证据不可替换或静默改变；
- 无数据、artifact/hash/schema 损坏、身份不一致和账本错误必须阻断；数据可信度与
  promotion 状态不足在 paper 中告警，live 始终拒绝。

提交前执行仓库规定的完整门禁：

```bash
ruff check backend/ tests/integration/
cd frontend && npx tsc -b --noEmit
pytest backend/tests/ -v --tb=short --timeout=120
pytest tests/integration/ -v --tb=short --timeout=180
cd frontend && npm run lint && npm run test && npm run build
```

## 12. 错误分类与恢复规则

| 类别 | 示例 | 是否自动重试 | 处理原则 |
|---|---|---:|---|
| 读取瞬态故障 | GET 超时、连接重置、服务重启时 `Failed to fetch` | 可以 | 指数退避、有界次数；不得改变业务状态 |
| SQLite 瞬态写冲突 | `SQLITE_BUSY`、`database is locked` | 可以 | 仅对已识别冲突有界重试；扫描批量原子提交 |
| 写入结果不确定 | POST 已观察到但响应超时 | 禁止直接重发 | 按唯一名称、意图键或持久化 ID 查询并恢复 |
| 数据源不可用 | TLS、断连、限流、上游结构变化 | 不应隐式重试实验 | 标为 provider outage；由显式数据任务恢复 |
| 输入/参数错误 | 422、未知参数、窗口无效、数据不足 | 否 | 用户可见、修正输入后创建新实验 |
| 策略算法错误 | 异常、非有限信号、真实 fit 失败 | 否 | 保留 traceback；修复代码并新建运行 |
| 完整性错误 | 快照哈希/schema、模型哈希、manifest 被篡改 | 否 | 失败关闭、隔离文件、审计调查 |
| 不可变性冲突 | 已有 RunManifest 与重跑候选不同 | 否 | 创建恢复副本，禁止覆盖原记录 |
| 权限/认证错误 | 401/403、跨用户资源 | 否 | 不降级权限，不把认证失败分类为网络瞬态 |
| 取消 | 用户取消、调度器取消 | 否 | 到达检查点即停止；不得随后写 completed |

重试必须具备幂等键或 CAS 条件。进度更新失败不能在实验产物已提交后把已完成实验
改写为算法失败；应区分“计算提交”和“状态同步”。任何自动恢复都必须记录旧任务、
新任务、触发原因和时间。

## 13. 前端到数据库的验收标准

正式链路验收必须通过真实 Chromium 操作，不能只调用 API：

1. 登录后进入“新建实验”；
2. 选择策略、参数、股票范围、行业、窗口和 `cache_only`；
3. 页面执行 readiness 检查；
4. 页面提交实验或参数扫描；
5. 后端原子创建实验和 job；
6. 调度器领取、心跳、进度、取消和完成状态可见；
7. 数据库保存实验、RunManifest、指标、净值、成交和模型产物；
8. 前端详情能查看相同数据；
9. 刷新页面、断线重连和服务短暂重启后继续观察原任务；
10. 报告记录 build SHA、实验 ID、job ID、数据摘要和最终状态，但不记录凭据。

验收断言至少包括：

- 每个用户动作产生且只产生预期写请求；
- readiness 即使使用 POST，也按“语义只读预检”单独登记；
- 响应必须是成功状态和 JSON，重定向不能被当作业务响应；
- 浏览器超时后先查数据库事实，不盲目再提交；
- 参数扫描成员数、job 数和数据库关联数一致；
- `completed` 实验具有完整指标、初始资本点、净值、成交或明确零成交解释；
- 前端展示时间与 API UTC 时间换算一致；
- 报告、checkpoint、截图权限受限且不含密码、token 或 Authorization。

## 14. 安全与数据泄露防护

策略是受信代码，但仍必须遵守最小权限：

- 不读取 `.env`、钥匙串、浏览器存储、用户数据库或其他用户产物；
- 不发起网络请求、shell 命令或动态 `eval/exec`；
- 不把绝对路径、用户名、密码、token 和 API key 写入参数、异常或模型；
- 模型和快照只保存经过校验的相对 storage key；
- 拒绝路径穿越、符号链接和存储根目录逃逸；
- 远程训练使用一次性令牌，完成、失败或取消后立即撤销；
- WebSocket 认证凭据通过首帧发送，不放在 URL；
- 日志经统一脱敏，浏览器子进程环境移除调优用户名和密码；
- 数据刷新、策略扫描、实验创建、扫描和部署分别执行 RBAC 校验。

数据定义型因子组合只允许引用白名单因子注册表和有限数值权重，不允许把用户输入
转换成 Python 源码执行。定义、所有者、版本和内容哈希必须一致，事务失败要回滚。

## 15. 已遇到的链路异常与标准处置

| 异常 | 根因/风险 | 标准处置 |
|---|---|---|
| `snapshot parquet schema changed` | pandas/PyArrow 时间单位回读可能从秒扩宽到毫秒；也可能是真实 schema 或文件变化 | 写前规范化时间单位并写后回读；若仍不一致按完整性错误失败关闭，禁止跳过校验 |
| `LegacyAdjustedCacheError` / 非 schema-v4 cache | 旧缓存缺少来源、复权和内容 provenance，无法证明研究输入 | 执行受控 force refresh 生成 schema-v4；旧缓存只可审计，不能作为运行 fallback |
| `industry_cache_missing_stale_or_invalid` | 行业缓存缺失、过期、哈希无效或目标代码覆盖不足 | 有行业选择时阻断；显式刷新后复检；不筛行业必须由用户清空，不能静默忽略 |
| 行业仅对部分股票池可用 | readiness 只按已有映射或固定池判断，没有按最终股票范围校验 | 对预设池、子集、自定义代码统一做精确代码集合和覆盖率检查 |
| Playwright `strict mode violation` | “股票池”等可访问名称同时命中步骤按钮和表单控件 | 使用 `exact`、role、关联 label 或稳定 test id；测试可访问名称的唯一性 |
| 找不到“自定义股票代码” | 字段受股票池选择条件控制，运行器使用了过期 UI 假设 | 先选择正确股票池并等待条件 UI；定位器契约随表单测试一起维护 |
| readiness POST 被写守卫判为违规 | 预检在 HTTP 层使用 POST，但语义上不创建实验 | 单独白名单为 `readiness`，严格限制路径和次数；不得扩大为任意 POST |
| `Response body is unavailable for redirect responses` | 提交到带/不带斜杠的非规范端点，Fetch/Playwright 获得 redirect | 使用规范端点；解析前检查最终状态、重定向和 `Content-Type` |
| `Failed to fetch` | 后端/前端重启、连接复位或上下文被销毁 | 仅对读取有界重试；写入先按意图查询是否已经成功 |
| `page.waitForResponse` 超时 | POST 可能已到达服务端，浏览器未获得匹配响应，结果处于不确定状态 | 若守卫已观察到恰好一次 POST，标记 ambiguous；通过唯一名称/意图恢复，禁止再发 POST |
| SQLite `database is locked` | API 连续插入扫描 job 时调度器过早唤醒并并发写同一 SQLite | 整组 job 与扫描关系单事务写入，提交后只唤醒一次；锁冲突使用有界退避 |
| 扫描只提交部分成员 | 循环逐个 submit 导致前几个已入队、后续锁失败 | 预检整组队列容量并批量原子提交；成员数、job 数和关联数必须相等 |
| 进度写锁导致实验被改写为失败 | 计算产物已提交，但随后的 job progress 更新抛出锁错误 | 对进度写做有界重试；计算结果提交与状态同步分层，禁止覆盖已提交完成证据 |
| 恢复时报 `ManifestConflictError` | 失败实验已经写入不可变 RunManifest，原地 reset 会用同一 ID 绑定不同运行 | 创建新的恢复实验副本并替换 sweep 成员；原实验、原清单和错误历史保持不变 |
| watchdog 安装器 `LABEL…: unbound variable` | shell 把紧邻变量名的非 ASCII 标点解析进参数边界 | 始终使用 `"${LABEL}"` 或 `printf '%s' "$LABEL"`；shellcheck 和实机安装测试 |
| 实验创建时间相差 8 小时或格式不一 | SQLite `datetime('now')` 是无时区 UTC，前端可能按错误 timezone 解析 | API 统一输出带 `Z` 的 UTC RFC3339，前端显式转 Asia/Shanghai；迁移旧值并测跨日排序 |
| provider TLS/断连后回退数据 | 上游不可用若被当成“无数据”，可能触发合成或不可信 fallback | 区分 provider outage 与合法空集；正式 `cache_only` 运行绝不联网或降级证据等级 |

这张表是回归测试输入，不只是运维记录。每次修复至少增加一个能在修复前失败的定向
测试，并验证错误分类没有把策略失败误判成瞬态故障。

### 15.1 本轮必须固化的数据和任务失败码

面向用户、任务中心和自动化脚本的错误必须使用稳定的机器码；异常类名、供应商错误
文本和本机路径只放在经脱敏的诊断字段。下表是新策略、数据适配器和前端运行器应复用
的最低集合。新增码只能细化，不得把完整性失败降级为可重试网络错误。

| 失败码 | 触发条件 | 自动动作 | 人工/系统恢复条件 |
|---|---|---|---|
| `market_raw_cross_validation_failed` | 两个独立原始价源在重叠日收益语义不一致 | 拒绝生成研究复权缓存 | 修复源映射或受控刷新并重验 |
| `market_interior_coverage_gap` | 两源共同可见区间内任一代码有单边缺交易日 | fail-closed，不以另一源补洞 | 获得完整原始行情后新建缓存 |
| `market_adjusted_reference_unavailable` | 后复权参考源不可用或不符合 HFQ 信息观察条件 | 保留原始价门禁结果；不把参考结果升级为真 | 恢复参考源后重新生成 provenance |
| `market_adjusted_factor_invalid` | 任一已观测复权因子为 NaN 以外的非有限值、零或负值 | 拒绝研究复权数据 | 修复企业行动/因子后重建 |
| `snapshot_schema_mismatch` | Parquet 逻辑 schema、dtype、轴或 kind 与元数据不符 | 隔离并拒绝读取 | 执行显式版本迁移，生成新快照/新实验 |
| `snapshot_hash_or_size_mismatch` | 文件 SHA-256、大小、路径或符号链接校验失败 | 拒绝读取、保留审计证据 | 从受信备份恢复或重新计算 |
| `cache_legacy_provenance` | 缓存不是 schema-v4 且无完整来源证明 | 不允许正式研究 fallback | controlled force refresh |
| `readiness_contract_failed` | 行情或 benchmark 不是同一窗口、同一 policy 的完整原子就绪 | 禁止提交实验/扫描 | 修复两端缓存后重新预检 |
| `browser_redirect_response` | 写请求收到 3xx 或最终 URL 非规范端点 | 不解析 response body、不重发 | 修正规范 URL 后以 intent 查询恢复 |
| `browser_write_ambiguous` | POST 已发出但页面超时/断开，无法确认响应 | 禁止直接重发 POST | 以 intent key/name 查询数据库并续跑 |
| `selector_contract_violation` | 定位器匹配多元素、条件字段不存在或控件类型不符 | 立即终止自动化，不提交半配置 | 更新页面 test id/表单契约及测试 |
| `protocol_config_mismatch` | 锁定协议的语义配置与当前提交配置不同 | 阻止提交 | 显式应用协议或新建协议；对象 key 顺序不同不应触发此码 |
| `isolated_cpu_spawn_failed` | 隔离 worker 不能启动、载荷超限或资源预算拒绝 | 不降级到 Web 进程计算 | 释放资源/修复 worker 后从已持久化任务恢复 |

### 15.2 行情、复权和快照的可执行数据契约

新策略不得直接假设“复权价格已经可信”。创建或消费行情缓存时，按下列顺序执行，
并将每一步的摘要写入 provenance 和 RunManifest：

1. 对**原始执行价**进行双源交叉验证。比较的是相同代码、共同观测日期的日收益/连续
   性语义，不是两个供应商的绝对价格水平。
2. 只允许任一端在共同区间之前或之后多出日期（例如上市首日、供应商边界）；必须记录
   `edge_only_coverage_difference`。两端共同区间中的任意单边日期都是
   `market_interior_coverage_gap`，不能插值、前填或由另一数据源静默补齐。
3. 原始价通过门禁后，才用官方前收递推重建研究用 HFQ 价格。第二个 HFQ 源仅用于
   **信息观察**：供应商复权锚点不一致不改变原始价双源门禁的结论，也不能替代它。
4. 宽表天然稀疏：某代码上市前、退市后或未在池内的格子可以是 NaN。质量检查只能对
   OHLC 的 **observed cells** 执行有限且正数校验；但只要已观测值为 `Inf`、`-Inf`、
   零或负数，或同一日 OHLC 不完整，即必须拒绝该缓存。
5. 把原始源、比较源、共同/边缘/内部日期计数、复权方法、因子检查、代码覆盖和内容
   哈希一起落盘。不得把“HFQ 参考可取”写成“原始价已交叉验证”。

Parquet 的契约也属于数据语义，不是可忽略的存储细节。每个 snapshot 至少绑定
`schema_version`、`kind`、逻辑 schema、index/column labels、dtype、相对 key、大小和
SHA-256。写入必须采用临时文件、原子替换、`fsync`（文件及父目录在系统支持时）并立刻
回读验证。读取时任一不一致都抛出完整性错误。

schema 升级按以下迁移协议进行：保留旧版本为只读证据；写一个版本特定、幂等的迁移器；
在隔离目录生成新文件；逐个验证 hash、逻辑 schema 和可读性；用新 snapshot key 创建
新运行，而不是覆写旧文件或篡改原 RunManifest。Windows 不保证 POSIX `chmod` 语义，
因此跨平台测试必须验证 Windows 可执行的原子写入、`flush`/`fsync`、哈希和路径安全；
只能在 POSIX 平台断言 mode bits，不能让 Windows 因不存在的权限语义误报失败。

### 15.3 策略研究模板（提交前必须填满）

每个新静态策略或因子应在 `docs/strategies/` 创建设计记录，并可直接按本模板填写。空
字段等同于未完成，不能通过研究晋级：

```text
策略 ID / 语义版本：
类型：static_single | trainable_single | portfolio | composite | generated_factor_combo
经济假设与失效条件：
信号可见时点、调仓时点、成交/估值语义：
输入字段、最小历史、最小横截面、warm-up：
股票池/行业/证券状态（PIT 与覆盖限制）：
数据角色：raw_execution / research_adjusted；来源、schema、provenance digest：
基准：代码、同窗 snapshot digest、可用性：
参数 schema、互斥/单调不变量、最大 trial 预算：
selection / validation / locked-test / shadow 窗口：
成本、容量、整手、停牌及不可成交假设：
输出：score/target weight/order；空信号与数据不足语义：
确定性、no-future、异常数据、API、浏览器 E2E 测试：
失败码、告警、恢复和是否允许重试：
证据：代码 SHA、manifest、快照、报告、所有者和审批：
```

`static_single` 才能进入“所有非机器学习单策略”的统一调优池。`portfolio`、
`composite` 和训练型策略分别采用自己的研究协议；运行时导出的 ID 必须匹配
`factor_combo_<12 位小写十六进制 digest>`（即 `^factor_combo_[0-9a-f]{12}$`）
是从因子研究定义和内容哈希生成的组合策略，不是静态单策略，必须从该批调优中排除。
它只能在因子研究页面以锁定因子定义、成分权重、成本、股票池和窗口为输入导出，并把
定义 digest 绑定到策略池版本。不能把生成式组合当作普通参数扫描候选混入单策略结果，
否则 trial 计数、可复现性和多重检验边界都会失真。

### 15.4 前端真实浏览器 E2E 门禁

API 测试不足以证明研究可从产品界面完成。每一种策略类型至少有一个真实 Chromium
流程；它在 CI 的受控服务和目标机的部署后冒烟中都应执行：

1. 通过可访问名称关联的精确 label 或稳定 `data-testid` 选择策略、股票池、条件字段、
   行业、窗口和参数。禁止使用会同时命中步骤导航和控件的模糊文本定位器。
2. 拦截请求并按**业务语义**分类：GET 是读；`/readiness` 的受限 POST 是只读预检；
   创建实验、扫描、导出和晋级是写。守卫必须验证方法、精确 path、payload shape 和次数，
   不能把任意 POST 加入白名单。
3. 对每个写响应先检查非重定向最终 URL、2xx 状态和 JSON `Content-Type`，再读取 body；
   所有调用使用服务端规范 URL，杜绝斜杠重定向触发 `response.json()` 异常。
4. 写前生成不含凭据的 canonical intent（策略、参数、范围、窗口、所有者和随机 nonce）。
   发生 `Failed to fetch` 或等待响应超时后，按 intent 查询任务/实验；发现已创建则观察它，
   未创建才在明确的新 intent 下重试。不得盲发第二次 POST。
5. 断言 cache-only readiness 中行情与 benchmark 使用同一请求窗口和 policy，且两者都
   具备完整覆盖、内容摘要、无网络访问。任何一项不成立都显示 `readiness_contract_failed`，
   不允许先提交行情后补基准。
6. 等待任务状态到终态，刷新详情页后核对 job、实验、指标、净值/成交、manifest 和前端
   展示时间。因子研究还要验证 history 分页、详情、证据下载、比较、导出策略池和失败
   信息脱敏。
7. 对锁定协议使用递归排序 key 的 canonical JSON 比较（数组顺序只在业务定义为无序时才
   可规范化）。对象插入顺序不同且值相同必须视为相等；真实字段变化才阻止提交。

### 15.5 隔离计算与资源边界

因子研究等 CPU/内存密集任务必须由受控 spawn worker 执行，而不是占用 API/Web 进程。
调用方只传递版本化、大小受限、可序列化的 request/pivot 引用；worker 只接受白名单
task，环境中剥离浏览器凭据和不必要密钥。结果同样经大小、类型、有限数值和 schema
校验后才持久化。取消、超时、崩溃和 spawn 失败都终止子进程并产生结构化失败码，绝不
回退到同进程执行。

调度器必须在启动前检查动态 CPU、内存和 I/O 预算，并为重任务保留可配置并发槽；压力
过高时保留队列原因而非把任务标为算法失败。worker 的异常向前端只提供稳定错误码和
经路径、token、用户名清理后的说明；完整 traceback 只进入受保护服务日志。每个隔离
任务记录 execution mode、资源决策、worker 生命周期和产物 digest，便于审计和恢复。

## 16. 代码审查清单

### 16.1 策略与数据

- [ ] 文件位于正确目录，辅助模块不会被自动发现。
- [ ] ID、版本、分类、训练模式和信号模式正确。
- [ ] 参数 schema 有边界，未知参数和非有限值失败关闭。
- [ ] `StrategyDataContract` 已登记字段、历史长度和最小股票数。
- [ ] 只使用传入 pivot 和 params，无网络、DB、环境或全局可变状态。
- [ ] 信号日期、调仓频率和 score/weight 语义有文档。
- [ ] no-future、截断等价、异常数据和输入不变性测试通过。
- [ ] PIT 股票池/行业风险可见，非 PIT 不可晋级。

### 16.2 训练与统计

- [ ] `TrainableStrategy` 只实现平台钩子，没有私有 walk-forward。
- [ ] 标签 horizon、purge、embargo、validation 和预测日边界正确。
- [ ] 随机 seed、依赖版本和设备记录在证据中。
- [ ] 参数空间和 trial 预算在运行前固定。
- [ ] selection 与 locked test 分离，锁定窗口没有被重复消费。
- [ ] DSR/PBO 使用数据库推导的完整候选集合。
- [ ] 只报告有限、可解释、样本量充分的指标。

### 16.3 执行、产物与安全

- [ ] 成本、最低佣金、印花税、整手和容量非零且写入清单。
- [ ] T/T+1、开盘成交、收盘估值与无有效开盘拒单通过。
- [ ] 快照、benchmark、模型和 RunManifest 哈希可复验。
- [ ] 失败、取消、重试和恢复不会覆盖不可变证据。
- [ ] 路径使用安全相对 key，不含密钥或用户本机路径。
- [ ] RBAC、所有者隔离、日志脱敏和负向安全测试通过。

## 17. 发布与服务验收清单

- [ ] 任务在经理分配的独立 worktree 开发并完成 PRD/设计确认。
- [ ] 代码、测试、API 文档、策略设计文档和 ROADMAP 状态同步。
- [ ] Ruff、后端单元、集成、前端 lint/test/build 全部通过。
- [ ] 对策略目录运行离线 research validation matrix。
- [ ] 使用临时数据库和受控快照完成 API 链路测试。
- [ ] 在目标 Mac 上以真实 Chromium 从前端创建并完成实验。
- [ ] 浏览器写守卫验证每种写操作恰好一次；readiness POST 仅按精确 path/payload 作为语义读。
- [ ] 所有浏览器写响应先通过 2xx、非重定向和 JSON 类型检查；超时后按 canonical intent 查询，未盲目重发。
- [ ] 行情和 benchmark 已以相同窗口、policy 和 provenance 原子 readiness 通过；任一缺失即未提交任务。
- [ ] 原始价双源门禁、内部日期缺口 fail-closed、observed-cells 复权校验及快照迁移回读均有回归测试。
- [ ] 生成式因子组合未混入静态单策略调优；导出版本包含锁定定义 digest 和研究证据。
- [ ] 重计算在隔离 worker 内完成，资源拒绝/取消/崩溃有结构化失败码且公开错误已脱敏。
- [ ] 实验详情可查看参数、时间、指标、曲线、成交、manifest 和模型证据。
- [ ] 服务重启和 Worker 故障后任务可观察、可恢复且不重复提交。
- [ ] watchdog 安装、健康阈值、冷却和单实例锁实机验证。
- [ ] 部署 build SHA 与已合并 master 一致，健康接口返回预期版本。
- [ ] 正式数据库先做一致性检查和可恢复备份，迁移后再复检。
- [ ] 管理员凭据、JWT、API key 和浏览器报告均未进入 Git 或日志。
- [ ] 任何 PIT、可交易性或实盘认证阻断项仍在前端明确展示。

只有上述门禁和真实服务链路都通过，策略功能才算完成。通过软件验收只证明链路
可运行；只有点时真实数据、预注册研究、独立锁定测试、成本容量和不可变证据同时
成立，才可以把结果称为可信量化研究。
