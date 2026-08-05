#!/usr/bin/env bash
# ============================================================
# lean-workflow 初始化脚本
# 在目标 git 仓库根目录运行：bash <skill 目录>/init.sh [选项]
# 或由协调者 agent 按 SKILL.md「初始化」章节执行相同步骤。
#
# 选项（全部可选）：
#   --owner <gh 用户名>         GitHub 仓库所有者（默认：从 git remote 推导）
#   --actor <gh 用户名>         自动 agent 门控用户（默认：gh api user 或 --owner）
#   --no-github-agent           不生成 opencode.yml（跳过 GitHub Actions 自动 agent）
#   --yes                       非交互（无确认提示）
#   --dir <path>                目标目录（默认：$PWD）
# ============================================================
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TPL="$SKILL_DIR/templates"

OWNER=""
ACTOR=""
GITHUB_AGENT=1
YES=0
TARGET="$PWD"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner) OWNER="$2"; shift 2 ;;
    --actor) ACTOR="$2"; shift 2 ;;
    --no-github-agent) GITHUB_AGENT=0; shift ;;
    --yes) YES=1; shift ;;
    --dir) TARGET="$2"; shift 2 ;;
    *) echo "未知选项: $1"; exit 1 ;;
  esac
done

cd "$TARGET"

# ---------- 1. 检查 git 仓库 ----------
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "❌ 不是 git 仓库：$TARGET（请先 git init 或 clone）"; exit 1
fi
if [[ -n "$(git status --porcelain)" ]] && [[ "$YES" == 0 ]]; then
  echo "⚠️  工作区有未提交改动。建议先提交/清理再初始化。回车继续或 Ctrl-C 中止。"; read -r _
fi

# ---------- 2. 推导 OWNER / REPO ----------
REPO="$(basename "$TARGET")"
REMOTE="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "$OWNER" ]]; then
  case "$REMOTE" in
    *github.com:*|*github.com/*)
      OWNER="$(echo "$REMOTE" | sed -E 's#.*github.com[:/]([^/]+)/([^/.]+)(\.git)?$#\1#')" ;;
    *)
      OWNER="$(gh api user --jq .login 2>/dev/null || echo "$USER")" ;;
  esac
fi
if [[ -z "$ACTOR" ]]; then
  ACTOR="$(gh api user --jq .login 2>/dev/null || echo "$OWNER")"
fi
echo "目标: $OWNER/$REPO  |  actor 门控: $ACTOR  |  自动 agent: $([ $GITHUB_AGENT == 1 ] && echo 开 || echo 关)"

# ---------- 3. 渲染模板 ----------
render() { sed -e "s|{{OWNER}}|$OWNER|g" -e "s|{{REPO}}|$REPO|g" -e "s|{{ACTOR}}|$ACTOR|g" "$1"; }

mkdir -p docs/todo .github/workflows .github/scripts

# AGENTS.md：存在则追加工作流章节；不存在则创建
if [[ -f AGENTS.md ]]; then
  printf '\n---\n' >> AGENTS.md
  render "$TPL/AGENTS.workflow.md" >> AGENTS.md
  echo "✔ AGENTS.md 追加工作流宪法章节"
else
  { echo "# $REPO"; echo; render "$TPL/AGENTS.workflow.md"; } > AGENTS.md
  echo "✔ 创建 AGENTS.md（含协调者角色）"
fi

render "$TPL/PROJECT_PHILOSOPHY.md"   > docs/PROJECT_PHILOSOPHY.md
render "$TPL/VERSIONING.md"           > docs/VERSIONING.md
render "$TPL/WORKFLOW_AUTOMATION.md"  > docs/WORKFLOW_AUTOMATION.md
render "$TPL/TODO_INDEX.md"           > docs/todo/TODO_INDEX.md
render "$TPL/ci.yml"                  > .github/workflows/ci.yml
render "$TPL/e2e_release.yml"         > .github/workflows/e2e_release.yml
render "$TPL/check_parallel.py"       > .github/scripts/check_parallel.py
chmod +x .github/scripts/check_parallel.py
echo "✔ docs/ 四份宪法文档 + TODO_INDEX"
echo "✔ ci.yml / e2e_release.yml / check_parallel.py"

if [[ "$GITHUB_AGENT" == 1 ]]; then
  render "$TPL/opencode.yml" > .github/workflows/opencode.yml
  echo "OK opencode.yml (GitHub Actions auto agent, actor=$ACTOR)"
fi

# ---------- 4. GitHub 标签 ----------
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  for L in epic agent behavior-change p:serial domain:api domain:data domain:core domain:frontend domain:infra domain:tests; do
    gh label create "$L" --repo "$OWNER/$REPO" --force >/dev/null 2>&1 || true
  done
  echo "✔ GitHub 标签：epic / agent / behavior-change / p:serial / domain:*"
else
  echo "⚠️  gh 不可用或未登录，跳过标签创建。"
fi

# ---------- 5. 收尾清单 ----------
echo
echo "=============================================================="
echo " 初始化完成。后续操作（协调者/用户）："
echo " 1. 提交并推送：git add -A && git commit -m 'chore: 初始化 lean-workflow' && git push"
echo " 2. 推送后建 test/integration 分支并推送（发布门禁目标分支）"
echo " 3. GitHub 设置：master 保护 ruleset → 必须 PR + Required checks（CI 首跑后）"
echo "    + 添加 e2e-release-verification check（L3 实现后）"
echo " 4. 重开 opencode：协调者身份自动激活（AGENTS.md）"
echo " 5. L2 自动体检机 / L3 真实验收：按 docs/WORKFLOW_AUTOMATION.md 由项目 agent 补全"
echo "=============================================================="
