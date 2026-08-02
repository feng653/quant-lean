# 项目工作报告：可信研究、模拟盘、数据源与开源平台学习

> 状态日期：2026-08-02（Asia/Shanghai）
> 项目基线：`110148b`
> 适用范围：个人本机的 A 股日频研究与模拟交易，不构成实盘认证或投资建议。
> 结论口径：本文严格区分“已在本机验收”“代码具备但未完成生产验收”和
> “缺外部数据、许可或授权”。接口存在、测试通过或供应商声称覆盖某字段，都不
> 自动等于数据具有 PIT（point-in-time）证明、研究无偏或可用于实盘。

## 一、结论先行

平台现在已经是一个**边界较清楚、失败时倾向阻断的本地研究与模拟工作台**，但还
不是一个能用生产数据严谨完成无偏实验、再把结论安全晋级到可置信模拟盘的平台。
主要瓶颈已经不是页面或策略数量，而是以下依赖链中最靠前的三项：

1. 没有装载一条覆盖研究窗口的、获许可且可重放的生产 PIT 数据链；
2. 原始成交价、研究复权价、调整因子、公司行为、停复牌/ST/可交易状态尚未以同一
   证券—交易日粒度完整绑定，且公司行为还没有进入持仓与现金状态机；
3. 数据供应商通常给出“历史记录”，却未必给出历史决策当时真实可得的
   `available_at`、后续 `revision` 和可保留的原始版本。没有这些字段，历史数据库
   很容易在事后修订后悄悄产生前视偏差。

因此，现有 122 次非 ML 单策略调优只能保留为“当前成分、非 PIT 数据上的条件性
研究”，不能称为无偏最优参数；现有模拟盘代码可以保存信号、订单、持仓和净值，
但在生产双账本、交易状态和公司行为未闭环时，不能把其成交与收益解释为可实盘
复现。

最适合个人用户的采购策略不是直接购买最贵的全库，而是先做一个有退出条件的
数据 PoC：以 **Tushare Pro 或 JQData/RQData 之一作为结构化候选主源**，以中证指数、
上交所、深交所、巨潮等官方材料做事件与时间证据，以 BaoStock/AKShare 做只读
交叉核验；如果 PoC 证明候选主源不能导出历史指数/行业/状态、真实可得时间和修订，
再向 **Wind WDS/Server API 或 Choice 量化接口**询价。免费源适合开发和质量对账，
没有一个经本次调研可被证明能单独满足本项目全部生产 PIT 契约。

## 二、现状分层：什么已经是真的，什么还只是能力

### 2.1 已在本机或真实浏览器链路验收

| 能力 | 已验证事实 | 仍不可外推的结论 |
|---|---|---|
| 服务边界 | 2026-08-01 实机 backend、frontend、backup、Caddy 由 launchd 管理；8000/5173 仅监听 loopback；本地、前端和公网健康检查均为 200；公网 `/docs`、`/redoc*`、`/openapi.json` 为 404；watchdog 每 30 秒运行且最近退出码为 0 | 不等于 MFA、外部告警、主机防火墙和全套互联网渗透测试已经完成 |
| 实验与任务链 | 实验、扫描、结果、交易和任务写 SQLite；有任务租约、心跳、取消、重试、陈旧 claim 回收、阶段超时和结构化失败 | 数据不可信时，任务完成只证明软件跑完，不能证明结论可信 |
| 研究工作流 | 前端有策略库、实验中心、因子研究、相关性分析、证据导出和研究晋级门禁；因子链路已经过真实浏览器提交/恢复/比较/下载验收 | 已验收 run 不能替代生产 PIT 数据，也不能自动发布为模拟或实盘策略 |
| 条件性调优 | 122/122 个非 ML 单策略基线、扫描和锁定实验从真实前端完成并与数据库核对，选模窗口 2016–2022、锁定窗口 2023–2026-06 | 股票池仍有当前成分幸存者偏差；不能称为无偏最佳参数或未来收益证明 |
| 模拟盘产品闭环 | 已实现批准候选绑定、组合版本、T 日信号、T+1 开盘模拟成交、幂等运行、订单/持仓/净值和历史回放 | 原始执行价与公司行为主路径未达生产就绪，不能把模拟结果解释为真实可成交结果 |
| 本机与异地密文备份 | SQLite online backup、完整性检查、scrypt + AES-256-GCM、manifest 哈希和隔离恢复工具已落地；2026-08-01 新归档 507,689,348 bytes 已完成本地数据库完整性验证，并于 `2026-08-01T16:02:36Z` 上传到 GitHub private Release `quant-platform-encrypted-backups-v1`，状态为 `uploaded`、保留数 30 | 上传成功不等于远端可恢复；仍需从远端重新下载、核对 digest、解密并完成隔离恢复/服务重建，才能据实声明 RPO/RTO 和异机恢复闭环 |

### 2.2 代码具备，但生产验收或主路径消费尚未完成

| 能力 | 代码已经做了什么 | 未完成的生产验收 |
|---|---|---|
| PIT master 与治理 | 当前锚点只能进 quarantine；历史包绑定 artifact hash、逐行复核、签名日历、职责分离、四个 scope receipt 和 activation；正式入口统一使用 PIT resolver 并 fail-closed | 生产 `pit_master_batches`/intervals 仍为空；真实归档尚无完整独立复核、可信日历和批准激活 |
| 独立 PIT 自动更新 | durable `collect → validate → classify → import → activate → canary → monitor`，有阶段截止、幂等键、租约和重启恢复 | 当前适配器缺可自动晋级的绿色生产数据；真实连续运行、跨日补数和告警仍待观察窗 |
| 双价格账本 | v2 记录 `effective_at/available_at/ingested_at/revision`，区分 `raw_execution` 与 `research_adjusted`，不可变批次、运行时 binding 和完整性复验已实现 | 生产价格、权威交易状态和公司行为记录未装载；普通实验 manifest 明示原始账本“已绑定快照但未被引擎消费” |
| 公司行为 | event 与 `confirmed_no_event` 均可追加保存，冲突原子拒绝，复权因子异常必须有证据解释 | 拆分、送转、分红、配股尚未确定性改变模拟盘的持仓数量、现金和成本基准 |
| 研究晋级 | 锁定期、benchmark、非零成本、容量、稳健性、模型产物和不可变报告有服务端门禁；批准可被撤销并在部署/运行时复核 | 全证据 production promotion 尚无法在真实 PIT 数据上完成端到端验收 |
| 资源治理 | 8GB 主机有动态并发容量、任务互斥和因子 CPU spawn 隔离 | 模型训练及部分重 CPU 代码仍是有界线程，尚未全部迁到可杀死、可限额的隔离进程 |
| 可观测性 | 已聚合任务、刷新、缓存质量、WebSocket、重启和失败率，watchdog 可恢复进程 | 缺独立外部告警、确认/升级、备份新鲜度、NTP、订单风险和对账 SLO |

### 2.3 缺外部数据、商业许可或账号授权

- 2016 至今的逐日证券主数据：上市、退市、暂停上市、代码/名称变更、ST 状态；
- 沪深 300/500/800/1000 的历史成分和权重，以及每次调样的公告、公告时间和生效日；
- 历史行业分类和逐日规模字段，至少能说明分类版本、何时生效、何时对研究者可得；
- 同源 raw OHLCV/成交额、复权因子、分红送转配股等公司行为和明确“无事件”状态；
- 权威交易日历、逐日停复牌与涨跌停/可交易状态；
- 供应商允许个人本地保存、备份、派生研究和保留旧修订的书面条款；
- 能够证明历史时点可知性的发布时间或 `available_at`。只有 `trade_date`/`report_date`
  不够，事后清洗后的“最终正确值”也不是 PIT。

### 2.4 当前最短板与依赖顺序

```text
数据许可与供应商字段确认
  → 受管原始 artifact / available_at / revision 留存
  → PIT 证券池、行业、证券状态激活
  → raw + adjusted + adjustment/corporate-action 双账本绑定
  → 公司行为持仓/现金状态机 + 可交易性撮合
  → 小样本 canary 与跨源核验
  → 重新运行预注册实验、扫描、锁定测试
  → 人工研究晋级
  → 模拟盘连续观察、对账、恢复演练
```

在这条链上继续增加策略或图表的边际价值已经低于补齐数据；任何绕过数据门禁的
“临时可运行”都会让平台看起来更好用，却降低研究真实性。

### 2.5 本报告采用的项目内证据

- [README 全流程与当前边界](../README.md)明确正式研究只接受激活 PIT，当前缺数据时
  应在建任务前阻断；
- [ROADMAP 总账](ROADMAP.md)逐项记录功能、验收范围和生产阻塞，尤其是 141–153；
- [PIT 主数据说明](POINT_IN_TIME_MASTER.md)记录当前锚点隔离、证据治理、逐行复核、
  签名交易日历、四 scope receipt 和仍未自动化的生产边界；
- [双价格账本](DUAL_PRICE_LEDGER.md)定义 raw/adjusted 用途、双时态导入、完整性检查
  和 `corporate_action_runtime_application_missing` 阻塞；
- [PIT-only 运行安全加固记录](reports/PIT_RUNTIME_SECURITY_HARDENING_20260801.md)
  列出 18 项已验收门禁及仍未关闭的 10 类风险；
- [非 ML 单策略调优协议与结果](research/NON_ML_SINGLE_STRATEGY_TUNING_20260731.md)
  说明 122 次前端实验的窗口、协议和条件性边界；
- [模拟盘工作流](PAPER_TRADING_WORKFLOW.md)记录 T/T+1 语义、幂等运行和当前模拟范围；
- [本机安全与恢复](LOCAL_SECURITY_AND_RECOVERY.md)记录 loopback/Caddy、LaunchDaemon、
  密文归档、GitHub private Release 和恢复协议。

## 三、距离“安全、严谨完成实验”的具体差距

### 3.1 数据真实性

1. **幸存者偏差仍未消除。** 当前成分快照不能回填历史股票池。退市股、被调出股、
   后来加入的赢家必须按每个观察日重建资格。
2. **双时态不是日期列命名。** 平台需要业务生效时间 `effective_at`、当时可获取时间
   `available_at`、本机摄取时间 `ingested_at` 和供应商修订序号。供应商若只返回
   最新修订后的历史表，平台只能把它标记为 declared/conditional。
3. **行业和规模必须逐日/逐期 PIT。** 用今天的行业给 2016 年股票做中性化同样会
   泄漏；财务与规模还要遵守公告日而不是报告期。
4. **价格必须覆盖每个 PIT member × session。** 缺行情只能由权威停牌/不可交易状态
   解释，不能用前值、删行或后来成分列表静默填补。
5. **公司行为需要事件与无事件证明。** raw 加事件应能确定性复算研究价；异常复权
   跳变、常数锚变化和实际收益冲突必须分类处理。

### 3.2 研究方法

- 重新调优前需固化假设、参数预算、训练/验证/锁定窗口和淘汰规则，避免看到全样本
  后再修改标准；
- 同一策略的大量参数和多个策略共同选择会产生多重检验偏差；现有 PSR/DSR、
  CSCV/PBO 是诊断，不是免检证书；
- 成交成本至少要按换手、参与率、涨跌停、停牌、佣金/印花税和流动性分层压力测试；
- 相关性不应只看全样本 Pearson，还需看尾部相关、持仓重叠、状态分段和组合边际
  风险；平台已有只读诊断，但必须在生产 PIT 输入上重跑；
- 每个结论都要绑定 data batch、timeline、价格 binding、代码 commit、参数、随机种子、
  benchmark 和不可变结果摘要。

### 3.3 安全与可重复性

- 管理员仍缺 MFA、refresh token 轮换/重放检测、设备审计和细粒度限流；单人平台可
  简化审批人数，但不应取消数据版本、不可变证据和晋级前复核；
- SQLite 适合当前单机规模，却不提供跨数据库原子快照、WORM 或多机高可用；在并发
  导入增多前，应先加入 generation token，一致性需求再升级 PostgreSQL/对象存储；
- joblib/PyTorch 等可执行反序列化产物不应接收不可信来源，长期应迁移到更受限的
  格式并校验 schema/hash；
- 仓库根目录当前没有 LICENSE/COPYING/NOTICE。只供本人本机使用时影响较小，但任何
  公开、分发或引入第三方实现前，必须先确定项目许可证、依赖 notices 和兼容边界；
- 需要一条贯穿 data package → experiment → model → promotion → deployment → paper run
  的 correlation ID，以及每日签名审计根；
- 应完成一次真正的私有远端下载、离线解密恢复、服务重建演练，再据实测给出 RTO。

## 四、距离“安全、严谨模拟盘部署”的具体差距

模拟盘不是“回测多跑一天”。它需要每日在时间边界内重演当时可知的数据和实际可
成交条件。当前剩余 P0 项是：

1. 将 `raw_execution` 正式接入成交和收盘估值，禁止调整价用于订单金额；
2. 公司行为状态机在除权日改变持仓数量、现金、成本基准和待结算权益，并与复权
   因子做守恒核对；
3. 逐日停牌、ST、涨跌停、一字板和上市/退市状态进入订单拒绝、延迟或取消逻辑；
4. 固定模拟运行的 as-known-at cutoff，越过盘前/盘后边界的数据不得被当日使用；
5. 将预测信号、目标仓位、订单、部分成交/拒绝、现金、持仓和净值做端到端守恒与
   每日自动对账；
6. 在重启、重复调度、数据修订和服务降级下做至少一个完整月的 canary，验证幂等和
   失败恢复；
7. 外部告警覆盖数据未更新、时钟漂移、备份过期、任务积压、订单异常和净值不守恒；
8. 只有重新通过 PIT 实验、锁定测试和 promotion 的候选才能进入模拟盘，旧实验继续
   `legacy_read_only`。

## 五、数据源调研方法与判定标准

调研时间为 2026-08-02。先检查本机 `127.0.0.1:7890`，当时无监听，故没有强行启动
或修改 Clash，改用正常网络访问。资料优先级为：供应商/交易所官方文档和条款 >
官方仓库 > 社区讨论和独立文章。社区材料只用于发现运维体验与待验证问题，不作为
字段、价格或许可事实。

评分关注以下问题：

- A 股日线、成交额、停牌/ST、上市退市、行业、历史指数成分、权重、公司行为；
- raw、调整因子、复权价能否同时取得，能否保留供应商版本；
- 是否有公告/发布时间、历史可得时间和修订记录，而不只是业务日期；
- 个人是否能注册、试用、批量导出并在本地长期保存；
- 限流、服务稳定性、错误修订通知、SLA 和技术支持；
- 许可是否允许本地研究、加密备份和派生结果，是否禁止再分发；
- 总成本包括订阅、人工复核、适配器维护和数据质量事故，而不仅是 API 标价。

## 六、适合本项目的候选数据源排行

### 6.1 个人综合适合度

| 排名 | 数据源/组合 | A 股与关键字段 | PIT 能力判断 | 个人成本/获取 | 本项目角色与结论 |
|---:|---|---|---|---|---|
| 1 | Tushare Pro + 官方公告证据 | 日线、复权因子、停复牌、每日指标、分红送股、指数/申万行业等覆盖较广 | 接口有业务日期和更新时间说明，但本次未找到统一的不可变 `available_at/revision` 契约；不能单独认证 PIT | 手机注册/token；基础非复权日线有零积分档，复权因子等按积分，部分数据另购；实际价格和权限以[官方权限表](https://tushare.pro/document/1?doc_id=290)为准 | **最适合先做低成本本地 PoC**。需由平台自行保留每日原始响应、摄取时间和修订，历史成分/行业仍用官方证据核验 |
| 2 | RQData | 官方列出 A 股 2005 至今、raw/复权行情、历史权重、行业变化、上市退市、公司行为和风险因子等 | 部分接口提供 `info_date/create_tm/rice_create_tm`，更新记录也提及成分入库时间，技术上较接近本项目；但这不证明所有表都有统一修订历史 | 免费试用，正式价格/权限需登录或商务确认 | **最值得做字段级付费 PoC 的第二主源**。若个人报价和本地保存许可合适，可能比自行拼接免费源省维护成本 |
| 3 | JQData | 官方文档明确支持指定日期的历史指数/行业成分，另有退市证券、行情、定点复权、ST/停牌和财务等 | `date` 参数和 `avoid_future_data` 限制优于当前快照；官方社区答疑也提示部分财务历史修订并非完整保留，不能单独认证全链双时态 | 可试用，正式历史范围/流量依账号；价格需登录或咨询，不在报告中猜测 | **易用的 PIT 股票池/行业候选**。签约前要求样例证明 2016 起四池、退市股、财务修订、首次可见时间和本地保存条款 |
| 4 | 中证指数 + 上交所/深交所/巨潮官方材料 | 指数规则/调样公告、交易日历、公司公告和分类是高等级事件证据 | 公告发布时间与原文适合形成 PIT 证据；通常不是完整、易查询的逐日研究数据库 | 免费公开访问，但历史归档解析和逐行复核人工成本高，且需逐站阅读使用条款 | **必须保留的证据层，不是单一主行情源**。内容寻址保存原始字节、URL、检索时间与 hash |
| 5 | BaoStock | 免费 A 股历史 K 线、成交额/交易状态、复权选项/因子，接入简单 | 无统一 available/revision 证明；当前代码也只将其标为 `declared` | 免费、登录式 API、适合批量日线 | **交叉核验与断链应急候选**，不可直接提升成 licensed/authoritative，不独立解释公司行为 |
| 6 | AKShare | 覆盖大量公开网页接口，行业/指数/行情发现能力强 | 聚合/抓取源经常缺稳定 schema、版本和 PIT 时间；官方风险页也列出复权负价等已知问题 | 免费开源、零 token，上手最快；上游页面变化会导致维护成本 | **发现与二源质量报警**，不作为生产 canonical 主源，更不能把公开网页等同可再分发许可 |
| 7 | Wind WDS / Server API | 机构级 A 股、行情、行业、公告等覆盖，数据库同步/API/质量支持较完整 | 最有机会按合同取得历史版本、发布时间、修订和服务支持，但必须在数据字典/合同中逐字段确认 | 企业销售、询价和授权流程较重；通常不符合“最好免费”，不得用终端抓屏代替 Server API 授权 | **免费/中价 PoC 失败后的最高质量升级项**。优先询问 WDS 数据库/FileSync 或 Server API，而非只买个人终端 |
| 8 | Choice 量化接口 | 官方指南列出基本面、财务、历史/实时行情、多资产，支持 macOS/Linux/Windows 与 Python 等 | 可能满足大量结构化字段，但公开指南没有证明本项目所需完整双时态/旧修订 | 可联系客户经理试用；价格和字段授权需询价 | **Wind 的价格竞争备选**。用同一书面 PoC 清单比价，不因终端看得到字段就假设 API 可导出或可长期保存 |

这里的排名是“个人本项目综合适合度”，不是绝对数据质量榜。若只比较覆盖、SLA 和
厂商支持，Wind/Choice 会排在免费聚合源之前；若只比较零成本和调用便利，AKShare
会更靠前，但它不能关闭本项目的严谨性门禁。

### 6.2 推荐落地组合

**方案 A：先验证、低成本。** Tushare Pro 为候选结构化主源；中证/交易所/巨潮为
公告和时间证据；BaoStock 为价格与交易状态交叉核验；AKShare 仅做目录发现和第三方
报警。优点是成本和接入门槛低，缺点是平台要自行建立每日 append-only 原始响应、
修订侦测和大量历史 PIT 补证。

**方案 B：个人付费、减少自建。** 对 RQData 与 JQData 做同一份 30 只证券、四个
指数、10 个历史调样日、退市/ST/停牌/分红案例测试，只购买能书面允许本地持久化且
样例通过的一家；官方材料和 BaoStock 仍保留。不要只看回测云平台中的函数，因为
本项目要求数据真正落入本地不可变账本。

**方案 C：可信度优先。** 让 Wind 和 Choice 分别按字段清单提供试用及合同答复，
采购能交付 Server API/数据库同步、历史版本/修订/发布时间和本地备份权利的一家。
即使选择付费源，也要保留官方事件证据和独立价格交叉核验；品牌不能替代逐字段验收。

### 6.3 不适合作为 A 股生产主源的国际服务

| 数据源 | 可取之处 | 不适合本项目主源的原因 |
|---|---|---|
| [Nasdaq Data Link](https://docs.data.nasdaq.com/docs/getting-started) | API/SDK 成熟，有免费与付费数据集 | 数据集逐项授权，重点不在中国 A 股 PIT；官方条款对再分发、SaaS 和派生数据有明确限制，适合作宏观/海外补充而非四个 CSI 池主库 |
| [Alpha Vantage](https://www.alphavantage.co/documentation/) | 全球股票 raw 日线、adjusted close、split/dividend 接口，上手快 | 免费档官方说明为每天 25 次且 full history 为付费能力；缺 A 股历史成分、行业、ST/停牌和本项目双时态证明 |
| Polygon/Massive | 美国 SIP 行情、REST/WebSocket/flat files 和公司行为体系较完善 | 面向美国市场，不解决 A 股 PIT；市场数据授权和展示/再分发条款需单独遵守 |
| Stooq/Yahoo 类公开下载 | 适合教学、宏观或海外价格的低成本交叉检查 | 来源、修订、许可和公司行为链不足，且不覆盖本项目 A 股主数据，不应进入 canonical production ledger |

### 6.4 签约或充值前必须让供应商书面回答

1. 能否按**历史日期**返回指数成分、权重、行业、ST/停牌、上市退市，并包含已经退市
   的证券？最早覆盖日和缺口是什么？
2. 每行是否有生效时间、对客户可用时间、最后修订时间、修订序号和来源公告 ID？
   数据修订后能否重取旧版本？
3. raw OHLCV/amount、复权因子和完整公司行为是否同源、可逐日确定性复算？
4. 是否允许个人在本机长期存储、做加密异地备份、生成不可逆派生指标？终止订阅后
   是否可继续保留？是否禁止模型/数据再分发？
5. 全量回填和每日增量的配额、频率、单次行数、并发、SLA、历史修订通知和支持渠道？
6. 提供 30 只样本证券在除权、停牌、ST、退市和代码变更前后的原始输出，以及
   10 个历史调样日的四池结果；平台先跑守恒、重叠、数量和 as-known-at 测试再采购。

## 七、来源说明与用户讨论的正确用法

### 7.1 主要官方来源

- Tushare：[数据目录](https://tushare.pro/document/2)、
  [复权因子](https://tushare.pro/document/2?doc_id=28)、
  [指数历史成分权重](https://tushare.pro/document/2?doc_id=96)、
  [申万行业历史成员](https://tushare.pro/document/2?doc_id=335)、
  [数据服务许可](https://tushare.pro/document/1?doc_id=405)、
  [积分与单独权限](https://tushare.pro/document/1?doc_id=290)。
- JQData：[数据范围与 SDK 文档](https://www.joinquant.com/help/api/doc?id=9845&name=JQDatadoc)、
  [历史指数与行业成分说明](https://www.joinquant.com/help/data/stock?f=home&m=footer)。
- RQData：[Python API 手册](https://www.ricequant.com/doc/rqdata/python/index-rqdatac)、
  [股票字段与 `info_date`](https://www.ricequant.com/doc/rqdata/python/stock-mod.html)、
  [更新记录与入库时间字段](https://www.ricequant.com/doc/rqdata/python/changelogs)、
  [产品覆盖说明](https://www.ricequant.com/welcome/rqdata)。
- AKShare：[项目说明与使用风险](https://akshare.akfamily.xyz/introduction.html)、
  [使用与商业限制说明](https://akshare.akfamily.xyz/special.html)、
  [数据问题示例](https://akshare.akfamily.xyz/data_tips.html)。
- BaoStock：[复权因子说明](https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)、
  [免责声明](https://baostock.com/disclaimer)。
- 官方证据锚点：[上交所数据服务](https://star.sse.com.cn/transparency/services/)、
  [深交所数据服务](https://www.szse.cn/English/services/dataServices/index.html)、
  [巨潮资讯](https://www.cninfo.com.cn/new/index)、
  [中证指数](https://www.csindex.com.cn/)、[国证指数](http://www.cnindex.com.cn/)。
- Wind：[WDS 总览](https://www.wind.com.cn/mobile/WDS/zh.html)、
  [Server API](https://www.wind.com.cn/mobile/WDS/sapi/zh.html)、
  [数据库同步](https://www.wind.com.cn/portal/zh/WDS/database.html)、
  [历史/实时行情服务](https://www.wind.com.cn/portal/zh/WDS/marketdata.html)。
- Choice：[终端与量化接口指南](https://choice.eastmoney.com/FileDownload/Guide/Choice%E9%87%91%E8%9E%8D%E7%BB%88%E7%AB%AF%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97.pdf)、
  [量化接口手册入口](https://quantapi.eastmoney.com/Manual?from=web)、
  [联系与登录入口](https://choice.eastmoney.com/Account/Login)。
- 国际补充：[Nasdaq Data Link 文档](https://docs.data.nasdaq.com/)、
  [Nasdaq 数据许可条款](https://data.nasdaq.com/terms)、
  [Alpha Vantage 文档](https://www.alphavantage.co/documentation/)、
  [Alpha Vantage 限额说明](https://www.alphavantage.co/support/)、
  [Polygon 股票数据概览](https://polygon.io/docs/rest/stocks/overview)。

### 7.2 社区讨论与文章（仅作线索）

- [V2EX 的早期米筐社区讨论](https://www.v2ex.com/t/407029)可以了解学习者体验，
  但发布时间较早，不能证明 2026 年产品权限或数据质量。
- [腾讯云社区的数据获取文章](https://developer.cloud.tencent.com/article/1425095)
  提到云平台数据导出和 Tushare 速度等个人体验；这是作者观点，不是供应商 SLA。
- [2026 免费数据源实践文章](https://agents-quant.com/blog/free-quant-data-sources-guide/)
  可用于发现运维问题和候选源；其中商业报价、免费额度和质量评价必须回到官网复核。
- Reddit 关于 [Backtrader 与 Zipline](https://www.reddit.com/r/algotrading/comments/efvtel/)
  的讨论反映部分用户认为 Zipline 自定义数据导入较繁琐、Backtrader 更灵活；这是
  旧社区样本，不是性能基准或当前维护状态事实。
- Reddit 的 [开源回测引擎讨论](https://www.reddit.com/r/quant/comments/16jgnj3/)
  提醒 LEAN 自托管仍需自备数据，也涉及云端数据导出限制；具体许可必须查官方条款。
- [Qlib PIT collector issue](https://github.com/microsoft/qlib/issues/1875)曾记录上游端点
  变化导致 collector 停滞并在之后修复；这是维护记录，不代表长期 SLA。
- [LEAN CLI 的本地数据讨论](https://github.com/QuantConnect/lean-cli/issues/1)反映过
  开源引擎与云端/付费数据可得性的落差；这是历史 issue，当前能力应回官方文档复核。
- [Zipline-reloaded issues](https://github.com/stefan-jansen/zipline-reloaded/issues)中
  的 DataPortal、calendar、slippage 或依赖兼容报告是待复现线索，不是已确认缺陷清单。
- [VeighNa DataFeed 与 Database 讨论](https://www.vnpy.com/forum/topic/30202-shu-ju-ku-database-he-shu-ju-fu-wu-datafeed-de-qu-bie)
  显示用户容易混淆在线源和本地库，支持本项目继续在 UI 区分采集、隔离和激活数据。

## 八、开源平台代码学习报告

本次把第三方仓库 shallow clone 到 `/private/tmp/quant-oss-review.R77pAR` 下独立临时
目录，只读检查 README、许可、主要包结构和数据/执行抽象；另一个被中止的重复临时
检出位于 `/private/tmp/quant-oss-review.OZnwWd`。两者合计约 107 MB，没有运行中的
clone/checkout 进程，没有把第三方代码复制进本项目，也不会随本报告提交或进入项目
备份。
commit 是调研时远端默认分支的瞬时快照，不代表稳定版本或安全认证。

| 项目 | 官方仓库 | 本次审查 HEAD / commit 时间 | 许可证 |
|---|---|---|---|
| Microsoft Qlib | [microsoft/qlib](https://github.com/microsoft/qlib) | `79633dd9506ea689e5400dea0197717b5b3d74b7` / 2026-07-23T16:15:29+08:00 | MIT |
| QuantConnect LEAN | [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | `962fcd6b58a56d7a52cf7178a42b965ff3681115` / 2026-07-30T18:15:44-03:00 | Apache-2.0 |
| Backtrader | [mementum/backtrader](https://github.com/mementum/backtrader) | `b853d7c90b6721476eb5a5ea3135224e33db1f14` / 2023-04-19T16:13:08+02:00 | GPL-3.0 |
| Zipline-reloaded | [stefan-jansen/zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | `943010b9da848e317fc520de87edade2b884d329` / 2025-11-13T10:14:32-05:00 | Apache-2.0 |
| VeighNa/vn.py | [vnpy/vnpy](https://github.com/vnpy/vnpy) | `1b78494979deb4c4996f6b864f234d9839f2f239` / 2026-05-17T16:05:38+08:00 | MIT |

### 8.1 Microsoft Qlib

Qlib 的核心长处是 ML 研究流水线：Data Handler/Provider、Dataset、Model、Strategy、
Recorder、workflow 和分析组件形成可配置实验图，还单独提供
[Point-in-Time database](https://qlib.readthedocs.io/en/stable/advanced/PIT.html)。其 PIT
实现以财报发布日期、报告期、值和版本链限制 `cur_time` 查询，是很好的财务修订
参考；官方文档也明确其当前范围主要是季度/年度财务数据，不能替代证券、行业、指数
成员、交易状态、价格和公司行为的全域双时态治理。
它比本项目成熟的部分是特征/标签处理器、模型与数据集配置化、实验 recorder、丰富
基线模型和在线策略研究；本项目更强的是个人网页工作流、RBAC、任务状态、PIT 证据
治理、双价格账本、不可变晋级门禁和本机安全部署的一体化。

可借鉴：

- 让因子/特征变换成为声明式、可缓存、可哈希的数据处理图；
- 将 dataset/handler/model/portfolio 配置统一成可复现实验模板；
- 参考 [Recorder](https://qlib.readthedocs.io/en/stable/component/recorder.html) 的
  artifact/metric/tag 接口，收敛项目里分散的实验、因子和模型证据；
- 用 Qlib 的滚动训练、在线管理和 concept-drift 研究作为模型生命周期测试基准。

不应直接照搬其示例数据或默认股票池；任何 Qlib 数据包仍需经过本项目的许可、PIT、
available-at、双账本和交易状态门禁。

### 8.2 QuantConnect LEAN

LEAN 的优势是统一的事件时间线与回测/实盘执行内核：数据订阅被同步成 time slice，
算法在 time frontier 上逐步接收数据；证券、组合、订单、费用、滑点、公司行为、
经纪商和实时结果都有清晰模型。官方还明确解释[当前指数成分带来的幸存者偏差](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide)
和[价格复权模式](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/settings)。
值得特别注意的是，完整历史 Adjusted 序列可能把未来才发生的拆分/分红反映到历史
价格锚；研究特定价格水平时应采用只消费当时已知公司行为的 ScaledRaw/等价语义。

可借鉴：

- 把研究和模拟都置于同一个确定性 event/time-slice 内核，减少 pandas 批处理前视；
- 引入 Security Identifier/MapFile 映射事件，正确处理改名、代码复用与退市；
- 将 fee/slippage/fill/margin/settlement/corporate-action 模型变成可替换且可测试组件；
- 使用 LEAN 风格的 brokerage message、订单状态机和 reconciliation 作为未来模拟盘
  完整性基准。

代价是 C#/.NET 引擎体量和学习成本很高，且其现成数据/公司行为主要面向美国市场。
本项目无需重写成 LEAN；应选择性借鉴事件内核和证券身份模型。

### 8.3 Backtrader

Backtrader 以 `Cerebro + Data Feed + Strategy + Broker + Analyzer/Observer` 组成轻量
事件驱动内核，数据源和经纪商扩展简单，参数优化也容易。它很适合做本项目的执行
语义对照实现：同一组小策略可在两套引擎跑，发现信号时点、成交顺序、费用和 warmup
差异。

但其数据 feed 对内容和来源几乎不作研究真实性判断；PIT、许可、历史成分、双时态、
晋级治理和多用户网页工作流仍由调用者负责。原项目维护节奏和 GPL 许可也要求在引入
代码前单独法务/兼容性评估；本次固定 HEAD 的最后提交日期为 2023-04-19，旧依赖和
长期未合并 PR 也增加维护风险。因此建议只借鉴 `Strategy` 生命周期、
`notify_order/notify_trade` 和 Analyzer/Observer 接口并做黑盒对照，不复制实现。
[Cerebro 官方文档](https://www.backtrader.com/docu/cerebro/)和
[live 支持说明](https://www.backtrader.com/docu/live/live/)可作为语义参考；GitHub
PR 活跃度只能视为社区信号，不能当 SLA。

### 8.4 Zipline-reloaded

Zipline 的 Data Portal、Bundle ingestion、TradingCalendar、AssetFinder、Pipeline 和
事件驱动模拟很适合股票横截面研究；bundle 是一个值得借鉴的数据接入边界。相比
Backtrader，它对资产生命周期、日历和 Pipeline 横截面计算更系统；相比本项目，它
缺少产品级前端、RBAC、任务/审批、PIT 证据包和本机灾备集成。

每次 bundle ingestion 用独立时间版本存放，并可通过 `--bundle-timestamp` 选择旧
ingestion，这对复现很有价值。可借鉴其原子临时导入、显式版本、交易日历、sid/资产
生命周期、DataPortal 时间前沿与 Pipeline mask-first 横截面计算；不要假设导入
bundle 就自动消除幸存者偏差或获得行级 `available_at/revision`，bundle 的来源和
构造仍决定真实性。参见[官方 bundle 文档](https://zipline.ml4trading.io/bundles.html)。

### 8.5 VeighNa（vn.py）

VeighNa 强项是国内交易生态：事件引擎、标准 MainEngine、Gateway、行情/委托/成交/
账户/持仓对象、CTA/组合/算法交易应用和多种数据库/柜台适配。它更接近“交易框架”
而非本项目的“研究证据治理平台”。

最值得借鉴的是 Gateway 与应用解耦、事件总线、订单/成交回报对象以及插件化数据库；
未来如果扩展 QMT/PTrade 模拟适配器，可以让本项目的批准 deployment 生成标准目标，
再经独立 gateway/risk/reconciliation 边界消费。不能把“支持实盘接口”当作安全门禁
完成，第三方 gateway 也不能绕过本项目的批准、限额、kill switch 和对账。VeighNa
4.0 的 `vnpy.alpha`/AlphaLab 已增加 parquet 数据、历史指数成分和 ML 研究能力，但
按日期成分仍不等于有 `available_at/ingested_at/revision`，pickle 模型持久化也不应
绕过本项目的反序列化门禁。参见[官方应用文档](https://www.vnpy.com/docs/cn/community/app/index.html)
和[数据库说明](https://www.vnpy.com/docs/cn/community/info/database.html)。

### 8.6 对比总表

| 维度 | 本项目 | Qlib | LEAN | Backtrader | Zipline-reloaded | VeighNa |
|---|---|---|---|---|---|---|
| 主定位 | 本地网页研究、证据治理、模拟盘 | AI/ML 研究流水线 | 多资产回测与实盘引擎 | 轻量策略回测/交易 | 股票研究与 Pipeline | 国内事件驱动交易框架 |
| 研究实验管理 | 前端、数据库、任务、比较、晋级 | Recorder/workflow 强 | 云平台强；自托管引擎偏运行 | 需自建 | 需外接 | 需外接 |
| PIT/防偏差 | 严格门禁和证据治理，但生产数据为空 | 有 PIT 数据库能力，输入仍决定质量 | time frontier、动态 universe/SID 强 | 调用者负责 | AssetFinder/Bundle/Pipeline 可支撑，源决定质量 | 调用者负责 |
| 公司行为/执行 | 双账本基础有，状态机未闭环 | 研究/回测为主 | 最成熟 | 有 broker 模型，真实性由 feed/broker 决定 | 股票回测较成熟 | 实盘事件与 gateway 强 |
| A 股生态 | 目标市场，数据待采购 | 示例/社区数据可用但需治理 | 非重点 | 自备 feed | 自备 bundle | 接口与交易生态较强 |
| 网页/RBAC/审计 | 项目优势 | 无产品级默认 UI | QuantConnect 云端有，自托管另论 | 无 | 无 | 桌面 GUI/应用体系，治理需自建 |
| 最适合借鉴 | — | 数据处理图、Recorder、滚动训练 | time-slice、SID、订单/公司行为模型 | 简洁策略/feed/analyzer API | Bundle、日历、Pipeline mask | Gateway、事件总线、国内交易对象 |

## 九、建议的下一阶段（按价值/依赖排序）

### P0：先关闭可信数据和模拟语义

1. 用本报告 PoC 清单在 JQData、RQData、Tushare 之间完成真实样例验收，记录许可答复；
2. 实现一个统一 `ProviderArtifactAdapter`：原始响应内容寻址、摄取时间、供应商版本、
   revision、请求参数和许可 receipt 全部不可变；
3. 导入 2016 至今的 PIT 证券/指数/行业/状态并激活四池，按 20 个事件案例与官方公告
   对账；
4. 装载同源 raw/adjusted/adjustment/corporate-action/amount，逐 member-session 门禁；
5. 实现公司行为持仓/现金状态机和逐日守恒测试，将 raw 正式用于模拟成交和估值；
6. 在可信数据上重跑非 ML 单策略的预注册开发、验证和锁定期，再重建组合；
7. 连续一个月运行模拟 canary，验证重启、重复、迟到数据、修订和拒单场景。

### P1：降低长期运维风险

8. generation token 或 PostgreSQL 一致快照，解决多库/文件并发读取一致性；
9. 任务与模型训练全部迁入可终止、可限 CPU/内存的隔离进程；
10. 外部告警、NTP/备份新鲜度/数据延迟/净值守恒 SLO 和确认升级；
11. 管理员 MFA、refresh rotation、设备会话、限流和密钥轮换；
12. 全链 correlation ID、每日签名审计根和远端对象保留锁；
13. 完成 GitHub 私有密文的远端下载—解密—数据库完整性—服务重建演练并测量 RTO；
14. 模型产物逐步迁移到非可执行或受限格式，禁止不可信 pickle/joblib/torch 反序列化；
15. 明确项目自身许可证、第三方依赖 notices 和可借鉴/不可复制边界，再考虑公开分发。

### P2：选择性吸收开源平台设计

16. 采用 Qlib 风格的声明式 Feature/Dataset graph 和统一 Recorder 接口；
17. 采用 LEAN 风格的 time-slice、证券永久身份、公司行为和订单状态模型；
18. 采用 Zipline Bundle 的版本化 ingestion 和 Pipeline mask 设计；
19. 采用 VeighNa Gateway 边界隔离未来券商适配器，但保持真实下单永久默认关闭；
20. 建一个很小的 cross-engine conformance suite，用 Backtrader/LEAN 对照信号与成交
   语义，不引入其生产依赖；
21. 数据闭环后再评估分钟级数据、实盘接口和容器化，避免提前扩大不可信输入面。

## 十、调研限制

- 数据产品价格、免费额度、接口范围和条款会变化；本文不替代供应商合同。无法公开
  访问的登录后价格一律标为“询价/以账号为准”，没有引用非官方报价作采购预算。
- 官方文档说明“历史日期查询”时，本文只认定其查询能力，不自动认定完整 PIT；
  必须用样例和合同验证 available-at、revision 和旧版本留存。
- 本次是代码结构级学习，不是对第三方项目的完整安全审计、性能 benchmark 或真实
  经纪商验收。社区评论有选择偏差，已明确与官方事实分开。
- 私有远端备份已取得上传成功回执，但尚未完成一次从该远端 asset 重新下载后的完整
  恢复演练，因此只认定“异地密文已上传”，不认定“异地恢复闭环”。
