# 本机动态负载调度

后台任务由 SQLite 持久队列和单个调度领导者管理。默认容量针对 Apple M2
8 核 / 8 GB 内存设置为 1，在持续低负载时最多扩展到 2；压力上升时立即停止
领取第二个任务，已经运行的任务不会被强制终止。

双槽只用于不超过 50 只股票、且不需要训练的自定义池回测，以及与缓存资源不冲突
的轻量维护/模拟任务。预设指数池回测、机器学习回测、历史回放和模型重训练按重
任务处理并占满当时可用容量，默认不会在 8 GB 机器上与另一任务并发。

## 执行边界

当前执行模式为 `hybrid_spawn_factor_research`：耐久调度、租约与取消仍由
API 进程管理；因子研究的纯 CPU 重计算在显式环境白名单启动的独立解释器中
执行，子进程不继承 SQLite 连接、JWT/API 密钥或无关文件描述符。取消、超时和
崩溃都会终止并回收子进程，且不会保存半成品证据。进程槽固定为 1，启动前继续
服从同一 CPU、内存和 I/O 硬预算。

回测、模型拟合和模拟中的同步 CPU 段目前仍通过受控线程卸载，健康检查不会直接
承担这些计算。模型库的内部并行统一受
`JOB_CPU_THREAD_BUDGET`（8 GB 默认 1）限制，禁止 `n_jobs=-1` 隐式创建无界
worker/信号量。隔离边界使用全平台均可用的新解释器 `spawn` 语义，不使用
`fork`；macOS、Windows、Linux 共享同一回收协议。线程内原生调用仍采用协作式
延迟取消，只有安全检查点才提交业务结果。训练进程隔离尚未完成，不能把当前
因子研究边界表述为全部重任务都已隔离。

## 容量模型

调度器使用标准库采样，不新增运行依赖：

- macOS：`vm_stat`、`sysctlbyname`，以及 IOKit 块设备读写服务时间增量；
- Linux：`/proc/meminfo` 与 `os.getloadavg()`；
- Windows：`GlobalMemoryStatusEx`；
- 其他 Unix：`sysconf`，取不到内存压力时保守保持单槽。

只有 CPU、可用内存、内存占用和 Swap 均满足阈值，并连续获得配置数量的健康
样本后，容量才从 1 升为 2。任一指标不可用或超过阈值都会保持/回落到 1。
Swap 指标在平台拒绝访问时显示为 `null`，不会伪报为 0，并保守保持单槽。
macOS I/O 首次采样显示 `macos_iokit_warmup`；计数器读取失败或重置时明确显示
`io_pressure=null, io_source=unknown`，不会再把 SQLite writer contention
冒充成设备 I/O 压力。SQLite contention 仍在独立运维事件中统计。

默认配置：

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `JOB_SCHEDULER_ENABLED` | `true` | 启用负载自适应；关闭时保留串行执行 |
| `JOB_SCHEDULER_MAX_CONCURRENCY` | `2` | 配置上限；代码硬上限仍为 2 |
| `JOB_SCHEDULER_CPU_LOAD_LIMIT` | `0.70` | 1 分钟负载 / CPU 核心数上限 |
| `JOB_SCHEDULER_MEMORY_USED_LIMIT` | `0.82` | 内存占用率上限 |
| `JOB_SCHEDULER_MIN_AVAILABLE_MEMORY_MB` | `1536` | 扩展到双槽所需可用内存 |
| `JOB_SCHEDULER_MAX_SWAP_USED_MB` | `1536` | Swap 使用上限 |
| `JOB_SCHEDULER_MAX_SWAP_GROWTH_MB` | `128` | 两次采样间 Swap 增长上限 |
| `JOB_SCHEDULER_SCALE_UP_SAMPLES` | `3` | 扩容前连续健康样本数 |
| `JOB_SCHEDULER_SAMPLE_SECONDS` | `5` | 资源采样周期 |
| `JOB_SCHEDULER_POLL_SECONDS` | `1` | SQLite 队列轮询周期 |
| `JOB_SCHEDULER_LEASE_SECONDS` | `45` | 调度/作业租约时长 |
| `JOB_SCHEDULER_SWEEP_MAX_RUNNING` | `1` | 单个参数扫描最多占用的运行槽 |
| `JOB_SCHEDULER_MAX_PENDING_JOBS` | `500` | 非关键任务背压上限 |
| `JOB_SCHEDULER_AGING_SECONDS` | `300` | 非关键任务每增加一个调度优先级所需等待秒数 |
| `JOB_SCHEDULER_LIGHT_BACKTEST_MAX_CODES` | `50` | 单槽轻量自定义回测的股票数上限 |
| `JOB_SCHEDULER_CRITICAL_CPU_LOAD` | `1.10` | 超过后暂停领取新重任务 |
| `JOB_SCHEDULER_CRITICAL_MEMORY_USED` | `0.92` | 内存硬预算占用率 |
| `JOB_SCHEDULER_CRITICAL_AVAILABLE_MEMORY_MB` | `768` | 重任务所需最低内存安全余量 |
| `JOB_SCHEDULER_MIN_DISK_FREE_MB` | `2048` | 新重任务所需最低磁盘余量 |
| `JOB_SCHEDULER_MAX_IO_PRESSURE` | `0.80` | Linux PSI / macOS IOKit I/O 硬阈值 |
| `JOB_CPU_THREAD_BUDGET` | `1` | 单个训练任务内部 CPU worker 上限 |
| `JOB_ISOLATED_CPU_TIMEOUT_SECONDS` | `1800` | 因子 CPU 子进程硬超时，超时后回收 |
| `JOB_OBSERVABILITY_RETENTION_HOURS` | `168` | 运维事件最大保留窗口 |
| `JOB_SLO_WINDOW_HOURS` | `24` | SLO 评估观察窗 |
| `JOB_SLO_EVALUATION_SECONDS` | `60` | 调度器评估 SLO 的最短周期 |
| `JOB_SLO_CONFIRMATIONS_REQUIRED` | `2` | breach/recovery 转换所需连续观测数 |
| `JOB_SLO_ALERT_COOLDOWN_SECONDS` | `900` | 同 objective、同转换通知冷却时间 |

不建议在 8 GB 机器上提高硬上限。需要验证阈值时，应记录 API 延迟、任务峰值
RSS、Swap 增长及同池缓存行为，再逐项调整。

## 优先级与资源锁

调度顺序从高到低为：

1. 纸面交易依赖的数据更新；
2. 每日交易模拟；
3. 手工数据更新；
4. 交互式回测；
5. 参数扫描成员；
6. 历史回放；
7. 模型重训练。

每日模拟带有 `required_data_job_uuid` 时，依赖完成前不会被领取；依赖失败后模拟
任务会被领取并以明确错误失败，避免永久排队。同一参数扫描最多运行一个成员，
因此扫描不能占满两个槽而饿死交互任务。

非关键任务每等待 5 分钟增加一个有效优先级，最高老化到 99，避免重训练、回放
或扫描被持续的普通交互回测永久饿死。纸面交易及其依赖更新的优先级为 100 以上，
不会被后台任务老化反超；持续不断的关键交易任务仍会有意推迟研究类后台工作。

数据更新与读取同一个 pool/cache 的回测互斥；全量更新（`pool_id=null`）与所有
池互斥。每日模拟可能读取多个部署的数据池，因此在任务载荷完整声明这些池之前，
按全局缓存读者处理并与任一数据更新互斥。数据更新本身也保持单任务执行，避免
外部数据源限流和共享元数据竞争。历史回放、模型重训练、预设指数池回测和训练型
回测默认独占，开始前要求当前没有其他运行任务。独占任务到达队首后停止用低优先
级轻任务填补空槽，让已有轻任务排空后尽快启动独占任务。

队列达到背压上限时，普通更新、回测、扫描、回放及重训练拒绝新任务；每日模拟
仍保留进入队列的能力。API 返回 429；队列故障返回 503，数据更新不会静默退回
同步执行而绕过背压或缓存互斥。

## 租约、恢复与取消

- SQLite `BEGIN IMMEDIATE` 原子选择最高优先级兼容任务。
- 每个 claim 记录唯一 `worker_id`、递增 `lease_generation`、心跳与到期时间。
  过期 worker 的进度提交会被 generation fencing 拒绝。
- `job_scheduler_lease` 保证同一 SQLite 队列仅有一个调度领导者。对于同机
  进程，即使事件循环被 CPU 工作拖过租约时间，只要原 PID 存活就拒绝抢占；
  租约同时记录进程启动身份，避免 PID 被系统复用后误判为旧 worker 仍存活。
  PID 已确认死亡或启动身份已变化时允许新进程立即恢复；身份无法读取时保守地
  视为仍存活。跨主机时按租约时间处理。
- 启动只恢复租约已过期的 `running` 作业，不再无条件重置其他进程的任务。
- 优雅停机把被中断的 `running` 作业退回队列；`cancel_requested` 作业保持
  取消语义并结束为 `cancelled`。
- 单个执行协程若提前返回或被独立取消，会立即释放仍活跃的 claim 并重新排队，
  不会一直占用扫描槽或缓存互斥直到服务重启。
- 排队回测的取消和重试会在同一数据库事务内同步实验与参数扫描状态，避免任务
  已终止但实验/扫描永久显示 `pending`。

这里没有实现热备自动接管。未获得领导租约的 API 进程保持只读/提交能力，不会
持续争抢领导权；领导进程退出后需要服务管理器重启实例。生产部署当前仍推荐
单 Uvicorn worker。作业 generation 会阻止旧 worker 更新 job 状态，但实验、
缓存和交易服务的所有业务写入尚未逐条携带 generation 条件；受支持的本机模式
依靠单领导者和 PID 存活检查避免双执行，不支持共享 SQLite 的跨主机 active-active。

## 可观测性

`GET /api/jobs/summary` 的 `worker` 字段提供：

- `online`、`leader`；
- `capacity` / `configured_max` / `running_slots`；
- `degraded` 与机器可读 `reasons`；
- CPU、内存、Swap 和采样来源 `metrics`；
- `execution_mode=hybrid_spawn_factor_research`，明确只有因子 CPU 段已进程隔离。

任务中心显示当前运行槽、配置上限、CPU/内存/磁盘预算和中文排队原因。达到硬
预算时 `admission_mode=pause_heavy`：不领取新的回测训练、模型重训、因子研究
或历史回放，轻任务仍可使用剩余单槽，已经运行的任务不会被突然杀死。

管理员可通过认证只读接口
`GET /api/jobs/observability?window_hours=24` 查看：

- 各任务类型提交数、成功/失败/取消率、排队与运行时长 P50/P95；
- 数据刷新阶段、有限频率股票完成进度和缓存质量聚合；
- WebSocket 连接/断开、服务启动/停止、调度重启和 SQLite contention；
- `operations-slo/v1` 机器可读阈值与达标状态。

聚合存入 `experiment.db` 的 `operational_events`，服务重启后仍可恢复，保留
窗口有界。标签只允许 `event_name/category/job_type/outcome/stage`，不包含
用户、路径、Token、job UUID 或股票代码；结构化日志使用
`operations-log/v1` 同一低基数约束。`/api/health` 同时暴露
`resource_budget`，watchdog 可观察降容但不会因正常压力而重启健康服务。
这些指标仅用于本机调度和展示，不会作为研究结果或交易信号输入。
