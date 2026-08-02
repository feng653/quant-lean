"""Research lineage and exact-replay API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.research_robustness_schemas import (
    ResearchRobustnessResponse,
    RobustnessQuery,
)
from backend.api.schemas import (
    ApiResponse,
    ResearchManifestResponse,
    ResearchRerunBody,
    ResearchRerunResponse,
)
from backend.config import settings
from backend.dependencies import (
    get_job_broker,
    get_strategy_registry,
    require_permission,
)
from backend.services.research_manifest import (
    ManifestConflictError,
    canonical_sha256,
    capture_runtime_environment,
    compare_replay_environment,
    load_run_manifest,
)
from backend.services.experiment_eligibility import assess_experiment_eligibility
from backend.services.research_robustness import (
    ResearchRobustnessError,
    build_robustness_report,
)


router = APIRouter(prefix="/api/research", tags=["Research"])
_DISPATCHING_JOB = "__dispatching__"


async def _owned_experiment(
    connection: aiosqlite.Connection,
    experiment_id: int,
    user: dict[str, Any],
) -> aiosqlite.Row:
    cursor = await connection.execute(
        "SELECT * FROM experiments WHERE id=?",
        (experiment_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    if not user.get("is_admin") and int(row["user_id"]) != int(user["id"]):
        raise HTTPException(status_code=403, detail="无权访问该实验")
    return row


async def _await_dispatched_job(
    db_path: Path,
    *,
    user_id: int,
    idempotency_key: str,
    experiment_id: int,
) -> str:
    """Wait briefly for the request owner and recover a persisted broker job."""
    for _ in range(40):
        connection = await aiosqlite.connect(str(db_path))
        connection.row_factory = aiosqlite.Row
        try:
            cursor = await connection.execute(
                """
                SELECT job_uuid FROM research_rerun_requests
                WHERE user_id=? AND idempotency_key=?
                """,
                (user_id, idempotency_key),
            )
            request_row = await cursor.fetchone()
            if (
                request_row is not None
                and request_row["job_uuid"]
                and request_row["job_uuid"] != _DISPATCHING_JOB
            ):
                return str(request_row["job_uuid"])

            # The broker writes the job before this API binds its UUID. If the
            # owner crashes in that narrow window, recover instead of creating
            # a duplicate job.
            try:
                cursor = await connection.execute(
                    """
                    SELECT job_uuid FROM jobs
                    WHERE job_type='backtest'
                      AND resource_type='experiment'
                      AND resource_id=?
                      AND user_id=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (str(experiment_id), user_id),
                )
                job_row = await cursor.fetchone()
            except aiosqlite.OperationalError:
                job_row = None
            if job_row is not None:
                recovered = str(job_row["job_uuid"])
                await connection.execute(
                    """
                    UPDATE research_rerun_requests SET job_uuid=?
                    WHERE user_id=? AND idempotency_key=? AND job_uuid=?
                    """,
                    (
                        recovered,
                        user_id,
                        idempotency_key,
                        _DISPATCHING_JOB,
                    ),
                )
                await connection.commit()
                return recovered
        finally:
            await connection.close()
        await asyncio.sleep(0.05)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "idempotency_request_in_progress",
            "message": "相同的重跑请求仍在投递中",
        },
    )


@router.get(
    "/experiments/{experiment_id}/manifest",
    response_model=ApiResponse[ResearchManifestResponse],
)
async def get_research_manifest(
    experiment_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    db_path = settings.abs_path(settings.EXPERIMENT_DB)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    try:
        await _owned_experiment(connection, experiment_id, user)
    finally:
        await connection.close()
    try:
        result = await load_run_manifest(db_path, experiment_id)
    except ManifestConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "manifest_integrity_failure",
                "differences": exc.differences,
            },
        ) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="该实验尚无运行清单")
    return {"data": {"experiment_id": experiment_id, **result}}


@router.get(
    "/experiments/{experiment_id}/robustness",
    response_model=ApiResponse[ResearchRobustnessResponse],
)
async def get_research_robustness(
    experiment_id: int,
    query: Annotated[RobustnessQuery, Query()],
    user: dict[str, Any] = Depends(
        require_permission("experiments:read")
    ),
) -> dict[str, Any]:
    """Return a read-only, explicitly post-hoc robustness diagnostic."""
    db_path = settings.abs_path(settings.EXPERIMENT_DB).resolve()
    if not db_path.is_file():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "experiment_database_unavailable",
                "message": "Experiment database is unavailable.",
            },
        )
    try:
        connection = await aiosqlite.connect(
            f"{db_path.as_uri()}?mode=ro",
            uri=True,
        )
    except aiosqlite.Error as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "experiment_database_unavailable",
                "message": "Experiment database cannot be opened read-only.",
            },
        ) from exc
    connection.row_factory = aiosqlite.Row
    try:
        experiment = await _owned_experiment(
            connection, experiment_id, user
        )
        report = await build_robustness_report(
            connection,
            experiment,
            **query.model_dump(),
        )
    except ResearchRobustnessError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail(),
        ) from exc
    finally:
        await connection.close()
    return {"data": report}


@router.post(
    "/experiments/{experiment_id}/rerun",
    response_model=ApiResponse[ResearchRerunResponse],
)
async def rerun_research_experiment(
    experiment_id: int,
    body: ResearchRerunBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
    registry: Any = Depends(get_strategy_registry),
    broker: Any = Depends(get_job_broker),
) -> dict[str, Any]:
    """Clone a completed run; validate environment now and data at execution."""
    db_path = settings.abs_path(settings.EXPERIMENT_DB)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    try:
        source = await _owned_experiment(connection, experiment_id, user)
    finally:
        await connection.close()
    if source["status"] != "completed":
        raise HTTPException(status_code=422, detail="只有已完成实验可以重跑")

    try:
        source_envelope = await load_run_manifest(db_path, experiment_id)
    except ManifestConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "manifest_integrity_failure",
                "differences": exc.differences,
            },
        ) from exc
    if source_envelope is None:
        raise HTTPException(status_code=409, detail="来源实验缺少运行清单")
    source_manifest = source_envelope["manifest"]
    eligibility = assess_experiment_eligibility(
        experiment_id=experiment_id,
        strategy_id=str(source["strategy_id"]),
        manifest_json=json.dumps(
            source_manifest, ensure_ascii=False, sort_keys=True
        ),
        manifest_hash=source_envelope["manifest_hash"],
        schema_version=source_envelope["schema_version"],
    )
    if not eligibility.eligible:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "legacy_experiment_rerun_forbidden",
                "message": "历史非 PIT 实验只能审计，不能创建精确重跑副本",
                "eligibility_code": eligibility.code,
            },
        )
    from backend.data.market_quality import (
        MarketDataQualityError,
        MarketDataQualitySnapshot,
    )

    try:
        source_quality = MarketDataQualitySnapshot.from_dict(
            source_manifest.get("market_data_quality", {})
        )
    except MarketDataQualityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "market_data_quality_evidence_invalid",
                "message": (
                    "The source experiment lacks verified market-data "
                    "quality evidence and cannot be replayed exactly."
                ),
            },
        ) from exc

    try:
        strategy = registry.create_strategy(source["strategy_id"])
        metadata = registry.get_metadata(source["strategy_id"])
    except KeyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "strategy_unavailable",
                "differences": [{
                    "field": "environment.strategy.strategy_id",
                    "expected": source["strategy_id"],
                    "actual": None,
                }],
            },
        ) from exc
    current_environment = capture_runtime_environment(strategy, metadata)
    environment_differences = compare_replay_environment(
        source_manifest,
        current_environment,
    )
    if environment_differences and not body.allow_environment_drift:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "environment_drift",
                "message": "当前代码或运行环境无法精确复现来源实验",
                "differences": environment_differences,
            },
        )

    replay_spec = {
        "source_manifest_hash": source_envelope["manifest_hash"],
        "dataset_digest": source_manifest["dataset"]["digest"],
        "universe_snapshot_hash": source_manifest["universe"][
            "snapshot_hash"
        ],
        "benchmark_sha256": source_manifest["benchmark"].get("sha256"),
        "market_data_quality_sha256": source_quality.content_sha256,
        "market_data_quality": source_quality.to_dict(),
        "environment_sha256": canonical_sha256(
            source_manifest["environment"]
        ),
        "allow_environment_drift": body.allow_environment_drift,
        "environment_differences": environment_differences,
    }
    pit_runtime = source_manifest.get("pit_runtime")
    if not (
        isinstance(pit_runtime, dict)
        and pit_runtime.get("verified") is True
        and pit_runtime.get("legacy_or_static_fallback_allowed") is False
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pit_replay_evidence_missing",
                "message": "来源实验不是 PIT-only 正式运行，禁止精确重跑或升级旧快照",
            },
        )
    replay_spec["pit_only_runtime_verified"] = True
    dataset_snapshot = source_manifest["dataset"].get("snapshot")
    if isinstance(dataset_snapshot, dict):
        replay_spec.update(
            {
                "dataset_snapshot": dataset_snapshot,
                "universe": source_manifest["universe"],
                "benchmark": source_manifest["benchmark"],
                "execution": source_manifest["execution"],
            }
        )
    source_run_spec: dict[str, Any] = {}
    if source["run_spec"]:
        try:
            candidate = json.loads(source["run_spec"])
            if isinstance(candidate, dict):
                source_run_spec = candidate
            else:
                raise HTTPException(
                    status_code=409,
                    detail="来源实验 run_spec 不是 JSON object",
                )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=409,
                detail="来源实验 run_spec JSON 已损坏",
            ) from None
    run_spec = {
        **source_run_spec,
        "research_replay": replay_spec,
    }

    new_experiment_id: int
    job_id: str | None = None
    owns_dispatch = False
    wait_for_dispatch = False
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    try:
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """
            SELECT * FROM research_rerun_requests
            WHERE user_id=? AND idempotency_key=?
            """,
            (user["id"], body.idempotency_key),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            if (
                int(existing["source_experiment_id"]) != experiment_id
                or bool(existing["allow_environment_drift"])
                != body.allow_environment_drift
            ):
                raise HTTPException(
                    status_code=409,
                    detail="幂等键已用于不同的重跑请求",
                )
            new_experiment_id = int(existing["new_experiment_id"])
            persisted_job = existing["job_uuid"]
            if persisted_job == _DISPATCHING_JOB:
                wait_for_dispatch = True
            elif persisted_job:
                job_id = str(persisted_job)
            else:
                cursor = await connection.execute(
                    """
                    UPDATE research_rerun_requests SET job_uuid=?
                    WHERE id=? AND job_uuid IS NULL
                    """,
                    (_DISPATCHING_JOB, existing["id"]),
                )
                owns_dispatch = cursor.rowcount == 1
        else:
            cursor = await connection.execute(
                """
                INSERT INTO experiments
                    (user_id, name, strategy_id, strategy_category,
                     pool_preset, pool_custom_codes, pool_industries,
                     train_start, train_end, test_start, test_end,
                     params, params_hash, mode, requires_training,
                     retrain_frequency, status, progress_pct,
                     progress_message, run_spec, source_experiment_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending', 0, '等待精确重跑', ?, ?)
                """,
                (
                    user["id"],
                    f"{source['name'] or source['strategy_id']} - 精确重跑",
                    source["strategy_id"],
                    source["strategy_category"],
                    source["pool_preset"],
                    source["pool_custom_codes"],
                    source["pool_industries"],
                    source["train_start"],
                    source["train_end"],
                    source["test_start"],
                    source["test_end"],
                    source["params"],
                    source["params_hash"],
                    source["mode"],
                    source["requires_training"],
                    source["retrain_frequency"],
                    json.dumps(run_spec, ensure_ascii=False, sort_keys=True),
                    experiment_id,
                ),
            )
            new_experiment_id = int(cursor.lastrowid)
            await connection.execute(
                """
                INSERT INTO research_rerun_requests
                    (user_id, idempotency_key, source_experiment_id,
                     new_experiment_id, allow_environment_drift,
                     environment_drift_json, job_uuid, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    body.idempotency_key,
                    experiment_id,
                    new_experiment_id,
                    1 if body.allow_environment_drift else 0,
                    json.dumps(
                        environment_differences,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    _DISPATCHING_JOB,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            owns_dispatch = True
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise
    finally:
        await connection.close()

    if wait_for_dispatch:
        job_id = await _await_dispatched_job(
            db_path,
            user_id=int(user["id"]),
            idempotency_key=body.idempotency_key,
            experiment_id=new_experiment_id,
        )

    if owns_dispatch:
        try:
            job_id = await broker.submit_job(
                job_type="backtest",
                params={
                    "experiment_id": new_experiment_id,
                    "strategy_id": source["strategy_id"],
                    "user_id": user["id"],
                    "research_replay": replay_spec,
                    "pool_preset": source["pool_preset"],
                    "pool_custom_codes": source["pool_custom_codes"],
                },
                user_id=user["id"],
                display_name=f"精确重跑 · 实验 #{experiment_id}",
                resource_type="experiment",
                resource_id=new_experiment_id,
            )
        except Exception as exc:
            async with aiosqlite.connect(str(db_path)) as failed_db:
                await failed_db.execute(
                    """
                    UPDATE research_rerun_requests SET job_uuid=NULL
                    WHERE user_id=? AND idempotency_key=? AND job_uuid=?
                    """,
                    (
                        user["id"],
                        body.idempotency_key,
                        _DISPATCHING_JOB,
                    ),
                )
                await failed_db.execute(
                    """
                    UPDATE experiments
                    SET status='failed', error_log=?,
                        progress_message='重跑任务提交失败'
                    WHERE id=?
                    """,
                    (
                        f"重跑任务提交失败: {type(exc).__name__}",
                        new_experiment_id,
                    ),
                )
                await failed_db.commit()
            raise HTTPException(
                status_code=503,
                detail="重跑任务提交失败，可使用同一幂等键重试",
            ) from exc
        async with aiosqlite.connect(str(db_path)) as update_db:
            await update_db.execute(
                """
                UPDATE research_rerun_requests SET job_uuid=?
                WHERE user_id=? AND idempotency_key=? AND job_uuid=?
                """,
                (
                    job_id,
                    user["id"],
                    body.idempotency_key,
                    _DISPATCHING_JOB,
                ),
            )
            await update_db.execute(
                """
                UPDATE experiments SET status='pending', error_log=NULL,
                    progress_message='等待精确重跑'
                WHERE id=?
                """,
                (new_experiment_id,),
            )
            await update_db.commit()

    if job_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_dispatch_unavailable",
                "message": "重跑投递状态不可用",
            },
        )

    replay_mode = (
        "environment_drift_allowed"
        if environment_differences
        else "exact_pending_input_validation"
    )
    return {
        "data": {
            "experiment_id": new_experiment_id,
            "job_id": job_id,
            "source_experiment_id": experiment_id,
            "replay_mode": replay_mode,
            "environment_differences": environment_differences,
        }
    }
