"""Transformer 深度学习排序策略 —— 自注意力序列预测 + Walk-Forward.

如果 PyTorch 不可用，降级为 sklearn RandomForestClassifier。
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from backend.config import settings
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


def _portable_relu(tensor: Any) -> Any:
    """Use the public ReLU path instead of macOS's unstable fused fast path."""
    import torch

    return torch.relu(tensor)

# ═══════════════════════════════════════════════════════════════════════════
# 参数 Schema
# ═══════════════════════════════════════════════════════════════════════════

PARAM_SCHEMA: list[ParamField] = [
    ParamField(
        name="seq_len",
        type="int",
        default=60,
        description="输入序列长度（日）",
        min=20,
        max=120,
        step=10,
    ),
    ParamField(
        name="hidden_size",
        type="int",
        default=64,
        description="Transformer 隐藏层大小",
        min=16,
        max=256,
        step=16,
    ),
    ParamField(
        name="num_layers",
        type="int",
        default=2,
        description="Transformer 编码层数",
        min=1,
        max=4,
        step=1,
    ),
    ParamField(
        name="nhead",
        type="int",
        default=8,
        description="多头注意力头数",
        min=2,
        max=16,
        step=2,
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
        default=50,
        description="训练轮数",
        min=10,
        max=200,
        step=10,
    ),
    ParamField(
        name="top_k_pct",
        type="float",
        default=0.10,
        description="买入股票比例（Top K%）",
        min=0.05,
        max=0.3,
        step=0.01,
    ),
] + ml_training_params(periodic=False, min_train_months=12)


# ═══════════════════════════════════════════════════════════════════════════
# PyTorch Transformer 模型
# ═══════════════════════════════════════════════════════════════════════════


def _build_transformer_model(
    input_size: int,
    hidden_size: int,
    num_layers: int,
    nhead: int,
    dropout: float,
    learning_rate: float,
    platform_name: str | None = None,
) -> Any:
    """构建 PyTorch Transformer 编码器模型."""
    import torch
    import torch.nn as nn

    runtime_platform = platform_name or sys.platform

    class PositionalEncoding(nn.Module):
        def __init__(self, d_model, max_len=200, dropout=0.1):
            super().__init__()
            self.dropout = nn.Dropout(p=dropout)
            pe = torch.zeros(max_len, d_model)
            position = torch.arange(0, max_len).unsqueeze(1).float()
            div_term = torch.exp(
                torch.arange(0, d_model, 2).float()
                * -(np.log(10000.0) / d_model)
            )
            pe[:, 0::2] = torch.sin(position * div_term)
            pe[:, 1::2] = torch.cos(position * div_term)
            pe = pe.unsqueeze(0)
            self.register_buffer("pe", pe)

        def forward(self, x):
            x = x + self.pe[:, : x.size(1), :]
            return self.dropout(x)

    class TransformerRanker(nn.Module):
        def __init__(self, input_size, hidden_size, num_layers, nhead, dropout):
            super().__init__()
            self.input_proj = nn.Linear(input_size, hidden_size)
            self.pos_encoder = PositionalEncoding(hidden_size, max_len=200, dropout=dropout)

            encoder_layer = nn.TransformerEncoderLayer(
                d_model=hidden_size,
                nhead=nhead,
                dropout=dropout,
                batch_first=True,
                activation=_portable_relu
                if runtime_platform == "darwin"
                else "relu",
            )
            self.encoder = nn.TransformerEncoder(
                encoder_layer,
                num_layers=num_layers,
                enable_nested_tensor=runtime_platform != "darwin",
            )

            self.fc = nn.Sequential(
                nn.Linear(hidden_size, hidden_size // 2),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(hidden_size // 2, 1),
            )

        def forward(self, x):
            x = self.input_proj(x)
            x = self.pos_encoder(x)
            out = self.encoder(x)
            out = out.mean(dim=1)  # 全局平均池化
            return self.fc(out).squeeze(-1)

    model = TransformerRanker(input_size, hidden_size, num_layers, nhead, dropout)
    model._quant_model_config = {
        "input_size": input_size,
        "hidden_size": hidden_size,
        "num_layers": num_layers,
        "nhead": nhead,
        "dropout": dropout,
        "learning_rate": learning_rate,
    }
    return model


def _train_transformer(
    model: Any,
    X: np.ndarray,
    y: np.ndarray,
    epochs: int,
    learning_rate: float,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    X_validation: Optional[np.ndarray] = None,
    y_validation: Optional[np.ndarray] = None,
) -> dict:
    """训练 Transformer 模型."""
    import torch

    device = torch.device(select_torch_device_name(torch))
    model = model.to(device)

    # Keep the complete panel on CPU and transfer mini-batches only.  A
    # full-split Transformer validation pass materializes attention tensors
    # far beyond an 8 GiB GPU on the 429-stock universe.
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
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=20, gamma=0.5)

    # Attention memory grows quadratically with sequence length.  These
    # universe-sized batches keep the 120-day candidate below 8 GiB while
    # avoiding thousands of tiny kernel launches for shorter sequences.
    max_batch_size = 128 if X.shape[1] <= 60 else 64
    batch_size = min(max_batch_size, len(X_train))
    best_val_loss = float("inf")
    best_state: Optional[dict[str, Any]] = None
    patience = 10
    patience_counter = 0

    for epoch in range(epochs):
        model.train()
        total_loss = 0.0
        indices = torch.randperm(len(X_train))
        n_batches = max(1, len(X_train) // batch_size)

        for i in range(0, len(X_train), batch_size):
            batch_idx = indices[i : i + batch_size]
            X_batch = X_train[batch_idx].to(device)
            y_batch = y_train[batch_idx].to(device)

            optimizer.zero_grad()
            pred = model(X_batch)
            loss = criterion(pred, y_batch)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += loss.item()

        scheduler.step()
        avg_train_loss = total_loss / n_batches

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


class TransformerRankStrategy(TrainableStrategy):
    """Transformer 深度学习排序策略。

    策略原理:
        使用 Transformer 编码器（基于自注意力机制）处理股票的日收益率时间序列。
        与 LSTM 不同，Transformer 通过多头自注意力机制可以同时关注序列中的所有位置，
        直接学习任意两天之间的全局依赖关系（如第 1 天的事件对第 60 天的影响）。
        多头注意力能从不同角度（趋势、波动、反转等）并行分析序列。
        在固定历史窗口训练一次并复用模型，持续预测 Top K% 股票。

    降级方案:
        如果 PyTorch 不可用，自动降级为 sklearn RandomForestClassifier。

    注意:
        Transformer 需要更多数据才能发挥优势；
        在 100 只股票规模下可能不如 LSTM 稳健。
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
            strategy_id="transformer_rank_v1",
            display_name="Transformer 深度学习排序策略",
            version="1.0.0",
            category=StrategyCategory.ML,
            description=(
                "基于 Transformer 自注意力机制的深度学习排序策略。"
                "将股票过去 N 日日收益率序列输入 Transformer 编码器，"
                "通过多头自注意力同时关注序列所有位置的全局依赖关系，"
                "预测下月上涨概率。模型在固定历史窗口完成一次训练并在实验期复用。"
                "相比 LSTM，Transformer 能并行处理序列并捕捉长距离依赖。"
                "若 PyTorch 不可用则自动降级为 sklearn RandomForest。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=True,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=600,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            supported_position_modes=["equal_weight"],
            tags=["深度学习", "Transformer", "自注意力", "排序学习", "一次训练", "PyTorch"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        seq_len = params.get("seq_len", 60)
        if not isinstance(seq_len, int) or seq_len < 10:
            return False, "seq_len 必须为 >=10 的整数"
        nhead = params.get("nhead", 8)
        hidden_size = params.get("hidden_size", 64)
        if hidden_size % nhead != 0:
            return False, "hidden_size 必须能被 nhead 整除"
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
                        "kind": "transformer_rank_torch",
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
                and checkpoint.get("kind") == "transformer_rank_torch"
                and isinstance(checkpoint.get("config"), dict)
                and isinstance(checkpoint.get("state_dict"), dict)
            ):
                raise RuntimeError("verified torch checkpoint has an invalid schema")
            model = _build_transformer_model(**checkpoint["config"])
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
                    and checkpoint.get("kind") == "transformer_rank_torch"
                ):
                    model = _build_transformer_model(**checkpoint["config"])
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
        """训练 Transformer（或 sklearn RandomForest）."""
        np.random.seed(42)
        seq_len: int = params.get("seq_len", 60)
        hidden_size: int = params.get("hidden_size", 64)
        num_layers: int = params.get("num_layers", 2)
        nhead: int = params.get("nhead", 8)
        dropout: float = params.get("dropout", 0.1)
        learning_rate: float = params.get("learning_rate", 0.001)
        epochs: int = params.get("epochs", 50)

        # Keep the sklearn fallback for an absent package only. A broken native
        # PyTorch runtime must fail loudly instead of silently changing models.
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
            logger.warning("PyTorch 不可用，降级为 sklearn RandomForestClassifier")

        if progress_callback:
            progress_callback(0.05, "正在构建序列样本...")

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
            input_size = 1
            X_reshaped = X.reshape(X.shape[0], X.shape[1], 1)

            model = _build_transformer_model(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                nhead=nhead,
                dropout=dropout,
                learning_rate=learning_rate,
            )

            train_metrics = _train_transformer(
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
            model_type = "Transformer (PyTorch)"
            model_implementation = "transformer"
            model_backend = "pytorch"
            fallback_used = False

        else:
            # sklearn RandomForest fallback
            from sklearn.ensemble import RandomForestClassifier

            y_binary = (y > 0).astype(int)

            rf = RandomForestClassifier(
                n_estimators=min(200, epochs * 4),
                max_depth=max(3, num_layers * 2),
                min_samples_leaf=10,
                random_state=42,
                n_jobs=max(int(settings.JOB_CPU_THREAD_BUDGET), 1),
            )

            if progress_callback:
                progress_callback(0.20, f"正在训练 RandomForest (样本数: {len(X)})...")

            X_flat = X.reshape(X.shape[0], -1)
            rf.fit(X_flat, y_binary)

            self._model = rf
            train_metrics = {
                "train_score": float(rf.score(X_flat, y_binary)),
                "n_estimators": rf.n_estimators,
            }
            model_type = "RandomForest (sklearn fallback)"
            model_implementation = "random_forest_classifier"
            model_backend = "sklearn"
            fallback_used = True

            if progress_callback:
                progress_callback(0.95, "RandomForest 训练完成")

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
        """在指定窗口训练 Transformer（或 sklearn RandomForest 降级）模型."""
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
                    proba = model.predict_proba(seq.reshape(1, -1))
                    pred = float(proba[0, 1]) if proba.shape[1] > 1 else float(proba[0, 0])
                else:
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

            windows = np.empty((n, seq_len), dtype=np.float32)
            for i in range(n):
                windows[i] = rets_vals[i : i + seq_len]

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
