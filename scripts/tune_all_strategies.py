"""Reproducible full-pool parameter search for all ten registered strategies.

Every candidate uses the project's complete 429-member CSI 500 snapshot, the
same cost model, and the same T-signal/T+1-open backtest engine.  Results are
checkpointed after every candidate so a long ML search can resume safely.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.cost_model import CostModel  # noqa: E402
from backend.core.engine import BacktestEngine  # noqa: E402
from backend.core.metrics import compute_all_metrics  # noqa: E402
from backend.data.cache import DataCache, has_price_field  # noqa: E402
from backend.strategies.registry import StrategyRegistry  # noqa: E402

VALIDATION_START = "2024-01-02"
VALIDATION_END = "2024-12-31"
TRAIN_START = "2019-01-02"
TRAIN_END = "2023-12-29"


def _product(base: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(base)
    return [
        dict(zip(keys, values))
        for values in itertools.product(*(base[key] for key in keys))
    ]


def candidate_spaces() -> dict[str, list[dict[str, Any]]]:
    return {
        "ma_cross_v1": _product(
            {
                "fast_period": [5, 10, 20],
                "slow_period": [30, 60],
                "min_score": [0.0, 0.3],
            }
        ),
        "rsi_reversal_v1": [
            {
                "period": period,
                "oversold": oversold,
                "overbought": overbought,
                "min_score": min_score,
            }
            for period in (7, 14)
            for oversold, overbought in ((25, 75), (30, 70), (35, 65))
            for min_score in (0.0, 0.2)
        ],
        "macd_signal_v1": [
            {
                "fast": fast,
                "slow": slow,
                "signal": signal,
                "min_score": min_score,
            }
            for fast, slow, signal in (
                (8, 21, 5),
                (12, 26, 9),
                (16, 32, 9),
                (10, 30, 12),
            )
            for min_score in (0.0, 0.2)
        ],
        "bollinger_breakout_v1": _product(
            {
                "period": [20, 40, 60],
                "std_multiplier": [1.5, 2.0],
                "min_score": [0.0, 0.2],
            }
        ),
        "risk_parity_v1": _product(
            {
                "lookback": [42, 63, 126, 252],
                "rebalance_frequency": ["weekly", "monthly"],
                "min_score": [0.0],
            }
        ),
        "alphamaster_gbr_v1": [
            {
                "n_estimators": 100,
                "max_depth": 2,
                "learning_rate": 0.05,
                "top_k": 20,
                "rebalance_days": 42,
                "min_train_months": 24,
            },
            {
                "n_estimators": 150,
                "max_depth": 3,
                "learning_rate": 0.03,
                "top_k": 30,
                "rebalance_days": 21,
                "min_train_months": 24,
            },
            {
                "n_estimators": 200,
                "max_depth": 4,
                "learning_rate": 0.03,
                "top_k": 40,
                "rebalance_days": 60,
                "min_train_months": 24,
            },
        ],
        "alpha158_lgb_v1": [
            {
                "n_estimators": 100,
                "max_depth": 3,
                "learning_rate": 0.05,
                "top_k_pct": 0.05,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "n_estimators": 200,
                "max_depth": 5,
                "learning_rate": 0.03,
                "top_k_pct": 0.10,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "n_estimators": 300,
                "max_depth": 3,
                "learning_rate": 0.03,
                "top_k_pct": 0.10,
                "retrain_months": 6,
                "min_train_months": 24,
            },
        ],
        "alpha158_xgb_v1": [
            {
                "n_estimators": 100,
                "max_depth": 3,
                "learning_rate": 0.05,
                "top_k_pct": 0.05,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "n_estimators": 200,
                "max_depth": 5,
                "learning_rate": 0.03,
                "top_k_pct": 0.10,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "n_estimators": 300,
                "max_depth": 3,
                "learning_rate": 0.03,
                "top_k_pct": 0.10,
                "retrain_months": 6,
                "min_train_months": 24,
            },
        ],
        "lstm_rank_v1": [
            {
                "seq_len": 20,
                "hidden_size": 32,
                "num_layers": 1,
                "dropout": 0.10,
                "learning_rate": 0.001,
                "epochs": 10,
                "top_k_pct": 0.05,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "seq_len": 60,
                "hidden_size": 64,
                "num_layers": 2,
                "dropout": 0.10,
                "learning_rate": 0.001,
                "epochs": 10,
                "top_k_pct": 0.10,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "seq_len": 120,
                "hidden_size": 64,
                "num_layers": 1,
                "dropout": 0.20,
                "learning_rate": 0.0005,
                "epochs": 20,
                "top_k_pct": 0.05,
                "retrain_months": 6,
                "min_train_months": 24,
            },
        ],
        "transformer_rank_v1": [
            {
                "seq_len": 20,
                "hidden_size": 32,
                "num_layers": 1,
                "nhead": 4,
                "dropout": 0.10,
                "learning_rate": 0.001,
                "epochs": 10,
                "top_k_pct": 0.05,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "seq_len": 40,
                "hidden_size": 32,
                "num_layers": 1,
                "nhead": 4,
                "dropout": 0.10,
                "learning_rate": 0.001,
                "epochs": 10,
                "top_k_pct": 0.10,
                "retrain_months": 6,
                "min_train_months": 24,
            },
            {
                "seq_len": 60,
                "hidden_size": 32,
                "num_layers": 1,
                "nhead": 4,
                "dropout": 0.20,
                "learning_rate": 0.0005,
                "epochs": 10,
                "top_k_pct": 0.05,
                "retrain_months": 6,
                "min_train_months": 24,
            },
        ],
    }


def _finite(metrics: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = metrics.get(key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else default


def _score(metrics: dict[str, Any], trades: int) -> float:
    if trades < 2:
        return -1_000_000.0
    sharpe = _finite(metrics, "sharpe_ratio", -100.0)
    annual = _finite(metrics, "annualized_return")
    drawdown = _finite(metrics, "max_drawdown")
    return sharpe + 0.25 * annual + 0.50 * drawdown


def _candidate_id(params: dict[str, Any]) -> str:
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _cleanup_accelerator() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def evaluate(
    registry: StrategyRegistry,
    pivot: pd.DataFrame,
    strategy_id: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    valid, error = registry.validate_params(strategy_id, params)
    if not valid:
        raise ValueError(error)
    metadata = registry.get_metadata(strategy_id)
    runtime_params = dict(params)
    if metadata.requires_training:
        runtime_params["_train_start"] = TRAIN_START
        runtime_params["_train_end"] = TRAIN_END

    strategy = registry.create_strategy(strategy_id)
    started = time.perf_counter()
    signals = strategy.generate_batch_signals(
        pivot,
        runtime_params,
        VALIDATION_START,
        VALIDATION_END,
    )
    signal_count = sum(len(items) for items in signals.values())
    if metadata.requires_training and signal_count == 0:
        raise RuntimeError("训练型策略没有生成信号")
    result = BacktestEngine(
        initial_capital=1_000_000,
        cost_model=CostModel(),
        start_date=VALIDATION_START,
        end_date=VALIDATION_END,
        max_positions=20,
    ).run(signals, pivot, strategy_id=strategy_id)
    metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)
    if "error" in metrics:
        raise RuntimeError(str(metrics["error"]))
    elapsed = time.perf_counter() - started
    trade_count = len(result.trade_log)
    return {
        "candidate_id": _candidate_id(params),
        "params": params,
        "status": "completed",
        "elapsed_seconds": round(elapsed, 3),
        "signals": signal_count,
        "trades": trade_count,
        "final_equity": result.final_equity,
        "score": _score(metrics, trade_count),
        "metrics": {
            "sharpe_ratio": _finite(metrics, "sharpe_ratio"),
            "annualized_return": _finite(metrics, "annualized_return"),
            "max_drawdown": _finite(metrics, "max_drawdown"),
            "win_rate": _finite(metrics, "win_rate"),
            "total_trades": _finite(metrics, "total_trades"),
        },
    }


def _save(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategies",
        nargs="*",
        help="Optional strategy IDs; default runs all ten",
    )
    parser.add_argument(
        "--report",
        default="data/tuning/csi500_429_tuning.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Discard existing results for the selected strategies",
    )
    args = parser.parse_args()

    cache = DataCache()
    pivot = __import__("asyncio").run(cache.load_pivot("csi500"))
    if pivot is None or pivot.empty:
        raise RuntimeError("csi500 行情缓存不存在")
    if not has_price_field(pivot, "open"):
        raise RuntimeError("csi500 仍是 close-only 缓存，请先构建 OHLCV")
    member_codes = sorted({str(code) for code in pivot.columns.get_level_values(0)})
    if len(member_codes) != 429:
        raise RuntimeError(f"调参必须使用429只成员，缓存实际为 {len(member_codes)}")

    registry = StrategyRegistry()
    registry.scan_directory("backend/strategies")
    spaces = candidate_spaces()
    selected = args.strategies or sorted(spaces)
    unknown = sorted(set(selected) - set(spaces))
    if unknown:
        raise ValueError(f"未知策略: {unknown}")

    report_path = settings_path = PROJECT_ROOT / args.report
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {
            "protocol": {
                "pool_id": "csi500",
                "pool_members": 429,
                "train_start": TRAIN_START,
                "train_end": TRAIN_END,
                "validation_start": VALIDATION_START,
                "validation_end": VALIDATION_END,
                "execution": "T signal, T+1 open fill, T+1 close valuation",
                "initial_capital": 1_000_000,
                "max_positions": 20,
                "objective": "Sharpe + 0.25*annual_return + 0.50*max_drawdown",
            },
            "strategies": {},
        }

    for strategy_id in selected:
        if args.force:
            report["strategies"].pop(strategy_id, None)
        strategy_report = report["strategies"].setdefault(
            strategy_id,
            {"candidates": []},
        )
        candidates = spaces[strategy_id]
        current_candidate_ids = {
            _candidate_id(params)
            for params in candidates
        }
        latest_by_id: dict[str, dict[str, Any]] = {}
        for item in strategy_report["candidates"]:
            if item.get("candidate_id") in current_candidate_ids:
                latest_by_id[item["candidate_id"]] = item
        strategy_report["candidates"] = list(latest_by_id.values())
        _save(settings_path, report)
        completed = {
            item["candidate_id"]
            for item in strategy_report["candidates"]
            if item.get("status") == "completed"
        }
        for index, params in enumerate(candidates, 1):
            candidate_id = _candidate_id(params)
            if candidate_id in completed:
                print(
                    f"[{strategy_id}] {index}/{len(candidates)} "
                    f"{candidate_id} already completed",
                    flush=True,
                )
                continue
            print(
                f"[{strategy_id}] {index}/{len(candidates)} {candidate_id} {params}",
                flush=True,
            )
            try:
                item = evaluate(registry, pivot, strategy_id, params)
            except Exception as exc:
                item = {
                    "candidate_id": candidate_id,
                    "params": params,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            strategy_report["candidates"] = [
                result
                for result in strategy_report["candidates"]
                if result.get("candidate_id") != candidate_id
            ]
            strategy_report["candidates"].append(item)
            successful = [
                result
                for result in strategy_report["candidates"]
                if result.get("status") == "completed"
            ]
            if successful:
                best = max(successful, key=lambda result: result["score"])
                strategy_report["best_candidate_id"] = best["candidate_id"]
                strategy_report["best_params"] = best["params"]
                strategy_report["best_score"] = best["score"]
            _save(settings_path, report)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            _cleanup_accelerator()

    print(f"report={report_path}", flush=True)


if __name__ == "__main__":
    main()
