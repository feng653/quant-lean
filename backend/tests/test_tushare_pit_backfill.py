from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from backend.data.provider_artifacts import (
    ContentAddressedProviderArtifactStore,
    canonical_sha256,
)
from backend.data.sources.tushare_candidate import (
    DATASET_SPECS,
    TushareCandidateClient,
)
from backend.data.sources.tushare_pit_backfill import (
    BackfillTask,
    BackfillCheckpointStore,
    TusharePitBackfillCollector,
    TusharePitBackfillError,
    TusharePitBackfillPlan,
)
from backend.services.tushare_research_trust import (
    assess_tushare_research_trust,
    build_tushare_research_timeline,
    load_latest_tushare_backfill_report,
)


def _response(fields: list[str], items: list[list[object]]) -> bytes:
    return json.dumps(
        {"code": 0, "msg": None, "data": {"fields": fields, "items": items}},
        separators=(",", ":"),
    ).encode()


def _row(fields: list[str], **values: object) -> list[object]:
    return [values.get(field) for field in fields]


def _dataset_for_api(api_name: str) -> str:
    return next(
        name for name, spec in DATASET_SPECS.items() if spec.api_name == api_name
    )


def _fixture_items(document: dict[str, object]) -> tuple[list[str], list[list[object]]]:
    api_name = str(document["api_name"])
    params = document["params"]
    assert isinstance(params, dict)
    dataset = _dataset_for_api(api_name)
    fields = list(DATASET_SPECS[dataset].fields)
    if dataset == "index_weight":
        index_code = str(params["index_code"])
        size = {
            "000300.SH": 300,
            "000905.SH": 500,
            "000906.SH": 800,
            "000852.SH": 1_000,
        }[index_code]
        return fields, [
            _row(
                fields,
                index_code=index_code,
                con_code=f"{number:06d}.SZ",
                trade_date=params["end_date"],
                weight=100 / size,
            )
            for number in range(1, size + 1)
        ]
    if dataset == "stock_basic":
        return fields, [
            _row(
                fields,
                ts_code="000001.SZ",
                symbol="000001",
                name="fixture",
                industry="bank",
                list_status=params["list_status"],
                list_date="19910403",
                delist_date=None,
            )
        ]
    if dataset == "trade_cal":
        first = datetime.strptime(str(params["start_date"]), "%Y%m%d").date()
        last = datetime.strptime(str(params["end_date"]), "%Y%m%d").date()
        return fields, [
            _row(
                fields,
                exchange="SSE",
                cal_date=(first + timedelta(days=offset)).strftime("%Y%m%d"),
                is_open=1 if offset == 0 else 0,
                pretrade_date="20151231",
            )
            for offset in range((last - first).days + 1)
        ]
    if dataset == "sw_classify":
        return fields, [
            _row(
                fields,
                index_code="801010.SI",
                industry_name="fixture industry",
                level="L1",
                industry_code="801010",
                is_pub="1",
                src="SW2021",
            )
        ]
    if dataset == "daily":
        trade_date = params.get("trade_date", params.get("start_date"))
        return fields, [
            _row(
                fields,
                ts_code=params.get("ts_code", "000001.SZ"),
                trade_date=trade_date,
                open=10,
                high=11,
                low=9,
                close=10.5,
                pre_close=10,
                change=0.5,
                pct_chg=5,
                vol=100,
                amount=1_000,
            )
        ]
    if dataset == "adj_factor":
        trade_date = params.get("trade_date", params.get("start_date"))
        return fields, [
            _row(
                fields,
                ts_code=params.get("ts_code", "000001.SZ"),
                trade_date=trade_date,
                adj_factor=1,
            )
        ]
    if dataset == "daily_basic":
        trade_date = params.get("trade_date", params.get("start_date"))
        return fields, [
            _row(
                fields,
                ts_code=params.get("ts_code", "000001.SZ"),
                trade_date=trade_date,
                total_share=1,
                float_share=1,
                total_mv=1,
                circ_mv=1,
            )
        ]
    if dataset == "index_daily":
        return fields, [
            _row(
                fields,
                ts_code=params["ts_code"],
                trade_date=params["start_date"],
                open=3_000,
                high=3_100,
                low=2_900,
                close=3_050,
                pre_close=3_000,
                change=50,
                pct_chg=1.67,
                vol=10_000,
                amount=100_000,
            )
        ]
    if dataset == "sw_membership":
        return fields, [
            _row(
                fields,
                l1_code="801010.SI",
                l1_name="fixture industry",
                ts_code=params["ts_code"],
                name="fixture",
                in_date="20160101",
                out_date=None,
                is_new="Y",
            )
        ]
    if dataset == "dividend":
        return fields, [
            _row(
                fields,
                ts_code=params["ts_code"],
                end_date="20161231",
                ann_date="20170101",
                div_proc="实施",
                record_date="20170601",
                ex_date="20170602",
                pay_date="20170603",
            )
        ]
    if dataset == "namechange":
        return fields, [
            _row(
                fields,
                ts_code=params["ts_code"],
                name="fixture",
                start_date="20160101",
                ann_date="20151231",
            )
        ]
    if dataset == "suspend_d":
        return fields, []
    raise AssertionError(dataset)


def _client(
    root: Path, handler: httpx.MockTransport | object
) -> TushareCandidateClient:
    async def no_wait(_seconds: float) -> None:
        return None

    return TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(root),
        transport=handler,  # type: ignore[arg-type]
        min_interval_seconds=0.3,
        max_attempts=1,
        sleep=no_wait,
        clock=lambda: 0.0,
    )


def _run_compact_session_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    daily_present: bool = True,
    vol: object = 100,
    amount: object = 1_000,
    suspend_timing: str | None = None,
    suspend_type: str = "S",
) -> dict[str, object]:
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {code: 1 for code in ("000300.SH", "000905.SH", "000906.SH", "000852.SH")},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        dataset = _dataset_for_api(str(document["api_name"]))
        fields, items = _fixture_items(document)
        if dataset == "index_weight":
            items = items[:1]
        params = document["params"]
        assert isinstance(params, dict)
        if dataset == "daily" and "trade_date" in params:
            if daily_present:
                items = [
                    _row(
                        fields,
                        ts_code="000001.SZ",
                        trade_date=params["trade_date"],
                        open=10,
                        high=11,
                        low=9,
                        close=10.5,
                        pre_close=10,
                        change=0.5,
                        pct_chg=5,
                        vol=vol,
                        amount=amount,
                    )
                ]
            else:
                items = []
        if dataset == "suspend_d" and "trade_date" in params:
            items = (
                [
                    _row(
                        fields,
                        ts_code="000001.SZ",
                        trade_date=params["trade_date"],
                        suspend_timing=suspend_timing,
                        suspend_type=suspend_type,
                    )
                ]
                if suspend_timing is not None
                else []
            )
        return httpx.Response(200, content=_response(fields, items))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    collector = TusharePitBackfillCollector(
        client=_client(tmp_path / "evidence", httpx.MockTransport(handler)),
        plan=plan,
        max_calls=32,
    )
    return asyncio.run(collector.run())


def test_complete_four_index_history_builds_conditional_research_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {code: 1 for code in ("000300.SH", "000905.SH", "000906.SH", "000852.SH")},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        fields, items = _fixture_items(document)
        if _dataset_for_api(str(document["api_name"])) == "index_weight":
            items = items[:1]
        return httpx.Response(200, content=_response(fields, items))

    evidence_root = tmp_path / "pit_evidence"
    store_root = evidence_root / "provider_candidates" / "tushare_backfill"
    plan = TusharePitBackfillPlan()
    collector = TusharePitBackfillCollector(
        client=_client(store_root, httpx.MockTransport(handler)),
        plan=plan,
        max_calls=128,
    )
    latest: dict[str, object] = {}
    for _ in range(8):
        latest = asyncio.run(collector.run())
        progress = latest.get("progress")
        if isinstance(progress, dict) and progress.get("complete") is True:
            break
    assert latest["candidate_collection_valid"] is True

    report, digest = load_latest_tushare_backfill_report(evidence_root)
    assert report is not None
    assessment = assess_tushare_research_trust(
        report=report,
        report_object_sha256=digest,
        required_start="2025-01-02",
        required_end="2025-01-03",
        purpose="execution_simulation",
    )
    assert assessment["eligible"] is True
    timeline = build_tushare_research_timeline(
        evidence_root=evidence_root,
        assessment=assessment,
        pool_id="csi300",
        trading_dates=["2025-01-02", "2025-01-03"],
    )
    assert timeline.dates == ("2025-01-02", "2025-01-03")
    assert timeline.members_by_date == (("000001.SZ",), ("000001.SZ",))
    assert timeline.bitemporal_availability_verified is False
    assert timeline.source_batches[0]["batch_digest"] == digest


def test_backfill_is_bounded_resumable_idempotent_and_quarantine_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, object]] = []
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {code: 1 for code in ("000300.SH", "000905.SH", "000906.SH", "000852.SH")},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        requests.append(document)
        fields, items = _fixture_items(document)
        dataset = _dataset_for_api(str(document["api_name"]))
        if dataset == "index_weight":
            items = items[:1]
        return httpx.Response(200, content=_response(fields, items))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=2,
        event_sample_size=1,
        market_chunk_months=1,
    )
    client = _client(tmp_path / "evidence", httpx.MockTransport(handler))
    collector = TusharePitBackfillCollector(client=client, plan=plan, max_calls=3)

    first = asyncio.run(collector.run())
    assert first["progress"]["calls_this_invocation"] == 3
    assert first["progress"]["completed_tasks"] == 3
    assert first["progress"]["foundation_complete"] is False
    assert first["classification"] == "quarantine"
    assert first["production_pit_ready"] is False
    assert first["runtime_data_changed"] is False

    latest = first
    for _ in range(10):
        latest = asyncio.run(collector.run())
        if latest["progress"]["complete"]:
            break
    assert latest["progress"]["complete"] is True
    assert latest["progress"]["planned_tasks"] == 20
    assert latest["progress"]["completed_tasks"] == 20
    assert latest["progress"]["all_index_historical_security_count"] == 1
    assert latest["progress"]["full_universe_planned_security_count"] == 1
    assert latest["progress"]["sampled_security_count"] == 1
    assert latest["progress"]["sampled_security_count_is_diagnostic_only"] is True
    assert latest["progress"]["canonical_open_session_count"] == 1
    assert latest["progress"]["reconciled_session_count"] == 1
    assert latest["progress"]["all_sessions_reconciled"] is True
    intersection = latest["session_universe_intersection"]
    assert intersection["valid_sessions"] == 1
    assert intersection["invalid_sessions"] == 0
    assert intersection["member_status_counts"] == {
        "observed_daily_liquidity_without_suspend": 1
    }
    assert intersection["production_full_day_tradability_proven"] is False
    assert latest["incomplete_index_months"] == []
    assert len(latest["index_month_coverage"]) == 4
    assert {
        row["maximum_unique_members_on_one_date"]
        for row in latest["index_month_coverage"]
    } == {1}
    calls_after_completion = len(requests)
    market_requests = [
        request
        for request in requests
        if _dataset_for_api(str(request["api_name"]))
        in {"daily", "adj_factor", "daily_basic", "suspend_d"}
    ]
    assert len(market_requests) == 4
    assert {tuple(request["params"].items()) for request in market_requests} == {
        (("trade_date", "20160101"),)
    }

    legacy_reconciliation = collector.checkpoints.load(plan)
    legacy_reconciliation["session_reconciliation"]["20160101"].update(
        {
            "schema_version": "tushare-session-universe-intersection/v1",
            "blockers": [
                {"code": "000001.SZ", "reason": "daily_and_suspend_conflict"}
            ],
            "valid": False,
        }
    )
    legacy_reconciliation["checkpoint_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in legacy_reconciliation.items()
            if key != "checkpoint_sha256"
        }
    )
    collector.checkpoints.save(legacy_reconciliation)

    repeated = asyncio.run(collector.run())
    assert repeated["progress"]["calls_this_invocation"] == 0
    assert repeated["progress"]["complete"] is True
    assert len(requests) == calls_after_completion

    retained_failure = collector.checkpoints.load(plan)
    retained_failure["session_reconciliation"]["20160101"].update(
        {
            "schema_version": "tushare-session-universe-intersection/v1",
            "blockers": [
                {"code": "000001.SZ", "reason": "daily_and_suspend_conflict"}
            ],
            "valid": False,
        }
    )
    retained_failure["failures"]["unrelated-provider-failure"] = {
        "task": {
            "task_id": "unrelated-provider-failure",
            "category": "sample_corporate_event",
            "dataset": "dividend",
            "params": {"ts_code": "999999.SZ"},
            "required": False,
        },
        "diagnostic": {"code": "provider_network_timeout", "retryable": True},
        "observed_at": "2026-08-02T00:00:00Z",
    }
    retained_failure["checkpoint_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in retained_failure.items()
            if key != "checkpoint_sha256"
        }
    )
    collector.checkpoints.save(retained_failure)
    retained = asyncio.run(collector.run())
    assert retained["progress"]["calls_this_invocation"] == 0
    assert retained["progress"]["complete"] is False
    assert retained["failures"] == [retained_failure["failures"]["unrelated-provider-failure"]]
    persisted = collector.checkpoints.load(plan)
    assert persisted["session_reconciliation"]["20160101"]["blockers"] == []
    assert persisted["session_reconciliation"]["20160101"]["valid"] is True
    assert "unrelated-provider-failure" in persisted["failures"]
    assert len(requests) == calls_after_completion

    checkpoint = next((tmp_path / "evidence" / "checkpoints").glob("*.json"))
    serialized = checkpoint.read_text(encoding="utf-8")
    checkpoint_document = json.loads(serialized)
    assert retained["checkpoint"]["sha256"] == checkpoint_document["checkpoint_sha256"]
    assert "fixture-token-value" not in serialized
    assert '"token"' not in serialized
    assert "proxy" not in serialized
    assert checkpoint.stat().st_mode & 0o777 == 0o600


def test_tradable_historical_member_missing_from_cross_section_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {code: 1 for code in ("000300.SH", "000905.SH", "000906.SH", "000852.SH")},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        dataset = _dataset_for_api(str(document["api_name"]))
        fields, items = _fixture_items(document)
        if dataset == "index_weight":
            items = items[:1]
        if dataset == "daily" and "trade_date" in document["params"]:
            items = []
        return httpx.Response(200, content=_response(fields, items))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    collector = TusharePitBackfillCollector(
        client=_client(tmp_path / "evidence", httpx.MockTransport(handler)),
        plan=plan,
        max_calls=32,
    )
    report = asyncio.run(collector.run())
    assert report["progress"]["completed_tasks"] == report["progress"]["planned_tasks"]
    assert report["progress"]["pending_tasks"] == 0
    assert report["progress"]["complete"] is False
    assert report["candidate_collection_valid"] is False
    assert report["session_universe_intersection"]["invalid_sessions"] == 1
    assert report["failures"] == [
        {
            "code": "historical_member_session_coverage_invalid",
            "retryable": False,
            "trade_date": "20160101",
            "blockers": [
                {
                    "code": "000001.SZ",
                    "reason": "tradable_member_missing_without_suspend_evidence",
                }
            ],
        }
    ]


def test_positive_daily_with_explicit_partial_suspension_is_restricted_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_compact_session_case(
        tmp_path,
        monkeypatch,
        vol=64_243.36,
        amount=112_972.7443,
        suspend_timing="09:30-13:00",
        suspend_type="S",
    )
    assert report["progress"]["complete"] is True
    assert report["failures"] == []
    intersection = report["session_universe_intersection"]
    assert intersection["member_status_counts"] == {
        "observed_liquidity_with_explicit_partial_suspension": 1
    }
    assert intersection["ambiguous_member_count"] == 0
    assert intersection["production_full_day_tradability_proven"] is False


@pytest.mark.parametrize(
    ("timing", "suspend_type", "vol", "amount", "reason", "ambiguous", "conflicts"),
    [
        (
            "09:30-15:00",
            "S",
            100,
            1_000,
            "daily_conflicts_with_full_day_suspension",
            0,
            1,
        ),
        (
            "盘中",
            "S",
            100,
            1_000,
            "daily_suspend_semantics_ambiguous",
            1,
            0,
        ),
        (
            "09:30-13:00",
            "UNKNOWN",
            100,
            1_000,
            "daily_suspend_semantics_ambiguous",
            1,
            0,
        ),
        (
            "09:30-13:00",
            "S",
            0,
            0,
            "daily_suspend_without_positive_liquidity",
            1,
            0,
        ),
    ],
)
def test_daily_suspend_invalid_semantics_remain_structured_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
    suspend_type: str,
    vol: object,
    amount: object,
    reason: str,
    ambiguous: int,
    conflicts: int,
) -> None:
    report = _run_compact_session_case(
        tmp_path,
        monkeypatch,
        vol=vol,
        amount=amount,
        suspend_timing=timing,
        suspend_type=suspend_type,
    )
    assert report["progress"]["complete"] is False
    failure = report["failures"][0]
    assert failure["code"] == "historical_member_session_coverage_invalid"
    assert failure["blockers"] == [{"code": "000001.SZ", "reason": reason}]
    assert report["session_universe_intersection"][
        "production_full_day_tradability_proven"
    ] is False
    assert report["session_universe_intersection"]["ambiguous_member_count"] == ambiguous
    assert report["session_universe_intersection"]["conflicting_member_count"] == conflicts


def test_no_daily_requires_explicit_full_day_suspension_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_compact_session_case(
        tmp_path,
        monkeypatch,
        daily_present=False,
        suspend_timing="全天",
        suspend_type="S",
    )
    assert report["progress"]["complete"] is True
    assert report["failures"] == []
    assert report["session_universe_intersection"]["member_status_counts"] == {
        "candidate_full_day_suspension_without_daily": 1
    }
    assert report["session_universe_intersection"][
        "production_full_day_tradability_proven"
    ] is False


def test_v3_plan_separates_session_cross_sections_from_member_metadata() -> None:
    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-06",
        sample_size=1,
        event_sample_size=0,
        market_chunk_months=2,
    )
    market = plan.session_market_tasks(["20160104", "20160105", "20160104"])
    metadata = plan.full_universe_metadata_tasks(
        {
            "000001.SZ": ["2016-01", "2016-02", "2016-03", "2016-06"],
            "600000.SH": ["2016-02"],
        }
    )
    assert len(market) == 8
    assert {tuple(task.params) for task in market} == {("trade_date",)}
    assert {task.params["trade_date"] for task in market} == {"20160104", "20160105"}
    assert {task.dataset for task in market} == {
        "daily",
        "adj_factor",
        "daily_basic",
        "suspend_d",
    }
    assert {
        task.params["ts_code"]
        for task in metadata
        if task.category == "full_universe_industry_membership"
    } == {"000001.SZ", "600000.SH"}
    assert {
        (task.dataset, task.params["ts_code"])
        for task in metadata
        if task.category == "full_universe_corporate_event"
    } == {
        ("dividend", "000001.SZ"),
        ("namechange", "000001.SZ"),
        ("dividend", "600000.SH"),
        ("namechange", "600000.SH"),
    }


def test_incomplete_canonical_calendar_fails_closed() -> None:
    task = BackfillTask.build(
        category="trading_calendar",
        dataset="trade_cal",
        params={
            "exchange": "SSE",
            "start_date": "20160101",
            "end_date": "20160103",
        },
        required=True,
    )
    with pytest.raises(TusharePitBackfillError, match="coverage is incomplete"):
        TusharePitBackfillCollector._validate_observation(
            task,
            [
                {
                    "exchange": "SSE",
                    "cal_date": "20160101",
                    "is_open": 1,
                    "pretrade_date": "20151231",
                }
            ],
        )


def test_backfill_stops_on_sanitized_failure_then_resumes(tmp_path: Path) -> None:
    fail = True
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if fail:
            return httpx.Response(
                200,
                content=json.dumps(
                    {"code": -2001, "msg": "account denied", "data": None}
                ).encode(),
            )
        document = json.loads(request.content)
        fields, items = _fixture_items(document)
        return httpx.Response(200, content=_response(fields, items))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    collector = TusharePitBackfillCollector(
        client=_client(tmp_path / "evidence", httpx.MockTransport(handler)),
        plan=plan,
        max_calls=1,
    )
    report = asyncio.run(collector.run())
    assert report["progress"]["completed_tasks"] == 0
    assert report["failures"][0]["diagnostic"] == {
        "code": "provider_permission_or_points_required",
        "provider_code": "-2001",
        "retryable": False,
    }
    assert "fixture-token-value" not in json.dumps(report)

    fail = False
    resumed = asyncio.run(collector.run())
    assert resumed["progress"]["completed_tasks"] == 1
    assert resumed["failures"] == []
    assert requests == 2


def test_incomplete_index_snapshot_is_retained_but_not_checkpointed_complete(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        fields, items = _fixture_items(document)
        return httpx.Response(200, content=_response(fields, items[:10]))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    collector = TusharePitBackfillCollector(
        client=_client(tmp_path / "evidence", httpx.MockTransport(handler)),
        plan=plan,
        max_calls=1,
    )
    report = asyncio.run(collector.run())
    assert report["progress"]["completed_tasks"] == 0
    assert report["index_month_coverage"][0]["status"] == "failed"
    assert report["failures"][0]["diagnostic"] == {
        "code": "incomplete_index_weight_monthly_snapshot",
        "retryable": True,
    }
    assert report["failures"][0]["receipt"]["classification"] == "quarantine"
    assert report["candidate_collection_valid"] is False


def test_backfill_checkpoint_tamper_and_unsafe_plan_fail_closed(tmp_path: Path) -> None:
    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    store = BackfillCheckpointStore(tmp_path / "evidence", plan.run_id)
    checkpoint = store.load(plan)
    checkpoint = store.save(checkpoint)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["completed"] = {"forged": {}}
    store.path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(TusharePitBackfillError, match="digest changed"):
        store.load(plan)

    with pytest.raises(TusharePitBackfillError, match="fully elapsed"):
        TusharePitBackfillPlan(first_month="2099-01", last_month="2099-01")
    with pytest.raises(TusharePitBackfillError, match="sample_size"):
        TusharePitBackfillPlan(
            first_month="2016-01", last_month="2016-01", sample_size=0
        )


def test_v1_checkpoint_migrates_without_losing_completed_receipts(
    tmp_path: Path,
) -> None:
    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    store = BackfillCheckpointStore(tmp_path / "evidence", plan.run_id)
    checkpoint = store.load(plan)
    checkpoint["completed"] = {
        "legacy-task": {
            "task": {
                "task_id": "legacy-task",
                "category": "sample_market_or_state",
                "dataset": "daily",
                "params": {
                    "ts_code": "000001.SZ",
                    "start_date": "20160101",
                    "end_date": "20160131",
                },
                "required": True,
            },
            "receipt": {"manifest_sha256": "a" * 64},
        }
    }
    checkpoint["checkpoint_sha256"] = canonical_sha256(
        {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
    )
    saved = store.save(checkpoint)
    legacy = dict(saved)
    legacy.pop("checkpoint_sha256")
    legacy["schema_version"] = "tushare-pit-candidate-backfill-checkpoint/v1"
    legacy["checkpoint_sha256"] = canonical_sha256(legacy)
    store.path.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = store.load(plan)
    assert migrated["schema_version"] == "tushare-pit-candidate-backfill-checkpoint/v3"
    assert "legacy-task" in migrated["completed"]
    assert migrated["migrations"] == [
        {
            "from": "tushare-pit-candidate-backfill-checkpoint/v1",
            "to": "tushare-pit-candidate-backfill-checkpoint/v3",
            "reason": "replace_per_security_market_calls_with_session_cross_sections",
            "legacy_completed_evidence_retained": True,
            "legacy_receipts_counted_as_cross_sections": False,
        }
    ]


def test_legacy_per_security_receipt_is_not_reused_as_cross_section(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        document = json.loads(request.content)
        fields, items = _fixture_items(document)
        return httpx.Response(200, content=_response(fields, items))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
        market_chunk_months=1,
    )
    client = _client(tmp_path / "evidence", httpx.MockTransport(handler))
    collector = TusharePitBackfillCollector(client=client, plan=plan, max_calls=1)
    params = {
        "ts_code": "000001.SZ",
        "start_date": "20160101",
        "end_date": "20160131",
    }
    observation = asyncio.run(client.fetch("daily", params))
    legacy = BackfillTask.build(
        category="sample_market_or_state",
        dataset="daily",
        params=params,
        required=True,
    )
    checkpoint = collector.checkpoints.load(plan)
    checkpoint["completed"][legacy.task_id] = {
        "task": legacy.public_scope(),
        "receipt": observation.receipt,
        "row_count": len(observation.rows),
        "validation": {"status": "observed"},
        "observed_at": observation.manifest["bitemporal"]["ingested_at"],
    }
    checkpoint["checkpoint_sha256"] = canonical_sha256(
        {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
    )
    checkpoint = collector.checkpoints.save(checkpoint)
    cross_section = next(
        task for task in plan.session_market_tasks(["20160104"])
        if task.dataset == "daily"
    )

    assert collector._reuse_compatible_completed_tasks([cross_section], checkpoint) == 0
    assert cross_section.task_id not in checkpoint["completed"]
    assert legacy.task_id in checkpoint["completed"]
    assert requests == 1


def test_optional_transient_failure_is_retried_and_does_not_block(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(500, json={"error": "fixture unavailable"})
        document = json.loads(request.content)
        fields, items = _fixture_items(document)
        return httpx.Response(200, content=_response(fields, items))

    plan = TusharePitBackfillPlan(first_month="2016-01", last_month="2016-01")
    collector = TusharePitBackfillCollector(
        client=_client(tmp_path / "evidence", httpx.MockTransport(handler)),
        plan=plan,
        max_calls=2,
    )
    optional = BackfillTask.build(
        category="benchmark_index_daily",
        dataset="index_daily",
        params={
            "ts_code": "000300.SH",
            "start_date": "20160101",
            "end_date": "20160131",
        },
        required=False,
    )
    checkpoint = collector.checkpoints.load(plan)

    assert asyncio.run(
        collector._execute_tasks([optional], checkpoint, budget=2)
    ) == 1
    assert optional.task_id not in checkpoint["completed"]
    failure = checkpoint["optional_failures"][optional.task_id]
    assert failure["diagnostic"]["code"] == "provider_service_unavailable"
    assert failure["diagnostic"]["retryable"] is True
    assert checkpoint["failures"] == {}
    assert asyncio.run(
        collector._execute_tasks([optional], checkpoint, budget=2)
    ) == 1
    assert checkpoint["completed"][optional.task_id]["row_count"] == 1
    assert checkpoint["optional_failures"] == {}
    assert requests == 2


def test_unclassified_completed_index_snapshot_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {code: 1 for code in ("000300.SH", "000905.SH", "000906.SH", "000852.SH")},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        fields, items = _fixture_items(document)
        return httpx.Response(200, content=_response(fields, items[:1]))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    collector = TusharePitBackfillCollector(
        client=_client(tmp_path / "evidence", httpx.MockTransport(handler)),
        plan=plan,
        max_calls=4,
    )
    first = asyncio.run(collector.run())
    assert first["progress"]["foundation_complete"] is False
    checkpoint = collector.checkpoints.load(plan)
    index_task = plan.foundation_tasks()[0]
    checkpoint["completed"][index_task.task_id]["validation"]["status"] = "observed"
    checkpoint["checkpoint_sha256"] = canonical_sha256(
        {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
    )
    collector.checkpoints.save(checkpoint)

    with pytest.raises(TusharePitBackfillError, match="not classified complete"):
        collector._historical_membership(
            plan.foundation_tasks(), checkpoint["completed"]
        )
