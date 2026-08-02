# 项目文档状态索引

> 审计日期：2026-08-02
>
> 先按本页判断文档用途。历史报告用于追溯当时事实，不能覆盖当前代码、活动数据代、
> [路线图](ROADMAP.md) 或 [TODO 唯一索引](todo/TODO_INDEX.md)。

## 当前权威入口

| 主题 | 文档 | 说明 |
|---|---|---|
| 安装和用户全流程 | [README](../README.md) | 当前个人研究/模拟边界、页面截图和操作入口 |
| 实际任务 | [TODO_INDEX](todo/TODO_INDEX.md) | 唯一工作真源；按代码版本→部署→无代码实验操作循环 |
| 版本规划 | [ROADMAP](ROADMAP.md) | 只包含已进入 TODO 的版本目标；未来实盘仅作暂停边界 |
| API | [API](API.md) | 现行端点；实际 schema 最终以运行服务 OpenAPI 和代码为准 |
| 架构 | [ARCHITECTURE_V3](ARCHITECTURE_V3.md) | 当前主架构；V1/V2 不再作为实现依据 |
| 研究数据 | [RESEARCH_DATA_MANAGEMENT](RESEARCH_DATA_MANAGEMENT.md) | Tushare/ResearchDataStore、来源、冲突和 generation |
| 模拟盘 | [PAPER_TRADING_WORKFLOW](PAPER_TRADING_WORKFLOW.md) | 当前用户操作、promotion 边界及每日模拟目标 |
| 因子研究 | [FACTOR_GOVERNANCE](FACTOR_GOVERNANCE.md) | 当前能力/缺口、组合方法和可信导出 |
| 策略开发 | [STRATEGY_GUIDE](STRATEGY_GUIDE.md)、[STRATEGY_STANDARD_METHOD](STRATEGY_STANDARD_METHOD.md) | 代码契约、时间边界、研究和模拟方法 |
| Agent 工作流 | [TECH_LEAD](TECH_LEAD.md)、[WORKTREE_WORKFLOW](WORKTREE_WORKFLOW.md) | 版本 TODO 重读循环和 task-owned worktree |

## 当前专项与运维文档

- 数据采集/回填：[TUSHARE_PIT_BACKFILL](TUSHARE_PIT_BACKFILL.md)、
  [TUSHARE_CANDIDATE_PREFLIGHT](TUSHARE_CANDIDATE_PREFLIGHT.md)、
  [BAOSTOCK_SESSION_CROSSCHECK](BAOSTOCK_SESSION_CROSSCHECK.md)、
  [PIT_EVIDENCE_RECONCILIATION](PIT_EVIDENCE_RECONCILIATION.md)。
- 任务/训练：[DYNAMIC_JOB_SCHEDULER](DYNAMIC_JOB_SCHEDULER.md)、
  [CPU_TRAINING_ISOLATION](CPU_TRAINING_ISOLATION.md)、
  [MACOS_ML_TRAINING](MACOS_ML_TRAINING.md)、[REMOTE_TRAINING](REMOTE_TRAINING.md)、
  [MODEL_ARTIFACT_SECURITY](MODEL_ARTIFACT_SECURITY.md)。
- 本机安全/恢复：[LOCAL_SECURITY_AND_RECOVERY](LOCAL_SECURITY_AND_RECOVERY.md)、
  [AUTH_SESSION_OPERATIONS](AUTH_SESSION_OPERATIONS.md)、
  [EXTERNAL_SLO_ALERTS](EXTERNAL_SLO_ALERTS.md)。
- 时间/发布契约：[TIME_CONTRACT](TIME_CONTRACT.md)、
  [GENERATION_PUBLICATION](GENERATION_PUBLICATION.md)。后者只描述旧 Parquet DataCache
  子系统，不是 ResearchDataStore 的活动代契约。
- 每个策略的设计说明在 [`docs/strategies/`](strategies/)；真实结果以实验数据库和
  immutable evidence 为准。

## 未来实盘专用（当前研究/模拟不执行其人工审批）

下列文档保留严格设计和审计价值，但其中“正式研究/模拟必须硬阻断”的旧表述不能当作
当前个人研究流程；当前政策以 ROADMAP 为准：数据可信度问题告警，技术完整性阻断，live
恒拒绝。

- [PIT_ONLY_DATA_POLICY](PIT_ONLY_DATA_POLICY.md)
- [POINT_IN_TIME_MASTER](POINT_IN_TIME_MASTER.md)
- [DUAL_PRICE_LEDGER](DUAL_PRICE_LEDGER.md)
- [PIT_E2E_VALIDATION_PROTOCOL](PIT_E2E_VALIDATION_PROTOCOL.md)
- [PRODUCTION_PIT_RELEASE_RUNBOOK](PRODUCTION_PIT_RELEASE_RUNBOOK.md)
- [PIT_PROVIDER_LICENCE_EVIDENCE](PIT_PROVIDER_LICENCE_EVIDENCE.md)
- [NON_PIT_CLEANUP_RUNBOOK](NON_PIT_CLEANUP_RUNBOOK.md)

旧数据必须先可恢复归档，不能因为“只保留 PIT”的目标而在新研究 generation 未稳定时删除。

## 历史/已被取代（只读证据）

| 文档 | 被取代原因 | 当前替代 |
|---|---|---|
| [ARCHITECTURE](ARCHITECTURE.md)、[ARCHITECTURE_V2](ARCHITECTURE_V2.md) | 早期系统/设计阶段目录与数据库已变化 | ARCHITECTURE_V3 |
| [CODE_REVIEW](CODE_REVIEW.md)、[COMPLETION_REPORT](COMPLETION_REPORT.md) | 2026-07 阶段总结，包含旧仓库/旧缓存结论 | 当前代码、CI、ROADMAP |
| [PERFORMANCE_ANALYSIS](PERFORMANCE_ANALYSIS.md) | 旧数据和旧窗口绩效，不能视为 PIT 新实验 | 实验数据库及 OPS-03 新报告 |
| [PRODUCT_UPGRADE_PLAN](PRODUCT_UPGRADE_PLAN.md) | 旧大而全规划，已按小版本拆分 | ROADMAP + TODO_INDEX |
| [ROADMAP_EXECUTION_AUDIT_20260802](ROADMAP_EXECUTION_AUDIT_20260802.md) | 审计的是重构前路线图 | 当前 TODO/ROADMAP |
| [PIT_QA_E2E](PIT_QA_E2E.md) | 旧 PIT/DataCache fixture 浏览器链；目标保留，实现将迁移到 ResearchDataStore | v0.2.8 + OPS-10 |
| [PIT_RUNTIME_SECURITY_HARDENING_20260801](reports/PIT_RUNTIME_SECURITY_HARDENING_20260801.md) | 当时采用研究/模拟严格硬门禁，现已改为 warning-only | ROADMAP 的当前边界 |

`docs/reports/`、`docs/research/` 和 JSON 文件均是带日期的运行/研究证据。它们只说明对应
commit、数据和时点，不自动升级为当前状态。`PROJECT_STATUS_AND_DATA_SOURCE_REPORT_20260802`
仍是数据源/开源平台调研事实基线，但它的“下一阶段”已经转入版本化 ROADMAP。

## 未合并旧分支

- `auth-session-hardening-20260802`：等价能力已由 master `67128ed` 实现；不要重复合并。
- `pit-qa-e2e-sol-0801`：旧 fixture/DataCache 实现被 ResearchDataStore 方向取代；隔离浏览器
  验收目标已进入 v0.2.8/OPS-10。
- `rigor-security-batch-sol-0801`：可用的排序、相关性、任务恢复和技术完整性目标已拆入
  v0.2.7/v0.2.8；与“研究/模拟数据风险只告警”冲突的硬认证门禁不进入 active TODO。

三者均不得直接合并；若复用局部思路，必须从当前 master 新建聚焦 worktree、重新审查和测试。
