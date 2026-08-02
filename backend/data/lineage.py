"""Immutable universe lineage and data-quality contracts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from backend.data.versioning import canonical_digest


UNIVERSE_SNAPSHOT_SCHEMA = "universe-snapshot/v1"

NON_POINT_IN_TIME = "non_point_in_time"
SURVIVORSHIP_BIAS = "survivorship_bias"
DUPLICATE_CODES = "duplicate_codes"
COUNT_MISMATCH = "count_mismatch"
MISSING_INDUSTRY_MAPPING = "missing_industry_mapping"
EMPTY_CODES = "empty_codes"
STATIC_UNIVERSE = "static_universe"


def _as_iso_date(value: str | date | datetime | pd.Timestamp | None) -> str | None:
    if value is None or value == "":
        return None
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("as-of date cannot be NaT")
    return timestamp.date().isoformat()


def _normalise_codes(codes: Iterable[Any]) -> tuple[list[str], int]:
    normalized: list[str] = []
    empty_count = 0
    for value in codes:
        if value is None:
            empty_count += 1
            continue
        code = str(value).strip()
        if not code:
            empty_count += 1
            continue
        normalized.append(code)
    return normalized, empty_count


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    """Machine-readable quality summary for one requested universe."""

    requested_count: int
    valid_requested_count: int
    unique_count: int
    duplicate_count: int
    duplicate_codes: tuple[str, ...]
    empty_code_count: int
    expected_count: int | None
    count_difference: int | None
    industry_mapping_provided: bool
    industry_mapped_count: int
    missing_industry_count: int
    missing_industry_codes: tuple[str, ...]
    issue_codes: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.issue_codes

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_count": self.requested_count,
            "valid_requested_count": self.valid_requested_count,
            "unique_count": self.unique_count,
            "duplicate_count": self.duplicate_count,
            "duplicate_codes": list(self.duplicate_codes),
            "empty_code_count": self.empty_code_count,
            "expected_count": self.expected_count,
            "count_difference": self.count_difference,
            "industry_mapping_provided": self.industry_mapping_provided,
            "industry_mapped_count": self.industry_mapped_count,
            "missing_industry_count": self.missing_industry_count,
            "missing_industry_codes": list(self.missing_industry_codes),
            "issue_codes": list(self.issue_codes),
            "is_clean": self.is_clean,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DataQualityReport":
        return cls(
            requested_count=int(payload["requested_count"]),
            valid_requested_count=int(
                payload.get("valid_requested_count", payload["requested_count"])
            ),
            unique_count=int(payload["unique_count"]),
            duplicate_count=int(payload.get("duplicate_count", 0)),
            duplicate_codes=tuple(
                sorted(str(code) for code in payload.get("duplicate_codes", ()))
            ),
            empty_code_count=int(payload.get("empty_code_count", 0)),
            expected_count=(
                int(payload["expected_count"])
                if payload.get("expected_count") is not None
                else None
            ),
            count_difference=(
                int(payload["count_difference"])
                if payload.get("count_difference") is not None
                else None
            ),
            industry_mapping_provided=bool(
                payload.get("industry_mapping_provided", False)
            ),
            industry_mapped_count=int(payload.get("industry_mapped_count", 0)),
            missing_industry_count=int(payload.get("missing_industry_count", 0)),
            missing_industry_codes=tuple(
                sorted(
                    str(code)
                    for code in payload.get("missing_industry_codes", ())
                )
            ),
            issue_codes=tuple(
                sorted(str(code) for code in payload.get("issue_codes", ()))
            ),
        )


def build_data_quality_report(
    requested_codes: Sequence[Any],
    *,
    source_requested_count: int | None = None,
    expected_count: int | None = None,
    industry_map: Mapping[str, str] | None = None,
) -> DataQualityReport:
    """Measure duplicates, count mismatch and industry-map coverage."""
    if expected_count is not None and expected_count < 0:
        raise ValueError("expected_count must be non-negative")
    if (
        source_requested_count is not None
        and source_requested_count < len(requested_codes)
    ):
        raise ValueError(
            "source_requested_count cannot be smaller than cached code entries"
        )

    normalized, empty_count = _normalise_codes(requested_codes)
    requested_count = (
        source_requested_count
        if source_requested_count is not None
        else len(requested_codes)
    )
    valid_requested_count = requested_count - empty_count
    counts = Counter(normalized)
    unique_codes = tuple(sorted(counts))
    duplicate_codes = tuple(sorted(code for code, count in counts.items() if count > 1))
    duplicate_count = valid_requested_count - len(unique_codes)
    difference = (
        len(unique_codes) - expected_count
        if expected_count is not None
        else None
    )

    mapping_provided = industry_map is not None
    normalized_map = {
        str(code).strip(): str(industry).strip()
        for code, industry in (industry_map or {}).items()
        if str(code).strip() and str(industry).strip()
    }
    missing_industry = (
        tuple(code for code in unique_codes if code not in normalized_map)
        if mapping_provided
        else ()
    )
    mapped_count = (
        len(unique_codes) - len(missing_industry) if mapping_provided else 0
    )

    issues: set[str] = set()
    if duplicate_count:
        issues.add(DUPLICATE_CODES)
    if empty_count:
        issues.add(EMPTY_CODES)
    if difference not in (None, 0):
        issues.add(COUNT_MISMATCH)
    if missing_industry:
        issues.add(MISSING_INDUSTRY_MAPPING)

    return DataQualityReport(
        requested_count=requested_count,
        valid_requested_count=valid_requested_count,
        unique_count=len(unique_codes),
        duplicate_count=duplicate_count,
        duplicate_codes=duplicate_codes,
        empty_code_count=empty_count,
        expected_count=expected_count,
        count_difference=difference,
        industry_mapping_provided=mapping_provided,
        industry_mapped_count=mapped_count,
        missing_industry_count=len(missing_industry),
        missing_industry_codes=missing_industry,
        issue_codes=tuple(sorted(issues)),
    )


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    """Immutable point-in-time claim and normalized universe membership."""

    pool_id: str
    requested_as_of: str | None
    source_as_of: str | None
    point_in_time: bool
    requested_count: int
    unique_count: int
    codes: tuple[str, ...]
    risk_warnings: tuple[str, ...]
    quality: DataQualityReport
    snapshot_hash: str
    timeline_identity: Mapping[str, Any] | None = None
    schema_version: str = UNIVERSE_SNAPSHOT_SCHEMA

    def _hash_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "pool_id": self.pool_id,
            "requested_as_of": self.requested_as_of,
            "source_as_of": self.source_as_of,
            "point_in_time": self.point_in_time,
            "requested_count": self.requested_count,
            "unique_count": self.unique_count,
            "codes": list(self.codes),
            "risk_warnings": list(self.risk_warnings),
            "quality": self.quality.to_dict(),
        }
        if self.timeline_identity is not None:
            payload["timeline_identity"] = dict(self.timeline_identity)
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "snapshot_hash": self.snapshot_hash}

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        verify_hash: bool = True,
    ) -> "UniverseSnapshot":
        schema_version = str(
            payload.get("schema_version", UNIVERSE_SNAPSHOT_SCHEMA)
        )
        if schema_version != UNIVERSE_SNAPSHOT_SCHEMA:
            raise ValueError(f"Unsupported universe snapshot schema: {schema_version}")
        quality = DataQualityReport.from_dict(payload["quality"])
        snapshot = cls(
            pool_id=str(payload.get("pool_id", "")),
            requested_as_of=_as_iso_date(payload.get("requested_as_of")),
            source_as_of=_as_iso_date(payload.get("source_as_of")),
            point_in_time=bool(payload.get("point_in_time", False)),
            requested_count=int(payload["requested_count"]),
            unique_count=int(payload["unique_count"]),
            codes=tuple(sorted(str(code) for code in payload.get("codes", ()))),
            risk_warnings=tuple(
                sorted(str(item) for item in payload.get("risk_warnings", ()))
            ),
            quality=quality,
            snapshot_hash=str(payload.get("snapshot_hash", "")),
            timeline_identity=(
                dict(payload["timeline_identity"])
                if isinstance(payload.get("timeline_identity"), Mapping)
                else None
            ),
            schema_version=schema_version,
        )
        expected = canonical_digest(snapshot._hash_payload())
        if verify_hash and snapshot.snapshot_hash != expected:
            raise ValueError("Universe snapshot hash verification failed")
        if snapshot.unique_count != len(snapshot.codes):
            raise ValueError("Universe snapshot unique_count does not match codes")
        if snapshot.requested_count != snapshot.quality.requested_count:
            raise ValueError(
                "Universe snapshot requested_count does not match quality report"
            )
        if snapshot.unique_count != snapshot.quality.unique_count:
            raise ValueError(
                "Universe snapshot unique_count does not match quality report"
            )
        return snapshot


def build_universe_snapshot(
    pool_id: str,
    requested_codes: Sequence[Any],
    *,
    requested_as_of: str | date | datetime | pd.Timestamp | None = None,
    source_as_of: str | date | datetime | pd.Timestamp | None = None,
    point_in_time: bool,
    source_requested_count: int | None = None,
    expected_count: int | None = None,
    industry_map: Mapping[str, str] | None = None,
    risk_warnings: Iterable[str] = (),
    timeline_identity: Mapping[str, Any] | None = None,
) -> UniverseSnapshot:
    """Build a normalized snapshot and make time-travel risks explicit."""
    normalized, _ = _normalise_codes(requested_codes)
    codes = tuple(sorted(set(normalized)))
    requested_date = _as_iso_date(requested_as_of)
    source_date = _as_iso_date(source_as_of)
    quality = build_data_quality_report(
        requested_codes,
        source_requested_count=source_requested_count,
        expected_count=expected_count,
        industry_map=industry_map,
    )

    risks = {str(item).strip() for item in risk_warnings if str(item).strip()}
    risks.update(quality.issue_codes)
    if not point_in_time:
        risks.add(NON_POINT_IN_TIME)
        if requested_date is not None:
            risks.add(SURVIVORSHIP_BIAS)
    elif requested_date and source_date and source_date > requested_date:
        raise ValueError(
            "point_in_time=True cannot use a source_as_of after requested_as_of"
        )
    if point_in_time and timeline_identity is None:
        raise ValueError(
            "point_in_time=True requires immutable timeline identity"
        )
    if not point_in_time and timeline_identity is not None:
        raise ValueError(
            "non-point-in-time snapshots cannot claim timeline identity"
        )

    without_hash = UniverseSnapshot(
        pool_id=str(pool_id).strip(),
        requested_as_of=requested_date,
        source_as_of=source_date,
        point_in_time=bool(point_in_time),
        requested_count=quality.requested_count,
        unique_count=len(codes),
        codes=codes,
        risk_warnings=tuple(sorted(risks)),
        quality=quality,
        snapshot_hash="",
        timeline_identity=(
            dict(timeline_identity)
            if timeline_identity is not None
            else None
        ),
    )
    return UniverseSnapshot(
        pool_id=without_hash.pool_id,
        requested_as_of=without_hash.requested_as_of,
        source_as_of=without_hash.source_as_of,
        point_in_time=without_hash.point_in_time,
        requested_count=without_hash.requested_count,
        unique_count=without_hash.unique_count,
        codes=without_hash.codes,
        risk_warnings=without_hash.risk_warnings,
        quality=without_hash.quality,
        snapshot_hash=canonical_digest(without_hash._hash_payload()),
        timeline_identity=without_hash.timeline_identity,
    )


def research_risk_warnings(
    pool_id: str,
    test_start: str | date | datetime | pd.Timestamp | None,
    snapshot: UniverseSnapshot | None = None,
) -> tuple[str, ...]:
    """Return conservative, reusable universe risks for a research run.

    Current preset-index membership is not historical membership.  Until a
    source supplies dated constituents, any dated research request for these
    pools must carry both non-PIT and survivorship-bias warnings.
    """
    normalized_pool = str(pool_id).strip().lower()
    risks = set(snapshot.risk_warnings if snapshot is not None else ())
    if snapshot is not None and snapshot.pool_id.lower() != normalized_pool:
        raise ValueError("snapshot pool_id does not match requested pool_id")

    if test_start not in (None, ""):
        _as_iso_date(test_start)
        if (
            normalized_pool in {"csi300", "csi500", "csi800", "csi1000"}
            and (snapshot is None or snapshot.point_in_time is not True)
        ):
            risks.update({NON_POINT_IN_TIME, SURVIVORSHIP_BIAS})
    return tuple(sorted(risks))
