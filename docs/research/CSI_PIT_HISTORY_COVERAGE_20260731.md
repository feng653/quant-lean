# 中证指数点时历史官方证据采集报告（2026-07-31）

## 结论

本次运行**没有建立 2015 至今的连续点时股票池**，没有批准或导入生产主数据。
受管包 `pitpkg_bf9726044897ab66eb3b4d7c94bd05b3` 保持 `pending`，可证明 staging
范围仅为三个官网当前锚点共同的观察日 `2026-07-30`。生产导入标记为
`false`，自动批准标记为 `false`。

该次运行生成的是旧 `csindex-pit-staging/v1` / review v1 产物。当前治理只接受
staging v2 的生产历史契约；旧 pending 包不会被追认或迁移。它只能作为当时采集
事实的审计记录，必须在 v2 下重新生成逐 archive-row 哈希 disposition、补采所有
目标行详情/附件、接受独立重放的 proposal hash，并绑定权威日历 artifact 后，才
可能进入批准评估。不会因本文记录了 current anchor 而形成正式历史区间。

运行产物（gitignored 原始证据目录）：

- coverage report:
  `data/pit_evidence/history_runs/csi-2015-20260731/coverage_report.json`
- review queue:
  `data/pit_evidence/history_runs/csi-2015-20260731/review_queue.json`
- checkpoint:
  `data/pit_evidence/history_runs/csi-2015-20260731/checkpoint.json`

## 官网归档事实

| 项目 | 实际值 |
|---|---:|
| 无筛选归档页 | 42 |
| 官网声明/返回的物理行 | 4,160 |
| 唯一公告 ID | 4,150 |
| 逐字段完全相同的分页边界重复 ID | 10 |
| 最早发布日期 | 2005-04-05 |
| 最新发布日期 | 2026-07-31 |
| 归档 manifest SHA-256 | `b59a87d7a3a28bd3c9840b0c58199bf6b9a7b7114a139935088d2347d41b8ea2` |
| review rows SHA-256 | `23e89594d28d52dc1a141b0057f2da08c96e693147ddeea383f5be6debe06d04` |

重复 ID 为 `79`、`1213`、`3793`、`4278`、`4641`、`6813`、`12639`、
`12722`、`12957`、`13219`。每个重复行的 canonical JSON 完全一致；所有原始
页仍留存并进入 manifest。实现只允许这种完全相同的边界重复；同 ID 字段不一致
会阻断。

三个 current anchor 均观察于 `2026-07-30`，成员数分别为 300、500、1000，
原始 XLS SHA-256 分别为：

- CSI 300: `47b37f1fe5d120a38f1dd2c320a748ff3fc0d2943ce2c4d123e89a770e984198`
- CSI 500: `a9acbd67a092463421bc0cd63bdf37de133f93fae22ca89608a7c89caabd563d`
- CSI 1000: `76b66cf88740b2d3f41ffede40ac59eb036170cc0794a27ba15817d203412808`

## 自动严格提案与可证边界

“严格提案”只是确定性 schema/parser 对官网正文、详情、全部附件、代码和数量
的一致性检查结果，不是第二复核人批准，也不是 production evidence。

| 指数 | 严格提案数 | 首个生效后收市日 | 最后生效后收市日 | 人工复核数 | 从 2015 连续 |
|---|---:|---|---|---:|---|
| CSI 300 | 6 | 2023-06-09 | 2026-06-12 | 0 | 否 |
| CSI 500 | 6 | 2023-06-09 | 2026-06-12 | 0 | 否 |
| CSI 1000 | 6 | 2023-06-09 | 2026-06-12 | 0 | 否 |

这 6 次定期调整仍处于 `awaiting_independent_row_review`。此外，泛称公告尚未全部
完成人工分类，老公告详情接口对 2005-06-22 至 2015-06-01 的 87 个候选返回
永久 403；临时调样常缺正文明确数量/固定生效日，另有旧附件 schema 不满足严格
解析。上述任一项都足以破坏逆向事件链连续性。

因此请求的 `2015-01-01` 至 `2026-07-29` 全区间被报告为
`historical_event_chain_not_fully_reviewed`。`--from=2015-01-01` 只是采集目标，
不构成覆盖声明。

## 剩余硬阻断

1. 独立复核人尚未在 v2 文件中为完整 4,147 行（锚点日及以前）逐行绑定原始 row
   hash、disposition 和理由，也未对所有 target 行完成受管补采并逐项接受精确
   proposal hash；全局“已复核”声明不能替代这些记录。
2. 尚未在仓库外配置经治理的 Ed25519 calendar trust key，也未提供由该 key 签名、
   带精确 provider/level、version、retrieved_at 和完整 sessions 的权威交易日历。
3. 老公告 403、泛称公告和临时调样缺口尚未通过其他官方证据闭环。
4. `license_status=not_attested_by_platform`；来源条款和本地研究用途尚待管理员
   结构化确认。

在这些阻断解除前，四个 CSI scope 只能留在 quarantine；不得 production
activate，不得将旧实验改称无幸存者偏差结果。
