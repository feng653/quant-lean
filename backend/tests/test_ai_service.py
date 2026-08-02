from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import aiosqlite
import httpx
import pytest

from backend.ai.cache import AiCache, build_cache_key
from backend.ai.client import ChatResult, DeepSeekClient
from backend.ai.service import AiService


class MockAiClient:
    def __init__(
        self,
        *,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def chat_with_usage(
        self,
        prompt_template: str,
        system_prompt: str,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(
            {
                "prompt_template": prompt_template,
                "system_prompt": system_prompt,
                "kwargs": kwargs,
            }
        )
        if self.error is not None:
            raise self.error
        return ChatResult(
            text=f"analysis-{len(self.calls)}",
            model="deepseek-test",
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        )


async def _usage_rows(db_path) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(db_path) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            """
            SELECT endpoint, user_id, model, prompt_tokens, completion_tokens,
                   total_tokens, cache_hit, success, error_type, latency_ms
            FROM ai_usage
            ORDER BY id
            """
        )
        return await cursor.fetchall()


def test_invoke_cache_miss_then_hit_records_usage(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        client = MockAiClient()
        service = AiService(client, db_path=db_path)

        first = await service.invoke(
            "backtest-analysis",
            42,
            "Analyze {strategy}",
            strategy="alpha",
            cache_context={"experiment_id": 7},
        )
        second = await service.invoke(
            "backtest-analysis",
            42,
            "Analyze {strategy}",
            cache_context={"experiment_id": 7},
            strategy="alpha",
        )

        assert len(client.calls) == 1
        assert first.cached is False
        assert first.total_tokens == 18
        assert second.cached is True
        assert second.text == first.text
        assert second.total_tokens == 0
        assert second.cache_key == first.cache_key
        assert first.latency_ms >= 0
        assert second.latency_ms >= 0

        rows = await _usage_rows(db_path)
        assert [row["cache_hit"] for row in rows] == [0, 1]
        assert [row["success"] for row in rows] == [1, 1]
        assert [row["total_tokens"] for row in rows] == [18, 0]
        assert rows[0]["latency_ms"] == pytest.approx(first.latency_ms)
        assert rows[1]["latency_ms"] == pytest.approx(second.latency_ms)
        assert all(row["user_id"] == 42 for row in rows)

    asyncio.run(scenario())


def test_cache_ttl_and_context_changes_force_model_call(tmp_path) -> None:
    async def scenario() -> None:
        now = [1_000.0]
        db_path = tmp_path / "experiment.db"
        client = MockAiClient()
        cache = AiCache(
            db_path,
            ttl_seconds=86_400,
            clock=lambda: now[0],
        )
        service = AiService(client, db_path=db_path, cache=cache)

        first = await service.invoke(
            "diagnosis",
            1,
            "{value}",
            value={"b": 2, "a": 1},
            cache_context={"version": 1},
        )
        reordered = await service.invoke(
            "diagnosis",
            1,
            "{value}",
            value={"a": 1, "b": 2},
            cache_context={"version": 1},
        )
        changed_context = await service.invoke(
            "diagnosis",
            1,
            "{value}",
            value={"a": 1, "b": 2},
            cache_context={"version": 2},
        )
        now[0] += 86_401
        expired = await service.invoke(
            "diagnosis",
            1,
            "{value}",
            value={"a": 1, "b": 2},
            cache_context={"version": 1},
        )

        assert first.cached is False
        assert reordered.cached is True
        assert changed_context.cached is False
        assert expired.cached is False
        assert len(client.calls) == 3

    asyncio.run(scenario())


def test_cache_keys_are_isolated_by_user(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        client = MockAiClient()
        service = AiService(client, db_path=db_path)

        results = await asyncio.gather(
            *[
                service.invoke(
                    "insight",
                    user_id,
                    "Portfolio {portfolio_id}",
                    portfolio_id=9,
                    cache_context={"revision": 3},
                )
                for user_id in range(5)
            ]
        )

        assert len(client.calls) == 5
        assert all(not result.cached for result in results)
        assert len({result.cache_key for result in results}) == 5
        rows = await _usage_rows(db_path)
        assert len(rows) == 5

    asyncio.run(scenario())


def test_concurrent_same_user_invocations_are_coalesced(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        client = MockAiClient()
        service = AiService(client, db_path=db_path)

        results = await asyncio.gather(
            *[
                service.invoke(
                    "insight",
                    7,
                    "Portfolio {portfolio_id}",
                    portfolio_id=9,
                    cache_context={"revision": 3},
                )
                for _ in range(5)
            ]
        )

        assert len(client.calls) == 1
        assert sum(not result.cached for result in results) == 1
        assert sum(result.cached for result in results) == 4

    asyncio.run(scenario())


def test_validator_runs_before_success_and_cache_write(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        client = MockAiClient()
        service = AiService(client, db_path=db_path)

        def reject_response(text: str) -> dict[str, str]:
            raise ValueError(f"invalid structured response: {text}")

        for _ in range(2):
            with pytest.raises(ValueError, match="invalid structured response"):
                await service.invoke(
                    "suggestions",
                    7,
                    "Suggest {strategy}",
                    strategy="alpha",
                    validator=reject_response,
                )

        assert len(client.calls) == 2
        rows = await _usage_rows(db_path)
        assert [row["success"] for row in rows] == [0, 0]
        assert [row["total_tokens"] for row in rows] == [18, 18]
        async with aiosqlite.connect(db_path) as connection:
            count = await (
                await connection.execute("SELECT COUNT(*) FROM ai_cache")
            ).fetchone()
        assert count[0] == 0

    asyncio.run(scenario())


def test_invalid_cached_response_is_evicted_and_refetched(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        client = MockAiClient()
        service = AiService(client, db_path=db_path)

        first = await service.invoke(
            "diagnosis",
            7,
            "Diagnose {item}",
            item="alpha",
        )

        def validate(text: str) -> dict[str, str]:
            if text != "analysis-2":
                raise ValueError("stale cache contract")
            return {"diagnosis": text}

        second = await service.invoke(
            "diagnosis",
            7,
            "Diagnose {item}",
            item="alpha",
            validator=validate,
        )
        third = await service.invoke(
            "diagnosis",
            7,
            "Diagnose {item}",
            item="alpha",
            validator=validate,
        )

        assert first.text == "analysis-1"
        assert second.cached is False
        assert second.structured == {"diagnosis": "analysis-2"}
        assert third.cached is True
        assert third.structured == {"diagnosis": "analysis-2"}
        assert len(client.calls) == 2
        rows = await _usage_rows(db_path)
        assert [row["success"] for row in rows] == [1, 1, 1]
        assert [row["cache_hit"] for row in rows] == [0, 0, 1]

    asyncio.run(scenario())


def test_concurrent_different_keys_write_cache_safely(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        client = MockAiClient()
        service = AiService(client, db_path=db_path)

        results = await asyncio.gather(
            *[
                service.invoke(
                    "analysis",
                    1,
                    "Analyze {item}",
                    item=f"item-{index}",
                )
                for index in range(8)
            ]
        )

        assert len(client.calls) == 8
        assert all(not result.cached for result in results)
        async with aiosqlite.connect(db_path) as connection:
            cache_count = await (
                await connection.execute("SELECT COUNT(*) FROM ai_cache")
            ).fetchone()
            usage_count = await (
                await connection.execute("SELECT COUNT(*) FROM ai_usage")
            ).fetchone()
        assert cache_count[0] == 8
        assert usage_count[0] == 8

    asyncio.run(scenario())


def test_concurrent_schema_initialization_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        async with aiosqlite.connect(db_path) as connection:
            await connection.execute(
                """
                CREATE TABLE ai_usage (
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
                    created_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            await connection.commit()
        services = [
            AiService(MockAiClient(), db_path=db_path)
            for _ in range(8)
        ]

        results = await asyncio.gather(
            *[
                service.invoke(
                    "schema-init",
                    index,
                    "Analyze {item}",
                    item=f"item-{index}",
                )
                for index, service in enumerate(services)
            ]
        )

        assert len(results) == 8
        async with aiosqlite.connect(db_path) as connection:
            usage_columns = await (
                await connection.execute("PRAGMA table_info(ai_usage)")
            ).fetchall()
            cache_columns = await (
                await connection.execute("PRAGMA table_info(ai_cache)")
            ).fetchall()
            usage_count = await (
                await connection.execute("SELECT COUNT(*) FROM ai_usage")
            ).fetchone()
        assert [column[1] for column in usage_columns].count("latency_ms") == 1
        assert [column[1] for column in cache_columns].count("cache_key") == 1
        assert usage_count[0] == 8

    asyncio.run(scenario())


def test_provider_error_is_recorded_but_not_cached(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        client = MockAiClient(error=RuntimeError("provider unavailable"))
        service = AiService(client, db_path=db_path)

        for _ in range(2):
            with pytest.raises(RuntimeError, match="provider unavailable"):
                await service.invoke("analysis", 5, "Analyze {item}", item="A")

        assert len(client.calls) == 2
        rows = await _usage_rows(db_path)
        assert len(rows) == 2
        assert all(row["success"] == 0 for row in rows)
        assert all(row["cache_hit"] == 0 for row in rows)
        assert all(row["error_type"] == "RuntimeError" for row in rows)
        assert all(row["latency_ms"] >= 0 for row in rows)
        async with aiosqlite.connect(db_path) as connection:
            cursor = await connection.execute("SELECT COUNT(*) FROM ai_cache")
            assert (await cursor.fetchone())[0] == 0

    asyncio.run(scenario())


def test_missing_api_key_does_not_fabricate_success(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("backend.ai.client.settings.DEEPSEEK_API_KEY", "")
        client = DeepSeekClient(api_key="")
        service = AiService(client, db_path=tmp_path / "experiment.db")

        with pytest.raises(ValueError, match="API key"):
            await service.invoke("analysis", 9, "No placeholders")

        rows = await _usage_rows(tmp_path / "experiment.db")
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["error_type"] == "ValueError"
        assert rows[0]["latency_ms"] >= 0

    asyncio.run(scenario())


def test_chat_remains_text_only_compatibility_entrypoint(monkeypatch) -> None:
    async def scenario() -> None:
        client = DeepSeekClient(api_key="test-key")
        expected = ChatResult(
            text="trimmed response",
            model="deepseek-test",
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
        )

        async def fake_chat_with_usage(*args, **kwargs):
            return expected

        monkeypatch.setattr(client, "chat_with_usage", fake_chat_with_usage)

        assert await client.chat("Hello {name}", name="quant") == expected.text

    asyncio.run(scenario())


def test_chat_with_usage_parses_provider_response() -> None:
    class TransportClient(DeepSeekClient):
        @property
        def _client(self) -> httpx.AsyncClient:
            async def handler(request: httpx.Request) -> httpx.Response:
                assert request.headers["Authorization"] == "Bearer test-key"
                return httpx.Response(
                    200,
                    json={
                        "model": "deepseek-chat-2026",
                        "choices": [
                            {"message": {"content": "  provider answer  "}}
                        ],
                        "usage": {
                            "prompt_tokens": 13,
                            "completion_tokens": 5,
                            "total_tokens": 18,
                        },
                    },
                )

            return httpx.AsyncClient(
                base_url="https://deepseek.invalid",
                transport=httpx.MockTransport(handler),
                headers={"Authorization": f"Bearer {self._api_key}"},
            )

    async def scenario() -> None:
        client = TransportClient(
            api_key="test-key",
            base_url="https://deepseek.invalid",
        )
        result = await client.chat_with_usage(
            "Analyze {strategy}",
            strategy="alpha",
        )

        assert result == ChatResult(
            text="provider answer",
            model="deepseek-chat-2026",
            prompt_tokens=13,
            completion_tokens=5,
            total_tokens=18,
        )

    asyncio.run(scenario())


def test_cache_key_normalization_is_stable_and_context_sensitive() -> None:
    first = build_cache_key(
        endpoint="analysis",
        prompt_template="{payload}",
        system_prompt="system",
        kwargs={"payload": {"b": 2, "a": [3, 1]}},
        cache_context={"experiment": 8, "labels": {"beta", "alpha"}},
    )
    reordered = build_cache_key(
        endpoint="analysis",
        prompt_template="{payload}",
        system_prompt="system",
        kwargs={"payload": {"a": [3, 1], "b": 2}},
        cache_context={"labels": {"alpha", "beta"}, "experiment": 8},
    )
    changed = build_cache_key(
        endpoint="analysis",
        prompt_template="{payload}",
        system_prompt="system",
        kwargs={"payload": {"a": [3, 1], "b": 2}},
        cache_context={"labels": {"alpha", "beta"}, "experiment": 9},
    )

    assert first == reordered
    assert first != changed
    assert len(first) == 64
    assert asdict(
        ChatResult("text", "model", 1, 2, 3)
    ) == {
        "text": "text",
        "model": "model",
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
