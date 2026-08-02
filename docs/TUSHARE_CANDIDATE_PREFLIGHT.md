# Tushare 候选数据自动获取与验证

## 定位与安全边界

`backend.data.sources.tushare_candidate` 是供应商字段 PoC 和自动预检，不是生产
PIT 导入器。成功响应仍固定标记为 `quarantine`，且 `promotion.eligible=false`。
它没有实现平台 `DataSource`，也没有调用 PIT master 的 import/activate 方法，因此
无法绕过既有审批和四 scope 原子激活门禁。

Tushare 提供结构化候选数据；中证官网公告和成分文件继续由
`csindex-pit-staging/v2` 治理链保存为权威事件证据。两者只能通过绑定中证 artifact
SHA-256 的 `tushare-csindex-anchor-comparison/v1` 做快照差异检查，不能用 Tushare
快照替代公告发布时间、调样生效日或历史修订证据。

## 已覆盖的候选端点

| 数据集 | API | 时间字段与当前限制 |
|---|---|---|
| 交易日探针 | `trade_cal` | 必须覆盖请求内每个自然日，且至少含一个供应商声明的开市日 |
| raw 日线/成交额 | `daily` | `trade_date` 为 effective；缺首次可得时间 |
| 复权因子 | `adj_factor` | `trade_date` 为 effective；不能单独证明公司行为完整 |
| 逐日规模 | `daily_basic` | 总/流通股本与市值；缺首次可得时间 |
| 证券/上市退市 | `stock_basic` | `list_date/delist_date`；当前记录不等于历史旧版本 |
| 名称/ST 变化候选 | `namechange` | `start/end_date`，`ann_date` 为供应商声明的 available 候选 |
| 停牌 | `suspend_d` | `trade_date`；空响应是“未观察到”，不是权威无事件证明 |
| 分红送配 | `dividend` | 记录/除权/支付等 effective 候选，公告/实施公告日为 available 候选 |
| 指数权重 | `index_weight` | 月度快照；预检自动扩到完整自然月以避免短窗假阴性 |
| 申万行业 | `index_classify/index_member_all` | 分类和成员候选；缺失历史时间时明确 declared |

每份响应保留：精确响应字节 SHA-256、无凭据请求参数、字段、行数、
`effective_at / available_at / ingested_at / revision` 声明及非空覆盖数。供应商没有
提供的 `available_at` 和 revision 明确写成 `declared_ingestion_time`、
`declared_observation`，不会伪造成供应商历史版本。

## 运行

令牌只放项目根目录 `.env`：

```env
TUSHARE_TOKEN=...
PIT_CANDIDATE_OUTBOUND_PROXY_URL=http://127.0.0.1:12001
```

`PIT_CANDIDATE_OUTBOUND_PROXY_URL` 是可选的 quarantine-only 出站代理。macOS
LaunchDaemon 不继承交互式用户的系统代理环境，因此后台自动任务若必须经代理访问，应
在根目录 `.env` 显式设置。适配器只接受 `http/https` 的 loopback 地址和有效端口；完整
URL 使用 `SecretStr`，即使包含代理认证信息也不会进入 job、artifact、report 或日志。
报告只显示 `explicit_proxy_configured` 和 `loopback_only`，不会显示主机、端口或认证。
不要把远端开放代理或供应商 token 拼进这个 URL。

执行有界预检（默认 11 次低速请求、单只证券、一个月内样本）：

```bash
.venv/bin/python scripts/preflight_tushare_candidate.py \
  --ts-code 000001.SZ \
  --start 2025-01-02 \
  --end 2025-01-10 \
  --index-code 000300.SH
```

脚本不接受命令行 token，不输出 token 或供应商错误正文。默认至少间隔 0.35 秒，
只对网络、429 和 5xx 做最多三次有界重试，不跟随重定向；权限和字段错误立即失败。
退出码 0 只表示必要候选端点能被采集和验证，不表示 `production_pit_ready`。

HTTP 200 不是数据覆盖证明。核心表执行以下 fail-closed 最小行数门禁：

| 数据集 | 最小覆盖 |
|---|---:|
| `trade_cal` | 请求范围内的自然日数，且开市日数至少 1 |
| `daily / adj_factor / daily_basic` | 各至少 1 行 |
| `stock_basic(list_status=L)` | 至少 1000 行 |
| `index_weight` | CSI300/500/800/1000 分别至少 300/500/800/1000 行 |

空表或不足表会保留原始 quarantine artifact，但数据集状态变为
`insufficient_rows`，整体 `candidate_collection_valid=false`。若窗口全为周末或休市日，
报告给出 `probe_window_has_no_open_trading_sessions` 并要求换取含开市日的窗口；事件类
空表只表示“未观察到事件”，不能作为权威无事件证明。

对 `index_weight`，报告还会给出 `index_weight_monthly_probe`：完整自然月返回零行时为
`no_monthly_snapshot_returned / provider_returned_empty_complete_month`，明确表示供应商未为该月
返回快照，不能被误诊为短日期窗口、指数零成分或 PIT 覆盖；完整月达到指数预期行数时仅为
`complete_monthly_snapshot_candidate`，仍然不是生产 PIT 证据。发布滞后、历史留存范围和账户
权限必须由供应商或其条款进一步确认。

自动窗口落在当前尚未结束的自然月时，权重探针会改用最近一个已经完整结束的月份，
不会为了“完整月”构造含未来日期的请求。最近完整月仍可能因供应商发布滞后返回空表；
该结果继续 fail-closed，并由稀疏契约探针区分“已观察到的最近完整候选月”和“其后的
首个空月”，但不会宣称探测间隔内的每个月已经连续覆盖。

### 四指数与 30 只证券稀疏契约探针

以下命令在最多 48 次请求内检查四个指数、稀疏历史月份、从四指数最新可用候选成分中
确定性抽取的 30 只证券、raw/复权因子/逐日规模、上市/退市/暂停列表、停牌以及最多
5 只证券的分红和名称/ST 变化：

```bash
.venv/bin/python scripts/probe_tushare_pit_contract.py
```

默认月份为 2016-01、2020-01、2023-01、2025-01 和最近两个已经结束的自然月；也可用
最多六个 `--month YYYY-MM` 显式指定。所有响应与汇总仍只写
`provider_candidates/tushare` 隔离区。命令成功仅表示这些**离散样点**和 30 只证券的
横截面可采集，不证明 2016 至今逐月/逐日连续，不证明 historical `available_at`、旧
revision 留存、官方事件一致性或许可，且始终返回 `production_pit_ready=false`。

原始候选默认保存到：

```text
data/pit_evidence/provider_candidates/tushare/
  artifacts/sha256/<prefix>/<response-sha256>
  manifests/sha256/<prefix>/<manifest-sha256>.json
  reports/sha256/<prefix>/<report-sha256>.json
```

文件按内容寻址、幂等写入、权限 0600，目录 0700。manifest 和 report 递归拒绝
`token/password/secret/api_key/authorization` 等凭据字段。篡改响应或 manifest 后离线
复验必定失败。存储初始化和每次读写都会从候选 evidence root 到
`artifacts|manifests|reports/sha256/<prefix>` 逐层使用 `lstat` 验证：符号链接、
非常规文件或无法收紧的权限立即 fail-closed；既有 0755 目录会被收紧为 0700，既有
内容对象会被收紧为 0600 后才可复用。不要手工修改该目录；这不是对父级工作目录或
供应商原始来源真实性的安全声明。

## 交叉验证与选择结果

日线对照按顺序尝试 BaoStock raw，再尝试 AKShare/Sina raw。对照只比较独立源的
重叠日收益，输出代码覆盖、日期差、冲突比例和样例；两个公共聚合源均不能提高
Tushare 的 PIT 权威等级。

可显式选择只抓取一个匹配指数的中证官网 current anchor，并把原始 XLS 内容寻址写入
既有 PIT evidence governance 后再比较：

```bash
.venv/bin/python scripts/preflight_tushare_candidate.py \
  --ts-code 000001.SZ \
  --start 2025-01-02 \
  --end 2025-01-10 \
  --index-code 000300.SH \
  --official-csindex-current-anchor \
  --official-actor-user-id 1
```

该选项只支持 CSI300/500/1000，每次只抓一个 current anchor，不采集公告历史，也不
创建、批准或导入生产 package。只有中证 XLS `observed_on` 与 Tushare 权重
`trade_date` 完全一致才计算集合差异；日期不同、多权重日期或时间字段异常统一返回
`not_comparable`，禁止把不同时点的成员差异误报为供应商冲突。

2026-08-02 的真实窄窗验证（`000001.SZ`，2025-01-02 至 2025-01-10）得到：

- `daily / adj_factor / daily_basic` 各 7 行；
- `dividend` 53 行、`namechange` 4 行、在市 `stock_basic` 5534 行；
- 申万 2021 一级分类 31 行；
- 将权重窗口自动扩展至完整 2025-01 后，沪深 300 权重 300 行；
- BaoStock 当次为 `ProviderOutageError`，自动改用 AKShare/Sina；6 个重叠收益点、
  0 个冲突，差异检查通过；
- 报告仍为 `production_pit_ready=false`。

因此当前个人候选链的顺序是：Tushare 作为结构化主候选，AKShare/Sina 作为可用的
行情异常检测源，BaoStock 保留为首选独立校验但需解决其当前网络可达性。中证官网
证据继续独立治理。任何供应商许可、available_at、官方事件或历史覆盖未通过时，
自动更新只能采集到 quarantine，不能 import/activate。

## 自动化验收

### 正式服务候选预检调度

候选预检有独立的 durable job 类型 `candidate_data_preflight`，由正式 FastAPI lifespan
启动的 `candidate-preflight-scheduler` 提交，再由与实验相同的租约式 job worker 执行。
它不调用 `pit_durable_update` 的 import/activate 阶段，也不实现 PIT master 写接口。

安全默认值是关闭自动调度：

```env
PIT_CANDIDATE_PREFLIGHT_AUTO_RUN=false
PIT_CANDIDATE_OUTBOUND_PROXY_URL=http://127.0.0.1:12001
PIT_CANDIDATE_PREFLIGHT_SCAN_MINUTES=360
PIT_CANDIDATE_PREFLIGHT_LOOKBACK_DAYS=14
PIT_CANDIDATE_PREFLIGHT_TS_CODE=000001.SZ
PIT_CANDIDATE_PREFLIGHT_INDEX_CODE=000300.SH
PIT_CANDIDATE_PREFLIGHT_CROSS_CHECK=true
```

启用前必须已有 `PIT_AUTOMATION_SERVICE_USER_ID`/`USERNAME` 对应的 active、non-admin、
仅具 `data:update` 的服务身份。调度器在每个 UTC 时间槽重新验证身份，只把代码、日期、
provider 和以下不可放宽的布尔门禁写入 `jobs.db`：

```json
{
  "quarantine_only": true,
  "production_import_permitted": false,
  "activation_permitted": false
}
```

`TUSHARE_TOKEN` 使用 `SecretStr` 从根目录 `.env` 载入；调度器提交阶段不读取它，也不会
把它传进 job params、resource ID、日志或报告。只有已领取 job 的 worker 在发出 HTTPS
请求前解析 secret。worker 在完成任务前再次断言 report 为 `classification=quarantine`、
`production_pit_ready=false`、`promotion.eligible=false`；任何可晋升结果会使任务失败。
显式代理同样只在 worker 构造候选客户端时解析；它不会进入 credential-free job params。

同一 UTC 时间槽使用固定 `idempotency_key`。broker 在一个事务内同时去重 active 和
terminal job，因此服务重启不会在同一槽重复请求；下一时间槽才产生新 observation。
自动窗口最多 31 天，默认只取前一日结束的 14 个自然日，仍受单次 32 请求硬上限和
Tushare 客户端限速约束。

#### 最近完整月尚无权重快照

自动预检不会为了制造“成功”而回退到更早月份。只有同时满足以下条件时，任务才以
`completed` 保存一次 **deferred observation**：

- 唯一 required failure 是 `index_weight`；
- 请求确实覆盖当前调度时间槽之前最近一个完整自然月；
- 完整月请求获得结构正确的空表，分类为 `no_monthly_snapshot_returned`；
- 其余 required dataset 与计划校验均通过。

此时 result 明确包含
`preflight_outcome=deferred_insufficient_coverage`、
`candidate_collection_valid=false`、`fresh_candidate_coverage=false`、
`observation_window_shifted=false`，stage 为 `candidate_preflight_deferred`。`completed`
只表示本次隔离观察和持久化动作完成，不表示最新数据可用；原因仍保守记录为
`publication_lag_retention_or_entitlement_unresolved`，不得猜测为单一供应商发布滞后。
同一时间槽仍去重，下一时间槽才按原始窗口重试，且 import/activation 始终为 false。

历史月份缺口、部分成员、其他 required dataset 缺失、计划校验失败、网络/代理、鉴权、
响应契约或安全边界错误不会套用 deferred 语义：任务保持 `failed`，并在 result 中保存
`provider-candidate-preflight-error/v1` 的有界错误码、retryable、required failure、供应商
诊断码和隔离报告 digest。供应商原始消息、HTTP body、token、代理 URL/凭据均不进入
job params、result、error 或日志。

合并部署后，启用配置并重启正式 backend 即会立即扫描一次。真实验收不读取 token：

```bash
curl --fail --silent http://127.0.0.1:8000/api/health
sqlite3 data/jobs.db \
  "SELECT job_uuid,status,json_extract(params,'$.idempotency_key'),json_extract(params,'$.quarantine_only'),json_extract(params,'$.production_import_permitted'),json_extract(params,'$.activation_permitted') FROM jobs WHERE job_type='candidate_data_preflight' ORDER BY id DESC LIMIT 3;"
sqlite3 data/jobs.db \
  "SELECT COUNT(*) FROM jobs WHERE job_type='candidate_data_preflight' AND lower(params) LIKE '%token%';"
find data/pit_evidence/provider_candidates/tushare/reports/sha256 \
  -type f -name '*.json' -mmin -30 -print
```

验收要求：服务健康为 200；三个门禁依次为 `1/0/0`；params 的 token 命中数为 `0`；
新 report 离线读取后仍是 quarantine/false/false。同一时间槽重启 backend 后
`idempotency_key` 对应的 job 计数必须仍为 1。若 required checks 全部通过，最新任务为
`completed`/`candidate_collected`；若仅最近完整月权重为空，则为
`completed`/`deferred_insufficient_coverage` 且 `candidate_collection_valid=false`；其余
错误必须为 `failed` 并带结构化错误 result。任何一种终态都不会改变
`pit_master_governed_activations` 或活动 runtime binding。

### 离线测试

离线测试不需要 token 或网络：

```bash
.venv/bin/ruff check backend/data/provider_artifacts.py \
  backend/data/sources/tushare_candidate.py \
  backend/services/candidate_preflight_scheduler.py \
  backend/tests/test_tushare_candidate.py \
  backend/tests/test_candidate_preflight_scheduler.py \
  scripts/preflight_tushare_candidate.py
.venv/bin/python -m pytest \
  backend/tests/test_tushare_candidate.py \
  backend/tests/test_candidate_preflight_scheduler.py -q
```

覆盖内容寻址幂等、篡改拒绝、凭据不落证据、供应商权限错误脱敏、全部端点自动
编排、自然月权重探针、中证官方 anchor hash 绑定、正式 scheduler 首次扫描、已完成
时间槽幂等、最近完整月空快照 deferred、历史缺口拒绝、网络/鉴权结构化失败、worker
路由和晋级结果拒绝。预检成功或 deferred 永远不能映射为 PIT 自动激活。
