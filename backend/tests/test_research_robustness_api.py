from __future__ import annotations
from backend.core.hashing import file_sha256

from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api import research as research_api
from backend.config import settings
from backend.dependencies import get_current_user
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    canonical_sha256,
)


INITIAL_CAPITAL = 1_000_000.0
TEST_START = "2024-01-02"
TEST_END = "2024-12-31"


def _returns(variant: int, periods: int = 40) -> list[float]:
    patterns = (
        [0.008, -0.003, 0.004, 0.001],
        [-0.002, 0.007, 0.003, -0.001],
        [0.004, 0.002, -0.004, 0.006],
    )
    pattern = patterns[variant % len(patterns)]
    return [
        pattern[position % len(pattern)]
        + 0.0002 * math.sin(position + variant)
        for position in range(periods)
    ]


def _dates(periods: int = 40) -> list[str]:
    baseline = date(2024, 1, 1)
    return [
        (baseline + timedelta(days=offset)).isoformat()
        for offset in range(periods + 1)
    ]


def _manifest(experiment_id: int, user_id: int = 7) -> dict:
    del user_id
    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "experiment": {
            "experiment_id": experiment_id,
            "strategy_id": "test_strategy",
            "mode": "batch",
        },
        "windows": {
            "test_start": TEST_START,
            "test_end": TEST_END,
        },
        "execution": {
            "initial_capital": INITIAL_CAPITAL,
        },
    }


def _insert_experiment(
    connection: sqlite3.Connection,
    experiment_id: int,
    *,
    user_id: int = 7,
    status: str = "completed",
    variant: int = 0,
    dates: list[str] | None = None,
    sharpe: float | None = None,
    source_experiment_id: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO experiments
            (id, user_id, strategy_id, status, test_start, test_end, params,
             source_experiment_id)
        VALUES (?, ?, 'test_strategy', ?, ?, ?, ?, ?)
        """,
        (
            experiment_id,
            user_id,
            status,
            TEST_START,
            TEST_END,
            json.dumps({"window": 10 + experiment_id}),
            source_experiment_id,
        ),
    )
    manifest = _manifest(experiment_id, user_id)
    connection.execute(
        """
        INSERT INTO research_run_manifests
            (experiment_id, user_id, schema_version, manifest_json,
             manifest_hash)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            experiment_id,
            user_id,
            RUN_MANIFEST_SCHEMA,
            json.dumps(manifest, allow_nan=False, sort_keys=True),
            canonical_sha256(manifest),
        ),
    )
    period_returns = _returns(variant)
    equity_dates = dates or _dates(len(period_returns))
    equity = INITIAL_CAPITAL
    rows = [(experiment_id, equity_dates[0], equity, None)]
    for equity_date, daily_return in zip(
        equity_dates[1:], period_returns
    ):
        equity *= 1.0 + daily_return
        rows.append((experiment_id, equity_date, equity, daily_return))
    connection.executemany(
        """
        INSERT INTO equity_curve
            (experiment_id, date, equity, daily_return)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )
    connection.execute(
        """
        INSERT INTO experiment_metrics (experiment_id, sharpe_ratio)
        VALUES (?, ?)
        """,
        (
            experiment_id,
            sharpe if sharpe is not None else 0.4 + 0.2 * variant,
        ),
    )


def _create_database(
    path: Path,
    *,
    with_sweep: bool = True,
    with_group: bool = False,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                strategy_id TEXT NOT NULL,
                status TEXT NOT NULL,
                test_start TEXT NOT NULL,
                test_end TEXT NOT NULL,
                params TEXT NOT NULL,
                source_experiment_id INTEGER
            );
            CREATE TABLE experiment_metrics (
                experiment_id INTEGER PRIMARY KEY,
                sharpe_ratio REAL
            );
            CREATE TABLE equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                equity REAL NOT NULL,
                daily_return REAL
            );
            CREATE TABLE research_run_manifests (
                experiment_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL
            );
            CREATE TABLE param_sweeps (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                strategy_id TEXT NOT NULL,
                total_experiments INTEGER,
                research_trust TEXT,
                selection_start TEXT,
                selection_end TEXT,
                promoted_experiment_id INTEGER,
                promotion_source_experiment_id INTEGER
            );
            CREATE TABLE sweep_experiments (
                sweep_id INTEGER NOT NULL,
                experiment_id INTEGER NOT NULL,
                param_combo TEXT NOT NULL,
                PRIMARY KEY (sweep_id, experiment_id)
            );
            CREATE TABLE research_experiment_groups (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                strategy_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE research_trials (
                id INTEGER PRIMARY KEY,
                group_id INTEGER NOT NULL,
                experiment_id INTEGER NOT NULL
            );
            """
        )
        for experiment_id in (1, 2, 3):
            _insert_experiment(
                connection,
                experiment_id,
                variant=experiment_id - 1,
                sharpe=0.2 * experiment_id,
            )
        _insert_experiment(
            connection,
            4,
            user_id=8,
            variant=1,
            sharpe=0.35,
        )
        if with_sweep:
            connection.execute(
                """
                INSERT INTO param_sweeps
                    (id, user_id, strategy_id, total_experiments,
                     research_trust, selection_start, selection_end)
                VALUES (
                    11, 7, 'test_strategy', 3, 'locked_test',
                    ?, ?
                )
                """,
                (TEST_START, TEST_END),
            )
            connection.executemany(
                """
                INSERT INTO sweep_experiments
                    (sweep_id, experiment_id, param_combo)
                VALUES (11, ?, ?)
                """,
                [
                    (experiment_id, json.dumps({"window": 10 + experiment_id}))
                    for experiment_id in (1, 2, 3)
                ],
            )
        if with_group:
            connection.execute(
                """
                INSERT INTO research_experiment_groups
                    (id, user_id, strategy_id, status)
                VALUES (21, 7, 'test_strategy', 'closed')
                """
            )
            connection.executemany(
                """
                INSERT INTO research_trials
                    (id, group_id, experiment_id)
                VALUES (?, 21, ?)
                """,
                [(31, 1), (32, 2), (33, 3)],
            )


@pytest.fixture
def robustness_client(tmp_path: Path, monkeypatch):
    database = tmp_path / "experiment.db"
    _create_database(database)
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["experiments:read"],
    }
    app = FastAPI()
    app.include_router(research_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, database, user


def _get(client: TestClient, experiment_id: int = 1, **params):
    return client.get(
        f"/api/research/experiments/{experiment_id}/robustness",
        params={
            "n_bootstrap": 100,
            "n_slices": 4,
            "max_combinations": 6,
            **params,
        },
    )


def test_owner_report_is_deterministic_read_only_and_explicitly_post_hoc(
    robustness_client,
) -> None:
    client, database, _ = robustness_client
    before = file_sha256(database)

    first = _get(client, seed=19)
    second = _get(client, seed=19)

    assert first.status_code == 200
    assert first.json() == second.json()
    assert file_sha256(database) == before
    report = first.json()["data"]
    assert report["analysis_role"] == "post_hoc_diagnostic"
    assert report["selection_eligible"] is False
    assert report["promotion_eligible"] is False
    assert report["evidence"]["external_market_data_queried"] is False
    assert report["evidence"]["report_persisted"] is False
    assert report["candidate_context"]["source"] == "parameter_sweep"
    assert report["candidate_context"]["candidate_count"] == 3
    assert report["diagnostics"]["block_bootstrap"]["status"] == "ok"
    assert report["diagnostics"]["probabilistic_sharpe_ratio"][
        "status"
    ] == "ok"
    assert report["diagnostics"]["deflated_sharpe_ratio"]["status"] == "ok"
    assert report["diagnostics"]["cscv_pbo"]["status"] == "ok"
    for name in (
        "parameter_stability",
        "cost_stress",
        "capacity",
        "multiple_testing",
    ):
        assert report["diagnostics"][name]["status"] == "unavailable"


def test_owner_boundary_admin_and_permission(
    robustness_client,
) -> None:
    client, _, user = robustness_client

    hidden = _get(client, 4)
    assert hidden.status_code == 403

    user["is_admin"] = True
    user["permissions"] = []
    admin = _get(client, 4)
    assert admin.status_code == 200
    assert admin.json()["data"]["candidate_context"]["candidate_count"] == 1

    user["is_admin"] = False
    denied = _get(client)
    assert denied.status_code == 403


def test_candidate_count_cannot_be_supplied_or_forged(
    robustness_client,
) -> None:
    client, _, _ = robustness_client

    forged = _get(client, candidate_count=999)

    assert forged.status_code == 422
    assert "candidate_count" in forged.text


def test_independent_experiment_has_one_candidate_and_no_fake_dsr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "independent.db"
    _create_database(database, with_sweep=False)
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    app = FastAPI()
    app.include_router(research_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["experiments:read"],
    }
    with TestClient(app) as client:
        response = _get(client)

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["candidate_context"]["source"] == "independent_experiment"
    assert report["candidate_context"]["candidate_count"] == 1
    dsr = report["diagnostics"]["deflated_sharpe_ratio"]
    assert dsr["status"] == "unavailable"
    assert dsr["reason_code"] == "independent_candidate_count_one"
    assert dsr["kernel_diagnostic"]["status"] == "degenerate_input"
    assert report["diagnostics"]["cscv_pbo"]["status"] == "unavailable"


def test_research_group_derives_completed_candidate_universe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "group.db"
    _create_database(database, with_sweep=False, with_group=True)
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    app = FastAPI()
    app.include_router(research_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["experiments:read"],
    }
    with TestClient(app) as client:
        response = _get(client)

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["candidate_context"]["source"] == "research_group"
    assert report["candidate_context"]["candidate_count"] == 3
    assert report["diagnostics"]["deflated_sharpe_ratio"]["status"] == "ok"
    assert report["diagnostics"]["cscv_pbo"]["reason_code"] == (
        "same_sweep_required"
    )


def test_promoted_locked_test_derives_candidates_from_parent_sweep(
    robustness_client,
) -> None:
    client, database, _ = robustness_client
    with sqlite3.connect(database) as connection:
        _insert_experiment(
            connection,
            5,
            variant=2,
            sharpe=0.9,
            source_experiment_id=1,
        )
        connection.execute(
            """
            UPDATE param_sweeps
            SET promoted_experiment_id=5,
                promotion_source_experiment_id=1
            WHERE id=11
            """
        )

    response = _get(client, 5)

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["candidate_context"]["source"] == "parameter_sweep"
    assert report["candidate_context"]["target_relation"] == (
        "promoted_locked_test"
    )
    assert report["candidate_context"]["candidate_count"] == 3
    assert report["diagnostics"]["deflated_sharpe_ratio"]["status"] == "ok"
    assert report["diagnostics"]["cscv_pbo"]["status"] == "ok"


def test_unaligned_sweep_dates_make_pbo_unavailable_without_imputation(
    robustness_client,
) -> None:
    client, database, _ = robustness_client
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM equity_curve WHERE experiment_id=2"
        )
        candidate_dates = _dates()
        candidate_dates.pop(20)
        candidate_dates.append("2024-02-11")
        equity = INITIAL_CAPITAL
        rows = [(2, candidate_dates[0], equity, None)]
        for equity_date, daily_return in zip(candidate_dates[1:], _returns(1)):
            equity *= 1 + daily_return
            rows.append((2, equity_date, equity, daily_return))
        connection.executemany(
            """
            INSERT INTO equity_curve
                (experiment_id, date, equity, daily_return)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )

    response = _get(client)

    assert response.status_code == 200
    pbo = response.json()["data"]["diagnostics"]["cscv_pbo"]
    assert pbo["status"] == "unavailable"
    assert pbo["reason_code"] == "sweep_return_dates_not_aligned"
    assert "no interpolation" in pbo["reason"].lower()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            """
            UPDATE experiments SET status='running' WHERE id=1
            """,
            "experiment_not_completed",
        ),
        (
            """
            DELETE FROM equity_curve
            WHERE experiment_id=1 AND id IN (
                SELECT id FROM equity_curve
                WHERE experiment_id=1 ORDER BY id DESC LIMIT 25
            )
            """,
            "insufficient_equity_samples",
        ),
        (
            """
            UPDATE equity_curve SET equity=0
            WHERE id=(
                SELECT id FROM equity_curve
                WHERE experiment_id=1 ORDER BY id LIMIT 1 OFFSET 2
            )
            """,
            "equity_value_invalid",
        ),
        (
            """
            UPDATE equity_curve SET daily_return=0
            WHERE id=(
                SELECT id FROM equity_curve
                WHERE experiment_id=1 ORDER BY id LIMIT 1
            )
            """,
            "initial_equity_point_missing",
        ),
        (
            """
            UPDATE equity_curve SET date='2024-01-02'
            WHERE id=(
                SELECT id FROM equity_curve
                WHERE experiment_id=1 ORDER BY id LIMIT 1 OFFSET 2
            )
            """,
            "equity_dates_duplicate",
        ),
        (
            """
            UPDATE equity_curve SET date='2023-12-31'
            WHERE id=(
                SELECT id FROM equity_curve
                WHERE experiment_id=1 ORDER BY id LIMIT 1 OFFSET 10
            )
            """,
            "equity_dates_not_monotonic",
        ),
        (
            """
            UPDATE equity_curve SET daily_return=0.5
            WHERE id=(
                SELECT id FROM equity_curve
                WHERE experiment_id=1 ORDER BY id LIMIT 1 OFFSET 3
            )
            """,
            "stored_return_mismatch",
        ),
        (
            """
            UPDATE equity_curve SET date='2024-1-04'
            WHERE id=(
                SELECT id FROM equity_curve
                WHERE experiment_id=1 ORDER BY id LIMIT 1 OFFSET 3
            )
            """,
            "equity_date_invalid",
        ),
    ],
)
def test_incomplete_or_corrupt_primary_equity_fails_closed(
    robustness_client,
    mutation: str,
    expected_code: str,
) -> None:
    client, database, _ = robustness_client
    with sqlite3.connect(database) as connection:
        connection.executescript(mutation)

    response = _get(client)

    assert response.status_code in {409, 422}
    assert response.json()["detail"]["code"] == expected_code


def test_nonfinite_equity_fails_closed(
    robustness_client,
) -> None:
    client, database, _ = robustness_client
    with sqlite3.connect(database) as connection:
        target_id = connection.execute(
            """
            SELECT id FROM equity_curve
            WHERE experiment_id=1 ORDER BY id LIMIT 1 OFFSET 2
            """
        ).fetchone()[0]
        connection.execute(
            "UPDATE equity_curve SET equity=? WHERE id=?",
            (float("inf"), target_id),
        )

    response = _get(client)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "equity_value_invalid"


def test_manifest_tampering_fails_closed(
    robustness_client,
) -> None:
    client, database, _ = robustness_client
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE research_run_manifests
            SET manifest_json='{}' WHERE experiment_id=1
            """
        )

    response = _get(client)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "run_manifest_integrity_failure"
    )


def test_missing_candidate_metric_makes_dsr_unavailable_not_under_counted(
    robustness_client,
) -> None:
    client, database, _ = robustness_client
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM experiment_metrics WHERE experiment_id=2"
        )

    response = _get(client)

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["candidate_context"]["candidate_count"] == 3
    dsr = report["diagnostics"]["deflated_sharpe_ratio"]
    assert dsr["status"] == "unavailable"
    assert dsr["reason_code"] == "candidate_metric_evidence_incomplete"
