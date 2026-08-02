"""Adapter contract for refreshers that produce point-in-time import batches.

Adapters may access a network in a separately controlled data-update job.  They
must return immutable import documents; persistence and overlap validation stay
inside :class:`PointInTimeMasterStore`.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, Sequence

from backend.data.sources.csindex_pit import (
    AdjustmentAnnouncement,
    ArchiveEvidence,
    CurrentAnchor,
    build_staging_package,
)


class PointInTimeImportAdapter(Protocol):
    """Materialize provider evidence without writing application storage."""

    provider_id: str
    dataset_id: str

    async def build_import_batches(
        self,
        *,
        coverage_from: str,
        coverage_to: str,
        scopes: list[str],
    ) -> list[dict[str, Any]]:
        """Return ``point-in-time-master-import/v1`` documents.

        Each document must carry a digest of the unmodified provider payload.
        A current-only provider must emit ``current_snapshot`` and a one-day
        coverage interval; it must never infer historical effective dates.
        """


class CsindexPointInTimeAdapter:
    """Offline CSI adapter whose output always requires administrator approval.

    Network collection is intentionally outside this class.  Construction
    requires already verified current anchors, full archive evidence, detailed
    announcements and an explicit trading calendar.
    """

    provider_id = "csindex_official"
    dataset_id = "constituent_adjustment_archive"

    def __init__(
        self,
        *,
        anchors: dict[str, CurrentAnchor],
        announcements: Sequence[AdjustmentAnnouncement],
        archive: ArchiveEvidence,
        trading_days: Sequence[date],
    ) -> None:
        self._anchors = dict(anchors)
        self._announcements = tuple(announcements)
        self._archive = archive
        self._trading_days = tuple(trading_days)

    def build_staging_package(
        self,
        *,
        coverage_from: str,
        coverage_to: str,
        scopes: list[str],
    ) -> dict[str, Any]:
        unknown = set(scopes) - {"csi300", "csi500", "csi800", "csi1000"}
        if unknown or not scopes:
            raise ValueError("unsupported or empty CSI scope selection")
        package = build_staging_package(
            anchors=self._anchors,
            announcements=self._announcements,
            archive=self._archive,
            trading_days=self._trading_days,
            coverage_from=date.fromisoformat(coverage_from),
            coverage_to=date.fromisoformat(coverage_to),
        )
        package["imports"] = [
            document
            for document in package["imports"]
            if document["scope_id"] in set(scopes)
        ]
        return package

    async def build_import_batches(
        self,
        *,
        coverage_from: str,
        coverage_to: str,
        scopes: list[str],
    ) -> list[dict[str, Any]]:
        """Return staged documents; this method never writes the PIT store."""

        return self.build_staging_package(
            coverage_from=coverage_from,
            coverage_to=coverage_to,
            scopes=scopes,
        )["imports"]
