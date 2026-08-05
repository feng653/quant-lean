# lean-autopilot skill（历史源）

> **真源已迁移**：本工作流已开源为 **https://github.com/feng653/lean-autopilot**（MIT，tag v1.0.0）。
> 本目录保留为历史快照（quant-lean 工作流的衍生源），**不再维护**。
> 安装/使用/更新请以开源仓库为准。

## 安装（开源仓库）

```bash
git clone https://github.com/feng653/lean-autopilot ~/.config/opencode/skills/lean-autopilot
cd ~/.config/opencode/skills/lean-autopilot && git pull   # 更新
```

## 使用

```bash
# 在目标新项目的 git 仓库根目录
bash ~/.config/opencode/skills/lean-autopilot/init.sh --yes
# 自动检测并接入本机 agent（opencode/codex/claude/通用），符号链接单真源
# 之后重开 agent → 协调者身份自动激活（AGENTS.md）
```

详见开源仓库 README 与 SKILL.md。

## 历史说明

此目录内容为 v1.0.0 前身（skill 名 lean-workflow）。改动请直接提交到
[lean-autopilot](https://github.com/feng653/lean-autopilot) 仓库，本目录仅作追溯。
