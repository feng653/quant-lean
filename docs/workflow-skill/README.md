# lean-workflow skill（源）

可移植工作流 skill 的仓库源：**协调者角色 + issue 驱动 + 版本体系 + 三层机器门禁**，
用于把本仓库验证过的工作流带入任何新项目。

## 安装（一次）

```bash
# 首次：把本目录内容部署到全局 skill 目录
mkdir -p ~/.config/opencode/skills/lean-workflow
cp SKILL.md init.sh ~/.config/opencode/skills/lean-workflow/
cp -r templates ~/.config/opencode/skills/lean-workflow/
```

## 使用

```bash
# 在目标新项目的 git 仓库根目录
bash ~/.config/opencode/skills/lean-workflow/init.sh --yes
# 之后重开 opencode → 协调者身份自动激活（AGENTS.md）
```

详见 `SKILL.md`（操作手册）。

## 同步约定

本目录是**唯一源**：修改先改这里（PR 合入 test/integration），再复制到
`~/.config/opencode/skills/lean-workflow/`。反向（只改全局）视为临时实验，需回写本目录。

## 组成

| 文件 | 说明 |
|---|---|
| `SKILL.md` | 协调者操作手册（初始化/发 issue/版本规划/发布流程/简报） |
| `init.sh` | 初始化脚本（模板渲染 + 标签创建 + 收尾清单） |
| `templates/AGENTS.workflow.md` | 协调者身份 + 工作流宪法章节（追加进项目 AGENTS.md） |
| `templates/PROJECT_PHILOSOPHY.md` | 七条宪法（通用版） |
| `templates/VERSIONING.md` | 版本规则（T-xx 任务 / 发布时定版本号） |
| `templates/WORKFLOW_AUTOMATION.md` | 流水线 + 三层门禁设计说明（L2/L3 由项目 agent 实现） |
| `templates/TODO_INDEX.md` | 队列镜像骨架 |
| `templates/ci.yml` | 门禁骨架（来源检查 + 契约快照 + summary） |
| `templates/opencode.yml` | 自动 agent（actor 门控参数化） |
| `templates/e2e_release.yml` | L3 真实验收占位（fail-closed） |
| `templates/check_parallel.py` | 并行守卫（仓库名参数化） |

占位符：`{{OWNER}}` `{{REPO}}` `{{ACTOR}}`（init.sh 渲染替换）。
