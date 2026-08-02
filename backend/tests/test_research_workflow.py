from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from typing import Any

import aiosqlite
from fastapi import HTTPException
import pandas as pd
import pytest

from backend.api import research_workflow as workflow_api
from backend.api.admin import _ALL_PERMISSIONS
from backend.api.research_workflow_schemas import PromotionTransitionBody
from backend.auth.permissions import (
    get_role_permissions,
    is_valid_permission,
)
from backend.config import settings
from backend.data.market_quality import audit_market_data
from backend.db.migrate import migrate_experiment
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    canonical_json_bytes,
    canonical_sha256,
)
from backend.services.research_workflow import (
    ResearchWorkflowService,
    WorkflowError,
)


OWNER = {
    "id": 7,
    "is_admin": False,
    "permissions": ["experiments:read", "experiments:create"],
}
REVIEWER = {
    "id": 8,
    "is_admin": False,
    "permissions": ["experiments:read", "experiments:promote"],
}
OUTSIDER = {
    "id": 9,
    "is_admin": False,
    "permissions": ["experiments:read", "experiments:create"],
}


def _valid_manifest(
    experiment_id: int,
    *,
    point_in_time: bool = True,
    dirty: bool = False,
    quality_clean: bool = True,
    risks: list[str] | None = None,
    valid_execution: bool = True,
    market_quality: str = "clean",
    benchmark_evidence: str = "valid",
    zero_cost_field: str | None = None,
    missing_cost_field: str | None = None,
    volume_participation: float | None = 0.1,
) -> dict[str, Any]:
    test_start = "2024-07-01" if experiment_id in {2, 4} else "2024-01-01"
    test_end = "2024-12-31" if experiment_id in {2, 4} else "2024-06-30"
    execution: dict[str, Any] = {
        "initial_capital": 1_000_000,
        "max_positions": 20,
        "cost_model": {
            "commission_rate": 0.0003,
            "slippage_rate": 0.001,
            "stamp_duty_rate": 0.001,
            "min_commission": 5.0,
        },
        "execution_constraints": {
            "volume_participation": volume_participation,
            "lot_size": 100,
        },
        "rebalance_mode": "signal_driven",
        "portfolio_signal_mode": "event_orders",
        "signal_timing": "signal_on_T_fill_next_session_open",
    }
    if not valid_execution:
        execution.pop("signal_timing")
    if zero_cost_field is not None:
        execution["cost_model"][zero_cost_field] = 0
    if missing_cost_field is not None:
        execution["cost_model"].pop(missing_cost_field)
    quality_frame = pd.DataFrame(
        {
            ("000001", "open"): [
                -10.0 if market_quality == "fatal" else 10.0
            ],
            ("000001", "high"): [11.0],
            ("000001", "low"): [9.0],
            ("000001", "close"): [10.5],
            ("000001", "volume"): [1000.0],
        },
        index=pd.DatetimeIndex(["2024-12-31"], name="date"),
    )
    quality_frame.columns = pd.MultiIndex.from_tuples(
        quality_frame.columns,
        names=["code", "field"],
    )
    manifest = {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "experiment": {
            "experiment_id": experiment_id,
            "strategy_id": "workflow_strategy",
            "mode": "batch",
        },
        "environment": {
            "git": {"sha": "a" * 40, "dirty": dirty},
        },
        "dataset": {
            "digest": "d" * 64,
            "rows": 100,
            "columns": 6,
            "start": "2020-01-01",
            "end": "2024-12-31",
        },
        "windows": {
            "test_start": test_start,
            "test_end": test_end,
        },
        "benchmark": {
            "code": "000300",
            "available": True,
            "sha256": "f" * 64,
            "fetch_start": "2023-12-20",
            "fetch_end": test_end,
            "snapshot": {
                "schema_version": "research-data-snapshot/v1",
                "kind": "benchmark",
                "key": "b" * 64,
                "relative_key": f"benchmark/{'b' * 64}.parquet",
                "file_sha256": "b" * 64,
                "size_bytes": 1024,
                "format": "parquet",
                "schema": {"sha256": "c" * 64},
                "series": {"name": "close", "dtype": "float64"},
            },
        },
        "universe": {
            "snapshot_hash": "u" * 64,
            "point_in_time": point_in_time,
            "quality": {"is_clean": quality_clean},
        },
        "research_risk_warnings": risks or [],
        "execution": execution,
    }
    if benchmark_evidence == "missing":
        manifest.pop("benchmark")
    elif benchmark_evidence == "unavailable":
        manifest["benchmark"]["available"] = False
    elif benchmark_evidence == "invalid_hash":
        manifest["benchmark"]["sha256"] = "not-a-sha256"
    elif benchmark_evidence == "tampered_snapshot":
        manifest["benchmark"]["snapshot"]["file_sha256"] = "c" * 64
    elif benchmark_evidence == "misaligned_window":
        manifest["benchmark"]["fetch_end"] = "2024-12-30"
    if market_quality != "missing":
        quality = audit_market_data(
            quality_frame,
            test_end="2024-12-31",
            source="akshare",
            price_adjustment="qfq",
        ).to_dict()
        if market_quality == "invalid":
            quality["source"]["provider"] = "tampered"
        manifest["market_data_quality"] = quality
    return manifest


def _initialize(path: Path) -> None:
    async def scenario() -> None:
        async with aiosqlite.connect(str(path)) as connection:
            await connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT,
                    strategy_id TEXT NOT NULL,
                    strategy_category TEXT NOT NULL DEFAULT 'technical',
                    pool_preset TEXT,
                    pool_custom_codes TEXT,
                    pool_industries TEXT,
                    train_start TEXT,
                    train_end TEXT,
                    test_start TEXT NOT NULL,
                    test_end TEXT NOT NULL,
                    params TEXT NOT NULL DEFAULT '{}',
                    params_hash TEXT NOT NULL DEFAULT 'hash',
                    mode TEXT NOT NULL DEFAULT 'batch',
                    requires_training INTEGER NOT NULL DEFAULT 0,
                    retrain_frequency TEXT DEFAULT 'never',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_log TEXT,
                    progress_pct REAL DEFAULT 0,
                    progress_message TEXT
                );
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER NOT NULL UNIQUE,
                    sharpe_ratio REAL,
                    annual_return REAL,
                    max_drawdown REAL
                );
                CREATE TABLE deployments (id INTEGER PRIMARY KEY);
                CREATE TABLE orders (id INTEGER PRIMARY KEY);
                """
            )
            await migrate_experiment(connection)
            await connection.commit()

    asyncio.run(scenario())


def _insert_experiments(
    path: Path,
    *,
    manifest_options: dict[str, Any] | None = None,
    sharpe: float = 1.5,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO experiments
                (id, user_id, name, strategy_id, test_start, test_end,
                 status, run_spec)
            VALUES
                (1, 7, 'selection', 'workflow_strategy',
                 '2024-01-01', '2024-06-30', 'completed', '{}'),
                (2, 7, 'locked', 'workflow_strategy',
                 '2024-07-01', '2024-12-31', 'completed',
                 '{"research_trust":"locked_test"}'),
                (3, 9, 'foreign', 'workflow_strategy',
                 '2024-01-01', '2024-06-30', 'completed', '{}'),
                (4, 7, 'fake locked', 'workflow_strategy',
                 '2024-07-01', '2024-12-31', 'completed',
                 '{"research_trust":"locked_test"}')
            """
        )
        connection.execute(
            "UPDATE experiments SET source_experiment_id=1 WHERE id=2"
        )
        connection.execute(
            """
            INSERT INTO param_sweeps
                (id, user_id, strategy_id, name, sweep_config,
                 research_trust, promoted_experiment_id,
                 promotion_source_experiment_id, promoted_at)
            VALUES (1, 7, 'workflow_strategy', 'strict', '{}',
                    'locked_test', 2, 1, datetime('now'))
            """
        )
        connection.executemany(
            """
            INSERT INTO experiment_metrics
                (experiment_id, sharpe_ratio, annual_return, max_drawdown)
            VALUES (?, ?, 0.2, -0.1)
            """,
            [(1, sharpe), (2, sharpe), (3, sharpe), (4, sharpe)],
        )
        for experiment_id in (1, 2, 3, 4):
            options = manifest_options if experiment_id == 2 else {}
            manifest = _valid_manifest(experiment_id, **(options or {}))
            connection.execute(
                """
                INSERT INTO research_run_manifests
                    (experiment_id, user_id, schema_version, manifest_json,
                     manifest_hash, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    experiment_id,
                    9 if experiment_id == 3 else 7,
                    RUN_MANIFEST_SCHEMA,
                    canonical_json_bytes(manifest).decode("utf-8"),
                    canonical_sha256(manifest),
                ),
            )


def _hypothesis_payload(
    *,
    key: str = "hypothesis-key-001",
    accepted_risks: list[str] | None = None,
    threshold: float = 1.0,
) -> dict[str, Any]:
    return {
        "title": "Momentum should survive a locked test",
        "falsifiable_statement": (
            "The locked-test Sharpe ratio will be at least the "
            "preregistered threshold."
        ),
        "preregistered_metrics": [
            {
                "name": "sharpe_ratio",
                "operator": "gte",
                "threshold": threshold,
            }
        ],
        "risk_acceptance": {
            "accepted_risks": accepted_risks or [],
            "rationale": "Risks were reviewed before any selection trial.",
        },
        "idempotency_key": key,
    }


def _group_payload(hypothesis_id: int) -> dict[str, Any]:
    return {
        "hypothesis_id": hypothesis_id,
        "name": "Preregistered momentum group",
        "strategy_id": "workflow_strategy",
        "selection_protocol": {
            "start": "2024-01-01",
            "end": "2024-06-30",
        },
        "locked_protocol": {
            "start": "2024-07-01",
            "end": "2024-12-31",
        },
        "manifest_policy": {
            "required": True,
            "schema_version": RUN_MANIFEST_SCHEMA,
            "require_clean_git": True,
        },
        "idempotency_key": "group-key-001",
    }


async def _preregister(
    service: ResearchWorkflowService,
    *,
    accepted_risks: list[str] | None = None,
    threshold: float = 1.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    hypothesis = await service.create_hypothesis(
        _hypothesis_payload(
            accepted_risks=accepted_risks,
            threshold=threshold,
        ),
        OWNER,
    )
    hypothesis = await service.transition_hypothesis(
        hypothesis["id"],
        target_status="submitted",
        expected_version=hypothesis["version"],
        user=OWNER,
    )
    group = await service.create_group(
        _group_payload(hypothesis["id"]),
        OWNER,
    )
    group = await service.transition_group(
        group["id"],
        target_status="active",
        expected_version=group["version"],
        user=OWNER,
    )
    return hypothesis, group


async def _link_evidence(
    service: ResearchWorkflowService,
    group: dict[str, Any],
    *,
    include_locked: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    selection = await service.link_trial(
        group["id"],
        {
            "experiment_id": 1,
            "role": "selection",
            "expected_group_version": group["version"],
            "idempotency_key": "selection-link-001",
        },
        OWNER,
    )
    if not include_locked:
        return selection, None
    refreshed = await service.get_group(group["id"], OWNER)
    locked = await service.link_trial(
        group["id"],
        {
            "experiment_id": 2,
            "role": "locked_test",
            "expected_group_version": refreshed["version"],
            "idempotency_key": "locked-link-001",
        },
        OWNER,
    )
    return selection, locked


async def _reports(
    service: ResearchWorkflowService,
    group_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    group = await service.get_group(group_id, OWNER)
    selection = await service.create_report(
        group_id,
        {
            "report_type": "selection",
            "expected_group_version": group["version"],
            "idempotency_key": "selection-report-001",
        },
        OWNER,
    )
    group = await service.get_group(group_id, OWNER)
    final = await service.create_report(
        group_id,
        {
            "report_type": "final",
            "expected_group_version": group["version"],
            "idempotency_key": "final-report-001",
        },
        OWNER,
    )
    return selection, final


def test_hypothesis_preregistration_is_versioned_and_immutable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    service = ResearchWorkflowService(db_path)
    hypothesis = asyncio.run(
        service.create_hypothesis(_hypothesis_payload(), OWNER)
    )
    repeated = asyncio.run(
        service.create_hypothesis(_hypothesis_payload(), OWNER)
    )
    assert hypothesis["id"] == repeated["id"]

    async def update_twice() -> list[Any]:
        payload = {
            **_hypothesis_payload(),
            "title": "Updated preregistered hypothesis",
            "expected_version": hypothesis["version"],
        }
        payload.pop("idempotency_key")
        return await asyncio.gather(
            service.update_hypothesis(hypothesis["id"], payload, OWNER),
            service.update_hypothesis(hypothesis["id"], payload, OWNER),
            return_exceptions=True,
        )

    results = asyncio.run(update_twice())
    assert sum(isinstance(item, WorkflowError) for item in results) == 1
    updated = next(item for item in results if isinstance(item, dict))
    submitted = asyncio.run(
        service.transition_hypothesis(
            hypothesis["id"],
            target_status="submitted",
            expected_version=updated["version"],
            user=OWNER,
        )
    )
    with pytest.raises(WorkflowError) as immutable:
        asyncio.run(
            service.update_hypothesis(
                hypothesis["id"],
                {
                    **{
                        key: value
                        for key, value in _hypothesis_payload().items()
                        if key != "idempotency_key"
                    },
                    "expected_version": submitted["version"],
                },
                OWNER,
            )
        )
    assert immutable.value.code == "hypothesis_core_immutable"
    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE research_hypotheses
                SET falsifiable_statement='silently rewritten'
                WHERE id=?
                """,
                (hypothesis["id"],),
            )


def test_trial_linking_enforces_idor_protocol_and_manual_locked_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    _insert_experiments(db_path)
    service = ResearchWorkflowService(db_path)
    _, group = asyncio.run(_preregister(service))

    with pytest.raises(WorkflowError) as idor:
        asyncio.run(
            service.link_trial(
                group["id"],
                {
                    "experiment_id": 3,
                    "role": "selection",
                    "expected_group_version": group["version"],
                    "idempotency_key": "foreign-link-001",
                },
                OWNER,
            )
        )
    assert idor.value.status_code == 404

    selection, _ = asyncio.run(
        _link_evidence(service, group, include_locked=False)
    )
    refreshed = asyncio.run(service.get_group(group["id"], OWNER))
    repeated = asyncio.run(
        service.link_trial(
            group["id"],
            {
                "experiment_id": 1,
                "role": "selection",
                "expected_group_version": group["version"],
                "idempotency_key": "selection-link-001",
            },
            OWNER,
        )
    )
    assert repeated["id"] == selection["id"]
    with pytest.raises(WorkflowError) as fake_locked:
        asyncio.run(
            service.link_trial(
                group["id"],
                {
                    "experiment_id": 4,
                    "role": "locked_test",
                    "expected_group_version": refreshed["version"],
                    "idempotency_key": "fake-locked-001",
                },
                OWNER,
            )
        )
    assert fake_locked.value.code == "locked_provenance_missing"

    locked = asyncio.run(
        service.link_trial(
            group["id"],
            {
                "experiment_id": 2,
                "role": "locked_test",
                "expected_group_version": refreshed["version"],
                "idempotency_key": "locked-link-001",
            },
            OWNER,
        )
    )
    assert locked["source_trial_id"] == selection["id"]
    with pytest.raises(WorkflowError) as cross_user:
        asyncio.run(service.get_group(group["id"], OUTSIDER))
    assert cross_user.value.status_code == 404


def test_reports_are_immutable_and_selection_never_claims_deployable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    _insert_experiments(db_path)
    service = ResearchWorkflowService(db_path)
    _, group = asyncio.run(_preregister(service))
    asyncio.run(_link_evidence(service, group))
    selection, final = asyncio.run(_reports(service, group["id"]))

    assert selection["snapshot"]["evidence_scope"] == "selection_only"
    assert selection["snapshot"]["deployment_eligible"] is False
    assert final["snapshot"]["evidence_scope"] == (
        "strict_locked_test_final"
    )
    assert final["snapshot"]["trials"][0][
        "manual_locked_promotion_verified"
    ] is True
    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE research_reports SET snapshot_json='{}'
                WHERE id=?
                """,
                (final["id"],),
            )


def test_selection_report_promotion_cannot_be_approved(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    _insert_experiments(db_path)
    service = ResearchWorkflowService(db_path)
    _, group = asyncio.run(_preregister(service))
    asyncio.run(_link_evidence(service, group, include_locked=False))
    refreshed = asyncio.run(service.get_group(group["id"], OWNER))
    selection = asyncio.run(
        service.create_report(
            group["id"],
            {
                "report_type": "selection",
                "expected_group_version": refreshed["version"],
                "idempotency_key": "selection-report-001",
            },
            OWNER,
        )
    )
    refreshed = asyncio.run(service.get_group(group["id"], OWNER))
    promotion = asyncio.run(
        service.create_promotion(
            group["id"],
            {
                "report_id": selection["id"],
                "rationale": "Selection evidence requires independent review.",
                "expected_group_version": refreshed["version"],
                "idempotency_key": "promotion-selection-001",
            },
            OWNER,
        )
    )
    promotion = asyncio.run(
        service.transition_promotion(
            promotion["id"],
            {
                "target_status": "reviewed",
                "expected_version": promotion["version"],
                "rationale": None,
            },
            OWNER,
        )
    )
    with pytest.raises(WorkflowError) as blocked:
        asyncio.run(
            service.transition_promotion(
                promotion["id"],
                {
                    "target_status": "approved",
                    "expected_version": promotion["version"],
                    "rationale": "Approve",
                },
                REVIEWER,
            )
        )
    assert blocked.value.code == "promotion_gate_blocked"
    assert "final_report_missing" in {
        item["code"] for item in blocked.value.blockers
    }


def test_approval_permission_gates_and_no_external_action(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    _insert_experiments(db_path)
    service = ResearchWorkflowService(db_path)
    _, group = asyncio.run(_preregister(service))
    asyncio.run(_link_evidence(service, group))
    _, final = asyncio.run(_reports(service, group["id"]))
    refreshed = asyncio.run(service.get_group(group["id"], OWNER))
    promotion = asyncio.run(
        service.create_promotion(
            group["id"],
            {
                "report_id": final["id"],
                "rationale": "Strict final evidence is ready for review.",
                "expected_group_version": refreshed["version"],
                "idempotency_key": "promotion-final-001",
            },
            OWNER,
        )
    )
    promotion = asyncio.run(
        service.transition_promotion(
            promotion["id"],
            {
                "target_status": "reviewed",
                "expected_version": promotion["version"],
                "rationale": None,
            },
            OWNER,
        )
    )
    with pytest.raises(WorkflowError) as forbidden:
        asyncio.run(
            service.transition_promotion(
                promotion["id"],
                {
                    "target_status": "approved",
                    "expected_version": promotion["version"],
                    "rationale": None,
                },
                OWNER,
            )
        )
    assert forbidden.value.status_code == 403

    approved = asyncio.run(
        service.transition_promotion(
            promotion["id"],
            {
                "target_status": "approved",
                "expected_version": promotion["version"],
                "rationale": "Independent approval after all gates passed.",
            },
            REVIEWER,
        )
    )
    assert approved["status"] == "approved"
    assert approved["decided_by"] == REVIEWER["id"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM deployments"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM orders"
        ).fetchone()[0] == 0
        events = connection.execute(
            """
            SELECT payload_json FROM research_workflow_events
            WHERE entity_type='promotion' AND entity_id=?
            ORDER BY id
            """,
            (promotion["id"],),
        ).fetchall()
    assert all(
        json.loads(row[0]).get("external_action") == "none"
        for row in events
    )


def test_promote_permission_is_grantable_but_not_an_operator_default() -> None:
    assert is_valid_permission("experiments:promote")
    assert "experiments:promote" not in get_role_permissions("operator")
    assert "experiments:promote" in {
        item["key"] for item in _ALL_PERMISSIONS
    }


@pytest.mark.parametrize(
    ("manifest_options", "threshold", "accepted_risks", "expected_code"),
    [
        ({"point_in_time": False}, 1.0, [], "point_in_time_required"),
        (
            {"market_quality": "missing"},
            1.0,
            [],
            "market_data_quality_missing",
        ),
        (
            {"market_quality": "fatal"},
            1.0,
            [],
            "market_data_quality_failed",
        ),
        (
            {"market_quality": "invalid"},
            1.0,
            [],
            "market_data_quality_integrity_failed",
        ),
        ({"dirty": True}, 1.0, [], "dirty_code"),
        (
            {"quality_clean": False},
            1.0,
            [],
            "universe_data_quality_failed",
        ),
        (
            {"valid_execution": False},
            1.0,
            [],
            "execution_risk_gate_failed",
        ),
        (
            {"benchmark_evidence": "missing"},
            1.0,
            [],
            "benchmark_evidence_missing",
        ),
        (
            {"benchmark_evidence": "unavailable"},
            1.0,
            [],
            "benchmark_unavailable",
        ),
        (
            {"benchmark_evidence": "invalid_hash"},
            1.0,
            [],
            "benchmark_hash_invalid",
        ),
        (
            {"benchmark_evidence": "tampered_snapshot"},
            1.0,
            [],
            "benchmark_snapshot_integrity_failed",
        ),
        (
            {"benchmark_evidence": "misaligned_window"},
            1.0,
            [],
            "benchmark_window_misaligned",
        ),
        (
            {"zero_cost_field": "commission_rate"},
            1.0,
            [],
            "execution_commission_required",
        ),
        (
            {"zero_cost_field": "slippage_rate"},
            1.0,
            [],
            "execution_slippage_required",
        ),
        (
            {"zero_cost_field": "stamp_duty_rate"},
            1.0,
            [],
            "execution_stamp_duty_required",
        ),
        (
            {"zero_cost_field": "min_commission"},
            1.0,
            [],
            "execution_minimum_commission_required",
        ),
        (
            {"missing_cost_field": "commission_rate"},
            1.0,
            [],
            "execution_commission_required",
        ),
        (
            {"volume_participation": None},
            1.0,
            [],
            "execution_volume_participation_required",
        ),
        (
            {"volume_participation": 0.21},
            1.0,
            [],
            "execution_volume_participation_invalid",
        ),
        (
            {"risks": ["survivorship_bias"]},
            1.0,
            [],
            "unaccepted_research_risk",
        ),
        ({}, 2.0, [], "preregistered_metric_failed"),
    ],
)
def test_approval_fails_closed_with_structured_blockers(
    tmp_path: Path,
    manifest_options: dict[str, Any],
    threshold: float,
    accepted_risks: list[str],
    expected_code: str,
) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    _insert_experiments(
        db_path,
        manifest_options=manifest_options,
    )
    service = ResearchWorkflowService(db_path)
    _, group = asyncio.run(
        _preregister(
            service,
            accepted_risks=accepted_risks,
            threshold=threshold,
        )
    )
    asyncio.run(_link_evidence(service, group))
    _, final = asyncio.run(_reports(service, group["id"]))
    refreshed = asyncio.run(service.get_group(group["id"], OWNER))
    promotion = asyncio.run(
        service.create_promotion(
            group["id"],
            {
                "report_id": final["id"],
                "rationale": "Review all preregistered gates.",
                "expected_group_version": refreshed["version"],
                "idempotency_key": "promotion-blocked-001",
            },
            OWNER,
        )
    )
    promotion = asyncio.run(
        service.transition_promotion(
            promotion["id"],
            {
                "target_status": "reviewed",
                "expected_version": promotion["version"],
                "rationale": None,
            },
            OWNER,
        )
    )
    with pytest.raises(WorkflowError) as blocked:
        asyncio.run(
            service.transition_promotion(
                promotion["id"],
                {
                    "target_status": "approved",
                    "expected_version": promotion["version"],
                    "rationale": None,
                },
                REVIEWER,
            )
        )
    assert expected_code in {
        item["code"] for item in blocked.value.blockers
    }


def test_approval_rechecks_live_manifest_integrity(tmp_path: Path) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    _insert_experiments(db_path)
    service = ResearchWorkflowService(db_path)
    _, group = asyncio.run(_preregister(service))
    asyncio.run(_link_evidence(service, group))
    _, final = asyncio.run(_reports(service, group["id"]))
    refreshed = asyncio.run(service.get_group(group["id"], OWNER))
    promotion = asyncio.run(
        service.create_promotion(
            group["id"],
            {
                "report_id": final["id"],
                "rationale": "Evidence integrity must be live-checked.",
                "expected_group_version": refreshed["version"],
                "idempotency_key": "promotion-tamper-001",
            },
            OWNER,
        )
    )
    promotion = asyncio.run(
        service.transition_promotion(
            promotion["id"],
            {
                "target_status": "reviewed",
                "expected_version": promotion["version"],
                "rationale": None,
            },
            OWNER,
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_research_manifest_no_update")
        connection.execute(
            """
            UPDATE research_run_manifests
            SET manifest_json='{}' WHERE experiment_id=2
            """
        )
        connection.commit()
    with pytest.raises(WorkflowError) as blocked:
        asyncio.run(
            service.transition_promotion(
                promotion["id"],
                {
                    "target_status": "approved",
                    "expected_version": promotion["version"],
                    "rationale": None,
                },
                REVIEWER,
            )
        )
    assert "manifest_integrity_failure" in {
        item["code"] for item in blocked.value.blockers
    }


def test_audit_is_append_only_and_idor_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    service = ResearchWorkflowService(db_path)
    hypothesis = asyncio.run(
        service.create_hypothesis(_hypothesis_payload(), OWNER)
    )
    events = asyncio.run(
        service.list_events(
            entity_type="hypothesis",
            entity_id=hypothesis["id"],
            user=OWNER,
        )
    )
    assert events[0]["event_type"] == "created"
    with pytest.raises(WorkflowError) as hidden:
        asyncio.run(
            service.list_events(
                entity_type="hypothesis",
                entity_id=hypothesis["id"],
                user=OUTSIDER,
            )
        )
    assert hidden.value.status_code == 404
    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                UPDATE research_workflow_events SET event_type='rewritten'
                WHERE id=?
                """,
                (events[0]["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM research_workflow_events WHERE id=?",
                (events[0]["id"],),
            )


def test_api_returns_structured_permission_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "workflow.db"
    _initialize(db_path)
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(db_path))
    service = ResearchWorkflowService(db_path)
    hypothesis = asyncio.run(
        service.create_hypothesis(_hypothesis_payload(), OWNER)
    )
    hypothesis = asyncio.run(
        service.transition_hypothesis(
            hypothesis["id"],
            target_status="submitted",
            expected_version=hypothesis["version"],
            user=OWNER,
        )
    )
    group = asyncio.run(
        service.create_group(_group_payload(hypothesis["id"]), OWNER)
    )
    # A non-owner cannot even review a foreign workflow promotion; approval
    # permission is checked separately once a promotion exists.
    with pytest.raises(HTTPException) as permission:
        asyncio.run(
            workflow_api.transition_promotion(
                999,
                PromotionTransitionBody(
                    expected_version=1,
                    target_status="approved",
                ),
                {
                    "id": 10,
                    "is_admin": False,
                    "permissions": ["experiments:read"],
                },
            )
        )
    assert permission.value.status_code == 404
    assert group["status"] == "draft"


def test_workflow_migration_is_concurrent_and_idempotent(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[set[str], int]:
        db_path = tmp_path / "migration.db"
        async with aiosqlite.connect(str(db_path)) as setup:
            await setup.executescript(
                """
                CREATE TABLE experiments (id INTEGER PRIMARY KEY);
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                """
            )
            await setup.commit()
        async with (
            aiosqlite.connect(str(db_path), isolation_level=None) as first,
            aiosqlite.connect(str(db_path), isolation_level=None) as second,
        ):
            await first.execute("PRAGMA busy_timeout=5000")
            await second.execute("PRAGMA busy_timeout=5000")
            await asyncio.gather(
                migrate_experiment(first),
                migrate_experiment(second),
            )
            await migrate_experiment(first)
            cursor = await first.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE 'research_%'
                """
            )
            tables = {row[0] for row in await cursor.fetchall()}
            cursor = await first.execute(
                """
                SELECT COUNT(*) FROM schema_migrations
                WHERE version='experiment-008-research-workflow'
                """
            )
            count = int((await cursor.fetchone())[0])
            return tables, count

    tables, count = asyncio.run(scenario())
    assert {
        "research_hypotheses",
        "research_experiment_groups",
        "research_trials",
        "research_reports",
        "research_promotions",
        "research_workflow_events",
    } <= tables
    assert count == 1
