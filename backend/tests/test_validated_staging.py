from __future__ import annotations

import asyncio
import os
import threading
import time

import pandas as pd
import pytest

from backend.data import validated_staging as staging_module
from backend.data.source_validation import (
    CrossSourceConflictError,
    DailyFetchResult,
    build_daily_fetch_evidence,
)
from backend.data.sources import validated as validated_module
from backend.data.sources.validated import CrossValidatedDailySource
from backend.data.validated_staging import (
    StagingIntegrityError,
    ValidatedDailyStaging,
)


def _frame(scale: float = 1.0) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-02", periods=25, name="date")
    close = pd.Series(
        [10 * scale * (1.01**position) for position in range(len(index))],
        index=index,
    )
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    frame = pd.DataFrame(index=index, columns=columns, dtype=float)
    frame[("000001", "open")] = close * 0.99
    frame[("000001", "high")] = close * 1.01
    frame[("000001", "low")] = close * 0.98
    frame[("000001", "close")] = close
    frame[("000001", "volume")] = 1000
    return frame


def _result(provider: str = "feed-a", scale: float = 1.0) -> DailyFetchResult:
    frame = _frame(scale)
    evidence = build_daily_fetch_evidence(
        frame,
        requested_codes=["000001"],
        start="2024-01-02",
        end="2024-02-05",
        provider=provider,
        endpoint=f"{provider}/daily",
        adjustment="hfq",
        evidence_level="declared",
    )
    return DailyFetchResult(frame, evidence)


def _identity(provider: str = "feed-a") -> dict[str, str]:
    return {
        "provider": provider,
        "endpoint": f"{provider}/daily",
        "adjustment": "hfq",
        "adapter_id": f"test/{provider}/v1",
    }


def test_staging_round_trip_is_private_request_bound_and_hash_verified(
    tmp_path,
) -> None:
    staging = ValidatedDailyStaging(tmp_path / "stage")
    staging.save(
        _result(),
        codes=["000001"],
        start="2024-01-02",
        end="2024-02-05",
        source_identity=_identity(),
    )

    loaded = staging.load(
        codes=["000001"],
        start="2024-01-02",
        end="2024-02-05",
        source_identity=_identity(),
    )

    assert loaded is not None
    pd.testing.assert_frame_equal(loaded.frame, _frame(), check_freq=False)
    if os.name != "nt":
        assert os.stat(tmp_path / "stage").st_mode & 0o077 == 0
    files = list((tmp_path / "stage").iterdir())
    assert len(files) == 2
    if os.name != "nt":
        assert all(os.stat(path).st_mode & 0o077 == 0 for path in files)
    assert (
        staging.load(
            codes=["000002"],
            start="2024-01-02",
            end="2024-02-05",
            source_identity=_identity(),
        )
        is None
    )

    parquet = next(path for path in files if path.suffix == ".parquet")
    with parquet.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(StagingIntegrityError, match="integrity"):
        staging.load(
            codes=["000001"],
            start="2024-01-02",
            end="2024-02-05",
            source_identity=_identity(),
        )


def test_reference_failure_reuses_primary_staging_then_discards_on_success(
    tmp_path,
) -> None:
    class Source:
        def __init__(
            self,
            provider: str,
            *,
            scale: float,
            fail_once: bool = False,
        ) -> None:
            self.provider = provider
            self.scale = scale
            self.fail_once = fail_once
            self.calls = 0

        def staging_identity(self):
            return _identity(self.provider)

        async def fetch_daily_result(self, codes, start, end):
            del codes, start, end
            self.calls += 1
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("reference unavailable")
            return _result(self.provider, self.scale)

    primary = Source("feed-a", scale=1)
    reference = Source("feed-b", scale=100, fail_once=True)
    progress: list[dict] = []

    async def report(event):
        progress.append(dict(event))

    source = CrossValidatedDailySource(
        primary,
        reference,
        staging=ValidatedDailyStaging(tmp_path / "stage"),
        progress=report,
    )
    with pytest.raises(RuntimeError, match="reference unavailable"):
        asyncio.run(
            source.fetch_daily_result(
                ["000001"],
                "2024-01-02",
                "2024-02-05",
            )
        )

    result = asyncio.run(
        source.fetch_daily_result(
            ["000001"],
            "2024-01-02",
            "2024-02-05",
        )
    )

    assert not result.frame.empty
    assert primary.calls == 1
    assert reference.calls == 2
    assert any(event["reused_staging"] for event in progress)
    assert list((tmp_path / "stage").iterdir()) == []


def test_windows_mode_does_not_treat_synthesized_posix_bits_as_acl(
    tmp_path,
    monkeypatch,
) -> None:
    staging = ValidatedDailyStaging(tmp_path / "stage")
    staging.save(
        _result(),
        codes=["000001"],
        start="2024-01-02",
        end="2024-02-05",
        source_identity=_identity(),
    )
    os.chmod(tmp_path / "stage", 0o777)
    for path in (tmp_path / "stage").iterdir():
        os.chmod(path, 0o666)
    monkeypatch.setattr(
        staging_module,
        "_HAS_POSIX_PERMISSION_BITS",
        False,
    )

    loaded = staging.load(
        codes=["000001"],
        start="2024-01-02",
        end="2024-02-05",
        source_identity=_identity(),
    )

    assert loaded is not None


def test_validation_conflict_retains_primary_staging(tmp_path) -> None:
    class Source:
        def __init__(self, provider: str, *, conflict: bool = False) -> None:
            self.provider = provider
            self.conflict = conflict

        def staging_identity(self):
            return _identity(self.provider)

        async def fetch_daily_result(self, codes, start, end):
            del codes, start, end
            result = _result(self.provider, 1)
            if not self.conflict:
                return result
            frame = result.frame.copy()
            date = frame.index[20]
            frame.loc[date, ("000001", "close")] *= 1.1
            return DailyFetchResult(
                frame,
                build_daily_fetch_evidence(
                    frame,
                    requested_codes=["000001"],
                    start="2024-01-02",
                    end="2024-02-05",
                    provider=self.provider,
                    endpoint=f"{self.provider}/daily",
                    adjustment="hfq",
                    evidence_level="declared",
                ),
            )

    staging_root = tmp_path / "stage"
    source = CrossValidatedDailySource(
        Source("feed-a"),
        Source("feed-b", conflict=True),
        staging=ValidatedDailyStaging(staging_root),
    )

    with pytest.raises(CrossSourceConflictError):
        asyncio.run(
            source.fetch_daily_result(
                ["000001"],
                "2024-01-02",
                "2024-02-05",
            )
        )

    assert len(list(staging_root.iterdir())) == 2


def test_staging_rejects_symlinked_root(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    root = tmp_path / "stage"
    root.symlink_to(target, target_is_directory=True)
    staging = ValidatedDailyStaging(root)

    with pytest.raises(StagingIntegrityError, match="safe directory"):
        staging.load(
            codes=["000001"],
            start="2024-01-02",
            end="2024-02-05",
            source_identity=_identity(),
        )


def test_large_validation_keeps_event_loop_heartbeats_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = validated_module.compare_independent_daily_frames
    worker_names: list[str] = []

    def slow_compare(*args, **kwargs):
        worker_names.append(threading.current_thread().name)
        time.sleep(0.15)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        validated_module,
        "compare_independent_daily_frames",
        slow_compare,
    )

    class Source:
        def __init__(self, provider: str, scale: float) -> None:
            self.provider = provider
            self.scale = scale

        async def fetch_daily_result(self, codes, start, end):
            del codes, start, end
            return _result(self.provider, self.scale)

    async def scenario() -> int:
        heartbeats = 0
        stop = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal heartbeats
            while not stop.is_set():
                heartbeats += 1
                await asyncio.sleep(0.01)

        pulse = asyncio.create_task(heartbeat())
        try:
            await CrossValidatedDailySource(
                Source("feed-a", 1),
                Source("feed-b", 100),
            ).fetch_daily_result(
                ["000001"],
                "2024-01-02",
                "2024-02-05",
            )
        finally:
            stop.set()
            await pulse
        return heartbeats

    assert asyncio.run(scenario()) >= 8
    assert worker_names
    assert worker_names[0].startswith("quant-data-integrity")


def test_cancellation_during_validation_is_prompt_and_keeps_staging(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = validated_module.compare_independent_daily_frames

    def blocked_compare(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(
        validated_module,
        "compare_independent_daily_frames",
        blocked_compare,
    )

    class Source:
        def __init__(self, provider: str, scale: float) -> None:
            self.provider = provider
            self.scale = scale

        def staging_identity(self):
            return _identity(self.provider)

        async def fetch_daily_result(self, codes, start, end):
            del codes, start, end
            return _result(self.provider, self.scale)

    async def scenario() -> float:
        source = CrossValidatedDailySource(
            Source("feed-a", 1),
            Source("feed-b", 100),
            staging=ValidatedDailyStaging(tmp_path / "stage"),
        )
        task = asyncio.create_task(
            source.fetch_daily_result(
                ["000001"],
                "2024-01-02",
                "2024-02-05",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - began
        release.set()
        while not finished.is_set():
            await asyncio.sleep(0.005)
        return elapsed

    assert asyncio.run(scenario()) < 0.2
    assert len(list((tmp_path / "stage").iterdir())) == 2
