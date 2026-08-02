"""Platform-owned research eligibility context for strategy calculations.

The market-data frame deliberately keeps pre-entry prices: those observations
were already public and are valid for a security's trailing time-series
features.  Membership is therefore supplied as a separate dated context and
must be applied only where a strategy forms a cross-section, changes state, or
allocates portfolio weight.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping, Sequence

import pandas as pd


class StrategyResearchContextError(ValueError):
    """A strategy cannot safely execute under the supplied research context."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        super().__init__(detail or reason)


@dataclass(frozen=True)
class StrategyResearchContext:
    """Immutable point-in-time inputs owned and bound by the platform."""

    point_in_time: bool
    eligible_codes_by_date: Mapping[str, frozenset[str]]
    timeline_hash: str | None
    price_role: str
    training_sample_eligibility_owned: bool = False
    training_label_eligibility_owned: bool = False

    @classmethod
    def point_in_time_universe(
        cls,
        *,
        dates: Sequence[str],
        members_by_date: Sequence[Sequence[str]],
        timeline_hash: str,
        price_role: str,
    ) -> "StrategyResearchContext":
        if len(dates) != len(members_by_date) or not dates:
            raise StrategyResearchContextError(
                "strategy_point_in_time_context_invalid"
            )
        normalized: dict[str, frozenset[str]] = {}
        for raw_day, raw_members in zip(dates, members_by_date):
            day = pd.Timestamp(raw_day).strftime("%Y-%m-%d")
            members = frozenset(
                str(code).strip() for code in raw_members if str(code).strip()
            )
            if day in normalized or not members:
                raise StrategyResearchContextError(
                    "strategy_point_in_time_context_invalid"
                )
            normalized[day] = members
        if not timeline_hash:
            raise StrategyResearchContextError(
                "strategy_point_in_time_context_invalid"
            )
        return cls(
            point_in_time=True,
            eligible_codes_by_date=MappingProxyType(normalized),
            timeline_hash=str(timeline_hash),
            price_role=str(price_role),
        )

    def members_on(self, day: object) -> frozenset[str]:
        normalized = pd.Timestamp(day).strftime("%Y-%m-%d")
        members = self.eligible_codes_by_date.get(normalized)
        if members is None:
            raise StrategyResearchContextError(
                "strategy_point_in_time_date_missing",
                f"strategy eligibility is missing for {normalized}",
            )
        return members


_ACTIVE_CONTEXT: ContextVar[StrategyResearchContext | None] = ContextVar(
    "quant_platform_strategy_research_context",
    default=None,
)


@contextmanager
def activate_research_context(
    context: StrategyResearchContext | None,
) -> Iterator[None]:
    """Bind a context to one strategy call without leaking across jobs."""

    token: Token[StrategyResearchContext | None] = _ACTIVE_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def active_research_context() -> StrategyResearchContext | None:
    return _ACTIVE_CONTEXT.get()


def eligible_codes_on(day: object) -> frozenset[str] | None:
    """Return dated membership, or ``None`` for conditional static research."""

    context = active_research_context()
    if context is None or not context.point_in_time:
        return None
    return context.members_on(day)


def code_is_eligible(code: object, day: object) -> bool:
    members = eligible_codes_on(day)
    return members is None or str(code) in members


def mask_cross_section(frame: pd.DataFrame) -> pd.DataFrame:
    """Mask each row to that date's members while retaining the input tape."""

    context = active_research_context()
    if context is None or not context.point_in_time:
        return frame
    result = frame.copy()
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index, errors="raise"))
    for day in result.index:
        members = context.members_on(day)
        excluded = [str(code) for code in result.columns if str(code) not in members]
        if excluded:
            result.loc[day, excluded] = float("nan")
    return result


def validate_strategy_research_context(
    *,
    requires_training: bool,
    trainable_protocol: bool,
    context: StrategyResearchContext | None,
    point_in_time_capability: str | None = None,
) -> None:
    """Fail closed for every platform-trainable strategy until both masks exist."""

    if trainable_protocol or requires_training:
        if context is None or not context.point_in_time:
            raise StrategyResearchContextError(
                "ml_point_in_time_universe_not_available",
                "training research requires a platform-owned point-in-time universe",
            )
        if not (
            context.training_sample_eligibility_owned
            and context.training_label_eligibility_owned
        ):
            raise StrategyResearchContextError(
                "ml_point_in_time_label_eligibility_not_supported",
                "training research requires platform-owned sample and label masks",
            )
        return
    if (
        context is not None
        and context.point_in_time
        and point_in_time_capability not in {
            "dated_signal_state",
            "dated_cross_section",
            "dated_portfolio_allocation",
            "dated_composite",
        }
    ):
        raise StrategyResearchContextError(
            "strategy_point_in_time_context_not_supported",
            "strategy has not declared a reviewed point-in-time calculation contract",
        )
