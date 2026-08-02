from __future__ import annotations

import asyncio
import hashlib
import pickle
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services import isolated_cpu
from backend.services import isolated_cpu_worker
from backend.services.isolated_cpu import (
    IsolatedCpuError,
    IsolatedCpuExecutor,
    IsolatedCpuTaskError,
)


class _HealthyCapacity:
    def decide(self) -> SimpleNamespace:
        return SimpleNamespace(pause_heavy=False)


class _PausedCapacity:
    def decide(self) -> SimpleNamespace:
        return SimpleNamespace(pause_heavy=True)


class _BlockingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self._finished = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._finished.wait(timeout):
            raise isolated_cpu.subprocess.TimeoutExpired("worker", timeout)
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._finished.set()

    def kill(self) -> None:
        self.returncode = -9
        self._finished.set()


def test_worker_environment_is_allowlisted_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "must-not-leak")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    monkeypatch.setenv("DATABASE_URL", "must-not-leak")
    monkeypatch.setenv("PATH", "/safe/path")

    environment = isolated_cpu._worker_environment()

    assert environment["PATH"] == "/safe/path"
    assert environment["PYTHONPATH"] == str(isolated_cpu.settings.PROJECT_ROOT)
    assert environment["OMP_NUM_THREADS"] == "1"
    assert environment["QUANT_PLATFORM_ISOLATED_CPU_MEMORY_LIMIT_MB"] == "4096"
    assert "JWT_SECRET" not in environment
    assert "DEEPSEEK_API_KEY" not in environment
    assert "DATABASE_URL" not in environment


def test_worker_rejects_parent_code_identity_drift(tmp_path: Path) -> None:
    request_path = tmp_path / "request.bin"
    result_path = tmp_path / "result.bin"
    parent_identity = isolated_cpu.runtime_code_identity()
    parent_identity["sha"] = (
        "f" * 40 if parent_identity["sha"] != "f" * 40 else "e" * 40
    )
    request_path.write_bytes(
        pickle.dumps(
            {
                "schema_version": "isolated-cpu-request/v1",
                "task": "not_registered",
                "payload": {},
                "runtime_code_identity": parent_identity,
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    )
    digest = hashlib.sha256(request_path.read_bytes()).hexdigest()

    exit_code = isolated_cpu_worker._main(
        ["worker", str(request_path), str(result_path), digest]
    )

    assert exit_code == 0
    response = pickle.loads(result_path.read_bytes())
    assert response["status"] == "failed"
    assert response["exception_type"] == "RuntimeCodeIdentityMismatch"
    assert response["runtime_code_identity"] == isolated_cpu.runtime_code_identity()


def test_executor_rejects_heavy_work_when_resource_budget_is_paused() -> None:
    executor = IsolatedCpuExecutor(capacity=_PausedCapacity())  # type: ignore[arg-type]

    with pytest.raises(IsolatedCpuError) as raised:
        asyncio.run(executor.run("factor_research_compute", {}))

    assert raised.value.code == "isolated_cpu_capacity_exhausted"


def test_executor_rejects_oversize_payload_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolated_cpu, "_MAX_REQUEST_BYTES", 1)
    monkeypatch.setattr(
        isolated_cpu.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("oversize input must not spawn"),
    )
    executor = IsolatedCpuExecutor(capacity=_HealthyCapacity())  # type: ignore[arg-type]

    with pytest.raises(IsolatedCpuError) as raised:
        asyncio.run(executor.run("factor_research_compute", {"value": "large"}))

    assert raised.value.code == "isolated_cpu_request_oversize"


def test_unregistered_task_fails_through_real_spawn_boundary() -> None:
    executor = IsolatedCpuExecutor(capacity=_HealthyCapacity())  # type: ignore[arg-type]

    with pytest.raises(IsolatedCpuTaskError) as raised:
        asyncio.run(executor.run("not_registered", {}, timeout_seconds=10))

    assert raised.value.original_type == "ValueError"


def test_cancellation_terminates_spawn_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _BlockingProcess()
    captured: dict[str, object] = {}

    def spawn(*args: object, **kwargs: object) -> _BlockingProcess:
        captured.update(kwargs)
        return process

    monkeypatch.setattr(isolated_cpu.subprocess, "Popen", spawn)
    monkeypatch.setenv("JWT_SECRET", "parent-only")
    executor = IsolatedCpuExecutor(capacity=_HealthyCapacity())  # type: ignore[arg-type]

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.run("factor_research_compute", {}, timeout_seconds=30)
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.terminated is True
    assert captured["close_fds"] is True
    assert str(captured["cwd"]) != str(isolated_cpu.settings.PROJECT_ROOT)
    assert "JWT_SECRET" not in captured["env"]  # type: ignore[operator]
    assert not Path(str(captured["cwd"])).exists()


def test_timeout_terminates_spawn_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _BlockingProcess()
    monkeypatch.setattr(isolated_cpu.subprocess, "Popen", lambda *args, **kwargs: process)
    executor = IsolatedCpuExecutor(capacity=_HealthyCapacity())  # type: ignore[arg-type]

    with pytest.raises(IsolatedCpuError) as raised:
        asyncio.run(
            executor.run(
                "factor_research_compute",
                {},
                timeout_seconds=0.01,
            )
        )

    assert raised.value.code == "isolated_cpu_timeout"
    assert process.terminated is True


def test_terminate_reaps_the_isolated_process_group_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if isolated_cpu.os.name == "nt":
        pytest.skip("process-group signalling is POSIX-specific")
    process = _BlockingProcess()
    process.pid = 4242  # type: ignore[attr-defined]
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(isolated_cpu.os, "getpgid", lambda pid: pid)
    def kill_group(process_group: int, signal: int) -> None:
        calls.append((process_group, signal))
        process.terminate()

    monkeypatch.setattr(isolated_cpu.os, "killpg", kill_group)

    isolated_cpu._terminate(process)  # type: ignore[arg-type]

    assert calls == [(4242, isolated_cpu.signal.SIGTERM)]


def test_worker_rejects_an_unsafe_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUANT_PLATFORM_ISOLATED_CPU_MEMORY_LIMIT_MB", "128")

    with pytest.raises(RuntimeError, match="unsafe"):
        isolated_cpu_worker._apply_resource_limits()


def test_crashed_worker_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _BlockingProcess()
    process.returncode = 9
    process._finished.set()
    monkeypatch.setattr(
        isolated_cpu.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    executor = IsolatedCpuExecutor(capacity=_HealthyCapacity())  # type: ignore[arg-type]

    with pytest.raises(IsolatedCpuError) as raised:
        asyncio.run(
            executor.run(
                "factor_research_compute",
                {},
                timeout_seconds=1,
            )
        )

    assert raised.value.code == "isolated_cpu_crashed"


def test_spawn_failure_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_spawn(*args: object, **kwargs: object) -> None:
        raise OSError("internal executable path")

    monkeypatch.setattr(isolated_cpu.subprocess, "Popen", fail_to_spawn)
    executor = IsolatedCpuExecutor(capacity=_HealthyCapacity())  # type: ignore[arg-type]

    with pytest.raises(IsolatedCpuError) as raised:
        asyncio.run(
            executor.run(
                "factor_research_compute",
                {},
                timeout_seconds=1,
            )
        )

    assert raised.value.code == "isolated_cpu_spawn_failed"
    assert "internal executable path" not in raised.value.message
