"""策略基类与组合策略抽象 —— 统一接口定义."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from backend.core.types import SignalDict, RealtimeSignal, SignalItem

# ═══════════════════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════════════════


class StrategyCategory(str, Enum):
    TECHNICAL = "technical"
    ML = "ml"
    FACTOR = "factor"
    PORTFOLIO = "portfolio"
    COMPOSITE = "composite"  # V3 新增


class StrategyMode(str, Enum):
    BATCH = "batch"
    REALTIME = "realtime"


class RetrainFrequency(str, Enum):
    NEVER = "never"  # 不需要重训练
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class PortfolioSignalMode(str, Enum):
    """How a strategy's BUY batch must be interpreted by execution."""

    EVENT_ORDERS = "event_orders"
    TARGET_WEIGHTS = "target_weights"


# ═══════════════════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ParamField:
    """策略参数定义。"""

    name: str
    type: str  # "int" | "float" | "str" | "bool" | "choice"
    default: Any
    description: str = ""
    required: bool = True
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[list] = None


def ml_training_params(
    *,
    periodic: bool,
    min_train_months: int,
    rolling_train_months: int = 36,
    retrain_months: int = 1,
) -> list[ParamField]:
    """Return the platform-owned training controls shared by ML strategies.

    Keeping these fields in one factory prevents individual strategies from
    drifting to different names or defaults.  A fresh list is returned for
    every metadata schema so callers may safely serialize or copy it.
    """
    fields = [
        ParamField(
            name="window_mode",
            type="choice",
            default="expanding" if periodic else "fixed",
            description=(
                "周期重训练窗口：扩展窗口或固定长度滚动窗口"
                if periodic
                else "一次训练使用用户指定的固定训练窗口"
            ),
            choices=["expanding", "rolling"] if periodic else ["fixed"],
        ),
        ParamField(
            name="rolling_train_months",
            type="int",
            default=rolling_train_months,
            description="滚动训练窗口长度（月；仅 rolling 模式生效）",
            min=1,
            max=120,
            step=1,
        ),
        ParamField(
            name="embargo_days",
            type="int",
            default=0,
            description="标签结束与预测日之间额外保留的交易日隔离带",
            min=0,
            max=60,
            step=1,
        ),
        ParamField(
            name="validation_months",
            type="int",
            default=1,
            description="从拟合窗口尾部留出的独立验证集长度（月）",
            min=0,
            max=24,
            step=1,
        ),
        ParamField(
            name="min_train_months",
            type="int",
            default=min_train_months,
            description="最小训练数据长度（月）",
            min=1,
            max=120,
            step=1,
        ),
        ParamField(
            name="min_validation_rank_ic",
            type="float",
            default=0.02,
            description="验证集 RankIC 最低门槛；低于门槛禁止进入测试预测",
            min=0.01,
            max=1.0,
            step=0.01,
        ),
    ]
    if periodic:
        fields.insert(
            0,
            ParamField(
                name="retrain_months",
                type="int",
                default=retrain_months,
                description="周期模型的重训练间隔（月）",
                min=1,
                max=12,
                step=1,
            ),
        )
    return fields


@dataclass
class SubStrategyRef:
    """组合策略对子策略的引用。"""

    strategy_id: str
    role: str  # 在组合中的角色说明
    params_override: dict = field(default_factory=dict)


@dataclass
class StrategyMetadata:
    """策略元数据 —— 策略自描述信息。"""

    strategy_id: str
    display_name: str
    version: str
    category: StrategyCategory
    description: str  # ⭐ 策略原理自述

    # 能力声明
    supported_modes: list[StrategyMode] = field(
        default_factory=lambda: [StrategyMode.BATCH]
    )
    requires_training: bool = False
    retrain_frequency: RetrainFrequency = RetrainFrequency.NEVER
    estimated_training_seconds: int = 60
    portfolio_signal_mode: PortfolioSignalMode = PortfolioSignalMode.EVENT_ORDERS

    # 参数
    params: list[ParamField] = field(default_factory=list)

    # 仓位
    max_position_pct: float = 0.05
    supported_position_modes: list[str] = field(
        default_factory=lambda: ["equal_weight"]
    )

    # ⭐ V3 新增：组合策略专属
    sub_strategies: list[SubStrategyRef] = field(default_factory=list)
    integration_method: str = ""

    # 标签
    tags: list[str] = field(default_factory=list)


EXECUTION_CONFIG_PARAM = "_execution"


@dataclass(frozen=True)
class PlatformExecutionConfig:
    """Platform-owned execution and transaction-cost configuration."""

    initial_capital: float = 1_000_000.0
    max_positions: int = 20
    lot_size: int = 100
    volume_participation: Optional[float] = None
    commission_rate: float = 0.0003
    slippage_rate: float = 0.001
    stamp_duty_rate: float = 0.001
    min_commission: float = 5.0

    @classmethod
    def from_params(cls, value: Any) -> PlatformExecutionConfig:
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError(f"{EXECUTION_CONFIG_PARAM} 必须是对象")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"未知执行参数: {', '.join(unknown)}")

        defaults = cls()
        integer_values = {
            "max_positions": (
                value.get("max_positions", defaults.max_positions),
                10_000,
            ),
            "lot_size": (value.get("lot_size", defaults.lot_size), 100_000),
        }
        normalized_integers: dict[str, int] = {}
        for name, (item, maximum) in integer_values.items():
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 1 <= item <= maximum
            ):
                raise ValueError(f"{name} 必须在 [1, {maximum}] 范围内")
            normalized_integers[name] = item

        numeric_bounds = {
            "initial_capital": (1.0, 1_000_000_000_000.0),
            "commission_rate": (0.0, 0.1),
            "slippage_rate": (0.0, 0.1),
            "stamp_duty_rate": (0.0, 0.1),
            "min_commission": (0.0, 10_000.0),
        }
        normalized: dict[str, float] = {}
        for name, (minimum, maximum) in numeric_bounds.items():
            item = value.get(name, getattr(defaults, name))
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not np.isfinite(item)
                or not minimum <= float(item) <= maximum
            ):
                raise ValueError(f"{name} 必须在 [{minimum}, {maximum}] 范围内")
            normalized[name] = float(item)

        participation = value.get(
            "volume_participation",
            defaults.volume_participation,
        )
        if participation is not None and (
            isinstance(participation, bool)
            or not isinstance(participation, (int, float))
            or not np.isfinite(participation)
            or not 0 < float(participation) <= 1
        ):
            raise ValueError("volume_participation 必须为 null 或 (0, 1] 范围内数字")

        return cls(
            initial_capital=normalized["initial_capital"],
            max_positions=normalized_integers["max_positions"],
            lot_size=normalized_integers["lot_size"],
            volume_participation=(
                float(participation)
                if participation is not None
                else None
            ),
            commission_rate=normalized["commission_rate"],
            slippage_rate=normalized["slippage_rate"],
            stamp_duty_rate=normalized["stamp_duty_rate"],
            min_commission=normalized["min_commission"],
        )


def split_platform_params(
    params: dict[str, Any],
) -> tuple[dict[str, Any], PlatformExecutionConfig]:
    """Separate platform execution controls from strategy-owned parameters."""
    strategy_params = dict(params)
    execution_value = strategy_params.pop(EXECUTION_CONFIG_PARAM, None)
    return strategy_params, PlatformExecutionConfig.from_params(execution_value)


@dataclass
class TrainedModel:
    """训练产物包装。"""

    model: Any
    feature_importance: Optional[dict] = None
    train_metrics: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TrainingWindowContext:
    """Explicit, non-overlapping fit and validation windows."""

    train_start: str
    train_end: str
    validation_start: Optional[str] = None
    validation_end: Optional[str] = None

    def __post_init__(self) -> None:
        train_start = pd.Timestamp(self.train_start)
        train_end = pd.Timestamp(self.train_end)
        if train_start >= train_end:
            raise ValueError("训练窗口起始必须早于结束")
        has_validation_start = self.validation_start is not None
        has_validation_end = self.validation_end is not None
        if has_validation_start != has_validation_end:
            raise ValueError("验证窗口起止日期必须同时提供")
        if has_validation_start:
            validation_start = pd.Timestamp(self.validation_start)
            validation_end = pd.Timestamp(self.validation_end)
            if validation_start > validation_end:
                raise ValueError("验证窗口起始不能晚于结束")
            if train_end >= validation_start:
                raise ValueError("训练窗口与验证窗口不能重叠")

    @property
    def has_validation(self) -> bool:
        return self.validation_start is not None


MIN_VALIDATION_EFFECTIVE_DATES = 5
MIN_VALIDATION_CROSS_SECTION_SIZE = 20
MIN_VALIDATION_RANK_IC = 0.01
DEFAULT_VALIDATION_RANK_IC = 0.02


def compute_validation_metrics(
    labels: Any,
    predictions: Any,
    prediction_dates: Any = None,
) -> dict[str, Any]:
    """Return date-wise cross-sectional validation telemetry.

    IC and RankIC are computed independently for each prediction date before
    their means are reported. Treating every date/security observation as one
    flattened vector can turn consistently negative daily RankIC into a
    positive number when between-date level shifts dominate.

    ``prediction_dates`` may be omitted only for callers that genuinely have a
    single cross-section. Platform walk-forward evaluation always supplies the
    actual feature/prediction date for every observation.
    """
    y_true = np.asarray(labels, dtype=float).reshape(-1)
    y_pred = np.asarray(predictions, dtype=float).reshape(-1)
    if y_true.shape != y_pred.shape:
        raise ValueError("验证标签和预测数量不一致")
    if prediction_dates is None:
        dates = np.full(y_true.shape, "__single_cross_section__", dtype=object)
    else:
        dates = np.asarray(prediction_dates, dtype=object).reshape(-1)
        if dates.shape != y_true.shape:
            raise ValueError("验证预测日期和标签数量不一致")
    finite = (
        np.isfinite(y_true)
        & np.isfinite(y_pred)
        & ~pd.isna(dates)
    )
    y_true = y_true[finite]
    y_pred = y_pred[finite]
    dates = dates[finite]
    count = int(len(y_true))
    metrics: dict[str, Any] = {
        "n_validation_samples": count,
        "n_validation_candidate_dates": 0,
        "n_validation_dates": 0,
        "min_validation_cross_section_size": 0,
        "validation_ic": None,
        "validation_ic_std": None,
        "validation_icir": None,
        "validation_rank_ic": None,
        "validation_rank_ic_std": None,
        "validation_rank_icir": None,
        "validation_loss": None,
        "validation_score": None,
    }
    if count == 0:
        return metrics
    metrics["validation_loss"] = float(np.mean((y_true - y_pred) ** 2))
    label_variance = float(np.sum((y_true - y_true.mean()) ** 2))
    if label_variance > 0:
        metrics["validation_score"] = float(
            1.0 - np.sum((y_true - y_pred) ** 2) / label_variance
        )

    observations = pd.DataFrame(
        {
            "date": dates,
            "label": y_true,
            "prediction": y_pred,
        }
    )
    metrics["n_validation_candidate_dates"] = int(
        observations["date"].nunique(dropna=True)
    )
    daily_ic: list[float] = []
    daily_rank_ic: list[float] = []
    cross_section_sizes: list[int] = []
    for _, cross_section in observations.groupby("date", sort=False):
        if len(cross_section) < 2:
            continue
        labels_for_date = cross_section["label"]
        predictions_for_date = cross_section["prediction"]
        if labels_for_date.nunique() < 2 or predictions_for_date.nunique() < 2:
            continue
        ic = labels_for_date.corr(predictions_for_date, method="pearson")
        rank_ic = labels_for_date.corr(predictions_for_date, method="spearman")
        if not np.isfinite(ic) or not np.isfinite(rank_ic):
            continue
        daily_ic.append(float(ic))
        daily_rank_ic.append(float(rank_ic))
        cross_section_sizes.append(int(len(cross_section)))

    effective_dates = len(daily_ic)
    metrics["n_validation_dates"] = effective_dates
    if not effective_dates:
        return metrics
    metrics["min_validation_cross_section_size"] = min(cross_section_sizes)

    ic_values = np.asarray(daily_ic, dtype=float)
    rank_ic_values = np.asarray(daily_rank_ic, dtype=float)
    metrics["validation_ic"] = float(ic_values.mean())
    metrics["validation_rank_ic"] = float(rank_ic_values.mean())
    ic_std = float(ic_values.std(ddof=1)) if effective_dates > 1 else 0.0
    rank_ic_std = (
        float(rank_ic_values.std(ddof=1)) if effective_dates > 1 else 0.0
    )
    metrics["validation_ic_std"] = ic_std
    metrics["validation_rank_ic_std"] = rank_ic_std
    if ic_std > 0:
        metrics["validation_icir"] = float(ic_values.mean() / ic_std)
    if rank_ic_std > 0:
        metrics["validation_rank_icir"] = float(
            rank_ic_values.mean() / rank_ic_std
        )
    return metrics


# ═══════════════════════════════════════════════════════════════════════════
# 策略协议（ABC）
# ═══════════════════════════════════════════════════════════════════════════


class StrategyProtocol(ABC):
    """所有策略（原子/组合）的统一接口。

    执行引擎、实验系统、交易系统不区分原子策略与组合策略，
    二者暴露完全相同的接口。
    """

    portfolio_signal_mode = PortfolioSignalMode.EVENT_ORDERS
    # Every strategy that consumes the platform PIT context must opt in with a
    # reviewed capability. Unknown/legacy implementations fail closed.
    point_in_time_context_capability: str | None = None

    @classmethod
    @abstractmethod
    def metadata(cls) -> StrategyMetadata:
        """返回策略的完整元数据。"""
        ...

    # ── 生命周期钩子 ─────────────────────────────────────────────────

    def on_register(self) -> None:
        """策略被注册中心发现并注册时调用。"""
        pass

    def on_unregister(self) -> None:
        """策略被注销时调用。"""
        pass

    # ── 参数校验 ─────────────────────────────────────────────────────

    def validate_params(self, params: dict) -> tuple[bool, str]:
        """校验用户提交的参数是否合法。

        Returns:
            (is_valid, error_message)
        """
        return True, ""

    # ── 数据预处理 ───────────────────────────────────────────────────

    def prepare_data(
        self, pivot: pd.DataFrame, params: dict
    ) -> pd.DataFrame:
        """对宽表数据进行策略特化预处理（衍生特征等）。

        基类默认返回原数据，子类可覆写。
        """
        return pivot

    # ── 信号生成（必须实现）─────────────────────────────────────────

    @abstractmethod
    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        """批量生成交易信号（回测/批量模式）。

        Args:
            pivot: 日线宽表数据。
            params: 策略参数。
            start_date: 信号起始日期 (YYYY-MM-DD)。
            end_date: 信号结束日期 (YYYY-MM-DD)。

        Returns:
            SignalDict: {日期 → [SignalItem, ...]}
        """
        ...

    def generate_realtime_signal(
        self,
        market_snapshot: pd.DataFrame,
        params: dict,
    ) -> RealtimeSignal:
        """生成实时信号（可选覆写）。

        Args:
            market_snapshot: 当前市场快照。
            params: 策略参数。

        Returns:
            RealtimeSignal

        Raises:
            NotImplementedError: 默认不支持实时模式。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support realtime signals"
        )

    # ── 模型训练 / 持久化（V3 新增）─────────────────────────────────

    def train(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        existing_model: Optional[Any] = None,
    ) -> TrainedModel:
        """训练（或重训练）模型。

        Args:
            pivot: 训练数据。
            params: 策略参数。
            train_start: 训练集起始。
            train_end: 训练集结束。
            progress_callback: 可选，进度回调 (progress_pct, message)。
            existing_model: 可选，旧模型（用于增量训练/warm-start）。

        Returns:
            TrainedModel

        Raises:
            NotImplementedError: 默认不支持训练。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support training"
        )

    def load_model(self, path: str) -> Any:
        """从磁盘加载模型。"""
        import joblib

        return joblib.load(path)

    def save_model(self, model: Any, path: str) -> None:
        """保存模型到磁盘。"""
        import joblib
        import os

        os.makedirs(os.path.dirname(path), exist_ok=True)
        joblib.dump(model, path)


# ═══════════════════════════════════════════════════════════════════════════
# 可训练策略基类（平台驱动 Walk-Forward）
# ═══════════════════════════════════════════════════════════════════════════


class TrainableStrategy(StrategyProtocol, ABC):
    """可训练策略基类 —— 由平台驱动周期性重训练（Walk-Forward）.

    子类只需实现三个钩子，不再自行编写月度重训练循环:
        - prepare(): 一次性预处理（如因子计算），结果缓存于实例
        - fit(): 在 [train_start, train_end] 窗口训练，返回模型并写入 self._model
        - predict_scores(): 用模型给截至 as_of_date 的股票打分 {code: score}

    平台驱动器 (backend.services.walkforward) 负责:
        月度调度、训练窗口计算、进度上报、取消检查、失败冒泡。
    重训练节奏由 metadata().retrain_frequency 声明:
        - NEVER: 仅用 _train_start/_train_end 训练一次（train-once）
        - MONTHLY 等: 按 retrain_months 参数间隔周期性重训练
    """

    # 当前平台的横截面模型按月生成持仓目标。该信号节奏与模型重训练
    # 生命周期解耦：NEVER 表示一次训练，并不表示月度预测应延后到月初收盘。
    prediction_frequency: RetrainFrequency = RetrainFrequency.MONTHLY
    portfolio_signal_mode = PortfolioSignalMode.TARGET_WEIGHTS

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._last_train_metrics: dict[str, Any] = {}

    # ── 平台驱动钩子 ─────────────────────────────────────────────────

    def prepare(self, pivot: pd.DataFrame, params: dict) -> None:
        """Walk-Forward 开始前调用一次（因子/特征预计算）。默认无操作。"""
        return None

    def label_horizon_days(self, params: dict) -> int:
        """Forward-label horizon in trading observations.

        The platform uses this value to purge samples whose labels would cross
        a prediction boundary.  Existing ML strategies all use 21-day forward
        returns; accepting the future public parameter keeps the hook ready for
        configurable labels without changing the fit interface.
        """
        return int(params.get("label_horizon_days", 21))

    @property
    def last_train_metrics(self) -> dict[str, Any]:
        """Metrics for the most recent ``fit`` call (copy for safe reporting)."""
        return dict(getattr(self, "_last_train_metrics", {}))

    def record_train_metrics(self, **metrics: Any) -> None:
        """Publish exact fit metrics to the platform walk-forward driver."""
        self._last_train_metrics = dict(metrics)

    def fit_with_validation(
        self,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> Any:
        """Fit through the explicit window contract, preserving legacy ``fit``.

        Strategies with native validation/early stopping can override this
        method.  The default path trains only on the fit window and evaluates
        the untouched validation tail through observable strategy scores.
        """
        fitted = self.fit(
            pivot,
            params,
            context.train_start,
            context.train_end,
        )
        if isinstance(fitted, TrainedModel):
            model = fitted.model
            metrics = {**fitted.train_metrics, **self.last_train_metrics}
        else:
            model = fitted
            metrics = self.last_train_metrics
        if context.has_validation:
            metrics.update(
                self.evaluate_validation(
                    model,
                    pivot,
                    params,
                    context,
                )
            )
        else:
            metrics.update(compute_validation_metrics([], []))
        metrics.update(
            {
                "train_start": context.train_start,
                "train_end": context.train_end,
                "validation_start": context.validation_start,
                "validation_end": context.validation_end,
            }
        )
        self.record_train_metrics(**metrics)
        return model

    def evaluate_validation(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> dict[str, Any]:
        """Evaluate monthly validation cross-sections without fitting labels."""
        if not context.has_validation:
            return compute_validation_metrics([], [])
        assert context.validation_start is not None
        assert context.validation_end is not None
        close = self._validation_close_frame(pivot)
        if close.empty:
            return compute_validation_metrics([], [])
        dates = pd.DatetimeIndex(close.index).sort_values().unique()
        validation_dates = dates[
            (dates >= pd.Timestamp(context.validation_start))
            & (dates <= pd.Timestamp(context.validation_end))
        ]
        if len(validation_dates) == 0:
            return compute_validation_metrics([], [])
        evaluation_dates = validation_dates.tolist()
        horizon = self.label_horizon_days(params)
        labels: list[float] = []
        predictions: list[float] = []
        prediction_dates: list[pd.Timestamp] = []
        for date in evaluation_dates:
            current_position = int(dates.get_loc(date))
            future_position = current_position + horizon
            if future_position >= len(dates):
                continue
            future_date = dates[future_position]
            if future_date > pd.Timestamp(context.validation_end):
                continue
            scores = self.predict_scores(model, pivot, params, pd.Timestamp(date))
            for code, score in scores.items():
                if code not in close.columns:
                    continue
                current = close.at[date, code]
                future = close.at[future_date, code]
                if (
                    not np.isfinite(current)
                    or not np.isfinite(future)
                    or not np.isfinite(score)
                    or current == 0
                ):
                    continue
                labels.append(float(future / current - 1.0))
                predictions.append(float(score))
                prediction_dates.append(pd.Timestamp(date))
        return compute_validation_metrics(
            labels,
            predictions,
            prediction_dates,
        )

    def validation_sample_window(
        self,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> tuple[str, str]:
        """Return validation feature dates whose labels stay inside validation."""
        if not context.has_validation:
            raise ValueError("未配置验证窗口")
        assert context.validation_start is not None
        assert context.validation_end is not None
        dates = pd.DatetimeIndex(pd.to_datetime(pivot.index)).sort_values().unique()
        start_position = int(
            dates.searchsorted(pd.Timestamp(context.validation_start), side="left")
        )
        end_position = (
            int(
                dates.searchsorted(
                    pd.Timestamp(context.validation_end),
                    side="right",
                )
            )
            - 1
            - self.label_horizon_days(params)
        )
        if (
            start_position >= len(dates)
            or end_position < start_position
        ):
            raise ValueError("验证窗口不足以容纳完整标签周期")
        return (
            str(dates[start_position].date()),
            str(dates[end_position].date()),
        )

    @staticmethod
    def _validation_close_frame(pivot: pd.DataFrame) -> pd.DataFrame:
        if isinstance(pivot.columns, pd.MultiIndex):
            values = {
                str(code): pivot[(code, "close")]
                for code in pivot.columns.get_level_values(0).unique()
                if (code, "close") in pivot.columns
            }
            return pd.DataFrame(values, index=pivot.index)
        return pivot.drop(columns=["date", "code"], errors="ignore")

    @abstractmethod
    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        """在指定窗口训练模型。

        Returns:
            训练好的模型（同时应写入 self._model 以便产物持久化）。
        """
        ...

    @abstractmethod
    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        """用模型对截至 as_of_date（含）的最新数据打分。

        Returns:
            {code: score}，分数越高越看好。
        """
        ...

    def get_universe(self, pivot: pd.DataFrame, params: dict) -> list[str]:
        """参与打分/训练的股票集合。默认从 pivot 列提取。"""
        if isinstance(pivot.columns, pd.MultiIndex):
            return sorted({str(c[0]) for c in pivot.columns if isinstance(c, tuple)})
        return sorted(str(c) for c in pivot.columns)

    def signal_decision_date(
        self, pivot: pd.DataFrame, prediction_date: pd.Timestamp
    ) -> pd.Timestamp:
        """Map a prediction/execution session to its observable decision date.

        Monthly models target the first session of a month. Their signal must
        therefore be stamped on the immediately preceding market session so the
        engine can execute it at that month-first open. The prediction cadence
        is intentionally independent from the model retraining cadence.
        """
        if self.prediction_frequency != RetrainFrequency.MONTHLY:
            return prediction_date
        dates = pd.DatetimeIndex(pd.to_datetime(pivot.index)).sort_values().unique()
        previous = dates[dates < prediction_date]
        return previous[-1] if len(previous) else prediction_date

    def select_signals(
        self, scores: dict[str, float], params: dict, date_str: str
    ) -> list[SignalItem]:
        """把预测得分转换为交易信号（默认 Top K% 买入，score 归一化）。可覆写。"""
        if not scores:
            return []
        top_k_pct: float = params.get("top_k_pct", 0.10)
        k = max(1, int(len(scores) * top_k_pct))
        sorted_codes = sorted(scores, key=scores.get, reverse=True)[:k]
        max_score = max(scores.values())
        if max_score <= 0:
            max_score = 1.0
        return [
            SignalItem(
                code=code,
                action="BUY",
                score=max(0.0, min(1.0, scores[code] / max_score)),
                weight=1.0,
            )
            for code in sorted_codes
        ]

    # ── 协议入口（兼容旧调用路径）─────────────────────────────────────

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        """批量生成交易信号 —— 委托给平台 Walk-Forward 驱动器。"""
        from backend.services.walkforward import run_walk_forward

        return run_walk_forward(self, pivot, params, start_date, end_date).signals


# ═══════════════════════════════════════════════════════════════════════════
# 组合策略基类
# ═══════════════════════════════════════════════════════════════════════════


class CompositeStrategy(StrategyProtocol, ABC):
    """组合策略基类。

    提供:
        - _get_sub_strategy(strategy_id) → 获取子策略实例（懒加载）
        - _merge_signals(signals_list, weights) → 信号合并工具

    子类只需实现 generate_batch_signals，在内部协调子策略即可。
    """

    def __init__(self) -> None:
        super().__init__()
        self._sub_instances: dict[str, StrategyProtocol] = {}

    def _get_sub_strategy(self, strategy_id: str) -> StrategyProtocol:
        """从注册中心懒加载子策略实例。"""
        if strategy_id not in self._sub_instances:
            # 延迟导入避免循环引用
            from backend.strategies.registry import get_registry  # type: ignore[import-untyped]

            registry = get_registry()
            self._sub_instances[strategy_id] = registry.create_strategy(strategy_id)
        return self._sub_instances[strategy_id]

    def _merge_signals(
        self,
        signals_list: list[SignalDict],
        weights: list[float],
    ) -> SignalDict:
        """合并多个子策略的信号。

        逻辑:
            1. 收集所有日期。
            2. 对每个日期，合并所有子策略的信号。
            3. 按权重分配 score。
            4. 同股票、同方向信号合并（取加权 score）。

        Args:
            signals_list: 各子策略的信号字典列表。
            weights: 对应权重，长度与 signals_list 一致。

        Returns:
            合并后的 SignalDict。
        """
        # 收集所有日期
        all_dates: set[str] = set()
        for sigs in signals_list:
            all_dates.update(sigs.keys())

        merged: SignalDict = {}
        for date_str in sorted(all_dates):
            # 收集该日所有子策略的信号并按方向净额化。
            net_scores: dict[str, float] = {}
            for sigs, w in zip(signals_list, weights):
                if w <= 0:
                    continue
                for item in sigs.get(date_str, []):
                    weighted = item.score * w
                    action = item.action.upper()
                    if action not in {"BUY", "SELL"}:
                        continue
                    direction = 1.0 if action == "BUY" else -1.0
                    net_scores[item.code] = (
                        net_scores.get(item.code, 0.0) + direction * weighted
                    )

            from backend.core.types import SignalItem

            day_items: list[SignalItem] = []
            for code, net_score in net_scores.items():
                if abs(net_score) <= 1e-12:
                    continue
                action = "BUY" if net_score > 0 else "SELL"
                score = abs(net_score)
                day_items.append(
                    SignalItem(
                        code=code,
                        action=action,
                        score=score,
                        weight=score if action == "BUY" else 0.0,
                    )
                )
            merged[date_str] = day_items

        return merged
