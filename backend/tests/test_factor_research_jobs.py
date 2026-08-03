"""Durable factor research worker contract."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.jobs.handlers import execute_job
from backend.jobs import broker as broker_module
from backend.jobs.broker import JobBroker
from backend.services import factor_research


def test_factor_worker_uses_job_owner_and_persists_only_result_digests(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        monkeypatch.setattr(broker_module, "_broker_instance", broker)
        captured: dict[str, Any] = {}

        async def fake_execute(
            body,
            *,
            owner_user_id: int,
            progress,
            source_job_uuid: str,
        ):
            captured["owner_user_id"] = owner_user_id
            captured["factor_id"] = body.factor_id
            captured["source_job_uuid"] = source_job_uuid
            await progress(0.5, "计算中", "computing")
            return {
                "run": {
                    "run_id": "frun_" + "1" * 32,
                    "dataset_digest": "a" * 64,
                    "result_digest": "b" * 64,
                },
                "sensitive_full_result": {"must_not": "enter job result"},
            }

        monkeypatch.setattr(
            factor_research,
            "execute_factor_research",
            fake_execute,
        )
        job_id = await broker.submit_job(
            "factor_research",
            {
                "factor_id": "momentum_20",
                "pool_preset": "csi300",
                "pool_custom_codes": [],
                "start": "2024-01-01",
                "end": "2024-02-01",
                "horizons": [1, 5],
                "primary_horizon": 5,
                "quantiles": 5,
                "winsor_method": "mad",
                "owner_user_id": 999,
            },
            user_id=7,
        )
        job = await broker.get_job_status(job_id)
        assert job is not None

        await execute_job(job)

        completed = await broker.get_job_status(job_id)
        assert completed is not None
        assert completed["status"] == "completed"
        assert captured == {
            "owner_user_id": 7,
            "factor_id": "momentum_20",
            "source_job_uuid": job_id,
        }
        assert completed["result"] == {
            "run_id": "frun_" + "1" * 32,
            "dataset_digest": "a" * 64,
            "result_digest": "b" * 64,
        }

    asyncio.run(scenario())


def test_factor_worker_records_safe_structured_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        monkeypatch.setattr(broker_module, "_broker_instance", broker)

        async def fail_execute(*_args, **_kwargs):
            raise factor_research.FactorResearchExecutionError(
                code="factor_cache_integrity_invalid",
                message="缓存完整性校验失败",
                status_code=409,
                cache_key="csi300",
            )

        monkeypatch.setattr(
            factor_research,
            "execute_factor_research",
            fail_execute,
        )
        job_id = await broker.submit_job(
            "factor_research",
            {
                "factor_id": "momentum_20",
                "start": "2024-01-01",
                "end": "2024-02-01",
            },
            user_id=7,
        )
        job = await broker.get_job_status(job_id)
        assert job is not None

        await execute_job(job)

        failed = await broker.get_job_status(job_id)
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["result"] == {
            "error_code": "factor_cache_integrity_invalid",
            "message": "缓存完整性校验失败",
            "cache_key": "csi300",
            "action": "refresh_in_data_center",
        }
        assert "/" not in str(failed["error"])

    asyncio.run(scenario())
