"""v0.5.0 去重：统一时间工具的单测与语义锁定。"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.core.timeutils import parse_iso_utc, to_iso_utc, utc_now


class TestUtcNow:
    def test_tz_aware_utc(self):
        now = utc_now()
        assert now.tzinfo is not None
        assert now.utcoffset().total_seconds() == 0


class TestToIsoUtc:
    def test_aware_input(self):
        dt = datetime(2026, 8, 3, 12, 30, 45, 123456, tzinfo=timezone.utc)
        assert to_iso_utc(dt) == "2026-08-03T12:30:45.123456Z"

    def test_naive_input_assumed_utc(self):
        dt = datetime(2026, 8, 3, 12, 30, 45)
        assert to_iso_utc(dt) == "2026-08-03T12:30:45.000000Z"

    def test_offset_normalised_to_utc(self):
        dt = datetime(2026, 8, 3, 20, 30, 45, tzinfo=timezone.utc)
        assert to_iso_utc(dt).endswith("Z")

    def test_default_is_now_utc(self):
        assert to_iso_utc().endswith("Z")


class TestParseIsoUtc:
    def test_roundtrip(self):
        value = to_iso_utc()
        parsed = parse_iso_utc(value)
        assert to_iso_utc(parsed) == value

    def test_z_suffix(self):
        parsed = parse_iso_utc("2026-08-03T12:30:45.123456Z")
        assert parsed == datetime(2026, 8, 3, 12, 30, 45, 123456, tzinfo=timezone.utc)

    def test_offset_form(self):
        parsed = parse_iso_utc("2026-08-03T20:30:45+08:00")
        assert parsed.hour == 12
        assert parsed.tzinfo is timezone.utc
