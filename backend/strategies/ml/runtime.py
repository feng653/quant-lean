"""Cross-platform runtime helpers for optional ML accelerators and libraries."""

from __future__ import annotations

import importlib
import os
import platform
import sys
from types import ModuleType
from typing import Any

_STRATEGY_NATIVE_RUNTIME = {
    "alpha158_lgb_v1": "lightgbm",
    "alpha158_rank_lgb_v1": "lightgbm",
    "alpha158_xgb_v1": "xgboost",
    "lstm_rank_v1": "torch",
    "transformer_rank_v1": "torch",
}


def _is_missing_top_level_package(exc: ModuleNotFoundError, package: str) -> bool:
    """Distinguish an absent optional package from a broken native dependency."""
    return exc.name == package


def preload_windows_lightgbm(platform_name: str | None = None) -> ModuleType | None:
    """Load LightGBM before pandas/pyarrow on Windows.

    A genuinely absent optional package is allowed so the application can still
    expose the strategy catalogue. ImportError/OSError from an installed but
    broken native runtime must remain visible instead of creating false
    availability.
    """
    if (platform_name or sys.platform) != "win32":
        return None
    try:
        return importlib.import_module("lightgbm")
    except ModuleNotFoundError as exc:
        if _is_missing_top_level_package(exc, "lightgbm"):
            return None
        raise


def preload_frame_safe_lightgbm(
    platform_name: str | None = None,
) -> ModuleType | None:
    """Load LightGBM before PyArrow on platforms with fragile native ordering.

    Windows keeps its fail-fast behavior for broken installed runtimes. On
    macOS LightGBM remains optional: a missing or broken native dependency must
    not prevent rule-based and PyTorch strategies from starting.
    """
    target = platform_name or sys.platform
    if target == "win32":
        return preload_windows_lightgbm(target)
    if target != "darwin":
        return None
    try:
        return importlib.import_module("lightgbm")
    except (ImportError, OSError):
        return None


def import_lightgbm(platform_name: str | None = None) -> ModuleType:
    """Load LightGBM on demand with actionable macOS OpenMP guidance."""
    try:
        return importlib.import_module("lightgbm")
    except ModuleNotFoundError as exc:
        if not _is_missing_top_level_package(exc, "lightgbm"):
            raise
        raise ImportError(
            "LightGBM is unavailable. Install it with: pip install lightgbm"
        ) from exc
    except OSError as exc:
        if (platform_name or sys.platform) == "darwin":
            raise ImportError(
                "LightGBM native library failed to load on macOS. "
                "Install the OpenMP runtime with: brew install libomp"
            ) from exc
        raise


def import_optional_torch() -> ModuleType | None:
    """Import optional PyTorch without hiding native-loader failures.

    Only a missing top-level ``torch`` package enables the documented sklearn
    fallback. ImportError/OSError raised while importing an installed PyTorch
    build is deliberately propagated, which is especially important for
    Windows ``torch._C`` DLL diagnostics.
    """
    try:
        return importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        if _is_missing_top_level_package(exc, "torch"):
            return None
        raise


def select_torch_device_name(
    torch_module: Any,
    requested: str | None = None,
) -> str:
    """Resolve an available Torch device with an explicit fail-closed override.

    ``ML_TORCH_DEVICE`` accepts ``auto``, ``cpu``, ``cuda`` or ``mps``.  The
    override is useful on memory-constrained Apple Silicon hosts where a
    particular PyTorch release may expose MPS but a production workload still
    needs a temporary CPU fallback.  An unavailable explicitly requested
    accelerator is an error instead of silently changing the research runtime.
    """
    selected = (
        requested
        if requested is not None
        else os.environ.get("ML_TORCH_DEVICE", "auto")
    )
    selected = str(selected).strip().lower()
    if selected not in {"auto", "cpu", "cuda", "mps"}:
        raise ValueError(
            "ML_TORCH_DEVICE must be one of: auto, cpu, cuda, mps"
        )
    if selected == "cpu":
        return "cpu"

    cuda_available = bool(torch_module.cuda.is_available())
    backends = getattr(torch_module, "backends", None)
    mps = getattr(backends, "mps", None)
    mps_available = bool(mps is not None and mps.is_available())

    if selected == "cuda":
        if not cuda_available:
            raise RuntimeError(
                "ML_TORCH_DEVICE=cuda was requested, but CUDA is unavailable"
            )
        return "cuda"
    if selected == "mps":
        if not mps_available:
            raise RuntimeError(
                "ML_TORCH_DEVICE=mps was requested, but Apple MPS is unavailable"
            )
        return "mps"

    if cuda_available:
        return "cuda"
    if mps_available:
        return "mps"
    return "cpu"


def import_xgboost() -> ModuleType:
    """Import XGBoost and turn native-loader failures into actionable guidance."""
    try:
        return importlib.import_module("xgboost")
    except ImportError as exc:
        raise ImportError(
            "XGBoost is unavailable. Install it with: pip install xgboost"
        ) from exc
    except OSError as exc:
        hint = (
            " Install the OpenMP runtime with: brew install libomp"
            if platform.system() == "Darwin"
            else " Reinstall XGBoost and its native runtime for this platform."
        )
        raise ImportError(f"XGBoost native library failed to load.{hint}") from exc


def preload_strategy_native_runtime(strategy_id: str) -> ModuleType | None:
    """Load a strategy's native dependency on the main worker thread.

    Strategy execution itself runs in a thread-pool executor. Loading native
    ML libraries for the first time inside that executor can crash the process
    on macOS before Python can surface an import error.
    """
    runtime = _STRATEGY_NATIVE_RUNTIME.get(strategy_id)
    if runtime == "lightgbm":
        return import_lightgbm()
    if runtime == "xgboost":
        return import_xgboost()
    if runtime == "torch":
        return import_optional_torch()
    return None
