#!/usr/bin/env python3
"""Collect official CSI constituent history into a pending governance package."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data.pit_evidence_governance import (  # noqa: E402
    PitEvidenceGovernance,
)
from backend.data.point_in_time_master import PointInTimeMasterStore  # noqa: E402
from backend.data.sources.csindex_history import (  # noqa: E402
    CsindexHistoryWorkflow,
)
from backend.data.sources.csindex_pit import CsindexOfficialCollector  # noqa: E402


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Traverse the complete unfiltered CSI announcement archive, retain "
            "content-addressed evidence, emit a review queue, and stage only "
            "the history window that can be proven. This command never "
            "approves or imports a package."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Checkpoint and machine-readable report directory.",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        required=True,
        help="Managed content-addressed evidence root.",
    )
    parser.add_argument(
        "--governance-db",
        type=Path,
        required=True,
        help="Governance SQLite journal path.",
    )
    parser.add_argument(
        "--master-db",
        type=Path,
        required=True,
        help=(
            "PIT master SQLite path reserved for a later approved import. "
            "This collector does not import into it."
        ),
    )
    parser.add_argument(
        "--from",
        dest="requested_from",
        type=_date,
        default=date(2015, 1, 1),
        help="Requested history start (default: 2015-01-01).",
    )
    parser.add_argument(
        "--actor-user-id",
        type=int,
        required=True,
        help="Existing administrative actor ID recorded in the audit journal.",
    )
    parser.add_argument(
        "--review-decisions",
        type=Path,
        help=(
            "Optional independent review document bound to the emitted archive "
            "and proposal hashes; its reviewer must first register the exact "
            "file through the authenticated auxiliary-artifact API."
        ),
    )
    parser.add_argument(
        "--trading-calendar",
        type=Path,
        help=(
            "Optional signed authoritative-trading-calendar/v2 JSON evidence; "
            "the signing key must be in PIT_CALENDAR_TRUSTED_KEYS_JSON."
        ),
    )
    parser.add_argument(
        "--rows-per-page",
        type=int,
        default=100,
        choices=range(1, 501),
    )
    parser.add_argument(
        "--minimum-interval-seconds",
        type=float,
        default=0.25,
        help="Minimum delay between official requests (default: 0.25).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=4,
        choices=range(1, 11),
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=45.0,
        help="Per-request official-source timeout (default: 45).",
    )
    parser.add_argument(
        "--no-current-anchor-package",
        action="store_true",
        help=(
            "Do not stage a current-anchor-only pending package when historical "
            "review or calendar evidence is incomplete."
        ),
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    master = PointInTimeMasterStore(args.master_db)
    governance = PitEvidenceGovernance(
        root=args.evidence_root,
        database_path=args.governance_db,
        master_store=master,
    )
    workflow = CsindexHistoryWorkflow(
        workspace=args.workspace,
        governance=governance,
        actor_user_id=args.actor_user_id,
        collector=CsindexOfficialCollector(
            timeout_seconds=args.request_timeout_seconds
        ),
        rows_per_page=args.rows_per_page,
        minimum_interval_seconds=args.minimum_interval_seconds,
        max_attempts=args.max_attempts,
    )
    result = await workflow.run(
        requested_from=args.requested_from,
        review_decisions_path=args.review_decisions,
        trading_calendar_path=args.trading_calendar,
        stage_current_anchor_if_blocked=not args.no_current_anchor_package,
    )
    return {
        "status": "pending_review",
        "package_id": result.package_id,
        "proven_coverage_from": result.coverage_from.isoformat(),
        "proven_coverage_to": result.coverage_to.isoformat(),
        "checkpoint": str(result.checkpoint_path),
        "review_queue": str(result.review_queue_path),
        "coverage_report": str(result.coverage_report_path),
        "automatic_approval_permitted": False,
        "production_import_performed": False,
    }


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
