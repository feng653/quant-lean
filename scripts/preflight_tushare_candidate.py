#!/usr/bin/env python3
"""Run a bounded Tushare candidate probe and persist quarantine evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data.provider_artifacts import (  # noqa: E402
    ContentAddressedProviderArtifactStore,
)
from backend.data.sources.tushare_candidate import (  # noqa: E402
    TushareCandidateClient,
    TushareCandidateError,
    collect_governed_csindex_current_anchor,
    run_standard_preflight,
)
from backend.config import settings  # noqa: E402


def _token() -> str:
    return settings.TUSHARE_TOKEN.get_secret_value()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect a small Tushare sample into quarantine and optionally "
            "cross-check daily prices with BaoStock, then AKShare/Sina."
        )
    )
    parser.add_argument("--ts-code", default="000001.SZ")
    parser.add_argument("--start", default="2025-01-02")
    parser.add_argument("--end", default="2025-01-10")
    parser.add_argument("--index-code", default="000300.SH")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "pit_evidence" / "provider_candidates" / "tushare",
    )
    parser.add_argument("--no-cross-check", action="store_true")
    parser.add_argument(
        "--official-csindex-current-anchor",
        action="store_true",
        help=(
            "Opt in to fetch exactly one matching CSI current anchor, record "
            "it in the existing governance store, and compare only when its "
            "observation date exactly matches the vendor weight date."
        ),
    )
    parser.add_argument(
        "--official-actor-user-id",
        type=int,
        help="Required audit actor when official anchor collection is enabled.",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    store = ContentAddressedProviderArtifactStore(args.evidence_root)
    client = TushareCandidateClient(
        token=_token(),
        store=store,
        proxy_url=settings.PIT_CANDIDATE_OUTBOUND_PROXY_URL.get_secret_value(),
    )
    official_evidence = None
    if args.official_csindex_current_anchor:
        scope_by_code = {
            "000300": "csi300",
            "000905": "csi500",
            "000852": "csi1000",
        }
        scope_id = scope_by_code.get(str(args.index_code).split(".", maxsplit=1)[0])
        if scope_id is None:
            raise TushareCandidateError(
                "official current-anchor comparison supports CSI300/500/1000"
            )
        official_evidence = await collect_governed_csindex_current_anchor(
            scope_id=scope_id,
            actor_user_id=args.official_actor_user_id,
        )
    return await run_standard_preflight(
        client,
        ts_code=args.ts_code,
        start=args.start,
        end=args.end,
        index_code=args.index_code,
        cross_check=not args.no_cross_check,
        official_index_evidence=official_evidence,
    )


def main() -> int:
    args = _parser().parse_args()
    if (
        args.official_csindex_current_anchor
        and args.official_actor_user_id is None
    ):
        _parser().error(
            "--official-actor-user-id is required with "
            "--official-csindex-current-anchor"
        )
    try:
        report = asyncio.run(_run(args))
    except TushareCandidateError as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("candidate_collection_valid") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
