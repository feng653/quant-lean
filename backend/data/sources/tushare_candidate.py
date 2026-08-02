"""Quarantine-only Tushare Pro REST adapter and public-source preflight.

The adapter intentionally has no ``DataSource`` implementation and no PIT
master import method.  Successful vendor calls are candidate observations,
not proof of historical availability or permission to retain/redistribute.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
import pandas as pd

from backend.data.provider_artifacts import (
    ContentAddressedProviderArtifactStore,
    ProviderArtifactError,
    build_candidate_artifact_manifest,
    canonical_sha256,
    utc_now,
)
from backend.data.source_validation import compare_independent_daily_frames


TUSHARE_ENDPOINT = "https://api.tushare.pro"
TUSHARE_ADAPTER_ID = "quant-platform/tushare-candidate-rest/v1"
TUSHARE_PREFLIGHT_SCHEMA = "tushare-candidate-preflight/v1"
_MAX_CALLS_PER_RUN = 32
_MAX_ROWS_PER_RESPONSE = 100_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TushareCandidateError(RuntimeError):
    """A sanitized Tushare candidate request or response failed."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic_code: str = "candidate_provider_error",
        provider_code: int | str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.provider_code = provider_code
        self.retryable = retryable

    def diagnostic(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.diagnostic_code,
            "retryable": self.retryable,
        }
        if self.provider_code is not None:
            result["provider_code"] = self.provider_code
        return result


@dataclass(frozen=True)
class TushareDatasetSpec:
    api_name: str
    fields: tuple[str, ...]
    required_fields: frozenset[str]
    effective_fields: tuple[str, ...]
    available_fields: tuple[str, ...] = ()
    optional: bool = False
    minimum_rows: int = 0

    def temporal_contract(self) -> dict[str, Any]:
        available: dict[str, Any]
        if self.available_fields:
            available = {
                "fields": list(self.available_fields),
                "evidence": "provider_field",
                "semantics": (
                    "provider-declared announcement/implementation timestamp; "
                    "field-level semantics still require review"
                ),
            }
        else:
            available = {
                "fields": [],
                "evidence": "declared_ingestion_time",
                "semantics": (
                    "provider exposes no first-seen timestamp in this endpoint; "
                    "ingested_at is only an upper-bound observation"
                ),
            }
        effective = (
            {
                "fields": list(self.effective_fields),
                "evidence": "provider_field",
            }
            if self.effective_fields
            else {
                "fields": [],
                "evidence": "declared_ingestion_time",
                "semantics": "endpoint does not expose a historical effective timestamp",
            }
        )
        return {
            "effective_at": effective,
            "available_at": available,
        }


DATASET_SPECS: dict[str, TushareDatasetSpec] = {
    "daily": TushareDatasetSpec(
        "daily",
        (
            "ts_code", "trade_date", "open", "high", "low", "close",
            "pre_close", "change", "pct_chg", "vol", "amount",
        ),
        frozenset({"ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"}),
        ("trade_date",),
        minimum_rows=1,
    ),
    "adj_factor": TushareDatasetSpec(
        "adj_factor", ("ts_code", "trade_date", "adj_factor"),
        frozenset({"ts_code", "trade_date", "adj_factor"}), ("trade_date",),
        minimum_rows=1,
    ),
    "daily_basic": TushareDatasetSpec(
        "daily_basic",
        (
            "ts_code", "trade_date", "turnover_rate", "volume_ratio", "pe",
            "pb", "total_share", "float_share", "total_mv", "circ_mv",
        ),
        frozenset({"ts_code", "trade_date", "total_share", "float_share", "total_mv", "circ_mv"}),
        ("trade_date",),
        minimum_rows=1,
    ),
    "stock_basic": TushareDatasetSpec(
        "stock_basic",
        (
            "ts_code", "symbol", "name", "area", "industry", "market",
            "exchange", "list_status", "list_date", "delist_date",
        ),
        frozenset({"ts_code", "symbol", "name", "list_status", "list_date"}),
        ("list_date", "delist_date"),
        minimum_rows=1_000,
    ),
    "namechange": TushareDatasetSpec(
        "namechange",
        ("ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"),
        frozenset({"ts_code", "name", "start_date"}),
        ("start_date", "end_date"),
        ("ann_date",),
        optional=True,
    ),
    "suspend_d": TushareDatasetSpec(
        "suspend_d",
        ("ts_code", "trade_date", "suspend_timing", "suspend_type"),
        frozenset({"ts_code", "trade_date"}),
        ("trade_date",),
        optional=True,
    ),
    "dividend": TushareDatasetSpec(
        "dividend",
        (
            "ts_code", "end_date", "ann_date", "div_proc", "stk_div",
            "stk_bo_rate", "stk_co_rate", "cash_div", "cash_div_tax",
            "record_date", "ex_date", "pay_date", "div_listdate",
            "imp_ann_date", "base_date", "base_share",
        ),
        frozenset({"ts_code", "end_date", "ann_date", "div_proc"}),
        ("record_date", "ex_date", "pay_date", "div_listdate"),
        ("ann_date", "imp_ann_date"),
        optional=True,
    ),
    "index_weight": TushareDatasetSpec(
        "index_weight", ("index_code", "con_code", "trade_date", "weight"),
        frozenset({"index_code", "con_code", "trade_date", "weight"}),
        ("trade_date",),
        minimum_rows=1,
    ),
    "index_daily": TushareDatasetSpec(
        "index_daily",
        (
            "ts_code", "trade_date", "close", "open", "high", "low",
            "pre_close", "change", "pct_chg", "vol", "amount",
        ),
        frozenset({"ts_code", "trade_date", "open", "high", "low", "close"}),
        ("trade_date",),
        minimum_rows=1,
    ),
    "trade_cal": TushareDatasetSpec(
        "trade_cal",
        ("exchange", "cal_date", "is_open", "pretrade_date"),
        frozenset({"exchange", "cal_date", "is_open"}),
        ("cal_date",),
        minimum_rows=1,
    ),
    "sw_classify": TushareDatasetSpec(
        "index_classify",
        ("index_code", "industry_name", "level", "industry_code", "is_pub", "src"),
        frozenset({"index_code", "industry_name", "level", "src"}),
        (),
        optional=True,
    ),
    "sw_membership": TushareDatasetSpec(
        "index_member_all",
        (
            "l1_code", "l1_name", "l2_code", "l2_name", "l3_code",
            "l3_name", "ts_code", "name", "in_date", "out_date", "is_new",
        ),
        frozenset({"l1_code", "l1_name", "ts_code", "in_date"}),
        ("in_date", "out_date"),
        optional=True,
    ),
}

_INDEX_WEIGHT_MINIMUM_ROWS = {
    "000300": 300,
    "000905": 500,
    "000906": 800,
    "000852": 1_000,
}


@dataclass(frozen=True)
class TushareCandidateObservation:
    dataset: str
    fields: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]
    receipt: dict[str, Any]


def _validate_token(token: str) -> str:
    value = str(token).strip()
    if not value or len(value) < 8 or len(value) > 512 or any(char.isspace() for char in value):
        raise TushareCandidateError("TUSHARE_TOKEN is missing or malformed")
    return value


def _compact_date(value: str, field: str) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise TushareCandidateError(f"{field} must be YYYY-MM-DD") from exc
    return parsed.strftime("%Y%m%d")


def _sanitize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in params.items():
        name = str(key).strip()
        if not name or not all(char.isalnum() or char == "_" for char in name):
            raise TushareCandidateError("Tushare parameter name is invalid")
        if "token" in name.lower() or "secret" in name.lower() or "password" in name.lower():
            raise TushareCandidateError("credentials must not appear in dataset parameters")
        if value is None:
            continue
        text = str(value).strip()
        if len(text) > 256:
            raise TushareCandidateError(f"Tushare parameter {name} is too long")
        result[name] = value
    return result


def _validate_outbound_proxy_url(value: str | None) -> str | None:
    """Accept an explicit local proxy without exposing its complete URL."""

    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise TushareCandidateError(
            "candidate outbound proxy must use HTTP or HTTPS",
            diagnostic_code="explicit_proxy_configuration_invalid",
        )
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise TushareCandidateError(
            "candidate outbound proxy must terminate on loopback",
            diagnostic_code="explicit_proxy_configuration_invalid",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise TushareCandidateError(
            "candidate outbound proxy port is invalid",
            diagnostic_code="explicit_proxy_configuration_invalid",
        ) from exc
    if port is None or not 1 <= port <= 65_535:
        raise TushareCandidateError(
            "candidate outbound proxy requires a valid port",
            diagnostic_code="explicit_proxy_configuration_invalid",
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise TushareCandidateError(
            "candidate outbound proxy URL must not contain path, query, or fragment",
            diagnostic_code="explicit_proxy_configuration_invalid",
        )
    return raw


def _provider_rejection_diagnostic(code: Any) -> tuple[str, bool]:
    """Map stable Tushare response codes without retaining vendor messages."""

    if code == -2001:
        return "provider_permission_or_points_required", False
    if code == -2002:
        return "provider_request_frequency_rejected", True
    return "provider_request_rejected", False


class TushareCandidateClient:
    """Low-rate, bounded REST client whose only persistence is quarantine."""

    def __init__(
        self,
        *,
        token: str,
        store: ContentAddressedProviderArtifactStore,
        endpoint: str = TUSHARE_ENDPOINT,
        min_interval_seconds: float = 0.35,
        timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = _validate_token(token)
        if endpoint != TUSHARE_ENDPOINT:
            raise TushareCandidateError("only the approved HTTPS Tushare endpoint is allowed")
        if not math.isfinite(min_interval_seconds) or min_interval_seconds < 0.3:
            raise TushareCandidateError("Tushare rate interval must be at least 0.3 seconds")
        if not 1 <= max_attempts <= 4:
            raise TushareCandidateError("max_attempts must be between 1 and 4")
        self.store = store
        self.endpoint = endpoint
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self._proxy_url = _validate_outbound_proxy_url(proxy_url)
        self.transport = transport
        self._sleep = sleep
        self._clock = clock
        self._rate_lock = asyncio.Lock()
        self._last_request_at: float | None = None

    def transport_diagnostic(self) -> dict[str, Any]:
        """Return a credential-free description for reports and logs."""

        return {
            "explicit_proxy_configured": self._proxy_url is not None,
            "proxy_boundary": (
                "loopback_only" if self._proxy_url else "environment_or_direct"
            ),
            "proxy_url_retained": False,
        }

    @classmethod
    def from_environment(
        cls, *, store: ContentAddressedProviderArtifactStore, **kwargs: Any
    ) -> "TushareCandidateClient":
        return cls(token=os.environ.get("TUSHARE_TOKEN", ""), store=store, **kwargs)

    async def _wait_rate_limit(self) -> None:
        async with self._rate_lock:
            now = self._clock()
            if self._last_request_at is not None:
                remaining = self.min_interval_seconds - (now - self._last_request_at)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_at = self._clock()

    async def _post(self, body: Mapping[str, Any], api_name: str) -> bytes:
        for attempt in range(1, self.max_attempts + 1):
            await self._wait_rate_limit()
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                    proxy=self._proxy_url,
                    trust_env=self._proxy_url is None,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(self.endpoint, json=body)
                if 300 <= response.status_code < 400:
                    raise TushareCandidateError(
                        f"{api_name} refused an HTTP redirect",
                        diagnostic_code="provider_http_redirect_refused",
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_attempts:
                        await self._sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                        continue
                    raise TushareCandidateError(
                        f"{api_name} transport failed after {attempt} attempts (HTTP {response.status_code})",
                        diagnostic_code=(
                            "provider_http_rate_limited"
                            if response.status_code == 429
                            else "provider_service_unavailable"
                        ),
                        provider_code=response.status_code,
                        retryable=True,
                    )
                if response.status_code >= 400:
                    raise TushareCandidateError(
                        f"{api_name} request was rejected (HTTP {response.status_code})",
                        diagnostic_code="provider_http_rejected",
                        provider_code=response.status_code,
                    )
                if len(response.content) > 32 * 1024 * 1024:
                    raise TushareCandidateError(f"{api_name} response exceeded the size limit")
                return bytes(response.content)
            except httpx.ProxyError as exc:
                if attempt < self.max_attempts:
                    await self._sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                raise TushareCandidateError(
                    f"{api_name} network failed after {attempt} attempts ({type(exc).__name__})",
                    diagnostic_code="explicit_proxy_transport_failed",
                    retryable=True,
                ) from exc
            except httpx.TimeoutException as exc:
                if attempt < self.max_attempts:
                    await self._sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                raise TushareCandidateError(
                    f"{api_name} network failed after {attempt} attempts ({type(exc).__name__})",
                    diagnostic_code="provider_network_timeout",
                    retryable=True,
                ) from exc
            except httpx.NetworkError as exc:
                if attempt < self.max_attempts:
                    await self._sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                raise TushareCandidateError(
                    f"{api_name} network failed after {attempt} attempts ({type(exc).__name__})",
                    diagnostic_code=(
                        "explicit_proxy_transport_failed"
                        if self._proxy_url
                        else "provider_network_unreachable"
                    ),
                    retryable=True,
                ) from exc
        raise AssertionError("unreachable retry state")

    async def fetch(
        self,
        dataset: str,
        params: Mapping[str, Any],
        *,
        ingested_at: str | None = None,
        licence_status: str = "unverified",
    ) -> TushareCandidateObservation:
        if dataset not in DATASET_SPECS:
            raise TushareCandidateError(f"unsupported Tushare candidate dataset: {dataset}")
        spec = DATASET_SPECS[dataset]
        sanitized_params = _sanitize_params(params)
        if any(str(value).strip() == self._token for value in sanitized_params.values()):
            raise TushareCandidateError("credentials must not appear in dataset parameters")
        fields_text = ",".join(spec.fields)
        response_payload = await self._post(
            {
                "api_name": spec.api_name,
                "token": self._token,
                "params": sanitized_params,
                "fields": fields_text,
            },
            spec.api_name,
        )
        if self._token.encode("utf-8") in response_payload:
            raise TushareCandidateError(
                f"{spec.api_name} response unexpectedly contained credentials"
            )
        try:
            document = json.loads(response_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TushareCandidateError(f"{spec.api_name} returned invalid JSON") from exc
        if not isinstance(document, Mapping):
            raise TushareCandidateError(f"{spec.api_name} returned a non-object response")
        code = document.get("code")
        if code != 0:
            code_text = str(code)[:32]
            diagnostic_code, retryable = _provider_rejection_diagnostic(code)
            raise TushareCandidateError(
                f"{spec.api_name} rejected the request (code={code_text})",
                diagnostic_code=diagnostic_code,
                provider_code=code_text,
                retryable=retryable,
            )
        data = document.get("data")
        if not isinstance(data, Mapping):
            raise TushareCandidateError(f"{spec.api_name} response has no data object")
        fields = data.get("fields")
        items = data.get("items")
        if (
            not isinstance(fields, list)
            or not all(isinstance(field, str) and field for field in fields)
            or len(fields) != len(set(fields))
            or not isinstance(items, list)
        ):
            raise TushareCandidateError(f"{spec.api_name} response table is malformed")
        if not spec.required_fields.issubset(fields):
            missing = sorted(spec.required_fields - set(fields))
            raise TushareCandidateError(f"{spec.api_name} response is missing required fields: {missing}")
        if len(items) > _MAX_ROWS_PER_RESPONSE:
            raise TushareCandidateError(f"{spec.api_name} response has too many rows")
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, list) or len(item) != len(fields):
                raise TushareCandidateError(f"{spec.api_name} response row shape changed")
            rows.append(dict(zip(fields, item, strict=True)))
        temporal_fields = {
            *spec.effective_fields,
            *spec.available_fields,
        }
        temporal_coverage = {
            field: sum(
                row.get(field) is not None and str(row.get(field)).strip() != ""
                for row in rows
            )
            for field in sorted(temporal_fields)
        }
        if rows and spec.effective_fields and not any(
            temporal_coverage.get(field, 0) for field in spec.effective_fields
        ):
            raise TushareCandidateError(
                f"{spec.api_name} response has no effective-time observations"
            )
        observed_at = ingested_at or utc_now()
        manifest = build_candidate_artifact_manifest(
            provider="tushare_pro",
            dataset=dataset,
            endpoint=self.endpoint,
            request={
                "adapter_id": TUSHARE_ADAPTER_ID,
                "api_name": spec.api_name,
                "params": sanitized_params,
                "fields": list(spec.fields),
            },
            response_payload=response_payload,
            response_fields=fields,
            row_count=len(rows),
            ingested_at=observed_at,
            temporal_contract=spec.temporal_contract(),
            temporal_coverage=temporal_coverage,
            licence_status=licence_status,
        )
        receipt = self.store.record(response_payload=response_payload, manifest=manifest)
        return TushareCandidateObservation(
            dataset=dataset,
            fields=tuple(fields),
            rows=tuple(rows),
            manifest=manifest,
            receipt=receipt,
        )


def standard_preflight_plan(
    *,
    ts_code: str,
    start: str,
    end: str,
    index_code: str = "000300.SH",
    observed_on: date | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return a bounded coverage probe, not a historical backfill plan."""

    if not isinstance(ts_code, str) or not ts_code.endswith((".SZ", ".SH", ".BJ")):
        raise TushareCandidateError("ts_code must include a supported exchange suffix")
    start_compact = _compact_date(start, "start")
    end_compact = _compact_date(end, "end")
    if start_compact > end_compact:
        raise TushareCandidateError("start must not be after end")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 31:
        raise TushareCandidateError("preflight date range may not exceed 31 days")
    end_date = date.fromisoformat(end)
    index_start = end_date.replace(day=1)
    index_end = end_date.replace(
        day=calendar.monthrange(end_date.year, end_date.month)[1]
    )
    # Never turn a partial current-month market window into a future-dated
    # complete-month index request. Historical windows keep their own month;
    # current/future month windows use the latest fully elapsed month.
    current = observed_on or datetime.now().astimezone().date()
    if index_end >= current:
        index_end = index_start.fromordinal(index_start.toordinal() - 1)
        index_start = index_end.replace(day=1)
    common = {"ts_code": ts_code, "start_date": start_compact, "end_date": end_compact}
    return [
        (
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": start_compact,
                "end_date": end_compact,
            },
        ),
        ("daily", common),
        ("adj_factor", common),
        ("daily_basic", common),
        ("suspend_d", common),
        ("dividend", {"ts_code": ts_code}),
        ("namechange", {"ts_code": ts_code}),
        (
            "index_weight",
            {
                "index_code": index_code,
                "start_date": index_start.strftime("%Y%m%d"),
                "end_date": index_end.strftime("%Y%m%d"),
            },
        ),
        ("stock_basic", {"list_status": "L"}),
        ("sw_classify", {"level": "L1", "src": "SW2021"}),
        ("sw_membership", {"ts_code": ts_code, "is_new": "N"}),
    ]


def _minimum_rows_for_preflight(
    dataset: str, *, index_code: str, start: str, end: str
) -> int:
    minimum = DATASET_SPECS[dataset].minimum_rows
    if dataset == "index_weight":
        bare_code = str(index_code).split(".", maxsplit=1)[0]
        minimum = max(minimum, _INDEX_WEIGHT_MINIMUM_ROWS.get(bare_code, 1))
    elif dataset == "trade_cal":
        minimum = max(
            minimum,
            (date.fromisoformat(end) - date.fromisoformat(start)).days + 1,
        )
    return minimum


def assess_index_weight_monthly_probe(
    observation: TushareCandidateObservation,
    *,
    index_code: str,
    probe_params: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify a complete-month Tushare weight probe without promoting it.

    Tushare documents ``index_weight`` as monthly and recommends the first and
    last calendar day.  HTTP 200 plus an empty table therefore means only that
    the provider returned no snapshot for that full month: it is neither a
    date-window bug nor proof that the index had no members.
    """

    if observation.dataset != "index_weight":
        raise TushareCandidateError("index_weight observation is required")
    start = str(probe_params.get("start_date") or "")
    end = str(probe_params.get("end_date") or "")
    if not re.fullmatch(r"[0-9]{8}", start) or not re.fullmatch(r"[0-9]{8}", end):
        raise TushareCandidateError("index_weight probe dates are invalid")
    expected_code = str(index_code).upper()
    minimum_rows = _minimum_rows_for_preflight(
        "index_weight",
        index_code=expected_code,
        start=f"{start[:4]}-{start[4:6]}-{start[6:]}",
        end=f"{end[:4]}-{end[4:6]}-{end[6:]}",
    )
    if not observation.rows:
        return {
            "status": "no_monthly_snapshot_returned",
            "reason": "provider_returned_empty_complete_month",
            "requested_complete_month": {"start_date": start, "end_date": end},
            "expected_index_code": expected_code,
            "minimum_member_rows": minimum_rows,
            "vendor_trade_dates": [],
            "guidance": (
                "The full calendar-month request was accepted but returned no "
                "weight snapshot. Preserve the artifact; confirm provider "
                "publication lag, retention range or entitlement before retrying."
            ),
        }
    vendor_dates: set[str] = set()
    mismatched_index_rows = 0
    for row in observation.rows:
        trade_date = str(row.get("trade_date") or "")
        if not re.fullmatch(r"[0-9]{8}", trade_date) or not start <= trade_date <= end:
            raise TushareCandidateError("index_weight row escaped requested month")
        vendor_dates.add(trade_date)
        if str(row.get("index_code") or "").upper() != expected_code:
            mismatched_index_rows += 1
    if mismatched_index_rows:
        return {
            "status": "index_code_mismatch",
            "reason": "provider_returned_rows_for_a_different_index",
            "requested_complete_month": {"start_date": start, "end_date": end},
            "expected_index_code": expected_code,
            "minimum_member_rows": minimum_rows,
            "vendor_trade_dates": sorted(vendor_dates),
            "mismatched_index_row_count": mismatched_index_rows,
        }
    if len(observation.rows) < minimum_rows:
        return {
            "status": "incomplete_monthly_snapshot",
            "reason": "provider_monthly_snapshot_below_expected_member_count",
            "requested_complete_month": {"start_date": start, "end_date": end},
            "expected_index_code": expected_code,
            "minimum_member_rows": minimum_rows,
            "row_count": len(observation.rows),
            "vendor_trade_dates": sorted(vendor_dates),
        }
    return {
        "status": "complete_monthly_snapshot_candidate",
        "reason": "candidate_only_not_pit_evidence",
        "requested_complete_month": {"start_date": start, "end_date": end},
        "expected_index_code": expected_code,
        "minimum_member_rows": minimum_rows,
        "row_count": len(observation.rows),
        "vendor_trade_dates": sorted(vendor_dates),
    }


def _open_sessions(observation: TushareCandidateObservation) -> list[str]:
    if observation.dataset != "trade_cal":
        raise TushareCandidateError("trade_cal observation is required")
    sessions = sorted(
        {
            str(row.get("cal_date", ""))
            for row in observation.rows
            if str(row.get("is_open", "")).strip() in {"1", "1.0"}
        }
    )
    return [session for session in sessions if re.fullmatch(r"[0-9]{8}", session)]


def tushare_daily_panel(observation: TushareCandidateObservation) -> pd.DataFrame:
    if observation.dataset != "daily":
        raise TushareCandidateError("daily observation is required for comparison")
    if not observation.rows:
        return pd.DataFrame()
    frame = pd.DataFrame(observation.rows)
    frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0]
    frame["date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="raise")
    for field in ("open", "high", "low", "close", "vol"):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
    frame.rename(columns={"vol": "volume"}, inplace=True)
    panel = frame.set_index(["date", "code"])[["open", "high", "low", "close", "volume"]].unstack("code")
    panel.columns = panel.columns.swaplevel(0, 1)
    panel.columns.names = ["code", "field"]
    return panel.sort_index().sort_index(axis=1)


async def cross_validate_daily_with_public_source(
    observation: TushareCandidateObservation,
    *,
    start: str,
    end: str,
    min_overlap_returns: int = 2,
) -> dict[str, Any]:
    """Try BaoStock, then AKShare/Sina, solely as anomaly detectors."""

    primary = tushare_daily_panel(observation)
    codes = sorted(set(primary.columns.get_level_values(0))) if not primary.empty else []
    attempts: list[dict[str, str]] = []
    if not codes:
        return {
            "schema_version": "tushare-public-cross-check/v1",
            "status": "insufficient_tushare_rows",
            "selected_reference": None,
            "attempts": attempts,
            "promotion_eligible": False,
        }
    references: list[tuple[str, Any]] = []
    from backend.data.sources.baostock_source import BaoStockSource
    from backend.data.sources.akshare_source import AKShareSource

    references.append(("baostock", BaoStockSource(price_adjustment="raw")))
    references.append(("akshare_sina", AKShareSource("sina", price_adjustment="raw")))
    for label, source in references:
        try:
            result = await source.fetch_daily_result(codes, start, end)
            comparison = compare_independent_daily_frames(
                primary,
                result.frame,
                primary_provider="tushare:pro",
                reference_provider=str(result.evidence["provider"]),
                requested_codes=codes,
                adjustment="raw",
                required_fields=("open", "high", "low", "close", "volume"),
                min_overlap_returns=min_overlap_returns,
                return_abs_tolerance=0.005,
                max_conflict_ratio=0.02,
            )
            return {
                "schema_version": "tushare-public-cross-check/v1",
                "status": "completed",
                "selected_reference": label,
                "attempts": attempts,
                "comparison": comparison,
                "interpretation": "public anomaly check only; does not upgrade PIT authority",
                "promotion_eligible": False,
            }
        except Exception as exc:
            attempts.append({"reference": label, "error_type": type(exc).__name__})
    return {
        "schema_version": "tushare-public-cross-check/v1",
        "status": "all_references_unavailable",
        "selected_reference": None,
        "attempts": attempts,
        "promotion_eligible": False,
    }


def compare_index_weight_to_official_members(
    observation: TushareCandidateObservation,
    *,
    official_member_codes: Sequence[str],
    official_observed_on: str,
    official_content_sha256: str,
) -> dict[str, Any]:
    """Compare a vendor snapshot with a separately retained official anchor."""

    if observation.dataset != "index_weight":
        raise TushareCandidateError("index_weight observation is required")
    try:
        date.fromisoformat(official_observed_on)
    except ValueError as exc:
        raise TushareCandidateError("official_observed_on must be YYYY-MM-DD") from exc
    if not _SHA256.fullmatch(official_content_sha256):
        raise TushareCandidateError("official content digest is invalid")

    vendor_dates: set[str] = set()
    for row in observation.rows:
        raw_date = str(row.get("trade_date", ""))
        try:
            vendor_dates.add(datetime.strptime(raw_date, "%Y%m%d").date().isoformat())
        except ValueError as exc:
            raise TushareCandidateError("vendor index weight date is invalid") from exc
    if vendor_dates != {official_observed_on}:
        return {
            "schema_version": "tushare-csindex-anchor-comparison/v1",
            "status": "not_comparable",
            "reason": "vendor_and_official_observation_dates_do_not_match",
            "official": {
                "provider": "csindex_official",
                "observed_on": official_observed_on,
                "content_sha256": official_content_sha256,
                "member_count": len(set(official_member_codes)),
            },
            "vendor": {
                "provider": "tushare_pro",
                "artifact_sha256": observation.receipt["artifact_sha256"],
                "trade_dates": sorted(vendor_dates),
                "member_count": len(observation.rows),
            },
            "promotion_eligible": False,
            "interpretation": (
                "Constituent sets from different observation dates must never "
                "be treated as agreement or disagreement evidence."
            ),
        }

    def normalize(value: Any) -> str:
        code = str(value or "").split(".", maxsplit=1)[0].zfill(6)
        if not re.fullmatch(r"[0-9]{6}", code):
            raise TushareCandidateError("index constituent code is invalid")
        return code

    vendor = {normalize(row.get("con_code")) for row in observation.rows}
    official = {normalize(code) for code in official_member_codes}
    if not official:
        raise TushareCandidateError("official member evidence is empty")
    vendor_only = sorted(vendor - official)
    official_only = sorted(official - vendor)
    return {
        "schema_version": "tushare-csindex-anchor-comparison/v1",
        "status": "exact_match" if not vendor_only and not official_only else "difference_detected",
        "official": {
            "provider": "csindex_official",
            "observed_on": official_observed_on,
            "content_sha256": official_content_sha256,
            "member_count": len(official),
        },
        "vendor": {
            "provider": "tushare_pro",
            "artifact_sha256": observation.receipt["artifact_sha256"],
            "member_count": len(vendor),
        },
        "vendor_only_count": len(vendor_only),
        "official_only_count": len(official_only),
        "vendor_only_examples": vendor_only[:20],
        "official_only_examples": official_only[:20],
        "promotion_eligible": False,
        "interpretation": (
            "snapshot anomaly evidence only; historical adjustment events and "
            "publication timestamps remain governed separately"
        ),
    }


async def collect_governed_csindex_current_anchor(
    *,
    scope_id: str,
    actor_user_id: int,
    collector: Any | None = None,
    governance: Any | None = None,
) -> dict[str, Any]:
    """Opt-in one-anchor collection into the existing official governance store."""

    if scope_id not in {"csi300", "csi500", "csi1000"}:
        raise TushareCandidateError("unsupported CSI current-anchor scope")
    if isinstance(actor_user_id, bool) or actor_user_id <= 0:
        raise TushareCandidateError("official anchor actor_user_id must be positive")
    if collector is None:
        from backend.data.sources.csindex_pit import CsindexOfficialCollector

        collector = CsindexOfficialCollector(timeout_seconds=20.0)
    if governance is None:
        from backend.data.pit_evidence_governance import PitEvidenceGovernance

        governance = PitEvidenceGovernance()
    anchor = await collector.fetch_current_anchor(scope_id)
    receipt = governance.record_artifact(
        artifact=anchor.artifact,
        actor_user_id=actor_user_id,
    )
    return {
        "schema_version": "csindex-current-anchor-preflight/v1",
        "provider": "csindex_official",
        "scope_id": scope_id,
        "observed_on": anchor.observed_on.isoformat(),
        "member_codes": [member.security_code for member in anchor.members],
        "content_sha256": anchor.artifact.content_sha256,
        "governance_receipt": receipt,
        "classification": "official_snapshot_evidence_only",
        "historical_replay_complete": False,
        "production_import_performed": False,
    }


async def run_standard_preflight(
    client: TushareCandidateClient,
    *,
    ts_code: str,
    start: str,
    end: str,
    index_code: str = "000300.SH",
    cross_check: bool = True,
    official_index_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    plan = standard_preflight_plan(
        ts_code=ts_code, start=start, end=end, index_code=index_code
    )
    if len(plan) > _MAX_CALLS_PER_RUN:
        raise TushareCandidateError("preflight call budget exceeded")
    datasets: list[dict[str, Any]] = []
    observations: dict[str, TushareCandidateObservation] = {}
    daily: TushareCandidateObservation | None = None
    index_weight: TushareCandidateObservation | None = None
    index_weight_probe: dict[str, Any] | None = None
    for dataset, params in plan:
        spec = DATASET_SPECS[dataset]
        minimum_rows = _minimum_rows_for_preflight(
            dataset,
            index_code=index_code,
            start=start,
            end=end,
        )
        try:
            observation = await client.fetch(dataset, params)
            observations[dataset] = observation
            if dataset == "daily":
                daily = observation
            elif dataset == "index_weight":
                index_weight = observation
            row_count = len(observation.rows)
            status = "ok" if row_count >= minimum_rows else "insufficient_rows"
            item: dict[str, Any] = {
                "dataset": dataset,
                "status": status,
                "optional": spec.optional,
                "row_count": row_count,
                "minimum_rows": minimum_rows,
                "artifact_sha256": observation.receipt["artifact_sha256"],
                "manifest_sha256": observation.receipt["manifest_sha256"],
            }
            if status == "insufficient_rows":
                item.update(
                    {
                        "reason": "required_dataset_below_minimum_rows",
                        "guidance": (
                            "HTTP success with an empty or undersized table is not "
                            "coverage evidence; verify the probe window, provider "
                            "permission, and trading calendar before retrying"
                        ),
                    }
                )
            elif row_count == 0:
                item["empty_semantics"] = (
                    "no_observation_not_authoritative_no_event_proof"
                )
            if dataset == "index_weight":
                index_weight_probe = assess_index_weight_monthly_probe(
                    observation,
                    index_code=index_code,
                    probe_params=params,
                )
                item["monthly_probe"] = index_weight_probe
                if index_weight_probe["status"] != "complete_monthly_snapshot_candidate":
                    item["reason"] = str(index_weight_probe["reason"])
            datasets.append(item)
        except (TushareCandidateError, ProviderArtifactError) as exc:
            item = {
                "dataset": dataset,
                "status": "failed",
                "optional": spec.optional,
                "minimum_rows": minimum_rows,
                "error_type": type(exc).__name__,
                "error": str(exc)[:240],
            }
            if isinstance(exc, TushareCandidateError):
                item["diagnostic"] = exc.diagnostic()
            datasets.append(item)
    required_failures = [
        item["dataset"]
        for item in datasets
        if item["status"] != "ok" and not item["optional"]
    ]
    plan_failures: list[dict[str, Any]] = []
    calendar_observation = observations.get("trade_cal")
    open_sessions: list[str] = []
    if calendar_observation is not None:
        open_sessions = _open_sessions(calendar_observation)
        if not open_sessions:
            plan_failures.append(
                {
                    "reason": "probe_window_has_no_open_trading_sessions",
                    "guidance": (
                        "Choose a window containing at least one provider-declared "
                        "open session; a weekend or exchange holiday window cannot "
                        "validate daily market-data coverage"
                    ),
                }
            )
        daily_observation = observations.get("daily")
        if daily_observation is not None and len(daily_observation.rows) > len(open_sessions):
            plan_failures.append(
                {
                    "reason": "daily_rows_exceed_declared_open_sessions",
                    "guidance": "Review the trading-calendar and daily response scopes.",
                }
            )
    cross_validation: dict[str, Any] = {
        "status": "not_requested",
        "promotion_eligible": False,
    }
    if cross_check and daily is not None:
        cross_validation = await cross_validate_daily_with_public_source(
            daily, start=start, end=end
        )
    official_comparison: dict[str, Any] = {
        "status": "not_supplied",
        "required_evidence_schema": "csindex-pit-staging/v2",
        "promotion_eligible": False,
    }
    if official_index_evidence is not None and index_weight is not None:
        official_comparison = compare_index_weight_to_official_members(
            index_weight,
            official_member_codes=official_index_evidence.get("member_codes", []),
            official_observed_on=str(official_index_evidence.get("observed_on", "")),
            official_content_sha256=str(
                official_index_evidence.get("content_sha256", "")
            ),
        )
    report: dict[str, Any] = {
        "schema_version": TUSHARE_PREFLIGHT_SCHEMA,
        "adapter_id": TUSHARE_ADAPTER_ID,
        "observed_at": utc_now(),
        "request_scope": {
            "ts_code": ts_code,
            "start": start,
            "end": end,
            "index_code": index_code,
        },
        "transport": client.transport_diagnostic(),
        "datasets": datasets,
        "required_failures": required_failures,
        "plan_validation": {
            "open_session_count": len(open_sessions),
            "failures": plan_failures,
        },
        "candidate_collection_valid": not required_failures and not plan_failures,
        "cross_validation": cross_validation,
        "official_index_comparison": official_comparison,
        "index_weight_monthly_probe": index_weight_probe
        or {
            "status": "not_collected",
            "reason": "index_weight_request_failed_before_classification",
        },
        "classification": "quarantine",
        "production_pit_ready": False,
        "promotion": {
            "eligible": False,
            "blockers": [
                "candidate_quarantine_only",
                "provider_available_at_incomplete",
                "official_event_evidence_and_review_required",
                "provider_retention_terms_unverified",
            ],
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    report["stored_report_sha256"] = client.store.record_report(report)
    return report
