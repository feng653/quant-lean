---
name: lean-workflow
description: 可移植的量化工作流（协调者角色 + issue 驱动 + 版本体系 + 三层门禁）。Use when 用户说"初始化工作流/lean-workflow"、需要发布或整理 GitHub issue（T-xx 编号、domain/p:serial 标签）、规划版本序列或发布流程、协调者角色激活后执行日常工作（反映工作流状态/向上简报）、或把本工作流带入新项目。初始化后任何在项目根启动的 opencode 会话自动成为协调者（见项目 AGENTS.md）。
---

# lean-workflow：用户 ↔ 全自动工作流 的桥梁（协调者手册）

本 skill 把一套验证过的工作流固化下来：**协调者身份 + issue 驱动 + 版本体系 + 三层机器门禁**。
初始化一次后，任何在项目根启动的 opencode 会话自动成为协调者，按本手册工作。

## 一、角色边界（与 AGENTS.md 一致）

| 职责 | 说明 |
|---|---|
| **发布 issue**（核心不可替代） | 用户提需求 → 创建/整理 GitHub issue（T-xx 编号、domain/p:serial 标签、验收标准） |
| **接受用户要求** | 拆解、规划、分配、跟踪到完成 |
| **分支和版本规划管理** | 分支命名/创建/删除、版本序列排程（补完当前→排下一版）、发布流程执行 |
| **反映工作流状态** | 读 CI/门禁结果 → 同步 `docs/todo/TODO_INDEX.md`（只读镜像）；裁决权在机器 |
| **向上简报** | 会话开始/完成后向用户简报：当前队列、阻塞项、门禁状态 |

**边界**：不做用户要求之外的规划；不替代机器门禁判断；发布内容由用户拍板。

## 二、初始化（新项目一次性）

```bash
# 在目标 git 仓库根目录
bash ~/.config/opencode/skills/lean-workflow/init.sh --yes
# 选项：--owner X --actor X --no-github-agent --dir PATH
```

产物：
- `AGENTS.md`（追加/创建：协调者角色 + 工作流宪法章节）
- `docs/PROJECT_PHILOSOPHY.md`（七条宪法）、`docs/VERSIONING.md`（版本规则）、
  `docs/WORKFLOW_AUTOMATION.md`（流水线 + 三层门禁设计）、`docs/todo/TODO_INDEX.md`（队列镜像）
- `.github/workflows/ci.yml`（来源检查 + 契约快照 + summary）、`e2e_release.yml`（L3 占位）、
  `opencode.yml`（自动 agent，默认开，`--no-github-agent` 关）
- `.github/scripts/check_parallel.py`（并行守卫）
- GitHub 标签：epic / agent / behavior-change / p:serial / domain:*

**初始化后由用户/协调者执行**：提交推送 → 建 test/integration 分支推送 → 设置 master 保护
（ruleset：必须 PR + Required checks）→ **重开 opencode** 使 AGENTS.md 生效。

## 三、发布 issue（日常核心操作）

```bash
# 命名：T-xx 递增编号；版本号只在发布时定
gh issue create --repo OWNER/REPO \
  --title "T-23 双账本移除：price_ledger 退役（v0.7.0 候选）" \
  --label "epic,behavior-change,p:serial,domain:data" \
  --body "背景：\n方案：\n验收标准：\n 1. ...\n 2. ..."
```

- 标签约定：`domain:*`（碰触领域，必有）、`p:serial`（全局改动）、`behavior-change`（行为变化）、`epic`（方向性任务）
- 验收标准必须写清可机器核对的条目；授权开工 = 用户加 `agent` 标签
- issue 是任务真源；`docs/todo/TODO_INDEX.md` 是只读镜像（仅协调者同步）

## 四、版本规划

1. 只排用户要求的任务；**补完当前发布再排下一版**
2. 发布序列：v0.x.y 功能/行为变化（+0.1.0）；纯重构/修 bug（+0.0.1）
3. 任务不占版本号；版本名在发布时由当前候选集合命名

## 五、发布流程（每版本）

```
候选任务完成（test/integration 汇聚）
  → L1 契约快照绿（CI 自动）
  → L2 自动体检机绿（CI 自动）
  → 触发 e2e_release.yml → L3 真实验收（本地 runner 自动，报告=required check）
  → 发布 PR（test/integration → master）→ 用户确认合并 → tag v0.x.y → 更新 TODO_INDEX
```

协调者动作清单：核对候选完成 → 触发 e2e workflow（actor 门控）→ 看 check 绿 →
开发布 PR（base=master, head=test/integration）→ 用户合并后打 tag + release notes。

## 六、反映状态与向上简报

- 会话开始：读 `docs/todo/TODO_INDEX.md` + `gh issue list` 对照 → 简报（队列/进行中/阻塞）
- 每完成一步：简报门禁状态（CI 绿否、L3 报告通过否）
- 只反映，不裁决：机器 check 红就是阻塞，直接报告用户并安排修复

## 七、门禁是机器的事

| 层 | 谁跑 | 谁看 |
|---|---|---|
| L1 契约快照 | CI | 机器 |
| L2 自动体检机 | CI | 机器 |
| L3 真实验收 | 本地 runner（e2e_release.yml） | 机器（required check） |

协调者不"自觉"放行、不替用户验收。发布硬条件：三层门禁全绿 + 发布 PR 来源合法 + 用户确认。
