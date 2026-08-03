"""Ensure repository-local packages are importable for every pytest entrypoint."""

from __future__ import annotations

import importlib
import platform
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _preload_apple_silicon_test_runtime(
    *,
    platform_name: str | None = None,
    machine: str | None = None,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> ModuleType | None:
    """Initialize Torch before LightGBM in Apple Silicon pytest processes.

    The application imports LightGBM early because Windows requires it before
    Pandas/PyArrow.  On macOS, importing LightGBM first and Torch later leaves
    two OpenMP-backed native stacks unstable in the long-lived full-suite
    process.  Pytest can safely initialize Torch here before test collection;
    Windows remains untouched and therefore retains its required LightGBM-first
    application startup order.
    """
    target_platform = platform_name or sys.platform
    target_machine = machine or platform.machine()
    if target_platform != "darwin" or target_machine != "arm64":
        return None
    return importer("torch")


_preload_apple_silicon_test_runtime()


@pytest.fixture
def allowed_isolated_cpu_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    """Keep research unit tests independent of transient CI host pressure."""

    from backend.services import isolated_cpu
    from backend.services.isolated_cpu import IsolatedCpuExecutor

    class AllowedCapacity:
        @staticmethod
        def decide() -> SimpleNamespace:
            return SimpleNamespace(pause_heavy=False)

    executor = IsolatedCpuExecutor(
        capacity=AllowedCapacity(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(isolated_cpu, "_EXECUTOR", executor)
    return executor


def pytest_addoption(parser: pytest.Parser) -> None:
    """v0.3.0 契约锁定层：注册 --update-snapshots 逃生口选项。

    行为不变的重构禁止运行 --update-snapshots；有意变更端点/响应结构时，
    运行 scripts/update_contract_snapshots.py 并在 PR 描述中附变更清单。
    """
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="重新生成 backend/tests/snapshots/ 下的契约快照（有意变更时使用）",
    )
