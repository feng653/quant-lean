from __future__ import annotations

import asyncio
import hashlib
import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.data.point_in_time_adapters import CsindexPointInTimeAdapter
from backend.data.point_in_time_master import (
    PointInTimeMasterStore,
    PointInTimeValidationError,
)
from backend.data.sources import csindex_pit
from backend.data.sources.csindex_pit import (
    AdjustmentAnnouncement,
    ArtifactEvidence,
    Constituent,
    CsindexEvidenceError,
    CsindexReplayError,
    CurrentAnchor,
    ScopeAdjustment,
    build_staging_package,
    parse_announcement_metadata,
    parse_archive_pages,
    parse_current_constituent_xls,
    replay_constituent_intervals,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "csindex_pit_offline_v1.json"
)


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _artifact(
    *,
    role: str,
    url: str,
    payload: bytes,
    announcement_id: str | None = None,
    request_payload: bytes | None = None,
) -> ArtifactEvidence:
    return ArtifactEvidence(
        role=role,  # type: ignore[arg-type]
        url=url,
        retrieved_at="2024-06-17T08:00:00Z",
        content_sha256=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        announcement_id=announcement_id,
        request_payload=request_payload,
        request_sha256=(
            hashlib.sha256(request_payload).hexdigest()
            if request_payload is not None
            else None
        ),
    )


def _anchor(scope_id: str, codes: range) -> CurrentAnchor:
    frame = pd.DataFrame(
        {
            "日期Date": ["20240617"] * len(codes),
            "指数代码 Index Code": [
                csindex_pit.INDEX_CODES[scope_id]
            ]
            * len(codes),
            "成份券代码Constituent Code": [
                f"{code:06d}" for code in codes
            ],
            "成份券名称Constituent Name": [
                f"N{code}" for code in codes
            ],
        }
    )
    output = io.BytesIO()
    frame.to_excel(output, index=False, engine="openpyxl")
    payload = output.getvalue()
    artifact = _artifact(
        role="current_anchor",
        url=(
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
            f"uploads/file/autofile/cons/{csindex_pit.INDEX_CODES[scope_id]}cons.xls"
        ),
        payload=payload,
    )
    return CurrentAnchor(
        scope_id=scope_id,  # type: ignore[arg-type]
        observed_on=date(2024, 6, 17),
        members=tuple(Constituent(f"{code:06d}", f"N{code}") for code in codes),
        artifact=artifact,
    )


def _evidence() -> tuple[
    dict[str, CurrentAnchor],
    list[AdjustmentAnnouncement],
    csindex_pit.ArchiveEvidence,
]:
    fixture = _fixture()
    request_payload = _canonical_bytes(fixture["archive"]["request"])
    archive_payload = _canonical_bytes(fixture["archive"]["response"])
    archive_artifact = _artifact(
        role="archive_page",
        url=(
            "https://www.csindex.com.cn/csindex-home/announcement/"
            "queryAnnouncementByVo"
        ),
        payload=archive_payload,
        request_payload=request_payload,
    )
    archive = parse_archive_pages(
        pages=[archive_artifact],
        adjustment_announcement_ids=["15267"],
        coverage_from=date(2024, 6, 1),
        coverage_to=date(2024, 6, 17),
    )
    changes = {
        "csi300": ScopeAdjustment(
            additions=(Constituent("000300", "added300"),),
            removals=(Constituent("900001", "removed300"),),
        ),
        "csi500": ScopeAdjustment(
            additions=(Constituent("100500", "added500"),),
            removals=(Constituent("900002", "removed500"),),
        ),
        "csi1000": ScopeAdjustment(
            additions=(Constituent("201000", "added1000"),),
            removals=(Constituent("900003", "removed1000"),),
        ),
    }
    attachment_url = (
        "https://oss-ch.csindex.com.cn/notice/fixture-adjustment.xlsx"
    )
    fixture["announcement"]["data"]["enclosureList"][0].update(
        fileName="fixture-adjustment.xlsx",
        fileUrl=attachment_url,
    )
    announcement_payload = _canonical_bytes(fixture["announcement"])
    announcement_artifact = _artifact(
        role="announcement",
        url=(
            "https://www.csindex.com.cn/csindex-home/announcement/"
            "queryAnnouncementById?id=15267"
        ),
        payload=announcement_payload,
        announcement_id="15267",
    )
    attachment_output = io.BytesIO()
    with pd.ExcelWriter(attachment_output, engine="openpyxl") as writer:
        for sheet_name, change_key in (
            ("调入", "additions"),
            ("调出", "removals"),
        ):
            rows = []
            for scope_id, change in changes.items():
                constituent = getattr(change, change_key)[0]
                rows.append(
                    {
                        "指数代码": csindex_pit.INDEX_CODES[scope_id],
                        "指数简称": scope_id,
                        "证券代码": constituent.security_code,
                        "证券简称": constituent.member_name,
                    }
                )
            pd.DataFrame(rows).to_excel(
                writer,
                sheet_name=sheet_name,
                index=False,
            )
    attachment_payload = attachment_output.getvalue()
    attachment = _artifact(
        role="attachment",
        url=attachment_url,
        payload=attachment_payload,
        announcement_id="15267",
    )
    event = parse_announcement_metadata(
        announcement=announcement_artifact,
        attachments=[attachment],
        reviewed_changes=changes,
    )
    anchors = {
        "csi300": _anchor("csi300", range(1, 301)),
        "csi500": _anchor("csi500", range(100001, 100501)),
        "csi1000": _anchor("csi1000", range(200001, 201001)),
    }
    return anchors, [event], archive


def test_parses_official_current_xls_schema_and_rejects_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(
        role="current_anchor",
        url=(
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
            "uploads/file/autofile/cons/000300cons.xls"
        ),
        payload=b"fixed legacy xls bytes",
    )
    frame = pd.DataFrame(
        {
            "日期Date": ["20240617"] * 300,
            "指数代码 Index Code": ["000300"] * 300,
            "成份券代码Constituent Code": [f"{item:06d}" for item in range(1, 301)],
            "成份券名称Constituent Name": [f"N{item}" for item in range(1, 301)],
        }
    )
    monkeypatch.setattr(csindex_pit.pd, "read_excel", lambda *_a, **_kw: frame)

    anchor = parse_current_constituent_xls(
        scope_id="csi300",
        artifact=artifact,
    )
    assert anchor.observed_on == date(2024, 6, 17)
    assert len(anchor.members) == 300

    monkeypatch.setattr(
        csindex_pit.pd,
        "read_excel",
        lambda *_a, **_kw: frame.drop(
            columns=["成份券代码Constituent Code"]
        ),
    )
    with pytest.raises(CsindexEvidenceError, match="schema changed"):
        parse_current_constituent_xls(
            scope_id="csi300",
            artifact=artifact,
        )
    monkeypatch.setattr(
        csindex_pit.pd,
        "read_excel",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            ImportError("Missing optional dependency 'xlrd'")
        ),
    )
    with pytest.raises(CsindexEvidenceError, match="xlrd is unavailable"):
        parse_current_constituent_xls(
            scope_id="csi300",
            artifact=artifact,
        )


def test_reverse_replay_applies_close_event_on_next_trading_day_and_derives_800() -> None:
    anchors, events, archive = _evidence()
    result = replay_constituent_intervals(
        anchors=anchors,
        announcements=events,
        archive=archive,
        trading_days=[date(2024, 6, 14), date(2024, 6, 17)],
        coverage_from=date(2024, 6, 1),
        coverage_to=date(2024, 6, 17),
    )

    removed = [
        row for row in result["csi300"] if row["security_code"] == "900001"
    ]
    added = [
        row for row in result["csi300"] if row["security_code"] == "000300"
    ]
    assert removed == [
        {
            "security_code": "900001",
            "member_name": "removed300",
            "effective_from": "2024-06-01",
            "effective_to": "2024-06-16",
        }
    ]
    assert added[-1]["effective_from"] == "2024-06-17"
    assert len(
        {
            row["security_code"]
            for row in result["csi800"]
            if row["effective_from"] == "2024-06-17"
        }
    ) == 800


def test_replay_fails_for_tamper_missing_event_attachment_calendar_and_overlap() -> None:
    anchors, events, archive = _evidence()
    fixture = _fixture()
    payload = fixture["attachment_payload"].encode()
    with pytest.raises(CsindexEvidenceError, match="digest mismatch"):
        ArtifactEvidence(
            role="attachment",
            url="https://oss-ch.csindex.com.cn/notice/fixture-adjustment.pdf",
            retrieved_at="2024-06-17T08:00:00Z",
            content_sha256="0" * 64,
            payload=payload,
            announcement_id="15267",
        )

    with pytest.raises(CsindexReplayError, match="requires adjustment events"):
        replay_constituent_intervals(
            anchors=anchors,
            announcements=[],
            archive=archive,
            trading_days=[date(2024, 6, 17)],
            coverage_from=date(2024, 6, 1),
            coverage_to=date(2024, 6, 17),
        )
    broken = events[0]
    with pytest.raises(CsindexEvidenceError, match="attachment is missing"):
        AdjustmentAnnouncement(
            announcement_id=broken.announcement_id,
            published_on=broken.published_on,
            effective_after_close=broken.effective_after_close,
            changes=broken.changes,
            announced_counts=broken.announced_counts,
            announcement=broken.announcement,
            attachments=(),
        )
    with pytest.raises(CsindexReplayError, match="next trading day"):
        replay_constituent_intervals(
            anchors=anchors,
            announcements=events,
            archive=archive,
            trading_days=[date(2024, 6, 14)],
            coverage_from=date(2024, 6, 1),
            coverage_to=date(2024, 6, 17),
        )
    overlapping = dict(anchors)
    overlapping["csi500"] = CurrentAnchor(
        scope_id="csi500",
        observed_on=anchors["csi500"].observed_on,
        members=(
            Constituent("000001", "overlap"),
            *anchors["csi500"].members[1:],
        ),
        artifact=anchors["csi500"].artifact,
    )
    with pytest.raises(CsindexReplayError, match="overlap"):
        replay_constituent_intervals(
            anchors=overlapping,
            announcements=events,
            archive=archive,
            trading_days=[date(2024, 6, 14), date(2024, 6, 17)],
            coverage_from=date(2024, 6, 1),
            coverage_to=date(2024, 6, 17),
        )


def test_archive_requires_unfiltered_complete_pages() -> None:
    fixture = _fixture()
    request = fixture["archive"]["request"]
    request["typelist"] = ["指数调样"]
    request_payload = _canonical_bytes(request)
    response_payload = _canonical_bytes(fixture["archive"]["response"])
    artifact = _artifact(
        role="archive_page",
        url=(
            "https://www.csindex.com.cn/csindex-home/announcement/"
            "queryAnnouncementByVo"
        ),
        payload=response_payload,
        request_payload=request_payload,
    )
    with pytest.raises(CsindexEvidenceError, match="unfiltered"):
        parse_archive_pages(
            pages=[artifact],
            adjustment_announcement_ids=["15267"],
            coverage_from=date(2024, 6, 1),
            coverage_to=date(2024, 6, 17),
        )

    fixture = _fixture()
    response = fixture["archive"]["response"]
    response["total"] = 3
    response_payload = _canonical_bytes(response)
    first_only = _artifact(
        role="archive_page",
        url=artifact.url,
        payload=response_payload,
        request_payload=_canonical_bytes(fixture["archive"]["request"]),
    )
    with pytest.raises(CsindexEvidenceError, match="gap"):
        parse_archive_pages(
            pages=[first_only],
            adjustment_announcement_ids=["15267"],
            coverage_from=date(2024, 6, 1),
            coverage_to=date(2024, 6, 17),
        )


def test_staging_is_idempotent_import_v1_and_never_auto_approves(
    tmp_path: Path,
) -> None:
    anchors, events, archive = _evidence()
    kwargs = {
        "anchors": anchors,
        "announcements": events,
        "archive": archive,
        "trading_days": [date(2024, 6, 14), date(2024, 6, 17)],
        "coverage_from": date(2024, 6, 1),
        "coverage_to": date(2024, 6, 17),
    }
    first = build_staging_package(**kwargs)
    second = build_staging_package(**kwargs)
    assert first == second
    assert first["approval"] == {
        "automatic_import_permitted": False,
        "requires_admin_attestation": True,
        "license_status": "not_attested_by_platform",
    }
    assert {item["scope_id"] for item in first["imports"]} == {
        "csi300",
        "csi500",
        "csi800",
        "csi1000",
    }
    assert all(
        item["schema_version"] == "point-in-time-master-import/v1"
        and item["source"]["evidence_level"] == "index_provider_authoritative"
        for item in first["imports"]
    )
    assert all(
        item["source"]["content_sha256"]
        == first["evidence_manifest_sha256"]
        for item in first["imports"]
    )

    adapter = CsindexPointInTimeAdapter(
        anchors=anchors,
        announcements=events,
        archive=archive,
        trading_days=[date(2024, 6, 14), date(2024, 6, 17)],
    )
    documents = asyncio.run(
        adapter.build_import_batches(
            coverage_from="2024-06-01",
            coverage_to="2024-06-17",
            scopes=["csi300", "csi800"],
        )
    )
    assert [item["scope_id"] for item in documents] == ["csi300", "csi800"]
    store = PointInTimeMasterStore(tmp_path / "experiment.db")
    with pytest.raises(PointInTimeValidationError, match="governance approval"):
        store.import_batch(**documents[0], imported_by_user_id=1)
