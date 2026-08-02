"""Immutable raw/research dual-price ledger and adjustment audit.

The existing Parquet cache is a convenient research input, not an execution
ledger.  This store keeps raw execution OHLCV, backward-adjusted research
OHLCV, and corporate-action/adjustment evidence in separate immutable tables.
Every read verifies the canonical batch and row digests before returning data.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from backend.config import settings

LEDGER_SCHEMA_VERSION = "dual-price-ledger/v1"
IMPORT_SCHEMA_VERSION = "dual-price-ledger-import/v1"
BITEMPORAL_IMPORT_SCHEMA_VERSION = "dual-price-ledger-import/v2"
READINESS_SCHEMA_VERSION = "dual-price-ledger-readiness/v1"
ADJUSTMENT_AUDIT_SCHEMA_VERSION = "price-adjustment-audit/v1"
CROSS_SCOPE_AUDIT_SCHEMA_VERSION = "price-cross-scope-audit/v1"
RUNTIME_BINDING_SCHEMA_VERSION = "price-ledger-runtime-binding/v1"
BITEMPORAL_RUNTIME_BINDING_SCHEMA_VERSION = "price-ledger-runtime-binding/v2"
CORPORATE_ACTION_EVIDENCE_SCHEMA_VERSION = (
    "corporate-action-bitemporal-evidence/v1"
)

PriceRole = Literal["raw_execution", "research_adjusted"]
_PRICE_ROLES = {"raw_execution", "research_adjusted"}
_ROLE_ADJUSTMENTS = {
    "raw_execution": "raw",
    "research_adjusted": "hfq",
}
_EVIDENCE_LEVELS = {
    "declared",
    "public_cross_validated",
    "licensed",
    "exchange_authoritative",
}
_RESEARCH_LEVELS = {
    "public_cross_validated",
    "licensed",
    "exchange_authoritative",
}
_EXECUTION_LEVELS = {"licensed", "exchange_authoritative"}
_AUTHORITATIVE_ACTION_LEVELS = {"licensed", "exchange_authoritative"}
_ACTION_TYPES = {
    "cash_dividend",
    "split",
    "bonus",
    "rights_issue",
    "merger",
    "other",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SECURITY_CODE = re.compile(r"^[0-9]{6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRICE_RECEIPT_ID = re.compile(r"^prpkg_[0-9a-f]{32}$")
_OHLC_FIELDS = ("open", "high", "low", "close")
_RATIO_REL_TOLERANCE = 1e-6
_FACTOR_CHANGE_TOLERANCE = 1e-4
_ABNORMAL_FACTOR_JUMP = 0.50
_DECLARED_FACTOR_TOLERANCE = 0.02
_CROSS_SCOPE_REL_TOLERANCE = 1e-10


class PriceLedgerError(RuntimeError):
    """Base class for fail-closed price-ledger operations."""


class PriceLedgerValidationError(PriceLedgerError):
    """Input or query parameters violate the ledger contract."""


class PriceLedgerConflictError(PriceLedgerError):
    """New evidence conflicts with an immutable accepted identity."""

    def __init__(
        self,
        message: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence = dict(evidence or {})


class PriceLedgerIntegrityError(PriceLedgerError):
    """Stored evidence no longer matches its cryptographic identity."""


_PRODUCTION_RELEASE_TOKEN = object()


@dataclass(frozen=True)
class _ProductionReleaseAuthorization:
    operation: str
    plan_sha256: str
    manifest_sha256: str
    document_sha256: str
    token: object


def _authorize_production_release(
    *,
    operation: str,
    plan_sha256: str,
    manifest_sha256: str,
    document_sha256: str,
) -> _ProductionReleaseAuthorization:
    """Issue an exact-document capability after release revalidation."""

    if operation not in {"import_batch", "bind_runtime_scope"} or not all(
        _SHA256.fullmatch(value)
        for value in (plan_sha256, manifest_sha256, document_sha256)
    ):
        raise PriceLedgerValidationError(
            "production release authorization is invalid"
        )
    return _ProductionReleaseAuthorization(
        operation=operation,
        plan_sha256=plan_sha256,
        manifest_sha256=manifest_sha256,
        document_sha256=document_sha256,
        token=_PRODUCTION_RELEASE_TOKEN,
    )


def _optional_row_value(row: sqlite3.Row, column: str) -> Any:
    """Read a v2 column without upgrading a read-only v1 ledger.

    Historical archives are intentionally opened read-only. A missing column
    is legacy evidence with no availability proof, never a reason to infer
    bitemporal metadata. ``sqlite3.Row`` raises ``IndexError`` for a missing
    named field.
    """

    try:
        return row[column]
    except (IndexError, KeyError):
        return None


@dataclass(frozen=True, slots=True)
class BoundRuntimePrices:
    """Two explicit price roles loaded from one verified runtime binding."""

    scope_id: str
    timeline_identity: dict[str, Any]
    trading_dates: tuple[str, ...]
    research_adjusted: Any
    raw_execution: Any
    binding: dict[str, Any]


PRICE_LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS price_ledger_batches (
    batch_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    coverage_from TEXT NOT NULL,
    coverage_to TEXT NOT NULL,
    raw_source_json TEXT NOT NULL,
    research_source_json TEXT NOT NULL,
    corporate_action_source_json TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    audit_json TEXT NOT NULL,
    audit_sha256 TEXT NOT NULL,
    batch_digest TEXT NOT NULL UNIQUE,
    imported_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    available_at TEXT,
    ingested_at TEXT,
    revision INTEGER,
    supersedes_batch_id TEXT
);

CREATE TABLE IF NOT EXISTS price_ledger_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    price_role TEXT NOT NULL CHECK (
        price_role IN ('raw_execution', 'research_adjusted')
    ),
    adjustment TEXT NOT NULL CHECK (adjustment IN ('raw', 'hfq')),
    security_code TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    source_provider TEXT NOT NULL,
    source_dataset TEXT NOT NULL,
    source_version TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    row_sha256 TEXT NOT NULL,
    effective_at TEXT,
    available_at TEXT,
    ingested_at TEXT,
    revision INTEGER,
    FOREIGN KEY (batch_id) REFERENCES price_ledger_batches(batch_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_price_ledger_price_identity
ON price_ledger_prices(
    scope_id, security_code, trading_date, source_provider, source_dataset,
    source_version, adjustment
);

CREATE INDEX IF NOT EXISTS idx_price_ledger_scope_role_date
ON price_ledger_prices(
    scope_id, price_role, trading_date, security_code
);

CREATE TABLE IF NOT EXISTS price_ledger_adjustment_factors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    security_code TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    raw_source_provider TEXT NOT NULL,
    raw_source_dataset TEXT NOT NULL,
    raw_source_version TEXT NOT NULL,
    research_source_provider TEXT NOT NULL,
    research_source_dataset TEXT NOT NULL,
    research_source_version TEXT NOT NULL,
    adjustment TEXT NOT NULL CHECK (adjustment = 'hfq_vs_raw'),
    implied_factor REAL NOT NULL,
    max_ohlc_ratio_delta REAL NOT NULL,
    factor_change REAL,
    row_sha256 TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES price_ledger_batches(batch_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_price_ledger_factor_identity
ON price_ledger_adjustment_factors(
    scope_id, security_code, trading_date, research_source_provider,
    research_source_dataset, research_source_version, adjustment
);

CREATE INDEX IF NOT EXISTS idx_price_ledger_factor_scope_date
ON price_ledger_adjustment_factors(scope_id, trading_date, security_code);

CREATE TABLE IF NOT EXISTS price_ledger_corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    security_code TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    action_type TEXT NOT NULL,
    adjustment_multiplier REAL,
    reference_id TEXT NOT NULL,
    source_provider TEXT NOT NULL,
    source_dataset TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_evidence_level TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES price_ledger_batches(batch_id)
);

CREATE TABLE IF NOT EXISTS corporate_action_bitemporal_evidence (
    evidence_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    security_code TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN ('event', 'confirmed_no_event')
    ),
    effective_at TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    available_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    supersedes_evidence_id TEXT,
    action_type TEXT,
    adjustment_multiplier REAL,
    reference_id TEXT NOT NULL,
    source_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL UNIQUE,
    imported_by_user_id INTEGER NOT NULL,
    FOREIGN KEY (supersedes_evidence_id)
        REFERENCES corporate_action_bitemporal_evidence(evidence_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_price_action_successor
ON corporate_action_bitemporal_evidence(supersedes_evidence_id)
WHERE supersedes_evidence_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_price_ledger_action_identity
ON price_ledger_corporate_actions(
    scope_id, security_code, effective_date, source_provider, source_dataset,
    source_version, action_type, reference_id
);

CREATE INDEX IF NOT EXISTS idx_price_ledger_action_scope_date
ON price_ledger_corporate_actions(scope_id, effective_date, security_code);

CREATE TABLE IF NOT EXISTS price_ledger_runtime_bindings (
    binding_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    coverage_from TEXT NOT NULL,
    coverage_to TEXT NOT NULL,
    timeline_json TEXT NOT NULL,
    timeline_sha256 TEXT NOT NULL,
    trading_dates_json TEXT NOT NULL,
    trading_dates_sha256 TEXT NOT NULL,
    batch_ids_json TEXT NOT NULL,
    batch_ids_sha256 TEXT NOT NULL,
    status_source_json TEXT NOT NULL,
    status_source_sha256 TEXT NOT NULL,
    suspensions_json TEXT NOT NULL,
    suspensions_sha256 TEXT NOT NULL,
    canonical_evidence_sha256 TEXT NOT NULL,
    binding_digest TEXT NOT NULL UNIQUE,
    bound_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    as_known_at TEXT,
    bitemporal_evidence_sha256 TEXT,
    price_role_usage_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_price_ledger_runtime_scope_date
ON price_ledger_runtime_bindings(scope_id, coverage_from, coverage_to);

CREATE TABLE IF NOT EXISTS price_ledger_batch_governance (
    batch_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    source_identity_sha256 TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES price_ledger_batches(batch_id)
);

CREATE TRIGGER IF NOT EXISTS price_ledger_batches_no_update
BEFORE UPDATE ON price_ledger_batches
BEGIN
    SELECT RAISE(ABORT, 'price ledger batch is immutable');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_batches_no_delete
BEFORE DELETE ON price_ledger_batches
BEGIN
    SELECT RAISE(ABORT, 'price ledger batch cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_prices_no_update
BEFORE UPDATE ON price_ledger_prices
BEGIN
    SELECT RAISE(ABORT, 'price ledger row is immutable');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_prices_no_delete
BEFORE DELETE ON price_ledger_prices
BEGIN
    SELECT RAISE(ABORT, 'price ledger row cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_factors_no_update
BEFORE UPDATE ON price_ledger_adjustment_factors
BEGIN
    SELECT RAISE(ABORT, 'price adjustment evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_factors_no_delete
BEFORE DELETE ON price_ledger_adjustment_factors
BEGIN
    SELECT RAISE(ABORT, 'price adjustment evidence cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_actions_no_update
BEFORE UPDATE ON price_ledger_corporate_actions
BEGIN
    SELECT RAISE(ABORT, 'corporate-action evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_actions_no_delete
BEFORE DELETE ON price_ledger_corporate_actions
BEGIN
    SELECT RAISE(ABORT, 'corporate-action evidence cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_action_evidence_no_update
BEFORE UPDATE ON corporate_action_bitemporal_evidence
BEGIN
    SELECT RAISE(ABORT, 'corporate-action bitemporal evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_action_evidence_no_delete
BEFORE DELETE ON corporate_action_bitemporal_evidence
BEGIN
    SELECT RAISE(ABORT, 'corporate-action bitemporal evidence cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_runtime_bindings_no_update
BEFORE UPDATE ON price_ledger_runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'price ledger runtime binding is immutable');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_runtime_bindings_no_delete
BEFORE DELETE ON price_ledger_runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'price ledger runtime binding cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_batch_governance_no_update
BEFORE UPDATE ON price_ledger_batch_governance
BEGIN
    SELECT RAISE(ABORT, 'price governance receipt is immutable');
END;

CREATE TRIGGER IF NOT EXISTS price_ledger_batch_governance_no_delete
BEFORE DELETE ON price_ledger_batch_governance
BEGIN
    SELECT RAISE(ABORT, 'price governance receipt cannot be deleted');
END;
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _anchor_semantics(adjustment: str) -> str:
    if adjustment == "raw":
        return "unadjusted_exchange_price"
    if adjustment == "hfq":
        return "hfq_absolute_anchor_bound_to_source_version"
    return "unsupported"


def _price_values(item: Mapping[str, Any]) -> dict[str, float]:
    return {
        field: float(item[field])
        for field in (*_OHLC_FIELDS, "volume")
    }


def _different_price_fields(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> list[str]:
    return [
        field
        for field in (*_OHLC_FIELDS, "volume")
        if not math.isclose(
            float(left[field]),
            float(right[field]),
            rel_tol=_CROSS_SCOPE_REL_TOLERANCE,
            abs_tol=0.0,
        )
    ]


def strict_unbiased_readiness(
    *,
    exact_pit_binding: bool,
    member_session_complete: bool,
    bitemporal_availability_verified: bool,
    trading_status_authoritative: bool,
    corporate_action_validated: bool,
    trusted_research_ledger: bool,
    trusted_execution_ledger: bool,
    adjustment_changes_explained: bool,
) -> bool:
    """One fail-closed definition for every readiness named ``unbiased``.

    Descriptive adjusted-return availability is intentionally excluded.  A
    legacy cache, low-grade suspension declaration, or a trusted hfq tape by
    itself can never satisfy this predicate.
    """

    return all(
        (
            exact_pit_binding,
            member_session_complete,
            bitemporal_availability_verified,
            trading_status_authoritative,
            corporate_action_validated,
            trusted_research_ledger,
            trusted_execution_ledger,
            adjustment_changes_explained,
        )
    )


def _readiness_gaps(limitations: Iterable[str]) -> list[dict[str, str]]:
    actions = {
        "canonical_runtime_binding_missing": "create an exact immutable PIT/runtime binding",
        "bitemporal_source_availability_not_verified": "import v2 evidence with available_at and bind using as_known_at",
        "corporate_action_authoritative_evidence_missing": "complete licensed artifact governance for event and no-event coverage",
        "trading_status_authoritative_evidence_missing": "bind an authoritative daily trading-status artifact",
        "research_price_source_evidence_insufficient": "approve a research-adjusted source artifact without changing its declared trust level",
        "raw_execution_source_evidence_insufficient": "approve a licensed raw execution source artifact",
        "corporate_action_runtime_application_missing": "implement split/dividend portfolio-state application before execution use",
    }
    return [
        {
            "code": code,
            "remediation": actions.get(
                code,
                "supply the missing governed evidence and rerun validation",
            ),
        }
        for code in dict.fromkeys(limitations)
    ]


def _ohlc_geometry(item: Mapping[str, Any]) -> tuple[float, ...]:
    close = float(item["close"])
    return tuple(float(item[field]) / close for field in _OHLC_FIELDS)


def _geometry_changed(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    return any(
        not math.isclose(
            left_value,
            right_value,
            rel_tol=_CROSS_SCOPE_REL_TOLERANCE,
            abs_tol=0.0,
        )
        for left_value, right_value in zip(
            _ohlc_geometry(left),
            _ohlc_geometry(right),
            strict=True,
        )
    )


def _safe_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text) or ".." in text:
        raise PriceLedgerValidationError(f"{field} is invalid")
    return text


def _iso_date(
    value: Any,
    field: str,
    *,
    allow_future: bool = False,
) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PriceLedgerValidationError(
            f"{field} must be YYYY-MM-DD"
        ) from exc
    if not allow_future and parsed > datetime.now(UTC).date():
        raise PriceLedgerValidationError(f"{field} cannot be in the future")
    return parsed.isoformat()


def _utc_timestamp(value: Any, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PriceLedgerValidationError(
            f"{field} must be an RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriceLedgerValidationError(f"{field} must include a timezone")
    normalized = parsed.astimezone(UTC)
    if normalized > datetime.now(UTC) + timedelta(minutes=5):
        raise PriceLedgerValidationError(f"{field} cannot be in the future")
    return normalized.isoformat().replace("+00:00", "Z")


def _security_code(value: Any) -> str:
    text = str(value or "").strip()
    if not _SECURITY_CODE.fullmatch(text):
        raise PriceLedgerValidationError(
            "security_code must contain exactly six digits"
        )
    return text


def _finite_number(value: Any, field: str, *, positive: bool) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PriceLedgerValidationError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise PriceLedgerValidationError(f"{field} must be finite")
    if positive and number <= 0:
        raise PriceLedgerValidationError(f"{field} must be positive")
    if not positive and number < 0:
        raise PriceLedgerValidationError(f"{field} must be non-negative")
    return number


def _normalize_source(
    source: Mapping[str, Any],
    *,
    expected_adjustment: str | None,
    field: str,
) -> dict[str, str]:
    adjustment = str(source.get("adjustment") or "").strip().lower()
    if expected_adjustment is not None and adjustment != expected_adjustment:
        raise PriceLedgerValidationError(
            f"{field}.adjustment must be {expected_adjustment}"
        )
    level = _safe_id(source.get("evidence_level"), f"{field}.evidence_level")
    if level not in _EVIDENCE_LEVELS:
        raise PriceLedgerValidationError(
            f"{field}.evidence_level is unsupported"
        )
    digest = str(source.get("content_sha256") or "").strip().lower()
    if not _SHA256.fullmatch(digest):
        raise PriceLedgerValidationError(
            f"{field}.content_sha256 must be a lowercase SHA-256"
        )
    normalized = {
        "provider": _safe_id(source.get("provider"), f"{field}.provider"),
        "dataset": _safe_id(source.get("dataset"), f"{field}.dataset"),
        "version": _safe_id(source.get("version"), f"{field}.version"),
        "adjustment": adjustment,
        "evidence_level": level,
        "retrieved_at": _utc_timestamp(
            source.get("retrieved_at"),
            f"{field}.retrieved_at",
        ),
        "content_sha256": digest,
    }
    if source.get("available_at") is not None:
        normalized["available_at"] = _utc_timestamp(
            source.get("available_at"),
            f"{field}.available_at",
        )
        if normalized["available_at"] > normalized["retrieved_at"]:
            raise PriceLedgerValidationError(
                f"{field}.available_at must not exceed retrieved_at"
            )
    return normalized


def _normalize_price(
    record: Mapping[str, Any],
    *,
    coverage_from: str,
    coverage_to: str,
) -> dict[str, Any]:
    code = _security_code(record.get("security_code"))
    trading_date = _iso_date(record.get("date"), "price.date")
    if not coverage_from <= trading_date <= coverage_to:
        raise PriceLedgerValidationError(
            "price date exceeds declared coverage"
        )
    normalized = {
        "security_code": code,
        "date": trading_date,
        **{
            field: _finite_number(
                record.get(field),
                f"price.{field}",
                positive=True,
            )
            for field in _OHLC_FIELDS
        },
        "volume": _finite_number(
            record.get("volume"),
            "price.volume",
            positive=False,
        ),
    }
    if (
        normalized["low"] > normalized["high"]
        or normalized["low"] > normalized["open"]
        or normalized["low"] > normalized["close"]
        or normalized["high"] < normalized["open"]
        or normalized["high"] < normalized["close"]
    ):
        raise PriceLedgerValidationError("price OHLC relationship is invalid")
    return normalized


def _normalize_prices(
    records: Iterable[Mapping[str, Any]],
    *,
    coverage_from: str,
    coverage_to: str,
    field: str,
) -> list[dict[str, Any]]:
    normalized = [
        _normalize_price(
            record,
            coverage_from=coverage_from,
            coverage_to=coverage_to,
        )
        for record in records
    ]
    if not normalized:
        raise PriceLedgerValidationError(f"{field} cannot be empty")
    normalized.sort(key=lambda item: (item["security_code"], item["date"]))
    identities = [
        (item["security_code"], item["date"]) for item in normalized
    ]
    if len(identities) != len(set(identities)):
        raise PriceLedgerValidationError(f"{field} contains duplicate rows")
    dates = [item["date"] for item in normalized]
    if min(dates) != coverage_from or max(dates) != coverage_to:
        raise PriceLedgerValidationError(
            f"{field} does not match declared coverage boundaries"
        )
    return normalized


def _normalize_actions(
    records: Iterable[Mapping[str, Any]],
    *,
    coverage_from: str,
    coverage_to: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for record in records:
        action_type = str(record.get("action_type") or "").strip().lower()
        if action_type not in _ACTION_TYPES:
            raise PriceLedgerValidationError(
                "corporate action type is unsupported"
            )
        effective_date = _iso_date(
            record.get("effective_date"),
            "corporate_action.effective_date",
        )
        if not coverage_from <= effective_date <= coverage_to:
            raise PriceLedgerValidationError(
                "corporate action exceeds declared coverage"
            )
        multiplier_value = record.get("adjustment_multiplier")
        multiplier = (
            None
            if multiplier_value is None
            else _finite_number(
                multiplier_value,
                "corporate_action.adjustment_multiplier",
                positive=True,
            )
        )
        normalized.append(
            {
                "security_code": _security_code(
                    record.get("security_code")
                ),
                "effective_date": effective_date,
                "action_type": action_type,
                "adjustment_multiplier": multiplier,
                "reference_id": _safe_id(
                    record.get("reference_id"),
                    "corporate_action.reference_id",
                ),
            }
        )
    normalized.sort(
        key=lambda item: (
            item["security_code"],
            item["effective_date"],
            item["action_type"],
            item["reference_id"],
        )
    )
    identities = [
        (
            item["security_code"],
            item["effective_date"],
            item["action_type"],
            item["reference_id"],
        )
        for item in normalized
    ]
    if len(identities) != len(set(identities)):
        raise PriceLedgerValidationError(
            "corporate actions contain duplicate identities"
        )
    return normalized


def _build_adjustment_audit(
    raw_prices: list[dict[str, Any]],
    research_prices: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    *,
    corporate_action_authoritative: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_by_identity = {
        (item["security_code"], item["date"]): item for item in raw_prices
    }
    research_by_identity = {
        (item["security_code"], item["date"]): item
        for item in research_prices
    }
    if set(raw_by_identity) != set(research_by_identity):
        raise PriceLedgerValidationError(
            "raw and hfq ledgers must contain the same code/date identities"
        )
    action_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for action in actions:
        action_by_identity[
            (action["security_code"], action["effective_date"])
        ].append(action)

    factors: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    previous_by_code: dict[str, tuple[str, float]] = {}
    for identity in sorted(raw_by_identity):
        raw = raw_by_identity[identity]
        research = research_by_identity[identity]
        ratios = [research[field] / raw[field] for field in _OHLC_FIELDS]
        factor = ratios[-1]
        max_delta = (max(ratios) - min(ratios)) / factor
        if (
            not math.isfinite(factor)
            or factor <= 0
            or not math.isfinite(max_delta)
            or max_delta > _RATIO_REL_TOLERANCE
        ):
            raise PriceLedgerValidationError(
                "raw/hfq OHLC imply inconsistent adjustment factors"
            )
        code, trading_date = identity
        previous = previous_by_code.get(code)
        factor_change: float | None = None
        if previous is not None:
            factor_change = factor / previous[1] - 1.0
            if abs(factor_change) > _FACTOR_CHANGE_TOLERANCE:
                matching_actions = action_by_identity.get(identity, [])
                declared = [
                    item["adjustment_multiplier"]
                    for item in matching_actions
                    if item["adjustment_multiplier"] is not None
                ]
                declared_consistent = bool(declared) and any(
                    math.isclose(
                        factor / previous[1],
                        value,
                        rel_tol=_DECLARED_FACTOR_TOLERANCE,
                        abs_tol=0.0,
                    )
                    for value in declared
                )
                explained = bool(
                    matching_actions
                    and corporate_action_authoritative
                    and declared_consistent
                )
                abnormal = abs(factor_change) > _ABNORMAL_FACTOR_JUMP
                changes.append(
                    {
                        "security_code": code,
                        "date": trading_date,
                        "previous_date": previous[0],
                        "factor_change": factor_change,
                        "abnormal": abnormal,
                        "corporate_action_count": len(matching_actions),
                        "authoritatively_explained": explained,
                        "declared_multiplier_consistent": declared_consistent,
                    }
                )
                if abnormal and not explained:
                    raise PriceLedgerValidationError(
                        "abnormal adjustment-factor jump lacks authoritative "
                        "corporate-action evidence: "
                        f"{code}@{trading_date} factor_change={factor_change:.6f}"
                    )
        factors.append(
            {
                "security_code": code,
                "date": trading_date,
                "implied_factor": factor,
                "max_ohlc_ratio_delta": max_delta,
                "factor_change": factor_change,
            }
        )
        previous_by_code[code] = (trading_date, factor)

    unmatched_actions = sorted(
        {
            f"{item['security_code']}:{item['effective_date']}"
            for item in actions
            if (
                item["security_code"],
                item["effective_date"],
            )
            not in {
                (change["security_code"], change["date"])
                for change in changes
            }
        }
    )
    unexplained_changes = [
        item for item in changes if not item["authoritatively_explained"]
    ]
    audit = {
        "schema_version": ADJUSTMENT_AUDIT_SCHEMA_VERSION,
        "ratio_relative_tolerance": _RATIO_REL_TOLERANCE,
        "factor_change_tolerance": _FACTOR_CHANGE_TOLERANCE,
        "abnormal_factor_jump": _ABNORMAL_FACTOR_JUMP,
        "factor_row_count": len(factors),
        "factor_change_count": len(changes),
        "unexplained_factor_change_count": len(unexplained_changes),
        "abnormal_factor_change_count": sum(
            bool(item["abnormal"]) for item in changes
        ),
        "corporate_action_authoritative": corporate_action_authoritative,
        "factor_changes": changes,
        "unmatched_corporate_actions": unmatched_actions[:100],
        "limitations": (
            []
            if corporate_action_authoritative
            else ["corporate_action_authoritative_evidence_missing"]
        ),
    }
    return factors, audit


def _intervals_cover(
    intervals: Iterable[tuple[str, str]],
    required_from: str,
    required_to: str,
) -> bool:
    cursor = date.fromisoformat(required_from)
    end = date.fromisoformat(required_to)
    for start_text, end_text in sorted(intervals):
        start = date.fromisoformat(start_text)
        interval_end = date.fromisoformat(end_text)
        if interval_end < cursor:
            continue
        if start > cursor:
            return False
        cursor = interval_end + timedelta(days=1)
        if cursor > end:
            return True
    return cursor > end


class PriceLedgerStore:
    """Append-only dual-price store with verified, path-free read results."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        initialize: bool = False,
    ) -> None:
        self.path = path or settings.abs_path(settings.EXPERIMENT_DB)
        if initialize:
            self.initialize_schema()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            if not self.path.exists():
                raise PriceLedgerValidationError(
                    "price_ledger_store_uninitialized"
                )
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=15,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(PRICE_LEDGER_SCHEMA_SQL)
            self._migrate_canonical_identity_schema(connection)

    @staticmethod
    def _migrate_canonical_identity_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """Upgrade only an empty legacy ledger; populated stores need review."""

        required_columns = {
            "price_ledger_batches": {
                "available_at": "TEXT",
                "ingested_at": "TEXT",
                "revision": "INTEGER",
                "supersedes_batch_id": "TEXT",
            },
            "price_ledger_prices": {
                "source_dataset": "TEXT NOT NULL DEFAULT ''",
                "effective_at": "TEXT",
                "available_at": "TEXT",
                "ingested_at": "TEXT",
                "revision": "INTEGER",
            },
            "price_ledger_adjustment_factors": {
                "raw_source_dataset": "TEXT NOT NULL DEFAULT ''",
                "research_source_dataset": "TEXT NOT NULL DEFAULT ''",
            },
            "price_ledger_corporate_actions": {
                "source_dataset": "TEXT NOT NULL DEFAULT ''",
            },
            "price_ledger_runtime_bindings": {
                "as_known_at": "TEXT",
                "bitemporal_evidence_sha256": "TEXT",
                "price_role_usage_json": "TEXT",
            },
        }
        missing: dict[str, dict[str, str]] = {}
        for table, definitions in required_columns.items():
            present = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            absent = {
                name: definition
                for name, definition in definitions.items()
                if name not in present
            }
            if absent:
                missing[table] = absent
        if missing:
            canonical_identity_tables = {
                "price_ledger_prices",
                "price_ledger_adjustment_factors",
                "price_ledger_corporate_actions",
            }
            populated = [
                table
                for table in missing
                if table in canonical_identity_tables
                and any(
                    name.endswith("source_dataset")
                    for name in missing[table]
                )
                if connection.execute(
                    f"SELECT 1 FROM {table} LIMIT 1"
                ).fetchone()
                is not None
            ]
            if populated:
                raise PriceLedgerIntegrityError(
                    "legacy price ledger canonical identity requires a "
                    "controlled evidence migration"
                )
            for table, definitions in missing.items():
                for name, definition in definitions.items():
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

        connection.executescript(
            """
            DROP INDEX IF EXISTS uq_price_ledger_price_identity;
            CREATE UNIQUE INDEX uq_price_ledger_price_identity
            ON price_ledger_prices(
                scope_id, security_code, trading_date, source_provider,
                source_dataset, source_version, adjustment
            );
            DROP INDEX IF EXISTS uq_price_ledger_factor_identity;
            CREATE UNIQUE INDEX uq_price_ledger_factor_identity
            ON price_ledger_adjustment_factors(
                scope_id, security_code, trading_date,
                research_source_provider,
                research_source_dataset, research_source_version, adjustment
            );
            DROP INDEX IF EXISTS uq_price_ledger_action_identity;
            CREATE UNIQUE INDEX uq_price_ledger_action_identity
            ON price_ledger_corporate_actions(
                scope_id, security_code, effective_date, source_provider,
                source_dataset, source_version, action_type, reference_id
            );
            """
        )

    @staticmethod
    def _tables_exist(connection: sqlite3.Connection) -> bool:
        rows = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name IN (
                'price_ledger_batches',
                'price_ledger_prices',
                'price_ledger_adjustment_factors',
                'price_ledger_corporate_actions'
            )
            """
        ).fetchall()
        return len(rows) == 4

    def import_batch(
        self,
        *,
        schema_version: str,
        scope_id: str,
        coverage_from: str,
        coverage_to: str,
        raw_source: Mapping[str, Any],
        research_source: Mapping[str, Any],
        corporate_action_source: Mapping[str, Any] | None,
        raw_prices: Iterable[Mapping[str, Any]],
        research_prices: Iterable[Mapping[str, Any]],
        corporate_actions: Iterable[Mapping[str, Any]],
        imported_by_user_id: int,
        revision: int | None = None,
        supersedes_batch_id: str | None = None,
        _production_release_authorization: (
            _ProductionReleaseAuthorization | None
        ) = None,
    ) -> dict[str, Any]:
        """Validate and atomically append one complete dual-price batch."""

        raw_price_input = [dict(item) for item in raw_prices]
        research_price_input = [dict(item) for item in research_prices]
        corporate_action_input = [dict(item) for item in corporate_actions]
        submitted_document = {
            "schema_version": schema_version,
            "scope_id": scope_id,
            "coverage_from": coverage_from,
            "coverage_to": coverage_to,
            "raw_source": dict(raw_source),
            "research_source": dict(research_source),
            "corporate_action_source": (
                dict(corporate_action_source)
                if corporate_action_source is not None
                else None
            ),
            "raw_prices": raw_price_input,
            "research_prices": research_price_input,
            "corporate_actions": corporate_action_input,
            "revision": revision,
            "supersedes_batch_id": supersedes_batch_id,
        }
        release_authorized = bool(
            _production_release_authorization is not None
            and _production_release_authorization.token
            is _PRODUCTION_RELEASE_TOKEN
            and _production_release_authorization.operation == "import_batch"
            and _production_release_authorization.document_sha256
            == _digest(submitted_document)
            and _SHA256.fullmatch(
                _production_release_authorization.plan_sha256
            )
            and _SHA256.fullmatch(
                _production_release_authorization.manifest_sha256
            )
        )

        if schema_version not in {
            IMPORT_SCHEMA_VERSION,
            BITEMPORAL_IMPORT_SCHEMA_VERSION,
        }:
            raise PriceLedgerValidationError(
                "unsupported dual-price import schema"
            )
        bitemporal = schema_version == BITEMPORAL_IMPORT_SCHEMA_VERSION
        scope = _safe_id(scope_id, "scope_id")
        start = _iso_date(coverage_from, "coverage_from")
        end = _iso_date(coverage_to, "coverage_to")
        if start > end:
            raise PriceLedgerValidationError(
                "coverage_from must not exceed coverage_to"
            )
        raw_source_value = _normalize_source(
            raw_source,
            expected_adjustment="raw",
            field="raw_source",
        )
        research_source_value = _normalize_source(
            research_source,
            expected_adjustment="hfq",
            field="research_source",
        )
        action_source_value = (
            _normalize_source(
                corporate_action_source,
                expected_adjustment="corporate_action",
                field="corporate_action_source",
            )
            if corporate_action_source is not None
            else None
        )
        normalized_revision: int | None = None
        normalized_predecessor: str | None = None
        batch_available_at: str | None = None
        if bitemporal:
            try:
                normalized_revision = int(revision)
            except (TypeError, ValueError) as exc:
                raise PriceLedgerValidationError(
                    "revision must be a positive integer"
                ) from exc
            if normalized_revision < 1:
                raise PriceLedgerValidationError(
                    "revision must be a positive integer"
                )
            if supersedes_batch_id is not None:
                normalized_predecessor = _safe_id(
                    supersedes_batch_id,
                    "supersedes_batch_id",
                )
            required_sources = [raw_source_value, research_source_value]
            if action_source_value is not None:
                required_sources.append(action_source_value)
            if any("available_at" not in item for item in required_sources):
                raise PriceLedgerValidationError(
                    "bitemporal price sources require available_at"
                )
            batch_available_at = max(
                str(item["available_at"]) for item in required_sources
            )
        unmanaged_sources = [
            item
            for item in (
                raw_source_value,
                research_source_value,
                action_source_value,
            )
            if item is not None
            and item["evidence_level"] != "declared"
        ]
        privileged_sources = [
            item
            for item in (
                raw_source_value,
                research_source_value,
                action_source_value,
            )
            if item is not None
            and item["evidence_level"]
            in {"licensed", "exchange_authoritative"}
        ]
        if unmanaged_sources and not release_authorized:
            raise PriceLedgerValidationError(
                "non-declared price evidence requires managed artifact "
                "governance; that workflow is not available"
            )
        raw_values = _normalize_prices(
            raw_price_input,
            coverage_from=start,
            coverage_to=end,
            field="raw_prices",
        )
        research_values = _normalize_prices(
            research_price_input,
            coverage_from=start,
            coverage_to=end,
            field="research_prices",
        )
        action_values = _normalize_actions(
            corporate_action_input,
            coverage_from=start,
            coverage_to=end,
        )
        if action_values and action_source_value is None:
            raise PriceLedgerValidationError(
                "corporate actions require a corporate_action_source"
            )
        raw_codes = {item["security_code"] for item in raw_values}
        research_codes = {
            item["security_code"] for item in research_values
        }
        if raw_codes != research_codes:
            raise PriceLedgerValidationError(
                "raw and hfq ledgers must cover the same securities"
            )
        action_codes = {
            item["security_code"] for item in action_values
        }
        if action_codes - raw_codes:
            raise PriceLedgerValidationError(
                "corporate actions contain securities outside the price batch"
            )

        action_authoritative = bool(
            action_source_value
            and action_source_value["evidence_level"]
            in _AUTHORITATIVE_ACTION_LEVELS
        )
        factors, audit = _build_adjustment_audit(
            raw_values,
            research_values,
            action_values,
            corporate_action_authoritative=action_authoritative,
        )
        payload = {
            "raw_prices": raw_values,
            "research_prices": research_values,
            "corporate_actions": action_values,
        }
        payload_json = _canonical_json(payload)
        payload_sha256 = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()
        audit_json = _canonical_json(audit)
        audit_sha256 = hashlib.sha256(
            audit_json.encode("utf-8")
        ).hexdigest()
        identity = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "scope_id": scope,
            "coverage_from": start,
            "coverage_to": end,
            "raw_source": raw_source_value,
            "research_source": research_source_value,
            "corporate_action_source": action_source_value,
            "payload_sha256": payload_sha256,
            "audit_sha256": audit_sha256,
        }
        if bitemporal:
            identity["bitemporal"] = {
                "available_at": batch_available_at,
                "revision": normalized_revision,
                "supersedes_batch_id": normalized_predecessor,
            }
        batch_digest = _digest(identity)
        batch_id = "plb_" + batch_digest[:32]
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        ingested_at = created_at if bitemporal else None
        canonical_rows_reused = 0

        self.initialize_schema()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT * FROM price_ledger_batches
                    WHERE batch_digest = ?
                    """,
                    (batch_digest,),
                ).fetchone()
                if existing is not None:
                    self._verify_batch(connection, existing)
                    connection.rollback()
                    return {
                        "batch_id": str(existing["batch_id"]),
                        "batch_digest": batch_digest,
                        "price_row_count": len(raw_values) + len(research_values),
                        "factor_row_count": len(factors),
                        "idempotent": True,
                        "canonical_rows_reused": (
                            len(raw_values) + len(research_values)
                        ),
                        "audit": audit,
                        "bitemporal": bitemporal,
                    }
                if bitemporal and normalized_predecessor is not None:
                    predecessor = connection.execute(
                        "SELECT * FROM price_ledger_batches WHERE batch_id=?",
                        (normalized_predecessor,),
                    ).fetchone()
                    if (
                        predecessor is None
                        or predecessor["scope_id"] != scope
                        or predecessor["revision"] is None
                        or int(predecessor["revision"])
                        >= int(normalized_revision or 0)
                    ):
                        raise PriceLedgerValidationError(
                            "price ledger supersession lineage is invalid"
                        )
                    existing_successor = connection.execute(
                        """
                        SELECT 1 FROM price_ledger_batches
                        WHERE supersedes_batch_id=?
                        """,
                        (normalized_predecessor,),
                    ).fetchone()
                    if existing_successor is not None:
                        raise PriceLedgerConflictError(
                            "price ledger revision already has a successor"
                        )
                existing_batches = self._verified_batches_all_scopes(
                    connection,
                    start=start,
                    end=end,
                )
                incoming_rows = self._incoming_canonical_rows(
                    scope_id=scope,
                    batch_id=batch_id,
                    batch_digest=batch_digest,
                    raw_source=raw_source_value,
                    research_source=research_source_value,
                    raw_prices=raw_values,
                    research_prices=research_values,
                )
                existing_rows = self._canonical_rows(
                    existing_batches,
                    start=start,
                    end=end,
                    security_codes=raw_codes,
                )
                existing_identity_values = {
                    (
                        self._canonical_identity(item),
                        _digest(_price_values(item)),
                    )
                    for item in existing_rows
                }
                canonical_rows_reused = sum(
                    (
                        self._canonical_identity(item),
                        _digest(_price_values(item)),
                    )
                    in existing_identity_values
                    for item in incoming_rows
                )
                cross_scope = self._build_cross_scope_report(
                    [*existing_rows, *incoming_rows],
                    start=start,
                    end=end,
                    # Import decisions must never depend on presentation
                    # truncation. Every conflicting identity in the candidate
                    # range remains available for the fail-closed decision.
                    limit=max(
                        1,
                        len(existing_rows) + len(incoming_rows),
                    ),
                )
                incoming_identities = {
                    self._canonical_identity(item)
                    for item in incoming_rows
                }
                relevant_conflicts = [
                    item
                    for item in cross_scope["conflicts"]
                    if (
                        item["security_code"],
                        item["date"],
                        item["source"]["provider"],
                        item["source"]["dataset"],
                        item["source"]["version"],
                        item["adjustment"],
                    )
                    in incoming_identities
                ]
                if relevant_conflicts:
                    connection.rollback()
                    raise PriceLedgerConflictError(
                        "cross-scope canonical price evidence conflicts with "
                        "an immutable accepted identity",
                        evidence={
                            "schema_version": (
                                CROSS_SCOPE_AUDIT_SCHEMA_VERSION
                            ),
                            "code": "cross_scope_price_conflict",
                            "conflict_identity_count": len(
                                relevant_conflicts
                            ),
                            "conflicts": relevant_conflicts[:20],
                            "truncated": len(relevant_conflicts) > 20,
                        },
                    )
                connection.execute(
                    """
                    INSERT INTO price_ledger_batches (
                        batch_id, schema_version, scope_id, coverage_from,
                        coverage_to, raw_source_json, research_source_json,
                        corporate_action_source_json, payload_json,
                        payload_sha256, audit_json, audit_sha256, batch_digest,
                        imported_by_user_id, created_at, available_at,
                        ingested_at, revision, supersedes_batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        LEDGER_SCHEMA_VERSION,
                        scope,
                        start,
                        end,
                        _canonical_json(raw_source_value),
                        _canonical_json(research_source_value),
                        (
                            _canonical_json(action_source_value)
                            if action_source_value is not None
                            else None
                        ),
                        payload_json,
                        payload_sha256,
                        audit_json,
                        audit_sha256,
                        batch_digest,
                        int(imported_by_user_id),
                        created_at,
                        batch_available_at,
                        ingested_at,
                        normalized_revision,
                        normalized_predecessor,
                    ),
                )
                for role, adjustment, source, rows in (
                    (
                        "raw_execution",
                        "raw",
                        raw_source_value,
                        raw_values,
                    ),
                    (
                        "research_adjusted",
                        "hfq",
                        research_source_value,
                        research_values,
                    ),
                ):
                    for item in rows:
                        row_identity = {
                            "batch_id": batch_id,
                            "scope_id": scope,
                            "price_role": role,
                            "adjustment": adjustment,
                            "source_provider": source["provider"],
                            "source_dataset": source["dataset"],
                            "source_version": source["version"],
                            **item,
                        }
                        if bitemporal:
                            row_identity.update(
                                effective_at=f"{item['date']}T00:00:00Z",
                                available_at=source["available_at"],
                                ingested_at=ingested_at,
                                revision=normalized_revision,
                            )
                        connection.execute(
                            """
                            INSERT INTO price_ledger_prices (
                                batch_id, scope_id, price_role, adjustment,
                                security_code, trading_date, source_provider,
                                source_dataset, source_version, open, high, low,
                                close, volume, row_sha256, effective_at,
                                available_at, ingested_at, revision
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?)
                            """,
                            (
                                batch_id,
                                scope,
                                role,
                                adjustment,
                                item["security_code"],
                                item["date"],
                                source["provider"],
                                source["dataset"],
                                source["version"],
                                item["open"],
                                item["high"],
                                item["low"],
                                item["close"],
                                item["volume"],
                                _digest(row_identity),
                                (
                                    f"{item['date']}T00:00:00Z"
                                    if bitemporal
                                    else None
                                ),
                                source.get("available_at"),
                                ingested_at,
                                normalized_revision,
                            ),
                        )
                for item in factors:
                    row_identity = {
                        "batch_id": batch_id,
                        "scope_id": scope,
                        "raw_source_provider": raw_source_value["provider"],
                        "raw_source_dataset": raw_source_value["dataset"],
                        "raw_source_version": raw_source_value["version"],
                        "research_source_provider": (
                            research_source_value["provider"]
                        ),
                        "research_source_dataset": (
                            research_source_value["dataset"]
                        ),
                        "research_source_version": (
                            research_source_value["version"]
                        ),
                        "adjustment": "hfq_vs_raw",
                        **item,
                    }
                    connection.execute(
                        """
                        INSERT INTO price_ledger_adjustment_factors (
                            batch_id, scope_id, security_code, trading_date,
                            raw_source_provider, raw_source_dataset,
                            raw_source_version, research_source_provider,
                            research_source_dataset, research_source_version,
                            adjustment, implied_factor, max_ohlc_ratio_delta,
                            factor_change, row_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            scope,
                            item["security_code"],
                            item["date"],
                            raw_source_value["provider"],
                            raw_source_value["dataset"],
                            raw_source_value["version"],
                            research_source_value["provider"],
                            research_source_value["dataset"],
                            research_source_value["version"],
                            "hfq_vs_raw",
                            item["implied_factor"],
                            item["max_ohlc_ratio_delta"],
                            item["factor_change"],
                            _digest(row_identity),
                        ),
                    )
                if action_source_value is not None:
                    for item in action_values:
                        row_identity = {
                            "batch_id": batch_id,
                            "scope_id": scope,
                            "source_provider": action_source_value["provider"],
                            "source_dataset": action_source_value["dataset"],
                            "source_version": action_source_value["version"],
                            "source_evidence_level": (
                                action_source_value["evidence_level"]
                            ),
                            **item,
                        }
                        connection.execute(
                            """
                            INSERT INTO price_ledger_corporate_actions (
                                batch_id, scope_id, security_code,
                                effective_date, action_type,
                                adjustment_multiplier, reference_id,
                                source_provider, source_dataset, source_version,
                                source_evidence_level, row_sha256
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                batch_id,
                                scope,
                                item["security_code"],
                                item["effective_date"],
                                item["action_type"],
                                item["adjustment_multiplier"],
                                item["reference_id"],
                                action_source_value["provider"],
                                action_source_value["dataset"],
                                action_source_value["version"],
                                action_source_value["evidence_level"],
                                _digest(row_identity),
                            ),
                        )
                if release_authorized and privileged_sources:
                    assert _production_release_authorization is not None
                    connection.execute(
                        """
                        INSERT INTO price_ledger_batch_governance (
                            batch_id, receipt_id, receipt_sha256,
                            source_identity_sha256
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            "prpkg_"
                            + _production_release_authorization.plan_sha256[:32],
                            _production_release_authorization.plan_sha256,
                            _digest(privileged_sources),
                        ),
                    )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PriceLedgerConflictError(
                "price ledger identity conflicts with immutable evidence"
            ) from exc
        return {
            "batch_id": batch_id,
            "batch_digest": batch_digest,
            "price_row_count": len(raw_values) + len(research_values),
            "factor_row_count": len(factors),
            "idempotent": False,
            "canonical_rows_reused": canonical_rows_reused,
            "audit": audit,
            "bitemporal": bitemporal,
            "available_at": batch_available_at,
            "ingested_at": ingested_at,
            "revision": normalized_revision,
            "supersedes_batch_id": normalized_predecessor,
        }

    @staticmethod
    def _parse_json(value: Any, label: str) -> Any:
        try:
            return json.loads(str(value))
        except (TypeError, json.JSONDecodeError) as exc:
            raise PriceLedgerIntegrityError(
                f"stored {label} is invalid"
            ) from exc

    def _verify_batch(
        self,
        connection: sqlite3.Connection,
        batch: sqlite3.Row,
    ) -> dict[str, Any]:
        payload_json = str(batch["payload_json"])
        audit_json = str(batch["audit_json"])
        if hashlib.sha256(payload_json.encode()).hexdigest() != batch[
            "payload_sha256"
        ]:
            raise PriceLedgerIntegrityError(
                "price ledger payload integrity mismatch"
            )
        if hashlib.sha256(audit_json.encode()).hexdigest() != batch[
            "audit_sha256"
        ]:
            raise PriceLedgerIntegrityError(
                "price ledger audit integrity mismatch"
            )
        raw_source = self._parse_json(batch["raw_source_json"], "raw source")
        research_source = self._parse_json(
            batch["research_source_json"],
            "research source",
        )
        action_source = (
            self._parse_json(
                batch["corporate_action_source_json"],
                "corporate-action source",
            )
            if batch["corporate_action_source_json"] is not None
            else None
        )
        privileged_sources = [
            item
            for item in (raw_source, research_source, action_source)
            if item is not None
            and item.get("evidence_level")
            in {"licensed", "exchange_authoritative"}
        ]
        try:
            governance = connection.execute(
                """
                SELECT * FROM price_ledger_batch_governance
                WHERE batch_id = ?
                """,
                (batch["batch_id"],),
            ).fetchone()
        except sqlite3.Error as exc:
            if privileged_sources:
                raise PriceLedgerIntegrityError(
                    "privileged price governance store is unavailable"
                ) from exc
            governance = None
        if privileged_sources:
            if (
                governance is None
                or not _PRICE_RECEIPT_ID.fullmatch(
                    str(governance["receipt_id"])
                )
                or not _SHA256.fullmatch(
                    str(governance["receipt_sha256"])
                )
                or governance["source_identity_sha256"]
                != _digest(privileged_sources)
            ):
                raise PriceLedgerIntegrityError(
                    "privileged price evidence governance receipt is invalid"
                )
        elif governance is not None:
            raise PriceLedgerIntegrityError(
                "unprivileged price batch has an unexpected governance receipt"
            )
        identity = {
            "schema_version": batch["schema_version"],
            "scope_id": batch["scope_id"],
            "coverage_from": batch["coverage_from"],
            "coverage_to": batch["coverage_to"],
            "raw_source": raw_source,
            "research_source": research_source,
            "corporate_action_source": action_source,
            "payload_sha256": batch["payload_sha256"],
            "audit_sha256": batch["audit_sha256"],
        }
        available_at = _optional_row_value(batch, "available_at")
        if available_at is not None:
            identity["bitemporal"] = {
                "available_at": available_at,
                "revision": _optional_row_value(batch, "revision"),
                "supersedes_batch_id": _optional_row_value(
                    batch, "supersedes_batch_id"
                ),
            }
        if _digest(identity) != batch["batch_digest"]:
            raise PriceLedgerIntegrityError(
                "price ledger batch integrity mismatch"
            )
        payload = self._parse_json(payload_json, "payload")
        audit = self._parse_json(audit_json, "audit")
        price_rows = connection.execute(
            """
            SELECT * FROM price_ledger_prices
            WHERE batch_id = ?
            ORDER BY price_role, security_code, trading_date
            """,
            (batch["batch_id"],),
        ).fetchall()
        reconstructed_prices: dict[str, list[dict[str, Any]]] = {
            "raw_execution": [],
            "research_adjusted": [],
        }
        for row in price_rows:
            item = {
                "security_code": str(row["security_code"]),
                "date": str(row["trading_date"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            row_identity = {
                "batch_id": str(row["batch_id"]),
                "scope_id": str(row["scope_id"]),
                "price_role": str(row["price_role"]),
                "adjustment": str(row["adjustment"]),
                "source_provider": str(row["source_provider"]),
                "source_dataset": str(row["source_dataset"]),
                "source_version": str(row["source_version"]),
                **item,
            }
            effective_at = _optional_row_value(row, "effective_at")
            if effective_at is not None:
                row_identity.update(
                    effective_at=str(effective_at),
                    available_at=str(_optional_row_value(row, "available_at")),
                    ingested_at=str(_optional_row_value(row, "ingested_at")),
                    revision=int(_optional_row_value(row, "revision")),
                )
            if _digest(row_identity) != row["row_sha256"]:
                raise PriceLedgerIntegrityError(
                    "price ledger row integrity mismatch"
                )
            reconstructed_prices[str(row["price_role"])].append(item)
        if (
            reconstructed_prices["raw_execution"]
            != payload.get("raw_prices")
            or reconstructed_prices["research_adjusted"]
            != payload.get("research_prices")
        ):
            raise PriceLedgerIntegrityError(
                "price ledger rows do not match payload"
            )
        action_rows = connection.execute(
            """
            SELECT * FROM price_ledger_corporate_actions
            WHERE batch_id = ?
            ORDER BY security_code, effective_date, action_type, reference_id
            """,
            (batch["batch_id"],),
        ).fetchall()
        reconstructed_actions: list[dict[str, Any]] = []
        for row in action_rows:
            item = {
                "security_code": str(row["security_code"]),
                "effective_date": str(row["effective_date"]),
                "action_type": str(row["action_type"]),
                "adjustment_multiplier": (
                    float(row["adjustment_multiplier"])
                    if row["adjustment_multiplier"] is not None
                    else None
                ),
                "reference_id": str(row["reference_id"]),
            }
            row_identity = {
                "batch_id": str(row["batch_id"]),
                "scope_id": str(row["scope_id"]),
                "source_provider": str(row["source_provider"]),
                "source_dataset": str(row["source_dataset"]),
                "source_version": str(row["source_version"]),
                "source_evidence_level": str(row["source_evidence_level"]),
                **item,
            }
            if _digest(row_identity) != row["row_sha256"]:
                raise PriceLedgerIntegrityError(
                    "corporate-action row integrity mismatch"
                )
            reconstructed_actions.append(item)
        if reconstructed_actions != payload.get("corporate_actions"):
            raise PriceLedgerIntegrityError(
                "corporate-action rows do not match payload"
            )
        factor_rows = connection.execute(
            """
            SELECT * FROM price_ledger_adjustment_factors
            WHERE batch_id = ?
            ORDER BY security_code, trading_date
            """,
            (batch["batch_id"],),
        ).fetchall()
        reconstructed_factors: list[dict[str, Any]] = []
        for row in factor_rows:
            item = {
                "security_code": str(row["security_code"]),
                "date": str(row["trading_date"]),
                "implied_factor": float(row["implied_factor"]),
                "max_ohlc_ratio_delta": float(
                    row["max_ohlc_ratio_delta"]
                ),
                "factor_change": (
                    float(row["factor_change"])
                    if row["factor_change"] is not None
                    else None
                ),
            }
            row_identity = {
                "batch_id": str(row["batch_id"]),
                "scope_id": str(row["scope_id"]),
                "raw_source_provider": str(row["raw_source_provider"]),
                "raw_source_dataset": str(row["raw_source_dataset"]),
                "raw_source_version": str(row["raw_source_version"]),
                "research_source_provider": str(
                    row["research_source_provider"]
                ),
                "research_source_dataset": str(
                    row["research_source_dataset"]
                ),
                "research_source_version": str(
                    row["research_source_version"]
                ),
                "adjustment": str(row["adjustment"]),
                **item,
            }
            if _digest(row_identity) != row["row_sha256"]:
                raise PriceLedgerIntegrityError(
                    "price adjustment row integrity mismatch"
                )
            reconstructed_factors.append(item)
        recomputed_factors, recomputed_audit = _build_adjustment_audit(
            list(payload.get("raw_prices") or []),
            list(payload.get("research_prices") or []),
            list(payload.get("corporate_actions") or []),
            corporate_action_authoritative=bool(
                action_source
                and action_source.get("evidence_level")
                in _AUTHORITATIVE_ACTION_LEVELS
            ),
        )
        if reconstructed_factors != recomputed_factors:
            raise PriceLedgerIntegrityError(
                "price adjustment coverage changed"
            )
        if audit != recomputed_audit:
            raise PriceLedgerIntegrityError(
                "price adjustment audit no longer matches price evidence"
            )
        return {
            "batch_id": str(batch["batch_id"]),
            "batch_digest": str(batch["batch_digest"]),
            "scope_id": str(batch["scope_id"]),
            "coverage_from": str(batch["coverage_from"]),
            "coverage_to": str(batch["coverage_to"]),
            "raw_source": raw_source,
            "research_source": research_source,
            "corporate_action_source": action_source,
            "audit": audit,
            "payload": payload,
            "bitemporal": {
                "verified": bool(
                    _optional_row_value(batch, "available_at") is not None
                    and _optional_row_value(batch, "ingested_at") is not None
                    and _optional_row_value(batch, "revision") is not None
                ),
                "available_at": _optional_row_value(batch, "available_at"),
                "ingested_at": _optional_row_value(batch, "ingested_at"),
                "revision": _optional_row_value(batch, "revision"),
                "supersedes_batch_id": _optional_row_value(
                    batch, "supersedes_batch_id"
                ),
            },
        }

    def _verified_batches(
        self,
        connection: sqlite3.Connection,
        *,
        scope_id: str,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM price_ledger_batches
            WHERE scope_id = ?
              AND coverage_from <= ?
              AND coverage_to >= ?
            ORDER BY coverage_from, coverage_to, batch_id
            """,
            (scope_id, end, start),
        ).fetchall()
        return [self._verify_batch(connection, row) for row in rows]

    def _verified_batches_all_scopes(
        self,
        connection: sqlite3.Connection,
        *,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM price_ledger_batches
            WHERE coverage_from <= ?
              AND coverage_to >= ?
            ORDER BY scope_id, coverage_from, coverage_to, batch_id
            """,
            (end, start),
        ).fetchall()
        return [self._verify_batch(connection, row) for row in rows]

    @staticmethod
    def _canonical_rows(
        batches: Iterable[Mapping[str, Any]],
        *,
        start: str,
        end: str,
        security_codes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for batch in batches:
            for role, payload_key, source_key, adjustment in (
                (
                    "raw_execution",
                    "raw_prices",
                    "raw_source",
                    "raw",
                ),
                (
                    "research_adjusted",
                    "research_prices",
                    "research_source",
                    "hfq",
                ),
            ):
                source = batch[source_key]
                for item in batch["payload"][payload_key]:
                    if not start <= item["date"] <= end:
                        continue
                    if (
                        security_codes
                        and item["security_code"] not in security_codes
                    ):
                        continue
                    rows.append(
                        {
                            "scope_id": str(batch["scope_id"]),
                            "batch_id": str(batch["batch_id"]),
                            "batch_digest": str(batch["batch_digest"]),
                            "price_role": role,
                            "adjustment": adjustment,
                            "security_code": str(item["security_code"]),
                            "date": str(item["date"]),
                            "source": {
                                "provider": str(source["provider"]),
                                "dataset": str(source["dataset"]),
                                "version": str(source["version"]),
                            },
                            **_price_values(item),
                        }
                    )
        return rows

    @staticmethod
    def _canonical_identity(
        row: Mapping[str, Any],
    ) -> tuple[str, str, str, str, str, str]:
        source = row["source"]
        return (
            str(row["security_code"]),
            str(row["date"]),
            str(source["provider"]),
            str(source["dataset"]),
            str(source["version"]),
            str(row["adjustment"]),
        )

    @staticmethod
    def _stream_identity(
        row: Mapping[str, Any],
    ) -> tuple[str, str, str, str, str]:
        identity = PriceLedgerStore._canonical_identity(row)
        return (
            identity[0],
            identity[2],
            identity[3],
            identity[4],
            identity[5],
        )

    @classmethod
    def _build_cross_scope_report(
        cls,
        rows: Iterable[Mapping[str, Any]],
        *,
        start: str,
        end: str,
        limit: int,
    ) -> dict[str, Any]:
        materialized = [dict(row) for row in rows]
        grouped: dict[
            tuple[str, str, str, str, str, str],
            list[dict[str, Any]],
        ] = defaultdict(list)
        for row in materialized:
            grouped[cls._canonical_identity(row)].append(row)

        return_conflicts: set[
            tuple[tuple[str, str, str, str, str, str], str, str]
        ] = set()
        stream_rows: dict[
            tuple[str, str, str, str, str],
            dict[str, dict[str, list[dict[str, Any]]]],
        ] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for row in materialized:
            stream_rows[cls._stream_identity(row)][str(row["scope_id"])][
                str(row["date"])
            ].append(row)
        for by_scope in stream_rows.values():
            scopes = sorted(by_scope)
            for left_index, left_scope in enumerate(scopes):
                for right_scope in scopes[left_index + 1 :]:
                    common_dates = sorted(
                        set(by_scope[left_scope])
                        & set(by_scope[right_scope])
                    )
                    for previous_date, current_date in zip(
                        common_dates,
                        common_dates[1:],
                    ):
                        left_previous = by_scope[left_scope][previous_date][0]
                        left_current = by_scope[left_scope][current_date][0]
                        right_previous = by_scope[right_scope][previous_date][0]
                        right_current = by_scope[right_scope][current_date][0]
                        left_return = (
                            float(left_current["close"])
                            / float(left_previous["close"])
                        )
                        right_return = (
                            float(right_current["close"])
                            / float(right_previous["close"])
                        )
                        if not math.isclose(
                            left_return,
                            right_return,
                            rel_tol=_CROSS_SCOPE_REL_TOLERANCE,
                            abs_tol=0.0,
                        ):
                            return_conflicts.add(
                                (
                                    cls._canonical_identity(left_current),
                                    left_scope,
                                    right_scope,
                                )
                            )

        conflicts: list[dict[str, Any]] = []
        for identity, variants in sorted(grouped.items()):
            reference = variants[0]
            changed_fields = sorted(
                {
                    field
                    for variant in variants[1:]
                    for field in _different_price_fields(
                        reference,
                        variant,
                    )
                }
            )
            if not changed_fields:
                continue
            scopes = sorted({str(item["scope_id"]) for item in variants})
            classifications = ["absolute_price_conflict"]
            if "volume" in changed_fields:
                classifications.append("volume_conflict")
            geometry_conflict = any(
                _geometry_changed(reference, variant)
                for variant in variants[1:]
            )
            if geometry_conflict:
                classifications.append("ohlc_geometry_conflict")
            if any(
                conflict_identity == identity
                for conflict_identity, _, _ in return_conflicts
            ):
                classifications.append("return_conflict")
            if (
                identity[-1] == "hfq"
                and not geometry_conflict
                and "volume" not in changed_fields
                and "return_conflict" not in classifications
            ):
                classifications.append("hfq_constant_anchor_conflict")
            conflicts.append(
                {
                    "security_code": identity[0],
                    "date": identity[1],
                    "source": {
                        "provider": identity[2],
                        "dataset": identity[3],
                        "version": identity[4],
                    },
                    "adjustment": identity[5],
                    "anchor_semantics": _anchor_semantics(identity[5]),
                    "scope_ids": scopes,
                    "fields": changed_fields,
                    "classifications": classifications,
                    "variant_digests": sorted(
                        {
                            _digest(
                                {
                                    "scope_id": variant["scope_id"],
                                    "values": _price_values(variant),
                                }
                            )
                            for variant in variants
                        }
                    ),
                }
            )

        all_scopes = sorted(
            {str(item["scope_id"]) for item in materialized}
        )
        conflicts.sort(
            key=lambda item: (
                item["date"],
                item["security_code"],
                item["source"]["provider"],
                item["source"]["dataset"],
                item["source"]["version"],
                item["adjustment"],
            )
        )
        truncated = len(conflicts) > limit
        rendered_conflicts = conflicts[:limit]
        identity_summary = [
            {
                "identity": identity,
                "value_digests": sorted(
                    {_digest(_price_values(item)) for item in variants}
                ),
            }
            for identity, variants in sorted(grouped.items())
        ]
        return {
            "schema_version": CROSS_SCOPE_AUDIT_SCHEMA_VERSION,
            "required_start": start,
            "required_end": end,
            "ready": not conflicts,
            "checked_scope_count": len(all_scopes),
            "checked_row_count": len(materialized),
            "canonical_identity_count": len(grouped),
            "conflict_identity_count": len(conflicts),
            "scope_ids": all_scopes,
            "canonical_evidence_sha256": _digest(identity_summary),
            "conflicts": rendered_conflicts,
            "truncated": truncated,
            "limitations": (
                []
                if not conflicts
                else [
                    "legacy_cross_scope_price_conflicts_present",
                    "audit_is_read_only_no_legacy_cache_rewrite",
                ]
            ),
        }

    @staticmethod
    def _incoming_canonical_rows(
        *,
        scope_id: str,
        batch_id: str,
        batch_digest: str,
        raw_source: Mapping[str, Any],
        research_source: Mapping[str, Any],
        raw_prices: Iterable[Mapping[str, Any]],
        research_prices: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for role, adjustment, source, prices in (
            ("raw_execution", "raw", raw_source, raw_prices),
            (
                "research_adjusted",
                "hfq",
                research_source,
                research_prices,
            ),
        ):
            for item in prices:
                rows.append(
                    {
                        "scope_id": scope_id,
                        "batch_id": batch_id,
                        "batch_digest": batch_digest,
                        "price_role": role,
                        "adjustment": adjustment,
                        "security_code": str(item["security_code"]),
                        "date": str(item["date"]),
                        "source": {
                            "provider": str(source["provider"]),
                            "dataset": str(source["dataset"]),
                            "version": str(source["version"]),
                        },
                        **_price_values(item),
                    }
                )
        return rows

    def _verified_batches_by_ids(
        self,
        connection: sqlite3.Connection,
        batch_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        normalized = sorted({_safe_id(item, "batch_id") for item in batch_ids})
        if not normalized:
            raise PriceLedgerValidationError(
                "runtime binding requires at least one price batch"
            )
        placeholders = ",".join("?" for _item in normalized)
        rows = connection.execute(
            f"""
            SELECT * FROM price_ledger_batches
            WHERE batch_id IN ({placeholders})
            ORDER BY batch_id
            """,
            normalized,
        ).fetchall()
        if len(rows) != len(normalized):
            raise PriceLedgerValidationError(
                "runtime binding references an unknown price batch"
            )
        return [self._verify_batch(connection, row) for row in rows]

    @staticmethod
    def _runtime_canonical_evidence(
        batches: Iterable[Mapping[str, Any]],
        *,
        start: str,
        end: str,
        security_codes: set[str],
    ) -> tuple[list[dict[str, Any]], str]:
        materialized = list(batches)
        rows = PriceLedgerStore._canonical_rows(
            materialized,
            start=start,
            end=end,
            security_codes=security_codes,
        )
        observations: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            identity = (
                str(row["price_role"]),
                str(row["security_code"]),
                str(row["date"]),
            )
            if identity in observations:
                raise PriceLedgerValidationError(
                    "runtime binding has ambiguous price role observations"
                )
            observations[identity] = row
        evidence = [
            {
                "batch_id": str(item["batch_id"]),
                "batch_digest": str(item["batch_digest"]),
            }
            for item in sorted(
                materialized,
                key=lambda item: str(item["batch_id"]),
            )
        ]
        evidence.extend(
            {
                "role": identity[0],
                "security_code": identity[1],
                "date": identity[2],
                "canonical_identity": list(
                    PriceLedgerStore._canonical_identity(row)
                ),
                "values_sha256": _digest(_price_values(row)),
            }
            for identity, row in sorted(observations.items())
        )
        return rows, _digest(evidence)

    @staticmethod
    def _normalize_runtime_timeline(
        *,
        scope_id: str,
        timeline_identity: Mapping[str, Any],
        trading_dates: Iterable[str],
    ) -> tuple[dict[str, Any], tuple[str, ...], Any]:
        from backend.data.point_in_time_universe import timeline_from_identity

        dates = tuple(
            _iso_date(item, "trading_date", allow_future=True)
            for item in trading_dates
        )
        if not dates or tuple(sorted(set(dates))) != dates:
            raise PriceLedgerValidationError(
                "runtime binding trading dates are invalid"
            )
        identity = json.loads(_canonical_json(dict(timeline_identity)))
        try:
            timeline = timeline_from_identity(
                identity,
                trading_dates=dates,
            )
        except Exception as exc:
            raise PriceLedgerValidationError(
                "runtime binding point-in-time timeline is invalid"
            ) from exc
        if timeline.pool_id != scope_id:
            raise PriceLedgerValidationError(
                "runtime binding scope does not match the timeline"
            )
        return identity, dates, timeline

    def import_corporate_action_evidence(
        self,
        *,
        scope_id: str,
        security_code: str,
        evidence_kind: Literal["event", "confirmed_no_event"],
        effective_at: str,
        effective_to: str | None,
        available_at: str,
        revision: int,
        supersedes_evidence_id: str | None,
        action_type: str | None,
        adjustment_multiplier: float | None,
        reference_id: str,
        source: Mapping[str, Any],
        imported_by_user_id: int,
    ) -> dict[str, Any]:
        """Append event or explicit no-event evidence without trust promotion."""

        scope = _safe_id(scope_id, "scope_id")
        code = _security_code(security_code)
        if evidence_kind not in {"event", "confirmed_no_event"}:
            raise PriceLedgerValidationError(
                "corporate-action evidence kind is invalid"
            )
        start = _utc_timestamp(effective_at, "effective_at")
        end = _utc_timestamp(
            effective_to if effective_to is not None else effective_at,
            "effective_to",
        )
        if start > end:
            raise PriceLedgerValidationError(
                "effective_at must not exceed effective_to"
            )
        known = _utc_timestamp(available_at, "available_at")
        try:
            normalized_revision = int(revision)
        except (TypeError, ValueError) as exc:
            raise PriceLedgerValidationError(
                "revision must be a positive integer"
            ) from exc
        if normalized_revision < 1:
            raise PriceLedgerValidationError(
                "revision must be a positive integer"
            )
        predecessor = (
            _safe_id(supersedes_evidence_id, "supersedes_evidence_id")
            if supersedes_evidence_id is not None
            else None
        )
        normalized_source = _normalize_source(
            source,
            expected_adjustment="corporate_action",
            field="source",
        )
        if normalized_source["evidence_level"] != "declared":
            raise PriceLedgerValidationError(
                "non-declared corporate-action evidence requires managed "
                "artifact governance"
            )
        normalized_action: str | None = None
        normalized_multiplier: float | None = None
        if evidence_kind == "event":
            normalized_action = str(action_type or "").strip().lower()
            if normalized_action not in _ACTION_TYPES:
                raise PriceLedgerValidationError(
                    "corporate action type is unsupported"
                )
            normalized_multiplier = (
                _finite_number(
                    adjustment_multiplier,
                    "adjustment_multiplier",
                    positive=True,
                )
                if adjustment_multiplier is not None
                else None
            )
            if start != end:
                raise PriceLedgerValidationError(
                    "event evidence must have one effective instant"
                )
        elif action_type is not None or adjustment_multiplier is not None:
            raise PriceLedgerValidationError(
                "confirmed no-event evidence cannot contain action terms"
            )
        reference = _safe_id(reference_id, "reference_id")
        ingested_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        identity = {
            "schema_version": CORPORATE_ACTION_EVIDENCE_SCHEMA_VERSION,
            "scope_id": scope,
            "security_code": code,
            "evidence_kind": evidence_kind,
            "effective_at": start,
            "effective_to": end,
            "available_at": known,
            "ingested_at": ingested_at,
            "revision": normalized_revision,
            "supersedes_evidence_id": predecessor,
            "action_type": normalized_action,
            "adjustment_multiplier": normalized_multiplier,
            "reference_id": reference,
            "source": normalized_source,
        }
        digest = _digest(identity)
        evidence_id = "cae_" + digest[:32]
        self.initialize_schema()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT evidence_id FROM corporate_action_bitemporal_evidence
                    WHERE evidence_digest=?
                    """,
                    (digest,),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    return {
                        "evidence_id": str(existing["evidence_id"]),
                        "evidence_digest": digest,
                        "idempotent": True,
                    }
                if predecessor is not None:
                    previous = connection.execute(
                        """
                        SELECT * FROM corporate_action_bitemporal_evidence
                        WHERE evidence_id=?
                        """,
                        (predecessor,),
                    ).fetchone()
                    if (
                        previous is None
                        or previous["scope_id"] != scope
                        or previous["security_code"] != code
                        or int(previous["revision"]) >= normalized_revision
                    ):
                        raise PriceLedgerValidationError(
                            "corporate-action supersession lineage is invalid"
                        )
                accepted = connection.execute(
                    """
                    SELECT * FROM corporate_action_bitemporal_evidence
                    WHERE scope_id=? AND security_code=?
                    """,
                    (scope, code),
                ).fetchall()
                for item in accepted:
                    opposite = str(item["evidence_kind"]) != evidence_kind
                    overlaps = str(item["effective_at"]) <= end and start <= str(
                        item["effective_to"]
                    )
                    if opposite and overlaps:
                        raise PriceLedgerConflictError(
                            "corporate action conflicts with confirmed no-event evidence",
                            evidence={
                                "code": "corporate_action_no_event_conflict",
                                "existing_evidence_id": str(item["evidence_id"]),
                                "security_code": code,
                            },
                        )
                connection.execute(
                    """
                    INSERT INTO corporate_action_bitemporal_evidence (
                        evidence_id, schema_version, scope_id, security_code,
                        evidence_kind, effective_at, effective_to, available_at,
                        ingested_at, revision, supersedes_evidence_id,
                        action_type, adjustment_multiplier, reference_id,
                        source_json, evidence_digest, imported_by_user_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        CORPORATE_ACTION_EVIDENCE_SCHEMA_VERSION,
                        scope,
                        code,
                        evidence_kind,
                        start,
                        end,
                        known,
                        ingested_at,
                        normalized_revision,
                        predecessor,
                        normalized_action,
                        normalized_multiplier,
                        reference,
                        _canonical_json(normalized_source),
                        digest,
                        int(imported_by_user_id),
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PriceLedgerConflictError(
                "corporate-action immutable evidence conflicts"
            ) from exc
        return {
            "evidence_id": evidence_id,
            "evidence_digest": digest,
            "idempotent": False,
            "ingested_at": ingested_at,
        }

    def query_corporate_action_evidence_as_known(
        self,
        *,
        scope_id: str,
        security_code: str,
        effective_start: str,
        effective_end: str,
        as_known_at: str,
    ) -> dict[str, Any]:
        """Read only immutable evidence visible at the knowledge cutoff."""

        scope = _safe_id(scope_id, "scope_id")
        code = _security_code(security_code)
        start = _utc_timestamp(effective_start, "effective_start")
        end = _utc_timestamp(effective_end, "effective_end")
        cutoff = _utc_timestamp(as_known_at, "as_known_at")
        if start > end:
            raise PriceLedgerValidationError(
                "effective_start must not exceed effective_end"
            )
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM corporate_action_bitemporal_evidence
                WHERE scope_id=? AND security_code=?
                  AND effective_at <= ? AND effective_to >= ?
                  AND available_at <= ? AND ingested_at <= ?
                ORDER BY revision, evidence_id
                """,
                (scope, code, end, start, cutoff, cutoff),
            ).fetchall()
        visible_ids = {str(row["evidence_id"]) for row in rows}
        superseded = {
            str(row["supersedes_evidence_id"])
            for row in rows
            if row["supersedes_evidence_id"] in visible_ids
        }
        evidence: list[dict[str, Any]] = []
        for row in rows:
            if str(row["evidence_id"]) in superseded:
                continue
            source = self._parse_json(row["source_json"], "action source")
            identity = {
                key: row[key]
                for key in (
                    "schema_version",
                    "scope_id",
                    "security_code",
                    "evidence_kind",
                    "effective_at",
                    "effective_to",
                    "available_at",
                    "ingested_at",
                    "revision",
                    "supersedes_evidence_id",
                    "action_type",
                    "adjustment_multiplier",
                    "reference_id",
                )
            }
            identity["source"] = source
            if _digest(identity) != row["evidence_digest"]:
                raise PriceLedgerIntegrityError(
                    "corporate-action evidence integrity mismatch"
                )
            evidence.append(
                {
                    **identity,
                    "evidence_id": str(row["evidence_id"]),
                    "evidence_digest": str(row["evidence_digest"]),
                }
            )
        return {
            "schema_version": CORPORATE_ACTION_EVIDENCE_SCHEMA_VERSION,
            "scope_id": scope,
            "security_code": code,
            "as_known_at": cutoff,
            "evidence": evidence,
            "confirmed_no_event": bool(evidence)
            and all(item["evidence_kind"] == "confirmed_no_event" for item in evidence),
        }

    def bind_runtime_scope(
        self,
        *,
        scope_id: str,
        timeline_identity: Mapping[str, Any],
        trading_dates: Iterable[str],
        batch_ids: Iterable[str],
        status_source: Mapping[str, Any],
        suspension_observations: Iterable[Mapping[str, Any]],
        bound_by_user_id: int,
        as_known_at: str | None = None,
        _production_release_authorization: (
            _ProductionReleaseAuthorization | None
        ) = None,
    ) -> dict[str, Any]:
        """Bind one exact PIT request to shared canonical dual-price batches.

        A binding contains no copied price rows.  Multiple index scopes can
        therefore reuse the same immutable canonical batches while retaining
        their own dated membership identity.
        """

        trading_date_input = [str(item) for item in trading_dates]
        batch_id_input = [str(item) for item in batch_ids]
        suspension_input = [dict(item) for item in suspension_observations]
        submitted_document = {
            "scope_id": scope_id,
            "timeline_identity": dict(timeline_identity),
            "trading_dates": trading_date_input,
            "batch_ids": batch_id_input,
            "status_source": dict(status_source),
            "suspension_observations": suspension_input,
            "as_known_at": as_known_at,
        }
        release_authorized = bool(
            _production_release_authorization is not None
            and _production_release_authorization.token
            is _PRODUCTION_RELEASE_TOKEN
            and _production_release_authorization.operation
            == "bind_runtime_scope"
            and _production_release_authorization.document_sha256
            == _digest(submitted_document)
            and _SHA256.fullmatch(
                _production_release_authorization.plan_sha256
            )
            and _SHA256.fullmatch(
                _production_release_authorization.manifest_sha256
            )
        )
        scope = _safe_id(scope_id, "scope_id")
        knowledge_cutoff = (
            _utc_timestamp(as_known_at, "as_known_at")
            if as_known_at is not None
            else None
        )
        identity, dates, timeline = self._normalize_runtime_timeline(
            scope_id=scope,
            timeline_identity=timeline_identity,
            trading_dates=trading_date_input,
        )
        normalized_batch_ids = sorted(
            {_safe_id(item, "batch_id") for item in batch_id_input}
        )
        normalized_status_source = _normalize_source(
            status_source,
            expected_adjustment="trading_status",
            field="status_source",
        )
        if normalized_status_source["evidence_level"] in {
            "licensed",
            "exchange_authoritative",
        } and not release_authorized:
            raise PriceLedgerValidationError(
                "authoritative trading-status binding is blocked until its "
                "artifact governance receipt workflow is available"
            )
        suspensions: list[dict[str, str]] = []
        seen_suspensions: set[tuple[str, str]] = set()
        for item in suspension_input:
            normalized = {
                "security_code": _security_code(item.get("security_code")),
                "date": _iso_date(
                    item.get("date"),
                    "suspension.date",
                    allow_future=True,
                ),
                "status": str(item.get("status") or "").strip(),
            }
            if normalized["status"] != "suspended":
                raise PriceLedgerValidationError(
                    "runtime binding only accepts explicit suspension gaps"
                )
            key = (normalized["security_code"], normalized["date"])
            if key in seen_suspensions:
                raise PriceLedgerValidationError(
                    "runtime binding contains duplicate suspension evidence"
                )
            seen_suspensions.add(key)
            suspensions.append(normalized)
        suspensions.sort(key=lambda item: (item["date"], item["security_code"]))

        self.initialize_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batches = self._verified_batches_by_ids(
                connection,
                normalized_batch_ids,
            )
            if knowledge_cutoff is not None:
                if not identity.get("bitemporal_availability_verified"):
                    raise PriceLedgerValidationError(
                        "runtime timeline lacks bitemporal availability proof"
                    )
                if identity.get("as_known_at") != knowledge_cutoff:
                    raise PriceLedgerValidationError(
                        "runtime timeline knowledge cutoff does not match"
                    )
                if any(
                    not item["bitemporal"]["verified"]
                    or str(item["bitemporal"]["available_at"])
                    > knowledge_cutoff
                    or str(item["bitemporal"]["ingested_at"])
                    > knowledge_cutoff
                    for item in batches
                ):
                    raise PriceLedgerValidationError(
                        "runtime price batch was not known at the cutoff"
                    )
            rows, canonical_evidence_sha256 = (
                self._runtime_canonical_evidence(
                    batches,
                    start=dates[0],
                    end=dates[-1],
                    security_codes=set(timeline.union_codes),
                )
            )
            role_observations = {
                (
                    str(row["price_role"]),
                    str(row["security_code"]),
                    str(row["date"]),
                )
                for row in rows
            }
            expected = {
                (code, day)
                for day, members in zip(
                    timeline.dates,
                    timeline.members_by_date,
                    strict=True,
                )
                for code in members
            }
            suspension_set = {
                (item["security_code"], item["date"]) for item in suspensions
            }
            if suspension_set - expected:
                connection.rollback()
                raise PriceLedgerValidationError(
                    "suspension evidence falls outside dated membership"
                )
            unresolved: list[dict[str, str]] = []
            inconsistent: list[dict[str, str]] = []
            for code, day in sorted(expected):
                raw_present = ("raw_execution", code, day) in role_observations
                research_present = (
                    "research_adjusted",
                    code,
                    day,
                ) in role_observations
                suspended = (code, day) in suspension_set
                if raw_present != research_present or (
                    suspended and raw_present
                ):
                    inconsistent.append(
                        {"security_code": code, "date": day}
                    )
                elif not raw_present and not suspended:
                    unresolved.append(
                        {"security_code": code, "date": day}
                    )
            if inconsistent or unresolved:
                connection.rollback()
                raise PriceLedgerValidationError(
                    "runtime binding has unresolved or inconsistent "
                    "membership price gaps"
                )

            timeline_json = _canonical_json(identity)
            trading_dates_json = _canonical_json(list(dates))
            batch_ids_json = _canonical_json(normalized_batch_ids)
            status_source_json = _canonical_json(normalized_status_source)
            suspensions_json = _canonical_json(suspensions)
            hashes = {
                "timeline_sha256": hashlib.sha256(
                    timeline_json.encode("utf-8")
                ).hexdigest(),
                "trading_dates_sha256": hashlib.sha256(
                    trading_dates_json.encode("utf-8")
                ).hexdigest(),
                "batch_ids_sha256": hashlib.sha256(
                    batch_ids_json.encode("utf-8")
                ).hexdigest(),
                "status_source_sha256": hashlib.sha256(
                    status_source_json.encode("utf-8")
                ).hexdigest(),
                "suspensions_sha256": hashlib.sha256(
                    suspensions_json.encode("utf-8")
                ).hexdigest(),
            }
            binding_identity = {
                "schema_version": (
                    BITEMPORAL_RUNTIME_BINDING_SCHEMA_VERSION
                    if knowledge_cutoff is not None
                    else RUNTIME_BINDING_SCHEMA_VERSION
                ),
                "scope_id": scope,
                "coverage_from": dates[0],
                "coverage_to": dates[-1],
                **hashes,
                "canonical_evidence_sha256": canonical_evidence_sha256,
            }
            role_usage = {
                "signal_and_research_features": "research_adjusted",
                "execution_fills_and_valuation": "raw_execution",
                "mixed_role_fallback_allowed": False,
            }
            bitemporal_evidence_sha256: str | None = None
            if knowledge_cutoff is not None:
                bitemporal_evidence_sha256 = _digest(
                    {
                        "as_known_at": knowledge_cutoff,
                        "timeline_as_known_at": identity.get("as_known_at"),
                        "batches": [
                            {
                                "batch_id": item["batch_id"],
                                **item["bitemporal"],
                            }
                            for item in batches
                        ],
                    }
                )
                binding_identity.update(
                    as_known_at=knowledge_cutoff,
                    bitemporal_evidence_sha256=bitemporal_evidence_sha256,
                    price_role_usage_sha256=_digest(role_usage),
                )
            binding_digest = _digest(binding_identity)
            binding_id = "plr_" + binding_digest[:32]
            existing = connection.execute(
                """
                SELECT binding_id FROM price_ledger_runtime_bindings
                WHERE binding_digest = ?
                """,
                (binding_digest,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return {
                    "binding_id": str(existing["binding_id"]),
                    "binding_digest": binding_digest,
                    "canonical_evidence_sha256": canonical_evidence_sha256,
                    "batch_ids": normalized_batch_ids,
                    "idempotent": True,
                    "as_known_at": knowledge_cutoff,
                    "bitemporal_availability_verified": bool(
                        knowledge_cutoff
                    ),
                    "price_role_usage": role_usage,
                }
            conflicting = connection.execute(
                """
                SELECT binding_id FROM price_ledger_runtime_bindings
                WHERE scope_id = ?
                  AND timeline_sha256 = ?
                  AND trading_dates_sha256 = ?
                LIMIT 1
                """,
                (
                    scope,
                    hashes["timeline_sha256"],
                    hashes["trading_dates_sha256"],
                ),
            ).fetchone()
            if conflicting is not None:
                connection.rollback()
                raise PriceLedgerConflictError(
                    "point-in-time runtime request is already bound to "
                    "different immutable price evidence",
                    evidence={
                        "code": "runtime_price_binding_conflict",
                        "scope_id": scope,
                        "timeline_sha256": hashes["timeline_sha256"],
                    },
                )
            connection.execute(
                """
                INSERT INTO price_ledger_runtime_bindings (
                    binding_id, schema_version, scope_id, coverage_from,
                    coverage_to, timeline_json, timeline_sha256,
                    trading_dates_json, trading_dates_sha256, batch_ids_json,
                    batch_ids_sha256, status_source_json,
                    status_source_sha256, suspensions_json,
                    suspensions_sha256, canonical_evidence_sha256, binding_digest,
                    bound_by_user_id, created_at, as_known_at,
                    bitemporal_evidence_sha256, price_role_usage_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    binding_id,
                    binding_identity["schema_version"],
                    scope,
                    dates[0],
                    dates[-1],
                    timeline_json,
                    hashes["timeline_sha256"],
                    trading_dates_json,
                    hashes["trading_dates_sha256"],
                    batch_ids_json,
                    hashes["batch_ids_sha256"],
                    status_source_json,
                    hashes["status_source_sha256"],
                    suspensions_json,
                    hashes["suspensions_sha256"],
                    canonical_evidence_sha256,
                    binding_digest,
                    int(bound_by_user_id),
                    datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    knowledge_cutoff,
                    bitemporal_evidence_sha256,
                    (
                        _canonical_json(role_usage)
                        if knowledge_cutoff is not None
                        else None
                    ),
                ),
            )
            connection.commit()
        return {
            "binding_id": binding_id,
            "binding_digest": binding_digest,
            "canonical_evidence_sha256": canonical_evidence_sha256,
            "batch_ids": normalized_batch_ids,
            "idempotent": False,
            "as_known_at": knowledge_cutoff,
            "bitemporal_availability_verified": bool(knowledge_cutoff),
            "price_role_usage": role_usage,
        }

    def _verify_runtime_binding(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        serialized = {
            "timeline": (
                str(row["timeline_json"]),
                str(row["timeline_sha256"]),
            ),
            "trading_dates": (
                str(row["trading_dates_json"]),
                str(row["trading_dates_sha256"]),
            ),
            "batch_ids": (
                str(row["batch_ids_json"]),
                str(row["batch_ids_sha256"]),
            ),
            "status_source": (
                str(row["status_source_json"]),
                str(row["status_source_sha256"]),
            ),
            "suspensions": (
                str(row["suspensions_json"]),
                str(row["suspensions_sha256"]),
            ),
        }
        decoded: dict[str, Any] = {}
        for label, (payload, expected_hash) in serialized.items():
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != expected_hash:
                raise PriceLedgerIntegrityError(
                    f"runtime binding {label} integrity mismatch"
                )
            decoded[label] = self._parse_json(payload, label)
        binding_identity = {
            "schema_version": str(row["schema_version"]),
            "scope_id": str(row["scope_id"]),
            "coverage_from": str(row["coverage_from"]),
            "coverage_to": str(row["coverage_to"]),
            "timeline_sha256": str(row["timeline_sha256"]),
            "trading_dates_sha256": str(row["trading_dates_sha256"]),
            "batch_ids_sha256": str(row["batch_ids_sha256"]),
            "status_source_sha256": str(row["status_source_sha256"]),
            "suspensions_sha256": str(row["suspensions_sha256"]),
            "canonical_evidence_sha256": str(
                row["canonical_evidence_sha256"]
            ),
        }
        role_usage: dict[str, Any] | None = None
        if row["schema_version"] == BITEMPORAL_RUNTIME_BINDING_SCHEMA_VERSION:
            if (
                row["as_known_at"] is None
                or row["bitemporal_evidence_sha256"] is None
                or row["price_role_usage_json"] is None
            ):
                raise PriceLedgerIntegrityError(
                    "bitemporal runtime binding evidence is incomplete"
                )
            role_usage = self._parse_json(
                row["price_role_usage_json"],
                "price role usage",
            )
            expected_usage = {
                "signal_and_research_features": "research_adjusted",
                "execution_fills_and_valuation": "raw_execution",
                "mixed_role_fallback_allowed": False,
            }
            if role_usage != expected_usage:
                raise PriceLedgerIntegrityError(
                    "runtime price role usage boundary is invalid"
                )
            binding_identity.update(
                as_known_at=str(row["as_known_at"]),
                bitemporal_evidence_sha256=str(
                    row["bitemporal_evidence_sha256"]
                ),
                price_role_usage_sha256=_digest(role_usage),
            )
        if (
            row["schema_version"]
            not in {
                RUNTIME_BINDING_SCHEMA_VERSION,
                BITEMPORAL_RUNTIME_BINDING_SCHEMA_VERSION,
            }
            or _digest(binding_identity) != row["binding_digest"]
        ):
            raise PriceLedgerIntegrityError(
                "runtime binding identity integrity mismatch"
            )
        identity, dates, timeline = self._normalize_runtime_timeline(
            scope_id=str(row["scope_id"]),
            timeline_identity=decoded["timeline"],
            trading_dates=decoded["trading_dates"],
        )
        try:
            normalized_status_source = _normalize_source(
                decoded["status_source"],
                expected_adjustment="trading_status",
                field="status_source",
            )
        except PriceLedgerValidationError as exc:
            raise PriceLedgerIntegrityError(
                "runtime trading-status source is invalid"
            ) from exc
        if normalized_status_source != decoded["status_source"]:
            raise PriceLedgerIntegrityError(
                "runtime trading-status source is noncanonical"
            )
        if dates[0] != row["coverage_from"] or dates[-1] != row["coverage_to"]:
            raise PriceLedgerIntegrityError(
                "runtime binding coverage no longer matches trading dates"
            )
        batches = self._verified_batches_by_ids(
            connection,
            decoded["batch_ids"],
        )
        if row["schema_version"] == BITEMPORAL_RUNTIME_BINDING_SCHEMA_VERSION:
            cutoff = str(row["as_known_at"])
            if (
                identity.get("as_known_at") != cutoff
                or identity.get("bitemporal_availability_verified") is not True
                or any(
                    not item["bitemporal"]["verified"]
                    or str(item["bitemporal"]["available_at"]) > cutoff
                    or str(item["bitemporal"]["ingested_at"]) > cutoff
                    for item in batches
                )
            ):
                raise PriceLedgerIntegrityError(
                    "runtime evidence was not known at the binding cutoff"
                )
            expected_bitemporal_hash = _digest(
                {
                    "as_known_at": cutoff,
                    "timeline_as_known_at": identity.get("as_known_at"),
                    "batches": [
                        {"batch_id": item["batch_id"], **item["bitemporal"]}
                        for item in batches
                    ],
                }
            )
            if expected_bitemporal_hash != row["bitemporal_evidence_sha256"]:
                raise PriceLedgerIntegrityError(
                    "runtime bitemporal evidence integrity mismatch"
                )
        rows, evidence_sha256 = self._runtime_canonical_evidence(
            batches,
            start=dates[0],
            end=dates[-1],
            security_codes=set(timeline.union_codes),
        )
        if evidence_sha256 != row["canonical_evidence_sha256"]:
            raise PriceLedgerIntegrityError(
                "runtime canonical price evidence changed"
            )
        role_observations = {
            (
                str(item["price_role"]),
                str(item["security_code"]),
                str(item["date"]),
            )
            for item in rows
        }
        expected = {
            (code, day)
            for day, members in zip(
                timeline.dates,
                timeline.members_by_date,
                strict=True,
            )
            for code in members
        }
        suspension_set = {
            (
                _security_code(item.get("security_code")),
                _iso_date(
                    item.get("date"),
                    "suspension.date",
                    allow_future=True,
                ),
            )
            for item in decoded["suspensions"]
            if isinstance(item, Mapping)
            and item.get("status") == "suspended"
        }
        if len(suspension_set) != len(decoded["suspensions"]):
            raise PriceLedgerIntegrityError(
                "runtime suspension evidence is invalid"
            )
        if suspension_set - expected:
            raise PriceLedgerIntegrityError(
                "runtime suspension evidence escaped dated membership"
            )
        for code, day in expected:
            raw_present = ("raw_execution", code, day) in role_observations
            research_present = (
                "research_adjusted",
                code,
                day,
            ) in role_observations
            suspended = (code, day) in suspension_set
            if (
                raw_present != research_present
                or suspended == raw_present
            ):
                raise PriceLedgerIntegrityError(
                    "runtime PIT member-session coverage is incomplete"
                )
        return {
            "binding_id": str(row["binding_id"]),
            "binding_digest": str(row["binding_digest"]),
            "scope_id": str(row["scope_id"]),
            "coverage_from": str(row["coverage_from"]),
            "coverage_to": str(row["coverage_to"]),
            "timeline_identity": identity,
            "trading_dates": dates,
            "batch_ids": list(decoded["batch_ids"]),
            "status_source": dict(decoded["status_source"]),
            "batch_digests": [
                str(item["batch_digest"]) for item in batches
            ],
            "suspensions": list(decoded["suspensions"]),
            "canonical_evidence_sha256": evidence_sha256,
            "rows": rows,
            "batches": batches,
            "as_known_at": row["as_known_at"],
            "bitemporal_availability_verified": bool(
                row["schema_version"]
                == BITEMPORAL_RUNTIME_BINDING_SCHEMA_VERSION
            ),
            "price_role_usage": role_usage,
        }

    def load_bound_runtime_prices(
        self,
        *,
        scope_id: str,
        timeline_identity: Mapping[str, Any],
        trading_dates: Iterable[str],
    ) -> BoundRuntimePrices | None:
        """Load exact bound adjusted/raw frames without any Parquet fallback."""

        import pandas as pd

        scope = _safe_id(scope_id, "scope_id")
        identity, dates, timeline = self._normalize_runtime_timeline(
            scope_id=scope,
            timeline_identity=timeline_identity,
            trading_dates=trading_dates,
        )
        try:
            connection = self._connect(readonly=True)
        except PriceLedgerValidationError:
            return None
        with connection:
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='price_ledger_runtime_bindings'
                """
            ).fetchone()
            if table is None:
                return None
            candidates = connection.execute(
                """
                SELECT * FROM price_ledger_runtime_bindings
                WHERE scope_id = ?
                  AND timeline_sha256 = ?
                  AND trading_dates_sha256 = ?
                ORDER BY binding_id
                """,
                (
                    scope,
                    _digest(identity),
                    _digest(list(dates)),
                ),
            ).fetchall()
            if not candidates:
                return None
            if len(candidates) != 1:
                raise PriceLedgerIntegrityError(
                    "runtime price binding identity is ambiguous"
                )
            binding = self._verify_runtime_binding(
                connection,
                candidates[0],
            )

        rows_by_role: dict[str, list[dict[str, Any]]] = {
            "raw_execution": [],
            "research_adjusted": [],
        }
        for row in binding["rows"]:
            rows_by_role[str(row["price_role"])].append(
                {
                    "security_code": str(row["security_code"]),
                    "date": str(row["date"]),
                    **_price_values(row),
                }
            )

        def build_frame(role: str) -> pd.DataFrame:
            fields = list((*_OHLC_FIELDS, "volume"))
            rows = rows_by_role[role]
            if not rows:
                raise PriceLedgerIntegrityError(
                    f"runtime binding has no {role} rows"
                )
            frame = pd.DataFrame(rows)
            panel = frame.set_index(["date", "security_code"])[fields].unstack(
                "security_code"
            )
            panel.columns = panel.columns.swaplevel(0, 1)
            panel.columns.names = ["code", "field"]
            panel.index = pd.DatetimeIndex(panel.index, name="date")
            panel = panel.reindex(pd.DatetimeIndex(dates, name="date"))
            panel.sort_index(axis=1, inplace=True)
            return panel

        rendered_binding = {
            key: value
            for key, value in binding.items()
            if key not in {"rows", "batches"}
        }
        rendered_binding["sources"] = {
            "raw_execution": [
                {
                    key: item["raw_source"][key]
                    for key in (
                        "provider",
                        "dataset",
                        "version",
                        "evidence_level",
                        "content_sha256",
                    )
                }
                for item in binding["batches"]
            ],
            "research_adjusted": [
                {
                    key: item["research_source"][key]
                    for key in (
                        "provider",
                        "dataset",
                        "version",
                        "evidence_level",
                        "content_sha256",
                    )
                }
                for item in binding["batches"]
            ],
            "corporate_action": [
                (
                    {
                        key: item["corporate_action_source"][key]
                        for key in (
                            "provider",
                            "dataset",
                            "version",
                            "evidence_level",
                            "content_sha256",
                        )
                    }
                    if item["corporate_action_source"] is not None
                    else None
                )
                for item in binding["batches"]
            ],
            "trading_status": binding["status_source"],
        }
        rendered_binding["timeline_sha256"] = _digest(identity)
        rendered_binding["trading_dates_sha256"] = _digest(list(dates))
        rendered_binding["runtime_price_roles_separated"] = True
        rendered_binding["adjustment_validation"] = {
            "unexplained_factor_change_count": sum(
                int(
                    item["audit"].get(
                        "unexplained_factor_change_count",
                        0,
                    )
                )
                for item in binding["batches"]
            ),
        }
        return BoundRuntimePrices(
            scope_id=scope,
            timeline_identity=identity,
            trading_dates=dates,
            research_adjusted=build_frame("research_adjusted"),
            raw_execution=build_frame("raw_execution"),
            binding=rendered_binding,
        )

    def validate_runtime_binding(
        self,
        *,
        binding_id: str,
        expected_scope_id: str | None = None,
        expected_binding_digest: str | None = None,
        require_bitemporal: bool = True,
    ) -> dict[str, Any]:
        """Verify one immutable binding for manifest admission."""

        normalized_id = _safe_id(binding_id, "binding_id")
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM price_ledger_runtime_bindings WHERE binding_id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise PriceLedgerValidationError(
                    "runtime price binding does not exist"
                )
            binding = self._verify_runtime_binding(connection, row)
        if (
            expected_scope_id is not None
            and binding["scope_id"] != _safe_id(
                expected_scope_id,
                "expected_scope_id",
            )
        ):
            raise PriceLedgerIntegrityError(
                "runtime price binding scope mismatch"
            )
        if (
            expected_binding_digest is not None
            and binding["binding_digest"] != expected_binding_digest
        ):
            raise PriceLedgerIntegrityError(
                "runtime price binding digest mismatch"
            )
        if require_bitemporal and not binding[
            "bitemporal_availability_verified"
        ]:
            raise PriceLedgerValidationError(
                "runtime price binding lacks bitemporal evidence"
            )
        return {
            "schema_version": str(row["schema_version"]),
            "binding_id": binding["binding_id"],
            "binding_digest": binding["binding_digest"],
            "scope_id": binding["scope_id"],
            "timeline_sha256": str(row["timeline_sha256"]),
            "trading_dates_sha256": str(row["trading_dates_sha256"]),
            "canonical_evidence_sha256": binding[
                "canonical_evidence_sha256"
            ],
            "as_known_at": binding["as_known_at"],
            "bitemporal_availability_verified": binding[
                "bitemporal_availability_verified"
            ],
            "price_role_usage": binding["price_role_usage"],
        }

    def inspect_bound_runtime_readiness(
        self,
        *,
        scope_id: str,
        timeline_identity: Mapping[str, Any],
        trading_dates: Iterable[str],
    ) -> dict[str, Any]:
        """Return readiness only when an exact PIT/hash request is bound."""

        identity, dates, timeline = self._normalize_runtime_timeline(
            scope_id=_safe_id(scope_id, "scope_id"),
            timeline_identity=timeline_identity,
            trading_dates=trading_dates,
        )
        bound = self.load_bound_runtime_prices(
            scope_id=scope_id,
            timeline_identity=identity,
            trading_dates=dates,
        )
        if bound is None:
            unavailable = self.unavailable_readiness(
                scope_id=scope_id,
                start=dates[0],
                end=dates[-1],
                reason="canonical_runtime_binding_missing",
            )
            unavailable["canonical_runtime_price_bound"] = False
            return unavailable
        sources = bound.binding["sources"]
        raw_levels = sorted(
            {
                str(item["evidence_level"])
                for item in sources["raw_execution"]
            }
        )
        research_levels = sorted(
            {
                str(item["evidence_level"])
                for item in sources["research_adjusted"]
            }
        )
        action_sources = sources["corporate_action"]
        status_source = sources["trading_status"]
        research_trusted = bool(
            research_levels
            and all(level in _RESEARCH_LEVELS for level in research_levels)
        )
        execution_trusted = bool(
            raw_levels
            and all(level in _EXECUTION_LEVELS for level in raw_levels)
        )
        action_authoritative = bool(
            action_sources
            and all(
                item is not None
                and item["evidence_level"] in _AUTHORITATIVE_ACTION_LEVELS
                for item in action_sources
            )
        )
        status_authoritative = bool(
            status_source["evidence_level"] in _AUTHORITATIVE_ACTION_LEVELS
        )
        adjustment_changes_explained = bool(
            bound.binding["adjustment_validation"][
                "unexplained_factor_change_count"
            ]
            == 0
        )
        strict_unbiased = strict_unbiased_readiness(
            exact_pit_binding=True,
            member_session_complete=True,
            bitemporal_availability_verified=bool(
                bound.binding.get("bitemporal_availability_verified")
            ),
            trading_status_authoritative=status_authoritative,
            corporate_action_validated=action_authoritative,
            trusted_research_ledger=research_trusted,
            trusted_execution_ledger=execution_trusted,
            adjustment_changes_explained=adjustment_changes_explained,
        )
        limitations: list[str] = []
        if not research_trusted:
            limitations.append("research_price_source_evidence_insufficient")
        if not execution_trusted:
            limitations.append("raw_execution_source_evidence_insufficient")
        if not action_authoritative:
            limitations.append(
                "corporate_action_authoritative_evidence_missing"
            )
        if not status_authoritative:
            limitations.append(
                "trading_status_authoritative_evidence_missing"
            )
        if not adjustment_changes_explained:
            limitations.append("adjustment_factor_changes_unexplained")
        if not bound.binding.get("bitemporal_availability_verified"):
            limitations.append("bitemporal_source_availability_not_verified")
        # The binding proves the two roles are distinct and complete.  It does
        # not claim the current engine has applied split/dividend position
        # transformations, which remains an independent live-readiness gate.
        limitations.append("corporate_action_runtime_application_missing")
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "ledger_available": True,
            "scope_id": scope_id,
            "required_start": dates[0],
            "required_end": dates[-1],
            "reason": None,
            "dual_ledger_complete": True,
            "batch_ids": bound.binding["batch_ids"],
            "batch_digests": bound.binding["batch_digests"],
            "binding_id": bound.binding["binding_id"],
            "binding_digest": bound.binding["binding_digest"],
            "timeline_hash": timeline.timeline_hash,
            "timeline_sha256": bound.binding["timeline_sha256"],
            "trading_dates_sha256": bound.binding[
                "trading_dates_sha256"
            ],
            "canonical_evidence_sha256": bound.binding[
                "canonical_evidence_sha256"
            ],
            "canonical_price_consistency": True,
            "canonical_runtime_price_bound": True,
            "runtime_price_roles_separated": True,
            "bitemporal_availability_verified": bool(
                bound.binding.get("bitemporal_availability_verified")
            ),
            "as_known_at": bound.binding.get("as_known_at"),
            "price_role_usage": bound.binding.get("price_role_usage"),
            "suspension_observation_count": len(
                bound.binding["suspensions"]
            ),
            "member_session_complete": True,
            "trading_status_authoritative": status_authoritative,
            "roles": {
                "raw_execution": {
                    "adjustment": "raw",
                    "available": True,
                    "trusted": execution_trusted,
                    "evidence_levels": raw_levels,
                },
                "research_adjusted": {
                    "adjustment": "hfq",
                    "available": True,
                    "trusted": research_trusted,
                    "evidence_levels": research_levels,
                },
            },
            "corporate_action_authoritative": action_authoritative,
            "corporate_action_validated": bool(
                action_authoritative and adjustment_changes_explained
            ),
            "descriptive_return_research_ready": research_trusted,
            "ready_for_return_research": research_trusted,
            "ready_for_unbiased_return_research": strict_unbiased,
            "ready_for_unbiased_research": strict_unbiased,
            "ready_for_execution_simulation": False,
            "ready_for_real_tuning": False,
            "limitations": limitations,
            "data_gaps": _readiness_gaps(limitations),
            "readiness_semantics": {
                "ready_for_return_research": (
                    "descriptive adjusted-return research only; this flag "
                    "does not prove point-in-time membership and is never a "
                    "promotion gate"
                ),
                "ready_for_unbiased_research": (
                    "exact PIT member-session binding, authoritative daily "
                    "tradability, validated corporate actions and trusted "
                    "raw/hfq ledgers"
                ),
            },
        }

    @staticmethod
    def unavailable_readiness(
        *,
        scope_id: str,
        start: str,
        end: str,
        reason: str = "ledger_unavailable",
    ) -> dict[str, Any]:
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "ledger_available": False,
            "scope_id": scope_id,
            "required_start": start,
            "required_end": end,
            "reason": reason,
            "dual_ledger_complete": False,
            "roles": {
                "raw_execution": {
                    "adjustment": "raw",
                    "available": False,
                    "trusted": False,
                },
                "research_adjusted": {
                    "adjustment": "hfq",
                    "available": False,
                    "trusted": False,
                },
            },
            "corporate_action_authoritative": False,
            "canonical_price_consistency": False,
            "canonical_evidence_sha256": None,
            "descriptive_return_research_ready": False,
            "ready_for_return_research": False,
            "ready_for_adjusted_price_return_research": False,
            "ready_for_unbiased_return_research": False,
            "return_research_semantics": (
                "price_role_only_requires_separate_point_in_time_binding"
            ),
            "ready_for_unbiased_research": False,
            "ready_for_execution_simulation": False,
            "ready_for_real_tuning": False,
            "limitations": [
                reason,
                "corporate_action_authoritative_evidence_missing",
            ],
            "data_gaps": _readiness_gaps(
                [
                    reason,
                    "corporate_action_authoritative_evidence_missing",
                ]
            ),
        }

    def audit_cross_scope_consistency(
        self,
        *,
        start: str,
        end: str,
        security_codes: Iterable[str] = (),
        limit: int = 1_000,
    ) -> dict[str, Any]:
        """Audit canonical price identities across all scopes, read-only."""

        required_start = _iso_date(start, "start", allow_future=True)
        required_end = _iso_date(end, "end", allow_future=True)
        if required_start > required_end:
            raise PriceLedgerValidationError("start must not exceed end")
        if not 1 <= int(limit) <= 10_000:
            raise PriceLedgerValidationError("limit is invalid")
        codes = {_security_code(item) for item in security_codes}
        unavailable = {
            "schema_version": CROSS_SCOPE_AUDIT_SCHEMA_VERSION,
            "ledger_available": False,
            "required_start": required_start,
            "required_end": required_end,
            "ready": False,
            "checked_scope_count": 0,
            "checked_row_count": 0,
            "canonical_identity_count": 0,
            "conflict_identity_count": 0,
            "scope_ids": [],
            "canonical_evidence_sha256": None,
            "conflicts": [],
            "truncated": False,
            "limitations": ["ledger_unavailable"],
        }
        try:
            connection = self._connect(readonly=True)
        except PriceLedgerValidationError:
            return unavailable
        with connection:
            if not self._tables_exist(connection):
                return unavailable
            batches = self._verified_batches_all_scopes(
                connection,
                start=required_start,
                end=required_end,
            )
        rows = self._canonical_rows(
            batches,
            start=required_start,
            end=required_end,
            security_codes=codes or None,
        )
        report = self._build_cross_scope_report(
            rows,
            start=required_start,
            end=required_end,
            limit=int(limit),
        )
        return {"ledger_available": bool(batches), **report}

    def inspect_readiness(
        self,
        *,
        scope_id: str,
        start: str,
        end: str,
        security_codes: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Verify dual-ledger coverage and role-specific source trust."""

        scope = _safe_id(scope_id, "scope_id")
        required_start = _iso_date(start, "start", allow_future=True)
        required_end = _iso_date(end, "end", allow_future=True)
        if required_start > required_end:
            raise PriceLedgerValidationError("start must not exceed end")
        codes = sorted({_security_code(item) for item in security_codes})
        try:
            connection = self._connect(readonly=True)
        except PriceLedgerValidationError:
            return self.unavailable_readiness(
                scope_id=scope,
                start=required_start,
                end=required_end,
            )
        with connection:
            if not self._tables_exist(connection):
                return self.unavailable_readiness(
                    scope_id=scope,
                    start=required_start,
                    end=required_end,
                )
            batches = self._verified_batches(
                connection,
                scope_id=scope,
                start=required_start,
                end=required_end,
            )
        if not batches:
            return self.unavailable_readiness(
                scope_id=scope,
                start=required_start,
                end=required_end,
            )
        cross_scope = self.audit_cross_scope_consistency(
            start=required_start,
            end=required_end,
            security_codes=codes,
            limit=10_000,
        )
        cross_scope_conflicts = [
            item
            for item in cross_scope["conflicts"]
            if scope in item["scope_ids"]
        ]
        canonical_consistent = bool(
            cross_scope["ledger_available"]
            and not cross_scope_conflicts
            and not cross_scope["truncated"]
        )
        covered = _intervals_cover(
            [
                (item["coverage_from"], item["coverage_to"])
                for item in batches
            ],
            required_start,
            required_end,
        )
        available_codes = sorted(
            {
                row["security_code"]
                for item in batches
                for row in item["payload"]["raw_prices"]
                if required_start <= row["date"] <= required_end
            }
        )
        missing_codes = sorted(set(codes) - set(available_codes))
        role_identities: dict[str, list[tuple[str, str]]] = {
            "raw_execution": [],
            "research_adjusted": [],
        }
        for item in batches:
            for role, payload_key in (
                ("raw_execution", "raw_prices"),
                ("research_adjusted", "research_prices"),
            ):
                role_identities[role].extend(
                    (row["security_code"], row["date"])
                    for row in item["payload"][payload_key]
                    if required_start <= row["date"] <= required_end
                    and (
                        not codes or row["security_code"] in codes
                    )
                )
        ambiguous_roles = sorted(
            role
            for role, identities in role_identities.items()
            if len(identities) != len(set(identities))
        )
        raw_levels = sorted(
            {item["raw_source"]["evidence_level"] for item in batches}
        )
        research_levels = sorted(
            {
                item["research_source"]["evidence_level"]
                for item in batches
            }
        )
        research_trusted = bool(
            research_levels
            and all(level in _RESEARCH_LEVELS for level in research_levels)
        )
        execution_trusted = bool(
            raw_levels
            and all(level in _EXECUTION_LEVELS for level in raw_levels)
        )
        action_authoritative = bool(
            batches
            and all(
                item["corporate_action_source"] is not None
                and item["corporate_action_source"]["evidence_level"]
                in _AUTHORITATIVE_ACTION_LEVELS
                for item in batches
            )
        )
        dual_complete = bool(
            covered
            and not missing_codes
            and not ambiguous_roles
            and canonical_consistent
        )
        limitations: list[str] = []
        if not covered:
            limitations.append("dual_ledger_range_not_covered")
        if missing_codes:
            limitations.append("dual_ledger_security_coverage_incomplete")
        if ambiguous_roles:
            limitations.append("dual_ledger_price_identity_ambiguous")
        if not canonical_consistent:
            limitations.append("cross_scope_canonical_price_conflict")
        if not research_trusted:
            limitations.append("research_price_source_evidence_insufficient")
        if not execution_trusted:
            limitations.append("raw_execution_source_evidence_insufficient")
        if not action_authoritative:
            limitations.append(
                "corporate_action_authoritative_evidence_missing"
            )
        if any(
            int(item["audit"].get("unexplained_factor_change_count", 0)) > 0
            for item in batches
        ):
            limitations.append("adjustment_factor_changes_unexplained")
        ready_for_return = bool(dual_complete and research_trusted)
        # A range query has no authoritative trading-calendar/member-session
        # identity.  It may describe stored prices, but it can never certify
        # execution or real tuning; only the exact runtime-binding path can
        # prove each required member-session (and that path remains blocked
        # until corporate actions are applied to portfolio state).
        ready_for_execution = False
        limitations.append("generic_readiness_not_execution_certification")
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "ledger_available": True,
            "scope_id": scope,
            "required_start": required_start,
            "required_end": required_end,
            "reason": None,
            "dual_ledger_complete": dual_complete,
            "batch_ids": [item["batch_id"] for item in batches],
            "batch_digests": [item["batch_digest"] for item in batches],
            "available_code_count": len(available_codes),
            "codes_sha256": (
                hashlib.sha256(
                    ",".join(available_codes).encode()
                ).hexdigest()
                if available_codes
                else None
            ),
            "missing_codes": missing_codes,
            "ambiguous_roles": ambiguous_roles,
            "roles": {
                "raw_execution": {
                    "adjustment": "raw",
                    "available": dual_complete,
                    "trusted": execution_trusted,
                    "evidence_levels": raw_levels,
                },
                "research_adjusted": {
                    "adjustment": "hfq",
                    "available": dual_complete,
                    "trusted": research_trusted,
                    "evidence_levels": research_levels,
                },
            },
            "corporate_action_authoritative": action_authoritative,
            "canonical_price_consistency": canonical_consistent,
            "canonical_evidence_sha256": cross_scope[
                "canonical_evidence_sha256"
            ],
            "cross_scope_conflict_count": len(cross_scope_conflicts),
            "descriptive_return_research_ready": ready_for_return,
            "ready_for_return_research": ready_for_return,
            "ready_for_adjusted_price_return_research": ready_for_return,
            # A price ledger cannot establish which securities were investable
            # on a date. The caller must bind a verified PIT timeline and the
            # exact runtime price batch before exposing an unbiased-return gate.
            "ready_for_unbiased_return_research": False,
            "return_research_semantics": (
                "price_role_only_requires_separate_point_in_time_binding"
            ),
            "ready_for_unbiased_research": False,
            "ready_for_execution_simulation": ready_for_execution,
            "ready_for_real_tuning": False,
            "adjustment_audit": {
                "schema_version": ADJUSTMENT_AUDIT_SCHEMA_VERSION,
                "factor_change_count": sum(
                    int(item["audit"].get("factor_change_count", 0))
                    for item in batches
                ),
                "unexplained_factor_change_count": sum(
                    int(
                        item["audit"].get(
                            "unexplained_factor_change_count",
                            0,
                        )
                    )
                    for item in batches
                ),
                "abnormal_factor_change_count": sum(
                    int(
                        item["audit"].get(
                            "abnormal_factor_change_count",
                            0,
                        )
                    )
                    for item in batches
                ),
            },
            "limitations": list(dict.fromkeys(limitations)),
            "data_gaps": _readiness_gaps(limitations),
        }

    def query_prices(
        self,
        *,
        scope_id: str,
        role: PriceRole,
        start: str,
        end: str,
        security_codes: Iterable[str] = (),
        limit: int = 10_000,
    ) -> dict[str, Any]:
        """Return verified business rows for one explicit price role."""

        if role not in _PRICE_ROLES:
            raise PriceLedgerValidationError("price role is invalid")
        if not 1 <= int(limit) <= 100_000:
            raise PriceLedgerValidationError("limit is invalid")
        readiness = self.inspect_readiness(
            scope_id=scope_id,
            start=start,
            end=end,
            security_codes=security_codes,
        )
        if not readiness["ledger_available"]:
            return {
                **readiness,
                "price_role": role,
                "adjustment": _ROLE_ADJUSTMENTS[role],
                "rows": [],
                "truncated": False,
            }
        scope = _safe_id(scope_id, "scope_id")
        required_start = _iso_date(start, "start", allow_future=True)
        required_end = _iso_date(end, "end", allow_future=True)
        codes = sorted({_security_code(item) for item in security_codes})
        with self._connect(readonly=True) as connection:
            batches = self._verified_batches(
                connection,
                scope_id=scope,
                start=required_start,
                end=required_end,
            )
        rows: list[dict[str, Any]] = []
        source_key = (
            "raw_source" if role == "raw_execution" else "research_source"
        )
        payload_key = (
            "raw_prices" if role == "raw_execution" else "research_prices"
        )
        for batch in batches:
            source = batch[source_key]
            for item in batch["payload"][payload_key]:
                if not required_start <= item["date"] <= required_end:
                    continue
                if codes and item["security_code"] not in codes:
                    continue
                rows.append(
                    {
                        **item,
                        "price_role": role,
                        "adjustment": _ROLE_ADJUSTMENTS[role],
                        "source": {
                            "provider": source["provider"],
                            "dataset": source["dataset"],
                            "version": source["version"],
                            "evidence_level": source["evidence_level"],
                        },
                        "batch_id": batch["batch_id"],
                        "batch_digest": batch["batch_digest"],
                    }
                )
        rows.sort(
            key=lambda item: (
                item["date"],
                item["security_code"],
                item["source"]["provider"],
                item["source"]["version"],
            )
        )
        truncated = len(rows) > limit
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "scope_id": scope,
            "price_role": role,
            "adjustment": _ROLE_ADJUSTMENTS[role],
            "required_start": required_start,
            "required_end": required_end,
            "rows": rows[:limit],
            "truncated": truncated,
            "limitations": readiness["limitations"],
        }
