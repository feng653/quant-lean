# 生产 PIT 发布候选：导入前门禁与运行手册

本文描述 `approved provider artifact → 完整性 dry-run → 原子发布授权 → 隔离代际物化`。
流程不会联网、不会写 `experiment.db`，也不会清理旧缓存。物化器只创建新的不可变
generation；现有应用数据库和当前活动 generation 始终保持原状，直到单一 manifest
原子切换。

## 当前边界

代码入口：

- `backend/data/production_pit_release.py`：批准制品验证、跨数据集覆盖检查、发布计划；
- `backend/data/production_pit_materializer.py`：重新验证授权和全部签名制品，在 staging
  SQLite 中构建 PIT master、双价格账本、四池 runtime binding 和逐行原始证据，再以
  generation manifest 原子发布；
- `scripts/preflight_production_pit_release.py`：只读 dry-run，以及显式确认后的专用登记库授权。

原子授权仍只把计划和制品 hash 写入独立的 `pit-release-registry.db`，返回
`runtime_materialised=false`。授权记录本身不能被解释为“生产 PIT 已激活”。物化器只接受
登记库中完整的零 blocker 授权；它会重新验证 Ed25519 签名、许可 receipt、payload hash、
schema、十类逻辑制品、跨数据集覆盖和授权 binding。任何差异在创建活动 manifest 前拒绝。

当前未提供面向真实生产路径的自动物化 CLI，这是有意的停线边界：Q-01～Q-03 尚未取得
合格真实授权包。代码入口只由受控部署任务调用，现阶段验收仅使用临时目录测试夹具，不能
指向 `data/experiment.db`、现有缓存或当前服务目录。

## 代际物化与读者契约

活动根目录布局：

```text
runtime-root/
├── generation-manifests/production_pit_runtime.json   # 唯一活动指针
└── generations/production_pit_runtime/<generation-id>/
    ├── runtime_db       # PIT master + dual-price ledger + 四池 binding
    └── release_evidence # 授权计划、制品清单、构建结果与 runtime_db SHA-256
```

`ProductionPitRuntimeReader.load()` 先读取一次活动 manifest，再验证两个文件的大小和
SHA-256、release evidence、SQLite `integrity_check`、计划 hash 和必需表。已打开的旧读者
继续使用旧 generation 的不可变路径；新读者只会取得完整新 generation，无法组合新旧
文件。构建、fsync、第二个 artifact 安装或 manifest replacement 任一步失败时，旧 manifest
不变；失败 generation 可保留取证但不可见。重复发布同一 `plan_sha256` 幂等返回当前代。

每个内部导入 capability 同时绑定 `plan_sha256`、批准 manifest SHA-256 和精确导入文档
SHA-256，不能把一次授权复用于不同数据。运行时 SQLite 还保存每个批准 payload 的逐行
canonical JSON 与 row hash，因此 amount、逐日状态、无事件证明等即使不在旧式业务表中，
也不会在物化时丢失。

## 真实运行的必要输入

发布窗口默认从 `2016-01-01` 到最近一个已完成且经权威日历确认的日期。以下十个逻辑
制品缺一不可；大数据可以按月或年分片，但同一逻辑身份的分片日期范围必须连续覆盖整个
发布窗口。

| 制品 | scope | 必须证明的内容 |
|---|---|---|
| 权威交易日历 | `cn_equity` | 交易日、逐行四时间字段、可信签名批准 |
| 历史指数成员 × 4 | `csi300/500/800/1000` | 每个交易日分别为 300/500/800/1000 只；包含曾经入池且后来退市的证券 |
| 证券主数据 | `all_a` | 所有 member-session 的上市/退市状态和永久证券身份 |
| 历史行业 | `cninfo_008001` | 每个 member-session 的当时行业代码及名称 |
| 逐日交易状态 | `all_a` | 每个 member-session 唯一标记为可交易、停牌、上市未交易或退市 |
| raw/研究调整价双账本 | `all_a` | 所有可交易 member-session 的两套 OHLCV、amount、调整因子 |
| 公司行为/无事件证据 | `all_a` | 每个 member-session 有事件或明确的 `confirmed_no_event` 证据 |

每个制品 manifest 都必须满足：

1. 内容寻址：manifest 和 payload 分别由 SHA-256 定位，任一字节变化即拒绝；
2. 许可：`licence_scope=local_research_retention`，绑定不可变许可 receipt hash，receipt
   原文也必须在内容寻址目录中可重新读取；
3. 独立复核：stager 与 reviewer 不同，manifest 由已配置的 Ed25519 公钥验证；
4. 证据等级：日历、证券/状态、指数、价格和公司行为分别满足合同允许的权威、许可或
   交叉验证等级，低等级声明不能因“已下载”自动升级；
5. 双时态与修订：每行都有 `effective_at`、`available_at`、`ingested_at`、正整数
   `revision`，并满足 `effective_at <= available_at <= ingested_at`；
6. 日期覆盖：分片不得越过预注册窗口，合并后不得有日期空洞；
7. 价格严谨性：可交易成员必须有 raw 和研究调整价，非成员不得混入；研究价必须可由
   raw 与调整因子确定性复算；停牌等缺价必须由
   权威逐日状态解释，不能用前值填充冒充成交价；
8. 公司行为严谨性：所有成员日期都有事件或无事件证明，调整因子变化必须绑定当日事件，
   不能用“没有抓到记录”推断无事件。

这些是实际运行前置条件，不可用现时成分快照、候选 quarantine artifact、测试 fixture、
供应商宣传页或一次 HTTP 200 代替。

## 内容寻址目录

批准制品根目录只读布局如下：

```text
approved-root/
├── artifacts/sha256/ab/<payload-sha256>.json
├── manifests/sha256/cd/<manifest-sha256>.json
└── licence-receipts/sha256/ef/<licence-receipt-sha256>
```

release bundle 使用 `production-pit-release-bundle/v1`，声明窗口并列出所有 manifest
SHA-256。可信 key 文件只保存 `{key_id: base64_ed25519_public_key}`，不含私钥或供应商
token。

## 第一步：只读 dry-run

```bash
python scripts/preflight_production_pit_release.py \
  --approved-root /受控路径/approved-provider-artifacts \
  --bundle /受控路径/release-bundle.json \
  --trusted-keys /受控路径/approval-public-keys.json \
  --coverage-from 2016-01-01 \
  --coverage-to YYYY-MM-DD
```

返回码 `0` 且 `dry_run.ready=true` 才表示十类制品的静态完整性门禁通过。返回码 `2`
表示阻断；报告会尽可能列出全部缺件和交叉覆盖异常，而不是只报第一个错误。无论结果如何，
`runtime_data_changed=false`、`production_tables_written=false`。

必须保存并独立复核报告中的：

- `plan_sha256`；
- 十类逻辑制品及所有分片的 manifest/payload hash；
- 交易日数、历史出现证券数、member-session 数和可交易 member-session 数；
- 所有 blocker，尤其是每日指数数量、证券/行业/状态/价格/公司行为缺口。

## 第二步：显式原子授权

复核 dry-run 后，用刚才的精确 `plan_sha256` 再次运行。程序会重新读取并验证全部制品，
所以复核后发生的任何变化都会让确认 hash 失效。

```bash
python scripts/preflight_production_pit_release.py \
  --approved-root /受控路径/approved-provider-artifacts \
  --bundle /受控路径/release-bundle.json \
  --trusted-keys /受控路径/approval-public-keys.json \
  --coverage-from 2016-01-01 \
  --coverage-to YYYY-MM-DD \
  --registry /受控路径/pit-release-registry.db \
  --confirm-plan-sha256 <本次复核的plan_sha256> \
  --actor-user-id <管理员用户ID>
```

登记库必须是空路径或只包含本模块的三张 append-only 表。若误指向实验库、用户库、模拟
交易库或任何已有业务 SQLite，程序在写入前拒绝。授权和所有 artifact binding 在一个
SQLite 事务内提交；重复相同计划幂等返回，部分 artifact binding 不会可见。

## 真实物化和服务切换前仍需关闭的门禁

原子授权完成后仍不能运行严谨实验。真实物化器必须另外完成并验收：

1. 用真实获许可制品运行现有隔离 generation 物化器，保存机器验收报告；
2. 对 `listed_not_trading`、退市估值等当前 runtime binding 尚不能表达的状态补齐语义；
3. 用磁盘满、进程崩溃、重复执行和制品篡改故障注入复验不可见 staging/旧代保留；
4. canary 对 20 个指数调样/上市退市/停复牌/分红样本与官方证据逐项对账；
5. 读者通过一个 generation token 切换，不能看到一半旧数据、一半新数据；
6. 激活后重新运行 runtime readiness，四池全窗口均为 ready，再由前端抽取代表策略；
7. 只有上述证据完成后，才可以运行非 PIT 缓存清理的可恢复归档门禁。

任何一步失败都保留当前活动数据和旧缓存，不执行生产删除。
