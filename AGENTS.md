# 量化验证平台 (Quant Platform)

FastAPI + React monorepo for quantitative strategy backtesting, parameter sweeping, paper trading, and AI-driven analysis.

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

**CI gate order**: lint → build (frontend: `tsc -b && vite build`) → unit tests → integration tests.

## Architecture

| Directory | Purpose |
|-----------|---------|
| `backend/` | FastAPI app (Python 3.11). Entry: `backend/main.py`. Config: `backend/config.py` (pydantic-settings) |
| `frontend/` | React 19 + TypeScript + Vite + Tailwind CSS + Zustand |
| `data/` | Runtime SQLite DBs + Parquet cache + trained models |

**Three SQLite databases** (all auto-created by `backend/main.py` on startup):
- `data/users.db` — auth, users, permissions
- `data/experiment.db` — experiments, results, param sweeps
- `data/trading_sim.db` — paper trading, portfolios, orders

**API docs** at `http://localhost:8000/docs` (Swagger). Full reference: `docs/API.md`.

## Config and env

Create `.env` in project root (gitignored). Minimal:

```env
JWT_SECRET=your-secret-key-change-me
DEEPSEEK_API_KEY=sk-xxxxxxxx   # optional, AI features disabled if missing
```

All config lives in `backend/config.py` → `Settings`. Paths are relative to `PROJECT_ROOT` (the repo root). Do not change `PROJECT_ROOT` or `DATABASE_DIR` unless you move the `backend/` package.

## Strategy system

Strategies live in `backend/strategies/` subdirectories (`technical/`, `ml/`, `factor/`, `portfolio/`, `composite/`).

- All strategies implement `StrategyProtocol` (`backend/strategies/base.py`)
- **Auto-registration**: `Registry.scan()` discovers all modules recursively. No manual registration needed.
- **Trainable strategies** (ML models with periodic retraining): extend `TrainableStrategy` (`base.py`) and implement `prepare()`/`fit()`/`predict_scores()` only. The platform driver `backend/services/walkforward.py` owns the monthly walk-forward loop — schedule, train-window computation, progress reporting, cancellation, and failure propagation (3 consecutive fit failures raise with the real root cause; never silently return empty signals). `retrain_frequency=NEVER` means train-once on `_train_start/_train_end`. Do NOT write a private walk-forward loop inside `generate_batch_signals`.
- **Native DLL load order (Windows)**: `lightgbm` MUST be imported at module top BEFORE any pandas/pyarrow import. If pyarrow's native DLLs load first, every LightGBM Dataset call crashes with `OSError: exception: access violation reading 0x0000000000000000` (this was the root cause of experiment 102's failure). See the guarded preload at the top of `backend/strategies/ml/alpha158_lgb.py`.
- **Composite strategies** include equal-weight, risk-parity, momentum and regime variants. Their default children are rule-based strategies; validation rejects unknown IDs, duplicates, self-reference and nested composites.
- **Stock pool IDs**: `csi300`, `csi500`, `csi800`, `csi1000`, `all_a` (NOT `hs300`/`zz500` — old names cause "Data not found").
- New strategy? Add a module in the correct subdirectory + ensure `get_metadata()` returns proper pool/compatible fields.

## Auth and RBAC

- JWT auth (`python-jose` + bcrypt via `passlib`). Token expiry: 24h (`JWT_EXPIRE_MINUTES`).
- In development, the first registered user becomes admin. In production, the first registration also requires `X-Bootstrap-Token` matching `BOOTSTRAP_ADMIN_TOKEN`; all other users receive read-only permissions.
- Admin can grant permissions at `/admin`. 14 granular permissions covering experiments, trading, data, strategies, AI, and admin.
- **Route order matters in `main.py`**: catch-all `/api/{path}` routes must be registered AFTER specific routes.

## Agent workflow conventions

The default agent is **tech-lead** (see `.opencode/agents/tech-lead.md`, `docs/TECH_LEAD.md`):

- **Two-phase**: Clarify requirements first (output PRD, get confirmation) → then design + delegate + integrate.
- **Task-owned worktrees**: Follow `docs/WORKTREE_WORKFLOW.md`. Read-only work may use the
  primary tree; every repository-writing task must receive a manager-created task ID,
  absolute worktree path, branch, and base commit before the first edit.
- **Fail closed**: A writing agent without an assigned task worktree must refuse to edit.
  The lead creates and assigns worktrees; workers never reuse another agent's worktree.
- **Primary-tree boundary**: `master` is for lead-agent review, integration, and final
  verification only. Never develop, stash, reset, clean, or force-remove work there.
- **Never skip verification**: After sub-agents finish, run the backend, call the affected API, check the frontend.
- **Trunk-based**: `master` is the only long-lived branch. Agent work uses manager-created
  short-lived `codex/<task>-<agent>-<shortid>` branches.
- **Versioned planning records**: [`docs/ROADMAP.md`](docs/ROADMAP.md) is the version-level
  strategic plan. [`docs/todo/TODO_INDEX.md`](docs/todo/TODO_INDEX.md) is the only operational
  entry point: one dated code TODO per focused small version plus a separate non-code experiment
  operations TODO. `docs/EXECUTION_TODO.md` is compatibility-only and must not become a second
  board. Read the index and every referenced TODO from disk before accepting or resuming work.
- **TODO upkeep and reread loop are mandatory**: Update the owning version TODO before starting,
  switching, handing off, merging, deploying, blocking, or completing a task. After every
  completed item, reread `TODO_INDEX.md` and all referenced TODO files from disk. Finish code
  TODOs in version order, including review, merge and deployment, before non-code experiment
  operations. If any unfinished item remains, continue; stop only when all TODOs are complete or
  an external blocker cannot be removed within the current authorization. Preserve user edits
  verbatim; never silently delete or reorder a user item.
- **Focused releases**: Each patch/minor release has one clear product direction. When a request
  mixes unrelated directions, warn briefly and split it into ordered version TODOs before
  implementation. Use SemVer: patch for compatible fixes/polish, minor for compatible product
  capability, major only for an intentionally incompatible product/API/data contract.
- **Definition of Done**: CI passes + owning TODO updated + ROADMAP updated when its
  strategic status changed + docs synced if architecture changed.

### Model routing

The tech-lead is responsible for selecting and explicitly pinning a model whenever it delegates work. This project uses **only** these two models:

| Work type | Model | Typical work |
|-----------|-------|--------------|
| Fast, well-bounded supporting work | `gpt-5.6-terra` | Small scripts, mechanical edits, focused codebase exploration, test/log triage, and routine test additions. |
| Complex, ambiguous, or high-risk work | `gpt-5.6-sol` | Architecture and implementation decisions, cross-cutting changes, authentication/authorization, database migrations, strategy logic, incident diagnosis, security review, and final integration. |

- **Actively delegate simple scripts to Terra.** When a request has a known input/output, touches a small and isolated surface, and has a clear validation command, the lead should delegate it to `gpt-5.6-terra` with low or medium reasoning effort.
- **Escalate deliberately.** Use Sol when requirements are unclear, a change can affect data integrity, security, money/trading behavior, APIs, multiple subsystems, or when Terra reports uncertainty or a failed validation.
- **Keep the lead accountable.** The lead owns task decomposition, final review, integration, and verification. A Terra result is not approval to merge without the lead checking its diff and relevant tests.
- **No unlisted model fallback.** Do not request or configure any model other than `gpt-5.6-sol` and `gpt-5.6-terra`. If one is unavailable, use the other and state the fallback in the handoff.
- **Avoid needless delegation.** Do not spawn a subagent for a one-line answer, an action that must be performed serially, or a task whose coordination cost exceeds the work itself.

## Local service lifecycle

- **Never use the `process` tool** to start a long-running local service. It tracks child processes and can leave the agent session waiting indefinitely. Use the `bash` tool instead.
- Do not directly run persistent commands such as `python -m uvicorn`, `npm run dev`, `vite`, or `docker compose up` without detached mode.

### Windows

Start a persistent process with PowerShell `Start-Process -PassThru`, redirect stdout/stderr to log files, save its PID under `.opencode/`, and return immediately. For the backend:

```powershell
New-Item -ItemType Directory -Force .opencode | Out-Null
$proc = Start-Process -FilePath "python.exe" -ArgumentList "-m uvicorn backend.main:app --host 127.0.0.1 --port 8000" -WorkingDirectory "D:\doc\pi" -WindowStyle Hidden -RedirectStandardOutput "D:\doc\pi\server_stdout.log" -RedirectStandardError "D:\doc\pi\server_stderr.log" -PassThru
$proc.Id | Set-Content "D:\doc\pi\.opencode\backend.pid"
Write-Output "Backend started, PID=$($proc.Id)"
```

Verify: `Invoke-WebRequest http://127.0.0.1:8000/docs -TimeoutSec 5`
Stop: `Stop-Process -Id (Get-Content .opencode\backend.pid) -Force`

### macOS / Linux

Use `nohup` to detach, redirect stdout/stderr, and save PID:

```bash
mkdir -p .opencode
nohup python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 \
  > server_stdout.log 2> server_stderr.log &
echo $! > .opencode/backend.pid
echo "Backend started, PID=$(cat .opencode/backend.pid)"
```

Verify: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/docs`
Stop: `kill $(cat .opencode/backend.pid)`

## Key references

| File | What it covers |
|------|---------------|
| `docs/API.md` | All 60+ endpoints with request/response examples |
| `docs/ARCHITECTURE_V3.md` | System design, DB schema, strategy class hierarchy, RBAC model |
| `docs/STRATEGY_GUIDE.md` | How to implement a new strategy |
| `docs/ROADMAP.md` | Versioned strategic roadmap and release criteria |
| `docs/todo/TODO_INDEX.md` | Human-editable unique actual-work index and mandatory reread loop |
| `docs/EXECUTION_TODO.md` | Compatibility pointer to the TODO index; never a second board |
| `docs/TECH_LEAD.md` | Agent team workflow and DoD |
| `docs/strategies/` | Per-strategy design docs |

## Gotchas

- **Backend must start from project root** — `config.py` resolves `PROJECT_ROOT` relative to its own path. Starting from `backend/` will fail to find `data/` files.
- **No venv configured** — `pip install` is global. If you need isolation, create a venv yourself.
- **No `request.json()` fallback** — routes that read request body use `await request.json()`. If the request lacks `Content-Type: application/json`, FastAPI returns 422.
- **Frontend has Vitest coverage for deterministic portfolio allocation**. Add tests for new stateful UI logic.
- **E2E tests are placeholder** — CI step exists but Playwright/Cypress is not configured.
- **`backend/__init__.py` is empty** — this is intentional. Don't add imports that would break the `python -m uvicorn backend.main:app` module path.
- **macOS: LightGBM / XGBoost need libomp**. Without `brew install libomp`, `import lightgbm` (or xgboost) throws `OSError: dlopen(libomp.dylib)`. Native-tree strategies whose libraries fail to load are skipped or unavailable, while the rest of the app and other ML strategies keep working. Install libomp to enable them.
- **macOS: PyTorch uses MPS on Apple Silicon**. The LSTM and Transformer strategies detect `torch.backends.mps.is_available()` and prefer it over CPU.
