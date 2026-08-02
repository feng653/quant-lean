"""Read-only API for strategy return-correlation diagnostics."""

from __future__ import annotations

from collections import defaultdict
import hmac
import json
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.api.schemas import ApiResponse
from backend.config import settings
from backend.dependencies import get_strategy_registry, require_permission
from backend.services.portfolio_candidates import build_portfolio_candidates
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    ManifestError,
    canonical_sha256,
)
from backend.services.strategy_correlation import analyze_strategy_correlations
from backend.strategies.base import StrategyCategory


router = APIRouter(
    prefix="/api/research/strategy-correlation",
    tags=["Research"],
)


class PortfolioCandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_ids: list[int] = Field(min_length=3, max_length=20)
    method: Literal["pearson", "spearman"] = "pearson"
    min_observations: int = Field(default=60, ge=10, le=2520)
    tail_fraction: float = Field(default=0.1, ge=0.01, le=0.25)
    max_components: int = Field(default=6, ge=3, le=8)
    max_pair_correlation: float = Field(default=0.8, ge=-1, le=1)
    max_holding_overlap: float = Field(default=0.6, ge=0, le=1)
    max_weight: float = Field(default=0.4, ge=0.2, le=0.5)


def _pit_manifest_hash(row: dict[str, Any]) -> str:
    """Verify a source experiment is bound to an intact PIT-only manifest."""
    try:
        manifest = json.loads(row["manifest_json"])
        manifest_hash = str(row["manifest_hash"])
        stored_params = json.loads(row["params"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pit_source_manifest_missing_or_invalid",
                "message": "组合研究仅接受具有完整 PIT 运行清单的实验。",
            },
        ) from exc
    try:
        actual_hash = canonical_sha256(manifest)
    except (ManifestError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pit_source_manifest_integrity_invalid",
                "message": "来源实验运行清单完整性校验失败。",
            },
        ) from exc
    if (
        row.get("manifest_schema_version") != RUN_MANIFEST_SCHEMA
        or not isinstance(manifest, dict)
        or manifest.get("schema_version") != RUN_MANIFEST_SCHEMA
        or not hmac.compare_digest(actual_hash, manifest_hash)
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pit_source_manifest_integrity_invalid",
                "message": "来源实验运行清单完整性校验失败。",
            },
        )
    experiment = manifest.get("experiment")
    parameters = manifest.get("parameters")
    universe = manifest.get("universe")
    runtime = manifest.get("pit_runtime")
    execution = manifest.get("execution")
    binding = execution.get("canonical_price_binding") if isinstance(execution, dict) else None
    timeline = universe.get("timeline_identity") if isinstance(universe, dict) else None
    manifest_user_matches = (
        isinstance(row.get("manifest_user_id"), int)
        and int(row["manifest_user_id"]) == int(row["user_id"])
    )
    identity_valid = (
        isinstance(experiment, dict)
        and experiment.get("experiment_id") == int(row["id"])
        and experiment.get("strategy_id") == row["strategy_id"]
        and experiment.get("data_access_policy") == "cache_only"
        and manifest_user_matches
        and isinstance(stored_params, dict)
        and isinstance(parameters, dict)
        and parameters.get("canonical") == stored_params
        and parameters.get("sha256") == canonical_sha256(stored_params)
    )
    pit_valid = (
        isinstance(universe, dict)
        and universe.get("point_in_time") is True
        and isinstance(timeline, dict)
        and isinstance(timeline.get("timeline_hash"), str)
        and timeline.get("timeline_hash")
        and isinstance(runtime, dict)
        and runtime.get("schema_version") == "pit-runtime-binding/v1"
        and runtime.get("verified") is True
        and runtime.get("network_accessed") is False
        and runtime.get("legacy_or_static_fallback_allowed") is False
        and runtime.get("timeline_hash") == timeline.get("timeline_hash")
        and isinstance(binding, dict)
        and runtime.get("canonical_price_binding_id") == binding.get("binding_id")
        and runtime.get("canonical_price_binding_digest") == binding.get("binding_digest")
    )
    if not identity_valid or not pit_valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pit_source_evidence_incomplete",
                "message": "来源实验没有通过 PIT-only 时间线与价格绑定校验。",
            },
        )
    return manifest_hash


async def _load_analysis_inputs(
    experiment_ids: list[int],
    user: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
    dict[int, str],
]:
    ordered_ids = list(dict.fromkeys(experiment_ids))
    if len(ordered_ids) != len(experiment_ids):
        raise HTTPException(status_code=422, detail="实验 ID 不能重复")
    placeholders = ",".join("?" for _ in ordered_ids)
    ownership = "" if user.get("is_admin") else " AND e.user_id = ?"
    params: list[Any] = [*ordered_ids]
    if not user.get("is_admin"):
        params.append(user["id"])

    db_path = settings.abs_path(settings.EXPERIMENT_DB).resolve()
    if not db_path.is_file():
        raise HTTPException(status_code=503, detail="实验数据库不可用")
    try:
        conn = await aiosqlite.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = aiosqlite.Row
        try:
            cursor = await conn.execute(
                f"""
                SELECT e.id, e.user_id, e.name, e.strategy_id,
                       e.strategy_category, e.requires_training, e.params,
                       e.status, e.pool_preset, e.test_start, e.test_end,
                       rm.user_id AS manifest_user_id,
                       rm.schema_version AS manifest_schema_version,
                       rm.manifest_json, rm.manifest_hash
                FROM experiments e
                LEFT JOIN research_run_manifests rm ON rm.experiment_id=e.id
                WHERE e.id IN ({placeholders}){ownership}
                """,
                params,
            )
            rows = await cursor.fetchall()
            by_id = {int(row["id"]): dict(row) for row in rows}
            if len(by_id) != len(ordered_ids):
                raise HTTPException(
                    status_code=404,
                    detail="一个或多个实验不存在或当前账号无权访问",
                )
            incomplete = [
                experiment_id
                for experiment_id in ordered_ids
                if by_id[experiment_id]["status"] != "completed"
            ]
            if incomplete:
                raise HTTPException(
                    status_code=422,
                    detail=f"仅支持已完成实验，未完成: {incomplete}",
                )
            manifest_hashes = {
                experiment_id: _pit_manifest_hash(by_id[experiment_id])
                for experiment_id in ordered_ids
            }
            cursor = await conn.execute(
                f"""
                SELECT experiment_id, date, equity
                FROM equity_curve
                WHERE experiment_id IN ({placeholders})
                ORDER BY experiment_id, date, id
                """,
                ordered_ids,
            )
            equity = await cursor.fetchall()
            table_cursor = await conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='trade_log'
                """
            )
            if await table_cursor.fetchone() is not None:
                cursor = await conn.execute(
                    f"""
                    SELECT experiment_id, date, code, action, shares
                    FROM trade_log
                    WHERE experiment_id IN ({placeholders})
                    ORDER BY experiment_id, date, id
                    """,
                    ordered_ids,
                )
                trades = await cursor.fetchall()
            else:
                trades = []
        finally:
            await conn.close()
    except HTTPException:
        raise
    except aiosqlite.Error as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "experiment_database_unavailable",
                "message": "实验数据库暂不可用，请稍后重试。",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "strategy_correlation_failed",
                "message": "策略相关性分析暂不可用。",
            },
        ) from exc

    experiment_rows: list[dict[str, Any]] = []
    for experiment_id in ordered_ids:
        item = by_id[experiment_id]
        try:
            item["params"] = json.loads(item["params"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "source_experiment_params_invalid",
                    "message": "来源实验参数无法复现。",
                },
            ) from exc
        if not isinstance(item["params"], dict):
            raise HTTPException(status_code=422, detail="来源实验参数必须是对象")
        experiment_rows.append(item)
    equity_by_experiment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in equity:
        equity_by_experiment[int(row["experiment_id"])].append(dict(row))
    trades_by_experiment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        trades_by_experiment[int(row["experiment_id"])].append(dict(row))
    return experiment_rows, equity_by_experiment, trades_by_experiment, manifest_hashes


@router.get("", response_model=ApiResponse[dict[str, Any]])
async def get_strategy_correlation(
    experiment_ids: list[int] = Query(..., min_length=2, max_length=20),
    method: Literal["pearson", "spearman"] = Query("pearson"),
    min_observations: int = Query(60, ge=10, le=2520),
    weights: list[float] | None = Query(default=None),
    tail_fraction: float = Query(default=0.1, ge=0.01, le=0.25),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """Compare aligned daily returns for two to twenty completed experiments."""
    if weights is not None and len(weights) != len(experiment_ids):
        raise HTTPException(
            status_code=422,
            detail="权重数量必须与实验数量一致",
        )
    experiments, equity_by_experiment, trades_by_experiment, manifest_hashes = (
        await _load_analysis_inputs(experiment_ids, user)
    )
    try:
        report = analyze_strategy_correlations(
            experiments,
            equity_by_experiment,
            method=method,
            min_observations=min_observations,
            trade_rows=trades_by_experiment,
            weights=weights,
            tail_fraction=tail_fraction,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report["pit_evidence"] = {
        "verified": True,
        "source_run_manifest_hashes": [
            manifest_hashes[int(item["id"])] for item in experiments
        ],
    }
    return {"data": report}


@router.post("/portfolio-candidates", response_model=ApiResponse[dict[str, Any]])
async def create_portfolio_candidates(
    body: PortfolioCandidateBody,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """Create five deterministic draft definitions without publishing them."""
    experiments, equity, trades, manifest_hashes = await _load_analysis_inputs(
        body.experiment_ids,
        user,
    )
    registry = get_strategy_registry()
    for item in experiments:
        try:
            metadata = registry.get_metadata(str(item["strategy_id"]))
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="来源策略未注册") from exc
        if (
            metadata.category
            in {
                StrategyCategory.ML,
                StrategyCategory.COMPOSITE,
                StrategyCategory.PORTFOLIO,
            }
            or metadata.requires_training
            or item.get("strategy_category") != metadata.category.value
            or bool(item.get("requires_training"))
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "atomic_non_ml_source_required",
                    "message": "候选组合只接受非机器学习单策略实验。",
                },
            )
        valid, validation_error = registry.validate_params(
            str(item["strategy_id"]),
            item["params"],
        )
        if not valid:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "source_experiment_params_invalid",
                    "message": f"来源实验参数无法按当前策略复现: {validation_error}",
                },
            )
    try:
        report = analyze_strategy_correlations(
            experiments,
            equity,
            method=body.method,
            min_observations=body.min_observations,
            trade_rows=trades,
            tail_fraction=body.tail_fraction,
        )
        result = build_portfolio_candidates(
            experiments=experiments,
            equity_rows=equity,
            correlation_report=report,
            manifest_hashes=manifest_hashes,
            min_observations=body.min_observations,
            tail_fraction=body.tail_fraction,
            max_components=body.max_components,
            max_pair_correlation=body.max_pair_correlation,
            max_holding_overlap=body.max_holding_overlap,
            max_weight=body.max_weight,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": result}
