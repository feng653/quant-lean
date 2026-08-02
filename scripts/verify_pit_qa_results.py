#!/usr/bin/env python3
"""Verify browser-created PIT QA experiments against durable SQLite state."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.research_manifest import canonical_sha256  # noqa: E402


def _one(connection: sqlite3.Connection, sql: str, values: tuple[object, ...]):
    row = connection.execute(sql, values).fetchone()
    if row is None:
        raise RuntimeError(f"Required database row missing: {sql.split()[1]}")
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    if root == (PROJECT_ROOT / "data").resolve():
        raise RuntimeError("Refusing to verify production data as a QA fixture")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    experiments = report.get("experiments")
    if not isinstance(experiments, list) or len(experiments) < 3:
        raise RuntimeError("At least three browser-created experiments are required")
    if not any(item.get("category") == "factor" for item in experiments):
        raise RuntimeError("Representative PIT QA must include a factor strategy")

    database = root / "experiment.db"
    verified: list[dict[str, object]] = []
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        for item in experiments:
            experiment_id = int(item["experiment_id"])
            experiment = _one(
                connection,
                "SELECT * FROM experiments WHERE id=?",
                (experiment_id,),
            )
            if (
                experiment["status"] != "completed"
                or experiment["strategy_id"] != item["strategy_id"]
            ):
                raise RuntimeError(f"Experiment {experiment_id} durable state mismatch")
            manifest_row = _one(
                connection,
                """
                SELECT manifest_json, manifest_hash
                FROM research_run_manifests WHERE experiment_id=?
                """,
                (experiment_id,),
            )
            manifest = json.loads(manifest_row["manifest_json"])
            manifest_hash = canonical_sha256(manifest)
            if manifest_hash != manifest_row["manifest_hash"] or manifest_hash != item[
                "manifest_hash"
            ]:
                raise RuntimeError(f"Experiment {experiment_id} manifest hash mismatch")
            pit_runtime = manifest.get("pit_runtime", {})
            timeline = manifest.get("universe", {}).get("timeline_identity", {})
            binding = manifest.get("execution", {}).get("canonical_price_binding", {})
            qa = pit_runtime.get("qa_runtime_attestation", {})
            if not (
                pit_runtime.get("verified") is True
                and pit_runtime.get("production_eligible") is False
                and qa.get("non_production") is True
                and qa.get("production_eligible") is False
                and timeline.get("timeline_hash") == pit_runtime.get("timeline_hash")
                and timeline.get("source_batches")
                and binding.get("binding_id") == pit_runtime.get(
                    "canonical_price_binding_id"
                )
                and binding.get("binding_digest") == pit_runtime.get(
                    "canonical_price_binding_digest"
                )
            ):
                raise RuntimeError(f"Experiment {experiment_id} PIT manifest is incomplete")
            for source_batch in timeline["source_batches"]:
                _one(
                    connection,
                    """
                    SELECT a.batch_id FROM pit_master_governed_activations a
                    JOIN pit_master_batches b ON b.batch_id=a.batch_id
                    WHERE a.batch_id=? AND b.batch_digest=?
                    """,
                    (source_batch["batch_id"], source_batch["batch_digest"]),
                )
            _one(
                connection,
                """
                SELECT binding_id FROM price_ledger_runtime_bindings
                WHERE binding_id=? AND binding_digest=?
                """,
                (binding["binding_id"], binding["binding_digest"]),
            )
            metrics = _one(
                connection,
                "SELECT * FROM experiment_metrics WHERE experiment_id=?",
                (experiment_id,),
            )
            equity_points = int(
                _one(
                    connection,
                    "SELECT COUNT(*) AS count FROM equity_curve WHERE experiment_id=?",
                    (experiment_id,),
                )["count"]
            )
            trade_count = int(
                _one(
                    connection,
                    "SELECT COUNT(*) AS count FROM trade_log WHERE experiment_id=?",
                    (experiment_id,),
                )["count"]
            )
            if equity_points < 2:
                raise RuntimeError(f"Experiment {experiment_id} lacks detailed equity data")
            verified.append(
                {
                    "experiment_id": experiment_id,
                    "strategy_id": item["strategy_id"],
                    "category": item["category"],
                    "status": experiment["status"],
                    "manifest_hash": manifest_hash,
                    "timeline_hash": timeline["timeline_hash"],
                    "membership_batch_count": len(timeline["source_batches"]),
                    "canonical_price_binding_id": binding["binding_id"],
                    "equity_points": equity_points,
                    "trade_count": trade_count,
                    "cumulative_return": metrics["cumulative_return"],
                    "sharpe_ratio": metrics["sharpe_ratio"],
                    "max_drawdown": metrics["max_drawdown"],
                }
            )
    report["status"] = "verified"
    report["database_verification"] = {
        "database": "experiment.db (isolated QA root)",
        "production_eligible": False,
        "experiment_count": len(verified),
        "experiments": verified,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["database_verification"], ensure_ascii=False))


if __name__ == "__main__":
    main()
