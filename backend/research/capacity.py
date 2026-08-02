"""JSON-safe transaction-cost, capacity, and portfolio constraint checks."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

_EPSILON = 1e-12


def _safe_number(value: Any) -> float | int | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return int(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        return False
    return math.isfinite(float(value))


def _failure(
    status: str,
    reason: str,
    assumptions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_assumptions = dict(assumptions or {})
    return {
        "status": status,
        "reason": reason,
        "method": resolved_assumptions.get("method"),
        "sample_count": resolved_assumptions.get("sample_count"),
        "seed": resolved_assumptions.get("seed"),
        "limitations": [
            "输入未通过 fail-closed 校验，不得将此结果用于 promotion 决策"
        ],
        "assumptions": resolved_assumptions,
    }


def _performance(
    returns: np.ndarray,
    *,
    periods_per_year: int,
) -> dict[str, Any]:
    standard_deviation = float(returns.std(ddof=1))
    wealth = np.cumprod(1.0 + returns)
    wealth_path = np.concatenate(([1.0], wealth))
    peak = np.maximum.accumulate(wealth_path)
    annualized_return = (
        float(wealth[-1] ** (periods_per_year / len(returns)) - 1.0)
        if wealth[-1] > 0
        else -1.0
    )
    return {
        "annualized_return": _safe_number(annualized_return),
        "sharpe_ratio": (
            _safe_number(
                float(
                    returns.mean()
                    / standard_deviation
                    * math.sqrt(periods_per_year)
                )
            )
            if standard_deviation > _EPSILON
            else None
        ),
        "max_drawdown": _safe_number(
            float(np.min(wealth_path / peak - 1.0))
        ),
        "cumulative_return": _safe_number(float(wealth[-1] - 1.0)),
    }


def cost_stress_scenarios(
    gross_returns: Sequence[float] | pd.Series | np.ndarray,
    turnover: float | Sequence[float] | pd.Series | np.ndarray,
    scenarios: Sequence[Mapping[str, Any]] | None = None,
    *,
    periods_per_year: int = 252,
    min_samples: int = 20,
) -> dict[str, Any]:
    """Apply linear commission, spread, slippage, and impact bps to turnover.

    For period ``t``, ``net_return_t = gross_return_t -
    turnover_t * total_cost_bps / 10_000``. Every cost assumption is echoed.
    """
    default_scenarios: list[Mapping[str, Any]] = [
        {
            "name": "base",
            "commission_bps": 2.0,
            "spread_bps": 5.0,
            "slippage_bps": 3.0,
            "impact_bps": 0.0,
        },
        {
            "name": "double_cost",
            "commission_bps": 4.0,
            "spread_bps": 10.0,
            "slippage_bps": 6.0,
            "impact_bps": 0.0,
        },
        {
            "name": "severe",
            "commission_bps": 6.0,
            "spread_bps": 15.0,
            "slippage_bps": 10.0,
            "impact_bps": 5.0,
        },
    ]
    chosen_scenarios = list(
        default_scenarios if scenarios is None else scenarios
    )
    assumptions = {
        "method": "linear_turnover_cost_stress",
        "periods_per_year": periods_per_year,
        "min_samples": min_samples,
        "cost_formula": "turnover * total_cost_bps / 10000",
        "seed": None,
        "sample_count": (
            len(gross_returns)
            if hasattr(gross_returns, "__len__")
            else None
        ),
    }
    if (
        isinstance(periods_per_year, bool)
        or not isinstance(periods_per_year, (int, np.integer))
        or periods_per_year <= 0
        or isinstance(min_samples, bool)
        or not isinstance(min_samples, (int, np.integer))
        or min_samples < 3
    ):
        return _failure(
            "invalid_input",
            "periods_per_year 或 min_samples 无效",
            assumptions,
        )
    try:
        returns = np.asarray(
            gross_returns.copy(deep=True)
            if isinstance(gross_returns, pd.Series)
            else list(gross_returns),
            dtype=float,
        ).reshape(-1)
    except (TypeError, ValueError):
        return _failure(
            "invalid_input", "gross_returns 必须全部为数字", assumptions
        )
    if len(returns) < min_samples:
        return _failure(
            "insufficient_samples", "成本压力测试样本不足", assumptions
        )
    if not np.isfinite(returns).all() or np.any(returns < -1.0):
        return _failure(
            "invalid_input",
            "收益含 NaN/Infinity 或低于 -100%",
            assumptions,
        )
    if float(returns.std(ddof=1)) <= _EPSILON:
        return _failure(
            "degenerate_input",
            "常数收益无法评估成本后的风险变化",
            assumptions,
        )
    if np.isscalar(turnover):
        if not _is_finite_number(turnover):
            return _failure(
                "invalid_input", "turnover 必须为有限数字", assumptions
            )
        turnover_values = np.full(len(returns), float(turnover), dtype=float)
    else:
        try:
            turnover_values = np.asarray(
                turnover.copy(deep=True)
                if isinstance(turnover, pd.Series)
                else list(turnover),
                dtype=float,
            ).reshape(-1)
        except (TypeError, ValueError):
            return _failure(
                "invalid_input", "turnover 必须为数字", assumptions
            )
    if (
        len(turnover_values) != len(returns)
        or not np.isfinite(turnover_values).all()
        or np.any(turnover_values < 0)
    ):
        return _failure(
            "invalid_input", "turnover 长度不匹配或包含无效值", assumptions
        )
    if not chosen_scenarios:
        return _failure("invalid_input", "至少需要一个成本情景", assumptions)

    scenario_results: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw_scenario in chosen_scenarios:
        name = str(raw_scenario.get("name", "")).strip()
        if not name or name in names:
            return _failure(
                "invalid_input", "成本情景名称不能为空或重复", assumptions
            )
        names.add(name)
        cost_fields = (
            "commission_bps",
            "spread_bps",
            "slippage_bps",
            "impact_bps",
        )
        try:
            costs = {
                field: float(raw_scenario.get(field, 0.0))
                for field in cost_fields
            }
        except (TypeError, ValueError):
            return _failure(
                "invalid_input", f"成本情景 {name} 包含非数字假设",
                assumptions,
            )
        if any(
            not math.isfinite(value) or value < 0
            for value in costs.values()
        ):
            return _failure(
                "invalid_input", f"成本情景 {name} 包含无效 bps",
                assumptions,
            )
        total_bps = sum(costs.values())
        period_costs = turnover_values * total_bps / 10_000.0
        net_returns = returns - period_costs
        if np.any(net_returns < -1.0):
            scenario_results.append(
                {
                    "name": name,
                    "status": "invalid_scenario",
                    "reason": "成本后单期收益低于 -100%",
                    "assumptions": {
                        **costs,
                        "total_cost_bps": _safe_number(total_bps),
                    },
                    "metrics": None,
                }
            )
            continue
        scenario_results.append(
            {
                "name": name,
                "status": "ok",
                "assumptions": {
                    **costs,
                    "total_cost_bps": _safe_number(total_bps),
                },
                "total_cost_return_drag": _safe_number(
                    float(period_costs.sum())
                ),
                "average_period_cost": _safe_number(
                    float(period_costs.mean())
                ),
                "metrics": _performance(
                    net_returns,
                    periods_per_year=periods_per_year,
                ),
            }
        )
    return {
        "status": "ok",
        "method": "linear_turnover_cost_stress",
        "seed": None,
        "gross_metrics": _performance(
            returns, periods_per_year=periods_per_year
        ),
        "scenarios": scenario_results,
        "sample_count": len(returns),
        "limitations": [
            "线性 bps 情景不模拟订单簿反馈或随规模变化的冲击",
            "容量效应应与 estimate_capacity_curve 联合评估",
        ],
        "average_turnover": _safe_number(float(turnover_values.mean())),
        "assumptions": assumptions,
    }


def estimate_capacity_curve(
    capital_levels: Sequence[float],
    *,
    average_daily_volume: float,
    one_way_turnover_rate: float,
    rebalances_per_year: int = 12,
    execution_days: int = 1,
    max_participation_rate: float = 0.10,
    fixed_cost_bps: float = 10.0,
    impact_coefficient_bps: float = 10.0,
    impact_exponent: float = 0.5,
    gross_annual_return: float | None = None,
) -> dict[str, Any]:
    """Estimate partial fills and square-root market impact by capital.

    ``target_trade = capital * turnover``; available liquidity is
    ``ADV * execution_days * max_participation``. Filled notional is their
    minimum. Impact is ``coefficient * participation**exponent`` bps.
    """
    assumptions = {
        "method": "aggregate_adv_square_root_impact",
        "average_daily_volume": average_daily_volume,
        "one_way_turnover_rate": one_way_turnover_rate,
        "rebalances_per_year": rebalances_per_year,
        "execution_days": execution_days,
        "max_participation_rate": max_participation_rate,
        "fixed_cost_bps": fixed_cost_bps,
        "impact_coefficient_bps": impact_coefficient_bps,
        "impact_exponent": impact_exponent,
        "gross_annual_return": gross_annual_return,
        "impact_model": "coefficient_bps * participation_rate ** exponent",
        "seed": None,
        "sample_count": (
            len(capital_levels)
            if hasattr(capital_levels, "__len__")
            else None
        ),
    }
    try:
        capital = sorted(set(float(value) for value in capital_levels))
    except (TypeError, ValueError):
        return _failure(
            "invalid_input", "capital_levels 必须为数字", assumptions
        )
    numeric_inputs = [
        average_daily_volume,
        one_way_turnover_rate,
        max_participation_rate,
        fixed_cost_bps,
        impact_coefficient_bps,
        impact_exponent,
    ]
    if (
        not capital
        or any(not math.isfinite(value) or value <= 0 for value in capital)
        or any(not _is_finite_number(value) for value in numeric_inputs)
        or average_daily_volume <= 0
        or one_way_turnover_rate <= 0
        or not 0 < max_participation_rate <= 1
        or fixed_cost_bps < 0
        or impact_coefficient_bps < 0
        or impact_exponent <= 0
        or isinstance(rebalances_per_year, bool)
        or rebalances_per_year <= 0
        or isinstance(execution_days, bool)
        or execution_days <= 0
        or (
            gross_annual_return is not None
            and not _is_finite_number(gross_annual_return)
        )
    ):
        return _failure(
            "invalid_input", "容量曲线输入假设无效", assumptions
        )

    available_adv = average_daily_volume * execution_days
    maximum_fill = available_adv * max_participation_rate
    full_fill_capacity = maximum_fill / one_way_turnover_rate
    points: list[dict[str, Any]] = []
    for capital_value in capital:
        target = capital_value * one_way_turnover_rate
        filled = min(target, maximum_fill)
        unfilled = target - filled
        fill_ratio = filled / target
        participation = filled / available_adv
        impact_bps = (
            impact_coefficient_bps * participation**impact_exponent
        )
        all_in_bps = fixed_cost_bps + impact_bps
        cost_per_rebalance = filled * all_in_bps / 10_000.0
        annual_cost_drag = (
            cost_per_rebalance / capital_value * rebalances_per_year
        )
        points.append(
            {
                "capital": _safe_number(capital_value),
                "target_trade_notional": _safe_number(target),
                "filled_notional": _safe_number(filled),
                "unfilled_notional": _safe_number(unfilled),
                "fill_ratio": _safe_number(fill_ratio),
                "adv_participation_rate": _safe_number(participation),
                "executed_turnover_rate": _safe_number(
                    filled / capital_value
                ),
                "impact_bps": _safe_number(impact_bps),
                "all_in_cost_bps": _safe_number(all_in_bps),
                "cost_per_rebalance": _safe_number(cost_per_rebalance),
                "annual_cost_drag": _safe_number(annual_cost_drag),
                "estimated_net_annual_return": (
                    _safe_number(gross_annual_return - annual_cost_drag)
                    if gross_annual_return is not None
                    else None
                ),
                "partially_filled": bool(fill_ratio < 1.0 - _EPSILON),
            }
        )
    return {
        "status": "ok",
        "method": "aggregate_adv_square_root_impact",
        "sample_count": len(capital),
        "seed": None,
        "limitations": [
            "使用聚合 ADV，未反映个股间流动性分布和相关性",
            "平方根冲击是情景模型，不是实盘成交保证",
        ],
        "full_fill_capacity": _safe_number(full_fill_capacity),
        "curve": points,
        "assumptions": assumptions,
    }


def check_portfolio_constraints(
    weights: Mapping[str, float],
    industries: Mapping[str, str],
    *,
    turnover_rate: float | None,
    adv_participation: Mapping[str, float] | None = None,
    max_single_weight: float = 0.10,
    max_industry_weight: float = 0.30,
    max_gross_leverage: float = 1.0,
    max_net_exposure: float = 1.0,
    max_turnover_rate: float = 0.50,
    max_adv_participation: float = 0.10,
    max_concentration_hhi: float = 0.20,
) -> dict[str, Any]:
    """Fail-closed checks for concentration, leverage, turnover, and liquidity."""
    limits = {
        "max_single_weight": max_single_weight,
        "max_industry_weight": max_industry_weight,
        "max_gross_leverage": max_gross_leverage,
        "max_net_exposure": max_net_exposure,
        "max_turnover_rate": max_turnover_rate,
        "max_adv_participation": max_adv_participation,
        "max_concentration_hhi": max_concentration_hhi,
    }
    assumptions = {
        "method": "deterministic_portfolio_constraint_check",
        "sample_count": len(weights),
        "seed": None,
        "limits": limits,
    }
    if not weights:
        return _failure(
            "insufficient_samples", "组合至少需要一个持仓",
            assumptions,
        )
    try:
        clean_weights = {
            str(code): float(weight) for code, weight in weights.items()
        }
    except (TypeError, ValueError):
        return _failure(
            "invalid_input", "持仓权重必须为数字", assumptions
        )
    if (
        len(clean_weights) != len(weights)
        or not all(math.isfinite(value) for value in clean_weights.values())
        or not all(
            _is_finite_number(value) and float(value) >= 0
            for value in limits.values()
        )
        or turnover_rate is None
        or not _is_finite_number(turnover_rate)
        or turnover_rate < 0
    ):
        return _failure(
            "invalid_input", "组合权重、限制或换手率无效",
            assumptions,
        )
    clean_industries = {
        str(code): str(industry)
        for code, industry in industries.items()
        if str(industry).strip()
    }
    missing_industries = sorted(
        set(clean_weights).difference(clean_industries)
    )
    if adv_participation is None:
        return _failure(
            "invalid_input", "缺少 ADV 参与率，流动性约束必须 fail-closed",
            assumptions,
        )
    try:
        participation = {
            str(code): float(value)
            for code, value in adv_participation.items()
        }
    except (TypeError, ValueError):
        return _failure(
            "invalid_input", "ADV 参与率必须为数字", assumptions
        )
    missing_liquidity = sorted(
        set(clean_weights).difference(participation)
    )
    if (
        missing_industries
        or missing_liquidity
        or any(
            not math.isfinite(value) or value < 0
            for value in participation.values()
        )
    ):
        return {
            "status": "invalid_input",
            "reason": "行业或流动性数据缺失/无效",
            "method": "deterministic_portfolio_constraint_check",
            "sample_count": len(clean_weights),
            "seed": None,
            "limitations": [
                "行业或流动性输入不完整，约束检查不得用于 promotion"
            ],
            "missing_industries": missing_industries,
            "missing_liquidity": missing_liquidity,
            "limits": limits,
        }

    absolute_weights = {
        code: abs(value) for code, value in clean_weights.items()
    }
    gross = sum(absolute_weights.values())
    if gross <= _EPSILON:
        return _failure(
            "degenerate_input",
            "组合总绝对权重为 0，无法检查集中度",
            assumptions,
        )
    net = abs(sum(clean_weights.values()))
    hhi = (
        sum((value / gross) ** 2 for value in absolute_weights.values())
        if gross > _EPSILON
        else 0.0
    )
    industry_exposure: dict[str, float] = {}
    for code, value in absolute_weights.items():
        industry = clean_industries[code]
        industry_exposure[industry] = (
            industry_exposure.get(industry, 0.0) + value
        )
    maximum_single = max(absolute_weights.values())
    maximum_industry = max(industry_exposure.values())
    maximum_participation = max(
        participation[code] for code in clean_weights
    )

    observations = {
        "gross_leverage": gross,
        "net_exposure": net,
        "max_single_weight": maximum_single,
        "max_industry_weight": maximum_industry,
        "concentration_hhi": hhi,
        "turnover_rate": float(turnover_rate),
        "max_adv_participation": maximum_participation,
    }
    check_specs = [
        ("gross_leverage", gross, max_gross_leverage),
        ("net_exposure", net, max_net_exposure),
        ("single_weight", maximum_single, max_single_weight),
        ("industry_weight", maximum_industry, max_industry_weight),
        ("concentration_hhi", hhi, max_concentration_hhi),
        ("turnover_rate", float(turnover_rate), max_turnover_rate),
        (
            "adv_participation",
            maximum_participation,
            max_adv_participation,
        ),
    ]
    checks = [
        {
            "constraint": name,
            "observed": _safe_number(observed),
            "limit": _safe_number(limit),
            "passed": bool(observed <= limit + _EPSILON),
        }
        for name, observed, limit in check_specs
    ]
    breaches = [
        item["constraint"] for item in checks if not item["passed"]
    ]
    return {
        "status": "ok",
        "method": "deterministic_portfolio_constraint_check",
        "sample_count": len(clean_weights),
        "seed": None,
        "limitations": [
            "约束检查使用点估计，不替代协方差或尾部风险模型",
            "ADV 参与率必须由上层使用同一交易假设计算",
        ],
        "passed": not breaches,
        "breaches": breaches,
        "checks": checks,
        "observations": {
            name: _safe_number(value)
            for name, value in observations.items()
        },
        "industry_exposure": {
            industry: _safe_number(value)
            for industry, value in sorted(industry_exposure.items())
        },
        "weights": {
            code: _safe_number(value)
            for code, value in sorted(clean_weights.items())
        },
        "adv_participation": {
            code: _safe_number(participation[code])
            for code in sorted(clean_weights)
        },
        "limits": {
            name: _safe_number(value) for name, value in limits.items()
        },
    }


# Short service-layer aliases.
capacity_curve = estimate_capacity_curve
portfolio_constraint_check = check_portfolio_constraints
