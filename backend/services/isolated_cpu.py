"""Cancellable spawn-process boundary for trusted CPU-heavy research tasks.

The worker is a fresh interpreter started with an explicit environment
allowlist.  It therefore cannot inherit parent SQLite connections, JWT
secrets, provider credentials, or unrelated file descriptors.  A single
process slot is intentional for the supported 8 GB host.
"""

from __future__ import annotations
from backend.core.hashing import file_sha256

import asyncio
import os
import pickle
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.jobs.resources import AdaptiveCapacityController
from backend.version import code_identities_match, runtime_code_identity

_INPUT_FILE = "request.bin"
_OUTPUT_FILE = "result.bin"
_MAX_REQUEST_BYTES = 384 * 1024 * 1024
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_ALLOWED_ENVIRONMENT = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


class IsolatedCpuError(RuntimeError):
    """Safe structured failure returned by the process boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IsolatedCpuTaskError(IsolatedCpuError):
    """A task raised a known, serialised exception inside the worker."""

    def __init__(self, message: str, *, original_type: str) -> None:
        super().__init__("isolated_cpu_task_failed", message)
        self.original_type = original_type


def _worker_environment(
    parent_identity: dict[str, Any] | None = None,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ALLOWED_ENVIRONMENT
    }
    thread_budget = str(max(int(settings.JOB_CPU_THREAD_BUDGET), 1))
    environment.update(
        {
            "OPENBLAS_NUM_THREADS": thread_budget,
            "OMP_NUM_THREADS": thread_budget,
            "MKL_NUM_THREADS": thread_budget,
            "NUMEXPR_NUM_THREADS": thread_budget,
            "QUANT_PLATFORM_ISOLATED_CPU_MEMORY_LIMIT_MB": str(
                max(int(settings.JOB_ISOLATED_CPU_MEMORY_LIMIT_MB), 1)
            ),
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            # Import the application without using its working directory. The
            # worker runs inside its private temp directory so pydantic-settings
            # cannot discover the repository's credential-bearing ``.env``.
            "PYTHONPATH": str(settings.PROJECT_ROOT),
        }
    )
    identity = parent_identity or runtime_code_identity()
    if identity.get("source") == "build_override":
        environment["QUANT_PLATFORM_BUILD_COMMIT"] = str(identity["sha"])
    return environment


def _write_private_pickle(path: Path, value: object) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(descriptor, "wb") as handle:
        pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        handle.flush()
        os.fsync(handle.fileno())


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process_id = getattr(process, "pid", None)
    if os.name != "nt" and isinstance(process_id, int) and process_id > 0:
        try:
            os.killpg(os.getpgid(process_id), signal.SIGTERM)
        except OSError:
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name != "nt" and isinstance(process_id, int) and process_id > 0:
            try:
                os.killpg(os.getpgid(process_id), signal.SIGKILL)
            except OSError:
                process.kill()
        else:
            process.kill()
        process.wait(timeout=3)


class IsolatedCpuExecutor:
    """One-slot executor governed by the existing adaptive resource budget."""

    def __init__(
        self,
        *,
        capacity: AdaptiveCapacityController | None = None,
    ) -> None:
        self._capacity = capacity or AdaptiveCapacityController()
        self._slot = asyncio.Semaphore(1)

    async def run(
        self,
        task_name: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if not task_name or len(task_name) > 80:
            raise IsolatedCpuError(
                "isolated_cpu_task_invalid",
                "CPU 隔离任务标识无效",
            )
        decision = self._capacity.decide()
        if decision.pause_heavy:
            raise IsolatedCpuError(
                "isolated_cpu_capacity_exhausted",
                "本机资源预算不足，CPU 密集任务已暂停",
            )
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else settings.JOB_ISOLATED_CPU_TIMEOUT_SECONDS
        )
        if timeout <= 0:
            raise IsolatedCpuError(
                "isolated_cpu_timeout_invalid",
                "CPU 隔离任务超时配置无效",
            )

        async with self._slot:
            try:
                directory = Path(tempfile.mkdtemp(prefix="quant-isolated-cpu-"))
            except OSError as exc:
                raise IsolatedCpuError(
                    "isolated_cpu_workspace_failed",
                    "无法创建 CPU 隔离任务的私有工作目录",
                ) from exc
            try:
                request_path = directory / _INPUT_FILE
                result_path = directory / _OUTPUT_FILE
                request = {
                    "schema_version": "isolated-cpu-request/v1",
                    "task": task_name,
                    "payload": payload,
                    "runtime_code_identity": runtime_code_identity(),
                }
                try:
                    if os.name != "nt":
                        directory.chmod(0o700)
                    _write_private_pickle(request_path, request)
                    request_size = request_path.stat().st_size
                except Exception as exc:
                    raise IsolatedCpuError(
                        "isolated_cpu_input_failed",
                        "CPU 隔离任务输入无法安全序列化",
                    ) from exc
                if request_size > _MAX_REQUEST_BYTES:
                    raise IsolatedCpuError(
                        "isolated_cpu_request_oversize",
                        "CPU 隔离任务输入超过 8GB 主机安全大小限制",
                    )
                try:
                    digest = file_sha256(request_path)
                except OSError as exc:
                    raise IsolatedCpuError(
                        "isolated_cpu_input_failed",
                        "CPU 隔离任务输入无法完成完整性校验",
                    ) from exc
                command = [
                    sys.executable,
                    "-m",
                    "backend.services.isolated_cpu_worker",
                    str(request_path),
                    str(result_path),
                    digest,
                ]
                try:
                    spawn_options: dict[str, Any] = {}
                    if os.name == "nt":
                        spawn_options["creationflags"] = getattr(
                            subprocess,
                            "CREATE_NEW_PROCESS_GROUP",
                            0,
                        )
                    else:
                        spawn_options["start_new_session"] = True
                    process = subprocess.Popen(
                        command,
                        cwd=str(directory),
                        env=_worker_environment(request["runtime_code_identity"]),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        **spawn_options,
                    )
                except OSError as exc:
                    raise IsolatedCpuError(
                        "isolated_cpu_spawn_failed",
                        "无法启动 CPU 隔离子进程",
                    ) from exc
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(process.wait),
                        timeout=timeout,
                    )
                except TimeoutError as exc:
                    await asyncio.to_thread(_terminate, process)
                    raise IsolatedCpuError(
                        "isolated_cpu_timeout",
                        "CPU 隔离任务执行超时，子进程已回收",
                    ) from exc
                except asyncio.CancelledError:
                    await asyncio.shield(asyncio.to_thread(_terminate, process))
                    raise

                if process.returncode != 0 or not result_path.is_file():
                    raise IsolatedCpuError(
                        "isolated_cpu_crashed",
                        "CPU 隔离子进程异常退出，未生成可信结果",
                    )
                if result_path.stat().st_size > _MAX_RESULT_BYTES:
                    raise IsolatedCpuError(
                        "isolated_cpu_result_oversize",
                        "CPU 隔离任务结果超过安全大小限制",
                    )
                try:
                    with result_path.open("rb") as handle:
                        response = pickle.load(handle)
                except Exception as exc:
                    raise IsolatedCpuError(
                        "isolated_cpu_result_invalid",
                        "CPU 隔离任务结果无法通过完整性解析",
                    ) from exc
                if (
                    not isinstance(response, dict)
                    or response.get("schema_version")
                    != "isolated-cpu-result/v1"
                ):
                    raise IsolatedCpuError(
                        "isolated_cpu_result_invalid",
                        "CPU 隔离任务结果结构无效",
                    )
                worker_identity = response.get("runtime_code_identity")
                if (
                    not isinstance(worker_identity, dict)
                    or not code_identities_match(
                        request["runtime_code_identity"],
                        worker_identity,
                    )
                ):
                    raise IsolatedCpuError(
                        "isolated_cpu_code_identity_mismatch",
                        "CPU 隔离子进程代码版本与父进程不一致，结果已拒绝",
                    )
                if response.get("status") == "failed":
                    raise IsolatedCpuTaskError(
                        str(response.get("message") or "CPU 隔离任务执行失败"),
                        original_type=str(
                            response.get("exception_type") or "RuntimeError"
                        ),
                    )
                if response.get("status") != "completed":
                    raise IsolatedCpuError(
                        "isolated_cpu_result_invalid",
                        "CPU 隔离任务结果状态无效",
                    )
                return response.get("result")
            finally:
                shutil.rmtree(directory, ignore_errors=True)


_EXECUTOR = IsolatedCpuExecutor()


async def run_isolated_cpu(
    task_name: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> Any:
    """Run a registered CPU task in the shared one-slot spawn boundary."""

    return await _EXECUTOR.run(
        task_name,
        payload,
        timeout_seconds=timeout_seconds,
    )
