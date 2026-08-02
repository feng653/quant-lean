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
| `behavior-change` | 行为变化需求：PR 需用户 Approve 才合并 |
| `blocked` | 阻塞中，agent 不得开工 |
| `epic` | 版本级大任务（内含子任务清单） |

## 五、版本顺序执行（一次托管全部任务）

所有版本 epic issue 一次性创建（任务板挂满），但**只有带 `agent` 标签的 issue 才会被 agent 处理**：

1. 给 `[v0.3.0]` 加 `agent` 标签 → agent 开工 → 合入。
2. 验收后给 `[v0.4.0]` 加标签 → 开工……依此类推。
3. 各版本内的并行子任务：子 issue 分别加 `agent` 标签即可并行（工作区隔离 + Merge Queue 保证安全）。

## 六、工作流文件

| 文件 | 触发 | 作用 |
|---|---|---|
| `.github/workflows/ci.yml` | push / PR / merge_group | 门禁：lint + 单测 + 集成 + 快照 diff + 前端 build |
| `.github/workflows/opencode.yml` | issue 加标签 / 评论 /oc | issue → agent → PR 自动流水线 |
| `.github/workflows/release.yml` | tag push | 发布验证 + 版本证据归档 |

## 七、已生效的 master 保护（ruleset: master-protection）

- required_status_checks：CI 的 "Required checks" 必须通过才能合入
- pull_request：所有改动必须走 PR，禁止直接 push master
- non_fast_forward + deletion：禁止强推、禁止删分支

**Merge Queue（可选增强）**：API 开启在免费版报错，需在 GitHub UI 手动开启：
Settings → Rules → master-protection → 编辑 → 勾选 "Require merge queue"。
开启后并发 PR 自动排队串行合并；未开启时靠"要求最新代码（strict）+ 手动按序合并"达到同样效果。
