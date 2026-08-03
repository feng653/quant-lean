"""Private worker entrypoint for :mod:`backend.services.isolated_cpu`."""

from __future__ import annotations
from backend.core.hashing import file_sha256

import os
import pickle
import platform
import stat
import sys
from pathlib import Path
from typing import Any, Callable

try:  # ``resource`` is unavailable on Windows by design.
    import resource
except ImportError:  # pragma: no cover - exercised on Windows only.
    resource = None  # type: ignore[assignment]

from backend.version import (
    code_identities_match,
    observed_worktree_drift,
    runtime_code_identity,
)

_TASKS: dict[str, tuple[str, str]] = {
    "factor_research_compute": (
        "backend.services.factor_research",
        "_compute_factor_research",
    ),
    "model_retrain_fit": (
        "backend.services.maintenance",
        "_isolated_retrain_fit",
    ),
}


class RuntimeCodeIdentityMismatch(RuntimeError):
    """The worker imported source different from its parent process."""


def _apply_resource_limits() -> None:
    """Apply the configured per-child address-space ceiling before task import."""

    raw_limit = os.environ.get("QUANT_PLATFORM_ISOLATED_CPU_MEMORY_LIMIT_MB", "")
    try:
        limit_mb = int(raw_limit)
    except ValueError as exc:
        raise RuntimeError("isolated CPU memory limit is invalid") from exc
    if limit_mb < 256:
        raise RuntimeError("isolated CPU memory limit is unsafe")
    if platform.system() != "Linux" or resource is None:
        # macOS does not support reducing RLIMIT_AS from infinity for an
        # unprivileged process (and Windows needs a Job Object). Those hosts
        # retain the one-slot/thread budget plus adaptive admission controls;
        # their lack of an OS hard RSS cap is explicitly documented, not
        # silently represented as a limit.
        return
    limit_bytes = limit_mb * 1024 * 1024
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY and limit_bytes > hard:
            raise ValueError("configured limit exceeds inherited hard limit")
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, hard))
    except (OSError, ValueError) as exc:
        raise RuntimeError("isolated CPU memory limit could not be applied") from exc


def _resolve_task(name: str) -> Callable[[dict[str, Any]], Any]:
    target = _TASKS.get(name)
    if target is None:
        raise ValueError("unregistered isolated CPU task")
    module_name, function_name = target
    module = __import__(module_name, fromlist=[function_name])
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError("isolated CPU task is not callable")
    return function


def _write_result(path: Path, response: dict[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(descriptor, "wb") as handle:
        pickle.dump(response, handle, protocol=pickle.HIGHEST_PROTOCOL)
        handle.flush()
        os.fsync(handle.fileno())


def _main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    request_path = Path(argv[1])
    result_path = Path(argv[2])
    expected_digest = argv[3]
    try:
        if file_sha256(request_path) != expected_digest:
            return 3
        with request_path.open("rb") as handle:
            request = pickle.load(handle)
        if (
            not isinstance(request, dict)
            or request.get("schema_version") != "isolated-cpu-request/v1"
            or not isinstance(request.get("payload"), dict)
            or not isinstance(request.get("runtime_code_identity"), dict)
        ):
            return 4
        worker_identity = runtime_code_identity()
        if not code_identities_match(
            request["runtime_code_identity"],
            worker_identity,
        ) or observed_worktree_drift()["detected"]:
            raise RuntimeCodeIdentityMismatch(
                "isolated worker runtime code identity mismatch"
            )
        _apply_resource_limits()
        task = _resolve_task(str(request.get("task") or ""))
        result = task(request["payload"])
        response = {
            "schema_version": "isolated-cpu-result/v1",
            "status": "completed",
            "result": result,
            "runtime_code_identity": worker_identity,
        }
    except Exception as exc:
        response = {
            "schema_version": "isolated-cpu-result/v1",
            "status": "failed",
            "exception_type": type(exc).__name__,
            "message": str(exc)[:1000],
            "runtime_code_identity": runtime_code_identity(),
        }
    try:
        _write_result(result_path, response)
    except Exception:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
