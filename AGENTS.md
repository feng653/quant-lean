# 量化验证平台 (Quant Lean)

FastAPI + React monorepo for personal quantitative strategy backtesting and paper trading.
本仓库是重构后的"精简版"（从冻结仓库 `feng653/quant-platform` 迁移）。**宪法文档必须先读**：
`docs/PROJECT_PHILOSOPHY.md`（七条宪法）、`docs/ARCHITECTURE_LEAN.md`（北极星架构）、
`docs/VERSIONING.md`（版本号与线路）、`docs/WORKFLOW_AUTOMATION.md`（全自动工作流）。

## Quick commands

**macOS 前置**: LightGBM / XGBoost 需要 OpenMP 运行时，先执行 `brew install libomp`。

```bash
# Backend (must run from project root)
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

# Frontend
cd frontend && npm install && npm run dev
```

## Verify before committing

```bash
# Python lint
ruff check backend/ tests/integration/

# TypeScript typecheck
cd frontend && npx tsc -b --noEmit

# Python unit tests
pytest backend/tests/ -v --tb=short --timeout=120

# Integration tests (isolated temporary databases)
pytest tests/integration/ -v --tb=short --timeout=180

# Frontend tests
cd frontend && npm run lint && npm run test && npm run build
```

**CI gate order**: lint → build → unit tests → integration tests → contract snapshot diff.

## 工作流总宪法（opencode 与 codex 共同遵守）

### 协调者角色（自动激活，最高优先级）

**任何在本仓库根目录（quant-lean）启动的 opencode 会话，自动成为"协调者"**，无需特殊配置或指令。你是本项目工作流的唯一管理者，职责固定如下：

| 职责 | 说明 |
|---|---|
| **发布 issue** | 用户提需求 → 你负责创建/整理 GitHub issue（T-xx 编号、domain/p:serial 标签、验收标准） |
| **管理和监视工作流** | 维护 `docs/todo/TODO_INDEX.md`（issue 真源的只读镜像）、版本队列、并行状态、门禁状态 |
| **接受用户要求** | 用户的每句话都是需求输入；拆解、规划、分配、跟踪到完成 |
| **分支和版本规划管理** | 分支命名/创建/删除、发布序列规划（v0.6→0.9）、发布流程（体检→真实验收→tag） |

**边界（不可逾越）**：
- **不做用户要求之外的规划**——用户没提的方向，不主动加任务、不开 issue、不扩展范围、不自行决定新版本内容。
- 规划（分支/版本）以用户要求为唯一依据；当前过渡期以 `docs/WORKFLOW_UPDATE_PLAN.md` 为临时执行清单（完成即弃）。

开工顺序：读本文件 → 读 `docs/todo/TODO_INDEX.md`（当前队列）→ 执行用户要求。
> 注意：`docs/WORKFLOW_UPDATE_PLAN.md` 只是**临时下一步计划**（当前过渡期一次性使用，阶段完成后删除），不是宪法；它只指导"现在下一步做什么"，长期规则以本文件与四份宪法为准。

### 任务模型

- **一个 issue = 一个需求 = 一个版本方向**。用户开 issue，标题带版本号（如 `[v0.4.0] main.py 抽层`）。
- **只有带 `agent` 标签的 issue 才允许被 agent 处理**（保证版本按序执行、并行安全）。
- **并行判定标签**：提 issue 时声明 `domain:*`（碰触的领域）与 `p:serial`（全局改动）。
  `opencode.yml` 开工前自动执行 `.github/scripts/check_parallel.py`：与进行中任务 domain 重叠或被
  p:serial 屏障挡住 → 自动留言原因并移除 agent 标签（排队）；无冲突 → 放行并行。
- 版本顺序：v0.3.0 契约锁定 → v0.4.0 抽层 → v0.5.0 去重 → v0.6.0 删除 → v0.7.0 数据收敛 → v0.8.0 行为简化（见 `docs/VERSIONING.md`）。

### 全自动流水线（GitHub 原生）

- 在 GitHub Actions 里工作时（`opencode/github@latest`）：读 issue → 分析 → 开分支 → 实现 → 跑测试 → 开 PR。
- 本地会话工作时：先读 `docs/todo/TODO_INDEX.md` 确认版本状态，只做当前版本方向内的改动。
- **契约快照守则**：
  - 行为不变的重构：快照必须零 diff（`backend/tests/snapshots/` 不得改动）。
  - 行为变化（改端点/响应/数据结构）：必须显式更新快照，并在 PR 描述列出变更清单，等用户 Approve。
  - 禁止静默更新快照来掩盖行为变化。

### 并行与合并

- 每个 agent 在独立分支工作；**版本 PR 默认合入 `test/integration`（测试分支）**。
- **发布门禁**：master 只接受来自 `test/integration` 的发布 PR（CI 强制检查，
  见 ci.yml release_source_check）；测试分支必须通过真实数据 E2E
  （注册→实验→模拟盘→前端全链路）后才允许发布到 master。
- 冲突时：后到者 rebase 最新测试分支，读两边代码解决，重跑 CI。
- 语义冲突由契约快照 diff 暴露；用户是行为变化的最终确认人。

### 线路

- master = 开发线（只收 CI 绿合并，永远可跑）；tag = 稳定线（只从 tag 部署）。
- 已发布版本出问题：`hotfix/v0.x.y-xxx` 从 tag 切出 → 修 → 合回 master → 打补丁 tag。

## Architecture

| Directory | Purpose |
|-----------|---------|
| `backend/` | FastAPI app (Python 3.11). Entry: `backend/main.py`. Config: `backend/config.py` |
| `frontend/` | React 19 + TypeScript + Vite + Tailwind CSS + Zustand |
| `data/` | Runtime SQLite DBs + Parquet cache + trained models |

北极星目标架构见 `docs/ARCHITECTURE_LEAN.md`（backend <40k 行、main.py <300 行、~60 端点、
单一事实源：一套价格存储、一份哈希、一份时间函数）。

**Three SQLite databases** (auto-created by `backend/main.py` on startup):
- `data/users.db` — auth, users, permissions
- `data/experiment.db` — experiments, results, param sweeps
- `data/trading_sim.db` — paper trading, portfolios, orders

## Config and env

Create `.env` in project root (gitignored). Minimal:

```env
JWT_SECRET=your-secret-key-change-me
DEEPSEEK_API_KEY=sk-xxxxxxxx   # optional, AI features disabled if missing
```

All config lives in `backend/config.py` → `Settings`. Paths relative to `PROJECT_ROOT`.
**Do not change `PROJECT_ROOT` or `DATABASE_DIR`.**

## Strategy system

- Strategies implement `StrategyProtocol` (`backend/strategies/base.py`); auto-registered via `Registry.scan()`.
- Trainable strategies extend `TrainableStrategy`; the walk-forward loop is owned by `backend/services/walkforward.py`. Do NOT write a private walk-forward loop inside `generate_batch_signals`.
- **Native DLL load order (Windows)**: `lightgbm` MUST be imported at module top BEFORE pandas/pyarrow. See guarded preload at top of `backend/strategies/ml/alpha158_lgb.py`.
- Stock pool IDs: `csi300`, `csi500`, `csi800`, `csi1000`, `all_a` (NOT `hs300`/`zz500`).

## Auth and RBAC

- JWT auth (`python-jose` + bcrypt via `passlib`). Token expiry: 24h.
- First registered user becomes admin. In production, first registration also requires `X-Bootstrap-Token`.
- 14 granular permissions (future direction: simplify to 2 tiers, see docs/PROJECT_PHILOSOPHY.md).

## Local service lifecycle

- Do not run persistent commands (`uvicorn`, `npm run dev`, `vite`) without detached mode.
- macOS/Linux:

```bash
mkdir -p .opencode
nohup python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 \
  > server_stdout.log 2> server_stderr.log &
echo $! > .opencode/backend.pid
```

Verify: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/docs`
Stop: `kill $(cat .opencode/backend.pid)`

## Key references

| File | What it covers |
|------|---------------|
| `docs/PROJECT_PHILOSOPHY.md` | 七条宪法（一切决策判据） |
| `docs/ARCHITECTURE_LEAN.md` | 北极星目标架构与验收标准 |
| `docs/VERSIONING.md` | 版本号规则 + 开发/稳定线 + hotfix 流程 |
| `docs/WORKFLOW_AUTOMATION.md` | 全自动流水线 + 并行安全机制 |
| `docs/todo/TODO_INDEX.md` | 版本队列（与 GitHub issue 对应） |
| `docs/API.md` | 端点参考（现状 181 个，目标 ~60） |

## Gotchas

- **Backend must start from project root** — `config.py` resolves `PROJECT_ROOT` relative to its own path.
- **No `request.json()` fallback** — routes read request body via pydantic models; FastAPI returns 422 without JSON content-type.
- **`backend/__init__.py` is empty** — intentional; don't add imports that break `python -m uvicorn backend.main:app`.
- **macOS: LightGBM/XGBoost need libomp** (`brew install libomp`); failed loads skip that strategy, rest of app keeps working.
- **macOS: PyTorch prefers MPS** on Apple Silicon (detected automatically).
- **契约快照目录 `backend/tests/snapshots/`**：行为不变的重构严禁改动；改动 = 行为变化声明，需用户确认。
