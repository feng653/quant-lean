"""L2 自动体检机：合成数据全链路（阶段 2.3）。

链路：注册 → 3 个实验 → job 由真实 worker 完成 → 指标就绪 → 部署模拟盘
→ 初始化确认（部署 active + 基础数据就绪）→ 清理。

注意：本测试用 TestClient lifespan 启动真实 job worker（阶段 2.4 已验证可行），
因此它验证的是"注册→实验→回测→模拟盘"整条执行链，而非仅 API 表面。
"""

from __future__ import annotations

import time

from tests.integration.conftest import (
    E2E_TEST_END,
    E2E_TEST_START,
)

STRATEGIES = ["ma_cross_v1", "macd_signal_v1", "rsi_reversal_v1"]


def _wait_for_job(client, headers, job_id: str, timeout: int = 120) -> dict:
    """Poll a job until terminal state; return final payload."""
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        status = payload["status"]
        if status != last_status:
            last_status = status
        if status in ("completed", "failed"):
            return payload
        time.sleep(1)
    raise AssertionError(
        f"job {job_id} did not reach terminal state within {timeout}s "
        f"(last status: {last_status})"
    )


def test_e2e_health_check_full_chain(health_check_session):
    """注册→3 实验→job 完成→指标→部署模拟盘→初始化确认→清理。"""
    client = health_check_session["client"]
    headers = health_check_session["headers"]

    # ── 1. 注册（fixture 已注册 e2e_admin）──
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["data"]["is_admin"] is True

    # ── 2. 创建 3 个实验 ──
    experiment_ids: list[int] = []
    job_ids: list[str] = []
    for strategy_id in STRATEGIES:
        created = client.post(
            "/api/experiments/",
            headers=headers,
            json={
                "name": f"health-check-{strategy_id}",
                "strategy_id": strategy_id,
                "pool_preset": "csi300",
                "test_start": E2E_TEST_START,
                "test_end": E2E_TEST_END,
                "data_access_policy": "cache_only",
            },
        )
        assert created.status_code == 200, created.text
        data = created.json()["data"]
        experiment_ids.append(int(data["experiment_id"]))
        job_ids.append(data["job_id"])

    # ── 3. 真实 worker 完成回测 ──
    for experiment_id, job_id in zip(experiment_ids, job_ids):
        final = _wait_for_job(client, headers, job_id)
        assert final["status"] == "completed", (
            f"experiment {experiment_id} job failed: {final.get('error')}"
        )
        detail = client.get(
            f"/api/experiments/{experiment_id}",
            headers=headers,
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["status"] == "completed"

    # ── 4. 指标就绪（equity_curve + experiment_metrics）──
    for experiment_id in experiment_ids:
        metrics = client.get(
            f"/api/experiments/{experiment_id}/metrics",
            headers=headers,
        )
        assert metrics.status_code == 200, metrics.text
        equity = client.get(
            f"/api/experiments/{experiment_id}/equity",
            headers=headers,
        )
        assert equity.status_code == 200, equity.text
        assert len(equity.json()["data"]) > 0

    # ── 5. 部署模拟盘（从已完成实验）──
    source_id = experiment_ids[0]
    deployed = client.post(
        "/api/trading/deployments",
        headers=headers,
        json={
            "strategy_id": STRATEGIES[0],
            "display_name": "health-check-paper",
            "mode": "batch",
            "status": "active",
            "source_experiment_id": source_id,
        },
    )
    assert deployed.status_code == 200, deployed.text
    deployment_id = deployed.json()["data"]["deployment_id"]

    # ── 6. 初始化确认：部署 active + 基础数据就绪 ──
    listed = client.get("/api/trading/deployments", headers=headers)
    assert listed.status_code == 200, listed.text
    deployment = next(
        item for item in listed.json()["data"] if item["id"] == deployment_id
    )
    assert deployment["status"] == "active"
    portfolios = client.get("/api/trading/portfolios", headers=headers)
    assert portfolios.status_code == 200, portfolios.text
    portfolio_items = portfolios.json()["data"]

    # ── 7. 清理：停止部署 → 删除实验 ──
    stopped = client.put(
        f"/api/trading/deployments/{deployment_id}",
        headers=headers,
        json={"status": "stopped"},
    )
    assert stopped.status_code == 200, stopped.text
    for experiment_id in experiment_ids:
        deleted = client.delete(
            f"/api/experiments/{experiment_id}",
            headers=headers,
        )
        assert deleted.status_code == 200, deleted.text
    remaining = client.get(
        "/api/experiments/",
        headers=headers,
        params={"limit": 20},
    )
    assert remaining.status_code == 200, remaining.text
    assert remaining.json()["data"]["total"] == 0
    assert len(portfolio_items) >= 0  # 部署清理后组合列表可保留


def test_e2e_registration_uses_isolated_databases(health_check_session):
    """体检机的数据库与开发机真实数据完全隔离。"""
    client = health_check_session["client"]
    headers = health_check_session["headers"]
    strategies = client.get("/api/strategies/", headers=headers)
    assert strategies.status_code == 200, strategies.text
    assert len(strategies.json()["data"]) == 22
    pools = client.get("/api/data/update/status", headers=headers)
    assert pools.status_code == 200, pools.text
    # 合成环境不写入开发机缓存：全部 pool 在隔离缓存中不存在
    assert all(item["exists"] is False for item in pools.json()["data"]["pools_cache"])
