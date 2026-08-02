# PIT-only 前端实验链路验收

> **旧 DataCache/PIT fixture 验收设计。** 隔离浏览器验收目标仍有效，但实现将改为活动
> ResearchDataStore generation；本文件不能证明当前真实数据或前端链路已经验收。见
> [v0.2.8](todo/CODE_TODO_v0.2.8_20260802.md) 与
> [OPS-10](todo/EXPERIMENT_OPERATIONS_TODO_20260802.md)。

`scripts/run_pit_qa_e2e.sh` 在一次性、非生产目录中启动真实 FastAPI、Vite 和
Chromium，通过前端逐项创建实验，再从 SQLite 与研究清单交叉核验结果。它用于验证
“浏览器 → API → 队列 → 策略 → 回测 → 数据库 → 前端详情/证据导出”链路，不用于
构造或补充生产研究数据。

## 隔离与真实性边界

- fixture 是确定性 synthetic QA 数据，始终标记
  `production_eligible=false`，不得作为收益研究结论。
- 仅当 `ENVIRONMENT=test`、显式配置 QA 根目录、存在规范隔离标记，并且用户库、
  实验库、交易库、缓存、暂存、模型与快照路径全部位于该根目录时，QA 证明才可用。
- QA 根目录不能位于仓库 `data/` 下；开发和生产环境即使看见同一 attestation 也会
  忽略它，生产 PIT=0 时创建实验仍返回结构化 HTTP 409 且不写实验或任务。
- QA 数据仍经过真实的四池成分批次激活、300 成分逐日时间线解析、不可变双价格账本、
  精确 runtime binding、schema-v4 缓存与 benchmark artifact 哈希校验。没有
  monkeypatch，也没有运行时联网。

## 运行

本机需要 Python 依赖、前端依赖和 Playwright Chromium。若 Playwright 不在项目依赖
中，显式提供其模块与浏览器位置：

```bash
export PLAYWRIGHT_MODULE=/path/to/node_modules/playwright/index.js
export PLAYWRIGHT_EXECUTABLE_PATH=/path/to/chromium
./scripts/run_pit_qa_e2e.sh
```

默认从策略注册表选择三条代表性单策略：至少一条因子策略和两条技术策略。筛选规则由
注册表元数据决定：`requires_training=false`、分类不是 `ml/portfolio/composite`、
且 `sub_strategies` 为空，不维护手工策略全集。全量串行验收：

```bash
PIT_QA_ALL=1 ./scripts/run_pit_qa_e2e.sh
```

可指定一个事先创建的空目录与报告位置：

```bash
PIT_QA_ROOT=/absolute/empty/qa-root \
PIT_QA_REPORT=/absolute/report.json \
./scripts/run_pit_qa_e2e.sh
```

脚本停止两个临时服务，但保留 QA 根目录供审计。报告逐条包含策略 ID、分类、最终状态、
manifest hash、成分批次、时间线 hash、价格 binding、权益点数、交易数与核心指标。

## 验收失败处理

任何下列情况都会返回非零状态：浏览器未通过可见表单提交、任务失败或超时、少于三条
代表性实验、缺少因子单策略、数据库状态不一致、manifest hash 不一致、PIT 批次未激活、
价格 binding 不匹配、QA 标记缺失/被篡改、或权益明细为空。日志位于 QA 根目录的
`backend.log` 和 `frontend.log`。
