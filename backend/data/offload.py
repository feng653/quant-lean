"""Bounded thread offload for blocking market-data integrity work.

Pandas, PyArrow and cryptographic frame digests are synchronous.  Running them
on FastAPI's event loop makes health checks, cancellation and websocket
heartbeats unavailable during a large refresh.  A single dedicated worker is
intentional on the 8 GB deployment host: work remains in-process and bounded,
without fork/ProcessPool memory duplication.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

_Result = TypeVar("_Result")

_DATA_INTEGRITY_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="quant-data-integrity",
)


async def run_data_integrity(
    function: Callable[..., _Result],
    /,
    *args: Any,
    **kwargs: Any,
) -> _Result:
    """Run one blocking data operation on the bounded integrity worker.

    Exceptions propagate unchanged.  Cancelling the awaiting coroutine remains
    immediate; an already-running native call is allowed to finish in the sole
    worker, but its result is discarded so the caller cannot continue to later
    validation or publication steps.  An atomic file operation that has itself
    already started may still finish its all-or-nothing replacement.
    """

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _DATA_INTEGRITY_EXECUTOR,
        partial(function, *args, **kwargs),
    )
