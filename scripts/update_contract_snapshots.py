#!/usr/bin/env python3
"""重新生成契约快照 golden（v0.3.0 逃生口）。

仅允许在"有意变更端点/响应结构"的 PR 使用：
    python scripts/update_contract_snapshots.py

行为不变的重构禁止运行本脚本；重构后应直接跑 CI，快照必须保持零 diff。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    print("重新生成契约快照 golden（--update-snapshots）…")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/test_contract_lock.py",
            "-q",
            "--tb=short",
            "--update-snapshots",
            "--timeout=300",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        print("快照重新生成失败", file=sys.stderr)
        return result.returncode
    print("完成。请人工核对 backend/tests/snapshots/ 的 diff，并在 PR 描述中附变更清单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
