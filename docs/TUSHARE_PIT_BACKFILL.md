# Tushare PIT 候选证据回填

本工具把 Tushare 历史响应保存到内容寻址的私有隔离区，为后续许可复核、权威材料
对账和治理导入提供可重放证据。它**不会**写 PIT master、行情 cache、实验数据库，
也没有审批、导入或激活接口。

默认计划固定在已经由限量探测证明能返回完整四指数快照的 `2016-01` 至
`2026-06`，包括：

- 四指数逐月 `index_weight`；
- 上市、退市和暂停上市证券主档，年度交易日历与申万一级行业目录；
- 只有全部逐月权重 artifact 均被分类为完整、范围一致且仍处于 quarantine 后，才从
  这些不可变响应推导四指数的**所有历史成分**；
- 只有 canonical `trade_cal` artifact 覆盖请求区间的每个自然日且无重复/冲突时，才从
  其中的 `is_open=1` 派生 session；代码不自行猜测工作日或补交易日；
- 每个开放 session 分别按 `trade_date` 保存全市场 `daily`、`adj_factor`、
  `daily_basic` 与 `suspend_d` 响应，再在本地使用历史月度指数权重求交；供应商返回的
  当前证券集合从不充当 PIT universe；
- 每只历史成分均采集行业成员、分红与名称变更候选证据。

2026-06-30 的真实隔离契约探测已证明上述四个接口接受 `trade_date`：`daily` 5,508 行、
`adj_factor` 5,534 行、`daily_basic` 5,508 行、`suspend_d` 23 行。对应响应仍全部为
quarantine；这些行数只证明接口截面能力，**不证明**历史成员关系、逐证券可交易状态、
`available_at` 或生产完整性。

本地求交为每个 session 记录四份 manifest hash、历史成员集合 hash、应覆盖成员数、
有正流动性成员数、候选不可交易证据数、状态分类计数、语义模糊/冲突数和 blocker。
历史成员存在 `daily` 时必须同时存在调整因子与逐日规模，且 `vol`、`amount` 都为有限
正数；零值或缺值不能冒充可交易。没有 `daily` 时，仅明确的全日停牌候选或上市/退市
主档非活动状态可以闭合候选采集；盘中停牌或不明停牌记录不足以解释整日无行情。

`daily` 与 `suspend_d` 同时存在不一定冲突。真实 quarantine 样例
`000693.SZ / 2016-01-04` 有正成交量与成交额，同时为
`suspend_timing=09:30-13:00, suspend_type=S`。当前分类器只有在以下条件全部成立时，才把它
分类为 `observed_liquidity_with_explicit_partial_suspension`：

1. `vol` 和 `amount` 均为有限正数；
2. `suspend_type` 明确为当前已观察并保守支持的 `S`；
3. `suspend_timing` 是 `HH:MM-HH:MM`，位于 `09:30-15:00` 内且不覆盖整个区间。

这只表示“该日观察到成交且存在明确盘中限制”，**不表示全天可交易**。全日停牌却有
daily、模糊 timing/type、重复停牌记录、零流动性以及盘中停牌却整日无 daily 都继续
结构化阻断。报告和每个 session 记录都固定
`production_full_day_tradability_proven=false`；原始 daily 与 suspend artifact 均保留，
不会互相覆盖。月度权重仍不能证明月内精确调样生效时点，因此正式激活前还必须用权威
调样公告解决 effective window，并独立审核停复牌语义。

报告 schema 为 `tushare-pit-candidate-backfill/v4`，session 求交记录为
`tushare-session-universe-intersection/v2`。重放已有 v1 求交记录时，会用原四份 receipt
重新计算并替换旧的派生 `daily_and_suspend_conflict`；不会重新请求、删除或改写原始
artifact。provider 网络、权限、频控、artifact 完整性等其他 checkpoint failure 原样
保留并继续阻断，不能借语义升级被清除。

`--sample-size` 只保留为报告中的确定性诊断样本数，`--event-sample-size` 只保留为
旧 checkpoint 的运行身份兼容参数；`--market-chunk-months` 同样只保留 checkpoint
身份兼容。三者都不会缩小全历史成分或 session 采集范围。v1/v2 checkpoint 会在校验
摘要、run ID 和公开计划完全匹配后迁移到 v3；旧逐证券 evidence 和失败记录继续保留，
但绝不会被计作 session 截面 receipt。行业/事件的 exact dataset+params receipt 可以在
artifact 完整性通过且仍为 quarantine 时复用。

行情和事件仍是候选证据，不是已经对账的生产双价格账本。报告始终返回
`production_pit_ready=false` 和 `runtime_data_changed=false`。许可证、历史
`available_at`、修订留存、官方事件对账和受治理导入仍是硬门禁。

## 可恢复执行

每次最多发起 16 个请求，每次成功响应先写内容寻址 artifact，再原子推进私有
checkpoint。相同命令可安全重复；完成项按 receipt 跳过。单 run 使用非阻塞文件锁，
避免两个进程同时推进同一 checkpoint。

```bash
cd /Users/xuhe/Developer/quant-platform
.venv/bin/python scripts/collect_tushare_pit_backfill.py \
  --from-month 2016-01 \
  --to-month 2026-06 \
  --sample-size 30 \
  --event-sample-size 10 \
  --market-chunk-months 12 \
  --max-calls 16
```

重复执行上面的同一命令，直到输出 `status=completed`。若收到权限、频控或网络错误，
本次以非零状态退出且保存已完成进度；修复外部原因后继续同一命令即可。不得通过改变
月份或样本参数来“跳过”失败项，因为参数变化会生成不同的 run ID 和 checkpoint。
v3 将原先约 93,760 个逐证券任务压缩为“开放 session × 4 个截面接口 + 历史成员 ×
3 个行业/事件接口”；实际任务数由 canonical 日历和历史成员 artifact 决定，不写死。
`--max-calls` 严格限制每次进程最多发起的供应商请求数，适合由外层 durable 调度器分批
续跑。缺少任何逐月权重、权重分类不足、calendar 自然日覆盖不全、artifact 范围不一致
或非 quarantine 时，全历史计划都拒绝物化，不能退回现时成分、系统日历或样本。

隔离根默认是：

```text
data/pit_evidence/provider_candidates/tushare_backfill/
```

其中响应、manifest 和 report 都按 SHA-256 寻址；checkpoint 只保存公开请求范围、
receipt、行数和脱敏诊断。Token 和代理完整 URL 只从 `Settings` 的 `SecretStr` 读取，
不会进入 artifact、checkpoint、report 或命令参数。

对于本回填器产生的 `suspend_without_daily_semantics_ambiguous` 或
`daily_suspend_semantics_ambiguous`，可以按
[BaoStock 历史逐日状态交叉证据](BAOSTOCK_SESSION_CROSSCHECK.md)采集第二候选源的指定
code/date receipt。该附件只增加复核上下文，绝不修改本报告的 `blockers` 或 `valid`；
候选源一致仍须官方证据与独立治理复核。
