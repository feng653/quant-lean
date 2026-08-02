from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

from backend.data.provider_artifacts import ContentAddressedProviderArtifactStore
from backend.data.sources.tushare_candidate import DATASET_SPECS, TushareCandidateClient
from backend.data.sources.tushare_contract_probe import (
    FOUR_INDEX_CODES,
    default_contract_probe_months,
    run_tushare_pit_contract_probe,
)


def _response(fields: list[str], rows: list[list[object]]) -> bytes:
    return json.dumps(
        {"code": 0, "msg": "", "data": {"fields": fields, "items": rows}}
    ).encode()


def test_default_contract_months_use_closed_months_only() -> None:
    assert default_contract_probe_months(date(2026, 8, 2)) == (
        "2016-01",
        "2020-01",
        "2023-01",
        "2025-01",
        "2026-06",
        "2026-07",
    )


def test_contract_probe_detects_sparse_availability_cutoff_without_promotion(
    tmp_path: Path,
) -> None:
    minimum = {
        "000300.SH": 300,
        "000905.SH": 500,
        "000906.SH": 800,
        "000852.SH": 1_000,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        api_name = document["api_name"]
        params = document["params"]
        spec = next(
            value for value in DATASET_SPECS.values() if value.api_name == api_name
        )
        fields = list(spec.fields)

        def row(**values: object) -> list[object]:
            return [values.get(field) for field in fields]

        rows: list[list[object]] = []
        if api_name == "index_weight" and params["start_date"][:6] != "202607":
            index_code = params["index_code"]
            trade_date = (
                "20160129"
                if params["start_date"][:6] == "201601"
                else "20260630"
            )
            rows = [
                row(
                    index_code=index_code,
                    con_code=f"{code:06d}.SZ",
                    trade_date=trade_date,
                    weight=1,
                )
                for code in range(1, minimum[index_code] + 1)
            ]
        elif api_name in {"daily", "adj_factor", "daily_basic"}:
            rows = [
                row(
                    ts_code=f"{code:06d}.SZ",
                    trade_date="20260630",
                    open=10,
                    high=11,
                    low=9,
                    close=10,
                    vol=1,
                    amount=1,
                    adj_factor=1,
                    total_share=1,
                    float_share=1,
                    total_mv=1,
                    circ_mv=1,
                )
                for code in range(1, 31)
            ]
        elif api_name == "stock_basic":
            status = params["list_status"]
            rows = [
                row(
                    ts_code="000001.SZ",
                    symbol="000001",
                    name="Fixture",
                    list_status=status,
                    list_date="19910403",
                    delist_date="20200101" if status == "D" else None,
                )
            ]
        elif api_name == "trade_cal":
            rows = [
                row(
                    exchange="SSE",
                    cal_date="20260630",
                    is_open=1,
                    pretrade_date="20260629",
                )
            ]
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
        run_tushare_pit_contract_probe(
            client,
            probe_months=("2016-01", "2026-06", "2026-07"),
            sample_size=4,
            event_security_count=0,
        )
    )

    assert report["candidate_collection_valid"] is True
    assert report["production_pit_ready"] is False
    assert report["production_import_performed"] is False
    assert report["activation_performed"] is False
    assert report["promotion"]["eligible"] is False
    assert report["contract_checks"] == {
        "four_index_2016_sparse_anchor_observed": True,
        "thirty_security_market_cross_section_observed": True,
        "continuous_2016_to_current_coverage_proven": False,
        "historical_available_at_proven": False,
        "historical_revision_retention_proven": False,
        "licence_retention_approved": False,
    }
    assert report["security_sample"]["sample_size"] == 4
    for code in FOUR_INDEX_CODES:
        availability = report["index_availability"][code]
        assert availability["latest_complete_month_observed"] == "2026-06"
        assert availability["first_empty_month_after_latest_complete"] == "2026-07"
        assert availability["cutoff_is_exact"] is False
    assert report["stored_report_sha256"]
