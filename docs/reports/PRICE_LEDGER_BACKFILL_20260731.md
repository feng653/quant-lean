# 价格账本回填与运行链路验证（2026-07-31）

## 结论

代码链路已具备 PIT timeline 并集采集、canonical 单份价格、跨池用途绑定、断点恢复、
逐 member-session 缺口审计以及 raw/hfq 双角色运行基础。生产 PIT/价格/权威状态和
公司行为尚未装载，本报告不把链路测试写成实盘认证。

## 真实只读 source smoke

- 来源：`baostock.query_history_k_data_plus`，`adjustflag=3`
- 证券/区间：`600519`，2026-07-20 至 2026-07-24
- raw 行：5；本地确定性 hfq 行：5
- 状态行：5 个 traded、0 个 suspended
- raw source evidence SHA-256：
  `a309638ab121af2e8a23253ded6e9a950a841499a899ac8f5245e959527058ec`
- hfq recurrence：通过；该短区间 factor change 为 0
- 网络采样没有写生产数据库、Parquet 或 PIT master。

该 smoke 只证明适配器能取得 raw/preclose、状态字段并确定性构造 hfq。BaoStock
是公开源，账本证据保持 `declared`，不证明许可、交易所权威、公司行为完整或实盘适用。

## 负例与回归

- checkpoint 首块导入后中断，恢复时不重复请求已完成代码。
- 两个 PIT 股票池共享同一 canonical 价格行，只各写一条 runtime binding。
- PIT member-session 无价格且无显式停牌时，在价格 batch 写入前阻断。
- 稀疏价格不能仅凭日期边界通过；binding 写入和每次读取都重做 member-session 覆盖。
- raw/hfq 任一角色缺失、隐含因子不一致、跨池 canonical 冲突均 fail-closed。
- 空公司行为 multiplier 不能解释异常 factor jump。
- `licensed` / `exchange_authoritative` 自报没有治理 receipt 时被 store 与 API 拒绝。
- 引擎双 tape 测试确认成交与估值来自 raw，而不是 adjusted feature tape。
- legacy 全量 Parquet 审计为管理员权限、单并发、60 秒缓存；不修改旧文件。

## 仍未关闭的门禁

1. 生产 PIT master 尚未具备完整 timeline，actual backfill 会主动阻断。
2. 价格 artifact 的正式上传、双人审批和 receipt UI 尚未落地；权威导入无旁路。
3. BaoStock 交易状态不是权威停复牌/上市/退市主数据。
4. 公司行为尚未驱动持仓数量和现金变化，execution/real-tuning readiness 为 false。
5. 旧 pool Parquet 的 98,427 个跨池冲突只读保留，绝不自动选赢家。
6. canonical 账本当前只开放 OHLCV；BaoStock `amount` 虽被源响应证据哈希覆盖，
   尚未成为逐行不可变账本字段。依赖真实成交额的容量/流动性结论不得据此认证，
   后续须以兼容迁移和逐行完整性校验补齐，禁止用 `close * volume` 冒充成交额。
