from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import experiments as experiments_api
from backend.config import settings
from backend.dependencies import get_current_user


def _create_database(path) -> None:
    with sqlite3.connect(path) as connection:
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
                pool_preset TEXT,
                pool_custom_codes TEXT,
                pool_industries TEXT,
                train_start TEXT,
                train_end TEXT,
                test_start TEXT,
                test_end TEXT,
                params TEXT,
                mode TEXT,
                status TEXT,
                progress_pct REAL,
                progress_message TEXT,
                error_log TEXT,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                source_experiment_id INTEGER
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
        rows = [
            (1, 7, "ML LGB", "ml_lgb", "ml", "2026-07-21"),
            (2, 7, "Value", "factor_value", "factor"),
            (3, 7, "MA", "technical_ma", "technical"),
            (4, 8, "Other user's LSTM", "ml_lstm", "ml"),
            (5, 7, "Composite", "composite_equal", "composite"),
            (6, 7, "ML XGB", "ml_xgb", "ml"),
            (
                7,
                7,
                "Delisted portfolio strategy",
                "removed_portfolio_strategy",
                "portfolio",
            ),
            (
                8,
                7,
                "Archived ML strategy",
                "removed_ml_strategy",
                "ml",
            ),
            (
                9,
                7,
                "Older ID same timestamp",
                "technical_old",
                "technical",
                "2026-07-30",
            ),
            (
                10,
                7,
                "Newer ID same timestamp",
                "technical_new",
                "technical",
                "2026-07-30",
            ),
        ]
        normalized_rows = [
            (*row[:5], row[5] if len(row) == 6 else f"2026-07-{20 + row[0]:02d}")
            for row in rows
        ]
        connection.executemany(
            """
            INSERT INTO experiments
                (id, user_id, name, strategy_id, strategy_category,
                 labels, params, status, progress_pct, created_at)
            VALUES (?, ?, ?, ?, ?, '[]', '{}', 'completed', 100, ?)
            """,
            normalized_rows,
        )


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = tmp_path / "experiment.db"
    _create_database(database)
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["experiments:read"],
    }

    app = FastAPI()
    app.include_router(experiments_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as test_client:
        yield test_client


def test_ml_category_shares_count_with_pagination(client) -> None:
    response = client.get(
        "/api/experiments/",
        params={"strategy_category": "ml", "limit": 1},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["total"] == 3
    assert len(payload["items"]) == 1
    assert payload["items"][0]["strategy_id"] == "removed_ml_strategy"


@pytest.mark.parametrize(
    ("category", "expected_strategy"),
    [
        ("factor", "factor_value"),
        ("composite", "composite_equal"),
        ("portfolio", "removed_portfolio_strategy"),
    ],
)
def test_other_persisted_categories_are_filtered(
    client,
    category: str,
    expected_strategy: str,
) -> None:
    response = client.get(
        "/api/experiments/",
        params={"strategy_category": category},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["total"] == 1
    assert [item["strategy_id"] for item in payload["items"]] == [
        expected_strategy
    ]


def test_all_five_persisted_categories_are_supported(client) -> None:
    expected = {
        "technical": 3,
        "ml": 3,
        "factor": 1,
        "portfolio": 1,
        "composite": 1,
    }
    for category, total in expected.items():
        response = client.get(
            "/api/experiments/",
            params={"strategy_category": category, "limit": 100},
        )
        assert response.status_code == 200
        assert response.json()["data"]["total"] == total


def test_delisted_strategy_remains_visible_from_persisted_snapshot(client) -> None:
    response = client.get(
        "/api/experiments/",
        params={"strategy_category": "portfolio"},
    )

    assert response.status_code == 200
    assert [
        item["strategy_id"] for item in response.json()["data"]["items"]
    ] == ["removed_portfolio_strategy"]


def test_category_and_strategy_id_conflict_returns_empty(client) -> None:
    response = client.get(
        "/api/experiments/",
        params={
            "strategy_category": "ml",
            "strategy_id": "factor_value",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["total"] == 0
    assert response.json()["data"]["items"] == []


def test_non_admin_category_filter_keeps_user_boundary(client) -> None:
    response = client.get(
        "/api/experiments/",
        params={"strategy_category": "ml", "limit": 100},
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert {item["user_id"] for item in items} == {7}
    assert "ml_lstm" not in {item["strategy_id"] for item in items}


def test_same_created_at_is_stably_sorted_by_descending_id(client) -> None:
    response = client.get(
        "/api/experiments/",
        params={"strategy_category": "technical", "limit": 100},
    )

    assert response.status_code == 200
    strategy_ids = [
        item["strategy_id"] for item in response.json()["data"]["items"]
    ]
    assert strategy_ids[:2] == ["technical_new", "technical_old"]
    assert {
        item["created_at"] for item in response.json()["data"]["items"]
    } == {
        "2026-07-30T00:00:00Z",
        "2026-07-23T00:00:00Z",
    }


def test_invalid_category_is_rejected_by_fastapi(client) -> None:
    response = client.get(
        "/api/experiments/",
        params={"strategy_category": "ml') OR 1=1 --"},
    )

    assert response.status_code == 422
