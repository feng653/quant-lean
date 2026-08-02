"""LSTM 深度学习排序策略 —— 序列预测 + Walk-Forward.

如果 PyTorch 不可用，降级为 sklearn MLPClassifier。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    TrainableStrategy,
    TrainedModel,
    TrainingWindowContext,
    ml_training_params,
)
from backend.strategies.ml.runtime import (
    import_optional_torch,
    select_torch_device_name,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# 参数 Schema
# ═══════════════════════════════════════════════════════════════════════════

PARAM_SCHEMA: list[ParamField] = [
    ParamField(
        name="seq_len",
        type="int",
        default=20,
        description="输入序列长度（日）",
        min=20,
        max=120,
        step=10,
    ),
    ParamField(
        name="hidden_size",
        type="int",
        default=32,
        description="LSTM 隐藏层大小",
        min=16,
        max=256,
        step=16,
    ),
    ParamField(
        name="num_layers",
        type="int",
        default=1,
        description="LSTM 层数",
        min=1,
        max=4,
        step=1,
    ),
    ParamField(
        name="dropout",
        type="float",
        default=0.1,
        description="Dropout 比例",
        min=0.0,
        max=0.5,
        step=0.05,
    ),
    ParamField(
        name="learning_rate",
        type="float",
        default=0.001,
        description="学习率",
        min=0.0001,
        max=0.01,
        step=0.0001,
    ),
    ParamField(
        name="epochs",
        type="int",
        default=10,
        description="训练轮数",
        min=10,
        max=200,
        step=10,
    ),
    ParamField(
        name="top_k_pct",
        type="float",
        default=0.05,
        description="买入股票比例（Top K%）",
        min=0.05,
        max=0.3,
        step=0.01,
    ),
] + ml_training_params(periodic=False, min_train_months=24)


# ═══════════════════════════════════════════════════════════════════════════
# PyTorch LSTM 模型
# ═══════════════════════════════════════════════════════════════════════════


def _build_lstm_model(
    input_size: int,
    hidden_size: int,
    num_layers: int,
    dropout: float,
    learning_rate: float,
) -> Any:
    """构建 PyTorch LSTM 模型."""
    import torch.nn as nn

    class LSTMRanker(nn.Module):
        def __init__(self, input_size, hidden_size, num_layers, dropout):
            super().__init__()
            self.lstm = nn.LSTM(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                dropout=dropout if num_layers > 1 else 0,
                batch_first=True,
            )
            self.fc = nn.Sequential(
                nn.Linear(hidden_size, hidden_size // 2),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(hidden_size // 2, 1),
            )

        def forward(self, x):
            # x: (batch, seq_len, input_size)
            out, _ = self.lstm(x)
            # 取最后一个时间步
            out = out[:, -1, :]
            return self.fc(out).squeeze(-1)

    model = LSTMRanker(input_size, hidden_size, num_layers, dropout)
    model._quant_model_config = {
        "input_size": input_size,
        "hidden_size": hidden_size,
        "num_layers": num_layers,
        "dropout": dropout,
        "learning_rate": learning_rate,
    }
    return model


def _train_lstm(
    model: Any,
    X: np.ndarray,
    y: np.ndarray,
    epochs: int,
    learning_rate: float,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    X_validation: Optional[np.ndarray] = None,
    y_validation: Optional[np.ndarray] = None,
) -> dict:
    """训练 LSTM 模型."""
    import torch

    device = torch.device(select_torch_device_name(torch))
    model = model.to(device)

    # Keep the full dataset on CPU.  Moving validation to CUDA in
    # one shot can require tens of GiB for recurrent activations on the full
    # 429-stock universe; only mini-batches belong on the accelerator.
    X_train = torch.tensor(X, dtype=torch.float32)
    y_train = torch.tensor(y, dtype=torch.float32)
    X_val = torch.tensor(
        X_validation if X_validation is not None else np.empty((0, *X.shape[1:])),
        dtype=torch.float32,
    )
    y_val = torch.tensor(
        y_validation if y_validation is not None else np.empty(0),
        dtype=torch.float32,
    )

    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    criterion = torch.nn.MSELoss()

    batch_size = min(64, len(X_train))
    n_batches = max(1, len(X_train) // batch_size)

    best_val_loss = float("inf")
    best_state: Optional[dict[str, Any]] = None
    patience = 10
    patience_counter = 0

    for epoch in range(epochs):
        model.train()
        total_loss = 0.0
        indices = torch.randperm(len(X_train))
        for i in range(0, len(X_train), batch_size):
            batch_idx = indices[i : i + batch_size]
            X_batch = X_train[batch_idx].to(device)
            y_batch = y_train[batch_idx].to(device)

            optimizer.zero_grad()
            pred = model(X_batch)
            loss = criterion(pred, y_batch)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        avg_train_loss = total_loss / n_batches

        # Validation
        model.eval()
        with torch.no_grad():
            if len(X_val) > 0:
                val_loss_total = 0.0
                for i in range(0, len(X_val), batch_size):
                    X_batch = X_val[i : i + batch_size].to(device)
                    y_batch = y_val[i : i + batch_size].to(device)
                    batch_loss = criterion(model(X_batch), y_batch).item()
                    val_loss_total += batch_loss * len(X_batch)
                val_loss = val_loss_total / len(X_val)
            else:
                val_loss = avg_train_loss

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {
                key: value.detach().cpu().clone()
                for key, value in model.state_dict().items()
            }
            patience_counter = 0
        else:
            patience_counter += 1

        if progress_callback and epoch % 5 == 0:
            progress_callback(
                0.3 + 0.6 * (epoch / epochs),
                f"Epoch {epoch+1}/{epochs} train_loss={avg_train_loss:.4f} val_loss={val_loss:.4f}",
            )

        if patience_counter >= patience:
            if progress_callback:
                progress_callback(0.95, f"Early stop at epoch {epoch+1}")
            break

    if best_state is not None:
        model.load_state_dict(best_state)
    return {"train_loss": avg_train_loss, "val_loss": val_loss, "epochs_trained": epoch + 1}


# ═══════════════════════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════════════════════


class LSTMRankStrategy(TrainableStrategy):
    """LSTM 深度学习排序策略。

    策略原理:
        使用 LSTM（长短期记忆网络）处理股票的日收益率时间序列。
        取每只股票过去 N 天的日收益率序列作为输入，通过双层 LSTM 网络
        自动学习时序模式（趋势延续、反转、波动率聚集等），预测下月上涨概率。
        在固定历史窗口训练一次并复用模型，买入预测上涨概率最高的 Top K% 股票。
        相比于传统技术指标，LSTM 能从序列中自动提取有意义的特征，
        不需要人工构造滞后项或因子。

    降级方案:
        如果 PyTorch 不可用，自动降级为 sklearn MLPClassifier，
        使用多层感知器进行涨跌二分类。

    适用范围:
        需要至少 50 只股票和 1 年以上历史数据；
        GPU 推荐用于加速训练，CPU 也可运行但较慢。
    """

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._use_torch: bool = True
        self._is_sklearn_fallback: bool = False
        self._active_validation_context: Optional[TrainingWindowContext] = None

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="lstm_rank_v1",
            display_name="LSTM 深度学习排序策略",
            version="1.0.0",
            category=StrategyCategory.ML,
            description=(
                "基于 LSTM 深度学习模型的排序策略。将股票过去 N 日的日收益率序列"
                "作为输入，通过双层 LSTM 网络自动学习时序依赖模式（趋势、反转、波动率聚集），"
                "预测下月上涨概率。模型在固定历史窗口完成一次训练并在实验期复用，"
                "买入预测概率最高的 Top K% 股票。"
                "相比传统技术指标，LSTM 能自动提取时序特征，无需人工构造。"
                "若 PyTorch 不可用则自动降级为 sklearn MLP。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=True,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=300,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            supported_position_modes=["equal_weight"],
            tags=["深度学习", "LSTM", "时序预测", "排序学习", "一次训练", "PyTorch"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        seq_len = params.get("seq_len", 60)
        if not isinstance(seq_len, int) or seq_len < 10:
            return False, "seq_len 必须为 >=10 的整数"
        return True, ""

    def save_model(self, model: Any, path: str) -> None:
        """Persist either a Torch checkpoint or the sklearn fallback."""
        import os

        os.makedirs(os.path.dirname(path), exist_ok=True)
        torch = import_optional_torch()
        if torch is not None:
            if isinstance(model, torch.nn.Module):
                torch.save(
                    {
                        "kind": "lstm_rank_torch",
                        "config": model._quant_model_config,
                        "state_dict": {
                            key: value.detach().cpu()
                            for key, value in model.state_dict().items()
                        },
                    },
                    path,
                )
                return
        super().save_model(model, path)

    def load_model(self, path: str) -> Any:
        torch = import_optional_torch()
        verified_format = getattr(self, "_verified_model_serialization", None)
        if verified_format == "torch-state-dict-v1":
            if torch is None:
                raise RuntimeError("PyTorch is required for a verified torch model")
            try:
                checkpoint = torch.load(path, map_location="cpu", weights_only=True)
            except (RuntimeError, TypeError, ValueError) as exc:
                raise RuntimeError("verified torch state-dict checkpoint is invalid") from exc
            if not (
                isinstance(checkpoint, dict)
                and checkpoint.get("kind") == "lstm_rank_torch"
                and isinstance(checkpoint.get("config"), dict)
                and isinstance(checkpoint.get("state_dict"), dict)
            ):
                raise RuntimeError("verified torch checkpoint has an invalid schema")
            model = _build_lstm_model(**checkpoint["config"])
            model.load_state_dict(checkpoint["state_dict"])
            model.eval()
            self._is_sklearn_fallback = False
            return model
        if verified_format == "joblib-platform-v1" or verified_format == "legacy-platform-joblib-v0":
            self._is_sklearn_fallback = True
            return super().load_model(path)
        if torch is not None:
            try:
                checkpoint = torch.load(path, map_location="cpu", weights_only=True)
                if (
                    isinstance(checkpoint, dict)
                    and checkpoint.get("kind") == "lstm_rank_torch"
                ):
                    model = _build_lstm_model(**checkpoint["config"])
                    model.load_state_dict(checkpoint["state_dict"])
                    model.eval()
                    self._is_sklearn_fallback = False
                    return model
            except (RuntimeError, TypeError):
                pass
        self._is_sklearn_fallback = True
        return super().load_model(path)

    # ── 训练 ────────────────────────────────────────────────────────

    def train(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        existing_model: Optional[Any] = None,
    ) -> TrainedModel:
        """训练 LSTM（或 sklearn MLP）模型."""
        np.random.seed(42)
        seq_len: int = params.get("seq_len", 60)
        hidden_size: int = params.get("hidden_size", 64)
        num_layers: int = params.get("num_layers", 2)
        dropout: float = params.get("dropout", 0.1)
        learning_rate: float = params.get("learning_rate", 0.001)
        epochs: int = params.get("epochs", 50)

        # Only a genuinely absent PyTorch package enables the sklearn fallback.
        # Native loader errors (for example torch._C DLL failures on Windows)
        # remain visible so a broken runtime is never reported as usable.
        torch = import_optional_torch()
        if torch is not None:
            torch.manual_seed(42)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(42)
            self._use_torch = True
            self._is_sklearn_fallback = False
        else:
            self._use_torch = False
            self._is_sklearn_fallback = True
            logger.warning("PyTorch 不可用，降级为 sklearn MLPClassifier")

        if progress_callback:
            progress_callback(0.05, "正在构建序列样本...")

        # 构建序列样本
        X, y = self._build_sequences(pivot, seq_len, train_start, train_end)
        X_validation = np.array([], dtype=np.float32)
        y_validation = np.array([], dtype=np.float32)
        if (
            self._active_validation_context is not None
            and self._active_validation_context.has_validation
        ):
            validation_start = self._active_validation_context.validation_start
            validation_end = self._active_validation_context.validation_end
            assert validation_start is not None and validation_end is not None
            dates = pd.DatetimeIndex(pd.to_datetime(pivot.index)).sort_values().unique()
            validation_position = int(
                dates.searchsorted(pd.Timestamp(validation_start), side="left")
            )
            lookback_start = dates[max(0, validation_position - seq_len - 1)]
            X_validation, y_validation = self._build_sequences(
                pivot,
                seq_len,
                str(lookback_start.date()),
                validation_end,
                sample_start=validation_start,
            )
            if len(X_validation) == 0:
                raise ValueError("验证窗口未产生完整标签样本")

        if len(X) == 0 or len(y) == 0:
            raise ValueError("训练数据为空")

        if progress_callback:
            progress_callback(0.10, f"样本数: {len(X)}, 特征维度: {seq_len}")

        if self._use_torch:
            # PyTorch LSTM
            input_size = 1  # 单变量: 日收益率
            X_reshaped = X.reshape(X.shape[0], X.shape[1], 1)

            model = _build_lstm_model(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                dropout=dropout,
                learning_rate=learning_rate,
            )

            train_metrics = _train_lstm(
                model,
                X_reshaped,
                y,
                epochs,
                learning_rate,
                progress_callback,
                X_validation=(
                    X_validation.reshape(
                        X_validation.shape[0],
                        X_validation.shape[1],
                        1,
                    )
                    if len(X_validation)
                    else None
                ),
                y_validation=y_validation if len(y_validation) else None,
            )

            self._model = model
            model_type = "LSTM (PyTorch)"
            model_implementation = "lstm"
            model_backend = "pytorch"
            fallback_used = False

        else:
            # sklearn MLP fallback
            from sklearn.neural_network import MLPClassifier

            # 标签二值化: 涨(>0) vs 跌(<=0)
            y_binary = (y > 0).astype(int)

            mlp = MLPClassifier(
                hidden_layer_sizes=(hidden_size, hidden_size // 2),
                activation="relu",
                solver="adam",
                alpha=0.0001,
                batch_size=64,
                learning_rate_init=learning_rate,
                max_iter=epochs,
                early_stopping=False,
                random_state=42,
            )

            if progress_callback:
                progress_callback(0.20, f"正在训练 sklearn MLP (样本数: {len(X)})...")

            X_flat = X.reshape(X.shape[0], -1)
            mlp.fit(X_flat, y_binary)

            self._model = mlp
            train_metrics = {
                "train_loss": mlp.loss_,
                "epochs_trained": mlp.n_iter_,
            }
            model_type = "MLP (sklearn fallback)"
            model_implementation = "mlp_classifier"
            model_backend = "sklearn"
            fallback_used = True

            if progress_callback:
                progress_callback(0.95, "MLP 训练完成")

        if progress_callback:
            progress_callback(1.0, "训练完成")

        return TrainedModel(
            model=self._model,
            train_metrics={
                **train_metrics,
                "n_samples": len(X),
                "model_type": model_type,
                "model_implementation": model_implementation,
                "model_backend": model_backend,
                "fallback_used": fallback_used,
            },
            metadata={
                "train_start": train_start,
                "train_end": train_end,
                "params": params,
                "seq_len": seq_len,
            },
        )

    # ── TrainableStrategy 钩子（平台驱动 Walk-Forward）────────────────

    def get_universe(self, pivot: pd.DataFrame, params: dict) -> list[str]:
        return self._extract_codes(pivot)

    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        """在指定窗口训练 LSTM（或 sklearn MLP 降级）模型."""
        trained = self.train(
            pivot,
            params,
            train_start,
            train_end,
            progress_callback=None,
        )
        self.record_train_metrics(**trained.train_metrics)
        self._model = trained.model
        return trained.model

    def fit_with_validation(
        self,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> Any:
        """Use only the explicit temporal validation window for early stopping."""
        self._active_validation_context = context
        try:
            return super().fit_with_validation(pivot, params, context)
        finally:
            self._active_validation_context = None

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        """对每只股票取最近 seq_len 天收益率序列打分."""
        seq_len: int = params.get("seq_len", 60)
        scores: dict[str, float] = {}
        for code in self.get_universe(pivot, params):
            close = self._get_close_series(pivot, code)
            if close is None or len(close) < seq_len + 1:
                continue

            close = close[:as_of_date]
            if len(close) < seq_len:
                continue

            rets = close.pct_change().dropna().tail(seq_len)
            if len(rets) < seq_len:
                continue

            seq = rets.values.astype(np.float32)

            try:
                if self._is_sklearn_fallback:
                    # sklearn MLP 预测
                    proba = model.predict_proba(seq.reshape(1, -1))
                    pred = float(proba[0, 1]) if proba.shape[1] > 1 else float(proba[0, 0])
                else:
                    # PyTorch 预测
                    import torch
                    device = next(model.parameters()).device
                    x = torch.tensor(seq.reshape(1, -1, 1), dtype=torch.float32).to(device)
                    model.eval()
                    with torch.no_grad():
                        pred = float(model(x).cpu().numpy()[0])
                scores[code] = pred
            except Exception:
                continue
        return scores

    # ── 内部辅助 ─────────────────────────────────────────────────────

    def _build_sequences(
        self,
        pivot: pd.DataFrame,
        seq_len: int,
        train_start: str,
        train_end: str,
        sample_start: Optional[str] = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """构建序列训练样本（向量化实现）."""
        codes = self._extract_codes(pivot)
        X_parts, y_parts = [], []

        for code in codes:
            close = self._get_close_series(pivot, code)
            if close is None or len(close) < seq_len + 22:
                continue

            rets = close.pct_change().dropna()
            train_mask = (rets.index >= train_start) & (rets.index <= train_end)
            rets_train = rets[train_mask]

            if len(rets_train) < seq_len + 22:
                continue

            rets_vals = rets_train.values.astype(np.float32)
            close_clipped = close.loc[rets_train.index].values.astype(np.float32)

            n = len(rets_vals) - seq_len - 21
            if n <= 0:
                continue

            # Pre-allocate and fill windows
            windows = np.empty((n, seq_len), dtype=np.float32)
            for i in range(n):
                windows[i] = rets_vals[i : i + seq_len]

            # Labels: 21-day forward return
            labels = close_clipped[seq_len + 21 : seq_len + 21 + n] / close_clipped[seq_len : seq_len + n] - 1.0
            if sample_start is not None:
                sample_dates = rets_train.index[seq_len : seq_len + n]
                sample_mask = sample_dates >= pd.Timestamp(sample_start)
                windows = windows[sample_mask]
                labels = labels[sample_mask]

            fine_mask = np.isfinite(windows).all(axis=1) & np.isfinite(labels)
            if fine_mask.any():
                X_parts.append(windows[fine_mask])
                y_parts.append(labels[fine_mask])

        if not X_parts:
            return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

        X = np.concatenate(X_parts, axis=0).astype(np.float32)
        y = np.concatenate(y_parts, axis=0).astype(np.float32)

        y_clip = np.clip(y, -0.5, 0.5)
        finite_mask = np.isfinite(y_clip)
        X = X[finite_mask]
        y = y_clip[finite_mask]

        return X, y

    @staticmethod
    def _extract_codes(pivot: pd.DataFrame) -> list[str]:
        if isinstance(pivot.columns, pd.MultiIndex):
            return sorted({c[0] for c in pivot.columns if isinstance(c, tuple)})
        if "code" in pivot.columns:
            return sorted(pivot["code"].unique())
        # Simple column names = stock codes (e.g., '000001', '000002')
        return sorted([str(c) for c in pivot.columns])

    @staticmethod
    def _get_close_series(pivot: pd.DataFrame, code: str) -> pd.Series | None:
        if isinstance(pivot.columns, pd.MultiIndex):
            for field in ["close", "Close", "CLOSE", "收盘"]:
                if (code, field) in pivot.columns:
                    return pivot[(code, field)].copy()
        if "close" in pivot.columns:
            return pivot["close"].copy()
        if code in pivot.columns:
            return pivot[code].copy()
        return None
