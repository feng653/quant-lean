from __future__ import annotations

import asyncio
import threading
import time

import pandas as pd
import pytest

from backend.data.cache import DataCache


def test_parquet_write_does_not_block_event_loop(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pd.DataFrame.to_parquet
    worker_names: list[str] = []

    def slow_write(frame, *args, **kwargs):
        worker_names.append(threading.current_thread().name)
        time.sleep(0.15)
        return original(frame, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", slow_write)
    frame = pd.DataFrame(
        {"close": [10.0, 10.1]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )

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
            await DataCache(str(tmp_path)).save_legacy_pivot_for_audit(
                "fixture",
                frame,
            )
        finally:
            stop.set()
            await pulse
        return heartbeats

    assert asyncio.run(scenario()) >= 8
    assert worker_names
    assert worker_names[0].startswith("quant-data-integrity")
