"""Shared point-in-time universe resolution and masking.

Readiness, factor research and strategy execution must consume this same
resolver.  A verified batch range is not sufficient by itself: every requested
trading date needs one non-empty (and, for fixed-size indexes, complete)
membership set, and every historical member needs the required market-data
columns before any result may claim point-in-time semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

import logging
import pandas as pd

from backend.data.point_in_time_master import PointInTimeMasterStore
from backend.data.versioning import canonical_digest


TIMELINE_SCHEMA_VERSION = "point-in-time-universe-timeline/v1"
REQUIRED_MARKET_FIELDS = ("open", "high", "low", "close", "volume")


class PointInTimeUniverseError(RuntimeError):
    """A historical universe cannot be used without complete evidence."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def _trading_dates(
    values: Iterable[Any],
    *,
    start: str | None = None,
    end: str | None = None,
) -> pd.DatetimeIndex:
    try:
        dates = pd.DatetimeIndex(pd.to_datetime(list(values), errors="raise"))
    except (TypeError, ValueError) as exc:
        raise PointInTimeUniverseError(
            "point_in_time_trading_dates_invalid"
        ) from exc
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    dates = dates.normalize()
    if dates.has_duplicates:
        raise PointInTimeUniverseError("point_in_time_trading_dates_duplicate")
    dates = dates.sort_values()
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start).normalize()]
    if end is not None:
        dates = dates[dates <= pd.Timestamp(end).normalize()]
    if dates.empty:
        raise PointInTimeUniverseError("point_in_time_trading_dates_empty")
    return dates


def _batch_identity(batches: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    unique: dict[str, dict[str, Any]] = {}
    for batch in batches:
        digest = str(batch.get("batch_digest") or "")
        batch_id = str(batch.get("batch_id") or "")
        if not digest or not batch_id:
            raise PointInTimeUniverseError(
                "point_in_time_source_batch_identity_missing"
            )
        unique[batch_id] = {
            "batch_id": batch_id,
            "batch_digest": digest,
            "coverage_from": str(batch.get("coverage_from") or ""),
            "coverage_to": str(batch.get("coverage_to") or ""),
        }
    if not unique:
        raise PointInTimeUniverseError(
            "point_in_time_source_batch_identity_missing"
        )
    return tuple(unique[key] for key in sorted(unique))


@dataclass(frozen=True, slots=True)
class PointInTimeUniverseTimeline:
    """Verified membership for exactly the trading dates supplied by a caller."""

    pool_id: str
    dates: tuple[str, ...]
    members_by_date: tuple[tuple[str, ...], ...]
    union_codes: tuple[str, ...]
    source_batches: tuple[dict[str, Any], ...]
    timeline_hash: str
    coverage_from: str
    coverage_to: str
    expected_count: int | None = None
    parent_timeline_hash: str | None = None
    industry_filter: tuple[str, ...] = ()
    code_filter: tuple[str, ...] = ()
    industry_source_batches: tuple[dict[str, Any], ...] = ()
    schema_version: str = TIMELINE_SCHEMA_VERSION
    as_known_at: str | None = None
    bitemporal_availability_verified: bool = False

    def __post_init__(self) -> None:
        if len(self.dates) != len(self.members_by_date) or not self.dates:
            raise PointInTimeUniverseError(
                "point_in_time_timeline_shape_invalid"
            )
        if tuple(sorted(set(self.dates))) != self.dates:
            raise PointInTimeUniverseError(
                "point_in_time_timeline_dates_invalid"
            )
        if any(not members for members in self.members_by_date):
            raise PointInTimeUniverseError(
                "historical_membership_empty"
            )
        actual_union = tuple(
            sorted({code for members in self.members_by_date for code in members})
        )
        if actual_union != self.union_codes:
            raise PointInTimeUniverseError(
                "point_in_time_timeline_union_invalid"
            )

    def members_on(self, value: Any) -> tuple[str, ...]:
        day = pd.Timestamp(value).strftime("%Y-%m-%d")
        try:
            return self.members_by_date[self.dates.index(day)]
        except ValueError as exc:
            raise PointInTimeUniverseError(
                "point_in_time_trading_date_not_resolved"
            ) from exc

    def identity(self) -> dict[str, Any]:
        """Portable manifest identity with compressed exact-replay membership."""

        identity = {
            "schema_version": self.schema_version,
            "pool_id": self.pool_id,
            "coverage_from": self.coverage_from,
            "coverage_to": self.coverage_to,
            "trading_date_count": len(self.dates),
            "union_code_count": len(self.union_codes),
            "union_codes_sha256": canonical_digest(list(self.union_codes)),
            "member_count_min": min(map(len, self.members_by_date)),
            "member_count_max": max(map(len, self.members_by_date)),
            "expected_count": self.expected_count,
            "timeline_hash": self.timeline_hash,
            "membership_intervals": self._membership_intervals(),
            "source_batches": [dict(item) for item in self.source_batches],
            "parent_timeline_hash": self.parent_timeline_hash,
            "industry_filter": list(self.industry_filter),
            "code_filter": list(self.code_filter),
            "industry_source_batches": [
                dict(item) for item in self.industry_source_batches
            ],
        }
        if self.as_known_at is not None:
            identity["as_known_at"] = self.as_known_at
            identity["bitemporal_availability_verified"] = (
                self.bitemporal_availability_verified
            )
        return identity

    def _membership_intervals(self) -> list[dict[str, str]]:
        intervals: list[dict[str, str]] = []
        for code in self.union_codes:
            active_positions = [
                index
                for index, members in enumerate(self.members_by_date)
                if code in members
            ]
            if not active_positions:
                continue
            start_position = active_positions[0]
            previous = start_position
            for position in active_positions[1:]:
                if position != previous + 1:
                    intervals.append(
                        {
                            "security_code": code,
                            "trading_from": self.dates[start_position],
                            "trading_to": self.dates[previous],
                        }
                    )
                    start_position = position
                previous = position
            intervals.append(
                {
                    "security_code": code,
                    "trading_from": self.dates[start_position],
                    "trading_to": self.dates[previous],
                }
            )
        return intervals


def _timeline_hash(
    *,
    pool_id: str,
    dates: Sequence[str],
    members_by_date: Sequence[Sequence[str]],
    parent_timeline_hash: str | None = None,
    industry_filter: Sequence[str] = (),
) -> str:
    # Source batches are intentionally not part of this semantic hash.  A
    # later batch may add future intervals while the early observed membership
    # remains identical.  Batch digests are bound separately in identity().
    return canonical_digest(
        {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "pool_id": pool_id,
            "dates": [
                [day, list(members)]
                for day, members in zip(dates, members_by_date)
            ],
            "parent_timeline_hash": parent_timeline_hash,
            "industry_filter": list(industry_filter),
        }
    )


def resolve_point_in_time_universe(
    store: PointInTimeMasterStore,
    *,
    pool_id: str,
    trading_dates: Iterable[Any],
    expected_count: int | None = None,
    start: str | None = None,
    end: str | None = None,
    as_known_at: str | None = None,
) -> PointInTimeUniverseTimeline:
    """Resolve complete membership for every requested trading date."""

    dates = _trading_dates(trading_dates, start=start, end=end)
    date_text = tuple(day.strftime("%Y-%m-%d") for day in dates)
    result = store.query_effective_history(
        domain="index_membership",
        scope_id=pool_id,
        start=date_text[0],
        end=date_text[-1],
        as_known_at=as_known_at,
    )
    if not result.get("available"):
        raise PointInTimeUniverseError(
            str(result.get("reason") or "point_in_time_universe_missing")
        )
    intervals: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in result.get("records") or []:
        if not isinstance(record, Mapping):
            raise PointInTimeUniverseError(
                "point_in_time_membership_record_invalid"
            )
        identity = (
            str(record.get("security_code") or ""),
            str(record.get("effective_from") or ""),
            str(record.get("effective_to") or ""),
        )
        if not identity[0] or identity in seen:
            raise PointInTimeUniverseError(
                "point_in_time_membership_record_duplicate"
            )
        seen.add(identity)
        intervals.append(identity)
    members_by_date: list[tuple[str, ...]] = []
    for day in date_text:
        members = tuple(
            sorted(
                code
                for code, effective_from, effective_to in intervals
                if effective_from <= day <= effective_to
            )
        )
        if not members:
            raise PointInTimeUniverseError(
                "historical_membership_empty",
                f"historical membership is empty on {day}",
            )
        if expected_count is not None and len(members) != expected_count:
            raise PointInTimeUniverseError(
                "historical_membership_count_mismatch",
                f"expected {expected_count} members on {day}, got {len(members)}",
            )
        members_by_date.append(members)
    union_codes = tuple(
        sorted({code for members in members_by_date for code in members})
    )
    batches = _batch_identity(result.get("source_batches") or [])
    return PointInTimeUniverseTimeline(
        pool_id=str(pool_id),
        dates=date_text,
        members_by_date=tuple(members_by_date),
        union_codes=union_codes,
        source_batches=batches,
        timeline_hash=_timeline_hash(
            pool_id=str(pool_id),
            dates=date_text,
            members_by_date=members_by_date,
        ),
        coverage_from=date_text[0],
        coverage_to=date_text[-1],
        expected_count=expected_count,
        as_known_at=result.get("as_known_at"),
        bitemporal_availability_verified=bool(
            result.get("bitemporal_availability_verified")
        ),
    )


def validate_market_data_columns(
    frame: pd.DataFrame,
    timeline: PointInTimeUniverseTimeline,
    *,
    required_fields: Sequence[str] = REQUIRED_MARKET_FIELDS,
    strict: bool = True,
) -> None:
    if not isinstance(frame.columns, pd.MultiIndex):
        raise PointInTimeUniverseError(
            "point_in_time_market_data_schema_invalid"
        )
    fields_by_code: dict[str, set[str]] = {}
    for column in frame.columns:
        code = str(column[0]).strip()
        field = str(column[-1]).strip().lower()
        fields_by_code.setdefault(code, set()).add(field)
    missing_codes = sorted(set(timeline.union_codes) - set(fields_by_code))
    if missing_codes:
        if strict:
            raise PointInTimeUniverseError(
                "membership_price_coverage_missing",
                "historical member price columns missing: "
                + ",".join(missing_codes[:20]),
            )
        # 研究/模拟降级放行（PIT 分级门禁，与 cache_readiness strict=False 一致）：
        # 会员价格列缺失时仅告警，用可用子集运行；结果仅供研究参考。
        logging.getLogger("quant_platform").warning(
            "historical member price columns missing（降级放行，仅供研究参考）：%s",
            ",".join(missing_codes[:20]),
        )
    required = set(required_fields)
    missing_fields = {
        code: sorted(required - fields_by_code.get(code, set()))
        for code in timeline.union_codes
        if required - fields_by_code.get(code, set())
    }
    if missing_fields:
        if strict:
            raise PointInTimeUniverseError(
                "membership_price_fields_missing",
                "historical member OHLCV columns are incomplete",
            )
        # 研究/模拟降级放行：字段缺失时仅告警（缺口已在上方记录）
        logging.getLogger("quant_platform").warning(
            "historical member OHLCV fields incomplete（降级放行，仅供研究参考）"
        )


def mask_market_data_to_timeline(
    frame: pd.DataFrame,
    timeline: PointInTimeUniverseTimeline,
    *,
    required_fields: Sequence[str] = REQUIRED_MARKET_FIELDS,
) -> pd.DataFrame:
    """Drop never-members and mask every field outside dated membership."""

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise PointInTimeUniverseError(
            "point_in_time_market_data_index_invalid"
        )
    normalized_index = frame.index.tz_localize(None).normalize()
    if normalized_index.has_duplicates:
        raise PointInTimeUniverseError(
            "point_in_time_market_data_dates_duplicate"
        )
    expected_dates = tuple(day.strftime("%Y-%m-%d") for day in normalized_index)
    if expected_dates != timeline.dates:
        raise PointInTimeUniverseError(
            "point_in_time_market_data_dates_mismatch"
        )
    validate_market_data_columns(
        frame,
        timeline,
        required_fields=required_fields,
    )
    member_set_by_date = [
        set(members) for members in timeline.members_by_date
    ]
    kept_columns = [
        column
        for column in frame.columns
        if str(column[0]) in set(timeline.union_codes)
    ]
    masked = frame.loc[:, kept_columns].copy()
    for code in timeline.union_codes:
        inactive = [
            code not in members for members in member_set_by_date
        ]
        if not any(inactive):
            continue
        code_columns = [
            column for column in masked.columns if str(column[0]) == code
        ]
        masked.loc[masked.index[inactive], code_columns] = float("nan")
    return masked


def select_market_data_for_timeline(
    frame: pd.DataFrame,
    timeline: PointInTimeUniverseTimeline,
    *,
    required_fields: Sequence[str] = REQUIRED_MARKET_FIELDS,
    strict: bool = True,
) -> pd.DataFrame:
    """Keep historical feature prices but remove securities never in the window."""

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise PointInTimeUniverseError(
            "point_in_time_market_data_index_invalid"
        )
    normalized_index = frame.index.tz_localize(None).normalize()
    expected_dates = tuple(day.strftime("%Y-%m-%d") for day in normalized_index)
    if expected_dates != timeline.dates:
        raise PointInTimeUniverseError(
            "point_in_time_market_data_dates_mismatch"
        )
    validate_market_data_columns(
        frame,
        timeline,
        required_fields=required_fields,
        strict=strict,
    )
    allowed = set(timeline.union_codes)
    return frame.loc[
        :,
        [column for column in frame.columns if str(column[0]) in allowed],
    ].copy()


def eligibility_panel(
    timeline: PointInTimeUniverseTimeline,
) -> pd.DataFrame:
    """Return the canonical date × code membership mask."""

    values = [
        [code in set(members) for code in timeline.union_codes]
        for members in timeline.members_by_date
    ]
    return pd.DataFrame(
        values,
        index=pd.DatetimeIndex(timeline.dates, name="date"),
        columns=list(timeline.union_codes),
        dtype=bool,
    )


def origin_date_label_eligibility(
    eligibility: pd.DataFrame,
) -> pd.DataFrame:
    """Return the PIT sample universe at each label origin date.

    A fixed-horizon security return is defined for a security selected at
    origin ``t``.  Removing it because the *future* index committee later
    removes the security conditions today's sample on future information and
    creates a different selection bias.  Post-origin prices still require a
    complete, verified research ledger; this helper does not claim to model an
    index reconstitution trade or raw execution.
    """

    if (
        eligibility.empty
        or eligibility.index.has_duplicates
        or eligibility.columns.has_duplicates
        or not all(
            pd.api.types.is_bool_dtype(dtype)
            for dtype in eligibility.dtypes
        )
        or eligibility.isna().any().any()
    ):
        raise PointInTimeUniverseError(
            "point_in_time_eligibility_panel_invalid"
        )
    result = eligibility.copy()
    result.index = pd.DatetimeIndex(
        pd.to_datetime(result.index, errors="raise")
    )
    return result.sort_index()


def timeline_from_identity(
    identity: Mapping[str, Any],
    *,
    trading_dates: Iterable[Any],
) -> PointInTimeUniverseTimeline:
    """Reconstruct and verify exact membership from a run manifest."""

    if identity.get("schema_version") != TIMELINE_SCHEMA_VERSION:
        raise PointInTimeUniverseError(
            "point_in_time_timeline_identity_invalid"
        )
    dates = _trading_dates(trading_dates)
    date_text = tuple(day.strftime("%Y-%m-%d") for day in dates)
    if (
        identity.get("coverage_from") != date_text[0]
        or identity.get("coverage_to") != date_text[-1]
        or int(identity.get("trading_date_count") or 0) != len(date_text)
    ):
        raise PointInTimeUniverseError(
            "point_in_time_timeline_replay_dates_mismatch"
        )
    positions = {day: index for index, day in enumerate(date_text)}
    members: list[set[str]] = [set() for _day in date_text]
    intervals = identity.get("membership_intervals")
    if not isinstance(intervals, list) or not intervals:
        raise PointInTimeUniverseError(
            "point_in_time_timeline_replay_membership_missing"
        )
    for interval in intervals:
        if not isinstance(interval, Mapping):
            raise PointInTimeUniverseError(
                "point_in_time_timeline_identity_invalid"
            )
        code = str(interval.get("security_code") or "")
        start = positions.get(str(interval.get("trading_from") or ""))
        end = positions.get(str(interval.get("trading_to") or ""))
        if not code or start is None or end is None or start > end:
            raise PointInTimeUniverseError(
                "point_in_time_timeline_identity_invalid"
            )
        for position in range(start, end + 1):
            if code in members[position]:
                raise PointInTimeUniverseError(
                    "point_in_time_timeline_identity_invalid"
                )
            members[position].add(code)
    members_by_date = tuple(
        tuple(sorted(day_members)) for day_members in members
    )
    union_codes = tuple(
        sorted({code for day_members in members for code in day_members})
    )
    source_batches = _batch_identity(identity.get("source_batches") or [])
    industry_batches_raw = identity.get("industry_source_batches") or []
    industry_batches = (
        _batch_identity(industry_batches_raw)
        if industry_batches_raw
        else ()
    )
    expected_count = identity.get("expected_count")
    code_filter = tuple(
        str(item) for item in identity.get("code_filter") or []
    )
    hash_industry_filter = (
        ("codes:" + canonical_digest(list(code_filter)),)
        if code_filter
        else tuple(
            str(item) for item in identity.get("industry_filter") or []
        )
    )
    timeline = PointInTimeUniverseTimeline(
        pool_id=str(identity.get("pool_id") or ""),
        dates=date_text,
        members_by_date=members_by_date,
        union_codes=union_codes,
        source_batches=source_batches,
        timeline_hash=_timeline_hash(
            pool_id=str(identity.get("pool_id") or ""),
            dates=date_text,
            members_by_date=members_by_date,
            parent_timeline_hash=(
                str(identity["parent_timeline_hash"])
                if identity.get("parent_timeline_hash")
                else None
            ),
            industry_filter=hash_industry_filter,
        ),
        coverage_from=date_text[0],
        coverage_to=date_text[-1],
        expected_count=(
            int(expected_count) if expected_count is not None else None
        ),
        parent_timeline_hash=(
            str(identity["parent_timeline_hash"])
            if identity.get("parent_timeline_hash")
            else None
        ),
        industry_filter=tuple(
            str(item) for item in identity.get("industry_filter") or []
        ),
        code_filter=code_filter,
        industry_source_batches=industry_batches,
        as_known_at=(
            str(identity["as_known_at"])
            if identity.get("as_known_at") is not None
            else None
        ),
        bitemporal_availability_verified=bool(
            identity.get("bitemporal_availability_verified")
        ),
    )
    if (
        timeline.timeline_hash != identity.get("timeline_hash")
        or canonical_digest(list(timeline.union_codes))
        != identity.get("union_codes_sha256")
        or len(timeline.union_codes)
        != int(identity.get("union_code_count") or 0)
    ):
        raise PointInTimeUniverseError(
            "point_in_time_timeline_identity_hash_mismatch"
        )
    return timeline


def filter_timeline_by_industry(
    store: PointInTimeMasterStore,
    timeline: PointInTimeUniverseTimeline,
    industries: Sequence[str],
    *,
    scope_id: str = "cninfo_008001",
) -> PointInTimeUniverseTimeline:
    """Apply an effective-dated industry filter to each membership date."""

    selected = tuple(
        sorted({str(item).strip() for item in industries if str(item).strip()})
    )
    if not selected:
        return timeline
    result = store.query_effective_history(
        domain="industry",
        scope_id=scope_id,
        start=timeline.coverage_from,
        end=timeline.coverage_to,
    )
    if not result.get("available"):
        raise PointInTimeUniverseError(
            str(result.get("reason") or "industry_coverage_missing")
        )
    intervals: list[tuple[str, str, str, str, str]] = []
    for record in result.get("records") or []:
        if not isinstance(record, Mapping):
            continue
        attributes = record.get("attributes")
        if not isinstance(attributes, Mapping):
            continue
        intervals.append(
            (
                str(record.get("security_code") or ""),
                str(record.get("effective_from") or ""),
                str(record.get("effective_to") or ""),
                str(attributes.get("industry_code") or ""),
                str(attributes.get("industry_name") or ""),
            )
        )
    filtered: list[tuple[str, ...]] = []
    selected_set = set(selected)
    for day, members in zip(timeline.dates, timeline.members_by_date):
        active: dict[str, tuple[str, str]] = {
            code: (industry_code, industry_name)
            for code, effective_from, effective_to, industry_code, industry_name
            in intervals
            if effective_from <= day <= effective_to and code in set(members)
        }
        missing = sorted(set(members) - set(active))
        if missing:
            raise PointInTimeUniverseError(
                "industry_effective_period_missing",
                f"industry evidence is incomplete on {day}",
            )
        chosen = tuple(
            sorted(
                code
                for code in members
                if (
                    active[code][0] in selected_set
                    or active[code][1] in selected_set
                )
            )
        )
        if not chosen:
            raise PointInTimeUniverseError(
                "industry_filter_membership_empty",
                f"industry filter has no members on {day}",
            )
        filtered.append(chosen)
    union_codes = tuple(
        sorted({code for members in filtered for code in members})
    )
    return replace(
        timeline,
        members_by_date=tuple(filtered),
        union_codes=union_codes,
        timeline_hash=_timeline_hash(
            pool_id=timeline.pool_id,
            dates=timeline.dates,
            members_by_date=filtered,
            parent_timeline_hash=timeline.timeline_hash,
            industry_filter=selected,
        ),
        expected_count=None,
        parent_timeline_hash=timeline.timeline_hash,
        industry_filter=selected,
        industry_source_batches=_batch_identity(
            result.get("source_batches") or []
        ),
    )


def filter_timeline_codes(
    timeline: PointInTimeUniverseTimeline,
    codes: Sequence[str],
) -> PointInTimeUniverseTimeline:
    """Apply a pre-declared code subset without converting it to a snapshot."""

    selected = tuple(
        sorted({str(item).strip() for item in codes if str(item).strip()})
    )
    if not selected:
        return timeline
    allowed = set(selected)
    filtered = [
        tuple(code for code in members if code in allowed)
        for members in timeline.members_by_date
    ]
    if any(not members for members in filtered):
        raise PointInTimeUniverseError(
            "point_in_time_code_filter_empty",
            "selected codes leave at least one trading date without members",
        )
    union_codes = tuple(
        sorted({code for members in filtered for code in members})
    )
    return replace(
        timeline,
        members_by_date=tuple(filtered),
        union_codes=union_codes,
        timeline_hash=_timeline_hash(
            pool_id=timeline.pool_id,
            dates=timeline.dates,
            members_by_date=filtered,
            parent_timeline_hash=timeline.timeline_hash,
            industry_filter=("codes:" + canonical_digest(list(selected)),),
        ),
        expected_count=None,
        parent_timeline_hash=timeline.timeline_hash,
        code_filter=selected,
    )


def validate_signals_against_timeline(
    signals: Mapping[str, Sequence[Any]],
    timeline: PointInTimeUniverseTimeline,
) -> None:
    """Reject strategy output that tries to escape dated order eligibility."""

    members = {
        day: set(codes)
        for day, codes in zip(timeline.dates, timeline.members_by_date)
    }
    for raw_day, day_signals in signals.items():
        day = pd.Timestamp(raw_day).strftime("%Y-%m-%d")
        eligible = members.get(day)
        if eligible is None and day_signals:
            raise PointInTimeUniverseError(
                "signal_date_outside_point_in_time_timeline"
            )
        for signal in day_signals:
            code = str(getattr(signal, "code", "")).strip()
            action = str(getattr(signal, "action", "")).upper()
            if (
                not code
                or eligible is None
                or (action == "BUY" and code not in eligible)
            ):
                raise PointInTimeUniverseError(
                    "signal_security_not_point_in_time_eligible",
                    f"signal security {code or '<empty>'} is not eligible on {day}",
                )


def require_point_in_time_training_eligibility(
    *,
    trainable: bool,
    timeline: PointInTimeUniverseTimeline | None,
) -> None:
    """Block ML evidence until training-label eligibility is platform-owned."""

    if not trainable:
        return
    if timeline is None:
        raise PointInTimeUniverseError(
            "ml_point_in_time_universe_not_available",
            "ML research requires a platform-owned point-in-time universe",
        )
    raise PointInTimeUniverseError(
        "ml_point_in_time_label_eligibility_not_supported",
        "PIT ML research requires platform-owned sample and label masks",
    )
