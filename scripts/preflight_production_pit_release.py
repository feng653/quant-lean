#!/usr/bin/env python3
"""Dry-run or authorise a complete approved production PIT release bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data.production_pit_release import (  # noqa: E402
    ApprovedProviderArtifactStore,
    AtomicPitReleaseRegistry,
    ProductionPitReleaseOrchestrator,
    ProductionPitReleasePolicy,
    ReleaseActivationBlocked,
)


def _object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate approved immutable artifacts without writing runtime PIT data"
    )
    parser.add_argument("--approved-root", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--trusted-keys", required=True, type=Path)
    parser.add_argument("--coverage-from", default="2016-01-01")
    parser.add_argument("--coverage-to", required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--actor-user-id", type=int)
    args = parser.parse_args()

    trusted_keys = _object(args.trusted_keys, "trusted approval keys")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in trusted_keys.items()):
        raise SystemExit("trusted approval keys must map key ids to base64 public keys")
    bundle = _object(args.bundle, "release bundle")
    orchestrator = ProductionPitReleaseOrchestrator(
        ApprovedProviderArtifactStore(
            args.approved_root,
            trusted_approval_keys=trusted_keys,  # type: ignore[arg-type]
        ),
        policy=ProductionPitReleasePolicy(
            coverage_from=args.coverage_from,
            coverage_to=args.coverage_to,
        ),
    )
    report = orchestrator.dry_run(bundle)
    result: dict[str, object] = {"dry_run": report}
    activation_requested = any(
        value is not None
        for value in (args.registry, args.confirm_plan_sha256, args.actor_user_id)
    )
    if activation_requested:
        if (
            args.registry is None
            or args.confirm_plan_sha256 is None
            or args.actor_user_id is None
        ):
            raise SystemExit(
                "--registry, --confirm-plan-sha256 and --actor-user-id are all required"
            )
        try:
            result["authorization"] = orchestrator.activate(
                bundle,
                confirmation_plan_sha256=args.confirm_plan_sha256,
                registry=AtomicPitReleaseRegistry(args.registry),
                actor_user_id=args.actor_user_id,
            )
        except ReleaseActivationBlocked as exc:
            result["authorization"] = {
                "authorised": False,
                "error": str(exc),
            }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
