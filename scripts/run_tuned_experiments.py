"""Create and verify one out-of-sample backend experiment per strategy.

The script deliberately enters through the same ``create_experiment`` handler
used by the frontend, then executes the queued job with the production
experiment runner.  It reads only the local CSI 500 cache and tries validation
candidates in score order until a usable out-of-sample result is produced.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.api.experiments import CreateExperimentBody, create_experiment  # noqa: E402
from backend.api.trading import CreateDeploymentBody, create_deployment  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.data.cache import DataCache, has_price_field  # noqa: E402
from backend.main import _init_databases, _run_experiment, _scan_strategies  # noqa: E402
from backend.services.model_artifacts import verify_model_file  # noqa: E402
from backend.strategies.registry import get_registry  # noqa: E402

TRAIN_START = "2019-01-02"
TRAIN_END = "2024-12-31"
TEST_START = "2025-01-02"
TEST_END = "2026-07-24"
TUNING_REPORT = PROJECT_ROOT / "data/tuning/csi500_429_tuning.json"
FINAL_REPORT = PROJECT_ROOT / "data/tuning/csi500_429_final_experiments.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _db_connect(kind: str = "experiment") -> sqlite3.Connection:
    databases = {
        "experiment": settings.EXPERIMENT_DB,
        "users": settings.USERS_DB,
        "trading_sim": settings.TRADING_SIM_DB,
    }
    relative = databases[kind]
    connection = sqlite3.connect(str(settings.abs_path(relative)))
    connection.row_factory = sqlite3.Row
    return connection


def _resolve_user() -> dict[str, Any]:
    with _db_connect("users") as connection:
        row = connection.execute(
            """
            SELECT id, username, is_admin
            FROM users
            WHERE COALESCE(is_active, 1) = 1
            ORDER BY is_admin DESC, id ASC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("No active local user is available for experiment ownership")
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "is_admin": bool(row["is_admin"]),
    }


def _ranked_candidates(strategy_report: dict[str, Any]) -> list[dict[str, Any]]:
    successful = [
        item
        for item in strategy_report.get("candidates", [])
        if item.get("status") == "completed"
        and isinstance(item.get("score"), (int, float))
        and math.isfinite(float(item["score"]))
    ]
    return sorted(successful, key=lambda item: float(item["score"]), reverse=True)


def _find_existing(name: str) -> tuple[int, str] | None:
    with _db_connect() as connection:
        row = connection.execute(
            """
            SELECT e.id, e.status, j.job_uuid
            FROM experiments e
            LEFT JOIN jobs j
              ON json_extract(j.params, '$.experiment_id') = e.id
            WHERE e.name = ?
            ORDER BY e.id DESC, j.id DESC
            LIMIT 1
            """,
            (name,),
        ).fetchone()
    if row is None:
        return None
    return int(row["id"]), str(row["job_uuid"] or f"exp-{row['id']}")


def _read_result(experiment_id: int) -> dict[str, Any]:
    with _db_connect() as connection:
        experiment = connection.execute(
            "SELECT * FROM experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
        metrics = connection.execute(
            "SELECT * FROM experiment_metrics WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        equity_count = connection.execute(
            "SELECT COUNT(*) AS count FROM equity_curve WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()["count"]
        trade_count = connection.execute(
            "SELECT COUNT(*) AS count FROM trade_log WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()["count"]
        artifact = connection.execute(
            """
            SELECT * FROM model_artifacts
            WHERE experiment_id = ?
            ORDER BY model_version DESC
            LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
    if experiment is None:
        raise RuntimeError(f"Experiment {experiment_id} disappeared")
    return {
        "experiment": dict(experiment),
        "metrics": dict(metrics) if metrics is not None else {},
        "equity_points": int(equity_count),
        "trades": int(trade_count),
        "artifact": dict(artifact) if artifact is not None else None,
    }


def _validate_trade_contract(experiment_id: int, pivot: pd.DataFrame) -> int:
    trading_days = pivot.loc[TEST_START:TEST_END].index
    previous_day = {
        trading_days[index].strftime("%Y-%m-%d"):
        trading_days[index - 1].strftime("%Y-%m-%d")
        for index in range(1, len(trading_days))
    }
    with _db_connect() as connection:
        trades = connection.execute(
            """
            SELECT date, signal_date, code, price
            FROM trade_log
            WHERE experiment_id = ?
            ORDER BY id
            """,
            (experiment_id,),
        ).fetchall()
    for trade in trades:
        trade_date = str(trade["date"])
        signal_date = str(trade["signal_date"])
        if previous_day.get(trade_date) != signal_date:
            raise AssertionError(
                f"Experiment {experiment_id}: {signal_date=} is not the "
                f"previous trading day of {trade_date=}"
            )
        key = (str(trade["code"]), "open")
        if key not in pivot.columns:
            raise AssertionError(f"Missing local open price for {key}")
        open_price = pivot.at[pd.Timestamp(trade_date), key]
        if not math.isfinite(float(open_price)) or not math.isclose(
            float(trade["price"]),
            float(open_price),
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise AssertionError(
                f"Experiment {experiment_id}: fill {trade['price']} does not "
                f"match local open {open_price} for {key[0]} on {trade_date}"
            )
    return len(trades)


def _validate_result(
    strategy_id: str,
    experiment_id: int,
    pivot: pd.DataFrame,
) -> dict[str, Any]:
    result = _read_result(experiment_id)
    experiment = result["experiment"]
    if experiment["status"] != "completed":
        raise RuntimeError(
            f"Experiment {experiment_id} ended as {experiment['status']}: "
            f"{experiment['error_log']}"
        )
    if result["equity_points"] <= 0 or result["trades"] <= 0:
        raise RuntimeError(
            f"Experiment {experiment_id} has no usable equity/trades"
        )
    metrics = result["metrics"]
    for field in ("sharpe_ratio", "annual_return", "max_drawdown"):
        value = metrics.get(field)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise RuntimeError(f"Experiment {experiment_id} has invalid {field}")

    metadata = get_registry().get_metadata(strategy_id)
    if metadata.requires_training:
        artifact = result["artifact"]
        if artifact is None:
            raise RuntimeError(f"Experiment {experiment_id} has no model artifact")
        model_path = Path(str(artifact["model_file_path"]))
        metadata_path = Path(str(artifact["metadata_file_path"]))
        if not model_path.is_file() or not metadata_path.is_file():
            raise RuntimeError(f"Experiment {experiment_id} artifact files are missing")
        asyncio.run(
            verify_model_file(
                model_path,
                artifact.get("artifact_sha256"),
                artifact.get("artifact_size"),
            )
        )

    checked_trades = _validate_trade_contract(experiment_id, pivot)
    return {
        "experiment_id": experiment_id,
        "status": experiment["status"],
        "equity_points": result["equity_points"],
        "trades": checked_trades,
        "metrics": {
            "sharpe_ratio": metrics["sharpe_ratio"],
            "annual_return": metrics["annual_return"],
            "max_drawdown": metrics["max_drawdown"],
            "win_rate": metrics.get("win_rate"),
        },
        "data_version": experiment["data_version"],
        "model_artifact": result["artifact"],
        "execution_verified": "T signal / T+1 local open fill",
        "frontend_path": f"/experiment/{experiment_id}",
    }


async def _create_and_run(
    strategy_id: str,
    params: dict[str, Any],
    candidate_id: str,
    rank: int,
    user: dict[str, Any],
) -> tuple[int, str]:
    name = f"CSI500-429 OOS 2025-2026 | {strategy_id} | rank-{rank} | {candidate_id}"
    existing = _find_existing(name)
    if existing is None:
        metadata = get_registry().get_metadata(strategy_id)
        body = CreateExperimentBody(
            name=name,
            strategy_id=strategy_id,
            pool_preset="csi500",
            train_start=TRAIN_START if metadata.requires_training else None,
            train_end=TRAIN_END if metadata.requires_training else None,
            test_start=TEST_START,
            test_end=TEST_END,
            params=params,
            mode="batch",
        )
        response = await create_experiment(body=body, user=user)
        experiment_id = int(response["data"]["experiment_id"])
        job_id = str(response["data"]["job_id"])
    else:
        experiment_id, job_id = existing

    print(
        f"[{strategy_id}] running experiment={experiment_id} "
        f"candidate={candidate_id} rank={rank}",
        flush=True,
    )
    await _run_experiment(experiment_id, job_id)
    return experiment_id, job_id


async def _deploy_verified_experiment(
    strategy_id: str,
    experiment_id: int,
    params: dict[str, Any],
    user: dict[str, Any],
) -> dict[str, Any]:
    with _db_connect("trading_sim") as connection:
        existing = connection.execute(
            """
            SELECT * FROM deployments
            WHERE source_experiment_id = ? AND strategy_id = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (experiment_id, strategy_id, user["id"]),
        ).fetchone()
    if existing is None:
        response = await create_deployment(
            body=CreateDeploymentBody(
                strategy_id=strategy_id,
                display_name=f"CSI500-429 tuned | {strategy_id}",
                source_experiment_id=experiment_id,
                params=params,
                mode="batch",
                retrain_frequency="never",
                status="active",
            ),
            user=user,
        )
        deployment_id = int(response["data"]["deployment_id"])
    else:
        deployment_id = int(existing["id"])

    with _db_connect("trading_sim") as connection:
        row = connection.execute(
            "SELECT * FROM deployments WHERE id = ?",
            (deployment_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"Deployment {deployment_id} disappeared")
    deployment = dict(row)
    if (
        deployment["status"] != "active"
        or int(deployment["source_experiment_id"]) != experiment_id
        or deployment["strategy_id"] != strategy_id
    ):
        raise RuntimeError(f"Deployment {deployment_id} failed source validation")
    metadata = get_registry().get_metadata(strategy_id)
    if metadata.requires_training and not deployment["source_model_artifact_id"]:
        raise RuntimeError(f"Deployment {deployment_id} did not pin a model artifact")
    return {
        "deployment_id": deployment_id,
        "status": deployment["status"],
        "source_experiment_id": experiment_id,
        "source_model_artifact_id": deployment["source_model_artifact_id"],
        "frontend_path": "/trading/portfolio",
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", nargs="*")
    parser.add_argument("--tuning-report", type=Path, default=TUNING_REPORT)
    parser.add_argument("--report", type=Path, default=FINAL_REPORT)
    args = parser.parse_args()

    await _init_databases()
    await _scan_strategies()
    tuning = _load_json(args.tuning_report)
    registry_ids = sorted(item.strategy_id for item in get_registry().list_all())
    selected = args.strategies or [
        strategy_id
        for strategy_id in tuning.get("strategies", {})
        if strategy_id in registry_ids
    ]
    if args.strategies is None and set(selected) != set(registry_ids):
        missing = sorted(set(registry_ids) - set(selected))
        raise RuntimeError(f"Tuning report is incomplete; missing {missing}")
    unknown = sorted(set(selected) - set(registry_ids))
    if unknown:
        raise ValueError(f"Unknown strategies: {unknown}")

    pivot = await DataCache().load_pivot("csi500")
    if pivot is None or pivot.empty or not has_price_field(pivot, "open"):
        raise RuntimeError("Validated local CSI 500 OHLCV cache is required")
    member_codes = sorted(set(pivot.columns.get_level_values(0)))
    if len(member_codes) != 429:
        raise RuntimeError(f"Expected 429 local members, found {len(member_codes)}")
    if pivot.index.min() > pd.Timestamp(TRAIN_START) or pivot.index.max() < pd.Timestamp(TEST_END):
        raise RuntimeError("Local CSI 500 cache does not cover the experiment window")

    user = _resolve_user()
    if args.report.exists():
        final_report = _load_json(args.report)
    else:
        final_report = {
            "protocol": {
                "pool_id": "csi500",
                "pool_members": 429,
                "data_source": "local cache only",
                "train_start": TRAIN_START,
                "train_end": TRAIN_END,
                "test_start": TEST_START,
                "test_end": TEST_END,
                "execution": "T signal / T+1 open fill / T+1 close valuation",
            },
            "strategies": {},
        }

    for strategy_id in selected:
        existing_result = final_report["strategies"].get(strategy_id)
        if (
            existing_result
            and existing_result.get("status") == "completed"
            and existing_result.get("deployment", {}).get("status") == "active"
        ):
            print(f"[{strategy_id}] already verified", flush=True)
            continue
        strategy_tuning = tuning.get("strategies", {}).get(strategy_id)
        if strategy_tuning is None:
            raise RuntimeError(f"Tuning report is missing {strategy_id}")
        candidates = _ranked_candidates(strategy_tuning)
        if not candidates:
            raise RuntimeError(f"Tuning has no successful candidate for {strategy_id}")

        failures: list[dict[str, Any]] = []
        for rank, candidate in enumerate(candidates, 1):
            candidate_id = str(candidate["candidate_id"])
            try:
                experiment_id, job_id = await _create_and_run(
                    strategy_id,
                    dict(candidate["params"]),
                    candidate_id,
                    rank,
                    user,
                )
                verified = _validate_result(strategy_id, experiment_id, pivot)
                deployment = await _deploy_verified_experiment(
                    strategy_id,
                    experiment_id,
                    dict(candidate["params"]),
                    user,
                )
                final_report["strategies"][strategy_id] = {
                    **verified,
                    "deployment": deployment,
                    "job_id": job_id,
                    "candidate_id": candidate_id,
                    "validation_rank": rank,
                    "params": candidate["params"],
                    "validation_metrics": candidate["metrics"],
                }
                _save_json(args.report, final_report)
                print(
                    f"[{strategy_id}] VERIFIED experiment={experiment_id} "
                    f"trades={verified['trades']}",
                    flush=True,
                )
                break
            except Exception as exc:
                failure = {
                    "candidate_id": candidate_id,
                    "rank": rank,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failures.append(failure)
                final_report["strategies"][strategy_id] = {
                    "status": "retrying",
                    "failures": failures,
                }
                _save_json(args.report, final_report)
                print(f"[{strategy_id}] FAILED {failure}", flush=True)
        else:
            final_report["strategies"][strategy_id] = {
                "status": "failed",
                "failures": failures,
            }
            _save_json(args.report, final_report)
            raise RuntimeError(f"All tuned candidates failed for {strategy_id}")

    print(f"report={args.report}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
