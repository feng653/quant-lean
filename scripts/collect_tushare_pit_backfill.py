#!/usr/bin/env python3
"""Advance one bounded batch of the durable Tushare PIT candidate backfill."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import settings  # noqa: E402
from backend.data.provider_artifacts import (  # noqa: E402
    ContentAddressedProviderArtifactStore,
    ProviderArtifactError,
)
from backend.data.sources.tushare_candidate import (  # noqa: E402
    TushareCandidateClient,
    TushareCandidateError,
)
from backend.data.sources.tushare_pit_backfill import (  # noqa: E402
    DEFAULT_FIRST_MONTH,
    DEFAULT_LAST_COMPLETE_MONTH,
    TusharePitBackfillCollector,
    TusharePitBackfillError,
    TusharePitBackfillPlan,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Advance a resumable Tushare four-index PIT candidate backfill. "
            "Every response remains in quarantine; production data is never changed."
        )
    )
    parser.add_argument("--from-month", default=DEFAULT_FIRST_MONTH)
    parser.add_argument("--to-month", default=DEFAULT_LAST_COMPLETE_MONTH)
    parser.add_argument(
        "--sample-size",
        type=int,
        default=30,
        help=(
            "Legacy-compatible diagnostic sample count shown in reports; "
            "it never limits full-universe collection."
        ),
    )
    parser.add_argument(
        "--event-sample-size",
        type=int,
        default=10,
        help=(
            "Legacy checkpoint identity parameter; events are collected for every "
            "historical constituent."
        ),
    )
    parser.add_argument(
        "--market-chunk-months",
        type=int,
        default=12,
        help=(
            "Legacy checkpoint identity parameter; v3 market evidence is planned "
            "as canonical per-session cross-sections."
        ),
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=16,
        help="Provider-call budget for this invocation (1..128).",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "pit_evidence"
            / "provider_candidates"
            / "tushare_backfill"
        ),
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    store = ContentAddressedProviderArtifactStore(args.evidence_root)
    client = TushareCandidateClient(
        token=settings.TUSHARE_TOKEN.get_secret_value(),
        store=store,
        proxy_url=settings.PIT_CANDIDATE_OUTBOUND_PROXY_URL.get_secret_value(),
    )
    plan = TusharePitBackfillPlan(
        first_month=args.from_month,
        last_month=args.to_month,
        sample_size=args.sample_size,
        event_sample_size=args.event_sample_size,
        market_chunk_months=args.market_chunk_months,
    )
    return await TusharePitBackfillCollector(
        client=client,
        plan=plan,
        max_calls=args.max_calls,
    ).run()


def main() -> int:
    args = _parser().parse_args()
    try:
        report = asyncio.run(_run(args))
    except (TushareCandidateError, TusharePitBackfillError, ProviderArtifactError) as exc:
        diagnostic = (
            exc.diagnostic()
            if isinstance(exc, TushareCandidateError)
            else {"code": type(exc).__name__, "retryable": False}
        )
        print(
            json.dumps(
                {"status": "failed", "diagnostic": diagnostic},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    progress = report["progress"]
    print(
        json.dumps(
            {
                "status": "completed" if progress["complete"] else "checkpointed",
                "classification": report["classification"],
                "run_id": report["run_id"],
                "calls_this_invocation": progress["calls_this_invocation"],
                "completed_tasks": progress["completed_tasks"],
                "planned_tasks": progress["planned_tasks"],
                "pending_tasks": progress["pending_tasks"],
                "production_pit_ready": report["production_pit_ready"],
                "runtime_data_changed": report["runtime_data_changed"],
                "report_sha256": report["report_sha256"],
                "stored_report_sha256": report["stored_report_sha256"],
                "failures": report["failures"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
