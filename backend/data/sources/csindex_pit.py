"""Fail-closed CSI constituent evidence parsing and timeline reconstruction.

This module deliberately does not download or import data.  A controlled
collector must first retain the exact response bytes and source metadata.  The
adapter then verifies those bytes, parses the official current-constituent XLS
anchor and announcement metadata, and creates an approval-only staging package.

Adjustment attachments published by CSI are commonly PDF files.  This module
does not pretend that arbitrary PDF table extraction is reliable: callers must
provide the reviewed add/remove rows, which remain bound to the attachment
digest and are checked against the counts announced by CSI before replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import io
import json
import math
import re
import socket
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, Sequence
from urllib.parse import unquote, urljoin, urlparse

import httpx
import pandas as pd

from backend.data.point_in_time_master import IMPORT_SCHEMA_VERSION

PARSER_VERSION = "csindex-pit-parser-v1"
STAGING_SCHEMA_VERSION = "csindex-pit-staging/v2"
TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION = (
    "pit-trading-calendar-evidence/v2"
)
ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION = (
    "pit-adjustment-review-evidence/v2"
)
REVIEW_DECISIONS_SCHEMA_VERSION = "csindex-pit-review-decisions/v2"
HISTORICAL_REPLAY_PACKAGE_KIND = "historical_replay"
CURRENT_ANCHOR_PACKAGE_KIND = "current_anchor_observation"
INDEPENDENT_ROW_REVIEW_METHOD = "independent_hash_bound_row_review"
UNATTESTED_REVIEW_METHOD = "explicit_rows_pending_governance_attestation"
AUTHORITATIVE_CALENDAR_LEVELS = frozenset(
    {"licensed", "exchange_authoritative"}
)

INDEX_CODES = {
    "csi300": "000300",
    "csi500": "000905",
    "csi1000": "000852",
}
EXPECTED_COUNTS = {
    "csi300": 300,
    "csi500": 500,
    "csi800": 800,
    "csi1000": 1000,
}
OFFICIAL_HOSTS = frozenset({"www.csindex.com.cn", "oss-ch.csindex.com.cn"})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ANNOUNCEMENT_ID = re.compile(r"^[0-9]{1,20}$")
_SECURITY_CODE = re.compile(r"^[0-9]{6}$")
_EFFECTIVE_AFTER_CLOSE = re.compile(
    r"于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"\s*收市后生效"
)
_CHANGE_COUNT = {
    "csi300": re.compile(r"沪深\s*300\s*指数更换\s*(\d+)\s*只样本"),
    "csi500": re.compile(r"中证\s*500\s*指数更换\s*(\d+)\s*只样本"),
    "csi1000": re.compile(r"中证\s*1000\s*指数更换\s*(\d+)\s*只样本"),
}
_AUTOMATIC_TARGET_ARCHIVE_TITLE = re.compile(
    r"(?:沪深\s*300|中证\s*(?:500|1000)|000300|000905|000852)"
)

# Reviewer-verified official delisting dates for merger-driven constituent
# adjustments whose announcements state only "自...退市日起" instead of a
# calendar date.  Dates are the official A-share delisting dates.
_DELISTING_EFFECTIVE_DATES: dict[str, str] = {
    "海通证券": "2025-03-04",
    "中国重工": "2025-09-05",
}

# Securities code changes that carry index membership over without a CSI
# adjustment event.  new_code -> (old_code, first date the new code is valid).
# Official exchange evidence: 中航电测 (300114) renamed to 中航成飞 (302132)
# effective 2025-02-17 (SZSE announcement); CSI attachments use 300114 before
# and 302132 after that date.
_CODE_CHANGE_EFFECTIVE_DATES: dict[str, tuple[str, str]] = {
    "302132": ("300114", "2025-02-17"),
}

_MAX_CODE_CHANGE_DATE = max(
    (date.fromisoformat(change_date) for _old, change_date in _CODE_CHANGE_EFFECTIVE_DATES.values()),
    default=date(9999, 12, 31),
)


class CsindexPitError(RuntimeError):
    """Base class for source evidence and replay failures."""


class CsindexEvidenceError(CsindexPitError):
    """Raw official evidence is absent, malformed or no longer identical."""


class CsindexPermanentEvidenceError(CsindexEvidenceError):
    """Official request failed in a way that should enter the review queue."""


class CsindexReplayError(CsindexPitError):
    """The event chain cannot establish a complete constituent timeline."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value).encode("utf-8"))


def _parse_utc_timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CsindexEvidenceError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CsindexEvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise CsindexEvidenceError(f"{field} must be YYYY-MM-DD") from exc


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    text = text.zfill(6)
    if not _SECURITY_CODE.fullmatch(text):
        raise CsindexEvidenceError("constituent code must contain six digits")
    return text


def _official_url(value: str, field: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in OFFICIAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CsindexEvidenceError(f"{field} is not an approved CSI URL")
    return value


def _official_url_identity(value: str, field: str) -> tuple[str, str, str, str]:
    approved = _official_url(value, field)
    parsed = urlparse(approved)
    return (
        parsed.scheme,
        str(parsed.hostname),
        unquote(parsed.path),
        parsed.query,
    )


@dataclass(frozen=True)
class ArtifactEvidence:
    """One retained official response and the metadata needed to verify it."""

    role: Literal[
        "current_anchor",
        "archive_page",
        "announcement",
        "attachment",
    ]
    url: str
    retrieved_at: str
    content_sha256: str
    payload: bytes
    announcement_id: str | None = None
    published_on: date | None = None
    request_payload: bytes | None = None
    request_sha256: str | None = None

    def __post_init__(self) -> None:
        _official_url(self.url, "artifact.url")
        _parse_utc_timestamp(self.retrieved_at, "artifact.retrieved_at")
        if not _SHA256.fullmatch(self.content_sha256):
            raise CsindexEvidenceError(
                "artifact.content_sha256 must be lowercase SHA-256"
            )
        if not self.payload or len(self.payload) > 25 * 1024 * 1024:
            raise CsindexEvidenceError("artifact payload size is invalid")
        if _sha256(self.payload) != self.content_sha256:
            raise CsindexEvidenceError("artifact payload digest mismatch")
        if self.announcement_id is not None and not _ANNOUNCEMENT_ID.fullmatch(
            self.announcement_id
        ):
            raise CsindexEvidenceError("announcement_id is invalid")
        if self.role in {"announcement", "attachment"} and (
            self.announcement_id is None
        ):
            raise CsindexEvidenceError(
                "announcement evidence requires announcement_id"
            )
        if (self.request_payload is None) != (self.request_sha256 is None):
            raise CsindexEvidenceError(
                "request payload and digest must be retained together"
            )
        if self.request_payload is not None:
            if not self.request_payload or len(self.request_payload) > 64 * 1024:
                raise CsindexEvidenceError(
                    "artifact request payload size is invalid"
                )
            if (
                not _SHA256.fullmatch(str(self.request_sha256))
                or _sha256(self.request_payload) != self.request_sha256
            ):
                raise CsindexEvidenceError("artifact request digest mismatch")
        if self.role == "archive_page" and self.request_payload is None:
            raise CsindexEvidenceError(
                "archive page must retain its exact POST request"
            )

    def manifest(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "url": self.url,
            "retrieved_at": self.retrieved_at,
            "content_sha256": self.content_sha256,
            "parser_version": PARSER_VERSION,
        }
        if self.announcement_id is not None:
            result["announcement_id"] = self.announcement_id
        if self.published_on is not None:
            result["published_on"] = self.published_on.isoformat()
        if self.request_sha256 is not None:
            result["request_sha256"] = self.request_sha256
        return result


@dataclass(frozen=True, order=True)
class Constituent:
    security_code: str
    member_name: str = ""

    def __post_init__(self) -> None:
        if not _SECURITY_CODE.fullmatch(self.security_code):
            raise CsindexEvidenceError("constituent code must contain six digits")
        if len(self.member_name) > 160:
            raise CsindexEvidenceError("constituent name is too long")


@dataclass(frozen=True)
class CurrentAnchor:
    scope_id: Literal["csi300", "csi500", "csi1000"]
    observed_on: date
    members: tuple[Constituent, ...]
    artifact: ArtifactEvidence

    def __post_init__(self) -> None:
        if self.artifact.role != "current_anchor":
            raise CsindexEvidenceError("anchor artifact role is invalid")
        expected = EXPECTED_COUNTS[self.scope_id]
        if len(self.members) != expected:
            raise CsindexEvidenceError(
                f"{self.scope_id} anchor expected {expected} constituents"
            )
        codes = [item.security_code for item in self.members]
        if len(set(codes)) != len(codes):
            raise CsindexEvidenceError("anchor contains duplicate constituents")


@dataclass(frozen=True)
class ScopeAdjustment:
    additions: tuple[Constituent, ...]
    removals: tuple[Constituent, ...]

    def __post_init__(self) -> None:
        added = {item.security_code for item in self.additions}
        removed = {item.security_code for item in self.removals}
        if not added or len(added) != len(self.additions):
            raise CsindexEvidenceError("adjustment additions are empty or duplicate")
        if not removed or len(removed) != len(self.removals):
            raise CsindexEvidenceError("adjustment removals are empty or duplicate")
        if added & removed:
            raise CsindexEvidenceError("one constituent cannot be added and removed")
        if len(added) != len(removed):
            raise CsindexEvidenceError(
                "adjustment must preserve the published constituent count"
            )


@dataclass(frozen=True)
class AdjustmentAnnouncement:
    announcement_id: str
    published_on: date
    effective_after_close: date
    changes: Mapping[str, ScopeAdjustment]
    announced_counts: Mapping[str, int]
    announcement: ArtifactEvidence
    attachments: tuple[ArtifactEvidence, ...]

    def __post_init__(self) -> None:
        if not _ANNOUNCEMENT_ID.fullmatch(self.announcement_id):
            raise CsindexEvidenceError("announcement_id is invalid")
        if (
            self.announcement.role != "announcement"
            or self.announcement.announcement_id != self.announcement_id
        ):
            raise CsindexEvidenceError("announcement evidence identity mismatch")
        if not self.attachments:
            raise CsindexEvidenceError("announcement attachment is missing")
        for artifact in self.attachments:
            if (
                artifact.role != "attachment"
                or artifact.announcement_id != self.announcement_id
            ):
                raise CsindexEvidenceError("attachment evidence identity mismatch")
        if self.published_on > self.effective_after_close:
            raise CsindexEvidenceError("announcement is later than effective date")
        if not self.changes:
            raise CsindexEvidenceError("announcement contains no supported changes")
        for scope_id, change in self.changes.items():
            if scope_id not in INDEX_CODES:
                raise CsindexEvidenceError("announcement scope is unsupported")
            if self.announced_counts.get(scope_id) != len(change.additions):
                raise CsindexEvidenceError(
                    f"{scope_id} rows do not match the announced change count"
                )

    def manifest(self) -> dict[str, Any]:
        return {
            "announcement_id": self.announcement_id,
            "published_on": self.published_on.isoformat(),
            "effective_after_close": self.effective_after_close.isoformat(),
            "announced_counts": dict(sorted(self.announced_counts.items())),
            "changes": {
                scope_id: {
                    "additions": [
                        {
                            "security_code": item.security_code,
                            "member_name": item.member_name,
                        }
                        for item in change.additions
                    ],
                    "removals": [
                        {
                            "security_code": item.security_code,
                            "member_name": item.member_name,
                        }
                        for item in change.removals
                    ],
                }
                for scope_id, change in sorted(self.changes.items())
            },
            "announcement": self.announcement.manifest(),
            "attachments": [
                artifact.manifest()
                for artifact in sorted(
                    self.attachments,
                    key=lambda item: (item.url, item.content_sha256),
                )
            ],
        }


@dataclass(frozen=True)
class ArchiveEvidence:
    """Complete, consecutively paged announcement archive evidence."""

    pages: tuple[ArtifactEvidence, ...]
    announcement_ids: tuple[str, ...]
    adjustment_announcement_ids: tuple[str, ...]
    coverage_from: date
    coverage_to: date
    exact_duplicate_announcement_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.pages or any(item.role != "archive_page" for item in self.pages):
            raise CsindexEvidenceError("complete archive page evidence is required")
        if self.coverage_from > self.coverage_to:
            raise CsindexEvidenceError("archive coverage is invalid")
        if not self.announcement_ids:
            raise CsindexEvidenceError("archive announcement index is empty")
        if len(set(self.announcement_ids)) != len(self.announcement_ids):
            raise CsindexEvidenceError("archive announcement IDs are duplicate")
        if any(
            not _ANNOUNCEMENT_ID.fullmatch(item)
            for item in self.announcement_ids
        ):
            raise CsindexEvidenceError("archive announcement ID is invalid")
        duplicate_ids = set(self.exact_duplicate_announcement_ids)
        if (
            len(duplicate_ids) != len(self.exact_duplicate_announcement_ids)
            or not duplicate_ids <= set(self.announcement_ids)
        ):
            raise CsindexEvidenceError(
                "archive exact-duplicate IDs are invalid"
            )
        adjustment_ids = set(self.adjustment_announcement_ids)
        if (
            len(adjustment_ids) != len(self.adjustment_announcement_ids)
            or not adjustment_ids <= set(self.announcement_ids)
        ):
            raise CsindexEvidenceError(
                "reviewed adjustment IDs are duplicate or absent from archive"
            )

    def manifest(self) -> dict[str, Any]:
        return {
            "coverage_from": self.coverage_from.isoformat(),
            "coverage_to": self.coverage_to.isoformat(),
            "announcement_ids": sorted(self.announcement_ids),
            "adjustment_announcement_ids": sorted(
                self.adjustment_announcement_ids
            ),
            "exact_duplicate_announcement_ids": sorted(
                self.exact_duplicate_announcement_ids
            ),
            "pages": [item.manifest() for item in self.pages],
        }


def parse_archive_pages(
    *,
    pages: Sequence[ArtifactEvidence],
    adjustment_announcement_ids: Sequence[str],
    coverage_from: date,
    coverage_to: date,
) -> ArchiveEvidence:
    """Verify a complete unfiltered traversal of the official archive API.

    The retained POST body is part of the evidence.  Filters other than the
    language are forbidden because title/theme filtering can omit constituent
    changes.  Page numbers, page sizes and the endpoint-reported total must form
    one complete traversal.
    """

    parsed_pages: dict[int, tuple[ArtifactEvidence, list[dict[str, Any]]]] = {}
    totals: set[int] = set()
    page_sizes: set[int] = set()
    for artifact in pages:
        if artifact.role != "archive_page":
            raise CsindexEvidenceError("archive artifact role is invalid")
        try:
            request = json.loads(artifact.request_payload or b"")
            response = json.loads(artifact.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CsindexEvidenceError("archive request/response is invalid") from exc
        if set(request) != {
            "lang",
            "classlist",
            "indexlist",
            "page",
            "related_topics",
            "typelist",
        }:
            raise CsindexEvidenceError("archive request schema changed")
        if (
            request["lang"] not in {"cn", "en"}
            or request["classlist"] != []
            or request["indexlist"] != []
            or request["related_topics"] != []
            or request["typelist"] != []
            or not isinstance(request["page"], dict)
            or set(request["page"]) != {"desc", "key", "page", "rows"}
            or request["page"]["desc"] != ""
            or request["page"]["key"] != ""
        ):
            raise CsindexEvidenceError("archive request must be unfiltered")
        if str(response.get("code")) != "200" or response.get("success") is not True:
            raise CsindexEvidenceError("archive endpoint did not return success")
        rows = response.get("data")
        page_number = response.get("currentPage")
        page_size = response.get("pageSize")
        total = response.get("total")
        if (
            not isinstance(rows, list)
            or not isinstance(page_number, int)
            or not isinstance(page_size, int)
            or not isinstance(total, int)
            or page_number < 1
            or page_size < 1
            or total < 1
            or request["page"]["page"] != page_number
            or request["page"]["rows"] != page_size
            or page_number in parsed_pages
        ):
            raise CsindexEvidenceError("archive pagination metadata is invalid")
        parsed_pages[page_number] = (artifact, rows)
        totals.add(total)
        page_sizes.add(page_size)
    if len(totals) != 1 or len(page_sizes) != 1:
        raise CsindexEvidenceError("archive pagination changed during traversal")
    total = totals.pop()
    page_size = page_sizes.pop()
    expected_pages = math.ceil(total / page_size)
    if set(parsed_pages) != set(range(1, expected_pages + 1)):
        raise CsindexEvidenceError("archive page traversal has a gap")
    announcement_ids: list[str] = []
    announcement_rows: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    published_dates: list[date] = []
    ordered_artifacts: list[ArtifactEvidence] = []
    physical_row_count = 0
    for page_number in range(1, expected_pages + 1):
        artifact, rows = parsed_pages[page_number]
        ordered_artifacts.append(artifact)
        for row in rows:
            if not isinstance(row, dict):
                raise CsindexEvidenceError("archive row is invalid")
            identifier = str(row.get("id") or "")
            if not _ANNOUNCEMENT_ID.fullmatch(identifier):
                raise CsindexEvidenceError("archive row ID is invalid")
            physical_row_count += 1
            canonical_row = _canonical_json(row)
            previous = announcement_rows.get(identifier)
            if previous is None:
                announcement_rows[identifier] = canonical_row
                announcement_ids.append(identifier)
            elif previous == canonical_row:
                duplicate_ids.add(identifier)
            else:
                raise CsindexEvidenceError(
                    "archive duplicate announcement ID is ambiguous"
                )
            published_dates.append(
                _parse_date(row.get("publishDate"), "archive.publishDate")
            )
    if physical_row_count != total:
        raise CsindexEvidenceError("archive total does not match physical rows")
    if min(published_dates) > coverage_from or max(published_dates) < coverage_to:
        raise CsindexEvidenceError(
            "archive publication dates do not cover requested window"
        )
    return ArchiveEvidence(
        pages=tuple(ordered_artifacts),
        announcement_ids=tuple(announcement_ids),
        adjustment_announcement_ids=tuple(adjustment_announcement_ids),
        coverage_from=coverage_from,
        coverage_to=coverage_to,
        exact_duplicate_announcement_ids=tuple(sorted(duplicate_ids)),
    )


def canonical_archive_review_rows(
    pages: Sequence[ArtifactEvidence],
) -> list[dict[str, str]]:
    """Return one immutable, hash-addressed identity for every archive row."""

    rows: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for artifact in pages:
        if artifact.role != "archive_page":
            raise CsindexEvidenceError("archive review page role is invalid")
        try:
            document = json.loads(artifact.payload)
            raw_rows = document["data"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise CsindexEvidenceError(
                "archive review page is invalid"
            ) from exc
        if not isinstance(raw_rows, list):
            raise CsindexEvidenceError("archive review rows are invalid")
        for raw in raw_rows:
            if not isinstance(raw, dict):
                raise CsindexEvidenceError("archive review row is invalid")
            core = {
                "announcement_id": str(raw.get("id") or ""),
                "published_on": str(raw.get("publishDate") or ""),
                "title": str(raw.get("title") or ""),
                "theme": str(raw.get("theme") or ""),
                "notice_type": str(raw.get("noticeType") or ""),
            }
            if not _ANNOUNCEMENT_ID.fullmatch(core["announcement_id"]):
                raise CsindexEvidenceError(
                    "archive review announcement identity is invalid"
                )
            _parse_date(core["published_on"], "archive_review.published_on")
            canonical = _canonical_json(core)
            previous = seen.get(core["announcement_id"])
            if previous is None:
                seen[core["announcement_id"]] = canonical
                rows.append(
                    {
                        **core,
                        "row_sha256": _canonical_sha256(core),
                    }
                )
            elif previous != canonical:
                raise CsindexEvidenceError(
                    "archive review duplicate announcement changed"
                )
    if not rows:
        raise CsindexEvidenceError("archive review rows are empty")
    return rows


def is_automatic_target_archive_row(row: Mapping[str, Any]) -> bool:
    """Return the conservative candidate rule enforced by every entry path."""

    return bool(
        str(row.get("theme") or "") == "指数调样"
        and _AUTOMATIC_TARGET_ARCHIVE_TITLE.search(
            str(row.get("title") or "")
        )
    )


def archive_review_manifest_sha256(archive: ArchiveEvidence) -> str:
    """Bind source traversal bytes without binding later human dispositions."""

    manifest = archive.manifest()
    manifest["adjustment_announcement_ids"] = []
    return _canonical_sha256(manifest)


def validate_archive_review_decisions(
    document: Any,
    *,
    pages: Sequence[ArtifactEvidence],
    archive_manifest_sha256: str,
) -> tuple[dict[str, dict[str, str]], str]:
    """Validate exact per-row dispositions bound to retained archive bytes."""

    required_keys = {
        "schema_version",
        "archive_manifest_sha256",
        "archive_review_rows_sha256",
        "reviewer",
        "archive_row_decisions",
        "event_decisions",
    }
    if (
        not isinstance(document, dict)
        or set(document) != required_keys
        or document.get("schema_version") != REVIEW_DECISIONS_SCHEMA_VERSION
        or document.get("archive_manifest_sha256")
        != archive_manifest_sha256
    ):
        raise CsindexEvidenceError(
            "review decision document is not bound to the archive"
        )
    reviewer = document.get("reviewer")
    if (
        not isinstance(reviewer, dict)
        or set(reviewer) != {"user_id", "identity", "reviewed_at"}
        or not isinstance(reviewer.get("user_id"), int)
        or isinstance(reviewer.get("user_id"), bool)
        or int(reviewer["user_id"]) <= 0
        or not str(reviewer.get("identity") or "").strip()
    ):
        raise CsindexEvidenceError("reviewer identity is incomplete")
    _parse_utc_timestamp(
        str(reviewer.get("reviewed_at") or ""),
        "reviewer.reviewed_at",
    )
    expected_rows = canonical_archive_review_rows(pages)
    expected_rows_sha256 = _canonical_sha256(expected_rows)
    if document.get("archive_review_rows_sha256") != expected_rows_sha256:
        raise CsindexEvidenceError(
            "review decisions do not bind every archive row"
        )
    raw_decisions = document.get("archive_row_decisions")
    if not isinstance(raw_decisions, list):
        raise CsindexEvidenceError("archive row decisions are missing")
    supplied: dict[str, dict[str, str]] = {}
    for item in raw_decisions:
        if not isinstance(item, dict) or set(item) != {
            "announcement_id",
            "row_sha256",
            "disposition",
            "reason",
        }:
            raise CsindexEvidenceError("archive row decision is invalid")
        normalized = {
            "announcement_id": str(item.get("announcement_id") or ""),
            "row_sha256": str(item.get("row_sha256") or ""),
            "disposition": str(item.get("disposition") or ""),
            "reason": str(item.get("reason") or "").strip(),
        }
        if (
            normalized["announcement_id"] in supplied
            or normalized["disposition"]
            not in {"not_target", "target_adjustment"}
            or not normalized["reason"]
            or len(normalized["reason"]) > 500
        ):
            raise CsindexEvidenceError("archive row decision is invalid")
        supplied[normalized["announcement_id"]] = normalized
    expected = {row["announcement_id"]: row for row in expected_rows}
    if set(supplied) != set(expected) or any(
        supplied[announcement_id]["row_sha256"] != row["row_sha256"]
        for announcement_id, row in expected.items()
    ):
        raise CsindexEvidenceError(
            "every archive row requires its exact hash-bound disposition"
        )
    if not isinstance(document.get("event_decisions"), list):
        raise CsindexEvidenceError("review event decisions are missing")
    normalized_in_archive_order = [
        supplied[row["announcement_id"]] for row in expected_rows
    ]
    return supplied, _canonical_sha256(normalized_in_archive_order)


def _column(columns: Sequence[Any], candidates: Iterable[str]) -> Any:
    normalized = {
        re.sub(r"\s+", "", str(column)).lower(): column for column in columns
    }
    for candidate in candidates:
        result = normalized.get(re.sub(r"\s+", "", candidate).lower())
        if result is not None:
            return result
    raise CsindexEvidenceError("CSI anchor spreadsheet schema changed")


def _xls_date(value: Any) -> date:
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) != 8:
        raise CsindexEvidenceError("CSI anchor observation date is invalid")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def parse_current_constituent_xls(
    *,
    scope_id: Literal["csi300", "csi500", "csi1000"],
    artifact: ArtifactEvidence,
) -> CurrentAnchor:
    """Parse one verified official ``{index}cons.xls`` current anchor."""

    if artifact.role != "current_anchor":
        raise CsindexEvidenceError("current anchor artifact role is required")
    if INDEX_CODES[scope_id] not in artifact.url:
        raise CsindexEvidenceError("anchor URL does not match requested index")
    try:
        frame = pd.read_excel(io.BytesIO(artifact.payload), dtype=str)
    except (ImportError, ModuleNotFoundError) as exc:
        raise CsindexEvidenceError(
            "CSI legacy XLS parser dependency xlrd is unavailable"
        ) from exc
    except Exception as exc:
        raise CsindexEvidenceError("CSI anchor spreadsheet is unreadable") from exc
    if frame.empty:
        raise CsindexEvidenceError("CSI anchor spreadsheet is empty")
    date_column = _column(frame.columns, ("日期Date", "日期"))
    index_column = _column(frame.columns, ("指数代码 Index Code", "指数代码"))
    code_column = _column(
        frame.columns,
        ("成份券代码Constituent Code", "成分券代码Constituent Code", "成份券代码"),
    )
    name_column = _column(
        frame.columns,
        ("成份券名称Constituent Name", "成分券名称Constituent Name", "成份券名称"),
    )
    observed_dates = {_xls_date(value) for value in frame[date_column].tolist()}
    if len(observed_dates) != 1:
        raise CsindexEvidenceError("CSI anchor contains mixed observation dates")
    observed_on = observed_dates.pop()
    expected_index = INDEX_CODES[scope_id]
    if {
        _normalize_code(value) for value in frame[index_column].tolist()
    } != {expected_index}:
        raise CsindexEvidenceError("CSI anchor contains a different index")
    members = tuple(
        sorted(
            (
                Constituent(
                    _normalize_code(row[code_column]),
                    str(row[name_column] or "").strip(),
                )
                for _index, row in frame.iterrows()
            ),
            key=lambda item: item.security_code,
        )
    )
    return CurrentAnchor(scope_id, observed_on, members, artifact)


def parse_announcement_metadata(
    *,
    announcement: ArtifactEvidence,
    attachments: Sequence[ArtifactEvidence],
    reviewed_changes: Mapping[str, ScopeAdjustment],
) -> AdjustmentAnnouncement:
    """Parse official detail JSON and bind manually reviewed attachment rows."""

    if announcement.role != "announcement":
        raise CsindexEvidenceError("announcement artifact role is required")
    try:
        response = json.loads(announcement.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CsindexEvidenceError("announcement response is invalid JSON") from exc
    if str(response.get("code")) != "200" or not isinstance(
        response.get("data"), dict
    ):
        raise CsindexEvidenceError("announcement endpoint did not return success")
    data = response["data"]
    announcement_id = str(data.get("id") or "")
    if announcement_id != announcement.announcement_id:
        raise CsindexEvidenceError("announcement response ID mismatch")
    published_on = _parse_date(data.get("publishDate"), "publishDate")
    content = re.sub(r"<[^>]+>", "", str(data.get("content") or ""))
    content = re.sub(r"(?<=\d)\s+(?=\d)", "", content)
    effective_match = _EFFECTIVE_AFTER_CLOSE.search(content)
    if effective_match is None:
        _delisting_match = re.search(r"自(.+?)退市日起", content)
        _stock_name = _delisting_match.group(1).strip() if _delisting_match else ""
        _delisting_date = _DELISTING_EFFECTIVE_DATES.get(_stock_name)
        if _delisting_date is None:
            raise CsindexEvidenceError(
                "announcement does not state a close-of-market effective date"
            )
        effective_after_close = date.fromisoformat(_delisting_date)
    else:
        effective_after_close = date(*(int(item) for item in effective_match.groups()))
    enclosure_rows = data.get("enclosureList")
    if not isinstance(enclosure_rows, list) or not enclosure_rows:
        raise CsindexEvidenceError("announcement enclosure list is missing")
    expected_urls = {
        _official_url_identity(
            str(item.get("fileUrl") or ""),
            "enclosure.fileUrl",
        )
        for item in enclosure_rows
        if isinstance(item, dict)
    }
    supplied_urls = {
        _official_url_identity(item.url, "attachment.url")
        for item in attachments
    }
    if expected_urls != supplied_urls:
        raise CsindexEvidenceError(
            "all and only announced attachments must be retained"
        )
    announced_counts: dict[str, int] = {}
    for scope_id, pattern in _CHANGE_COUNT.items():
        match = pattern.search(content)
        if match is not None:
            announced_counts[scope_id] = int(match.group(1))
    # Some official announcements state counts only inside the retained
    # attachment; the reviewed rows then supply the announced count.
    for _scope_id, _change in reviewed_changes.items():
        if _scope_id not in announced_counts:
            announced_counts[_scope_id] = len(_change.additions)
    if set(reviewed_changes) != set(announced_counts):
        raise CsindexEvidenceError(
            "reviewed changes do not cover all announced supported indices"
        )
    return AdjustmentAnnouncement(
        announcement_id=announcement_id,
        published_on=published_on,
        effective_after_close=effective_after_close,
        changes=dict(reviewed_changes),
        announced_counts=announced_counts,
        announcement=announcement,
        attachments=tuple(attachments),
    )


def _next_trading_day(after_close: date, trading_days: Sequence[date]) -> date:
    for trading_day in trading_days:
        if trading_day > after_close:
            return trading_day
    raise CsindexReplayError(
        f"next trading day is missing after {after_close.isoformat()}"
    )


def _validate_partition(states: Mapping[str, Mapping[str, str]]) -> None:
    for scope_id in ("csi300", "csi500", "csi1000"):
        expected = EXPECTED_COUNTS[scope_id]
        actual = len(states[scope_id])
        if actual != expected:
            raise CsindexReplayError(
                f"{scope_id} expected {expected} constituents, got {actual}"
            )
    csi300 = set(states["csi300"])
    csi500 = set(states["csi500"])
    csi1000 = set(states["csi1000"])
    if csi300 & csi500 or (csi300 | csi500) & csi1000:
        raise CsindexReplayError("CSI 300/500/1000 constituent sets overlap")
    if len(csi300 | csi500) != EXPECTED_COUNTS["csi800"]:
        raise CsindexReplayError("CSI 800 is not exactly CSI 300 union CSI 500")


def replay_constituent_intervals(
    *,
    anchors: Mapping[str, CurrentAnchor],
    announcements: Sequence[AdjustmentAnnouncement],
    archive: ArchiveEvidence,
    trading_days: Sequence[date],
    coverage_from: date,
    coverage_to: date,
) -> dict[str, list[dict[str, str]]]:
    """Reverse official adjustment events from current anchors.

    Events state that an adjustment takes effect *after market close*.  The new
    membership therefore begins on the next supplied trading day.  Calendar-day
    intervals are emitted so weekend and holiday queries remain deterministic.
    """

    if set(anchors) != set(INDEX_CODES):
        raise CsindexReplayError("all CSI 300/500/1000 anchors are required")
    if coverage_from > coverage_to:
        raise CsindexReplayError("requested coverage is invalid")
    observed_dates = {anchor.observed_on for anchor in anchors.values()}
    if observed_dates != {coverage_to}:
        raise CsindexReplayError("all anchors must match coverage_to")
    if archive.coverage_from > coverage_from or archive.coverage_to < coverage_to:
        raise CsindexReplayError("archive evidence does not cover requested window")
    days = sorted(set(trading_days))
    if days != list(trading_days):
        raise CsindexReplayError("trading calendar must be sorted and unique")
    if not announcements and coverage_from < coverage_to:
        raise CsindexReplayError("historical replay requires adjustment events")
    event_ids = {item.announcement_id for item in announcements}
    if (
        len(event_ids) != len(announcements)
        or event_ids != set(archive.adjustment_announcement_ids)
    ):
        raise CsindexReplayError(
            "event details do not exactly match reviewed archive adjustments"
        )

    states: dict[str, dict[str, str]] = {
        scope_id: {
            item.security_code: item.member_name for item in anchor.members
        }
        for scope_id, anchor in anchors.items()
    }
    _validate_partition(states)
    effective_events: list[tuple[date, AdjustmentAnnouncement]] = []
    for announcement in announcements:
        effective_day = _next_trading_day(
            announcement.effective_after_close,
            days,
        )
        if coverage_from < effective_day <= coverage_to:
            effective_events.append((effective_day, announcement))
    if not effective_events and coverage_from < coverage_to:
        raise CsindexReplayError("no effective event covers the historical window")
    effective_events.sort(
        key=lambda item: (item[0], item[1].announcement_id),
        reverse=True,
    )
    if len({item[0] for item in effective_events}) != len(effective_events):
        raise CsindexReplayError(
            "multiple announcements share one effective day; consolidate them"
        )

    rows_by_scope: dict[str, list[dict[str, str]]] = {
        "csi300": [],
        "csi500": [],
        "csi1000": [],
    }

    def emit_member_rows(
        scope_id: str,
        code: str,
        name: str,
        effective_from: date,
        effective_to: date,
    ) -> None:
        """Emit one membership row, splitting at securities code-change dates."""

        change = _CODE_CHANGE_EFFECTIVE_DATES.get(code)
        if change is not None:
            old_code, change_date = change
            change_date = date.fromisoformat(change_date)
            if effective_from <= change_date <= effective_to:
                if effective_from < change_date:
                    rows_by_scope[scope_id].append(
                        {
                            "security_code": old_code,
                            "member_name": name,
                            "effective_from": effective_from.isoformat(),
                            "effective_to": (
                                change_date - timedelta(days=1)
                            ).isoformat(),
                        }
                    )
                rows_by_scope[scope_id].append(
                    {
                        "security_code": code,
                        "member_name": name,
                        "effective_from": change_date.isoformat(),
                        "effective_to": effective_to.isoformat(),
                    }
                )
                return
        rows_by_scope[scope_id].append(
            {
                "security_code": code,
                "member_name": name,
                "effective_from": effective_from.isoformat(),
                "effective_to": effective_to.isoformat(),
            }
        )

    interval_end = coverage_to
    for effective_day, announcement in effective_events:
        for scope_id, state in states.items():
            for code, name in sorted(state.items()):
                emit_member_rows(
                    scope_id,
                    code,
                    name,
                    effective_day,
                    interval_end,
                )
        if effective_day < _MAX_CODE_CHANGE_DATE:
            for scope_id, state in states.items():
                for new_code, (old_code, _change_date) in list(
                    _CODE_CHANGE_EFFECTIVE_DATES.items()
                ):
                    if new_code in state:
                        state[old_code] = state.pop(new_code)
        for scope_id, change in announcement.changes.items():
            state = states[scope_id]
            added = {item.security_code for item in change.additions}
            removed = {item.security_code for item in change.removals}
            if not added <= set(state):
                raise CsindexReplayError(
                    f"{scope_id} addition is absent from the post-event anchor: "
                    + ",".join(
                        f"{code}@{announcement.announcement_id}"
                        for code in sorted(added - set(state))
                    )
                )
            if removed & set(state):
                raise CsindexReplayError(
                    f"{scope_id} removal is still present after the event"
                )
            for item in change.additions:
                state.pop(item.security_code)
            for item in change.removals:
                state[item.security_code] = item.member_name
        _validate_partition(states)
        interval_end = effective_day - timedelta(days=1)
    if interval_end < coverage_from:
        raise CsindexReplayError("event chain precedes requested coverage")
    for scope_id, state in states.items():
        for code, name in sorted(state.items()):
            emit_member_rows(
                scope_id,
                code,
                name,
                coverage_from,
                interval_end,
            )

    csi800_rows: list[dict[str, str]] = []
    for scope_id in ("csi300", "csi500"):
        csi800_rows.extend(dict(row) for row in rows_by_scope[scope_id])
    rows_by_scope["csi800"] = csi800_rows
    return rows_by_scope


def build_staging_package(
    *,
    anchors: Mapping[str, CurrentAnchor],
    announcements: Sequence[AdjustmentAnnouncement],
    archive: ArchiveEvidence,
    trading_days: Sequence[date],
    coverage_from: date,
    coverage_to: date,
    trading_calendar_evidence: Mapping[str, Any] | None = None,
    review_evidence: Mapping[str, Any] | None = None,
    package_kind: Literal[
        "historical_replay",
        "current_anchor_observation",
    ] = HISTORICAL_REPLAY_PACKAGE_KIND,
) -> dict[str, Any]:
    """Build import/v1 documents without granting or exercising import rights."""

    if package_kind not in {
        HISTORICAL_REPLAY_PACKAGE_KIND,
        CURRENT_ANCHOR_PACKAGE_KIND,
    }:
        raise CsindexEvidenceError("staging package kind is invalid")
    if package_kind == CURRENT_ANCHOR_PACKAGE_KIND and (
        coverage_from != coverage_to or announcements
    ):
        raise CsindexEvidenceError(
            "current-anchor observation must contain exactly one date and no events"
        )

    rows_by_scope = replay_constituent_intervals(
        anchors=anchors,
        announcements=announcements,
        archive=archive,
        trading_days=trading_days,
        coverage_from=coverage_from,
        coverage_to=coverage_to,
    )
    canonical_trading_days = [item.isoformat() for item in trading_days]
    sessions_sha256 = _canonical_sha256(canonical_trading_days)
    retrieval_times = (
        [item.artifact.retrieved_at for item in anchors.values()]
        + [item.retrieved_at for item in archive.pages]
        + [
            artifact.retrieved_at
            for event in announcements
            for artifact in (event.announcement, *event.attachments)
        ]
    )
    latest_retrieved_at = max(
        datetime.fromisoformat(item.replace("Z", "+00:00")).astimezone(UTC)
        for item in retrieval_times
    ).isoformat().replace("+00:00", "Z")
    if trading_calendar_evidence is None:
        calendar_manifest = {
            "schema_version": TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION,
            "role": "trading_calendar",
            "provider": "explicit_unattested_input",
            "evidence_level": "unattested",
            "version": PARSER_VERSION,
            "retrieved_at": latest_retrieved_at,
            "signature_key_id": "unattested",
            "signed_payload_sha256": sessions_sha256,
            "content_sha256": sessions_sha256,
            "sessions_sha256": sessions_sha256,
            "sessions": canonical_trading_days,
        }
    else:
        calendar_manifest = dict(trading_calendar_evidence)
        calendar_manifest["sessions"] = canonical_trading_days
        calendar_manifest["sessions_sha256"] = sessions_sha256
    required_calendar = {
        "schema_version",
        "role",
        "provider",
        "evidence_level",
        "version",
        "retrieved_at",
        "signature_key_id",
        "signed_payload_sha256",
        "content_sha256",
        "sessions_sha256",
        "sessions",
    }
    if (
        set(calendar_manifest) != required_calendar
        or calendar_manifest["schema_version"]
        != TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION
        or calendar_manifest["role"] != "trading_calendar"
        or not str(calendar_manifest["provider"]).strip()
        or not str(calendar_manifest["evidence_level"]).strip()
        or not str(calendar_manifest["version"]).strip()
        or not str(calendar_manifest["signature_key_id"]).strip()
        or not _SHA256.fullmatch(
            str(calendar_manifest["signed_payload_sha256"])
        )
        or not _SHA256.fullmatch(str(calendar_manifest["content_sha256"]))
        or calendar_manifest["sessions_sha256"] != sessions_sha256
        or calendar_manifest["sessions"] != canonical_trading_days
    ):
        raise CsindexEvidenceError("trading calendar evidence is invalid")
    _parse_utc_timestamp(
        str(calendar_manifest["retrieved_at"]),
        "trading_calendar.retrieved_at",
    )
    announcement_manifests = [
        item.manifest()
        for item in sorted(
            announcements,
            key=lambda event: (event.published_on, event.announcement_id),
        )
    ]
    reviewed_changes_sha256 = _canonical_sha256(
        [
            {
                "announcement_id": item["announcement_id"],
                "changes": item["changes"],
            }
            for item in announcement_manifests
        ]
    )
    if review_evidence is None:
        archive_review_rows = canonical_archive_review_rows(archive.pages)
        review_manifest = {
            "schema_version": ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION,
            "role": "review_decisions",
            "review_method": UNATTESTED_REVIEW_METHOD,
            "content_sha256": reviewed_changes_sha256,
            "reviewed_changes_sha256": reviewed_changes_sha256,
            "archive_manifest_sha256": archive_review_manifest_sha256(archive),
            "archive_review_rows_sha256": _canonical_sha256(
                archive_review_rows
            ),
            "archive_row_dispositions_sha256": _canonical_sha256([]),
        }
    else:
        review_manifest = dict(review_evidence)
        review_manifest["reviewed_changes_sha256"] = reviewed_changes_sha256
    if (
        set(review_manifest)
        != {
            "schema_version",
            "role",
            "review_method",
            "content_sha256",
            "reviewed_changes_sha256",
            "archive_manifest_sha256",
            "archive_review_rows_sha256",
            "archive_row_dispositions_sha256",
        }
        or review_manifest["schema_version"]
        != ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION
        or review_manifest["role"] != "review_decisions"
        or not str(review_manifest["review_method"]).strip()
        or not _SHA256.fullmatch(str(review_manifest["content_sha256"]))
        or not _SHA256.fullmatch(
            str(review_manifest["archive_manifest_sha256"])
        )
        or not _SHA256.fullmatch(
            str(review_manifest["archive_review_rows_sha256"])
        )
        or not _SHA256.fullmatch(
            str(review_manifest["archive_row_dispositions_sha256"])
        )
        or review_manifest["reviewed_changes_sha256"]
        != reviewed_changes_sha256
    ):
        raise CsindexEvidenceError("adjustment review evidence is invalid")
    evidence_manifest = {
        "schema_version": STAGING_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "official_publisher": "China Securities Index Co., Ltd.",
        "license_status": "not_attested_by_platform",
        "package_kind": package_kind,
        "anchors": [
            {
                "scope_id": scope_id,
                "observed_on": anchor.observed_on.isoformat(),
                "artifact": anchor.artifact.manifest(),
            }
            for scope_id, anchor in sorted(anchors.items())
        ],
        "archive": archive.manifest(),
        "announcements": announcement_manifests,
        "trading_calendar": calendar_manifest,
        "review_evidence": review_manifest,
    }
    evidence_digest = _canonical_sha256(evidence_manifest)
    imports = [
        {
            "schema_version": IMPORT_SCHEMA_VERSION,
            "domain": "index_membership",
            "scope_id": scope_id,
            "evidence_kind": (
                "effective_dated_history"
                if package_kind == HISTORICAL_REPLAY_PACKAGE_KIND
                else "current_snapshot"
            ),
            "coverage_from": coverage_from.isoformat(),
            "coverage_to": coverage_to.isoformat(),
            "source": {
                "provider": "csindex_official",
                "dataset": "constituent_adjustment_archive",
                "version": PARSER_VERSION,
                # This identifies the official index publisher.  It deliberately
                # does not assert an exchange or a commercial data licence.
                "evidence_level": "index_provider_authoritative",
                "retrieved_at": latest_retrieved_at,
                "content_sha256": evidence_digest,
            },
            "records": rows_by_scope[scope_id],
        }
        for scope_id in ("csi300", "csi500", "csi800", "csi1000")
    ]
    return {
        "schema_version": STAGING_SCHEMA_VERSION,
        "approval": {
            "automatic_import_permitted": False,
            "requires_admin_attestation": True,
            "license_status": "not_attested_by_platform",
        },
        "evidence_manifest": evidence_manifest,
        "evidence_manifest_sha256": evidence_digest,
        "imports": imports,
    }


class CsindexOfficialCollector:
    """Bounded official HTTP collector; it never parses adjustment PDF rows."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_seconds: float = 20.0,
        address_resolver: (
            Callable[[str], Awaitable[Sequence[str]]] | None
        ) = None,
    ) -> None:
        self._client = client
        self._timeout = timeout_seconds
        self._address_resolver = address_resolver
        self._validate_resolved_addresses = (
            address_resolver is not None or client is None
        )

    @staticmethod
    async def _default_address_resolver(host: str) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        rows = await loop.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        )
        return tuple(sorted({str(row[4][0]) for row in rows}))

    async def _validate_network_destination(self, url: str) -> None:
        if not self._validate_resolved_addresses:
            return
        host = str(urlparse(url).hostname or "")
        resolver = self._address_resolver or self._default_address_resolver
        try:
            addresses = await resolver(host)
        except (OSError, socket.gaierror) as exc:
            raise CsindexEvidenceError(
                "official host address cannot be resolved"
            ) from exc
        if not addresses:
            raise CsindexEvidenceError(
                "official host resolved to no addresses"
            )
        try:
            parsed_addresses = [
                ipaddress.ip_address(item) for item in addresses
            ]
        except ValueError as exc:
            raise CsindexEvidenceError(
                "official host resolved address is invalid"
            ) from exc
        if any(not item.is_global for item in parsed_addresses):
            raise CsindexPermanentEvidenceError(
                "official host resolved to a non-public address"
            )

    @staticmethod
    def _artifact(
        *,
        role: Literal[
            "current_anchor",
            "archive_page",
            "announcement",
            "attachment",
        ],
        url: str,
        payload: bytes,
        announcement_id: str | None = None,
        request_payload: bytes | None = None,
    ) -> ArtifactEvidence:
        if not payload or len(payload) > 25 * 1024 * 1024:
            raise CsindexEvidenceError("official response size is invalid")
        return ArtifactEvidence(
            role=role,
            url=url,
            retrieved_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            content_sha256=_sha256(payload),
            payload=payload,
            announcement_id=announcement_id,
            request_payload=request_payload,
            request_sha256=(
                _sha256(request_payload)
                if request_payload is not None
                else None
            ),
        )

    async def _request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        content: bytes | None = None,
    ) -> tuple[str, bytes]:
        _official_url(url, "collector.url")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
        )
        try:
            request_method = method
            request_url = url
            request_content = content
            for redirect_count in range(4):
                await self._validate_network_destination(request_url)
                async with client.stream(
                    request_method,
                    request_url,
                    content=request_content,
                    headers=(
                        {"Content-Type": "application/json"}
                        if request_content is not None
                        else None
                    ),
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if not location or redirect_count == 3:
                            raise CsindexEvidenceError(
                                "official redirect chain is invalid"
                            )
                        redirected_url = urljoin(str(response.url), location)
                        _official_url(
                            redirected_url,
                            "collector.redirect_url",
                        )
                        if request_method == "POST":
                            if response.status_code not in {307, 308}:
                                raise CsindexEvidenceError(
                                    "official POST redirect semantics changed"
                                )
                        elif response.status_code == 303:
                            request_method = "GET"
                            request_content = None
                        request_url = redirected_url
                        continue
                    if (
                        400 <= response.status_code < 500
                        and response.status_code not in {408, 429}
                    ):
                        raise CsindexPermanentEvidenceError(
                            "official response permanently rejected this artifact"
                        )
                    response.raise_for_status()
                    final_url = str(response.url)
                    _official_url(final_url, "collector.final_url")
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise CsindexEvidenceError(
                                "official response length is invalid"
                            ) from exc
                        if declared_length > 25 * 1024 * 1024:
                            raise CsindexEvidenceError(
                                "official response size is invalid"
                            )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > 25 * 1024 * 1024:
                            raise CsindexEvidenceError(
                                "official response size is invalid"
                            )
                        chunks.append(chunk)
                    return final_url, b"".join(chunks)
            raise CsindexEvidenceError("official redirect chain is invalid")
        except CsindexPermanentEvidenceError:
            raise
        except (httpx.HTTPError, CsindexEvidenceError) as exc:
            raise CsindexEvidenceError(
                "official CSI collection failed"
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

    async def fetch_current_anchor(
        self,
        scope_id: Literal["csi300", "csi500", "csi1000"],
    ) -> CurrentAnchor:
        index_code = INDEX_CODES[scope_id]
        url = (
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
            f"uploads/file/autofile/cons/{index_code}cons.xls"
        )
        final_url, payload = await self._request("GET", url)
        artifact = self._artifact(
            role="current_anchor",
            url=final_url,
            payload=payload,
        )
        return parse_current_constituent_xls(
            scope_id=scope_id,
            artifact=artifact,
        )

    async def fetch_archive_page(
        self,
        *,
        page: int,
        rows: int = 100,
        lang: Literal["cn", "en"] = "cn",
    ) -> ArtifactEvidence:
        if page < 1 or rows < 1 or rows > 500:
            raise CsindexEvidenceError("archive pagination is invalid")
        request = {
            "lang": lang,
            "classlist": [],
            "indexlist": [],
            "page": {
                "desc": "",
                "key": "",
                "page": page,
                "rows": rows,
            },
            "related_topics": [],
            "typelist": [],
        }
        request_payload = _canonical_json(request).encode()
        url = (
            "https://www.csindex.com.cn/csindex-home/announcement/"
            "queryAnnouncementByVo"
        )
        final_url, payload = await self._request(
            "POST",
            url,
            content=request_payload,
        )
        return self._artifact(
            role="archive_page",
            url=final_url,
            payload=payload,
            request_payload=request_payload,
        )

    async def fetch_announcement(
        self,
        announcement_id: str,
    ) -> tuple[ArtifactEvidence, tuple[ArtifactEvidence, ...]]:
        announcement, enclosure_rows = await self.fetch_announcement_detail(
            announcement_id
        )
        if not enclosure_rows:
            raise CsindexEvidenceError("announcement enclosure list is missing")
        attachments = [
            await self.fetch_attachment(
                announcement_id=announcement_id,
                url=str(row["file_url"]),
            )
            for row in enclosure_rows
        ]
        return announcement, tuple(attachments)

    async def fetch_announcement_detail(
        self,
        announcement_id: str,
    ) -> tuple[ArtifactEvidence, tuple[dict[str, str], ...]]:
        """Fetch one detail response without requiring or fetching attachments.

        History traversal must classify every archive candidate, including
        press releases that have no enclosure.  Keeping detail and attachment
        fetches separate also gives the resumable collector an artifact-level
        checkpoint boundary.
        """

        if not _ANNOUNCEMENT_ID.fullmatch(announcement_id):
            raise CsindexEvidenceError("announcement_id is invalid")
        url = (
            "https://www.csindex.com.cn/csindex-home/announcement/"
            f"queryAnnouncementById?id={announcement_id}"
        )
        final_url, payload = await self._request("GET", url)
        announcement = self._artifact(
            role="announcement",
            url=final_url,
            payload=payload,
            announcement_id=announcement_id,
        )
        try:
            response = json.loads(payload)
            enclosure_rows = response["data"]["enclosureList"]
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            raise CsindexEvidenceError(
                "announcement enclosure metadata is invalid"
            ) from exc
        if not isinstance(enclosure_rows, list):
            raise CsindexEvidenceError("announcement enclosure list is invalid")
        normalized: list[dict[str, str]] = []
        for row in enclosure_rows:
            if not isinstance(row, dict):
                raise CsindexEvidenceError("announcement enclosure is invalid")
            attachment_url = _official_url(
                str(row.get("fileUrl") or ""),
                "enclosure.fileUrl",
            )
            normalized.append(
                {
                    "file_name": str(row.get("fileName") or "").strip(),
                    "file_url": attachment_url,
                }
            )
        return announcement, tuple(normalized)

    async def fetch_attachment(
        self,
        *,
        announcement_id: str,
        url: str,
    ) -> ArtifactEvidence:
        """Fetch one bounded enclosure from an already verified detail row."""

        if not _ANNOUNCEMENT_ID.fullmatch(announcement_id):
            raise CsindexEvidenceError("announcement_id is invalid")
        attachment_url = _official_url(url, "enclosure.fileUrl")
        final_url, payload = await self._request("GET", attachment_url)
        return self._artifact(
            role="attachment",
            url=final_url,
            payload=payload,
            announcement_id=announcement_id,
        )
