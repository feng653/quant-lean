"""Cross-platform, dependency-free host load sampling and capacity decisions."""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from backend.config import settings

logger = logging.getLogger("quant_platform.jobs.resources")
_MB = 1024 * 1024


@dataclass(frozen=True)
class SystemLoadSnapshot:
    """A best-effort, serialisable view of host pressure."""

    cpu_count: int
    load_1m: float | None
    normalized_load: float | None
    memory_total_mb: float | None
    memory_available_mb: float | None
    memory_used_ratio: float | None
    swap_used_mb: float | None
    source: str
    error: str | None = None
    disk_free_mb: float | None = None
    io_pressure: float | None = None
    io_source: str | None = None

    def public_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CapacityDecision:
    """Scheduler capacity selected for one sampling interval."""

    capacity: int
    configured_max: int
    degraded: bool
    reasons: tuple[str, ...]
    metrics: SystemLoadSnapshot
    pause_heavy: bool = False
    admission_mode: str = "normal"

    def public_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


class LoadProvider(Protocol):
    def sample(self) -> SystemLoadSnapshot: ...


@dataclass(frozen=True)
class _MacOSIOCounters:
    """Monotonic device service-time counters sampled from IOKit."""

    sampled_ns: int
    service_time_ns: int


def _load_average(cpu_count: int) -> tuple[float | None, float | None]:
    try:
        load_1m = float(os.getloadavg()[0])
    except (AttributeError, OSError):
        return None, None
    return load_1m, load_1m / max(cpu_count, 1)


def _linux_memory() -> tuple[float, float, float, float]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        name, raw = line.split(":", 1)
        match = re.search(r"\d+", raw)
        if match:
            values[name] = int(match.group()) * 1024
    total = values["MemTotal"]
    available = values["MemAvailable"]
    swap_used = max(values.get("SwapTotal", 0) - values.get("SwapFree", 0), 0)
    return total / _MB, available / _MB, 1 - available / total, swap_used / _MB


def _run_readonly(command: list[str]) -> str:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=1.0,
    ).stdout.strip()


def _macos_memsize() -> int:
    value = ctypes.c_uint64()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    libc = ctypes.CDLL(None)
    if (
        libc.sysctlbyname(
            b"hw.memsize",
            ctypes.byref(value),
            ctypes.byref(size),
            None,
            0,
        )
        != 0
    ):
        raise OSError("sysctlbyname(hw.memsize) failed")
    return int(value.value)


class _MacSwapUsage(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("available", ctypes.c_uint64),
        ("used", ctypes.c_uint64),
        ("page_size", ctypes.c_uint32),
        ("encrypted", ctypes.c_uint32),
    ]


def _macos_swap_used_mb() -> float | None:
    value = _MacSwapUsage()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    libc = ctypes.CDLL(None)
    if (
        libc.sysctlbyname(
            b"vm.swapusage",
            ctypes.byref(value),
            ctypes.byref(size),
            None,
            0,
        )
        != 0
    ):
        return None
    return value.used / _MB


def _macos_memory() -> tuple[float, float, float, float | None]:
    total = _macos_memsize()
    output = _run_readonly(["/usr/bin/vm_stat"])
    page_match = re.search(r"page size of (\d+) bytes", output)
    if page_match is None:
        raise ValueError("vm_stat page size unavailable")
    page_size = int(page_match.group(1))
    pages: dict[str, int] = {}
    for line in output.splitlines()[1:]:
        match = re.match(r"([^:]+):\s+(\d+)\.", line)
        if match:
            pages[match.group(1)] = int(match.group(2))
    reclaimable_names = (
        "Pages free",
        "Pages inactive",
        "Pages speculative",
        "Pages purgeable",
    )
    available = sum(pages.get(name, 0) for name in reclaimable_names) * page_size
    if available <= 0:
        raise ValueError("vm_stat reclaimable pages unavailable")
    swap_used_mb = _macos_swap_used_mb()
    return total / _MB, available / _MB, 1 - available / total, swap_used_mb


class _WindowsMemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_phys", ctypes.c_ulonglong),
        ("avail_phys", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("avail_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("avail_virtual", ctypes.c_ulonglong),
        ("avail_extended_virtual", ctypes.c_ulonglong),
    ]


def _windows_memory() -> tuple[float, float, float, float]:
    status = _WindowsMemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        raise OSError("GlobalMemoryStatusEx failed")
    swap_used = max(
        (status.total_page_file - status.avail_page_file)
        - (status.total_phys - status.avail_phys),
        0,
    )
    return (
        status.total_phys / _MB,
        status.avail_phys / _MB,
        status.memory_load / 100,
        swap_used / _MB,
    )


def _generic_memory() -> tuple[float, float, float, None]:
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
    available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
    available = available_pages * page_size
    return total / _MB, available / _MB, 1 - available / total, None


def _linux_io_pressure() -> tuple[float | None, str]:
    """Read Linux PSI without blocking on an external process."""
    path = Path("/proc/pressure/io")
    if not path.is_file():
        return None, "unavailable"
    first = path.read_text(encoding="utf-8").splitlines()[0]
    match = re.search(r"\bavg10=(\d+(?:\.\d+)?)", first)
    if match is None:
        return None, "unavailable"
    # PSI is a percentage of wall time stalled during the last ten seconds.
    return min(max(float(match.group(1)) / 100.0, 0.0), 1.0), "linux_psi"


def _macos_io_counters() -> _MacOSIOCounters:
    """Read cumulative block-device service time without sampling SQLite.

    ``IOBlockStorageDriver`` exposes actual device read/write service time.
    A delta divided by monotonic wall time is a bounded utilisation proxy.
    Multiple devices are summed and the eventual pressure is clamped to one.
    """

    output = _run_readonly(
        ["/usr/sbin/ioreg", "-r", "-c", "IOBlockStorageDriver", "-l"]
    )
    read_times = [
        int(value)
        for value in re.findall(r'"Total Time \(Read\)"=(\d+)', output)
    ]
    write_times = [
        int(value)
        for value in re.findall(r'"Total Time \(Write\)"=(\d+)', output)
    ]
    if not read_times and not write_times:
        raise ValueError("IOKit block-device service counters unavailable")
    return _MacOSIOCounters(
        sampled_ns=time.monotonic_ns(),
        service_time_ns=sum(read_times) + sum(write_times),
    )


def _macos_io_pressure(
    previous: _MacOSIOCounters | None,
) -> tuple[float | None, str, _MacOSIOCounters | None]:
    try:
        current = _macos_io_counters()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("Unable to sample macOS I/O pressure: %s", exc)
        return None, "unknown", previous
    if previous is None:
        return None, "macos_iokit_warmup", current
    elapsed = current.sampled_ns - previous.sampled_ns
    service = current.service_time_ns - previous.service_time_ns
    if elapsed <= 0 or service < 0:
        return None, "unknown", current
    return min(max(service / elapsed, 0.0), 1.0), "macos_iokit", current


def _disk_free_mb() -> float | None:
    try:
        return shutil.disk_usage(settings.PROJECT_ROOT).free / _MB
    except OSError:
        return None


class SystemLoadProvider:
    """Collect load without requiring psutil.

    Platform-specific readers are optional optimisations. Any failure is
    reported in the snapshot so the controller can conservatively stay at one
    slot instead of guessing.
    """

    def __init__(self) -> None:
        self._previous_macos_io: _MacOSIOCounters | None = None

    def sample(self) -> SystemLoadSnapshot:
        cpu_count = max(os.cpu_count() or 1, 1)
        load_1m, normalized_load = _load_average(cpu_count)
        system = platform.system()
        disk_free_mb = _disk_free_mb()
        if system == "Linux":
            io_pressure, io_source = _linux_io_pressure()
        elif system == "Darwin":
            (
                io_pressure,
                io_source,
                self._previous_macos_io,
            ) = _macos_io_pressure(self._previous_macos_io)
        else:
            io_pressure, io_source = None, "unknown"
        try:
            if system == "Linux" and Path("/proc/meminfo").is_file():
                memory = _linux_memory()
                source = "procfs"
            elif system == "Darwin":
                memory = _macos_memory()
                source = "vm_stat"
            elif system == "Windows":
                memory = _windows_memory()
                source = "global_memory_status"
            else:
                memory = _generic_memory()
                source = "sysconf"
            total, available, used_ratio, swap_used = memory
            return SystemLoadSnapshot(
                cpu_count=cpu_count,
                load_1m=load_1m,
                normalized_load=normalized_load,
                memory_total_mb=round(total, 1),
                memory_available_mb=round(available, 1),
                memory_used_ratio=round(used_ratio, 4),
                swap_used_mb=round(swap_used, 1) if swap_used is not None else None,
                source=source,
                disk_free_mb=(
                    round(disk_free_mb, 1) if disk_free_mb is not None else None
                ),
                io_pressure=io_pressure,
                io_source=io_source,
            )
        except Exception as exc:
            logger.warning("Unable to sample system memory pressure: %s", exc)
            return SystemLoadSnapshot(
                cpu_count=cpu_count,
                load_1m=load_1m,
                normalized_load=normalized_load,
                memory_total_mb=None,
                memory_available_mb=None,
                memory_used_ratio=None,
                swap_used_mb=None,
                source="unavailable",
                error=f"{type(exc).__name__}: {exc}",
                disk_free_mb=(
                    round(disk_free_mb, 1) if disk_free_mb is not None else None
                ),
                io_pressure=io_pressure,
                io_source=io_source,
            )


class AdaptiveCapacityController:
    """Scale from one to two slots after sustained low pressure."""

    HARD_MAX_CAPACITY = 2

    def __init__(self, provider: LoadProvider | None = None) -> None:
        self._provider = provider or SystemLoadProvider()
        self._healthy_samples = 0
        self._previous_swap_mb: float | None = None

    def decide(self) -> CapacityDecision:
        metrics = self._provider.sample()
        configured_max = max(
            1,
            min(
                int(settings.JOB_SCHEDULER_MAX_CONCURRENCY),
                self.HARD_MAX_CAPACITY,
            ),
        )
        reasons: list[str] = []
        if not settings.JOB_SCHEDULER_ENABLED:
            reasons.append("scheduler_disabled")
        if configured_max <= 1:
            reasons.append("configured_single_slot")
        if metrics.cpu_count < 4:
            reasons.append("insufficient_cpu_cores")
        if metrics.normalized_load is None:
            reasons.append("cpu_load_unavailable")
        elif metrics.normalized_load > float(settings.JOB_SCHEDULER_CPU_LOAD_LIMIT):
            reasons.append("cpu_load_high")
        if metrics.memory_used_ratio is None or metrics.memory_available_mb is None:
            reasons.append("memory_pressure_unavailable")
        else:
            if metrics.memory_used_ratio > float(
                settings.JOB_SCHEDULER_MEMORY_USED_LIMIT
            ):
                reasons.append("memory_used_high")
            if metrics.memory_available_mb < int(
                settings.JOB_SCHEDULER_MIN_AVAILABLE_MEMORY_MB
            ):
                reasons.append("memory_available_low")
        if metrics.swap_used_mb is None:
            reasons.append("swap_pressure_unavailable")
        else:
            if metrics.swap_used_mb > int(settings.JOB_SCHEDULER_MAX_SWAP_USED_MB):
                reasons.append("swap_used_high")
            if (
                self._previous_swap_mb is not None
                and metrics.swap_used_mb - self._previous_swap_mb
                > int(settings.JOB_SCHEDULER_MAX_SWAP_GROWTH_MB)
            ):
                reasons.append("swap_growing")
        self._previous_swap_mb = metrics.swap_used_mb
        if (
            metrics.disk_free_mb is not None
            and metrics.disk_free_mb < int(settings.JOB_SCHEDULER_MIN_DISK_FREE_MB)
        ):
            reasons.append("disk_free_low")
        if (
            metrics.io_pressure is not None
            and metrics.io_pressure > float(settings.JOB_SCHEDULER_MAX_IO_PRESSURE)
        ):
            reasons.append("io_pressure_high")

        critical_reasons: list[str] = []
        if (
            metrics.normalized_load is not None
            and metrics.normalized_load
            > float(settings.JOB_SCHEDULER_CRITICAL_CPU_LOAD)
        ):
            critical_reasons.append("cpu_budget_exhausted")
        if (
            metrics.memory_used_ratio is not None
            and metrics.memory_used_ratio
            > float(settings.JOB_SCHEDULER_CRITICAL_MEMORY_USED)
        ):
            critical_reasons.append("memory_budget_exhausted")
        if (
            metrics.memory_available_mb is not None
            and metrics.memory_available_mb
            < int(settings.JOB_SCHEDULER_CRITICAL_AVAILABLE_MEMORY_MB)
        ):
            critical_reasons.append("memory_reserve_exhausted")
        if (
            metrics.disk_free_mb is not None
            and metrics.disk_free_mb < int(settings.JOB_SCHEDULER_MIN_DISK_FREE_MB)
        ):
            critical_reasons.append("io_budget_exhausted")
        if (
            metrics.io_pressure is not None
            and metrics.io_pressure > float(settings.JOB_SCHEDULER_MAX_IO_PRESSURE)
        ):
            critical_reasons.append("io_budget_exhausted")
        for reason in critical_reasons:
            if reason not in reasons:
                reasons.append(reason)

        if reasons:
            self._healthy_samples = 0
            capacity = 1
        else:
            self._healthy_samples += 1
            stable_samples = max(int(settings.JOB_SCHEDULER_SCALE_UP_SAMPLES), 1)
            capacity = configured_max if self._healthy_samples >= stable_samples else 1
            if capacity == 1 and configured_max > 1:
                reasons.append("scale_up_warmup")
        capacity = max(
            1,
            min(capacity, configured_max, self.HARD_MAX_CAPACITY),
        )
        return CapacityDecision(
            capacity=capacity,
            configured_max=configured_max,
            degraded=capacity < configured_max,
            reasons=tuple(reasons),
            metrics=metrics,
            pause_heavy=bool(critical_reasons),
            admission_mode=(
                "pause_heavy" if critical_reasons else "normal"
            ),
        )
