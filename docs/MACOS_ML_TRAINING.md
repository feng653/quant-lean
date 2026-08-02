# Apple Silicon ML training

This guide covers the native ML runtime used by experiments on M-series Macs.
It does not change the dataset, research manifest, locked-test or promotion
gates.

## Reproducible setup

Run the bootstrap from the repository root in a native arm64 terminal:

```bash
./scripts/bootstrap_macos_ml.sh
```

The script:

- refuses an x86_64/Rosetta Python;
- installs Homebrew `libomp` when it is missing;
- preserves an existing arm64 virtual environment;
- installs the validated versions in `requirements-macos-arm64.txt`;
- runs native LightGBM, XGBoost and Torch optimization smoke tests.

`uv` environments do not necessarily contain `pip`. This is valid, but they
must be maintained with:

```bash
uv pip install --python .venv/bin/python \
  --requirement requirements-macos-arm64.txt
```

Do not run `.venv/bin/python -m pip` unless the doctor reports that pip exists.

## Diagnose before retrying an experiment

```bash
.venv/bin/python scripts/ml_runtime_doctor.py --fit-smoke
```

The output is machine-readable JSON and never reads `.env`, databases, cached
market data or credentials.

For launchd, include Apple Silicon Homebrew in `PATH`:

```text
/Users/<user>/<project>/.venv/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

Then verify the daemon uses the intended virtual environment:

```bash
launchctl print system/com.quant-platform.backend
```

## Torch device control

The default `ML_TORCH_DEVICE=auto` order is CUDA, Apple MPS, then CPU. A
temporary CPU fallback can isolate MPS release-specific problems:

```bash
ML_TORCH_DEVICE=cpu .venv/bin/python scripts/ml_runtime_doctor.py --fit-smoke
```

Accepted values are `auto`, `cpu`, `cuda` and `mps`. Explicitly requesting an
unavailable accelerator fails closed; the platform does not silently train on
a different device.

Changing the device can change floating-point results. Record the override in
the deployment environment and rerun the experiment rather than reusing an
artifact produced on another device.

## Failure triage

Classify the first traceback frame before changing native dependencies:

| Failure location | Meaning | Action |
| --- | --- | --- |
| `research_snapshots.py` | immutable input serialization/preflight | do not rebuild data; preserve the error and fix the round-trip contract |
| `research_manifest.py` | execution/reproducibility preflight | fix manifest serialization; the model has not trained yet |
| `lightgbm` / `xgboost` import | OpenMP or wrong-architecture wheel | verify arm64, install `libomp`, rerun the doctor |
| Torch operation on `mps` | device/runtime issue | reproduce with the doctor, then compare `ML_TORCH_DEVICE=cpu` |
| `walkforward.py` or strategy `fit()` | actual model training | inspect train/validation windows and the original exception |

Never delete caches, overwrite experiment databases, or downgrade integrity
checks merely to make a training task complete.

## 2026-07-29 M-series acceptance

The deployment host was verified as macOS arm64 with Python 3.11. The installed
LightGBM, XGBoost and Torch runtimes imported successfully; LightGBM/XGBoost
native fits and Torch MPS forward/backward passed. Using existing cached market
data in an isolated temporary directory, immutable pivot/benchmark snapshots,
execution manifest resolution, LSTM, Transformer, Alpha158-LightGBM and
Alpha158-XGBoost fits all completed.

Historical failed experiments were blocked before model training by snapshot
timestamp round-trip and enum serialization defects. The current code contains
the corresponding fixes. Those old failed rows remain historical evidence and
must not be rewritten as successful experiments.
