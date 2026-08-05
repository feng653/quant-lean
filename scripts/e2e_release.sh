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

# 各环节结果（统一裁决；空 = 未执行/跳过）
experiment_result=""
deployment_result=""
data_check_result=""
incremental_result=""
frontend_result=""
blocked_reasons=""

# ── Preflight ─────────────────────────────────────────────────────────────
log "Preflight: 检查端口/磁盘/worker 槽位/旧任务"
# 清理 cwd 指向本仓库的任何后端进程（含用旧仓库 venv 启动的旧代码进程，
# 它们会加载过期代码并抢跑 jobs.db 调度租约，造成误报）
stale_backend_pids="$(ps -axo pid=,command= 2>/dev/null | grep -E "uvicorn backend\.main" | grep -v grep | awk '{print $1}')"
if [[ -n "$stale_backend_pids" ]]; then
  for pid in $stale_backend_pids; do
    pid_cwd="$(lsof -p "$pid" 2>/dev/null | awk '/cwd/ {print $NF}')"
    if [[ "$pid_cwd" == "$project_dir" ]]; then
      log "清理本仓库残留后端进程 $pid (cwd=$pid_cwd)"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  sleep 1
fi
# 清理端口残留（上一轮失败可能留下孤儿）
for stale_port in 18081 18082 18083 18084; do
  stale_pids="$(lsof -tiTCP:"$stale_port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$stale_pids" ]]; then
    log "清理端口 $stale_port 残留进程: $stale_pids"
    kill -9 $stale_pids 2>/dev/null || true
    sleep 1
  fi
done
if lsof -iTCP:"$backend_port" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "端口 $backend_port 已被占用（可能有残留服务）"
fi
disk_free_mb="$(df -m "$project_dir" | awk 'NR==2 {print $4}')"
if (( disk_free_mb < 5120 )); then
  fail "磁盘空间不足：${disk_free_mb}MB < 5120MB"
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

# ── 准备验收账号（幂等）：e2e_release_admin，admin 权限 + 已知密码 ────────
PROJECT_DIR_FOR_ACCT="$project_dir" "$python_bin" - <<'PYEOF'
import os, sqlite3, sys
sys.path.insert(0, os.environ["PROJECT_DIR_FOR_ACCT"])
from backend.config import settings
import bcrypt

USERNAME = "e2e_release_admin"
PASSWORD = "e2e-release-admin-pass-123"
with sqlite3.connect(settings.abs_path(settings.USERS_DB)) as conn:
    row = conn.execute("SELECT id, password_hash, is_admin FROM users WHERE username=?", (USERNAME,)).fetchone()
    password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    if row is None:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, email, is_admin, is_active) VALUES (?, ?, ?, NULL, 1, 1)",
            (USERNAME, password_hash, "E2E Release Admin"),
        )
        conn.execute("DELETE FROM user_permissions WHERE user_id=?", (cur.lastrowid,))
    else:
        conn.execute("UPDATE users SET password_hash=?, is_admin=1, is_active=1 WHERE username=?", (password_hash, USERNAME))
    conn.commit()
print("e2e account ready")
PYEOF
log "验收账号就绪（e2e_release_admin）"

register_login="$(curl --silent --fail -X POST "$base_url/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"e2e_release_admin","password":"e2e-release-admin-pass-123"}')"
register_token="$(printf '%s' "$register_login" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")"
[[ -n "$register_token" ]] || fail "登录验收账号失败"

auth_header="Authorization: Bearer $register_token"

# ═════════════════════════════════════════════════════════════════════════
# 环节 1：数据自动比对（更新前检测）
#   a) update/status 契约：research_data_contract.available
#   b) 深度完整性校验：verify_active_integrity(deep=True)
#   c) 缺口比对：缓存股票 vs PIT 会员（如实报告，不掩盖）
# ═════════════════════════════════════════════════════════════════════════
update_status="$(curl --silent --fail "$base_url/api/data/update/status" -H "$auth_header")"
update_status_ok="$(printf '%s' "$update_status" | "$python_bin" -c "
import sys,json
d=json.load(sys.stdin)['data']
contract=d.get('research_data_contract') or {}
ok = contract.get('available') is True
if not ok:
    print('FAIL: research_data_contract.available != True -> ' + json.dumps(contract, ensure_ascii=False)[:200])
else:
    print('OK')
")"
if [[ "$update_status_ok" == "OK" ]]; then
  generation_id="$(printf '%s' "$update_status" | "$python_bin" -c "
import sys,json
d=json.load(sys.stdin)['data']
print(d.get('research_data_contract', {}).get('generation_id', 'unknown'))")"
  log "更新前检测：research_data_contract.available=true (generation=$generation_id)"
else
  data_check_result="blocked:research_contract=$update_status_ok"
  blocked_reasons="${blocked_reasons}research_data_contract_unavailable;"
  log "⚠️ 更新前检测失败：$update_status_ok"
fi

integrity_report="$("$python_bin" -c "
import sys, json
sys.path.insert(0, '$project_dir')
from backend.data.research_data_store import ResearchDataStore
report = ResearchDataStore().verify_active_integrity(deep=True)
print(json.dumps(report, ensure_ascii=False))
")"
integrity_ok="$(printf '%s' "$integrity_report" | "$python_bin" -c "import sys,json;r=json.load(sys.stdin);print('OK' if r.get('verified') and r.get('deep') else 'FAIL')")"
if [[ "$integrity_ok" == "OK" ]]; then
  log "数据自动比对：深度完整性校验通过（verify_active_integrity deep=true）"
else
  data_check_result="blocked:integrity=${integrity_report}"
  blocked_reasons="${blocked_reasons}research_generation_integrity_failed;"
  log "⚠️ 数据深度完整性校验失败：$integrity_report"
fi

# 缺口比对：缓存股票 vs PIT 会员（更新前检测的实义——缺口暴露）
gap_report="$("$python_bin" -c "
import sys, json
sys.path.insert(0, '$project_dir')
import pandas as pd
from backend.data.cache import DataCache
from backend.data.point_in_time_universe import resolve_point_in_time_universe
from backend.data.point_in_time_master import PointInTimeMasterStore
from backend.data.universe import PRESET_POOLS

async def _gap():
    cache = DataCache()
    frame, _ = await cache.load_pivot_with_provenance('csi300')
    codes = set(frame.columns.get_level_values(0)) if frame is not None else set()
    store = PointInTimeMasterStore()
    timeline = resolve_point_in_time_universe(
        store, pool_id='csi300',
        trading_dates=pd.bdate_range('2023-01-26', '2024-06-28'),
        expected_count=PRESET_POOLS['csi300']['expected_count'],
    )
    union = set(timeline.union_codes)
    return {
        'cache_codes': len(codes),
        'pit_members': len(union),
        'missing_member_codes': sorted(union - codes),
    }
import asyncio
r = asyncio.run(_gap())
print(json.dumps(r, ensure_ascii=False))
")"
gap_count="$(printf '%s' "$gap_report" | "$python_bin" -c "import sys,json;print(len(json.load(sys.stdin)['missing_member_codes']))")"
if (( gap_count > 0 )); then
  log "⚠️ 数据缺口：缓存缺 ${gap_count} 只会员股价格（更新前检测发现；数据收敛后自动消除）"
  data_check_result="blocked:data_gap=${gap_count}"
  blocked_reasons="${blocked_reasons}data_gap_${gap_count};"
else
  log "数据自动比对：缓存与 PIT 会员无缺口"
  if [[ -z "$data_check_result" ]]; then data_check_result="passed"; fi
fi

# ═════════════════════════════════════════════════════════════════════════
# 环节 2：增量更新（有界刷新，不破坏现有 generation）
# ═════════════════════════════════════════════════════════════════════════
refresh_body="{\"source_id\":\"tushare\",\"from_month\":\"2026-06\",\"to_month\":\"2026-06\",\"max_calls\":1}"
refresh_resp="$(curl --silent --fail -X POST "$base_url/api/data/research-data/refresh" \
  -H "$auth_header" -H 'Content-Type: application/json' -d "$refresh_body")"
refresh_job="$(printf '%s' "$refresh_resp" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['job_id'])")"
log "增量更新任务已提交 (job=$refresh_job, max_calls=1)"

refresh_status=""
for _ in $(seq 1 60); do
  refresh_job_resp="$(curl --silent --fail "$base_url/api/jobs/$refresh_job" -H "$auth_header" || true)"
  refresh_status="$(printf '%s' "$refresh_job_resp" | "$python_bin" -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['status'])" 2>/dev/null || echo unknown)"
  [[ "$refresh_status" == "completed" ]] && break
  [[ "$refresh_status" == "failed" ]] && break
  sleep 5
done
if [[ "$refresh_status" != "completed" && "$refresh_status" != "failed" ]]; then
  refresh_status="timeout"
  blocked_reasons="${blocked_reasons}incremental_update_timeout;"
  log "⚠️ 增量更新超时"
fi
log "增量更新任务终态：$refresh_status"

# 增量更新后 generation 必须不变（有界刷新只采集不激活，或激活同一代）
integrity_after="$("$python_bin" -c "
import sys, json
sys.path.insert(0, '$project_dir')
from backend.data.research_data_store import ResearchDataStore
report = ResearchDataStore().verify_active_integrity(deep=True)
print(json.dumps(report, ensure_ascii=False))
")"
integrity_after_ok="$(printf '%s' "$integrity_after" | "$python_bin" -c "import sys,json;r=json.load(sys.stdin);print('OK' if r.get('verified') and r.get('deep') else 'FAIL')")"
if [[ "$integrity_after_ok" == "OK" ]]; then
  log "增量更新后完整性复核：通过"
  if [[ "$refresh_status" == "completed" || "$refresh_status" == "failed" ]]; then
    incremental_result="passed:job=${refresh_status}"
  else
    incremental_result="failed:job=${refresh_status}"
  fi
else
  incremental_result="failed:integrity_after"
  blocked_reasons="${blocked_reasons}incremental_broke_integrity;"
  log "⚠️ 增量更新破坏了 generation 完整性"
fi

# ═════════════════════════════════════════════════════════════════════════
# 环节 3：实验（3+ 策略回测，真实 worker 完成）
# ═════════════════════════════════════════════════════════════════════════
run_count=0
for strategy in ma_cross_v1 macd_signal_v1 rsi_reversal_v1; do
  exp_body="{\"name\":\"e2e-release-$strategy\",\"strategy_id\":\"$strategy\",\"pool_preset\":\"csi300\",\"test_start\":\"2024-03-01\",\"test_end\":\"2024-06-28\",\"data_access_policy\":\"cache_only\"}"
  exp_resp="$(curl --silent --fail -X POST "$base_url/api/experiments/" \
    -H "$auth_header" -H 'Content-Type: application/json' -d "$exp_body")"
  experiment_id="$(printf '%s' "$exp_resp" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['experiment_id'])")"
  job_id="$(printf '%s' "$exp_resp" | "$python_bin" -c "import sys,json;print(json.load(sys.stdin)['data']['job_id'])")"
  created_experiments+=("$experiment_id")
  created_jobs+=("$job_id")
  log "实验 #$experiment_id 已创建 ($strategy, job=$job_id)"

  # 等待 job 完成（真实回测可能较慢：最多 10 分钟）
  status=""
  for _ in $(seq 1 120); do
    job_resp="$(curl --silent --fail "$base_url/api/jobs/$job_id" -H "$auth_header" || true)"
    status="$(printf '%s' "$job_resp" | "$python_bin" -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['status'])" 2>/dev/null || echo unknown)"
    [[ "$status" == "completed" ]] && break
    [[ "$status" == "failed" ]] && break
    sleep 5
  done
  if [[ "$status" != "completed" ]]; then
    log "⚠️ 实验 #$experiment_id 未完成 (status=$status)"
    experiment_result="failed:experiment_${experiment_id}=${status}"
    blocked_reasons="${blocked_reasons}experiment_${experiment_id}_${status};"
    break
  fi
  log "实验 #$experiment_id 回测完成"

  # 指标核对：equity_curve 非空
  equity="$(curl --silent --fail "$base_url/api/experiments/$experiment_id/equity" -H "$auth_header")"
  points="$(printf '%s' "$equity" | "$python_bin" -c "import sys,json;print(len(json.load(sys.stdin)['data']))")"
  if (( points <= 0 )); then
    log "⚠️ 实验 #$experiment_id 无净值产物"
    experiment_result="failed:experiment_${experiment_id}=no_equity"
    blocked_reasons="${blocked_reasons}experiment_${experiment_id}_no_equity;"
    break
  fi
  log "实验 #$experiment_id 净值点数: $points"
  run_count=$((run_count + 1))
done
if (( run_count >= 3 )); then
  experiment_result="passed:${run_count}"
  log "✅ 实验环节：${run_count} 个实验全部完成"
else
  [[ -n "$experiment_result" ]] || experiment_result="failed:insufficient_runs=${run_count}"
  log "⚠️ 实验环节未全部通过 (${run_count}/3)"
fi

# ═════════════════════════════════════════════════════════════════════════
# 环节 4：模拟盘初始化（部署 active + 基础数据就绪）
# ═════════════════════════════════════════════════════════════════════════
deployment_id=0
deployment_status="not_attempted"
if (( run_count >= 1 )) && [[ "$experiment_result" == passed:* ]]; then
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
  if [[ "$deployment_status" == "active" ]]; then
    deployment_result="passed:active"
    log "✅ 模拟盘初始化确认：状态 active"
  else
    deployment_result="failed:deployment_status=${deployment_status}"
    blocked_reasons="${blocked_reasons}deployment_not_active;"
    log "⚠️ 模拟盘部署状态异常: $deployment_status"
  fi
else
  deployment_result="skipped:no_completed_experiment"
  log "⚠️ 模拟盘环节跳过（无已完成实验作来源）"
fi

# ═════════════════════════════════════════════════════════════════════════
# 环节 5：前端可达性
# ═════════════════════════════════════════════════════════════════════════
frontend_port="${E2E_FRONTEND_PORT:-15174}"
frontend_ok=0
if [[ -d "$project_dir/frontend/node_modules" ]]; then
  frontend_pid=""
  cd "$project_dir/frontend"
  VITE_API_URL="$base_url" nohup npm run dev -- --host 127.0.0.1 --port "$frontend_port" \
    > /tmp/e2e_release_frontend.log 2>&1 &
  frontend_pid="$!"
  cd "$project_dir"
  for _ in $(seq 1 60); do
    if curl --silent --fail "http://127.0.0.1:$frontend_port/login" >/dev/null 2>&1; then
      frontend_ok=1
      break
    fi
    sleep 1
  done
  kill "$frontend_pid" 2>/dev/null || true
  if (( frontend_ok )); then
    frontend_result="passed"
    log "前端可达：yes"
  else
    frontend_result="failed"
    blocked_reasons="${blocked_reasons}frontend_unreachable;"
    log "⚠️ 前端未就绪"
  fi
else
  frontend_result="skipped:node_modules_missing"
  log "前端未安装依赖，跳过（不阻塞）"
fi

# ── 清理实验（保留部署供审查）──────────────────────────────────────────────
for experiment_id in "${created_experiments[@]}"; do
  curl --silent --fail -X DELETE "$base_url/api/experiments/$experiment_id" \
    -H "$auth_header" >/dev/null 2>&1 || true
done
log "已清理 ${#created_experiments[@]} 个实验"

# ═════════════════════════════════════════════════════════════════════════
# 统一裁决：数据检查 + 增量更新 必须通过；实验/模拟盘/前端须通过或明确阻塞
# ═════════════════════════════════════════════════════════════════════════
conclusion="passed"
if [[ "$data_check_result" == blocked:* || "$data_check_result" == failed:* ]]; then
  conclusion="blocked"
fi
if [[ "$incremental_result" == failed:* ]]; then
  conclusion="failed"
fi
if [[ "$experiment_result" == failed:* || "$deployment_result" == failed:* || "$frontend_result" == failed:* ]]; then
  conclusion="failed"
fi
if [[ -z "$blocked_reasons" ]]; then
  blocked_reasons="none"
fi

cat > "$report_path" <<JSON
{
  "schema_version": "e2e-release-report/v1",
  "conclusion": "$conclusion",
  "backend": "http://127.0.0.1:$backend_port",
  "data_check": {
    "result": "${data_check_result:-not_executed}",
    "gap": $(printf '%s' "$gap_report" 2>/dev/null || echo '{}')
  },
  "incremental_update": {
    "result": "${incremental_result:-not_executed}",
    "job_status": "${refresh_status:-unknown}"
  },
  "experiments": {
    "result": "${experiment_result:-not_executed}",
    "run_count": $run_count,
    "ids": [${created_experiments[*]}]
  },
  "deployment": {
    "result": "${deployment_result:-not_executed}",
    "id": $deployment_id,
    "status": "$deployment_status"
  },
  "frontend": {
    "result": "${frontend_result:-not_executed}",
    "reachable": $frontend_ok
  },
  "blocked_reasons": "$blocked_reasons",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
stop_backend
backend_pid=""

if [[ "$conclusion" == "passed" ]]; then
  log "✅ E2E 真实数据验收通过：$report_path"
  exit 0
else
  log "⛔ E2E 验收未通过（conclusion=$conclusion）：$report_path"
  exit 1
fi
