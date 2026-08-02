# API 文档

Base URL: `http://localhost:8000`

所有接口统一返回格式：

```json
{
  "data": { ... },
  "detail": "错误描述（仅错误时）"
}
```

认证方式：`Authorization: Bearer <access_token>`

时间字段：实验、参数方案、研究清单和策略关联实验接口的时间戳统一为
RFC 3339 UTC（例如 `2026-07-30T00:00:00Z`）；交易日区间仍使用
`YYYY-MM-DD`。数据库与前端的完整处理约定见
[时间字段契约](TIME_CONTRACT.md)。

---

## 目录

- [认证 (Auth)](#认证-auth)
- [策略 (Strategies)](#策略-strategies)
- [实验 (Experiments)](#实验-experiments)
- [策略相关性诊断](#策略相关性诊断)
- [研究稳健性诊断](#研究稳健性诊断)
- [交易 (Trading)](#交易-trading)
- [数据 (Data)](#数据-data)
- [AI 分析 (AI)](#ai-分析-ai)
- [管理 (Admin)](#管理-admin)
- [研究工作流与策略晋级](#研究工作流与策略晋级)
- [任务 (Jobs)](#任务-jobs)
- [WebSocket](#websocket)
- [健康检查](#健康检查)
- [运维网络与恢复边界（非 HTTP API）](#运维网络与恢复边界非-http-api)

---

## 认证 (Auth)

前缀：`/api/auth`

### POST /register — 注册

开发环境中首位注册用户自动成为管理员。生产环境首位注册还必须携带 `X-Bootstrap-Token`，其值与服务端 `BOOTSTRAP_ADMIN_TOKEN` 一致；否则按只读用户创建。

权限：无

请求体：
```json
{
  "username": "string (3–32位ASCII字母、数字或下划线)",
  "password": "string (8–72字符且UTF-8不超过72字节)",
  "display_name": "string | null",
  "email": "string | null"
}
```

响应：
```json
{
  "data": {
    "user_id": 1,
    "username": "admin",
    "is_admin": true,
    "access_token": "...",
    "refresh_token": "..."
  }
}
```

### POST /login — 登录

权限：无

请求体：
```json
{
  "username": "string",
  "password": "string"
}
```

响应：
```json
{
  "data": {
    "user_id": 1,
    "username": "admin",
    "display_name": "...",
    "email": "...",
    "is_admin": true,
    "access_token": "...",
    "refresh_token": "..."
  }
}
```

### POST /refresh — 刷新 Token

权限：无（使用 refresh token）

请求体：
```json
{
  "refresh_token": "string"
}
```

响应：
```json
{
  "data": {
    "access_token": "...",
    "refresh_token": "..."
  }
}
```

### GET /me — 当前用户信息

权限：登录用户

响应：
```json
{
  "data": {
    "id": 1,
    "username": "admin",
    "display_name": "...",
    "email": "...",
    "is_admin": true,
    "is_active": true,
    "permissions": ["experiments:read", ...]
  }
}
```

---

## 策略 (Strategies)

前缀：`/api/strategies`

权限要求：`strategies:read`（除 scan 需要 `strategies:scan`）

### GET / — 策略列表

查询参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `category` | string? | 筛选分类：`technical` \| `ml` \| `factor` \| `portfolio` \| `composite` |

响应：
```json
{
  "data": [
    {
      "strategy_id": "ma_cross_v1",
      "display_name": "双均线交叉策略",
      "version": "1.0.0",
      "category": "technical",
      "description": "...",
      "supported_modes": ["batch"],
      "requires_training": false,
      "retrain_frequency": "never",
      "params": [
        {
          "name": "fast_period",
          "type": "int",
          "default": 20,
          "description": "快速均线周期（日）"
        }
      ],
      "sub_strategies": [],
      "tags": ["趋势跟踪", "均线"]
    }
  ]
}
```

### POST /scan — 扫描热加载

权限：`strategies:scan`

重新扫描 `backend/strategies/` 目录，注册新增策略。

响应：
```json
{
  "data": { "before": 10, "after": 11, "added": 1 }
}
```

### GET /{strategy_id} — 策略详情

响应同列表项，额外包含：`estimated_training_seconds`、`max_position_pct`、`supported_position_modes`、`experiment_count`、`deployment_count`。

### GET /{strategy_id}/sub-strategies — 子策略

获取组合策略的子策略列表。

### GET /{strategy_id}/parent-strategies — 父策略

获取引用了该策略的组合策略列表。

### GET /{strategy_id}/best-experiments — 最佳实验

查询参数：`limit` (int, 默认 10)

返回该策略下星标/已标注的已完成实验，按 Sharpe 降序。

### POST /{strategy_id}/validate — 参数校验

权限：`strategies:read`

请求体：
```json
{
  "params": { "fast_period": 20, "slow_period": 60 }
}
```

响应：
```json
{
  "data": { "valid": true, "message": "参数校验通过" }
}
```

---

## 实验 (Experiments)

前缀：`/api/experiments`

### GET / — 实验列表

权限：`experiments:read`（非 admin 仅看自己）

查询参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `strategy_id` | string? | 按策略筛选 |
| `strategy_category` | string? | 按实验创建时保存的策略分类筛选：technical \| ml \| factor \| portfolio \| composite |
| `status` | string? | 按状态筛选：pending \| running \| completed \| failed |
| `starred` | bool? | 仅星标 |
| `label` | string? | 按标签模糊搜索 |
| `search` | string? | 按实验名称或策略 ID 模糊搜索 |
| `sort_by` | string | 全量排序字段：created_at \| annual_return \| sharpe_ratio \| max_drawdown \| strategy_id \| status；默认 created_at |
| `sort_order` | string | 排序方向：asc \| desc；默认 desc |
| `page` | int (≥1) | 页码，默认 1 |
| `limit` | int (1-100) | 每页条数，默认 20 |

排序在所有筛选条件之后、分页之前执行；指标缺失值始终排在末尾，同值使用
实验 ID 作确定性次级排序。

响应：
```json
{
  "data": {
    "items": [
      {
        "id": 1,
        "name": "MA Cross Test",
        "strategy_id": "ma_cross_v1",
        "strategy_category": "technical",
        "is_starred": false,
        "labels": [],
        "test_start": "2024-01-01",
        "test_end": "2026-06-30",
        "params": { "fast_period": 20 },
        "status": "completed",
        "sharpe_ratio": 1.517,
        "annual_return": 1.023,
        "max_drawdown": -0.303,
        "win_rate": 0.55
      }
    ],
    "total": 42,
    "page": 1,
    "limit": 20,
    "sort_by": "created_at",
    "sort_order": "desc"
  }
}
```

### POST / — 创建实验

权限：`experiments:create`

请求体：
```json
{
  "name": "string",
  "strategy_id": "ma_cross_v1",
  "pool_preset": "csi300 | csi500 | csi800 | csi1000 | all_a | null",
  "pool_custom_codes": "000001.SZ,000002.SZ | null",
  "pool_industries": "银行,医药生物 | null",
  "train_start": "2020-01-01 | null",
  "train_end": "2023-12-31 | null",
  "test_start": "2024-01-01",
  "test_end": "2026-06-30",
  "params": { ... },
  "mode": "batch",
  "data_access_policy": "allow_fetch | cache_only",
  "source_experiment_id": 42
}
```

`data_access_policy` 默认为 `allow_fetch`。`cache_only` 会把策略持久化到
`run_spec` 和研究运行清单；执行时只接受本地 schema-v4 OHLCV 缓存及本地
基准指数缓存，并严格校验请求代码和完整时间覆盖。任何缺失都会使实验安全失败，
不会构造公共数据源或调用网络 fallback。

`source_experiment_id` 可选。通过“继承配置，新建实验”创建时保存来源关系，
但不会自动提交，用户仍可修改股票池、时间窗口和全部策略参数。

响应：
```json
{
  "data": {
    "experiment_id": 42,
    "job_id": "exp-42"
  }
}
```

### GET /{experiment_id} — 实验详情

权限：`experiments:read`

返回实验完整信息 + 指标摘要，并包含持久化的
`data_access_policy`（`allow_fetch` 或 `cache_only`）。

### GET /{experiment_id}/export — 导出研究证据

权限：`experiments:read`（非 admin 仅能导出自己的实验；不存在与越权均返回
404）

查询参数：`format=json|csv`，默认 `json`。仅允许导出 `completed` 实验；其他
状态返回 409。

- `json` 返回流式 `application/json`，包含导出 schema、UTC 生成时间、实验
  配置/参数、完整指标、完整净值与成交、不可变研究清单及其 SHA-256、数据血缘、
  风险摘要和证据完整性计数。
- `csv` 返回 `application/zip`。压缩包内包含 `metadata.csv`、
  `experiment.csv`、`metrics.csv`、`equity_curve.csv`、`trades.csv`、
  `risk_summary.csv`、`evidence_completeness.csv`，以及保留嵌套结构的
  `research_manifest.json` 和 `data_lineage.json`。

响应使用安全的固定格式文件名、`Cache-Control: no-store` 和
`X-Content-Type-Options: nosniff`。服务端分批读取净值与成交；CSV 文本字段会
转义电子表格公式前缀。错误日志、令牌/密钥类字段、模型存储路径和本机绝对路径
不会被导出。缺少历史研究清单的旧实验仍可导出，但清单字段为 `null`，且
`risk_summary.evidence_warnings` 会明确包含 `immutable_manifest_missing`，不可
将其视为可复现实验证据。

### DELETE /{experiment_id} — 删除实验

权限：`experiments:delete`

级联删除关联的指标、净值曲线、交易记录、模型产物。

### GET /{experiment_id}/metrics — 36 项指标

权限：`experiments:read`

响应包含：`sharpe_ratio`、`annual_return`、`max_drawdown`、`volatility`、`calmar_ratio`、`sortino_ratio`、`win_rate`、`profit_loss_ratio`、`var_95`、`cvar_95`、`alpha`、`beta`、`information_ratio`、`skewness`、`kurtosis` 等 36 项指标。

### GET /{experiment_id}/equity — 净值曲线

权限：`experiments:read`

查询参数：`resolution` — `daily` \| `weekly` \| `monthly`

响应：
```json
{
  "data": [
    { "date": "2024-01-02", "equity": 1000000.0, "benchmark": 1003200.0, "daily_return": 0.01, "drawdown": 0.0 }
  ]
}
```

### GET /{experiment_id}/trades — 成交明细

权限：`experiments:read`

查询参数：`page` (默认 1), `limit` (默认 50, 最大 500)

`signal_date` 是信号日 T，`date` 是下一交易日的实际成交日 T+1，`price` 为该成交日开盘价。

### GET /{experiment_id}/models — 模型产物

权限：`experiments:read`

返回该实验关联的模型产物列表（仅 ML 策略）。

### PUT /{experiment_id}/star — 切换星标

权限：`experiments:read`

请求体：
```json
{ "is_starred": true }
```

### PUT /{experiment_id}/labels — 设置标签

权限：`experiments:read`

请求体：
```json
{ "labels": ["优秀", "稳健"] }
```

### GET /picker — 部署选择器

权限：`experiments:read`

查询参数：`strategy_id`? / `starred_only`? / `sort` (sharpe\|return\|drawdown\|date) / `limit`

返回已完成实验列表，用于部署时选择。

### GET /recovery — 精确提交恢复查询

权限：`experiments:read`

查询参数：`name`、`strategy_id`（均为精确匹配）。

仅返回当前登录用户拥有的同名实验与参数扫描，即使管理员调用也不会跨用户查询。
该只读接口供浏览器自动化在“POST 已提交成功、checkpoint 尚未写入”后恢复既有
ID；调用方必须继续核对窗口、参数、股票池和运行模式，出现多个候选时应停止。
恢复记录同时返回从持久化 `run_spec` 解析的 `data_access_policy`；扫描候选还返回
成员绑定的 `source_experiment_id`，供调用方把缓存策略与基线身份纳入恢复指纹。

### POST /sweep — 创建参数扫描

权限：`experiments:sweep`

请求体：
```json
{
  "strategy_id": "ma_cross_v1",
  "name": "MA参数优化",
  "param_grid": {
    "fast_period": [10, 20, 30],
    "slow_period": [50, 60, 80]
  },
  "base_params": { "min_score": 0.5 },
  "pool_preset": "csi800",
  "selection_start": "2024-01-01",
  "selection_end": "2025-06-30",
  "locked_test_start": "2025-07-01",
  "locked_test_end": "2026-06-30",
  "data_access_policy": "cache_only"
}
```

扫描成员继承基准实验的数据访问策略并写入每个成员的 `run_spec`；人工晋升的
唯一锁定测试实验继续继承该策略。服务重启后的任务恢复只读取持久化策略，不依赖
队列内的瞬时参数。

响应：
```json
{
  "data": {
    "sweep_id": 1,
    "total_experiments": 9,
    "experiment_ids": [43, 44, ...],
    "job_ids": [...]
  }
}
```

### GET /sweep/{sweep_id} — 扫描结果

权限：`experiments:read`

返回 sweep 信息 + 所有子实验（按 Sharpe 排序）。

### POST /compare — 多实验对比

权限：`experiments:read`

请求体：`{ "experiment_ids": [1, 2, 3] }`（2-10 个）

### 参数方案

#### GET /parameter-presets — 参数方案列表

权限：`experiments:read`

查询参数：`strategy_id`? / `page` / `limit`。仅返回当前用户的方案。

#### POST /parameter-presets — 保存参数方案

权限：`experiments:create`

保存策略参数、运行模式、股票池、来源实验、指标快照、标签和备注。来源实验必须
已完成并与策略一致；同一用户、同一策略下名称唯一。

#### GET /parameter-presets/{preset_id} — 参数方案详情

权限：`experiments:read`

#### PUT /parameter-presets/{preset_id} — 更新参数方案

权限：`experiments:create`

支持修改名称、参数、股票池、备注、标签和默认状态；同一用户同一策略最多一个默认方案。

#### DELETE /parameter-presets/{preset_id} — 删除参数方案

权限：`experiments:create`

只删除方案本身，不删除来源实验；来源实验删除后，方案中的参数快照仍可继续使用。

### 训练模式契约

策略列表与详情响应新增 `training_mode`：

- `none`：无需训练；
- `train_once`：一次训练，创建实验时必须提供 `train_start/train_end`；
- `periodic`：周期重训练，创建实验时只需测试区间，平台按重训练点自动构造历史窗口。

周期模型的公共参数包含 `window_mode`、`rolling_train_months`、
`validation_months`、`embargo_days` 和 `min_train_months`。

## 策略相关性诊断

### GET /api/research/strategy-correlation

权限：`experiments:read`（非 admin 只能分析自己的实验）

查询参数：

| 参数 | 范围 | 默认值 |
|------|------|--------|
| `experiment_ids` | 重复查询参数，2–20 个不重复 ID | 必填 |
| `method` | pearson \| spearman | pearson |
| `min_observations` | 10–2520 | 60 |
| `weights` | 与实验顺序一致的重复非负数；服务端归一化 | 等权 |
| `tail_fraction` | 0.01–0.25 | 0.10 |

接口仅接受已完成且通过 PIT-only 清单校验的实验，从数据库已持久化净值推导相邻观测
收益，不请求外部行情、不写库。缺少运行清单、清单哈希不一致、使用网络/旧缓存回退、
PIT 时间线或规范价格绑定不一致时返回 422，不会把历史旧实验包装成正式相关性证据。
配对时同时对齐收益结束日期和前一观测日期，避免把跨多日收益与单日收益相关。响应包含
相关系数矩阵、共同观测数矩阵、每组配对的对齐区间、常数序列/观测不足原因，以及高度
同向和数据质量告警。该结果固定标记为
`post_hoc_diversification_diagnostic`，不能直接作为选模或自动组合依据。

若实验保存了 `trade_log`，响应还按每日收盘后库存重建股票代码集合，给出配对持仓
Jaccard 重叠；没有可复核交易记录时明确返回 unavailable。尾部相关使用任一策略
进入自身尾部分位的共同日期，观测不足时不计算。`portfolio_contribution` 按查询
权重报告共同样本的年化收益、波动、边际收益/风险和组合尾部收益贡献，
`constraint_suggestions` 只给出人工审查上限及理由。响应固定声明
`mutates_portfolio=false`；接口不会保存权重、调整组合或创建订单。
数据库不可用或未知服务异常分别返回稳定的
`experiment_database_unavailable` / `strategy_correlation_failed`，
响应不会拼接 SQLite 错误、本机路径或堆栈。

### POST /api/research/strategy-correlation/portfolio-candidates

权限：`experiments:read`（非 admin 只能使用自己的实验）

请求体包含 3–20 个不重复的 `experiment_ids`，以及可选的 `method`、
`min_observations`、`tail_fraction`、`max_components`、
`max_pair_correlation`、`max_holding_overlap` 和 `max_weight`。每个来源必须是已完成、
已注册、非机器学习、非组合/配置类的单策略实验，并通过与上方相同的 PIT 时间线、
规范价格绑定和清单哈希校验；同一策略不能重复占据多个来源位置。

响应固定生成五个确定性草案：收益风险平衡、收益质量、低相关、尾部韧性和逆波动。
每个草案都给出 `composite_research_weighted_v1` 可执行参数、组件实验/参数/权重、
风险约束结果、来源运行清单哈希和候选定义 SHA-256。来源调优参数作为数据而非代码
传入，组合运行不会反序列化来源实验产物。所有候选固定为 `status=draft`、
`automatic=false`；接口不注册策略、不提交实验、不修改模拟盘或实盘配置。

## 研究稳健性诊断

### GET /api/research/experiments/{experiment_id}/robustness

权限：`experiments:read`（非 admin 只能读取自己的实验）

查询参数：

| 参数 | 范围 | 默认值 |
|------|------|--------|
| `seed` | 0–4294967295 | 0 |
| `n_bootstrap` | 100–5000 | 1000 |
| `bootstrap_method` | moving \| stationary | moving |
| `n_slices` | 4–20 的偶数 | 8 |
| `max_combinations` | 1–512 | 256 |

接口只读打开 `experiment.db`，不会请求行情、写报告或改变研究工作流。仅接受已完成、
具备有效 RunManifest 和完整初始资本点的实验；净值日期重复、非单调、非有限、
非正、样本不足或首行并非 manifest 绑定的初始资本时 fail closed。

候选数量不能由客户端传入。DSR 从同一参数扫描、该扫描晋升的锁定测试或同一研究组
的已完成试验推导；独立实验明确记为 1 个候选。CSCV/PBO 只使用同一扫描中日期完全
一致的收益矩阵，不插值、不补齐。缺少 gross return、逐期 turnover、PIT ADV、
不可变排名指标或可信 p-value 时，成本压力、容量、参数稳定区和多重检验明确返回
`unavailable`。

所有响应固定包含：

```json
{
  "data": {
    "analysis_role": "post_hoc_diagnostic",
    "selection_eligible": false,
    "promotion_eligible": false
  }
}
```

该结果是测试区间的事后诊断。若要作为晋级证据，必须先预注册协议，并将结果固化到
immutable workflow Report；此接口本身永远不会修改 promotion gate。

## Execution（QMT / PTrade 接入准备）

### GET /api/execution/live-readiness

权限：`trading:read`

返回机器可读的实盘安全硬门禁。报告固定包含：

- `schema_version`、`capability_version`、`ready` 与 `certification`；
- 按数据、市场规则、撮合、券商生命周期、风控、安全运维、模型治理、
  时钟与交易日历划分的 `domains`；
- 每项必选能力的状态、证据、限制、阻断原因与整改要求；
- 从实际 adapter capability、SDK 探测和配置缺口派生的只读证据。

当前响应必为 `ready=false`、`certification=not_certified`，
`platform_scope=research_and_paper_trading_only`。安装 SDK、填写环境变量，
甚至修改适配器自己的健康声明，都不能使平台自动获得认证。接口不会连接券商、
访问账户、读取持仓或访问网络。

### GET /api/execution/adapters/readiness

权限：`trading:read`

只读返回 QMT/PTrade 的可选 SDK、所需配置、能力声明和连接就绪状态。
当前 `live_order_submission=false`，不会连接券商或下单。

### POST /api/execution/orders/validate

权限：`trading:execute`

请求体：

```json
{
  "adapter_id": "qmt",
  "order": {
    "symbol": "600000.SH",
    "side": "buy",
    "order_type": "limit",
    "quantity": 100,
    "limit_price": 10.5
  }
}
```

仅执行本地订单格式、账户和适配器能力预检。`can_submit` 只有在订单有效、
适配器健康且明确启用真实提交时才可能为 `true`；当前始终为 `false`。
该接口不是订单提交接口，路由中不存在真实提交、撤单或成交变更能力。

---

## 交易 (Trading)

前缀：`/api/trading`

本节所有部署、组合、运行、订单和持仓均属于模拟盘。它们不读取券商账户，
不代表实盘状态，也不能绕过 `/api/execution/live-readiness` 的硬门禁。

### 部署

#### GET /deployments — 部署列表

权限：`trading:read`

查询参数：`status`? / `strategy_id`?

#### POST /deployments — 创建部署

权限：`trading:deploy`

请求体：
```json
{
  "strategy_id": "ma_cross_v1",
  "display_name": "MA模拟观察",
  "source_experiment_id": 42,
  "source_model_artifact_id": null,
  "research_promotion_id": null,
  "params": { "fast_period": 20, "slow_period": 60 },
  "mode": "batch",
  "retrain_frequency": "monthly | null",
  "position_mode": "equal_weight",
  "position_config": {},
  "status": "active",
  "portfolio_id": 3,
  "target_weight_bps": 2000
}
```

当 `source_experiment_id` 指向训练型策略实验且未显式传入
`source_model_artifact_id` 时，服务端自动绑定该实验的最新模型产物；来源实验
必须已完成并包含净值。模拟盘生成信号时直接加载被绑定的模型版本，不会在每个
交易日重复训练。

`status="active"` 必须绑定当前用户的已完成来源实验、净值和 RunManifest。
`research_promotion_id` 对个人模拟可选：留空时服务端固定来源实验的数据代、来源、窗口、
manifest hash 和研究告警快照；提供时必须是同一 locked-test 实验的 approved promotion，
并冻结 Report、RunManifest 和模型证据身份。已绑定 promotion 不可替换或移除。

来源/PIT/冲突、双价格账本或未审批是 paper 可信度告警；来源实验/manifest/artifact 损坏、
策略/参数/模型身份不一致仍返回技术性错误。无论是否绑定 promotion，均不获得 live 资格。

`portfolio_id` 与 `target_weight_bps` 必须同时提供或同时省略。提供时还需要
`trading:rebalance` 权限；服务端会在同一事务内创建部署并只发布到指定模拟盘
的新组合版本，目标仓位不得超过该模拟盘的剩余现金仓位。

#### PUT /deployments/{deployment_id} — 更新部署

权限：`trading:deploy`

可更新：`status`、`position_config`、`status_tags`、`user_notes`、`display_name`、
`research_promotion_id`（仅用于尚未绑定的 paused 草稿）

将状态更新为 `stopped` 时，部署不能仍被任何组合以正权重引用；否则返回 `409`。
应先通过组合草稿发布一个不再包含该部署的新版本。停止部署不会删除历史净值、
订单、信号或持仓快照。

每次模拟运行在策略计算前和账本提交前重验不可变绑定。绑定 promotion 的部署重验其
approved 证据；未绑定 promotion 的部署重验研究风险快照、来源实验 eligibility 和
RunManifest hash。任何快照/证据篡改或旧 active 部署缺少有效来源绑定时失败并回滚。

#### DELETE /deployments/{deployment_id} — 删除部署

权限：`trading:deploy`

#### PUT /deployments/{deployment_id}/retrain — 触发重训练

权限：`trading:deploy`

提交后台 retrain 任务。

#### GET /deployments/{deployment_id}/models — 模型版本历史

权限：`trading:read`

#### GET /deployments/{deployment_id}/model-lifecycle — 模型生命周期证据

权限：`trading:read`。返回部署调度状态、下一次到期时间、最多 100 条重训练
尝试、不可变版本摘要和安全边界。模型与元数据只返回相对存储键，失败信息中的本机
路径会被脱敏。`manifest_verified=true` 表示候选的模型字节、规范清单和晋级状态
证据完整；失败候选不会覆盖当前冠军。

自动调度由 `MODEL_RETRAIN_AUTO_RUN` 和 `MODEL_RETRAIN_SCAN_MINUTES` 控制。
调度器与人工接口都只向统一持久化队列提交 `retrain` 任务，并按 deployment
对活动任务去重。失败后还会遵守 `MODEL_RETRAIN_FAILURE_RETRY_HOURS` 冷却，
防止不可恢复的训练错误形成任务风暴。任何路径都不会自动发布到实盘。

### 投资组合

#### GET /portfolios — 组合列表

权限：`trading:read`

#### POST /portfolios — 创建组合

权限：`trading:rebalance`

请求体：
```json
{
  "name": "稳健组合",
  "total_capital": 1000000,
  "rebalance_frequency": "monthly",
  "allocations": [
    {
      "deployment_id": 1,
      "target_weight_bps": 4500,
      "min_weight_bps": 3000,
      "max_weight_bps": 6000,
      "risk_budget_bps": 5000,
      "locked": false
    },
    {
      "deployment_id": 2,
      "target_weight_bps": 3500,
      "min_weight_bps": 2000,
      "max_weight_bps": 5000,
      "risk_budget_bps": 5000,
      "locked": false
    }
  ]
}
```

权重使用整数基点（`10000 bps = 100%`）。策略权重可以小于 100%，差额为显式现金仓位；不能超过 100%。`weight` 小数形式仅为旧客户端兼容字段。

#### PUT /portfolios/{portfolio_id} — 更新组合

权限：`trading:rebalance`

#### POST /portfolios/{portfolio_id}/validate — 校验配置

权限：`trading:rebalance`

规范化基点配置，并返回上下限、重复部署、总权重、现金权重和风险预算的校验结果。

#### POST /portfolios/{portfolio_id}/preview — 调仓预览

权限：`trading:rebalance`

以最近持仓快照为基准返回每个部署的目标资金、资金差额、买卖方向、换手率和估算成本。

#### POST /portfolios/{portfolio_id}/drafts — 保存草稿

权限：`trading:rebalance`

保存不可变修订版，不立即改变当前生效配置。请求体与校验接口相同，可带 `effective_date`。

#### POST /portfolios/{portfolio_id}/drafts/{revision}/publish — 发布草稿

权限：`trading:rebalance`

只允许发布校验通过的草稿；原发布版本归档。

#### GET /portfolios/{portfolio_id}/versions — 版本历史

权限：`trading:read`

#### GET /portfolios/{portfolio_id}/nav — 组合净值

权限：`trading:read`

#### GET /portfolios/{portfolio_id}/overview — 组合总览

权限：`trading:read`

返回组合权益、现金、收益、回撤、Sharpe、当前策略权重与实际仓位、最近订单和持仓。

#### GET /portfolios/{portfolio_id}/strategy-analytics — 策略级分析

权限：`trading:read`

查询参数：`start_date`? / `end_date`?（`YYYY-MM-DD`）。

返回每个部署的现金流调整后收益、独立净值、盈亏、回撤、交易成本、换手率和
组合贡献，以及逐日策略序列。组合内部资金调拨记录在 `net_flow` 中，不计入策略收益；
无足够历史或无法可靠推导的指标返回 `null`，不以 0 代替。

### 持仓

#### GET /positions — 持仓查询

权限：`trading:read`

查询参数：`portfolio_id`? / `date`?（默认最新）

这是模拟交易日收盘后的持仓快照，不是盘中实时持仓。

### 信号

#### GET /signals — 信号列表

权限：`trading:read`

查询参数：`deployment_id`? / `date`? / `limit` (默认 100)

`date` 表示信号日 T。信号只能读取截至 T 日的数据。

### 订单

#### GET /orders — 订单记录

权限：`trading:read`

查询参数：`deployment_id`? / `page` / `limit`

成交订单的 `date` 为 T+1 交易日，成交价取该日 `open`，`filled_at` 为 09:30。若无有效开盘价，订单记录为 `rejected`，不会用收盘价代替。

### 模拟执行

#### POST /simulate/run — 触发模拟

权限：`trading:execute`

提交 daily_simulation 后台任务。

模拟执行严格遵循：T 日收盘后信号 → T+1 交易日开盘成交 → T+1 收盘估值。相同用户和交易日重复执行时复用已有运行批次，不重复下单。

请求日期必须在该部署行情中存在，周末、节假日或缺失行情不会静默回退到前一交易日。
已完成较新交易日后不允许回补更早日期，避免用未来现金余额污染历史快照。

#### GET /simulate/calendar — 历史回放日历

权限：`trading:read`

只读加载固定的 `csi500` 本地缓存，因为历史回放服务本身按中证 500 交易日驱动。
成功时返回缓存中的最早/最晚日期、建议的最近 20 个交易日起点和实际交易日数；
不会联网刷新，也不会用周末或推测日期补齐交易日历。

缓存未下载或为空时返回 404 `simulation_calendar_cache_missing`；旧 schema 或来源
证据无效时返回 409 `simulation_calendar_cache_legacy`；价格质量、来源证明、日期索引
或其他本地完整性校验失败时返回 409
`simulation_calendar_cache_integrity_invalid`。错误响应稳定包含
`detail`、`code`、`pool_id=csi500` 和 `action`，不包含内部异常或本地路径。

#### GET /simulate/status — 模拟状态

权限：`trading:read`

#### GET /simulate/runs — 模拟运行批次

权限：`trading:read`

查询最近的真实运行批次、信号日、交易日、状态和执行摘要。

---

## 数据 (Data)

前缀：`/api/data`

### 股票池

#### POST /experiment-readiness — 实验本地数据就绪检查

权限：`data:read`

请求只接受 `data_access_policy=cache_only`、`price_purpose`、PIT 股票池
以及训练和测试窗口。`price_purpose` 可为 `compatibility_research`（默认兼容）、
`return_research`、`real_tuning` 或 `execution_simulation`；后三种分别执行可信
调整价、完整双账本真实调优和 raw 执行价门禁。响应同时给出日线 OHLCV 与对应
基准指数的日期覆盖、代码/字段缺口和摘要；
日线摘要从同一个锁内原子读取的 frame/provenance 对生成，包含日期数、字段、
代码摘要、调整方式、来源等级与 frame digest，可安全替代分步读取
`GET /cache/status` 做提交身份门禁。
响应还包含非正/非有限价格、OHLC 逻辑、重复/未来日期、字段和成交量质量统计；
`ready_for_return_research`（旧静态集合价格研究兼容字段）、
`ready_for_unbiased_return_research`、`ready_for_execution_simulation` 与
`ready_for_unbiased_tuning` 分开表达用途，避免把调整价冒充原始成交价，或把当前
成分股或自定义静态列表冒充 point-in-time 股票池。行情缓存没有经过独立验证的
成分有效期时间线时，普通缓存 `ready` 可以为真，但
`ready_for_unbiased_tuning` 必须为假并返回
`point_in_time_universe_missing`。`ready_for_return_research` 还要求 licensed /
exchange evidence，或所有批次均通过独立公共源交叉验证；`declared` 测试数据不会
被升级为研究级来源。该旧字段即使为 true 也不能晋级；缺 PIT、规范价格账本或
runtime batch/hash、权威日历、PIT 基准任一绑定缺失时，
`ready_for_unbiased_return_research` 必须为 false。正式实验和扫描只接受四个 CSI
治理池，且创建记录前执行同一门禁；`compatibility_research` 仅保留请求兼容性，
其 `ready` 也按 PIT 正式研究口径计算。
该端点不初始化外部数据源、不写缓存、不暴露文件路径，`network_accessed` 固定为
`false`。请求体禁止额外字段。

#### GET /pools — 股票池列表

权限：`data:read`

只返回可进入 PIT 治理流程的 CSI 300/500/800/1000。`all_a` 与自定义静态池不再
作为正式研究候选。

#### GET /pools/{pool_id} — 池详情

权限：`data:read`

含行业分布。

#### GET /pools/{pool_id}/stocks — 池内股票列表

权限：`data:read`

查询参数：`industry`?（按行业筛选）

### 行业分类

#### GET /industries — 行业列表

权限：`data:read`

查询参数：`classification=cninfo_008001`（默认）以及可选 `pool_id`。

只读取经过 schema、新鲜度和 SHA-256 完整性校验的本地缓存，不会隐式联网。
传入 `pool_id` 时返回该股票池映射覆盖率；覆盖率低于 95% 时不可用于筛选。返回的
行业条目只包含该范围内已有可信映射的行业，避免提交后得到空股票集。

#### POST /industries/readiness — 自定义范围行业就绪检查

权限：`data:read`

请求体为 `{ "codes": ["000001.SZ", "600000.SH"] }`；代码只接受 6 位证券代码及
可选 `.SH`/`.SZ`/`.BJ` 后缀。该端点仅校验本地目录和映射、不会联网或写缓存，返回
与 `GET /industries` 相同的精确范围目录和覆盖证据。无效或模糊代码以 422 拒绝，
而非猜测分类。

#### POST /industries/refresh — 刷新行业目录及股票映射

权限：`data:update`

查询参数 `pool_id` 与请求体 `{ "codes": [...] }` 必须二选一，可选
`classification=cninfo_008001`。显式访问巨潮行业目录及证券行业变更记录，并以原子
方式更新缓存；读取、重试和刷新三条路径严格分离。

#### GET /industries/map — 代码→行业映射（摘要）

权限：`data:read`

返回前 10 条已缓存样本；缓存缺失或校验失败时返回 503，不隐式联网。

#### GET /industries/map/full — 代码→行业映射（完整）

权限：`data:read`

### 点时证券主数据

这组接口管理证券、指数成分和行业分类的有效期证据。详细存储与适配契约见
`docs/POINT_IN_TIME_MASTER.md`。现有巨潮行业缓存属于当前分类缓存，不会自动导入
为历史证据。

#### POST /api/data/point-in-time/imports

权限：`admin:users`（管理员专属）。原子导入一个
`point-in-time-master-import/v1` 批次。`domain` 为 `security`、
`index_membership` 或 `industry`；每条记录必须声明 `effective_from` /
`effective_to`。来源身份、取回时间、原始内容 SHA-256 和证据等级均为必填。
该操作是受信研究数据管理员对来源身份和证据等级的人工 attestation；SHA-256
只固定上游载荷身份，不代表平台独立认证了上游真实性。普通 `data:update`
operator 不能导入或提升证据等级。

`source.provider=csindex_official` 不允许使用这个通用接口直传，返回
`pit_evidence_governance_required`；必须走下面的原始证据上传、staging、批准和
受管导入链。底层 `PointInTimeMasterStore` 同样执行治理授权检查，内部调用也不能
靠跳过 HTTP 接口绕过。

`current_snapshot` 只能声明取回日的一天，不能覆盖历史区间；
`effective_dated_history` 才能参与历史研究。canonical CSI 四池还额外拒绝把
current anchor 写入生产 interval ledger；它只能留在下述治理 staging/quarantine，
不能 activation。重复的同内容请求幂等返回原批次，
同一 domain/scope/security 的区间重叠返回 409。响应不包含数据库或文件路径。

#### POST /api/data/point-in-time/governance/artifacts

权限：`admin:users`。一次上传一个已固定的中证官网 artifact，字段包括 role、
官方 HTTPS URL、`retrieved_at`、SHA-256 和 `payload_base64`；公告/附件还带
announcement ID，归档页同时带 POST request 原文及摘要。响应只返回内容摘要、
大小和幂等状态。响应原文最大 25 MiB，request 原文最大 64 KiB。

#### POST /api/data/point-in-time/governance/packages

权限：`admin:users`。持久化一个 `csindex-pit-staging/v2` 包。包引用的全部
artifact 必须已经逐个上传且读取复验通过。初始状态固定为 `pending`；staging
JSON 最大 20 MiB。`package_kind=current_anchor_observation` 只用于保存单日观察证据，
批准和导入恒定拒绝；生产包必须是 `historical_replay`，所有 imports 必须为
`effective_dated_history` 且覆盖至少两个日期。

生产 historical package 还必须引用受管采集工作流记录的
`authoritative-trading-calendar/v2` 和 `csindex-pit-review-decisions/v2` 原始载荷。
日历 evidence level 只接受 `licensed` 或 `exchange_authoritative`，且正文 Ed25519
签名必须命中服务端 `PIT_CALENDAR_TRUSTED_KEYS_JSON` 的 key/provider/level 精确
注册；显式人工日期列表或自报 provider 只能 staging。复核文件必须为 archive 中
每个绑定 row hash 的行给出 disposition
和理由，并为每个 `target_adjustment` 接受由详情、全部附件及严格解析结果生成的
精确 proposal hash。全局“已复核”声明、缺行列表或任意自填 proposal hash 都不能
通过 staging 重放；明显自动调样候选也不能借直接 package API 标为 not-target。

#### POST /api/data/point-in-time/governance/auxiliary-artifacts

权限：`admin:users`。由已认证主体逐份登记 calendar 或 review 原始载荷，请求字段为
`kind=trading_calendar|review_decisions`、`content_sha256` 和 `payload_base64`。calendar
必须通过已配置 Ed25519 信任锚验签；review v2 的 `reviewer.user_id` 必须等于当前
actor。kind、签名/审核身份与 provenance 摘要写入不可变表，同一内容摘要不能换
kind 或来源身份。package 构建者不能代替 reviewer 首次登记审核文件。

生产包的 reviewer、package stager、approver 强制为三个不同平台用户；同一主体
自审、自构、自批均返回 409。拒绝决定不受该职责分离约束，仍可用于快速封禁坏包。

#### GET /api/data/point-in-time/governance/packages/:id

权限：`admin:users`。返回 package digest、状态、revision、决定原因和逐 scope
导入回执，不返回原始字节、存储路径或数据库路径。

#### GET /api/data/point-in-time/governance/packages/:id/events

权限：`admin:users`。按追加顺序返回 staging、批准/拒绝和导入审计事件，并在返回
前复验每个事件的 SHA-256。响应不包含原始 artifact 字节或本机路径。

#### POST /api/data/point-in-time/governance/packages/:id/decision

权限：`admin:users`。请求包含 `expected_revision`、`decision`
（`approved`/`rejected`）和必填 reason。批准还必须携带严格的
`pit-evidence-attestation/v1`：

```json
{
  "schema_version": "pit-evidence-attestation/v1",
  "all_adjustment_rows_reviewed": true,
  "archive_completeness_reviewed": true,
  "source_terms_acknowledged": true,
  "local_research_only": true,
  "redistribution_not_authorized": true
}
```

字段不可省略或扩展，五个声明必须为 JSON boolean `true`；reason 文本不能替代
声明。拒绝不携带 attestations。仅 `pending` 可决定；revision CAS 失败返回 409，
批准和拒绝都不可撤销。`source_terms_acknowledged` 记录的是管理员完成条款确认，
不代表平台作出法律许可结论。`all_adjustment_rows_reviewed` 是批准人的流程声明，
不能替代 package 已绑定且通过重放的 v2 逐行复核 artifact。

#### POST /api/data/point-in-time/governance/packages/:id/import

权限：`admin:users`。仅导入已批准包。操作会重新校验包体、parser version、所有
artifact/request 原始字节摘要、持久化的完整批准声明和所有 import 文档。逐 scope
幂等，崩溃后可以安全重试；重放还会重新计算每个目标事件 proposal hash，并要求
四份 durable receipt 全成后才原子 activation。拒绝包、当前锚点包、非权威日历、
逐行复核缺失、声明缺失、证据缺失或篡改均返回 409。

#### GET /api/data/point-in-time/as-of

权限：`data:read`。参数为 `domain`、`scope_id`、`date`，可重复传
`security_code`。只有有效期历史、研究级来源和完整性摘要均通过时才返回该日记录；
仅有当前快照时显式返回
`current_snapshot_not_valid_for_historical_research`。

#### GET /api/data/point-in-time/coverage

权限：`data:read`。参数为 `pool_id`、`start`、`end`、可重复的
`security_code` 和可选 `industry_scope`。分别返回历史成分、证券主数据和行业有效期
覆盖，并给出 `neutralization_ready`。缺口保持 unavailable，不回退到今日分类。

### 原始价与复权价双账本

#### GET /api/data/price-ledger/import-contract

返回 `dual-price-ledger-import/v1` 的 raw 执行价、hfq 研究价、公司行为证据、
不可变身份和质量门禁。需要 `data:read`。

#### POST /api/data/price-ledger/imports

仅管理员可原子构建完整双价格账本。raw 和 hfq 必须覆盖相同 code/date，存储按
code/date/provider/dataset/version/adjustment 建立跨 scope 的规范身份；相同内容
复用，不同内容原子冲突并返回 409 及脱敏结构化证据。
同步请求每个价格角色最多 20,000 行；大规模 PIT 并集使用 checkpointed CLI。
直接 API 只允许 `declared` 暂存证据；`public_cross_validated`、`licensed` 与
`exchange_authoritative` 在价格 artifact、复核决定和 receipt 存储全部落地前均返回
`409 price_evidence_governance_required`。接口不访问网络，也不返回存储路径。

#### GET /api/data/price-ledger/readiness

需要 `scope_id`、`start`、`end`，可重复传入 `security_code`。分别返回
`descriptive_return_research_ready`（兼容别名 `ready_for_return_research`）、
`ready_for_adjusted_price_return_research`、
`ready_for_unbiased_return_research`、`ready_for_unbiased_research`、
`ready_for_execution_simulation` 与 `ready_for_real_tuning`。描述性字段只表示调整
收益可研究，禁止用于 promotion。两个无偏字段采用同一严格定义：精确 PIT/runtime
binding、完整 member-session、双时态 as-known-at 可得时间、权威逐日
tradability/status、公司行为验证、可信 raw/hfq 双账本且复权变化无未解释风险。低等级
停牌声明、仅有 effective time 或 legacy return fallback 永远不能使其
为真。账本单独查询不能证明 PIT 集合，所以无偏字段固定为 false；只有绑定实际运行
输入的上层 readiness 才能组合为 true。旧单口径缓存明确为 `ledger_unavailable`。

#### GET /api/data/price-ledger/cross-scope-audit

仅管理员可用。需要 `start`、`end`，可重复传 `security_code`。单并发、60 秒同参
缓存，只读核验已入账证据的跨 scope 规范身份，分开报告绝对价、收益、OHLC 几何及
hfq 常数锚冲突；不修改旧数据。

#### GET /api/data/price-ledger/legacy-cache-audit

仅管理员可用。需要 `start`、`end`，可重复传 `scope_id` 和 `security_code`。
单并发、60 秒同参缓存，只读扫描当前池级
Parquet，报告 schema-3、qfq、混合口径、非正价格及跨池重叠冲突。响应仅含业务标识
和摘要哈希，不暴露文件路径，也不会自动重写旧缓存。即使审计无冲突，也只返回
`descriptive_return_consistency`；`ready_for_unbiased_return_research` 和
`ready_for_unbiased_research` 固定为 false。

#### GET /api/data/price-ledger/prices

除范围参数外必须显式指定 `role=raw_execution|research_adjusted`；撮合消费者只能
读取 raw 角色。详细契约见 `docs/DUAL_PRICE_LEDGER.md`。

### 因子研究

#### GET /api/factor-research/protocols

权限：`data:read`。返回当前用户的研究协议系列、全部不可变版本、状态、摘要和被
本人因子运行引用的次数。管理员也遵守本人隔离，不越权读取其他用户协议。

#### POST /api/factor-research/protocols

权限：`experiments:create`。创建 v1 草稿。请求固化研究问题、可证伪假设、因子集合、数据
版本策略、精确窗口、周期/分组/调仓/成本/中性化口径、RankIC/多空接受阈值和策略
导出规则。未知字段被拒绝。协议载荷即使处于草稿也不可更新；修改必须创建新版本。

#### POST /api/factor-research/protocols/{protocol_id}/versions

权限：`experiments:create`。以 `expected_current_version` 乐观并发创建新草稿版本。相同
载荷摘要不能重复创建，旧版本不会被覆盖。

#### POST /api/factor-research/protocols/{protocol_id}/versions/{version}/lock

权限：`experiments:create`。提交审查过的 `payload_digest` 后锁定版本。只有锁定版本能作为
`POST /jobs` 或 `/analyze` 的 `protocol` 引用。执行前再次校验所有者、版本、摘要
及因子/池/窗口/成本等配置；偏离时 fail closed。固定数据摘要策略还会在计算后、
保存证据前核验实际 dataset digest。

带协议的运行结果保存 `protocol_review`，逐项记录实际值、预注册门槛和通过状态。
策略导出会执行协议中的 `allow_strategy_export`、全部阈值、最少证据数和数据摘要
一致性规则。审查不自动发布策略。

#### GET /api/factor-research/catalog

权限：`data:read`。返回全部代码注册版本（默认含已弃用版本），包括不可变
`version`/`definition_digest`、拒绝未知字段的 `parameter_schema`、
`required_fields`、依赖、`supersedes`、生命周期状态及并发 `revision`。
`include_deprecated=false` 只返回可创建新研究的版本。新因子只能由后端
`register_factor` decorator 随受审代码注册；目录治理接口不接收 Python、表达式
或自定义计算逻辑。`current=true` 标识当前代码默认版本；即使历史版本仍为
`published`，新研究也只使用当前版本，避免版本选择与实际 builder 不一致。

#### POST /api/factor-research/catalog/{factor_id}/versions/{version}/publish

#### POST /api/factor-research/catalog/{factor_id}/versions/{version}/deprecate

权限：`admin:users`（管理员天然具备）。请求必须提供精确
`definition_digest`、`expected_revision` 和 `idempotency_key`。仅能改变已随代码
交付清单的生命周期；定义本体不可覆盖。并发修订冲突返回 409，操作写入追加式审计。
弃用版本不再接受新研究，但旧运行仍通过其保存的定义快照解析。

#### GET /api/factor-research/readiness

权限：`data:read`。只读检查命名池和安全格式的 `custom_<digest>` 缓存，返回每个池
的 schema、可信来源等级、提供方、可用日期、股票数、字段、可运行因子和
`disabled_reason`；不返回绝对路径，也不会刷新或联网。只有 schema-v4 且来源证据
达到研究级别的缓存会标记 `ready=true`。
每个股票池还返回 `point_in_time`、`ready_for_unbiased_research` 与
`neutralization_ready`。行情可用于普通收益研究不代表历史成分与行业已就绪；因子
页会解释行业中性化被阻断的具体有效期缺口。
`neutralization.modes` 分别报告 `none`、`industry`、`size` 和
`industry+size` 的可用性。规模模式只认可 `float_market_cap` 或 `market_cap`
字段，且缓存 provenance 必须包含 `point-in-time-field-provenance/v1` 字段级
证据；缺字段、缺证据或可用时点不明确时均为 unavailable，不以收盘价或今日市值
代替。

#### POST /api/factor-research/jobs

权限：`data:read`。校验完整研究请求后提交 `factor_research` 后台任务，返回
HTTP 202，响应
`{ "data": { "job_id": "...", "status": "pending" } }`。任务由 SQLite
JobBroker 持久化，服务重启后会重放未完成任务；任务使用两个调度槽并与回测、行情
更新和模拟等任务互斥，防止 8GB 机器出现内存争用。Worker 只使用任务记录的
`user_id` 作为所有者，不信任 params 中的身份字段。

任务仅使用本地已激活 PIT 成分、精确规范价格绑定和权威日历，创建 job 前与 worker
启动后各校验一次；任一缺失返回 409，且不落 `factor_research_runs`。任务计算
IC/RankIC、因子衰减和分层收益；单次最多 500 只
股票、十年窗口，`horizons` 最多 12 个且每个在 1–252。成功后才写入
`experiment.db` 的 `factor_research_runs` 不可变记录，并固化因子版本、定义摘要
和完整定义 JSON。Job 的完成 result 只包含
`run_id`、`dataset_digest` 和 `result_digest`；运行详情另含 RFC3339 UTC
`created_at`。同一 Job 在崩溃恢复后使用 `source_job_uuid` 幂等复用已提交证据，
避免“证据已落库、任务状态未提交”窗口产生重复运行。
失败 result 使用 `error_code` 和安全消息，不包含本机路径。取消在数据加载、摘要和
持久化边界即时生效；pandas CPU 线程不能安全中断，因此该阶段会延迟取消，但取消后
不会保存半成品研究结论。

请求保持向后兼容，并可增加以下实施与多因子参数：

- `related_factor_ids`：同窗研究因子，最多 5 个且不能与主因子重复；
- `rebalance_interval`：1–252 个交易日，默认 5；
- `default_cost_bps`：默认 10 bps；`cost_scenarios_bps` 默认
  `[0, 5, 10, 20]`，最多 8 档且必须包含默认费率；
- `capacity_participation_rates`：默认 `[0.01, 0.05, 0.1]`，每档须在
  `(0, 0.25]`；
- `orthogonalize`：是否按因子 ID 字典序执行逐日截面 OLS 正交化；
- `combination_weights`：可选非负有界权重，键必须完整覆盖所选因子且权重和为 1；
  省略时使用确定性等权。
- `stability`：可选的预注册三段样本外配置。`mode` 固定为
  `fixed_three_way`，必须显式给出严格不重叠且有序的 `train`、`validation`、
  `locked` 日期，并在提交前设置 `locked_declared=true`。训练窗至少需要 252 个
  实际交易日，验证窗和锁定窗各至少 63 个；数据不足直接拒绝。另可声明
  `hypotheses_tested=1..10000`、`alpha` 和 `correction=bonferroni`。
  除原始交易日门槛外，主周期可评估 RankIC 日期在训练窗至少为 126 个、验证窗和
  锁定窗各至少为 42 个；因子值或前瞻收益不足同样 fail-closed。
- `neutralization`：`none`（默认）、`industry`、`size` 或
  `industry+size`；行业模式按每个交易日独立查询指定 `industry_scope` 的
  effective-dated PIT 行业，规模模式使用 `size_field=auto|float_market_cap|
  market_cap`。不可用模式在排队前返回 422，不创建后台任务。
- `protocol`：可选 `{protocol_id, version, payload_digest}`。只接受当前用户已锁定
  版本；协议及阈值审查写入不可变运行证据。

`factor-research/v4` 结果保留 v3 的 `implementation` 与 `multi_factor`。前者保存毛/净
分层收益、多档费率敏感性、单边调仓换手、可评估/可交易覆盖与成交额参与率容量。
缓存没有 `amount` 时容量结构化返回 `unavailable/amount_field_missing`，绝不填充
估算值；成交额不完整时返回 `partial`。后者按同一日期和股票代码计算 Pearson 与
Spearman 相关，正交化只使用请求窗口，保存固定次序、变换步骤、输入与组合分值
SHA-256。组合约束为 `[0,1]`、权重和 1、无做空，并始终返回
`publication.status=not_published`，不会自动注册或发布策略。旧 v2 运行仍可读取，
前端以明确空态提示重新运行。

当请求包含 `stability` 时，v4 还保存不可变的 `factor-stability/v1` 证据。每个
窗口先按自身结束日截断价格，再独立生成前瞻收益、IC/RankIC、ICIR、胜率、分层
多空、衰减和覆盖率；日度指标不跨窗合并。结果保存 Bonferroni 校正前后的近似
p 值、检验总数和锁定窗相对验证窗变化，并明确提示：统计显著性不能证明因子
有效、可交易或未来仍有效。未提交 `stability` 的旧请求保持兼容，旧运行在前端
显示明确空态。

请求中性化时，结果还包含 `neutralization`：逐日同截面 OLS 的前后行业/对数市值
暴露、R²、样本数、覆盖率、排除原因和输入来源摘要。回归不跨日期拟合；暴露缺口、
样本不足或秩亏日期不会进入后续 IC/分层收益。请求、来源摘要、诊断和结果均纳入
不可变运行及 SHA-256。JSON 导出自动包含完整结构；CSV ZIP 另含
`neutralization_summary.csv`、`neutralization_daily.csv` 和
`neutralization_inputs.json`。未请求该功能的旧运行仍按 `none` 兼容显示。

通过通用 `GET /api/jobs/{job_id}`、`DELETE /api/jobs/{job_id}` 和
`POST /api/jobs/{job_id}/retry` 查询、取消和重试；WebSocket `/ws/jobs` 只作为
刷新通知，REST 快照仍是事实来源。因子页使用
`GET /api/jobs/?job_type=factor_research&mine=true`，使管理员界面也只恢复本人
研究任务，避免把其他用户任务误绑定到当前用户的研究历史。

#### POST /api/factor-research/analyze

兼容旧客户端的同步接口，权限和研究证据边界与后台任务一致。新前端使用 `/jobs`，
以避免 HTTP 超时和页面刷新丢失状态。旧缓存返回 409 `factor_cache_legacy`，缺失
或来源不可信返回结构化 422。

#### GET /api/factor-research/runs

权限：`data:read`。返回当前用户的研究运行稳定分页：
`{items,total,page,page_size}`。查询参数：

- `page>=1`、`page_size=1..200`；
- `factor_id` 精确筛选，`query` 按运行 ID 或因子 ID 搜索；
- `sort=newest|oldest|factor|horizon`，每种顺序均以创建时间和运行 ID 作稳定
  tie-break；
- `include_archived=true` 可包含逻辑归档证据，默认仅返回未归档运行。

筛选、计数、排序与分页均在 SQLite 内完成，并始终先约束当前用户；摘要响应不返回
大型结果 JSON，但仍逐条验证请求、结果和运行摘要。旧版仅返回数组及 `limit` 参数的
契约已移除。

#### GET /api/factor-research/runs/{run_id}

权限：`data:read`。返回当前用户的一条不可变运行及完整结果；其他用户的运行按
404 处理，避免泄露存在性。详情明确返回 `request_digest`、`dataset_digest`、
`result_digest`、`run_digest`、`source_job_uuid`、因子定义版本及数据载荷中的
缓存身份/来源证据版本，供前端完整展示复现链。

#### GET /api/factor-research/runs/{run_id}/export

权限：`data:read`。查询参数 `format=json|csv`，默认 JSON。仅导出当前用户已完成的
因子研究运行；管理员沿用因子研究既有的本人隔离语义，不越权读取其他用户运行。
未知与越权运行返回相同 404。逻辑归档运行仍可导出，并在证据中显式标记
`archived=true` 与归档时间。

JSON 使用流式响应；`format=csv` 返回内存有界的多表 ZIP，含请求、因子定义与定义
摘要、数据覆盖/来源/摘要、IC 与 RankIC 汇总和时序、衰减、分层收益、预处理、
成本/容量（运行结果存在时）、局限及可复验 manifest。响应设置
`Cache-Control: no-store`、`X-Content-Type-Options: nosniff` 和安全下载文件名。
CSV 单元格阻断公式注入；凭据、token、本机绝对路径和异常堆栈会被脱敏。
带预注册协议的运行另含 `protocol_review.csv`、`protocol_thresholds.csv` 和
`protocol_export_rules.json`。

导出前重新计算请求、结果和运行 SHA-256，并验证数据摘要与运行元数据。运行摘要
覆盖 run ID、所有者、因子、请求/数据/结果摘要、schema、创建时间和来源 Job，
但不覆盖允许变化的逻辑归档时间。摘要不匹配、持久化载荷超限或关联 Job 尚未完成
时 fail closed，不会返回部分证据。

#### DELETE /api/factor-research/runs/{run_id}

权限：`data:read`。对当前用户运行执行逻辑归档，只更新 `archived_at`，不物理删除
请求、结果和摘要证据。

#### POST /api/factor-research/compare

权限：`data:read`。请求 `{ "run_ids": ["frun_...", "frun_..."] }`（2–20 条），
返回 RankIC 均值/IR/胜率、多空收益、分层单调性以及
`dataset_consistent` 提示。只允许比较当前用户运行。

#### POST /api/factor-research/export-strategy

权限：`strategies:scan`。将白名单因子及权重导出为数据定义策略并注册到策略池。
`research_run_ids` 必填（1–20 条）。服务器在同一 SQLite 写事务中重新验证运行归属、
因子匹配、数据摘要、结果摘要和因子定义摘要，再创建不可变策略版本、切换当前版本
指针并写入追加式审计。可选 `idempotency_key`；旧客户端不传时服务器从规范化请求
生成稳定幂等键。发布新版本须同时提供 `strategy_id` 和 `expected_version`，并发
冲突返回 409。定义始终为纯 JSON，未知请求字段被拒绝。

历史 JSON 策略仍可运行，但标记 `legacy_unbound`，不能加入受治理版本链、晋级或
回滚；需要先用已完成研究运行重新导出。

#### GET /api/factor-research/strategies/{strategy_id}/versions

权限：`strategies:scan`。仅返回当前用户的策略版本链、当前版本、修订号和各版本
绑定的研究证据。其他用户或 `legacy_unbound` 策略统一按不存在处理。

#### POST /api/factor-research/strategies/{strategy_id}/rollback

权限：`strategies:scan`。以 `target_version`、`expected_version` 和
`idempotency_key` 原子切回已有版本。回滚只移动当前版本指针，不更新或删除历史
定义、运行证据及审计事件。

#### GET /api/factor-research/governance/audit

权限：`admin:users`。返回最多 500 条因子发布、弃用、策略发布和回滚的追加式审计
事件，不包含本机路径或凭据。

### 行情数据

#### GET /stocks/{code} — 单股数据

权限：`data:read`

查询参数：`start`? / `end`? / `resolution` (daily\|weekly\|monthly)

数据源优先级：Parquet 缓存 → JSON 兼容缓存 → AKShare 拉取。日线记录包含 `open/high/low/close/volume/amount`。

#### GET /stocks/batch — 批量数据（Pivot）

权限：`data:read`

查询参数：`codes` (必填, 逗号分隔) / `start`? / `end`? / `only_close` (默认 true)

最多 100 个代码。`only_close=true` 返回每个代码的收盘价列；设为 `false` 时列名为 `代码.字段`，字段包括 OHLCV 和成交额。

### 交易日历

#### GET /calendar — 交易日列表

权限：`data:read`

查询参数：`start` (默认 2020-01-01) / `end`? (默认今天)

#### GET /calendar/check/{date} — 交易日检查

权限：`data:read`

返回 `is_trading_day`、`next_trading_day`、`prev_trading_day`。

### 数据更新

#### POST /update — 触发更新

权限：`data:update`

查询参数：`pool_id`?（不传则更新全部）

只接受 CSI 300/500/800/1000（不传表示四池原子治理范围）。任务遍历中证官方完整
公告归档并写入内容寻址 evidence、checkpoint、coverage report 和 review queue；
响应状态为 `pending_review`，且
`automatic_approval_permitted/production_import_performed/activation_performed/
runtime_data_changed` 均为 false。它不会更新普通 Parquet、不会自动导入或激活，
也不会把 current anchor、自动解析提案或 quarantine 包提升为可运行数据。
无人值守调度必须配置 `PIT_AUTOMATION_ACTOR_USER_ID`，默认 0 时 fail-closed。
完整契约见 `docs/PIT_ONLY_DATA_POLICY.md`。

#### GET /update/status — 更新状态

权限：`data:read`

返回任务队列状态 + 各池缓存信息。

#### GET /cache/status — 严格缓存就绪状态

权限：`data:read`

查询参数：`pool_id`（必填，只允许 1–64 位字母、数字、下划线和连字符）。

只读加载并验证 schema 4、来源证据、Parquet 内容绑定及 OHLCV 价格域，不会访问
外部数据源。
响应包含实际日期/股票/字段覆盖、复权口径、代码集 SHA-256、来源证据等级、
帧摘要、完整覆盖标志、有限/正价格及 OHLC 逻辑计数、价格账本用途和时点股票池
风险；不返回本地文件路径、完整来源证据或异常内部信息。缺失、旧版、来源无效和
价格质量无效分别返回稳定错误码及 `recommended_action`。当前公共研究刷新明确
使用 `hfq` 调整价账本；它适合收益研究但不等同 raw 成交价，因而
`ready_for_execution_simulation=false`。完整 raw/adjusted 双账本仍是后续门禁。

#### POST /cache/invalidate — 失效缓存

权限：`data:update`

查询参数：`pool_id` (必填)

---

## AI 分析 (AI)

前缀：`/api/ai`

权限要求：所有接口需要 `ai:use`

依赖环境变量 `DEEPSEEK_API_KEY`，未配置时返回 503。

所有端点通过统一 `AiService` 调用。缓存键包含端点、规范化输入和数据版本上下文，
默认 TTL 为 24 小时；每次调用（含缓存命中和失败）都会写入 `ai_usage`。
成功响应均包含：

```json
{
  "cached": false,
  "model": "deepseek-chat",
  "usage": {
    "prompt_tokens": 120,
    "completion_tokens": 80,
    "total_tokens": 200,
    "latency_ms": 842.5
  }
}
```

### POST /analyze-backtest — 回测分析

请求体：
```json
{
  "experiment_id": 42
}
```

AI 对已完成实验的 12 项关键指标进行专业解读。

### POST /suggest-params — 调参建议

请求体：
```json
{
  "strategy_id": "ma_cross_v1",
  "current_params": { "fast_period": 20 }
}
```

AI 结合策略参数定义、当前值和历史最佳实验提出调优建议。返回值经过严格 JSON、
参数名、类型、枚举和边界校验：

```json
{
  "suggestions": [
    {
      "param_name": "fast_period",
      "current_value": 20,
      "suggested_value": 15,
      "reason": "缩短短周期以提高响应速度"
    }
  ]
}
```

### POST /market-insight — 市场解读

请求体：
```json
{
  "portfolio_id": 1
}
```

AI 分析组合近期表现、真实最新持仓的行业市值暴露和风险。

### POST /diagnose-error — 错误诊断

请求体：
```json
{
  "experiment_id": 42,
  "error_log": "ValueError: ..."
}
```

AI 诊断失败原因并给出修复建议。诊断结果自动保存到实验的 `ai_diagnosis` 字段，
并返回结构化结果：

```json
{
  "structured": {
    "category": "strategy_code",
    "root_cause": "策略输出字段不符合协议",
    "evidence": "traceback 与策略源码位置",
    "fix_suggestion": "修正信号返回结构",
    "auto_fixable": true
  }
}
```

`category` 仅允许 `strategy_interface / strategy_code / data / params /
environment / unknown`；只有前两类允许 `auto_fixable=true`。当前版本不会自动改代码。

### POST /explain-signal — 信号解释

请求体：
```json
{
  "strategy_id": "ma_cross_v1",
  "signal": { "code": "000001.SZ", "action": "BUY", "score": 0.85, "confidence": 0.9 },
  "context": {}
}
```

---

## 管理 (Admin)

前缀：`/api/admin`

权限要求：所有接口需要 `admin:users`

### GET /users — 用户列表

返回所有用户（含角色、权限数）。

### POST /users — 创建用户

请求体：
```json
{
  "username": "string",
  "password": "string",
  "display_name": "string | null",
  "email": "string | null",
  "is_admin": false
}
```

非 admin 用户自动分配只读权限。

### PUT /users/{user_id}/permissions — 更新权限

请求体：
```json
{
  "permissions": ["experiments:read", "experiments:create", "trading:read"]
}
```

全量替换权限。admin 用户跳过（自动拥有所有权限）。

### PUT /users/{user_id}/status — 启用或停用用户

请求体：
```json
{ "is_active": false }
```

停用后登录、访问令牌、刷新令牌均不可继续使用。

### DELETE /users/{user_id} — 安全停用用户

不能停用自己。此操作保留审计关联，撤销显式权限和会话，不物理删除跨库业务记录。

### GET /permissions — 可用权限列表

返回系统中所有权限定义（14 种）。

---

## 研究工作流与策略晋级

接口前缀：`/api/research/workflows`

研究工作流采用
`Hypothesis → ExperimentGroup → Trial → Report → Promotion`。晋级审批需要
`experiments:promote` 权限，审批只产生研究审计记录，不会自动部署或发送订单。
该状态机是可选高级研究治理；个人模拟部署不要求创建 promotion，未审批状态只进入
paper 风险快照，未来实盘仍保持硬阻断。

`POST /promotions/{promotion_id}/transition` 在目标状态为 `approved` 时重新读取
锁定测试实验及不可变证据。训练型策略除 RunManifest、点时股票池、数据质量、
执行配置和预注册指标外，还必须具有唯一的 latest 模型产物，以及
`trained-model-promotion-evidence/v2` 不可变补充清单。清单绑定所有者、实验、
策略、规范化参数、训练/验证/测试窗口、标签周期、embargo、样本数、有限的
validation 指标、门槛结论、模型 SHA-256/大小和 RunManifest hash；审批仅校验
文件字节，不反序列化模型。

训练证据失败以 `promotion_gate_blocked` 返回，并在 `blockers` 中提供结构化
`code / field / message / expected / actual`。常见 code：

- `ml_artifact_missing`、`ml_artifact_latest_not_unique`：没有唯一 latest 模型。
- `ml_artifact_unverified_legacy`、`ml_artifact_integrity_failed`：旧产物无 hash/
  大小，或模型/元数据文件校验失败。
- `ml_evidence_legacy_or_missing`、`ml_evidence_identity_mismatch`：缺少不可变
  validation 补充清单，或所有者、参数、实验、策略、RunManifest 绑定不一致。
- `ml_training_not_accepted`、`ml_training_attempt_failed`：训练失败或候选被拒绝。
- `ml_model_fallback_disallowed`、`ml_model_implementation_unverified`：实际训练
  backend 为 sklearn fallback，或模型类型与策略声明的原生实现状态不一致。即使
  validation 指标有限且达标也禁止晋级。
- `ml_validation_window_missing`、`ml_validation_window_invalid`、
  `ml_validation_gate_failed`：验证窗口、样本、有限指标或预注册门槛不合格。
- `ml_contract_noncompliant`：策略未使用平台 `TrainableStrategy` 合约。当前
  `alphamaster_gbr_v1` 使用 legacy self-walk-forward，明确禁止晋级为研究批准模型。

远程训练上传产物在隔离区中，不进入 `model_artifacts`，因此不能成为晋级证据。
精确重跑会为新实验生成并绑定自己的 RunManifest 与模型补充清单，不复用来源行。
非训练策略不要求模型证据。

---

## 任务 (Jobs)

前缀：`/api/jobs`

权限：登录即可（非 admin 仅看自己的任务）

### GET / — 任务列表

查询参数：

- `status`：`pending | running | cancel_requested | completed | failed | cancelled`
- `job_type`：`backtest | daily_simulation | simulation_backfill | data_update | retrain`
- `page`、`page_size`：分页参数，`page_size` 最大为 100

返回 `items / total / page / page_size`。任务包含显示名称、关联资源、队列位置、
当前阶段、进度说明、重试次数和 Worker 信息。

### GET /summary — 任务汇总

返回各状态数量、当前活动任务数及 Worker 在线状态。`worker` 同时包含：

- `capacity / configured_max / running_slots`：当前可用槽、硬配置上限和已占槽；
- `degraded / reasons`：是否降容及机器可读原因（例如
  `memory_available_low`、`cpu_load_high`、`scale_up_warmup`）；
- `metrics`：CPU 核心、1 分钟负载、可用内存、内存占用、Swap 与采样来源；
- `leader`、心跳时间及
  `execution_mode=hybrid_spawn_factor_research`（因子 CPU 段独立 spawn
  子进程；训练和其他同步段仍是有界线程）。

当前容量默认按 8 GB M2 保守地在 1 和 2 之间调整。详细优先级、资源锁、租约
恢复和环境变量见 [本机动态负载调度](DYNAMIC_JOB_SCHEDULER.md)。

### `GET /api/jobs/observability`

管理员认证只读接口。`window_hours` 为 1–168，默认 24。返回
`operations-observability/v1`：

- `jobs.by_type`：各任务类型的成功/失败/取消率、排队和运行时长 P50/P95；
- `data_refresh.recent`：最近刷新阶段与股票批次进度；
- `cache_quality`：缓存质量、研究/执行可用数量聚合；
- `events`：WebSocket、重启和 SQLite contention 的低基数聚合；
- `slo.objectives`：`operations-slo/v1` 固定目标的目标值、实际值、样本数和
  达标/越界状态；
- `slo.alerting`：连续观测确认数、通知冷却时间、固定 objective 的当前状态，
  以及最近 20 条 `breach/recovery` 审计事件。冷却期内重复越界仍留审计记录，
  但 `notification_emitted=false`，不会重复输出告警日志；
- `worker`：CPU、内存、Swap、磁盘、I/O 与重任务准入预算。

非管理员返回 403。响应不会返回用户名、文件路径、Token、job UUID 或缓存键。
SLO 评估由调度器按 `JOB_SLO_EVALUATION_SECONDS` 周期执行，读取接口本身不会
写数据库。默认采用 24 小时观察窗、连续 2 次确认及 15 分钟同类通知冷却；
objective 名称是代码内固定集合，禁止把用户、路径或任务标识作为告警标签。

当管理员显式启用签名 HTTPS webhook 时，`slo.alerting.external_delivery` 仅返回
安全聚合的 outbox 状态、未确认 breach 数与确认升级时限；默认关闭且不发生网络投递。
完整 payload、验签、重试及故障演练见[外部 SLO 告警投递](EXTERNAL_SLO_ALERTS.md)。

### `POST /api/jobs/observability/alerts/{alert_id}/acknowledge`

管理员确认一个已成功投递的 breach。`alert_id` 只在已签名的受控 webhook payload 中
出现，聚合查询不会返回它。确认会阻止该 breach 的确认超时升级，但不会替代后续 SLO
恢复。未找到、已确认或尚未投递的 ID 返回 404。

### GET /{job_id} — 任务详情

返回任务状态、进度、脱敏参数、结果、错误及状态事件时间线。

### DELETE /{job_id} — 取消任务

仅可取消 `pending` / `running` 状态的任务。排队任务立即变为 `cancelled`；
运行中任务先变为 `cancel_requested`，在安全检查点停止。

### POST /{job_id}/retry — 重试任务

仅允许重试 `failed` / `cancelled` 任务。新任务记录 `parent_job_uuid` 和递增的
`attempt`，原任务历史保持不变。队列达到背压上限时返回 429。

---

## 远程模型训练

接口前缀：`/api/remote-training`

远程训练不会进入后端进程内任务队列。Web 用户创建任务后，Windows
客户端使用仅限该任务的一次性凭据下载不可变 Parquet 快照，在本机执行
`TrainableStrategy.prepare()`、`fit()` 和 `save_model()`，再把训练报告与
模型文件上传到隔离区。服务端不会自动反序列化远端上传的模型。

用户接口使用 JWT：

- `POST /tasks`：从当前用户已有实验创建单窗口远程训练任务，可选覆盖
  `train_start`、`train_end`；原始训练令牌只在本次响应中返回。
- `GET /tasks?experiment_id={id}`：查询当前用户的远程训练任务。
- `GET /tasks/{task_uuid}`：查询任务状态、进度、设备和训练报告。
- `POST /tasks/{task_uuid}/cancel`：取消尚未结束的任务。

Windows 客户端使用 `X-Training-Token`：

- `GET /tasks/{task_uuid}/bundle`：获取 `remote-training-bundle/v1` 清单。
- `GET /tasks/{task_uuid}/data`：下载经 SHA-256 固定的 Parquet 快照。
- `POST /tasks/{task_uuid}/start`：声明开始训练。
- `POST /tasks/{task_uuid}/progress`：上报 `progress` 和可选 `message`。
- `POST /tasks/{task_uuid}/complete`：上传 multipart 字段 `report_json`
  和 `artifact`。
- `POST /tasks/{task_uuid}/fail`：上报失败原因。

完整安装、Windows CUDA 检查和命令示例见 `docs/REMOTE_TRAINING.md`。

## WebSocket

端点前缀：`/ws`

所有 WebSocket 使用与 REST 相同的 access JWT 和 RBAC，但 bearer 凭据不得放入
URL。浏览器建立不含查询凭据的连接后，必须在 5 秒内发送第一条 JSON 数据帧：

```json
{"type": "authenticate", "token": "<access_token>"}
```

服务端完成 token 类型、用户有效状态、权限和资源 owner 检查后返回：

```json
{"type": "authenticated"}
```

随后才会把连接加入相应的通知/任务/训练/实时信号订阅。缺少、超时、无效或 refresh
token 使用关闭码 `4401`；权限或资源归属失败使用 `4403`。`?token=...`、
`?access_token=...` 和 `?authorization=...` 均被拒绝，避免 bearer JWT 进入
Uvicorn、反向代理、浏览器历史或监控系统的请求 URL。

### /ws/training/{experiment_id}

训练进度推送。ML 策略训练时实时推送 epoch 进度。

### /ws/realtime/{deployment_id}

实时行情信号推送。部署策略运行时的实时信号。

### /ws/notifications

系统通知推送。任务完成、错误等通知。

### /ws/jobs

后台任务状态增量推送。消息类型为 `job_updated`；客户端应先通过 REST 获取快照，
收到增量事件后刷新列表，并在 WebSocket 断线时降级轮询。

---

## 健康检查

### GET /api/health

无需认证。

响应：
```json
{
  "status": "ok",
  "version": "0.1.1",
  "commit": "298f0b6d3db8c7ec9df698e6fd15189f5d79a25f",
  "started_at": "2026-07-30T02:25:20+00:00"
}
```

`commit` 是运行进程启动时加载的 Git 提交；`started_at` 用于判断代码更新后
服务是否已经重启。无 Git 元数据的发布包可通过
`QUANT_PLATFORM_BUILD_COMMIT` 注入提交摘要，否则返回 `unknown`。

本地开发前端可使用 `http://localhost:5173` 或 `http://127.0.0.1:5173`；
默认 CORS 白名单同时包含这两个来源。

---

## 权限速查

| 权限 Key | 说明 | 影响接口 |
|----------|------|----------|
| `experiments:read` | 查看实验 | GET /experiments |
| `experiments:create` | 创建实验 | POST /experiments |
| `experiments:delete` | 删除实验 | DELETE /experiments |
| `experiments:sweep` | 参数扫描 | POST /experiments/sweep |
| `trading:read` | 查看交易 | GET /trading/* |
| `trading:deploy` | 部署策略 | POST/PUT/DELETE /trading/deployments |
| `trading:execute` | 执行模拟 | POST /trading/simulate/run |
| `trading:rebalance` | 再平衡 | POST/PUT /trading/portfolios |
| `data:read` | 查看数据 | GET /data/* |
| `data:update` | 更新数据 | POST /data/update |
| `strategies:read` | 查看策略 | GET /strategies |
| `strategies:scan` | 扫描策略 | POST /strategies/scan |
| `ai:use` | 使用AI | POST /ai/* |
| `admin:users` | 管理用户 | /api/admin/* |

# 运维网络与恢复边界（非 HTTP API）

备份、恢复、备份密钥和服务配置安装**不提供 HTTP endpoint**。即使管理员 JWT
泄露，远端请求也不能通过应用 API 下载 `.env`、生成数据库副本、提交恢复路径或
读取备份密钥。相关操作只能由本机固定路径、非 root 的
`backend.ops.disaster_recovery` wrapper 执行；恢复只允许空的隔离目录，不覆盖
生产。

生产反向代理不公开 `/docs`、`/redoc`、`/openapi.json`，`/api/admin/*` 还要求
Caddy 观察到的实际客户端地址属于私网，随后才进入原有 JWT、活跃用户和
`admin:users` 检查。普通 `/api/*` 与 `/ws/*` 合约不变。完整部署、密钥、audit、
恢复演练和剩余风险见
[`LOCAL_SECURITY_AND_RECOVERY.md`](LOCAL_SECURITY_AND_RECOVERY.md)。
