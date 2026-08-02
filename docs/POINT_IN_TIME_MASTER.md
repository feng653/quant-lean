# 点时证券、指数成分与行业主数据

> **严格 PIT/未来实盘参考。** 当前个人研究优先使用 ResearchDataStore 的版本化候选数据，
> 不要求人工批准才能运行，但必须显示本文所列 PIT 缺口；严格规则在未来实盘仍硬阻断。

## 研究边界

历史研究必须按目标交易日读取当日可知的证券状态、指数成分和行业分类。当前巨潮
分类缓存和当前指数成分仍可用于界面展示，但不能推断过去；系统把它们标为
`current_snapshot`，只允许覆盖观察日一天。

能参与历史研究的批次必须标为 `effective_dated_history`，并同时满足：

- 每条记录具有闭区间 `effective_from` / `effective_to`；
- 批次声明完整的 `coverage_from` / `coverage_to`；
- 来源身份、数据集版本、带时区 `retrieved_at` 和原始内容 SHA-256 完整；
- 来源证据等级为交易所、许可数据或已交叉验证公共来源；
- 同一 domain、scope、证券不存在重叠区间；
- 批次、payload 和逐行摘要读取时重新校验。

任一条件缺失时查询返回明确的 unavailable 原因，不使用当前分类补洞。

`point-in-time-master-import/v2` 在上述有效区间之外，要求逐记录
`effective_at/available_at`、来源 `available_at/revision` 和可选的显式
`supersedes_batch_id`；`ingested_at` 只能由服务端生成。带 `as_known_at` 的读取同时
过滤供应商可得时间与平台入库时间，只采用截止时刻已可见的 successor。修订会新增
batch/row，旧证据由 trigger 禁止修改或删除。v1 数据继续保留原等级，但返回
`bitemporal_availability_verified=false`，不能被 manifest 当作生产双时态证明。

## 受管存储

数据保存在现有 `experiment.db` 的 `pit_master_batches` 和
`pit_master_intervals` 表中，由启动迁移 `experiment-009-point-in-time-master`
创建。批次和区间都有禁止 UPDATE/DELETE 的触发器；导入使用
`BEGIN IMMEDIATE`，批次元数据与全部区间一次提交。相同内容的批次摘要构成幂等键。

API 和 readiness 只返回不透明 batch ID、内容摘要、计数与缺口，不返回数据库路径。
即使数据库触发器被绕过，读取路径也会通过 canonical JSON 和 SHA-256 发现篡改并
fail closed。

三个 domain 的固定 scope 约定：

| domain | scope 示例 | 含义 |
|---|---|---|
| `security` | `cn_equity` | A 股证券名称、交易所和上市状态有效期 |
| `index_membership` | `csi300` | 指数或受管股票池成分有效期 |
| `industry` | `cninfo_008001` | 指定分类标准下行业归属有效期 |

## 导入与刷新适配器

外部适配器实现
`backend.data.point_in_time_adapters.PointInTimeImportAdapter`，只负责取得来源并
产出一个或多个 `point-in-time-master-import/v1` 文档，不直接写数据库。受控
data-update job 再把文档提交给 `PointInTimeMasterStore.import_batch` 或管理员
导入接口。

HTTP 导入要求 `admin:users`，表示受信研究数据管理员对 source identity 与
evidence level 作人工 attestation。普通 `data:update` operator 不能自行把来源
标成 licensed 或 exchange-authoritative。`content_sha256` 只用于固定上游载荷
身份和发现后续篡改，不表示平台独立验证或认证了该来源的真实性。内部刷新任务若
直接调用 store，也必须位于同等受控的管理员数据治理边界内。

适配器必须：

1. 保留来源原始 payload 的 SHA-256；
2. 使用来源实际给出的生效/失效日期，不自行倒推；
3. 只有当前状态的提供方输出单日 `current_snapshot`；
4. 把一次完整 scope 的记录放在同一原子批次；
5. 先在固定夹具上验证代码、日期、交接日和空档，再允许生产刷新。

仓库固定夹具位于
`backend/tests/fixtures/point_in_time_master_v1.json`，覆盖证券主数据、CSI300
成分交接和巨潮行业有效期。它只用于离线契约测试，不是生产市场数据。
双时态闭环夹具位于 `backend/tests/fixtures/bitemporal_pit_ledger_v2.json`，其中价格
来源明确保持 `declared`，同样不得作为生产或权威历史数据。

## 中证指数官网成分时间线（第一批）

`backend.data.sources.csindex_pit` 实现了不联网、不入库的中证官网证据适配层。
它把采集、解析和管理员审批明确分开，避免网络任务取得一份当前名单后直接把它
扩展为历史：

1. 受控采集器在外部取得并保管官网响应原始字节，记录 HTTPS URL、
   `retrieved_at`、公告 ID 和原始字节 SHA-256；
2. 当前锚点只接受中证官网 `{index_code}cons.xls`，固定使用
   `xlrd==2.0.2` 解析 legacy XLS，并严格校验表头、观察日、指数代码、唯一代码
   和 300/500/1000 行数；
3. 公告归档必须从
   `POST /csindex-home/announcement/queryAnnouncementByVo` 无标题、主题、指数
   等筛选地遍历全部分页；每页的 POST 原文及响应原文分别摘要，分页或总数缺口
   都失败；
4. 每个被逐行判定为调样事件的公告必须经受管采集器补采并保留
   `GET /csindex-home/announcement/queryAnnouncementById?id=...` 响应和公告列出的
   全部附件。当前公告附件常为 PDF，适配层不信任通用 PDF 表格猜测；严格解析的
   调入/调出行必须与公告公布的更换数量一致，独立复核人再接受绑定该解析结果的
   proposal hash；
5. 公告写“某日收市后生效”时，新成分从显式交易日历中的下一交易日起生效。
   缺下一交易日、缺详情、缺附件、缺归档中的已复核事件、调入调出不守恒均
   fail closed；
6. 从同一观察日的 300/500/1000 当前锚点逆序回放。每一步强制 300、500、
   1000 的成分数量，三者不得重叠，并只按 `CSI800 = CSI300 ∪ CSI500` 派生
   800；不单独信任一份当前 800 列表。

完整历史的最早覆盖不能早于指数基准事件链可核验的起点：沪深 300 为
2005-04-08，中证 500/800 为 2007-01-15，中证 1000 为 2014-10-17。一个联合
300/500/800/1000 数据包因此不能把 `coverage_from` 声称为早于 2014-10-17；
若研究需要更早区间，应按 scope 分批生成并分别门禁。

适配层输出 `csindex-pit-staging/v2`，其中含证据 manifest、manifest SHA-256、
四个现有 `point-in-time-master-import/v1` 文档，以及：

```json
{
  "automatic_import_permitted": false,
  "requires_admin_attestation": true,
  "license_status": "not_attested_by_platform"
}
```

来源等级使用 `index_provider_authoritative`，仅表示成分数据来自指数的官方编制
发布者；它不声称中证指数是证券交易所，也不声称平台已取得商业数据许可。管理员
仍需在导入前审核使用条款和证据包。

staging v2 明确区分 `historical_replay` 与 `current_anchor_observation`。后者只会
生成观察日一天的 `current_snapshot` 文档，用于留存和继续采集；生产批准、受管
导入、主库 canonical CSI 区间写入和 activation 四层都会拒绝它。因此当前锚点
不能占用 `effective_dated_history` 正式区间，也不能因管理员声明而变成历史时间线。

### 证据治理与批准

`backend.data.pit_evidence_governance` 把 staging 补成受管导入链：

- 官网响应原始字节按 SHA-256 内容寻址，写入固定的
  `data/pit_evidence/artifacts/sha256/<prefix>/<digest>`；先写同目录临时文件、
  `fsync` 后用硬链接原子发布，不接受调用方文件路径，不跟随符号链接；
- 每次读取重新计算 SHA-256。缺文件、被替换、不是普通文件或摘要变化都会阻断
  批准包导入；
- `data/pit_evidence/governance.db` 只存小型 artifact 身份、完整 staging JSON、
  package/artifact 绑定、批准状态、逐 scope 导入回执和追加式审计事件，不把大
  BLOB 放入 `experiment.db`；
- 原始 artifact 逐个上传，避免单个请求携带数千个大 base64 对象；单响应上限
  25 MiB，归档 POST 请求原文上限 64 KiB，staging JSON 上限 20 MiB；
- package JSON、证据 manifest、parser version、全部 artifact/request digest
  和所有 import 文档共同形成 package SHA-256。包体和证据关联有禁止修改/删除
  触发器；
- 状态从 `pending` 通过 revision compare-and-swap 进入 `approved` 或
  `rejected`；决定原因必填，事件不可更新/删除。并发批准只有一个能成功。批准
  不能用 reason 文本代替结构化声明，必须按
  `pit-evidence-attestation/v1` 全部显式确认：所有调样行已复核、公告归档完整性
  已复核、已阅读并确认来源使用条款、本次仅限本地研究，以及不具有再分发授权。
  缺字段或任一项为 false 都不能批准；拒绝不需要这些声明。该声明只是批准人的
  法律/流程确认，不能替代下述逐行哈希复核文件；
- 只有 `approved` 包才能导入。导入前重新校验 package、全部原始字节、artifact
  关联、已保存的结构化批准声明和每份 import 文档；`PointInTimeMasterStore` 对
  `source.provider=csindex_official` 还要求治理服务在复验后签发的进程内授权，
  所以旧 `/imports` 和直接 adapter document 都不能绕过批准；
- 四个 scope 分别使用 PIT batch 幂等摘要，并逐 scope 写不可变回执。若进程在
  主库已写入、治理回执未提交之间崩溃，重试会取得原 batch 的幂等结果，再继续
  其余 scope，最终只产生四个批次。

`CsindexOfficialCollector` 可用固定官方 URL 流式采集 current anchor、无筛选归档
页、公告详情和全部附件；在读入内存前检查 `Content-Length`，流式累计也执行
25 MiB 硬上限。HTTP 客户端禁用自动跳转；每一跳先校验 HTTPS、中证官方 host、
无 userinfo，并要求 DNS 解析得到的全部地址均为公网地址，再决定是否继续，因此
不能用 DNS 重绑定或“最终 URL 又回到官网”掩盖中间的内网 SSRF 请求。采集器不会
用宽松的通用表格识别猜测 PDF 调样表。

### 可恢复历史采集与复核队列

`scripts/collect_csindex_pit_history.py` 将上述原语组织为受管运行：

- 固定配置写入原子 checkpoint；当前锚点、每一归档页、公告详情和每一附件一经
  下载即写入内容寻址存储，进程退出后从最后一个完整 artifact 继续；
- 归档仍无标题、主题或指数筛选地遍历全部分页。官网当前归档会在部分分页边界
  返回逐字段完全相同的重复 ID；运行会保留所有页原文，并把这些 ID 显式写入
  manifest。只有 canonical JSON 完全相同才允许去重，同 ID 任一字段不同即
  fail-closed；
- 首轮自动采集对标题明确出现沪深 300/中证 500/中证 1000 或其代码的“指数
  调样”条目下载详情和全部附件；这只是有界下载候选集，不是“其他公告无关”的
  分类证据。包括泛称“部分指数/等指数”的每一个归档行仍进入
  `review_queue.json`。独立复核必须为每个绑定原始字节的 row hash 写入
  `target_adjustment` 或 `not_target` disposition 和理由；全局 boolean 无效。自动
  规则识别出的明显调样候选在最终 governance 重放中也必须是 target，不能通过绕过
  CLI 直接构包把它标为 not-target。被
  人工判为目标的泛称行会通过同一受管、限速、可恢复的 collector 补采详情和全部
  附件，先保存 checkpoint；补齐逐事件 proposal 决定后可离线恢复而不重复联网；
- XLS/XLSX 只接受恰好两个 `调入`/`调出` sheet 和固定列名；PDF 只接受明确的
  沪深 300/中证 500/中证 1000 分节、成对调出/调入列。重复分节、未解析的
  六位代码行、数量不符、正文缺明确“收市后生效”日期或多附件同时可解析均
  进入 review queue，不生成事件；
- 请求有最小发送间隔、流式大小上限、有限指数退避重试和逐 artifact 审计。
  最终 `coverage_report.json` 同时列出请求范围、可证明范围、缺口、阻断原因、
  锚点/归档/复核/日历摘要和 pending package ID。

示例（命令只会 staging，不会批准或导入）：

```bash
python scripts/collect_csindex_pit_history.py \
  --workspace data/pit_evidence/history_runs/csi-history \
  --evidence-root data/pit_evidence/history_runs/csi-history/governance \
  --governance-db data/pit_evidence/history_runs/csi-history/governance/governance.db \
  --master-db data/pit_evidence/history_runs/csi-history/master.db \
  --from 2015-01-01 --actor-user-id 1
```

历史包还必须提供 `authoritative-trading-calendar/v2` 和
`csindex-pit-review-decisions/v2`。日历 provider、evidence level、version、
retrieved_at、原始文件 SHA-256、完整 sessions 和 sessions SHA-256 均进入
manifest；`explicit_unattested_input` 或非 `licensed` / `exchange_authoritative`
日历只能 staging，永远不能批准或导入。等级也不能由 JSON 自报：日历正文必须由
`PIT_CALENDAR_TRUSTED_KEYS_JSON` 中预置的 Ed25519 公钥验证，key 条目同时固定精确
provider 和允许的 evidence level；仓库默认信任注册表为空，未由运维在仓库外配置
信任锚时所有历史包保持 fail-closed。签名及其 signed-payload hash 写入不可变
auxiliary provenance。复核文件精确绑定 archive manifest、每个
归档行的 row hash/disposition/reason、disposition 集合摘要及每个目标事件的
proposal hash。治理服务在 staging 和 import 时从留存的 XLS、归档页、详情、
附件、逐行复核文件和交易日历重新解析 proposal，再逐字节比较四份 imports；审核
文件中的 proposal hash 若不等于独立重放结果，即使内容摘要自洽也会被拒绝。

review v2 的 `reviewer` 必须同时包含平台 `user_id`、显示 identity 与带时区审核
时间。审核文件首次写入受管存储时，已认证 API actor 必须等于该 user ID；package
stager、reviewer 与 approver 必须是三个不同用户。构包脚本若遇到尚未由 reviewer
通过 auxiliary-artifact API 登记的决定文件，只记录结构化 blocker，不会把构包
actor 冒充审核人。

CSI 300/500/800/1000 的 `index_membership` 由 scope/domain 强制治理，改写
provider 或 evidence level 不能绕过。四个 scope 的 batch 先进入 quarantine；
治理库的四份 durable receipt 全部成功后，主库才在单事务写入四份 activation。
resolver 只读取已 activation 的 batch，因此第三个 scope 永久冲突、进程在中途
崩溃或缺任一 receipt 时，先写入的 scope 也不会泄露到研究路径。

仍未自动化的真实生产边界：

- 必须完整遍历目标时间窗内的公告归档，并由独立审核人员对每条绑定 hash 的公告
  是否属于目标指数调样作全量 disposition；不能只按标题关键词，也不能用全局
  “已复核”布尔值代替；
- 每个目标 disposition 必须完成受管详情/全部附件补采，并由独立复核人接受严格
  解析产生的精确 proposal hash；老公告 403 等无法形成官方证据闭环的行仍是 gap；
- 必须取得并绑定符合准入 evidence level 的权威交易日历 artifact；人工输入的日期
  列表、未注册签名 key 或全局 attestation 不能提升其等级；
- `license_status=not_attested_by_platform` 不会因技术批准自动变成商业许可。
  `source_terms_acknowledged` 只证明管理员完成了条款确认动作，不是平台作出的
  法律许可结论；若用途超出本地研究，必须重新取得合适授权；
- 完成上述采集和审核前，生产库仍不得声称已具有完整 PIT 历史覆盖。

## Readiness 语义

`GET /api/data/point-in-time/coverage` 和因子研究 readiness 分别报告：

- `universe`：研究区间内完整的成分时间线，且所有历史成员都有行情列；
- `security_master`：成员有效期内证券身份无缺口；
- `industry`：成员有效期内行业归属无缺口；
- `neutralization_ready`：只有行业项完整时才为真；
- `ready`：三项全部满足。

普通 schema-v4 行情仍可在其原有用途门禁下使用，但旧字段
`ready_for_return_research` 只表示静态集合上的调整价/来源检查可用于探索性价格
研究，不能作为晋级证据。只有 PIT 时间线、规范研究价格账本、公司行为及运行时
batch/hash 全部绑定后，新字段 `ready_for_unbiased_return_research` 才能为 true；
当前运行时仍读 Parquet，因此该字段以及 `ready_for_unbiased_research`、
`ready_for_unbiased_tuning` 都保持 false。

## 运行时解析与执行语义

历史预设池不再通过当前成分接口构建研究缓存。受控刷新和实验入口先用共享
`point_in_time_universe` resolver 解析实际计算窗，再以全部历史成员的 union
构建 schema-v4 行情缓存。resolver 会逐交易日验证：

- 成员集合非空，固定规模指数的每日数量与契约一致；
- 所有历史成员均有完整 OHLCV 列；
- 批次覆盖无缺口，批次与逐行摘要通过完整性校验；
- 选股子集和行业筛选在每个交易日应用，行业分类不回退当前快照。

行情、成分时间线和来源批次是三个不同证据层。入池前已经公开的历史行情可以用于
计算动量等单证券回看特征，不能简单置空，否则新成分会系统性缺少特征；但每个
研究日的因子排名、Top-K 分母、组合权重、组合子策略和 regime 市场横截面必须先
消费当日点时资格。平台以不可跨 job 泄漏的 `StrategyResearchContext` 提供资格；
规则策略必须显式声明并实现经复核的 capability，未声明的新旧策略直接
`strategy_point_in_time_context_not_supported`，信号生成后的 BUY 校验仅是第二道
防线，不能代替计算阶段门禁。当前 22 个内置策略加 1 个数据定义因子契约均有红队
测试：未来成员价格变化不得改变当日结果；其中训练型策略走下述独立硬阻断。

h 期因子标签定义为“t 日成员证券从 t 到 t+h 的固定期证券研究收益”，样本资格
只读 t 日当时可知的成分。不得因为 t 之后的调样决定删除 t 日样本，否则会用未来
信息选择今天的截面。证券在 horizon 内退池仍需要完整、可信的 t+h 研究价格；该
标签不模拟指数调样交易、收盘竞价或 raw 成交。IC、RankIC、分层收益、衰减、组合
质量与三段稳定性全部使用同一 origin-date 规则。

回测 BUY 同时要求信号日和下一交易日都在池中。官网“某日收市后生效”的调样由
主数据映射到下一交易日成员；已有持仓在首个非成员交易日开盘使用完整的复权研究
价格带尝试退出，若停牌则后续交易日继续尝试。这只是
`research_next_session_open` 兼容处置，不是指数跟踪、调样日 raw 收盘竞价或已认证
成交；manifest 固定记录 `adjusted_research_compatibility_not_raw_execution`、
`execution_certified=false` 和限制项。`raw_effective_close_auction` 接口已预留但
当前 fail-closed，必须等原始执行账本和竞价语义完成后另行实现。

每次运行的 universe snapshot 绑定语义 `timeline_hash`、来源 batch digests、
覆盖区间和压缩后的逐交易日成员区间。精确重放从 manifest 重建时间线、复核摘要，
并对同一不可变行情快照重新执行资格门禁；不重新查询当前成分。

可训练策略目前尚未统一暴露“训练样本资格”和“训练标签资格”两个平台所有的
钩子。为避免特征可见但训练标签混入非成员样本，所有 `TrainableStrategy` 以及
自管理训练器默认 fail-closed：缺 PIT 上下文返回
`ml_point_in_time_universe_not_available`，有 PIT 但缺任一 mask 返回
`ml_point_in_time_label_eligibility_not_supported`。`custom`、`all_a` 或用户声明的
静态篮子不能绕过；静态篮子也不能晋级为无偏研究或实盘候选。只有平台能逐训练样本
和标签复验两个 mask 后才允许新增受控放行路径。
