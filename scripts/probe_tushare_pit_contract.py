#!/usr/bin/env python3
"""Collect a bounded four-index PIT contract report into quarantine."""

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
)
from backend.data.sources.tushare_candidate import (  # noqa: E402
    TushareCandidateClient,
    TushareCandidateError,
)
from backend.data.sources.tushare_contract_probe import (  # noqa: E402
    default_contract_probe_months,
    run_tushare_pit_contract_probe,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe sparse Tushare PIT contracts for four CSI indexes and 30 "
            "securities; persist quarantine artifacts only."
        )
    )
    parser.add_argument(
        "--month",
        action="append",
        dest="months",
        help="Sparse complete month in YYYY-MM form (repeat up to six times).",
    )
    parser.add_argument("--sample-size", type=int, default=30)
    parser.add_argument("--event-security-count", type=int, default=5)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "pit_evidence"
            / "provider_candidates"
            / "tushare"
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
    return await run_tushare_pit_contract_probe(
        client,
        probe_months=args.months or default_contract_probe_months(),
        sample_size=args.sample_size,
        event_security_count=args.event_security_count,
    )


def main() -> int:
    args = _parser().parse_args()
    try:
        report = asyncio.run(_run(args))
    except TushareCandidateError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "diagnostic": exc.diagnostic(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    summary = {
        "status": "completed",
        "classification": report["classification"],
        "candidate_collection_valid": report["candidate_collection_valid"],
        "production_pit_ready": report["production_pit_ready"],
        "report_sha256": report["report_sha256"],
        "stored_report_sha256": report["stored_report_sha256"],
        "index_availability": report["index_availability"],
        "security_sample": {
            "market_date": report["security_sample"]["market_date"],
            "sample_size": report["security_sample"]["sample_size"],
        },
        "promotion": report["promotion"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if report["candidate_collection_valid"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
