"""Deterministic, non-publishing portfolio candidate construction."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import hashlib
import json
import math
from typing import Any

from backend.services.research_manifest import canonical_sha256
from backend.services.strategy_correlation import _build_return_series


CANDIDATE_SET_SCHEMA = "portfolio-candidate-set/v1"
CANDIDATE_MANIFEST_SCHEMA = "portfolio-candidate-manifest/v1"
CANDIDATE_STRATEGY_ID = "composite_research_weighted_v1"
TRADING_DAYS = 252


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _metrics(values: Sequence[float], tail_fraction: float) -> dict[str, float]:
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / max(len(values) - 1, 1)
    volatility = math.sqrt(max(variance, 0.0))
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, equity / peak - 1.0)
    cutoff = _quantile(values, tail_fraction)
    tail = [value for value in values if value <= cutoff]
    annual_return = mean * TRADING_DAYS
    annual_volatility = volatility * math.sqrt(TRADING_DAYS)
    return {
        "annualized_return": annual_return,
        "annualized_volatility": annual_volatility,
        "return_to_risk": annual_return / annual_volatility if annual_volatility > 1e-15 else 0.0,
        "tail_mean": math.fsum(tail) / len(tail),
        "max_drawdown": max_drawdown,
    }


def _capped_weights(raw: Sequence[float], cap: float) -> list[float]:
    """Normalize non-negative scores under a per-component cap."""
    size = len(raw)
    if size * cap < 1 - 1e-12:
        raise ValueError("max_weight 与组件数量不相容")
    positive = [max(0.0, float(value)) for value in raw]
    if math.fsum(positive) <= 1e-15:
        positive = [1.0] * size
    weights = [0.0] * size
    remaining = set(range(size))
    remaining_total = 1.0
    while remaining:
        score_total = math.fsum(positive[index] for index in remaining)
        tentative = {
            index: (
                remaining_total * positive[index] / score_total
                if score_total > 1e-15
                else remaining_total / len(remaining)
            )
            for index in remaining
        }
        capped = [index for index, value in tentative.items() if value > cap + 1e-12]
        if not capped:
            for index, value in tentative.items():
                weights[index] = value
            break
        for index in capped:
            weights[index] = cap
            remaining_total -= cap
            remaining.remove(index)
    correction = 1.0 - math.fsum(weights)
    weights[max(range(size), key=weights.__getitem__)] += correction
    return weights


def _pair_map(report: Mapping[str, Any]) -> dict[tuple[int, int], Mapping[str, Any]]:
    return {
        tuple(sorted((int(item["left_experiment_id"]), int(item["right_experiment_id"])))): item
        for item in report.get("pairs", [])
    }


def _pair_penalty(item: Mapping[str, Any]) -> float:
    correlation = item.get("correlation")
    tail = item.get("tail_correlation") or {}
    tail_correlation = tail.get("correlation")
    holding = item.get("holding_overlap") or {}
    holding_mean = holding.get("mean")
    return (
        max(0.0, float(correlation or 0.0))
        + 0.75 * max(0.0, float(tail_correlation or 0.0))
        + 0.75 * max(0.0, float(holding_mean or 0.0))
    )


def _select_components(
    ordered_ids: Sequence[int],
    *,
    pair_by_ids: Mapping[tuple[int, int], Mapping[str, Any]],
    max_components: int,
    minimum_components: int,
    max_pair_correlation: float,
    max_holding_overlap: float,
) -> tuple[list[int], list[str]]:
    selected: list[int] = []
    relaxed: list[str] = []
    for experiment_id in ordered_ids:
        if len(selected) >= max_components:
            break
        pairs = [pair_by_ids.get(tuple(sorted((experiment_id, other)))) for other in selected]
        if all(
            pair is not None
            and pair.get("correlation") is not None
            and float(pair["correlation"]) <= max_pair_correlation
            and (
                (pair.get("holding_overlap") or {}).get("mean") is None
                or float((pair.get("holding_overlap") or {})["mean"])
                <= max_holding_overlap
            )
            for pair in pairs
        ):
            selected.append(experiment_id)
    minimum = min(minimum_components, len(ordered_ids))
    if len(selected) < minimum:
        remaining = [item for item in ordered_ids if item not in selected]
        while len(selected) < minimum and remaining:
            candidate = min(
                remaining,
                key=lambda item: math.fsum(
                    _pair_penalty(pair_by_ids[tuple(sorted((item, other)))])
                    for other in selected
                    if tuple(sorted((item, other))) in pair_by_ids
                ),
            )
            selected.append(candidate)
            remaining.remove(candidate)
        relaxed.append("minimum_component_count_required_constraint_relaxation")
    return selected, relaxed


def build_portfolio_candidates(
    *,
    experiments: Sequence[Mapping[str, Any]],
    equity_rows: Mapping[int, Iterable[Mapping[str, Any]]],
    correlation_report: Mapping[str, Any],
    manifest_hashes: Mapping[int, str],
    min_observations: int,
    tail_fraction: float,
    max_components: int = 6,
    max_pair_correlation: float = 0.80,
    max_holding_overlap: float = 0.60,
    max_weight: float = 0.40,
) -> dict[str, Any]:
    """Build exactly five review-only definitions from verified PIT runs."""
    if len(experiments) < 3:
        raise ValueError("至少需要三个非机器学习单策略实验")
    if not 3 <= max_components <= 8:
        raise ValueError("max_components 必须在 3..8 范围内")
    if not 0.2 <= max_weight <= 0.5:
        raise ValueError("max_weight 必须在 0.2..0.5 范围内")
    minimum_components = max(3, math.ceil(1 / max_weight - 1e-12))
    if len(experiments) < minimum_components or max_components < minimum_components:
        raise ValueError("来源数量或 max_components 无法满足 max_weight 上限")
    strategy_ids = [str(item["strategy_id"]) for item in experiments]
    if len(strategy_ids) != len(set(strategy_ids)):
        raise ValueError("每个候选来源必须对应不同的单策略")

    series: dict[int, dict[str, tuple[str, float]]] = {}
    for experiment in experiments:
        experiment_id = int(experiment["id"])
        returns, _ = _build_return_series(equity_rows.get(experiment_id, ()))
        series[experiment_id] = returns
    common_dates = sorted(set.intersection(*(set(item) for item in series.values())))
    common_dates = [
        date
        for date in common_dates
        if len({item[date][0] for item in series.values()}) == 1
    ]
    if len(common_dates) < min_observations:
        raise ValueError("所有来源实验的共同日收益观测不足")

    by_id = {int(item["id"]): item for item in experiments}
    experiment_metrics = {
        experiment_id: _metrics(
            [series[experiment_id][date][1] for date in common_dates],
            tail_fraction,
        )
        for experiment_id in by_id
    }
    pair_by_ids = _pair_map(correlation_report)
    source_digest = canonical_sha256(
        {
            "common_dates": common_dates,
            "correlation_method": correlation_report.get("method"),
            "manifest_hashes": [
                {"experiment_id": item, "manifest_hash": manifest_hashes[item]}
                for item in sorted(manifest_hashes)
            ],
            "return_definition": correlation_report.get("return_definition"),
        }
    )

    def average_penalty(experiment_id: int) -> float:
        related = [
            pair_by_ids[tuple(sorted((experiment_id, other)))]
            for other in by_id
            if other != experiment_id
            and tuple(sorted((experiment_id, other))) in pair_by_ids
        ]
        return math.fsum(_pair_penalty(item) for item in related) / max(len(related), 1)

    policies = [
        (
            "balanced",
            "收益风险平衡",
            lambda item: experiment_metrics[item]["return_to_risk"] - average_penalty(item),
            lambda item: max(0.0, experiment_metrics[item]["return_to_risk"]) + 0.1,
        ),
        (
            "return_quality",
            "收益质量优先",
            lambda item: experiment_metrics[item]["annualized_return"] - 0.5 * experiment_metrics[item]["annualized_volatility"],
            lambda item: max(0.0, experiment_metrics[item]["annualized_return"]) / max(experiment_metrics[item]["annualized_volatility"], 1e-9) + 0.1,
        ),
        (
            "low_correlation",
            "低相关分散",
            lambda item: -average_penalty(item) + 0.1 * experiment_metrics[item]["return_to_risk"],
            lambda item: 1.0 / (0.2 + average_penalty(item)),
        ),
        (
            "tail_resilience",
            "尾部韧性",
            lambda item: experiment_metrics[item]["tail_mean"] - average_penalty(item) / 100,
            lambda item: 1.0 / max(abs(experiment_metrics[item]["tail_mean"]), 1e-6),
        ),
        (
            "inverse_volatility",
            "逆波动稳健",
            lambda item: -experiment_metrics[item]["annualized_volatility"] - average_penalty(item) / 10,
            lambda item: 1.0 / max(experiment_metrics[item]["annualized_volatility"], 1e-6),
        ),
    ]

    candidates: list[dict[str, Any]] = []
    for policy_id, display_name, order_score, weight_score in policies:
        ordered = sorted(by_id, key=lambda item: (-order_score(item), item))
        selected, relaxations = _select_components(
            ordered,
            pair_by_ids=pair_by_ids,
            max_components=min(max_components, len(ordered)),
            minimum_components=minimum_components,
            max_pair_correlation=max_pair_correlation,
            max_holding_overlap=max_holding_overlap,
        )
        weights = _capped_weights(
            [weight_score(item) for item in selected],
            max_weight,
        )
        component_specs = [
            {
                "params": by_id[experiment_id]["params"],
                "source_experiment_id": experiment_id,
                "source_manifest_hash": manifest_hashes[experiment_id],
                "strategy_id": by_id[experiment_id]["strategy_id"],
            }
            for experiment_id in selected
        ]
        violations: list[str] = list(relaxations)
        holding_evidence_complete = True
        for left_index, left_id in enumerate(selected):
            for right_id in selected[left_index + 1:]:
                pair = pair_by_ids.get(tuple(sorted((left_id, right_id))))
                if pair is None or pair.get("correlation") is None:
                    violations.append("pairwise_return_evidence_incomplete")
                    continue
                if float(pair["correlation"]) > max_pair_correlation:
                    violations.append("pairwise_correlation_cap_exceeded")
                holding = (pair.get("holding_overlap") or {}).get("mean")
                if holding is None:
                    holding_evidence_complete = False
                    violations.append("holding_overlap_evidence_incomplete")
                elif float(holding) > max_holding_overlap:
                    violations.append("holding_overlap_cap_exceeded")
        definition = {
            "component_specs": component_specs,
            "policy": policy_id,
            "source_digest": source_digest,
            "static_weights": weights,
        }
        definition_hash = canonical_sha256(definition)
        candidate_id = "pcand_" + hashlib.sha256(
            (CANDIDATE_MANIFEST_SCHEMA + definition_hash).encode("utf-8")
        ).hexdigest()[:16]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "name": display_name,
                "selection_policy": policy_id,
                "strategy_id": CANDIDATE_STRATEGY_ID,
                "params": {
                    "component_specs": _canonical_json(component_specs),
                    "static_weights": _canonical_json(weights),
                },
                "components": [
                    {
                        "experiment_id": experiment_id,
                        "strategy_id": by_id[experiment_id]["strategy_id"],
                        "weight": weights[index],
                        "metrics": experiment_metrics[experiment_id],
                    }
                    for index, experiment_id in enumerate(selected)
                ],
                "risk_constraints": {
                    "passed": not violations,
                    "violations": sorted(set(violations)),
                    "holding_evidence_complete": holding_evidence_complete,
                },
                "source_manifest": {
                    "schema_version": CANDIDATE_MANIFEST_SCHEMA,
                    "definition_sha256": definition_hash,
                    "source_digest": source_digest,
                    "source_run_manifest_hashes": [
                        manifest_hashes[item] for item in selected
                    ],
                },
                "publication": {
                    "status": "draft",
                    "automatic": False,
                    "eligible_for_experiment": True,
                    "message": "候选不会自动进入策略池、模拟盘或实盘。",
                },
            }
        )

    return {
        "schema_version": CANDIDATE_SET_SCHEMA,
        "analysis_role": "pit_research_candidate_generation",
        "source_digest": source_digest,
        "common_observations": len(common_dates),
        "common_start": common_dates[0],
        "common_end": common_dates[-1],
        "constraints": {
            "max_components": max_components,
            "max_pair_correlation": max_pair_correlation,
            "max_holding_overlap": max_holding_overlap,
            "max_weight": max_weight,
            "tail_fraction": tail_fraction,
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "automation": {
            "mutates_strategy_registry": False,
            "mutates_portfolio": False,
            "submits_experiment": False,
        },
    }
