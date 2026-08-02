"""Single fail-closed policy for every formal research/runtime data consumer.

The platform deliberately keeps collection separate from execution.  A legacy
Parquet cache may help a governance workflow discover sessions, but it is never
runtime evidence.  Formal consumers receive data only after an activated PIT
membership timeline and an exact canonical dual-price binding have both been
verified for the requested window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

import pandas as pd

from backend.data.cache import DataCache
from backend.data.cache_readiness import CachedMarketData, inspect_cached_market_data


PIT_RUNTIME_POOLS = frozenset({"csi300", "csi500", "csi800", "csi1000"})
PIT_ONLY_DATA_POLICY = "pit_cache_only"
PitPurpose = Literal["research", "tuning", "execution"]


class PitRuntimeDataError(RuntimeError):
    """A formal run cannot prove that every input is point-in-time."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        report: dict[str, Any] | None = None,
    ) -> None:
        # Keep the stable machine code in worker tracebacks and durable job
        # errors; HTTP handlers still expose ``message`` separately.
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.report = report or {}


@dataclass(frozen=True)
class PitRuntimeInput:
    pool_id: str
    purpose: PitPurpose
    market: CachedMarketData


def require_pit_pool(pool_id: str | None) -> str:
    normalized = str(pool_id or "csi300").strip().lower()
    if normalized not in PIT_RUNTIME_POOLS:
        raise PitRuntimeDataError(
            "point_in_time_pool_unsupported",
            "正式研究仅支持已治理的中证点时股票池；自定义、全 A 与静态股票池已停用",
            report={"pool_id": normalized, "allowed_pools": sorted(PIT_RUNTIME_POOLS)},
        )
    return normalized


def _failure_code(report: dict[str, Any], purpose: PitPurpose) -> str:
    point_in_time = report.get("point_in_time")
    universe = (
        point_in_time.get("universe")
        if isinstance(point_in_time, dict)
        else None
    )
    if not report.get("universe_point_in_time"):
        if isinstance(universe, dict) and universe.get("reason"):
            return str(universe["reason"])
        return "point_in_time_universe_missing"
    if not report.get("canonical_runtime_price_bound"):
        ledger = report.get("price_ledger")
        if isinstance(ledger, dict) and ledger.get("reason"):
            return str(ledger["reason"])
        return "canonical_runtime_binding_missing"
    if not report.get("authoritative_trading_calendar_bound"):
        return "pit_trading_calendar_binding_missing"
    if report.get("required_point_in_time_benchmark") and not report.get(
        "point_in_time_benchmark_bound"
    ):
        return "pit_benchmark_binding_missing"
    if purpose == "execution":
        return "pit_execution_evidence_incomplete"
    if purpose == "tuning":
        return "pit_tuning_evidence_incomplete"
    return "pit_unbiased_research_evidence_incomplete"


async def inspect_pit_runtime_input(
    *,
    pool_id: str | None,
    required_start: str,
    required_end: str,
    purpose: PitPurpose,
    requested_codes: Iterable[str] = (),
    cache: DataCache | None = None,
    point_in_time_store: Any | None = None,
    price_ledger_store: Any | None = None,
    require_benchmark: bool = True,
) -> PitRuntimeInput:
    """Inspect exact local evidence without constructing a network source."""

    normalized_pool = require_pit_pool(pool_id)
    try:
        start = pd.Timestamp(required_start).normalize()
        end = pd.Timestamp(required_end).normalize()
    except (TypeError, ValueError) as exc:
        raise PitRuntimeDataError(
            "pit_runtime_window_invalid",
            "PIT 运行窗口无效",
        ) from exc
    if start > end:
        raise PitRuntimeDataError(
            "pit_runtime_window_invalid",
            "PIT 运行窗口起始日期不能晚于结束日期",
        )
    inspected = await inspect_cached_market_data(
        cache or DataCache(),
        cache_key=normalized_pool,
        pool_id=normalized_pool,
        requested_codes=requested_codes,
        required_start=start.strftime("%Y-%m-%d"),
        required_end=end.strftime("%Y-%m-%d"),
        point_in_time_store=point_in_time_store,
        price_ledger_store=price_ledger_store,
    )
    report = inspected.report
    report["required_point_in_time_benchmark"] = bool(require_benchmark)
    purpose_ready = {
        "research": bool(report.get("ready_for_unbiased_return_research")),
        "tuning": bool(report.get("ready_for_real_tuning")),
        "execution": bool(report.get("ready_for_execution_simulation")),
    }[purpose]
    if not (
        report.get("ready")
        and report.get("universe_point_in_time")
        and report.get("canonical_runtime_price_bound")
        and report.get("authoritative_trading_calendar_bound") is True
        and (
            not require_benchmark
            or report.get("point_in_time_benchmark_bound") is True
        )
        and purpose_ready
        and inspected.frame is not None
    ):
        code = _failure_code(report, purpose)
        raise PitRuntimeDataError(
            code,
            (
                "正式运行已启用 PIT-only 门禁：缺少已激活点时成分、精确双价格账本绑定、"
                "双时态可得性或用途级安全证据；不会回退到当前成分快照、隔离数据、"
                "旧 Parquet 或运行时联网抓取"
            ),
            report=report,
        )
    return PitRuntimeInput(
        pool_id=normalized_pool,
        purpose=purpose,
        market=inspected,
    )


async def require_pit_runtime_input(**kwargs: Any) -> PitRuntimeInput:
    return await inspect_pit_runtime_input(**kwargs)
