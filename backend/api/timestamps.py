"""Timestamp serialization helpers for the public API.

SQLite's ``datetime('now')`` stores UTC wall-clock values without an offset.
Those values must be interpreted as UTC at the API boundary, not as server or
browser local time.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Iterable, MutableMapping

UTC_TIMESTAMP_FIELDS = frozenset(
    {
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "submitted_at",
        "token_expires_at",
    }
)


def serialize_utc_timestamp(value: Any) -> str | None:
    """Return a timestamp as canonical RFC 3339 UTC.

    Naive values are the legacy SQLite representation and therefore mean UTC.
    Aware values are converted to UTC, preserving the represented instant.
    """

    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("timestamp must not be empty")
        try:
            parsed = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
            )
        except ValueError as exc:
            raise ValueError(f"invalid timestamp: {value!r}") from exc
    else:
        raise TypeError(f"unsupported timestamp type: {type(value).__name__}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def serialize_utc_timestamp_fields(
    payload: MutableMapping[str, Any],
    fields: Iterable[str] = UTC_TIMESTAMP_FIELDS,
) -> MutableMapping[str, Any]:
    """Normalize timestamp fields in a response mapping in place."""

    for field in fields:
        if field in payload and payload[field] is not None:
            payload[field] = serialize_utc_timestamp(payload[field])
    return payload
