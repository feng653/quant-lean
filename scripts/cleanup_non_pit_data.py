#!/usr/bin/env python3
"""Inventory, dry-run and guarded archival for legacy non-PIT market cache."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    # A maintenance command is intentionally runnable as the documented
    # ``python scripts/...`` form, not only through ``python -m``.
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data.non_pit_cleanup import (
    build_inventory,
    execute_cleanup,
    inspect_cleanup_gate,
    restore_archive,
    write_dry_run_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data", help="project data directory")
    parser.add_argument("--coverage-start", help="explicit PIT coverage start (YYYY-MM-DD)")
    parser.add_argument("--coverage-end", help="explicit PIT coverage end (YYYY-MM-DD)")
    parser.add_argument("--report", help="write dry-run report to this local path")
    parser.add_argument("--execute", action="store_true", help="archive after all hard gates pass")
    parser.add_argument("--maintenance-window-id")
    parser.add_argument("--confirm-inventory-sha256")
    parser.add_argument("--second-confirmation")
    parser.add_argument("--restore-run", help="restore a prior archive run; no PIT gate bypass")
    args = parser.parse_args()

    if args.restore_run:
        result = restore_archive(
            data_root=args.data_root,
            run_id=args.restore_run,
            second_confirmation=args.second_confirmation,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if not args.coverage_start or not args.coverage_end:
        parser.error("--coverage-start and --coverage-end are required for inventory/dry-run")
    inventory = build_inventory(Path(args.data_root))
    gate = asyncio.run(
        inspect_cleanup_gate(
            coverage_start=args.coverage_start,
            coverage_end=args.coverage_end,
        )
    )
    report_path = Path(args.report) if args.report else Path(args.data_root) / "maintenance" / "non_pit_cleanup" / "latest-dry-run.json"
    report = write_dry_run_report(inventory=inventory, gate=gate, output_path=report_path)
    if not args.execute:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if gate.ready else 2
    result = execute_cleanup(
        inventory=inventory,
        gate=gate,
        maintenance_window_id=args.maintenance_window_id,
        inventory_sha256=args.confirm_inventory_sha256,
        second_confirmation=args.second_confirmation,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
