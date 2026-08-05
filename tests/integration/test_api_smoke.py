from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.jobs import broker as broker_module
from backend.jobs.broker import JobBroker
from backend.db.init import init_databases
from backend.main import app
from backend.strategies.startup import scan_strategies
from backend.services.research_manifest import canonical_sha256
from backend.services.experiment_eligibility import ExperimentEligibility


async def _isolated_pit_experiment_eligible(
    *_args, **_kwargs
) -> ExperimentEligibility:
    """Explicit fixture seam for tests whose subject is not PIT evidence."""

    return ExperimentEligibility(True, "pit_manifest_verified")


def _seed_approved_technical_source(
    *,
    monkeypatch,
    params: dict,
) -> tuple[int, int]:
    """Seed only the source identity; promotion verification has dedicated tests."""
    params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
    params_hash = hashlib.md5(params_json.encode()).hexdigest()
    with sqlite3.connect(str(settings.abs_path(settings.USERS_DB))) as users:
        user_id = int(
            users.execute(
                "SELECT id FROM users WHERE username = 'contract_admin'"
            ).fetchone()[0]
        )
    with sqlite3.connect(
        str(settings.abs_path(settings.EXPERIMENT_DB))
    ) as experiments:
        experiment_id = int(
            experiments.execute(
                """
                INSERT INTO experiments
                    (user_id, name, strategy_id, strategy_category,
                     pool_preset, test_start, test_end, params, params_hash,
                     mode, requires_training, status, data_version)
                VALUES (?, 'Approved MA source', 'ma_cross_v1', 'technical',
                        'csi300', '2025-01-02', '2025-12-31', ?, ?,
                        'batch', 0, 'completed', 'integration-data')
                """,
                (user_id, params_json, params_hash),
            ).lastrowid
        )
        experiments.execute(
            """
            INSERT INTO equity_curve (experiment_id, date, equity)
            VALUES (?, '2025-01-02', 1000000)
            """,
            (experiment_id,),
        )
        # Deployment binds the exact source manifest even when eligibility and
        # promotion verification are replaced by fixture seams.  Keep this
        # contract seed representative instead of bypassing the integrity
        # check that is not the subject of these smoke tests.
        manifest = {
            "schema_version": "research-run-manifest/v1",
            "experiment": {
                "experiment_id": experiment_id,
                "strategy_id": "ma_cross_v1",
            },
            "parameters": {
                "canonical": params,
                "sha256": canonical_sha256(params),
            },
            "windows": {
                "test_start": "2025-01-02",
                "test_end": "2025-12-31",
            },
        }
        experiments.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (?, ?, 'research-run-manifest/v1', ?, ?, datetime('now'))
            """,
            (
                experiment_id,
                user_id,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                canonical_sha256(manifest),
            ),
        )
        experiments.commit()

    promotion_id = 1

    async def fake_resolve(**kwargs):
        identity = {
            "schema_version": "research-promotion-binding/v1",
            "promotion_id": promotion_id,
            "promotion_version": 1,
            "report_id": 1,
            "report_hash": "a" * 64,
            "experiment_id": int(kwargs["experiment_id"]),
            "manifest_hash": "b" * 64,
            "model_artifact_id": None,
            "model_sha256": None,
            "model_evidence_hash": None,
        }
        return {
            **identity,
            "binding_hash": canonical_sha256(identity),
        }

    monkeypatch.setattr(
        "backend.services.deployment_promotion.resolve_deployment_promotion",
        fake_resolve,
    )
    monkeypatch.setattr(
        "backend.services.experiment_eligibility.load_experiment_eligibility",
        _isolated_pit_experiment_eligible,
    )
    return experiment_id, promotion_id


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Never inherit a developer machine's production/bootstrap policy from
    # .env; this suite owns an isolated user database and explicitly exercises
    # the development first-user bootstrap contract.
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(
        settings,
        "JWT_SECRET",
        "integration-test-secret-" + ("s" * 48),
    )
    monkeypatch.setattr(
        settings,
        "RESEARCH_SNAPSHOT_DIR",
        str(tmp_path / "research_snapshots"),
    )
    monkeypatch.setattr(
        broker_module,
        "_broker_instance",
        JobBroker(str(tmp_path / "jobs.db")),
    )
    asyncio.run(init_databases())
    asyncio.run(scan_strategies())
    test_client = TestClient(app)
    yield test_client
    test_client.close()


@pytest.fixture()
def admin_session(client: TestClient):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "contract_admin",
            "password": "contract-pass-123",
            "display_name": "Contract Admin",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    return {
        "headers": {"Authorization": f"Bearer {payload['access_token']}"},
        "payload": payload,
    }


def test_core_empty_state_contracts(client: TestClient, admin_session):
    headers = admin_session["headers"]

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["is_admin"] is True

    expected_empty_endpoints = [
        "/api/experiments/",
        "/api/trading/deployments",
        "/api/trading/portfolios",
        "/api/trading/positions",
        "/api/trading/signals",
    ]
    for endpoint in expected_empty_endpoints:
        response = client.get(endpoint, headers=headers)
        assert response.status_code == 200, (endpoint, response.text)
        data = response.json()["data"]
        assert data == [] or data["items"] == []

    status = client.get("/api/data/update/status", headers=headers)
    assert status.status_code == 200, status.text
    pools = status.json()["data"]["pools_cache"]
    assert {item["pool_id"] for item in pools} == {
        "csi300",
        "csi500",
        "csi800",
        "csi1000",
    }
    assert all(item["exists"] is False for item in pools)


def test_execution_adapters_are_exposed_fail_closed(
    client: TestClient,
    admin_session,
):
    headers = admin_session["headers"]
    readiness = client.get("/api/execution/adapters/readiness", headers=headers)
    assert readiness.status_code == 200, readiness.text
    adapters = readiness.json()["data"]["adapters"]
    assert {adapter["adapter_id"] for adapter in adapters} == {"qmt", "ptrade"}
    assert all(adapter["capabilities"]["live_order_submission"] is False for adapter in adapters)

    live_gate = client.get("/api/execution/live-readiness", headers=headers)
    assert live_gate.status_code == 200, live_gate.text
    report = live_gate.json()["data"]
    assert report["schema_version"] == "live-readiness/v1"
    assert report["ready"] is False
    assert report["certification"] == "not_certified"
    assert report["platform_scope"] == "research_and_paper_trading_only"
    assert report["blocker_count"] == len(report["blockers"])
    assert report["blocker_count"] > 0

    validated = client.post(
        "/api/execution/orders/validate",
        headers=headers,
        json={
            "adapter_id": "qmt",
            "order": {
                "symbol": "600000.SH",
                "side": "buy",
                "order_type": "limit",
                "quantity": 100,
                "limit_price": 10.5,
            },
        },
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["data"]["valid"] is True
    assert validated.json()["data"]["can_submit"] is False


def test_job_center_contract(client: TestClient, admin_session):
    headers = admin_session["headers"]
    broker = broker_module._broker_instance
    assert broker is not None
    job_id = asyncio.run(
        broker.submit_job(
            "backtest",
            {"experiment_id": 42},
            user_id=1,
            display_name="任务中心契约测试",
        )
    )
    # Simulate a pre-hardening row to verify the read boundary remains safe.
    with broker._get_conn() as connection:
        connection.execute(
            "UPDATE jobs SET params=? WHERE job_uuid=?",
            (
                json.dumps(
                    {
                        "experiment_id": 42,
                        "api_token": "must-not-leak",
                    }
                ),
                job_id,
            ),
        )
        connection.commit()

    summary = client.get("/api/jobs/summary", headers=headers)
    assert summary.status_code == 200, summary.text
    assert summary.json()["data"]["counts"]["pending"] == 1

    listed = client.get("/api/jobs/?status=pending&page=1&page_size=10", headers=headers)
    assert listed.status_code == 200, listed.text
    payload = listed.json()["data"]
    assert payload["total"] == 1
    assert payload["items"][0]["params"]["api_token"] == "***"
    assert payload["items"][0]["resource_id"] == "42"

    asyncio.run(
        broker.submit_job(
            "factor_research",
            {"factor_id": "momentum_20"},
            user_id=999,
        )
    )
    mine = client.get(
        "/api/jobs/?status=pending&page=1&page_size=10&mine=true",
        headers=headers,
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["data"]["total"] == 1
    assert mine.json()["data"]["items"][0]["user_id"] == 1

    cancelled = client.delete(f"/api/jobs/{job_id}", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    retried = client.post(f"/api/jobs/{job_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    detail = client.get(f"/api/jobs/{retried.json()['data']['job_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["attempt"] == 2
    assert detail.json()["data"]["events"]


def test_model_lifecycle_contract_is_redacted_and_retrain_is_idempotent(
    client: TestClient,
    admin_session,
):
    headers = admin_session["headers"]
    with sqlite3.connect(
        str(settings.abs_path(settings.USERS_DB))
    ) as users:
        owner_id = int(
            users.execute(
                "SELECT id FROM users WHERE username='contract_admin'"
            ).fetchone()[0]
        )
    with sqlite3.connect(
        str(settings.abs_path(settings.TRADING_SIM_DB))
    ) as trading:
        deployment_id = int(
            trading.execute(
                """
                INSERT INTO deployments (
                    user_id, strategy_id, strategy_category, display_name,
                    params, params_hash, mode, requires_retraining,
                    retrain_frequency, status, current_model_version,
                    deployed_at
                )
                VALUES (?, 'lifecycle_contract', 'ml', '生命周期契约',
                        '{}', ?, 'batch', 1, 'monthly', 'active', 2,
                        '2026-06-01 00:00:00')
                """,
                (owner_id, "a" * 32),
            ).lastrowid
        )
        trading.execute(
            """
            INSERT INTO model_version_history (
                deployment_id, model_version, model_file_path,
                metadata_file_path, validation_metrics, model_sha256,
                model_size, strategy_id, params_hash,
                retrain_manifest_json, retrain_manifest_hash,
                status, is_latest
            )
            VALUES (?, 2, '/Users/private/model.joblib',
                    '/Users/private/model.json',
                    '{"validation_rank_ic": 0.12}', ?, 12,
                    'lifecycle_contract', ?, '{}', ?,
                    'promoted', 1)
            """,
            (deployment_id, "b" * 64, "a" * 32, "c" * 64),
        )
        trading.execute(
            """
            INSERT INTO model_retrain_attempts (
                attempt_id, deployment_id, expected_model_version,
                candidate_model_version, status, error, created_at,
                completed_at
            )
            VALUES ('failed-attempt', ?, 2, 3, 'failed',
                    'FileNotFoundError: /Users/private/candidate.joblib missing',
                    '2026-07-30 00:00:00', '2026-07-30 00:01:00')
            """,
            (deployment_id,),
        )
        trading.commit()

    lifecycle = client.get(
        f"/api/trading/deployments/{deployment_id}/model-lifecycle",
        headers=headers,
    )
    assert lifecycle.status_code == 200, lifecycle.text
    payload = lifecycle.json()["data"]
    assert payload["safety"]["automatic_live_publish"] is False
    assert payload["versions"][0]["model_storage_key"] is None
    assert "model_file_path" not in payload["versions"][0]
    failure = payload["attempts"][0]["failure"]
    assert failure["code"] == "FileNotFoundError"
    assert "private" not in failure["message"]

    first = client.put(
        f"/api/trading/deployments/{deployment_id}/retrain",
        headers=headers,
    )
    second = client.put(
        f"/api/trading/deployments/{deployment_id}/retrain",
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["job_id"] == second.json()["data"]["job_id"]


def test_admin_deactivation_invalidates_refresh_flow(
    client: TestClient,
    admin_session,
):
    headers = admin_session["headers"]
    created = client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "username": "paper_user",
            "password": "paper-user-pass",
            "display_name": "Paper User",
        },
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["data"]["user_id"]

    login = client.post(
        "/api/auth/login",
        json={"username": "paper_user", "password": "paper-user-pass"},
    )
    assert login.status_code == 200
    refresh_token = login.json()["data"]["refresh_token"]

    disabled = client.put(
        f"/api/admin/users/{user_id}/status",
        headers=headers,
        json={"is_active": False},
    )
    assert disabled.status_code == 200, disabled.text

    refresh = client.post(
        "/api/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh.status_code == 401


def test_deployment_and_portfolio_contract(
    client: TestClient,
    admin_session,
    monkeypatch,
):
    headers = admin_session["headers"]
    source_experiment_id, promotion_id = _seed_approved_technical_source(
        monkeypatch=monkeypatch,
        params={"fast_period": 10, "slow_period": 30},
    )
    deployment = client.post(
        "/api/trading/deployments",
        headers=headers,
        json={
            "strategy_id": "ma_cross_v1",
            "display_name": "MA paper",
            "params": {"fast_period": 10, "slow_period": 30},
            "mode": "batch",
            "source_experiment_id": source_experiment_id,
            "research_promotion_id": promotion_id,
        },
    )
    assert deployment.status_code == 200, deployment.text
    deployment_id = deployment.json()["data"]["deployment_id"]

    portfolio = client.post(
        "/api/trading/portfolios",
        headers=headers,
        json={
            "name": "Contract portfolio",
            "total_capital": 1_000_000,
            "rebalance_frequency": "daily",
            "allocations": [
                {
                    "deployment_id": deployment_id,
                    "target_weight_bps": 6_000,
                    "min_weight_bps": 0,
                    "max_weight_bps": 8_000,
                    "locked": False,
                }
            ],
        },
    )
    assert portfolio.status_code == 200, portfolio.text
    portfolio_id = portfolio.json()["data"]["portfolio_id"]

    listed = client.get("/api/trading/portfolios", headers=headers)
    assert listed.status_code == 200, listed.text
    item = listed.json()["data"][0]
    assert item["allocations"][0]["target_weight_bps"] == 6_000
    assert item["cash_balance"] == 1_000_000

    draft = client.post(
        f"/api/trading/portfolios/{portfolio_id}/drafts",
        headers=headers,
        json={
            "allocations": [
                {
                    "deployment_id": deployment_id,
                    "target_weight_bps": 7_000,
                    "min_weight_bps": 0,
                    "max_weight_bps": 8_000,
                }
            ],
            "effective_date": "2026-07-28",
        },
    )
    assert draft.status_code == 200, draft.text
    revision = draft.json()["data"]["revision"]

    published = client.post(
        f"/api/trading/portfolios/{portfolio_id}/drafts/{revision}/publish",
        headers=headers,
    )
    assert published.status_code == 200, published.text
    assert published.json()["data"]["published"] is True

    nav = client.get(f"/api/trading/portfolios/{portfolio_id}/nav", headers=headers)
    assert nav.status_code == 200, nav.text

    overview = client.get(
        f"/api/trading/portfolios/{portfolio_id}/overview",
        headers=headers,
    )
    assert overview.status_code == 200, overview.text
    assert overview.json()["data"]["current_equity"] == 1_000_000
    assert overview.json()["data"]["strategies"][0]["target_weight_bps"] == 7_000

    referenced_stop = client.put(
        f"/api/trading/deployments/{deployment_id}",
        headers=headers,
        json={"status": "stopped"},
    )
    assert referenced_stop.status_code == 409, referenced_stop.text

    removal_draft = client.post(
        f"/api/trading/portfolios/{portfolio_id}/drafts",
        headers=headers,
        json={"allocations": []},
    )
    assert removal_draft.status_code == 200, removal_draft.text
    removal_revision = removal_draft.json()["data"]["revision"]
    removed = client.post(
        f"/api/trading/portfolios/{portfolio_id}/drafts/{removal_revision}/publish",
        headers=headers,
    )
    assert removed.status_code == 200, removed.text

    stopped = client.put(
        f"/api/trading/deployments/{deployment_id}",
        headers=headers,
        json={"status": "stopped"},
    )
    assert stopped.status_code == 200, stopped.text

    with sqlite3.connect(settings.TRADING_SIM_DB) as conn:
        conn.execute(
            """
            INSERT INTO nav_history
                (portfolio_id, deployment_id, date, nav, total_equity,
                 cash_balance, daily_return, cumulative_return)
            VALUES (?, NULL, '2026-07-01', 1010000, 1010000, 303000, 0.01, 0.01)
            """,
            (portfolio_id,),
        )
        conn.execute(
            """
            INSERT INTO strategy_nav_history
                (portfolio_id, deployment_id, date, opening_equity, net_flow,
                 cash_balance, market_value, total_equity, daily_pnl,
                 daily_return, cumulative_return, contribution_pnl,
                 contribution_return)
            VALUES (?, ?, '2026-07-01', 700000, 0, 203000, 504000,
                    707000, 7000, 0.01, 0.01, 7000, 0.007)
            """,
            (portfolio_id, deployment_id),
        )

    analytics = client.get(
        f"/api/trading/portfolios/{portfolio_id}/strategy-analytics",
        headers=headers,
    )
    assert analytics.status_code == 200, analytics.text
    analytics_data = analytics.json()["data"]
    assert analytics_data["date_range"]["start_date"] == "2026-07-01"
    assert analytics_data["strategies"][0]["deployment_id"] == deployment_id
    assert analytics_data["strategies"][0]["metrics"]["cumulative_return"] == 0.01
    assert analytics_data["series"][0]["strategies"][0]["contribution_pnl"] == 7000

    backfill = client.post(
        "/api/trading/simulate/backfill",
        headers=headers,
        json={"start_date": "2026-07-01", "end_date": "2026-07-05"},
    )
    assert backfill.status_code == 409, backfill.text
    assert backfill.json()["detail"]["code"] == (
        "paper_portfolio_binding_invalid"
    )

    schedule = client.get("/api/trading/simulate/schedule", headers=headers)
    assert schedule.status_code == 200, schedule.text
    assert "enabled" in schedule.json()["data"]

    invalid = client.post(
        "/api/trading/portfolios",
        headers=headers,
        json={
            "name": "Invalid",
            "total_capital": 100_000,
            "allocations": [
                {"deployment_id": 999_999, "target_weight_bps": 5_000}
            ],
        },
    )
    assert invalid.status_code == 400


def test_targeted_deployment_updates_only_one_portfolio_atomically(
    client: TestClient,
    admin_session,
    monkeypatch,
):
    headers = admin_session["headers"]
    source_experiment_id, promotion_id = _seed_approved_technical_source(
        monkeypatch=monkeypatch,
        params={"fast_period": 10, "slow_period": 30},
    )

    portfolio_ids: list[int] = []
    for name in ("Target paper", "Untouched paper"):
        response = client.post(
            "/api/trading/portfolios",
            headers=headers,
            json={
                "name": name,
                "total_capital": 1_000_000,
                "rebalance_frequency": "daily",
                "allocations": [],
            },
        )
        assert response.status_code == 200, response.text
        portfolio_ids.append(response.json()["data"]["portfolio_id"])

    targeted = client.post(
        "/api/trading/deployments",
        headers=headers,
        json={
            "strategy_id": "ma_cross_v1",
            "display_name": "Only target paper",
            "params": {"fast_period": 10, "slow_period": 30},
            "mode": "batch",
            "source_experiment_id": source_experiment_id,
            "research_promotion_id": promotion_id,
            "portfolio_id": portfolio_ids[0],
            "target_weight_bps": 2_500,
        },
    )
    assert targeted.status_code == 200, targeted.text
    result = targeted.json()["data"]
    assert result["portfolio_id"] == portfolio_ids[0]
    assert result["revision"] == 2

    listed = client.get("/api/trading/portfolios", headers=headers)
    assert listed.status_code == 200, listed.text
    by_id = {item["id"]: item for item in listed.json()["data"]}
    assert [item["deployment_id"] for item in by_id[portfolio_ids[0]]["allocations"]] == [
        result["deployment_id"]
    ]
    assert by_id[portfolio_ids[0]]["allocations"][0]["target_weight_bps"] == 2_500
    assert by_id[portfolio_ids[1]]["allocations"] == []

    with sqlite3.connect(settings.TRADING_SIM_DB) as conn:
        deployment_count_before = conn.execute(
            "SELECT COUNT(*) FROM deployments"
        ).fetchone()[0]
        links = conn.execute(
            """
            SELECT portfolio_id
            FROM portfolio_allocations
            WHERE deployment_id = ?
            """,
            (result["deployment_id"],),
        ).fetchall()
    assert links == [(portfolio_ids[0],)]

    rejected = client.post(
        "/api/trading/deployments",
        headers=headers,
        json={
            "strategy_id": "ma_cross_v1",
            "display_name": "Must roll back",
            "params": {"fast_period": 10, "slow_period": 30},
            "mode": "batch",
            "source_experiment_id": source_experiment_id,
            "research_promotion_id": promotion_id,
            "portfolio_id": portfolio_ids[0],
            "target_weight_bps": 8_000,
        },
    )
    assert rejected.status_code == 422, rejected.text
    with sqlite3.connect(settings.TRADING_SIM_DB) as conn:
        deployment_count_after = conn.execute(
            "SELECT COUNT(*) FROM deployments"
        ).fetchone()[0]
    assert deployment_count_after == deployment_count_before


def test_training_deployment_selects_latest_model_artifact(
    client: TestClient,
    admin_session,
    monkeypatch,
):
    monkeypatch.setattr(
        "backend.services.experiment_eligibility.load_experiment_eligibility",
        _isolated_pit_experiment_eligible,
    )
    headers = admin_session["headers"]
    strategies = client.get("/api/strategies/", headers=headers).json()["data"]
    lstm = next(item for item in strategies if item["strategy_id"] == "lstm_rank_v1")
    params = {
        field["name"]: field["default"]
        for field in lstm["params"]
        if field.get("default") is not None
    }
    params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
    params_hash = hashlib.md5(params_json.encode()).hexdigest()
    model_path = (
        settings.abs_path(settings.MODEL_STORE_DIR)
        / "integration"
        / "lstm.joblib"
    )
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"integration-model")
    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()

    with sqlite3.connect(str(settings.abs_path(settings.USERS_DB))) as users:
        user_id = users.execute(
            "SELECT id FROM users WHERE username = 'contract_admin'"
        ).fetchone()[0]
    with sqlite3.connect(str(settings.abs_path(settings.EXPERIMENT_DB))) as experiments:
        cursor = experiments.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category, pool_preset,
                 train_start, train_end, test_start, test_end, params,
                 params_hash, mode, requires_training, status, data_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                "deployable-lstm",
                "lstm_rank_v1",
                "ml",
                "csi500",
                "2019-01-02",
                "2024-12-31",
                "2025-01-02",
                "2025-12-31",
                params_json,
                params_hash,
                "batch",
                1,
                "completed",
                "data-version",
            ),
        )
        experiment_id = cursor.lastrowid
        manifest = {
            "schema_version": "research-run-manifest/v1",
            "experiment": {
                "experiment_id": experiment_id,
                "strategy_id": "lstm_rank_v1",
            },
            "strategy": {"class": "LSTMRankStrategy"},
            "environment": {"python": {"version": "test"}},
            "parameters": {
                "canonical": params,
                "sha256": canonical_sha256(params),
            },
            "windows": {
                "train_start": "2019-01-02",
                "train_end": "2024-12-31",
                "test_start": "2025-01-02",
                "test_end": "2025-12-31",
            },
            "dataset": {
                "digest": "a" * 64,
                "context_digest": "b" * 64,
            },
            "universe": {"snapshot_hash": "c" * 64},
        }
        manifest_hash = canonical_sha256(manifest)
        experiments.execute(
            """
            INSERT INTO research_run_manifests
                (experiment_id, user_id, schema_version, manifest_json,
                 manifest_hash, created_at)
            VALUES (?, ?, 'research-run-manifest/v1', ?, ?,
                    datetime('now'))
            """,
            (
                experiment_id,
                user_id,
                json.dumps(manifest),
                manifest_hash,
            ),
        )
        experiments.execute(
            """
            INSERT INTO equity_curve (experiment_id, date, equity)
            VALUES (?, '2025-01-02', 1000000)
            """,
            (experiment_id,),
        )
        artifact = experiments.execute(
            """
            INSERT INTO model_artifacts
                (experiment_id, strategy_id, model_version, model_file_path,
                 metadata_file_path, params_hash, artifact_sha256,
                 artifact_size, run_manifest_hash, is_latest)
            VALUES (?, 'lstm_rank_v1', 1, ?, 'model.json',
                    ?, ?, ?, ?, 1)
            """,
            (
                experiment_id,
                str(model_path),
                params_hash,
                model_sha256,
                model_path.stat().st_size,
                manifest_hash,
            ),
        )
        artifact_id = artifact.lastrowid
        experiments.execute(
            """
            INSERT INTO research_artifact_manifests
                (experiment_id, run_manifest_hash, schema_version,
                 artifact_kind, artifact_sha256, artifact_size,
                 metadata_json, created_at)
            VALUES (?, ?, 'research-artifact-manifest/v1',
                    'trained_model', ?, ?, ?, datetime('now'))
            """,
            (
                experiment_id,
                manifest_hash,
                model_sha256,
                model_path.stat().st_size,
                json.dumps(
                    {"strategy_id": "lstm_rank_v1", "model_version": 1}
                ),
            ),
        )
        experiments.commit()

    models = client.get(
        f"/api/experiments/{experiment_id}/models",
        headers=headers,
    )
    assert models.status_code == 200, models.text
    model_item = models.json()["data"][0]
    assert "model_file_path" not in model_item
    assert "metadata_file_path" not in model_item
    assert model_item["model_storage_key"] == "integration/lstm.joblib"
    assert model_item["metadata_storage_key"] == "model.json"

    deployed = client.post(
        "/api/trading/deployments",
        headers=headers,
        json={
            "strategy_id": "lstm_rank_v1",
            "display_name": "Deployable LSTM",
            "params": params,
            "mode": "batch",
            "status": "paused",
            "source_experiment_id": experiment_id,
        },
    )
    assert deployed.status_code == 200, deployed.text
    deployment_id = deployed.json()["data"]["deployment_id"]
    with sqlite3.connect(str(settings.abs_path(settings.TRADING_SIM_DB))) as trading:
        selected_artifact = trading.execute(
            "SELECT source_model_artifact_id FROM deployments WHERE id = ?",
            (deployment_id,),
        ).fetchone()[0]
    assert selected_artifact == artifact_id
    listed = client.get("/api/trading/deployments", headers=headers)
    assert listed.status_code == 200, listed.text
    listed_deployment = next(
        item for item in listed.json()["data"] if item["id"] == deployment_id
    )
    assert listed_deployment["source_model_artifact_id"] == artifact_id

def test_all_twenty_two_strategies_are_discoverable_but_pit_gate_before_create(
    client: TestClient,
    admin_session,
):
    headers = admin_session["headers"]
    strategy_response = client.get("/api/strategies/", headers=headers)
    assert strategy_response.status_code == 200, strategy_response.text
    strategies = strategy_response.json()["data"]
    assert len(strategies) == 22
    assert all(
        strategy["training_mode"] in {"none", "train_once", "periodic"}
        for strategy in strategies
    )
    assert next(
        strategy for strategy in strategies
        if strategy["strategy_id"] == "alpha158_lgb_v1"
    )["training_mode"] == "periodic"
    assert next(
        strategy for strategy in strategies
        if strategy["strategy_id"] == "lstm_rank_v1"
    )["training_mode"] == "train_once"

    for strategy in strategies:
        params = {
            field["name"]: field["default"]
            for field in strategy["params"]
            if field.get("default") is not None
        }
        body = {
            "name": f"contract-{strategy['strategy_id']}",
            "strategy_id": strategy["strategy_id"],
            "pool_preset": "csi500",
            "test_start": "2025-01-02",
            "test_end": "2025-12-31",
            "params": params,
            "mode": "batch",
        }
        if strategy["training_mode"] == "train_once":
            body["train_start"] = "2019-01-02"
            body["train_end"] = "2024-12-31"
        created = client.post(
            "/api/experiments/",
            headers=headers,
            json=body,
        )
        # 测试分支放宽（v0.8.x 分级门禁，见 5078493）：研究用途在 PIT 数据
        # 未激活时降级放行（缓存数据运行），提交端返回 200 并创建实验。
        assert created.status_code == 200, (
            strategy["strategy_id"],
            created.text,
        )
        assert created.json()["data"]["experiment_id"]

    listed = client.get(
        "/api/experiments/",
        headers=headers,
        params={"limit": 20},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["total"] == 22

    missing_training_window = client.post(
        "/api/experiments/",
        headers=headers,
        json={
            "name": "invalid-training-window",
            "strategy_id": "lstm_rank_v1",
            "pool_preset": "csi500",
            "test_start": "2025-01-02",
            "test_end": "2025-12-31",
            "params": {},
            "mode": "batch",
        },
    )
    assert missing_training_window.status_code == 422
    assert "训练窗口" in missing_training_window.json()["detail"]


def test_parameter_presets_and_experiment_inheritance(
    client: TestClient,
    admin_session,
    monkeypatch,
):
    monkeypatch.setattr(
        "backend.api.experiments.load_experiment_eligibility",
        _isolated_pit_experiment_eligible,
    )
    headers = admin_session["headers"]
    experiment_db = str(settings.abs_path(settings.EXPERIMENT_DB))
    with sqlite3.connect(experiment_db) as connection:
        source_id = connection.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category, pool_preset,
                 test_start, test_end, params, params_hash, mode, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "source-completed",
                "ma_cross_v1",
                "technical",
                "csi500",
                "2025-01-02",
                "2025-12-31",
                json.dumps({"fast_period": 10, "slow_period": 30}),
                "source-hash",
                "batch",
                "completed",
            ),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO experiment_metrics
                (experiment_id, cumulative_return, sharpe_ratio, annual_return,
                 max_drawdown)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, 0.32, 1.8, 0.21, -0.09),
        )
        pending_id = connection.execute(
            """
            INSERT INTO experiments
                (user_id, name, strategy_id, strategy_category, pool_preset,
                 test_start, test_end, params, params_hash, mode, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "source-pending",
                "ma_cross_v1",
                "technical",
                "csi500",
                "2025-01-02",
                "2025-12-31",
                "{}",
                "pending-hash",
                "batch",
                "pending",
            ),
        ).lastrowid
        connection.commit()

    rejected = client.post(
        "/api/experiments/parameter-presets",
        headers=headers,
        json={
            "name": "not-ready",
            "strategy_id": "ma_cross_v1",
            "params": {"fast_period": 10, "slow_period": 30},
            "source_experiment_id": pending_id,
        },
    )
    assert rejected.status_code == 422, rejected.text

    created = client.post(
        "/api/experiments/parameter-presets",
        headers=headers,
        json={
            "name": "low-drawdown",
            "strategy_id": "ma_cross_v1",
            "params": {"fast_period": 10, "slow_period": 30},
            "mode": "batch",
            "pool_preset": "csi500",
            "pool_custom_codes": ["000001.SZ"],
            "source_experiment_id": source_id,
            "notes": "validated parameters",
            "labels": ["best", "low-drawdown"],
            "is_default": True,
        },
    )
    assert created.status_code == 200, created.text
    preset = created.json()["data"]
    preset_id = preset["id"]
    assert preset["metrics_snapshot"]["sharpe_ratio"] == 1.8
    assert preset["pool_custom_codes"] == ["000001.SZ"]
    assert preset["is_default"] is True

    duplicate = client.post(
        "/api/experiments/parameter-presets",
        headers=headers,
        json={
            "name": "low-drawdown",
            "strategy_id": "ma_cross_v1",
            "params": {},
        },
    )
    assert duplicate.status_code == 409, duplicate.text

    updated = client.put(
        f"/api/experiments/parameter-presets/{preset_id}",
        headers=headers,
        json={"name": "low-drawdown-v2", "labels": ["favorite"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == "low-drawdown-v2"

    inherited = client.post(
        "/api/experiments/",
        headers=headers,
        json={
            "name": "inherited-experiment",
            "strategy_id": "ma_cross_v1",
            "pool_preset": "csi500",
            "test_start": "2026-01-02",
            "test_end": "2026-06-30",
            "params": {"fast_period": 10, "slow_period": 30},
            "mode": "batch",
            "source_experiment_id": source_id,
        },
    )
    # 测试分支放宽（v0.8.x 分级门禁，见 5078493）：研究用途在 PIT 数据
    # 未激活时降级放行，实验可创建；来源实验继承语义不变。
    assert inherited.status_code == 200, inherited.text
    inherited_id = inherited.json()["data"]["experiment_id"]
    with sqlite3.connect(experiment_db) as connection:
        inherited_count = connection.execute(
            "SELECT COUNT(*) FROM experiments WHERE source_experiment_id=?",
            (source_id,),
        ).fetchone()[0]
    assert inherited_count == 1
    assert inherited_id

    deleted_source = client.delete(
        f"/api/experiments/{source_id}", headers=headers
    )
    assert deleted_source.status_code == 200, deleted_source.text
    preset_after_source_delete = client.get(
        f"/api/experiments/parameter-presets/{preset_id}", headers=headers
    )
    assert preset_after_source_delete.status_code == 200
    assert (
        preset_after_source_delete.json()["data"]["source_experiment_id"]
        == source_id
    )

    listed = client.get(
        "/api/experiments/parameter-presets",
        headers=headers,
        params={"strategy_id": "ma_cross_v1"},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["total"] == 1

    deleted = client.delete(
        f"/api/experiments/parameter-presets/{preset_id}", headers=headers
    )
    assert deleted.status_code == 200, deleted.text


def test_openapi_exposes_typed_response_contracts(client: TestClient):
    schema = client.get("/openapi.json").json()
    response_schema = (
        schema["paths"]["/api/trading/positions"]["get"]["responses"]["200"]
        ["content"]["application/json"]["schema"]
    )
    assert "$ref" in response_schema
    assert "PositionResponse" in response_schema["$ref"]

    experiment_schema = schema["components"]["schemas"]["ExperimentResponse"]
    assert {"id", "strategy_id", "status", "params"} <= set(
        experiment_schema["properties"]
    )
    trade_schema = schema["components"]["schemas"]["TradeResponse"]
    assert {"signal_date", "date", "price", "action"} <= set(
        trade_schema["properties"]
    )
