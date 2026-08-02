"""Run a validated remote-training task on a trusted Windows workstation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from remote_worker import (  # noqa: E402
    RemoteTrainingHTTPClient,
    RemoteTrainingRunner,
    doctor_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train one signed-by-hash remote ML task locally.",
    )
    parser.add_argument("--server", help="Remote platform base URL")
    parser.add_argument("--task-id", help="Canonical remote task UUID")
    parser.add_argument(
        "--token",
        help=(
            "Training token. QUANT_REMOTE_TRAINING_TOKEN takes precedence "
            "and avoids exposing the token in process listings."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="remote-training-output",
        help="Local root for atomically published model bundles",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto"],
        help="Strategies select CUDA or CPU; currently only auto is supported",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate manifest, source and dataset without training or upload",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print dependency and CUDA diagnostics without contacting a server",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.doctor:
        print(json.dumps(doctor_report(), ensure_ascii=False, indent=2))
        return 0

    if not args.server or not args.task_id:
        raise SystemExit("--server and --task-id are required")
    token = os.environ.get("QUANT_REMOTE_TRAINING_TOKEN") or args.token
    if not token:
        if not sys.stdin.isatty():
            raise SystemExit(
                "set QUANT_REMOTE_TRAINING_TOKEN for non-interactive use"
            )
        token = getpass.getpass("One-time remote training token: ").strip()
    if not token:
        raise SystemExit("remote training token is required")

    with RemoteTrainingHTTPClient(
        args.server,
        args.task_id,
        token,
    ) as transport:
        runner = RemoteTrainingRunner(
            transport,
            args.output_dir,
            device=args.device,
            project_root=PROJECT_ROOT,
        )
        result = runner.run(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
