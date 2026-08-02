#!/usr/bin/env python3
"""Read-only diagnostics for the platform's optional ML runtimes.

The doctor deliberately loads LightGBM before dataframe libraries.  Windows
and macOS both have native-library load-order failure modes that a diagnostic
must not accidentally introduce itself.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.strategies.ml.runtime import (  # noqa: E402
    preload_frame_safe_lightgbm,
    select_torch_device_name,
)

preload_frame_safe_lightgbm()

REQUIRED_PACKAGES = (
    "numpy",
    "pandas",
    "pyarrow",
    "sklearn",
    "lightgbm",
    "xgboost",
    "torch",
)


def _brew_path() -> str | None:
    candidates = (
        shutil.which("brew"),
        "/opt/homebrew/bin/brew",
        "/usr/local/bin/brew",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def _uv_path() -> str | None:
    candidates = (
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / "uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def _package_report(name: str) -> dict[str, Any]:
    try:
        module = importlib.import_module(name)
    except BaseException as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return {
        "ok": True,
        "version": getattr(module, "__version__", None),
    }


def _native_fit_smoke() -> dict[str, Any]:
    import numpy as np

    result: dict[str, Any] = {}
    features = np.arange(160, dtype=np.float32).reshape(40, 4)
    target = np.sin(np.arange(40, dtype=np.float32))

    try:
        import lightgbm as lgb

        model = lgb.LGBMRegressor(
            n_estimators=3,
            max_depth=2,
            n_jobs=1,
            verbose=-1,
        ).fit(features, target)
        result["lightgbm"] = {
            "ok": True,
            "prediction_finite": bool(
                np.isfinite(model.predict(features[:1])[0])
            ),
        }
    except BaseException as exc:
        result["lightgbm"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    try:
        import xgboost as xgb

        model = xgb.XGBRegressor(
            n_estimators=3,
            max_depth=2,
            n_jobs=1,
        ).fit(features, target)
        result["xgboost"] = {
            "ok": True,
            "prediction_finite": bool(
                np.isfinite(model.predict(features[:1])[0])
            ),
        }
    except BaseException as exc:
        result["xgboost"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return result


def _torch_report(*, run_fit_smoke: bool) -> dict[str, Any]:
    try:
        import torch
    except BaseException as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    try:
        selected_device = select_torch_device_name(torch)
    except (RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "version": getattr(torch, "__version__", None),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    mps = getattr(getattr(torch, "backends", None), "mps", None)
    report: dict[str, Any] = {
        "ok": True,
        "version": getattr(torch, "__version__", None),
        "selected_device": selected_device,
        "cuda_available": bool(torch.cuda.is_available()),
        "mps_available": bool(mps is not None and mps.is_available()),
    }
    if not run_fit_smoke:
        return report

    try:
        device = torch.device(selected_device)
        model = torch.nn.Sequential(
            torch.nn.Linear(4, 8),
            torch.nn.ReLU(),
            torch.nn.Linear(8, 1),
        ).to(device)
        optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
        features = torch.arange(
            64,
            dtype=torch.float32,
            device=device,
        ).reshape(16, 4)
        target = torch.sin(
            torch.arange(16, dtype=torch.float32, device=device)
        ).reshape(16, 1)
        optimizer.zero_grad()
        loss = ((model(features) - target) ** 2).mean()
        loss.backward()
        optimizer.step()
        report["fit_smoke"] = {
            "ok": True,
            "loss_finite": bool(torch.isfinite(loss).item()),
        }
    except BaseException as exc:
        report["ok"] = False
        report["fit_smoke"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return report


def build_report(*, run_fit_smoke: bool = False) -> dict[str, Any]:
    system = platform.system()
    machine = platform.machine().lower()
    errors: list[str] = []
    warnings: list[str] = []
    packages = {name: _package_report(name) for name in REQUIRED_PACKAGES}

    for name, evidence in packages.items():
        if not evidence["ok"]:
            errors.append(f"{name} failed to import")

    if system == "Darwin" and machine not in {"arm64", "aarch64"}:
        errors.append(
            "macOS process is not arm64; recreate the virtual environment "
            "outside Rosetta"
        )

    pip_available = importlib.util.find_spec("pip") is not None
    uv_path = _uv_path()
    if not pip_available and uv_path is None:
        errors.append("neither pip nor uv is available for environment repair")
    elif not pip_available:
        warnings.append(
            "the virtual environment has no pip; use `uv pip --python "
            ".venv/bin/python` for maintenance"
        )

    brew = _brew_path() if system == "Darwin" else None
    libomp_prefix = None
    if system == "Darwin":
        if brew is None:
            warnings.append(
                "Homebrew is not on PATH; include /opt/homebrew/bin for "
                "Apple Silicon launchd services"
            )
        else:
            probe = subprocess.run(
                [brew, "--prefix", "libomp"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if probe.returncode == 0:
                libomp_prefix = probe.stdout.strip()
            elif not packages["lightgbm"]["ok"] or not packages["xgboost"]["ok"]:
                errors.append("libomp is unavailable; run `brew install libomp`")

    torch_evidence = _torch_report(run_fit_smoke=run_fit_smoke)
    if not torch_evidence["ok"]:
        errors.append("Torch device smoke test failed")

    fit_smoke = None
    if run_fit_smoke and packages["numpy"]["ok"]:
        fit_smoke = _native_fit_smoke()
        for name, evidence in fit_smoke.items():
            if not evidence["ok"]:
                errors.append(f"{name} native fit smoke test failed")

    return {
        "schema_version": "ml-runtime-doctor/v1",
        "ok": not errors,
        "platform": {
            "system": system,
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "executable": sys.executable,
        },
        "environment": {
            "pip_available": pip_available,
            "uv_path": uv_path,
            "brew_path": brew,
            "libomp_prefix": libomp_prefix,
        },
        "packages": packages,
        "torch": torch_evidence,
        "native_fit_smoke": fit_smoke,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fit-smoke",
        action="store_true",
        help="run tiny LightGBM, XGBoost and Torch optimization steps",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat maintenance warnings as a failed readiness check",
    )
    args = parser.parse_args()
    report = build_report(run_fit_smoke=args.fit_smoke)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        return 1
    if args.strict and report["warnings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
