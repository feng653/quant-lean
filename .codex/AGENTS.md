# Codex 工作守则（本仓库专用）

本仓库的一切规则以根目录 `AGENTS.md` 与 `docs/` 下四份宪法文档为准。此文件只列 codex 的强制约束：

## 强制约束

1. **单会话单任务**：一个 codex 会话只认领一个 GitHub issue（带 `agent` 标签的）。
2. **只改与任务相关的文件**：任务外的大范围重构、清理、格式调整一律禁止。
3. **契约快照守则**：`backend/tests/snapshots/` 只允许在"行为变化"任务中更新，且必须在提交说明里列出变更清单。
4. **提交规范**：提交信息以 `[T-<issue号>] <动词>: <一句话>` 开头；推送到独立分支，不开到 master。
5. **验证必做**：任何修改后必须跑 `ruff check backend/ tests/integration/` 和受影响模块的 pytest。
6. **不提交秘密**：`.env`、`*.db`、`data/cache/` 等被 gitignore 的运行时文件绝不 add。

## 开工前必读

1. `AGENTS.md`
2. `docs/PROJECT_PHILOSOPHY.md`
3. `docs/WORKFLOW_AUTOMATION.md`
4. 对应版本的 `docs/todo/` 记录与 issue 正文（含验收标准）
