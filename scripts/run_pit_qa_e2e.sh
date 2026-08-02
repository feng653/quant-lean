#!/bin/bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
qa_root="${PIT_QA_ROOT:-}"
if [[ -z "$qa_root" ]]; then
  qa_root="$(mktemp -d "${TMPDIR:-/tmp}/quant-pit-qa.XXXXXX")"
fi
qa_root="$(cd "$qa_root" && pwd)"
report_path="${PIT_QA_REPORT:-$qa_root/pit-qa-report.json}"
backend_port="${PIT_QA_BACKEND_PORT:-18080}"
frontend_port="${PIT_QA_FRONTEND_PORT:-15173}"

export ENVIRONMENT=test
export JWT_SECRET="pit-qa-only-jwt-secret-that-is-never-production"
export DATABASE_DIR="$qa_root"
export USERS_DB="$qa_root/users.db"
export EXPERIMENT_DB="$qa_root/experiment.db"
export TRADING_SIM_DB="$qa_root/trading.db"
export TRADING_LIVE_DB="$qa_root/trading-live.db"
export DATA_CACHE_DIR="$qa_root/cache"
export DATA_STAGING_DIR="$qa_root/staging"
export PIT_EVIDENCE_DIR="$qa_root/pit-evidence"
export PIT_EVIDENCE_DB="$qa_root/pit-evidence/governance.db"
export MODEL_STORE_DIR="$qa_root/models"
export RESEARCH_SNAPSHOT_DIR="$qa_root/research-snapshots"
export PIT_QA_FIXTURE_ROOT="$qa_root"
export PIT_QA_ATTESTATION="$qa_root/pit-qa-attestation.json"
export PAPER_SIMULATION_AUTO_RUN=false
export MODEL_RETRAIN_AUTO_RUN=false
export CORS_ORIGINS="[\"http://127.0.0.1:$frontend_port\"]"

backend_pid=""
frontend_pid=""
stop_services() {
  if [[ -n "$frontend_pid" ]]; then kill "$frontend_pid" 2>/dev/null || true; fi
  if [[ -n "$backend_pid" ]]; then kill "$backend_pid" 2>/dev/null || true; fi
}
trap stop_services EXIT INT TERM

cd "$project_dir"
python_bin="${PIT_QA_PYTHON:-$project_dir/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  common_git_dir="$(git -C "$project_dir" rev-parse --path-format=absolute --git-common-dir)"
  primary_venv="$(dirname "$common_git_dir")/.venv/bin/python"
  if [[ -x "$primary_venv" ]]; then python_bin="$primary_venv"; else python_bin="python3"; fi
fi

"$python_bin" scripts/prepare_pit_qa_fixture.py --root "$qa_root"
nohup "$python_bin" -m uvicorn backend.main:app \
  --host 127.0.0.1 --port "$backend_port" \
  > "$qa_root/backend.log" 2>&1 &
backend_pid="$!"

for _ in $(seq 1 60); do
  if curl --silent --fail "http://127.0.0.1:$backend_port/api/health" >/dev/null; then
    break
  fi
  sleep 1
done
curl --silent --fail "http://127.0.0.1:$backend_port/api/health" >/dev/null

cd "$project_dir/frontend"
if [[ ! -x node_modules/.bin/vite ]]; then
  npm ci
fi
VITE_API_URL="http://127.0.0.1:$backend_port" nohup npm run dev -- \
  --host 127.0.0.1 --port "$frontend_port" \
  > "$qa_root/frontend.log" 2>&1 &
frontend_pid="$!"
for _ in $(seq 1 60); do
  if curl --silent --fail "http://127.0.0.1:$frontend_port/login" >/dev/null; then
    break
  fi
  sleep 1
done
curl --silent --fail "http://127.0.0.1:$frontend_port/login" >/dev/null

cd "$project_dir"
browser_args=(
  --base-url "http://127.0.0.1:$frontend_port"
  --api-url "http://127.0.0.1:$backend_port"
  --report "$report_path"
)
if [[ "${PIT_QA_ALL:-0}" == "1" ]]; then browser_args+=(--all); fi
node scripts/run_pit_qa_browser.mjs "${browser_args[@]}"
"$python_bin" scripts/verify_pit_qa_results.py \
  --root "$qa_root" --report "$report_path"

printf '%s\n' "PIT QA verified. Report: $report_path"
printf '%s\n' "QA data retained for inspection: $qa_root"
