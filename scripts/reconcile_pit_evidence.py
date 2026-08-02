#!/usr/bin/env python3
"""Reconcile normalised official and Tushare quarantine evidence offline."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data.pit_evidence_reconciliation import (  # noqa: E402
    reconcile_pit_evidence,
    verify_reconciliation_report,
)


MAX_INPUT_BYTES = 32 * 1024 * 1024


def _document(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("input must be a regular non-symlink file")
        if metadata.st_size <= 0 or metadata.st_size > MAX_INPUT_BYTES:
            raise ValueError("input size is outside the safe limit")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value is forbidden: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"reconciliation input is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("reconciliation input must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline fail-closed comparison of 20 corporate actions, four-index "
            "member events, and bitemporal evidence"
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="normalised quarantine reconciliation input JSON",
    )
    args = parser.parse_args()

    report = reconcile_pit_evidence(_document(args.input))
    verify_reconciliation_report(report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["reconciliation_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
