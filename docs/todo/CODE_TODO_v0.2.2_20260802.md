# v0.2.2 代码 TODO：可消费的研究数据底座

> 单一方向：让 Tushare 候选数据形成可恢复、可查询、可告警使用的研究 generation。

| 顺序 | ID | 工作项目 | 状态 | 继承旧项 | 完成验收 |
|---:|---|---|---|---|---|
| 1 | V022-01 | 合并并验收全市场行情、状态、基准物化 | 完成 | WIP-11/12、Q-R01～03 | exact-run、流式不可变 generation、四池与 `all_a`、进度、来源和冲突接口通过测试；真实覆盖不能被夸大 |
| 2 | V022-02 | 将实验、扫描和模拟读取切到活动 ResearchDataStore | 完成 | WIP-10/13、Q-R04/05 | 有可计算数据即可告警运行；无数据、损坏、无窗口、参数非法或账本不守恒才技术阻断；live 恒拒绝；因子运行时归 v0.2.6 |
| 3 | V022-03 | 完成数据源切换与字段级冲突/未比较展示 | 完成 | WIP-08、Q-R03 | 每个来源报告覆盖、失败、冲突及未比较原因；不可用校验源不伪装成无冲突；不静默混源 |
| 4 | V022-04 | 修复股票池不可用、刷新和 0/0 假成功 | 完成 | Q-R04 | 四个 CSI 池与 `all_a` 可从活动研究代读取；刷新显示真实计划/完成/失败；结构化告警不误阻断研究 |
| 5 | V022-05 | 版本回归、审查、合并、部署后技术验收 | 完成 | WIP-14 已完成 | lint、后端/前端测试/build、健康与边界审计通过；服务版本匹配提交；真实数据操作留给实验操作 TODO |

已完成证据：WIP-14 的参数/模型身份校验和环境无关部署门禁回归已通过，未放宽 live。

## 本轮代码验收与发布证据（2026-08-02）

- 主树已合并提交 `2d44fc4`，并已推送 GitHub；部署健康端点报告
  `version=0.2.2`、`commit=2d44fc4`、`dirty=false`。loopback/Caddy 网络边界审计全部通过。
- 主树回归通过：Ruff；后端 `1166 passed, 1 skipped`；集成 `13 passed`；前端 lint、
  `183` 项测试和 production build。独立复审覆盖 `45` 项，复审意见已闭环。
- durable research scheduler 已创建并运行 job `519`，状态页显示真实非 `0/0` 进度。它会从
  可恢复 checkpoint 继续物化，不能因为任务已启动而宣称历史全量数据已经具备。
- 部署时 active generation 仍是 membership-only。四池/全市场行情、状态和基准的真实历史
  回填、核对与切换仍严格属于 `OPS-01`；本版本完成的是可恢复物化和可消费运行时，而不是
  对真实数据覆盖率的声明。

- Sweep/calendar review follow-up（完成）：selection 子实验只绑定 selection
  window；晋级 locked-test 时从来源成员固定同一 generation，并为 locked window 重新派生
  actual window 与 timeline，worker 统一复核 generation 对应的数据摘要、窗口及时间线，禁止
  跨窗口绑定。指定模拟组合先验证 owner/status，再读取规范化 allocations；仅在旧组合没有
  规范化记录时校验并读取 JSON allocations，不存在、禁用、越权、空绑定和失效部署均不再
  回退 CSI500。定向 `34 passed`、全后端 `1166 passed, 1 skipped`、集成 `13 passed`，
  Ruff、compileall、diff-check 通过；未操作真实数据或服务。
- Review follow-up（完成）：ResearchDataStore 活动/历史 generation 均校验
  sealed sidecar、文件摘要、内嵌身份和 SQLite 完整性；基准结构/文件损坏在 readiness、提交和
  worker 一致技术阻断，缺少基准行仍保持 warning-only。混合 strict/research 模拟绑定全部
  校验；组合/日期幂等键统一并增加可过期 claim；每日研究刷新支持冷却和单日有界重试；
  `all_a` 依据同代证券上市/退市状态构造可重放时间线，运行时只加载必要字段且提交/worker
  复核数据摘要、实际窗口和时间线身份。系统自动刷新任务可在数据页查看，README 回归保持
  研究层与未来实盘双价格层分离且研究更新不授予 live。定向 38 项、全后端
  `1164 passed, 1 skipped`、集成 `13 passed`，Ruff、compileall、diff-check 通过；未操作
  真实数据或服务。
- V022-01：活动研究代已提供四池/`all_a`、行情、基准、来源、冲突和不可变 generation
  查询；指定 generation 可精确读取，活动指针损坏不会令无关旧部署静默换代。流式导入和
  内存预算仍保留。`test_research_data_store.py` 覆盖物化、单位、基准、不可变性和旧代读取。
- V022-02：实验 readiness、创建门禁、worker 和 paper simulation 已切换到
  `ResearchDataStore`；提交与运行绑定同一 generation，基准缺失、PIT/行业/双价格证据缺口
  只告警，缺代、损坏、零覆盖、不可计算窗口仍技术阻断。旧未绑定部署保留 DataCache
  兼容路径且不会静默升级；模拟日历/回放使用组合实际股票池的共同交易日。
- V022-03：研究来源状态和字段级冲突/未比较接口沿用已集成实现；运行结果、模拟 readiness
  和前端历史回放展示研究告警、来源代和股票池范围，不把不可用的第二来源显示为无冲突。
- V022-04：研究池从活动代读取；阶段性
  `historical_member_session_coverage_invalid` 不再误停自动续跑，真实 provider/contract
  失败仍阻断，并用“本批有进度”条件防止忙循环。新增独立于模拟组合、按来源+本地日期
  幂等去重的温和 Tushare 日常刷新调度；完成历史 pending 后继续探测最新完整月。
- 回归：定向后端 99 项通过；新增运行时/刷新调度/股票池交集测试通过；Ruff、compileall、
  前端生产 build 和 PortfolioManager 模拟单测通过。随后主树完整回归、独立复审、部署、
  版本/健康和边界验证均已完成；真实活动代覆盖验证仍属于 `OPS-01`。

## 当前交接事实（迁自旧执行看板）

| 旧 ID | 当前事实 | 进入本版的下一步 |
|---|---|---|
| WIP-06 | Tushare 四指数/全市场可恢复采集仍在运行；实际计划、完成、失败必须读取最新 checkpoint，不能沿用旧手工数字 | 保留原始 receipt、归一化 artifact、checkpoint 和失败；真实回填验收归 OPS-01 |
| WIP-08 | BaoStock 真实登录失败，尚无独立 observation；不阻塞 Tushare 单源研究 | V022-03 明确显示“校验源不可用/未比较”，不得显示“无冲突” |
| WIP-10/13 | 条件信任档案、paper 风险快照、promotion 可选和 live 恒拒绝已合并；运行仍需从旧 DataCache 切到 ResearchDataStore | V022-02 完成实验/扫描/模拟读取和真实研究代测试 |
| WIP-11/12 | 全市场研究物化 worktree 已实现 exact-run、流式不可变 generation、四池/`all_a`、行情/状态/基准和数据页；当时定向测试通过，仍待主树审查集成 | V022-01 主树验证；不得把临时导入覆盖或行数写成当前 active generation |
| WIP-14 | 参数/模型身份不匹配已在读取 manifest 前返回稳定 422；环境无关部署门禁回归完成 | 作为回归保护保留，不重复开发 |

任何工作树、负责人、checkpoint 或真实覆盖发生变化时，直接更新本表并保留用户备注。

<!-- 人工说明：保留用户在此追加的项目、顺序和备注。 -->
