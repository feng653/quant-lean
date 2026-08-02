from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api import strategy_correlation as correlation_api
from backend.config import settings
from backend.dependencies import get_current_user
from backend.services.research_manifest import RUN_MANIFEST_SCHEMA, canonical_sha256
from backend.services.strategy_correlation import analyze_strategy_correlations
from backend.strategies.registry import get_registry


def _experiment(experiment_id: int, name: str = "strategy") -> dict[str, object]:
    return {
        "id": experiment_id,
        "name": name,
        "strategy_id": f"strategy_{experiment_id}",
        "test_start": "2024-01-01",
        "test_end": "2024-12-31",
    }


def _equity(experiment_id: int, values: list[float]) -> list[dict[str, object]]:
    return [
        {
            "experiment_id": experiment_id,
            "date": f"2024-01-{index + 1:02d}",
            "equity": value,
        }
        for index, value in enumerate(values)
    ]


def test_pairwise_alignment_uses_equity_derived_returns() -> None:
    rows_1 = _equity(1, [100, 110, 99, 108.9, 87.12])
    rows_2 = _equity(2, [200, 220, 198, 217.8, 174.24])
    # Experiment 2 lacks the first return date. Pairwise overlap remains three.
    rows_2.pop(0)
    report = analyze_strategy_correlations(
        [_experiment(1), _experiment(2)],
        {1: rows_1, 2: rows_2},
        min_observations=3,
    )

    assert report["return_definition"] == "adjacent_persisted_equity_pct_change"
    assert report["matrix"]["overlap_counts"] == [[4, 3], [3, 3]]
    assert report["matrix"]["values"][0][1] == pytest.approx(1.0)
    assert report["pairs"][0]["overlap_start"] == "2024-01-03"
    assert report["pairs"][0]["classification"] == "near_duplicate"
    assert report["summary"]["high_correlation_pairs"] == 1


def test_spearman_handles_ties_and_constant_series_is_null() -> None:
    # Return sequences: [0, 1, 1, 2] and [0, 3, 3, 9] have identical tied ranks.
    tied_left = _equity(1, [1, 1, 2, 4, 12])
    tied_right = _equity(2, [1, 1, 4, 16, 160])
    constant = _equity(3, [100, 100, 100, 100, 100])
    report = analyze_strategy_correlations(
        [_experiment(1), _experiment(2), _experiment(3)],
        {1: tied_left, 2: tied_right, 3: constant},
        method="spearman",
        min_observations=4,
    )

    by_ids = {
        (pair["left_experiment_id"], pair["right_experiment_id"]): pair
        for pair in report["pairs"]
    }
    assert by_ids[(1, 2)]["correlation"] == pytest.approx(1.0)
    assert by_ids[(1, 3)]["correlation"] is None
    assert by_ids[(1, 3)]["unavailable_reason"] == "constant_series"
    assert report["matrix"]["values"][2][2] is None


def test_insufficient_overlap_and_bad_points_are_disclosed() -> None:
    left = _equity(1, [100, 101, 102])
    left.append({"date": "2024-01-03", "equity": 103})
    left.append({"date": "2024-01-04", "equity": "not-a-number"})
    report = analyze_strategy_correlations(
        [_experiment(1), _experiment(2)],
        {1: left, 2: _equity(2, [100, 99, 98])},
        min_observations=3,
    )

    assert report["pairs"][0]["correlation"] is None
    assert report["pairs"][0]["unavailable_reason"] == "insufficient_overlap"
    assert {warning["code"] for warning in report["warnings"]} >= {
        "insufficient_history",
        "duplicate_equity_dates",
        "invalid_equity_data",
    }


def test_pairwise_alignment_excludes_different_return_intervals() -> None:
    left = _equity(1, [100, 101, 102, 103, 104, 105])
    right = _equity(2, [100, 101, 102, 103, 104, 105])
    # Removing an internal observation means the Jan-04 return spans Jan-02 to
    # Jan-04 for experiment 2, but only Jan-03 to Jan-04 for experiment 1.
    right = [row for row in right if row["date"] != "2024-01-03"]
    report = analyze_strategy_correlations(
        [_experiment(1), _experiment(2)],
        {1: left, 2: right},
        min_observations=3,
    )
    pair = report["pairs"][0]
    assert pair["interval_mismatch_exclusions"] == 1
    assert pair["overlap"] == 3


def test_holdings_tail_and_marginal_contributions_are_read_only() -> None:
    left = _equity(1, [100 + index + (index % 3) for index in range(20)])
    right = _equity(2, [100 + index * 0.8 - (index % 4) for index in range(20)])
    trades = {
        1: [
            {"date": "2024-01-01", "code": "000001", "action": "BUY", "shares": 100},
            {"date": "2024-01-01", "code": "000002", "action": "BUY", "shares": 100},
        ],
        2: [
            {"date": "2024-01-01", "code": "000002", "action": "BUY", "shares": 100},
            {"date": "2024-01-01", "code": "000003", "action": "BUY", "shares": 100},
        ],
    }
    report = analyze_strategy_correlations(
        [_experiment(1), _experiment(2)],
        {1: left, 2: right},
        trade_rows=trades,
        weights=[0.7, 0.3],
        min_observations=10,
        tail_fraction=0.25,
    )
    pair = report["pairs"][0]
    assert pair["holding_overlap"]["mean"] == pytest.approx(1 / 3)
    assert pair["tail_correlation"]["observations"] >= 5
    assert report["portfolio_contribution"]["available"] is True
    assert [
        item["weight"]
        for item in report["portfolio_contribution"]["contributions"]
    ] == pytest.approx([0.7, 0.3])
    assert report["automation"]["mutates_portfolio"] is False
    assert all(
        item["action"] == "review_only"
        for item in report["constraint_suggestions"]
    )


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_category TEXT NOT NULL,
                requires_training INTEGER NOT NULL,
                params TEXT NOT NULL,
                status TEXT NOT NULL,
                pool_preset TEXT,
                test_start TEXT NOT NULL,
                test_end TEXT NOT NULL
            );
            CREATE TABLE equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                equity REAL
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
            INSERT INTO experiments
                (id, user_id, name, strategy_id, strategy_category,
                 requires_training, params, status, pool_preset,
                 test_start, test_end)
            VALUES (?, ?, ?, ?, 'technical', 0, '{}', ?, 'csi300',
                    '2024-01-01', '2024-12-31')
            """,
            [
                (1, 7, "owner one", "ma_cross_v1", "completed"),
                (2, 7, "owner two", "macd_signal_v1", "completed"),
                (3, 8, "other user", "rsi_reversal_v1", "completed"),
                (4, 7, "still running", "bollinger_breakout_v1", "running"),
            ],
        )
        strategy_ids = {
            1: "ma_cross_v1",
            2: "macd_signal_v1",
            3: "rsi_reversal_v1",
            4: "bollinger_breakout_v1",
        }
        users = {1: 7, 2: 7, 3: 8, 4: 7}
        for experiment_id, strategy_id in strategy_ids.items():
            binding_id = "bind_" + f"{experiment_id:x}" * 32
            binding_digest = f"{experiment_id:x}" * 64
            timeline_hash = f"{experiment_id + 4:x}" * 64
            manifest = {
                "schema_version": RUN_MANIFEST_SCHEMA,
                "experiment": {
                    "experiment_id": experiment_id,
                    "strategy_id": strategy_id,
                    "data_access_policy": "cache_only",
                },
                "parameters": {
                    "canonical": {},
                    "sha256": canonical_sha256({}),
                },
                "universe": {
                    "point_in_time": True,
                    "timeline_identity": {"timeline_hash": timeline_hash},
                },
                "execution": {
                    "canonical_price_binding": {
                        "binding_id": binding_id,
                        "binding_digest": binding_digest,
                    }
                },
                "pit_runtime": {
                    "schema_version": "pit-runtime-binding/v1",
                    "verified": True,
                    "network_accessed": False,
                    "legacy_or_static_fallback_allowed": False,
                    "timeline_hash": timeline_hash,
                    "canonical_price_binding_id": binding_id,
                    "canonical_price_binding_digest": binding_digest,
                },
            }
            connection.execute(
                """
                INSERT INTO research_run_manifests
                    (experiment_id, user_id, schema_version, manifest_json,
                     manifest_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    users[experiment_id],
                    RUN_MANIFEST_SCHEMA,
                    json.dumps(manifest, sort_keys=True),
                    canonical_sha256(manifest),
                ),
            )
        for experiment_id, multiplier in ((1, 1.0), (2, 2.0), (3, -1.0), (4, 0.5)):
            equity = 100.0
            values = []
            for day in range(15):
                equity *= 1 + multiplier * (0.001 + (day % 3) * 0.0005)
                values.append(
                    (
                        experiment_id,
                        f"2024-01-{day + 1:02d}",
                        equity,
                    )
                )
            connection.executemany(
                "INSERT INTO equity_curve (experiment_id, date, equity) VALUES (?, ?, ?)",
                values,
            )


@pytest.fixture
def correlation_client(tmp_path: Path, monkeypatch):
    database = tmp_path / "experiment.db"
    _create_database(database)
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(database))
    user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["experiments:read"],
    }
    app = FastAPI()
    app.include_router(correlation_api.router)
    registry = get_registry()
    if not registry.list_all():
        registry.scan_directory(Path(__file__).resolve().parents[1] / "strategies")
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, user


def test_api_enforces_owner_and_returns_overlap_matrix(correlation_client) -> None:
    client, _ = correlation_client
    database = settings.abs_path(settings.EXPERIMENT_DB)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    response = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "2"),
            ("experiment_ids", "1"),
            ("method", "pearson"),
            ("min_observations", "10"),
        ],
    )
    assert response.status_code == 200
    report = response.json()["data"]
    assert report["matrix"]["experiment_ids"] == [2, 1]
    assert report["matrix"]["overlap_counts"][0][1] == 14
    assert report["experiments"][0]["name"] == "owner two"
    assert report["pit_evidence"]["verified"] is True
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    inaccessible = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "1"),
            ("experiment_ids", "3"),
            ("min_observations", "10"),
        ],
    )
    assert inaccessible.status_code == 404
    assert "无权访问" in inaccessible.json()["detail"]


def test_api_admin_can_compare_all_users_and_rejects_incomplete(
    correlation_client,
) -> None:
    client, user = correlation_client
    user["is_admin"] = True
    response = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "1"),
            ("experiment_ids", "3"),
            ("method", "spearman"),
            ("min_observations", "10"),
        ],
    )
    assert response.status_code == 200
    assert response.json()["data"]["method"] == "spearman"

    incomplete = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "1"),
            ("experiment_ids", "4"),
            ("min_observations", "10"),
        ],
    )
    assert incomplete.status_code == 422
    assert "未完成" in incomplete.json()["detail"]


def test_api_requires_permission_and_unique_ids(correlation_client) -> None:
    client, user = correlation_client
    user["permissions"] = []
    forbidden = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "1"),
            ("experiment_ids", "2"),
            ("min_observations", "10"),
        ],
    )
    assert forbidden.status_code == 403

    user["permissions"] = ["experiments:read"]
    duplicate = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "1"),
            ("experiment_ids", "1"),
            ("min_observations", "10"),
        ],
    )
    assert duplicate.status_code == 422


def test_api_unexpected_failure_is_stable_and_redacted(
    correlation_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = correlation_client
    secret = "/Users/private/experiment.db"

    def fail_connect(*_args, **_kwargs):
        raise RuntimeError(f"unable to open {secret}")

    monkeypatch.setattr(correlation_api.aiosqlite, "connect", fail_connect)
    response = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "1"),
            ("experiment_ids", "2"),
            ("min_observations", "10"),
        ],
    )
    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "strategy_correlation_failed",
        "message": "策略相关性分析暂不可用。",
    }
    assert secret not in response.text


def test_api_rejects_non_pit_completed_experiment(correlation_client) -> None:
    client, _ = correlation_client
    with sqlite3.connect(settings.abs_path(settings.EXPERIMENT_DB)) as connection:
        connection.execute(
            "DELETE FROM research_run_manifests WHERE experiment_id=2"
        )
    response = client.get(
        "/api/research/strategy-correlation",
        params=[
            ("experiment_ids", "1"),
            ("experiment_ids", "2"),
            ("min_observations", "10"),
        ],
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == (
        "pit_source_manifest_missing_or_invalid"
    )


def test_candidate_api_returns_five_manifest_bound_non_publishing_drafts(
    correlation_client,
) -> None:
    client, user = correlation_client
    user["is_admin"] = True
    database = settings.abs_path(settings.EXPERIMENT_DB)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    request = {
        "experiment_ids": [1, 2, 3],
        "min_observations": 10,
        "max_components": 3,
    }
    response = client.post(
        "/api/research/strategy-correlation/portfolio-candidates",
        json=request,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["schema_version"] == "portfolio-candidate-set/v1"
    assert result["candidate_count"] == 5
    assert result["automation"] == {
        "mutates_strategy_registry": False,
        "mutates_portfolio": False,
        "submits_experiment": False,
    }
    assert len({item["candidate_id"] for item in result["candidates"]}) == 5
    for candidate in result["candidates"]:
        assert candidate["strategy_id"] == "composite_research_weighted_v1"
        assert candidate["publication"]["automatic"] is False
        specs = json.loads(candidate["params"]["component_specs"])
        weights = json.loads(candidate["params"]["static_weights"])
        assert len(specs) == 3
        assert all(len(item["source_manifest_hash"]) == 64 for item in specs)
        assert math.fsum(weights) == pytest.approx(1.0)
        assert max(weights) <= 0.4 + 1e-12
    repeated = client.post(
        "/api/research/strategy-correlation/portfolio-candidates",
        json=request,
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"] == result
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
