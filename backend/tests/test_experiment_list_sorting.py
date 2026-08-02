from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import experiments as experiments_api
from backend.config import settings
from backend.dependencies import get_current_user


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = tmp_path / "experiment.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT,
                strategy_id TEXT NOT NULL,
                strategy_category TEXT,
                is_starred INTEGER DEFAULT 0,
                labels TEXT,
                params TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE experiment_metrics (
                experiment_id INTEGER PRIMARY KEY,
                sharpe_ratio REAL,
                annual_return REAL,
                max_drawdown REAL,
                win_rate REAL
            );
            CREATE TABLE research_run_manifests (
                experiment_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO experiments (
                id, user_id, name, strategy_id, strategy_category,
                labels, params, status, created_at
            )
            VALUES (?, ?, ?, ?, 'technical', '[]', '{}', ?, ?)
            """,
            [
                (1, 7, "Alpha", "zeta", "completed", "2026-07-01"),
                (2, 7, "Beta", "alpha", "failed", "2026-07-02"),
                (3, 7, "Gamma", "beta", "running", "2026-07-03"),
                (4, 7, "Delta", "alpha", "pending", "2026-07-04"),
                (5, 7, "Epsilon", "omega", "cancelled", "2026-07-04"),
                (6, 8, "Other user", "aardvark", "completed", "2026-07-05"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO experiment_metrics (
                experiment_id, sharpe_ratio, annual_return, max_drawdown,
                win_rate
            )
            VALUES (?, ?, ?, ?, 0.5)
            """,
            [
                (1, 1.0, 0.10, -0.20),
                (2, 0.5, 0.20, -0.10),
                (3, 2.0, None, None),
                (4, 0.5, 0.20, -0.30),
                (5, None, -0.05, -0.05),
                (6, 99.0, 99.0, 0.0),
            ],
        )

    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    app = FastAPI()
    app.include_router(experiments_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["experiments:read"],
    }
    with TestClient(app) as test_client:
        yield test_client


def _ids(response) -> list[int]:
    assert response.status_code == 200
    return [item["id"] for item in response.json()["data"]["items"]]


def test_default_sort_preserves_newest_first_with_stable_id_tie_break(
    client,
) -> None:
    response = client.get("/api/experiments/", params={"limit": 100})

    assert _ids(response) == [5, 4, 3, 2, 1]
    assert response.json()["data"]["sort_by"] == "created_at"
    assert response.json()["data"]["sort_order"] == "desc"


@pytest.mark.parametrize(
    ("sort_by", "sort_order", "expected_ids"),
    [
        ("annual_return", "desc", [4, 2, 1, 5, 3]),
        ("annual_return", "asc", [5, 1, 2, 4, 3]),
        ("sharpe_ratio", "desc", [3, 1, 4, 2, 5]),
        ("max_drawdown", "desc", [5, 2, 1, 4, 3]),
        ("strategy_id", "asc", [2, 4, 3, 5, 1]),
        ("status", "asc", [5, 1, 2, 4, 3]),
    ],
)
def test_supported_sort_keys_apply_before_pagination(
    client,
    sort_by: str,
    sort_order: str,
    expected_ids: list[int],
) -> None:
    response = client.get(
        "/api/experiments/",
        params={
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": 100,
        },
    )

    assert _ids(response) == expected_ids
    assert response.json()["data"]["sort_by"] == sort_by
    assert response.json()["data"]["sort_order"] == sort_order


def test_full_filtered_dataset_is_sorted_before_pages_are_sliced(client) -> None:
    first_page = client.get(
        "/api/experiments/",
        params={
            "strategy_category": "technical",
            "sort_by": "annual_return",
            "sort_order": "desc",
            "page": 1,
            "limit": 2,
        },
    )
    second_page = client.get(
        "/api/experiments/",
        params={
            "strategy_category": "technical",
            "sort_by": "annual_return",
            "sort_order": "desc",
            "page": 2,
            "limit": 2,
        },
    )

    assert _ids(first_page) == [4, 2]
    assert _ids(second_page) == [1, 5]
    assert first_page.json()["data"]["total"] == 5


def test_sorting_preserves_existing_status_and_search_filters(client) -> None:
    response = client.get(
        "/api/experiments/",
        params={
            "status": "completed",
            "search": "Alpha",
            "sort_by": "sharpe_ratio",
            "sort_order": "asc",
        },
    )

    assert _ids(response) == [1]
    assert response.json()["data"]["total"] == 1


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("sort_by", "created_at DESC; DROP TABLE experiments; --"),
        ("sort_order", "desc; DROP TABLE experiments; --"),
    ],
)
def test_sort_parameters_are_strictly_whitelisted(
    client,
    parameter: str,
    value: str,
) -> None:
    response = client.get("/api/experiments/", params={parameter: value})

    assert response.status_code == 422
    assert _ids(client.get("/api/experiments/", params={"limit": 100})) == [
        5,
        4,
        3,
        2,
        1,
    ]
