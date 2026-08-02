from __future__ import annotations

import numpy as np
import pandas as pd
import torch

from backend.strategies.ml.lstm_rank import (
    LSTMRankStrategy,
    _build_lstm_model,
)
from backend.strategies.ml.transformer_rank import (
    TransformerRankStrategy,
    _build_transformer_model,
)


def test_lstm_torch_model_round_trip(tmp_path):
    strategy = LSTMRankStrategy()
    model = _build_lstm_model(1, 16, 1, 0.1, 0.001)
    path = tmp_path / "lstm.joblib"

    strategy.save_model(model, str(path))
    restored = strategy.load_model(str(path))
    model.eval()
    restored.eval()

    sample = torch.zeros((2, 20, 1))
    with torch.no_grad():
        assert torch.allclose(model(sample), restored(sample))


def test_transformer_torch_model_round_trip(tmp_path):
    torch.manual_seed(42)
    strategy = TransformerRankStrategy()
    model = _build_transformer_model(1, 16, 1, 4, 0.1, 0.001)
    path = tmp_path / "transformer.joblib"

    strategy.save_model(model, str(path))
    restored = strategy.load_model(str(path))
    model.eval()
    restored.eval()

    sample = torch.zeros((2, 20, 1))
    with torch.no_grad():
        assert torch.allclose(model(sample), restored(sample))


def test_lstm_loaded_deployment_model_skips_retraining():
    class FakeClassifier:
        def predict_proba(self, values):
            return np.tile([0.25, 0.75], (len(values), 1))

    dates = pd.bdate_range("2019-01-02", "2025-01-31")
    pivot = pd.DataFrame(
        {
            ("000001", "close"): np.linspace(10.0, 30.0, len(dates)),
            ("000002", "close"): np.linspace(20.0, 35.0, len(dates)),
        },
        index=dates,
    )
    pivot.columns = pd.MultiIndex.from_tuples(pivot.columns)

    strategy = LSTMRankStrategy()
    strategy._model = FakeClassifier()
    strategy._is_sklearn_fallback = True

    def fail_train(*args, **kwargs):
        raise AssertionError("deployed model must not retrain")

    strategy.train = fail_train
    signals = strategy.generate_batch_signals(
        pivot,
        {
            "seq_len": 20,
            "hidden_size": 32,
            "num_layers": 1,
            "dropout": 0.1,
            "learning_rate": 0.001,
            "epochs": 10,
            "top_k_pct": 0.5,
            "retrain_months": 6,
            "min_train_months": 24,
            "_train_start": "2019-01-02",
            "_train_end": "2024-12-31",
        },
        "2025-01-02",
        "2025-01-31",
    )
    assert signals
