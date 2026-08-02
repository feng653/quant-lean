from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from backend.data.pit_evidence_governance import (
    PitEvidenceGovernance,
    PitEvidenceStateError,
)
from backend.data.point_in_time_master import (
    PointInTimeMasterStore,
    PointInTimeValidationError,
)
from backend.data.sources import csindex_history
from backend.data.sources.csindex_history import (
    CsindexAttachmentSchemaError,
    CsindexHistoryWorkflow,
    parse_adjustment_attachments,
)
from backend.data.sources.csindex_pit import (
    ArtifactEvidence,
    CsindexEvidenceError,
    CsindexOfficialCollector,
    CsindexPermanentEvidenceError,
    parse_archive_pages,
)
from backend.tests.test_csindex_pit_source import (
    _artifact,
    _evidence,
    _fixture,
)

_CALENDAR_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x29" * 32)
_CALENDAR_KEY_ID = "history-fixture-calendar-key"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _calendar_trust_registry() -> dict[str, dict[str, str]]:
    public_key = _CALENDAR_PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        _CALENDAR_KEY_ID: {
            "public_key_base64": base64.b64encode(public_key).decode(),
            "provider": "fixture_exchange",
            "evidence_level": "exchange_authoritative",
        }
    }


def _signed_calendar(trading_days: list[str]) -> dict[str, Any]:
    signed = {
        "schema_version": "authoritative-trading-calendar/v2",
        "source": {
            "provider": "fixture_exchange",
            "evidence_level": "exchange_authoritative",
            "version": "fixture-v1",
            "retrieved_at": "2024-06-18T00:00:00Z",
            "signature_key_id": _CALENDAR_KEY_ID,
        },
        "trading_days": trading_days,
    }
    return {
        **signed,
        "signature": base64.b64encode(
            _CALENDAR_PRIVATE_KEY.sign(_canonical_bytes(signed))
        ).decode(),
    }


def _xlsx_adjustments() -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet, codes in (
            (
                "调入",
                {
                    "000300": "000301",
                    "000905": "100501",
                    "000852": "201001",
                },
            ),
            (
                "调出",
                {
                    "000300": "900001",
                    "000905": "900002",
                    "000852": "900003",
                },
            ),
        ):
            pd.DataFrame(
                [
                    {
                        "指数代码": index_code,
                        "指数简称": scope,
                        "证券代码": security_code,
                        "证券简称": f"N{security_code}",
                    }
                    for scope, (index_code, security_code) in zip(
                        ("csi300", "csi500", "csi1000"),
                        codes.items(),
                        strict=True,
                    )
                ]
            ).to_excel(writer, sheet_name=sheet, index=False)
    return output.getvalue()


def _attachment(payload: bytes, suffix: str) -> ArtifactEvidence:
    return _artifact(
        role="attachment",
        url=f"https://oss-ch.csindex.com.cn/notice/strict.{suffix}",
        payload=payload,
        announcement_id="15267",
    )


def test_checkpoint_write_is_portable_without_directory_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    target = tmp_path / "checkpoint.json"

    csindex_history._atomic_json(target, {"status": "portable"})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "status": "portable"
    }


def test_strict_xlsx_parser_accepts_only_explicit_split_sheet_schema() -> None:
    changes, evidence = parse_adjustment_attachments(
        attachments=[_attachment(_xlsx_adjustments(), "xlsx")],
        expected_counts={"csi300": 1, "csi500": 1, "csi1000": 1},
    )
    assert evidence["schema"] == "xlsx_split_sheets"
    assert changes["csi300"].additions[0].security_code == "000301"
    assert changes["csi1000"].removals[0].security_code == "900003"

    payload = io.BytesIO()
    with pd.ExcelWriter(payload, engine="openpyxl") as writer:
        pd.DataFrame(
            [{"指数代码": "000300", "证券代码": "000001", "证券简称": "A"}]
        ).to_excel(writer, sheet_name="名单", index=False)
    with pytest.raises(CsindexAttachmentSchemaError, match="exactly one"):
        parse_adjustment_attachments(
            attachments=[_attachment(payload.getvalue(), "xlsx")],
            expected_counts={"csi300": 1},
        )


def test_strict_pdf_parser_rejects_unparsed_or_count_mismatched_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = """
    沪深 300 指数样本调整名单：
    调出名单 调入名单
    证券代码 证券名称 证券代码 证券名称
    900001 Old A 000301 New A
    中证 500 指数样本调整名单：
    调出名单 调入名单
    证券代码 证券名称 证券代码 证券名称
    900002 Old B 100501 New B
    中证 1000 指数样本调整名单：
    调出名单 调入名单
    股票代码 股票名称 股票代码 股票名称
    900003 Old C 201001 New C
    """
    monkeypatch.setattr(csindex_history, "_pdf_text", lambda _payload: text)
    changes, evidence = parse_adjustment_attachments(
        attachments=[_attachment(b"%PDF strict fixture", "pdf")],
        expected_counts={"csi300": 1, "csi500": 1, "csi1000": 1},
    )
    assert evidence["schema"] == "pdf_paired_columns"
    assert set(changes) == {"csi300", "csi500", "csi1000"}

    monkeypatch.setattr(
        csindex_history,
        "_pdf_text",
        lambda _payload: text.replace(
            "900001 Old A 000301 New A",
            "900001 broken row",
        ),
    )
    with pytest.raises(CsindexAttachmentSchemaError, match="exactly one"):
        parse_adjustment_attachments(
            attachments=[_attachment(b"%PDF strict fixture", "pdf")],
            expected_counts={"csi300": 1, "csi500": 1, "csi1000": 1},
        )


def test_collector_rejects_external_redirect_before_following_it() -> None:
    requested_hosts: list[str] = []

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(str(request.url.host))
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1:8000/private"},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            collector = CsindexOfficialCollector(client)
            with pytest.raises(CsindexEvidenceError, match="collection failed"):
                await collector.fetch_archive_page(page=1)

    asyncio.run(scenario())
    assert requested_hosts == ["www.csindex.com.cn"]


def test_collector_rejects_official_host_resolving_to_private_address() -> None:
    request_count = 0

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(200, json={"data": [], "total": 0})

        async def private_resolver(_host: str) -> tuple[str, ...]:
            return ("127.0.0.1",)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            collector = CsindexOfficialCollector(
                client,
                address_resolver=private_resolver,
            )
            with pytest.raises(
                CsindexPermanentEvidenceError,
                match="non-public",
            ):
                await collector.fetch_archive_page(page=1)

    asyncio.run(scenario())
    assert request_count == 0


def test_archive_records_exact_boundary_duplicates_but_rejects_ambiguity() -> None:
    fixture = _fixture()
    request = fixture["archive"]["request"]
    request["page"]["rows"] = 3
    response = fixture["archive"]["response"]
    response["pageSize"] = 3
    response["total"] = 3
    response["data"].append(dict(response["data"][0]))

    def artifact_for(value: dict[str, Any]) -> ArtifactEvidence:
        request_payload = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        response_payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return _artifact(
            role="archive_page",
            url=(
                "https://www.csindex.com.cn/csindex-home/announcement/"
                "queryAnnouncementByVo"
            ),
            payload=response_payload,
            request_payload=request_payload,
        )

    archive = parse_archive_pages(
        pages=[artifact_for(response)],
        adjustment_announcement_ids=[],
        coverage_from=date(2024, 5, 31),
        coverage_to=date(2024, 6, 17),
    )
    assert archive.exact_duplicate_announcement_ids == ("15267",)
    assert len(archive.announcement_ids) == 2

    response["data"][-1]["title"] = "changed duplicate identity"
    with pytest.raises(CsindexEvidenceError, match="ambiguous"):
        parse_archive_pages(
            pages=[artifact_for(response)],
            adjustment_announcement_ids=[],
            coverage_from=date(2024, 5, 31),
            coverage_to=date(2024, 6, 17),
        )


class _FixtureCollector:
    def __init__(self, *, fail_on_network: bool = False) -> None:
        self.fail_on_network = fail_on_network
        self.calls: list[str] = []
        self.anchors, self.events, self.archive = _evidence()

    def _called(self, label: str) -> None:
        self.calls.append(label)
        if self.fail_on_network:
            raise AssertionError("checkpointed run performed a network call")

    async def fetch_current_anchor(self, scope_id: str):
        self._called(f"anchor:{scope_id}")
        return self.anchors[scope_id]

    async def fetch_archive_page(self, *, page: int, rows: int, lang: str = "cn"):
        del rows, lang
        self._called(f"archive:{page}")
        assert page == 1
        return self.archive.pages[0]

    async def fetch_announcement_detail(self, announcement_id: str):
        self._called(f"detail:{announcement_id}")
        event = self.events[0]
        return event.announcement, (
            {
                "file_name": "fixture-adjustment.pdf",
                "file_url": event.attachments[0].url,
            },
        )

    async def fetch_attachment(self, *, announcement_id: str, url: str):
        self._called(f"attachment:{announcement_id}")
        event = self.events[0]
        assert url == event.attachments[0].url
        return event.attachments[0]


def _governance(tmp_path: Path) -> PitEvidenceGovernance:
    root = tmp_path / "evidence"
    return PitEvidenceGovernance(
        root=root,
        database_path=root / "governance.db",
        master_store=PointInTimeMasterStore(tmp_path / "master.db"),
        trusted_calendar_keys=_calendar_trust_registry(),
    )


def test_history_run_is_resumable_and_stages_only_proven_current_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = _FixtureCollector()
    anchors = collector.anchors

    def parse_anchor(*, scope_id: str, artifact: ArtifactEvidence):
        assert artifact.content_sha256 == anchors[scope_id].artifact.content_sha256
        return anchors[scope_id]

    monkeypatch.setattr(
        csindex_history,
        "parse_current_constituent_xls",
        parse_anchor,
    )
    monkeypatch.setattr(
        csindex_history,
        "parse_adjustment_attachments",
        lambda **_kwargs: (
            collector.events[0].changes,
            {
                "parser_version": "fixture",
                "schema": "fixture",
                "content_sha256": collector.events[0]
                .attachments[0]
                .content_sha256,
                "all_attachment_sha256": [
                    collector.events[0].attachments[0].content_sha256
                ],
                "diagnostics": [],
            },
        ),
    )
    governance = _governance(tmp_path)
    workflow = CsindexHistoryWorkflow(
        workspace=tmp_path / "run",
        governance=governance,
        actor_user_id=1,
        collector=collector,  # type: ignore[arg-type]
        rows_per_page=2,
        minimum_interval_seconds=0,
    )
    result = asyncio.run(workflow.run(requested_from=date(2024, 6, 1)))
    assert result.coverage_from == date(2024, 6, 17)
    assert result.package_id is not None
    package_summary = governance.get_package(result.package_id)
    assert package_summary["status"] == "pending"
    with sqlite3.connect(governance.database_path) as connection:
        stored_package = json.loads(
            connection.execute(
                "SELECT package_json FROM pit_evidence_packages "
                "WHERE package_id=?",
                (result.package_id,),
            ).fetchone()[0]
        )
    assert stored_package["evidence_manifest"]["package_kind"] == (
        "current_anchor_observation"
    )
    assert {
        item["evidence_kind"] for item in stored_package["imports"]
    } == {"current_snapshot"}
    with pytest.raises(PitEvidenceStateError, match="authoritative calendar"):
        governance.decide(
            package_id=result.package_id,
            expected_revision=1,
            decision="approved",
            actor_user_id=2,
            reason="must remain observation-only",
            attestations={
                "schema_version": "pit-evidence-attestation/v1",
                "all_adjustment_rows_reviewed": True,
                "archive_completeness_reviewed": True,
                "source_terms_acknowledged": True,
                "local_research_only": True,
                "redistribution_not_authorized": True,
            },
        )
    with pytest.raises(PointInTimeValidationError, match="quarantine-only"):
        governance.master_store.import_batch(
            **stored_package["imports"][0],
            imported_by_user_id=2,
        )
    governance.master_store.initialize_schema()
    with sqlite3.connect(governance.master_store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_master_batches"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM pit_master_intervals"
        ).fetchone()[0] == 0
    report = json.loads(result.coverage_report_path.read_text())
    assert report["production_import_performed"] is False
    assert report["gaps"] == [
        {
            "from": "2024-06-01",
            "to": "2024-06-16",
            "reason": "historical_event_chain_not_fully_reviewed",
            "detail": (
                "Complete archive classification, every applicable adjustment "
                "row, and an authoritative trading calendar are not all "
                "independently verified; --from does not establish coverage."
            ),
        }
    ]

    resumed = CsindexHistoryWorkflow(
        workspace=tmp_path / "run",
        governance=governance,
        actor_user_id=1,
        collector=_FixtureCollector(fail_on_network=True),  # type: ignore[arg-type]
        rows_per_page=2,
        minimum_interval_seconds=0,
    )
    repeated = asyncio.run(resumed.run(requested_from=date(2024, 6, 1)))
    assert repeated.package_id == result.package_id


def test_hash_bound_review_and_calendar_can_stage_historical_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = _FixtureCollector()
    anchors = collector.anchors
    monkeypatch.setattr(
        csindex_history,
        "parse_current_constituent_xls",
        lambda *, scope_id, artifact: anchors[scope_id],
    )
    monkeypatch.setattr(
        csindex_history,
        "parse_adjustment_attachments",
        lambda **_kwargs: (
            collector.events[0].changes,
            {
                "parser_version": "fixture",
                "schema": "fixture",
                "content_sha256": collector.events[0]
                .attachments[0]
                .content_sha256,
                "all_attachment_sha256": [
                    collector.events[0].attachments[0].content_sha256
                ],
                "diagnostics": [],
            },
        ),
    )
    governance = _governance(tmp_path)
    first = CsindexHistoryWorkflow(
        workspace=tmp_path / "run",
        governance=governance,
        actor_user_id=1,
        collector=collector,  # type: ignore[arg-type]
        rows_per_page=2,
        minimum_interval_seconds=0,
    )
    initial = asyncio.run(
        first.run(
            requested_from=date(2024, 6, 1),
            stage_current_anchor_if_blocked=False,
        )
    )
    queue = json.loads(initial.review_queue_path.read_text())
    proposal = next(
        row for row in queue["events"] if row.get("proposal_sha256")
    )
    decisions = {
        "schema_version": "csindex-pit-review-decisions/v2",
        "archive_manifest_sha256": queue["archive_manifest_sha256"],
        "archive_review_rows_sha256": queue["archive_review_rows_sha256"],
        "reviewer": {
            "user_id": 2,
            "identity": "fixture-independent-reviewer",
            "reviewed_at": "2024-06-18T00:00:00Z",
        },
        "archive_row_decisions": [
            {
                "announcement_id": row["announcement_id"],
                "row_sha256": row["row_sha256"],
                "disposition": (
                    "target_adjustment"
                    if row["announcement_id"] == proposal["announcement_id"]
                    else "not_target"
                ),
                "reason": (
                    "fixture target row"
                    if row["announcement_id"] == proposal["announcement_id"]
                    else "fixture unrelated row"
                ),
            }
            for row in queue["events"]
        ],
        "event_decisions": [
            {
                "announcement_id": proposal["announcement_id"],
                "decision": "accepted",
                "proposal_sha256": proposal["proposal_sha256"],
                "reason": "fixture rows independently matched",
            }
        ],
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(decisions), encoding="utf-8")
    review_payload = review_path.read_bytes()
    governance.record_auxiliary_artifact(
        kind="review_decisions",
        payload=review_payload,
        expected_sha256=hashlib.sha256(review_payload).hexdigest(),
        actor_user_id=2,
    )
    calendar_payload = _signed_calendar(
        ["2024-06-01", "2024-06-14", "2024-06-17"]
    )
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps(calendar_payload), encoding="utf-8")

    resumed = CsindexHistoryWorkflow(
        workspace=tmp_path / "run",
        governance=governance,
        actor_user_id=1,
        collector=_FixtureCollector(fail_on_network=True),  # type: ignore[arg-type]
        rows_per_page=2,
        minimum_interval_seconds=0,
    )
    result = asyncio.run(
        resumed.run(
            requested_from=date(2024, 6, 1),
            review_decisions_path=review_path,
            trading_calendar_path=calendar_path,
        )
    )
    assert result.coverage_from == date(2024, 6, 1)
    report = json.loads(result.coverage_report_path.read_text())
    assert report["gaps"] == []
    assert report["automatic_approval_permitted"] is False
    assert set(report["current_anchors"]) == {
        "csi300",
        "csi500",
        "csi800",
        "csi1000",
    }
    assert report["current_anchors"]["csi800"]["derivation"] == (
        "union_of_csi300_and_csi500"
    )
    assert report["current_anchors"]["csi800"]["member_count"] == 800
    for scope_id in ("csi300", "csi500", "csi800", "csi1000"):
        coverage = report["per_index_daily_member_coverage"][scope_id]
        assert coverage["status"] == "quarantine_proposed_not_activated"
        assert coverage["production_ready"] is False
        assert len(coverage["sessions"]) == 3
        assert coverage["available_at_gap_member_session_count"] > 0
        members = report["all_historical_member_codes"][scope_id]
        assert members["current_anchor_codes"]
    assert report["all_historical_member_codes"]["csi300"][
        "all_codes_seen_in_strict_proposals"
    ]
    assert report["all_historical_member_codes"]["csi300"][
        "all_codes_seen_in_independently_reviewed_events"
    ]
    assert report["official_vs_tushare_comparison"]["status"] == (
        "not_collected_by_official_history_workflow"
    )


def test_manual_target_disposition_collects_generic_row_and_resumes_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = _FixtureCollector()
    archive_page = collector.archive.pages[0]
    archive_document = json.loads(archive_page.payload)
    target_row = next(
        row for row in archive_document["data"] if str(row["id"]) == "15267"
    )
    target_row["title"] = "关于部分指数样本调整的公告"
    target_row["theme"] = "其他"
    payload = json.dumps(
        archive_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    changed_page = _artifact(
        role="archive_page",
        url=archive_page.url,
        payload=payload,
        request_payload=archive_page.request_payload,
    )
    collector.archive = parse_archive_pages(
        pages=[changed_page],
        adjustment_announcement_ids=[],
        coverage_from=date(2024, 6, 1),
        coverage_to=date(2024, 6, 17),
    )
    anchors = collector.anchors
    monkeypatch.setattr(
        csindex_history,
        "parse_current_constituent_xls",
        lambda *, scope_id, artifact: anchors[scope_id],
    )
    monkeypatch.setattr(
        csindex_history,
        "parse_adjustment_attachments",
        lambda **_kwargs: (
            collector.events[0].changes,
            {
                "parser_version": "fixture",
                "schema": "fixture",
                "content_sha256": collector.events[0]
                .attachments[0]
                .content_sha256,
                "all_attachment_sha256": [
                    collector.events[0].attachments[0].content_sha256
                ],
                "diagnostics": [],
            },
        ),
    )
    governance = _governance(tmp_path)
    first = CsindexHistoryWorkflow(
        workspace=tmp_path / "run",
        governance=governance,
        actor_user_id=1,
        collector=collector,  # type: ignore[arg-type]
        rows_per_page=2,
        minimum_interval_seconds=0,
    )
    initial = asyncio.run(
        first.run(
            requested_from=date(2024, 6, 1),
            stage_current_anchor_if_blocked=False,
        )
    )
    queue = json.loads(initial.review_queue_path.read_text())
    generic = next(
        row for row in queue["events"] if row["announcement_id"] == "15267"
    )
    assert generic["automated_disposition"] == (
        "manual_row_disposition_required"
    )
    assert generic.get("proposal_sha256") is None
    assert not any(call.startswith("detail:15267") for call in collector.calls)

    decisions = {
        "schema_version": "csindex-pit-review-decisions/v2",
        "archive_manifest_sha256": queue["archive_manifest_sha256"],
        "archive_review_rows_sha256": queue["archive_review_rows_sha256"],
        "reviewer": {
            "user_id": 2,
            "identity": "fixture-independent-reviewer",
            "reviewed_at": "2024-06-18T00:00:00Z",
        },
        "archive_row_decisions": [
            {
                "announcement_id": row["announcement_id"],
                "row_sha256": row["row_sha256"],
                "disposition": (
                    "target_adjustment"
                    if row["announcement_id"] == "15267"
                    else "not_target"
                ),
                "reason": "manual full-archive fixture disposition",
            }
            for row in queue["events"]
        ],
        "event_decisions": [],
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(decisions), encoding="utf-8")
    review_payload = review_path.read_bytes()
    governance.record_auxiliary_artifact(
        kind="review_decisions",
        payload=review_payload,
        expected_sha256=hashlib.sha256(review_payload).hexdigest(),
        actor_user_id=2,
    )
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(
        json.dumps(
            _signed_calendar(
                ["2024-06-01", "2024-06-14", "2024-06-17"]
            )
        ),
        encoding="utf-8",
    )
    second = CsindexHistoryWorkflow(
        workspace=tmp_path / "run",
        governance=governance,
        actor_user_id=1,
        collector=collector,  # type: ignore[arg-type]
        rows_per_page=2,
        minimum_interval_seconds=0,
    )
    pending = asyncio.run(
        second.run(
            requested_from=date(2024, 6, 1),
            review_decisions_path=review_path,
            trading_calendar_path=calendar_path,
            stage_current_anchor_if_blocked=False,
        )
    )
    assert pending.coverage_from == date(2024, 6, 17)
    assert "detail:15267" in collector.calls
    assert "attachment:15267" in collector.calls
    supplemented_queue = json.loads(pending.review_queue_path.read_text())
    proposal = next(
        row
        for row in supplemented_queue["events"]
        if row["announcement_id"] == "15267"
    )
    assert proposal["proposal_sha256"]

    decisions["event_decisions"] = [
        {
            "announcement_id": "15267",
            "decision": "accepted",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "supplemented managed artifacts independently matched",
        }
    ]
    review_path.write_text(json.dumps(decisions), encoding="utf-8")
    review_payload = review_path.read_bytes()
    governance.record_auxiliary_artifact(
        kind="review_decisions",
        payload=review_payload,
        expected_sha256=hashlib.sha256(review_payload).hexdigest(),
        actor_user_id=2,
    )
    offline_collector = _FixtureCollector(fail_on_network=True)
    resumed = CsindexHistoryWorkflow(
        workspace=tmp_path / "run",
        governance=governance,
        actor_user_id=1,
        collector=offline_collector,  # type: ignore[arg-type]
        rows_per_page=2,
        minimum_interval_seconds=0,
    )
    completed = asyncio.run(
        resumed.run(
            requested_from=date(2024, 6, 1),
            review_decisions_path=review_path,
            trading_calendar_path=calendar_path,
            stage_current_anchor_if_blocked=False,
        )
    )
    assert completed.coverage_from == date(2024, 6, 1)
    assert offline_collector.calls == []
    report = json.loads(completed.coverage_report_path.read_text())
    assert report["gaps"] == []


@pytest.mark.skipif(
    os.environ.get("RUN_CSINDEX_SOURCE_SMOKE") != "1",
    reason="explicit opt-in real official-source contract smoke",
)
def test_real_csindex_source_contract_smoke() -> None:
    async def scenario() -> None:
        collector = CsindexOfficialCollector(timeout_seconds=30)
        anchor = await collector.fetch_current_anchor("csi300")
        page = await collector.fetch_archive_page(page=1, rows=10)
        response: dict[str, Any] = json.loads(page.payload)
        assert len(anchor.members) == 300
        assert response["success"] is True
        assert response["currentPage"] == 1
        assert response["pageSize"] == 10
        assert page.request_payload is not None

    try:
        asyncio.run(scenario())
    except CsindexEvidenceError:
        pytest.fail("official CSI source contract changed or is unavailable")
