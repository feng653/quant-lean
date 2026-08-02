"""Platform compatibility tests for optional ML runtimes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from backend.strategies.ml import transformer_rank
from backend.strategies.ml.runtime import (
    import_lightgbm,
    import_optional_torch,
    import_xgboost,
    preload_frame_safe_lightgbm,
    preload_strategy_native_runtime,
    preload_windows_lightgbm,
    select_torch_device_name,
)


class _Availability:
    def __init__(self, available: bool) -> None:
        self.available = available

    def is_available(self) -> bool:
        return self.available


def _fake_torch(*, cuda: bool, mps: bool | None) -> SimpleNamespace:
    backends = SimpleNamespace()
    if mps is not None:
        backends.mps = _Availability(mps)
    return SimpleNamespace(cuda=_Availability(cuda), backends=backends)


def test_torch_device_keeps_cuda_as_first_choice() -> None:
    assert select_torch_device_name(_fake_torch(cuda=True, mps=True)) == "cuda"


def test_torch_device_selects_mps_on_apple_silicon() -> None:
    assert select_torch_device_name(_fake_torch(cuda=False, mps=True)) == "mps"


def test_torch_device_falls_back_when_mps_backend_is_missing() -> None:
    assert select_torch_device_name(_fake_torch(cuda=False, mps=None)) == "cpu"


def test_torch_device_can_force_cpu_when_mps_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ML_TORCH_DEVICE", "cpu")

    assert select_torch_device_name(_fake_torch(cuda=False, mps=True)) == "cpu"


def test_torch_device_rejects_unavailable_explicit_accelerator() -> None:
    with pytest.raises(RuntimeError, match="Apple MPS is unavailable"):
        select_torch_device_name(
            _fake_torch(cuda=False, mps=False),
            requested="mps",
        )


def test_torch_device_rejects_unknown_override() -> None:
    with pytest.raises(ValueError, match="ML_TORCH_DEVICE"):
        select_torch_device_name(
            _fake_torch(cuda=False, mps=False),
            requested="metal",
        )


def test_transformer_uses_portable_attention_path_on_macos(
) -> None:
    model = transformer_rank._build_transformer_model(
        1,
        16,
        1,
        4,
        0.1,
        0.001,
        platform_name="darwin",
    )
    encoder_layer = model.encoder.layers[0]

    assert model.encoder.enable_nested_tensor is False
    assert encoder_layer.activation_relu_or_gelu == 0


def test_xgboost_loader_gives_macos_openmp_guidance() -> None:
    with patch(
        "backend.strategies.ml.runtime.platform.system", return_value="Darwin"
    ):
        with patch(
            "backend.strategies.ml.runtime.importlib.import_module",
            side_effect=OSError("libomp.dylib"),
        ):
            with pytest.raises(ImportError, match="brew install libomp"):
                import_xgboost()


def test_xgboost_loader_does_not_give_brew_guidance_on_windows() -> None:
    with patch(
        "backend.strategies.ml.runtime.platform.system", return_value="Windows"
    ):
        with patch(
            "backend.strategies.ml.runtime.importlib.import_module",
            side_effect=OSError("DLL load failed"),
        ):
            with pytest.raises(ImportError, match="native runtime") as exc_info:
                import_xgboost()

    assert "brew" not in str(exc_info.value)


def _missing_package(name: str) -> ModuleNotFoundError:
    return ModuleNotFoundError(f"No module named '{name}'", name=name)


def test_windows_preload_is_noop_on_other_platforms() -> None:
    with patch("backend.strategies.ml.runtime.importlib.import_module") as importer:
        assert preload_windows_lightgbm("darwin") is None
    importer.assert_not_called()


def test_windows_preload_allows_only_a_genuinely_missing_lightgbm() -> None:
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module",
        side_effect=_missing_package("lightgbm"),
    ):
        assert preload_windows_lightgbm("win32") is None


@pytest.mark.parametrize(
    "failure",
    [
        ImportError("DLL load failed while importing _lightgbm"),
        OSError("lib_lightgbm.dll could not be loaded"),
    ],
)
def test_windows_preload_preserves_broken_native_runtime_errors(
    failure: BaseException,
) -> None:
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module",
        side_effect=failure,
    ):
        with pytest.raises(type(failure)) as exc_info:
            preload_windows_lightgbm("win32")
    assert exc_info.value is failure


def test_macos_frame_safe_preload_tolerates_optional_runtime_failure() -> None:
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module",
        side_effect=OSError("libomp is unavailable"),
    ):
        assert preload_frame_safe_lightgbm("darwin") is None


def test_frame_safe_preload_is_lazy_on_linux() -> None:
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module"
    ) as import_module:
        assert preload_frame_safe_lightgbm("linux") is None
    import_module.assert_not_called()


def test_lightgbm_loader_gives_macos_openmp_guidance() -> None:
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module",
        side_effect=OSError("dlopen(libomp.dylib)"),
    ):
        with pytest.raises(ImportError, match="brew install libomp") as exc_info:
            import_lightgbm("darwin")
    assert isinstance(exc_info.value.__cause__, OSError)


def test_lightgbm_loader_preserves_windows_oserror() -> None:
    failure = OSError("DLL load failed")
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module",
        side_effect=failure,
    ):
        with pytest.raises(OSError) as exc_info:
            import_lightgbm("win32")
    assert exc_info.value is failure


def test_optional_torch_falls_back_only_when_package_is_absent() -> None:
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module",
        side_effect=_missing_package("torch"),
    ):
        assert import_optional_torch() is None


@pytest.mark.parametrize(
    "failure",
    [
        ImportError("DLL load failed while importing torch._C"),
        OSError("WinError 126 loading torch._C"),
    ],
)
def test_optional_torch_preserves_native_loader_errors(
    failure: BaseException,
) -> None:
    with patch(
        "backend.strategies.ml.runtime.importlib.import_module",
        side_effect=failure,
    ):
        with pytest.raises(type(failure)) as exc_info:
            import_optional_torch()
    assert exc_info.value is failure


@pytest.mark.parametrize(
    ("strategy_id", "loader_name"),
    [
        ("alpha158_lgb_v1", "import_lightgbm"),
        ("alpha158_rank_lgb_v1", "import_lightgbm"),
        ("alpha158_xgb_v1", "import_xgboost"),
        ("lstm_rank_v1", "import_optional_torch"),
        ("transformer_rank_v1", "import_optional_torch"),
    ],
)
def test_strategy_native_runtime_is_preloaded(
    strategy_id: str,
    loader_name: str,
) -> None:
    sentinel = ModuleType("sentinel")
    with patch(
        f"backend.strategies.ml.runtime.{loader_name}",
        return_value=sentinel,
    ) as loader:
        assert preload_strategy_native_runtime(strategy_id) is sentinel
    loader.assert_called_once_with()


def test_rule_strategy_has_no_native_runtime_preload() -> None:
    assert preload_strategy_native_runtime("ma_cross_v1") is None


def test_backend_import_and_registry_scan_are_native_runtime_safe(
    tmp_path: Path,
) -> None:
    """Probe a clean interpreter without starting FastAPI's DB-writing lifespan."""
    project_root = Path(__file__).resolve().parents[2]
    probe = textwrap.dedent(
        r"""
        import importlib.abc
        import importlib.util
        import inspect
        import json
        import os
        from pathlib import Path
        import sys

        events = []

        class MissingLightGBMLoader(importlib.abc.Loader):
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                raise ModuleNotFoundError(
                    "No module named 'lightgbm'",
                    name="lightgbm",
                )

        class NativeImportProbe(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                root = fullname.partition(".")[0]
                if root in {"lightgbm", "pandas", "pyarrow", "torch"}:
                    events.append(root)
                if fullname == "lightgbm":
                    if sys.platform in {"darwin", "win32"}:
                        return importlib.util.spec_from_loader(
                            fullname,
                            MissingLightGBMLoader(),
                        )
                    raise AssertionError(
                        "non-Windows app/registry import eagerly loaded LightGBM"
                    )
                if fullname == "torch":
                    raise AssertionError(
                        "app/registry import eagerly loaded PyTorch"
                    )
                return None

        db_root = Path(os.environ["DATABASE_DIR"])
        before = {
            str(path.resolve()): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in db_root.rglob("*.db*")
            if path.is_file()
        }
        sys.meta_path.insert(0, NativeImportProbe())

        import backend.main  # noqa: F401
        from backend.strategies.registry import StrategyRegistry

        registry = StrategyRegistry()
        count = registry.scan_directory(Path.cwd() / "backend" / "strategies")
        ids = {item.strategy_id for item in registry.list_all()}
        strategy = registry.create_strategy("lstm_rank_v1")
        source_path = inspect.getsourcefile(type(strategy))
        assert source_path is not None
        assert Path(source_path).resolve().name == "lstm_rank.py"
        assert type(strategy).__module__ in sys.modules
        required = {
            "alpha158_lgb_v1",
            "alpha158_rank_lgb_v1",
            "alpha158_xgb_v1",
            "lstm_rank_v1",
            "transformer_rank_v1",
        }
        assert required <= ids, (count, sorted(required - ids), events)
        assert "torch" not in events, events
        if sys.platform in {"darwin", "win32"}:
            assert "lightgbm" in events, events
            native_or_frame = [
                item
                for item in events
                if item in {"lightgbm", "pandas", "pyarrow"}
            ]
            assert native_or_frame[0] == "lightgbm", native_or_frame
        else:
            assert "lightgbm" not in events, events

        after = {
            str(path.resolve()): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in db_root.rglob("*.db*")
            if path.is_file()
        }
        assert after == before, (before, after)
        print(json.dumps({"count": count, "events": events}))
        """
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    isolated_data = tmp_path / "data"
    isolated_data.mkdir()
    env.update(
        {
            "DATABASE_DIR": str(isolated_data),
            "USERS_DB": str(isolated_data / "users.db"),
            "EXPERIMENT_DB": str(isolated_data / "experiment.db"),
            "TRADING_SIM_DB": str(isolated_data / "trading_sim.db"),
            "TRADING_LIVE_DB": str(isolated_data / "trading_live.db"),
            "DATA_CACHE_DIR": str(isolated_data / "cache"),
            "DATA_STAGING_DIR": str(isolated_data / "staging"),
            "PIT_EVIDENCE_DIR": str(isolated_data / "pit_evidence"),
            "PIT_EVIDENCE_DB": str(
                isolated_data / "pit_evidence" / "governance.db"
            ),
            "MODEL_STORE_DIR": str(isolated_data / "models"),
            "RESEARCH_SNAPSHOT_DIR": str(
                isolated_data / "research_snapshots"
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
