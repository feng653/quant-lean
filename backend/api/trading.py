"""交易 API — 模拟盘/实盘交易管理（部署、组合、信号、持仓、订单）."""

from __future__ import annotations

import json as _json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.config import settings
from backend.dependencies import (
    get_db,
    get_job_broker,
    require_permission,
)
from backend.api.schemas import (
    ApiResponse,
    DeploymentResponse,
    IdResponse,
    OrderResponse,
    Page,
    PortfolioResponse,
    PositionResponse,
    SignalResponse,
)
from backend.api.storage_paths import redact_model_storage_paths
from backend.services.model_lifecycle import (
    next_retrain_at,
    parse_json_object,
    public_failure,
)

router = APIRouter(prefix="/api/trading", tags=["Trading"])


def _derive_retraining_contract(
    metadata: Any,
    requested_frequency: str | None,
) -> tuple[bool, str]:
    """Derive deployment retraining from immutable strategy metadata."""
    metadata_frequency = (
        metadata.retrain_frequency.value
        if hasattr(metadata.retrain_frequency, "value")
        else str(metadata.retrain_frequency)
    )
    expected_frequency = (
        metadata_frequency
        if metadata.requires_training and metadata_frequency != "never"
        else "never"
    )
    if (
        requested_frequency is not None
        and requested_frequency != expected_frequency
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "部署重训练频率必须与策略元数据一致: "
                f"expected={expected_frequency}, requested={requested_frequency}"
            ),
        )
    return expected_frequency != "never", expected_frequency


# ── Pydantic 请求体 ──────────────────────────────────────────────────────────

class CreateDeploymentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    display_name: str | None = None
    source_experiment_id: int | None = None
    source_model_artifact_id: int | None = None
    research_promotion_id: int | None = Field(default=None, ge=1)
    params: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["batch", "realtime"] = "batch"
    retrain_frequency: Literal[
        "daily", "weekly", "monthly", "quarterly", "never"
    ] | None = None
    position_mode: str = "equal_weight"
    position_config: dict[str, Any] | None = None
    status: Literal["active", "paused", "stopped"] = "active"
    portfolio_id: int | None = Field(default=None, ge=1)
    target_weight_bps: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_portfolio_target(self) -> CreateDeploymentBody:
        if (self.portfolio_id is None) != (self.target_weight_bps is None):
            raise ValueError("portfolio_id and target_weight_bps must be provided together")
        if self.portfolio_id is not None and self.status != "active":
            raise ValueError("a portfolio-targeted deployment must be active")
        return self


class UpdateDeploymentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "paused", "stopped"] | None = None
    position_config: dict[str, Any] | None = None
    status_tags: list[str] | None = None
    user_notes: str | None = None
    display_name: str | None = None
    research_promotion_id: int | None = Field(default=None, ge=1)


class AllocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: int
    target_weight_bps: int | None = None
    weight: float | None = None
    min_weight_bps: int = 0
    max_weight_bps: int = 10_000
    locked: bool = False
    risk_budget_bps: int | None = None


class CreatePortfolioBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    total_capital: float = Field(gt=0)
    rebalance_frequency: Literal["daily", "weekly", "monthly", "quarterly"] = "monthly"
    allocations: list[AllocationBody] = Field(default_factory=list)


class UpdatePortfolioBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    total_capital: float | None = Field(default=None, gt=0)
    rebalance_frequency: Literal["daily", "weekly", "monthly", "quarterly"] | None = None
    allocations: list[AllocationBody] | None = None
    status: str | None = None


class AllocationRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocations: list[AllocationBody]
    effective_date: str | None = None


class SimulationRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    portfolio_id: int | None = Field(default=None, ge=1)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is not None:
            from datetime import date

            date.fromisoformat(value)
        return value


class SimulationBackfillBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: str
    end_date: str
    portfolio_id: int | None = Field(default=None, ge=1)
    restart: bool = False

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_dates(cls, value: str) -> str:
        from datetime import date

        date.fromisoformat(value)
        return value


def _allocation_dicts(items: list[AllocationBody]) -> list[dict[str, Any]]:
    return [item.model_dump(exclude_none=True) for item in items]


async def _validate_allocation_deployments(
    conn,
    allocations: list[dict[str, Any]],
    user: dict[str, Any],
) -> None:
    deployment_ids = [int(item["deployment_id"]) for item in allocations]
    if not deployment_ids:
        return
    placeholders = ",".join("?" for _ in deployment_ids)
    query = f"SELECT id, user_id, status FROM deployments WHERE id IN ({placeholders})"
    cursor = await conn.execute(query, deployment_ids)
    rows = await cursor.fetchall()
    found = {int(row["id"]): row for row in rows}
    missing = sorted(set(deployment_ids) - set(found))
    if missing:
        raise HTTPException(status_code=400, detail=f"部署不存在: {missing}")
    for deployment_id, row in found.items():
        if not user.get("is_admin") and int(row["user_id"]) != int(user["id"]):
            raise HTTPException(status_code=403, detail=f"无权使用部署: {deployment_id}")
        if row["status"] != "active":
            raise HTTPException(status_code=400, detail=f"部署未启用: {deployment_id}")


async def _replace_portfolio_allocations(
    conn,
    portfolio_id: int,
    revision: int,
    allocations: list[dict[str, Any]],
) -> None:
    await conn.execute(
        "DELETE FROM portfolio_allocations WHERE portfolio_id = ?",
        (portfolio_id,),
    )
    if allocations:
        await conn.executemany(
            """
            INSERT INTO portfolio_allocations
                (portfolio_id, deployment_id, target_weight_bps,
                 min_weight_bps, max_weight_bps, locked,
                 risk_budget_bps, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    portfolio_id,
                    item["deployment_id"],
                    item["target_weight_bps"],
                    item["min_weight_bps"],
                    item["max_weight_bps"],
                    1 if item["locked"] else 0,
                    item["risk_budget_bps"],
                    revision,
                )
                for item in allocations
            ],
        )


# ═══════════════════════════════════════════════════════════════════════════
# 部署
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/deployments", response_model=ApiResponse[list[DeploymentResponse]])
async def list_deployments(
    status: str | None = Query(None),
    strategy_id: str | None = Query(None),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """获取部署列表。"""
    conditions: list[str] = []
    params: list[Any] = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if strategy_id:
        conditions.append("strategy_id = ?")
        params.append(strategy_id)

    # FIXED: reviewer issue #11 — 增加 user_id 过滤（非 admin 只能看自己的）
    if not user.get("is_admin"):
        conditions.append("user_id = ?")
        params.append(user["id"])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async for conn in get_db("trading_sim"):
            cursor = await conn.execute(
                f"SELECT * FROM deployments {where} ORDER BY created_at DESC",
                params,
            )
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询部署列表失败: {e}")

    items: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["params"] = _json.loads(d["params"]) if d.get("params") else {}
        d["position_config"] = _json.loads(d["position_config"]) if d.get("position_config") else {}
        d["status_tags"] = _json.loads(d["status_tags"]) if d.get("status_tags") else []
        from backend.services.experiment_eligibility import (
            PaperRiskBindingError,
            verify_paper_risk_binding,
        )

        try:
            d["research_risk_snapshot"] = verify_paper_risk_binding(d)
            d["risk_binding_integrity"] = (
                True if d["research_risk_snapshot"] is not None else None
            )
            d["risk_binding_error"] = None
        except PaperRiskBindingError:
            d["research_risk_snapshot"] = None
            d["risk_binding_integrity"] = False
            d["risk_binding_error"] = "paper_research_risk_binding_invalid"
        items.append(d)

    return {"data": items}


@router.post("/deployments", response_model=ApiResponse[IdResponse])
async def create_deployment(
    body: CreateDeploymentBody,
    user: dict[str, Any] = Depends(require_permission("trading:deploy")),
) -> dict[str, Any]:
    """创建部署（从实验发布或手动配置）。"""
    # 获取策略元数据
    from backend.dependencies import get_strategy_registry
    registry = get_strategy_registry()
    try:
        meta = registry.get_metadata(body.strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {body.strategy_id}")
    supported_modes = {
        item.value if hasattr(item, "value") else str(item)
        for item in meta.supported_modes
    }
    if body.mode not in supported_modes:
        raise HTTPException(
            status_code=422,
            detail=f"策略不支持运行模式 {body.mode}",
        )
    requires_retraining, retrain_frequency = _derive_retraining_contract(
        meta,
        body.retrain_frequency,
    )
    if (
        body.source_model_artifact_id is not None
        and body.source_experiment_id is None
    ):
        raise HTTPException(
            status_code=422,
            detail="模型产物必须与明确的来源实验一起发布",
        )

    if body.status == "active" and body.source_experiment_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "paper_source_experiment_required",
                "message": (
                    "Active paper deployments require a completed source "
                    "experiment so data generation, window and warnings can "
                    "be bound immutably"
                ),
            },
        )
    if (
        body.research_promotion_id is not None
        and body.source_experiment_id is None
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "promotion_experiment_required",
                "message": (
                    "Research promotion must be bound to its source experiment"
                ),
            },
        )

    source_snapshot: dict[str, Any] = {}
    source_manifest_snapshot: dict[str, Any] = {}
    source_eligibility: Any = None
    source_artifact_snapshot: dict[str, Any] = {}
    source_model_artifact_id = body.source_model_artifact_id
    if body.source_experiment_id is not None:
        async for experiment_conn in get_db("experiment"):
            cursor = await experiment_conn.execute(
                "SELECT * FROM experiments WHERE id = ? AND user_id = ?",
                (body.source_experiment_id, user["id"]),
            )
            source_row = await cursor.fetchone()
            if source_row is None:
                raise HTTPException(status_code=404, detail="来源实验不存在或无权访问")
            source_snapshot = dict(source_row)
            if source_snapshot["status"] != "completed":
                raise HTTPException(status_code=400, detail="只有已完成实验可以发布部署")
            if source_snapshot["strategy_id"] != body.strategy_id:
                raise HTTPException(status_code=400, detail="部署策略与来源实验不一致")
            from backend.services.experiment_eligibility import (
                load_experiment_eligibility,
            )

            eligibility = await load_experiment_eligibility(
                experiment_conn,
                experiment_id=int(body.source_experiment_id),
                strategy_id=body.strategy_id,
            )
            if not eligibility.eligible:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "legacy_experiment_deployment_forbidden",
                        "message": "历史非 PIT 实验不能进入部署或模拟交易链路",
                        "eligibility_code": eligibility.code,
                    },
                )
            source_eligibility = eligibility
            cursor = await experiment_conn.execute(
                "SELECT COUNT(*) FROM equity_curve WHERE experiment_id = ?",
                (body.source_experiment_id,),
            )
            if (await cursor.fetchone())[0] == 0:
                raise HTTPException(status_code=400, detail="来源实验缺少净值产物，不能发布")
            if source_model_artifact_id is not None:
                cursor = await experiment_conn.execute(
                    """
                    SELECT * FROM model_artifacts
                    WHERE id = ? AND experiment_id = ? AND strategy_id = ?
                    """,
                    (
                        source_model_artifact_id,
                        body.source_experiment_id,
                        body.strategy_id,
                    ),
                )
                artifact_row = await cursor.fetchone()
                if artifact_row is None:
                    raise HTTPException(status_code=400, detail="模型产物不属于来源实验")
                source_artifact_snapshot = dict(artifact_row)
            elif meta.requires_training:
                cursor = await experiment_conn.execute(
                    """
                    SELECT * FROM model_artifacts
                    WHERE experiment_id = ? AND strategy_id = ?
                    ORDER BY is_latest DESC, model_version DESC, id DESC
                    LIMIT 1
                    """,
                    (body.source_experiment_id, body.strategy_id),
                )
                artifact_row = await cursor.fetchone()
                if artifact_row is None:
                    raise HTTPException(
                        status_code=400,
                        detail="训练型策略的来源实验缺少模型产物，不能发布",
                    )
                source_model_artifact_id = int(artifact_row["id"])
                source_artifact_snapshot = dict(artifact_row)

    import hashlib
    deployment_params = body.params or (
        _json.loads(source_snapshot["params"]) if source_snapshot else {}
    )
    is_valid, validation_error = registry.validate_params(body.strategy_id, deployment_params)
    if not is_valid:
        raise HTTPException(status_code=422, detail=f"策略参数无效: {validation_error}")
    params_str = _json.dumps(deployment_params, ensure_ascii=False, sort_keys=True)
    params_hash = hashlib.md5(params_str.encode()).hexdigest()
    if source_snapshot and (
        source_snapshot.get("params_hash") != params_hash
        or (
            source_artifact_snapshot
            and source_artifact_snapshot.get("params_hash") != params_hash
        )
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "deployment parameters must match the source model "
                "training parameters"
            ),
        )
    if source_artifact_snapshot:
        from backend.services.model_artifacts import (
            ModelArtifactIntegrityError,
            verify_source_experiment_model,
        )

        try:
            await verify_source_experiment_model(
                {
                    "user_id": user["id"],
                    "strategy_id": body.strategy_id,
                    "params": params_str,
                    "params_hash": params_hash,
                    "source_experiment_id": body.source_experiment_id,
                    "source_model_artifact_id": source_model_artifact_id,
                }
            )
        except ModelArtifactIntegrityError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"source model artifact verification failed: {exc}",
            ) from exc
    if source_snapshot:
        manifest_row = None
        async for experiment_conn in get_db("experiment"):
            cursor = await experiment_conn.execute(
                """
                SELECT manifest_json, manifest_hash
                FROM research_run_manifests
                WHERE experiment_id = ?
                """,
                (body.source_experiment_id,),
            )
            manifest_row = await cursor.fetchone()
        if manifest_row is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "paper_source_manifest_missing",
                    "message": "来源实验缺少可复核运行清单，不能绑定模拟版本",
                },
            )
        try:
            source_manifest_snapshot = _json.loads(
                manifest_row["manifest_json"]
            )
        except (_json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "paper_source_manifest_invalid",
                    "message": "来源实验运行清单不可读取",
                },
            ) from exc
        source_manifest_snapshot["_manifest_hash"] = manifest_row[
            "manifest_hash"
        ]
    promotion_binding: dict[str, Any] | None = None
    if body.research_promotion_id is not None:
        from backend.services.deployment_promotion import (
            DeploymentPromotionError,
            resolve_deployment_promotion,
        )

        try:
            promotion_binding = await resolve_deployment_promotion(
                promotion_id=body.research_promotion_id,
                owner_user_id=int(user["id"]),
                experiment_id=int(body.source_experiment_id),
                strategy_id=body.strategy_id,
                params_hash=params_hash,
                model_artifact_id=source_model_artifact_id,
            )
        except DeploymentPromotionError as exc:
            raise HTTPException(
                status_code=409,
                detail=exc.detail(),
            ) from exc
    research_risk_snapshot: dict[str, Any] | None = None
    research_risk_snapshot_hash: str | None = None
    research_generation_id: str | None = None
    research_source_id: str | None = None
    research_window_start: str | None = None
    research_window_end: str | None = None
    if source_manifest_snapshot:
        quality = source_manifest_snapshot.get("market_data_quality")
        windows = source_manifest_snapshot.get("windows")
        trust = source_manifest_snapshot.get("research_trust")
        runtime_binding = trust.get("runtime_binding") if isinstance(trust, dict) else None
        source_ids = (
            runtime_binding.get("source_ids")
            if isinstance(runtime_binding, dict)
            else None
        )
        research_generation_id = str(
            (
                runtime_binding.get("generation_id")
                if isinstance(runtime_binding, dict)
                else None
            )
            or ""
        ) or None
        research_source_id = (
            ",".join(sorted(str(item) for item in source_ids))
            if isinstance(source_ids, list) and source_ids
            else str(
                (
                    quality.get("source", {}).get("provider")
                    if isinstance(quality, dict)
                    and isinstance(quality.get("source"), dict)
                    else ""
                )
            )
            or None
        )
        research_window_start = (
            str(windows.get("test_start"))
            if isinstance(windows, dict) and windows.get("test_start")
            else source_snapshot.get("test_start")
        )
        research_window_end = (
            str(windows.get("test_end"))
            if isinstance(windows, dict) and windows.get("test_end")
            else source_snapshot.get("test_end")
        )
        warnings = {
            str(item)
            for item in [
                *list(source_manifest_snapshot.get("research_risk_warnings") or []),
                *list(getattr(source_eligibility, "warnings", ()) or ()),
            ]
            if str(item).strip()
        }
        if promotion_binding is None:
            warnings.add("manual_research_approval_missing")
        if research_generation_id is None:
            warnings.add("research_generation_id_missing")
        warnings.add("paper_only_live_trading_not_eligible")
        research_risk_snapshot = {
            "schema_version": "paper-deployment-research-risk/v1",
            "source_experiment_id": int(body.source_experiment_id),
            "source_manifest_hash": source_manifest_snapshot["_manifest_hash"],
            "eligibility_code": source_eligibility.code,
            "warnings": sorted(warnings),
            "warning_severity": "high" if warnings else "none",
            "research_generation_id": research_generation_id,
            "research_source_id": research_source_id,
            "window": {
                "start": research_window_start,
                "end": research_window_end,
            },
            "research_promotion_bound": promotion_binding is not None,
            "paper_eligible": True,
            "live_eligible": False,
        }
        canonical_snapshot = _json.dumps(
            research_risk_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        research_risk_snapshot_hash = hashlib.sha256(
            canonical_snapshot.encode("utf-8")
        ).hexdigest()
    category = meta.category.value if hasattr(meta.category, "value") else str(meta.category)
    pos_config_str = _json.dumps(body.position_config, ensure_ascii=False) if body.position_config else None

    try:
        async for conn in get_db("trading_sim"):
            normalized_allocations: list[dict[str, Any]] | None = None
            allocation_validation: dict[str, Any] | None = None
            portfolio_revision: int | None = None
            if body.portfolio_id is not None:
                if (
                    not user.get("is_admin")
                    and "trading:rebalance" not in user.get("permissions", [])
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="需要权限: trading:rebalance",
                    )
                portfolio_query = "SELECT * FROM portfolios WHERE id = ?"
                portfolio_params: list[Any] = [body.portfolio_id]
                if not user.get("is_admin"):
                    portfolio_query += " AND user_id = ?"
                    portfolio_params.append(user["id"])
                cursor = await conn.execute(portfolio_query, portfolio_params)
                portfolio_row = await cursor.fetchone()
                if portfolio_row is None:
                    raise HTTPException(
                        status_code=404,
                        detail="目标模拟盘不存在或无权访问",
                    )
                if portfolio_row["status"] != "active":
                    raise HTTPException(status_code=409, detail="目标模拟盘未启用")

                cursor = await conn.execute(
                    """
                    SELECT deployment_id, target_weight_bps, min_weight_bps,
                           max_weight_bps, locked, risk_budget_bps
                    FROM portfolio_allocations
                    WHERE portfolio_id = ?
                    ORDER BY deployment_id
                    """,
                    (body.portfolio_id,),
                )
                existing_allocations = [dict(row) for row in await cursor.fetchall()]
                from backend.services.allocations import canonicalize_allocations

                normalized_allocations, allocation_validation = canonicalize_allocations(
                    [
                        *existing_allocations,
                        {
                            "deployment_id": 0,
                            "target_weight_bps": body.target_weight_bps,
                            "min_weight_bps": 0,
                            "max_weight_bps": 10_000,
                            "locked": False,
                            "risk_budget_bps": None,
                        },
                    ],
                    float(portfolio_row["total_capital"]),
                )
                if not allocation_validation["valid"]:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "allocation_errors": allocation_validation["errors"],
                        },
                    )
                cursor = await conn.execute(
                    """
                    SELECT COALESCE(MAX(revision), 0) + 1
                    FROM portfolio_versions
                    WHERE portfolio_id = ?
                    """,
                    (body.portfolio_id,),
                )
                portfolio_revision = int((await cursor.fetchone())[0])

            await conn.execute("BEGIN")
            cursor = await conn.execute(
                """
                INSERT INTO deployments
                    (user_id, strategy_id, strategy_category, display_name,
                     params, params_hash, mode,
                     source_experiment_id, source_model_artifact_id,
                     research_promotion_id, promotion_version,
                     promotion_report_id, promotion_report_hash,
                     promotion_manifest_hash, promotion_model_artifact_id,
                     promotion_model_sha256, promotion_evidence_hash,
                     promotion_binding_hash,
                     research_risk_snapshot, research_risk_snapshot_hash,
                     research_generation_id, research_source_id,
                     research_window_start, research_window_end,
                     requires_retraining, retrain_frequency,
                     position_mode, position_config, status,
                     pool_preset, pool_custom_codes, pool_industries, data_version)
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    user["id"],
                    body.strategy_id,
                    category,
                    body.display_name or meta.display_name,
                    params_str,
                    params_hash,
                    body.mode,
                    body.source_experiment_id,
                    source_model_artifact_id,
                    (
                        promotion_binding["promotion_id"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["promotion_version"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["report_id"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["report_hash"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["manifest_hash"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["model_artifact_id"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["model_sha256"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["model_evidence_hash"]
                        if promotion_binding
                        else None
                    ),
                    (
                        promotion_binding["binding_hash"]
                        if promotion_binding
                        else None
                    ),
                    (
                        _json.dumps(
                            research_risk_snapshot,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if research_risk_snapshot is not None
                        else None
                    ),
                    research_risk_snapshot_hash,
                    research_generation_id,
                    research_source_id,
                    research_window_start,
                    research_window_end,
                    1 if requires_retraining else 0,
                    retrain_frequency,
                    body.position_mode,
                    pos_config_str,
                    body.status,
                    source_snapshot.get("pool_preset"),
                    source_snapshot.get("pool_custom_codes"),
                    source_snapshot.get("pool_industries"),
                    source_snapshot.get("data_version"),
                ),
            )
            deployment_id = cursor.lastrowid
            if (
                body.portfolio_id is not None
                and normalized_allocations is not None
                and allocation_validation is not None
                and portfolio_revision is not None
            ):
                normalized_allocations[-1]["deployment_id"] = deployment_id
                allocations_str = _json.dumps(
                    normalized_allocations,
                    ensure_ascii=False,
                )
                await _replace_portfolio_allocations(
                    conn,
                    body.portfolio_id,
                    portfolio_revision,
                    normalized_allocations,
                )
                await conn.execute(
                    """
                    UPDATE portfolios
                    SET allocations = ?, current_revision = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (allocations_str, portfolio_revision, body.portfolio_id),
                )
                await conn.execute(
                    """
                    UPDATE portfolio_versions
                    SET status = 'archived'
                    WHERE portfolio_id = ? AND status = 'published'
                    """,
                    (body.portfolio_id,),
                )
                await conn.execute(
                    """
                    INSERT INTO portfolio_versions
                        (portfolio_id, user_id, revision, allocations,
                         validation_result, status, effective_date, published_at)
                    VALUES (?, ?, ?, ?, ?, 'published', date('now'), datetime('now'))
                    """,
                    (
                        body.portfolio_id,
                        user["id"],
                        portfolio_revision,
                        allocations_str,
                        _json.dumps(allocation_validation, ensure_ascii=False),
                    ),
                )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建部署失败: {e}")

    return {
        "data": {
            "deployment_id": deployment_id,
            "portfolio_id": body.portfolio_id,
            "revision": portfolio_revision,
            "research_risk_snapshot": research_risk_snapshot,
            "research_risk_snapshot_hash": research_risk_snapshot_hash,
        }
    }


@router.put("/deployments/{deployment_id}")
async def update_deployment(
    deployment_id: int,
    body: UpdateDeploymentBody,
    user: dict[str, Any] = Depends(require_permission("trading:deploy")),
) -> dict[str, Any]:
    """更新部署配置。"""
    updates: list[str] = []
    params: list[Any] = []

    if body.status is not None:
        updates.append("status = ?")
        params.append(body.status)
        if body.status == "stopped":
            updates.append("stopped_at = datetime('now')")

    if body.position_config is not None:
        updates.append("position_config = ?")
        params.append(_json.dumps(body.position_config, ensure_ascii=False))

    if body.status_tags is not None:
        updates.append("status_tags = ?")
        params.append(_json.dumps(body.status_tags, ensure_ascii=False))

    if body.user_notes is not None:
        updates.append("user_notes = ?")
        params.append(body.user_notes)

    if body.display_name is not None:
        updates.append("display_name = ?")
        params.append(body.display_name)

    promotion_requested = "research_promotion_id" in body.model_fields_set
    if promotion_requested and body.research_promotion_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "promotion_binding_immutable",
                "message": "A research promotion binding cannot be detached",
            },
        )

    if not updates and not promotion_requested:
        return {"data": {"updated": False, "detail": "没有需要更新的字段"}}

    try:
        async for conn in get_db("trading_sim"):
            # FIXED: reviewer issue #11 — 增加 user_id 过滤
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT * FROM deployments WHERE id = ? AND user_id = ?",
                    (deployment_id, user["id"]),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM deployments WHERE id = ?",
                    (deployment_id,),
                )
            deployment_row = await cursor.fetchone()
            if deployment_row is None:
                raise HTTPException(status_code=404, detail=f"部署不存在: {deployment_id}")

            deployment = dict(deployment_row)
            from backend.services.deployment_promotion import (
                DeploymentPromotionError,
                resolve_deployment_promotion,
                verify_deployment_promotion,
            )

            target_status = body.status or deployment["status"]
            if promotion_requested:
                if (
                    deployment.get("promotion_binding_hash") is not None
                    and int(deployment["research_promotion_id"])
                    != int(body.research_promotion_id)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "promotion_binding_immutable",
                            "message": (
                                "Deployment promotion binding cannot be "
                                "replaced"
                            ),
                        },
                    )
                try:
                    promotion_binding = await resolve_deployment_promotion(
                        promotion_id=int(body.research_promotion_id),
                        owner_user_id=int(deployment["user_id"]),
                        experiment_id=int(
                            deployment["source_experiment_id"]
                        ),
                        strategy_id=str(deployment["strategy_id"]),
                        params_hash=str(deployment["params_hash"]),
                        model_artifact_id=(
                            int(deployment["source_model_artifact_id"])
                            if deployment.get("source_model_artifact_id")
                            is not None
                            else None
                        ),
                    )
                except (DeploymentPromotionError, TypeError, ValueError) as exc:
                    detail = (
                        exc.detail()
                        if isinstance(exc, DeploymentPromotionError)
                        else {
                            "code": "promotion_binding_incomplete",
                            "message": (
                                "Deployment has no valid source experiment"
                            ),
                        }
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=detail,
                    ) from exc
                if deployment.get("promotion_binding_hash") is None:
                    binding_columns = {
                        "research_promotion_id": "promotion_id",
                        "promotion_version": "promotion_version",
                        "promotion_report_id": "report_id",
                        "promotion_report_hash": "report_hash",
                        "promotion_manifest_hash": "manifest_hash",
                        "promotion_model_artifact_id": "model_artifact_id",
                        "promotion_model_sha256": "model_sha256",
                        "promotion_evidence_hash": "model_evidence_hash",
                        "promotion_binding_hash": "binding_hash",
                    }
                    for column, key in binding_columns.items():
                        updates.append(f"{column} = ?")
                        params.append(promotion_binding[key])
                    deployment.update(
                        {
                            column: promotion_binding[key]
                            for column, key in binding_columns.items()
                        }
                    )
                elif target_status == "active":
                    try:
                        await verify_deployment_promotion(deployment)
                    except DeploymentPromotionError as exc:
                        raise HTTPException(
                            status_code=409,
                            detail=exc.detail(),
                        ) from exc
            elif target_status == "active":
                if deployment.get("promotion_binding_hash"):
                    try:
                        await verify_deployment_promotion(deployment)
                    except DeploymentPromotionError as exc:
                        raise HTTPException(
                            status_code=409,
                            detail=exc.detail(),
                        ) from exc
                else:
                    from backend.services.experiment_eligibility import (
                        PaperRiskBindingError,
                        load_experiment_eligibility,
                        verify_paper_risk_binding,
                    )

                    source_experiment_id = deployment.get(
                        "source_experiment_id"
                    )
                    if source_experiment_id is None:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "paper_source_experiment_required",
                                "message": "未绑定来源实验的旧部署不能激活",
                            },
                        )
                    try:
                        risk_snapshot = verify_paper_risk_binding(deployment)
                    except PaperRiskBindingError as exc:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "paper_research_risk_binding_invalid",
                                "message": "模拟部署风险快照完整性校验失败",
                            },
                        ) from exc
                    async for experiment_conn in get_db("experiment"):
                        eligibility = await load_experiment_eligibility(
                            experiment_conn,
                            experiment_id=int(source_experiment_id),
                            strategy_id=str(deployment["strategy_id"]),
                        )
                        cursor = await experiment_conn.execute(
                            "SELECT manifest_hash FROM research_run_manifests WHERE experiment_id=?",
                            (source_experiment_id,),
                        )
                        manifest_row = await cursor.fetchone()
                    if not eligibility.eligible:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "paper_source_experiment_ineligible",
                                "eligibility_code": eligibility.code,
                            },
                        )
                    if risk_snapshot is not None and (
                        manifest_row is None
                        or risk_snapshot.get("source_manifest_hash")
                        != manifest_row["manifest_hash"]
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "paper_source_manifest_binding_changed",
                                "message": "来源实验清单与部署风险快照不一致",
                            },
                        )

            if body.status == "stopped":
                cursor = await conn.execute(
                    """
                    SELECT p.id, p.name
                    FROM portfolio_allocations pa
                    JOIN portfolios p ON p.id = pa.portfolio_id
                    WHERE pa.deployment_id = ?
                      AND pa.target_weight_bps > 0
                      AND p.status != 'archived'
                    ORDER BY p.id
                    LIMIT 1
                    """,
                    (deployment_id,),
                )
                referenced_portfolio = await cursor.fetchone()
                if referenced_portfolio is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"部署仍被组合 {referenced_portfolio['name']} "
                            f"(#{referenced_portfolio['id']}) 引用，请先发布移出版本"
                        ),
                    )

            if not updates:
                return {
                    "data": {
                        "updated": False,
                        "detail": "Promotion binding is already current",
                    }
                }
            params.append(deployment_id)
            await conn.execute(
                f"UPDATE deployments SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新部署失败: {e}")

    return {"data": {"updated": True, "deployment_id": deployment_id}}


@router.delete("/deployments/{deployment_id}")
async def delete_deployment(
    deployment_id: int,
    user: dict[str, Any] = Depends(require_permission("trading:deploy")),
) -> dict[str, Any]:
    """删除部署。"""
    try:
        async for conn in get_db("trading_sim"):
            # FIXED: reviewer issue #11 — 增加 user_id 过滤
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM deployments WHERE id = ? AND user_id = ?",
                    (deployment_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT id FROM deployments WHERE id = ?", (deployment_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"部署不存在: {deployment_id}")

            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("DELETE FROM deployments WHERE id = ?", (deployment_id,))
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除部署失败: {e}")

    return {"data": {"deleted": True, "deployment_id": deployment_id}}


@router.put("/deployments/{deployment_id}/retrain")
async def trigger_retrain(
    deployment_id: int,
    user: dict[str, Any] = Depends(require_permission("trading:deploy")),
) -> dict[str, Any]:
    """手动触发重训练（提交后台任务）。"""
    try:
        async for conn in get_db("trading_sim"):
            # FIXED: reviewer issue #11 — 增加 user_id 过滤
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT * FROM deployments WHERE id = ? AND user_id = ?",
                    (deployment_id, user["id"]),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM deployments WHERE id = ?",
                    (deployment_id,),
                )
            row = await cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"部署不存在: {deployment_id}")

            if not row["requires_retraining"]:
                raise HTTPException(status_code=400, detail="该部署未启用重训练")
            deployment_owner_id = int(row["user_id"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询部署失败: {e}")

    broker = get_job_broker()
    try:
        # FIXED: reviewer issue #4 — submit() → submit_job()
        job_id = await broker.submit_job(
            job_type="retrain",
            params={
                "deployment_id": deployment_id,
                "user_id": deployment_owner_id,
                "requested_by_user_id": int(user["id"]),
            },
            user_id=user["id"],
            resource_type="deployment",
            resource_id=deployment_id,
            deduplicate_active=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"提交重训练任务失败: {e}")

    return {"data": {"deployment_id": deployment_id, "job_id": job_id}}


@router.get("/deployments/{deployment_id}/models")
async def get_deployment_models(
    deployment_id: int,
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """获取该部署的模型版本历史。"""
    try:
        async for conn in get_db("trading_sim"):
            # FIXED: reviewer issue #11 — 验证部署归属
            if not user.get("is_admin"):
                dep_cursor = await conn.execute(
                    "SELECT id FROM deployments WHERE id = ? AND user_id = ?",
                    (deployment_id, user["id"]),
                )
            else:
                dep_cursor = await conn.execute(
                    "SELECT id FROM deployments WHERE id = ?",
                    (deployment_id,),
                )
            if await dep_cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"部署不存在: {deployment_id}")

            cursor = await conn.execute(
                """
                SELECT * FROM model_version_history
                WHERE deployment_id = ?
                ORDER BY model_version DESC
                """,
                (deployment_id,),
            )
            rows = await cursor.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询模型版本失败: {e}")

    items: list[dict[str, Any]] = []
    for row in rows:
        d = redact_model_storage_paths(dict(row))
        d["train_metrics"] = _json.loads(d["train_metrics"]) if d.get("train_metrics") else {}
        d["feature_importance"] = _json.loads(d["feature_importance"]) if d.get("feature_importance") else {}
        items.append(d)

    return {"data": items}


@router.get("/deployments/{deployment_id}/model-lifecycle")
async def get_model_lifecycle(
    deployment_id: int,
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """Return redacted schedule, immutable versions, and retrain evidence."""
    try:
        async for conn in get_db("trading_sim"):
            if user.get("is_admin"):
                cursor = await conn.execute(
                    """
                    SELECT d.*,
                           (
                               SELECT MAX(COALESCE(a.completed_at, a.created_at))
                               FROM model_retrain_attempts a
                               WHERE a.deployment_id=d.id
                                 AND a.status='failed'
                           ) AS last_attempt_at
                    FROM deployments d
                    WHERE d.id=?
                    """,
                    (deployment_id,),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT d.*,
                           (
                               SELECT MAX(COALESCE(a.completed_at, a.created_at))
                               FROM model_retrain_attempts a
                               WHERE a.deployment_id=d.id
                                 AND a.status='failed'
                           ) AS last_attempt_at
                    FROM deployments d
                    WHERE d.id=? AND d.user_id=?
                    """,
                    (deployment_id, user["id"]),
                )
            deployment_row = await cursor.fetchone()
            if deployment_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"部署不存在: {deployment_id}",
                )
            version_cursor = await conn.execute(
                """
                SELECT * FROM model_version_history
                WHERE deployment_id=?
                ORDER BY model_version DESC, id DESC
                """,
                (deployment_id,),
            )
            versions = [dict(row) for row in await version_cursor.fetchall()]
            attempt_cursor = await conn.execute(
                """
                SELECT * FROM model_retrain_attempts
                WHERE deployment_id=?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (deployment_id,),
            )
            attempts = [dict(row) for row in await attempt_cursor.fetchall()]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"查询模型生命周期失败: {type(exc).__name__}",
        ) from exc

    deployment = dict(deployment_row)
    due = next_retrain_at(deployment)
    public_versions: list[dict[str, Any]] = []
    for item in versions:
        public = redact_model_storage_paths(item)
        public["train_metrics"] = parse_json_object(public.get("train_metrics"))
        public["feature_importance"] = parse_json_object(
            public.get("feature_importance")
        )
        public["validation_metrics"] = parse_json_object(
            public.get("validation_metrics")
        )
        public["manifest_verified"] = bool(
            public.get("retrain_manifest_hash")
            and public.get("model_sha256")
            and public.get("model_size")
            and public.get("status") == "promoted"
        )
        public.pop("retrain_manifest_json", None)
        public["failure"] = public_failure(public.pop("error", None))
        public_versions.append(public)

    public_attempts: list[dict[str, Any]] = []
    for item in attempts:
        item["validation_metrics"] = parse_json_object(
            item.get("validation_metrics")
        )
        item["manifest_verified"] = bool(
            item.get("retrain_manifest_hash")
            and item.get("model_sha256")
            and item.get("model_size")
            and item.get("status") == "promoted"
        )
        item.pop("retrain_manifest_json", None)
        item["failure"] = public_failure(item.pop("error", None))
        public_attempts.append(item)

    return {
        "data": {
            "deployment": {
                "id": int(deployment["id"]),
                "display_name": deployment.get("display_name"),
                "strategy_id": deployment["strategy_id"],
                "status": deployment["status"],
                "requires_retraining": bool(
                    deployment.get("requires_retraining")
                ),
                "retrain_frequency": deployment.get("retrain_frequency"),
                "current_model_version": int(
                    deployment.get("current_model_version") or 0
                ),
                "last_retrain_at": deployment.get("last_retrain_at"),
            },
            "schedule": {
                "enabled": bool(settings.MODEL_RETRAIN_AUTO_RUN),
                "eligible": bool(
                    deployment.get("requires_retraining")
                    and deployment.get("status") == "active"
                ),
                "next_retrain_at": due.isoformat() if due else None,
                "scan_minutes": max(
                    int(settings.MODEL_RETRAIN_SCAN_MINUTES),
                    1,
                ),
            },
            "versions": public_versions,
            "attempts": public_attempts,
            "safety": {
                "automatic_live_publish": False,
                "candidate_requires_validation": True,
                "failed_candidate_preserves_champion": True,
            },
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# 投资组合
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/portfolios", response_model=ApiResponse[list[PortfolioResponse]])
async def list_portfolios(
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """获取组合列表。"""
    try:
        async for conn in get_db("trading_sim"):
            # FIXED: reviewer issue #11 — 增加 user_id 过滤（非 admin 只能看自己的）
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT * FROM portfolios WHERE user_id = ? ORDER BY created_at DESC",
                    (user["id"],),
                )
            else:
                cursor = await conn.execute("SELECT * FROM portfolios ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            portfolio_ids = [int(row["id"]) for row in rows]
            allocation_rows = []
            if portfolio_ids:
                placeholders = ",".join("?" for _ in portfolio_ids)
                cursor = await conn.execute(
                    f"""
                    SELECT pa.*, d.display_name, d.strategy_id, d.status AS deployment_status
                    FROM portfolio_allocations pa
                    JOIN deployments d ON d.id = pa.deployment_id
                    WHERE pa.portfolio_id IN ({placeholders})
                    ORDER BY pa.portfolio_id, pa.deployment_id
                    """,
                    portfolio_ids,
                )
                allocation_rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询组合列表失败: {e}")

    allocations_by_portfolio: dict[int, list[dict[str, Any]]] = {}
    for allocation_row in allocation_rows:
        allocation = dict(allocation_row)
        allocations_by_portfolio.setdefault(
            int(allocation["portfolio_id"]), []
        ).append(allocation)

    items: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        relational = allocations_by_portfolio.get(int(d["id"]), [])
        d["allocations"] = relational or (
            _json.loads(d["allocations"]) if d.get("allocations") else []
        )
        d["cash_balance"] = (
            d.get("cash_balance")
            if d.get("cash_balance") is not None
            else d["total_capital"]
        )
        items.append(d)

    return {"data": items}


@router.post("/portfolios", response_model=ApiResponse[IdResponse])
async def create_portfolio(
    body: CreatePortfolioBody,
    user: dict[str, Any] = Depends(require_permission("trading:rebalance")),
) -> dict[str, Any]:
    """创建投资组合。"""
    from backend.services.allocations import canonicalize_allocations

    normalized, validation = canonicalize_allocations(
        _allocation_dicts(body.allocations),
        body.total_capital,
    )
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail={"allocation_errors": validation["errors"]})
    allocations_str = _json.dumps(normalized, ensure_ascii=False)

    try:
        async for conn in get_db("trading_sim"):
            await _validate_allocation_deployments(conn, normalized, user)
            await conn.execute("BEGIN")
            cursor = await conn.execute(
                """
                INSERT INTO portfolios
                    (user_id, name, total_capital, rebalance_frequency,
                     allocations, status, cash_balance, current_revision)
                VALUES (?, ?, ?, ?, ?, 'active', ?, 1)
                """,
                (
                    user["id"],
                    body.name,
                    body.total_capital,
                    body.rebalance_frequency,
                    allocations_str,
                    body.total_capital,
                ),
            )
            portfolio_id = cursor.lastrowid
            await _replace_portfolio_allocations(conn, portfolio_id, 1, normalized)
            await conn.execute(
                """
                INSERT INTO portfolio_versions
                    (portfolio_id, user_id, revision, allocations,
                     validation_result, status, effective_date, published_at)
                VALUES (?, ?, 1, ?, ?, 'published', date('now'), datetime('now'))
                """,
                (
                    portfolio_id,
                    user["id"],
                    allocations_str,
                    _json.dumps(validation, ensure_ascii=False),
                ),
            )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建组合失败: {e}")

    return {
        "data": {
            "portfolio_id": portfolio_id,
            "revision": 1,
            "validation": validation,
        }
    }


@router.put("/portfolios/{portfolio_id}")
async def update_portfolio(
    portfolio_id: int,
    body: UpdatePortfolioBody,
    user: dict[str, Any] = Depends(require_permission("trading:rebalance")),
) -> dict[str, Any]:
    """立即发布组合配置；需要审批预览时请使用 drafts 接口。"""
    if not any(
        value is not None
        for value in (
            body.name,
            body.total_capital,
            body.rebalance_frequency,
            body.allocations,
            body.status,
        )
    ):
        return {"data": {"updated": False}}

    try:
        async for conn in get_db("trading_sim"):
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT * FROM portfolios WHERE id = ? AND user_id = ?",
                    (portfolio_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,))
            current_row = await cursor.fetchone()
            if current_row is None:
                raise HTTPException(status_code=404, detail=f"组合不存在: {portfolio_id}")

            current = dict(current_row)
            total_capital = float(body.total_capital or current["total_capital"])
            updates: list[str] = []
            params: list[Any] = []
            for field, value in (
                ("name", body.name),
                ("total_capital", body.total_capital),
                ("rebalance_frequency", body.rebalance_frequency),
                ("status", body.status),
            ):
                if value is not None:
                    updates.append(f"{field} = ?")
                    params.append(value)

            validation = None
            revision = int(current.get("current_revision") or 0)
            normalized: list[dict[str, Any]] | None = None
            if body.allocations is not None:
                from backend.services.allocations import canonicalize_allocations

                normalized, validation = canonicalize_allocations(
                    _allocation_dicts(body.allocations),
                    total_capital,
                )
                if not validation["valid"]:
                    raise HTTPException(
                        status_code=422,
                        detail={"allocation_errors": validation["errors"]},
                    )
                await _validate_allocation_deployments(conn, normalized, user)
                revision += 1
                updates.extend(["allocations = ?", "current_revision = ?"])
                params.extend([
                    _json.dumps(normalized, ensure_ascii=False),
                    revision,
                ])

            updates.append("updated_at = datetime('now')")
            params.append(portfolio_id)
            await conn.execute("BEGIN")
            await conn.execute(
                f"UPDATE portfolios SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            if normalized is not None:
                await _replace_portfolio_allocations(
                    conn, portfolio_id, revision, normalized
                )
                await conn.execute(
                    """
                    INSERT INTO portfolio_versions
                        (portfolio_id, user_id, revision, allocations,
                         validation_result, status, effective_date, published_at)
                    VALUES (?, ?, ?, ?, ?, 'published', date('now'), datetime('now'))
                    """,
                    (
                        portfolio_id,
                        user["id"],
                        revision,
                        _json.dumps(normalized, ensure_ascii=False),
                        _json.dumps(validation, ensure_ascii=False),
                    ),
                )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新组合失败: {e}")

    return {
        "data": {
            "updated": True,
            "portfolio_id": portfolio_id,
            "revision": revision,
            "validation": validation,
        }
    }


@router.post("/portfolios/{portfolio_id}/validate")
async def validate_portfolio_allocations(
    portfolio_id: int,
    body: AllocationRequestBody,
    user: dict[str, Any] = Depends(require_permission("trading:rebalance")),
) -> dict[str, Any]:
    from backend.services.allocations import canonicalize_allocations

    async for conn in get_db("trading_sim"):
        query = "SELECT * FROM portfolios WHERE id=?"
        params: list[Any] = [portfolio_id]
        if not user.get("is_admin"):
            query += " AND user_id=?"
            params.append(user["id"])
        cursor = await conn.execute(query, params)
        portfolio = await cursor.fetchone()
        if portfolio is None:
            raise HTTPException(status_code=404, detail="组合不存在")
        normalized, validation = canonicalize_allocations(
            _allocation_dicts(body.allocations),
            float(portfolio["total_capital"]),
        )
        await _validate_allocation_deployments(conn, normalized, user)
    return {"data": {"allocations": normalized, "validation": validation}}


@router.post("/portfolios/{portfolio_id}/preview")
async def preview_portfolio_rebalance(
    portfolio_id: int,
    body: AllocationRequestBody,
    user: dict[str, Any] = Depends(require_permission("trading:rebalance")),
) -> dict[str, Any]:
    from backend.services.allocations import (
        build_rebalance_preview,
        canonicalize_allocations,
    )

    async for conn in get_db("trading_sim"):
        query = "SELECT * FROM portfolios WHERE id=?"
        params: list[Any] = [portfolio_id]
        if not user.get("is_admin"):
            query += " AND user_id=?"
            params.append(user["id"])
        cursor = await conn.execute(query, params)
        portfolio = await cursor.fetchone()
        if portfolio is None:
            raise HTTPException(status_code=404, detail="组合不存在")
        normalized, validation = canonicalize_allocations(
            _allocation_dicts(body.allocations),
            float(portfolio["total_capital"]),
        )
        await _validate_allocation_deployments(conn, normalized, user)
        cursor = await conn.execute(
            """
            SELECT deployment_id, SUM(market_value) AS market_value
            FROM position_snapshots
            WHERE portfolio_id=? AND date=(
                SELECT MAX(date) FROM position_snapshots WHERE portfolio_id=?
            )
            GROUP BY deployment_id
            """,
            (portfolio_id, portfolio_id),
        )
        current_values = {
            int(row["deployment_id"]): float(row["market_value"] or 0)
            for row in await cursor.fetchall()
            if row["deployment_id"] is not None
        }
    preview = build_rebalance_preview(
        normalized,
        float(portfolio["total_capital"]),
        current_values,
    )
    return {"data": {"validation": validation, **preview}}


@router.post("/portfolios/{portfolio_id}/drafts")
async def create_portfolio_draft(
    portfolio_id: int,
    body: AllocationRequestBody,
    user: dict[str, Any] = Depends(require_permission("trading:rebalance")),
) -> dict[str, Any]:
    from backend.services.allocations import canonicalize_allocations

    async for conn in get_db("trading_sim"):
        query = "SELECT * FROM portfolios WHERE id=?"
        params: list[Any] = [portfolio_id]
        if not user.get("is_admin"):
            query += " AND user_id=?"
            params.append(user["id"])
        cursor = await conn.execute(query, params)
        portfolio = await cursor.fetchone()
        if portfolio is None:
            raise HTTPException(status_code=404, detail="组合不存在")
        normalized, validation = canonicalize_allocations(
            _allocation_dicts(body.allocations),
            float(portfolio["total_capital"]),
        )
        await _validate_allocation_deployments(conn, normalized, user)
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM portfolio_versions WHERE portfolio_id=?",
            (portfolio_id,),
        )
        revision = int((await cursor.fetchone())[0])
        await conn.execute(
            """
            INSERT INTO portfolio_versions
                (portfolio_id, user_id, revision, allocations,
                 validation_result, status, effective_date)
            VALUES (?, ?, ?, ?, ?, 'draft', ?)
            """,
            (
                portfolio_id,
                user["id"],
                revision,
                _json.dumps(normalized, ensure_ascii=False),
                _json.dumps(validation, ensure_ascii=False),
                body.effective_date,
            ),
        )
        await conn.commit()
    return {
        "data": {
            "portfolio_id": portfolio_id,
            "revision": revision,
            "validation": validation,
        }
    }


@router.post("/portfolios/{portfolio_id}/drafts/{revision}/publish")
async def publish_portfolio_draft(
    portfolio_id: int,
    revision: int,
    user: dict[str, Any] = Depends(require_permission("trading:rebalance")),
) -> dict[str, Any]:
    async for conn in get_db("trading_sim"):
        query = """
            SELECT pv.*, p.user_id AS portfolio_user_id
            FROM portfolio_versions pv
            JOIN portfolios p ON p.id=pv.portfolio_id
            WHERE pv.portfolio_id=? AND pv.revision=? AND pv.status='draft'
        """
        params: list[Any] = [portfolio_id, revision]
        if not user.get("is_admin"):
            query += " AND p.user_id=?"
            params.append(user["id"])
        cursor = await conn.execute(query, params)
        draft = await cursor.fetchone()
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        validation = _json.loads(draft["validation_result"] or "{}")
        if not validation.get("valid"):
            raise HTTPException(status_code=422, detail="草稿校验未通过")
        allocations = _json.loads(draft["allocations"])
        await conn.execute("BEGIN")
        await _replace_portfolio_allocations(
            conn, portfolio_id, revision, allocations
        )
        await conn.execute(
            """
            UPDATE portfolios
            SET allocations=?, current_revision=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (draft["allocations"], revision, portfolio_id),
        )
        await conn.execute(
            """
            UPDATE portfolio_versions
            SET status='archived'
            WHERE portfolio_id=? AND status='published'
            """,
            (portfolio_id,),
        )
        await conn.execute(
            """
            UPDATE portfolio_versions
            SET status='published', published_at=datetime('now'),
                effective_date=COALESCE(effective_date, date('now'))
            WHERE portfolio_id=? AND revision=?
            """,
            (portfolio_id, revision),
        )
        await conn.commit()
    return {"data": {"portfolio_id": portfolio_id, "revision": revision, "published": True}}


@router.get("/portfolios/{portfolio_id}/versions")
async def list_portfolio_versions(
    portfolio_id: int,
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    async for conn in get_db("trading_sim"):
        query = """
            SELECT pv.* FROM portfolio_versions pv
            JOIN portfolios p ON p.id=pv.portfolio_id
            WHERE pv.portfolio_id=?
        """
        params: list[Any] = [portfolio_id]
        if not user.get("is_admin"):
            query += " AND p.user_id=?"
            params.append(user["id"])
        query += " ORDER BY pv.revision DESC"
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["allocations"] = _json.loads(item["allocations"])
        item["validation_result"] = _json.loads(item["validation_result"] or "{}")
        items.append(item)
    return {"data": items}


@router.get("/portfolios/{portfolio_id}/nav")
async def get_portfolio_nav(
    portfolio_id: int,
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    async for conn in get_db("trading_sim"):
        query = """
            SELECT n.* FROM nav_history n
            JOIN portfolios p ON p.id=n.portfolio_id
            WHERE n.portfolio_id=? AND n.deployment_id IS NULL
        """
        params: list[Any] = [portfolio_id]
        if not user.get("is_admin"):
            query += " AND p.user_id=?"
            params.append(user["id"])
        query += " ORDER BY n.date"
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
    return {"data": [dict(row) for row in rows]}


def _strategy_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate comparable sleeve metrics from cash-flow-adjusted daily returns."""
    import math
    import statistics

    if not rows:
        return {
            "cumulative_return": None,
            "annualized_return": None,
            "annualized_volatility": None,
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "calmar_ratio": None,
            "max_drawdown": None,
            "positive_day_ratio": None,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "transaction_cost": 0.0,
            "turnover": 0.0,
            "turnover_rate": None,
            "contribution_pnl": 0.0,
            "contribution_return": 0.0,
        }
    returns = [float(row.get("daily_return") or 0.0) for row in rows]
    cumulative_return = float(rows[-1].get("cumulative_return") or 0.0)
    annualized_return = (
        (1.0 + cumulative_return) ** (252 / len(rows)) - 1.0
        if cumulative_return > -1.0 else -1.0
    )
    volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else None
    sharpe = (
        statistics.mean(returns) / statistics.pstdev(returns) * math.sqrt(252)
        if len(returns) > 1 and statistics.pstdev(returns) > 0 else None
    )
    downside = [value for value in returns if value < 0]
    downside_deviation = (
        math.sqrt(sum(value * value for value in downside) / len(returns))
        if downside else 0.0
    )
    sortino = (
        statistics.mean(returns) / downside_deviation * math.sqrt(252)
        if downside_deviation > 0 else None
    )
    peak = 1.0
    max_drawdown = 0.0
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
        peak = max(peak, compounded)
        max_drawdown = min(max_drawdown, compounded / peak - 1.0)
    total_opening = sum(
        max(float(row.get("opening_equity") or 0.0) + float(row.get("net_flow") or 0.0), 0.0)
        for row in rows
    )
    turnover = sum(float(row.get("turnover") or 0.0) for row in rows)
    return {
        "cumulative_return": cumulative_return,
        "annualized_return": annualized_return,
        "annualized_volatility": volatility,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": annualized_return / abs(max_drawdown) if max_drawdown < 0 else None,
        "max_drawdown": max_drawdown,
        "positive_day_ratio": sum(value > 0 for value in returns) / len(returns),
        "realized_pnl": sum(float(row.get("realized_pnl") or 0.0) for row in rows),
        "unrealized_pnl": float(rows[-1].get("unrealized_pnl") or 0.0),
        "transaction_cost": sum(float(row.get("transaction_cost") or 0.0) for row in rows),
        "turnover": turnover,
        "turnover_rate": turnover / total_opening if total_opening else None,
        "contribution_pnl": sum(float(row.get("contribution_pnl") or 0.0) for row in rows),
        "contribution_return": sum(float(row.get("contribution_return") or 0.0) for row in rows),
    }


@router.get("/portfolios/{portfolio_id}/strategy-analytics")
async def get_portfolio_strategy_analytics(
    portfolio_id: int,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """Return per-deployment NAV, P&L attribution, and effectiveness metrics."""
    from datetime import date

    try:
        if start_date:
            date.fromisoformat(start_date)
        if end_date:
            date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD") from exc
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must not precede start_date")

    async for conn in get_db("trading_sim"):
        portfolio_query = "SELECT * FROM portfolios WHERE id=?"
        portfolio_params: list[Any] = [portfolio_id]
        if not user.get("is_admin"):
            portfolio_query += " AND user_id=?"
            portfolio_params.append(user["id"])
        cursor = await conn.execute(portfolio_query, portfolio_params)
        portfolio = await cursor.fetchone()
        if portfolio is None:
            raise HTTPException(status_code=404, detail="Portfolio not found")

        conditions = ["sn.portfolio_id=?"]
        params: list[Any] = [portfolio_id]
        if start_date:
            conditions.append("sn.date>=?")
            params.append(start_date)
        if end_date:
            conditions.append("sn.date<=?")
            params.append(end_date)
        cursor = await conn.execute(
            f"""
            SELECT sn.* FROM strategy_nav_history sn
            WHERE {' AND '.join(conditions)}
            ORDER BY sn.date, sn.deployment_id
            """,
            params,
        )
        nav_rows = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """
            SELECT d.id AS deployment_id, d.strategy_id, d.display_name,
                   d.status, d.source_experiment_id, d.params,
                   pa.target_weight_bps
            FROM deployments d
            LEFT JOIN portfolio_allocations pa
              ON pa.deployment_id=d.id AND pa.portfolio_id=?
            WHERE pa.portfolio_id=? OR EXISTS (
                SELECT 1 FROM strategy_nav_history sn
                WHERE sn.portfolio_id=? AND sn.deployment_id=d.id
            )
            ORDER BY d.id
            """,
            (portfolio_id, portfolio_id, portfolio_id),
        )
        deployment_rows = [dict(row) for row in await cursor.fetchall()]
        portfolio_conditions = ["portfolio_id=?", "deployment_id IS NULL"]
        portfolio_params = [portfolio_id]
        if start_date:
            portfolio_conditions.append("date>=?")
            portfolio_params.append(start_date)
        if end_date:
            portfolio_conditions.append("date<=?")
            portfolio_params.append(end_date)
        cursor = await conn.execute(
            f"""
            SELECT date, COALESCE(total_equity, nav) AS total_equity, daily_return
            FROM nav_history WHERE {' AND '.join(portfolio_conditions)} ORDER BY date
            """,
            portfolio_params,
        )
        portfolio_nav = [dict(row) for row in await cursor.fetchall()]

    rows_by_deployment: dict[int, list[dict[str, Any]]] = {}
    rows_by_date: dict[str, list[dict[str, Any]]] = {}
    peaks: dict[int, float] = {}
    target_weights = {
        int(row["deployment_id"]): int(row.get("target_weight_bps") or 0)
        for row in deployment_rows
    }
    portfolio_by_date = {row["date"]: row for row in portfolio_nav}
    previous_portfolio_equity = float(portfolio["total_capital"])
    portfolio_daily_pnl: dict[str, float] = {}
    for row in portfolio_nav:
        equity = float(row["total_equity"])
        portfolio_daily_pnl[row["date"]] = equity - previous_portfolio_equity
        previous_portfolio_equity = equity
    for row in nav_rows:
        deployment_id = int(row["deployment_id"])
        equity = float(row["total_equity"])
        peak = max(peaks.get(deployment_id, equity), equity)
        peaks[deployment_id] = peak
        row["drawdown"] = equity / peak - 1.0 if peak else 0.0
        row["target_weight_pct"] = target_weights.get(deployment_id, 0) / 100.0
        portfolio_equity = float(portfolio_by_date.get(row["date"], {}).get("total_equity") or 0.0)
        row["actual_weight_pct"] = equity / portfolio_equity * 100 if portfolio_equity else 0.0
        rows_by_deployment.setdefault(deployment_id, []).append(row)
        rows_by_date.setdefault(row["date"], []).append(row)

    strategies = []
    for deployment in deployment_rows:
        deployment_id = int(deployment["deployment_id"])
        deployment["params"] = _json.loads(deployment.get("params") or "{}")
        deployment["target_weight_bps"] = int(deployment.get("target_weight_bps") or 0)
        deployment["metrics"] = _strategy_metrics(rows_by_deployment.get(deployment_id, []))
        strategies.append(deployment)
    series = []
    for current_date in sorted(rows_by_date):
        portfolio_row = portfolio_by_date.get(current_date, {})
        series.append(
            {
                "date": current_date,
                "portfolio_total_equity": portfolio_row.get("total_equity"),
                "portfolio_daily_pnl": portfolio_daily_pnl.get(current_date),
                "portfolio_daily_return": portfolio_row.get("daily_return"),
                "strategies": rows_by_date[current_date],
            }
        )
    dates = sorted(rows_by_date)
    return {
        "data": {
            "portfolio_id": portfolio_id,
            "date_range": {
                "start_date": dates[0] if dates else None,
                "end_date": dates[-1] if dates else None,
                "trading_days": len(dates),
            },
            "strategies": strategies,
            "series": series,
        }
    }


@router.get("/portfolios/{portfolio_id}/overview")
async def get_portfolio_overview(
    portfolio_id: int,
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """Return the key paper-portfolio KPIs and current strategy sleeves."""
    import math
    import statistics

    async for conn in get_db("trading_sim"):
        query = "SELECT * FROM portfolios WHERE id=?"
        params: list[Any] = [portfolio_id]
        if not user.get("is_admin"):
            query += " AND user_id=?"
            params.append(user["id"])
        cursor = await conn.execute(query, params)
        portfolio_row = await cursor.fetchone()
        if portfolio_row is None:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        portfolio = dict(portfolio_row)

        cursor = await conn.execute(
            """
            SELECT * FROM nav_history
            WHERE portfolio_id=? AND deployment_id IS NULL
            ORDER BY date
            """,
            (portfolio_id,),
        )
        nav_rows = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """
            SELECT pa.*, d.display_name, d.strategy_id, d.status,
                   d.source_experiment_id, d.params,
                   COALESCE((
                       SELECT SUM(ps.market_value) FROM position_snapshots ps
                       WHERE ps.portfolio_id=pa.portfolio_id
                         AND ps.deployment_id=pa.deployment_id
                         AND ps.date=(SELECT MAX(ps2.date) FROM position_snapshots ps2
                                      WHERE ps2.portfolio_id=pa.portfolio_id)
                   ), 0) AS current_market_value,
                   COALESCE((
                       SELECT SUM(ps.unrealized_pnl) FROM position_snapshots ps
                       WHERE ps.portfolio_id=pa.portfolio_id
                         AND ps.deployment_id=pa.deployment_id
                         AND ps.date=(SELECT MAX(ps2.date) FROM position_snapshots ps2
                                      WHERE ps2.portfolio_id=pa.portfolio_id)
                   ), 0) AS unrealized_pnl,
                   (SELECT COUNT(*) FROM position_snapshots ps
                    WHERE ps.portfolio_id=pa.portfolio_id
                      AND ps.deployment_id=pa.deployment_id
                      AND ps.date=(SELECT MAX(ps2.date) FROM position_snapshots ps2
                                   WHERE ps2.portfolio_id=pa.portfolio_id)) AS position_count,
                   (SELECT COUNT(*) FROM orders o
                    WHERE o.portfolio_id=pa.portfolio_id
                      AND o.deployment_id=pa.deployment_id
                      AND o.status='filled') AS filled_orders
            FROM portfolio_allocations pa
            JOIN deployments d ON d.id=pa.deployment_id
            WHERE pa.portfolio_id=?
            ORDER BY pa.deployment_id
            """,
            (portfolio_id,),
        )
        strategy_rows = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """
            SELECT o.*, d.display_name AS deployment_name
            FROM orders o JOIN deployments d ON d.id=o.deployment_id
            WHERE o.portfolio_id=?
            ORDER BY o.date DESC, o.id DESC LIMIT 20
            """,
            (portfolio_id,),
        )
        recent_orders = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """
            SELECT ps.*, d.display_name AS deployment_name
            FROM position_snapshots ps JOIN deployments d ON d.id=ps.deployment_id
            WHERE ps.portfolio_id=?
              AND ps.date=(SELECT MAX(ps2.date) FROM position_snapshots ps2 WHERE ps2.portfolio_id=?)
            ORDER BY ps.market_value DESC LIMIT 50
            """,
            (portfolio_id, portfolio_id),
        )
        positions = [dict(row) for row in await cursor.fetchall()]

    initial_capital = float(portfolio["total_capital"])
    latest = nav_rows[-1] if nav_rows else None
    previous = nav_rows[-2] if len(nav_rows) > 1 else None
    current_equity = float(latest["total_equity"] or latest["nav"]) if latest else initial_capital
    previous_equity = float(previous["total_equity"] or previous["nav"]) if previous else initial_capital
    daily_pnl = current_equity - previous_equity
    daily_returns = [float(row["daily_return"]) for row in nav_rows if row.get("daily_return") is not None]
    peak = 0.0
    max_drawdown = 0.0
    for row in nav_rows:
        equity = float(row["total_equity"] or row["nav"])
        peak = max(peak, equity)
        if peak:
            max_drawdown = min(max_drawdown, equity / peak - 1)
    sharpe = None
    if len(daily_returns) > 1 and statistics.pstdev(daily_returns) > 0:
        sharpe = statistics.mean(daily_returns) / statistics.pstdev(daily_returns) * math.sqrt(252)

    strategies = []
    for row in strategy_rows:
        row["params"] = _json.loads(row["params"] or "{}")
        row["target_capital"] = round(initial_capital * int(row["target_weight_bps"]) / 10_000, 2)
        row["actual_weight_pct"] = round(float(row["current_market_value"]) / current_equity * 100, 4) if current_equity else 0
        strategies.append(row)
    return {
        "data": {
            "portfolio_id": portfolio_id,
            "name": portfolio["name"],
            "status": portfolio["status"],
            "current_revision": portfolio.get("current_revision") or 0,
            "rebalance_frequency": portfolio["rebalance_frequency"],
            "start_date": nav_rows[0]["date"] if nav_rows else None,
            "latest_date": latest["date"] if latest else None,
            "trading_days": len(nav_rows),
            "initial_capital": initial_capital,
            "current_equity": round(current_equity, 2),
            "cash_balance": round(float(portfolio.get("cash_balance") or 0), 2),
            "daily_pnl": round(daily_pnl, 2),
            "daily_return": float(latest["daily_return"]) if latest and latest.get("daily_return") is not None else 0,
            "cumulative_return": current_equity / initial_capital - 1 if initial_capital else 0,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "strategies": strategies,
            "recent_orders": recent_orders,
            "positions": positions,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# 持仓
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/positions", response_model=ApiResponse[list[PositionResponse]])
async def get_positions(
    portfolio_id: int | None = Query(None),
    date: str | None = Query(None),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """获取当前持仓。"""
    conditions: list[str] = []
    params: list[Any] = []

    if portfolio_id:
        conditions.append("ps.portfolio_id = ?")
        params.append(portfolio_id)

    # FIXED: reviewer issue #11 — 验证组合归属（通过 portfolio 表的 user_id）
    if not user.get("is_admin"):
        conditions.append(
            "ps.portfolio_id IN (SELECT id FROM portfolios WHERE user_id = ?)"
        )
        params.append(user["id"])
    if date:
        conditions.append("ps.date = ?")
        params.append(date)
    else:
        # 默认最新日期
        conditions.append(
            """
            ps.date = (
                SELECT MAX(ps2.date) FROM position_snapshots ps2
                WHERE ps2.portfolio_id = ps.portfolio_id
            )
            """
        )

    where = "WHERE " + " AND ".join(conditions)

    try:
        async for conn in get_db("trading_sim"):
            cursor = await conn.execute(
                f"""
                SELECT ps.*, d.display_name AS deployment_name,
                       d.strategy_id, ps.code AS name
                FROM position_snapshots ps
                LEFT JOIN deployments d ON d.id = ps.deployment_id
                {where}
                ORDER BY ps.market_value DESC
                """,
                params,
            )
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询持仓失败: {e}")

    return {"data": [dict(r) for r in rows]}


# ═══════════════════════════════════════════════════════════════════════════
# 信号
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/signals", response_model=ApiResponse[list[SignalResponse]])
async def get_signals(
    deployment_id: int | None = Query(None),
    date: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """获取信号列表。"""
    conditions: list[str] = []
    params: list[Any] = []

    if deployment_id:
        conditions.append("s.deployment_id = ?")
        params.append(deployment_id)
    if date:
        conditions.append("s.date = ?")
        params.append(date)

    # FIXED: reviewer issue #11 — 验证部署归属（通过 deployments 表的 user_id）
    if not user.get("is_admin"):
        conditions.append(
            "s.deployment_id IN (SELECT id FROM deployments WHERE user_id = ?)"
        )
        params.append(user["id"])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async for conn in get_db("trading_sim"):
            cursor = await conn.execute(
                f"""
                SELECT s.*, d.display_name AS deployment_name
                FROM daily_signals s
                LEFT JOIN deployments d ON d.id=s.deployment_id
                {where}
                ORDER BY s.date DESC, s.score DESC LIMIT ?
                """,
                params + [limit],
            )
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询信号失败: {e}")

    return {"data": [dict(r) for r in rows]}


# ═══════════════════════════════════════════════════════════════════════════
# 订单
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/orders", response_model=ApiResponse[Page[OrderResponse]])
async def get_orders(
    deployment_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """获取订单记录。"""
    conditions: list[str] = []
    params: list[Any] = []

    if deployment_id:
        conditions.append("o.deployment_id = ?")
        params.append(deployment_id)

    # FIXED: reviewer issue #11 — 验证部署归属（通过 deployments 表的 user_id）
    if not user.get("is_admin"):
        conditions.append(
            "o.deployment_id IN (SELECT id FROM deployments WHERE user_id = ?)"
        )
        params.append(user["id"])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit

    try:
        async for conn in get_db("trading_sim"):
            cursor = await conn.execute(
                f"SELECT COUNT(*) as cnt FROM orders o {where}",
                params,
            )
            total_row = await cursor.fetchone()
            total = total_row["cnt"] if total_row else 0

            cursor = await conn.execute(
                f"""
                SELECT o.*, d.display_name AS deployment_name
                FROM orders o
                LEFT JOIN deployments d ON d.id=o.deployment_id
                {where}
                ORDER BY o.date DESC, o.id DESC LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            )
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询订单失败: {e}")

    return {
        "data": {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "limit": limit,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# 模拟执行
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/simulate/run", response_model=ApiResponse[IdResponse])
async def run_simulation(
    body: SimulationRunBody | None = None,
    user: dict[str, Any] = Depends(require_permission("trading:execute")),
) -> dict[str, Any]:
    """触发每日模拟执行（提交后台任务）。"""
    from backend.data.pit_runtime import PitRuntimeDataError
    from backend.services.simulation import require_simulation_pit_readiness

    requested_date = body.date if body else None
    if requested_date is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pit_simulation_date_required",
                "message": "PIT-only 模拟必须显式选择已完成交易日",
            },
        )
    try:
        await require_simulation_pit_readiness(
            user_id=int(user["id"]),
            start_date=requested_date,
            end_date=requested_date,
            portfolio_id=body.portfolio_id if body else None,
        )
    except PitRuntimeDataError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    broker = get_job_broker()
    try:
        # FIXED: reviewer issue #4 — submit() → submit_job()
        job_id = await broker.submit_job(
            job_type="daily_simulation",
            params={
                "user_id": user["id"],
                "date": body.date if body else None,
                "portfolio_id": body.portfolio_id if body else None,
            },
            user_id=user["id"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"提交模拟任务失败: {e}")

    return {"data": {"job_id": job_id}}


@router.post("/simulate/backfill", response_model=ApiResponse[IdResponse])
async def backfill_simulation(
    body: SimulationBackfillBody,
    user: dict[str, Any] = Depends(require_permission("trading:execute")),
) -> dict[str, Any]:
    """Replay common executable dates for the selected portfolio pools."""
    from backend.data.pit_runtime import PitRuntimeDataError
    from backend.services.simulation import require_simulation_pit_readiness

    try:
        await require_simulation_pit_readiness(
            user_id=int(user["id"]),
            start_date=body.start_date,
            end_date=body.end_date,
            portfolio_id=body.portfolio_id,
        )
    except PitRuntimeDataError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    broker = get_job_broker()
    job_id = await broker.submit_job(
        job_type="simulation_backfill",
        params={
            "user_id": user["id"],
            "start_date": body.start_date,
            "end_date": body.end_date,
            "portfolio_id": body.portfolio_id,
            "restart": body.restart,
        },
        user_id=user["id"],
    )
    return {"data": {"job_id": job_id}}


def _simulation_calendar_cache_error(
    *,
    status_code: int,
    code: str,
    detail: str,
    action: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": detail,
            "code": code,
            "pool_id": "csi500",
            "action": action,
        },
    )


def _simulation_calendar_integrity_error() -> JSONResponse:
    return _simulation_calendar_cache_error(
        status_code=409,
        code="simulation_calendar_cache_integrity_invalid",
        detail="中证500行情缓存完整性校验失败，请先在数据中心受控重建",
        action="refresh_in_data_center",
    )


@router.get("/simulate/calendar", response_model=None)
async def get_simulation_calendar(
    portfolio_id: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any] | JSONResponse:
    """Describe dates executable by the selected portfolio's actual pools."""
    import pandas as pd

    if portfolio_id is None:
        # Backward-compatible platform calendar for clients that have not yet
        # selected a portfolio. New clients always pass the selected id.
        from backend.data.pit_runtime import require_pit_runtime_input

        try:
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            inspected = await require_pit_runtime_input(
                pool_id="csi500",
                required_start=(
                    pd.Timestamp(today) - pd.Timedelta(days=370)
                ).strftime("%Y-%m-%d"),
                required_end=today,
                purpose="execution",
            )
            pivot = inspected.market.frame
        except Exception:
            return _simulation_calendar_integrity_error()
        if pivot is None or pivot.empty:
            return _simulation_calendar_cache_error(
                status_code=404,
                code="simulation_calendar_cache_missing",
                detail="中证500行情缓存尚未下载，请先在数据中心更新数据",
                action="update_in_data_center",
            )
        try:
            dates = sorted(pivot.index)
            if (
                not dates
                or bool(getattr(pivot.index, "hasnans", True))
                or not bool(getattr(pivot.index, "is_unique", False))
            ):
                raise ValueError("invalid simulation calendar index")
            formatted_dates = [value.strftime("%Y-%m-%d") for value in dates]
            if len(set(formatted_dates)) != len(formatted_dates):
                raise ValueError("duplicate simulation calendar dates")
        except Exception:
            return _simulation_calendar_integrity_error()
        suggested_index = max(0, len(formatted_dates) - 20)
        return {
            "data": {
                "pool_id": "csi500",
                "min_date": formatted_dates[0],
                "max_date": formatted_dates[-1],
                "suggested_start": formatted_dates[suggested_index],
                "trading_days": len(formatted_dates),
                "trust_tier": "governed_production_pit",
                "warning_severity": "none",
                "live_eligible": False,
            }
        }

    from backend.services.simulation import (
        PortfolioSimulationScopeError,
        _load_pivot,
        simulation_pool_bindings,
    )

    try:
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        required_start = (
            pd.Timestamp(today) - pd.Timedelta(days=370)
        ).strftime("%Y-%m-%d")
        bindings = await simulation_pool_bindings(
            int(user["id"]), portfolio_id
        )
        shared_cache: dict[str, pd.DataFrame] = {}
        date_sets: list[set[str]] = []
        for binding in bindings:
            if binding["generation_id"]:
                pivot = await _load_pivot(
                    str(binding["pool_id"]),
                    today,
                    shared_cache,
                    required_start=required_start,
                    generation_id=binding["generation_id"],
                )
            else:
                from backend.data.pit_runtime import require_pit_runtime_input

                inspected = await require_pit_runtime_input(
                    pool_id=str(binding["pool_id"]),
                    required_start=required_start,
                    required_end=today,
                    purpose="execution",
                    require_benchmark=False,
                )
                pivot = inspected.market.frame
            if pivot.empty or bool(getattr(pivot.index, "hasnans", True)):
                raise ValueError("invalid simulation calendar index")
            date_sets.append(
                {value.strftime("%Y-%m-%d") for value in pivot.index}
            )
        formatted_dates = sorted(set.intersection(*date_sets))
        if not formatted_dates:
            raise ValueError("portfolio pools have no common trading date")
    except PortfolioSimulationScopeError:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "模拟组合不存在、未启用或不属于当前用户",
                "code": "simulation_portfolio_not_found",
                "portfolio_id": portfolio_id,
                "action": "select_owned_active_portfolio",
            },
        )
    except Exception:
        pool_ids = sorted(
            {str(item["pool_id"]) for item in locals().get("bindings", [])}
        )
        return JSONResponse(
            status_code=409,
            content={
                "detail": "模拟组合实际股票池的数据完整性校验失败",
                "code": "simulation_calendar_pool_integrity_invalid",
                "pool_ids": pool_ids,
                "action": "refresh_selected_portfolio_pools",
            },
        )
    suggested_index = max(0, len(formatted_dates) - 20)
    generation_ids = sorted(
        {
            str(item["generation_id"])
            for item in bindings
            if item["generation_id"]
        }
    )
    warnings = ["live_trading_not_eligible"]
    if generation_ids:
        warnings.extend(
            [
                "single_source_tushare_research",
                "paper_execution_uses_adjusted_price_compatibility",
            ]
        )
    if any(item["generation_id"] is None for item in bindings):
        warnings.append("legacy_deployment_uses_legacy_cache_binding")
    return {
        "data": {
            "pool_id": "+".join(sorted({str(item["pool_id"]) for item in bindings})),
            "pool_ids": sorted({str(item["pool_id"]) for item in bindings}),
            "generation_ids": generation_ids,
            "min_date": formatted_dates[0],
            "max_date": formatted_dates[-1],
            "suggested_start": formatted_dates[suggested_index],
            "trading_days": len(formatted_dates),
            "trust_tier": (
                "conditional_personal_research"
                if generation_ids
                else "legacy_compatibility"
            ),
            "warnings": warnings,
            "warning_severity": "high" if warnings else "none",
            "live_eligible": False,
        }
    }


@router.get("/simulate/status")
async def get_simulation_status(
    portfolio_id: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """获取模拟状态。"""
    broker = get_job_broker()
    try:
        jobs = await broker.list_jobs()
        candidates = [
            item
            for item in jobs
            if item.get("job_type") in {"daily_simulation", "simulation_backfill"}
            and int(item.get("user_id") or 0) == int(user["id"])
            and (
                portfolio_id is None
                or int((item.get("params") or {}).get("portfolio_id") or 0)
                == portfolio_id
            )
        ]
        status_info = max(
            (item for item in candidates if item),
            key=lambda item: int(item.get("id") or 0),
            default=None,
        )
    except Exception:
        status_info = None

    return {"data": status_info or {"status": "not_started"}}


@router.get("/simulate/schedule")
async def get_simulation_schedule(
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    """Expose the automatic EOD schedule so the UI can explain data freshness."""
    from backend.config import settings

    return {
        "data": {
            "enabled": settings.PAPER_SIMULATION_AUTO_RUN,
            "run_time": settings.PAPER_SIMULATION_RUN_TIME,
            "timezone": settings.PAPER_SIMULATION_TIMEZONE,
            "refresh_data": settings.PAPER_SIMULATION_REFRESH_DATA,
            "scope": "active portfolios",
        }
    }


@router.get("/simulate/runs")
async def list_simulation_runs(
    limit: int = Query(20, ge=1, le=100),
    portfolio_id: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(require_permission("trading:read")),
) -> dict[str, Any]:
    async for conn in get_db("trading_sim"):
        if portfolio_id is not None:
            cursor = await conn.execute(
                """
                SELECT * FROM simulation_runs
                WHERE user_id=? AND portfolio_id=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (user["id"], portfolio_id, limit),
            )
        elif user.get("is_admin"):
            cursor = await conn.execute(
                "SELECT * FROM simulation_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT * FROM simulation_runs
                WHERE user_id=? ORDER BY created_at DESC LIMIT ?
                """,
                (user["id"], limit),
            )
        rows = await cursor.fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["summary"] = _json.loads(item["summary"] or "{}")
        items.append(item)
    return {"data": items}
