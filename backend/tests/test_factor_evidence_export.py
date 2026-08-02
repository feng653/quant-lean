from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from typing import Any
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api import factor_research
from backend.data.factor_research_runs import FactorResearchRunStore
from backend.dependencies import get_current_user


def _result() -> dict[str, Any]:
    return {
        "schema_version": "factor-research/v2",
        "factor": {
            "factor_id": "momentum_20",
            "name": "=unsafe formula",
            "parameters": {"window": 20},
        },
        "request": {
            "factor_id": "momentum_20",
            "primary_horizon": 5,
        },
        "dataset": {
            "cache_key": "csi300",
            "rows": 200,
            "codes": 300,
            "date_start": "2024-01-01",
            "date_end": "2024-12-31",
            "content_sha256": "a" * 64,
            "source_provenance": {
                "providers": ["provider-a"],
                "local_path": "/private/cache/data.parquet",
            },
        },
        "preprocessing": {
            "config": {"winsor_method": "mad"},
            "diagnostics": [],
        },
        "ic": {
            "5": {
                "summary": {
                    "rank_ic": {
                        "count": 2,
                        "mean": 0.1,
                        "std": 0.02,
                        "icir": 5.0,
                        "positive_ratio": 1.0,
                        "t_stat": 7.0,
                    }
                },
                "series": [
                    {
                        "date": "2024-01-02",
                        "pearson_ic": 0.08,
                        "rank_ic": 0.1,
                        "sample_count": 300,
                    }
                ],
            }
        },
        "decay": {
            "points": [
                {
                    "horizon": 5,
                    "rank_ic": {"mean": 0.1},
                    "pearson_ic": {"mean": 0.08},
                }
            ]
        },
        "quantile_returns": {
            "mean_group_returns": {"1": -0.01, "5": 0.02},
            "long_short": {"mean": 0.03},
            "monotonicity": 0.9,
        },
        "cost_capacity": {
            "turnover": 0.2,
            "estimated_cost": 0.001,
        },
        "implementation": {
            "status": "available",
            "cost_sensitivity": [
                {
                    "cost_bps": 10,
                    "mean_group_returns": {"1": -0.01, "5": 0.02},
                    "long_short": {"mean": 0.03},
                }
            ],
            "turnover": {
                "series": [
                    {
                        "date": "2024-01-02",
                        "group_turnover": {"1": 0.2},
                        "long_short_turnover": 0.3,
                    }
                ]
            },
            "capacity": {
                "daily": [
                    {
                        "date": "2024-01-02",
                        "status": "available",
                        "estimates": {"0.05": 1_000_000},
                    }
                ]
            },
        },
        "neutralization": {
            "schema_version": "factor-neutralization/v1",
            "mode": "industry",
            "status": "completed",
            "fit_window": "same_trading_date_only",
            "inputs": {
                "industry": {
                    "scope_id": "cninfo_008001",
                    "source_batches": [{"batch_digest": "d" * 64}],
                },
                "size": None,
            },
            "primary_factor": {
                "summary": {
                    "dates_total": 1,
                    "dates_neutralized": 1,
                    "dates_excluded": 0,
                    "coverage_ratio": 1.0,
                },
                "daily": [
                    {
                        "date": "2024-01-02",
                        "status": "ok",
                        "sample_count": 300,
                        "candidate_count": 300,
                        "coverage_ratio": 1.0,
                        "dropped_by_reason": {},
                        "before": {"r_squared": 0.2},
                        "after": {"r_squared": 0.0},
                    }
                ],
            },
            "factor_summaries": {},
        },
        "protocol_review": {
            "schema_version": "factor-research-protocol-review/v1",
            "protocol_id": "fproto_" + "1" * 32,
            "version": 1,
            "payload_digest": "e" * 64,
            "question": "因子是否有效？",
            "hypothesis": "RankIC 达标。",
            "passed": True,
            "read_only": True,
            "checks": [
                {
                    "metric": "rank_ic_mean",
                    "operator": ">=",
                    "threshold": 0.02,
                    "actual": 0.1,
                    "passed": True,
                }
            ],
            "export_rules": {
                "allow_strategy_export": True,
                "require_all_thresholds": True,
            },
        },
        "debug_traceback": "secret stack should not export",
        "limitations": ["结果只用于研究。"],
    }


@pytest.fixture
def export_app(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FactorResearchRunStore]:
    store = FactorResearchRunStore(tmp_path / "factor-runs.db")
    monkeypatch.setattr(factor_research, "_run_store", lambda: store)
    app = FastAPI()
    app.include_router(factor_research.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read"],
    }
    return TestClient(app), store


def _create_run(
    store: FactorResearchRunStore,
    *,
    owner: int = 7,
    source_job_uuid: str | None = None,
) -> dict[str, Any]:
    return store.create(
        owner_user_id=owner,
        factor_id="momentum_20",
        request={"factor_id": "momentum_20", "primary_horizon": 5},
        result=_result(),
        source_job_uuid=source_job_uuid,
    )


def test_json_export_is_owned_safe_archivable_and_reproducible(
    export_app,
) -> None:
    client, store = export_app
    run = _create_run(store)
    assert store.archive(owner_user_id=7, run_id=run["run_id"])

    response = client.get(
        f"/api/factor-research/runs/{run['run_id']}/export?format=json"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].endswith('.json"')
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["archived"] is True
    assert payload["run"]["request_digest"] == run["request_digest"]
    assert payload["run"]["result_digest"] == run["result_digest"]
    assert payload["run"]["run_digest"] == run["run_digest"]
    assert payload["dataset"]["source_provenance"]["local_path"] == (
        "[REDACTED_LOCAL_PATH]"
    )
    assert payload["analysis"]["debug_traceback"] == "[REDACTED_ERROR_DETAIL]"
    assert "secret stack" not in response.text
    assert "/private/cache" not in response.text

    manifest = payload.pop("reproducibility_manifest")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        manifest["export_evidence_sha256"]
    )
    manifest_hash = manifest.pop("manifest_sha256")
    canonical_manifest = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(canonical_manifest.encode()).hexdigest() == manifest_hash


def test_csv_zip_has_bounded_fixed_tables_and_formula_protection(
    export_app,
) -> None:
    client, store = export_app
    run = _create_run(store)

    response = client.get(
        f"/api/factor-research/runs/{run['run_id']}/export?format=csv"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"].endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "run.csv",
            "request.csv",
            "factor_definition.csv",
            "dataset_provenance.csv",
            "ic_summary.csv",
            "ic_series.csv",
            "decay.csv",
            "quantile_returns.csv",
            "cost_capacity.csv",
            "implementation_cost_sensitivity.csv",
            "implementation_turnover.csv",
            "implementation_capacity.csv",
            "neutralization_summary.csv",
            "neutralization_daily.csv",
            "neutralization_inputs.json",
            "protocol_review.csv",
            "protocol_thresholds.csv",
            "protocol_export_rules.json",
            "limitations.csv",
            "reproducibility_manifest.json",
        } <= names
        factor_csv = archive.read("factor_definition.csv").decode("utf-8-sig")
        assert "'=unsafe formula" in factor_csv
        assert "/private/cache" not in archive.read(
            "dataset_provenance.csv"
        ).decode("utf-8-sig")
        assert "rank_ic" in archive.read("ic_summary.csv").decode("utf-8-sig")
        assert "300" in archive.read("ic_series.csv").decode("utf-8-sig")
        assert "same_trading_date_only" in archive.read(
            "neutralization_summary.csv"
        ).decode("utf-8-sig")


def test_unknown_foreign_and_admin_foreign_runs_are_same_404(
    export_app,
) -> None:
    client, store = export_app
    foreign = _create_run(store, owner=8)
    missing = "frun_" + "f" * 32

    foreign_response = client.get(
        f"/api/factor-research/runs/{foreign['run_id']}/export"
    )
    missing_response = client.get(
        f"/api/factor-research/runs/{missing}/export"
    )
    assert foreign_response.status_code == missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()

    client.app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": True,
        "permissions": [],
    }
    admin_response = client.get(
        f"/api/factor-research/runs/{foreign['run_id']}/export"
    )
    assert admin_response.status_code == 404
    assert admin_response.json() == missing_response.json()


def test_running_job_and_tampered_evidence_fail_closed(export_app) -> None:
    client, store = export_app
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TABLE jobs (job_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO jobs (job_uuid, status) VALUES (?, 'running')",
            ("job-running",),
        )
    running = _create_run(store, source_job_uuid="job-running")
    response = client.get(
        f"/api/factor-research/runs/{running['run_id']}/export"
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "仅已完成因子研究运行可导出证据"

    completed = _create_run(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER factor_runs_immutable_update")
        connection.execute(
            """
            UPDATE factor_research_runs
            SET request_json = '{"factor_id":"short_reversal_5"}'
            WHERE run_id = ?
            """,
            (completed["run_id"],),
        )
    tampered = client.get(
        f"/api/factor-research/runs/{completed['run_id']}/export"
    )
    assert tampered.status_code == 409
    assert tampered.json()["detail"] == "因子研究证据完整性校验失败"


def test_v2_store_migrates_digests_before_enforcing_new_immutability(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-v2.db"
    request_json = json.dumps(
        {"factor_id": "momentum_20"},
        sort_keys=True,
        separators=(",", ":"),
    )
    result_json = json.dumps(
        _result(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result_digest = hashlib.sha256(result_json.encode()).hexdigest()
    run_id = "frun_" + "1" * 32
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE factor_research_runs (
                run_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                factor_id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                dataset_digest TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_job_uuid TEXT,
                archived_at TEXT
            );
            CREATE TRIGGER factor_runs_immutable_update
            BEFORE UPDATE OF
                owner_user_id, factor_id, request_json, result_json,
                dataset_digest, result_digest, schema_version, created_at,
                source_job_uuid
            ON factor_research_runs
            BEGIN
                SELECT RAISE(ABORT, 'factor research run is immutable');
            END;
            """
        )
        connection.execute(
            """
            INSERT INTO factor_research_runs (
                run_id, owner_user_id, factor_id, request_json, result_json,
                dataset_digest, result_digest, schema_version, created_at
            ) VALUES (?, 7, 'momentum_20', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                request_json,
                result_json,
                "a" * 64,
                result_digest,
                "factor-research-run/v2",
                "2026-07-31T00:00:00Z",
            ),
        )

    store = FactorResearchRunStore(path)
    migrated = store.get(owner_user_id=7, run_id=run_id)

    assert migrated is not None
    assert len(migrated["request_digest"]) == 64
    assert len(migrated["run_digest"]) == 64
    with sqlite3.connect(path) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="immutable",
    ):
        connection.execute(
            "UPDATE factor_research_runs SET run_digest = ? WHERE run_id = ?",
            ("b" * 64, run_id),
        )


def test_database_failure_returns_safe_service_unavailable(
    export_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store = export_app

    def unavailable_store():
        raise sqlite3.OperationalError("/private/db/path is unavailable")

    monkeypatch.setattr(factor_research, "_run_store", unavailable_store)
    response = client.get(
        f"/api/factor-research/runs/frun_{'f' * 32}/export"
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "因子研究证据数据库暂不可用"
    assert "/private/" not in response.text
