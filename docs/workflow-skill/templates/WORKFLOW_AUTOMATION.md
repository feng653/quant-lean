# 全自动工作流

> 本文件与 `docs/VERSIONING.md`（版本规则）、`docs/PROJECT_PHILOSOPHY.md`（七条宪法）、
> `docs/todo/TODO_INDEX.md`（任务队列）配合使用。

## 一、任务如何启动（issue → agent）

1. 用户提需求 → 协调者发布 issue：标题 `T-xx xxx（v0.y.z 候选）`，加 `epic`/`behavior-change` 标签、
   `domain:*`（碰触领域）+ 可选 `p:serial`（全局改动）、验收标准写入正文。
2. 用户授权开工：给 issue 加 `agent` 标签。
3. 触发 `opencode.yml`（GitHub Actions）→ 先跑 `.github/scripts/check_parallel.py` 并行守卫
   （domain 互斥 / p:serial 串行屏障）→ 通过才放行 agent 执行。
4. agent 独立分支实现 → 开 PR 到 `test/integration`（测试分支）→ CI 三层门禁。

## 二、测试分支与发布门禁

```
各版本 PR ──base: test/integration──→ test/integration（测试分支）
                                          │ 三层门禁：
                                          │   L1 契约快照（每次 PR，机器）
                                          │   L2 自动体检机（合成数据全流程，机器）
                                          │   L3 真实验收（真实数据全流程，发布前自动）
                                          ▼
master（稳定）← 发布 PR（base: master, head: test/integration）← CI 强制检查来源
```

- **机器强制**：ci.yml `release_source_check` job——任何 base=master 的 PR，head 不是
  `test/integration` 则 CI 失败（需加入 Required checks）。
- **指示层**：opencode.yml prompt 要求 agent 默认开 PR 到 test/integration。
- **测试数据清理**：E2E 产生的实验/模拟盘记录在验收后清理（避免污染生产数据）。

## 三、三层可用性门禁（机器强制，全部自动化）

### L1 契约快照（已有骨架，必用）

- 响应结构快照（如 FastAPI 项目：生成全部端点响应的 golden 文件）；
- 行为不变的重构 → 快照必须零 diff；行为变化 → 显式更新快照 + PR 变更清单 + 用户确认；
- 禁止静默更新快照掩盖行为变化。
- 初始化提供的 `ci.yml` 含 `contract_snapshot` job（目录存在时自动跑，fail-closed）。

### L2 自动体检机（设计说明，由项目 agent 实现）

- 目标：每次改动自动验证"链路能跑通"（合成数据全流程），坏改动合不进去。
- 做法：合成数据 fixture → 集成测试跑注册→创建→执行→结果核对→清理全流程 → CI 强制。
- 实现位置：`tests/integration/test_e2e_availability.py`（示例结构），接入 ci.yml 并加入 Required checks。
- 验证法：故意改坏一处链路，确认 CI 红。

### L3 真实验收（设计说明，由项目 agent 实现）

- 目标：发布前用真实数据全流程验收，报告由机器检查，无需人看。
- 做法：`.github/workflows/e2e_release.yml`（初始化提供占位骨架）在自托管 runner 上
  workflow_dispatch 触发：preflight → 启动后端 → 真实数据全流程 → 报告 json + 退出码(0=通过) →
  job 结论作为发布 PR 的 required check（master 保护 ruleset 增加该 check context）。
- 验收口径约定：模拟盘验证到"初始化成功"（部署创建 + 状态 active + 基础数据就绪）即可，
  不需要等一个交易日跑完。
- 安全：actor 门控（只允许仓库拥有者触发）。

## 四、角色分工

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| 用户 | 提需求、授权、发布时确认合并 | 不看验收报告、不写代码 |
| Agent | 实现、测试、修问题、开 PR | 不合并、不发布、不改 TODO_INDEX |
| 机器 | 守卫、CI、体检、真实验收、来源检查 | 不做判断 |
| 协调者 | 发布 issue、接受用户要求、分支/版本规划、反映状态、向上简报 | 不做用户要求之外的规划、不替代机器门禁 |

## 五、权限体系

GitHub 侧安全边界（自托管 runner 场景）：
- `opencode.yml` 事件门控 `github.actor == '<仓库拥有者>'`——本地 runner 有完整机器权限，
  任何第三方触发即远程代码执行，必须门控；
- master 保护 ruleset：必须 PR + Required checks（CI summary + L3 e2e check）+ 禁强推/禁删分支；
- 本地交互模式与 CI 模式权限分离（CI 用一次性全权限配置，本地保留确认网）。

## 六、分支生命周期

| 分支 | 类型 | 生命周期 | 用途 |
|---|---|---|---|
| master | 长命 | 永远 | 正式版 |
| test/integration | 长命 | 永远 | 测试版（汇聚 + 门禁） |
| opencode/T-xx-* | 短命 | 合并即删 | Agent 干活 |
| hotfix/v0.x.y-fix | 短命 | 修复完即删 | 紧急修复 |
