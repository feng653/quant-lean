from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from backend.api import data as data_api
from backend.data.provider_artifacts import (
    ContentAddressedProviderArtifactStore,
    canonical_json_bytes,
    canonical_sha256,
)
from backend.data.point_in_time_universe import timeline_from_identity
from backend.data.research_data_store import (
    ResearchDataStore,
    ResearchDataStoreError,
    _ResearchRowSpool,
)
from backend.data.sources.tushare_candidate import TushareCandidateClient
from backend.data.sources.tushare_pit_backfill import (
    TusharePitBackfillCollector,
    TusharePitBackfillPlan,
)
from backend.tests.test_tushare_pit_backfill import (
    _client as _full_client,
    _dataset_for_api,
    _fixture_items,
    _response,
    _row,
)


def _client(root: Path) -> TushareCandidateClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        params = body["params"]
        assert body["api_name"] == "index_weight"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "fields": ["index_code", "con_code", "trade_date", "weight"],
                    "items": [[params["index_code"], "000001.SZ", "20160129", 1.0]],
                },
            },
        )

    async def no_wait(_seconds: float) -> None:
        return None

    return TushareCandidateClient(
        token="test-token-value",
        store=ContentAddressedProviderArtifactStore(root),
        transport=httpx.MockTransport(handler),
        min_interval_seconds=0.3,
        max_attempts=1,
        sleep=no_wait,
        clock=lambda: 0.0,
    )


def test_read_only_status_does_not_create_a_research_directory(tmp_path: Path) -> None:
    root = tmp_path / "does-not-exist"
    assert ResearchDataStore(root).status()["available"] is False
    assert not root.exists()


def test_legacy_crash_spool_is_quarantined_without_activation(tmp_path: Path) -> None:
    store = ResearchDataStore(tmp_path / "research")
    store._ensure_write_directories()  # noqa: SLF001
    legacy = store.generations / ".research-spool.legacy"
    legacy.write_bytes(b"not-a-generation")

    report = store.quarantine_stale_import_spools()

    assert not legacy.exists()
    assert len(report["quarantined"]) == 1
    isolated = store.generations / "import-quarantine" / report["quarantined"][0][
        "quarantine_name"
    ]
    assert isolated.read_bytes() == b"not-a-generation"
    assert report["automatic_delete"] is False
    assert report["active_generation_changed"] is False
    assert not store.active_pointer.exists()


def test_live_owned_spool_is_not_quarantined(tmp_path: Path) -> None:
    store = ResearchDataStore(tmp_path / "research")
    store._ensure_write_directories()  # noqa: SLF001
    spool = _ResearchRowSpool(store.generations)
    journal = Path(f"{spool.path}-journal")
    journal.write_bytes(b"live-sidecar")
    try:
        report = store.quarantine_stale_import_spools()
        assert spool.path.exists()
        assert journal.read_bytes() == b"live-sidecar"
        assert report["quarantined"] == []
        assert report["live_spools_skipped"] == [spool.path.name]
    finally:
        spool.close()
        journal.unlink(missing_ok=True)


def test_fresh_remote_owned_spool_is_fail_safe_live_unknown(
    tmp_path: Path,
) -> None:
    store = ResearchDataStore(tmp_path / "research")
    store._ensure_write_directories()  # noqa: SLF001
    spool = _ResearchRowSpool(store.generations)
    metadata = json.loads(spool.metadata_path.read_text(encoding="utf-8"))
    metadata["owner_host"] = "remote-research-host"
    spool.metadata_path.write_bytes(canonical_json_bytes(metadata))
    try:
        report = store.quarantine_stale_import_spools()
        assert spool.path.exists()
        assert report["quarantined"] == []
        assert report["live_spools_skipped"] == [spool.path.name]
    finally:
        spool.close()


def test_crash_generation_build_is_quarantined_without_activation(
    tmp_path: Path,
) -> None:
    store = ResearchDataStore(tmp_path / "research")
    store._ensure_write_directories()  # noqa: SLF001
    partial = store.generations / ".research.crashed.sqlite"
    partial.write_bytes(b"partial-generation")

    report = store.quarantine_stale_import_spools()

    assert not partial.exists()
    assert report["quarantined"][0]["temporary_kind"] == "generation_build"
    assert not store.active_pointer.exists()


def test_bound_historical_generation_survives_unrelated_active_pointer_damage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    generations = root / "generations"
    generations.mkdir(parents=True)
    generation_id = "a" * 64
    database = generations / f"{generation_id}.sqlite"
    identity = {"fixture": "historical-generation"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE generation_metadata (key TEXT PRIMARY KEY, value_json TEXT)"
        )
        connection.execute(
            "INSERT INTO generation_metadata VALUES ('generation_id', ?)",
            (json.dumps(generation_id),),
        )
        connection.execute(
            "INSERT INTO generation_metadata VALUES ('identity', ?)",
            (json.dumps(identity),),
        )
    store = ResearchDataStore(root)
    store._write_generation_binding(database, generation_id, identity)  # noqa: SLF001
    (root / "active.json").write_text("{broken", encoding="utf-8")

    pointer, resolved = store._generation(generation_id)  # noqa: SLF001

    assert resolved == database
    assert pointer["generation_id"] == generation_id
    assert pointer["historical_generation_binding_verified"] is True

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE generation_metadata SET value_json=? WHERE key='identity'",
            (json.dumps({"fixture": "tampered"}),),
        )
    with pytest.raises(Exception, match="binding changed"):
        store._generation(generation_id)  # noqa: SLF001


def test_bound_historical_generation_rejects_non_object_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    generations = root / "generations"
    generations.mkdir(parents=True)
    generation_id = "b" * 64
    database = generations / f"{generation_id}.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE generation_metadata (key TEXT PRIMARY KEY, value_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO generation_metadata VALUES (?, ?)",
            [
                ("generation_id", json.dumps(generation_id)),
                ("identity", json.dumps({"fixture": "malformed-binding"})),
            ],
        )
    database.with_suffix(".binding.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ResearchDataStoreError, match="binding is invalid"):
        ResearchDataStore(root)._generation(generation_id)  # noqa: SLF001


def test_all_a_timeline_uses_delisted_status_over_older_listed_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    generations = root / "generations"
    generations.mkdir(parents=True)
    generation_id = "c" * 64
    database = generations / f"{generation_id}.sqlite"
    identity = {"fixture": "all-a-status-history"}
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE generation_metadata (
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL
            );
            CREATE TABLE market_daily (
                trade_date TEXT NOT NULL,
                security_code TEXT NOT NULL,
                hfq_close REAL NOT NULL
            );
            CREATE TABLE security_master (
                security_code TEXT NOT NULL,
                list_status TEXT NOT NULL,
                list_date TEXT NOT NULL,
                delist_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_manifest_sha256 TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO generation_metadata VALUES (?, ?)",
            [
                ("generation_id", json.dumps(generation_id)),
                ("identity", json.dumps(identity)),
                ("warnings", "[]"),
                ("source_manifests", "[]"),
            ],
        )
        connection.executemany(
            "INSERT INTO market_daily VALUES (?, ?, ?)",
            [
                (day, code, close)
                for day, close in (("2020-01-02", 10.0), ("2020-01-03", 11.0))
                for code in ("000001.SZ", "000002.SZ")
            ],
        )
        connection.executemany(
            "INSERT INTO security_master VALUES (?, ?, ?, ?, '{}', ?, ?)",
            [
                ("000001.SZ", "L", "20100101", "", "l" * 64, "2020-01-01"),
                (
                    "000001.SZ",
                    "D",
                    "20100101",
                    "20200102",
                    "d" * 64,
                    "2020-01-03",
                ),
                ("000002.SZ", "L", "20100101", "", "k" * 64, "2020-01-01"),
            ],
        )
    store = ResearchDataStore(root)
    store._write_generation_binding(  # noqa: SLF001
        database, generation_id, identity
    )

    result = store.load_market_frame(
        pool_id="all_a",
        required_start="2020-01-02",
        required_end="2020-01-03",
        generation_id=generation_id,
        fields=("close",),
    )
    timeline = timeline_from_identity(
        result["report"]["timeline_identity"],
        trading_dates=result["frame"].index,
    )

    assert timeline.members_on("2020-01-02") == ("000001.SZ", "000002.SZ")
    assert timeline.members_on("2020-01-03") == ("000002.SZ",)


def test_tushare_index_candidates_materialize_into_research_only_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    research = tmp_path / "research"
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {
            "000300.SH": 1,
            "000905.SH": 1,
            "000906.SH": 1,
            "000852.SH": 1,
        },
    )
    report = asyncio.run(
        TusharePitBackfillCollector(
            client=_client(evidence),
            plan=TusharePitBackfillPlan(
                first_month="2016-01",
                last_month="2016-01",
            ),
            max_calls=4,
        ).run()
    )
    assert report["progress"]["completed_tasks"] == 4

    store = ResearchDataStore(research)
    imported = store.import_tushare_index_history(evidence)
    assert imported["available"] is True
    assert imported["row_count"] == 4
    assert imported["pool_count"] == 4
    assert imported["live_eligible"] is False
    assert imported["classification"] == "vendor_research_trusted"
    assert imported["research_trust_profile"] == "tushare_research_trusted"
    assert len(imported["candidate_report_sha256"]) == 64
    assert imported["import"]["production_tables_changed"] is False

    pool = store.query_pool("csi300", "2016-01-31")
    assert pool["available"] is True
    assert pool["resolved_month"] == "2016-01"
    assert pool["records"][0]["security_code"] == "000001.SZ"
    assert pool["resolved_vendor_trade_date"] == "2016-01-29"
    assert "monthly_snapshot_not_exact_intramonth_timeline" in pool["warnings"]
    assert pool["live_eligible"] is False

    second = store.import_tushare_index_history(evidence)
    assert second["generation_id"] == imported["generation_id"]

    before_vendor_snapshot = store.query_pool("csi300", "2016-01-01")
    assert before_vendor_snapshot == {
        "available": False,
        "reason": "research_pool_history_not_covered",
        "records": [],
    }


def test_conflict_report_is_truthful_when_activated_source_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {code: 1 for code in ("000300.SH", "000905.SH", "000906.SH", "000852.SH")},
    )
    asyncio.run(
        TusharePitBackfillCollector(
            client=_client(evidence),
            plan=TusharePitBackfillPlan(first_month="2016-01", last_month="2016-01"),
            max_calls=4,
        ).run()
    )
    store = ResearchDataStore(tmp_path / "research")
    store.import_tushare_index_history(evidence)

    monkeypatch.setattr(
        "backend.data.point_in_time_master.PointInTimeMasterStore.query_as_of",
        lambda *_args, **_kwargs: {"available": False, "records": []},
    )
    conflicts = store.conflict_report()
    assert conflicts["conflict_count"] == 0
    assert len(conflicts["comparisons"]) == 4
    assert {row["status"] for row in conflicts["comparisons"]} == {
        "right_source_unavailable"
    }
    assert conflicts["status"] == "insufficient_sources"
    assert conflicts["cross_validated"] is False


def test_same_month_materialization_selects_the_most_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(
        "backend.data.sources.tushare_pit_backfill._INDEX_MINIMUM_MEMBERS",
        {code: 1 for code in ("000300.SH", "000905.SH", "000906.SH", "000852.SH")},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        params = document["params"]
        rows = [
            [params["index_code"], "000001.SZ", "20160110", 1.0],
            [params["index_code"], "000002.SZ", "20160110", 2.0],
            [params["index_code"], "000003.SZ", "20160129", 3.0],
        ]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "fields": ["index_code", "con_code", "trade_date", "weight"],
                    "items": rows,
                },
            },
        )

    async def no_wait(_seconds: float) -> None:
        return None

    client = TushareCandidateClient(
        token="test-token",
        store=ContentAddressedProviderArtifactStore(evidence),
        transport=httpx.MockTransport(handler),
        min_interval_seconds=0.3,
        max_attempts=1,
        sleep=no_wait,
        clock=lambda: 0.0,
    )
    asyncio.run(
        TusharePitBackfillCollector(
            client=client,
            plan=TusharePitBackfillPlan(first_month="2016-01", last_month="2016-01"),
            max_calls=4,
        ).run()
    )
    store = ResearchDataStore(tmp_path / "research")
    store.import_tushare_index_history(evidence)

    pool = store.query_pool("csi300", "2016-01-31")
    assert pool["resolved_vendor_trade_date"] == "2016-01-10"
    assert [row["security_code"] for row in pool["records"]] == [
        "000001.SZ",
        "000002.SZ",
    ]


def test_reconciled_market_materialization_is_partial_streamed_and_unit_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
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
                    vol=123,
                    amount=456,
                )
            ]
        if dataset == "adj_factor" and "trade_date" in params:
            items = [
                _row(
                    fields,
                    ts_code="000001.SZ",
                    trade_date=params["trade_date"],
                    adj_factor=2,
                )
            ]
        if dataset == "daily_basic" and "trade_date" in params:
            items = [
                _row(
                    fields,
                    ts_code="000001.SZ",
                    trade_date=params["trade_date"],
                    turnover_rate=3.5,
                    volume_ratio=1.2,
                    pe=8,
                    pb=1.1,
                    total_share=100,
                    float_share=80,
                    total_mv=1_000,
                    circ_mv=800,
                )
            ]
        if dataset == "suspend_d" and "trade_date" in params:
            items = [
                _row(
                    fields,
                    ts_code="000001.SZ",
                    trade_date=params["trade_date"],
                    suspend_timing="盘中",
                    suspend_type="S",
                )
            ]
        if dataset == "index_daily":
            items = [
                _row(
                    fields,
                    ts_code=params["ts_code"],
                    trade_date=trade_date,
                    open=3_000,
                    high=3_100,
                    low=2_900,
                    close=3_050,
                    pre_close=3_000,
                    vol=100,
                    amount=1_000,
                )
                for trade_date in ("20160101", "20160105", "20160129")
            ]
        return httpx.Response(200, content=_response(fields, items))

    plan = TusharePitBackfillPlan(
        first_month="2016-01",
        last_month="2016-01",
        sample_size=1,
        event_sample_size=0,
    )
    report = asyncio.run(
        TusharePitBackfillCollector(
            client=_full_client(evidence, httpx.MockTransport(handler)),
            plan=plan,
            max_calls=32,
        ).run()
    )
    assert report["progress"]["pending_tasks"] == 0
    assert report["progress"]["all_sessions_reconciled"] is False

    store = ResearchDataStore(tmp_path / "research")
    imported = store.import_tushare_reconciled_history(
        evidence,
        run_id=plan.run_id,
        candidate_report_sha256=report["stored_report_sha256"],
        collection_report=report,
    )
    assert imported["classification"] == "single_source_research"
    assert imported["market"]["available"] is True
    assert imported["market"]["row_count"] == 1
    assert "strict_tradability_reconciliation_failed_warning_only" in imported["warnings"]
    assert imported["live_eligible"] is False

    all_a = store.load_market_frame(
        pool_id="all_a",
        required_start="2016-01-01",
        required_end="2016-01-01",
    )
    assert all_a["report"]["ready"] is True
    frame = all_a["frame"]
    assert frame.loc["2016-01-01", ("000001.SZ", "close")] == 21
    assert frame.loc["2016-01-01", ("000001.SZ", "volume")] == 12_300
    assert frame.loc["2016-01-01", ("000001.SZ", "amount")] == 456_000
    assert frame.loc["2016-01-01", ("000001.SZ", "turnover_rate")] == 3.5
    assert frame.loc["2016-01-01", ("000001.SZ", "total_share")] == 1_000_000
    assert frame.loc["2016-01-01", ("000001.SZ", "total_mv")] == 10_000_000
    assert all_a["source_provenance"]["source_manifest_count"] > 0
    assert "source_manifests" not in all_a["source_provenance"]
    all_a_timeline = timeline_from_identity(
        all_a["report"]["timeline_identity"],
        trading_dates=frame.index,
    )
    assert all_a_timeline.pool_id == "all_a"
    assert all_a_timeline.members_on("2016-01-01") == ("000001.SZ",)

    assert store.query_pool("csi300", "2016-01-01")["available"] is False
    assert store.query_pool("csi300", "2016-01-31")["available"] is True
    all_a_pool = store.query_pool("all_a", "2016-01-01")
    assert all_a_pool["available"] is True
    assert set(all_a_pool["records"][0]) == {
        "security_code",
        "list_date",
        "source_manifest_sha256",
        "ingested_at",
    }
    benchmark = store.load_benchmark(
        index_code="000300",
        required_start="2016-01-01",
        required_end="2016-01-31",
    )
    assert benchmark["report"]["ready"] is True
    assert benchmark["series"].iloc[0] == 3_050
    assert benchmark["report"]["coverage_complete"] is False
    assert benchmark["report"]["maximum_internal_gap_days"] == 24
    assert "requested_benchmark_window_partially_covered" in benchmark["report"]["warnings"]

    generation_path = (
        tmp_path / "research" / "generations" / f"{imported['generation_id']}.sqlite"
    )
    generation_mtime = generation_path.stat().st_mtime_ns
    second = store.import_tushare_reconciled_history(
        evidence,
        run_id=plan.run_id,
        candidate_report_sha256=report["stored_report_sha256"],
        collection_report=report,
    )
    assert second["generation_id"] == imported["generation_id"]
    assert generation_path.stat().st_mtime_ns == generation_mtime
    assert len(list((tmp_path / "research" / "generations").glob("*.sqlite"))) == 1

    active_before_cancel = store.active_pointer.read_bytes()

    def cancel_before_activation(event):
        if event["stage"] == "research_import_activate":
            raise RuntimeError("fixture cancellation before activation")

    with pytest.raises(RuntimeError, match="cancellation before activation"):
        store.import_tushare_reconciled_history(
            evidence,
            run_id=plan.run_id,
            candidate_report_sha256=report["stored_report_sha256"],
            collection_report=report,
            progress=cancel_before_activation,
        )
    assert store.active_pointer.read_bytes() == active_before_cancel

    original_extend = _ResearchRowSpool.extend

    def fail_after_spool_created(self, kind, rows):
        if kind == "market_daily":
            raise RuntimeError("fixture spool failure")
        return original_extend(self, kind, rows)

    with monkeypatch.context() as scoped:
        scoped.setattr(_ResearchRowSpool, "extend", fail_after_spool_created)
        with pytest.raises(RuntimeError, match="fixture spool failure"):
            store.import_tushare_reconciled_history(
                evidence,
                run_id=plan.run_id,
                candidate_report_sha256=report["stored_report_sha256"],
                collection_report=report,
            )
    assert list((tmp_path / "research" / "generations").glob(".research-spool.*")) == []

    optional_evidence = tmp_path / "optional-evidence"
    shutil.copytree(evidence, optional_evidence)
    optional_checkpoint_path = (
        optional_evidence / "checkpoints" / f"{plan.run_id}.json"
    )
    optional_checkpoint = json.loads(optional_checkpoint_path.read_text(encoding="utf-8"))
    for task_id, completed in list(optional_checkpoint["completed"].items()):
        task = completed.get("task") or {}
        if task.get("dataset") == "daily_basic" and task.get("params", {}).get("trade_date"):
            optional_checkpoint["completed"][task_id] = {
                "task": task,
                "row_count": 0,
                "validation": {"status": "optional_source_unavailable"},
                "optional_failure": {"code": "fixture_optional_unavailable"},
                "observed_at": completed["observed_at"],
            }
    for reconciliation in optional_checkpoint["session_reconciliation"].values():
        reconciliation["dataset_manifest_sha256"].pop("daily_basic", None)
    optional_checkpoint["checkpoint_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in optional_checkpoint.items()
            if key != "checkpoint_sha256"
        }
    )
    optional_checkpoint_path.write_bytes(canonical_json_bytes(optional_checkpoint))
    optional_store = ResearchDataStore(tmp_path / "optional-research")
    optional_import = optional_store.import_tushare_reconciled_history(
        optional_evidence,
        run_id=plan.run_id,
    )
    assert optional_import["market"]["available"] is True
    assert "all_market_daily_basic_rows_missing" in optional_import["warnings"]
    optional_frame = optional_store.load_market_frame(
        pool_id="all_a",
        required_start="2016-01-01",
        required_end="2016-01-01",
    )["frame"]
    assert optional_frame.loc["2016-01-01", ("000001.SZ", "turnover_rate")] != 3.5

    optional_generation = (
        tmp_path
        / "optional-research"
        / "generations"
        / f"{optional_import['generation_id']}.sqlite"
    )
    optional_store.active_pointer.unlink()
    optional_generation.with_suffix(".binding.json").unlink()
    orphan_connection = sqlite3.connect(optional_generation)
    orphan_connection.execute("UPDATE market_daily SET hfq_close=999")
    orphan_connection.commit()
    orphan_connection.close()
    rebuilt = optional_store.import_tushare_reconciled_history(
        optional_evidence,
        run_id=plan.run_id,
    )
    assert rebuilt["generation_id"] == optional_import["generation_id"]
    rebuilt_frame = optional_store.load_market_frame(
        pool_id="all_a",
        required_start="2016-01-01",
        required_end="2016-01-01",
    )["frame"]
    assert rebuilt_frame.loc["2016-01-01", ("000001.SZ", "close")] == 21
    assert list(optional_generation.parent.glob(".orphan.*")) == []

    generation = generation_path
    connection = sqlite3.connect(generation)
    connection.execute(
        "UPDATE market_daily SET hfq_close=hfq_close+1 WHERE security_code='000001.SZ'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(Exception, match="changed"):
        store.load_benchmark(
            index_code="000300.SH",
            required_start="2016-01-01",
            required_end="2016-01-31",
        )
    with pytest.raises(Exception, match="binding changed"):
        store.import_tushare_reconciled_history(
            evidence,
            run_id=plan.run_id,
            candidate_report_sha256=report["stored_report_sha256"],
            collection_report=report,
        )
    with pytest.raises(Exception, match="checkpoint is missing"):
        store.import_tushare_reconciled_history(evidence, run_id="0" * 32)


def test_research_refresh_api_uses_a_distinct_warning_only_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broker:
        calls: list[dict[str, object]] = []

        async def submit_job(self, **kwargs):
            self.calls.append(kwargs)
            return "research-job"

    broker = Broker()
    monkeypatch.setattr("backend.dependencies.get_job_broker", lambda: broker)
    response = asyncio.run(
        data_api.trigger_research_data_refresh(
            data_api.ResearchDataRefreshBody(
                source_id="tushare",
                from_month="2016-01",
                to_month="2026-06",
                max_calls=16,
            ),
            user={"id": 7},
        )
    )
    assert response["data"]["mode"] == "async_research_data_warning_only"
    assert response["data"]["automatic_production_activation"] is False
    assert broker.calls[0]["job_type"] == "research_data_refresh"
    assert broker.calls[0]["resource_type"] == "research_data_source"

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            data_api.trigger_research_data_refresh(
                data_api.ResearchDataRefreshBody(source_id="baostock"),
                user={"id": 7},
            )
        )
    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "research_source_not_refreshable"
