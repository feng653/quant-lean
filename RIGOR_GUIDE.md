# 量化平台实验严谨性要求与分阶段实验流程

> 本文档说明当前平台为保证研究严谨性设计的所有机制，定义“一个严谨的实验”必须满足的要求，并给出本次组合策略实验的分阶段执行流程与时间分配。文档以平台实际代码为准，关键机制均可回溯到对应模块。

## 1. 总体原则

平台对研究实验的严谨性采用 **fail-closed（失败即关闭）** 策略：任何证据缺失、摘要不符、窗口重叠或来源不可验证，宁可拒绝运行，也不静默降级或“补洞”。核心目标有五条：

1. **防前视偏差**：历史研究只能使用目标交易日“当时可知”的数据（点时语义）；
2. **样本外验证**：参数选择与最终验证必须使用严格不重叠的时间窗口；
3. **可复现**：每次运行的代码、参数、数据、环境、执行配置全部以哈希绑定并持久化；
4. **可追溯**：从原始来源证据到最终指标，每一层都有不可变记录；
5. **可审计**：管理员审批、晋级动作、导入回执等全部写入追加式事件表。

## 2. 平台严谨性机制全景

### 2.1 数据层：点时（Point-In-Time）主数据

对应模块：`backend/data/point_in_time_master.py`、`backend/data/point_in_time_universe.py`、`backend/data/sources/csindex_pit.py`、`backend/data/pit_evidence_governance.py`、`docs/POINT_IN_TIME_MASTER.md`。

- `pit_master_batches` / `pit_master_intervals` 两张表（位于 `experiment.db`）保存三类点时主数据：`security`（证券身份）、`index_membership`（指数成分）、`industry`（行业归属）；
- 只有 `evidence_kind='effective_dated_history'` 的批次才能用于历史研究；`current_snapshot`（当前快照）只允许覆盖观察日一天，**不能推断过去**；
- 每条记录必须携带 `effective_from / effective_to` 闭区间，批次声明完整 `coverage_from / coverage_to`；同一 domain/scope/证券不允许重叠区间；
- 读取时重新校验批次摘要、逐行摘要和内容 SHA-256，任何缺失返回明确的 `unavailable` 原因；
- 实验入口对预设池（csi300/csi500/csi800/csi1000）强制调用 `resolve_point_in_time_universe`，逐交易日校验成员集合非空、固定规模指数数量与契约一致（300/500/1000）、区间无缺口；失败即拒绝运行；
- 中证官网数据采用“采集—解析—人工复核—管理员审批—导入”分离：官方原始字节按 SHA-256 内容寻址归档，公告 PDF 的调入/调出名单必须人工复核，且与公告公布数量一致；`license_status` 与 `requires_admin_attestation` 显式记录。

### 2.2 数据层：暂存、来源证据与行情缓存

对应模块：`backend/data/sources/validated.py`、`backend/data/source_validation.py`、`backend/data/cache.py`、`backend/data/cache_readiness.py`、`backend/data/market_quality.py`、`backend/data/price_ledger.py`。

- 行情抓取先落 `data/staging/market-validation/`，JSON 证据（`validated-daily-staging/v1`）与 Parquet 数据成对保存，绑定 `content_sha256`、`data_sha256`、抓取请求、来源身份与有效期；
- 合并进正式缓存时必须带 `source_provenance`（`cache-source-provenance/v1`），校验：单一 provider/endpoint/adjustment 身份一致、全部批次交叉验证标志、`frame_codes` 与 `frame_digest` 与真实帧一致；
- `cache_only` 实验前执行 `inspect_cached_market_data`：检查日期覆盖、代码覆盖、OHLCV 字段、数据质量快照、价格账本就绪状态；任一缺口都拒绝（`CacheOnlyDataError`）；
- `ready_for_return_research` / `ready_for_execution_simulation` / `ready_for_unbiased_tuning` 是分级的就绪标志，只有对应证据齐备才为真；v0.8.4 起模拟盘（L2）`ready_for_execution_simulation` 只要求研究级执行源，实盘（L3）`ready_for_real_tuning` 保持硬锁；
- 价格账本区分研究用复权价与执行用原始成交价，`adjusted_research_compatibility_not_raw_execution` 在 manifest 中显式声明。

### 2.3 运行层：不可变研究快照

对应模块：`backend/data/research_snapshots.py`。

- 每次运行的输入 pivot 与基准指数被写成内容寻址 Parquet（`research-data-snapshot/v1`），文件名为其 SHA-256；
- 快照绑定完整 schema 摘要（行数、列数、索引类型/层级/标签哈希、列 dtype），读取时重新计算并比对，文件大小/哈希/符号链接/路径逃逸全部校验；
- 写一次、不可修改；精确重放从快照重建输入，不重新查询当前成分。

### 2.4 运行层：不可变运行 Manifest 与代码身份

对应模块：`backend/services/research_manifest.py`、`backend/version.py`。

- 每次运行持久化 `research-run-manifest/v1`，内容包括：实验 ID、策略 ID 与策略源码 SHA-256、规范化参数与参数 SHA-256、训练/测试/数据窗口、数据集版本与摘要、universe 快照与风险警告、基准、执行配置（成本模型、约束、信号时序）、随机种子与线程设置、Python/平台/依赖/GPU 清单、进程启动时的 git 身份与工作树漂移检测；
- manifest 使用 canonical JSON（排序、去 NaN、去绝对路径、拒绝密钥类字段），整体 SHA-256 绑定到实验的 `code_version`；
- 同一实验只允许一个初始 manifest，重复写入若哈希不一致抛 `ManifestConflictError`；产物清单（`research-artifact-manifest/v1`）只追加哈希，不改动初始 manifest；
- 精确重放前比较来源 manifest 哈希、数据集摘要、universe 快照哈希、基准 SHA-256 与市场数据质量摘要，任何差异拒绝执行。

### 2.5 实验协议层：窗口契约、参数扫描与锁定测试晋级

对应模块：`backend/api/experiments.py`（sweep/promote）、`frontend/src/pages/ExperimentCenter/paramSweepProtocol.ts`。

- 严谨扫描必须同时提供 **selection 窗口** 与 **locked test 窗口**，且 `selection_end < locked_test_start`，两者严格不重叠；旧版单一 test 窗口被标记为 `legacy_unlocked`，不再代表样本外验证；
- 参数扫描把参数网格的笛卡尔积批量创建为子实验，全部子实验与扫描记录（`param_sweeps`、`sweep_experiments`）入库；
- 只有 `status='completed'` 且指标可用的成员才能被 promote；promote 创建一条**新的锁定测试实验**（窗口=locked test），并记录 `promoted_experiment_id`、`promotion_source_experiment_id`、`promoted_at`，幂等，不可重复覆盖；
- `research_trust` 字段固化：`locked_test` 为可信样本外验证，`legacy_unlocked` 仅为兼容；
- 星标（`is_starred`）由前端 `PUT /api/experiments/{id}/star` 设置，建议只对通过锁定测试且指标达标的结果加星。

### 2.6 策略层：注册、参数与训练契约

对应模块：`backend/strategies/base.py`、`backend/strategies/registry.py`、`backend/strategies/composite/_common.py`。

- 所有策略实现 `StrategyProtocol`，注册时校验元数据（分类、股票池、参数 schema、模式、训练契约）；重复 ID、未知子策略、自引用、嵌套组合一律拒绝；
- `validate_params` 在实验创建与 manifest 构建时都执行，参数以 canonical JSON 哈希绑定；
- 可训练策略必须继承 `TrainableStrategy`，`retrain_frequency` 决定训练模式；walk-forward 循环由平台驱动，禁止策略内部私写训练循环；训练失败向上传播根因，不允许静默返回空信号；
- 组合策略只允许规则型（无需训练）子策略，扫描 `sub_strategy_ids` 时同样走注册表校验。

### 2.7 任务层：持久化队列、租约与幂等

对应模块：`backend/jobs/broker.py`、`backend/jobs/scheduler.py`。

- 实验通过持久 job 队列提交，SQLite 落库，进程重启后恢复未完成任务；
- 任务执行采用 leader 租约 + 任务租约（lease_generation/expires_at），失去租约即停止写入；`attempt` 与幂等键保证重试不产生重复实验；
- sweep 提交使用批量原子入队；任一批次提交失败会标记对应实验为失败并保留错误日志。

### 2.8 权限与治理层

对应模块：`backend/api/auth.py`、`backend/api/admin.py`、`backend/api/point_in_time.py`、`backend/services/research_evidence_export.py`。

- 14 项细粒度权限覆盖实验、交易、数据、策略、AI 与管理；实验创建/扫描/读取/取消各有权限点；
- PIT 导入要求管理员身份并完成 `pit-evidence-attestation/v1` 结构化声明（调样行已复核、归档完整性已复核、条款已阅读、仅限本地研究、无再分发授权），缺一不可批准；审批与导入回执追加式记录；
- 证据导出（`GET /api/experiments/{id}/export`）只导出 manifest/产物哈希等证据，清洗绝对路径与密钥字段，任何完整性失败直接报错。

## 3. 一个严谨的实验必须满足的要求（Checklist）

| 维度 | 要求 | 平台对应校验 |
|---|---|---|
| 策略 | 策略已注册、元数据有效、参数通过 `validate_params` | Registry + manifest |
| 参数 | 规范化参数与 SHA-256 固化，扫描网格边界明确 | `params_sha256` |
| 股票池 | 预设池必须有点时成分时间线；自定义池必须声明为静态快照 | PIT resolver / `pool_preset='custom'` |
| 数据 | 缓存覆盖完整窗口与代码、OHLCV 齐全、来源证据与质量快照有效 | `cache_readiness` |
| 窗口 | 训练（如需）< 选择窗口 < 锁定测试窗口，严格不重叠 | SweepBody 校验 |
| 验证协议 | 参数选择只看 selection 窗口；最终结论只用 locked test 结果 | `research_trust='locked_test'` |
| 可复现 | 代码身份、环境、依赖、随机种子、执行配置全部入 manifest | `research-run-manifest/v1` |
| 可追溯 | 输入快照、基准、产物均以 SHA-256 记录，可导出证据包 | snapshot/artifact manifest |
| 记录 | 实验、扫描、成员、指标、晋级、星标全部入库 | 数据库各表 |
| 结论 | 只有锁定测试完成且指标达标的实验才能被标记为“已验证有效” | 人工/本流程规则 |

## 4. 分阶段实验流程与时间分配

设计原则：**先用小数据快速验证链路与粗筛，再对通过者使用更全的数据做最终验证**；每个阶段都走与前端一致的 API 链路（创建实验 → 参数扫描 → 晋级锁定测试），阶段之间窗口不混用，失败策略不进入下一阶段。

### 阶段 0：环境与链路自检（约 10–15 分钟）

- 登录平台 API（admin），确认后端、调度器、策略注册、股票池、缓存状态；
- 用 1 个策略、极小窗口（30 股、3 个月）创建 baseline 冒烟实验，确认“前端链路”可通；
- 输出：链路自检记录。

### 阶段 1：小数据冒烟（约 15–20 分钟）

- 数据规模：30 只上市早、数据完整的沪深300成分，窗口 2023-07-31 → 2023-12-29；
- 范围：全部 6 个缺失策略各 1 个 baseline（组合4 + risk_parity + factor_combo）；
- 目的：确认每个策略在当前代码下可运行、参数合法、数据可用；不评估绩效；
- 输出：6 条 baseline 实验记录（completed）。

### 阶段 2：初筛（约 40–70 分钟）

- 数据规模：同 30 股；selection 2023-07-31 → 2023-12-29，locked test 2024-01-02 → 2024-03-29；
- 范围：6 个策略各做 1 个参数扫描（每策略 3–6 个组合），取 selection 窗口 Sharpe 最高且完成的成员 promote 到锁定测试；
- 淘汰规则：baseline 失败、扫描成员全部失败、锁定测试失败或指标明显异常（年化 ≤ 0 或 Sharpe ≤ 0）者不进入下一阶段；
- 输出：6 条锁定测试实验 + 全部成员记录。

### 阶段 3：全量数据验证（约 90–150 分钟）

- 数据规模：当前沪深300全部 288 只成分（自定义静态池，来源为已通过质量校验的 baostock hfq 缓存）；selection 2016-01-04 → 2022-12-30，locked test 2023-01-03 → 2026-06-30；
- 范围：通过阶段 2 的策略；
- 全量缓存若未就绪，先物化/抓取缓存（后台进行），实验一律 `cache_only` 保证输入可复现；
- 输出：全量锁定测试实验与指标。

### 阶段 4：组合策略构造与验证（约 30–60 分钟）

- 在阶段 3 验证通过的单策略基础上，构造组合策略变体：对 `composite_equal_v1` / `composite_riskparity_v1` / `composite_momentum_v1` / `composite_regime_v1` 扫描 `sub_strategy_ids`（不同子策略集合）及各自参数（`lookback_days`、`dominant_weight`、`regime_ma_days`）；
- 每个组合策略：baseline → 扫描（selection 2016-2022）→ 最优成员 promote → 锁定测试（2023-2026）；
- 输出：组合策略锁定测试结果。

### 阶段 5：结论与星标（约 10–15 分钟）

- 有效性标准（锁定测试）：`annual_return > 0.10`、`sharpe_ratio > 0.5`、`max_drawdown > -0.40`；
- 对达标的组合策略实验调用 `PUT /api/experiments/{id}/star` 加星；
- 核对数据库：`experiments`、`param_sweeps`、`sweep_experiments`、`experiment_metrics`、`research_run_manifests`、`jobs` 均有对应记录；
- 输出：最终报告（策略、参数、指标、实验 ID、星标状态）。

### 时间分配总表

| 阶段 | 内容 | 数据规模 | 预计耗时 |
|---|---|---|---|
| 0 | 环境/链路自检 | 30股×3月×1策略 | 10–15 min |
| 1 | 冒烟 | 30股×3月×6策略 | 15–20 min |
| 2 | 初筛+锁定测试 | 30股×6月+3月 | 40–70 min |
| 3 | 全量验证 | 288股×7年+3.5年 | 90–150 min |
| 4 | 组合构造+验证 | 288股×7年+3.5年 | 30–60 min |
| 5 | 星标+核对 | — | 10–15 min |
| 合计 | | | 约 3.5–5.5 小时 |

> 注：耗时为单机 Mac mini 调度下的经验估算；若服务器资源繁忙会顺延。

## 5. 当前平台数据状态与本流程的严谨性说明

### 5.1 已就绪

- 后端运行于 `192.168.0.2:8000`，调度器健康，之前已完成 10 个单策略的完整锁定测试链路（记录在库）；
- `csi300` 缓存：schema_v4、hfq、来源 baostock（public_aggregator）、`ready_for_return_research=true`、含价格账本就绪标志，覆盖 2015-01-05 → 2026-07-30；
- 实验、扫描、晋级、manifest、快照、证据导出机制全部可用。

### 5.2 已知限制：PIT 主数据缺失

- `pit_master_batches` / `pit_master_intervals` 为空，预设池（csi300 等）实验被 PIT 门禁 fail-closed 拒绝（`effective_dated_history_missing`）；
- 补齐需要中证官网官方证据（当前成分锚点 + 全量公告归档 + 人工复核调样名单）+ 管理员审批，当前未导入；
- **本流程使用 `pool_preset='custom'`（当前沪深300成分的静态名单）执行**：平台允许、链路与前端一致、数据来自已验证缓存，但**不构成 PIT 验证**；实验记录与报告会显式标注 `universe=static_custom`；
- 升级路径：PIT 数据导入后，将同一批实验以 `csi300` 预设池重跑即可获得点时验证等级。

### 5.3 严谨性分级

| 等级 | 说明 | 本次是否达到 |
|---|---|---|
| L3 点时验证 | PIT 预设池 + 锁定测试 | 待 PIT 数据导入后升级 |
| L2 全量静态验证 | 全量成分静态池 + 锁定测试 | 阶段 3/4 达到 |
| L1 初筛 | 小股票池 + 短窗口 + 锁定测试 | 阶段 1/2 达到 |
| L0 冒烟 | 链路可运行性 | 阶段 0/1 达到 |

## 6. 附录：关键接口

```text
POST /api/auth/login                        登录
GET  /api/strategies/                       策略清单
POST /api/experiments/                      创建实验（前端普通链路）
POST /api/experiments/sweep                 参数扫描（selection + locked test）
GET  /api/experiments/sweep/{id}            扫描结果
POST /api/experiments/sweep/{id}/promote    晋级锁定测试
GET  /api/experiments/{id}                  实验状态
GET  /api/experiments/{id}/metrics          指标
GET  /api/experiments/{id}/export           证据导出
PUT  /api/experiments/{id}/star             星标
GET  /api/data/pools/{pool}/stocks          股票池成分
GET  /api/data/cache/status?pool_id=...     缓存状态
DELETE /api/jobs/{job_id}                   取消任务
```
