"""BaoStock daily-price adapter with an auditable post-adjustment contract.

BaoStock's public API exposes raw OHLCV, ``preclose`` and trading status.  This
adapter deliberately rebuilds the post-adjusted series from raw prices and the
official previous-close recurrence instead of trusting a pre-adjusted endpoint
whose factor continuity cannot be independently checked.
"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import logging
import math
import threading
from typing import Any, Awaitable, Callable

import numpy as np
import pandas as pd

from backend.data.source_validation import (
    DailyFetchResult,
    build_daily_fetch_evidence,
)

from .akshare_source import ProviderOutageError
from .base import DataSource

logger = logging.getLogger("quant_platform.data.baostock")

BaoStockProgress = Callable[[dict[str, Any]], Awaitable[None]]

_BAOSTOCK_NATIVE_LOCK = threading.Lock()
_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,"
    "adjustflag,tradestatus,pctChg"
)
_ENDPOINT = "baostock.query_history_k_data_plus/raw+preclose"


@dataclass(frozen=True)
class BaoStockLedgerFetchResult:
    """Raw trading rows plus explicit suspension observations for a ledger job."""

    frame: pd.DataFrame
    status_rows: tuple[dict[str, str], ...]
    evidence: dict[str, Any]


def _market_code(code: str) -> str:
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}.{code}"


def _capture_call(function, *args, **kwargs):
    """Keep BaoStock's unconditional console messages out of service logs."""

    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = function(*args, **kwargs)
    captured = output.getvalue().strip()
    if captured:
        logger.debug("BaoStock native output: %s", captured)
    return result


def _normalize_raw(raw: pd.DataFrame, code: str) -> pd.DataFrame:
    """Validate one BaoStock raw response and retain its preclose evidence."""

    required = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "tradestatus",
    }
    if not required.issubset(raw.columns):
        raise ValueError(
            f"BaoStock response for {code} is missing fields: "
            f"{sorted(required - set(raw.columns))}"
        )
    frame = raw.loc[raw["tradestatus"].astype(str) == "1"].copy()
    if frame.empty:
        return pd.DataFrame()
    if set(frame["adjustflag"].astype(str)) != {"3"}:
        raise ValueError(f"BaoStock raw adjustment flag changed for {code}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    numeric = ("open", "high", "low", "close", "preclose", "volume", "amount")
    for field in numeric:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame = frame.sort_values("date")
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError(f"BaoStock dates are invalid or duplicated for {code}")
    prices = frame.loc[:, ["open", "high", "low", "close"]]
    if (
        prices.isna().any().any()
        or not np.isfinite(prices.to_numpy(dtype="float64")).all()
        or (prices <= 0).any().any()
    ):
        raise ValueError(f"BaoStock OHLC contains invalid values for {code}")
    volume = frame["volume"].to_numpy(dtype="float64")
    amount = frame["amount"].to_numpy(dtype="float64")
    if (
        not np.isfinite(volume).all()
        or not np.isfinite(amount).all()
        or (volume < 0).any()
        or (amount < 0).any()
    ):
        raise ValueError(f"BaoStock volume/amount contains invalid values for {code}")

    frame["code"] = code
    return frame[
        [
            "date",
            "code",
            "open",
            "high",
            "low",
            "close",
            "preclose",
            "volume",
            "amount",
        ]
    ]


def _normalize_status_rows(
    raw: pd.DataFrame,
    code: str,
) -> list[dict[str, str]]:
    """Retain BaoStock's explicit traded/suspended state without inventing gaps."""

    required = {"date", "adjustflag", "tradestatus"}
    if not required.issubset(raw.columns):
        raise ValueError(
            f"BaoStock response for {code} is missing status fields: "
            f"{sorted(required - set(raw.columns))}"
        )
    if raw.empty:
        return []
    dates = pd.to_datetime(raw["date"], errors="coerce")
    if dates.isna().any() or dates.duplicated().any():
        raise ValueError(f"BaoStock status dates are invalid or duplicated for {code}")
    flags = raw["adjustflag"].astype(str)
    if set(flags) != {"3"}:
        raise ValueError(f"BaoStock raw adjustment flag changed for {code}")
    statuses = raw["tradestatus"].astype(str)
    unknown = sorted(set(statuses) - {"0", "1"})
    if unknown:
        raise ValueError(
            f"BaoStock trading status changed for {code}: {unknown}"
        )
    return [
        {
            "security_code": code,
            "date": pd.Timestamp(day).strftime("%Y-%m-%d"),
            "status": "traded" if status == "1" else "suspended",
        }
        for day, status in zip(dates, statuses, strict=True)
    ]


def _apply_hfq_recurrence(
    frame: pd.DataFrame,
    code: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Apply the official recurrence and retain bounded factor-jump evidence."""

    frame = frame.copy()
    raw_close = frame["close"].to_numpy(dtype="float64")
    preclose = frame["preclose"].to_numpy(dtype="float64")
    factors = np.ones(len(frame), dtype="float64")
    jumps: list[dict[str, Any]] = []
    for position in range(1, len(frame)):
        previous_close = raw_close[position - 1]
        current_preclose = preclose[position]
        if (
            not math.isfinite(previous_close)
            or not math.isfinite(current_preclose)
            or previous_close <= 0
            or current_preclose <= 0
        ):
            raise ValueError(
                f"BaoStock preclose continuity is invalid for {code} at "
                f"{frame.iloc[position]['date']:%Y-%m-%d}"
            )
        factors[position] = (
            factors[position - 1] * previous_close / current_preclose
        )
        if not math.isfinite(factors[position]) or factors[position] <= 0:
            raise ValueError(f"BaoStock hfq factor became invalid for {code}")
        ratio = previous_close / current_preclose
        if not math.isclose(ratio, 1.0, rel_tol=0.0, abs_tol=1e-12):
            jumps.append(
                {
                    "code": code,
                    "date": pd.Timestamp(frame.iloc[position]["date"]).strftime(
                        "%Y-%m-%d"
                    ),
                    "factor_ratio": float(ratio),
                }
            )

    for field in ("open", "high", "low", "close"):
        frame[field] = frame[field].to_numpy(dtype="float64") * factors
    frame["code"] = code
    adjusted = frame[
        ["date", "code", "open", "high", "low", "close", "volume", "amount"]
    ]
    return adjusted, jumps


def _rebuild_hfq(raw: pd.DataFrame, code: str) -> pd.DataFrame:
    """Build a first-observation-anchored hfq series from raw/preclose.

    The recurrence is the BaoStock-published post-adjustment formula:
    ``factor[t] = factor[t-1] * close[t-1] / preclose[t]``.  Its arbitrary
    initial anchor does not affect returns or cross-source comparison.
    """

    normalized = _normalize_raw(raw, code)
    rebuilt, _ = _apply_hfq_recurrence(normalized, code)
    return rebuilt


def rebuild_hfq_panel(
    raw_panel: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Convert a validated raw panel to hfq and emit factor evidence."""

    if raw_panel.empty or not isinstance(raw_panel.columns, pd.MultiIndex):
        raise ValueError("BaoStock raw panel is empty or malformed")
    codes = sorted(
        {str(value).strip() for value in raw_panel.columns.get_level_values(0)}
    )
    frames: list[pd.DataFrame] = []
    jump_count = 0
    examples: list[dict[str, Any]] = []
    for code in codes:
        required = {
            "open",
            "high",
            "low",
            "close",
            "preclose",
            "volume",
            "amount",
        }
        available = {
            str(field).strip().lower()
            for field in raw_panel[code].columns
        }
        if not required.issubset(available):
            raise ValueError(
                f"BaoStock raw panel for {code} is missing fields: "
                f"{sorted(required - available)}"
            )
        frame = raw_panel[code].loc[:, sorted(required)].dropna(how="all").copy()
        frame.index = pd.to_datetime(frame.index, errors="raise")
        frame = frame.reset_index()
        frame.rename(columns={frame.columns[0]: "date"}, inplace=True)
        adjusted, jumps = _apply_hfq_recurrence(frame, code)
        frames.append(adjusted)
        jump_count += len(jumps)
        examples.extend(jumps[: max(0, 20 - len(examples))])
    combined = pd.concat(frames, ignore_index=True)
    fields = ["open", "close", "high", "low", "volume", "amount"]
    panel = combined.set_index(["date", "code"])[fields].unstack("code")
    panel.columns = panel.columns.swaplevel(0, 1)
    panel.columns.names = ["code", "field"]
    panel.sort_index(inplace=True)
    panel.sort_index(axis=1, inplace=True)
    values = panel.loc[:, panel.columns.get_level_values(-1).isin(
        ["open", "high", "low", "close"]
    )].to_numpy(dtype="float64")
    observed_values = values[~np.isnan(values)]
    return panel, {
        "schema_version": "adjustment-factor-validation/v1",
        "method": "baostock_raw_preclose_hfq_recurrence",
        "input_adjustment": "raw",
        "output_adjustment": "hfq",
        "recurrence_validated": True,
        "factors_finite_positive": bool(
            observed_values.size
            and np.isfinite(observed_values).all()
            and (observed_values > 0).all()
        ),
        "corporate_action_jump_count": jump_count,
        "corporate_action_examples": examples,
        "evidence_truncated": jump_count > len(examples),
    }


class BaoStockSource(DataSource):
    """Independent public OHLCV source with raw and auditable hfq semantics."""

    def __init__(self, *, price_adjustment: str = "hfq") -> None:
        if price_adjustment not in {"raw", "hfq"}:
            raise ValueError("BaoStockSource supports raw or hfq")
        self.price_adjustment = price_adjustment

    def staging_identity(self) -> dict[str, str]:
        return {
            "provider": "baostock:official",
            "endpoint": _ENDPOINT,
            "adjustment": self.price_adjustment,
            "adapter_id": "quant-platform/baostock-raw-preclose-hfq/v1",
        }

    def adjust_validated_raw(
        self,
        frame: pd.DataFrame,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Build research hfq only after independent raw validation passed."""

        if self.price_adjustment != "raw":
            raise ValueError("only a raw BaoStock source can adjust validated data")
        return rebuild_hfq_panel(frame)

    async def fetch_daily(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        return (await self.fetch_daily_result(codes, start, end)).frame

    async def fetch_daily_result(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> DailyFetchResult:
        return await self.fetch_daily_result_with_progress(
            codes,
            start,
            end,
            progress=None,
            source_role="primary",
        )

    async def fetch_daily_result_with_progress(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        progress: BaoStockProgress | None,
        source_role: str,
    ) -> DailyFetchResult:
        normalized_codes = sorted(
            {str(code).strip() for code in codes if str(code).strip()}
        )
        if not normalized_codes:
            frame = pd.DataFrame()
        else:
            frame = await self._fetch_panel(
                normalized_codes,
                start,
                end,
                progress=progress,
                source_role=source_role,
            )
        evidence = build_daily_fetch_evidence(
            frame,
            requested_codes=normalized_codes,
            start=start,
            end=end,
            provider="baostock:official",
            endpoint=_ENDPOINT,
            adjustment=self.price_adjustment,
            evidence_level="public_aggregator",
            transformations=(
                [
                    "filter:tradestatus=1",
                    (
                        "hfq_factor[t]=hfq_factor[t-1]*"
                        "raw_close[t-1]/raw_preclose[t]"
                    ),
                    "hfq_ohlc=raw_ohlc*hfq_factor",
                    "volume=raw",
                ]
                if self.price_adjustment == "hfq"
                else [
                    "filter:tradestatus=1",
                    "retain:raw_preclose",
                    "volume=raw",
                ]
            ),
        )
        return DailyFetchResult(frame, evidence)

    async def fetch_ledger_daily_result(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        progress: BaoStockProgress | None = None,
    ) -> BaoStockLedgerFetchResult:
        """Fetch raw/preclose once and preserve explicit suspension evidence.

        The ledger path deliberately accepts only the raw adapter.  ``hfq`` is
        rebuilt locally from these exact raw rows so a fetch can never mix two
        independently queried price roles.
        """

        if self.price_adjustment != "raw":
            raise ValueError("ledger fetch requires BaoStock raw adjustment")
        normalized_codes = sorted(
            {str(code).strip() for code in codes if str(code).strip()}
        )
        if normalized_codes:
            frame, status_rows = await self._fetch_panel_with_status(
                normalized_codes,
                start,
                end,
                progress=progress,
                source_role="canonical_raw",
            )
        else:
            frame, status_rows = pd.DataFrame(), []
        evidence = build_daily_fetch_evidence(
            frame,
            requested_codes=normalized_codes,
            start=start,
            end=end,
            provider="baostock:official",
            endpoint=_ENDPOINT,
            adjustment="raw",
            evidence_level="public_aggregator",
            transformations=[
                "retain:raw_preclose",
                "retain:tradestatus=0|1",
                "traded_ohlcv:tradestatus=1",
                "suspension_evidence:tradestatus=0",
            ],
        )
        return BaoStockLedgerFetchResult(
            frame=frame,
            status_rows=tuple(status_rows),
            evidence=evidence,
        )

    async def _fetch_panel(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        progress: BaoStockProgress | None,
        source_role: str,
    ) -> pd.DataFrame:
        frame, _ = await self._fetch_panel_with_status(
            codes,
            start,
            end,
            progress=progress,
            source_role=source_role,
        )
        return frame

    async def _fetch_panel_with_status(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        progress: BaoStockProgress | None,
        source_role: str,
    ) -> tuple[pd.DataFrame, list[dict[str, str]]]:
        import baostock as bs

        completed = 0
        frames: list[pd.DataFrame] = []
        status_rows: list[dict[str, str]] = []
        await asyncio.to_thread(_BAOSTOCK_NATIVE_LOCK.acquire)
        try:
            try:
                login = await asyncio.to_thread(_capture_call, bs.login)
            except Exception as exc:
                raise ProviderOutageError(
                    f"baostock login transport failed: {type(exc).__name__}"
                ) from exc
            if str(getattr(login, "error_code", "")) != "0":
                raise ProviderOutageError(
                    "baostock login failed: "
                    f"{getattr(login, 'error_msg', 'unknown error')}"
                )
            try:
                for code in codes:
                    try:
                        result = await asyncio.to_thread(
                            bs.query_history_k_data_plus,
                            _market_code(code),
                            _FIELDS,
                            start_date=pd.Timestamp(start).strftime("%Y-%m-%d"),
                            end_date=pd.Timestamp(end).strftime("%Y-%m-%d"),
                            frequency="d",
                            adjustflag="3",
                        )
                    except Exception as exc:
                        raise ProviderOutageError(
                            f"baostock daily transport failed for {code}: "
                            f"{type(exc).__name__}"
                        ) from exc
                    if str(getattr(result, "error_code", "")) != "0":
                        raise ProviderOutageError(
                            f"baostock daily query failed for {code}: "
                            f"{getattr(result, 'error_msg', 'unknown error')}"
                        )
                    rows: list[list[str]] = []
                    while result.next():
                        rows.append(result.get_row_data())
                    raw = pd.DataFrame(rows, columns=result.fields)
                    status_rows.extend(_normalize_status_rows(raw, code))
                    normalized = _normalize_raw(raw, code)
                    rebuilt = (
                        _apply_hfq_recurrence(normalized, code)[0]
                        if self.price_adjustment == "hfq"
                        else normalized
                    )
                    if not rebuilt.empty:
                        frames.append(rebuilt)
                    completed += 1
                    report_step = max(1, len(codes) // 50)
                    if progress is not None and (
                        completed == len(codes)
                        or completed % report_step == 0
                    ):
                        await progress(
                            {
                                "source_role": source_role,
                                "provider": "baostock:official",
                                "completed_codes": completed,
                                "total_codes": len(codes),
                                "reused_staging": False,
                            }
                        )
            finally:
                await asyncio.to_thread(_capture_call, bs.logout)
        finally:
            _BAOSTOCK_NATIVE_LOCK.release()

        status_rows.sort(
            key=lambda item: (item["date"], item["security_code"])
        )
        if not frames:
            return pd.DataFrame(), status_rows
        combined = pd.concat(frames, ignore_index=True)
        fields = ["open", "close", "high", "low", "volume", "amount"]
        if self.price_adjustment == "raw":
            fields.append("preclose")
        panel = combined.set_index(["date", "code"])[fields].unstack("code")
        panel.columns = panel.columns.swaplevel(0, 1)
        panel.columns.names = ["code", "field"]
        panel.sort_index(inplace=True)
        panel.sort_index(axis=1, inplace=True)
        return panel, status_rows

    async def fetch_index_components(
        self,
        index_code: str,
        date: str | None = None,
    ) -> list[str]:
        del index_code, date
        raise NotImplementedError(
            "BaoStock reference adapter does not provide universe membership"
        )

    async def fetch_trading_calendar(
        self,
        start: str,
        end: str,
    ) -> list[str]:
        del start, end
        raise NotImplementedError(
            "BaoStock reference adapter does not provide the platform calendar"
        )

    async def fetch_industry_list(self) -> list[dict]:
        raise NotImplementedError(
            "BaoStock reference adapter does not provide industry taxonomy"
        )
