"""Leakage-safe, pre-registered out-of-sample factor stability analysis.

Each window owns its forward-return calculation and is truncated at the
window end before shifting prices.  Observations can therefore never borrow a
realized return from a later validation or locked window.
"""

from __future__ import annotations

import math
from typing import Any, Literal

import pandas as pd

from backend.research.factor_analysis import (
    analyze_factor_decay,
    analyze_quantile_returns,
    calculate_ic,
    compute_forward_returns,
    cross_sectional_preprocess,
)

MIN_WINDOW_SESSIONS = {
    "train": 252,
    "validation": 63,
    "locked": 63,
}
MIN_EVALUABLE_PRIMARY_DATES = {
    "train": 126,
    "validation": 42,
    "locked": 42,
}


def _normal_two_sided_p_value(t_stat: float | None) -> float | None:
    """Return a conservative normal approximation for an IC t statistic."""
    if t_stat is None or not math.isfinite(t_stat):
        return None
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


def _adjust_p_value(
    p_value: float | None,
    *,
    hypotheses_tested: int,
    correction: Literal["bonferroni"],
) -> float | None:
    if p_value is None:
        return None
    if correction == "bonferroni":
        return min(1.0, p_value * hypotheses_tested)
    raise ValueError("不支持的多重检验校正")


def _slice_panel(payload: dict[str, Any], start: str, end: str) -> pd.DataFrame:
    frame = pd.DataFrame(
        payload["values"],
        index=pd.to_datetime(payload["dates"]),
        columns=[str(code) for code in payload["codes"]],
        dtype=float,
    )
    return frame.loc[pd.Timestamp(start) : pd.Timestamp(end)]


def _finite_range(values: list[float | None]) -> float | None:
    finite = [value for value in values if value is not None and math.isfinite(value)]
    return max(finite) - min(finite) if finite else None


def analyze_pre_registered_stability(
    factor: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    windows: list[dict[str, str]],
    horizons: list[int],
    primary_horizon: int,
    quantiles: int,
    winsor_method: Literal["mad", "quantile", "none"],
    hypotheses_tested: int,
    correction: Literal["bonferroni"],
    alpha: float,
    eligibility: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Evaluate fixed train/validation/locked windows without cross-window fit.

    Factor parameters and preprocessing rules are fixed before all windows.
    Preprocessing is cross-sectional and independently fitted on each date.
    No observations or daily IC values are pooled across windows.
    """
    if [window.get("role") for window in windows] != [
        "train",
        "validation",
        "locked",
    ]:
        raise ValueError("稳定性窗口必须依次为 train、validation、locked")
    if hypotheses_tested < 1:
        raise ValueError("hypotheses_tested 必须至少为 1")
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须位于 0 与 1 之间")

    min_samples = quantiles * 2
    window_results: list[dict[str, Any]] = []
    for window in windows:
        role = window["role"]
        start = window["start"]
        end = window["end"]
        session_index = prices.loc[pd.Timestamp(start) : pd.Timestamp(end)].index
        required_sessions = MIN_WINDOW_SESSIONS[role]
        if len(session_index) < required_sessions:
            raise ValueError(
                f"{role} 窗口只有 {len(session_index)} 个交易日，"
                f"至少需要 {required_sessions} 个交易日"
            )

        # Fit preprocessing independently within this window.  Its transform
        # is cross-sectional per date, so no future date enters an earlier row.
        window_factor = factor.loc[
            pd.Timestamp(start) : pd.Timestamp(end)
        ].copy()
        processed = cross_sectional_preprocess(
            window_factor,
            winsor_method=winsor_method,
            min_samples=min_samples,
        )

        # Critical leakage boundary: truncate prices before shift(-horizon).
        window_forward = compute_forward_returns(
            prices.loc[: pd.Timestamp(end)],
            horizons=horizons,
            evaluation_end=end,
        )["horizons"]
        masked_forward: dict[str, pd.DataFrame] = {}
        horizon_results: dict[str, Any] = {}
        for horizon in sorted(horizons):
            returns = _slice_panel(window_forward[str(horizon)], start, end)
            if eligibility is not None:
                from backend.data.point_in_time_universe import (
                    origin_date_label_eligibility,
                )

                label_eligibility = origin_date_label_eligibility(
                    eligibility.loc[: pd.Timestamp(end)],
                )
                returns = returns.where(
                    label_eligibility.reindex(
                        index=returns.index,
                        columns=returns.columns,
                        fill_value=False,
                    )
                )
            masked_forward[str(horizon)] = returns
            horizon_ic = calculate_ic(
                processed["values"],
                returns,
                min_samples=min_samples,
            )
            rank_summary = horizon_ic["summary"]["rank_ic"]
            raw_p = _normal_two_sided_p_value(rank_summary["t_stat"])
            horizon_results[str(horizon)] = {
                "ic": horizon_ic,
                "multiple_testing": {
                    "raw_approx_p_value": raw_p,
                    "adjusted_p_value": _adjust_p_value(
                        raw_p,
                        hypotheses_tested=hypotheses_tested,
                        correction=correction,
                    ),
                    "passes_adjusted_alpha": (
                        raw_p is not None
                        and _adjust_p_value(
                            raw_p,
                            hypotheses_tested=hypotheses_tested,
                            correction=correction,
                        )
                        <= alpha
                    ),
                },
            }

        primary_returns = masked_forward[str(primary_horizon)]
        quantile_returns = analyze_quantile_returns(
            processed["values"],
            primary_returns,
            quantiles=quantiles,
            min_samples=min_samples,
        )
        decay = analyze_factor_decay(
            processed["values"],
            {
                str(horizon): masked_forward[str(horizon)]
                for horizon in sorted(horizons)
            },
            min_samples=min_samples,
        )
        diagnostics = processed["diagnostics"]
        valid_factor_dates = sum(
            item["status"] not in {"insufficient_samples"}
            for item in diagnostics
        )
        evaluable_dates = horizon_results[str(primary_horizon)]["ic"]["summary"][
            "rank_ic"
        ]["count"]
        minimum_evaluable = MIN_EVALUABLE_PRIMARY_DATES[role]
        if evaluable_dates < minimum_evaluable:
            raise ValueError(
                f"{role} 窗口只有 {evaluable_dates} 个可评估 RankIC 日期，"
                f"至少需要 {minimum_evaluable} 个"
            )
        window_results.append(
            {
                "role": role,
                "requested_start": start,
                "requested_end": end,
                "actual_start": str(pd.Timestamp(session_index.min()).date()),
                "actual_end": str(pd.Timestamp(session_index.max()).date()),
                "sessions": len(session_index),
                "minimum_sessions": required_sessions,
                "horizons": horizon_results,
                "quantile_returns": quantile_returns,
                "decay": decay,
                "coverage": {
                    "factor_dates": len(diagnostics),
                    "valid_factor_dates": valid_factor_dates,
                    "evaluable_primary_dates": evaluable_dates,
                    "minimum_evaluable_primary_dates": minimum_evaluable,
                    "primary_evaluation_ratio": (
                        evaluable_dates / len(session_index)
                        if len(session_index)
                        else None
                    ),
                },
            }
        )

    rank_ic_means = [
        window["horizons"][str(primary_horizon)]["ic"]["summary"]["rank_ic"][
            "mean"
        ]
        for window in window_results
    ]
    rank_ic_irs = [
        window["horizons"][str(primary_horizon)]["ic"]["summary"]["rank_ic"][
            "icir"
        ]
        for window in window_results
    ]
    long_short_means = [
        window["quantile_returns"]["long_short"]["mean"]
        for window in window_results
    ]
    finite_means = [
        value
        for value in rank_ic_means
        if value is not None and math.isfinite(value)
    ]
    sign_consistent = (
        len(finite_means) == len(window_results)
        and (all(value > 0 for value in finite_means) or all(value < 0 for value in finite_means))
    )
    validation_mean = rank_ic_means[1]
    locked_mean = rank_ic_means[2]
    return {
        "schema_version": "factor-stability/v1",
        "design": {
            "mode": "fixed_three_way",
            "pre_registered": True,
            "locked_declared_before_run": True,
            "parameter_policy": (
                "因子参数与预处理规则在三个窗口开始前固定，窗口间不重新选择"
            ),
            "factor_data_policy": (
                "trailing_only_builder_rebuilt_with_rows_visible_by_window_end"
            ),
            "fit_policy": "cross_sectional_per_date_only",
            "forward_return_policy": (
                "truncate_at_each_window_end_before_shift;"
                "require_origin_date_membership;"
                "fixed_horizon_security_return_not_reconstitution_execution"
                if eligibility is not None
                else "truncate_at_each_window_end_before_shift"
            ),
            "aggregation_policy": "window_metrics_never_pool_daily_observations",
        },
        "windows": window_results,
        "stability_summary": {
            "primary_horizon": primary_horizon,
            "rank_ic_means": rank_ic_means,
            "rank_ic_irs": rank_ic_irs,
            "long_short_means": long_short_means,
            "rank_ic_mean_range": _finite_range(rank_ic_means),
            "rank_ic_sign_consistent": sign_consistent,
            "locked_minus_validation_rank_ic": (
                locked_mean - validation_mean
                if locked_mean is not None and validation_mean is not None
                else None
            ),
            "windows_with_evaluable_primary_ic": sum(
                value is not None for value in rank_ic_means
            ),
        },
        "multiple_testing": {
            "hypotheses_tested": hypotheses_tested,
            "correction": correction,
            "alpha": alpha,
            "adjusted_alpha": alpha / hypotheses_tested,
            "p_value_method": "two_sided_normal_approximation_from_daily_rank_ic_t_stat",
            "interpretation": (
                "校正后的统计显著性只表示该样本中的证据强度，"
                "不证明因子有效、可交易或未来仍有效。"
            ),
        },
        "warnings": [
            "训练窗、验证窗和锁定窗严格隔离，指标不得跨窗混算。",
            "锁定窗结果只能在预注册配置下解释，查看后不得据此回改本次运行。",
            "多重检验校正不能消除数据挖掘、幸存者偏差或交易成本风险。",
        ],
    }
