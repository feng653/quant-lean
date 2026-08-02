#!/bin/zsh

set -u
set -o pipefail
umask 077

repo=${QUANT_TUNING_REPO:-${0:A:h:h}}
artifact=${QUANT_TUNING_ARTIFACT_DIR:-$HOME/.local/state/quant-platform/frontend-tuning/non-ml-single-30-stock-locked-test-20260730}
node_bin=${QUANT_TUNING_NODE_BIN:-/Users/xuhe/.local/node/bin/node}
max_resume_attempts=${QUANT_TUNING_RESUME_ATTEMPTS:-5}
report=$artifact/report.json

if [[ $max_resume_attempts != <1-> ]] || (( max_resume_attempts < 1 || max_resume_attempts > 10 )); then
  print -u2 "QUANT_TUNING_RESUME_ATTEMPTS 必须是 1 到 10 的整数。"
  exit 64
fi
if [[ ! -x $node_bin ]]; then
  print -u2 "Node 不可执行：$node_bin"
  exit 69
fi

cleanup_credentials() {
  unset QUANT_TUNING_USERNAME QUANT_TUNING_PASSWORD
}
trap cleanup_credentials EXIT HUP INT TERM

cd "$repo" || exit 72
print "量化平台 136 次非 ML 单策略前端实验：请输入现有账号。密码不会显示、写入文件或命令参数。"
read "QUANT_TUNING_USERNAME?用户名: "
read -s "QUANT_TUNING_PASSWORD?密码: "
print
if [[ -z $QUANT_TUNING_USERNAME || -z $QUANT_TUNING_PASSWORD ]]; then
  print -u2 "用户名或密码为空，已取消。"
  exit 1
fi
export QUANT_TUNING_USERNAME QUANT_TUNING_PASSWORD

export QUANT_TUNING_PLAYWRIGHT_MODULE=${QUANT_TUNING_PLAYWRIGHT_MODULE:-/Users/xuhe/.local/share/quant-platform-playwright-1.55.0/node_modules/playwright/index.js}
export QUANT_TUNING_BROWSER_EXECUTABLE=${QUANT_TUNING_BROWSER_EXECUTABLE:-/Users/xuhe/Library/Caches/ms-playwright/chromium_headless_shell-1187/chrome-mac/headless_shell}
export QUANT_TUNING_ARTIFACT_DIR=$artifact
export QUANT_TUNING_FRONTEND_URL=${QUANT_TUNING_FRONTEND_URL:-http://127.0.0.1:5173}
export QUANT_TUNING_BACKEND_URL=${QUANT_TUNING_BACKEND_URL:-http://127.0.0.1:8000}

run_with_bounded_resume() {
  mode=$1
  label=$2
  attempt=1
  while (( attempt <= max_resume_attempts )); do
    "$node_bin" scripts/run_frontend_non_ml_tuning.mjs "$mode"
    result=$?
    if (( result == 0 )); then
      return 0
    fi
    if (( attempt >= max_resume_attempts )); then
      print -u2 "$label 已达到 $max_resume_attempts 次有限续跑上限。"
      return "$result"
    fi
    classification=$(
      "$node_bin" scripts/frontend_tuning/transient_failures.mjs "$report" 2>/dev/null
    )
    classifier_result=$?
    if (( classifier_result != 0 )) || [[ $classification != transient ]]; then
      print -u2 "$label 发生非瞬态错误；不会自动续跑。请查看：$report"
      return "$result"
    fi
    delay=$(( 5 * (2 ** (attempt - 1)) ))
    (( delay > 30 )) && delay=30
    print -u2 "$label 遇到已确认的瞬态网络/导航错误，${delay} 秒后从 checkpoint/intent 自动续跑（$attempt/$max_resume_attempts）。"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

print "正在进行真实浏览器登录与缓存只读预检……"
run_with_bounded_resume --live-preflight "前端预检" || exit $?

print "预检通过，开始 11 基线 + 114 调优 + 11 锁定测试。"
export QUANT_TUNING_EXECUTE_CONFIRM=136_FRONTEND_EXPERIMENTS
run_with_bounded_resume --execute "136 次非 ML 单策略前端实验" || exit $?
print "全部前端实验执行器已完成。报告：$report"
