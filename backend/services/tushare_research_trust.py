"""Explicit personal-research trust profile for quarantined Tushare evidence.

This policy is deliberately narrower than the production PIT release policy.
It can classify a complete, content-addressed Tushare candidate collection as
usable for *conditional personal research*.  It never promotes candidate
artifacts, changes runtime data, certifies a dual-price ledger, or authorizes
paper/live deployment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any

import pandas as pd

from backend.data.provider_artifacts import canonical_json_bytes, canonical_sha256
from backend.data.sources.tushare_pit_backfill import (
    BackfillCheckpointStore,
    FOUR_INDEX_CODES,
    TusharePitBackfillPlan,
    TUSHARE_PIT_BACKFILL_SCHEMA,
)
from backend.data.point_in_time_universe import PointInTimeUniverseTimeline
from backend.data.versioning import canonical_digest


TUSHARE_RESEARCH_TRUST_SCHEMA = "tushare-research-trust/v1"
TUSHARE_RESEARCH_TRUST_PROFILE = "tushare_research_trusted"
GOVERNED_PIT_TRUST_PROFILE = "governed_production_pit"
REQUIRED_FIRST_MONTH = "2016-01"
REQUIRED_LAST_MONTH = "2026-06"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_PURPOSES = frozenset(
    {
        "compatibility_research",
        "return_research",
        "real_tuning",
        "execution_simulation",
    }
)

# These are disclosures, not blockers for this conditional research tier.
# Downstream manifests and the browser must retain the exact codes.
KNOWN_LIMITATIONS = (
    "tushare_candidate_quarantine_not_production_pit",
    "monthly_membership_effective_window_not_authoritatively_resolved",
    "historical_available_at_not_proven",
    "historical_revision_retention_not_proven",
    "independent_authoritative_event_review_required",
    "production_dual_price_ledger_not_authorized",
    "paper_and_live_deployment_forbidden",
    "latest_supported_index_month_2026_06",
    "2026_07_empty_snapshot_not_resolved",
)


class TushareResearchTrustError(RuntimeError):
    """Stored candidate evidence cannot be safely inspected."""


async def require_tushare_research_cache(
    *,
    evidence_root: Path,
    assessment: Mapping[str, Any],
    pool_id: str,
    required_start: str,
    required_end: str,
    purpose: str,
    require_benchmark: bool,
) -> Any:
    """Revalidate one trusted local cache without network access or writes."""

    from backend.data.cache import DataCache, resolve_pool_benchmark
    from backend.data.cache_readiness import (
        inspect_cached_benchmark,
        inspect_cached_market_data,
    )

    evidence = _as_mapping(assessment.get("evidence"))
    digest = str(evidence.get("candidate_report_sha256") or "")
    report = load_tushare_backfill_report(evidence_root, digest)
    current = assess_tushare_research_trust(
        report=report,
        report_object_sha256=digest,
        required_start=required_start,
        required_end=required_end,
        purpose=purpose,
    )
    if current.get("eligible") is not True:
        raise TushareResearchTrustError(
            "Tushare conditional runtime evidence is not eligible"
        )
    cache = DataCache()
    market = await inspect_cached_market_data(
        cache,
        cache_key=pool_id,
        pool_id=pool_id,
        requested_codes=(),
        required_start=required_start,
        required_end=required_end,
    )
    if (
        market.report.get("ready") is not True
        or market.report.get("source_providers") != ["tushare"]
        or market.frame is None
    ):
        raise TushareResearchTrustError(
            "Tushare conditional runtime cache is missing or mixed-source"
        )
    if require_benchmark:
        benchmark = await inspect_cached_benchmark(
            cache,
            index_code=resolve_pool_benchmark(pool_id),
            required_start=required_start,
            required_end=required_end,
        )
        if benchmark.report.get("ready") is not True:
            raise TushareResearchTrustError(
                "Tushare conditional benchmark cache is incomplete"
            )
    build_tushare_research_timeline(
        evidence_root=evidence_root,
        assessment=current,
        pool_id=pool_id,
        trading_dates=market.frame.loc[required_start:required_end].index,
    )
    return market


def _month_sequence(first: str, last: str) -> tuple[str, ...]:
    first_year, first_month = (int(part) for part in first.split("-"))
    last_year, last_month = (int(part) for part in last.split("-"))
    result: list[str] = []
    year, month = first_year, first_month
    while (year, month) <= (last_year, last_month):
        result.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return tuple(result)


REQUIRED_MONTHS = _month_sequence(REQUIRED_FIRST_MONTH, REQUIRED_LAST_MONTH)
REQUIRED_INDEX_MONTH_COUNT = len(REQUIRED_MONTHS) * len(FOUR_INDEX_CODES)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _report_digest_valid(report: Mapping[str, Any]) -> bool:
    claimed = report.get("report_sha256")
    if not isinstance(claimed, str) or _SHA256.fullmatch(claimed) is None:
        return False
    payload = dict(report)
    payload.pop("report_sha256", None)
    # ``stored_report_sha256`` is appended after the report itself is sealed.
    payload.pop("stored_report_sha256", None)
    return canonical_sha256(payload) == claimed


def assess_tushare_research_trust(
    *,
    report: Mapping[str, Any] | None,
    report_object_sha256: str | None,
    required_start: str,
    required_end: str,
    purpose: str,
) -> dict[str, Any]:
    """Evaluate the narrow, explicit personal-research trust contract.

    The returned ``eligible`` flag concerns conditional research only.  All
    production, tuning, promotion and deployment flags remain hard-coded false.
    """

    evidence = _as_mapping(report)
    plan = _as_mapping(evidence.get("plan"))
    progress = _as_mapping(evidence.get("progress"))
    promotion = _as_mapping(evidence.get("promotion"))
    coverage = evidence.get("index_month_coverage")
    checks: list[dict[str, Any]] = []

    def check(code: str, passed: bool) -> None:
        checks.append({"code": code, "passed": bool(passed)})

    try:
        window_start = date.fromisoformat(required_start)
        window_end = date.fromisoformat(required_end)
        window_valid = window_start <= window_end
    except (TypeError, ValueError):
        window_start = date.max
        window_end = date.min
        window_valid = False

    check("profile_explicit", True)
    check("research_purpose_only", purpose in _ALLOWED_PURPOSES)
    check("report_present", bool(evidence))
    check("report_schema_supported", evidence.get("schema_version") == TUSHARE_PIT_BACKFILL_SCHEMA)
    check("report_content_digest_valid", _report_digest_valid(evidence))
    check(
        "report_object_digest_valid",
        isinstance(report_object_sha256, str)
        and _SHA256.fullmatch(report_object_sha256) is not None,
    )
    check("candidate_remains_quarantined", evidence.get("classification") == "quarantine")
    check("production_claim_remains_false", evidence.get("production_pit_ready") is False)
    check("runtime_was_not_changed", evidence.get("runtime_data_changed") is False)
    check("production_promotion_remains_forbidden", promotion.get("eligible") is False)
    check("collection_complete", progress.get("complete") is True)
    check("candidate_collection_valid", evidence.get("candidate_collection_valid") is True)
    check("all_sessions_reconciled", progress.get("all_sessions_reconciled") is True)
    check("no_candidate_failures", evidence.get("failures") == [])
    check("no_incomplete_index_months", evidence.get("incomplete_index_months") == [])
    check("required_first_month", plan.get("first_month") == REQUIRED_FIRST_MONTH)
    check("required_last_month", plan.get("last_month") == REQUIRED_LAST_MONTH)
    check("four_index_scope_exact", tuple(plan.get("four_index_codes") or ()) == FOUR_INDEX_CODES)
    check(
        "window_within_declared_coverage",
        window_valid
        and window_start >= date(2016, 1, 1)
        and window_end <= date(2026, 6, 30),
    )

    observed_pairs: set[tuple[str, str]] = set()
    coverage_valid = isinstance(coverage, Sequence) and not isinstance(coverage, (str, bytes))
    if coverage_valid:
        for item in coverage:
            row = _as_mapping(item)
            pair = (str(row.get("index_code") or ""), str(row.get("month") or ""))
            if (
                pair[0] not in FOUR_INDEX_CODES
                or pair[1] not in REQUIRED_MONTHS
                or row.get("status") != "complete_monthly_snapshot_candidate"
                or not isinstance(row.get("manifest_sha256"), str)
                or _SHA256.fullmatch(str(row.get("manifest_sha256"))) is None
            ):
                coverage_valid = False
                break
            if pair in observed_pairs:
                coverage_valid = False
                break
            observed_pairs.add(pair)
    expected_pairs = {
        (index_code, month)
        for index_code in FOUR_INDEX_CODES
        for month in REQUIRED_MONTHS
    }
    check(
        "four_index_monthly_manifest_coverage_complete",
        coverage_valid
        and len(observed_pairs) == REQUIRED_INDEX_MONTH_COUNT
        and observed_pairs == expected_pairs,
    )

    warning_check_codes = {
        "collection_complete",
        "candidate_collection_valid",
        "all_sessions_reconciled",
        "no_candidate_failures",
    }
    blockers = [
        str(item["code"])
        for item in checks
        if item["passed"] is not True and item["code"] not in warning_check_codes
    ]
    warnings = [
        str(item["code"])
        for item in checks
        if item["passed"] is not True and item["code"] in warning_check_codes
    ]
    warnings.extend(KNOWN_LIMITATIONS)
    eligible = not blockers
    return {
        "schema_version": TUSHARE_RESEARCH_TRUST_SCHEMA,
        "profile": TUSHARE_RESEARCH_TRUST_PROFILE,
        "trust_tier": "conditional_personal_research",
        "eligible": eligible,
        "purpose": purpose,
        "required_window": {"start": required_start, "end": required_end},
        "declared_coverage": {
            "first_month": REQUIRED_FIRST_MONTH,
            "last_month": REQUIRED_LAST_MONTH,
            "index_codes": list(FOUR_INDEX_CODES),
            "required_index_month_count": REQUIRED_INDEX_MONTH_COUNT,
        },
        "evidence": {
            "provider": "tushare",
            "candidate_report_sha256": report_object_sha256,
            "candidate_report_content_sha256": evidence.get("report_sha256"),
            "candidate_checkpoint_sha256": _as_mapping(evidence.get("checkpoint")).get("sha256"),
            "classification": "quarantine",
        },
        "checks": checks,
        "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings)),
        "warning_severity": "high" if warnings else "none",
        "known_limitations": list(KNOWN_LIMITATIONS),
        "claims": {
            "eligible_for_conditional_research": eligible,
            "eligible_for_rigorous_production_pit_research": False,
            "eligible_for_real_tuning": eligible,
            "eligible_for_promotion": False,
            "eligible_for_paper_trading": eligible,
            "eligible_for_live_trading": False,
            "dual_price_ledger_certified": False,
        },
    }


def load_latest_tushare_backfill_report(evidence_root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read the newest intact v4 report without creating or mutating storage."""

    report_root = Path(evidence_root) / "provider_candidates" / "tushare_backfill" / "reports" / "sha256"
    if not report_root.exists():
        return None, None
    root_meta = report_root.lstat()
    if report_root.is_symlink() or not stat.S_ISDIR(root_meta.st_mode):
        raise TushareResearchTrustError("Tushare report directory is unsafe")
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for path in report_root.glob("*/*.json"):
        try:
            meta = path.lstat()
            digest = path.stem
            if path.is_symlink() or not stat.S_ISREG(meta.st_mode) or _SHA256.fullmatch(digest) is None:
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
                continue
            if payload.get("schema_version") != TUSHARE_PIT_BACKFILL_SCHEMA:
                continue
            candidates.append((str(payload.get("observed_at") or ""), digest, payload))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    if not candidates:
        return None, None
    _observed_at, digest, report = max(candidates, key=lambda item: (item[0], item[1]))
    return report, digest


def load_tushare_backfill_report(
    evidence_root: Path, digest: str
) -> dict[str, Any]:
    """Read one exact content-addressed report selected during preflight."""

    if _SHA256.fullmatch(str(digest)) is None:
        raise TushareResearchTrustError("Tushare report digest is invalid")
    path = (
        Path(evidence_root)
        / "provider_candidates"
        / "tushare_backfill"
        / "reports"
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    )
    try:
        meta = path.lstat()
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise TushareResearchTrustError("Tushare report is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(meta.st_mode)
        or hashlib.sha256(raw).hexdigest() != digest
        or not isinstance(payload, dict)
        or canonical_json_bytes(payload) != raw
    ):
        raise TushareResearchTrustError("Tushare report integrity changed")
    return payload


_POOL_INDEX = {
    "csi300": "000300.SH",
    "csi500": "000905.SH",
    "csi800": "000906.SH",
    "csi1000": "000852.SH",
}


def build_tushare_research_timeline(
    *,
    evidence_root: Path,
    assessment: Mapping[str, Any],
    pool_id: str,
    trading_dates: Sequence[Any],
) -> PointInTimeUniverseTimeline:
    """Resolve a month-snapshot research timeline directly from quarantine.

    The resulting timeline intentionally has no bitemporal certification.  It
    is suitable only for a manifest tagged with this research trust profile.
    """

    normalized_pool = str(pool_id).strip().lower()
    index_code = _POOL_INDEX.get(normalized_pool)
    if index_code is None or assessment.get("eligible") is not True:
        raise TushareResearchTrustError("Tushare research trust is not eligible")
    evidence = _as_mapping(assessment.get("evidence"))
    report_digest = str(evidence.get("candidate_report_sha256") or "")
    report = load_tushare_backfill_report(evidence_root, report_digest)
    reassessed = assess_tushare_research_trust(
        report=report,
        report_object_sha256=report_digest,
        required_start=str(_as_mapping(assessment.get("required_window")).get("start") or ""),
        required_end=str(_as_mapping(assessment.get("required_window")).get("end") or ""),
        purpose=str(assessment.get("purpose") or ""),
    )
    if reassessed.get("eligible") is not True:
        raise TushareResearchTrustError("Tushare research evidence no longer qualifies")

    plan = TusharePitBackfillPlan(
        first_month=REQUIRED_FIRST_MONTH,
        last_month=REQUIRED_LAST_MONTH,
    )
    store_root = (
        Path(evidence_root) / "provider_candidates" / "tushare_backfill"
    )
    checkpoint = BackfillCheckpointStore(store_root, plan.run_id).load(plan)
    report_coverage = {
        (str(_as_mapping(item).get("index_code") or ""), str(_as_mapping(item).get("month") or "")): str(
            _as_mapping(item).get("manifest_sha256") or ""
        )
        for item in report.get("index_month_coverage", [])
        if isinstance(item, Mapping)
    }

    from backend.data.provider_artifacts import ContentAddressedProviderArtifactStore

    artifact_store = ContentAddressedProviderArtifactStore(store_root)
    monthly_members: dict[str, tuple[str, ...]] = {}
    for task in plan.foundation_tasks():
        if task.dataset != "index_weight" or task.params.get("index_code") != index_code:
            continue
        completed = _as_mapping(checkpoint.get("completed")).get(task.task_id)
        result = _as_mapping(completed)
        receipt = _as_mapping(result.get("receipt"))
        manifest_digest = str(receipt.get("manifest_sha256") or "")
        task_month = (
            f"{str(task.params['start_date'])[:4]}-"
            f"{str(task.params['start_date'])[4:6]}"
        )
        if report_coverage.get((index_code, task_month)) != manifest_digest:
            raise TushareResearchTrustError(
                "Tushare index receipt differs from the selected report"
            )
        manifest, raw = artifact_store.read(manifest_digest)
        try:
            document = json.loads(raw)
            fields = document["data"]["fields"]
            items = document["data"]["items"]
            rows = [dict(zip(fields, item, strict=True)) for item in items]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TushareResearchTrustError("Tushare index artifact is invalid") from exc
        if manifest.get("classification") != "quarantine":
            raise TushareResearchTrustError("Tushare index artifact left quarantine")
        by_date: dict[str, set[str]] = {}
        for row in rows:
            if str(row.get("index_code") or "") != index_code:
                raise TushareResearchTrustError("Tushare index artifact scope changed")
            trade_date = str(row.get("trade_date") or "")
            code = str(row.get("con_code") or "")
            if re.fullmatch(r"\d{8}", trade_date) and re.fullmatch(
                r"\d{6}\.(?:SH|SZ|BJ)", code
            ):
                by_date.setdefault(trade_date, set()).add(code)
        if not by_date:
            raise TushareResearchTrustError("Tushare monthly membership is empty")
        selected_date, selected = max(
            by_date.items(), key=lambda item: (len(item[1]), item[0])
        )
        month = f"{selected_date[:4]}-{selected_date[4:6]}"
        monthly_members[month] = tuple(sorted(selected))
    if set(monthly_members) != set(REQUIRED_MONTHS):
        raise TushareResearchTrustError("Tushare monthly membership coverage changed")

    dates = pd.DatetimeIndex(pd.to_datetime(list(trading_dates), errors="raise"))
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    dates = dates.normalize().sort_values()
    date_text = tuple(day.strftime("%Y-%m-%d") for day in dates)
    members_by_date = tuple(monthly_members[day.strftime("%Y-%m")] for day in dates)
    union_codes = tuple(sorted({code for members in members_by_date for code in members}))
    timeline_hash = canonical_digest(
        {
            "schema_version": "point-in-time-universe-timeline/v1",
            "pool_id": normalized_pool,
            "dates": [[day, list(members)] for day, members in zip(date_text, members_by_date)],
            "parent_timeline_hash": None,
            "industry_filter": [],
        }
    )
    return PointInTimeUniverseTimeline(
        pool_id=normalized_pool,
        dates=date_text,
        members_by_date=members_by_date,
        union_codes=union_codes,
        source_batches=(
            {
                "batch_id": f"tushare-research-{report_digest[:16]}",
                "batch_digest": report_digest,
                "coverage_from": date_text[0],
                "coverage_to": date_text[-1],
            },
        ),
        timeline_hash=timeline_hash,
        coverage_from=date_text[0],
        coverage_to=date_text[-1],
        expected_count=None,
        as_known_at=None,
        bitemporal_availability_verified=False,
    )
