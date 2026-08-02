from __future__ import annotations

import subprocess

import pytest

from backend.jobs import resources


def test_macos_iokit_pressure_uses_service_time_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = iter(
        [
            resources._MacOSIOCounters(
                sampled_ns=1_000_000_000,
                service_time_ns=400_000_000,
            ),
            resources._MacOSIOCounters(
                sampled_ns=2_000_000_000,
                service_time_ns=650_000_000,
            ),
        ]
    )
    monkeypatch.setattr(resources, "_macos_io_counters", lambda: next(samples))

    first_pressure, first_source, first = resources._macos_io_pressure(None)
    pressure, source, second = resources._macos_io_pressure(first)

    assert first_pressure is None
    assert first_source == "macos_iokit_warmup"
    assert pressure == pytest.approx(0.25)
    assert source == "macos_iokit"
    assert second is not None


def test_macos_iokit_failure_is_explicitly_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> resources._MacOSIOCounters:
        raise subprocess.TimeoutExpired("ioreg", 1)

    monkeypatch.setattr(resources, "_macos_io_counters", fail)

    pressure, source, current = resources._macos_io_pressure(None)

    assert pressure is None
    assert source == "unknown"
    assert current is None


def test_macos_iokit_counter_reset_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = resources._MacOSIOCounters(
        sampled_ns=2_000_000_000,
        service_time_ns=900_000_000,
    )
    current = resources._MacOSIOCounters(
        sampled_ns=3_000_000_000,
        service_time_ns=100_000_000,
    )
    monkeypatch.setattr(resources, "_macos_io_counters", lambda: current)

    pressure, source, sampled = resources._macos_io_pressure(previous)

    assert pressure is None
    assert source == "unknown"
    assert sampled == current
