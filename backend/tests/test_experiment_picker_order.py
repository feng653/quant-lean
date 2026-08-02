from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import experiments as experiments_api
from backend.config import settings
from backend.dependencies import get_current_user
from backend.services.experiment_eligibility import ExperimentEligibility


@pytest.fixture
def picker_client(tmp_path, monkeypatch):
    database = tmp_path / "experiment.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT,
                strategy_id TEXT NOT NULL,
                is_starred INTEGER DEFAULT 0,
                labels TEXT,
                params TEXT,
                test_start TEXT,
                test_end TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE experiment_metrics (
                experiment_id INTEGER PRIMARY KEY,
                sharpe_ratio REAL,
                annual_return REAL,
                max_drawdown REAL,
                win_rate REAL,
                total_trades INTEGER
            );
            CREATE TABLE research_run_manifests (
                experiment_id INTEGER PRIMARY KEY,
                schema_version TEXT,
                manifest_json TEXT,
                manifest_hash TEXT
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO experiments
                (id, user_id, name, strategy_id, labels, params, status,
                 created_at)
            VALUES (?, 7, ?, 'ma_cross_v1', '[]', '{}', 'completed',
                    '2026-07-30')
            """,
            [(1, "one"), (2, "two"), (3, "three"), (4, "four")],
        )
        connection.executemany(
            """
            INSERT INTO experiment_metrics
                (experiment_id, sharpe_ratio, annual_return, max_drawdown)
            VALUES (?, ?, ?, ?)
            """,
            [
                (1, None, None, -0.40),
                (2, 1.2, 0.10, -0.10),
                (3, 1.2, 0.20, 0.00),
                (4, None, None, -0.20),
            ],
        )
        connection.executemany(
            """
            INSERT INTO research_run_manifests
                (experiment_id, schema_version, manifest_json, manifest_hash)
            VALUES (?, 'fixture', '{}', 'fixture')
            """,
            [(1,), (2,), (3,), (4,)],
        )

    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    monkeypatch.setattr(
        experiments_api,
        "assess_experiment_eligibility",
        lambda **_kwargs: ExperimentEligibility(
            True, "pit_manifest_verified"
        ),
    )
    app = FastAPI()
    app.include_router(experiments_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["experiments:read"],
    }
    with TestClient(app) as client:
        yield client


def test_picker_sharpe_orders_null_last_and_breaks_ties_by_id(
    picker_client,
) -> None:
    response = picker_client.get(
        "/api/experiments/picker",
        params={"sort": "sharpe", "limit": 100},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [3, 2, 4, 1]


def test_picker_drawdown_prefers_values_closest_to_zero(
    picker_client,
) -> None:
    response = picker_client.get(
        "/api/experiments/picker",
        params={"sort": "drawdown", "limit": 100},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [3, 2, 4, 1]


def test_picker_silently_excludes_legacy_candidates(
    picker_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        experiments_api,
        "assess_experiment_eligibility",
        lambda **kwargs: ExperimentEligibility(
            kwargs["experiment_id"] != 3,
            (
                "pit_manifest_verified"
                if kwargs["experiment_id"] != 3
                else "legacy_manifest_missing"
            ),
        ),
    )
    response = picker_client.get(
        "/api/experiments/picker",
        params={"sort": "sharpe", "limit": 100},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [2, 4, 1]
