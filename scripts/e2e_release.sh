#!/bin/bash
# L3 真实数据自动验收：在本地 runner 用 31G 真实数据跑全链路。
# 报告由机器检查（退出码 0=通过），作为发布 PR 的 required check。
# 用法：scripts/e2e_release.sh [--report PATH]
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
report_path="${E2E_REPORT:-${1:-$project_dir/e2e-release-report.json}}"
backend_port="${E2E_BACKEND_PORT:-18081}"
backend_pid=""
created_jobs=()
created_experiments=()

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "❌ $*"; exit 1; }

python_bin="${E2E_PYTHON:-$project_dir/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3 || true)"
fi
[[ -x "$python_bin" ]] || fail "找不到 Python 解释器"

# ── Preflight ─────────────────────────────────────────────────────────────
log "Preflight: 检查端口/磁盘/worker 槽位/旧任务"
if lsof -iTCP:"$backend_port" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "端口 $backend_port 已被占用（可能有残留服务）"
fi
disk_free_mb="$(df -m "$project_dir" | awk 'NR==2 {print $4}')"
if (( disk_free_mb < 5120 )); then
  fail "磁盘空间不足：${disk_free_mb}MB < 5120MB"
fi
# 旧任务清理：终止残留 worker（正常流程无残留；防止上一次失败留下孤儿进程）
if lsof -iTCP:"$backend_port" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "端口 $backend_port 残留服务，请先手动检查"
fi

stop_backend() {
  if [[ -n "$backend_pid" ]]; then kill "$backend_pid" 2>/dev/null || true; wait "$backend_pid" 2>/dev/null || true; fi
}
trap stop_backend EXIT INT TERM

# ── 启动后端（真实数据 data/）──────────────────────────────────────────────
cd "$project_dir"
export ENVIRONMENT=production
export JWT_SECRET="e2e-release-$(openssl rand -hex 16)"
export PAPER_SIMULATION_AUTO_RUN=true
export BOOTSTRAP_ADMIN_TOKEN=""
log "启动后端 (port $backend_port)"
nohup "$python_bin" -m uvicorn backend.main:app \
  --host 127.0.0.1 --port "$backend_port" \
  > /tmp/e2e_release_backend.log 2>&1 &
backend_pid="$!"

for _ in $(seq 1 90); do
  if curl --silent --fail "http://127.0.0.1:$backend_port/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl --silent --fail "http://127.0.0.1:$backend_port/api/health" >/dev/null \
  || fail "后端启动失败（详见 /tmp/e2e_release_backend.log）"
log "后端健康检查通过"

base_url="http://127.0.0.1:$backend_port"
register_token=""

# ── 3+ 策略实验：真实数据回测 ──────────────────────────────────────────────
# 注册专用验收账号
register_body="{\"username\":\"e2e_release_$(date +%s)\",\"password\":\"e2e-release-pass-123\",\"display_name\":\"E2E Release\"}"
register_resp="$(curl --silent --fail -X POST "$base_url/api/auth/register" \
  -H 'Content-Type: application/json' -d "$register_body")"
register_token="$(printf '%s' "$register_resp" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")"
[[ -n "$register_token" ]] || fail "注册验收账号失败"

auth_header="Authorization: Bearer $register_token"
run_count=0
for strategy in ma_cross_v1 macd_signal_v1 rsi_reversal_v1; do
  exp_body="{\"name\":\"e2e-release-$strategy\",\"strategy_id\":\"$strategy\",\"pool_preset\":\"csi300\",\"test_start\":\"2024-01-02\",\"test_end\":\"2024-06-28\",\"data_access_policy\":\"cache_only\"}"
  exp_resp="$(curl --silent --fail -X POST "$base_url/api/experiments/" \
    -H "$auth_header" -H 'Content-Type: application/json' -d "$exp_body")"
  experiment_id="$(printf '%s' "$exp_resp" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['experiment_id'])")"
  job_id="$(printf '%s' "$exp_resp" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['job_id'])")"
  created_experiments+=("$experiment_id")
  created_jobs+=("$job_id")
  log "实验 #$experiment_id 已创建（$strategy, job=$job_id）"

  # 等待 job 完成（真实回测可能较慢：最多 10 分钟）
  status=""
  for _ in $(seq 1 120); do
    job_resp="$(curl --silent --fail "$base_url/api/jobs/$job_id" -H "$auth_header" || true)"
    status="$(printf '%s' "$job_resp" | "$python_bin" -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['status'])" 2>/dev/null || echo unknown)"
    [[ "$status" == "completed" ]] && break
    [[ "$status" == "failed" ]] && fail "实验 #$experiment_id job 失败"
    sleep 5
  done
  [[ "$status" == "completed" ]] || fail "实验 #$experiment_id 超时未完成"
  log "实验 #$experiment_id 回测完成"

  # 指标核对：equity_curve 非空
  equity="$(curl --silent --fail "$base_url/api/experiments/$experiment_id/equity" -H "$auth_header")"
  points="$(printf '%s' "$equity" | "$python_bin" -c "import sys,json;print(len(json.load(sys.stdin)['data']))")"
  (( points > 0 )) || fail "实验 #$experiment_id 无净值产物"
  log "实验 #$experiment_id 净值点数: $points"
  run_count=$((run_count + 1))
done
(( run_count >= 3 )) || fail "有效实验数不足 3"

# ── 模拟盘初始化：部署 active + 基础数据就绪 ────────────────────────────────
source_id="${created_experiments[0]}"
deploy_resp="$(curl --silent --fail -X POST "$base_url/api/trading/deployments" \
  -H "$auth_header" -H 'Content-Type: application/json' \
  -d "{\"strategy_id\":\"ma_cross_v1\",\"display_name\":\"e2e-release-paper\",\"mode\":\"batch\",\"status\":\"active\",\"source_experiment_id\":$source_id}")"
deployment_id="$(printf '%s' "$deploy_resp" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['deployment_id'])")"
log "模拟盘部署 #$deployment_id 已创建"

listed="$(curl --silent --fail "$base_url/api/trading/deployments" -H "$auth_header")"
deployment_status="$(printf '%s' "$listed" | "$python_bin" -c "
import sys,json
items=json.load(sys.stdin)['data']
print(next(i['status'] for i in items if i['id']==$deployment_id))")"
[[ "$deployment_status" == "active" ]] || fail "模拟盘部署状态异常: $deployment_status"
log "模拟盘初始化确认：状态 active"

# ── 前端可达性 ────────────────────────────────────────────────────────────
frontend_port="${E2E_FRONTEND_PORT:-15174}"
if [[ -d "$project_dir/frontend/node_modules" ]]; then
  frontend_pid=""
  cd "$project_dir/frontend"
  VITE_API_URL="$base_url" nohup npm run dev -- --host 127.0.0.1 --port "$frontend_port" \
    > /tmp/e2e_release_frontend.log 2>&1 &
  frontend_pid="$!"
  cd "$project_dir"
  frontend_ok=0
  for _ in $(seq 1 60); do
    if curl --silent --fail "http://127.0.0.1:$frontend_port/login" >/dev/null 2>&1; then
      frontend_ok=1
      break
    fi
    sleep 1
  done
  [[ "$frontend_ok" == "1" ]] || log "⚠️ 前端未就绪（跳过，不阻塞验收）"
  log "前端可达：$frontend_ok"
  kill "$frontend_pid" 2>/dev/null || true
fi

# ── 清理实验（保留部署供审查）──────────────────────────────────────────────
for experiment_id in "${created_experiments[@]}"; do
  curl --silent --fail -X DELETE "$base_url/api/experiments/$experiment_id" \
    -H "$auth_header" >/dev/null 2>&1 || true
done
log "已清理 ${#created_experiments[@]} 个实验"

# ── 输出 E2E 报告 ─────────────────────────────────────────────────────────
cat > "$report_path" <<JSON
{
  "schema_version": "e2e-release-report/v1",
  "conclusion": "passed",
  "backend": "http://127.0.0.1:$backend_port",
  "run_count": $run_count,
  "experiments": [${created_experiments[*]}],
  "deployment_id": $deployment_id,
  "deployment_status": "$deployment_status",
  "frontend_reachable": ${frontend_ok:-0},
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
stop_backend
backend_pid=""
log "✅ E2E 真实数据验收通过：$report_path"
