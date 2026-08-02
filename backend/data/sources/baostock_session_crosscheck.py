"""Bounded BaoStock cross-checks for ambiguous Tushare session evidence.

BaoStock observations collected here are independent candidate evidence only.
They never remove a Tushare blocker, approve an import, or mutate runtime data.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence

from backend.data.provider_artifacts import (
    ContentAddressedProviderArtifactStore,
    build_candidate_artifact_manifest,
    canonical_json_bytes,
    canonical_sha256,
    utc_now,
)


BAOSTOCK_CROSSCHECK_INPUT_SCHEMA = "baostock-session-crosscheck-input/v1"
BAOSTOCK_CROSSCHECK_REPORT_SCHEMA = "baostock-session-crosscheck-report/v1"
BAOSTOCK_CROSSCHECK_CHECKPOINT_SCHEMA = "baostock-session-crosscheck-checkpoint/v1"
BAOSTOCK_QUERY_FIELDS = ("date", "code", "volume", "amount", "tradestatus")
BAOSTOCK_PROVIDER_REFERENCE = "https://www.baostock.com/"
_MAX_CALLS = 64
_MAX_PAIRS = 256
_TS_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BaoStockCrosscheckError(RuntimeError):
    """A cross-check plan, provider response, or checkpoint is unsafe."""

    def __init__(self, message: str, *, diagnostic_code: str) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code

    def diagnostic(self) -> dict[str, Any]:
        return {"code": self.diagnostic_code, "retryable": False}


class _DiscardedProviderOutput:
    """A non-retaining text sink for noisy third-party SDK output."""

    encoding = "utf-8"

    @staticmethod
    def write(value: Any) -> int:
        return len(str(value))

    @staticmethod
    def flush() -> None:
        return None

    @staticmethod
    def isatty() -> bool:
        return False


@contextmanager
def discard_baostock_sdk_output() -> Iterator[None]:
    """Prevent untrusted SDK stdout/stderr from crossing the JSON boundary."""

    sink = _DiscardedProviderOutput()
    with redirect_stdout(sink), redirect_stderr(sink):
        yield


class _BaoStockResult(Protocol):
    error_code: str
    fields: Sequence[str]

    def next(self) -> bool: ...

    def get_row_data(self) -> Sequence[Any]: ...


class BaoStockSdk(Protocol):
    def login(self) -> Any: ...

    def logout(self) -> Any: ...

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        *,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> _BaoStockResult: ...


@dataclass(frozen=True, order=True)
class SessionBlockerPair:
    ts_code: str
    trade_date: str
    tushare_reason: str

    def __post_init__(self) -> None:
        if not _TS_CODE.fullmatch(self.ts_code):
            raise BaoStockCrosscheckError(
                "ts_code must be an SSE/SZSE Tushare code",
                diagnostic_code="crosscheck_pair_invalid",
            )
        try:
            date.fromisoformat(self.trade_date)
        except ValueError as exc:
            raise BaoStockCrosscheckError(
                "trade_date must be YYYY-MM-DD",
                diagnostic_code="crosscheck_pair_invalid",
            ) from exc
        if not _REASON.fullmatch(self.tushare_reason):
            raise BaoStockCrosscheckError(
                "tushare_reason is invalid",
                diagnostic_code="crosscheck_pair_invalid",
            )

    @property
    def task_id(self) -> str:
        return canonical_sha256(self.public_scope())[:32]

    @property
    def baostock_code(self) -> str:
        symbol, exchange = self.ts_code.split(".")
        return f"{exchange.lower()}.{symbol}"

    def public_scope(self) -> dict[str, str]:
        return {
            "ts_code": self.ts_code,
            "trade_date": self.trade_date,
            "tushare_reason": self.tushare_reason,
        }


@dataclass(frozen=True)
class BaoStockCrosscheckPlan:
    pairs: tuple[SessionBlockerPair, ...]

    def __post_init__(self) -> None:
        if not self.pairs or len(self.pairs) > _MAX_PAIRS:
            raise BaoStockCrosscheckError(
                f"cross-check requires 1..{_MAX_PAIRS} blocker pairs",
                diagnostic_code="crosscheck_plan_invalid",
            )
        if tuple(sorted(set(self.pairs))) != self.pairs:
            raise BaoStockCrosscheckError(
                "cross-check pairs must be unique and canonically sorted",
                diagnostic_code="crosscheck_plan_invalid",
            )

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "BaoStockCrosscheckPlan":
        if set(document) != {"schema_version", "blocker_pairs"}:
            raise BaoStockCrosscheckError(
                "cross-check input contains unsupported fields",
                diagnostic_code="crosscheck_input_invalid",
            )
        if document.get("schema_version") != BAOSTOCK_CROSSCHECK_INPUT_SCHEMA:
            raise BaoStockCrosscheckError(
                "cross-check input schema is unsupported",
                diagnostic_code="crosscheck_input_invalid",
            )
        rows = document.get("blocker_pairs")
        if not isinstance(rows, list):
            raise BaoStockCrosscheckError(
                "blocker_pairs must be an array",
                diagnostic_code="crosscheck_input_invalid",
            )
        pairs: list[SessionBlockerPair] = []
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {
                "ts_code",
                "trade_date",
                "tushare_reason",
            }:
                raise BaoStockCrosscheckError(
                    "each blocker pair must contain only code, date, and reason",
                    diagnostic_code="crosscheck_input_invalid",
                )
            pairs.append(
                SessionBlockerPair(
                    ts_code=str(row["ts_code"]),
                    trade_date=str(row["trade_date"]),
                    tushare_reason=str(row["tushare_reason"]),
                )
            )
        return cls(tuple(sorted(pairs)))

    @property
    def run_id(self) -> str:
        return canonical_sha256(self.public_scope())[:32]

    def public_scope(self) -> dict[str, Any]:
        return {
            "blocker_pairs": [pair.public_scope() for pair in self.pairs],
            "classification": "quarantine",
            "purpose": "optional_independent_session_state_crosscheck",
        }


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def classify_baostock_session(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Classify one exact BaoStock daily row without inferring tradability."""

    if not rows:
        return {
            "status": "no_observation",
            "governance_review_required": True,
            "production_tradability_proven": False,
        }
    if len(rows) != 1:
        return {
            "status": "multiple_observations_ambiguous",
            "governance_review_required": True,
            "production_tradability_proven": False,
        }
    row = rows[0]
    trade_status = str(row.get("tradestatus") or "").strip()
    volume = _decimal(row.get("volume"))
    amount = _decimal(row.get("amount"))
    if volume is None or amount is None:
        status = "provider_numeric_state_invalid"
    elif trade_status == "0" and volume == 0 and amount == 0:
        status = "provider_reports_not_trading_without_liquidity"
    elif trade_status == "1" and volume > 0 and amount > 0:
        status = "provider_reports_trading_with_liquidity"
    elif trade_status == "0" and (volume > 0 or amount > 0):
        status = "provider_state_liquidity_conflict"
    elif trade_status == "1":
        status = "provider_trading_without_positive_liquidity_ambiguous"
    else:
        status = "provider_trade_status_unknown"
    return {
        "status": status,
        "governance_review_required": True,
        "production_tradability_proven": False,
    }


def _comparison(tushare_reason: str, baostock_status: str) -> str:
    if tushare_reason == "suspend_without_daily_semantics_ambiguous":
        if baostock_status == "provider_reports_not_trading_without_liquidity":
            return "candidate_supports_non_trading_interpretation"
        if baostock_status == "provider_reports_trading_with_liquidity":
            return "candidate_disagrees_with_missing_daily_interpretation"
    if tushare_reason == "daily_suspend_semantics_ambiguous":
        if baostock_status == "provider_reports_trading_with_liquidity":
            return "candidate_supports_observed_trading_but_not_suspend_timing"
        if baostock_status == "provider_reports_not_trading_without_liquidity":
            return "candidate_disagrees_with_tushare_daily_observation"
    return "candidate_observation_requires_governance_review"


class _CheckpointStore:
    def __init__(self, evidence_root: Path, run_id: str) -> None:
        self.directory = Path(os.path.abspath(evidence_root.expanduser())) / "checkpoints"
        self.path = self.directory / f"{run_id}.json"
        self.lock_path = self.directory / f"{run_id}.lock"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.directory.lstat()
        if self.directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise BaoStockCrosscheckError(
                "checkpoint directory is unsafe",
                diagnostic_code="crosscheck_checkpoint_unsafe",
            )
        os.chmod(self.directory, 0o700)

    @contextmanager
    def lease(self) -> Iterator[None]:
        descriptor = os.open(
            self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BaoStockCrosscheckError(
                    "another process owns this cross-check",
                    diagnostic_code="crosscheck_checkpoint_busy",
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self, plan: BaoStockCrosscheckPlan) -> dict[str, Any]:
        if not self.path.exists():
            payload = {
                "schema_version": BAOSTOCK_CROSSCHECK_CHECKPOINT_SCHEMA,
                "run_id": plan.run_id,
                "plan": plan.public_scope(),
                "completed": {},
                "updated_at": utc_now(),
            }
            return self._seal(payload)
        metadata = self.path.lstat()
        if self.path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise BaoStockCrosscheckError(
                "checkpoint file is unsafe",
                diagnostic_code="crosscheck_checkpoint_unsafe",
            )
        os.chmod(self.path, 0o600)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BaoStockCrosscheckError(
                "checkpoint is unreadable",
                diagnostic_code="crosscheck_checkpoint_invalid",
            ) from exc
        checksum = payload.pop("checkpoint_sha256", None)
        if checksum != canonical_sha256(payload):
            raise BaoStockCrosscheckError(
                "checkpoint digest changed",
                diagnostic_code="crosscheck_checkpoint_invalid",
            )
        if (
            payload.get("schema_version") != BAOSTOCK_CROSSCHECK_CHECKPOINT_SCHEMA
            or payload.get("run_id") != plan.run_id
            or payload.get("plan") != plan.public_scope()
        ):
            raise BaoStockCrosscheckError(
                "checkpoint plan changed",
                diagnostic_code="crosscheck_checkpoint_invalid",
            )
        return self._seal(payload)

    @staticmethod
    def _seal(payload: Mapping[str, Any]) -> dict[str, Any]:
        sealed = dict(payload)
        sealed["checkpoint_sha256"] = canonical_sha256(sealed)
        return sealed

    def save(self, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(checkpoint)
        checksum = payload.pop("checkpoint_sha256", None)
        if checksum != canonical_sha256(payload):
            raise BaoStockCrosscheckError(
                "refusing an unsealed checkpoint",
                diagnostic_code="crosscheck_checkpoint_invalid",
            )
        payload["updated_at"] = utc_now()
        sealed = self._seal(payload)
        descriptor, name = tempfile.mkstemp(prefix=".checkpoint.", dir=self.directory)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json_bytes(sealed))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
        return sealed


class BaoStockSessionCrosscheckCollector:
    """Collect exact SDK rows for an explicit, immutable blocker-pair plan."""

    def __init__(
        self,
        *,
        sdk: BaoStockSdk,
        store: ContentAddressedProviderArtifactStore,
        plan: BaoStockCrosscheckPlan,
        max_calls: int = 8,
    ) -> None:
        if not 1 <= max_calls <= _MAX_CALLS:
            raise BaoStockCrosscheckError(
                f"max_calls must be between 1 and {_MAX_CALLS}",
                diagnostic_code="crosscheck_call_budget_invalid",
            )
        self.sdk = sdk
        self.store = store
        self.plan = plan
        self.max_calls = max_calls
        self.checkpoints = _CheckpointStore(store.root, plan.run_id)

    def run(self) -> dict[str, Any]:
        with self.checkpoints.lease():
            checkpoint = self.checkpoints.load(self.plan)
            pending = [
                pair
                for pair in self.plan.pairs
                if pair.task_id not in checkpoint["completed"]
            ][: self.max_calls]
            if pending:
                self._collect_pending(pending, checkpoint)
            return self._report(checkpoint, calls_this_invocation=len(pending))

    def _collect_pending(
        self,
        pending: Sequence[SessionBlockerPair],
        checkpoint: dict[str, Any],
    ) -> None:
        logged_in = False
        try:
            try:
                with discard_baostock_sdk_output():
                    result = self.sdk.login()
                    login_code = str(getattr(result, "error_code", ""))
            except Exception as exc:
                raise BaoStockCrosscheckError(
                    "BaoStock login failed",
                    diagnostic_code="baostock_login_failed",
                ) from exc
            if login_code != "0":
                raise BaoStockCrosscheckError(
                    "BaoStock login failed",
                    diagnostic_code="baostock_login_failed",
                )
            logged_in = True
            for pair in pending:
                observation = self._query(pair)
                checkpoint["completed"][pair.task_id] = observation
                checkpoint["checkpoint_sha256"] = canonical_sha256(
                    {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
                )
                persisted = self.checkpoints.save(checkpoint)
                checkpoint.clear()
                checkpoint.update(persisted)
        finally:
            if logged_in:
                try:
                    with discard_baostock_sdk_output():
                        logout_result = self.sdk.logout()
                        logout_code = str(
                            getattr(logout_result, "error_code", "")
                        )
                except Exception as exc:
                    raise BaoStockCrosscheckError(
                        "BaoStock logout failed",
                        diagnostic_code="baostock_logout_failed",
                    ) from exc
                if logout_code != "0":
                    raise BaoStockCrosscheckError(
                        "BaoStock logout failed",
                        diagnostic_code="baostock_logout_failed",
                    )

    def _query(self, pair: SessionBlockerPair) -> dict[str, Any]:
        try:
            with discard_baostock_sdk_output():
                result = self.sdk.query_history_k_data_plus(
                    pair.baostock_code,
                    ",".join(BAOSTOCK_QUERY_FIELDS),
                    start_date=pair.trade_date,
                    end_date=pair.trade_date,
                    frequency="d",
                    adjustflag="3",
                )
                query_code = str(getattr(result, "error_code", ""))
                fields = tuple(str(field) for field in getattr(result, "fields", ()))
        except Exception as exc:
            raise BaoStockCrosscheckError(
                "BaoStock session query failed",
                diagnostic_code="baostock_query_failed",
            ) from exc
        if query_code != "0":
            raise BaoStockCrosscheckError(
                "BaoStock session query failed",
                diagnostic_code="baostock_query_failed",
            )
        if fields != BAOSTOCK_QUERY_FIELDS:
            raise BaoStockCrosscheckError(
                "BaoStock response fields changed",
                diagnostic_code="baostock_response_contract_changed",
            )
        rows: list[list[str]] = []
        try:
            with discard_baostock_sdk_output():
                while result.next():
                    if len(rows) >= 2:
                        raise BaoStockCrosscheckError(
                            "BaoStock returned too many rows for one session",
                            diagnostic_code="baostock_response_scope_changed",
                        )
                    row = [str(value) for value in result.get_row_data()]
                    if len(row) != len(fields):
                        raise BaoStockCrosscheckError(
                            "BaoStock response row width changed",
                            diagnostic_code="baostock_response_contract_changed",
                        )
                    rows.append(row)
        except BaoStockCrosscheckError:
            raise
        except Exception as exc:
            raise BaoStockCrosscheckError(
                "BaoStock response could not be read",
                diagnostic_code="baostock_response_contract_changed",
            ) from exc
        mapped = [dict(zip(fields, row, strict=True)) for row in rows]
        for row in mapped:
            if row["date"] != pair.trade_date or row["code"] != pair.baostock_code:
                raise BaoStockCrosscheckError(
                    "BaoStock response escaped the requested pair",
                    diagnostic_code="baostock_response_scope_changed",
                )
        raw_document = {
            "schema_version": "baostock-sdk-response/v1",
            "provider": "baostock",
            "fields": list(fields),
            "rows": rows,
        }
        response_payload = canonical_json_bytes(raw_document)
        ingested_at = utc_now()
        manifest = build_candidate_artifact_manifest(
            provider="baostock",
            dataset="daily_session_state",
            endpoint=BAOSTOCK_PROVIDER_REFERENCE,
            request={
                "adapter": "quant-platform/baostock-session-crosscheck/v1",
                "code": pair.baostock_code,
                "start_date": pair.trade_date,
                "end_date": pair.trade_date,
                "frequency": "d",
                "adjustflag": "3",
                "fields": list(BAOSTOCK_QUERY_FIELDS),
            },
            response_payload=response_payload,
            response_fields=fields,
            row_count=len(rows),
            ingested_at=ingested_at,
            temporal_contract={
                "effective_at": {
                    "fields": ["date"],
                    "evidence": "provider_field",
                },
                "available_at": {
                    "fields": [],
                    "evidence": "declared_ingestion_time",
                    "semantics": "SDK exposes no historical first-seen timestamp",
                },
            },
            temporal_coverage={"date": len(rows)},
            licence_status="unverified",
        )
        receipt = self.store.record(
            response_payload=response_payload,
            manifest=manifest,
        )
        classification = classify_baostock_session(mapped)
        return {
            "pair": pair.public_scope(),
            "receipt": receipt,
            "observed_at": ingested_at,
            "provider_session_state": classification,
            "comparison": _comparison(pair.tushare_reason, classification["status"]),
            "tushare_blocker_resolved": False,
            "official_governance_review_required": True,
        }

    def _report(
        self, checkpoint: Mapping[str, Any], *, calls_this_invocation: int
    ) -> dict[str, Any]:
        completed = checkpoint["completed"]
        observations = [
            completed[pair.task_id]
            for pair in self.plan.pairs
            if pair.task_id in completed
        ]
        report: dict[str, Any] = {
            "schema_version": BAOSTOCK_CROSSCHECK_REPORT_SCHEMA,
            "run_id": self.plan.run_id,
            "observed_at": utc_now(),
            "classification": "quarantine",
            "plan": self.plan.public_scope(),
            "progress": {
                "calls_this_invocation": calls_this_invocation,
                "max_calls_per_invocation": self.max_calls,
                "planned_pairs": len(self.plan.pairs),
                "completed_pairs": len(observations),
                "pending_pairs": len(self.plan.pairs) - len(observations),
                "complete": len(observations) == len(self.plan.pairs),
            },
            "observations": observations,
            "reconciliation_policy": {
                "mode": "optional_annotation_only",
                "tushare_blockers_may_be_removed": False,
                "official_governance_review_required": True,
            },
            "production_pit_ready": False,
            "runtime_data_changed": False,
            "promotion": {
                "eligible": False,
                "blockers": [
                    "candidate_quarantine_only",
                    "provider_retention_terms_unverified",
                    "historical_available_at_not_proven",
                    "official_session_state_review_required",
                    "governed_import_and_activation_required",
                ],
            },
        }
        report["report_sha256"] = canonical_sha256(report)
        stored = self.store.record_report(report)
        report["stored_report_sha256"] = stored
        return report


def annotate_tushare_session_reconciliation(
    session: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach optional BaoStock receipts while preserving every Tushare blocker."""

    if report.get("schema_version") != BAOSTOCK_CROSSCHECK_REPORT_SCHEMA:
        raise BaoStockCrosscheckError(
            "cross-check report schema is unsupported",
            diagnostic_code="crosscheck_report_invalid",
        )
    supplied_hash = report.get("report_sha256")
    unsigned = {
        key: value
        for key, value in report.items()
        if key not in {"report_sha256", "stored_report_sha256"}
    }
    if not isinstance(supplied_hash, str) or not _SHA256.fullmatch(supplied_hash):
        raise BaoStockCrosscheckError(
            "cross-check report digest is invalid",
            diagnostic_code="crosscheck_report_invalid",
        )
    if canonical_sha256(unsigned) != supplied_hash:
        raise BaoStockCrosscheckError(
            "cross-check report digest changed",
            diagnostic_code="crosscheck_report_invalid",
        )
    policy = report.get("reconciliation_policy")
    observations = report.get("observations")
    if (
        report.get("classification") != "quarantine"
        or report.get("production_pit_ready") is not False
        or report.get("runtime_data_changed") is not False
        or not isinstance(policy, Mapping)
        or policy.get("mode") != "optional_annotation_only"
        or policy.get("tushare_blockers_may_be_removed") is not False
        or not isinstance(observations, list)
        or any(
            not isinstance(observation, Mapping)
            or observation.get("tushare_blocker_resolved") is not False
            or observation.get("official_governance_review_required") is not True
            for observation in observations
        )
    ):
        raise BaoStockCrosscheckError(
            "cross-check report promotion boundary changed",
            diagnostic_code="crosscheck_report_invalid",
        )
    trade_date = str(session.get("trade_date") or "")
    blockers = session.get("blockers")
    if not isinstance(blockers, list) or session.get("valid") is not False:
        raise BaoStockCrosscheckError(
            "Tushare session must be invalid with explicit blockers",
            diagnostic_code="tushare_session_invalid",
        )
    exact_blockers = {
        (str(row.get("code") or ""), str(row.get("reason") or ""))
        for row in blockers
        if isinstance(row, Mapping)
    }
    annotations: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        pair = observation.get("pair")
        if not isinstance(pair, Mapping) or pair.get("trade_date") != trade_date:
            continue
        key = (str(pair.get("ts_code") or ""), str(pair.get("tushare_reason") or ""))
        if key not in exact_blockers:
            continue
        annotations.append(
            {
                "code": key[0],
                "tushare_reason": key[1],
                "baostock_receipt": observation.get("receipt"),
                "provider_session_state": observation.get("provider_session_state"),
                "comparison": observation.get("comparison"),
                "tushare_blocker_resolved": False,
                "official_governance_review_required": True,
            }
        )
    annotated = dict(session)
    annotated["blockers"] = [dict(row) if isinstance(row, Mapping) else row for row in blockers]
    annotated["valid"] = session.get("valid")
    annotated["optional_baostock_crosscheck"] = {
        "source_report_sha256": supplied_hash,
        "mode": "annotation_only",
        "tushare_blockers_changed": False,
        "observations": annotations,
    }
    return annotated
