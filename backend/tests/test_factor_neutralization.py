from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from backend.data.factor_research_runs import FactorResearchRunStore
from backend.data.point_in_time_master import PointInTimeMasterStore
from backend.research.factor_analysis import neutralize_factor_exposures
from backend.services.factor_neutralization import (
    NeutralizationInputError,
    extract_size_panel,
    inspect_size_capability,
    load_industry_panel,
)
from backend.services.factor_research import (
    FactorResearchBody,
    execute_factor_research,
)


@pytest.fixture(autouse=True)
def isolated_pit_factor_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ready(**kwargs: Any) -> SimpleNamespace:
        cache = kwargs["cache"]
        pool_id = str(kwargs["pool_id"])
        pivot, provenance = await cache.load_pivot_with_provenance(pool_id)
        return SimpleNamespace(
            pool_id=pool_id,
            market=SimpleNamespace(
                frame=pivot,
                source_provenance=provenance,
            ),
        )

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        ready,
    )


def _frame(payload: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        payload["values"],
        index=pd.DatetimeIndex(pd.to_datetime(payload["dates"])),
        columns=payload["codes"],
        dtype=float,
    )


def _industry_document(codes: list[str], *, snapshot: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "point-in-time-master-import/v1",
        "domain": "industry",
        "scope_id": "cninfo_008001",
        "evidence_kind": (
            "current_snapshot" if snapshot else "effective_dated_history"
        ),
        "coverage_from": "2024-01-01",
        "coverage_to": "2024-01-01" if snapshot else "2024-03-31",
        "source": {
            "provider": "fixture_industry",
            "dataset": "industry_history",
            "version": "2024q1",
            "evidence_level": "public_cross_validated",
            "retrieved_at": "2024-04-01T00:00:00Z",
            "content_sha256": "a" * 64,
        },
        "records": [
            {
                "security_code": code,
                "effective_from": "2024-01-01",
                "effective_to": "2024-01-01" if snapshot else "2024-03-31",
                "industry_code": "BANK" if index < len(codes) // 2 else "TECH",
                "industry_name": "银行" if index < len(codes) // 2 else "科技",
            }
            for index, code in enumerate(codes)
        ],
    }


def _market_frame(codes: list[str]) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=45)
    columns = pd.MultiIndex.from_product(
        [codes, ["close", "amount", "float_market_cap"]]
    )
    rows: list[list[float]] = []
    for day in range(len(dates)):
        row: list[float] = []
        for rank in range(1, len(codes) + 1):
            row.extend(
                [
                    float(10 + rank) * (1 + rank / 10_000) ** day,
                    float(rank * 1_000_000 + day),
                    float(np.exp(10 + rank / 20) + day),
                ]
            )
        rows.append(row)
    return pd.DataFrame(rows, index=dates, columns=columns)


def _provenance() -> dict[str, Any]:
    return {
        "providers": ["fixture"],
        "evidence_levels": ["public_cross_validated"],
        "adjustments": ["hfq"],
        "content_sha256": "b" * 64,
        "all_batches_raw_cross_validated": True,
        "all_batches_adjusted_factor_validated": True,
        "field_provenance": {
            "float_market_cap": {
                "schema_version": "point-in-time-field-provenance/v1",
                "field": "float_market_cap",
                "point_in_time": True,
                "effective_date_semantics": "trading_date_close",
                "available_at": "market_close",
                "observation_lag_sessions": 0,
                "evidence_level": "licensed_vendor",
                "provider": "fixture_vendor",
                "dataset": "daily_float_market_cap",
                "version": "2024q1",
                "retrieved_at": "2024-04-01T00:00:00Z",
                "content_sha256": "c" * 64,
            }
        },
    }


def test_daily_joint_ols_has_no_cross_date_fit_or_future_leakage() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    codes = [f"{index:06d}" for index in range(12)]
    industries = pd.DataFrame(
        [
            ["A"] * 6 + ["B"] * 6,
            ["A"] * 4 + ["B"] * 4 + ["C"] * 4,
        ],
        index=dates,
        columns=codes,
    )
    caps = pd.DataFrame(
        [np.exp(np.linspace(8, 12, 12)), np.exp(np.linspace(9, 15, 12))],
        index=dates,
        columns=codes,
    )
    factor = pd.DataFrame(
        [
            2 + np.array([1] * 6 + [-1] * 6) + 3 * np.log(caps.iloc[0]),
            -50 + np.linspace(-4, 7, 12) - 2 * np.log(caps.iloc[1]),
        ],
        index=dates,
        columns=codes,
    )
    first = neutralize_factor_exposures(
        factor,
        mode="industry+size",
        industries=industries,
        market_caps=caps,
        min_samples=6,
    )
    mutated_factor = factor.copy()
    mutated_factor.iloc[1] *= 10_000
    mutated_industry = industries.copy()
    mutated_industry.iloc[1] = ["Z"] * 12
    mutated_caps = caps.copy()
    mutated_caps.iloc[1] *= 999
    second = neutralize_factor_exposures(
        mutated_factor,
        mode="industry+size",
        industries=mutated_industry,
        market_caps=mutated_caps,
        min_samples=6,
    )

    first_residual = _frame(first["residuals"])
    second_residual = _frame(second["residuals"])
    pd.testing.assert_series_equal(
        first_residual.iloc[0],
        second_residual.iloc[0],
    )
    assert first["daily"][0]["before"]["r_squared"] == pytest.approx(1.0)
    assert abs(first["daily"][0]["after"]["log_market_cap"]) < 1e-10
    assert first["daily"][0]["date"] == "2024-01-02"


def test_strict_neutralization_reports_missing_and_rank_exclusions() -> None:
    date = pd.Timestamp("2024-01-02")
    factor = pd.DataFrame(
        [[1.0, 2.0, 3.0, np.nan]],
        index=[date],
        columns=list("ABCD"),
    )
    industries = pd.DataFrame(
        [["BANK", None, "TECH", "TECH"]],
        index=[date],
        columns=list("ABCD"),
    )
    result = neutralize_factor_exposures(
        factor,
        mode="industry",
        industries=industries,
        min_samples=3,
    )

    assert result["daily"][0]["status"] == "insufficient_samples"
    assert result["daily"][0]["dropped_by_reason"] == {
        "factor_missing": 1,
        "industry_missing": 1,
        "size_missing_or_nonpositive": 0,
    }
    assert result["summary"]["dates_excluded"] == 1


def test_industry_loader_rejects_snapshot_and_returns_verified_batches(
    tmp_path: Path,
) -> None:
    codes = [f"{index:06d}" for index in range(12)]
    historical = PointInTimeMasterStore(tmp_path / "historical.db")
    historical.import_batch(
        **_industry_document(codes),
        imported_by_user_id=7,
    )
    panel, evidence = load_industry_panel(
        historical,
        dates=pd.bdate_range("2024-01-01", periods=3),
        codes=codes,
    )
    assert panel.notna().all().all()
    assert evidence["coverage"]["coverage_ratio"] == 1.0
    assert len(evidence["source_batches"]) == 1
    assert len(evidence["source_batches"][0]["batch_digest"]) == 64

    snapshot = PointInTimeMasterStore(tmp_path / "snapshot.db")
    snapshot.import_batch(
        **_industry_document(codes, snapshot=True),
        imported_by_user_id=7,
    )
    with pytest.raises(
        NeutralizationInputError,
        match="缺少完整点时行业覆盖",
    ) as exc_info:
        load_industry_panel(
            snapshot,
            dates=pd.DatetimeIndex([pd.Timestamp("2024-01-01")]),
            codes=codes,
        )
    assert exc_info.value.code == (
        "current_snapshot_not_valid_for_historical_research"
    )


def test_size_requires_field_level_pit_provenance_and_full_coverage() -> None:
    codes = [f"{index:06d}" for index in range(12)]
    frame = _market_frame(codes)
    unavailable = inspect_size_capability(
        ["close", "float_market_cap"],
        {"content_sha256": "b" * 64},
    )
    assert unavailable["ready"] is False
    assert unavailable["reason"] == "point_in_time_size_provenance_missing"

    panel, evidence = extract_size_panel(
        frame,
        dates=frame.index,
        codes=codes,
        provenance=_provenance(),
    )
    assert panel.shape == (45, 12)
    assert evidence["field"] == "float_market_cap"
    assert evidence["coverage"]["coverage_ratio"] == 1.0

    missing = frame.copy()
    missing.loc[missing.index[2], (codes[0], "float_market_cap")] = np.nan
    with pytest.raises(NeutralizationInputError) as exc_info:
        extract_size_panel(
            missing,
            dates=missing.index,
            codes=codes,
            provenance=_provenance(),
        )
    assert exc_info.value.code == "point_in_time_size_coverage_missing"


def test_execution_persists_neutralization_request_result_and_digest(
    tmp_path: Path,
    allowed_isolated_cpu_executor: object,
) -> None:
    codes = [f"{index:06d}" for index in range(20)]
    pivot = _market_frame(codes)
    provenance = _provenance()

    class Cache:
        async def load_pivot_with_provenance(self, _cache_key: str):
            return pivot, provenance

        @staticmethod
        def _source_trust(_value: dict[str, Any]) -> str:
            return "licensed"

    pit = PointInTimeMasterStore(tmp_path / "pit.db")
    pit.import_batch(
        **_industry_document(codes),
        imported_by_user_id=7,
    )
    store = FactorResearchRunStore(tmp_path / "runs.db")
    body = FactorResearchBody(
        factor_id="momentum_20",
        pool_preset="custom",
        pool_custom_codes=codes,
        start="2024-01-29",
        end="2024-02-29",
        horizons=[1, 5],
        primary_horizon=5,
        quantiles=5,
        neutralization="industry+size",
    )
    result = asyncio.run(
        execute_factor_research(
            body,
            owner_user_id=7,
            cache=Cache(),  # type: ignore[arg-type]
            store=store,
            point_in_time_store=pit,
        )
    )

    assert result["request"]["neutralization"] == "industry+size"
    assert result["neutralization"]["status"] == "completed"
    assert result["neutralization"]["primary_factor"]["daily"]
    run = store.get(owner_user_id=7, run_id=result["run"]["run_id"])
    assert run is not None
    assert run["request_digest"] == result["run"]["request_digest"]
    assert run["dataset_digest"] == result["dataset"]["content_sha256"]

    changed_document = deepcopy(_industry_document(codes))
    changed_document["source"]["version"] = "2024q1-other"
    changed_document["source"]["content_sha256"] = "d" * 64
    other_pit = PointInTimeMasterStore(tmp_path / "pit-other.db")
    other_pit.import_batch(**changed_document, imported_by_user_id=7)
    other = asyncio.run(
        execute_factor_research(
            body,
            owner_user_id=7,
            cache=Cache(),  # type: ignore[arg-type]
            store=FactorResearchRunStore(tmp_path / "runs-other.db"),
            point_in_time_store=other_pit,
        )
    )
    assert other["dataset"]["content_sha256"] != result["dataset"]["content_sha256"]
