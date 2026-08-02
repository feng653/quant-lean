"""A fail-closed market-data source composed from independent providers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from backend.config import settings

from backend.data.offload import run_data_integrity
from backend.data.source_validation import (
    DailyFetchResult,
    build_daily_fetch_evidence,
    compare_independent_daily_frames,
    fetch_daily_with_evidence,
    require_cross_source_acceptance,
)
from backend.data.validated_staging import (
    StagingIntegrityError,
    ValidatedDailyStaging,
)

from .base import DataSource

logger = logging.getLogger("quant_platform.data.validated")
ValidationProgress = Callable[[dict[str, Any]], Awaitable[None]]


def build_public_research_source(
    *,
    progress: ValidationProgress | None = None,
    staging_root: str | Path | None = None,
) -> "CrossValidatedDailySource":
    """Build the production research feed; it remains public/research-only.

    Long-history qfq series can become non-positive after cumulative cash
    distributions are rebased into the past.  The production research ledger
    therefore uses hfq, whose declared purpose is return research.  It is not
    silently presented as a raw execution-price ledger.
    """

    from .akshare_source import AKShareSource
    from .baostock_source import BaoStockSource

    return CrossValidatedDailySource(
        BaoStockSource(price_adjustment="raw"),
        AKShareSource("sina", price_adjustment="raw"),
        adjusted_reference=AKShareSource("sina", price_adjustment="hfq"),
        progress=progress,
        staging=ValidatedDailyStaging(
            staging_root
            or settings.abs_path(settings.DATA_STAGING_DIR)
            / "market-validation"
        ),
    )


class CrossValidatedDailySource(DataSource):
    """Return primary observations only after an independent feed agrees.

    This class does not blend or repair values.  Automatic blending makes it
    impossible to tell which provider supplied a cell and can conceal a bad
    corporate-action adjustment.  A disagreement blocks the whole request.
    """

    def __init__(
        self,
        primary: DataSource,
        reference: DataSource,
        *,
        min_overlap_returns: int = 20,
        return_abs_tolerance: float = 0.005,
        max_conflict_ratio: float = 0.0,
        adjusted_reference: DataSource | None = None,
        progress: ValidationProgress | None = None,
        staging: ValidatedDailyStaging | None = None,
    ) -> None:
        if primary is reference:
            raise ValueError("primary and reference sources must be independent")
        self.primary = primary
        self.reference = reference
        self.min_overlap_returns = min_overlap_returns
        self.return_abs_tolerance = return_abs_tolerance
        self.max_conflict_ratio = max_conflict_ratio
        self.adjusted_reference = adjusted_reference
        self.progress = progress
        self.staging = staging

    @staticmethod
    def _source_identity(source: DataSource) -> dict[str, str]:
        identity = getattr(source, "staging_identity", None)
        if not callable(identity):
            raise RuntimeError(
                f"{type(source).__name__} has no stable staging identity"
            )
        payload = identity()
        if not isinstance(payload, Mapping):
            raise RuntimeError("source staging identity is invalid")
        required = {"provider", "endpoint", "adjustment", "adapter_id"}
        normalized = {
            str(key): str(value).strip()
            for key, value in payload.items()
        }
        if not required.issubset(normalized) or any(
            not normalized[key] for key in required
        ):
            raise RuntimeError("source staging identity is incomplete")
        return normalized

    async def _report(
        self,
        event: Mapping[str, Any],
        *,
        phase_start: float,
        phase_end: float,
    ) -> None:
        if self.progress is None:
            return
        completed = max(0, int(event.get("completed_codes", 0)))
        total = max(0, int(event.get("total_codes", 0)))
        fraction = min(1.0, completed / total) if total else 0.0
        await self.progress(
            {
                **dict(event),
                "overall_fraction": (
                    phase_start + (phase_end - phase_start) * fraction
                ),
            }
        )

    async def _fetch_source(
        self,
        source: DataSource,
        codes: list[str],
        start: str,
        end: str,
        *,
        source_role: str,
        phase_start: float,
        phase_end: float,
    ) -> DailyFetchResult:
        native = getattr(source, "fetch_daily_result_with_progress", None)
        if callable(native):
            async def source_progress(event: dict[str, Any]) -> None:
                await self._report(
                    event,
                    phase_start=phase_start,
                    phase_end=phase_end,
                )

            return await native(
                codes,
                start,
                end,
                progress=source_progress,
                source_role=source_role,
            )
        result = await fetch_daily_with_evidence(source, codes, start, end)
        await self._report(
            {
                "source_role": source_role,
                "provider": result.evidence["provider"],
                "completed_codes": len(codes),
                "total_codes": len(codes),
                "reused_staging": False,
            },
            phase_start=phase_start,
            phase_end=phase_end,
        )
        return result

    async def fetch_daily_result(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> DailyFetchResult:
        # Fetch sequentially and keep only the complete primary response in a
        # request-bound checkpoint.  A reference outage or process restart can
        # then resume without downloading the primary twice.
        primary_identity = (
            self._source_identity(self.primary)
            if self.staging is not None
            else None
        )
        primary_result: DailyFetchResult | None = None
        if self.staging is not None:
            try:
                primary_result = await run_data_integrity(
                    self.staging.load,
                    codes=codes,
                    start=start,
                    end=end,
                    source_identity=primary_identity or {},
                )
            except StagingIntegrityError as exc:
                logger.warning("Ignoring unsafe market-data staging: %s", exc)
        if primary_result is not None:
            await self._report(
                {
                    "source_role": "primary",
                    "provider": primary_result.evidence["provider"],
                    "completed_codes": len(codes),
                    "total_codes": len(codes),
                    "reused_staging": True,
                },
                phase_start=0.0,
                phase_end=0.45,
            )
        else:
            primary_result = await self._fetch_source(
                self.primary,
                codes,
                start,
                end,
                source_role="primary",
                phase_start=0.0,
                phase_end=0.45,
            )
            if (
                self.staging is not None
                and primary_result.evidence["complete_code_coverage"]
                and not primary_result.frame.empty
            ):
                await run_data_integrity(
                    self.staging.save,
                    primary_result,
                    codes=codes,
                    start=start,
                    end=end,
                    source_identity=primary_identity or {},
                )
        reference_result = await self._fetch_source(
            self.reference,
            codes,
            start,
            end,
            source_role="reference",
            phase_start=0.45,
            phase_end=0.9,
        )
        first = primary_result.evidence
        second = reference_result.evidence
        for label, evidence in (
            ("primary", first),
            ("reference", second),
        ):
            if (
                not evidence["complete_code_coverage"]
                or evidence["response"]["failed_codes"]
            ):
                raise RuntimeError(
                    f"{label} source did not cover every requested code"
                )
        if first["adjustment"] != second["adjustment"]:
            raise RuntimeError(
                "independent feeds use different adjustment semantics: "
                f"{first['adjustment']} vs {second['adjustment']}"
            )
        validation = await run_data_integrity(
            compare_independent_daily_frames,
            primary_result.frame,
            reference_result.frame,
            primary_provider=first["provider"],
            reference_provider=second["provider"],
            requested_codes=codes,
            adjustment=first["adjustment"],
            min_overlap_returns=self.min_overlap_returns,
            return_abs_tolerance=self.return_abs_tolerance,
            max_conflict_ratio=self.max_conflict_ratio,
        )
        await self._report(
            {
                "source_role": "validation",
                "provider": (
                    f"{first['provider']}+{second['provider']}"
                ),
                "completed_codes": len(codes),
                "total_codes": len(codes),
                "reused_staging": False,
            },
            phase_start=0.9,
            phase_end=0.91,
        )
        require_cross_source_acceptance(validation)
        output_frame = primary_result.frame
        output_adjustment = str(first["adjustment"])
        transformations = first.get("transformations")
        adjustment_validation: dict[str, Any] | None = None
        adjust_validated_raw = getattr(
            self.primary,
            "adjust_validated_raw",
            None,
        )
        if callable(adjust_validated_raw):
            output_frame, adjustment_validation = await run_data_integrity(
                adjust_validated_raw,
                primary_result.frame,
            )
            output_adjustment = str(
                adjustment_validation.get("output_adjustment", "")
            )
            transformations = [
                *(transformations or []),
                (
                    "hfq_factor[t]=hfq_factor[t-1]*"
                    "raw_close[t-1]/raw_preclose[t]"
                ),
                "hfq_ohlc=raw_ohlc*hfq_factor",
            ]

        if (
            adjustment_validation is not None
            and self.adjusted_reference is not None
        ):
            try:
                adjusted_reference_result = await self._fetch_source(
                    self.adjusted_reference,
                    codes,
                    start,
                    end,
                    source_role="adjusted_reference",
                    phase_start=0.91,
                    phase_end=0.99,
                )
                adjusted_evidence = adjusted_reference_result.evidence
                if (
                    adjusted_evidence["complete_code_coverage"]
                    and not adjusted_evidence["response"]["failed_codes"]
                    and adjusted_evidence["adjustment"] == output_adjustment
                ):
                    adjustment_validation[
                        "informational_hfq_cross_source"
                    ] = await run_data_integrity(
                        compare_independent_daily_frames,
                        output_frame,
                        adjusted_reference_result.frame,
                        primary_provider=first["provider"],
                        reference_provider=adjusted_evidence["provider"],
                        requested_codes=codes,
                        adjustment=output_adjustment,
                        min_overlap_returns=self.min_overlap_returns,
                        return_abs_tolerance=self.return_abs_tolerance,
                        max_conflict_ratio=0.0,
                    )
                else:
                    adjustment_validation["informational_hfq_status"] = (
                        "reference_incomplete"
                    )
            except Exception as exc:
                logger.warning(
                    "Informational adjusted-price comparison unavailable: %s",
                    type(exc).__name__,
                )
                adjustment_validation["informational_hfq_status"] = (
                    f"unavailable:{type(exc).__name__}"
                )

        failures = dict(first["response"]["failed_codes"])
        evidence = await run_data_integrity(
            build_daily_fetch_evidence,
            output_frame,
            requested_codes=codes,
            start=start,
            end=end,
            provider=first["provider"],
            endpoint=first["endpoint"],
            adjustment=output_adjustment,
            evidence_level=first["evidence_level"],
            failed_codes=failures,
            cross_validation=validation,
            transformations=transformations,
            adjustment_validation=adjustment_validation,
        )
        if self.staging is not None:
            await run_data_integrity(
                self.staging.discard,
                codes=codes,
                start=start,
                end=end,
                source_identity=primary_identity or {},
            )
        return DailyFetchResult(output_frame, evidence)

    async def fetch_daily(
        self,
        codes: list[str],
        start: str,
        end: str,
    ):
        return (await self.fetch_daily_result(codes, start, end)).frame

    async def fetch_index_daily(
        self,
        index_code: str,
        start: str,
        end: str,
    ):
        # Index cross-validation is a separate contract; do not imply that the
        # daily equity check covered it.
        return await self.reference.fetch_index_daily(index_code, start, end)

    async def fetch_index_components(
        self,
        index_code: str,
        date: str | None = None,
    ) -> list[str]:
        return await self.reference.fetch_index_components(index_code, date)

    async def fetch_trading_calendar(
        self,
        start: str,
        end: str,
    ) -> list[str]:
        return await self.reference.fetch_trading_calendar(start, end)

    async def fetch_industry_list(self) -> list[dict[str, Any]]:
        return await self.reference.fetch_industry_list()

    async def fetch_industry_components(
        self,
        industry_name: str,
    ) -> list[str]:
        fetch = getattr(self.reference, "fetch_industry_components", None)
        if not callable(fetch):
            return []
        return await fetch(industry_name)

    async def fetch_industry_map(
        self,
        codes: list[str],
    ) -> dict[str, str]:
        fetch = getattr(self.reference, "fetch_industry_map", None)
        if not callable(fetch):
            return {}
        return await fetch(codes)
