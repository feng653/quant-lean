from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api import experiments as experiments_api
from backend.config import settings
from backend.dependencies import get_current_user
from backend.services.research_evidence_export import (
    RESEARCH_EVIDENCE_SCHEMA,
    csv_safe_cell,
)
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    canonical_sha256,
)


def _manifest(experiment_id: int) -> dict:
    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "experiment": {
            "experiment_id": experiment_id,
            "strategy_id": "ma_cross_v1",
        },
        "dataset": {
            "digest": "dataset-sha256",
            "source": "akshare",
        },
        "universe": {
            "pool_id": "custom",
            "point_in_time": True,
            "quality": {"is_clean": True},
        },
        "market_data_quality": {
            "schema_version": "market-data-quality/v1",
            "row_count": 2,
            "instrument_count": 1,
            "start": "2024-01-02",
            "end": "2024-01-03",
            "source": "akshare",
            "price_adjustment": "qfq",
            "warnings": [],
            "fatal": [],
            "is_clean": True,
        },
        "benchmark": {"code": "000300"},
        "environment": {"git": {"dirty": False}},
        "execution": {"initial_capital": 1_000_000},
        "research_risk_warnings": [],
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
                pool_preset TEXT,
                pool_custom_codes TEXT,
                pool_industries TEXT,
                train_start TEXT,
                train_end TEXT,
                test_start TEXT,
                test_end TEXT,
                params TEXT,
                params_hash TEXT,
                mode TEXT,
                requires_training INTEGER,
                data_version TEXT,
                code_version TEXT,
                status TEXT,
                error_log TEXT,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                source_experiment_id INTEGER
            );
            CREATE TABLE experiment_metrics (
                id INTEGER PRIMARY KEY,
                experiment_id INTEGER UNIQUE,
                sharpe_ratio REAL,
                annual_return REAL,
                max_drawdown REAL,
                total_trades INTEGER,
                created_at TEXT
            );
            CREATE TABLE equity_curve (
                id INTEGER PRIMARY KEY,
                experiment_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                equity REAL,
                benchmark REAL,
                daily_return REAL,
                drawdown REAL
            );
            CREATE TABLE trade_log (
                id INTEGER PRIMARY KEY,
                experiment_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                signal_date TEXT,
                code TEXT,
                action TEXT,
                price REAL,
                shares INTEGER,
                amount REAL,
                cost REAL,
                signal_strategy TEXT,
                signal_score REAL
            );
            CREATE TABLE research_run_manifests (
                experiment_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE research_artifact_manifests (
                id INTEGER PRIMARY KEY,
                experiment_id INTEGER NOT NULL,
                artifact_kind TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                artifact_size INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        experiments = (
            (
                1,
                7,
                "=formula name",
                "completed",
                '{"fast": 10, "api_token": "secret",'
                ' "local": "/Users/research/private.csv"}',
            ),
            (2, 9, "foreign", "completed", "{}"),
            (3, 7, "running", "running", "{}"),
            (4, 7, "legacy", "completed", "{}"),
            (5, 7, "bad manifest", "completed", "{}"),
        )
        for experiment_id, owner, name, status, params in experiments:
            connection.execute(
                """
                INSERT INTO experiments
                    (id, user_id, name, strategy_id, strategy_category,
                     pool_preset, pool_custom_codes, pool_industries,
                     test_start, test_end, params, params_hash, mode,
                     requires_training, data_version, code_version, status,
                     error_log, created_at, started_at, completed_at)
                VALUES (?, ?, ?, 'ma_cross_v1', 'technical', 'custom',
                        '["000001"]', '[]', '2024-01-02', '2024-01-03',
                        ?, 'params-hash', 'batch', 0, 'data-v1', 'code-v1',
                        ?, 'Bearer should never export',
                        '2026-07-30 01:02:03',
                        '2026-07-30T01:02:04+00:00',
                        '2026-07-30T01:02:05Z')
                """,
                (experiment_id, owner, name, params, status),
            )
        connection.execute(
            """
            INSERT INTO experiment_metrics
                (id, experiment_id, sharpe_ratio, annual_return,
                 max_drawdown, total_trades, created_at)
            VALUES (1, 1, 1.25, 0.12, -0.08, 1, '2026-07-30')
            """
        )
        connection.executemany(
            """
            INSERT INTO equity_curve
                (id, experiment_id, date, equity, benchmark,
                 daily_return, drawdown)
            VALUES (?, 1, ?, ?, ?, ?, ?)
            """,
            (
                (1, "2024-01-02", 1_000_000, 1_000_000, None, 0),
                (2, "2024-01-03", 1_010_000, 1_005_000, 0.01, -0.01),
            ),
        )
        connection.execute(
            """
            INSERT INTO trade_log
                (id, experiment_id, date, signal_date, code, action,
                 price, shares, amount, cost, signal_strategy, signal_score)
            VALUES (1, 1, '2024-01-03', '2024-01-02', '=CMD()', 'BUY',
                    -12.5, 100, 1250, 5, '@malicious', 0.8)
            """
        )
        for experiment_id, owner in ((1, 7), (2, 9)):
            manifest = _manifest(experiment_id)
            connection.execute(
                """
                INSERT INTO research_run_manifests
                    (experiment_id, user_id, schema_version, manifest_json,
                     manifest_hash, created_at)
                VALUES (?, ?, ?, ?, ?, '2026-07-30 01:02:04')
                """,
                (
                    experiment_id,
                    owner,
                    RUN_MANIFEST_SCHEMA,
                    json.dumps(manifest, sort_keys=True),
                    canonical_sha256(manifest),
                ),
            )
        bad_manifest = _manifest(5)
        connection.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (5, 7, ?, ?, 'wrong-hash', '2026-07-30')
            """,
            (RUN_MANIFEST_SCHEMA, json.dumps(bad_manifest)),
        )
        connection.execute(
            """
            INSERT INTO research_artifact_manifests
                (id, experiment_id, artifact_kind, artifact_sha256,
                 artifact_size, metadata_json, created_at)
            VALUES (1, 1, 'model', 'artifact-sha256', 128,
                    '{"fold": 1}', '2026-07-30 01:02:05')
            """
        )


@pytest.fixture
def app_client(tmp_path, monkeypatch):
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
    with TestClient(app) as client:
        yield client, user


def test_json_export_contains_complete_safe_utc_evidence(app_client) -> None:
    client, _ = app_client

    response = client.get("/api/experiments/1/export?format=json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="research-evidence-experiment-1-'
    )
    payload = response.json()
    assert payload["schema_version"] == RESEARCH_EVIDENCE_SCHEMA
    assert payload["generated_at"].endswith("Z")
    assert payload["experiment"]["created_at"] == "2026-07-30T01:02:03Z"
    assert payload["experiment"]["started_at"] == "2026-07-30T01:02:04Z"
    assert payload["metrics"]["sharpe_ratio"] == 1.25
    assert len(payload["equity_curve"]) == 2
    assert payload["trades"][0]["code"] == "=CMD()"
    assert (
        canonical_sha256(payload["research_manifest"]["manifest"])
        == payload["research_manifest"]["manifest_hash"]
    )
    assert payload["research_manifest"]["created_at"].endswith("Z")
    assert payload["data_lineage"]["dataset"]["digest"] == "dataset-sha256"
    assert payload["risk_summary"]["manifest_integrity_valid"] is True
    assert payload["evidence_completeness"] == {
        "metrics_present": True,
        "immutable_manifest_present": True,
        "equity_points": 2,
        "trades": 1,
    }
    encoded = response.content.decode("utf-8")
    assert "Bearer should never export" not in encoded
    assert "secret" not in encoded
    assert "/Users/research/private.csv" not in encoded
    assert payload["experiment"]["params"]["api_token"] == "[REDACTED]"
    assert (
        payload["experiment"]["params"]["local"]
        == "[REDACTED_LOCAL_PATH]"
    )


def test_csv_zip_is_structured_and_neutralizes_formula_injection(
    app_client,
) -> None:
    client, _ = app_client

    response = client.get("/api/experiments/1/export?format=csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"].endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "metadata.csv",
            "experiment.csv",
            "metrics.csv",
            "risk_summary.csv",
            "evidence_completeness.csv",
            "research_manifest.json",
            "data_lineage.json",
            "equity_curve.csv",
            "trades.csv",
        }
        experiment_rows = list(
            csv.reader(
                io.TextIOWrapper(
                    archive.open("experiment.csv"),
                    encoding="utf-8-sig",
                )
            )
        )
        name_index = experiment_rows[0].index("name")
        assert experiment_rows[1][name_index] == "'=formula name"
        trade_rows = list(
            csv.reader(
                io.TextIOWrapper(
                    archive.open("trades.csv"),
                    encoding="utf-8-sig",
                )
            )
        )
        code_index = trade_rows[0].index("code")
        strategy_index = trade_rows[0].index("signal_strategy")
        price_index = trade_rows[0].index("price")
        assert trade_rows[1][code_index] == "'=CMD()"
        assert trade_rows[1][strategy_index] == "'@malicious"
        assert trade_rows[1][price_index] == "-12.5"


def test_owner_boundary_hides_foreign_and_unknown_experiments(
    app_client,
) -> None:
    client, _ = app_client

    assert client.get("/api/experiments/2/export").status_code == 404
    assert client.get("/api/experiments/999/export").status_code == 404


def test_admin_can_export_foreign_experiment(app_client) -> None:
    client, user = app_client
    user["is_admin"] = True

    response = client.get("/api/experiments/2/export")

    assert response.status_code == 200
    assert response.json()["experiment"]["id"] == 2


def test_requires_read_permission(app_client) -> None:
    client, user = app_client
    user["permissions"] = []

    response = client.get("/api/experiments/1/export")

    assert response.status_code == 403


def test_rejects_non_completed_and_corrupt_manifest(app_client) -> None:
    client, _ = app_client

    running = client.get("/api/experiments/3/export")
    corrupt = client.get("/api/experiments/5/export")

    assert running.status_code == 409
    assert running.json()["detail"]["code"] == "experiment_not_completed"
    assert corrupt.status_code == 409
    assert corrupt.json()["detail"]["code"] == "manifest_integrity_failure"


def test_legacy_export_is_explicitly_incomplete(app_client) -> None:
    client, _ = app_client

    response = client.get("/api/experiments/4/export")

    assert response.status_code == 200
    payload = response.json()
    assert payload["research_manifest"] is None
    assert payload["risk_summary"]["legacy_no_manifest"] is True
    assert "immutable_manifest_missing" in payload["risk_summary"][
        "evidence_warnings"
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("+SUM(A1)", "'+SUM(A1)"),
        ("-2+3", "'-2+3"),
        ("@cmd", "'@cmd"),
        (" \t=cmd", "' \t=cmd"),
        (-12.5, -12.5),
        ("000001", "000001"),
    ],
)
def test_csv_safe_cell(value, expected) -> None:
    assert csv_safe_cell(value) == expected
