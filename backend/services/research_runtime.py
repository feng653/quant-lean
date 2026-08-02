"""Runtime adapter for warning-first personal research generations.

This module is deliberately separate from the governed production PIT gate.
Research and paper simulation may proceed with disclosed evidence gaps, while
missing/corrupt/non-computable market input still fails before persistence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from backend.data.research_data_store import (
    ResearchDataStore,
    ResearchDataStoreError,
)


RESEARCH_RUNTIME_FIELDS = ("open", "high", "low", "close", "volume", "amount")


class ResearchRuntimeError(RuntimeError):
    """The selected immutable research generation cannot be computed."""

    def __init__(self, code: str, message: str, report: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.report = dict(report or {})


async def load_research_market(
    *,
    pool_id: str,
    required_start: str,
    required_end: str,
    generation_id: str | None = None,
    store: ResearchDataStore | None = None,
) -> dict[str, Any]:
    """Load a bounded local research generation without any network fallback."""

    selected = store or ResearchDataStore()
    try:
        result = await asyncio.to_thread(
            selected.load_market_frame,
            pool_id=pool_id,
            required_start=required_start,
            required_end=required_end,
            generation_id=generation_id,
            fields=RESEARCH_RUNTIME_FIELDS,
        )
    except ResearchDataStoreError as exc:
        raise ResearchRuntimeError(
            "research_generation_integrity_invalid",
            str(exc),
        ) from exc
    report = result.get("report") if isinstance(result, Mapping) else None
    frame = result.get("frame") if isinstance(result, Mapping) else None
    if not isinstance(report, Mapping) or report.get("ready") is not True or frame is None:
        issues = [str(item) for item in (report or {}).get("issues", [])]
        code = issues[0] if issues else "research_market_not_computable"
        raise ResearchRuntimeError(
            code,
            "研究数据代缺少可计算的本地行情窗口",
            report,
        )
    if frame.empty or len(frame.columns) == 0:
        raise ResearchRuntimeError(
            "research_market_zero_coverage",
            "研究数据代的行情窗口为空",
            report,
        )
    if not isinstance(report.get("timeline_identity"), Mapping):
        raise ResearchRuntimeError(
            "research_membership_timeline_not_replayable",
            "研究指数窗口缺少可重放的 PIT 成分时间线",
            {
                **dict(report),
                "ready": False,
                "issues": ["research_membership_timeline_not_replayable"],
            },
        )
    return dict(result)


async def load_research_benchmark(
    *,
    index_code: str,
    required_start: str,
    required_end: str,
    generation_id: str,
    store: ResearchDataStore | None = None,
) -> dict[str, Any]:
    """Load a same-generation benchmark; absence remains a research warning."""

    selected = store or ResearchDataStore()
    try:
        return await asyncio.to_thread(
            selected.load_benchmark,
            index_code=index_code,
            required_start=required_start,
            required_end=required_end,
            generation_id=generation_id,
        )
    except ResearchDataStoreError as exc:
        raise ResearchRuntimeError(
            "research_benchmark_integrity_invalid",
            "研究数据代的基准文件或数据库完整性校验失败",
            {
                "ready": False,
                "generation_id": generation_id,
                "issues": ["research_benchmark_integrity_invalid"],
                "live_eligible": False,
            },
        ) from exc


def build_research_trust(
    *,
    market_result: Mapping[str, Any],
    required_start: str,
    required_end: str,
    purpose: str,
    benchmark_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the durable warning-first contract persisted with a run."""

    report = dict(market_result.get("report") or {})
    provenance = dict(market_result.get("source_provenance") or {})
    generation_id = str(report.get("generation_id") or "")
    candidate_report_sha256 = report.get("candidate_report_sha256")
    warnings = {
        "single_source_tushare_research",
        "not_cross_validated",
        "provider_available_at_missing",
        "production_dual_price_ledger_not_certified",
        "live_trading_not_eligible",
        *[str(item) for item in report.get("warnings", [])],
    }
    if benchmark_report is not None:
        warnings.update(str(item) for item in benchmark_report.get("warnings", []))
        if benchmark_report.get("ready") is not True:
            warnings.add("benchmark_unavailable_metrics_are_na")
            warnings.update(
                f"benchmark:{item}" for item in benchmark_report.get("issues", [])
            )
    limitations = sorted(
        {
            "single_source_not_independent_consensus",
            "monthly_membership_snapshot_not_exact_intramonth_event_time",
            "bitemporal_provider_availability_not_proven",
            "adjusted_prices_not_certified_for_execution_or_valuation",
            *warnings,
        }
    )
    return {
        "schema_version": "tushare-research-trust/v1",
        "profile": "tushare_research_trusted",
        "purpose": purpose,
        "eligible": True,
        "blockers": [],
        "warnings": sorted(warnings),
        "known_limitations": limitations,
        "claims": {
            "eligible_for_conditional_research": True,
            "eligible_for_real_tuning": True,
            "eligible_for_paper_trading": True,
            "eligible_for_live_trading": False,
        },
        "evidence": {
            "candidate_report_sha256": candidate_report_sha256,
            "research_generation_id": generation_id,
            "source_provenance_sha256": provenance.get("content_sha256"),
        },
        "runtime_binding": {
            "generation_id": generation_id,
            "runtime_dataset_digest": provenance.get("content_sha256"),
            "source_ids": [str(provenance.get("provider") or "tushare")],
            "required_window": {"start": required_start, "end": required_end},
            "actual_window": {
                "start": report.get("date_start"),
                "end": report.get("date_end"),
            },
            "timeline_identity": report.get("timeline_identity"),
            "network_accessed": False,
            "live_eligible": False,
        },
    }


def normalize_research_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the plural legacy shape while retaining generation identity."""

    result = dict(value)
    provider = str(result.get("provider") or "")
    adjustment = str(result.get("adjustment") or "")
    result["providers"] = [provider] if provider else []
    result["adjustments"] = [adjustment] if adjustment else []
    return result


def verify_research_runtime_binding(
    binding: Mapping[str, Any],
    market_result: Mapping[str, Any],
) -> None:
    """Require worker-derived semantics to equal this experiment's binding."""

    report = market_result.get("report")
    provenance = market_result.get("source_provenance")
    if not isinstance(report, Mapping) or not isinstance(provenance, Mapping):
        raise ResearchRuntimeError(
            "research_runtime_binding_unverifiable",
            "研究数据运行结果缺少可复核语义",
        )
    actual_window = {
        "start": report.get("date_start"),
        "end": report.get("date_end"),
    }
    if (
        binding.get("runtime_dataset_digest")
        != provenance.get("content_sha256")
        or binding.get("actual_window") != actual_window
        or binding.get("timeline_identity") != report.get("timeline_identity")
    ):
        raise ResearchRuntimeError(
            "research_runtime_binding_changed",
            "研究数据提交绑定与 worker 实际派生语义不一致",
            {
                "ready": False,
                "generation_id": report.get("generation_id"),
                "issues": ["research_runtime_binding_changed"],
            },
        )
