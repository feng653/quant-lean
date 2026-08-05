# 全自动工作流（GitHub 原生流水线）

> 参考开源方案：opencode 官方 GitHub 集成（opencode.ai/docs/github），issue 中提及 `/opencode` 或 `/oc` 即触发 agent 在 GitHub Actions 里工作。配合 GitHub 原生 Branch Protection + Merge Queue。

## 一、全自动流水线（用户视角只有 4 步）

```
① 用户：GitHub 开一个 issue（一句话需求）
   （或本地会话说"建 issue" → agent 代开）
      │
② GitHub Actions（opencode/github@latest）自动触发：
   agent 读需求 → 分析代码 → 开分支 → 实现 → 跑测试 → 开 PR
      │
③ CI 门禁（每个 PR 自动跑）：
   ruff → 单测 → 集成 → ★契约快照 diff★ → 前端 build
      │
④ 合并（机器+用户两级把关）：
   ├─ 行为不变（快照零 diff）→ CI 绿 → Merge Queue 自动合入 master
   └─ 行为变化（快照有 diff）→ PR 附变更清单 → 用户 Approve → 合入
      │
⑤ 验收：手工跑一遍 → 打 tag v0.x.y → tag 触发发布验证 workflow
```

## 二、并行安全的三层机制

| 层 | 机制 | 保证 |
|---|---|---|
| 1 隔离 | 每个 issue → 独立 Action job → 独立 checkout 工作区 + 独立分支 | agent 编辑时物理隔离，互不可见互不干扰 |
| 2 合并 | GitHub Merge Queue：并发 PR 自动排队、自动 rebase 最新 master；文件冲突时标记需更新，agent 修复重推 | 串行化合并，冲突在合入前解决 |
| 3 语义 | 契约快照 diff：两人都改同一 API 但没撞文件 → 快照必然报红 → 逐个 PR 逐个确认 | 行为等价性机器裁决 |

## 三、运行环境

- **self-hosted runner** 装在本机 Mac（31G 行情数据、torch、libomp 都在本机；云端 runner 无数据）。
- 本地大任务（需要真实数据的实验/回测验证）：本地 opencode 会话执行，不走 GitHub。
- 触发方式：
  - `issue` 打开时若带 `agent` 标签 → 自动开工（标签门控，保证版本顺序执行）
  - issue/PR 评论写 `/opencode 帮我…` → 即时触发
  - `workflow_dispatch` → 手动触发

## 四、标签约定

| 标签 | 含义 |
|---|---|
| `agent` | 自动化 agent 可以开工（用户/协调者加此标签） |
| `p:serial` | **全串行任务**：与任何进行中任务冲突（全局改动/行为变化版本必打） |
| `domain:core` / `domain:data` / `domain:engine` / `domain:api` / `domain:jobs` / `domain:strategies` / `domain:frontend` / `domain:docs` / `domain:infra` / `domain:tests` | 任务碰触的领域（可多个），并行冲突判定的依据 |
| `behavior-change` | 行为变化需求：PR 需用户 Approve 才合并 |
| `blocked` | 阻塞中，agent 不得开工 |
| `epic` | 版本级大任务（内含子任务清单） |

## 五、并行判定（机器自动，agent 开工前执行）

`opencode.yml` 在启动 agent 前运行 `.github/scripts/check_parallel.py`：

```
规则1 串行屏障：新任务带 p:serial 而任一任务在进行中 → 拒绝
        或 任一进行中任务带 p:serial → 拒绝一切新任务
规则2 领域冲突：新任务与任一进行中任务的 domain:* 有交集 → 拒绝
        无交集 → 放行并行（工作区隔离 + 串行合并保证安全）
被拒时：自动在 issue 留言原因 + 移除 agent 标签（回到排队状态），
        完成后重新加回 agent 标签即可开工
```

**例子**：v0.3.0（domain:tests,infra）在进行中时，一个只碰 `domain:strategies` 的新功能 issue 可以并行开工；而 v0.4.0（p:serial）必须等 v0.3.0 结束。

## 六、版本顺序执行（一次托管全部任务）

所有版本 epic issue 一次性创建（任务板挂满），但**只有带 `agent` 标签的 issue 才会被 agent 处理**：

1. 给 `[v0.3.0]` 加 `agent` 标签 → 守卫检查 → 开工 → 合入。
2. 验收后给 `[v0.4.0]` 加 `agent` 标签 → 开工……依此类推。
3. 版本内/版本间不相干任务：domain 标签不重叠即可并行（守卫自动放行）。

## 六、工作流文件

| 文件 | 触发 | 作用 |
|---|---|---|
| `.github/workflows/ci.yml` | push / PR / merge_group | 门禁：lint + 单测 + 集成 + 快照 diff + 快照基线比对 + **L2 体检机** + 前端 build |
| `.github/workflows/opencode.yml` | issue 加标签 / 评论 /oc | issue → agent → PR 自动流水线 |
| `.github/workflows/release.yml` | tag push | 发布验证 + 版本证据归档 |
| `.github/workflows/e2e_release.yml` | workflow_dispatch / master 目标 PR | **L3 真实数据自动验收**（本地 runner，机器检查） |

## 七、已生效的分支保护（rulesets）

### master-protection（master）

- required_status_checks：CI 的 "Required checks"（含 `Contract snapshot diff` + `Snapshot baseline diff`）+ **`e2e-release-verification`（L3 真实验收）**必须通过才能合入
- pull_request：所有改动必须走 PR，禁止直接 push master；**`require_code_owner_review` 开启**——`backend/tests/snapshots/` 归 @feng653 所有（`.github/CODEOWNERS`），动快照的 PR 必须用户 Approve
- non_fast_forward + deletion：禁止强推、禁止删分支

### test-integration-protection（test/integration）

- pull_request：所有改动必须走 PR + **同样的 codeowner 审批**（动快照必须用户 Approve，堵住"静默改快照混进测试分支 → 随发布 PR 进 master"的路径）
- non_fast_forward + deletion：禁止强推、禁止删分支
- 不强制 status checks（避免把 L3 真实验收强加到每个版本 PR）

**Merge Queue（可选增强）**：API 开启在免费版报错，需在 GitHub UI 手动开启：
Settings → Rules → master-protection → 编辑 → 勾选 "Require merge queue"。
开启后并发 PR 自动排队串行合并；未开启时靠"要求最新代码（strict）+ 手动按序合并"达到同样效果。

## 八、测试分支与三层发布门禁（全机器强制）

```
各版本 PR ──base: test/integration──→ test/integration（测试分支）
                                          │
                                          ▼
                    ┌──────── 三层可用性门禁 ────────┐
                    │ L1 契约快照：181 端点结构零漂移 │ CI 自动
                    │ L2 自动体检机：合成数据全链路   │ CI 自动
                    │   （注册→实验→回测→模拟盘初始化）│
                    │ L3 真实数据自动验收：31G 真实   │ 本地 runner
                    │   数据全流程，报告机器检查      │ e2e_release.yml
                    └──────────────────────────────┘
                                          │ 全绿
                                          ▼
master（稳定）← 发布 PR（base: master, head: test/integration）← CI 强制检查来源
```

### L1 契约快照（结构门禁）

- 每次 PR 自动跑 `backend/tests/test_contract_lock.py`：181 端点响应结构零漂移（`Contract snapshot diff` job，**已纳入 Required checks**，红了不可合并）。
- 行为不变的重构：快照零 diff；行为变化：显式更新快照 + PR 变更清单 + 用户确认。
- **防静默改快照（机器强制，双保险）**：
  - `Snapshot baseline diff` job（Required checks）：比对 PR 与 base 分支的 `backend/tests/snapshots/`——快照有改动但 PR 未带 `behavior-change` 标签 → job 红。
  - CODEOWNERS + ruleset 审批：`backend/tests/snapshots/` 归 @feng653 所有；master 与 test/integration 的 ruleset 均开启 `require_code_owner_review` → 任何动快照的 PR（无论合哪条线）必须用户 Approve，agent 无法自行绕过。

### L2 自动体检机（链路门禁）

- `tests/integration/test_e2e_availability.py`：合成数据（300 股 × 300 日 + PIT 会员 + benchmark）
  全链路——注册 → 3 实验 → **真实 worker 完成回测** → 指标 → 部署模拟盘 → 初始化确认 → 清理。
- ci.yml 独立 job `L2 health check (synthetic E2E)`，纳入 Required checks。

### L3 真实数据自动验收（真实门禁，v2 全自动）

- `scripts/e2e_release.sh`：preflight（端口/磁盘≥5G）→ 启动后端（真实 data/）→ 3+ 策略实验
  → job 完成 → 指标核对 → 模拟盘部署初始化（status active）→ 前端可达 → 清理实验
  → 输出 `e2e-release-report.json` + 退出码（0=通过）。
- `.github/workflows/e2e_release.yml`：`workflow_dispatch` 或 master 目标 PR 触发，
  本地 runner（self-hosted, macos, arm64）执行；`actor==feng653` 门控（31G 数据 = 远程代码执行风险）；
  报告存 artifact；**job 名 `e2e-release-verification`，报告由机器检查**。
- **发布门禁**：ruleset（master-protection）已把 `e2e-release-verification` 加入
  required_status_checks → 发布 PR 无 E2E check 或 check 红 = 不可合并。

### 发布 DoD（缺一不可）

1. L1 + L2 全绿（CI）
2. L3 E2E check 绿（机器检查）
3. 来源合法：master 只收来自 test/integration 的发布 PR（ci.yml release_source_check）
4. 用户确认合并（唯一人工点）→ 打 tag v0.x.y

- **E2E 执行者**：本地 runner 自动执行（workflow_dispatch / 发布 PR 自动触发），
  用户/协调者不再手工跑验收。
- **测试数据清理**：E2E 产生的实验在脚本内自动清理（部署保留供审查）。

> 权限体系（各级权限/矩阵/安全边界）：见 `docs/WORKFLOW_PERMISSIONS.md`
