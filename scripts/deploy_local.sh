#!/bin/bash
# 本地一键部署（test/integration 测试版）：切换分支 → 启动后端 → 启动前端 → 健康检查。
# 用法: bash scripts/deploy_local.sh [--branch test/integration] [--backend-port 8001] [--frontend-port 5173]
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
branch="${BRANCH:-test/integration}"
backend_port="${BACKEND_PORT:-8001}"
frontend_port="${FRONTEND_PORT:-5173}"
python_bin="${PYTHON_BIN:-$project_dir/.venv/bin/python}"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "❌ $*"; exit 1; }

# ── 1. 分支切换 ────────────────────────────────────────────────────────────
cd "$project_dir"
current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$branch" ]]; then
  log "切换分支: $current_branch -> $branch"
  if ! git checkout "$branch" 2>&1 | tail -1; then
    fail "切换分支失败（可能有未提交改动，先 stash 或 commit）"
  fi
fi
git pull origin "$branch" 2>&1 | tail -1 || log "pull 跳过（网络或无需更新）"
log "当前分支: $branch @ $(git rev-parse --short HEAD)"

# ── 2. 停旧服务 ────────────────────────────────────────────────────────────
log "停止旧服务（后端 $backend_port / 前端 $frontend_port）"
if [[ -f .opencode/backend.pid ]]; then
  kill "$(cat .opencode/backend.pid)" 2>/dev/null || true
  rm -f .opencode/backend.pid
fi
if [[ -f /tmp/quant_lean_frontend.pid ]]; then
  kill "$(cat /tmp/quant_lean_frontend.pid)" 2>/dev/null || true
  rm -f /tmp/quant_lean_frontend.pid
fi
# 兜底清理端口残留
for p in $backend_port $frontend_port; do
  for pid in $(lsof -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null || true); do
    log "清理端口 $p 残留进程 $pid"
    kill -9 "$pid" 2>/dev/null || true
  done
done
sleep 1

# ── 3. 启动后端 ────────────────────────────────────────────────────────────
[[ -x "$python_bin" ]] || fail "找不到 venv python: $python_bin（先: uv venv --python 3.11 .venv && uv pip install -r requirements.txt）"
log "启动后端 (port $backend_port)"
mkdir -p .opencode
nohup "$python_bin" -m uvicorn backend.main:app \
  --host 127.0.0.1 --port "$backend_port" \
  > /tmp/quant_lean_backend.log 2>&1 &
echo $! > .opencode/backend.pid

for _ in $(seq 1 30); do
  if curl -s -m 5 -o /dev/null "http://127.0.0.1:$backend_port/api/health" 2>/dev/null; then
    break
  fi
  sleep 1
done
curl -s -m 5 -o /dev/null "http://127.0.0.1:$backend_port/api/health" || fail "后端启动失败（见 /tmp/quant_lean_backend.log）"
log "后端健康检查通过 (commit $(git rev-parse --short HEAD))"

# ── 4. 启动前端 ────────────────────────────────────────────────────────────
if [[ -d frontend/node_modules ]]; then
  log "启动前端 (port $frontend_port, API -> $backend_port)"
  cd frontend
  VITE_API_URL="http://127.0.0.1:$backend_port" nohup npm run dev -- \
    --host 127.0.0.1 --port "$frontend_port" --strictPort \
    > /tmp/quant_lean_frontend.log 2>&1 &
  echo $! > /tmp/quant_lean_frontend.pid
  cd "$project_dir"
  for _ in $(seq 1 30); do
    if curl -s -m 5 -o /dev/null "http://127.0.0.1:$frontend_port/login" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  curl -s -m 5 -o /dev/null "http://127.0.0.1:$frontend_port/login" \
    || log "⚠️ 前端未就绪（见 /tmp/quant_lean_frontend.log）"
  log "前端就绪: http://127.0.0.1:$frontend_port"
else
  log "⚠️ frontend/node_modules 缺失，跳过前端（先: cd frontend && npm ci）"
fi

# ── 5. 汇总 ────────────────────────────────────────────────────────────────
log "✅ 部署完成"
log "   前端: http://127.0.0.1:$frontend_port"
log "   后端: http://127.0.0.1:$backend_port"
log "   分支: $branch @ $(git rev-parse --short HEAD)"
log "   停止: kill \$(cat .opencode/backend.pid) && kill \$(cat /tmp/quant_lean_frontend.pid)"
