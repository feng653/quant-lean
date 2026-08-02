# 原始价与复权价双账本

> **生产双价格账本/未来实盘参考。** 当前 ResearchDataStore 可为研究保存 raw、adj factor
> 和 HFQ，并在缺项时告警；它不等于本文所述已认证生产双账本。个人模拟可在明确退化告警
> 下运行，未来实盘不得退化。

## 目标

单个 Parquet 行情缓存只能声明一种复权口径，无法同时证明“回测收益使用了连续的
后复权价格”和“成交撮合使用了当日真实可交易价格”。双账本把三个证据域永久分开：

| 账本 | 价格口径 | 用途 | 禁止用途 |
|---|---|---|---|
| `raw_execution` | `raw` | 撮合、成交金额、滑点和费用 | 因子长期收益序列 |
| `research_adjusted` | `hfq` | 收益、因子、横截面研究 | 成交价、订单金额 |
| adjustment / corporate action | `hfq_vs_raw` | 解释两套 OHLC 的比例变化 | 直接作为价格 |

任何旧版单口径缓存都只读兼容，并返回
`ledger_unavailable` / `legacy_cache_is_not_a_dual_price_ledger`。系统不会依据文件名、
元数据字段或现有 hfq 缓存伪造双账本。

## 不可变导入

生产双时态契约使用 `dual-price-ledger-import/v2`：每个来源必须提供供应商实际
`available_at`，服务端写入 `ingested_at`，批次以递增 `revision` 和显式
`supersedes_batch_id` 追加。v1 行保持可读但永远不自动获得双时态证明。仓库的 v2
fixture 全部标为 `declared`，只验证链路，不代表权威历史。

管理员通过 `POST /api/data/price-ledger/imports` 一次提交完整的 raw、hfq 及可选公司
行为证据。raw 与 hfq 必须具有完全相同的 `security_code + date` 集合；导入事务要么
全部成功，要么完全不写入。

直接 API 只接受每个价格角色最多 20,000 行的有界研究证据。`licensed` 和
`exchange_authoritative` 不允许通过管理员自报字符串与任意 hash 导入；底层 store
要求受管 artifact 审批产生的 capability，并把 receipt ID/hash 与 batch 一同写入
不可变表。价格 artifact 的正式审批 UI 尚未提供，因此权威价格导入继续 fail-closed。

每条价格的规范认证身份是：

```
security_code + trading_date + source_provider + source_dataset
+ source_version + adjustment
```

`scope_id` 只表示股票池/用途覆盖，不再允许它分裂同一证券行情身份。同一规范身份在
不同股票池重复导入时，价格完全相同则复用已认证证据；任一 OHLCV 字段冲突都在
`BEGIN IMMEDIATE` 事务内原子拒绝，并返回代码、日期、字段、口径、scope 和仅含摘要
哈希的结构化证据。不同 provider/dataset/version 的来源彼此隔离，不会被错误合并。

生产回填按多个已验证 PIT timeline 的证券并集采集，每个代码/区间只请求一次。价格
只写入 `canonical_cn_a_<plan hash>` 批次；各指数池只保存不可变 runtime binding，
不复制价格行。绑定时和每次读取时均逐 `PIT member × session` 检查：每格必须同时
有 raw/hfq，或有显式停牌状态；稀疏首尾两行不能冒充完整覆盖。

批次、价格、隐含复权因子和公司行为记录均为 append-only。SQLite trigger 阻止更新
和删除；读取时重新验证 canonical JSON、批次 SHA-256、逐行 SHA-256，并从 raw/hfq
重新计算复权因子和审计摘要。完全相同的批次是幂等导入；相同业务身份的不同内容
返回 `409 price_ledger_immutable_conflict`。

`hfq` 的绝对价格锚明确绑定 source version。同一 source version 的常数倍锚差仍属于
不可变身份冲突，但审计将其分类为 `hfq_constant_anchor_conflict`，不会误称为收益
冲突；只有相邻重叠日期的 close 收益不一致才标记 `return_conflict`，同日 OHLC/close
几何不一致单独标记 `ohlc_geometry_conflict`。`qfq` 查询终点锚不属于规范双账本口径。

仅 `admin:users` 可以导入受限研究证据。拥有 `data:update` 的普通操作员不能导入；
管理员也不能绕过 artifact receipt 宣称 `licensed` 或
`exchange_authoritative`。导入端点不会访问网络。

## 质量与复权审计

导入门禁包括：

- OHLC 必须有限且为正，成交量有限且非负；
- `low <= open/close <= high`；
- raw 与 hfq 的四个同日 OHLC 必须给出一致、为正的隐含复权因子；
- 声明覆盖边界必须与实际数据边界一致；
- 复权因子变化逐笔记录，超过 50% 的异常跳变必须有同日、权威公司行为证据；
- 公司行为声明的 multiplier 与隐含变化不一致时，不能解释该变化。
- 缺少有限正数 multiplier 的公司行为不能解释变化，即使来源标记为权威。

普通因子变化可以在缺少权威公司行为源时入库以支持受限研究，但 readiness 明确返回
`corporate_action_authoritative_evidence_missing` 和
`adjustment_factor_changes_unexplained`，不得用于真实调优或执行仿真。

## 就绪度与读取

- `GET /api/data/price-ledger/import-contract`：字段、身份、门禁和权限契约。
- `GET /api/data/price-ledger/readiness`：按 scope、日期和股票范围验证双账本。
- `GET /api/data/price-ledger/cross-scope-audit`：对已入账证据执行全 scope 的只读
  规范身份审计；仅管理员可用，并有单并发与 60 秒同参缓存；报告旧冲突但不选赢家、
  不修文件。
- `GET /api/data/price-ledger/legacy-cache-audit`：直接只读扫描现有池级 Parquet，
  分开报告相同口径绝对价/收益/OHLC 几何冲突与 qfq/hfq 混合口径不可比风险。
- `GET /api/data/price-ledger/prices`：必须显式选择
  `raw_execution` 或 `research_adjusted`，返回经过完整性验证的业务数据。
- `POST/GET /api/data/price-ledger/corporate-action-evidence`：追加或按
  `as_known_at` 读取事件/明确无事件证据；同一证券区间的 event 与
  `confirmed_no_event` 冲突会原子拒绝。
- `GET /api/data/price-ledger/runtime-bindings/{id}/validate`：按 binding ID、预期
  scope/hash 重新验证 manifest 可接入的双时态证据及固定用途边界。

readiness 的 `data_gaps` 将缺失证据映射为具体补齐动作，但不自动采集、不选择来源，
也不改变任何已有来源的 evidence level。

就绪字段分为描述性能力和严格门禁：

- `descriptive_return_research_ready`：只描述 hfq 完整且研究源证据合格；旧
  `ready_for_return_research` 与 `ready_for_adjusted_price_return_research` 仅为
  兼容别名。这些字段不证明 PIT，也永远不能作为晋级门禁；
- `ready_for_unbiased_return_research` 与 `ready_for_unbiased_research` 使用同一
  fail-closed 定义：精确 PIT/runtime binding、逐 member-session 完整、双时态
  as-known-at 可得时间、权威逐日 tradability/status、公司行为验证、可信 raw 与 hfq
  双账本及无未解释调整变化全部成立。
  账本单独查询不能证明 PIT，因此这两个字段固定为 false；只有绑定实际运行输入的上层
  readiness 才可组合为 true；
- `ready_for_execution_simulation`：raw 完整、执行源合格且公司行为证据权威；
- `ready_for_real_tuning`：在严格无偏门禁上继续要求运行时公司行为状态机等执行能力。

低等级 `suspended` 声明只能解释价格缺口；legacy Parquet 即使来源标识完整、收益一致、
无跨池冲突，也只能返回 `descriptive_return_consistency=true`，所有名为 `unbiased` 的
字段必须保持 false。

实验数据预检支持
`price_purpose=compatibility_research|return_research|real_tuning|execution_simulation`。
默认 `compatibility_research` 保持旧的纯研究预检行为；`return_research` 只要求可信
调整价并明确属于静态/价格探索，显式真实调优或执行仿真在缺失双账本时 fail
closed。无偏收益与无偏调优仍额外需要 point-in-time 股票池、证券主数据、规范
账本运行时绑定，以及按用途要求的行业证据。

cache-only 实验先用缓存交易日解析精确 PIT identity；只有该 identity 与不可变
runtime binding 的 timeline/batch/hash 完全一致时，
`canonical_runtime_price_bound=true`。策略特征读取 `research_adjusted`；两份
snapshot 与 binding 均进入 manifest。由于公司行为尚未进入持仓/现金状态机，普通
实验当前不会自动把 `raw_execution` 交给引擎成交或估值；manifest 明确记录
`bound_and_snapshotted_but_not_consumed`，执行模拟继续失败关闭。没有精确绑定时继续返回
`legacy_cache_cross_pool_consistency_not_certified`。

这仍不代表执行或实盘就绪：引擎尚未应用拆分、分红等持仓/现金公司行为，
`corporate_action_runtime_application_missing` 继续阻断 execution 与 real tuning。

## PIT 并集回填

CLI 只接受能从本机不可变 PIT master 精确重建的计划；PIT 为空、batch/hash 漂移或
member-session 缺口都会在写价格前阻断。checkpoint 使用内容 hash 和原子 `0600`
文件，重启只抓取未完成代码块：

```bash
python -m backend.data.price_ledger_backfill \
  --plan data/staging/price-ledger-plan.json \
  --checkpoint data/staging/price-ledger.checkpoint.json \
  --chunk-size 20 --rate-limit-seconds 0.25
```

BaoStock raw/preclose 只用于公开研究候选和链路验证，行情、`tradestatus` 及推导 hfq
均保持 `declared`，不能升级为授权行情、权威停复牌或公司行为证据。旧 Parquet 只读
审计，绝不被回填器选作赢家或覆盖 canonical 数据。

所有历史实验保持原结果且标记为 conditional。升级不会静默重写旧 Parquet、旧实验
快照或指标；研究人员应通过只读审计查看冲突，再用统一 hfq source version 和 raw
执行账本重新生成候选实验。

所有查询结果只包含业务标识、来源摘要、哈希和数据行，不返回 SQLite 或 Parquet
路径。完整性失败返回稳定错误码，不向客户端暴露存储实现。
