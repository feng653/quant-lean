from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import httpx
import pytest

from backend.data.provider_artifacts import (
    ContentAddressedProviderArtifactStore,
    ProviderArtifactError,
    build_candidate_artifact_manifest,
)
from backend.data.sources.tushare_candidate import (
    DATASET_SPECS,
    TushareCandidateObservation,
    TushareCandidateClient,
    TushareCandidateError,
    collect_governed_csindex_current_anchor,
    compare_index_weight_to_official_members,
    run_standard_preflight,
    standard_preflight_plan,
    tushare_daily_panel,
)


def _response(fields: list[str], items: list[list[object]]) -> bytes:
    return json.dumps(
        {"code": 0, "msg": None, "data": {"fields": fields, "items": items}},
        separators=(",", ":"),
    ).encode()


def _manifest(response: bytes, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "provider": "fixture_vendor",
        "dataset": "daily",
        "endpoint": "https://example.invalid/api",
        "request": {"api_name": "daily", "params": {}, "fields": ["date"]},
        "response_payload": response,
        "response_fields": ["date"],
        "row_count": 1,
        "ingested_at": "2026-08-02T00:00:00Z",
        "temporal_contract": {
            "effective_at": {"fields": ["date"], "evidence": "provider_field"},
            "available_at": {"fields": [], "evidence": "declared_ingestion_time"},
        },
    }
    values.update(overrides)
    return build_candidate_artifact_manifest(**values)  # type: ignore[arg-type]


def test_content_addressed_store_is_quarantine_only_and_detects_tamper(
    tmp_path: Path,
) -> None:
    response = b'{"date":"20260801"}'
    manifest = _manifest(response)
    store = ContentAddressedProviderArtifactStore(tmp_path)
    first = store.record(response_payload=response, manifest=manifest)
    second = store.record(response_payload=response, manifest=manifest)
    assert first == second
    loaded, loaded_response = store.read(first["manifest_sha256"])
    assert loaded_response == response
    assert loaded["classification"] == "quarantine"
    assert loaded["promotion"]["eligible"] is False

    artifact = (
        tmp_path
        / "artifacts"
        / "sha256"
        / first["artifact_sha256"][:2]
        / first["artifact_sha256"]
    )
    artifact.write_bytes(b"tampered")
    with pytest.raises(ProviderArtifactError, match="response bytes changed"):
        store.read(first["manifest_sha256"])


def test_candidate_store_repairs_preexisting_permissions_and_keeps_files_private(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate-evidence"
    preexisting = [
        root,
        root / "artifacts",
        root / "artifacts" / "sha256",
        root / "manifests",
        root / "manifests" / "sha256",
        root / "reports",
        root / "reports" / "sha256",
    ]
    for directory in preexisting:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)

    response = b'{"date":"20260801"}'
    store = ContentAddressedProviderArtifactStore(root)
    result = store.record(response_payload=response, manifest=_manifest(response))
    artifact = store._target(store.artifact_root, result["artifact_sha256"])
    manifest = store._target(
        store.manifest_root,
        result["manifest_sha256"],
        ".json",
    )

    for directory in [*preexisting, artifact.parent, manifest.parent]:
        assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
    assert stat.S_IMODE(artifact.lstat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.lstat().st_mode) == 0o600

    # Re-recording known content repairs an out-of-band broad file mode.
    artifact.chmod(0o644)
    store.record(response_payload=response, manifest=_manifest(response))
    assert stat.S_IMODE(artifact.lstat().st_mode) == 0o600


def test_candidate_store_rejects_symlinked_root_or_content_path(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    os.symlink(outside, root_link)
    with pytest.raises(ProviderArtifactError, match="directory is unsafe"):
        ContentAddressedProviderArtifactStore(root_link)

    root = tmp_path / "candidate-evidence"
    (root / "artifacts").mkdir(parents=True)
    os.symlink(outside, root / "artifacts" / "sha256")
    with pytest.raises(ProviderArtifactError, match="directory is unsafe"):
        ContentAddressedProviderArtifactStore(root)

    secure_root = tmp_path / "secure-evidence"
    response = b'{"date":"20260801"}'
    store = ContentAddressedProviderArtifactStore(secure_root)
    result = store.record(response_payload=response, manifest=_manifest(response))
    artifact = store._target(store.artifact_root, result["artifact_sha256"])
    artifact.unlink()
    os.symlink(outside, artifact)
    with pytest.raises(ProviderArtifactError, match="object is unsafe"):
        store.read(result["manifest_sha256"])


def test_manifest_refuses_credential_like_evidence() -> None:
    with pytest.raises(ProviderArtifactError, match="credential-like"):
        _manifest(b"payload", request={"api_name": "daily", "token": "forbidden"})


def test_tushare_fetch_records_exact_response_without_token(tmp_path: Path) -> None:
    spec = DATASET_SPECS["daily"]
    item = [
        "000001.SZ", "20250102", 10, 11, 9, 10.5, 10, 0.5, 5, 100, 1000,
    ]
    observed_requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        observed_requests.append(document)
        return httpx.Response(200, content=_response(list(spec.fields), [item]))

    store = ContentAddressedProviderArtifactStore(tmp_path)
    client = TushareCandidateClient(
        token="fixture-token-value",
        store=store,
        transport=httpx.MockTransport(handler),
        min_interval_seconds=0.3,
    )
    observation = asyncio.run(
        client.fetch(
            "daily",
            {"ts_code": "000001.SZ", "trade_date": "20250102"},
            ingested_at="2026-08-02T00:00:00Z",
        )
    )
    assert observed_requests[0]["token"] == "fixture-token-value"
    serialized_manifest = json.dumps(observation.manifest)
    assert "fixture-token-value" not in serialized_manifest
    assert '"token"' not in serialized_manifest
    assert observation.manifest["classification"] == "quarantine"
    assert observation.rows[0]["close"] == 10.5

    panel = tushare_daily_panel(observation)
    assert ("000001", "volume") in panel.columns
    assert float(panel.loc["2025-01-02", ("000001", "close")]) == 10.5


def test_tushare_errors_are_sanitized_and_not_retried_for_permissions(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=json.dumps(
                {"code": -2001, "msg": "token fixture-token-value has no permission", "data": None}
            ).encode(),
        )

    client = TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TushareCandidateError) as caught:
        asyncio.run(client.fetch("daily", {"ts_code": "000001.SZ"}))
    assert calls == 1
    assert "fixture-token-value" not in str(caught.value)
    assert "no permission" not in str(caught.value)


def test_tushare_http_error_does_not_expose_response_body(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="fixture-token-value must not escape")

    client = TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TushareCandidateError) as caught:
        asyncio.run(client.fetch("daily", {"ts_code": "000001.SZ"}))
    assert str(caught.value) == "daily request was rejected (HTTP 403)"
    assert caught.value.diagnostic() == {
        "code": "provider_http_rejected",
        "provider_code": 403,
        "retryable": False,
    }


def test_explicit_proxy_is_loopback_only_and_never_exposed(tmp_path: Path) -> None:
    client = TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(tmp_path),
        proxy_url="http://user:fixture-password@127.0.0.1:12001",
    )
    diagnostic = client.transport_diagnostic()
    assert diagnostic == {
        "explicit_proxy_configured": True,
        "proxy_boundary": "loopback_only",
        "proxy_url_retained": False,
    }
    assert "12001" not in json.dumps(diagnostic)
    assert "fixture-password" not in json.dumps(diagnostic)

    with pytest.raises(
        TushareCandidateError,
        match="must terminate on loopback",
    ) as caught:
        TushareCandidateClient(
            token="fixture-token-value",
            store=ContentAddressedProviderArtifactStore(tmp_path / "remote"),
            proxy_url="http://proxy.example:8080",
        )
    assert caught.value.diagnostic()["code"] == (
        "explicit_proxy_configuration_invalid"
    )


def test_provider_permission_rejection_has_stable_sanitized_diagnostic(
    tmp_path: Path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                    {
                        "code": -2001,
                        "msg": "account permission detail must not escape",
                    "data": None,
                }
            ).encode(),
        )

    client = TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TushareCandidateError) as caught:
        asyncio.run(client.fetch("daily", {"ts_code": "000001.SZ"}))
    assert caught.value.diagnostic() == {
        "code": "provider_permission_or_points_required",
        "provider_code": "-2001",
        "retryable": False,
    }
    assert "permission detail" not in str(caught.value)


def test_standard_preflight_rejects_empty_required_datasets(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        spec = next(
            candidate
            for candidate in DATASET_SPECS.values()
            if candidate.api_name == document["api_name"]
        )
        return httpx.Response(200, content=_response(list(spec.fields), []))

    async def no_wait(_seconds: float) -> None:
        return None

    client = TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(tmp_path),
        transport=httpx.MockTransport(handler),
        sleep=no_wait,
        clock=lambda: 0.0,
    )
    report = asyncio.run(
        run_standard_preflight(
            client,
            ts_code="000001.SZ",
            start="2025-01-02",
            end="2025-01-10",
            cross_check=False,
        )
    )
    assert report["candidate_collection_valid"] is False
    assert report["production_pit_ready"] is False
    assert report["promotion"]["eligible"] is False
    assert len(report["datasets"]) == 11
    by_name = {row["dataset"]: row for row in report["datasets"]}
    for dataset in (
        "trade_cal",
        "daily",
        "adj_factor",
        "daily_basic",
        "index_weight",
        "stock_basic",
    ):
        assert by_name[dataset]["status"] == "insufficient_rows"
        assert by_name[dataset]["reason"] == (
            "provider_returned_empty_complete_month"
            if dataset == "index_weight"
            else "required_dataset_below_minimum_rows"
        )
        assert dataset in report["required_failures"]
    assert by_name["index_weight"]["minimum_rows"] == 300
    assert by_name["index_weight"]["monthly_probe"] == {
        "status": "no_monthly_snapshot_returned",
        "reason": "provider_returned_empty_complete_month",
        "requested_complete_month": {
            "start_date": "20250101",
            "end_date": "20250131",
        },
        "expected_index_code": "000300.SH",
        "minimum_member_rows": 300,
        "vendor_trade_dates": [],
        "guidance": (
            "The full calendar-month request was accepted but returned no "
            "weight snapshot. Preserve the artifact; confirm provider "
            "publication lag, retention range or entitlement before retrying."
        ),
    }
    assert report["index_weight_monthly_probe"]["status"] == (
        "no_monthly_snapshot_returned"
    )
    assert by_name["stock_basic"]["minimum_rows"] == 1_000
    assert report["plan_validation"]["failures"][0]["reason"] == (
        "probe_window_has_no_open_trading_sessions"
    )
    assert report["stored_report_sha256"]


def test_standard_preflight_accepts_meaningful_required_coverage(
    tmp_path: Path,
) -> None:
    def rows_for(api_name: str, fields: list[str]) -> list[list[object]]:
        def row(**values: object) -> list[object]:
            return [values.get(field) for field in fields]

        if api_name == "trade_cal":
            return [
                row(
                    exchange="SSE",
                    cal_date=f"202501{day:02d}",
                    is_open=1 if day in {2, 3, 6, 7, 8, 9, 10} else 0,
                    pretrade_date="20241231",
                )
                for day in range(2, 11)
            ]
        if api_name == "daily":
            return [
                row(
                    ts_code="000001.SZ",
                    trade_date="20250102",
                    open=10,
                    high=11,
                    low=9,
                    close=10.5,
                    vol=100,
                    amount=1_000,
                )
            ]
        if api_name == "adj_factor":
            return [row(ts_code="000001.SZ", trade_date="20250102", adj_factor=1)]
        if api_name == "daily_basic":
            return [
                row(
                    ts_code="000001.SZ",
                    trade_date="20250102",
                    total_share=1,
                    float_share=1,
                    total_mv=1,
                    circ_mv=1,
                )
            ]
        if api_name == "stock_basic":
            return [
                row(
                    ts_code=f"{code:06d}.SZ",
                    symbol=f"{code:06d}",
                    name=f"N{code}",
                    list_status="L",
                    list_date="20000101",
                )
                for code in range(1, 1_001)
            ]
        if api_name == "index_weight":
            return [
                row(
                    index_code="000300.SH",
                    con_code=f"{code:06d}.SZ",
                    trade_date="20250102",
                    weight=100 / 300,
                )
                for code in range(1, 301)
            ]
        return []

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        spec = next(
            candidate
            for candidate in DATASET_SPECS.values()
            if candidate.api_name == document["api_name"]
        )
        fields = list(spec.fields)
        return httpx.Response(
            200,
            content=_response(fields, rows_for(spec.api_name, fields)),
        )

    async def no_wait(_seconds: float) -> None:
        return None

    client = TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(tmp_path),
        transport=httpx.MockTransport(handler),
        sleep=no_wait,
        clock=lambda: 0.0,
    )
    report = asyncio.run(
        run_standard_preflight(
            client,
            ts_code="000001.SZ",
            start="2025-01-02",
            end="2025-01-10",
            cross_check=False,
        )
    )
    assert report["candidate_collection_valid"] is True
    assert report["required_failures"] == []
    assert report["plan_validation"] == {
        "open_session_count": 7,
        "failures": [],
    }
    assert report["production_pit_ready"] is False


def test_closed_market_probe_is_invalid_even_with_complete_calendar(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        spec = next(
            candidate
            for candidate in DATASET_SPECS.values()
            if candidate.api_name == document["api_name"]
        )
        items: list[list[object]] = []
        if spec.api_name == "trade_cal":
            items = [
                ["SSE", f"202501{day:02d}", 0, "20241231"]
                for day in range(2, 11)
            ]
        return httpx.Response(200, content=_response(list(spec.fields), items))

    async def no_wait(_seconds: float) -> None:
        return None

    report = asyncio.run(
        run_standard_preflight(
            TushareCandidateClient(
                token="fixture-token-value",
                store=ContentAddressedProviderArtifactStore(tmp_path),
                transport=httpx.MockTransport(handler),
                sleep=no_wait,
                clock=lambda: 0.0,
            ),
            ts_code="000001.SZ",
            start="2025-01-02",
            end="2025-01-10",
            cross_check=False,
        )
    )
    trade_calendar = next(
        row for row in report["datasets"] if row["dataset"] == "trade_cal"
    )
    assert trade_calendar["status"] == "ok"
    assert report["candidate_collection_valid"] is False
    assert report["plan_validation"]["failures"] == [
        {
            "reason": "probe_window_has_no_open_trading_sessions",
            "guidance": (
                "Choose a window containing at least one provider-declared open "
                "session; a weekend or exchange holiday window cannot validate "
                "daily market-data coverage"
            ),
        }
    ]


def test_index_weight_probe_expands_short_range_to_complete_month() -> None:
    plan = dict(
        standard_preflight_plan(
            ts_code="000001.SZ",
            start="2025-01-02",
            end="2025-01-10",
        )
    )
    assert plan["daily"]["start_date"] == "20250102"
    assert plan["daily"]["end_date"] == "20250110"
    assert plan["index_weight"]["start_date"] == "20250101"
    assert plan["index_weight"]["end_date"] == "20250131"


def test_index_weight_probe_never_requests_future_days_for_current_month() -> None:
    plan = dict(
        standard_preflight_plan(
            ts_code="000001.SZ",
            start="2026-07-19",
            end="2026-08-01",
            observed_on=__import__("datetime").date(2026, 8, 2),
        )
    )
    assert plan["daily"] == {
        "ts_code": "000001.SZ",
        "start_date": "20260719",
        "end_date": "20260801",
    }
    assert plan["index_weight"] == {
        "index_code": "000300.SH",
        "start_date": "20260701",
        "end_date": "20260731",
    }


def test_index_weight_monthly_probe_marks_complete_candidate_not_pit(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        spec = next(
            candidate
            for candidate in DATASET_SPECS.values()
            if candidate.api_name == document["api_name"]
        )
        fields = list(spec.fields)
        if spec.api_name == "index_weight":
            rows = [
                ["000300.SH", f"{code:06d}.SZ", "20250102", 1.0]
                for code in range(1, 301)
            ]
        elif spec.api_name == "stock_basic":
            rows = [
                [f"{code:06d}.SZ", f"{code:06d}", "N", "L", "20000101"]
                for code in range(1, 1001)
            ]
        elif spec.api_name == "trade_cal":
            rows = [["SSE", "20250102", 1, "20241231"]]
        elif spec.api_name in {"daily", "adj_factor", "daily_basic"}:
            values = {
                "daily": ["000001.SZ", "20250102", 1, 1, 1, 1, 1, 0, 0, 1, 1],
                "adj_factor": ["000001.SZ", "20250102", 1],
                "daily_basic": ["000001.SZ", "20250102", 1, 1, 1, 1, 1, 1, 1, 1],
            }
            rows = [values[spec.api_name]]
        else:
            rows = []
        return httpx.Response(200, content=_response(fields, rows))

    async def no_wait(_seconds: float) -> None:
        return None

    client = TushareCandidateClient(
        token="fixture-token-value",
        store=ContentAddressedProviderArtifactStore(tmp_path),
        transport=httpx.MockTransport(handler),
        sleep=no_wait,
        clock=lambda: 0.0,
    )
    report = asyncio.run(
        run_standard_preflight(
            client,
            ts_code="000001.SZ",
            start="2025-01-02",
            end="2025-01-02",
            cross_check=False,
        )
    )
    probe = report["index_weight_monthly_probe"]
    assert probe["status"] == "complete_monthly_snapshot_candidate"
    assert probe["vendor_trade_dates"] == ["20250102"]
    assert report["production_pit_ready"] is False


def test_index_weight_comparison_binds_official_anchor_digest() -> None:
    observation = TushareCandidateObservation(
        dataset="index_weight",
        fields=("index_code", "con_code", "trade_date", "weight"),
        rows=(
            {"index_code": "000300.SH", "con_code": "000001.SZ", "trade_date": "20250102", "weight": 1.0},
            {"index_code": "000300.SH", "con_code": "600000.SH", "trade_date": "20250102", "weight": 1.0},
        ),
        manifest={},
        receipt={"artifact_sha256": "a" * 64},
    )
    exact = compare_index_weight_to_official_members(
        observation,
        official_member_codes=["000001", "600000"],
        official_observed_on="2025-01-02",
        official_content_sha256="b" * 64,
    )
    assert exact["status"] == "exact_match"
    assert exact["official"]["content_sha256"] == "b" * 64

    difference = compare_index_weight_to_official_members(
        observation,
        official_member_codes=["000001", "600001"],
        official_observed_on="2025-01-02",
        official_content_sha256="b" * 64,
    )
    assert difference["status"] == "difference_detected"
    assert difference["vendor_only_examples"] == ["600000"]
    assert difference["official_only_examples"] == ["600001"]

    not_comparable = compare_index_weight_to_official_members(
        observation,
        official_member_codes=["000001", "600000"],
        official_observed_on="2025-01-03",
        official_content_sha256="b" * 64,
    )
    assert not_comparable["status"] == "not_comparable"
    assert not_comparable["reason"] == (
        "vendor_and_official_observation_dates_do_not_match"
    )
    assert "vendor_only_count" not in not_comparable


def test_official_anchor_opt_in_records_only_governed_snapshot() -> None:
    artifact = SimpleNamespace(content_sha256="c" * 64)
    anchor = SimpleNamespace(
        observed_on=__import__("datetime").date(2025, 1, 2),
        members=(
            SimpleNamespace(security_code="000001"),
            SimpleNamespace(security_code="600000"),
        ),
        artifact=artifact,
    )

    class Collector:
        async def fetch_current_anchor(self, scope_id: str) -> object:
            assert scope_id == "csi300"
            return anchor

    class Governance:
        def record_artifact(self, *, artifact: object, actor_user_id: int) -> dict[str, object]:
            assert artifact is anchor.artifact
            assert actor_user_id == 7
            return {"schema_version": "pit-evidence-governance/v1", "idempotent": False}

    evidence = asyncio.run(
        collect_governed_csindex_current_anchor(
            scope_id="csi300",
            actor_user_id=7,
            collector=Collector(),
            governance=Governance(),
        )
    )
    assert evidence["member_codes"] == ["000001", "600000"]
    assert evidence["content_sha256"] == "c" * 64
    assert evidence["production_import_performed"] is False
    assert evidence["historical_replay_complete"] is False

    with pytest.raises(
        TushareCandidateError,
        match="actor_user_id must be positive",
    ):
        asyncio.run(
            collect_governed_csindex_current_anchor(
                scope_id="csi300",
                actor_user_id=0,
                collector=Collector(),
                governance=Governance(),
            )
        )
