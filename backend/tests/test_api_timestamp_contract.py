from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import strategies as strategies_api
from backend.api.schemas import ExperimentResponse, ParameterPresetResponse
from backend.api.timestamps import serialize_utc_timestamp
from backend.config import settings
from backend.dependencies import get_current_user
from backend.services.experiment_eligibility import ExperimentEligibility


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-30 00:00:00", "2026-07-30T00:00:00Z"),
        ("2026-07-30T00:00:00Z", "2026-07-30T00:00:00Z"),
        ("2026-07-30T08:00:00+08:00", "2026-07-30T00:00:00Z"),
        ("2026-07-30", "2026-07-30T00:00:00Z"),
        (
            datetime(
                2026,
                7,
                30,
                8,
                tzinfo=timezone(timedelta(hours=8)),
            ),
            "2026-07-30T00:00:00Z",
        ),
    ],
)
def test_serialize_utc_timestamp_preserves_instants(value, expected) -> None:
    assert serialize_utc_timestamp(value) == expected


def test_invalid_timestamp_is_rejected_at_api_boundary() -> None:
    with pytest.raises(ValueError, match="invalid timestamp"):
        serialize_utc_timestamp("not-a-timestamp")


def test_experiment_response_serializes_all_lifecycle_timestamps() -> None:
    response = ExperimentResponse(
        id=1,
        strategy_id="ma_cross_v1",
        created_at="2026-07-30 00:00:00",
        started_at="2026-07-30T08:00:01+08:00",
        completed_at="2026-07-30T00:00:02Z",
    )

    assert response.model_dump()["created_at"] == "2026-07-30T00:00:00Z"
    assert response.model_dump()["started_at"] == "2026-07-30T00:00:01Z"
    assert response.model_dump()["completed_at"] == "2026-07-30T00:00:02Z"


def test_parameter_preset_response_uses_same_timestamp_contract() -> None:
    response = ParameterPresetResponse(
        id=1,
        user_id=7,
        name="基准参数",
        strategy_id="ma_cross_v1",
        created_at="2026-07-30 00:00:00",
        updated_at="2026-07-30 01:02:03",
    )

    assert response.created_at == "2026-07-30T00:00:00Z"
    assert response.updated_at == "2026-07-30T01:02:03Z"


def test_strategy_experiment_api_emits_rfc3339_utc(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "experiment.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                name TEXT,
                strategy_id TEXT,
                is_starred INTEGER,
                labels TEXT,
                train_start TEXT,
                train_end TEXT,
                test_start TEXT,
                test_end TEXT,
                params TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE experiment_metrics (
                experiment_id INTEGER PRIMARY KEY,
                sharpe_ratio REAL,
                annual_return REAL,
                max_drawdown REAL
            );
            CREATE TABLE research_run_manifests (
                experiment_id INTEGER PRIMARY KEY,
                schema_version TEXT,
                manifest_json TEXT,
                manifest_hash TEXT
            );
            INSERT INTO experiments
                (id, name, strategy_id, is_starred, labels, params, status,
                 created_at)
            VALUES
                (1, 'MA baseline', 'ma_cross_v1', 1, '[]', '{}',
                 'completed', '2026-07-30 00:00:00');
            INSERT INTO experiment_metrics
                (experiment_id, sharpe_ratio, annual_return, max_drawdown)
            VALUES (1, 1.2, 0.1, -0.2);
            INSERT INTO research_run_manifests
                (experiment_id, schema_version, manifest_json, manifest_hash)
            VALUES (1, 'fixture', '{}', 'fixture');
            """
        )

    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    monkeypatch.setattr(
        strategies_api,
        "assess_experiment_eligibility",
        lambda **_kwargs: ExperimentEligibility(
            True, "pit_manifest_verified"
        ),
    )
    app = FastAPI()
    app.include_router(strategies_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["strategies:read"],
    }

    with TestClient(app) as client:
        response = client.get(
            "/api/strategies/ma_cross_v1/best-experiments"
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["created_at"] == (
        "2026-07-30T00:00:00Z"
    )
