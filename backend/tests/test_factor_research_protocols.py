from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api import factor_research_protocols as protocol_api
from backend.data.factor_research_protocols import (
    FactorProtocolError,
    FactorResearchProtocolStore,
    evaluate_protocol,
)
from backend.dependencies import get_current_user


def _payload() -> dict:
    return {
        "schema_version": "factor-research-protocol/v1",
        "question": "该因子在成本后是否仍有稳定预测能力？",
        "hypothesis": "锁定窗口 RankIC 和多空收益超过预注册阈值。",
        "factor_ids": ["momentum_20"],
        "data": {
            "pool_id": "csi300",
            "version_policy": "latest_trusted_at_execution",
            "expected_dataset_digest": None,
        },
        "window": {"start": "2021-01-01", "end": "2024-12-31"},
        "implementation": {
            "horizons": [1, 5, 20],
            "primary_horizon": 5,
            "quantiles": 5,
            "rebalance_interval": 5,
            "default_cost_bps": 10.0,
            "cost_scenarios_bps": [0.0, 10.0, 20.0],
            "neutralization": "none",
        },
        "thresholds": {
            "rank_ic_mean_min": 0.02,
            "rank_ic_ir_min": 0.3,
            "long_short_mean_min": 0.0,
        },
        "export_rules": {
            "allow_strategy_export": True,
            "require_all_thresholds": True,
            "require_dataset_consistency": True,
            "minimum_evidence_runs": 1,
        },
    }


def _request(reference: dict) -> dict:
    return {
        "factor_id": "momentum_20",
        "related_factor_ids": [],
        "pool_preset": "csi300",
        "start": "2021-01-01",
        "end": "2024-12-31",
        "horizons": [1, 5, 20],
        "primary_horizon": 5,
        "quantiles": 5,
        "rebalance_interval": 5,
        "default_cost_bps": 10.0,
        "cost_scenarios_bps": [0.0, 10.0, 20.0],
        "neutralization": "none",
        "protocol": reference,
    }


def test_protocol_versions_are_immutable_and_owner_isolated(
    tmp_path: Path,
) -> None:
    database = tmp_path / "experiment.db"
    store = FactorResearchProtocolStore(database)
    series = store.create(owner_user_id=7, name="momentum", payload=_payload())
    version = series["versions"][0]

    with pytest.raises(FactorProtocolError, match="必须先锁定"):
        store.require_locked(
            owner_user_id=7,
            reference=version,
            request=_request(version),
        )
    locked = store.lock(
        owner_user_id=7,
        protocol_id=series["protocol_id"],
        version=1,
        payload_digest=version["payload_digest"],
    )
    verified = store.require_locked(
        owner_user_id=7,
        reference=locked,
        request=_request(locked),
    )
    assert verified["status"] == "locked"

    with sqlite3.connect(database) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="immutable",
    ):
        connection.execute(
            """
            UPDATE factor_research_protocol_versions
            SET payload_json = '{}'
            WHERE protocol_id = ? AND version = 1
            """,
            (series["protocol_id"],),
        )
    with sqlite3.connect(database) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="lock is immutable",
    ):
        connection.execute(
            """
            UPDATE factor_research_protocol_versions
            SET status = 'draft', locked_at = NULL
            WHERE protocol_id = ? AND version = 1
            """,
            (series["protocol_id"],),
        )
    with pytest.raises(FactorProtocolError) as inaccessible:
        store.get(owner_user_id=8, protocol_id=series["protocol_id"])
    assert inaccessible.value.status_code == 404


def test_protocol_mismatch_and_review_are_fail_closed(tmp_path: Path) -> None:
    store = FactorResearchProtocolStore(tmp_path / "experiment.db")
    series = store.create(owner_user_id=7, name="momentum", payload=_payload())
    version = store.lock(
        owner_user_id=7,
        protocol_id=series["protocol_id"],
        version=1,
        payload_digest=series["versions"][0]["payload_digest"],
    )
    request = _request(version)
    request["default_cost_bps"] = 5.0
    with pytest.raises(FactorProtocolError) as mismatch:
        store.require_locked(
            owner_user_id=7,
            reference=version,
            request=request,
        )
    assert mismatch.value.code == "protocol_request_mismatch"

    review = evaluate_protocol(
        {**version, "payload": _payload()},
        {
            "ic": {
                "5": {
                    "summary": {
                        "rank_ic": {"mean": 0.03, "icir": 0.2},
                    }
                }
            },
            "quantile_returns": {"long_short": {"mean": 0.01}},
        },
    )
    assert review["passed"] is False
    assert [item["passed"] for item in review["checks"]] == [True, False, True]
    assert review["read_only"] is True


def test_protocol_api_lists_only_current_user(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "experiment.db"
    monkeypatch.setattr(
        protocol_api,
        "_store",
        lambda: FactorResearchProtocolStore(database),
    )
    user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read", "experiments:create"],
    }
    app = FastAPI()
    app.include_router(protocol_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    payload = _payload()
    with TestClient(app) as client:
        created = client.post(
            "/api/factor-research/protocols",
            json={"name": "owner protocol", "payload": payload},
        )
        assert created.status_code == 201
        protocol = created.json()["data"]
        locked = client.post(
            f"/api/factor-research/protocols/{protocol['protocol_id']}"
            "/versions/1/lock",
            json={
                "payload_digest": protocol["versions"][0]["payload_digest"],
            },
        )
        assert locked.status_code == 200
        assert locked.json()["data"]["status"] == "locked"

        user["id"] = 8
        assert client.get("/api/factor-research/protocols").json()["data"] == []


def test_protocol_writes_require_experiment_create_permission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "experiment.db"
    monkeypatch.setattr(
        protocol_api,
        "_store",
        lambda: FactorResearchProtocolStore(database),
    )
    user = {
        "id": 7,
        "is_admin": False,
        "permissions": ["data:read"],
    }
    app = FastAPI()
    app.include_router(protocol_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    existing = FactorResearchProtocolStore(database).create(
        owner_user_id=7,
        name="existing",
        payload=_payload(),
    )
    with TestClient(app) as client:
        assert client.get("/api/factor-research/protocols").status_code == 200
        create_response = client.post(
            "/api/factor-research/protocols",
            json={"name": "forbidden", "payload": _payload()},
        )
        version_response = client.post(
            f"/api/factor-research/protocols/{existing['protocol_id']}/versions",
            json={
                "expected_current_version": 1,
                "payload": {**_payload(), "question": "另一个足够长的研究问题是什么？"},
            },
        )
        lock_response = client.post(
            f"/api/factor-research/protocols/{existing['protocol_id']}"
            "/versions/1/lock",
            json={
                "payload_digest": existing["versions"][0]["payload_digest"],
            },
        )
    for response in (create_response, version_response, lock_response):
        assert response.status_code == 403
        assert response.json()["detail"] == "需要权限: experiments:create"
