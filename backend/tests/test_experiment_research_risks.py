from __future__ import annotations

import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from backend.api import experiments as experiments_api
from backend.config import settings
from backend.data.market_quality import audit_market_data
from backend.dependencies import get_current_user
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    canonical_sha256,
)


def _quality() -> dict:
    frame = pd.DataFrame(
        {
            ("000001", "open"): [10.0],
            ("000001", "high"): [11.0],
            ("000001", "low"): [9.0],
            ("000001", "close"): [10.5],
            ("000001", "volume"): [1000.0],
        },
        index=pd.DatetimeIndex(["2024-12-31"], name="date"),
    )
    frame.columns = pd.MultiIndex.from_tuples(
        frame.columns,
        names=["code", "field"],
    )
    return audit_market_data(
        frame,
        test_end="2024-12-31",
        source="akshare",
        price_adjustment="qfq",
    ).to_dict()


def _manifest(experiment_id: int, *, point_in_time: bool) -> dict:
    risks = [] if point_in_time else [
        "non_point_in_time",
        "survivorship_bias",
    ]
    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "experiment": {
            "experiment_id": experiment_id,
            "strategy_id": "test_strategy",
        },
        "universe": {
            "pool_id": "custom" if point_in_time else "csi300",
            "point_in_time": point_in_time,
        },
        "market_data_quality": _quality(),
        "research_risk_warnings": risks,
    }


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
            INSERT INTO experiments
                (id, user_id, name, strategy_id, strategy_category, labels,
                 params, status, progress_pct, created_at)
            VALUES
                (1, 7, 'legacy', 'test_strategy', 'technical', '[]', '{}',
                 'completed', 100, '2024-01-01'),
                (2, 7, 'current pool', 'test_strategy', 'technical', '[]',
                 '{}', 'completed', 100, '2024-01-02'),
                (3, 9, 'foreign pit', 'test_strategy', 'technical', '[]',
                 '{}', 'completed', 100, '2024-01-03'),
                (4, 7, 'legacy manifest', 'test_strategy', 'technical', '[]',
                 '{}', 'completed', 100, '2024-01-04');
            """
        )
        for experiment_id, point_in_time, owner in (
            (2, False, 7),
            (3, True, 9),
            (4, False, 7),
        ):
            manifest = _manifest(
                experiment_id,
                point_in_time=point_in_time,
            )
            if experiment_id == 4:
                manifest.pop("market_data_quality")
            connection.execute(
                """
                INSERT INTO research_run_manifests
                    (experiment_id, user_id, schema_version, manifest_json,
                     manifest_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    owner,
                    RUN_MANIFEST_SCHEMA,
                    json.dumps(manifest, allow_nan=False, sort_keys=True),
                    canonical_sha256(manifest),
                ),
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


def test_list_exposes_conservative_legacy_and_current_pool_risks(client) -> None:
    response = client.get("/api/experiments/", params={"limit": 100})

    assert response.status_code == 200
    items = {
        item["id"]: item
        for item in response.json()["data"]["items"]
    }
    assert set(items) == {1, 2, 4}
    legacy = items[1]["research_risk_summary"]
    assert legacy["legacy_no_manifest"] is True
    assert legacy["invalid_market_data"] is True
    assert legacy["live_eligible"] is False

    current = items[2]["research_risk_summary"]
    assert current["manifest_integrity_valid"] is True
    assert current["non_point_in_time"] is True
    assert current["current_constituents"] is True
    assert current["survivorship_bias"] is True
    assert current["invalid_market_data"] is False
    assert current["live_eligible"] is False
    old_manifest = items[4]["research_risk_summary"]
    assert old_manifest["legacy"] is True
    assert old_manifest["no_manifest"] is False
    assert old_manifest["invalid_market_data"] is True


def test_detail_keeps_owner_boundary_and_never_returns_foreign_risk(client) -> None:
    owned = client.get("/api/experiments/2")
    hidden = client.get("/api/experiments/3")

    assert owned.status_code == 200
    assert owned.json()["data"]["research_risk_summary"][
        "current_constituents"
    ] is True
    assert hidden.status_code == 404
