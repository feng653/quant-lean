"""Governed AI invocation service with caching and usage accounting."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import aiosqlite

from backend.ai.cache import AiCache, build_cache_key
from backend.ai.client import ChatResult, DeepSeekClient, get_deepseek_client
from backend.config import settings

DEFAULT_SYSTEM_PROMPT = "你是一名专业的量化金融分析师。"


@dataclass(frozen=True)
class AiInvocationResult:
    """Result returned to callers after cache and usage governance."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached: bool
    cache_key: str
    latency_ms: float
    structured: Any = None


class AiService:
    """Single entry point for cached, auditable model calls."""

    def __init__(
        self,
        client: DeepSeekClient | None = None,
        *,
        db_path: str | Path | None = None,
        cache: AiCache | None = None,
        clock: Callable[[], float] = time.time,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        resolved_db = db_path or settings.abs_path(settings.EXPERIMENT_DB)
        self.db_path = str(resolved_db)
        self.client = client or get_deepseek_client()
        self.cache = cache or AiCache(self.db_path, clock=clock)
        self._timer = timer

    async def _connect(self) -> aiosqlite.Connection:
        connection = await aiosqlite.connect(self.db_path)
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                user_id INTEGER,
                cache_key TEXT NOT NULL,
                model TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cache_hit INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1,
                error_type TEXT,
                latency_ms REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cursor = await connection.execute("PRAGMA table_info(ai_usage)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "latency_ms" not in columns:
            try:
                await connection.execute(
                    """
                    ALTER TABLE ai_usage
                    ADD COLUMN latency_ms REAL NOT NULL DEFAULT 0
                    """
                )
            except aiosqlite.OperationalError as exc:
                # A concurrent first caller may have added it after our PRAGMA.
                if "duplicate column name" not in str(exc).lower():
                    raise
        await connection.commit()
        return connection

    async def _record_usage(
        self,
        *,
        endpoint: str,
        user_id: int | None,
        cache_key: str,
        result: ChatResult | None,
        cache_hit: bool,
        invocation_started: float,
        error: Exception | None = None,
    ) -> float:
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                INSERT INTO ai_usage
                    (endpoint, user_id, cache_key, model, prompt_tokens,
                     completion_tokens, total_tokens, cache_hit, success,
                     error_type, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    endpoint,
                    user_id,
                    cache_key,
                    result.model if result is not None else None,
                    0 if cache_hit or result is None else result.prompt_tokens,
                    0 if cache_hit or result is None else result.completion_tokens,
                    0 if cache_hit or result is None else result.total_tokens,
                    1 if cache_hit else 0,
                    0 if error is not None else 1,
                    type(error).__name__ if error is not None else None,
                    0.0,
                ),
            )
            await connection.commit()
            latency_ms = max(
                0.0,
                (self._timer() - invocation_started) * 1000,
            )
            await connection.execute(
                "UPDATE ai_usage SET latency_ms = ? WHERE id = ?",
                (latency_ms, cursor.lastrowid),
            )
            await connection.commit()
            return latency_ms
        finally:
            await connection.close()

    @staticmethod
    def _to_invocation(
        result: ChatResult,
        *,
        cached: bool,
        cache_key: str,
        latency_ms: float,
        structured: Any = None,
    ) -> AiInvocationResult:
        return AiInvocationResult(
            text=result.text,
            model=result.model,
            prompt_tokens=0 if cached else result.prompt_tokens,
            completion_tokens=0 if cached else result.completion_tokens,
            total_tokens=0 if cached else result.total_tokens,
            cached=cached,
            cache_key=cache_key,
            latency_ms=latency_ms,
            structured=structured,
        )

    async def invoke(
        self,
        endpoint: str,
        user_id: int | None,
        prompt_template: str,
        *,
        cache_context: Any = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        validator: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> AiInvocationResult:
        """Invoke a model once per stable input and record every attempt."""
        invocation_started = self._timer()
        cache_key = build_cache_key(
            endpoint=endpoint,
            prompt_template=prompt_template,
            system_prompt=system_prompt,
            kwargs=kwargs,
            cache_context={
                "user_id": user_id,
                "context": cache_context,
            },
        )
        async with self.cache.lock(cache_key):
            cached = await self.cache.get(cache_key)
            if cached is not None:
                try:
                    structured = (
                        validator(cached.text)
                        if validator is not None
                        else None
                    )
                except Exception:
                    # Never serve a legacy/corrupt response that no longer
                    # satisfies the endpoint contract.
                    await self.cache.delete(cache_key)
                else:
                    latency_ms = await self._record_usage(
                        endpoint=endpoint,
                        user_id=user_id,
                        cache_key=cache_key,
                        result=cached,
                        cache_hit=True,
                        invocation_started=invocation_started,
                    )
                    return self._to_invocation(
                        cached,
                        cached=True,
                        cache_key=cache_key,
                        latency_ms=latency_ms,
                        structured=structured,
                    )

            try:
                result = await self.client.chat_with_usage(
                    prompt_template,
                    system_prompt=system_prompt,
                    **kwargs,
                )
            except Exception as exc:
                await self._record_usage(
                    endpoint=endpoint,
                    user_id=user_id,
                    cache_key=cache_key,
                    result=None,
                    cache_hit=False,
                    invocation_started=invocation_started,
                    error=exc,
                )
                raise

            structured = None
            try:
                if validator is not None:
                    structured = validator(result.text)
            except Exception as exc:
                await self._record_usage(
                    endpoint=endpoint,
                    user_id=user_id,
                    cache_key=cache_key,
                    result=result,
                    cache_hit=False,
                    invocation_started=invocation_started,
                    error=exc,
                )
                raise

            await self.cache.set(cache_key, result)
            latency_ms = await self._record_usage(
                endpoint=endpoint,
                user_id=user_id,
                cache_key=cache_key,
                result=result,
                cache_hit=False,
                invocation_started=invocation_started,
            )
            return self._to_invocation(
                result,
                cached=False,
                cache_key=cache_key,
                latency_ms=latency_ms,
                structured=structured,
            )
