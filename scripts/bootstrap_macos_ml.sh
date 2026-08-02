#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This bootstrap is only for macOS." >&2
  exit 2
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "An arm64 shell is required. Do not bootstrap through Rosetta." >&2
  exit 2
fi

brew_bin="${HOMEBREW_BIN:-/opt/homebrew/bin/brew}"
if [[ ! -x "$brew_bin" ]]; then
  brew_bin="$(command -v brew || true)"
fi
if [[ -z "$brew_bin" || ! -x "$brew_bin" ]]; then
  echo "Homebrew is required. Expected /opt/homebrew/bin/brew." >&2
  exit 2
fi
"$brew_bin" list libomp >/dev/null 2>&1 || "$brew_bin" install libomp

if command -v uv >/dev/null 2>&1; then
  if [[ ! -x .venv/bin/python ]]; then
    uv venv --python 3.11 .venv
  fi
  if [[ "$(.venv/bin/python -c 'import platform; print(platform.machine())')" != "arm64" ]]; then
    echo "Existing .venv is not arm64; move it aside and rerun." >&2
    exit 2
  fi
  uv pip install \
    --python .venv/bin/python \
    --requirement requirements-macos-arm64.txt
else
  if [[ ! -x .venv/bin/python ]]; then
    python3.11 -m venv .venv
  fi
  if [[ "$(.venv/bin/python -c 'import platform; print(platform.machine())')" != "arm64" ]]; then
    echo "Existing .venv is not arm64; move it aside and rerun." >&2
    exit 2
  fi
  .venv/bin/python -m pip install \
    --requirement requirements-macos-arm64.txt
fi

.venv/bin/python scripts/ml_runtime_doctor.py --fit-smoke
