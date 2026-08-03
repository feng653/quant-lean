"""统一时间工具（v0.5.0 去重：全项目唯一实现）。

约定：
- ``utc_now()`` 返回带 UTC 时区的 ``datetime``（调用方自行决定序列化格式）。
- ``to_iso_utc()`` 输出 API 统一的 UTC ISO-8601（``Z`` 结尾，微秒保留）。
- ``parse_iso_utc()`` 解析上述格式或普通 ISO 字符串。
"""

from __future__ import annotations

from datetime import datetime, timezone

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now() -> datetime:
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def to_iso_utc(value: datetime | None = None) -> str:
    """Serialize a datetime to UTC ISO-8601 with Z suffix.

    Naive datetimes are assumed to be UTC. Defaults to ``utc_now()``.
    """
    dt = value if value is not None else utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(_ISO_FMT)


def parse_iso_utc(value: str) -> datetime:
    """Parse a UTC ISO-8601 string (with or without Z / offset) to tz-aware UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
