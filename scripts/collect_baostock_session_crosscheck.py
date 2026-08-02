#!/usr/bin/env python3
"""Collect one bounded BaoStock session-state cross-check batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data.provider_artifacts import (  # noqa: E402
    ContentAddressedProviderArtifactStore,
    ProviderArtifactError,
)
from backend.data.sources.baostock_session_crosscheck import (  # noqa: E402
    BaoStockCrosscheckError,
    BaoStockCrosscheckPlan,
    BaoStockSessionCrosscheckCollector,
    discard_baostock_sdk_output,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect optional BaoStock evidence for explicitly listed Tushare "
            "session blockers. Output remains quarantine-only."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="JSON file containing explicit code/date/Tushare-reason pairs",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "pit_evidence"
            / "provider_candidates"
            / "baostock_session_crosscheck"
        ),
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=8,
        help="Strict provider-call budget for this invocation (1..64)",
    )
    return parser


def _read_document(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaoStockCrosscheckError(
            "cross-check input is unreadable",
            diagnostic_code="crosscheck_input_invalid",
        ) from exc
    if not isinstance(document, dict):
        raise BaoStockCrosscheckError(
            "cross-check input must be an object",
            diagnostic_code="crosscheck_input_invalid",
        )
    return document


def main() -> int:
    args = _parser().parse_args()
    try:
        # Import lazily so routine backend startup has no BaoStock dependency or login.
        with discard_baostock_sdk_output():
            import baostock as bs

        plan = BaoStockCrosscheckPlan.from_document(_read_document(args.input))
        report = BaoStockSessionCrosscheckCollector(
            sdk=bs,
            store=ContentAddressedProviderArtifactStore(args.evidence_root),
            plan=plan,
            max_calls=args.max_calls,
        ).run()
    except (BaoStockCrosscheckError, ProviderArtifactError, ImportError) as exc:
        diagnostic = (
            exc.diagnostic()
            if isinstance(exc, BaoStockCrosscheckError)
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
                "completed_pairs": progress["completed_pairs"],
                "pending_pairs": progress["pending_pairs"],
                "production_pit_ready": report["production_pit_ready"],
                "runtime_data_changed": report["runtime_data_changed"],
                "report_sha256": report["report_sha256"],
                "stored_report_sha256": report["stored_report_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
