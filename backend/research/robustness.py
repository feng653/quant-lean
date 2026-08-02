"""JSON-safe statistical robustness tools for strategy research.

The module intentionally has no API, database, or job-queue dependencies.
Invalid, non-finite, constant, or undersized samples fail closed with an
explicit status instead of returning a persuasive-looking statistic.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from statistics import NormalDist
from typing import Any, Literal

import numpy as np
import pandas as pd

_EPSILON = 1e-12
_EULER_GAMMA = 0.5772156649015329
_MAX_CSCV_COMBINATIONS = 10_000


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
    *,
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


def _returns_array(
    returns: Sequence[float] | pd.Series | np.ndarray,
    *,
    min_samples: int,
) -> tuple[np.ndarray | None, str | None, str | None]:
    try:
        values = np.asarray(
            returns.copy(deep=True)
            if isinstance(returns, pd.Series)
            else list(returns),
            dtype=float,
        ).reshape(-1)
    except (TypeError, ValueError):
        return None, "invalid_input", "收益序列必须全部为数字"
    if len(values) < min_samples:
        return None, "insufficient_samples", (
            f"至少需要 {min_samples} 个收益观测，当前为 {len(values)}"
        )
    if not np.isfinite(values).all():
        return None, "invalid_input", "收益序列包含 NaN 或 Infinity"
    if np.any(values < -1.0):
        return None, "invalid_input", "单期收益不能小于 -100%"
    if float(values.std(ddof=1)) <= _EPSILON:
        return None, "degenerate_input", "常数收益序列无法估计风险统计"
    return values, None, None


def _performance_metrics(
    returns: np.ndarray,
    *,
    periods_per_year: int,
    risk_free_rate: float,
) -> dict[str, float | None]:
    wealth = np.cumprod(1.0 + returns)
    annualized_return = (
        float(wealth[-1] ** (periods_per_year / len(returns)) - 1.0)
        if wealth[-1] > 0
        else -1.0
    )
    standard_deviation = float(returns.std(ddof=1))
    sharpe = (
        float(
            (returns.mean() - risk_free_rate / periods_per_year)
            / standard_deviation
            * math.sqrt(periods_per_year)
        )
        if standard_deviation > _EPSILON
        else None
    )
    wealth_with_origin = np.concatenate(([1.0], wealth))
    running_peak = np.maximum.accumulate(wealth_with_origin)
    max_drawdown = float(
        np.min(wealth_with_origin / running_peak - 1.0)
    )
    return {
        "annualized_return": _safe_number(annualized_return),
        "sharpe_ratio": _safe_number(sharpe),
        "max_drawdown": _safe_number(max_drawdown),
    }


def _moving_block_sample(
    values: np.ndarray,
    *,
    block_size: int,
    rng: np.random.Generator,
) -> np.ndarray:
    sample_parts: list[np.ndarray] = []
    maximum_start = len(values) - block_size
    while sum(len(part) for part in sample_parts) < len(values):
        start = int(rng.integers(0, maximum_start + 1))
        sample_parts.append(values[start : start + block_size])
    return np.concatenate(sample_parts)[: len(values)]


def _stationary_block_sample(
    values: np.ndarray,
    *,
    block_size: int,
    rng: np.random.Generator,
) -> np.ndarray:
    restart_probability = 1.0 / block_size
    indices = np.empty(len(values), dtype=int)
    current = int(rng.integers(0, len(values)))
    for position in range(len(values)):
        if position == 0 or rng.random() < restart_probability:
            current = int(rng.integers(0, len(values)))
        else:
            current = (current + 1) % len(values)
        indices[position] = current
    return values[indices]


def block_bootstrap_performance(
    returns: Sequence[float] | pd.Series | np.ndarray,
    *,
    n_bootstrap: int = 1_000,
    confidence_level: float = 0.95,
    block_size: int | None = None,
    method: Literal["moving", "stationary"] = "moving",
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
    seed: int = 0,
    min_samples: int = 20,
) -> dict[str, Any]:
    """Block-bootstrap annual return, Sharpe, and maximum drawdown.

    Moving blocks sample contiguous windows with replacement. Stationary
    bootstrap uses geometrically distributed block lengths with restart
    probability ``1 / block_size``. IID resampling is deliberately not the
    default because it destroys serial dependence in strategy returns.
    """
    assumptions = {
        "n_bootstrap": n_bootstrap,
        "confidence_level": confidence_level,
        "block_size": block_size,
        "method": method,
        "periods_per_year": periods_per_year,
        "risk_free_rate": risk_free_rate,
        "seed": seed,
        "min_samples": min_samples,
        "sample_count": (
            len(returns) if hasattr(returns, "__len__") else None
        ),
    }
    if method not in {"moving", "stationary"}:
        return _failure(
            "invalid_input", "method 必须是 moving 或 stationary",
            assumptions=assumptions,
        )
    if (
        isinstance(n_bootstrap, bool)
        or not isinstance(n_bootstrap, int)
        or n_bootstrap < 100
        or n_bootstrap > 100_000
    ):
        return _failure(
            "invalid_input", "n_bootstrap 必须在 [100, 100000] 内",
            assumptions=assumptions,
        )
    if (
        isinstance(seed, bool)
        or not isinstance(seed, (int, np.integer))
        or seed < 0
        or isinstance(periods_per_year, bool)
        or not isinstance(periods_per_year, (int, np.integer))
        or periods_per_year <= 0
        or isinstance(min_samples, bool)
        or not isinstance(min_samples, (int, np.integer))
        or min_samples < 3
    ):
        return _failure(
            "invalid_input",
            "seed、periods_per_year 或 min_samples 无效",
            assumptions=assumptions,
        )
    if (
        not _is_finite_number(confidence_level)
        or not 0 < float(confidence_level) < 1
    ):
        return _failure(
            "invalid_input", "confidence_level 必须在 (0, 1) 内",
            assumptions=assumptions,
        )
    if not _is_finite_number(risk_free_rate):
        return _failure(
            "invalid_input", "年化周期和无风险利率无效",
            assumptions=assumptions,
        )
    values, status, reason = _returns_array(
        returns, min_samples=min_samples
    )
    if values is None:
        return _failure(status or "invalid_input", reason or "", assumptions=assumptions)
    resolved_block_size = (
        max(2, int(round(len(values) ** (1 / 3))))
        if block_size is None
        else block_size
    )
    assumptions["block_size"] = resolved_block_size
    if (
        isinstance(resolved_block_size, bool)
        or not isinstance(resolved_block_size, int)
        or not 2 <= resolved_block_size <= len(values)
    ):
        return _failure(
            "invalid_input",
            "block_size 必须是 [2, 样本长度] 内的整数",
            assumptions=assumptions,
        )

    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {
        "annualized_return": [],
        "sharpe_ratio": [],
        "max_drawdown": [],
    }
    sampler = (
        _moving_block_sample
        if method == "moving"
        else _stationary_block_sample
    )
    for _ in range(n_bootstrap):
        sampled = sampler(
            values,
            block_size=resolved_block_size,
            rng=rng,
        )
        metrics = _performance_metrics(
            sampled,
            periods_per_year=periods_per_year,
            risk_free_rate=risk_free_rate,
        )
        for name, value in metrics.items():
            if value is not None:
                samples[name].append(float(value))

    tail = (1.0 - confidence_level) / 2.0
    intervals: dict[str, Any] = {}
    for name, estimates in samples.items():
        if len(estimates) < max(30, int(n_bootstrap * 0.8)):
            intervals[name] = {
                "lower": None,
                "upper": None,
                "valid_bootstrap_samples": len(estimates),
            }
            continue
        intervals[name] = {
            "lower": _safe_number(float(np.quantile(estimates, tail))),
            "upper": _safe_number(float(np.quantile(estimates, 1.0 - tail))),
            "valid_bootstrap_samples": len(estimates),
        }
    return {
        "status": "ok",
        "method": f"{method}_block_bootstrap",
        "seed": seed,
        "point_estimate": _performance_metrics(
            values,
            periods_per_year=periods_per_year,
            risk_free_rate=risk_free_rate,
        ),
        "confidence_intervals": intervals,
        "sample_count": len(values),
        "limitations": [
            "置信区间依赖区块长度与历史样本的弱平稳性",
            "百分位 bootstrap 区间不保证包含原样本点估计",
        ],
        "assumptions": assumptions,
    }


def _return_moments(values: np.ndarray) -> tuple[float, float]:
    centered = values - float(values.mean())
    sigma = float(np.sqrt(np.mean(np.square(centered))))
    if sigma <= _EPSILON:
        return float("nan"), float("nan")
    skewness = float(np.mean(centered**3) / sigma**3)
    kurtosis = float(np.mean(centered**4) / sigma**4)
    return skewness, kurtosis


def _psr_probability(
    values: np.ndarray,
    *,
    benchmark_per_period: float,
) -> tuple[float | None, dict[str, Any]]:
    standard_deviation = float(values.std(ddof=1))
    observed = float(values.mean() / standard_deviation)
    skewness, kurtosis = _return_moments(values)
    denominator_squared = (
        1.0
        - skewness * observed
        + ((kurtosis - 1.0) / 4.0) * observed**2
    )
    if (
        not math.isfinite(denominator_squared)
        or denominator_squared <= _EPSILON
    ):
        return None, {
            "observed_sharpe_per_period": _safe_number(observed),
            "skewness": _safe_number(skewness),
            "kurtosis": _safe_number(kurtosis),
            "z_score": None,
        }
    z_score = (
        (observed - benchmark_per_period)
        * math.sqrt(len(values) - 1)
        / math.sqrt(denominator_squared)
    )
    probability = NormalDist().cdf(z_score)
    return _safe_number(probability), {
        "observed_sharpe_per_period": _safe_number(observed),
        "skewness": _safe_number(skewness),
        "kurtosis": _safe_number(kurtosis),
        "z_score": _safe_number(z_score),
    }


def probabilistic_sharpe_ratio(
    returns: Sequence[float] | pd.Series | np.ndarray,
    *,
    benchmark_sharpe: float = 0.0,
    periods_per_year: int = 252,
    min_samples: int = 20,
) -> dict[str, Any]:
    """Compute PSR with skewness and non-excess kurtosis adjustment.

    ``PSR = Φ((SR-SR*)√(n-1) /
    √(1-skew·SR+((kurtosis-1)/4)·SR²))``.
    Sharpe inputs shown to callers are annualized; the finite-sample formula
    operates on per-period Sharpe.
    """
    assumptions = {
        "method": "probabilistic_sharpe_ratio",
        "benchmark_sharpe": benchmark_sharpe,
        "periods_per_year": periods_per_year,
        "min_samples": min_samples,
        "seed": None,
        "sample_count": (
            len(returns) if hasattr(returns, "__len__") else None
        ),
    }
    if (
        isinstance(periods_per_year, bool)
        or not isinstance(periods_per_year, (int, np.integer))
        or periods_per_year <= 0
        or isinstance(min_samples, bool)
        or not isinstance(min_samples, (int, np.integer))
        or min_samples < 3
        or not _is_finite_number(benchmark_sharpe)
    ):
        return _failure(
            "invalid_input", "Sharpe 基准或年化周期无效",
            assumptions=assumptions,
        )
    values, status, reason = _returns_array(
        returns, min_samples=min_samples
    )
    if values is None:
        return _failure(status or "invalid_input", reason or "", assumptions=assumptions)
    probability, diagnostics = _psr_probability(
        values,
        benchmark_per_period=benchmark_sharpe / math.sqrt(periods_per_year),
    )
    if probability is None:
        return _failure(
            "degenerate_input",
            "偏度/峰度调整后的 Sharpe 方差无效",
            assumptions=assumptions,
        )
    return {
        "status": "ok",
        "method": "probabilistic_sharpe_ratio",
        "seed": None,
        "probabilistic_sharpe_ratio": probability,
        "observed_sharpe": _safe_number(
            diagnostics["observed_sharpe_per_period"]
            * math.sqrt(periods_per_year)
        ),
        "diagnostics": diagnostics,
        "sample_count": len(values),
        "limitations": [
            "有限样本校正依赖样本偏度和峰度估计",
            "不校正未披露的多次试验；多试验应使用 DSR",
        ],
        "assumptions": assumptions,
    }


def deflated_sharpe_ratio(
    returns: Sequence[float] | pd.Series | np.ndarray,
    trial_sharpes: Sequence[float],
    *,
    benchmark_sharpe: float = 0.0,
    periods_per_year: int = 252,
    min_samples: int = 20,
) -> dict[str, Any]:
    """Compute Deflated Sharpe Ratio for multiple tried strategies.

    The expected maximum under multiple trials is approximated by
    ``mean(SR) + std(SR) * ((1-γ)Z(1-1/N) + γZ(1-1/(N·e)))``.
    The larger of this threshold and ``benchmark_sharpe`` is passed into the
    skew/kurtosis-adjusted PSR formula.
    """
    assumptions = {
        "method": "deflated_sharpe_ratio",
        "benchmark_sharpe": benchmark_sharpe,
        "periods_per_year": periods_per_year,
        "min_samples": min_samples,
        "number_of_trials": len(trial_sharpes),
        "seed": None,
        "sample_count": (
            len(returns) if hasattr(returns, "__len__") else None
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
            assumptions=assumptions,
        )
    values, status, reason = _returns_array(
        returns, min_samples=min_samples
    )
    if values is None:
        return _failure(status or "invalid_input", reason or "", assumptions=assumptions)
    try:
        trials = np.asarray(list(trial_sharpes), dtype=float)
    except (TypeError, ValueError):
        return _failure(
            "invalid_input", "trial_sharpes 必须全部为数字",
            assumptions=assumptions,
        )
    if (
        len(trials) < 2
        or not np.isfinite(trials).all()
        or float(trials.std(ddof=1)) <= _EPSILON
        or not _is_finite_number(benchmark_sharpe)
    ):
        return _failure(
            "degenerate_input",
            "DSR 至少需要两个有横截面差异的有限 trial Sharpe",
            assumptions=assumptions,
        )
    count = len(trials)
    normal = NormalDist()
    expected_maximum = float(
        trials.mean()
        + trials.std(ddof=1)
        * (
            (1.0 - _EULER_GAMMA)
            * normal.inv_cdf(1.0 - 1.0 / count)
            + _EULER_GAMMA
            * normal.inv_cdf(1.0 - 1.0 / (count * math.e))
        )
    )
    deflated_benchmark = max(float(benchmark_sharpe), expected_maximum)
    probability, diagnostics = _psr_probability(
        values,
        benchmark_per_period=(
            deflated_benchmark / math.sqrt(periods_per_year)
        ),
    )
    if probability is None:
        return _failure(
            "degenerate_input",
            "偏度/峰度调整后的 Sharpe 方差无效",
            assumptions=assumptions,
        )
    return {
        "status": "ok",
        "method": "deflated_sharpe_ratio",
        "seed": None,
        "deflated_sharpe_ratio": probability,
        "observed_sharpe": _safe_number(
            diagnostics["observed_sharpe_per_period"]
            * math.sqrt(periods_per_year)
        ),
        "deflated_benchmark_sharpe": _safe_number(deflated_benchmark),
        "expected_max_trial_sharpe": _safe_number(expected_maximum),
        "trial_sharpe_mean": _safe_number(float(trials.mean())),
        "trial_sharpe_std": _safe_number(float(trials.std(ddof=1))),
        "diagnostics": diagnostics,
        "sample_count": len(values),
        "limitations": [
            "trial_sharpes 必须覆盖所有实际尝试，漏报会高估显著性",
            "预期最大 Sharpe 使用渐近近似",
        ],
        "assumptions": assumptions,
    }


def multiple_testing_correction(
    p_values: Mapping[str, float] | Sequence[float],
    *,
    method: Literal["bonferroni", "holm", "bh"] = "bh",
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Adjust p-values by Bonferroni, Holm step-down, or BH-FDR."""
    assumptions = {
        "method": method,
        "alpha": alpha,
        "seed": None,
        "sample_count": (
            len(p_values) if hasattr(p_values, "__len__") else None
        ),
    }
    if (
        method not in {"bonferroni", "holm", "bh"}
        or not _is_finite_number(alpha)
        or not 0 < float(alpha) < 1
    ):
        return _failure(
            "invalid_input",
            "method 或 alpha 无效",
            assumptions=assumptions,
        )
    if isinstance(p_values, Mapping):
        items = sorted(
            ((str(name), value) for name, value in p_values.items()),
            key=lambda item: item[0],
        )
        if len(items) != len({name for name, _ in items}):
            return _failure(
                "invalid_input", "检验名称转为字符串后不能重复",
                assumptions=assumptions,
            )
    else:
        items = [(str(index), value) for index, value in enumerate(p_values)]
    if not items:
        return _failure(
            "insufficient_samples", "至少需要一个 p-value",
            assumptions=assumptions,
        )
    try:
        raw = np.asarray([value for _, value in items], dtype=float)
    except (TypeError, ValueError):
        return _failure(
            "invalid_input", "p-value 必须为数字",
            assumptions=assumptions,
        )
    if (
        not np.isfinite(raw).all()
        or np.any(raw < 0)
        or np.any(raw > 1)
    ):
        return _failure(
            "invalid_input", "p-value 必须是 [0, 1] 内有限值",
            assumptions=assumptions,
        )

    count = len(raw)
    adjusted = np.empty(count, dtype=float)
    if method == "bonferroni":
        adjusted = np.minimum(raw * count, 1.0)
    else:
        order = sorted(range(count), key=lambda index: (raw[index], items[index][0]))
        ordered = raw[order]
        if method == "holm":
            ordered_adjusted = np.maximum.accumulate(
                ordered * np.arange(count, 0, -1)
            )
        else:
            candidate = ordered * count / np.arange(1, count + 1)
            ordered_adjusted = np.minimum.accumulate(candidate[::-1])[::-1]
        ordered_adjusted = np.minimum(ordered_adjusted, 1.0)
        for position, original_index in enumerate(order):
            adjusted[original_index] = ordered_adjusted[position]
    hypotheses = [
        {
            "name": name,
            "p_value": _safe_number(raw[index]),
            "adjusted_p_value": _safe_number(adjusted[index]),
            "rejected": bool(adjusted[index] <= alpha),
        }
        for index, (name, _) in enumerate(items)
    ]
    return {
        "status": "ok",
        "method": method,
        "sample_count": count,
        "seed": None,
        "limitations": [
            (
                "BH-FDR 的严格控制依赖独立或正相关检验"
                if method == "bh"
                else "校正只处理已提供的检验，未披露试验不在控制范围内"
            )
        ],
        "alpha": float(alpha),
        "hypotheses": hypotheses,
        "rejected_count": sum(item["rejected"] for item in hypotheses),
    }


def _trial_sharpes(returns: np.ndarray) -> np.ndarray | None:
    standard_deviations = returns.std(axis=1, ddof=1)
    if (
        not np.isfinite(standard_deviations).all()
        or np.any(standard_deviations <= _EPSILON)
    ):
        return None
    return returns.mean(axis=1) / standard_deviations


def _sample_combinations(
    n_slices: int,
    *,
    max_combinations: int,
    seed: int,
) -> tuple[list[tuple[int, ...]], int, bool]:
    half = n_slices // 2
    total = math.comb(n_slices, half)
    if total <= max_combinations:
        return list(itertools.combinations(range(n_slices), half)), total, False
    rng = np.random.default_rng(seed)
    sampled: set[tuple[int, ...]] = set()
    while len(sampled) < max_combinations:
        combination = tuple(
            sorted(
                int(value)
                for value in rng.choice(
                    n_slices, size=half, replace=False
                )
            )
        )
        sampled.add(combination)
    return sorted(sampled), total, True


def cscv_probability_of_backtest_overfitting(
    trial_period_returns: pd.DataFrame | np.ndarray,
    *,
    n_slices: int = 8,
    max_combinations: int = 2_000,
    seed: int = 0,
    min_periods_per_half: int = 10,
) -> dict[str, Any]:
    """Estimate PBO with Combinatorially Symmetric Cross-Validation.

    Each split selects the best in-sample trial by Sharpe, ranks that exact
    trial out of sample, and records ``logit(rank/(N+1))``. PBO is the share
    of valid logits at or below zero. In- and out-of-sample periods are always
    disjoint contiguous-slice unions.
    """
    assumptions = {
        "method": "cscv_pbo",
        "n_slices": n_slices,
        "max_combinations": max_combinations,
        "seed": seed,
        "min_periods_per_half": min_periods_per_half,
        "selection_metric": "per_period_sharpe",
        "sample_count": None,
    }
    if (
        isinstance(n_slices, bool)
        or not isinstance(n_slices, int)
        or n_slices < 4
        or n_slices % 2
        or isinstance(max_combinations, bool)
        or not isinstance(max_combinations, int)
        or not 1 <= max_combinations <= _MAX_CSCV_COMBINATIONS
        or isinstance(seed, bool)
        or not isinstance(seed, (int, np.integer))
        or seed < 0
        or isinstance(min_periods_per_half, bool)
        or not isinstance(min_periods_per_half, int)
        or min_periods_per_half < 2
    ):
        return _failure(
            "invalid_input",
            (
                "n_slices 必须是 >=4 的偶数；max_combinations 必须在 "
                f"[1, {_MAX_CSCV_COMBINATIONS}]；seed/最小样本必须有效"
            ),
            assumptions=assumptions,
        )
    if isinstance(trial_period_returns, pd.DataFrame):
        source = trial_period_returns.copy(deep=True)
        trial_names = [str(index) for index in source.index]
        if len(trial_names) != len(set(trial_names)):
            return _failure(
                "invalid_input",
                "trial 名称转为字符串后不能重复",
                assumptions=assumptions,
            )
        try:
            values = source.to_numpy(dtype=float)
        except (TypeError, ValueError):
            return _failure(
                "invalid_input", "CSCV 收益必须全部为数字",
                assumptions=assumptions,
            )
    else:
        try:
            values = np.asarray(trial_period_returns, dtype=float).copy()
        except (TypeError, ValueError):
            return _failure(
                "invalid_input", "CSCV 收益必须全部为数字",
                assumptions=assumptions,
            )
        trial_names = [str(index) for index in range(values.shape[0] if values.ndim else 0)]
    if values.ndim != 2 or values.shape[0] < 2:
        return _failure(
            "insufficient_samples", "CSCV 至少需要两个 trial",
            assumptions=assumptions,
        )
    assumptions["sample_count"] = {
        "trials": int(values.shape[0]),
        "periods": int(values.shape[1]),
    }
    if not np.isfinite(values).all() or np.any(values < -1.0):
        return _failure(
            "invalid_input", "CSCV 收益包含 NaN/Infinity 或低于 -100%",
            assumptions=assumptions,
        )
    if values.shape[1] < max(n_slices, min_periods_per_half * 2):
        return _failure(
            "insufficient_samples", "CSCV 时间区间不足",
            assumptions=assumptions,
        )
    if _trial_sharpes(values) is None:
        return _failure(
            "degenerate_input", "至少一个 trial 是常数收益序列",
            assumptions=assumptions,
        )

    period_slices = [
        np.asarray(part, dtype=int)
        for part in np.array_split(np.arange(values.shape[1]), n_slices)
    ]
    combinations, total_combinations, sampled = _sample_combinations(
        n_slices,
        max_combinations=max_combinations,
        seed=seed,
    )
    diagnostics: list[dict[str, Any]] = []
    logits: list[float] = []
    all_slices = set(range(n_slices))
    for in_sample_slices in combinations:
        out_sample_slices = tuple(
            sorted(all_slices.difference(in_sample_slices))
        )
        in_indices = np.concatenate(
            [period_slices[index] for index in in_sample_slices]
        )
        out_indices = np.concatenate(
            [period_slices[index] for index in out_sample_slices]
        )
        in_sharpes = _trial_sharpes(values[:, in_indices])
        out_sharpes = _trial_sharpes(values[:, out_indices])
        if in_sharpes is None or out_sharpes is None:
            continue
        selected = int(np.argmax(in_sharpes))
        selected_out = out_sharpes[selected]
        lower = int(np.sum(out_sharpes < selected_out))
        equal = int(np.sum(np.isclose(out_sharpes, selected_out)))
        average_rank = lower + 1.0 + (equal - 1.0) / 2.0
        percentile = average_rank / (len(out_sharpes) + 1.0)
        logit = math.log(percentile / (1.0 - percentile))
        logits.append(logit)
        diagnostics.append(
            {
                "in_sample_slices": list(in_sample_slices),
                "out_of_sample_slices": list(out_sample_slices),
                "overlap_count": 0,
                "selected_trial": trial_names[selected],
                "selected_in_sample_sharpe": _safe_number(in_sharpes[selected]),
                "selected_out_of_sample_sharpe": _safe_number(selected_out),
                "out_of_sample_rank_percentile": _safe_number(percentile),
                "logit": _safe_number(logit),
            }
        )
    if not logits:
        return _failure(
            "degenerate_input",
            "所有 CSCV 切分均出现常数子样本，无法估计 PBO",
            assumptions=assumptions,
        )
    return {
        "status": "ok",
        "method": "cscv_pbo",
        "sample_count": {
            "trials": int(values.shape[0]),
            "periods": int(values.shape[1]),
        },
        "seed": seed,
        "limitations": [
            "PBO 对时间切片数和候选 trial 集合敏感",
            "确定抽样仅在组合超限时近似完整 CSCV",
        ],
        "probability_of_backtest_overfitting": _safe_number(
            float(np.mean(np.asarray(logits) <= 0.0))
        ),
        "mean_logit": _safe_number(float(np.mean(logits))),
        "valid_combinations": len(logits),
        "total_possible_combinations": total_combinations,
        "deterministically_sampled": sampled,
        "splits": diagnostics,
        "assumptions": assumptions,
    }


def parameter_stability_region(
    results: pd.DataFrame,
    parameter_columns: Sequence[str],
    *,
    metric_column: str = "score",
    maximize: bool = True,
    top_fraction: float = 0.2,
    min_neighbors: int = 2,
    plateau_threshold: float = 0.65,
) -> dict[str, Any]:
    """Assess whether the best grid point lies on a local performance plateau.

    Immediate grid neighbors differ by at most one sorted value step on every
    parameter. ``plateau_score = 0.5·gap_quality + 0.3·top_neighbor_ratio
    + 0.2·coverage``. Gap quality scales degradation by the larger of the
    absolute best metric and the observed range. An isolated optimum always
    scores zero.
    """
    assumptions = {
        "method": "parameter_stability_region",
        "metric_column": metric_column,
        "maximize": maximize,
        "top_fraction": top_fraction,
        "min_neighbors": min_neighbors,
        "plateau_threshold": plateau_threshold,
        "neighbor_definition": "one_grid_step_per_parameter",
        "seed": None,
        "sample_count": len(results),
    }
    if (
        not parameter_columns
        or metric_column not in results.columns
        or any(column not in results.columns for column in parameter_columns)
    ):
        return _failure(
            "invalid_input", "参数列或指标列不存在",
            assumptions=assumptions,
        )
    if (
        not _is_finite_number(top_fraction)
        or not 0 < float(top_fraction) <= 1
        or isinstance(min_neighbors, bool)
        or not isinstance(min_neighbors, int)
        or min_neighbors < 1
        or not _is_finite_number(plateau_threshold)
        or not 0 <= float(plateau_threshold) <= 1
    ):
        return _failure(
            "invalid_input", "稳定区阈值参数无效",
            assumptions=assumptions,
        )
    frame = results[[*parameter_columns, metric_column]].copy(deep=True)
    for column in [*parameter_columns, metric_column]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if len(frame) < 2:
        return _failure(
            "insufficient_samples", "参数稳定性至少需要两个点",
            assumptions=assumptions,
        )
    if (
        frame.isna().any().any()
        or not np.isfinite(frame.to_numpy(dtype=float)).all()
    ):
        return _failure(
            "invalid_input", "参数或指标包含 NaN/Infinity",
            assumptions=assumptions,
        )
    if frame.duplicated(list(parameter_columns)).any():
        return _failure(
            "invalid_input", "参数网格存在重复坐标",
            assumptions=assumptions,
        )
    if float(frame[metric_column].std(ddof=0)) <= _EPSILON:
        return _failure(
            "degenerate_input",
            "指标为常数，无法识别稳定区或最优点",
            assumptions=assumptions,
        )
    frame = frame.sort_values(
        list(parameter_columns), kind="stable"
    ).reset_index(drop=True)
    metric = frame[metric_column]
    best_index = int(metric.idxmax() if maximize else metric.idxmin())
    best = frame.loc[best_index]
    value_positions: dict[str, dict[float, int]] = {}
    for column in parameter_columns:
        unique = sorted(float(value) for value in frame[column].unique())
        value_positions[column] = {
            value: position for position, value in enumerate(unique)
        }

    neighbor_indices: list[int] = []
    for index, candidate in frame.iterrows():
        if index == best_index:
            continue
        if all(
            abs(
                value_positions[column][float(candidate[column])]
                - value_positions[column][float(best[column])]
            )
            <= 1
            for column in parameter_columns
        ):
            neighbor_indices.append(int(index))
    boundary_parameters = [
        column
        for column in parameter_columns
        if len(value_positions[column]) > 1
        and (
            float(best[column]) == min(value_positions[column])
            or float(best[column]) == max(value_positions[column])
        )
    ]
    best_parameters = {
        column: _safe_number(best[column]) for column in parameter_columns
    }
    if not neighbor_indices:
        return {
            "status": "ok",
            "method": "parameter_stability_region",
            "sample_count": len(frame),
            "seed": None,
            "limitations": [
                "稳定性仅针对已评估参数网格，不能外推到未扫描区域"
            ],
            "best_parameters": best_parameters,
            "best_metric": _safe_number(best[metric_column]),
            "neighbors": [],
            "plateau_score": 0.0,
            "is_stable": False,
            "warnings": [
                "孤立最优点没有邻域证据，不能称为稳定参数区",
                *(
                    [f"最优点位于参数边界: {', '.join(boundary_parameters)}"]
                    if boundary_parameters
                    else []
                ),
            ],
            "assumptions": assumptions,
        }

    neighbors = frame.loc[neighbor_indices].copy()
    metric_range = float(metric.max() - metric.min())
    gap_scale = max(
        abs(float(best[metric_column])),
        metric_range,
        _EPSILON,
    )
    median_gap = float(
        np.median(
            np.abs(
                neighbors[metric_column].to_numpy(dtype=float)
                - float(best[metric_column])
            )
        )
    )
    gap_quality = max(0.0, min(1.0, 1.0 - median_gap / gap_scale))
    threshold = float(
        metric.quantile(1.0 - top_fraction if maximize else top_fraction)
    )
    if maximize:
        top_ratio = float((neighbors[metric_column] >= threshold).mean())
    else:
        top_ratio = float((neighbors[metric_column] <= threshold).mean())
    coverage = min(1.0, len(neighbors) / min_neighbors)
    plateau_score = (
        0.5 * gap_quality + 0.3 * top_ratio + 0.2 * coverage
    )
    neighbor_records: list[dict[str, Any]] = []
    for _, neighbor in neighbors.iterrows():
        neighbor_records.append(
            {
                "parameters": {
                    column: _safe_number(neighbor[column])
                    for column in parameter_columns
                },
                "metric": _safe_number(neighbor[metric_column]),
                "absolute_gap_from_best": _safe_number(
                    abs(
                        float(neighbor[metric_column])
                        - float(best[metric_column])
                    )
                ),
            }
        )
    warnings: list[str] = []
    if boundary_parameters:
        warnings.append(
            f"最优点位于参数边界: {', '.join(boundary_parameters)}"
        )
    if plateau_score < plateau_threshold:
        warnings.append("邻域表现不足，最优点更像单点峰值而非稳定平台")
    return {
        "status": "ok",
        "method": "parameter_stability_region",
        "sample_count": len(frame),
        "seed": None,
        "limitations": [
            "稳定性仅针对已评估参数网格，边界外表现未知"
        ],
        "best_parameters": best_parameters,
        "best_metric": _safe_number(best[metric_column]),
        "neighbors": neighbor_records,
        "neighbor_count": len(neighbor_records),
        "neighborhood_median_metric": _safe_number(
            float(neighbors[metric_column].median())
        ),
        "plateau_score": _safe_number(plateau_score),
        "is_stable": bool(
            len(neighbors) >= min_neighbors
            and plateau_score >= plateau_threshold
        ),
        "boundary_optimum": bool(boundary_parameters),
        "boundary_parameters": boundary_parameters,
        "warnings": warnings,
        "assumptions": assumptions,
    }


# Concise aliases for notebook and service-layer callers.
bootstrap_performance = block_bootstrap_performance
cscv_pbo = cscv_probability_of_backtest_overfitting
adjust_pvalues = multiple_testing_correction
