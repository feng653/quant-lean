"""Resumable, fail-closed collection of official CSI PIT evidence.

The workflow intentionally stops before approval.  It traverses the complete
unfiltered announcement archive, stores every collected byte in the managed
content-addressed evidence store, proposes rows only from explicitly supported
attachment schemas, and emits an independent review queue.  A historical
package can be staged only when a review decision file is bound to the exact
archive and proposal hashes.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now_iso

import asyncio
import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence
from urllib.parse import unquote, urlparse

import pandas as pd

from backend.data.pit_evidence_governance import (
    PitEvidenceGovernance,
    PitEvidenceIntegrityError,
    PitEvidenceStateError,
)
from backend.data.sources.csindex_pit import (
    ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION,
    CURRENT_ANCHOR_PACKAGE_KIND,
    HISTORICAL_REPLAY_PACKAGE_KIND,
    INDEPENDENT_ROW_REVIEW_METHOD,
    INDEX_CODES,
    _DELISTING_EFFECTIVE_DATES,
    TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION,
    AdjustmentAnnouncement,
    ArchiveEvidence,
    ArtifactEvidence,
    Constituent,
    CsindexEvidenceError,
    CsindexOfficialCollector,
    CsindexPermanentEvidenceError,
    CurrentAnchor,
    ScopeAdjustment,
    archive_review_manifest_sha256,
    build_staging_package,
    canonical_archive_review_rows,
    is_automatic_target_archive_row,
    parse_announcement_metadata,
    parse_archive_pages,
    parse_current_constituent_xls,
    validate_archive_review_decisions,
)

HISTORY_RUN_SCHEMA_VERSION = "csindex-pit-history-run/v1"
REVIEW_QUEUE_SCHEMA_VERSION = "csindex-pit-review-queue/v2"
COVERAGE_REPORT_SCHEMA_VERSION = "csindex-pit-coverage-report/v3"
ATTACHMENT_PARSER_VERSION = "csindex-adjustment-attachment-v1"
DEFAULT_ROWS_PER_PAGE = 100

_TARGET_SCOPE_ORDER = ("csi300", "csi500", "csi1000")
# CSI 800 is deterministically the union of CSI 300 and CSI 500 in the
# canonical replay package.  It has no independent official anchor endpoint,
# so coverage reports must show its derivation rather than pretend it was
# separately fetched.
_REPORT_SCOPE_ORDER = ("csi300", "csi500", "csi800", "csi1000")
_TARGET_NAMES = {
    "csi300": re.compile(r"沪深\s*300(?!精)"),
    "csi500": re.compile(r"中证\s*500"),
    "csi1000": re.compile(r"中证\s*1000"),
}
_COUNT_PATTERNS = {
    scope_id: re.compile(pattern.pattern)
    for scope_id, pattern in {
        "csi300": re.compile(r"沪深\s*300\s*指数更换\s*(\d+)\s*只样本"),
        "csi500": re.compile(r"中证\s*500\s*指数更换\s*(\d+)\s*只样本"),
        "csi1000": re.compile(r"中证\s*1000\s*指数更换\s*(\d+)\s*只样本"),
    }.items()
}
_EFFECTIVE_AFTER_CLOSE = re.compile(
    r"(?:于|自)\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"\s*收市后生效"
)
_SIX_DIGIT = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_PDF_SECTION = {
    "csi300": re.compile(r"沪深\s*300\s*指数样本调整名单\s*[：:]?"),
    "csi500": re.compile(r"中证\s*500\s*指数样本调整名单\s*[：:]?"),
    "csi1000": re.compile(r"中证\s*1000\s*指数样本调整名单\s*[：:]?"),
}
_PDF_ANY_SECTION = re.compile(
    r"(?m)^[^\n]{1,80}?(?:指数样本调整名单|指数备选名单)\s*[：:]?"
)
_PDF_ROW = re.compile(
    r"^\s*(\d{6})\s+(.+?)\s+(\d{6})\s+(.+?)\s*$"
)
_HTML_TAG = re.compile(r"<[^>]+>")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CsindexHistoryError(RuntimeError):
    """Base error for managed CSI history runs."""


class CsindexHistoryStateError(CsindexHistoryError):
    """Checkpoint or review state is absent, inconsistent, or unsafe."""


class CsindexAttachmentSchemaError(CsindexHistoryError):
    """An attachment cannot be interpreted without ambiguity."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    payload = (_canonical_json(value) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise CsindexHistoryStateError(
            "history workspace cannot contain a symbolic-link directory"
        )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        file_descriptor = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                written += os.write(file_descriptor, view[written:])
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is not None:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | directory_flag,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


def _safe_workspace(path: Path) -> Path:
    workspace = path.absolute()
    if workspace.exists() and workspace.is_symlink():
        raise CsindexHistoryStateError(
            "history workspace cannot be a symbolic link"
        )
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not workspace.is_dir():
        raise CsindexHistoryStateError("history workspace is not a directory")
    return workspace


def _artifact_record(artifact: ArtifactEvidence) -> dict[str, Any]:
    result = artifact.manifest()
    result["size_bytes"] = len(artifact.payload)
    return result


def _artifact_from_record(
    record: Mapping[str, Any],
    governance: PitEvidenceGovernance,
) -> ArtifactEvidence:
    digest = str(record.get("content_sha256") or "")
    if not _SHA256.fullmatch(digest):
        raise CsindexHistoryStateError("checkpoint artifact digest is invalid")
    request_digest = record.get("request_sha256")
    if request_digest is not None and not _SHA256.fullmatch(str(request_digest)):
        raise CsindexHistoryStateError(
            "checkpoint request artifact digest is invalid"
        )
    try:
        role = str(record["role"])
        url = str(record["url"])
        retrieved_at = str(record["retrieved_at"])
    except KeyError as exc:
        raise CsindexHistoryStateError(
            "checkpoint artifact identity is incomplete"
        ) from exc
    published = record.get("published_on")
    return ArtifactEvidence(
        role=role,  # type: ignore[arg-type]
        url=url,
        retrieved_at=retrieved_at,
        content_sha256=digest,
        payload=governance.artifacts.read(digest),
        announcement_id=(
            str(record["announcement_id"])
            if record.get("announcement_id") is not None
            else None
        ),
        published_on=(
            date.fromisoformat(str(published)) if published is not None else None
        ),
        request_payload=(
            governance.artifacts.read(str(request_digest))
            if request_digest is not None
            else None
        ),
        request_sha256=(
            str(request_digest) if request_digest is not None else None
        ),
    )


def _clean_html(value: Any) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", str(value or ""))).strip()


def _normalize_column(value: Any) -> str:
    return re.sub(r"[\s_]+", "", str(value or "")).lower()


def _required_column(
    columns: Sequence[Any],
    candidates: Sequence[str],
) -> Any:
    normalized = {_normalize_column(item): item for item in columns}
    matches = {
        normalized[_normalize_column(candidate)]
        for candidate in candidates
        if _normalize_column(candidate) in normalized
    }
    if len(matches) != 1:
        raise CsindexAttachmentSchemaError(
            "adjustment spreadsheet schema is absent or ambiguous"
        )
    return matches.pop()


def _security_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    text = text.zfill(6)
    if not re.fullmatch(r"\d{6}", text):
        raise CsindexAttachmentSchemaError(
            "adjustment security code is not six digits"
        )
    return text


def _member_name(value: Any) -> str:
    result = re.sub(r"\s+", " ", str(value or "").strip())
    if not result or len(result) > 160:
        raise CsindexAttachmentSchemaError(
            "adjustment security name is empty or too long"
        )
    return result


def _scope_for_index_code(value: Any) -> str | None:
    code = _security_code(value)
    reverse = {index_code: scope for scope, index_code in INDEX_CODES.items()}
    return reverse.get(code)


def _build_changes(
    additions: Mapping[str, list[Constituent]],
    removals: Mapping[str, list[Constituent]],
    expected_counts: Mapping[str, int] | None,
) -> dict[str, ScopeAdjustment]:
    result: dict[str, ScopeAdjustment] = {}
    if expected_counts is None:
        scopes = sorted(set(additions).union(removals).intersection(INDEX_CODES))
        for scope_id in scopes:
            added = tuple(additions.get(scope_id, ()))
            removed = tuple(removals.get(scope_id, ()))
            if not added or len(added) != len(removed):
                raise CsindexAttachmentSchemaError(
                    f"{scope_id} attachment rows are inconsistent"
                )
            result[scope_id] = ScopeAdjustment(additions=added, removals=removed)
        return result
    for scope_id, expected in sorted(expected_counts.items()):
        added = tuple(additions.get(scope_id, ()))
        removed = tuple(removals.get(scope_id, ()))
        if len(added) != expected or len(removed) != expected:
            raise CsindexAttachmentSchemaError(
                f"{scope_id} attachment rows do not match announced count"
            )
        result[scope_id] = ScopeAdjustment(
            additions=added,
            removals=removed,
        )
    unexpected = (
        set(additions).union(removals).intersection(INDEX_CODES)
        - set(expected_counts)
    )
    if unexpected:
        raise CsindexAttachmentSchemaError(
            "attachment includes an unannounced target index"
        )
    return result


def _parse_spreadsheet_adjustments(
    payload: bytes,
    *,
    expected_counts: Mapping[str, int] | None,
) -> dict[str, ScopeAdjustment]:
    try:
        workbook = pd.ExcelFile(io.BytesIO(payload))
    except Exception as exc:
        raise CsindexAttachmentSchemaError(
            "adjustment spreadsheet is unreadable"
        ) from exc
    normalized_sheets = {}
    for item in workbook.sheet_names:
        key = _normalize_column(item)
        if key.endswith("名单"):
            key = key[:-2]
        key = key.replace("换入", "调入").replace("换出", "调出")
        normalized_sheets[key] = item
    if set(normalized_sheets) == {"调入", "调出"}:
        additions, removals = _parse_two_sheet_adjustments(
            payload,
            workbook,
            normalized_sheets,
            expected_counts,
        )
        return _build_changes(additions, removals, expected_counts)
    if len(workbook.sheet_names) == 1 and _looks_like_merged_adjustment_sheet(
        payload,
        workbook.sheet_names[0],
    ):
        additions, removals = _parse_merged_adjustment_sheet(
            payload,
            workbook.sheet_names[0],
            expected_counts,
        )
        return _build_changes(additions, removals, expected_counts)
    raise CsindexAttachmentSchemaError(
        "adjustment spreadsheet must contain exactly 调入 and 调出 sheets "
        "or a single merged 调出/调入 sheet"
    )


def _looks_like_merged_adjustment_sheet(
    payload: bytes,
    sheet_name: str,
) -> bool:
    frame = pd.read_excel(
        io.BytesIO(payload),
        sheet_name=sheet_name,
        header=None,
        dtype=str,
        nrows=3,
    )
    header_text = " ".join(
        str(cell) for cell in frame.iloc[0].tolist() if str(cell) != "nan"
    )
    return "指数代码" in header_text and ("调出" in header_text or "调入" in header_text)


def _parse_merged_adjustment_sheet(
    payload: bytes,
    sheet_name: str,
    expected_counts: Mapping[str, int] | None,
) -> tuple[dict[str, list[Constituent]], dict[str, list[Constituent]]]:
    frame = pd.read_excel(
        io.BytesIO(payload),
        sheet_name=sheet_name,
        header=None,
        dtype=str,
    )
    additions: dict[str, list[Constituent]] = {}
    removals: dict[str, list[Constituent]] = {}
    for row_index, row in frame.iterrows():
        cells = [str(cell) if str(cell) != "nan" else "" for cell in row.tolist()]
        if len(cells) < 6:
            continue
        index_code = cells[0].strip()
        if not index_code or index_code == "指数代码":
            continue
        try:
            scope_id = _scope_for_index_code(index_code)
        except CsindexAttachmentSchemaError:
            continue
        if scope_id is None:
            continue
        removed_code = cells[2].strip()
        removed_name = cells[3].strip()
        added_code = cells[4].strip()
        added_name = cells[5].strip()
        if removed_code and removed_code not in ("-", "—"):
            removals.setdefault(scope_id, []).append(
                Constituent(_security_code(removed_code), _member_name(removed_name))
            )
        if added_code and added_code not in ("-", "—"):
            additions.setdefault(scope_id, []).append(
                Constituent(_security_code(added_code), _member_name(added_name))
            )
    if not additions and not removals:
        raise CsindexAttachmentSchemaError(
            "merged adjustment sheet contains no target index rows"
        )
    return additions, removals


def _parse_two_sheet_adjustments(
    payload: bytes,
    workbook: pd.ExcelFile,
    normalized_sheets: Mapping[str, str],
    expected_counts: Mapping[str, int] | None,
) -> tuple[dict[str, list[Constituent]], dict[str, list[Constituent]]]:
    additions: dict[str, list[Constituent]] = {}
    removals: dict[str, list[Constituent]] = {}
    for normalized_name, target in (
        ("调入", additions),
        ("调出", removals),
    ):
        frame = pd.read_excel(
            io.BytesIO(payload),
            sheet_name=normalized_sheets[normalized_name],
            dtype=str,
        )
        if frame.empty:
            raise CsindexAttachmentSchemaError(
                "adjustment spreadsheet sheet is empty"
            )
        index_column = _required_column(
            frame.columns,
            ("指数代码", "Index Code", "指数代码Index Code"),
        )
        code_column = _required_column(
            frame.columns,
            ("证券代码", "股票代码", "Constituent Code", "成份券代码"),
        )
        name_column = _required_column(
            frame.columns,
            ("证券简称", "股票简称", "证券名称", "股票名称", "成份券名称"),
        )
        permitted_columns = {
            _normalize_column(index_column),
            _normalize_column(code_column),
            _normalize_column(name_column),
            _normalize_column("指数简称"),
            _normalize_column("Index Name"),
        }
        if any(
            _normalize_column(column) not in permitted_columns
            for column in frame.columns
        ):
            raise CsindexAttachmentSchemaError(
                "adjustment spreadsheet contains an unsupported column"
            )
        for _row_index, row in frame.iterrows():
            scope_id = _scope_for_index_code(row[index_column])
            if scope_id is None:
                continue
            target.setdefault(scope_id, []).append(
                Constituent(
                    _security_code(row[code_column]),
                    _member_name(row[name_column]),
                )
            )
    return additions, removals


def _pdf_text(payload: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise CsindexAttachmentSchemaError(
            "pypdf is required for strict adjustment PDF parsing"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise CsindexAttachmentSchemaError(
                "encrypted adjustment PDF is unsupported"
            )
        pages = [page.extract_text() for page in reader.pages]
    except CsindexAttachmentSchemaError:
        raise
    except Exception as exc:
        raise CsindexAttachmentSchemaError(
            "adjustment PDF text extraction failed"
        ) from exc
    if not pages or any(item is None for item in pages):
        raise CsindexAttachmentSchemaError(
            "adjustment PDF has a non-text or empty page"
        )
    text = "\n".join(str(item) for item in pages)
    if not text.strip():
        raise CsindexAttachmentSchemaError("adjustment PDF is empty")
    return text


def _parse_pdf_adjustments(
    payload: bytes,
    *,
    expected_counts: Mapping[str, int] | None,
) -> dict[str, ScopeAdjustment]:
    text = _pdf_text(payload).replace("\u3000", " ")
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    starts: list[tuple[int, str, int]] = []
    for scope_id, pattern in _PDF_SECTION.items():
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            raise CsindexAttachmentSchemaError(
                f"{scope_id} adjustment PDF has duplicate sections"
            )
        if matches:
            starts.append((matches[0].start(), scope_id, matches[0].end()))
    scopes_found = {item[1] for item in starts}
    if expected_counts is not None and scopes_found != set(expected_counts):
        raise CsindexAttachmentSchemaError(
            "adjustment PDF sections do not match announced target indices"
        )
    additions: dict[str, list[Constituent]] = {}
    removals: dict[str, list[Constituent]] = {}
    starts.sort()
    for position, (_start, scope_id, content_start) in enumerate(starts):
        content_end = len(text)
        following = _PDF_ANY_SECTION.search(text, content_start)
        if following is not None:
            content_end = following.start()
        if position + 1 < len(starts):
            content_end = min(content_end, starts[position + 1][0])
        section = text[content_start:content_end]
        normalized_section = re.sub(r"\s+", "", section[:300])
        if "调出名单调入名单" not in normalized_section:
            raise CsindexAttachmentSchemaError(
                f"{scope_id} adjustment PDF paired-list header changed"
            )
        scope_additions: list[Constituent] = []
        scope_removals: list[Constituent] = []
        unparsable_code_lines: list[str] = []
        for raw_line in section.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line or not _SIX_DIGIT.search(line):
                continue
            match = _PDF_ROW.fullmatch(line)
            if match is None:
                unparsable_code_lines.append(line)
                continue
            removed_code, removed_name, added_code, added_name = match.groups()
            scope_removals.append(
                Constituent(
                    _security_code(removed_code),
                    _member_name(removed_name),
                )
            )
            scope_additions.append(
                Constituent(
                    _security_code(added_code),
                    _member_name(added_name),
                )
            )
        if unparsable_code_lines:
            raise CsindexAttachmentSchemaError(
                f"{scope_id} adjustment PDF contains unparsed code rows"
            )
        additions[scope_id] = scope_additions
        removals[scope_id] = scope_removals
    return _build_changes(additions, removals, expected_counts)


def parse_adjustment_attachments(
    *,
    attachments: Sequence[ArtifactEvidence],
    expected_counts: Mapping[str, int] | None = None,
) -> tuple[dict[str, ScopeAdjustment], dict[str, Any]]:
    """Parse exactly one supported table-bearing attachment.

    All enclosures remain evidence, but exactly one may contain rows for the
    announced CSI 300/500/1000 changes.  Zero or multiple successful parsers is
    ambiguous and therefore rejected.
    """

    if not attachments:
        raise CsindexAttachmentSchemaError("announcement has no attachment")
    parsed: list[
        tuple[
            ArtifactEvidence,
            Literal["pdf_paired_columns", "xlsx_split_sheets"],
            dict[str, ScopeAdjustment],
        ]
    ] = []
    diagnostics: list[dict[str, str]] = []
    for artifact in attachments:
        path = unquote(urlparse(artifact.url).path).lower()
        parser: Callable[..., dict[str, ScopeAdjustment]] | None = None
        schema: Literal["pdf_paired_columns", "xlsx_split_sheets"] | None = None
        if artifact.payload.startswith(b"%PDF") and path.endswith(".pdf"):
            parser = _parse_pdf_adjustments
            schema = "pdf_paired_columns"
        elif (
            artifact.payload.startswith(b"PK\x03\x04")
            and path.endswith(".xlsx")
        ) or (
            artifact.payload.startswith(b"\xd0\xcf\x11\xe0")
            and path.endswith(".xls")
        ):
            parser = _parse_spreadsheet_adjustments
            schema = "xlsx_split_sheets"
        if parser is None or schema is None:
            diagnostics.append(
                {
                    "content_sha256": artifact.content_sha256,
                    "status": "unsupported_media_or_extension",
                }
            )
            continue
        try:
            changes = parser(
                artifact.payload,
                expected_counts=expected_counts,
            )
        except (CsindexAttachmentSchemaError, CsindexEvidenceError) as exc:
            diagnostics.append(
                {
                    "content_sha256": artifact.content_sha256,
                    "status": "schema_rejected",
                    "reason": str(exc),
                }
            )
            continue
        parsed.append((artifact, schema, changes))
    if len(parsed) != 1:
        raise CsindexAttachmentSchemaError(
            "exactly one unambiguous adjustment attachment is required; "
            f"accepted={len(parsed)} diagnostics={_canonical_json(diagnostics)}"
        )
    artifact, schema, changes = parsed[0]
    return changes, {
        "parser_version": ATTACHMENT_PARSER_VERSION,
        "schema": schema,
        "content_sha256": artifact.content_sha256,
        "all_attachment_sha256": sorted(
            item.content_sha256 for item in attachments
        ),
        "diagnostics": diagnostics,
    }


def _changes_json(
    changes: Mapping[str, ScopeAdjustment],
) -> dict[str, Any]:
    return {
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
        for scope_id, change in sorted(changes.items())
    }


def _changes_from_json(
    value: Mapping[str, Any],
) -> dict[str, ScopeAdjustment]:
    result: dict[str, ScopeAdjustment] = {}
    for scope_id, rows in value.items():
        if scope_id not in INDEX_CODES or not isinstance(rows, dict):
            raise CsindexHistoryStateError("reviewed changes schema is invalid")
        if set(rows) != {"additions", "removals"}:
            raise CsindexHistoryStateError("reviewed changes schema is invalid")

        def constituents(key: str) -> tuple[Constituent, ...]:
            source = rows[key]
            if not isinstance(source, list):
                raise CsindexHistoryStateError(
                    "reviewed constituent list is invalid"
                )
            try:
                return tuple(
                    Constituent(
                        _security_code(item["security_code"]),
                        _member_name(item["member_name"]),
                    )
                    for item in source
                    if isinstance(item, dict)
                )
            except KeyError as exc:
                raise CsindexHistoryStateError(
                    "reviewed constituent row is incomplete"
                ) from exc

        result[scope_id] = ScopeAdjustment(
            additions=constituents("additions"),
            removals=constituents("removals"),
        )
    return result


def adjustment_review_proposal(
    proposal: AdjustmentAnnouncement,
    parser_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the exact review identity independently replayed at import time."""

    return {
        "announcement_id": proposal.announcement_id,
        "published_on": proposal.published_on.isoformat(),
        "effective_after_close": proposal.effective_after_close.isoformat(),
        "announced_counts": dict(sorted(proposal.announced_counts.items())),
        "changes": _changes_json(proposal.changes),
        "parser_evidence": dict(parser_evidence),
        "detail_sha256": proposal.announcement.content_sha256,
        "attachment_sha256": sorted(
            item.content_sha256 for item in proposal.attachments
        ),
    }


def _historical_member_sets(
    *,
    anchors: Mapping[str, CurrentAnchor],
    proposals: Mapping[str, AdjustmentAnnouncement],
    reviewed_events: Sequence[AdjustmentAnnouncement] | None,
) -> dict[str, dict[str, Any]]:
    """Report all observed/candidate/reviewed member codes without promotion.

    An archive proposal is not production evidence.  The three separate sets
    make that distinction machine-readable and include CSI 800 only as the
    documented 300+500 derived universe.
    """

    observed = {
        scope_id: {item.security_code for item in anchor.members}
        for scope_id, anchor in anchors.items()
    }
    proposed = {scope_id: set(codes) for scope_id, codes in observed.items()}
    reviewed = {scope_id: set(codes) for scope_id, codes in observed.items()}
    for proposal in proposals.values():
        for scope_id, change in proposal.changes.items():
            proposed.setdefault(scope_id, set()).update(
                item.security_code
                for item in (*change.additions, *change.removals)
            )
    for proposal in reviewed_events or ():
        for scope_id, change in proposal.changes.items():
            reviewed.setdefault(scope_id, set()).update(
                item.security_code
                for item in (*change.additions, *change.removals)
            )
    for sets in (observed, proposed, reviewed):
        sets["csi800"] = set(sets.get("csi300", set())) | set(
            sets.get("csi500", set())
        )
    return {
        scope_id: {
            "derivation": (
                "union_of_csi300_and_csi500"
                if scope_id == "csi800"
                else "official_scope"
            ),
            "current_anchor_codes": sorted(observed.get(scope_id, set())),
            "all_codes_seen_in_strict_proposals": sorted(
                proposed.get(scope_id, set())
            ),
            "all_codes_seen_in_independently_reviewed_events": sorted(
                reviewed.get(scope_id, set())
            ),
        }
        for scope_id in _REPORT_SCOPE_ORDER
    }


def _quarantine_daily_coverage(
    *,
    package: Mapping[str, Any] | None,
    calendar_days: Sequence[date] | None,
) -> dict[str, Any]:
    """Build per-session proposed coverage without claiming activation.

    Historical packages presently use the older import schema and therefore
    have no trustworthy ``available_at`` proof.  The report deliberately
    counts that absence on every member-session, which prevents a complete
    event chain from being confused with bitemporal production readiness.
    """

    unavailable = {
        "status": "unavailable",
        "reason": "historical_replay_package_or_authoritative_calendar_missing",
        "sessions": [],
        "available_at_gap_member_session_count": None,
        "production_ready": False,
    }
    if package is None or calendar_days is None:
        return {scope_id: dict(unavailable) for scope_id in _REPORT_SCOPE_ORDER}
    imports = package.get("imports")
    if not isinstance(imports, list):
        return {scope_id: dict(unavailable) for scope_id in _REPORT_SCOPE_ORDER}
    records_by_scope: dict[str, list[dict[str, Any]]] = {}
    for item in imports:
        if not isinstance(item, dict):
            continue
        scope_id = str(item.get("scope_id") or "")
        records = item.get("records")
        if scope_id in _REPORT_SCOPE_ORDER and isinstance(records, list):
            records_by_scope[scope_id] = [
                record for record in records if isinstance(record, dict)
            ]
    result: dict[str, Any] = {}
    for scope_id in _REPORT_SCOPE_ORDER:
        records = records_by_scope.get(scope_id, [])
        sessions: list[dict[str, Any]] = []
        missing_available_at = 0
        for session in calendar_days:
            day = session.isoformat()
            active = [
                record
                for record in records
                if str(record.get("effective_from") or "") <= day
                <= str(record.get("effective_to") or "")
            ]
            missing = sum(
                record.get("available_at") in (None, "") for record in active
            )
            missing_available_at += missing
            sessions.append(
                {
                    "session": day,
                    "member_count": len(active),
                    "available_at_missing_member_count": missing,
                    "member_codes_sha256": _canonical_sha256(
                        sorted(
                            str(record.get("security_code") or "")
                            for record in active
                        )
                    ),
                }
            )
        result[scope_id] = {
            "status": "quarantine_proposed_not_activated",
            "reason": "bitemporal_available_at_not_proven",
            "sessions": sessions,
            "available_at_gap_member_session_count": missing_available_at,
            "production_ready": False,
        }
    return result


@dataclass(frozen=True)
class HistoryRunResult:
    checkpoint_path: Path
    review_queue_path: Path
    coverage_report_path: Path
    package_id: str | None
    coverage_from: date
    coverage_to: date


class _RateLimiter:
    def __init__(self, minimum_interval_seconds: float) -> None:
        if minimum_interval_seconds < 0:
            raise CsindexHistoryStateError("rate interval cannot be negative")
        self._minimum_interval = minimum_interval_seconds
        self._last_started = 0.0

    async def wait(self) -> None:
        remaining = (
            self._last_started + self._minimum_interval - time.monotonic()
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_started = time.monotonic()


class CsindexHistoryWorkflow:
    """Checkpointed official archive collector and staging orchestrator."""

    def __init__(
        self,
        *,
        workspace: Path,
        governance: PitEvidenceGovernance,
        actor_user_id: int,
        collector: CsindexOfficialCollector | None = None,
        rows_per_page: int = DEFAULT_ROWS_PER_PAGE,
        minimum_interval_seconds: float = 0.25,
        max_attempts: int = 4,
    ) -> None:
        if actor_user_id < 1:
            raise CsindexHistoryStateError("actor_user_id must be positive")
        if rows_per_page < 1 or rows_per_page > 500:
            raise CsindexHistoryStateError("rows_per_page is invalid")
        if max_attempts < 1 or max_attempts > 10:
            raise CsindexHistoryStateError("max_attempts is invalid")
        self.workspace = _safe_workspace(workspace)
        self.governance = governance
        self.actor_user_id = actor_user_id
        self.collector = collector or CsindexOfficialCollector()
        self.rows_per_page = rows_per_page
        self.max_attempts = max_attempts
        self.rate_limiter = _RateLimiter(minimum_interval_seconds)
        self.checkpoint_path = self.workspace / "checkpoint.json"
        self.review_queue_path = self.workspace / "review_queue.json"
        self.coverage_report_path = self.workspace / "coverage_report.json"
        self._state: dict[str, Any] = {}

    def _load_state(
        self,
        *,
        requested_from: date,
    ) -> None:
        configuration = {
            "requested_from": requested_from.isoformat(),
            "rows_per_page": self.rows_per_page,
            "official_archive_mode": "complete_unfiltered",
        }
        if self.checkpoint_path.exists():
            try:
                state = json.loads(
                    self.checkpoint_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise CsindexHistoryStateError(
                    "history checkpoint is unreadable"
                ) from exc
            if (
                not isinstance(state, dict)
                or state.get("schema_version") != HISTORY_RUN_SCHEMA_VERSION
                or state.get("configuration") != configuration
            ):
                raise CsindexHistoryStateError(
                    "checkpoint configuration does not match this run"
                )
            self._state = state
            return
        self._state = {
            "schema_version": HISTORY_RUN_SCHEMA_VERSION,
            "configuration": configuration,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "anchors": {},
            "archive_pages": {},
            "archive_snapshot": None,
            "announcements": {},
            "attempts": {},
        }
        self._save_state()

    def _save_state(self) -> None:
        self._state["updated_at"] = utc_now_iso()
        _atomic_json(self.checkpoint_path, self._state)

    async def _retry(
        self,
        label: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            await self.rate_limiter.wait()
            try:
                result = await operation()
            except Exception as exc:
                last_error = exc
                self._state["attempts"][label] = {
                    "attempt": attempt,
                    "last_error_type": type(exc).__name__,
                    "last_error": str(exc)[:500],
                    "failed_at": utc_now_iso(),
                }
                self._save_state()
                if isinstance(exc, CsindexPermanentEvidenceError):
                    break
                if attempt < self.max_attempts:
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                continue
            self._state["attempts"][label] = {
                "attempt": attempt,
                "completed_at": utc_now_iso(),
            }
            self._save_state()
            return result
        assert last_error is not None
        raise CsindexHistoryError(
            f"official collection exhausted retries for {label}: {last_error}"
        ) from last_error

    def _record_artifact(self, artifact: ArtifactEvidence) -> dict[str, Any]:
        self.governance.record_artifact(
            artifact=artifact,
            actor_user_id=self.actor_user_id,
        )
        return _artifact_record(artifact)

    async def _collect_anchors(self) -> dict[str, CurrentAnchor]:
        records = self._state["anchors"]
        for scope_id in _TARGET_SCOPE_ORDER:
            if scope_id in records:
                continue
            anchor = await self._retry(
                f"anchor:{scope_id}",
                lambda scope_id=scope_id: self.collector.fetch_current_anchor(
                    scope_id  # type: ignore[arg-type]
                ),
            )
            records[scope_id] = {
                "observed_on": anchor.observed_on.isoformat(),
                "artifact": self._record_artifact(anchor.artifact),
            }
            self._save_state()
        anchors: dict[str, CurrentAnchor] = {}
        for scope_id, record in records.items():
            artifact = _artifact_from_record(record["artifact"], self.governance)
            anchor = parse_current_constituent_xls(
                scope_id=scope_id,  # type: ignore[arg-type]
                artifact=artifact,
            )
            if anchor.observed_on.isoformat() != record["observed_on"]:
                raise CsindexHistoryStateError(
                    "checkpoint anchor observation date changed"
                )
            anchors[scope_id] = anchor
        if set(anchors) != set(_TARGET_SCOPE_ORDER):
            raise CsindexHistoryStateError("all current anchors are required")
        observed = {item.observed_on for item in anchors.values()}
        if len(observed) != 1:
            raise CsindexHistoryStateError(
                "official current anchors have different observation dates"
            )
        return anchors

    @staticmethod
    def _archive_snapshot(artifact: ArtifactEvidence) -> dict[str, int]:
        try:
            response = json.loads(artifact.payload)
            current_page = int(response["currentPage"])
            page_size = int(response["pageSize"])
            total = int(response["total"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
            raise CsindexHistoryStateError(
                "official archive pagination metadata is invalid"
            ) from exc
        if (
            current_page < 1
            or page_size < 1
            or total < 1
            or not isinstance(response.get("data"), list)
        ):
            raise CsindexHistoryStateError(
                "official archive pagination metadata is invalid"
            )
        return {
            "current_page": current_page,
            "page_size": page_size,
            "total": total,
            "page_count": (total + page_size - 1) // page_size,
        }

    async def _collect_archive(
        self,
        *,
        requested_from: date,
        observed_on: date,
    ) -> ArchiveEvidence:
        page_records = self._state["archive_pages"]
        snapshot = self._state.get("archive_snapshot")
        if snapshot is None:
            first = await self._retry(
                "archive:1",
                lambda: self.collector.fetch_archive_page(
                    page=1,
                    rows=self.rows_per_page,
                ),
            )
            snapshot = self._archive_snapshot(first)
            if snapshot["page_size"] != self.rows_per_page:
                raise CsindexHistoryStateError(
                    "official archive changed requested page size"
                )
            self._state["archive_snapshot"] = snapshot
            page_records["1"] = self._record_artifact(first)
            self._save_state()
        for page in range(1, int(snapshot["page_count"]) + 1):
            key = str(page)
            if key in page_records:
                continue
            artifact = await self._retry(
                f"archive:{page}",
                lambda page=page: self.collector.fetch_archive_page(
                    page=page,
                    rows=self.rows_per_page,
                ),
            )
            actual = self._archive_snapshot(artifact)
            if {
                key: actual[key]
                for key in ("page_size", "total", "page_count")
            } != {
                key: snapshot[key]
                for key in ("page_size", "total", "page_count")
            }:
                raise CsindexHistoryStateError(
                    "official archive changed during traversal; "
                    "start a new immutable run"
                )
            if actual["current_page"] != page:
                raise CsindexHistoryStateError(
                    "official archive returned a different page"
                )
            page_records[key] = self._record_artifact(artifact)
            self._save_state()
        pages = [
            _artifact_from_record(page_records[str(page)], self.governance)
            for page in range(1, int(snapshot["page_count"]) + 1)
        ]
        return parse_archive_pages(
            pages=pages,
            adjustment_announcement_ids=[],
            coverage_from=requested_from,
            coverage_to=observed_on,
        )

    @staticmethod
    def _archive_rows(archive: ArchiveEvidence) -> list[dict[str, Any]]:
        return canonical_archive_review_rows(archive.pages)

    async def _collect_adjustment_details(
        self,
        archive_rows: Sequence[dict[str, Any]],
        *,
        requested_from: date,
        observed_on: date,
        required_announcement_ids: set[str] | None = None,
    ) -> None:
        del requested_from
        announcement_records = self._state["announcements"]
        required = set(required_announcement_ids or ())
        available_ids = {
            str(row["announcement_id"])
            for row in archive_rows
            if date.fromisoformat(row["published_on"]) <= observed_on
        }
        if required - available_ids:
            raise CsindexHistoryStateError(
                "review requested detail outside the retained archive"
            )
        candidates = [
            row
            for row in archive_rows
            if date.fromisoformat(row["published_on"]) <= observed_on
            and (
                row["announcement_id"] in required
                or is_automatic_target_archive_row(row)
            )
        ]
        for row in candidates:
            announcement_id = row["announcement_id"]
            record = announcement_records.setdefault(
                announcement_id,
                {
                    "archive_row": row,
                    "detail": None,
                    "enclosures": [],
                    "attachments": {},
                    "collection_errors": {},
                },
            )
            checkpoint_row = dict(record["archive_row"])
            checkpoint_row.pop("row_sha256", None)
            current_row = dict(row)
            current_row.pop("row_sha256", None)
            if checkpoint_row != current_row:
                raise CsindexHistoryStateError(
                    "archive row changed for a checkpointed announcement"
                )
            if record["archive_row"] != row:
                record["archive_row"] = row
                self._save_state()
            if record["detail"] is None:
                detail_error = record.get("collection_errors", {}).get(
                    "detail",
                    {},
                )
                if "permanently rejected" in str(
                    detail_error.get("error") or ""
                ):
                    continue
                try:
                    detail, enclosures = await self._retry(
                        f"announcement:{announcement_id}",
                        lambda announcement_id=announcement_id: (
                            self.collector.fetch_announcement_detail(
                                announcement_id
                            )
                        ),
                    )
                except CsindexHistoryError as exc:
                    record.setdefault("collection_errors", {})["detail"] = {
                        "error": str(exc)[:500],
                        "recorded_at": utc_now_iso(),
                    }
                    self._save_state()
                    continue
                record["detail"] = self._record_artifact(detail)
                record["enclosures"] = list(enclosures)
                self._save_state()
            for enclosure in record["enclosures"]:
                url = enclosure["file_url"]
                if url in record["attachments"]:
                    continue
                attachment_error = record.get(
                    "collection_errors",
                    {},
                ).get(url, {})
                if "permanently rejected" in str(
                    attachment_error.get("error") or ""
                ):
                    continue
                try:
                    attachment = await self._retry(
                        (
                            f"attachment:{announcement_id}:"
                            f"{len(record['attachments'])}"
                        ),
                        lambda announcement_id=announcement_id, url=url: (
                            self.collector.fetch_attachment(
                                announcement_id=announcement_id,
                                url=url,
                            )
                        ),
                    )
                except CsindexHistoryError as exc:
                    record.setdefault("collection_errors", {})[url] = {
                        "error": str(exc)[:500],
                        "recorded_at": utc_now_iso(),
                    }
                    self._save_state()
                    continue
                record["attachments"][url] = self._record_artifact(attachment)
                self._save_state()

    def _classify_events(
        self,
        archive: ArchiveEvidence,
        *,
        requested_from: date,
        observed_on: date,
        review_target_ids: set[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, AdjustmentAnnouncement]]:
        target_ids = set(review_target_ids or ())
        archive_rows = [
            row
            for row in self._archive_rows(archive)
            if date.fromisoformat(row["published_on"]) <= observed_on
        ]
        events: list[dict[str, Any]] = []
        proposals: dict[str, AdjustmentAnnouncement] = {}
        for row in archive_rows:
            machine_candidate = is_automatic_target_archive_row(row)
            record = self._state["announcements"].get(
                row["announcement_id"]
            )
            retained_detail = bool(
                isinstance(record, dict) and record.get("detail") is not None
            )
            base = {
                **row,
                "automated_disposition": (
                    "automatic_target_candidate"
                    if machine_candidate
                    else "manual_row_disposition_required"
                ),
                "managed_detail_required": bool(
                    machine_candidate
                    or row["announcement_id"] in target_ids
                ),
                "managed_detail_retained": retained_detail,
            }
            if not base["managed_detail_required"] and not retained_detail:
                events.append(base)
                continue
            if not record or record.get("detail") is None:
                events.append(
                    {
                        **base,
                        "status": "blocked_missing_detail",
                        "collection_errors": (
                            record.get("collection_errors", {})
                            if isinstance(record, dict)
                            else {}
                        ),
                    }
                )
                continue
            announcement = _artifact_from_record(
                record["detail"],
                self.governance,
            )
            attachments = tuple(
                _artifact_from_record(item, self.governance)
                for _url, item in sorted(record["attachments"].items())
            )
            try:
                detail = json.loads(announcement.payload)["data"]
                if str(detail["id"]) != row["announcement_id"]:
                    raise CsindexHistoryStateError(
                        "announcement detail identity mismatch"
                    )
                content = _clean_html(detail.get("content"))
                content = re.sub(r"(?<=\d)\s+(?=\d)", "", content)
                title_norm = re.sub(r"(?<=\d)\s+(?=\d)", "", row["title"])
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
            ) as exc:
                events.append(
                    {
                        **base,
                        "status": "blocked_detail_schema",
                        "reason": str(exc)[:300],
                    }
                )
                continue
            mentioned = [
                scope_id
                for scope_id, pattern in _TARGET_NAMES.items()
                if pattern.search(content) or pattern.search(title_norm)
            ]
            if not mentioned:
                events.append(
                    {
                        **base,
                        "status": "no_target_index_name_in_title_or_detail",
                        "detail_sha256": announcement.content_sha256,
                        "attachment_sha256": sorted(
                            item.content_sha256 for item in attachments
                        ),
                    }
                )
                continue
            counts: dict[str, int] = {}
            for scope_id, pattern in _COUNT_PATTERNS.items():
                match = pattern.search(content)
                if match is not None:
                    counts[scope_id] = int(match.group(1))
            effective = _EFFECTIVE_AFTER_CLOSE.search(content)
            if effective is None:
                _dm = re.search(r"\u81ea(.+?)\u9000\u5e02\u65e5\u8d77", content)
                _sn = _dm.group(1).strip() if _dm else ""
                if _sn in _DELISTING_EFFECTIVE_DATES:
                    effective = True
            if effective is not None and set(counts) != set(mentioned):
                try:
                    inferred_changes, _inferred_evidence = parse_adjustment_attachments(
                        attachments=attachments,
                        expected_counts=None,
                    )
                    inferred_counts = {
                        scope_id: len(change.additions)
                        for scope_id, change in inferred_changes.items()
                    }
                    if set(inferred_counts) == set(mentioned):
                        counts = inferred_counts
                except (CsindexEvidenceError, CsindexAttachmentSchemaError):
                    pass
            if set(counts) != set(mentioned) or effective is None:
                events.append(
                    {
                        **base,
                        "status": "blocked_missing_explicit_count_or_effective_date",
                        "mentioned_scopes": sorted(mentioned),
                        "announced_counts": counts,
                        "detail_sha256": announcement.content_sha256,
                        "attachment_sha256": sorted(
                            item.content_sha256 for item in attachments
                        ),
                    }
                )
                continue
            try:
                changes, parser_evidence = parse_adjustment_attachments(
                    attachments=attachments,
                    expected_counts=counts,
                )
                proposal = parse_announcement_metadata(
                    announcement=announcement,
                    attachments=attachments,
                    reviewed_changes=changes,
                )
            except (CsindexEvidenceError, CsindexAttachmentSchemaError) as exc:
                events.append(
                    {
                        **base,
                        "status": "blocked_attachment_schema_or_consistency",
                        "mentioned_scopes": sorted(mentioned),
                        "announced_counts": counts,
                        "detail_sha256": announcement.content_sha256,
                        "attachment_sha256": sorted(
                            item.content_sha256 for item in attachments
                        ),
                        "collection_errors": record.get(
                            "collection_errors",
                            {},
                        ),
                        "reason": str(exc)[:1000],
                    }
                )
                continue
            proposal_json = adjustment_review_proposal(
                proposal,
                parser_evidence,
            )
            proposal_hash = _canonical_sha256(proposal_json)
            events.append(
                {
                    **base,
                    "status": "awaiting_independent_row_review",
                    "proposal_sha256": proposal_hash,
                    "proposal": proposal_json,
                }
            )
            proposals[proposal.announcement_id] = proposal
        archive_review_rows = canonical_archive_review_rows(archive.pages)
        archive_review_digest = _canonical_sha256(archive_review_rows)
        queue = {
            "schema_version": REVIEW_QUEUE_SCHEMA_VERSION,
            "generated_at": utc_now_iso(),
            "requested_from": requested_from.isoformat(),
            "official_anchor_on": observed_on.isoformat(),
            "archive_manifest_sha256": archive_review_manifest_sha256(archive),
            "archive_review_rows_sha256": archive_review_digest,
            "archive_row_count": len(events),
            "instructions": {
                "automatic_approval_permitted": False,
                "archive_review": (
                    "Give every row an exact row_sha256-bound not_target or "
                    "target_adjustment disposition with a reason. A global "
                    "all-reviewed boolean is not accepted."
                ),
                "event_review": (
                    "Rows marked target_adjustment are fetched through the "
                    "managed collector. After detail and every attachment are "
                    "retained, independently compare all rows, counts, codes "
                    "and effective date; accept only the exact proposal_sha256."
                ),
            },
            "events": events,
        }
        return queue, proposals

    @staticmethod
    def _review_dispositions(
        *,
        queue: Mapping[str, Any],
        archive: ArchiveEvidence,
        review_decisions_path: Path,
    ) -> tuple[dict[str, Any], dict[str, dict[str, str]], str]:
        try:
            decisions = json.loads(
                review_decisions_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise CsindexHistoryStateError(
                "review decision document is unreadable"
            ) from exc
        try:
            dispositions, dispositions_sha256 = (
                validate_archive_review_decisions(
                    decisions,
                    pages=archive.pages,
                    archive_manifest_sha256=str(
                        queue.get("archive_manifest_sha256") or ""
                    ),
                )
            )
        except CsindexEvidenceError as exc:
            raise CsindexHistoryStateError(
                str(exc)
            ) from exc
        if decisions.get("archive_review_rows_sha256") != queue.get(
            "archive_review_rows_sha256"
        ):
            raise CsindexHistoryStateError(
                "review decision rows differ from the emitted queue"
            )
        return decisions, dispositions, dispositions_sha256

    @staticmethod
    def _reviewed_events(
        *,
        queue: Mapping[str, Any],
        proposals: Mapping[str, AdjustmentAnnouncement],
        archive: ArchiveEvidence,
        review_decisions_path: Path,
    ) -> tuple[list[AdjustmentAnnouncement], str]:
        decisions, dispositions, dispositions_sha256 = (
            CsindexHistoryWorkflow._review_dispositions(
                queue=queue,
                archive=archive,
                review_decisions_path=review_decisions_path,
            )
        )
        events_by_id = {
            str(row["announcement_id"]): row for row in queue["events"]
        }
        for announcement_id, disposition in dispositions.items():
            event = events_by_id[announcement_id]
            if disposition["disposition"] == "target_adjustment":
                if (
                    announcement_id not in proposals
                    or event.get("status")
                    != "awaiting_independent_row_review"
                    or not event.get("managed_detail_retained")
                ):
                    raise CsindexHistoryStateError(
                        "target disposition requires managed detail, every "
                        "attachment, and a strict parsed proposal"
                    )
            elif (
                announcement_id in proposals
                or (
                    event.get("automated_disposition")
                    == "automatic_target_candidate"
                    and event.get("status")
                    != "no_target_index_name_in_title_or_detail"
                )
                or (
                    event.get("managed_detail_retained")
                    and event.get("status")
                    not in {"no_target_index_name_in_title_or_detail", None}
                )
            ):
                raise CsindexHistoryStateError(
                    "not-target disposition conflicts with retained target evidence"
                )
        raw_event_decisions = decisions.get("event_decisions")
        if not isinstance(raw_event_decisions, list):
            raise CsindexHistoryStateError(
                "review event decisions are missing"
            )
        accepted: list[AdjustmentAnnouncement] = []
        seen: set[str] = set()
        for item in raw_event_decisions:
            if not isinstance(item, dict):
                raise CsindexHistoryStateError(
                    "review event decision is invalid"
                )
            announcement_id = str(item.get("announcement_id") or "")
            if (
                set(item)
                != {
                    "announcement_id",
                    "decision",
                    "proposal_sha256",
                    "reason",
                }
                or announcement_id in seen
                or announcement_id not in proposals
                or dispositions[announcement_id]["disposition"]
                != "target_adjustment"
            ):
                raise CsindexHistoryStateError(
                    "review event decision identity is invalid"
                )
            seen.add(announcement_id)
            queue_event = next(
                row
                for row in queue["events"]
                if row["announcement_id"] == announcement_id
            )
            if (
                item.get("decision") != "accepted"
                or item.get("proposal_sha256")
                != queue_event.get("proposal_sha256")
                or not str(item.get("reason") or "").strip()
            ):
                raise CsindexHistoryStateError(
                    "target event proposal was not explicitly accepted"
                )
            accepted.append(proposals[announcement_id])
        if seen != set(proposals):
            raise CsindexHistoryStateError(
                "every target event proposal needs an explicit decision"
            )
        return accepted, dispositions_sha256

    def _trading_days(
        self,
        path: Path,
        *,
        requested_from: date,
        observed_on: date,
    ) -> tuple[list[date], str, dict[str, Any], bytes]:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        try:
            document, normalized_sessions, signed_payload_sha256 = (
                self.governance.verify_trading_calendar_payload(payload)
            )
        except PitEvidenceIntegrityError as exc:
            raise CsindexHistoryStateError(
                str(exc)
            ) from exc
        days = [date.fromisoformat(item) for item in normalized_sessions]
        if (
            not days
            or days[0] > requested_from
            or days[-1] < observed_on
        ):
            raise CsindexHistoryStateError(
                "trading calendar does not cover the requested window"
            )
        evidence = {
            "schema_version": TRADING_CALENDAR_EVIDENCE_SCHEMA_VERSION,
            "role": "trading_calendar",
            "provider": str(document["source"]["provider"]),
            "evidence_level": str(document["source"]["evidence_level"]),
            "version": str(document["source"]["version"]),
            "retrieved_at": str(document["source"]["retrieved_at"]),
            "signature_key_id": str(
                document["source"]["signature_key_id"]
            ),
            "signed_payload_sha256": signed_payload_sha256,
            "content_sha256": digest,
            "sessions_sha256": _canonical_sha256(
                [item.isoformat() for item in days]
            ),
            "sessions": [item.isoformat() for item in days],
        }
        return days, digest, evidence, payload

    async def run(
        self,
        *,
        requested_from: date,
        review_decisions_path: Path | None = None,
        trading_calendar_path: Path | None = None,
        stage_current_anchor_if_blocked: bool = True,
    ) -> HistoryRunResult:
        self._load_state(requested_from=requested_from)
        anchors = await self._collect_anchors()
        observed_on = next(iter(anchors.values())).observed_on
        if requested_from > observed_on:
            raise CsindexHistoryStateError(
                "requested start is later than the official current anchor"
            )
        archive = await self._collect_archive(
            requested_from=requested_from,
            observed_on=observed_on,
        )
        archive_rows = self._archive_rows(archive)
        await self._collect_adjustment_details(
            archive_rows,
            requested_from=requested_from,
            observed_on=observed_on,
        )
        queue, proposals = self._classify_events(
            archive,
            requested_from=requested_from,
            observed_on=observed_on,
        )
        _atomic_json(self.review_queue_path, queue)

        package_id: str | None = None
        package_coverage_from = observed_on
        blockers: list[dict[str, Any]] = []
        reviewed_events: list[AdjustmentAnnouncement] | None = None
        calendar_days: list[date] | None = None
        calendar_sha256: str | None = None
        calendar_evidence: dict[str, Any] | None = None
        review_evidence: dict[str, Any] | None = None
        if review_decisions_path is None:
            blockers.append(
                {
                    "code": "independent_archive_and_row_review_missing",
                    "detail": "A hash-bound review decision document is required.",
                }
            )
        else:
            try:
                (
                    _review_document,
                    archive_dispositions,
                    archive_dispositions_sha256,
                ) = self._review_dispositions(
                    queue=queue,
                    archive=archive,
                    review_decisions_path=review_decisions_path,
                )
                review_target_ids = {
                    announcement_id
                    for announcement_id, disposition in (
                        archive_dispositions.items()
                    )
                    if disposition["disposition"] == "target_adjustment"
                }
                await self._collect_adjustment_details(
                    archive_rows,
                    requested_from=requested_from,
                    observed_on=observed_on,
                    required_announcement_ids=review_target_ids,
                )
                queue, proposals = self._classify_events(
                    archive,
                    requested_from=requested_from,
                    observed_on=observed_on,
                    review_target_ids=review_target_ids,
                )
                _atomic_json(self.review_queue_path, queue)
                reviewed_events, verified_dispositions_sha256 = (
                    self._reviewed_events(
                        queue=queue,
                        proposals=proposals,
                        archive=archive,
                        review_decisions_path=review_decisions_path,
                    )
                )
                if (
                    verified_dispositions_sha256
                    != archive_dispositions_sha256
                ):
                    raise CsindexHistoryStateError(
                        "archive dispositions changed during managed collection"
                    )
                review_payload = review_decisions_path.read_bytes()
                review_sha256 = hashlib.sha256(review_payload).hexdigest()
                self.governance.record_auxiliary_artifact(
                    kind="review_decisions",
                    payload=review_payload,
                    expected_sha256=review_sha256,
                    actor_user_id=self.actor_user_id,
                )
                review_evidence = {
                    "schema_version": (
                        ADJUSTMENT_REVIEW_EVIDENCE_SCHEMA_VERSION
                    ),
                    "role": "review_decisions",
                    "review_method": INDEPENDENT_ROW_REVIEW_METHOD,
                    "content_sha256": review_sha256,
                    "reviewed_changes_sha256": "",
                    "archive_manifest_sha256": queue[
                        "archive_manifest_sha256"
                    ],
                    "archive_review_rows_sha256": queue[
                        "archive_review_rows_sha256"
                    ],
                    "archive_row_dispositions_sha256": (
                        verified_dispositions_sha256
                    ),
                }
            except (
                CsindexEvidenceError,
                CsindexHistoryStateError,
                PitEvidenceIntegrityError,
                PitEvidenceStateError,
            ) as exc:
                blockers.append(
                    {
                        "code": "independent_review_incomplete_or_invalid",
                        "detail": str(exc),
                    }
                )
        if trading_calendar_path is None:
            blockers.append(
                {
                    "code": "authoritative_trading_calendar_missing",
                    "detail": (
                        "Historical effective dates require a hash-bound "
                        "authoritative trading calendar."
                    ),
                }
            )
        else:
            try:
                (
                    calendar_days,
                    calendar_sha256,
                    calendar_evidence,
                    calendar_payload,
                ) = self._trading_days(
                    trading_calendar_path,
                    requested_from=requested_from,
                    observed_on=observed_on,
                )
                self.governance.record_auxiliary_artifact(
                    kind="trading_calendar",
                    payload=calendar_payload,
                    expected_sha256=calendar_sha256,
                    actor_user_id=self.actor_user_id,
                )
            except (
                OSError,
                CsindexHistoryStateError,
                PitEvidenceIntegrityError,
                PitEvidenceStateError,
            ) as exc:
                blockers.append(
                    {
                        "code": "authoritative_trading_calendar_invalid",
                        "detail": str(exc),
                    }
                )

        package: dict[str, Any] | None = None
        if reviewed_events is not None and calendar_days is not None:
            reviewed_archive = ArchiveEvidence(
                pages=archive.pages,
                announcement_ids=archive.announcement_ids,
                adjustment_announcement_ids=tuple(
                    item.announcement_id for item in reviewed_events
                ),
                coverage_from=archive.coverage_from,
                coverage_to=archive.coverage_to,
                exact_duplicate_announcement_ids=(
                    archive.exact_duplicate_announcement_ids
                ),
            )
            try:
                package = build_staging_package(
                    anchors=anchors,
                    announcements=reviewed_events,
                    archive=reviewed_archive,
                    trading_days=calendar_days,
                    coverage_from=requested_from,
                    coverage_to=observed_on,
                    trading_calendar_evidence=calendar_evidence,
                    review_evidence=review_evidence,
                    package_kind=HISTORICAL_REPLAY_PACKAGE_KIND,
                )
                package_coverage_from = requested_from
            except (CsindexEvidenceError, RuntimeError) as exc:
                blockers.append(
                    {
                        "code": "historical_replay_not_proven",
                        "detail": str(exc),
                    }
                )
        if package is None and stage_current_anchor_if_blocked:
            current_archive = ArchiveEvidence(
                pages=archive.pages,
                announcement_ids=archive.announcement_ids,
                adjustment_announcement_ids=(),
                coverage_from=observed_on,
                coverage_to=observed_on,
                exact_duplicate_announcement_ids=(
                    archive.exact_duplicate_announcement_ids
                ),
            )
            package = build_staging_package(
                anchors=anchors,
                announcements=[],
                archive=current_archive,
                trading_days=[observed_on],
                coverage_from=observed_on,
                coverage_to=observed_on,
                package_kind=CURRENT_ANCHOR_PACKAGE_KIND,
            )
        if package is not None:
            staged = self.governance.stage_package(
                package=package,
                actor_user_id=self.actor_user_id,
            )
            package_id = str(staged["package_id"])

        status_counts: dict[str, int] = {}
        for event in queue["events"]:
            key = str(
                event.get("status")
                or event.get("automated_disposition")
                or "unknown"
            )
            status_counts[key] = status_counts.get(key, 0) + 1
        proposal_dates_by_scope: dict[str, list[str]] = {
            scope_id: [] for scope_id in _REPORT_SCOPE_ORDER
        }
        reviewed_dates_by_scope: dict[str, list[str]] = {
            scope_id: [] for scope_id in _REPORT_SCOPE_ORDER
        }
        for proposal in proposals.values():
            for scope_id in proposal.changes:
                proposal_dates_by_scope[scope_id].append(
                    proposal.effective_after_close.isoformat()
                )
        for event in reviewed_events or []:
            for scope_id in event.changes:
                reviewed_dates_by_scope[scope_id].append(
                    event.effective_after_close.isoformat()
                )
        # CSI 800 is a derived 300+500 union in the canonical replay package.
        # Its effective-event evidence is therefore the union of those two
        # source timelines rather than a fabricated independent feed.
        proposal_dates_by_scope["csi800"] = sorted(
            set(proposal_dates_by_scope["csi300"])
            | set(proposal_dates_by_scope["csi500"])
        )
        reviewed_dates_by_scope["csi800"] = sorted(
            set(reviewed_dates_by_scope["csi300"])
            | set(reviewed_dates_by_scope["csi500"])
        )
        current_anchor_report = {
            scope_id: {
                "observed_on": anchor.observed_on.isoformat(),
                "member_count": len(anchor.members),
                "content_sha256": anchor.artifact.content_sha256,
                "derivation": "official_scope",
            }
            for scope_id, anchor in sorted(anchors.items())
        }
        csi800_anchor_codes = {
            item.security_code for item in anchors["csi300"].members
        } | {
            item.security_code for item in anchors["csi500"].members
        }
        current_anchor_report["csi800"] = {
            "observed_on": observed_on.isoformat(),
            "member_count": len(csi800_anchor_codes),
            "content_sha256": _canonical_sha256(
                {
                    "csi300": anchors["csi300"].artifact.content_sha256,
                    "csi500": anchors["csi500"].artifact.content_sha256,
                }
            ),
            "derivation": "union_of_csi300_and_csi500",
        }
        daily_coverage = _quarantine_daily_coverage(
            package=package,
            calendar_days=calendar_days,
        )
        historical_members = _historical_member_sets(
            anchors=anchors,
            proposals=proposals,
            reviewed_events=reviewed_events,
        )
        physical_archive_rows = sum(
            len(json.loads(page.payload)["data"]) for page in archive.pages
        )
        unique_archive_rows = self._archive_rows(archive)
        report = {
            "schema_version": COVERAGE_REPORT_SCHEMA_VERSION,
            "generated_at": utc_now_iso(),
            "requested_coverage": {
                "from": requested_from.isoformat(),
                "to": observed_on.isoformat(),
            },
            "proven_staging_coverage": {
                "from": package_coverage_from.isoformat(),
                "to": observed_on.isoformat(),
            },
            "complete_unfiltered_archive": {
                "page_count": len(archive.pages),
                "physical_row_count": physical_archive_rows,
                "announcement_count": len(archive.announcement_ids),
                "oldest_published_on": min(
                    item["published_on"] for item in unique_archive_rows
                ),
                "newest_published_on": max(
                    item["published_on"] for item in unique_archive_rows
                ),
                "exact_duplicate_announcement_ids": list(
                    archive.exact_duplicate_announcement_ids
                ),
                "manifest_sha256": queue["archive_manifest_sha256"],
                "review_rows_sha256": queue["archive_review_rows_sha256"],
            },
            "current_anchors": {
                scope_id: current_anchor_report[scope_id]
                for scope_id in _REPORT_SCOPE_ORDER
            },
            "classification_counts": dict(sorted(status_counts.items())),
            "strict_attachment_proposal_count": len(proposals),
            "per_index_effective_event_coverage": {
                scope_id: {
                    "strict_proposal_count": len(
                        proposal_dates_by_scope[scope_id]
                    ),
                    "strict_proposal_first_effective_after_close": (
                        min(proposal_dates_by_scope[scope_id])
                        if proposal_dates_by_scope[scope_id]
                        else None
                    ),
                    "strict_proposal_last_effective_after_close": (
                        max(proposal_dates_by_scope[scope_id])
                        if proposal_dates_by_scope[scope_id]
                        else None
                    ),
                    "independently_reviewed_count": len(
                        reviewed_dates_by_scope[scope_id]
                    ),
                    "independently_reviewed_first_effective_after_close": (
                        min(reviewed_dates_by_scope[scope_id])
                        if reviewed_dates_by_scope[scope_id]
                        else None
                    ),
                    "independently_reviewed_last_effective_after_close": (
                        max(reviewed_dates_by_scope[scope_id])
                        if reviewed_dates_by_scope[scope_id]
                        else None
                    ),
                    "continuous_from_requested_start": (
                        package_coverage_from == requested_from
                    ),
                }
                for scope_id in _REPORT_SCOPE_ORDER
            },
            "per_index_daily_member_coverage": daily_coverage,
            "all_historical_member_codes": historical_members,
            "official_vs_tushare_comparison": {
                "status": "not_collected_by_official_history_workflow",
                "reason": (
                    "Official CSI collection does not treat a Tushare response "
                    "as authoritative evidence. Compare only a separately "
                    "quarantined Tushare candidate artifact to the exact "
                    "official scope/date before review."
                ),
                "production_decision_affected": False,
            },
            "trading_calendar_sha256": calendar_sha256,
            "staged_package_id": package_id,
            "automatic_approval_permitted": False,
            "requires_admin_attestation": True,
            "license_status": "not_attested_by_platform",
            "production_import_performed": False,
            "blockers": blockers,
            "gaps": (
                []
                if package_coverage_from == requested_from
                else [
                    {
                        "from": requested_from.isoformat(),
                        "to": (
                            package_coverage_from.fromordinal(
                                package_coverage_from.toordinal() - 1
                            ).isoformat()
                        ),
                        "reason": "historical_event_chain_not_fully_reviewed",
                        "detail": (
                            "Complete archive classification, every applicable "
                            "adjustment row, and an authoritative trading "
                            "calendar are not all independently verified; "
                            "--from does not establish coverage."
                        ),
                    }
                ]
            ),
            "artifacts": {
                "checkpoint": str(self.checkpoint_path),
                "review_queue": str(self.review_queue_path),
            },
        }
        _atomic_json(self.coverage_report_path, report)
        return HistoryRunResult(
            checkpoint_path=self.checkpoint_path,
            review_queue_path=self.review_queue_path,
            coverage_report_path=self.coverage_report_path,
            package_id=package_id,
            coverage_from=package_coverage_from,
            coverage_to=observed_on,
        )
