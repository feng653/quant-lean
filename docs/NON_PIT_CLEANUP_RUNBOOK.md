# 非 PIT 行情缓存清理运行手册

## 结论

当前**禁止执行生产清理**。四个中证池尚未具备 2016 至今已激活、可得时间已验证的
历史 PIT 成分；生产 raw/hfq 双价格账本、公司行为/交易状态、精确 runtime binding 和
权威日历也没有全部通过。因此，旧 `data/cache/` 虽然已经不能被正式研究或模拟盘当成
运行时真相，仍必须保留，直到可证明替代链已完成。

本工具仅把 `data/cache/` 移到同一文件系统下的可恢复隔离归档。它**不删除**：

- `experiment.db`、`trading_sim.db`、`users.db`、`jobs.db`；
- `pit_evidence/`、备份、模型、研究快照、staging；
- 历史实验、模拟盘或其审计证据。

历史实验/快照可能包含旧行情衍生结果，但它们受 PIT-only policy 的只读隔离；若未来
要处理这些审计记录，需要独立的数据保留审批，不能混入“清理股票缓存”。

## 现有数据 inventory

维护脚本会只读扫描 data 根目录，并产生：

- 可归档 target 的文件数、字节数和内容哈希；
- PIT 证据、应用数据库、备份/模型/快照/staging 等受保护路径；
- SQLite 只读 integrity/schema 域摘要，不导出账户、实验参数或结果；
- 未识别路径，要求人工分类，绝不会自动选择为删除目标。

在真实机器上先执行以下**默认 dry-run**（不移动任何文件）：

```sh
cd /Users/xuhe/Developer/quant-platform
python scripts/cleanup_non_pit_data.py \
  --data-root data \
  --coverage-start 2016-01-01 \
  --coverage-end 2026-07-31 \
  --report data/maintenance/non_pit_cleanup/preflight-2016-present.json
```

脚本已带 repository-root import bootstrap，可直接按上面的 `python scripts/...` 形式
运行，不需要设置 `PYTHONPATH`；仓库 checkout 中也带可执行位，可按需使用
`./scripts/cleanup_non_pit_data.py`。

退出码 `2` 表示预期的 fail-closed 阻断；报告会逐池给出原因。当前典型原因包括：
`pit_membership_available_at_not_verified`、`dual_price_ledger_not_complete`、
`runtime_binding_not_ready`、`authoritative_calendar_not_bound`。

## 实际归档的全部前置条件

工具对 `csi300`、`csi500`、`csi800`、`csi1000` 的同一明确区间逐一要求：

1. 已激活的有效历史 PIT 成分与 `available_at`；不是 current snapshot、fixture 或
   quarantine 证据；
2. raw execution 与 hfq research 两种价格角色完整且可信；
3. bitemporal availability、精确 PIT/runtime binding、member-session 完整；
4. 权威交易日历及 PIT 基准绑定；
5. 无偏研究 runtime readiness；
6. 8000、5173 与本机 443 没有 listener。必须先停止 backend、frontend 和 Caddy，
   防止进程持有、写回或重新生成旧 cache；
7. 显式维护窗口 ID，以及读取 dry-run 后复制的 inventory SHA-256 和第二次确认。

任一条件不成立，`--execute` 会在移动前失败。即使四池有一天的 active PIT batch，
历史覆盖、价格账本或状态不完整也不能绕过该门禁。

满足条件后，命令形式为（当前不要运行）：

```sh
python scripts/cleanup_non_pit_data.py \
  --data-root data --coverage-start 2016-01-01 --coverage-end YYYY-MM-DD \
  --execute --maintenance-window-id maint-YYYYMMDD-001 \
  --confirm-inventory-sha256 '<dry-run inventory_sha256>' \
  --second-confirmation ARCHIVE_NON_PIT_CACHE
```

归档位于 `data/maintenance/non_pit_cleanup/<run-id>/cache/`，保存包含完整哈希的
`receipt.json`。操作用 `rename` 在同一文件系统移动整个 cache；移动后重算每个文件
的哈希。若重算、创建新空 cache 或写收据失败，工具会原子移回原 cache 并再次核验。
它不会执行物理删除。

## 恢复演练

仅在服务仍停线、活动 `data/cache/` 为空时，可恢复一份已验证归档：

```sh
python scripts/cleanup_non_pit_data.py --data-root data \
  --restore-run '<run-id>' \
  --second-confirmation RESTORE_NON_PIT_CACHE
```

恢复拒绝覆盖非空 cache，且先验证归档 hash。恢复后仍不代表旧缓存成为 PIT 运行数据。

## 依赖整改

当前 `inspect_cached_market_data` 仍以 legacy cache 的交易日索引来构造 runtime 请求，
随后才由 canonical ledger 替换价格。因而在真正清理前，NEXT-03/04 还必须让权威 PIT
日历和 runtime binding 提供交易 session，不再依赖旧 Parquet 的日期索引。该依赖在本
工具的 runtime gate 中故意以 fail-closed 形式暴露，不能以“服务已停”掩盖。
