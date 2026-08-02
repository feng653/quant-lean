"""Stable, SQLite-backed cache primitives for AI invocations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import aiosqlite

from backend.ai.client import ChatResult

DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


def normalize_cache_value(value: Any) -> Any:
    """Convert supported values into deterministic JSON-compatible data."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("AI cache input cannot contain NaN or infinity")
        return 0.0 if value == 0 else value
    if isinstance(value, Decimal):
        return {"__decimal__": format(value, "f")}
    if isinstance(value, (datetime, date)):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, Enum):
        return normalize_cache_value(value.value)
    if isinstance(value, Path):
        return {"__path__": str(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return normalize_cache_value(asdict(value))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("AI cache mapping keys must be strings")
        normalized_items = sorted(
            ((key, normalize_cache_value(item)) for key, item in value.items()),
            key=lambda item: item[0],
        )
        return {key: item for key, item in normalized_items}
    if isinstance(value, (list, tuple)):
        return [normalize_cache_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [normalize_cache_value(item) for item in value]
        return sorted(normalized, key=canonical_json)
    raise TypeError(f"Unsupported AI cache input type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize values with stable ordering and separators."""
    return json.dumps(
        normalize_cache_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_cache_key(
    *,
    endpoint: str,
    prompt_template: str,
    system_prompt: str,
    kwargs: Mapping[str, Any],
    cache_context: Any = None,
) -> str:
    """Hash the endpoint, normalized prompt inputs, and caller context."""
    payload = {
        "endpoint": endpoint,
        "input": {
            "prompt_template": prompt_template,
            "system_prompt": system_prompt,
            "kwargs": kwargs,
        },
        "cache_context": cache_context,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class AiCache:
    """Concurrency-safe result cache stored in the experiment database."""

    _locks: dict[tuple[str, str], _LockEntry] = {}
    _locks_guard = threading.Lock()

    def __init__(
        self,
        db_path: str | Path,
        *,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.db_path = str(db_path)
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    async def _connect(self) -> aiosqlite.Connection:
        connection = await aiosqlite.connect(self.db_path)
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_cache (
                cache_key TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await connection.commit()
        return connection

    async def get(self, cache_key: str) -> ChatResult | None:
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                SELECT result_json, expires_at
                FROM ai_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            if float(row[1]) <= self._clock():
                await connection.execute(
                    "DELETE FROM ai_cache WHERE cache_key = ? AND expires_at <= ?",
                    (cache_key, self._clock()),
                )
                await connection.commit()
                return None
            try:
                payload = json.loads(row[0])
                return ChatResult(**payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                await connection.execute(
                    "DELETE FROM ai_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                await connection.commit()
                return None
        finally:
            await connection.close()

    async def delete(self, cache_key: str) -> None:
        """Evict a cache entry that failed downstream validation."""
        connection = await self._connect()
        try:
            await connection.execute(
                "DELETE FROM ai_cache WHERE cache_key = ?",
                (cache_key,),
            )
            await connection.commit()
        finally:
            await connection.close()

    async def set(self, cache_key: str, result: ChatResult) -> None:
        connection = await self._connect()
        try:
            await connection.execute(
                """
                INSERT INTO ai_cache
                    (cache_key, result_json, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result_json = excluded.result_json,
                    expires_at = excluded.expires_at,
                    updated_at = datetime('now')
                """,
                (
                    cache_key,
                    canonical_json(asdict(result)),
                    self._clock() + self.ttl_seconds,
                ),
            )
            await connection.commit()
        finally:
            await connection.close()

    @asynccontextmanager
    async def lock(self, cache_key: str) -> AsyncIterator[None]:
        """Serialize same-key misses across service/cache instances."""
        identity = (str(Path(self.db_path).resolve()), cache_key)
        with self._locks_guard:
            entry = self._locks.setdefault(
                identity,
                _LockEntry(lock=asyncio.Lock()),
            )
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            with self._locks_guard:
                entry.users -= 1
                if entry.users == 0 and not entry.lock.locked():
                    self._locks.pop(identity, None)
