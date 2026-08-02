# PIT 候选证据离线对账

`backend.data.pit_evidence_reconciliation` 对已取得并留存在隔离区的供应商候选证据做
确定性对账。它不联网，不读取或写入 PIT 主表、价格账本、SQLite、Parquet 缓存，也没有
导入或激活入口。

通过只表示“提供给本次运行的 Tushare 候选记录与提供给本次运行的官方记录一致”。报告
始终保留 `classification=quarantine`，并固定：

- `production_pit_ready=false`
- `production_import_permitted=false`
- `activation_permitted=false`

因此，fixture 通过、20 个样本通过或四次历史调样通过都不能替代 2016 至今的连续覆盖、
许可与独立发布审批。

## 机器门禁

一次输入必须同时满足：

1. 恰好 20 个不同的公司行为案例。每例包含一份官方证据与一份 Tushare quarantine
   候选证据；证券、事件类型、生效时间和全部标准化条款必须完全一致。
2. `csi300`、`csi500`、`csi800`、`csi1000` 各至少一个历史调样事件。每个事件包含
   官方增加/删除名单，以及 Tushare 调样前后完整快照；完整快照行数分别必须为
   300、500、800、1000，按官方事件重放后的集合必须与调样后快照完全相同。
3. 每份证据都要有内容 SHA-256、manifest SHA-256、`effective_at`、`available_at`、
   `ingested_at` 和有来源证明的 `revision`。`ingested_at` 不得早于 `available_at`。
4. Tushare 的 `available_at_evidence` 和 `revision_evidence` 必须为
   `provider_field`。本地下载时间 `declared_ingestion_time` 不能冒充历史可得时间；响应
   hash 也不能冒充供应商修订号。
5. 官方记录必须标为 `authority_level=official`、`classification=official`，且可得时间
   由官方发布时间或官方字段证明。候选记录必须是 `provider=tushare`、
   `classification=quarantine`。

缺字段、空值、重复 ID、重复成分、未知事件条款、非完整指数快照、条款差异、成员差异、
非权威时间或修订声明全部失败关闭。报告只给出差异字段和计数，不复制敏感条款值。

## 标准化输入

顶层 schema 为 `pit-evidence-reconciliation-input/v1`：

```json
{
  "schema_version": "pit-evidence-reconciliation-input/v1",
  "classification": "quarantine",
  "prepared_at": "2026-08-02T08:00:00Z",
  "corporate_action_cases": [],
  "index_member_events": []
}
```

公司行为案例结构：

```json
{
  "case_id": "official-event-stable-id",
  "official": {
    "evidence_id": "official-artifact-row-id",
    "provider": "cninfo_official",
    "authority_level": "official",
    "classification": "official",
    "artifact_sha256": "64-lowercase-hex",
    "manifest_sha256": "64-lowercase-hex",
    "security_code": "600000",
    "action_type": "cash_dividend",
    "terms": {"cash_per_share": 0.1, "tax_basis": "pre_tax"},
    "effective_at": "2024-06-17T00:00:00Z",
    "available_at": "2024-05-31T09:00:00Z",
    "ingested_at": "2024-05-31T09:10:00Z",
    "available_at_evidence": "official_published_at",
    "revision": "official-document-version",
    "revision_evidence": "official_document_version"
  },
  "candidate": {
    "evidence_id": "tushare-artifact-row-id",
    "provider": "tushare",
    "authority_level": "candidate",
    "classification": "quarantine",
    "artifact_sha256": "64-lowercase-hex",
    "manifest_sha256": "64-lowercase-hex",
    "security_code": "600000.SH",
    "action_type": "cash_dividend",
    "terms": {"cash_per_share": 0.1, "tax_basis": "pre_tax"},
    "effective_at": "2024-06-17T00:00:00Z",
    "available_at": "2024-05-31T09:00:00Z",
    "ingested_at": "2024-05-31T09:11:00Z",
    "available_at_evidence": "provider_field",
    "revision": "provider-revision",
    "revision_evidence": "provider_field"
  }
}
```

指数事件的 `official` 使用相同证据字段并增加 `additions`、`removals`；
`candidate_before`、`candidate_after` 使用候选证据字段并增加 `members`。成员必须是完整
快照，不能只提交变更行。`candidate_before.effective_at` 必须早于官方事件生效时间，
`candidate_after.effective_at` 不得早于官方事件。

## 真实 artifact 仍需取得

当前代码和测试只证明门禁行为，以下输入仍须由真实来源获取并以不可变原始字节、请求
参数和 manifest 留存在 quarantine：

| 范围 | 官方输入 | Tushare 候选输入 | 未满足时结论 |
|---|---|---|---|
| 20 个公司行为 | 巨潮、上交所或深交所公告正文与附件；公告 ID、发布时间、生效日、事件条款及历史修订版本 | 同一证券和事件的 `dividend`/适用事件接口原始响应；提供者字段级公告时间、事件生效时间、修订号 | `corporate_action_unknown` 或 mismatch |
| 沪深 300 | 中证指数历史调样公告、hash 绑定附件增加/删除行、生效时间 | 调样生效前后两个完整 `000300.SH index_weight` 快照 | scope 不通过 |
| 中证 500 | 同上，指数 `000905` | 完整 `000905.SH` 前后快照 | scope 不通过 |
| 中证 800 | 中证 800 官方调样事件；若由 300+500 推导，必须保存官方成分定义及两个子池同批次原子证据 | 完整 `000906.SH` 前后快照 | scope 不通过 |
| 中证 1000 | 同上，指数 `000852` | 完整 `000852.SH` 前后快照 | scope 不通过 |
| 双时态与修订 | 官方首次发布时间、后续更正时间和版本关系 | 供应商原生 `available_at`、revision 字段及旧版本留存样本 | 不得用摄取时间或响应 hash 代替 |
| 权利与留存 | 官方站点允许的本地研究留存条款 | Tushare 对批量调用、原始响应留存、历史修订可得性的书面答复 | 不得进入生产导入评审 |

20 个案例应覆盖现金分红、送股/转增、配股、拆并股、吸收合并/退市处置等会改变价格、
现金或持仓的事件；不能用 20 条同类分红替代事件类型覆盖。正式案例清单须在采集前冻结，
避免看到差异后更换样本。

## 运行与审计

输入文件必须是小于 32 MiB 的普通非符号链接 JSON 文件：

```bash
.venv/bin/python scripts/reconcile_pit_evidence.py \
  --input /absolute/quarantine/path/reconciliation-input.json \
  > /absolute/audit/path/reconciliation-report.json
```

退出码 `0` 表示本次对账通过但仍在 quarantine；退出码 `2` 表示失败关闭。报告绑定
`input_sha256`，自身由 `report_sha256` 防篡改。`evidence_refs` 只列出证据 ID、来源角色和
两个 hash，审阅者据此回读内容寻址原始对象。若输入本身包含 token、password、secret、
authorization 等凭据字段，运行会失败且报告不会复制凭据值。
