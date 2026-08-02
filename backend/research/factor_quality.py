"""Auditable implementation and multi-factor quality analysis.

All calculations are deterministic and operate only on the caller-provided
request window.  Capacity is never inferred without an explicit amount panel.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from backend.research.factor_analysis import (
    analyze_quantile_returns,
    calculate_ic,
    factor_correlation_matrix,
)

_EPSILON = 1e-12


def _safe(value: Any) -> float | int | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _frame(panel: pd.DataFrame | Mapping[str, Any], name: str) -> pd.DataFrame:
    if isinstance(panel, pd.DataFrame):
        result = panel.copy(deep=True)
    elif isinstance(panel, Mapping):
        result = pd.DataFrame(
            panel.get("values"),
            index=pd.to_datetime(panel.get("dates")),
            columns=[str(code) for code in panel.get("codes", [])],
            dtype=float,
        )
    else:
        raise TypeError(f"{name} 必须是 DataFrame 或面板载荷")
    if isinstance(result.columns, pd.MultiIndex):
        raise ValueError(f"{name} 必须是 date × code 单字段面板")
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index))
    result.columns = result.columns.map(str)
    if result.index.has_duplicates or result.columns.has_duplicates:
        raise ValueError(f"{name} 的日期和股票代码必须唯一")
    return (
        result.sort_index(kind="stable")
        .reindex(sorted(result.columns), axis=1)
        .apply(pd.to_numeric, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
    )


def _summary(values: Sequence[float | None]) -> dict[str, Any]:
    finite = np.asarray(
        [value for value in values if value is not None and math.isfinite(value)],
        dtype=float,
    )
    if not len(finite):
        return {"count": 0, "mean": None, "min": None, "max": None}
    return {
        "count": int(len(finite)),
        "mean": _safe(float(finite.mean())),
        "min": _safe(float(finite.min())),
        "max": _safe(float(finite.max())),
    }


def _canonical_panel_digest(factors: Mapping[str, pd.DataFrame]) -> str:
    payload: dict[str, Any] = {}
    for factor_id in sorted(factors):
        frame = _frame(factors[factor_id], factor_id)
        payload[factor_id] = {
            "dates": [date.strftime("%Y-%m-%d") for date in frame.index],
            "codes": list(frame.columns),
            "values": [
                [_safe(value) for value in row]
                for row in frame.to_numpy(dtype=float)
            ],
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _daily_groups(
    factor: pd.Series,
    forward_return: pd.Series,
    *,
    quantiles: int,
    min_samples: int,
) -> tuple[pd.DataFrame, pd.Series] | None:
    aligned = pd.concat(
        [
            factor.rename("factor"),
            forward_return.rename("forward_return"),
        ],
        axis=1,
    ).replace([np.inf, -np.inf], np.nan).dropna().sort_index()
    if len(aligned) < min_samples:
        return None
    ranks = aligned["factor"].rank(method="first", ascending=True)
    groups = pd.qcut(
        ranks,
        q=quantiles,
        labels=list(range(1, quantiles + 1)),
    ).astype(int)
    return aligned, groups


def analyze_implementation_quality(
    factor: pd.DataFrame | Mapping[str, Any],
    forward_returns: pd.DataFrame | Mapping[str, Any],
    *,
    amount: pd.DataFrame | Mapping[str, Any] | None,
    quantiles: int,
    rebalance_interval: int,
    cost_scenarios_bps: Sequence[float],
    default_cost_bps: float,
    capacity_participation_rates: Sequence[float],
    min_samples: int,
) -> dict[str, Any]:
    """Estimate gross/net stratified returns, turnover and amount capacity.

    Returns are sampled at the requested rebalance interval.  Equal-weight
    quantile portfolios are rebuilt on those dates.  Transaction cost is
    charged against one-way turnover and therefore remains an explicit,
    inspectable assumption instead of a hidden backtest default.
    """

    factor_frame = _frame(factor, "factor")
    return_frame = _frame(forward_returns, "forward_returns")
    amount_frame = _frame(amount, "amount") if amount is not None else None
    dates = factor_frame.index.intersection(return_frame.index).sort_values()
    codes = sorted(set(factor_frame.columns).intersection(return_frame.columns))
    sampled_dates = dates[::rebalance_interval]
    group_gross: dict[int, list[float]] = {
        group: [] for group in range(1, quantiles + 1)
    }
    group_turnovers: dict[int, list[float]] = {
        group: [] for group in range(1, quantiles + 1)
    }
    group_net: dict[float, dict[int, list[float]]] = {
        float(cost): {
            group: [] for group in range(1, quantiles + 1)
        }
        for cost in cost_scenarios_bps
    }
    spread_gross: list[float] = []
    spread_turnovers: list[float] = []
    spread_net: dict[float, list[float]] = {
        float(cost): [] for cost in cost_scenarios_bps
    }
    previous_groups: dict[int, pd.Series] = {}
    previous_spread: pd.Series | None = None
    turnover_series: list[dict[str, Any]] = []
    capacity_rows: list[dict[str, Any]] = []
    evaluable_observations = 0
    possible_observations = len(sampled_dates) * len(codes)
    tradable_observations = 0
    amount_observations = 0

    for date in sampled_dates:
        grouped = _daily_groups(
            factor_frame.loc[date, codes],
            return_frame.loc[date, codes],
            quantiles=quantiles,
            min_samples=min_samples,
        )
        if grouped is None:
            continue
        aligned, labels = grouped
        evaluable_observations += len(aligned)
        current_groups: dict[int, pd.Series] = {}
        for group in range(1, quantiles + 1):
            members = aligned.index[labels == group]
            weights = pd.Series(0.0, index=codes, dtype=float)
            weights.loc[members] = 1.0 / len(members)
            current_groups[group] = weights
            previous = previous_groups.get(group)
            turnover = (
                1.0
                if previous is None
                else 0.5 * float((weights - previous).abs().sum())
            )
            gross = float(aligned.loc[members, "forward_return"].mean())
            group_gross[group].append(gross)
            group_turnovers[group].append(turnover)
            for cost in cost_scenarios_bps:
                group_net[float(cost)][group].append(
                    gross - turnover * float(cost) / 10_000.0
                )

        spread_weights = (
            0.5 * current_groups[quantiles] - 0.5 * current_groups[1]
        )
        spread_turnover = (
            1.0
            if previous_spread is None
            else 0.5 * float((spread_weights - previous_spread).abs().sum())
        )
        gross_spread = float(
            aligned.loc[labels == quantiles, "forward_return"].mean()
            - aligned.loc[labels == 1, "forward_return"].mean()
        )
        spread_gross.append(gross_spread)
        spread_turnovers.append(spread_turnover)
        for cost in cost_scenarios_bps:
            spread_net[float(cost)].append(
                gross_spread - spread_turnover * float(cost) / 10_000.0
            )
        turnover_series.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "group_turnover": {
                    str(group): _safe(group_turnovers[group][-1])
                    for group in range(1, quantiles + 1)
                },
                "long_short_turnover": _safe(spread_turnover),
            }
        )

        capacity_row: dict[str, Any] = {
            "date": date.strftime("%Y-%m-%d"),
            "status": "unavailable",
            "estimates": {},
        }
        if amount_frame is not None and date in amount_frame.index:
            traded = spread_weights if previous_spread is None else (
                spread_weights - previous_spread
            )
            traded = traded[traded.abs() > _EPSILON]
            date_amount = amount_frame.loc[date].reindex(traded.index)
            amount_observations += len(date_amount)
            valid = date_amount.notna() & (date_amount > 0)
            tradable_observations += int(valid.sum())
            if len(traded) and bool(valid.all()):
                capacity_row["status"] = "available"
                capacity_row["estimates"] = {
                    str(rate): _safe(
                        float(
                            (
                                float(rate)
                                * date_amount
                                / traded.abs()
                            ).min()
                        )
                    )
                    for rate in capacity_participation_rates
                }
            elif len(traded) == 0:
                capacity_row["status"] = "no_trade"
        capacity_rows.append(capacity_row)
        previous_groups = current_groups
        previous_spread = spread_weights

    sensitivity = []
    for cost in cost_scenarios_bps:
        cost_key = float(cost)
        sensitivity.append(
            {
                "cost_bps": cost_key,
                "mean_group_returns": {
                    str(group): _summary(group_net[cost_key][group])["mean"]
                    for group in range(1, quantiles + 1)
                },
                "long_short": _summary(spread_net[cost_key]),
            }
        )
    default_result = next(
        item for item in sensitivity
        if math.isclose(item["cost_bps"], default_cost_bps, abs_tol=1e-9)
    )
    available_capacity = [
        item for item in capacity_rows if item["status"] == "available"
    ]
    capacity_status = (
        "unavailable"
        if amount_frame is None
        else "available"
        if capacity_rows
        and all(
            row["status"] in {"available", "no_trade"}
            for row in capacity_rows
        )
        else "partial"
    )
    capacity_scenarios = {
        str(rate): _summary(
            [
                row["estimates"].get(str(rate))
                for row in available_capacity
            ]
        )
        for rate in capacity_participation_rates
    }
    return {
        "schema_version": "factor-implementation/v1",
        "status": "available" if spread_gross else "insufficient_samples",
        "assumptions": {
            "portfolio": "equal_weight_quantiles_high_minus_low",
            "rebalance_interval_sessions": int(rebalance_interval),
            "return_horizon_sessions": None,
            "default_cost_bps": float(default_cost_bps),
            "cost_scenarios_bps": [float(value) for value in cost_scenarios_bps],
            "cost_convention": "one_way_turnover_times_bps",
            "initial_entry_turnover": 1.0,
            "capacity_participation_rates": [
                float(value) for value in capacity_participation_rates
            ],
            "capacity_currency": "source_amount_currency",
        },
        "coverage": {
            "sampled_rebalance_dates": int(len(sampled_dates)),
            "evaluated_rebalance_dates": int(len(spread_gross)),
            "evaluable_observations": int(evaluable_observations),
            "possible_observations": int(possible_observations),
            "evaluation_ratio": _safe(
                evaluable_observations / possible_observations
                if possible_observations
                else None
            ),
            "tradable": {
                "status": "unavailable" if amount_frame is None else capacity_status,
                "reason": (
                    "amount_field_missing"
                    if amount_frame is None
                    else "amount_incomplete"
                    if capacity_status == "partial"
                    else None
                ),
                "positive_amount_observations": int(tradable_observations),
                "amount_observations": int(amount_observations),
                "ratio": _safe(
                    tradable_observations / amount_observations
                    if amount_observations
                    else None
                ),
            },
        },
        "gross": {
            "mean_group_returns": {
                str(group): _summary(group_gross[group])["mean"]
                for group in range(1, quantiles + 1)
            },
            "long_short": _summary(spread_gross),
        },
        "net_default": default_result,
        "cost_sensitivity": sensitivity,
        "turnover": {
            "series": turnover_series,
            "long_short": _summary(spread_turnovers),
            "mean_group_turnover": {
                str(group): _summary(group_turnovers[group])["mean"]
                for group in range(1, quantiles + 1)
            },
        },
        "capacity": {
            "status": capacity_status,
            "reason": (
                "amount_field_missing"
                if amount_frame is None
                else "amount_incomplete"
                if capacity_status == "partial"
                else None
            ),
            "amount_field": "amount" if amount_frame is not None else None,
            "available_rebalance_dates": int(len(available_capacity)),
            "total_rebalance_dates": int(len(capacity_rows)),
            "scenarios": capacity_scenarios,
            "daily": capacity_rows,
        },
    }


def orthogonalize_factor_panels(
    factors: Mapping[str, pd.DataFrame],
    *,
    min_samples: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Sequentially residualize factors in stable lexical factor-id order."""

    order = sorted(str(factor_id) for factor_id in factors)
    frames = {factor_id: _frame(factors[factor_id], factor_id) for factor_id in order}
    input_digest = _canonical_panel_digest(frames)
    transformed: dict[str, pd.DataFrame] = {}
    steps: list[dict[str, Any]] = []
    for position, factor_id in enumerate(order):
        source = frames[factor_id]
        if position == 0:
            transformed[factor_id] = source
            steps.append(
                {
                    "factor_id": factor_id,
                    "regressed_on": [],
                    "method": "identity_first_factor",
                    "successful_dates": int(source.notna().any(axis=1).sum()),
                    "insufficient_dates": 0,
                }
            )
            continue
        residuals = pd.DataFrame(np.nan, index=source.index, columns=source.columns)
        regressors = order[:position]
        successful = 0
        insufficient = 0
        for date in source.index:
            pieces = [source.loc[date].rename("target")]
            for other in regressors:
                if date in transformed[other].index:
                    pieces.append(transformed[other].loc[date].rename(other))
                else:
                    pieces.append(
                        pd.Series(
                            np.nan,
                            index=source.columns,
                            name=other,
                            dtype=float,
                        )
                    )
            aligned = pd.concat(pieces, axis=1).dropna().sort_index()
            if len(aligned) < max(min_samples, len(regressors) + 2):
                insufficient += 1
                continue
            y = aligned["target"].to_numpy(dtype=float)
            x = np.column_stack(
                [
                    np.ones(len(aligned), dtype=float),
                    aligned[regressors].to_numpy(dtype=float),
                ]
            )
            coefficients, _, _, _ = np.linalg.lstsq(x, y, rcond=None)
            residual = y - x @ coefficients
            std = float(residual.std(ddof=0))
            if std > _EPSILON:
                residual = (residual - float(residual.mean())) / std
            residuals.loc[date, aligned.index] = residual
            successful += 1
        transformed[factor_id] = residuals
        steps.append(
            {
                "factor_id": factor_id,
                "regressed_on": regressors,
                "method": "daily_cross_sectional_ols_residual_zscore",
                "successful_dates": successful,
                "insufficient_dates": insufficient,
            }
        )
    return transformed, {
        "enabled": True,
        "order": order,
        "order_rule": "lexical_factor_id_ascending",
        "fit_window": "request_start_to_end_only",
        "method": "sequential_daily_cross_sectional_ols",
        "input_digest": input_digest,
        "steps": steps,
    }


def analyze_multi_factor_quality(
    factors: Mapping[str, pd.DataFrame],
    forward_returns: pd.DataFrame | Mapping[str, Any],
    *,
    weights: Mapping[str, float],
    quantiles: int,
    min_samples: int,
    orthogonalize: bool,
) -> dict[str, Any]:
    """Correlate, optionally orthogonalize and evaluate a bounded combination."""

    ordered = sorted(str(factor_id) for factor_id in factors)
    if not ordered:
        raise ValueError("至少需要一个因子")
    if set(weights) != set(ordered):
        raise ValueError("weights 必须完整覆盖因子")
    if any(
        isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
        or float(value) > 1
        for value in weights.values()
    ) or not math.isclose(
        sum(float(value) for value in weights.values()),
        1.0,
        abs_tol=1e-9,
    ):
        raise ValueError("weights 必须有界且权重和为 1")
    frames = {factor_id: _frame(factors[factor_id], factor_id) for factor_id in ordered}
    input_digest = _canonical_panel_digest(frames)
    pearson = factor_correlation_matrix(
        frames,
        method="pearson",
        min_samples=min_samples,
    )
    spearman = factor_correlation_matrix(
        frames,
        method="spearman",
        min_samples=min_samples,
    )
    if orthogonalize:
        transformed, transform = orthogonalize_factor_panels(
            frames,
            min_samples=min_samples,
        )
    else:
        transformed = frames
        transform = {
            "enabled": False,
            "order": ordered,
            "order_rule": "lexical_factor_id_ascending",
            "fit_window": "request_start_to_end_only",
            "method": "none",
            "input_digest": input_digest,
            "steps": [],
        }
    combination = sum(
        transformed[factor_id] * float(weights[factor_id])
        for factor_id in ordered
    )
    combination = combination.replace([np.inf, -np.inf], np.nan)
    forward = _frame(forward_returns, "forward_returns")
    score_digest = _canonical_panel_digest({"combination": combination})
    return {
        "schema_version": "factor-multi-quality/v1",
        "status": "available" if len(ordered) > 1 else "single_factor",
        "input_digest": input_digest,
        "correlation": {
            "alignment": "same_date_and_code_pairwise_complete",
            "pearson": pearson,
            "spearman": spearman,
        },
        "orthogonalization": transform,
        "combination": {
            "weights": {
                factor_id: float(weights[factor_id]) for factor_id in ordered
            },
            "constraints": {
                "lower_bound": 0.0,
                "upper_bound": 1.0,
                "sum": 1.0,
                "shorting": False,
            },
            "score_digest": score_digest,
            "ic": calculate_ic(
                combination,
                forward,
                min_samples=min_samples,
            ),
            "quantile_returns": analyze_quantile_returns(
                combination,
                forward,
                quantiles=quantiles,
                min_samples=min_samples,
            ),
        },
        "publication": {
            "status": "not_published",
            "automatic_publish": False,
            "message": "组合仅为研究结果，必须人工审查后单独导出到策略池。",
        },
    }
