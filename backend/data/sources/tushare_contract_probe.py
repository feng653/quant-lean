"""Bounded, quarantine-only Tushare PIT contract evidence probe.

This module deliberately stops at provider artifacts and a diagnostic report.
It has no PIT master, cache, importer, approval, or activation dependency.
Sampling historical months demonstrates endpoint reachability and candidate
retention only; it never claims continuous point-in-time coverage.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime
from typing import Any, Mapping, Sequence

from backend.data.provider_artifacts import ProviderArtifactError, canonical_sha256, utc_now
from backend.data.sources.tushare_candidate import (
    TushareCandidateClient,
    TushareCandidateError,
    assess_index_weight_monthly_probe,
)


TUSHARE_CONTRACT_PROBE_SCHEMA = "tushare-pit-contract-probe/v1"
FOUR_INDEX_CODES = ("000300.SH", "000905.SH", "000906.SH", "000852.SH")
_MONTH = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_MAX_PROBE_MONTHS = 6
_MAX_CALLS = 48


def default_contract_probe_months(observed_on: date | None = None) -> tuple[str, ...]:
    """Return sparse history anchors plus the latest two closed months."""

    current = observed_on or datetime.now(UTC).date()
    first_this_month = current.replace(day=1)
    previous_end = first_this_month.fromordinal(first_this_month.toordinal() - 1)
    previous_start = previous_end.replace(day=1)
    prior_end = previous_start.fromordinal(previous_start.toordinal() - 1)
    values = {
        "2016-01",
        "2020-01",
        "2023-01",
        "2025-01",
        prior_end.strftime("%Y-%m"),
        previous_end.strftime("%Y-%m"),
    }
    return tuple(sorted(values))


def _month_params(index_code: str, month: str) -> dict[str, str]:
    if not _MONTH.fullmatch(month):
        raise TushareCandidateError(
            "contract probe month must be YYYY-MM",
            diagnostic_code="contract_probe_scope_invalid",
        )
    year, month_number = (int(part) for part in month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    compact = month.replace("-", "")
    return {
        "index_code": index_code,
        "start_date": f"{compact}01",
        "end_date": f"{compact}{last_day:02d}",
    }


def _failure(dataset: str, exc: Exception, **scope: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "dataset": dataset,
        "status": "failed",
        "error_type": type(exc).__name__,
        **scope,
    }
    if isinstance(exc, TushareCandidateError):
        item["diagnostic"] = exc.diagnostic()
    else:
        item["diagnostic"] = {
            "code": "candidate_artifact_validation_failed",
            "retryable": False,
        }
    return item


def _availability_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    complete = sorted(
        str(row["month"])
        for row in rows
        if row.get("status") == "complete_monthly_snapshot_candidate"
    )
    unavailable = sorted(
        str(row["month"])
        for row in rows
        if row.get("status") == "no_monthly_snapshot_returned"
    )
    latest = complete[-1] if complete else None
    first_empty_after_latest = (
        next((month for month in unavailable if latest is not None and month > latest), None)
    )
    return {
        "classification": "bounded_sparse_probe_not_continuous_coverage",
        "earliest_complete_month_observed": complete[0] if complete else None,
        "latest_complete_month_observed": latest,
        "complete_months_observed": complete,
        "empty_months_observed": unavailable,
        "first_empty_month_after_latest_complete": first_empty_after_latest,
        "cutoff_is_exact": False,
        "interpretation": (
            "Only the requested complete months were observed. Empty months may "
            "reflect publication lag, retention, or entitlement and require a "
            "later observation plus provider confirmation."
        ),
    }


def _sample_codes(
    latest_rows_by_index: Mapping[str, Sequence[Mapping[str, Any]]],
    sample_size: int,
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    positions = {code: 0 for code in FOUR_INDEX_CODES}
    while len(selected) < sample_size:
        changed = False
        for index_code in FOUR_INDEX_CODES:
            rows = latest_rows_by_index.get(index_code, ())
            position = positions[index_code]
            while position < len(rows):
                value = str(rows[position].get("con_code") or "").upper()
                position += 1
                if re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", value) and value not in seen:
                    seen.add(value)
                    selected.append(value)
                    changed = True
                    break
            positions[index_code] = position
            if len(selected) >= sample_size:
                break
        if not changed:
            break
    return selected


async def run_tushare_pit_contract_probe(
    client: TushareCandidateClient,
    *,
    probe_months: Sequence[str] | None = None,
    sample_size: int = 30,
    event_security_count: int = 5,
) -> dict[str, Any]:
    """Collect sparse four-index, 30-security, and event contract evidence."""

    months = tuple(dict.fromkeys(probe_months or default_contract_probe_months()))
    if not 1 <= len(months) <= _MAX_PROBE_MONTHS or any(
        not _MONTH.fullmatch(month) for month in months
    ):
        raise TushareCandidateError(
            "contract probe requires one to six unique YYYY-MM months",
            diagnostic_code="contract_probe_scope_invalid",
        )
    if not 1 <= sample_size <= 30 or not 0 <= event_security_count <= 5:
        raise TushareCandidateError(
            "contract probe sample limits are invalid",
            diagnostic_code="contract_probe_scope_invalid",
        )
    expected_calls = (
        len(FOUR_INDEX_CODES) * len(months)
        + 3  # daily, adjustment factor, daily basic cross sections
        + 3  # listed/delisted/paused security masters
        + 2  # trade calendar and suspension cross section
        + 2 * event_security_count  # dividend and name/ST observations
    )
    if expected_calls > _MAX_CALLS:
        raise TushareCandidateError(
            "contract probe call budget exceeded",
            diagnostic_code="contract_probe_scope_invalid",
        )

    index_rows: list[dict[str, Any]] = []
    latest_complete: dict[str, tuple[str, Any]] = {}
    for index_code in FOUR_INDEX_CODES:
        for month in months:
            params = _month_params(index_code, month)
            try:
                observation = await client.fetch("index_weight", params)
                assessment = assess_index_weight_monthly_probe(
                    observation,
                    index_code=index_code,
                    probe_params=params,
                )
                item = {
                    "dataset": "index_weight",
                    "index_code": index_code,
                    "month": month,
                    "artifact_sha256": observation.receipt["artifact_sha256"],
                    "manifest_sha256": observation.receipt["manifest_sha256"],
                    **assessment,
                }
                index_rows.append(item)
                if assessment["status"] == "complete_monthly_snapshot_candidate":
                    previous = latest_complete.get(index_code)
                    if previous is None or month > previous[0]:
                        latest_complete[index_code] = (month, observation)
            except (TushareCandidateError, ProviderArtifactError) as exc:
                index_rows.append(
                    _failure("index_weight", exc, index_code=index_code, month=month)
                )

    latest_rows_by_index = {
        code: value[1].rows for code, value in latest_complete.items()
    }
    sampled_codes = _sample_codes(latest_rows_by_index, sample_size)
    snapshot_dates = sorted(
        {
            str(row.get("trade_date") or "")
            for observation_rows in latest_rows_by_index.values()
            for row in observation_rows
            if re.fullmatch(r"[0-9]{8}", str(row.get("trade_date") or ""))
        }
    )
    market_date = snapshot_dates[-1] if snapshot_dates else None

    market_rows: list[dict[str, Any]] = []
    if market_date is not None:
        for dataset in ("daily", "adj_factor", "daily_basic"):
            try:
                observation = await client.fetch(dataset, {"trade_date": market_date})
                returned = {
                    str(row.get("ts_code") or "").upper() for row in observation.rows
                }
                missing = sorted(set(sampled_codes) - returned)
                market_rows.append(
                    {
                        "dataset": dataset,
                        "status": "ok" if not missing and len(sampled_codes) == sample_size else "incomplete_sample",
                        "trade_date": market_date,
                        "requested_sample_size": sample_size,
                        "sample_rows_observed": len(set(sampled_codes) & returned),
                        "missing_code_examples": missing[:10],
                        "provider_cross_section_rows": len(observation.rows),
                        "artifact_sha256": observation.receipt["artifact_sha256"],
                        "manifest_sha256": observation.receipt["manifest_sha256"],
                    }
                )
            except (TushareCandidateError, ProviderArtifactError) as exc:
                market_rows.append(_failure(dataset, exc, trade_date=market_date))

    security_master_rows: list[dict[str, Any]] = []
    for list_status in ("L", "D", "P"):
        try:
            observation = await client.fetch("stock_basic", {"list_status": list_status})
            security_master_rows.append(
                {
                    "dataset": "stock_basic",
                    "list_status": list_status,
                    "status": "ok" if observation.rows else "empty",
                    "row_count": len(observation.rows),
                    "artifact_sha256": observation.receipt["artifact_sha256"],
                    "manifest_sha256": observation.receipt["manifest_sha256"],
                }
            )
        except (TushareCandidateError, ProviderArtifactError) as exc:
            security_master_rows.append(
                _failure("stock_basic", exc, list_status=list_status)
            )

    daily_state_rows: list[dict[str, Any]] = []
    if market_date is not None:
        for dataset, params in (
            ("trade_cal", {"exchange": "SSE", "start_date": market_date, "end_date": market_date}),
            ("suspend_d", {"trade_date": market_date}),
        ):
            try:
                observation = await client.fetch(dataset, params)
                daily_state_rows.append(
                    {
                        "dataset": dataset,
                        "status": "observed" if observation.rows else "empty_not_no_event_proof",
                        "row_count": len(observation.rows),
                        "artifact_sha256": observation.receipt["artifact_sha256"],
                        "manifest_sha256": observation.receipt["manifest_sha256"],
                    }
                )
            except (TushareCandidateError, ProviderArtifactError) as exc:
                daily_state_rows.append(_failure(dataset, exc, trade_date=market_date))

    event_rows: list[dict[str, Any]] = []
    for ts_code in sampled_codes[:event_security_count]:
        for dataset in ("dividend", "namechange"):
            try:
                observation = await client.fetch(dataset, {"ts_code": ts_code})
                bitemporal = observation.manifest["bitemporal"]
                available_fields = bitemporal["available_at"]["fields"]
                coverage = bitemporal["field_non_null_counts"]
                event_rows.append(
                    {
                        "dataset": dataset,
                        "ts_code": ts_code,
                        "status": "observed" if observation.rows else "empty_not_no_event_proof",
                        "row_count": len(observation.rows),
                        "available_fields": available_fields,
                        "available_field_nonempty_counts": {
                            field: coverage.get(field, 0) for field in available_fields
                        },
                        "artifact_sha256": observation.receipt["artifact_sha256"],
                        "manifest_sha256": observation.receipt["manifest_sha256"],
                    }
                )
            except (TushareCandidateError, ProviderArtifactError) as exc:
                event_rows.append(_failure(dataset, exc, ts_code=ts_code))

    index_availability = {
        code: _availability_summary(
            [row for row in index_rows if row.get("index_code") == code]
        )
        for code in FOUR_INDEX_CODES
    }
    four_index_2016_observed = all(
        any(
            row.get("index_code") == code
            and row.get("month") == "2016-01"
            and row.get("status") == "complete_monthly_snapshot_candidate"
            for row in index_rows
        )
        for code in FOUR_INDEX_CODES
    )
    thirty_security_market_observed = (
        len(sampled_codes) == sample_size
        and len(market_rows) == 3
        and all(row.get("status") == "ok" for row in market_rows)
    )
    report: dict[str, Any] = {
        "schema_version": TUSHARE_CONTRACT_PROBE_SCHEMA,
        "observed_at": utc_now(),
        "classification": "quarantine",
        "transport": client.transport_diagnostic(),
        "request_scope": {
            "probe_months": list(months),
            "four_index_codes": list(FOUR_INDEX_CODES),
            "sample_size": sample_size,
            "event_security_count": event_security_count,
            "maximum_calls": _MAX_CALLS,
            "planned_calls": expected_calls,
        },
        "index_weight_probes": index_rows,
        "index_availability": index_availability,
        "security_sample": {
            "selection": "deterministic_round_robin_from_latest_complete_four_index_candidates",
            "market_date": market_date,
            "sample_size": len(sampled_codes),
            "codes": sampled_codes,
            "market_datasets": market_rows,
        },
        "security_master": security_master_rows,
        "daily_state": daily_state_rows,
        "events": event_rows,
        "contract_checks": {
            "four_index_2016_sparse_anchor_observed": four_index_2016_observed,
            "thirty_security_market_cross_section_observed": thirty_security_market_observed,
            "continuous_2016_to_current_coverage_proven": False,
            "historical_available_at_proven": False,
            "historical_revision_retention_proven": False,
            "licence_retention_approved": False,
        },
        "candidate_collection_valid": (
            four_index_2016_observed and thirty_security_market_observed
        ),
        "production_pit_ready": False,
        "production_import_performed": False,
        "activation_performed": False,
        "promotion": {
            "eligible": False,
            "blockers": [
                "candidate_quarantine_only",
                "sparse_months_do_not_prove_continuous_history",
                "provider_available_at_incomplete",
                "provider_revision_history_unproven",
                "provider_retention_terms_unverified",
                "official_event_reconciliation_required",
            ],
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    report["stored_report_sha256"] = client.store.record_report(report)
    return report
