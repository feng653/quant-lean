"""Pairwise strategy-return correlation analytics.

The analysis is deliberately computed from persisted experiment equity curves.
It never fetches market data and never mutates research evidence. Returns are
derived from adjacent equity observations so every experiment uses the same
definition even when legacy ``daily_return`` values are absent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date as calendar_date
import math
from typing import Any, Literal


CorrelationMethod = Literal["pearson", "spearman"]

HIGH_CORRELATION = 0.80
VERY_HIGH_CORRELATION = 0.95
LOW_CORRELATION = 0.20
NEGATIVE_DIVERSIFIER = -0.25
TRADING_DAYS = 252


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rank(values: Sequence[float]) -> list[float]:
    """Return one-based average ranks, including deterministic tie handling."""
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    position = 0
    while position < len(ordered):
        end = position + 1
        while end < len(ordered) and ordered[end][1] == ordered[position][1]:
            end += 1
        average_rank = ((position + 1) + end) / 2
        for original_index, _ in ordered[position:end]:
            ranks[original_index] = average_rank
        position = end
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    left_mean = math.fsum(left) / len(left)
    right_mean = math.fsum(right) / len(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    left_ss = math.fsum(value * value for value in left_centered)
    right_ss = math.fsum(value * value for value in right_centered)
    if left_ss <= 1e-30 or right_ss <= 1e-30:
        return None
    covariance = math.fsum(
        left_value * right_value
        for left_value, right_value in zip(left_centered, right_centered, strict=True)
    )
    result = covariance / math.sqrt(left_ss * right_ss)
    return max(-1.0, min(1.0, result))


def _correlation(
    left: Sequence[float],
    right: Sequence[float],
    method: CorrelationMethod,
) -> float | None:
    if method == "spearman":
        return _pearson(_rank(left), _rank(right))
    return _pearson(left, right)


def _classification(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value >= VERY_HIGH_CORRELATION:
        return "near_duplicate"
    if value >= HIGH_CORRELATION:
        return "high_positive"
    if value <= -HIGH_CORRELATION:
        return "high_negative"
    if value <= NEGATIVE_DIVERSIFIER:
        return "negative"
    if abs(value) <= LOW_CORRELATION:
        return "low"
    return "moderate"


def _build_return_series(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, tuple[str, float]], dict[str, Any]]:
    equity_by_date: dict[str, float] = {}
    invalid_points = 0
    duplicate_dates = 0
    for row in rows:
        date = str(row.get("date") or "").strip()
        equity = _finite_number(row.get("equity"))
        try:
            canonical_date = calendar_date.fromisoformat(date).isoformat()
        except ValueError:
            canonical_date = ""
        if not date or canonical_date != date or equity is None:
            invalid_points += 1
            continue
        if date in equity_by_date:
            duplicate_dates += 1
        # Keep the last database row deterministically, while disclosing the
        # integrity issue. The API orders by row id for this purpose.
        equity_by_date[date] = equity

    # Keep the previous observation date with each return. Two values are
    # considered aligned only when both the return date and its start date
    # match; this avoids correlating a multi-session gap with a one-day move.
    returns: dict[str, tuple[str, float]] = {}
    invalid_returns = 0
    ordered = sorted(equity_by_date.items())
    for index in range(1, len(ordered)):
        date, equity = ordered[index]
        previous_date, previous_equity = ordered[index - 1]
        if previous_equity == 0:
            invalid_returns += 1
            continue
        value = equity / previous_equity - 1.0
        if not math.isfinite(value):
            invalid_returns += 1
            continue
        returns[date] = (previous_date, value)

    quality = {
        "equity_observations": len(equity_by_date),
        "return_observations": len(returns),
        "invalid_equity_points": invalid_points,
        "duplicate_dates": duplicate_dates,
        "invalid_returns": invalid_returns,
        "return_start": min(returns) if returns else None,
        "return_end": max(returns) if returns else None,
    }
    return returns, quality


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires observations")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _holding_snapshots(
    rows: Iterable[Mapping[str, Any]],
    dates: Sequence[str],
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """Reconstruct end-of-day code holdings from persisted trade evidence."""
    ordered_rows: list[tuple[str, int, str, int]] = []
    invalid = 0
    for index, row in enumerate(rows):
        date = str(row.get("date") or "")
        code = str(row.get("code") or "").strip()
        action = str(row.get("action") or "").strip().lower()
        shares_value = _finite_number(row.get("shares"))
        if (
            not date
            or not code
            or shares_value is None
            or shares_value < 0
            or action not in {"buy", "sell", "买入", "卖出"}
        ):
            invalid += 1
            continue
        signed = int(round(shares_value))
        if action in {"sell", "卖出"}:
            signed = -signed
        ordered_rows.append((date, index, code, signed))
    ordered_rows.sort()
    positions: dict[str, int] = {}
    snapshots: dict[str, set[str]] = {}
    cursor = 0
    for date in sorted(dates):
        while cursor < len(ordered_rows) and ordered_rows[cursor][0] <= date:
            _, _, code, signed = ordered_rows[cursor]
            positions[code] = max(0, positions.get(code, 0) + signed)
            cursor += 1
        snapshots[date] = {
            code for code, shares in positions.items() if shares > 0
        }
    return snapshots, {
        "valid_trade_rows": len(ordered_rows),
        "invalid_trade_rows": invalid,
        "method": "end_of_day_inventory_from_persisted_trades",
    }


def _portfolio_contributions(
    *,
    experiment_items: Sequence[Mapping[str, Any]],
    series: Mapping[int, Mapping[str, tuple[str, float]]],
    weights: Sequence[float],
    min_observations: int,
    tail_fraction: float,
) -> dict[str, Any]:
    ids = [int(item["id"]) for item in experiment_items]
    dates = sorted(set.intersection(*(set(series[item]) for item in ids)))
    dates = [
        date
        for date in dates
        if len({series[item][date][0] for item in ids}) == 1
    ]
    if len(dates) < min_observations:
        return {
            "available": False,
            "unavailable_reason": "insufficient_common_overlap",
            "common_observations": len(dates),
            "weights": [
                {"experiment_id": item, "weight": weight}
                for item, weight in zip(ids, weights, strict=True)
            ],
            "read_only": True,
        }
    matrix = [
        [series[experiment_id][date][1] for date in dates]
        for experiment_id in ids
    ]
    portfolio = [
        math.fsum(
            weights[index] * matrix[index][column]
            for index in range(len(ids))
        )
        for column in range(len(dates))
    ]
    portfolio_mean = math.fsum(portfolio) / len(portfolio)
    portfolio_variance = math.fsum(
        (value - portfolio_mean) ** 2 for value in portfolio
    ) / max(len(portfolio) - 1, 1)
    daily_volatility = math.sqrt(max(portfolio_variance, 0))
    cutoff = _quantile(portfolio, tail_fraction)
    tail_indexes = [
        index for index, value in enumerate(portfolio) if value <= cutoff
    ]
    contributions: list[dict[str, Any]] = []
    for row_index, experiment_id in enumerate(ids):
        values = matrix[row_index]
        mean = math.fsum(values) / len(values)
        covariance = math.fsum(
            (values[index] - mean) * (portfolio[index] - portfolio_mean)
            for index in range(len(values))
        ) / max(len(values) - 1, 1)
        annual_return_contribution = weights[row_index] * mean * TRADING_DAYS
        annual_risk_contribution = (
            weights[row_index]
            * covariance
            / daily_volatility
            * math.sqrt(TRADING_DAYS)
            if daily_volatility > 1e-15
            else None
        )
        tail_contribution = (
            weights[row_index]
            * math.fsum(values[index] for index in tail_indexes)
            / len(tail_indexes)
            if tail_indexes
            else None
        )
        contributions.append(
            {
                "experiment_id": experiment_id,
                "weight": weights[row_index],
                "annual_return_contribution": annual_return_contribution,
                "annual_risk_contribution": annual_risk_contribution,
                "risk_contribution_share": (
                    annual_risk_contribution
                    / (daily_volatility * math.sqrt(TRADING_DAYS))
                    if annual_risk_contribution is not None
                    and daily_volatility > 1e-15
                    else None
                ),
                "tail_return_contribution": tail_contribution,
            }
        )
    return {
        "available": True,
        "common_observations": len(dates),
        "common_start": dates[0],
        "common_end": dates[-1],
        "weight_policy": "user_supplied_normalized",
        "annualized_return": portfolio_mean * TRADING_DAYS,
        "annualized_volatility": daily_volatility * math.sqrt(TRADING_DAYS),
        "tail_fraction": tail_fraction,
        "tail_cutoff": cutoff,
        "tail_observations": len(tail_indexes),
        "contributions": contributions,
        "read_only": True,
    }


def analyze_strategy_correlations(
    experiments: Sequence[Mapping[str, Any]],
    equity_rows: Mapping[int, Iterable[Mapping[str, Any]]],
    *,
    method: CorrelationMethod = "pearson",
    min_observations: int = 60,
    trade_rows: Mapping[int, Iterable[Mapping[str, Any]]] | None = None,
    weights: Sequence[float] | None = None,
    tail_fraction: float = 0.1,
) -> dict[str, Any]:
    """Build a pairwise correlation matrix and auditable pair diagnostics."""
    if method not in {"pearson", "spearman"}:
        raise ValueError(f"unsupported correlation method: {method}")
    if min_observations < 2:
        raise ValueError("min_observations must be at least 2")
    if not 0.01 <= tail_fraction <= 0.25:
        raise ValueError("tail_fraction must be between 0.01 and 0.25")
    if weights is None:
        weights = [1 / len(experiments)] * len(experiments)
    if (
        len(weights) != len(experiments)
        or any(not math.isfinite(value) or value < 0 for value in weights)
        or math.fsum(weights) <= 0
    ):
        raise ValueError("weights must align, be finite and have a positive sum")
    weight_sum = math.fsum(weights)
    normalized_weights = [value / weight_sum for value in weights]

    series: dict[int, dict[str, tuple[str, float]]] = {}
    experiment_items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for experiment in experiments:
        experiment_id = int(experiment["id"])
        returns, quality = _build_return_series(equity_rows.get(experiment_id, ()))
        series[experiment_id] = returns
        item = {
            "id": experiment_id,
            "name": str(experiment["name"]),
            "strategy_id": str(experiment["strategy_id"]),
            "test_start": experiment.get("test_start"),
            "test_end": experiment.get("test_end"),
            "quality": quality,
        }
        experiment_items.append(item)
        if quality["return_observations"] < min_observations:
            warnings.append(
                {
                    "level": "warning",
                    "code": "insufficient_history",
                    "experiment_ids": [experiment_id],
                    "message": (
                        f"实验 #{experiment_id} 仅有 {quality['return_observations']} "
                        f"个有效日收益观测，少于门槛 {min_observations}。"
                    ),
                }
            )
        if quality["duplicate_dates"]:
            warnings.append(
                {
                    "level": "warning",
                    "code": "duplicate_equity_dates",
                    "experiment_ids": [experiment_id],
                    "message": (
                        f"实验 #{experiment_id} 的净值曲线含 "
                        f"{quality['duplicate_dates']} 个重复日期；分析使用每个日期最后一条记录。"
                    ),
                }
            )
        if quality["invalid_equity_points"] or quality["invalid_returns"]:
            warnings.append(
                {
                    "level": "warning",
                    "code": "invalid_equity_data",
                    "experiment_ids": [experiment_id],
                    "message": (
                        f"实验 #{experiment_id} 跳过了 "
                        f"{quality['invalid_equity_points']} 个无效净值点和 "
                        f"{quality['invalid_returns']} 个无效收益。"
                    ),
                }
            )

    size = len(experiment_items)
    values: list[list[float | None]] = [[None] * size for _ in range(size)]
    overlaps: list[list[int]] = [[0] * size for _ in range(size)]
    pairs: list[dict[str, Any]] = []
    all_dates = sorted({date for item in series.values() for date in item})
    holding_snapshots: dict[int, dict[str, set[str]]] = {}
    holding_quality: dict[int, dict[str, Any]] = {}
    for experiment_id in series:
        snapshots, quality = _holding_snapshots(
            (trade_rows or {}).get(experiment_id, ()),
            all_dates,
        )
        holding_snapshots[experiment_id] = snapshots
        holding_quality[experiment_id] = quality

    for left_index in range(size):
        left_id = experiment_items[left_index]["id"]
        for right_index in range(left_index, size):
            right_id = experiment_items[right_index]["id"]
            candidate_dates = sorted(set(series[left_id]).intersection(series[right_id]))
            common_dates = [
                date
                for date in candidate_dates
                if series[left_id][date][0] == series[right_id][date][0]
            ]
            overlap = len(common_dates)
            interval_mismatch_exclusions = len(candidate_dates) - overlap
            overlaps[left_index][right_index] = overlap
            overlaps[right_index][left_index] = overlap
            value: float | None = None
            reason: str | None = None
            left_values = [series[left_id][date][1] for date in common_dates]
            right_values = [series[right_id][date][1] for date in common_dates]
            if overlap < min_observations:
                reason = "insufficient_overlap"
            else:
                value = _correlation(left_values, right_values, method)
                if value is None:
                    reason = "constant_series"
            values[left_index][right_index] = value
            values[right_index][left_index] = value

            if left_index == right_index:
                continue
            pair = {
                "left_experiment_id": left_id,
                "right_experiment_id": right_id,
                "correlation": value,
                "overlap": overlap,
                "overlap_start": common_dates[0] if common_dates else None,
                "overlap_end": common_dates[-1] if common_dates else None,
                "interval_mismatch_exclusions": interval_mismatch_exclusions,
                "classification": _classification(value),
                "unavailable_reason": reason,
            }
            if common_dates:
                left_tail_cutoff = _quantile(left_values, tail_fraction)
                right_tail_cutoff = _quantile(right_values, tail_fraction)
                tail_indexes = [
                    index
                    for index, (left_value, right_value) in enumerate(
                        zip(left_values, right_values, strict=True)
                    )
                    if left_value <= left_tail_cutoff
                    or right_value <= right_tail_cutoff
                ]
            else:
                tail_indexes = []
            tail_minimum = max(5, math.ceil(min_observations * tail_fraction))
            pair["tail_correlation"] = {
                "fraction": tail_fraction,
                "observations": len(tail_indexes),
                "correlation": (
                    _correlation(
                        [left_values[index] for index in tail_indexes],
                        [right_values[index] for index in tail_indexes],
                        method,
                    )
                    if len(tail_indexes) >= tail_minimum
                    else None
                ),
                "unavailable_reason": (
                    None
                    if len(tail_indexes) >= tail_minimum
                    else "insufficient_tail_overlap"
                ),
            }
            holding_values: list[float] = []
            for date in common_dates:
                left_codes = holding_snapshots[left_id].get(date, set())
                right_codes = holding_snapshots[right_id].get(date, set())
                union = left_codes | right_codes
                if union:
                    holding_values.append(len(left_codes & right_codes) / len(union))
            pair["holding_overlap"] = {
                "method": "daily_code_jaccard",
                "observations": len(holding_values),
                "mean": (
                    math.fsum(holding_values) / len(holding_values)
                    if holding_values
                    else None
                ),
                "latest": holding_values[-1] if holding_values else None,
                "maximum": max(holding_values) if holding_values else None,
                "unavailable_reason": (
                    None if holding_values else "trade_inventory_unavailable"
                ),
            }
            pairs.append(pair)
            if value is not None and value >= HIGH_CORRELATION:
                level = "danger" if value >= VERY_HIGH_CORRELATION else "warning"
                warnings.append(
                    {
                        "level": level,
                        "code": (
                            "near_duplicate_returns"
                            if value >= VERY_HIGH_CORRELATION
                            else "high_positive_correlation"
                        ),
                        "experiment_ids": [left_id, right_id],
                        "message": (
                            f"实验 #{left_id} 与 #{right_id} 的相关系数为 {value:.3f}，"
                            "组合后的分散化收益可能有限。"
                        ),
                    }
                )

    pairs.sort(
        key=lambda pair: (
            pair["correlation"] is None,
            -(abs(pair["correlation"]) if pair["correlation"] is not None else 0),
            pair["left_experiment_id"],
            pair["right_experiment_id"],
        )
    )
    available_pairs = [pair for pair in pairs if pair["correlation"] is not None]
    high_pairs = [
        pair
        for pair in available_pairs
        if pair["correlation"] >= HIGH_CORRELATION
    ]
    diversifier_pairs = [
        pair
        for pair in available_pairs
        if pair["correlation"] <= NEGATIVE_DIVERSIFIER
    ]
    portfolio = _portfolio_contributions(
        experiment_items=experiment_items,
        series=series,
        weights=normalized_weights,
        min_observations=min_observations,
        tail_fraction=tail_fraction,
    )
    suggestions: list[dict[str, Any]] = []
    if portfolio.get("available"):
        contributions = portfolio["contributions"]
        for item in contributions:
            experiment_id = int(item["experiment_id"])
            related_pairs = [
                pair
                for pair in available_pairs
                if experiment_id
                in {
                    pair["left_experiment_id"],
                    pair["right_experiment_id"],
                }
            ]
            high_links = sum(
                1
                for pair in related_pairs
                if pair["correlation"] >= HIGH_CORRELATION
                or (pair["holding_overlap"]["mean"] or 0) >= 0.6
            )
            risk_share = item["risk_contribution_share"]
            reasons: list[str] = []
            suggested_cap = min(0.5, 1.5 / max(size, 1))
            if high_links:
                suggested_cap = min(suggested_cap, 1 / max(size, 1))
                reasons.append("与其他策略高度相关或持仓重叠")
            if risk_share is not None and risk_share > item["weight"] + 0.1:
                suggested_cap = min(suggested_cap, item["weight"])
                reasons.append("边际风险贡献明显高于资金权重")
            suggestions.append(
                {
                    "experiment_id": experiment_id,
                    "suggested_max_weight": suggested_cap,
                    "reasons": reasons or ["未发现显著集中信号，仍建议设置组合上限"],
                    "action": "review_only",
                }
            )

    return {
        "analysis_role": "post_hoc_diversification_diagnostic",
        "method": method,
        "min_observations": min_observations,
        "return_definition": "adjacent_persisted_equity_pct_change",
        "thresholds": {
            "near_duplicate": VERY_HIGH_CORRELATION,
            "high_positive": HIGH_CORRELATION,
            "negative_diversifier": NEGATIVE_DIVERSIFIER,
            "low_absolute": LOW_CORRELATION,
        },
        "experiments": experiment_items,
        "holding_quality": holding_quality,
        "matrix": {
            "experiment_ids": [item["id"] for item in experiment_items],
            "values": values,
            "overlap_counts": overlaps,
        },
        "pairs": pairs,
        "portfolio_contribution": portfolio,
        "constraint_suggestions": suggestions,
        "automation": {
            "mutates_portfolio": False,
            "message": "建议仅用于人工审查，不会自动修改组合或交易配置。",
        },
        "warnings": warnings,
        "summary": {
            "total_pairs": len(pairs),
            "available_pairs": len(available_pairs),
            "unavailable_pairs": len(pairs) - len(available_pairs),
            "high_correlation_pairs": len(high_pairs),
            "negative_diversifier_pairs": len(diversifier_pairs),
        },
    }
