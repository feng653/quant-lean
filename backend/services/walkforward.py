"""Platform-owned walk-forward training and prediction.

The driver supports two deliberately different ML lifecycles:

* periodic models use expanding or rolling history and may retrain every N
  prediction months;
* ``RetrainFrequency.NEVER`` models train once on an explicit fixed window and
  reuse that artifact for every prediction month.

For either lifecycle, the training sample boundary is purged by the strategy's
forward-label horizon and then separated from prediction by ``embargo_days``.
This prevents labels near a prediction boundary from observing future prices.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

import pandas as pd

from backend.core.types import SignalDict
from backend.strategies.base import (
    DEFAULT_VALIDATION_RANK_IC,
    MIN_VALIDATION_CROSS_SECTION_SIZE,
    MIN_VALIDATION_EFFECTIVE_DATES,
    MIN_VALIDATION_RANK_IC,
    RetrainFrequency,
    TrainedModel,
    TrainingWindowContext,
)

if TYPE_CHECKING:
    from backend.strategies.base import TrainableStrategy

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3


class WalkForwardCancelled(Exception):
    """Raised when a walk-forward caller requests cancellation."""


@dataclass
class TrainCycle:
    """Execution and training telemetry for one prediction month."""

    pred_month: str
    pred_date: Optional[str] = None
    train_start: Optional[str] = None
    train_end: Optional[str] = None
    validation_start: Optional[str] = None
    validation_end: Optional[str] = None
    retrained: bool = False
    fit_seconds: float = 0.0
    n_scores: int = 0
    error: Optional[str] = None
    # Additive fields keep existing result consumers backward compatible.
    window_mode: Optional[str] = None
    label_horizon_days: int = 0
    embargo_days: int = 0
    validation_months: int = 0
    n_train_samples: Optional[int] = None
    n_validation_samples: Optional[int] = None
    n_train_features: Optional[int] = None
    train_metrics: dict[str, Any] = field(default_factory=dict)
    validation_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class WalkForwardResult:
    """Walk-forward signals, model artifact, and display-ready telemetry."""

    signals: SignalDict
    cycles: list[TrainCycle] = field(default_factory=list)
    last_model: Any = None
    last_window: Optional[tuple[str, str]] = None
    last_validation_window: Optional[tuple[str, str]] = None
    elapsed_seconds: float = 0.0

    def summary(self) -> str:
        retrained = [cycle for cycle in self.cycles if cycle.retrained]
        failed = [cycle for cycle in self.cycles if cycle.error]
        samples = sum(cycle.n_train_samples or 0 for cycle in retrained)
        fit_seconds = sum(cycle.fit_seconds for cycle in retrained)
        return (
            f"{len(self.cycles)} 个预测月, {len(retrained)} 次训练, "
            f"{samples} 个训练样本, 训练耗时 {fit_seconds:.2f}s, "
            f"{len(failed)} 次失败, {len(self.signals)} 个信号日"
        )


def _integer_param(
    params: dict,
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    value = params.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是 >= {minimum} 的整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是 >= {minimum} 的整数") from exc
    if parsed < minimum or parsed != value:
        raise ValueError(f"{name} 必须是 >= {minimum} 的整数")
    return parsed


def _timestamp_param(params: dict, name: str, *, required: bool) -> pd.Timestamp | None:
    value = params.get(name)
    if value in (None, ""):
        if required:
            raise ValueError(f"一次训练模式必须提供 {name}")
        return None
    try:
        return pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 不是有效日期: {value}") from exc


def _float_param(
    params: dict,
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = params.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须在 [{minimum}, {maximum}] 范围内")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} 必须在 [{minimum}, {maximum}] 范围内"
        ) from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} 必须在 [{minimum}, {maximum}] 范围内")
    return parsed


def _safe_training_end(
    all_dates: pd.DatetimeIndex,
    pred_date: pd.Timestamp,
    label_horizon_days: int,
    embargo_days: int,
) -> pd.Timestamp:
    """Return the last sample date whose forward label is safely observable."""
    pred_position = int(all_dates.searchsorted(pred_date, side="left"))
    train_end_position = pred_position - label_horizon_days - embargo_days - 1
    if train_end_position < 0:
        raise RuntimeError(
            f"预测日 {pred_date.date()} 前没有足够历史数据满足 "
            f"{label_horizon_days} 日标签周期 + {embargo_days} 日隔离带"
        )
    return all_dates[train_end_position]


def _align_training_window(
    all_dates: pd.DatetimeIndex,
    lower_bound: pd.Timestamp,
    upper_bound: pd.Timestamp,
) -> tuple[str, str]:
    start_position = int(all_dates.searchsorted(lower_bound, side="left"))
    end_position = int(all_dates.searchsorted(upper_bound, side="right")) - 1
    if start_position >= len(all_dates) or end_position < 0 or start_position >= end_position:
        raise RuntimeError(
            f"训练窗口 [{lower_bound.date()}, {upper_bound.date()}] 内可用行情数据不足"
        )
    return (
        str(all_dates[start_position].date()),
        str(all_dates[end_position].date()),
    )


def _split_fit_and_validation_windows(
    all_dates: pd.DatetimeIndex,
    *,
    lower_bound: pd.Timestamp,
    upper_bound: pd.Timestamp,
    validation_months: int,
    label_horizon_days: int,
    embargo_days: int,
    window_mode: str,
    rolling_train_months: int,
) -> tuple[tuple[str, str], Optional[tuple[str, str]]]:
    """Carve an untouched validation tail from the available safe history."""
    upper_position = int(all_dates.searchsorted(upper_bound, side="right")) - 1
    if upper_position < 0:
        raise RuntimeError(f"{upper_bound.date()} 前没有可用训练数据")

    validation_window: Optional[tuple[str, str]] = None
    fit_upper_position = upper_position
    if validation_months:
        validation_lower = (
            all_dates[upper_position]
            - pd.DateOffset(months=validation_months)
            + pd.Timedelta(days=1)
        )
        validation_start_position = int(all_dates.searchsorted(validation_lower, side="left"))
        # A short calendar month (for example February) can contain fewer
        # sessions than the forward-label horizon. Extend the validation tail
        # backwards so the server-side minimum number of feature dates have
        # labels that land fully inside validation_end.
        validation_start_position = min(
            validation_start_position,
            (
                upper_position
                - label_horizon_days
                - MIN_VALIDATION_EFFECTIVE_DATES
                + 1
            ),
        )
        if (
            validation_start_position < 0
            or validation_start_position > upper_position
        ):
            raise RuntimeError(f"无法从 {upper_bound.date()} 前预留 {validation_months} 个月验证集")
        # A training feature date is safe only when its complete forward label,
        # plus the requested embargo, ends strictly before validation begins.
        fit_upper_position = (
            validation_start_position
            - label_horizon_days
            - embargo_days
            - 1
        )
        if fit_upper_position < 0:
            raise RuntimeError(f"预留 {validation_months} 个月验证集后无可用训练数据")
        validation_window = (
            str(all_dates[validation_start_position].date()),
            str(all_dates[upper_position].date()),
        )

    fit_upper = all_dates[fit_upper_position]
    if window_mode == "rolling":
        lower_bound = max(
            lower_bound,
            fit_upper - pd.DateOffset(months=rolling_train_months) + pd.Timedelta(days=1),
        )
    fit_window = _align_training_window(all_dates, lower_bound, fit_upper)
    return fit_window, validation_window


def run_walk_forward(
    strategy: "TrainableStrategy",
    pivot: pd.DataFrame,
    params: dict,
    start_date: str,
    end_date: str,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> WalkForwardResult:
    """Train and predict through monthly walk-forward cycles.

    Periodic strategies infer their training history from ``pivot`` when no
    ``_train_start``/``_train_end`` values are provided.  Train-once strategies
    require both values and fit at most once.
    """
    run_started = time.monotonic()
    if pivot.empty:
        raise RuntimeError("行情数据为空，无法执行 Walk-Forward")
    if not isinstance(pivot.index, pd.DatetimeIndex):
        pivot = pivot.copy()
        try:
            pivot.index = pd.to_datetime(pivot.index)
        except (TypeError, ValueError) as exc:
            raise ValueError("行情索引必须是有效日期") from exc

    pivot = pivot.sort_index()
    all_dates = pd.DatetimeIndex(pivot.index.unique()).sort_values()
    try:
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("start_date/end_date 必须是有效日期") from exc
    if start > end:
        raise ValueError("start_date 必须早于或等于 end_date")

    metadata = strategy.metadata()
    train_once = metadata.retrain_frequency == RetrainFrequency.NEVER
    default_window_mode = "fixed" if train_once else "expanding"
    window_mode = str(params.get("window_mode", default_window_mode)).lower()
    allowed_window_modes = {"fixed"} if train_once else {"expanding", "rolling"}
    if window_mode not in allowed_window_modes:
        allowed = ", ".join(sorted(allowed_window_modes))
        raise ValueError(f"window_mode 必须是: {allowed}")

    retrain_months = _integer_param(params, "retrain_months", 1, minimum=1)
    min_train_months = _integer_param(params, "min_train_months", 12, minimum=1)
    rolling_train_months = _integer_param(params, "rolling_train_months", 36, minimum=1)
    embargo_days = _integer_param(params, "embargo_days", 0, minimum=0)
    validation_months = _integer_param(params, "validation_months", 1, minimum=0)
    min_validation_rank_ic = _float_param(
        params,
        "min_validation_rank_ic",
        DEFAULT_VALIDATION_RANK_IC,
        minimum=MIN_VALIDATION_RANK_IC,
        maximum=1.0,
    )
    label_horizon_days = strategy.label_horizon_days(params)
    if (
        isinstance(label_horizon_days, bool)
        or not isinstance(label_horizon_days, int)
        or label_horizon_days < 0
    ):
        raise ValueError("label_horizon_days 必须是 >= 0 的整数")
    if window_mode == "rolling" and rolling_train_months < min_train_months:
        raise ValueError("rolling_train_months 不能小于 min_train_months")

    requested_start = _timestamp_param(params, "_train_start", required=train_once)
    requested_end = _timestamp_param(params, "_train_end", required=train_once)
    if (
        requested_start is not None
        and requested_end is not None
        and requested_start >= requested_end
    ):
        raise ValueError("_train_start 必须早于 _train_end")

    pred_dates = all_dates[(all_dates >= start) & (all_dates <= end)]
    if len(pred_dates) == 0:
        raise RuntimeError(f"测试区间 [{start_date}, {end_date}] 无可用行情数据")

    pred_monthly = pred_dates.to_period("M")
    unique_pred_months = sorted(set(pred_monthly))
    verified_deployment_model = getattr(
        strategy,
        "_verified_deployment_model",
        None,
    )
    if progress_callback:
        progress_callback(0.0, "正在预处理数据（因子/特征计算）...")
    strategy.prepare(pivot, params)
    if verified_deployment_model is not None:
        strategy._model = verified_deployment_model

    if not strategy.get_universe(pivot, params):
        raise RuntimeError("策略股票池为空，无法生成信号")

    signals: SignalDict = {}
    cycles: list[TrainCycle] = []
    consecutive_failures = 0
    last_errors: list[str] = []
    model = getattr(strategy, "_model", None)
    reuse_loaded_model = verified_deployment_model is not None
    if reuse_loaded_model and model is None:
        raise RuntimeError(
            "verified deployment model was lost before prediction"
        )
    last_training_window: Optional[tuple[str, str]] = None
    last_validation_window: Optional[tuple[str, str]] = None

    # Train-once uses one immutable effective window, based on the first
    # prediction boundary, even when later prediction months are processed.
    fixed_training_window: Optional[tuple[str, str]] = None
    fixed_validation_window: Optional[tuple[str, str]] = None
    if train_once:
        first_pred_date = pred_dates[pred_monthly == unique_pred_months[0]][0]
        safe_end = _safe_training_end(all_dates, first_pred_date, label_horizon_days, embargo_days)
        assert requested_start is not None
        assert requested_end is not None
        fixed_training_window, fixed_validation_window = _split_fit_and_validation_windows(
            all_dates,
            lower_bound=requested_start,
            upper_bound=min(requested_end, safe_end),
            validation_months=validation_months,
            label_horizon_days=label_horizon_days,
            embargo_days=embargo_days,
            window_mode=window_mode,
            rolling_train_months=rolling_train_months,
        )
        if model is not None:
            last_training_window = fixed_training_window
            last_validation_window = fixed_validation_window

    total = len(unique_pred_months)
    for month_idx, pred_month in enumerate(unique_pred_months):
        if cancel_callback is not None and cancel_callback():
            raise WalkForwardCancelled("任务已取消")

        month_pred_dates = pred_dates[pred_monthly == pred_month]
        pred_date = month_pred_dates[0]
        cycle = TrainCycle(
            pred_month=str(pred_month),
            pred_date=pred_date.strftime("%Y-%m-%d"),
            window_mode=window_mode,
            label_horizon_days=label_horizon_days,
            embargo_days=embargo_days,
            validation_months=validation_months,
        )

        if train_once:
            training_window = fixed_training_window
            validation_window = fixed_validation_window
            should_train = model is None and not reuse_loaded_model
        else:
            should_train = (
                not reuse_loaded_model
                and (
                    model is None
                    or retrain_months == 1
                    or month_idx % retrain_months == 0
                )
            )
            training_window = None
            validation_window = None
            if should_train:
                safe_end = _safe_training_end(
                    all_dates, pred_date, label_horizon_days, embargo_days
                )
                upper_bound = (
                    min(safe_end, requested_end) if requested_end is not None else safe_end
                )
                lower_bound = all_dates[0]
                if requested_start is not None:
                    lower_bound = max(lower_bound, requested_start)
                training_window, validation_window = _split_fit_and_validation_windows(
                    all_dates,
                    lower_bound=lower_bound,
                    upper_bound=upper_bound,
                    validation_months=validation_months,
                    label_horizon_days=label_horizon_days,
                    embargo_days=embargo_days,
                    window_mode=window_mode,
                    rolling_train_months=rolling_train_months,
                )
                actual_months = (
                    pd.Period(training_window[1], freq="M")
                    - pd.Period(training_window[0], freq="M")
                ).n
                if actual_months < min_train_months:
                    raise RuntimeError(
                        f"训练窗口仅覆盖 {actual_months} 个月，"
                        f"不足 min_train_months={min_train_months}"
                    )

        if should_train:
            assert training_window is not None
            cycle.train_start, cycle.train_end = training_window
            if validation_window is not None:
                cycle.validation_start, cycle.validation_end = validation_window
            if model is None or training_window != last_training_window:
                fit_started = time.monotonic()
                previous_model = model
                try:
                    context = TrainingWindowContext(
                        train_start=training_window[0],
                        train_end=training_window[1],
                        validation_start=(
                            validation_window[0] if validation_window else None
                        ),
                        validation_end=(
                            validation_window[1] if validation_window else None
                        ),
                    )
                    fitted = strategy.fit_with_validation(
                        pivot,
                        params,
                        context,
                    )
                    if isinstance(fitted, TrainedModel):
                        strategy.record_train_metrics(**fitted.train_metrics)
                        candidate_model = fitted.model
                    else:
                        candidate_model = fitted
                    metrics = strategy.last_train_metrics
                    if validation_window is not None:
                        validation_count = int(
                            metrics.get("n_validation_samples") or 0
                        )
                        validation_dates = int(
                            metrics.get("n_validation_dates") or 0
                        )
                        minimum_cross_section = int(
                            metrics.get(
                                "min_validation_cross_section_size"
                            )
                            or 0
                        )
                        validation_rank_ic = metrics.get(
                            "validation_rank_ic"
                        )
                        if validation_count <= 0:
                            raise RuntimeError(
                                "验证集未产生可评估样本，禁止进入测试预测"
                            )
                        if (
                            validation_dates
                            < MIN_VALIDATION_EFFECTIVE_DATES
                        ):
                            raise RuntimeError(
                                "验证集有效截面日期不足，禁止进入测试预测: "
                                f"{validation_dates} < "
                                f"{MIN_VALIDATION_EFFECTIVE_DATES}"
                            )
                        if (
                            minimum_cross_section
                            < MIN_VALIDATION_CROSS_SECTION_SIZE
                        ):
                            raise RuntimeError(
                                "验证集最小截面样本不足，禁止进入测试预测: "
                                f"{minimum_cross_section} < "
                                f"{MIN_VALIDATION_CROSS_SECTION_SIZE}"
                            )
                        if (
                            validation_count
                            < validation_dates * minimum_cross_section
                        ):
                            raise RuntimeError(
                                "验证集截面证据计数不一致，禁止进入测试预测"
                            )
                        if (
                            validation_rank_ic is None
                            or not math.isfinite(float(validation_rank_ic))
                        ):
                            raise RuntimeError(
                                "验证集 RankIC 不可用，禁止进入测试预测"
                            )
                        if float(validation_rank_ic) < min_validation_rank_ic:
                            raise RuntimeError(
                                "验证集质量门未通过: "
                                f"RankIC={float(validation_rank_ic):.6f} < "
                                f"min_validation_rank_ic="
                                f"{min_validation_rank_ic:.6f}"
                            )
                    model = candidate_model
                    cycle.retrained = True
                    last_training_window = training_window
                    last_validation_window = validation_window
                    consecutive_failures = 0
                    cycle.train_metrics = metrics
                    cycle.validation_metrics = {
                        key: value
                        for key, value in metrics.items()
                        if key.startswith("validation_")
                        or key.startswith("n_validation_")
                        or key == "min_validation_cross_section_size"
                    }
                    n_samples = metrics.get("n_samples")
                    n_features = metrics.get("n_features")
                    n_validation_samples = metrics.get("n_validation_samples")
                    cycle.n_train_samples = int(n_samples) if n_samples is not None else None
                    cycle.n_train_features = int(n_features) if n_features is not None else None
                    cycle.n_validation_samples = (
                        int(n_validation_samples) if n_validation_samples is not None else None
                    )
                except Exception as exc:
                    model = previous_model
                    strategy._model = previous_model
                    consecutive_failures += 1
                    message = f"{type(exc).__name__}: {exc}"
                    cycle.error = message
                    last_errors.append(f"[{training_window[0]} ~ {training_window[1]}] {message}")
                    logger.warning(
                        "Walk-Forward 训练失败 [%s, %s]: %s",
                        training_window[0],
                        training_window[1],
                        message,
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        detail = "; ".join(last_errors[-MAX_CONSECUTIVE_FAILURES:])
                        raise RuntimeError(
                            f"Walk-Forward 训练连续 {consecutive_failures} 次失败，"
                            f"已停止。最近错误: {detail}"
                        ) from exc
                finally:
                    cycle.fit_seconds = round(time.monotonic() - fit_started, 3)
                if cycle.error:
                    cycles.append(cycle)
                    continue
        else:
            training_window = last_training_window
            validation_window = last_validation_window

        if training_window is not None:
            cycle.train_start, cycle.train_end = training_window
        if validation_window is not None:
            cycle.validation_start, cycle.validation_end = validation_window
        if model is None:
            cycles.append(cycle)
            continue

        # ── 预测并生成信号 ──
        decision_date = strategy.signal_decision_date(pivot, pred_date)
        scores = strategy.predict_scores(model, pivot, params, decision_date)
        cycle.n_scores = len(scores)
        decision_date_str = decision_date.strftime("%Y-%m-%d")

        items = strategy.select_signals(scores, params, decision_date_str)
        if items:
            signals.setdefault(decision_date_str, []).extend(items)
        cycles.append(cycle)

        if progress_callback:
            progress_callback(
                (month_idx + 1) / total,
                f"Walk-Forward {month_idx + 1}/{total}: {pred_month} "
                f"(训练窗口 {cycle.train_start} ~ {cycle.train_end}, "
                f"样本 {cycle.n_train_samples if cycle.retrained else '复用'}, "
                f"得分 {cycle.n_scores} 只)",
            )

    result = WalkForwardResult(
        signals=signals,
        cycles=cycles,
        last_model=model,
        last_window=last_training_window,
        last_validation_window=last_validation_window,
        elapsed_seconds=round(time.monotonic() - run_started, 3),
    )
    logger.info("Walk-Forward 完成: %s", result.summary())
    return result
