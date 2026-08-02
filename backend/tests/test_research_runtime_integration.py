from __future__ import annotations

import asyncio

import pandas as pd
import pytest
from fastapi import HTTPException

from backend.api import data as data_api, experiments
from backend.data.research_data_store import ResearchDataStoreError
from backend.services import research_runtime, simulation


def _market_result() -> dict:
    frame = pd.DataFrame(
        {
            ("000001.SZ", "open"): [10.0, 11.0],
            ("000001.SZ", "close"): [10.5, 11.5],
            ("000001.SZ", "volume"): [100.0, 120.0],
        },
        index=pd.to_datetime(["2020-01-02", "2020-01-03"]),
    )
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    generation = "a" * 64
    return {
        "frame": frame,
        "source_provenance": {
            "provider": "tushare",
            "adjustment": "hfq",
            "generation_id": generation,
            "content_sha256": "b" * 64,
        },
        "report": {
            "ready": True,
            "generation_id": generation,
            "candidate_report_sha256": "c" * 64,
            "date_start": "2020-01-02",
            "date_end": "2020-01-03",
            "timeline_identity": {"timeline_hash": "d" * 64},
            "warnings": ["requested_window_partially_covered"],
            "issues": [],
        },
    }


def test_experiment_submission_binds_active_research_generation(monkeypatch) -> None:
    market = _market_result()

    async def load_market(**_kwargs):
        return market

    async def load_benchmark(**kwargs):
        assert kwargs["generation_id"] == "a" * 64
        return {
            "series": None,
            "report": {
                "ready": False,
                "issues": ["benchmark_index_daily_not_materialized"],
                "warnings": ["benchmark_missing_warning_only_for_research"],
            },
        }

    monkeypatch.setattr(research_runtime, "load_research_market", load_market)
    monkeypatch.setattr(research_runtime, "load_research_benchmark", load_benchmark)

    trust = asyncio.run(
        experiments._require_pit_submission(
            pool_id="csi300",
            train_start=None,
            test_start="2020-01-02",
            test_end="2020-01-03",
            data_access_policy="cache_only",
            research_trust_profile="tushare_research_trusted",
            purpose="research",
        )
    )

    assert trust is not None
    assert trust["eligible"] is True
    assert trust["runtime_binding"]["generation_id"] == "a" * 64
    assert "benchmark_unavailable_metrics_are_na" in trust["warnings"]
    assert trust["claims"]["eligible_for_live_trading"] is False


def test_benchmark_integrity_error_is_hard_block_but_missing_rows_are_not(
    monkeypatch,
) -> None:
    class BrokenStore:
        def load_benchmark(self, **_kwargs):
            raise ResearchDataStoreError("research generation binding changed")

    with pytest.raises(
        research_runtime.ResearchRuntimeError,
        match="基准文件或数据库完整性",
    ) as captured:
        asyncio.run(
            research_runtime.load_research_benchmark(
                index_code="000300.SH",
                required_start="2020-01-01",
                required_end="2020-01-03",
                generation_id="a" * 64,
                store=BrokenStore(),
            )
        )
    assert captured.value.code == "research_benchmark_integrity_invalid"

    class MissingRowsStore:
        def load_benchmark(self, **_kwargs):
            return {
                "series": None,
                "report": {
                    "ready": False,
                    "issues": ["benchmark_index_daily_not_materialized"],
                },
            }

    missing = asyncio.run(
        research_runtime.load_research_benchmark(
            index_code="000300.SH",
            required_start="2020-01-01",
            required_end="2020-01-03",
            generation_id="a" * 64,
            store=MissingRowsStore(),
        )
    )
    assert missing["report"]["ready"] is False


def test_readiness_and_submission_reject_benchmark_integrity_error(
    monkeypatch,
) -> None:
    market = _market_result()

    async def load_market(**_kwargs):
        return market

    async def broken_benchmark(**_kwargs):
        raise research_runtime.ResearchRuntimeError(
            "research_benchmark_integrity_invalid",
            "研究数据代的基准完整性失败",
            {
                "ready": False,
                "issues": ["research_benchmark_integrity_invalid"],
            },
        )

    monkeypatch.setattr(research_runtime, "load_research_market", load_market)
    monkeypatch.setattr(
        research_runtime, "load_research_benchmark", broken_benchmark
    )

    readiness = asyncio.run(
        data_api.inspect_experiment_data_readiness(
            data_api.ExperimentDataReadinessBody(
                data_access_policy="cache_only",
                research_trust_profile="tushare_research_trusted",
                pool_preset="csi300",
                test_start="2020-01-02",
                test_end="2020-01-03",
            ),
            user={"id": 7},
        )
    )["data"]
    assert readiness["ready"] is False
    assert readiness["technical_blockers"] == [
        {
            "code": "research_benchmark_integrity_invalid",
            "source": "tushare_candidate_evidence",
        }
    ]

    with pytest.raises(HTTPException) as submission:
        asyncio.run(
            experiments._require_pit_submission(
                pool_id="csi300",
                train_start=None,
                test_start="2020-01-02",
                test_end="2020-01-03",
                data_access_policy="cache_only",
                research_trust_profile="tushare_research_trusted",
                purpose="research",
            )
        )
    assert submission.value.status_code == 409
    assert submission.value.detail["code"] == (
        "research_benchmark_integrity_invalid"
    )


def test_simulation_loads_bound_generation_without_legacy_cache(monkeypatch) -> None:
    market = _market_result()
    calls: list[dict] = []

    async def load_market(**kwargs):
        calls.append(kwargs)
        return market

    monkeypatch.setattr(research_runtime, "load_research_market", load_market)

    loaded = asyncio.run(
        simulation._load_pivot(
            "csi300",
            "2020-01-03",
            {},
            required_start="2020-01-02",
            generation_id="a" * 64,
        )
    )

    assert loaded is market["frame"]
    assert calls == [
        {
            "pool_id": "csi300",
            "required_start": "2020-01-02",
            "required_end": "2020-01-03",
            "generation_id": "a" * 64,
        }
    ]


def test_market_without_replayable_timeline_is_technical_blocker() -> None:
    market = _market_result()
    market["report"].pop("timeline_identity")

    class Store:
        def load_market_frame(self, **_kwargs):
            return market

    with pytest.raises(research_runtime.ResearchRuntimeError) as captured:
        asyncio.run(
            research_runtime.load_research_market(
                pool_id="csi300",
                required_start="2020-01-01",
                required_end="2020-01-03",
                store=Store(),
            )
        )
    assert captured.value.code == (
        "research_membership_timeline_not_replayable"
    )
